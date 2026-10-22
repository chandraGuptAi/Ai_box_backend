# Structured report API (version 2)

POST `/api/reports/generate` keeps the existing request shape. Example:

```json
{"report_type":"shipment","prompt":"Analyze cancellation reasons and identify couriers with the most cancellations.","data":{"getOrderList":[{"order_status":"Cancelled","courier_name":"Courier A","cancellation_reason":"Customer refused","payment_type":"COD"}]}}
```

The response retains `content`, `metrics` (the original calculation dictionary), `key_metrics`, `tables`, metadata and `report_id`. New Angular clients should render `structured_report`, which contains:

- `schema_version`, `report_id`, `report_type`, `title`, `subtitle`, `generated_at`.
- `executive_summary`: heading, short paragraphs, highlights.
- `metrics`: at most four display KPIs, each with key, label, value, unit and priority.
- `sections`: ordered id/title/order objects with typed `paragraph`, `bullet_list` and `table` blocks. Table blocks have title, columns and rows. Values in tables come from Python, through validated AI table references.
- `findings`: title/detail/severity (`high`, `medium`, `low`, `info`).
- `recommendations`: action/rationale/priority (`high`, `medium`, `low`).
- `limitations`, `charts`, and `scope`. Chart datasets have label/data arrays matching their labels. Charts use calculated values only.

These field names intentionally preserve established finding and recommendation conventions. No HTML is accepted. Tables contain formatted string cells; chart values are numeric. Render text with Angular interpolation, not innerHTML. Iterate sections in returned order and switch on block.type. Use the existing chart library with labels/datasets/type. Prefer the four display metrics over legacy key_metrics to avoid excessive cards. No Angular files were changed.

A full example response generated with a fake AI and the existing 12-record fixture is in `samples/structured_shipment_response.json`. This is a contract example, not a live DeepSeek result; fixture data is never substituted into real requests.

Generation returns `X-Report-Access-Token` in its response headers (exposed through CORS). Retain that token securely with the report id. Send `Authorization: Bearer <token>` for:

- GET `/api/reports/{report_id}`: returns the stored response.
- GET `/api/reports/{report_id}/download?format=pdf` or `format=docx`: exports that same report without another AI call.

Angular must use HttpClient with `observe: 'response'` to read the generation header and download as a blob with the bearer header. Missing/wrong tokens and missing/expired reports return 404. Do not put tokens in query strings. Generation secrets are never returned. The report token is a capability for one report, not the DeepSeek API key.

Legacy POST `/api/reports/export/pdf` and `/export/docx` continue accepting complete documents. They render the caller-supplied payload, not stored private data. New clients should use downloads to guarantee they export the saved original. `/types` and `/health` remain available.

## Prompt-driven analysis plan (additive fields)

Every shipment request is turned into a validated analysis plan before any number is calculated (`app/analysis/`):
prompt + current data -> plan -> Python results -> verification -> optional single DeepSeek narrative. All new fields are
optional and additive; existing clients and saved reports keep working, and `schema_version` stays `2.0`.

- `structured_report.plan`: what was applied. `filters` (field, op, value, human-readable `description`), `date_range`
  (`from`, `to`, the user's words in `requested`, and the resolution `basis`, e.g. relative to the payload's `as_of`),
  `tables`, `charts`, `detail_level` (`summary`/`standard`/`detailed`), `lookup`, `ai_narrative`, `unsupported`
  (`requested`, `reason`, `missing`) and interpretation `notes`.
- `structured_report.scope` adds `records_supplied`, `api_reported_total` (e.g. `getOrderList.total`),
  `account_total_orders` (`account_summary.all_orders`), `records_after_filters`, `is_partial`, `api_filter`
  (`report_filter` as supplied), `requested_period`, `supplied_data_period`, `excluded_missing_values`,
  `data_fingerprint` and a plain-language `statement`. A partial or paginated response is never described as complete.
- `structured_report.verification`: `status` (`passed` / `passed_with_corrections`), `checks` (one per metric group,
  chart, table and KPI, each `passed` / `failed` / `not_verifiable`), `failed_and_removed` and
  `not_independently_verified`. Figures are recounted from the scoped records with separate code; a figure that does not
  match is removed (charts get a `chart_notes` entry). Figures that cannot be recomputed (for example account counters
  copied from the API, or insights KPI cards) are listed as not independently verified rather than claimed accurate.
