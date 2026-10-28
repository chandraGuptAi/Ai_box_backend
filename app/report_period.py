"""Resolve UI date presets once, using the selected local calendar and inclusive days.

Also the single place that decides which period a shipment report covers:

- DateRangeResolver reads the period named in the prompt (English, Hindi, Hinglish) with fixed rules, no AI.
- ReportDateValidator enforces the maximum report range of six calendar months.
- resolve_report_period applies the precedence rule (explicit selection > prompt > preset/default filters) and
  returns the request whose ``filters`` every later step (source query, metrics, charts, exports) reads.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_REPORT_MONTHS = 6


def local_today(timezone: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone '{timezone}'") from None


def preset_days(label: str | None) -> int | None:
    if label is None or label.strip().lower() in {"", "custom", "custom range", "custom_range"}:
        return None
    match = re.fullmatch(r"(?:last[\s_-]*)?(\d+)[\s_-]*(?:days?|d)?", label.strip(), re.I)
    if not match or int(match[1]) < 1:
        raise ValueError("date_range must be 'Last N Days', 'last_N_days', or 'custom'")
    return int(match[1])


def last_days_period(days: int, timezone: str, *, today: date | None = None) -> tuple[date, date]:
    if days < 1 or days > 3660:
        raise ValueError("range_days must be between 1 and 3660")
    end = today if today is not None else local_today(timezone)
    return end - timedelta(days=days - 1), end


def period_label(period: dict | None) -> str | None:
    if not period or not period.get("from_date") or not period.get("to_date"):
        return None
    field = period.get("date_field", "order_date").replace("_", " ")
    return f"{period['from_date']} to {period['to_date']} ({field}; {period.get('timezone', 'Asia/Kolkata')})"


# --- Calendar months ------------------------------------------------------------------------

def months_before(day: date, months: int) -> date:
    """The same day-of-month ``months`` earlier; a day the target month lacks moves to the next month's 1st.

    Moving forward (31 Aug - 6 months = 1 Mar, not 28/29 Feb) keeps every window at most N months long.
    """
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, day.day) if day.day <= last else date(year, month, last) + timedelta(days=1)


def last_months_period(months: int, end: date) -> tuple[date, date]:
    """N calendar months ending on ``end`` inclusive: [months_before(end + 1 day, N), end]."""
    return months_before(end + timedelta(days=1), months), end


def _month_span(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


class ReportDateValidator:
    """One rule for every entry point: a report covers at most MAX_REPORT_MONTHS calendar months.

    The window is inclusive on both days. [start, end] is accepted when start >= months_before(end + 1 day, N),
    so 1 Apr-30 Sep (exactly six months) passes and 31 Mar-30 Sep or 1 Apr-1 Oct (six months and a day) fail.
    Old dates are fine; only the length of the interval is limited. Nothing is ever truncated.
    """

    def __init__(self, max_months: int = MAX_REPORT_MONTHS):
        self.max_months = max_months

    def earliest_start(self, end: date) -> date:
        return months_before(end + timedelta(days=1), self.max_months)

    def validate(self, start: date | None, end: date | None, *, source: str) -> None:
        from app.errors import InvalidDateRangeError, ReportDateRangeExceededError
        if start is None or end is None:
            return
        if start > end:
            raise InvalidDateRangeError("The start date is after the end date. Select a valid date range.",
                                        details={"field": "from_date", "from_date": start.isoformat(),
                                                 "to_date": end.isoformat(), "source": source})
        if start < self.earliest_start(end):
            raise ReportDateRangeExceededError(
                f"You can generate reports for up to {self.max_months} months at a time. "
                "Please select a shorter date range.",
                details={"max_months": self.max_months, "suggested_range": f"last_{self.max_months}_months",
                         "requested": {"from_date": start.isoformat(), "to_date": end.isoformat(), "source": source},
                         "earliest_from_date_for_end": self.earliest_start(end).isoformat()})


# --- Prompt periods --------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptPeriod:
    start: date
    end: date
    label: str
    kind: str  # rolling_days | rolling_months | calendar_week | calendar_month | calendar_quarter | calendar_year | explicit
    span: tuple[int, int] = field(default=(0, 0), compare=False)


_DEV = "ऀ-ॿ"
_L = rf"(?<![\w{_DEV}])"   # Python's \w excludes Devanagari vowel signs, so word edges are explicit.
_R = rf"(?![\w{_DEV}])"
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "thirty": 30, "sixty": 60, "ninety": 90,
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6,
    "che": 6, "chheh": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "pandrah": 15, "tees": 30, "saath": 60,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "छे": 6, "सात": 7, "आठ": 8,
    "नौ": 9, "दस": 10, "पंद्रह": 15, "तीस": 30, "साठ": 60, "नब्बे": 90,
}
_NUM = r"(\d{1,4}|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + ")"
_UNITS = {
    "d": r"days?|din|dinon|dino|dinoan|दिनों|दिनो|दिन",
    "w": r"weeks?|wks?|hafton|hafte|hafta|saptah|हफ़्तों|हफ्तों|हफ़्ते|हफ्ते|हफ़्ता|हफ्ता|सप्ताहों|सप्ताह",
    "m": r"months?|mahinon|mahino|mahine|mahina|maheene|maheena|महीनों|महीनो|महीने|महीना|माह",
    "y": r"years?|yrs?|saalon|saal|sal|varsh|सालों|साल|वर्षों|वर्ष",
}
_UNIT = "(" + "|".join(_UNITS.values()) + ")"
_PAST = (r"last|past|previous|recent|pichle|pichhle|picchle|pichla|pichhla|pichli|pichhli|gat|"
         r"पिछले|पिछली|पिछला|बीते|गत|आख़िरी|आखिरी|अंतिम")
_MARKER = (rf"(?:(?:for|over|in|during|within)\s+(?:the\s+)?(?:{_PAST})|{_PAST}|for(?:\s+the)?)")
# "delivered within 3 days", "older than 30 days" describe shipments, not the report period.
_NOT_PERIOD = re.compile(r"(?:within|after|before|than|over|under|above|below|upto|up to|in|older|aged?|late)\s*$")
_REPORTISH = r"(?:report|reports|data|analysis|insights?|shipments?|orders?|summary|ka|ki|ke|का|की|के)"

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6, "जुलाई": 7, "अगस्त": 8,
    "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
_MONTH = "(" + "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(\d{4})"
# One date: ISO, numeric day-first, "1 Aug [2026]" or "Aug 1[,] [2026]". Groups are read by _date_parts.
_DATE = (rf"(?:(\d{{4}})-(\d{{1,2}})-(\d{{1,2}})|(\d{{1,2}})[/.-](\d{{1,2}})[/.-](\d{{4}})"
         rf"|{_DAY}\s+{_MONTH},?(?:\s+{_YEAR})?|{_MONTH}\s+{_DAY},?(?:\s+{_YEAR})?)")
_TO = r"\s*(?:to|till|until|upto|up to|through|thru|-|–|—|से)\s*"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in text)
    text = re.sub(r"(\w)['’]s\b", r"\1", text.casefold())
    text = re.sub(r"\s+", " ", text)
    # "between X and Y" is a range; a bare "and" is not ("March and April" names two periods).
    return re.sub(r"\bbetween\s+(.{1,40}?)\s+and\s+", r"from \1 to ", text)


def _count(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _unit(token: str) -> str:
    return next(key for key, pattern in _UNITS.items() if re.fullmatch(pattern, token))


def _date_parts(groups: tuple) -> tuple[int | None, int, int]:
    """(year or None, month, day) from the 12 groups of one _DATE match."""
    iy, im, idd, nd, nm, ny, d1, m1, y1, m2, d2, y2 = groups
    if iy:
        return int(iy), int(im), int(idd)
    if nd:
        return int(ny), int(nm), int(nd)
    if d1:
        return (int(y1) if y1 else None), _MONTHS[m1], int(d1)
    return (int(y2) if y2 else None), _MONTHS[m2], int(d2)


def _make_date(year: int, month: int, day: int) -> date:
    from app.errors import InvalidDateRangeError
    try:
        return date(year, month, day)
    except ValueError:
        raise InvalidDateRangeError(f"The prompt names a date that does not exist ({day:02d}-{month:02d}-{year}).",
                                    details={"field": "prompt", "reason": "invalid_date"}) from None


def _iso(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


class DateRangeResolver:
    """The single reporting period named in a prompt, resolved with fixed rules against ``today``.

    Returns None when the prompt names no period. Raises ClarificationRequiredError when it names two
    different periods ("compare this month with last month") and InvalidDateRangeError for impossible or
    reversed explicit dates. No AI call is made; anything else is left to the existing planner, whose date
    filters are checked deterministically against the resolved range.
    """

    def resolve(self, prompt: str, today: date) -> PromptPeriod | None:
        text = _normalize(prompt)
        found: list[PromptPeriod] = []
        taken: list[tuple[int, int]] = []

        def add(match, start, end, label, kind):
            span = match.span()
            if any(span[0] < b and a < span[1] for a, b in taken):
                return
            taken.append(span)
            found.append(PromptPeriod(start, end, label, kind, span))

        for match in re.finditer(rf"{_L}(?:from|between)?\s*{_DATE}{_TO}{_DATE}(?:\s*(?:तक|tak))?{_R}", text):
            add(match, *self._explicit_range(match.groups(), today))
        for match in re.finditer(rf"{_L}(?:from|between)?\s*{_MONTH}(?:\s+{_YEAR})?{_TO}{_MONTH}(?:\s+{_YEAR})?{_R}", text):
            add(match, *self._month_range(match.groups(), today))
        for match in re.finditer(rf"{_L}(?:since|from)\s+{_DATE}{_R}", text):
            year, month, day = _date_parts(match.groups())
            start = _make_date(year or today.year, month, day)
            if year is None and start > today:
                start = _make_date(today.year - 1, month, day)
            add(match, start, today, f"Since {_iso(start)}", "explicit")
        for match in re.finditer(rf"{_L}(?:on\s+)?{_DATE}{_R}", text):
            year, month, day = _date_parts(match.groups())
            if year is None:
                continue  # "1 Aug" alone is too uncertain to be a reporting period
            day_value = _make_date(year, month, day)
            add(match, day_value, day_value, _iso(day_value), "explicit")
        for match in re.finditer(rf"{_L}{_MARKER}\s+{_NUM}\s*{_UNIT}{_R}", text):
            add(match, *self._rolling(_count(match[1]), _unit(match[2]), today))
        for match in re.finditer(rf"{_L}{_NUM}\s*{_UNIT}\s+{_REPORTISH}{_R}", text):
            if not _NOT_PERIOD.search(text[:match.start()]):
                add(match, *self._rolling(_count(match[1]), _unit(match[2]), today))
        for pattern, resolve in self._named(today):
            for match in re.finditer(pattern, text):
                add(match, *resolve)
        for match in re.finditer(rf"{_L}(?:(in|for|during|of|month of|ka|ki|ke|का|की|के)\s+)?{_MONTH}(?:\s+{_YEAR})?{_R}", text):
            name, year = match[2], match[3]
            if not (match[1] or year) and (name in {"may", "mar", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
                                                    "dec", "jan", "feb", "apr"} or len(name) < 3):
                continue  # bare abbreviations and "may" are too easily other words
            month = _MONTHS[name]
            year_value = int(year) if year else (today.year if month <= today.month else today.year - 1)
            start, end = _month_span(year_value, month)
            add(match, start, min(end, today) if start <= today else end, f"{calendar.month_name[month]} {year_value}",
                "calendar_month")

        distinct = {(p.start, p.end) for p in found}
        if len(distinct) > 1:
            from app.errors import ClarificationRequiredError
            periods = sorted({(p.start, p.end, p.label) for p in found})
            raise ClarificationRequiredError(
                "Your request mentions more than one period. Generate one report per period, or select a single "
                "date range.", details={"clarification_required": True, "reason": "multiple_periods",
                                        "periods": [{"label": label, "from_date": s.isoformat(), "to_date": e.isoformat()}
                                                    for s, e, label in periods]})
        return found[0] if found else None

    @staticmethod
    def _rolling(n: int, unit: str, today: date) -> tuple[date, date, str, str]:
        from app.errors import InvalidDateRangeError
        if n < 1:
            raise InvalidDateRangeError("The requested period must be at least one day.",
                                        details={"field": "prompt", "reason": "empty_period"})
        if unit == "d":
            return today - timedelta(days=n - 1), today, f"Last {n} day{'s' * (n != 1)}", "rolling_days"
        if unit == "w":
            return today - timedelta(days=7 * n - 1), today, f"Last {n} week{'s' * (n != 1)}", "rolling_days"
        months = n if unit == "m" else 12 * n
        start, end = last_months_period(months, today)
        label = f"Last {n} month{'s' * (n != 1)}" if unit == "m" else f"Last {n} year{'s' * (n != 1)}"
        return start, end, label, "rolling_months"

    @staticmethod
    def _explicit_range(groups: tuple, today: date) -> tuple[date, date, str, str]:
        from app.errors import InvalidDateRangeError
        (y1, m1, d1), (y2, m2, d2) = _date_parts(groups[:12]), _date_parts(groups[12:24])
        no_year = y1 is None and y2 is None
        if y2 is None:
            y2 = y1 if y1 is not None else today.year
        if y1 is None:
            y1 = y2 if (m1, d1) <= (m2, d2) else y2 - 1   # "15 Dec to 15 Jan 2027" starts in 2026
        start, end = _make_date(y1, m1, d1), _make_date(y2, m2, d2)
        if no_year and end > today:   # "1 Aug to 30 Sep" without a year: the most recent such period
            start, end = _make_date(y1 - 1, m1, d1), _make_date(y2 - 1, m2, d2)
        if start > end:
            raise InvalidDateRangeError("The prompt's start date is after its end date. Check the dates and try again.",
                                        details={"field": "prompt", "reason": "reversed_dates",
                                                 "from_date": start.isoformat(), "to_date": end.isoformat()})
        return start, end, f"{_iso(start)} to {_iso(end)}", "explicit"

    @staticmethod
    def _month_range(groups: tuple, today: date) -> tuple[date, date, str, str]:
        from app.errors import InvalidDateRangeError
        m1, y1, m2, y2 = _MONTHS[groups[0]], groups[1], _MONTHS[groups[2]], groups[3]
        year2 = int(y2) if y2 else (int(y1) if y1 else (today.year if m2 <= today.month else today.year - 1))
        year1 = int(y1) if y1 else (year2 if m1 <= m2 else year2 - 1)
        start, end = _month_span(year1, m1)[0], _month_span(year2, m2)[1]
        if start > end:
            raise InvalidDateRangeError("The prompt's start month is after its end month. Check the months and try again.",
                                        details={"field": "prompt", "reason": "reversed_dates"})
        end = min(end, today) if start <= today else end
        return start, end, f"{calendar.month_name[m1]} {year1} to {calendar.month_name[m2]} {year2}", "explicit"

    @staticmethod
    def _named(today: date):
        week_start = today - timedelta(days=today.weekday())
        previous_month = months_before(today.replace(day=1), 1)
        quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        previous_quarter = months_before(quarter_start, 3)
        this_, last = r"(?:this|current|is|इस)", rf"(?:last|previous|{_PAST.replace('last|past|previous|', '')})"
        return [
            (rf"{_L}(?:today|aaj|आज){_R}", (today, today, "Today", "calendar_day")),
            (rf"{_L}yesterday{_R}", (today - timedelta(days=1), today - timedelta(days=1), "Yesterday", "calendar_day")),
            (rf"{_L}{this_}\s+(?:week|hafte|hafta|हफ़्ते|हफ्ते|सप्ताह){_R}", (week_start, today, "This week", "calendar_week")),
            (rf"{_L}{last}\s+(?:week|hafte|hafta|हफ़्ते|हफ्ते|सप्ताह){_R}",
             (week_start - timedelta(days=7), week_start - timedelta(days=1), "Last week", "calendar_week")),
            (rf"{_L}(?:{this_}\s+(?:month|mahine|mahina|महीने|महीना|माह)|month to date|mtd){_R}",
             (today.replace(day=1), today, "This month", "calendar_month")),
            (rf"{_L}{last}\s+(?:month|mahine|mahina|महीने|महीना|माह){_R}",
             (previous_month, today.replace(day=1) - timedelta(days=1), f"Last month ({previous_month:%B %Y})", "calendar_month")),
            (rf"{_L}(?:{this_}\s+quarter|quarter to date|qtd){_R}", (quarter_start, today, "This quarter", "calendar_quarter")),
            (rf"{_L}{last}\s+quarter{_R}",
             (previous_quarter, quarter_start - timedelta(days=1), "Last quarter", "calendar_quarter")),
            (rf"{_L}(?:{this_}\s+(?:year|saal|साल|वर्ष)|year to date|ytd){_R}",
             (date(today.year, 1, 1), today, "This year", "calendar_year")),
            (rf"{_L}{last}\s+(?:year|saal|साल|वर्ष){_R}",
             (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), f"Last year ({today.year - 1})", "calendar_year")),
        ]


# --- Precedence ------------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectivePeriod:
    """The period a report covers and why. ``to_dict`` is the public ``scope.date_range`` metadata."""
    source: str          # prompt | selected_filters | previous_report
    start: date
    end: date
    label: str
    timezone: str
    date_field: str
    selected: dict[str, Any] | None = None   # the structured range that was sent, when it differs
    prompt_period: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "label": self.label, "start_date": self.start.isoformat(),
                "end_date": self.end.isoformat(), "timezone": self.timezone, "date_field": self.date_field,
                "max_months": MAX_REPORT_MONTHS, "validation": "within_limit",
                **({"selected_filters": self.selected} if self.selected else {}),
                **({"prompt_period": self.prompt_period} if self.prompt_period else {})}


# Request-local, like request_id_var: the planner and the report scope read the resolution of this request.
effective_period_var: ContextVar[EffectivePeriod | None] = ContextVar("effective_period", default=None)


def _structured_kind(filters) -> str:
    """How much the structured range can be trusted as the seller's own choice."""
    if filters is None or filters.from_date is None or filters.to_date is None:
        return "none"
    if filters.date_source:
        return filters.date_source            # "user" or "default", sent by clients that know
    # A preset ("Last 7 days", range_days) is what the UI sends by default, so it cannot override the prompt.
    # Explicit custom dates are a deliberate selection.
    return "preset" if filters.range_days is not None else "custom"


