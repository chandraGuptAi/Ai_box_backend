# ai_box_backend

FastAPI backend for the Angular **AI Box** module. It takes JSON data and a user prompt from Angular, calculates metrics in Python, asks DeepSeek for an analytical write-up of those metrics, and returns structured report JSON for preview. The same report can then be exported as **DOCX** or **PDF**.

The first report type is the **Shipment Analysis Report**. You can add more types (courier performance, orders, escalations, finance, custom) without changing the API, the services or the generators. See [Adding a report type](#adding-a-report-type).

> ⚠️ **No authentication yet.** This version is meant for local and internal development. Before any production deployment, put authenticated access in front of it (see [Before production](#before-production)).

---

## How it works

```
Angular ──POST /api/reports/generate──▶ report_routes
                                         │
                                         ▼
                                   report_service
                                   1. pick ReportDefinition by report_type (prompts/registry.py)
                                   2. analyze(data): validate, extract allow-listed fields,
                                      strip personal data, calculate metrics in Python
                                   3. build prompt: rules + metrics + sanitised sample
                                   4. deepseek_service: JSON output mode, timeouts, error mapping
                                   5. validate the AI JSON with Pydantic
                                         │
Angular ◀──── ReportResponse ────────────┘   key_metrics/tables/metrics are calculated in Python
                                             content (narrative) is written by the AI
Angular ──POST /api/reports/export/{docx|pdf} (same JSON)──▶ generators ──▶ file download
```

**Accuracy by design:** every number in `key_metrics`, `tables` and `metrics` is calculated in Python. The AI only writes the narrative (`content`), it is told to quote those metrics exactly, and it must list anything the data cannot support under `data_limitations`.

**Privacy by design:** raw orders never go to DeepSeek. Only an allow-list of operational fields is read: status, courier, payment mode, cancellation reason, state, city, zone, date, value and weight. Customer names, mobile numbers, emails, addresses, pincodes and AWB numbers are never extracted. Anything in free text that looks like an email address or phone number is replaced with `[redacted]`.

## Project structure

```text
ai_box_backend/
├── app/
│   ├── main.py                  # FastAPI app, CORS, error handlers, /health
│   ├── config.py                # Settings from .env
│   ├── errors.py                # Error types + JSON error format
│   ├── middleware.py            # Request body size limit
│   ├── api/report_routes.py     # /api/reports/* endpoints
│   ├── services/
│   │   ├── deepseek_service.py  # All DeepSeek API logic
│   │   └── report_service.py    # Orchestration + exports
│   ├── generators/
│   │   ├── docx_generator.py    # Word document (python-docx)
│   │   └── pdf_generator.py     # PDF document (ReportLab)
│   ├── prompts/
│   │   ├── base.py              # ReportDefinition, shared AI rules, output format
│   │   ├── registry.py          # report_type → ReportDefinition
│   │   └── shipment_prompt.py   # Shipment extraction, metrics, prompt
│   └── models/report_schema.py  # Pydantic request/response/report models
├── samples/
│   ├── shipment_request.json            # Sample request (12 orders, includes PII fields)
│   └── shipment_response_example.json   # Example response
├── tests/test_reports.py        # API tests (DeepSeek is mocked)
├── output/                      # Optional private copies of exports (git-ignored, never served)
├── .env / .env.example
├── requirements.txt / requirements-dev.txt
└── README.md
```

---

## Setup (Windows)

Requires **Python 3.12+** and **Docker Desktop** running.

```powershell
cd E:\Office\AI_Box_backend

# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
#   If PowerShell blocks the script, run this once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#   (In cmd.exe use:  .venv\Scripts\activate.bat)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
copy .env.example .env
#   then edit .env and set Ai_box_key=<your DeepSeek API key>

# 4. Start PostgreSQL (waits until ready)
docker compose -f docker-compose.yml -f compose.local.yaml up -d --wait postgres

# 5. Run
uvicorn app.main:app --reload --port 8000
```

Reports are stored in PostgreSQL using `DATABASE_URL` from `.env`. The Docker
service uses PostgreSQL 17 on `127.0.0.1:5434` and keeps its data in the
`ai-box_postgres_data` Docker volume. The example credentials are for local
development. If changing credentials or the published port, update both the
`POSTGRES_*` settings and `DATABASE_URL`. The backend runs on the host; a
containerized backend connects to `postgres:5432` automatically. For the complete
Docker backend + private PostgreSQL stack and Ubuntu deployment, see
[DEPLOYMENT.md](DEPLOYMENT.md). Use `docker compose up -d --build` for that stack.

Storage initialization fails if PostgreSQL is unavailable. Start the database
before starting the backend or running tests. There is no SQLite fallback.
`docker compose stop` preserves the database; `docker compose down -v` deletes it.

To copy legacy saved reports from `data/ai_box.db`, run
`python -m scripts.migrate_sqlite_to_postgres`. This preserves the SQLite file
and skips report IDs already present in PostgreSQL.

Check that it works: open <http://localhost:8000/health>.

```json
{"status": "ok", "service": "AI Box Report Backend", "version": "1.0.0", "environment": "development", "ai_configured": true, "timestamp": "..."}
```

If `ai_configured` is `false`, the API key has not been set. `/generate` will then return `503 ai_not_configured`.

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `Ai_box_key` | – | DeepSeek API key (**server only**) |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Model name |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API base URL |
| `DEEPSEEK_TIMEOUT_SECONDS` | `120` | HTTP timeout per AI call |
| `DEEPSEEK_MAX_RETRIES` | `0` | SDK retries on connection, 429 and 5xx errors. Keep 0: a timed-out call may still be billed, so each report makes exactly one DeepSeek call |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | `4096` | Maximum length of the AI response |
| `DEEPSEEK_TEMPERATURE` | `0.2` | Kept low for factual output |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:4200` | Comma-separated Angular origins |
| `MAX_REQUEST_BYTES` | `10485760` (10 MB) | Request body limit (413 above it) |
| `MAX_RECORDS` | `20000` | Maximum orders per request |
| `MAX_PROMPT_CHARS` | `2000` | Maximum user prompt length |
| `AI_SAMPLE_RECORDS` | `25` | Sanitised example records sent to the AI (`0` sends metrics only) |
| `ENABLE_DOCS` | `true` | Serve `/docs`, `/redoc` and `/openapi.json` |
| `SAVE_GENERATED_FILES` | `false` | Keep a private copy of exports in `output/` |
| `PDF_FONT_REGULAR` / `PDF_FONT_BOLD` | auto | Unicode TTF fonts for the PDF (Arial is found automatically on Windows) |

Variables set in the system environment override `.env`.

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/reports/types` | Supported report types (for an Angular dropdown) |
| POST | `/api/reports/generate` | Generate report JSON |
| POST | `/api/reports/export/docx` | Report JSON → `.docx` download |
| POST | `/api/reports/export/pdf` | Report JSON → `.pdf` download |

### `POST /api/reports/generate`

```json
{
  "report_type": "shipment",
  "prompt": "Analyze shipment status and cancellation reasons",
  "data": { "getOrderList": [ { "order_status": "Delivered", "courier_name": "Delhivery", "...": "..." } ] }
}
```

- `prompt` is optional. When it is empty, a default analysis focus is used.
- The orders are read from `data.getOrderList`. The full Angular API response shape, `data.data.getOrderList`, and `data.orders` also work, as does a paginated `getOrderList: {"data": [...]}`.
- Field names are matched flexibly. `order_status`, `orderStatus` and `shipment_status` are all read as the status. Nested objects such as `consignee.state` are supported too. The alias lists are `FIELD_ALIASES` in [app/prompts/shipment_prompt.py](app/prompts/shipment_prompt.py). If your Angular order objects use other key names, add them there.

**Response** (trimmed; the full example is in [samples/shipment_response_example.json](samples/shipment_response_example.json)):

```json
{
  "report_id": "dcc37b7d61454a5ea668a6bf7200a284",
  "report_type": "shipment",
  "report_name": "Shipment Analysis Report",
  "generated_at": "2026-10-13T05:27:37Z",
  "ai_model": "deepseek-chat",
  "prompt": "Analyze shipment status and cancellation reasons...",
  "records_analyzed": 12,
  "key_metrics": [
    {"label": "Total Orders", "value": "12", "note": null},
    {"label": "Delivered", "value": "3 (25.0%)", "note": null},
    {"label": "Cancelled", "value": "4 (33.3%)", "note": null},
    {"label": "Top Cancellation Reason", "value": "Customer not available", "note": "2 of 4 cancellations with a reason"}
  ],
  "tables": [
    {"title": "Courier Performance",
     "columns": ["Courier", "Orders", "Delivered", "Cancelled", "RTO", "Delivery Rate (%)"],
     "rows": [["Delhivery", "5", "1", "1", "0", "20.0"], ["Xpressbees", "3", "0", "2", "1", "0.0"]]}
  ],
  "content": {
    "title": "Shipment Analysis Report: 02–07 January 2025",
    "executive_summary": "This report analyses 12 orders ... Only 3 orders (25.0%) were delivered, while 4 orders (33.3%) were cancelled ...",
    "sections": [{"heading": "Cancellation Analysis", "content": "..."}],
    "findings": [{"title": "Cancellations exceed deliveries", "detail": "4 orders (33.3%) ... versus 3 delivered (25.0%).", "severity": "high"}],
    "recommendations": [{"action": "Introduce pre-dispatch confirmation for COD orders", "rationale": "...", "priority": "high"}],
    "data_limitations": ["The dataset is small (12 orders); percentages may not be representative."]
  },
  "metrics": { "total_orders": 12, "rates": {"delivered_percent": 25.0, "...": "..."}, "courier_performance": ["..."], "...": "..." },
  "disclaimer": "This report was generated with AI assistance ..."
}
```

`metrics` holds the full Python-calculated data set: status breakdowns, courier performance, cancellation reasons, payment modes, states, cities, date range, order value, field coverage and data quality notes. You can use it to draw charts in Angular.

### `POST /api/reports/export/docx` and `/export/pdf`

Post back the object returned by `/generate`, with any edits the user made in the preview. The response is a file download with a `Content-Disposition: attachment; filename="shipment_report_YYYYMMDD_HHMMSS.pdf"` header. Files are generated in memory and are never served from a public URL.

### Errors

Every error has the same shape:

```json
{"error": {"code": "invalid_report_data", "category": "validation", "message": "data.getOrderList contains no orders to analyse.", "details": null}}
```

| HTTP | `category` | `code` examples | Meaning |
|---|---|---|---|
| 400 | validation | `unsupported_report_type` | Unknown `report_type` |
| 413 | validation | `request_too_large` | Body over `MAX_REQUEST_BYTES` |
| 422 | validation | `request_validation_error`, `invalid_report_data` | Bad request schema, or unusable data |
| 502 | report_generation | `ai_invalid_response`, `ai_response_truncated`, `ai_authentication_failed`, `ai_insufficient_balance` | AI call or AI output failed |
| 503 | report_generation | `ai_not_configured`, `ai_unavailable`, `ai_rate_limited` | AI not available |
| 504 | report_generation | `ai_timeout` | AI did not respond in time |
| 500 | export / server | `report_export_failed`, `internal_error` | Document or server failure |

Show `error.message` to the user. Use `error.category` to choose between "fix your input" and "try again later".

---

## Testing with Swagger (`/docs`)

1. Start the server and open <http://localhost:8000/docs>.
2. **GET /health**: click *Try it out*, then *Execute*. Check that `ai_configured: true`.
3. **POST /api/reports/generate**: click *Try it out*, replace the body with the contents of [samples/shipment_request.json](samples/shipment_request.json), then *Execute*. The AI call usually takes 10–60 seconds.
4. Copy the whole **Response body**.
5. **POST /api/reports/export/pdf** (or `/docx`): click *Try it out*, paste the response body, then *Execute*. Swagger shows a **Download file** link.

Things to try:
- `{"report_type": "shipment", "data": {"getOrderList": []}}` returns 422 `invalid_report_data`.
- `"report_type": "finance"` returns 400 `unsupported_report_type`.

From PowerShell, without Swagger:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/reports/generate `
  -ContentType 'application/json' -InFile .\samples\shipment_request.json | ConvertTo-Json -Depth 10
```

Automated tests (DeepSeek is mocked, so no key or network is needed):

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## Connecting Angular

### 1. Environment

```ts
// src/environments/environment.ts
export const environment = {
  production: false,
  aiBoxApiUrl: 'http://localhost:8000',
};
```

The DeepSeek key stays in the backend `.env`. Angular never sees it. CORS already allows `http://localhost:4200`. To allow other origins, add them to `CORS_ALLOWED_ORIGINS`.

> Alternative: use an Angular dev-server proxy (`proxy.conf.json` with `"/api": {"target": "http://localhost:8000"}`) and set `aiBoxApiUrl: ''`. CORS is then not involved.

### 2. Service

```ts
// src/app/ai-box/services/ai-report.service.ts
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse, HttpResponse } from '@angular/common/http';
import { Observable, catchError, from, map, switchMap, throwError } from 'rxjs';
import { environment } from '../../../environments/environment';

export interface KeyMetric { label: string; value: string; note?: string | null; }
export interface ReportTable { title: string; columns: string[]; rows: string[][]; }
export interface ReportContent {
  title: string;
  executive_summary: string;
  sections: { heading: string; content: string }[];
  findings: { title: string; detail: string; severity: 'high' | 'medium' | 'low' | 'info' }[];
  recommendations: { action: string; rationale: string; priority: 'high' | 'medium' | 'low' }[];
  data_limitations: string[];
}
export interface ReportResponse {
  report_id: string;
  report_type: string;
  report_name: string;
  generated_at: string;
  ai_model: string;
  prompt: string;
  records_analyzed: number;
  key_metrics: KeyMetric[];
  tables: ReportTable[];
  metrics: Record<string, any>;
  content: ReportContent;
  disclaimer: string;
}
export interface ApiError {
  code: string;
  category: 'validation' | 'report_generation' | 'export' | 'server' | 'network';
  message: string;
  details?: unknown;
}

@Injectable({ providedIn: 'root' })
export class AiReportService {
  private http = inject(HttpClient);
  private baseUrl = `${environment.aiBoxApiUrl}/api/reports`;

  generate(reportType: string, prompt: string, data: unknown): Observable<ReportResponse> {
    return this.http
      .post<ReportResponse>(`${this.baseUrl}/generate`, { report_type: reportType, prompt, data })
      .pipe(catchError(err => this.toApiError(err)));
  }

  download(report: ReportResponse, format: 'docx' | 'pdf'): Observable<void> {
    return this.http
      .post(`${this.baseUrl}/export/${format}`, report, { responseType: 'blob', observe: 'response' })
      .pipe(
        map(res => this.saveFile(res, `${report.report_type}_report.${format}`)),
        catchError(err => this.toApiError(err)),
      );
  }

  private saveFile(res: HttpResponse<Blob>, fallbackName: string): void {
    const disposition = res.headers.get('Content-Disposition') ?? '';
    const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? fallbackName;
    const url = URL.createObjectURL(res.body!);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  private toApiError(err: HttpErrorResponse): Observable<never> {
    // With responseType 'blob', the JSON error body arrives as a Blob.
    if (err.error instanceof Blob) {
      return from(err.error.text()).pipe(
        switchMap(text => throwError(() => this.parse(err.status, this.safeJson(text)))),
      );
    }
    return throwError(() => this.parse(err.status, err.error));
  }

  private parse(status: number, body: any): ApiError {
    if (status === 0) {
      return { code: 'network_error', category: 'network', message: 'Cannot reach the AI Box backend.' };
    }
    return body?.error ?? { code: 'http_error', category: 'server', message: `Request failed (HTTP ${status}).` };
  }

  private safeJson(text: string): any {
    try { return JSON.parse(text); } catch { return null; }
  }
}
```

### 3. Component usage

```ts
// inside your ai_box component
report?: ReportResponse;
error?: ApiError;
loading = false;
prompt = 'Analyze shipment status and cancellation reasons';

generateReport(): void {
  this.loading = true;
  this.error = undefined;
  // shipmentApiResponse.data is the object that contains getOrderList
  this.aiReportService.generate('shipment', this.prompt, this.shipmentApiResponse.data).subscribe({
    next: report => { this.report = report; this.loading = false; },
    error: (e: ApiError) => { this.error = e; this.loading = false; },
  });
}

download(format: 'docx' | 'pdf'): void {
  if (!this.report) return;
  this.aiReportService.download(this.report, format).subscribe({ error: (e: ApiError) => (this.error = e) });
}
```

```html
<div *ngIf="error" class="alert alert-danger">{{ error.message }}</div>

<div *ngIf="report">
  <h2>{{ report.content.title }}</h2>
  <p>{{ report.content.executive_summary }}</p>

  <div *ngFor="let m of report.key_metrics"><strong>{{ m.label }}:</strong> {{ m.value }}</div>

  <section *ngFor="let s of report.content.sections">
    <h3>{{ s.heading }}</h3>
    <p style="white-space: pre-line">{{ s.content }}</p>
  </section>

  <table *ngFor="let t of report.tables">
    <caption>{{ t.title }}</caption>
    <tr><th *ngFor="let c of t.columns">{{ c }}</th></tr>
    <tr *ngFor="let r of t.rows"><td *ngFor="let cell of r">{{ cell }}</td></tr>
  </table>

  <ul><li *ngFor="let f of report.content.findings">[{{ f.severity }}] <b>{{ f.title }}</b> – {{ f.detail }}</li></ul>
  <ol><li *ngFor="let r of report.content.recommendations"><b>{{ r.action }}</b> – {{ r.rationale }}</li></ol>

  <button (click)="download('docx')">Download Word</button>
  <button (click)="download('pdf')">Download PDF</button>
</div>
```

Report generation takes 10–60 seconds, so show a loading indicator. Users can edit the report in the preview before downloading: change `report.content` and post the edited object to the export endpoint.

---

## Adding a report type

1. Create `app/prompts/<type>_prompt.py` containing:
   - a system prompt describing the analyst role and focus areas
   - an `analyze(data: dict) -> AnalysisResult` function that validates the input (raise `InvalidReportDataError`), extracts **only** the fields you need without personal data, calculates `metrics`, and builds `key_metrics`, `tables` and a sanitised `ai_context`
   - a `ReportDefinition(report_type="courier_performance", ...)`
2. Register it in `_DEFINITIONS` in [app/prompts/registry.py](app/prompts/registry.py).

That is all. `/generate`, the AI rules and output format, Pydantic validation, `/types` and both exporters work for the new type automatically. Use [app/prompts/shipment_prompt.py](app/prompts/shipment_prompt.py) as the template.

---

## Security notes

- The DeepSeek key is read only on the server, from `.env`, which is git-ignored. It is never returned by any endpoint.
- Request size, prompt length, record count and every request/response schema are validated.
- Shipment data and user prompts are treated as untrusted. They are wrapped in delimited blocks, the AI is told to ignore instructions inside them, and attempts to close the delimiter tags are neutralised.
- Personal data is excluded through a field allow-list, with regex redaction of phone numbers and emails in free text.
- DeepSeek calls use HTTP timeouts and bounded retries. Failures map to clear `report_generation` errors, kept separate from `validation` errors.
- Request data, prompts and AI output are never logged.
- Generated documents are streamed back with `Cache-Control: no-store` and are never exposed through a public URL.

### Before production

- **Add authenticated access.** For example, validate the same JWT or session your Angular app already uses, or put the API behind an authenticated gateway. Without it, anyone who can reach the server can use your DeepSeek balance.
- Set `ENABLE_DOCS=false`, `APP_ENV=production`, and a strict `CORS_ALLOWED_ORIGINS`.
- Serve over HTTPS behind a reverse proxy (IIS/nginx), and add rate limiting there.
- Run without `--reload`, e.g. `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`.


See [REPORT_API.md](REPORT_API.md) for the structured report contract, protected downloads, Angular integration and storage limitations.
