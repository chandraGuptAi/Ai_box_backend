"""Backend-owned shipment fetch from the BigShip order-list API (``source="shipment_api"``).

Confirmed from the existing Angular integration (ai-report.service.ts / ai-box.utils.ts):
- POST ``{base}/api/get-order-list`` with the logged-in user's ``Authorization: Bearer <token>``;
  the token decides which seller's shipments are returned (seller scope is enforced upstream).
- ``type`` is the status filter (SHIPMENT_STATUS_TYPES below, 0 = all statuses).
- ``fromDate``/``toDate`` are the selected local calendar days as IST "YYYY-MM-DD HH:MM:SS",
  start inclusive at local midnight, end exclusive at the following local midnight.
- Rows: ``data.getOrderList``; matching total: ``data.totalRecords`` (never ``data.total``).

Supplied with the task, not used by Angular: ``date_filter_type`` (e.g. "manifest_datetime").
``only_order_stats`` is always 0 because its behaviour is unconfirmed.

The fetch reads the matching total first, then sizes the pages from that total (about one page per
concurrent request, never below SHIPMENT_API_MIN_PAGE_SIZE) and reads them concurrently. A page the
server cannot serve (5xx or timeout) is split in half and read again, so no fixed page size is needed.
A server that clamps the page size is detected and read again with the observed size; repeated,
empty or overlapping pages and a changing total stop the fetch. Nothing is ever truncated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.analysis.completeness import _identity
from app.config import Settings, settings
from app.errors import request_id_var, report_id_var
from app.errors import (IncompleteSourceError, NoMatchingRecordsError, ShipmentAuthInvalidError,
                        ShipmentAuthRequiredError, ShipmentSourceNotConfiguredError,
                        SourceChangedError, SourceTooLargeError, UnsupportedFilterError, UpstreamAuthError,
                        UpstreamForbiddenError, UpstreamInvalidResponseError, UpstreamPaginationError,
                        UpstreamRateLimitedError, UpstreamRejectedError, UpstreamTimeoutError,
                        UpstreamUnavailableError)
from app.prompts.shipment_prompt import _flatten

logger = logging.getLogger("ai_box.shipment_source")

# Label -> /api/get-order-list `type`, exactly as the Angular status selector sends it.
SHIPMENT_STATUS_TYPES: dict[str, int] = {
    "All Statuses": 0, "Pickup Scheduled": 2, "Not Picked": 3, "In-Transit": 5, "Out For Delivery": 6,
    "Delivered": 7, "Undelivered": 8, "RTO Intransit": 9, "RTO Delivered": 10, "Cancelled": 4, "Lost": 11,
}
_STATUS_KEYS = {re.sub(r"[\s_-]+", " ", label.lower()): label for label in SHIPMENT_STATUS_TYPES}
_STATUS_KEYS.update({"all": "All Statuses", "": "All Statuses"})
# Account-level counters the same response carries (context only; never used as the matching total).
ACCOUNT_COUNTER_KEYS = ("all_orders", "pickup_scheduled", "not_pickup", "in_transit", "out_for_delivery", "delivered",
                        "rto_in_transit", "rto_delivered", "undelivered", "cancel", "lost")
# Opaque token (JWT, Sanctum "id|secret", random...): any visible ASCII, no spaces or control characters.
_TOKEN = re.compile(r"^[!-~]{1,8192}$")
_PLACEHOLDERS = {"null", "undefined", "bearer"}  # what a browser sends when no session is stored


# --- Filter adapter ------------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceQuery:
    """The one upstream filter set; every request of a fetch sends exactly these values."""
    status_type: int
    status_label: str
    from_date: str
    to_date: str                      # exclusive
    date_filter_type: str
    segment_type: str
    timezone: str

    def body(self, page: int, per_page: int) -> dict[str, Any]:
        return {"type": self.status_type, "page": page, "perPage": per_page, "search": "",
                "search_by_courier": "", "search_by_order_id": "", "search_awb_number": "",
                "search_lrn_number": "", "search_by_cust_id": "", "search_by_company_name": "",
                "search_by_seller_email": "", "only_order_stats": 0, "segment_type": self.segment_type,
                "fromDate": self.from_date, "toDate": self.to_date, "date_filter_type": self.date_filter_type}

    def describe(self) -> dict[str, Any]:
        return {"type": self.status_type, "status": self.status_label, "fromDate": self.from_date,
                "toDate": self.to_date, "toDate_exclusive": True, "date_filter_type": self.date_filter_type,
                "segment_type": self.segment_type, "timezone": self.timezone, "request_timezone": "Asia/Kolkata"}


def status_label(value: str | None) -> str:
    """The supported label for a selected status, or UnsupportedFilterError."""
    key = re.sub(r"[\s_-]+", " ", (value or "").strip().lower())
    if key not in _STATUS_KEYS:
        raise UnsupportedFilterError(f"The status '{value}' is not supported for reports.",
                                     details={"field": "status", "supported": list(SHIPMENT_STATUS_TYPES)})
    return _STATUS_KEYS[key]


def ist_boundary(day: date, zone: ZoneInfo) -> str:
    """Local midnight starting ``day`` as the upstream IST "YYYY-MM-DD HH:MM:SS" string."""
    return datetime.combine(day, dtime(0), zone).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")


def build_query(filters: Any, config: Settings = settings, today: date | None = None) -> SourceQuery:
    """Validate the selected filters and convert them once into the upstream request values."""
    if filters.date_field not in config.shipment_api_date_fields:
        raise UnsupportedFilterError(
            f"Reports cannot filter shipments by '{filters.date_field}'.",
            details={"field": "date_field", "supported": list(config.shipment_api_date_fields)})
    try:
        zone = ZoneInfo(filters.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise UnsupportedFilterError(f"Unknown timezone '{filters.timezone}'.", details={"field": "timezone"}) from None
    start, end = filters.from_date, filters.to_date
    today = today or datetime.now(zone).date()
    if end > today:
        raise UnsupportedFilterError("The selected dates cannot be in the future.",
                                     details={"field": "to_date", "latest": today.isoformat()})
    days = (end - start).days + 1
    if days > config.shipment_api_max_range_days:
        raise UnsupportedFilterError(
            f"The selected range is {days} days; reports support at most {config.shipment_api_max_range_days}. "
            "Select a shorter date range.", details={"field": "from_date", "max_days": config.shipment_api_max_range_days})
    label = status_label(filters.status)
    return SourceQuery(status_type=SHIPMENT_STATUS_TYPES[label], status_label=label,
                       from_date=ist_boundary(start, zone), to_date=ist_boundary(end + timedelta(days=1), zone),
                       date_filter_type=filters.date_field, segment_type=config.shipment_api_segment_type,
                       timezone=filters.timezone)


def delegated_credential(header: str | None) -> str:
    """``Authorization`` value for the shipment API from the caller's X-Shipment-Authorization header.

    The token is opaque and forwarded unchanged; it is returned to the caller's request only (never stored,
    logged or set as a client default), and sent only to the configured shipment API host.
    """
    value = (header or "").strip()
    if not value:
        raise ShipmentAuthRequiredError(
            "Your shipment service session was not sent with the request. Log in again and retry.")
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not _TOKEN.match(token) or token.lower() in _PLACEHOLDERS:
        raise ShipmentAuthInvalidError(
            "Your shipment service session could not be read. Log in again and retry.",
            details={"header": "X-Shipment-Authorization", "expected": "Bearer <token>"})
    return f"Bearer {token}"


# --- Fetch ---------------------------------------------------------------------------------------

@dataclass
class Page:
    number: int
    per_page: int
    rows: list[dict[str, Any]]
    total: int
    summary: dict[str, int]


class _Clamped(Exception):
    """The server returned fewer rows than asked without being at the end: it clamps perPage."""

    def __init__(self, rows: int) -> None:
        super().__init__(rows)
        self.rows = rows


class _WindowMismatch(Exception):
    """Date-window totals did not add up to the range total; the range is read by pages instead."""

    def __init__(self, found: int) -> None:
        super().__init__(found)
        self.found = found


async def _all(coroutines):
    """gather() that cancels the remaining requests as soon as one fails."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


