# Docker deployment

## Repository findings

Python 3.12 (local venv: 3.12.7), FastAPI, Uvicorn entry point
`app.main:app`, port 8000. Dependencies are in `requirements.txt`.
`app/storage/database.py` uses psycopg 3 directly, with a new connection per
operation and a 10-second connect timeout; there is no SQLAlchemy pool.
Startup creates `saved_reports` and its indexes with `IF NOT EXISTS`.
There is no Alembic configuration or versioned migration system. Future schema
changes need reviewed SQL migrations and backups before deployment.
APIs cover report generation/export/storage, analytics, charts and prompt skills.
DeepSeek is accessed through the OpenAI-compatible SDK. No upload directory was
found. Report binaries are stored in PostgreSQL and also under `output/reports`;
optional generated files use `OUTPUT_DIR`. Both are covered by `/app/output`.
Legacy SQLite files are not the active database and are excluded from the image.

The existing workspace has no Git repository metadata. No Git push, server
connection, production database modification or server configuration is performed
by this setup.

```text
AI_Box_backend/
  app/                       # existing application, unchanged
  scripts/                   # existing utilities, including SQLite migration
  docker/
    start.py                 # escaped psycopg URL, then exec application
    healthcheck.py            # HTTP + database probe
  Dockerfile
  docker-compose.yml         # authoritative deployment
  compose.yaml               # compatibility include for default discovery
  compose.local.yaml         # optional host development DB port
  .dockerignore
  .env.example
  .gitignore
  DEPLOYMENT.md
  requirements.txt
```

## Prerequisites and configuration

Use Docker Engine with the Compose v2 plugin **2.20+** (includes support for
`include`), Git, and sufficient disk space. On Windows use Docker Desktop in
Linux container mode. On Ubuntu follow the official Engine installation guide:
https://docs.docker.com/engine/install/ubuntu/ . Install the `docker-compose-plugin`
package from that repository and enable Docker at boot:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker aiuser
# Log out and back in; Docker group membership grants root-equivalent access.
docker version
docker compose version
```

Copy `.env.example` to `.env` only if `.env` does not already exist. Preserve local
credentials for existing volumes. Set `POSTGRES_DB`, `POSTGRES_USER` and a strong
`POSTGRES_PASSWORD`; for example generate a password with `openssl rand -hex 32`.
Use single quotes around values containing `$` or `#` in `.env`.
Set `Ai_box_key` to the DeepSeek key, `APP_ENV=production`, `ENABLE_DOCS=false`,
`CORS_ALLOWED_ORIGINS=https://YOUR_FRONTEND_DOMAIN`, and select a free
`BACKEND_PORT` (default 8000). Optional model, timeout, token/cost limits,
report limits, PDF fonts and export settings are listed in `.env.example`.
`AI_BOX_KEY` and `DEEPSEEK_API_KEY` are accepted key aliases; use one key name.
Custom font paths must exist inside the image or be mounted read-only.

Compose ignores the host `DATABASE_URL` and builds a URL with percent-encoded
credentials targeting `postgres:5432`. Host Python continues to read its own
`DATABASE_URL` unchanged. Container exports always use `/app/output`.
Changing database credentials in `.env` does not change users/passwords in an
already initialized volume: coordinate an explicit SQL password change instead.
Never print resolved Compose configuration into shared logs; it contains secrets.

## Local Docker testing

```powershell
# Preserve an existing .env; otherwise:
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
curl.exe --fail http://127.0.0.1:8000/health
docker compose logs --tail=100 backend
```

This default stack uses the existing `ai-box_postgres_data` volume. Back up any
existing database before first use. PostgreSQL has no published port. To continue
running Uvicorn on Windows against Docker PostgreSQL, explicitly opt in:

