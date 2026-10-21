---
name: shipment-analysis
description: Calculate verified shipment counts and categorical charts.
---

Production runtime workflow only. Execution is allowlisted in app/report_engine/service.py and
app/analysis/executor.py (typed AnalysisPlan sections, executed by section_reports.py).
Read references/contract.md for constraints; never execute generated or downloaded code.

AI Box reports reuse AnalysisPlan and its typed ExecutionSection coverage list.
Section IDs, titles, order, dimensions, filters, metrics and visuals come from the current prompt.
There is no fixed list of report sections; compose registered analytical operations.
Use schema metadata before fetching rows, not claimed data facts.
A conflict with protected UI scope or materially ambiguous intent requires clarification before fetch.
Plan before aggregating. Return allowlisted fields, cohort, ordered sections, metrics, ranking,
charts, missing-data handling and material clarifications, never executable code or a finished report.
Buyers means consignees; "buyers first" specifies section order. Established consignee IDs are
preferred. Name/city/pincode keys are approximate and must be labelled; do not infer first-time buyers.
RTO-pending means explicitly pending/initiated/in-transit return-to-origin statuses. Pickup Scheduled,
Not Picked and Undelivered are not RTO. Unqualified RTO has unknown return completion, not assumed pending.
Courier ranking defaults to shipment volume and must not be described as best performance.
Destination city plus pincode is a compound grouping; states are not a substitute.
Keep a requested zero-result or unavailable section. Python computes all facts; narrative cites
metric IDs only, avoids invented causes/reasons, and retains truthful scope/coverage limitations.
