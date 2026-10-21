# Report accuracy gate

`ReportService.generate_report` freezes the source-gated data into request-local JSON before analytics.
`app.analysis.accuracy.validate_final` runs after insights, coverage repair, previous-report merging,
scope labels, and customer-response serialization. It independently reads the immutable source
snapshot; it does not fetch again or ask the language model to validate anything.

The existing source gate remains authoritative for completeness and strict/partial scope policy.
Its raw rows, duplicate counts, removed rows, source total, selected date field and timezone remain
separate from the normalized dataset and the intent-filtered analysis subset. Dataset and subset
fingerprints identify the evidence. Request/report IDs are assigned before the upstream call.
The authenticated AI Box account and delegated shipment credential are represented by a one-way
scope digest; this does not assert a seller identity that the shipment service did not supply.

## Checks and failure policy

The final gate checks source reconciliation and identities, normalization (including nested city,
state and transit timestamps), subset identities/count, status percentages, courier/destination
counts, financial totals/averages/median, visible metric cards, typed/display table consistency,
categorical chart values, declared top-N coverage, and requested components from the plan.
Money is read independently with Decimal. Monetary displays use two decimal places and half-up
rounding; existing one-decimal percentage/duration displays use half-even rounding. Counts are
exact. Missing money is not zero. The v3 median follows the documented dashboard definition:
sorted values at index `n // 2` (upper middle for an even cohort).

Accuracy checks run in audit-only mode by default (`REPORT_ACCURACY_STRICT=false`). Numerical,
identity and scope-label failures stay FAIL in CMD, but delivery is allowed; the result line includes
`delivery=allowed policy=audit_only`. Set `REPORT_ACCURACY_STRICT=true` to block these failures with
HTTP 422 `report_accuracy_failed`. Public-schema and unreadable-component failures still block in
either mode. Authentication and the earlier source gate retain their existing policy and errors.
Failed components remain in the evidence instead of being silently removed.

Missing requested city/courier/state/RTO-reason groupings are rebuilt when possible. Missing delivery
duration output is rebuilt from consistent delivered timestamps, or explicitly marked unavailable.
Coverage limitations remain visible even if optional limitations were omitted by the prompt.
Cancellation reasons are never used as RTO reasons.

Source-account counters are separate from fetched-cohort and analysis-subset counts. A missing
manifest timestamp is WARN/UNVERIFIED; orderDate is never used to verify manifest scope. Existing
source policy is unchanged. Noncritical missing coverage and chart forms without an independent
mapping log WARN, not PASS. General natural-language semantics remain UNVERIFIED; existing numeric
evidence validation still runs, plus independent checks of explicit final cohort-count claims.
The checker does not claim full natural-language entailment or independent verification of every
specialized chart form. Diagnostic checklists are omitted from customer JSON and exports.

## Terminal and structured events

One logging record contains the whole block, preventing lines from simultaneous requests from
mixing. Repeated successful table-cell checks are summarized. `LogRecord.report_check` contains
the structured event with individual check IDs, expected/actual values and component references.
The normal configured INFO console handler prints the block. ASCII is selected when the stderr
encoding cannot represent the check symbols. No full rows, credentials, customer/category names,
addresses, phones, or full prompts appear in these blocks. Prompts are represented by a digest.

Example shape (illustrative counts, calculated from the actual request at runtime):

```text
[REPORT CHECK] request_id=... report_id=... dataset=...
[PASS] source_count expected=246 actual=246 ref=source.raw ...
[PASS] analysis_subset expected=124 actual="124" ref=records_analyzed precision=0
[FAIL] insights_median_charge expected="172.10" actual="174.26" ref=insights.kpis.median_charge precision=2
[WARN] selected_date_scope expected="manifest_datetime" actual="not_verifiable" ref=source.filters.date ...
[RESULT] FAILED checks_passed=... checks_failed=1 warnings=...
```

## Export revision binding

The public `validation_token` is an opaque HMAC receipt binding the request/report IDs, dataset
fingerprint, exportable revision digest and full response digest. Angular must preserve it when
posting an export or saving a report. No diagnostic checklist is included in the token.
PDF/Word generation does not recalculate metrics. Service exports, saves, and saved-file downloads
verify the receipt. Pure chart presentation edits first verify the previous receipt and then sign
the edited revision without fetching or recalculating values.

Modified, unsigned legacy, or invalidly signed shipment export inputs return HTTP 409
`report_revision_mismatch`. Regenerate a legacy report to obtain a validated revision. Other report
types keep their existing export behavior. The separate canonical report engine retains its
existing persisted-version checks; this gate serves the shipment AI Box report pipeline.

Set `REPORT_VALIDATION_SIGNING_KEY` to a stable random secret of at least 32 bytes for deployments
with multiple hosts. Without it, a private 32-byte key is created atomically in
`OUTPUT_DIR/.report_revision.key`, shared by workers using that output directory and retained across
restarts. Preserve this key when moving saved reports; rotating/removing it invalidates old receipts.
Neither the key nor diagnostic evidence should be served as public files.