- Tables (`tables[]` and table blocks) may add `id`, `column_meta` (`key`, `label`, `type`
  `text|number|currency|percent|date`, `align`, `decimals`, `currency` = null meaning "as supplied"), `values` (the same
  cells as typed numbers/strings/null, parallel to `rows`), `total_rows`, `truncated` and `note`. `rows` remain display
  strings for existing renderers and the Word/PDF exports. Detail tables show at most 200 rows; when more match,
  `truncated` is true and `note` says how many were shown (the note is also printed in Word/PDF).

Behaviour:

- Filters understood from the prompt: statuses ("delivered shipments", "except cancelled"), payment modes
  ("COD and Prepaid"), couriers/states/cities (matched only against values present in the current data), numeric
  thresholds on shipping charge, invoice value, collectable amount and weight ("charges above ₹1,000", "between 500 and
  2,000"), and periods ("last month", "last 7 days", "September 2026", explicit dates). Periods filter by order date and
  are resolved against the payload's `as_of` (else today's date, which the `basis` states). Rates and distributions are
  never computed on a status-filtered denominator.
- Record lookups ("Show cancelled orders with reasons in a table", "Show shipping charges above ₹1,000") and empty
  scopes are answered by Python with zero DeepSeek calls. Chart-only requests remain zero-call.
- Focused (non-"detailed") requests get no unrequested charts, and record lookups no recommendations. "Detailed",
  "complete", "management" reports keep the full layout.
- Requests the data cannot answer return HTTP 422 `unsupported_request` with `details.unsupported` and
  `details.available_fields` (e.g. profit, collected revenue, a period when no record has an order date). Partly
  answerable requests return the report with `Not available: ...` limitations.
