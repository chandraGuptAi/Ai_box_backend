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
- `previous_report` content is merged only when it was built from the same records (`data_fingerprint`) and the same
  `customer_id`; otherwise the new report stands alone and `generation.notice` explains why.
- `REPORT_AI_PLANNER=true` (default false) allows one extra, small DeepSeek call when the rules cannot read a condition
  the user stated; its JSON plan is validated against the same field/operator/value vocabulary. No code or query from
  DeepSeek is ever executed.

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