Run `.venv/Scripts/python.exe -m pytest -q`. Synthetic regressions are in
`tests/test_report_accuracy.py`; existing shipment-source, source-scope, intent, saved-report,
chart-edit and export tests cover integration behavior.

## Files changed for this task

- Added `app/analysis/accuracy.py`, `tests/test_report_accuracy.py`, and this document.
- Source evidence and mappings: `app/analysis/completeness.py`, `app/prompts/shipment_prompt.py`,
  `app/charts/fields.py`, `app/services/shipment_source.py`, `app/services/shipment_insights.py`.
- Plan requirements: `app/analysis/interpreter.py`, `app/analysis/vocabulary.py`.
- Final gate, privacy, errors and receipts: `app/services/report_service.py`,
  `app/models/report_schema.py`, `app/report_visibility.py`, `app/errors.py`.
- Save/export consistency: `app/api/report_routes.py`, `app/api/insights_routes.py`,
  `app/storage/user_reports.py`.
- Configuration example: `.env.example` (shared signing-key option).
- Updated regression expectations for internal diagnostics and truthful coverage:
  `tests/test_dynamic_reports.py`, `tests/test_report_intent.py`, `tests/test_reports.py`,
  `tests/test_shipment_insights.py`, `tests/test_shipment_source.py`.

Validation result: **515 passed** with the full pytest suite. No commit or deployment was performed.


## Requested-content workflow

Shipment reports default to `REPORT_AI_PLANNER=true`. The first DeepSeek call receives the exact
prompt, protected UI/account scope, available normalized field definitions, supported operations,
relevant runtime skills and the typed plan schema before any shipment fetch or report metric calculation. Follow-up
requests additionally receive existing report identifiers and section IDs. The existing AnalysisPlan
now carries ordered ExecutionSection values with arbitrary IDs and titles composed from registered
metrics, dimensions, filters and visuals. There is no fixed report-section list. Unknown operations,
code and SQL are rejected before source access.

Python executes the approved sections over request-local validated data. Buyers mean consignees;
established IDs produce opaque grouping labels, while name/city/pincode fallback identities are
explicitly approximate. RTO-pending uses explicit return-stage statuses and excludes pickup-pending
and undelivered shipments. Requested city/pincode groupings cannot become state tables. Courier
rankings declare their metric; shipment volume is the default. Missing charges remain unavailable.

The second DeepSeek call receives verified section metrics and tables for grounded narrative.
Invalid narrative falls back to factual text for the same ordered sections. An empty overall cohort
returns `no_matching_records`; a zero-result requested section in otherwise nonempty data remains
explicit. Tables and section order are shared by the public structured JSON and PDF/Word exports.

`verify_section_coverage` independently recounts source groups, denominators, ranking values and
financial totals and checks final section order, chart coverage and export table parity. Essential
coverage failures block delivery even when `REPORT_ACCURACY_STRICT=false`; general accuracy
checks retain the configured audit-only behavior. Internal diagnostic evidence stays in terminal
logs, including section references, instead of customer report text.

Implementation: `app/analysis/{plan,interpreter,executor,section_reports}.py`,
`app/services/report_service.py`, normalized shipment fields, runtime skill registry/instructions,
configuration and `tests/test_ordered_reports.py`. Legacy report regression fixtures explicitly test
`REPORT_AI_PLANNER=false`; ordered workflow tests explicitly enable the production default.

Validation after the dynamic planning update: **580 passed** (`.venv/Scripts/python.exe -m pytest -q`).
DeepSeek responses are mocked in these tests; no live provider call or deployment was performed.


The dynamic pipeline performs prompt interpretation before the backend totalRecords query. The source
query remains the UI-selected scope; prompt subsets execute locally to preserve comparisons and rate
denominators. Plan rejection makes no upstream calls. Tests cover uncapped full fetch and capped-page
pagination, delegated-token isolation, English/Hindi/Hinglish plan contracts, arbitrary section IDs,
new combinations of zone/payment/weight, missing fields, zero subsets, invalid plans, stale-report
isolation, grounded fallback, numeric charts through registered builders, and matching exports.
The multilingual tests script model plans and verify exact prompt forwarding; they do not measure
live DeepSeek language comprehension. The existing authentication/completeness regressions remain enabled.


### Browser JSON round trips

New receipts declare `numeric_format=json_numbers_v1` and normalize integral floats to integers
when hashing signed public content. JavaScript JSON serialization converts values such as `251.0`
to `251`; these equivalent representations must not invalidate an otherwise unchanged report.
Strings, nulls, booleans and actual numeric values remain distinct. Unknown formats are rejected.
Existing receipts without the marker retain their original verification rules. After deploying
this fix, regenerate reports that failed saving so they receive a new receipt. The signing key
and database contents do not need changing.
