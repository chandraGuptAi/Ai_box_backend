# Escalation Analyst integration

Add app/escalation_analyst as an isolated FastAPI subapplication mounted at /api/escalation. Existing AI Box routers, exceptions, report models and storage remain unchanged. Reuse app.api.auth.get_current_user for dashboard identity; that host dependency trusts upstream X-User-Id and must remain behind the same trusted ERP gateway. Internal ingest additionally requires a distinct shared sidecar secret.

Use ESCALATION_DATABASE_URL when supplied; otherwise a separate local SQLite database. PostgreSQL uses its own escalation_analyst schema. Database initialization is lazy, no import-time migration against AI Box tables. SQL is restricted to repositories. Jobs and locks are persisted; scheduler is opt-in, lifespan owned explicitly by integration hooks. Missing AI/sidecar configuration affects only this module.

Folders: api (thin routes), repositories (SQL), analysis, logistics, performance, exports, whatsapp, utils, prompts. Keep most files below 300 lines. Copy source fixtures and prompts verbatim. Never copy secrets or .wwebjs_auth.

Node retains whatsapp-web.js, its authentication directory and compatibility fixes. Add an optional bridge entry point; do not run both clients with the same profile. Python owns captured records, media extraction, analysis and reporting after cutover. No production cutover or data deletion during implementation.

Order: database and date helpers; groups/clusters/settings; sidecar/ingest; analysis/jobs/master; history/exports; logistics goldens; performance/workbooks; scheduler and migration utility. Test with temporary SQLite and mocked providers/sidecar, compare deterministic outputs against Node, then run all 625 existing AI Box regressions. Baseline: 625 passed.