```powershell
docker compose -f docker-compose.yml -f compose.local.yaml up -d --wait postgres
# Host DATABASE_URL must use 127.0.0.1:5434 and matching, URL-encoded credentials.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Do not run the host and container backends on the same host port. The two Compose
filenames produce a harmless discovery warning; `compose.yaml` includes the
authoritative file. Use `-f docker-compose.yml` to suppress it.

## GitHub push (run locally)

Replace `YOUR_ORG/YOUR_REPOSITORY` with the actual GitHub repository. Review staged
files before committing; this workspace also contains local PDFs/images which
should only be committed intentionally.

```powershell
git init -b main
git add app scripts tests samples requirements.txt requirements-dev.txt pytest.ini README.md REPORT_API.md CHARTS.md Dockerfile docker docker-compose.yml compose.yaml compose.local.yaml .dockerignore .env.example .gitignore DEPLOYMENT.md
git diff --cached --stat
git check-ignore .env
git commit -m "Add Docker deployment for backend and PostgreSQL"
git remote add origin https://github.com/YOUR_ORG/YOUR_REPOSITORY.git
git push -u origin main
```

For an already initialized checkout, skip `git init` and remote creation. Never
commit `.env`, backups or local database files.

## Ubuntu deployment

After prerequisites are installed and GitHub access is configured:

```bash
ssh aiuser@206.1.25.27
git clone https://github.com/YOUR_ORG/YOUR_REPOSITORY.git /home/aiuser/AI_Box
cd /home/aiuser/AI_Box
umask 077
test -f .env || cp .env.example .env
nano .env
chmod 600 .env
ss -ltn | grep ':8000 ' || true
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/health
docker compose exec -T backend python docker/healthcheck.py
```

For an existing checkout use `cd /home/aiuser/AI_Box && git pull --ff-only` instead
of cloning. Keep `BACKEND_PORT` synchronized with Nginx. `/health` reports
`ai_configured`; a placeholder key allows startup but generation returns 503.
The Docker health probe additionally runs `SELECT 1`; no billed AI call is made.

## Persistence and initialization

PostgreSQL 17 stores its cluster in named volume `ai-box_postgres_data` mounted at
`/var/lib/postgresql/data`, preserving the previous Compose volume identity.
Report files live in `ai-box_report_output`. Rebuilds, restarts, ordinary Compose
shutdowns and reboots retain both volumes. Docker's `unless-stopped` restart policy
restarts services after reboot when the Docker daemon is enabled, unless they
were explicitly stopped. Health status alone does not restart a running container.
Never remove these volumes or run volume-pruning commands against them.

On an empty PostgreSQL volume the image creates the configured user/database;
the backend then creates missing tables/indexes. Existing tables are preserved.
Do not change PostgreSQL major version by editing the tag against an old volume;
use a tested logical migration or pg_upgrade. Keep off-server backups: named
volumes do not protect against disk failure.

## Backup, migration and restore

These commands are for Ubuntu/bash. Use the PostgreSQL 17 container client for
databases running 17 or earlier; newer source servers require an equal/newer
pg_dump and a separately planned compatible target version.

Create a custom-format database backup inside the container, then copy it out
(avoids binary corruption from Windows PowerShell output redirection):

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/ai-box.dump'
docker compose cp postgres:/tmp/ai-box.dump ./backups/ai-box.dump
chmod 600 backups/ai-box.dump
docker compose exec -T postgres pg_restore --list /tmp/ai-box.dump
docker compose exec -T backend tar -czf /tmp/report-output.tar.gz -C /app/output .
docker compose cp backend:/tmp/report-output.tar.gz ./backups/report-output.tar.gz
```

Use dated backup names in routine operations so earlier snapshots are preserved.
Copy both archives off the server and schedule/test backups. To dump a separate
local PostgreSQL server, use its compatible client and an interactive password:

```powershell
pg_dump -h 127.0.0.1 -p 5434 -U YOUR_LOCAL_USER -d YOUR_LOCAL_DATABASE -W -Fc -f local-postgres.dump
scp local-postgres.dump aiuser@206.1.25.27:/home/aiuser/AI_Box/backups/local-postgres.dump
```

On the destination start only PostgreSQL and restore into a **new database name**;
do not overwrite the initialized application database. Select an unused name:

```bash
cd /home/aiuser/AI_Box
docker compose up -d --wait postgres
docker compose cp ./backups/local-postgres.dump postgres:/tmp/import.dump
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" ai_box_import_20261015'
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d ai_box_import_20261015 --no-owner --no-privileges --exit-on-error --single-transaction /tmp/import.dump'
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d ai_box_import_20261015 -c "SELECT count(*) FROM saved_reports;"'
```

Verify source/destination counts and sample PDF/DOCX downloads before switching.
Stop the backend, set `POSTGRES_DB=ai_box_import_20261015` in `.env`, then run
`docker compose up -d --force-recreate` and verify health. Keep the previous
database intact for recovery. The same procedure restores backups into another
unused database. No `--clean`, database deletion or destructive restore is needed.
The image creates `POSTGRES_USER` as the bootstrap superuser; a separately managed
least-privilege role is appropriate where your security policy requires it.

Files with Windows absolute paths stored in old rows will not resolve on Linux.
Rows with database blobs still download normally. For file-only legacy rows,
copy files and explicitly migrate paths after a backup; verify these downloads
before switching traffic. Copying the output directory alone does not rewrite paths.

