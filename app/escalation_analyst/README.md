# Escalation Analyst backend

This package adds the WhatsApp workspace to AI Box at **`/api/escalation`**. Existing `/api/reports`, insights, chart and canonical-report APIs keep their own code and database. The only host integration is `install_escalation_analyst(app)` in `app/main.py`; its lifespan wrapper preserves the existing host lifespan.

## Where to change code

- `api/`: request validation and routes, grouped by page/API area.
- `repositories/`: database queries, schema, durable jobs and imports.
- `analysis/`: streaming DeepSeek calls, report reuse, group/master analysis and metric parsing.
- `logistics/`: AWB detection, classification, evidence/TAT and Excel export.
- `performance/`: mappings, one frozen dataset, ten sheets, conversation details and template styles.
- `whatsapp/`: Node transport client, capture and Gemini media extraction.
- `exports/`, `utils/`, `prompts/`: document downloads, IST/JS compatibility and editable prompt files.

Dashboard routes reuse the host's `X-User-Id` identity dependency. As with AI Box, a trusted ERP gateway must authenticate that identity before forwarding it. The workspace is shared across its operators; presets are scoped by user. Internal capture additionally requires `X-Sidecar-Secret`. No old dashboard password/session is imported.

## Configure and run

Copy the variables from this folder's `.env.example` into the **host project's existing `.env`**. Do not replace the host file. No new runtime dependencies are needed; SQLite is built in, while psycopg, httpx, OpenAI, openpyxl and ReportLab already exist in requirements.

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

By default, storage is a separate `data/escalation.sqlite3` file. `ESCALATION_SQLITE_PATH` can select another location. With `ESCALATION_DATABASE_URL`, only the private PostgreSQL schema `escalation_analyst` is created. Existing `saved_reports` tables are never migrated or altered. Schema creation happens on first access, not import. Missing AI/WhatsApp credentials affect this module's operations, not AI Box startup.

POST analysis creates a durable job and returns immediately. Workers persist results/errors; the UI polls status. Group and master jobs share database claims, and heartbeats protect active jobs. Expired claims become errors on the next submission. Snapshot generation, filters, details and Excel exports use the same deterministic dataset. Schedules and daily message retention are **off until `ESCALATION_SCHEDULER_ENABLED=true`**; reports are never purged. Set retention days to `0` to keep messages.

## WhatsApp transport and cutover

The optional Node entry point is `E:\Office\whatsapp-ai\src\sidecar\server.js`. It retains `whatsapp-web.js`, the existing QR/recovery/name/backfill fixes and the original `.wwebjs_auth` directory. Python owns message storage, Gemini, reports and schedules. The old Node entry point is unchanged.

1. Back up the old database. Run the import dry-run below, inspect counts, then add `--apply`. Do not point the import at the AI Box database as its source.
2. Put the same randomly generated `WHATSAPP_SIDECAR_SECRET` in both projects. In Node set `ESCALATION_PYTHON_URL=http://127.0.0.1:8000/api/escalation`.
3. Start Python. **Stop the old Node bot before starting the sidecar**; two clients must never share the authentication profile. From the original `whatsapp-ai` root run `node src/sidecar/server.js`. This implementation has not performed that live switch.
4. Check `/api/escalation/whatsapp/status` with the ERP identity header. Reconnect returns the real Node QR when pairing is required. Connect Angular services to this prefix separately: the current ERP UI still uses demo data.

```powershell
.venv\Scripts\python.exe -m app.escalation_analyst.migrate --source-sqlite E:\Office\whatsapp-ai\data\messages.db
# Add --apply only after reviewing the dry run. For presets add --owner-id <ERP user ID>.
# PostgreSQL source: --source-postgres <connection-url> --source-schema public
.venv\Scripts\python.exe -m pytest tests/escalation_analyst -q
```

Import is repeatable and keeps a raw source archive. Historical deterministic runs receive numeric Python IDs and retain their original UUID as `request.legacyId`. Source sessions and running locks are not copied. Imported running snapshots become errors. Old master-run progress stays in the archive; saved AI reports remain available. Node API keys are archived/imported as hashes, but the optional legacy `/api/v1` key-auth facade is not mounted; use the ERP identity API above.

## Validation and limits

Golden tests cover original Node ten-sheet columns/values, KPI/status/TAT distributions, reference Excel positions/formulas/cached values, deduplication, locks, disconnected access, report reuse, exports and migration. Compare another captured workbook with `scripts/compare_escalation_workbooks.py old.xlsx new.xlsx` before a production cutover. Real provider calls, PostgreSQL import and live pairing require your environment and have not been exercised by offline tests.

PDF uses the host's ReportLab instead of adding Chromium; its layout differs from the old HTML PDF. Markdown/Word exports preserve escaped content; their layout is simpler than the original dashboard renderer. Media payloads are limited to 6MB so base64 fits the existing host 10MB body limit; CSV/XLSX parse locally, other formats use Gemini. These differences are explicit; no provider output or connection success is simulated by the backend.