_STAMP = "%Y-%m-%d %H:%M:%S"
_SPLIT_ERRORS = (UpstreamUnavailableError, UpstreamTimeoutError)
_MIN_SPLIT_ROWS = 25      # a failing page is not split below this many rows
_PAGE_STEP = 16           # page sizes are multiples of this, so a failing page halves exactly


@dataclass
class FetchResult:
    rows: list[dict[str, Any]]
    total: int
    account_summary: dict[str, int]
    fetched_at: str
    requests: int
    page_size: int
    pages: int
    attempts: int
    page_size_clamped: bool
    query: dict[str, Any] = field(default_factory=dict)

    def source_info(self) -> dict[str, Any]:
        return {"source": "shipment_api", "fetched_at": self.fetched_at, "upstream_requests": self.requests,
                "page_size": self.page_size, "pages": self.pages, "fetch_attempts": self.attempts,
                "page_size_clamped": self.page_size_clamped, "source_snapshot": None,
                "consistency": "matching total unchanged across every request; the source offers no snapshot",
                "upstream_query": self.query}


class ShipmentSource:
    def __init__(self, config: Settings = settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _http(self) -> httpx.AsyncClient:
        base = self._settings.shipment_api_base_url
        parts = urlsplit(base)
        local = parts.hostname in ("localhost", "127.0.0.1")
        if not base or parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local) or \
                parts.path not in ("", "/") or parts.query or parts.username:
            if base:
                logger.error("SHIPMENT_API_BASE_URL must be an https origin without path or credentials.")
            raise ShipmentSourceNotConfiguredError("Shipment reports are not available: the shipment service is "
                                                   "not configured on the server.")
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop or self._client.is_closed:
            cfg = self._settings
            self._client = httpx.AsyncClient(
                base_url=base, transport=self._transport, follow_redirects=False,  # a redirect never carries the token
                timeout=httpx.Timeout(cfg.shipment_api_read_timeout, connect=cfg.shipment_api_connect_timeout))
            self._loop = loop
        return self._client

    async def fetch(self, query: SourceQuery, credential: str) -> FetchResult:
        cfg = self._settings
        client = self._http()
        deadline = time.monotonic() + cfg.shipment_api_total_timeout
        started, last = time.monotonic(), None
        for attempt in range(1, cfg.shipment_api_fetch_attempts + 1):
            try:
                result = await self._fetch_once(client, query, credential, deadline)
            except (SourceChangedError, UpstreamPaginationError) as exc:
                last = exc
                logger.warning("Shipment fetch attempt %s inconsistent (%s); %s", attempt, exc.code,
                               "retrying" if attempt < cfg.shipment_api_fetch_attempts else "giving up")
                continue
            result.attempts = attempt
            result.query = query.describe()
            logger.info("Shipment fetch complete: total=%s requests=%s page_size=%s clamped=%s attempts=%s ms=%d",
                        result.total, result.requests, result.page_size, result.page_size_clamped, attempt,
                        (time.monotonic() - started) * 1000)
            return result
        raise last

    async def _fetch_once(self, client, query, credential, deadline) -> FetchResult:
        cfg = self._settings
        calls, failure = 0, None
        semaphore = asyncio.Semaphore(max(1, cfg.shipment_api_concurrency))

        async def page(q: SourceQuery, number: int, per_page: int, retries: int | None = None) -> Page:
            nonlocal calls, failure
            if calls >= cfg.shipment_api_max_requests:
                if failure is not None:
                    raise failure  # recovering from upstream failures used the request budget: report those
                raise SourceTooLargeError("Too many shipments match this range. Select a shorter date range.",
                                          details={"max_requests": cfg.shipment_api_max_requests})
            calls += 1
            async with semaphore:
                try:
                    return await self._call(client, q, number, per_page, credential, deadline, retries)
                except _SPLIT_ERRORS as exc:
                    failure = exc
                    raise

        # Step A: the matching total.
        count = await page(query, 1, cfg.shipment_api_count_page_size)
        total = count.total
        if total == 0:
            raise NoMatchingRecordsError("No shipments matched the selected dates and status.",
                                         details={"expected_records": 0, "filters": query.describe()})
        limit = min(cfg.shipment_api_max_records, cfg.max_records)
        if total > limit:
            raise SourceTooLargeError(
                f"Too many shipments match this range ({total:,}; a report supports at most {limit:,}). "
                "Select a shorter date range or a single status.", details={"expected_records": total, "max_records": limit})

        # Step B: large results are read as date windows (each counted, then read in one page), small ones
        # as concurrent pages. The order-list API fails once (page + 1) * perPage passes about 1,780 rows,
        # so deep pages of a large result cannot be read at any page size; small date windows never get there.
        try:
            rows, pages, size, clamped = await self._read_range(page, query, total, deadline, windows=True, top=True)
        except _WindowMismatch as mismatch:
            logger.warning("Shipment date windows matched %s of %s shipments; reading the whole range by pages.",
                           mismatch.found, total)
            rows, pages, size, clamped = await self._read_range(page, query, total, deadline, windows=False, top=True)
        if len(rows) != total:
            raise IncompleteSourceError(f"Received {len(rows):,} of {total:,} matching shipments. Please retry.",
                                        details={"expected_records": total, "fetched_records": len(rows)},
                                        status_code=502, retryable=True)
        return FetchResult(rows=rows, total=total, account_summary=count.summary,
                           fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), requests=calls,
                           page_size=size, pages=pages, attempts=1, page_size_clamped=clamped)

    async def _read_range(self, page, q: SourceQuery, total: int, deadline: float, *, windows: bool, top: bool):
        """(rows, pages, page size, clamped) for one date range whose matching total is known."""
        cfg = self._settings
        cuts = self._day_cuts(q) if windows else []
        if cuts and total > cfg.shipment_api_window_records:
            return await self._read_windows(page, q, total, deadline, cuts,
                                            math.ceil(total / cfg.shipment_api_window_records))
        try:
            # Inside a window one page per window keeps requests few; a whole small range uses concurrent pages.
            return await self._read_paged(page, q, total, deadline, single=not top, split_pages=not cuts)
        except _SPLIT_ERRORS as exc:
            # Only a large range is retried as smaller windows; a small one failing is a real outage.
            if not cuts or total <= cfg.shipment_api_min_page_size or time.monotonic() >= deadline:
                raise
            logger.warning("Shipment range %s..%s (%s shipments) failed (%s); reading it as two date windows.",
                           q.from_date, q.to_date, total, exc.code)
            return await self._read_windows(page, q, total, deadline, cuts, 2)

    async def _read_windows(self, page, q: SourceQuery, total: int, deadline: float, cuts: list[datetime], parts: int):
        """Read ``q`` as ``parts`` consecutive date windows split at local midnights; their totals must add up."""
        parts = min(parts, len(cuts) + 1)
        days = len(cuts) + 1
        chosen = sorted({cuts[min(len(cuts), max(1, round(i * days / parts))) - 1] for i in range(1, parts)})
        bounds = [q.from_date, *(c.strftime(_STAMP) for c in chosen), q.to_date]
        subqueries = [replace(q, from_date=a, to_date=b) for a, b in zip(bounds, bounds[1:])]

        # Every window is counted before any is read: totals that do not add up cost no row reads.
        counts = await _all(page(w, 1, self._settings.shipment_api_count_page_size) for w in subqueries)
        found = sum(c.total for c in counts)
        logger.info("Shipment range %s..%s read as %s date windows: %s", q.from_date, q.to_date, len(subqueries),
                    [c.total for c in counts])
        if found != total:
            raise _WindowMismatch(found)
        results = await _all(self._read_range(page, w, c.total, deadline, windows=True, top=False)
                             for w, c in zip(subqueries, counts) if c.total)
        return ([row for r in results for row in r[0]], sum(r[1] for r in results),
                max(r[2] for r in results), any(r[3] for r in results))

    @staticmethod
    def _day_cuts(q: SourceQuery) -> list[datetime]:
        """Local midnights strictly inside the range: the only points a range is split at."""
        start, end = datetime.strptime(q.from_date, _STAMP), datetime.strptime(q.to_date, _STAMP)
        day, cuts = datetime.combine(start.date() + timedelta(days=1), dtime(0)), []
        while day < end:
            cuts.append(day)
            day += timedelta(days=1)
        return cuts

    async def _read_paged(self, page, q: SourceQuery, total: int, deadline: float, *, single: bool, split_pages: bool):
        def same_total(p: Page) -> None:
            if p.total != total:
                raise SourceChangedError("The shipments changed while they were being loaded. Please try again.",
                                         details={"expected_records": total, "latest_total": p.total})

        size, clamped = (min(total, self._settings.shipment_api_max_page_size) if single else self._page_size(total)), False
        while True:
            try:
                chunks = await self._read_pages(page, q, total, size, deadline, split_pages)
                return self._assemble(chunks, total, same_total), len(chunks), size, clamped
            except _Clamped as clamp:
                if clamped:
                    raise UpstreamPaginationError("The shipment service returned pages of changing size. Please try again.",
                                                  details={"expected_rows": size, "received_rows": clamp.rows})
                # Pages after the first at the requested size would skip rows: read again with the observed size.
                size, clamped = clamp.rows, True

    def _page_size(self, total: int) -> int:
        """About one page per concurrent request; small totals stay in one page."""
        cfg = self._settings
        size = max(math.ceil(total / max(1, cfg.shipment_api_concurrency)), cfg.shipment_api_min_page_size)
        size = math.ceil(size / _PAGE_STEP) * _PAGE_STEP
        return max(1, min(size, total, cfg.shipment_api_max_page_size))

    async def _read_pages(self, page, q: SourceQuery, total: int, size: int, deadline: float,
                          split_pages: bool) -> list[tuple[int, int, Page]]:
        """(first row offset, page size, page) for every page, read concurrently.

        With ``split_pages`` a page that fails with a 5xx or timeout is read again as the two halves it
        covers (page n of size s is pages 2n-1 and 2n of size s/2); otherwise the caller splits the date range.
        """
        async def read(number: int, per_page: int) -> list[tuple[int, int, Page]]:
            start = (number - 1) * per_page
            if start >= total:
                return []
            splittable = split_pages and per_page % 2 == 0 and per_page // 2 >= _MIN_SPLIT_ROWS
            try:
                # A failure that will be split is not retried at the same size: splitting is the retry.
                current = await page(q, number, per_page, 0 if splittable or not split_pages else None)
            except _SPLIT_ERRORS as exc:
                if not splittable or time.monotonic() >= deadline:
                    raise
                half = per_page // 2
                logger.warning("Shipment API page=%s per_page=%s failed (%s); reading it as pages of %s.",
                               number, per_page, exc.code, half)
                first, second = await _all([read(2 * number - 1, half), read(2 * number, half)])
                return first + second
            return [(start, per_page, current)]

        results = await _all(read(n, size) for n in range(1, math.ceil(total / size) + 1))
        return sorted((chunk for result in results for chunk in result), key=lambda chunk: chunk[0])

    @staticmethod
    def _assemble(chunks: list[tuple[int, int, Page]], total: int, same_total) -> list[dict[str, Any]]:
        """Check the pages in row order and join them; any gap, repeat or overlap stops the fetch."""
        rows: list[dict[str, Any]] = []
        seen: set = set()
        previous = None
        for start, size, current in chunks:
            same_total(current)
            expected = min(size, total - start)
            details = {"page": current.number, "page_size": size, "expected_rows": expected,
                       "received_rows": len(current.rows)}
            if 0 < len(current.rows) < expected and start == 0 and not rows:
                raise _Clamped(len(current.rows))
            if not current.rows:
                raise UpstreamPaginationError("The shipment service returned an empty page before all shipments were "
                                              "loaded. Please try again.", details=details)
            if previous is not None and current.rows == previous.rows:
                raise UpstreamPaginationError("The shipment service returned the same page twice. Please try again.",
                                              details=details)
            keys = [k for k in (_identity(_flatten(r)) for r in current.rows) if k is not None]
            overlap = sum(1 for k in keys if k in seen)
            if keys and overlap == len(keys):
                raise UpstreamPaginationError("The shipment service returned a page of shipments already loaded. "
                                              "Please try again.", details={**details, "repeated_rows": overlap})
            if overlap:
                # Rows moved between pages while reading: some other row was skipped.
                raise SourceChangedError("The shipments changed while they were being loaded. Please try again.",
                                         details={**details, "repeated_rows": overlap})
            if len(current.rows) != expected:
                raise UpstreamPaginationError("The shipment service returned a page of unexpected size. Please try again.",
                                              details=details)
            seen.update(keys)
            rows.extend(current.rows)
            previous = current
        if len(rows) != total:
            raise IncompleteSourceError(f"Received {len(rows):,} of {total:,} matching shipments. Please retry.",
                                        details={"expected_records": total, "fetched_records": len(rows)},
                                        status_code=502, retryable=True)
        return rows

    async def _call(self, client, query, number, per_page, credential, deadline, retries=None) -> Page:
        cfg = self._settings
        retries = cfg.shipment_api_retries if retries is None else retries
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            started = time.monotonic()
            try:
                async with asyncio.timeout(remaining):
                    async with client.stream("POST", cfg.shipment_api_path, json=query.body(number, per_page),
                                             headers={"Authorization": credential, "Accept": "application/json"}) as resp:
                        status, headers = resp.status_code, resp.headers
                        logger.info("Order list API response: request_id=%s report_id=%s status=%s page=%s per_page=%s",
                                    request_id_var.get(), report_id_var.get(), status, number, per_page)
                        if status >= 500 and attempt < retries:
                            logger.warning("Shipment API page=%s returned %s; retrying.", number, status)
                            await asyncio.sleep(min(0.5 * (attempt + 1), max(deadline - time.monotonic(), 0)))
                            continue
                        raw = await self._read_limited(resp) if 200 <= status < 300 else b""
                        logger.info("Order list API response: page=%s bytes=%s", number, len(raw))
            except (httpx.TimeoutException, TimeoutError):
                if attempt < retries and deadline - time.monotonic() > 0:
                    logger.warning("Shipment API page=%s timed out; retrying.", number)
                    continue
                break
            except httpx.TransportError as exc:
                logger.warning("Shipment API page=%s network error (%s).", number, type(exc).__name__)
                if attempt < retries and deadline - time.monotonic() > 0:
                    await asyncio.sleep(min(0.5 * (attempt + 1), max(deadline - time.monotonic(), 0)))
                    continue
                raise UpstreamUnavailableError("The shipment service could not be reached. Please try again.") from None
            parsed = self._parse(status, headers, raw, number, per_page)
            logger.info("Shipment API page=%s per_page=%s rows=%s total=%s ms=%d", number, per_page, len(parsed.rows),
                        parsed.total, (time.monotonic() - started) * 1000)
            return parsed
        raise UpstreamTimeoutError("The shipment service took too long to respond. Please try again.",
                                   details={"timeout_seconds": cfg.shipment_api_total_timeout})

    async def _read_limited(self, resp: httpx.Response) -> bytes:
        limit = self._settings.shipment_api_max_response_bytes
        declared = resp.headers.get("content-length", "")
        too_large = SourceTooLargeError("Too many shipments match this range. Select a shorter date range.",
                                        details={"max_response_bytes": limit})
        if declared.isdigit() and int(declared) > limit:
            raise too_large
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise too_large
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(status: int, headers: httpx.Headers, raw: bytes, number: int, per_page: int) -> Page:
        details = {"upstream_status": status}
        if status == 401:
            raise UpstreamAuthError("Your shipment service session has expired or is not valid. Log in again and retry.",
                                    details=details)
        if status == 403:
            raise UpstreamForbiddenError("Your account is not allowed to read these shipments.", details=details)
        if status == 429:
            retry_after = headers.get("retry-after", "")
            if retry_after.isdigit() and int(retry_after) <= 3600:
                details["retry_after_seconds"] = int(retry_after)
            raise UpstreamRateLimitedError("The shipment service is receiving too many requests. Please wait and "
                                           "try again.", details=details)
        if status >= 500:
            raise UpstreamUnavailableError("The shipment service is unavailable. Please try again.", details=details)
        if not 200 <= status < 300:
            raise UpstreamRejectedError("The shipment service rejected the request.", details=details)
        invalid = UpstreamInvalidResponseError("The shipment service returned an unreadable response.", details=details)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise invalid from None
        if not isinstance(body, dict):
            raise invalid
        inner = body.get("status_code")
        if body.get("success") is not True or (inner is not None and inner != 200):
            code = inner if isinstance(inner, int) else None
            if code == 401:
                raise UpstreamAuthError("Your shipment service session has expired or is not valid. Log in again "
                                        "and retry.", details={**details, "upstream_status_code": code})
            if code == 403:
                raise UpstreamForbiddenError("Your account is not allowed to read these shipments.",
                                             details={**details, "upstream_status_code": code})
            raise UpstreamRejectedError("The shipment service could not return the shipments.",
                                        details={**details, "upstream_status_code": code})
        data = body.get("data")
        rows = data.get("getOrderList") if isinstance(data, dict) else None
        total = data.get("totalRecords") if isinstance(data, dict) else None
        if isinstance(total, str) and total.strip().isdigit():
            total = int(total.strip())
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) or isinstance(total, bool) \
                or not isinstance(total, int) or total < 0 or len(rows) > per_page:
            raise invalid
        summary = {k: data[k] for k in ACCOUNT_COUNTER_KEYS
                   if isinstance(data.get(k), int) and not isinstance(data.get(k), bool) and data[k] >= 0}
        return Page(number=number, per_page=per_page, rows=rows, total=total, summary=summary)