- Narrative sentences claiming that data is unavailable when the scoped records contain it (e.g. "No cancellation
  reasons are available") are removed, and `generation.notice` says so.
- Dynamic generation uses only the current approved plan. `previous_report` supplies follow-up identifiers,
  never stale tables or sections. The legacy rule-based mode retains its fingerprint/customer merge checks.
- `REPORT_AI_PLANNER=true` (default) enables a typed planning call before metrics and a grounded narrative call after deterministic computation
  of the requested sections. Its JSON plan is validated against the same field/operator/value vocabulary. No code or query from
  DeepSeek is ever executed.

## Dynamic shipment execution plan

With `REPORT_AI_PLANNER=true` (default), the first call interprets the exact prompt using registered
schema/capability metadata and protected UI/account scope, before source access. English, Hindi and
Hinglish prompts follow the same workflow. The typed plan uses arbitrary section IDs/titles and ordered
combinations of registered dimensions, metrics, filters, ranking and visuals; no example-specific section
list is enforced. Unknown operations/code/SQL are rejected. Invalid plans return a safe error; materially
ambiguous intent or source-scope conflicts return `clarification_required` before upstream requests.

`AnalysisPlan.sections` contains `ExecutionSection` objects with requirement IDs. Global cohort filters
apply to every section; section filters isolate subsets without shrinking other sections' denominators.
Prompt filters remain local, while the backend fetches the complete UI-authorized source scope using the
existing totalRecords/dynamic-perPage/pagination safeguards and delegated credential handling.

Python calculates only the planned metrics. Missing required fields yield explicit unavailable results
or clarification; zero section subsets remain explicit; an empty overall dataset produces
`no_matching_records` without report generation. Decimal arithmetic and documented denominators govern
money, averages and rates. Verified metrics feed the second DeepSeek call for grounded narrative, with
factual fallback preserving exactly the approved sections. Existing structured report JSON is retained.

Final coverage independently checks requirement IDs, dimensions, rankings, metric references, KPI values,
chart datasets, section order and export-table parity. Essential coverage failures block delivery even in
general accuracy audit-only mode. Angular renders the returned sections; PDF/Word use the validated revision.
`REPORT_AI_PLANNER=false` explicitly selects the previous rule-based compatibility behavior.

## Source completeness gate (shipment reports)

Runs after the metadata-only execution plan is accepted, before any metric, chart or narrative call
(`app/analysis/completeness.py`). Ambiguity and invalid plans stop before fetching shipments.

- Optional request fields: `filters` (`date_field`, `from_date`, `to_date`, `status`, `timezone`, default
  `Asia/Kolkata`), `expected_records` and `allow_partial` (default false). Without `filters`, `data.report_filter` is
  used. Without `expected_records`, the paginated list's `total` is used.
- Rows are identified by order ID + AWB. Identical repeated rows (overlapping pages) are removed. A shipment that
  arrives again with different values is never counted twice: the request stops with `conflicting_records`, or, with
  `allow_partial`, the first version is used and a limitation says so.
- The record dates are checked in the selected timezone. A timestamp with no timezone offset that could fall on either
  side of a day boundary is counted as `ambiguous_records`, not guessed. Statuses are checked when the selected label
  maps to a known status category.
- `completeness.filters` separates `applied_at_source` (the backend sent the filter) from each filter's
  `verification`: `verified`, `partially_verified`, `not_verifiable` (the records do not carry the field) or
  `not_selected`. `scope_verification` summarises both filters. A date filter is checked only against its own field.
- HTTP 422 `conflicting_records`: the same shipment arrived with different values.
- HTTP 422 `incomplete_source`: fewer unique rows than the source matched (e.g. only page 1 of 143).
- HTTP 422 `scope_mismatch`: received rows fall outside the selected period or status (the source ignored a filter).
- HTTP 422 `no_matching_records`: nothing is left in scope.
- HTTP 422 `clarification_required`: the prompt's period or status contradicts the selection.
  `details.conflict` is `period` or `status`, and `details.options` lists the choices. The selection is never
  silently replaced.
- `allow_partial: true` builds the report anyway. Out-of-scope rows are excluded and the report is labelled
  `Partial report: X of Y ...`.
- `REPORT_REQUIRE_COMPLETE_SOURCE=false` disables the two stop conditions for every request.
- Coverage is audit data, not report content. The completeness record (expected, fetched, unique, duplicate,
  conflicting, out-of-scope and analysed counts, selected filters, filter verification, `data_as_of`) and its notes
  are written to the backend audit log (below) and are **not** returned in the report or shown in exports. Only a
  partial report (`allow_partial`) keeps its `Partial report: X of Y` limitation so it never reads as complete.
  DeepSeek still receives the selected filters and counts; for a single-status cohort it is told not to state
  account-wide delivery, RTO or cancellation rates.
- Coverage audit log: `output/report_coverage_audit.jsonl` (rotating, 5 MB x 5 files, never served over HTTP, git-
  ignored). One JSON line per shipment report attempt:
  `{"event": "report_coverage", "at", "request_id", "report_id", "user_id", "report_type", "source", "outcome",
  "completeness", "notes", "details"}`. `outcome` is `generated` or the error code that stopped the fetch or gate
  (`details` then holds the error details). No credential, prompt text or shipment row is written. Find a user's
  failed request by the `request_id` shown in the error response.
- Export filenames are `Bigship_<Seller>_<ReportName>_<YYYY-MM-DD>.<ext>` (ASCII letters, digits and hyphens; dated in
  IST).

## Backend-owned shipment fetch (`source: "shipment_api"`)

The browser sends the prompt and the selected filters. The backend loads every matching shipment from the order-list
API (`app/services/shipment_source.py`), runs the completeness gate and the normal report pipeline, and returns the
same `ReportResponse`. `source: "payload"` (the default, records in `data`) is unchanged.

Request:

```http
POST /api/reports/generate
X-User-Id: admin:123                          (AI Box login, set by the identity interceptor)
X-Shipment-Authorization: Bearer <BigShip login token>
```

```json
{
  "report_type": "shipment",
  "source": "shipment_api",
  "prompt": "Show shipment status counts and percentages with graphs",
  "filters": {"date_field": "manifest_datetime", "from_date": "2026-10-01", "to_date": "2026-10-12",
              "status": "Delivered", "timezone": "Asia/Kolkata"},
  "previous_report": null
}
```

Shared header contract (delegated BigShip authentication, separate from AI Box's own `X-User-Id` login):

- Angular → FastAPI: `X-Shipment-Authorization: Bearer <BigShip login token>` (the same `localStorage.token` the
  BigShip interceptor sends). The scheme is case-insensitive.
- FastAPI → `POST {SHIPMENT_API_BASE_URL}/api/get-order-list`: `Authorization: Bearer <same token>` on the count
  request, the full-data request, every page of the clamped-page fallback and every retry.
- The token is opaque (JWT, Sanctum `id|secret`, or anything else): any visible ASCII without spaces, at most 8192
  characters. It is passed per request, never set as an HTTP-client default, never logged, stored in reports, put in
  error details or sent to DeepSeek. Redirects are not followed, so it never reaches another host.
- Missing header → 401 `shipment_auth_required`; not `Bearer <token>` (other scheme, spaces, `null`/`undefined`)
  → 401 `shipment_auth_invalid`. Both fail before any upstream call.
- Upstream 401 (or in-body `status_code: 401`) → 401 `upstream_auth_failed`: the session expired or was rejected,
  log in again. Upstream 403 → 403 `upstream_forbidden`: the session is valid but not allowed these shipments.
- CORS allows every request header (`allow_headers=["*"]`), so preflight accepts `X-Shipment-Authorization`
  together with `X-User-Id`/`X-User-Name`. A reverse proxy must forward the header unchanged (nginx does by default;
  its name has no underscore) and must not demand its own authentication on `OPTIONS` preflight requests.

`data` and `expected_records` are rejected in this mode (422). `filters.from_date`/`to_date` are required. `status` is
an Angular status label (`All Statuses`, `Pickup Scheduled`, `Not Picked`, `In-Transit`, `Out For Delivery`,
`Delivered`, `Undelivered`, `RTO Intransit`, `RTO Delivered`, `Cancelled`, `Lost`). `date_field` must be listed in
`SHIPMENT_API_DATE_FIELDS`. Ranges are limited to `SHIPMENT_API_MAX_RANGE_DAYS` and cannot end in the future.

Upstream calls, all with identical filters and the caller's token:

1. `page=1, perPage=SHIPMENT_API_COUNT_PAGE_SIZE` → `data.totalRecords` (never `data.total` or the status counters).
   0 → `no_matching_records`; more than `SHIPMENT_API_MAX_RECORDS` → `source_too_large`.
2. `page=1, perPage=totalRecords` (at most `SHIPMENT_API_MAX_PAGE_SIZE`). If fewer rows arrive, the server clamps the
   page size: the fetch restarts with the observed size and reads every page. Empty pages, a repeated page, a page of
   already-loaded shipments, rows shifting between pages, or a changing `totalRecords` stop the attempt; the whole
   fetch is retried `SHIPMENT_API_FETCH_ATTEMPTS` times, then fails. Rows are never truncated.

Request mapping (from the existing Angular integration): `type` = status code; `fromDate`/`toDate` = local midnight
of `from_date` and of the day after `to_date` (exclusive end), in IST (`Asia/Kolkata`) as `YYYY-MM-DD HH:MM:SS`; `date_filter_type` =
`date_field`; `only_order_stats` = 0; `segment_type` = `domestic` (set in backend code, independent of `.env`); every search field empty.

Success: the normal report. The coverage audit record's `completeness` adds `source: "shipment_api"`, `fetched_at`,
`upstream_requests`, `page_size`, `pages`, `fetch_attempts`, `page_size_clamped`, `upstream_query`,
`source_snapshot: null` (the API offers none), `data_as_of: null` and `reference_date` (today, used for ages):

```json
{"status": "complete", "expected_records": 272, "fetched_records": 272, "unique_records": 272,
 "duplicate_records_removed": 0, "conflicting_duplicates": 0, "outside_selected_period": 0,
 "outside_selected_status": 0, "analyzed_records": 272, "scope_verification": "partially_verified",
 "filters": {"applied_at_source": true,
             "date": {"field": "manifest_datetime", "verification": "not_verifiable", "missing_field_records": 272},
             "status": {"label": "Delivered", "verification": "verified", "checked_records": 272}},
 "source": "shipment_api", "fetched_at": "2026-10-21T06:12:03+00:00", "source_snapshot": null}
```

Every error has the standard envelope plus `retryable` and `request_id` (also the `X-Request-Id` response header):

```json
{"error": {"code": "incomplete_source", "category": "validation",
           "message": "Received 270 of 272 matching shipments. Please retry.",
           "details": {"expected_records": 272, "fetched_records": 270}, "retryable": true,
           "request_id": "5f0c2e8b9a7d4c1e8f3b2a6d0c9e7f14"}}
```

| HTTP | code | retryable | when |
|---|---|---|---|
| 401 | `ai_box_auth_required` | no | no AI Box login (`X-User-Id`) |
| 401 | `shipment_auth_required` | no | no `X-Shipment-Authorization` |
| 401 | `shipment_auth_invalid` | no | `X-Shipment-Authorization` is not `Bearer <token>` |
| 401 | `upstream_auth_failed` | no | the shipment API rejected the token (HTTP or in-body 401) |
| 403 | `upstream_forbidden` | no | the shipment API refused access |
| 422 | `request_validation_error` | no | malformed body, `data` in this mode, `from_date` after `to_date` |
| 422 | `unsupported_filter` | no | unknown status/date field/timezone, future dates, range too long |
| 422 | `no_matching_records` | no | `totalRecords` is 0, or nothing in scope |
| 422 | `source_too_large` | no | too many records, pages or response bytes |
| 422 | `clarification_required` | no | the prompt's period/status contradicts the selection |
| 429 | `upstream_rate_limited` | yes | upstream 429 (`Retry-After` passed on) |
| 409 | `source_changed` | yes | total changed or rows shifted during the fetch |
| 502 | `incomplete_source` | yes | fewer rows than `totalRecords` |
| 502 | `conflicting_records` | yes | one shipment returned with different values |
| 502 | `scope_mismatch` | no | returned rows outside the selected period/status (checked fields only) |
| 502 | `upstream_pagination_inconsistent` | yes | empty, repeated or wrong-size pages |
| 502 | `upstream_unavailable` | yes | network failure or 5xx (retried once first) |
| 502 | `upstream_rejected` | no | other 4xx, or `success: false` |
| 502 | `upstream_invalid_response` | no | not JSON, or missing/invalid `getOrderList`/`totalRecords` |
| 503 | `shipment_source_not_configured` | no | `SHIPMENT_API_BASE_URL` empty or not an https origin |
| 504 | `upstream_timeout` | yes | the shipment API exceeded its timeouts |
| 504 | `report_deadline_exceeded` | yes | the whole request exceeded `REPORT_DEADLINE_SECONDS` |
| 502/503/504 | `ai_*` | per code | DeepSeek failures (unchanged) |
| 502 | `ai_invalid_structure`, `ai_unverified_numbers` | no | final report validation failed (unchanged) |

No DeepSeek call is made before the fetch and completeness gate succeed. Upstream bodies, tokens and request headers
are never logged or returned; logs carry page numbers, counts, durations and the request ID.

Open contract gaps (not guessed):

- The returned rows carry no `manifest_datetime`, so that filter is `applied_at_source` but `not_verifiable`, and the
  report says so. The API's default date field (no `date_filter_type`) is undocumented and therefore not selectable.
- Supported `date_filter_type` values, `segment_type` values and `only_order_stats` behaviour are undocumented.
- Whether `toDate` is exclusive comes from the existing Angular comment, not API documentation.
- Timestamps without an offset are not assumed to be IST or UTC; near midnight they are counted as ambiguous.
- No snapshot or stable sort key is offered: consistency is checked by an unchanged `totalRecords` and
  non-overlapping pages, which cannot prove a concurrent change that keeps the total constant.
- Seller scope is enforced by the shipment API from the delegated token (the same authorization the browser had).
  AI Box's `X-User-Id` is not verified against that token.
- The fetch is synchronous inside the request (bounded by `REPORT_DEADLINE_SECONDS`); there is no job queue, so
  reports above `SHIPMENT_API_MAX_RECORDS` need a narrower range until a worker exists.

## Calculations and scope

All rates use supplied records, never unrelated summary counters. Paginated `getOrderList: {data: [...], total: 1000}` retains total separately in scope.reported_total. `dataset_scope: "sample"` explicitly marks a supplied sample; a bare list is labelled supplied_records, never presumed a full dataset. Sanitized AI samples are qualitative only.

Statuses use the explicit normalized text mapping in shipment_prompt.STATUS_MAPPING. Unknown strings or numeric codes remain other and produce a limitation. This repository supplies no authoritative numeric status-code catalog; add verified business codes to that map before using them. Courier outcome counts and rates are calculated before AI generation. Grouped presentation tables have documented limits; omitted groups do not mean zero.

The AI decides relevant narrative sections from the actual user prompt, with backend-calculated tables available for reference. It cannot supply arbitrary tables/charts. The backend validates references, blocks, length limits, section ids/orders and chart shapes. Observed numeric claims are checked against typed backend metrics. Numeric strings, list lengths, prompt numbers and unrelated financial figures are not count evidence. Named derived_percentages contain deterministic numerator/denominator calculations within the supplied-record scope. Percentage rounding tolerance is half of the final stated decimal place, capped at half a percentage point; counts must match exactly. Equivalent numeric formats such as 25%, 25.0% and 25.00% compare equally; identifiers, JSON field names and numbered-list markers do not count as claims. This numeric check cannot prove that a number is attached to the correct semantic claim; management should review narrative interpretation. It does not replace deterministic calculation checks.

## Storage and operations

Storage is private process memory, capped at 500 reports, expiring after 24 hours. Restarting loses reports; multiple workers do not share storage. Run one worker for this simple implementation. Integrate an authenticated user dependency and shared owner-indexed persistence for account-based access or multi-worker deployment. Tokens authorize possession of a report; they do not establish a user identity. Generation and legacy caller-payload exports remain unauthenticated.

DeepSeek JSON mode, SDK transient retries, two attempts for malformed JSON, API timeout/error handling, request/record/prompt limits and PII allow-list sanitization remain in place. AI context is capped at 250,000 characters. Schema-invalid JSON returns 502. Numerical evidence failures trigger at most two targeted corrections (three generations total). Correction input includes the prior schema-validated, PII-scrubbed report and typed field-specific validation errors. DeepSeek must correct unsupported fields and preserve valid content; every correction is validated. Exhaustion returns 502 with a trace_id, attempts and safe field paths, without rejected values or report text. Structured JSON logs identify rejected values, fields, section ids, semantic types, reasons and typed comparison metrics under the trace_id. Long digit strings that may be PII are redacted. Correction calls occur only after numerical validation failure. No API key was read or changed for this task.

## Verification

Run `.\.venv\Scripts\python.exe -m pytest -q`. The suite uses fake AI responses and existing shipment fixtures, so it requires no network or DeepSeek key. It checks prompt forwarding and structured contracts, not live AI quality. PDF bytes and DOCX contents are tested; visual document review and live DeepSeek generation were not performed.


## Numerical validation repair

The former validator compared a global bag of numbers, including list lengths, prompt values and an unconditional 100. It could reject valid derived percentages while accepting unrelated numbers. The replacement records exact rejection diagnostics and uses count, percentage, amount, weight, date and recommendation semantics. Known dates/years, identifiers and section numbering are handled separately. Explicit top-N requests permit ranking format only; they never authorize shipment claims. Server policy disables proposed numeric targets, including user-requested targets, until a future policy explicitly approves them.

This remains a conservative text validator, not a general proof of every free-text statement. It supports named dataset-boundary dates and named backend ratios; arbitrary ratios, numerical deadlines and unsupported historical comparisons are rejected. Matching a numeric value does not prove every causal conclusion. Use backend tables as the primary numerical evidence. No stored response schema or export/token API was changed.

Run `.\.venv\Scripts\python.exe -m pytest -q` for the offline regression suite. Live DeepSeek and the original rejected response were not available during this repair; the new structured logs diagnose the next live rejection precisely.


## Deterministic repair after correction exhaustion

Three model generations remain the maximum. After the third numerical rejection the service attempts a conservative sentence removal in narrative fields only. The validator now supplies offsets into the exact original field text; logging/correction diagnostics from safe_rejections are redacted and truncated and are never used as edit coordinates. List-marker and date masking preserves those offsets.

Repair operates on a copy and removes complete unsupported sentences. It never substitutes numbers, modifies KPI/table/chart/dataset values, deletes sections or list entries, or repairs titles/metadata. Ambiguous sentence boundaries, stale offsets, empty resulting fields, sentences sharing protected numerical occurrences, and immediately dependent following statements fail closed. Schema and numerical validation run again before returning the repaired report, and normal orchestration validates it again before storage. No fourth AI call occurs.

Structured report_numeric_repair events include trace_id, attempt=3, field, redacted unsupported value, rejection reason, repair_status and safe repair_reason. Failure retains the existing controlled 502 and trace ID. A successful repair exports and stores through the existing paths without changing the Angular response schema. Offline tests cover repeated failure, exact-span sentence repair, dates/identifiers/decimals, stale metadata, required fields, protected numbers, dependent statements and revalidation.


## Schema correction and shipment financial reports

The prompt now includes StructuredNarrative.model_json_schema(): required/optional fields, bounds, enums and supported blocks come directly from Pydantic. The AI accepts paragraph, bullet_list and table_reference blocks only; Python expands verified references into display tables and calculates charts. Inline table or chart blocks remain invalid. Legacy paragraph responses remain compatible when explicitly shaped as legacy content.

Schema errors log report_schema_rejection events with trace_id, generation attempt, location, type and safe message; input/ctx are excluded. Unknown extra-field names and invalid discriminator values are redacted to avoid leaking untrusted data. One schema-only correction retry is permitted per request, preserving the original analysis context. Persistent schema failure returns controlled 502 with trace_id and safe field paths. Numerical validation retains its three numerical attempts and deterministic fallback; at most one additional schema-correction call can occur. No partially validated report is stored.

Shipment financial calculations read only totalCharge, not collectableAmount or invoice value. shipping_financial contains total_shipping_charges, average_charge_per_order, charges_associated_with_cancelled_orders, valid/missing/invalid counts, courier_charges, payment_mode_charges, and COD/ToPay/Prepaid totals. Calculations use Decimal; JSON money is an exact two-decimal string, rounded half up. Missing/null/non-finite/negative/invalid charges are excluded, never silently zeroed. Average denominators are orders_with_valid_charge. Subtotals with records but no valid amounts are unavailable; no cancelled records have a known zero cancelled-charge total. Group tables show up to 200 groups; totals use all supplied records.

Financial prompts select four display KPIs: Total Shipping Charges, Average Charge per Order, Charges Associated with Cancelled Orders, Orders Analyzed. Monetary display metrics retain unit=text and exact decimal string values, preserving the current schema and exporters. Currency is not inferred; source values must use compatible currency units. Charges are not evidence of revenue, profit or cancelled-shipment losses. Unsupported monetary claims remain rejected by numeric evidence validation. Standard shipment KPIs remain unchanged for other prompts.

See samples/financial_shipment_request.json and samples/financial_shipment_response.json for an illustrative offline fixture and fake-AI response (not a live or production report). The fixture demonstrates exact total 10.30, valid-charge average 3.43, cancelled charges 10.00, COD 0.10, ToPay 0.20 and Prepaid 10.00. Invalid and missing charges are excluded. No fixture is substituted into live requests.

The historical schema log contained only an error count, not the response/failed field, so its exact field cannot be reconstructed. The regression reproduces an inline AI table failing at sections[0].blocks[0] (union_tag_invalid) and verifies correction to a supported table_reference. Live DeepSeek testing was not performed.


## Financial unit-classification regression

Production trace 7f08cd627a7940b3b566ed46c1d63667 revealed that cancelled-order charges were incorrectly registered as counts, counted financial coverage labels were treated as amounts, and payment-mode monetary shorthand was treated as counts. Registry classification now explicitly includes charges_associated_with_cancelled_orders as money. Narrative classification respects count labels/parentheses/status adjectives and monetary comparison wording. "X of Y" is count coverage only when its denominator is explicitly orders/records, avoiding misclassification of monetary shares. Every value must still match evidence of the selected unit; retries and fail-closed repair remain unchanged.

Safe schema/numeric/repair events are persisted in private output/report_validation.jsonl (rotation at 1 MB with two backups). Narrative context is excluded from the file; existing redacted diagnostic values, field paths, numeric comparison metrics and trace IDs are retained. Audit-write failures do not block generation. This backend path is not served over HTTP. The added regression uses values and sentences from the supplied logs; it does not substitute them into production data or constitute a new live DeepSeek test.