def _preset_label(filters) -> str:
    if filters.range_days is not None:
        return f"Last {filters.range_days} day{'s' * (filters.range_days != 1)}"
    return f"{_iso(filters.from_date)} to {_iso(filters.to_date)}"


def _previous_period(previous_report: dict | None) -> dict | None:
    scope = ((previous_report or {}).get("structured_report") or {}).get("scope") or {}
    period = scope.get("date_range")
    return period if isinstance(period, dict) and period.get("source") in {"prompt", "previous_report"} else None


def resolve_report_period(request, *, default_date_field: str, today: date | None = None,
                          validator: ReportDateValidator | None = None):
    """(request with the effective filters, EffectivePeriod or None). Runs before any AI call or fetch.

    Precedence for backend-fetched reports (source=shipment_api):
    1. A structured range the seller explicitly chose (filters.date_source="user", or custom from/to dates)
       wins. A prompt period inside it is analysed as a sub-period, as before; a different one asks to clarify.
    2. Otherwise the prompt's period. A preset/default range ("Last 7 days", range_days) never hides it.
    3. Otherwise a follow-up keeps the prompt period of the report it continues.
    4. Otherwise the structured range as sent.
    Payload reports analyse the records they were given, so only the limits are checked there.
    Every range that is used, and every prompt period, must pass ReportDateValidator.
    """
    from app.errors import ClarificationRequiredError
    from app.models.report_schema import ReportFilters
    validator = validator or ReportDateValidator()
    filters = request.filters
    timezone = filters.timezone if filters else "Asia/Kolkata"
    try:
        today = today or local_today(timezone)
    except ValueError:
        from app.errors import UnsupportedFilterError
        raise UnsupportedFilterError(f"Unknown timezone '{timezone}'.", details={"field": "timezone"}) from None
    prompt = DateRangeResolver().resolve(request.prompt or "", today)
    if prompt:
        validator.validate(prompt.start, prompt.end, source="prompt")
    kind = _structured_kind(filters)
    selected = None if kind == "none" else {"from_date": filters.from_date.isoformat(),
                                            "to_date": filters.to_date.isoformat(), "kind": kind}
    prompt_dict = None if not prompt else {"label": prompt.label, "from_date": prompt.start.isoformat(),
                                           "to_date": prompt.end.isoformat()}
    date_field = filters.date_field if filters else default_date_field

    def effective(source, start, end, label, **extra):
        return EffectivePeriod(source, start, end, label, timezone, date_field, **extra)

    if request.source != "shipment_api":
        if kind == "none":
            return request, None
        validator.validate(filters.from_date, filters.to_date, source="selected_filters")
        return request, effective("selected_filters", filters.from_date, filters.to_date, _preset_label(filters),
                                  prompt_period=prompt_dict)

    if prompt and kind in {"user", "custom"}:
        if (prompt.start, prompt.end) != (filters.from_date, filters.to_date) and not (
                filters.from_date <= prompt.start and prompt.end <= filters.to_date):
            raise ClarificationRequiredError(
                f"Your prompt asks for {prompt.label} ({prompt.start.isoformat()} to {prompt.end.isoformat()}), but the "
                f"selected date filter is {filters.from_date.isoformat()} to {filters.to_date.isoformat()}. Change the "
                "date filter to match, or remove the period from the prompt.",
                details={"clarification_required": True, "reason": "date_range_conflict", "prompt_range": prompt_dict,
                         "selected_filters": selected})
        validator.validate(filters.from_date, filters.to_date, source="selected_filters")
        return request, effective("selected_filters", filters.from_date, filters.to_date, _preset_label(filters),
                                  prompt_period=prompt_dict)

    if prompt:
        start, end, source, label, overridden = prompt.start, prompt.end, "prompt", prompt.label, selected
    elif (previous := _previous_period(request.previous_report)) and kind in {"none", "preset", "default"}:
        try:
            start, end = date.fromisoformat(previous["start_date"]), date.fromisoformat(previous["end_date"])
        except (KeyError, TypeError, ValueError):
            from app.errors import InvalidDateRangeError
            raise InvalidDateRangeError("The previous report's period could not be read. Generate a new report.",
                                        details={"field": "previous_report", "reason": "invalid_period"}) from None
        source, label, overridden = "previous_report", str(previous.get("label") or f"{_iso(start)} to {_iso(end)}"), selected
    elif kind != "none":
        validator.validate(filters.from_date, filters.to_date, source="selected_filters")
        return request, effective("selected_filters", filters.from_date, filters.to_date, _preset_label(filters))
    else:
        return request, None

    validator.validate(start, end, source=source)
    if filters is None:
        new_filters = ReportFilters(date_field=default_date_field, from_date=start, to_date=end)
    else:
        new_filters = filters.model_copy(update={"from_date": start, "to_date": end, "range_days": None, "date_range": None})
    if (start, end) == ((filters.from_date, filters.to_date) if filters else (None, None)):
        overridden = None
    return (request.model_copy(update={"filters": new_filters}),
            effective(source, start, end, label, selected=overridden, prompt_period=prompt_dict if source != "prompt" else None))