Legacy `data/ai_box.db` migration is supported by the existing utility:
`python -m scripts.migrate_sqlite_to_postgres`. It reads SQLite without modifying
it and skips IDs already in PostgreSQL. Mount a preserved legacy directory
read-only to `/app/data` when running it in Docker:

```bash
docker compose run --rm -v "$PWD/data:/app/data:ro" backend python -m scripts.migrate_sqlite_to_postgres
```

## Nginx reverse proxy

The application currently permits any HTTP(S) CORS origin via a regex, and exposes
APIs without a complete authentication layer. An origin list is not an access
control boundary. Configure authenticated access and HTTPS before public use.
The following internal-access example uses Nginx Basic Auth; substitute your domain
or the server IP and chosen port. It requires no application response changes.

```bash
sudo apt-get install nginx apache2-utils
sudo htpasswd -c /etc/nginx/ai-box.htpasswd aioperator
sudo nano /etc/nginx/sites-available/ai-box
```

```nginx
server {
    listen 80;
    server_name 206.1.25.27;
    client_max_body_size 10m;
    auth_basic "AI Box";
    auth_basic_user_file /etc/nginx/ai-box.htpasswd;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/ai-box /etc/nginx/sites-enabled/ai-box
sudo nginx -t
sudo systemctl reload nginx
```

Replace Basic Auth with your existing application gateway authentication for an
Angular integration, including correct preflight handling. Report bearer tokens
also use the Authorization header, so the Basic Auth example is intended for
internal testing, not a transparent replacement for that gateway. Obtain a trusted
TLS certificate for your real domain before sending credentials over the network.
Do not open the backend or PostgreSQL ports to the public internet. No Nginx or
firewall changes have been applied by this task.

## Updates, rollback and troubleshooting

Back up first. Record the currently deployed commit and preserve its image:

```bash
cd /home/aiuser/AI_Box
git rev-parse HEAD
docker image tag ai-box-backend:local ai-box-backend:rollback
git pull --ff-only
docker compose config --quiet
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/health
docker compose logs --tail=100 backend postgres
```

For an application-only rollback using the saved image:

```bash
IMAGE_TAG=rollback docker compose up -d --no-build --no-deps backend
```

Check out the recorded commit before the next build to restore source/config as
well. Schema changes may prevent an old image from working; restore the backup
into a new database and deliberately switch `POSTGRES_DB` as described above.
Preserve failed databases for investigation and account for writes made since backup.

```bash
docker compose ps
docker compose logs --tail=200 backend postgres
docker compose exec -T backend id
docker compose exec -T backend python docker/healthcheck.py
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose restart backend
df -h
```

Connection failures: check actual initialized credentials and PostgreSQL health;
the container hostname is `postgres`, not localhost. Port conflicts: change
`BACKEND_PORT` and Nginx upstream. Missing AI: check exact case of `Ai_box_key`.
Missing fonts: use the installed DejaVu fonts or mount valid TTFs. Export permission
errors on preexisting output volumes: confirm files are writable by UID/GID 10001
before a reviewed ownership correction. New volumes inherit image ownership.
Avoid logging credentials or full resolved configuration.

Docker startup ordering reference:
https://docs.docker.com/compose/how-tos/startup-order/ . PostgreSQL initialization
and persistence reference: https://hub.docker.com/_/postgres .

## Validation performed (2026-10-15)

- Default Compose, explicit deployment file, and local-development override passed
  `docker compose config --quiet`.
- Docker built the Python 3.12 Linux image successfully; installation and
  `pip check` passed. Existing dependency ranges were preserved; use a reviewed
  lockfile if reproducible dependency versions across future builds are required.
- An isolated Compose project used separate volumes and loopback port 18080.
  Both services became healthy; HTTP `/health` and the database-aware probe passed.
  PostgreSQL had no published host port and the backend ran as UID/GID 10001.
- Container database credentials containing `@`, `:` and `/` connected successfully.
- All 305 existing tests passed inside the Linux image with tests and samples
  mounted read-only. The first test invocation omitted the sample mount and failed
  collection; the corrected complete run passed.
- A probe database row and output file survived forced recreation of both services.
- Test containers/network were removed with ordinary Compose shutdown; test volumes
  `ai-box-deployment-check-postgres` and `ai-box-deployment-check-output` remain.
  The existing application database volume was not used during validation.
- No real DeepSeek request, reboot test, backup restore drill, SSH deployment,
  Nginx configuration or GitHub push was performed. API/business logic source files
  were not changed. The real production environment and access gateway still need
  configuration and verification.
