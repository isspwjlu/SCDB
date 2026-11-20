# Deployment

> **Scope.** This repository holds the extraction pipeline. The web
> interface and its HTTP API are not part of it.

This guide covers the local Docker Compose stack (the supported deployment target) and the
production wiring choices you'll need for a real environment.

## Local one-command stack

```bash
cp .env.example .env       # edit secrets / ports as needed
make up                   # docker compose build + up -d
make logs                 # tail all service logs
```

After `make up`, the following endpoints are live on `localhost`:

| Service     | URL                                                |
|-------------|----------------------------------------------------|
| Frontend    | <http://localhost:5173>                            |
| Backend API | <http://localhost:8000>  (GET `/health`)           |
| MinIO UI    | <http://localhost:9001>  (`scdb` / `scdb_dev_pw`)  |
| Postgres    | `localhost:5432` (`scdb` / `scdb_dev_pw`)          |

`docker-compose.yml` is the production-ish base; `docker-compose.dev.yml` overrides bind
mounts (for live source reload) and turns on the dev worker profile. For a clean rebuild
from scratch:

```bash
make down                 # docker compose down -v (drops volumes)
make up
make migrate              # alembic upgrade head
make seed                 # SuperCon CSV -> materials (optional, large)
```

## Environment variables

All variables are read from `.env` (which must NOT be committed) or the environment. The
committed `.env.example` is the canonical reference; everything below lists each var,
its default, and what it controls.

### Database

| Var                | Default            | Purpose                                              |
|--------------------|--------------------|------------------------------------------------------|
| `POSTGRES_USER`    | `scdb`             | Postgres role name.                                  |
| `POSTGRES_PASSWORD`| `scdb_dev_pw`      | Postgres role password. **Change in prod.**          |
| `POSTGRES_DB`      | `superconductor`   | Database name.                                       |
| `POSTGRES_HOST`    | `postgres`         | Hostname inside the Compose network.                 |
| `POSTGRES_PORT`    | `5432`             | Host port (left side of the `:HOST:CONTAINER` map).  |

### Object store (MinIO / S3)

| Var                  | Default       | Purpose                                             |
|----------------------|---------------|-----------------------------------------------------|
| `MINIO_ROOT_USER`    | `scdb`        | MinIO root user.                                    |
| `MINIO_ROOT_PASSWORD`| `scdb_dev_pw` | MinIO root password. **Change in prod.**            |
| `MINIO_BUCKET`       | `scdb-cache`  | Bucket created at startup if missing.               |

### Auth / JWT

| Var                       | Default                                                | Purpose                                                                |
|---------------------------|--------------------------------------------------------|------------------------------------------------------------------------|
| `JWT_SECRET`              | `change_me_to_random_64_chars_string_for_production_*` | HMAC signing key for access tokens. **MUST be ≥64 random chars in prod.** |
| `JWT_EXPIRY_HOURS`        | `24`                                                   | Token lifetime in hours.                                               |
| `ALLOW_OPEN_REGISTRATION` | `false`                                                | When `true`, `POST /api/v1/auth/register` is open to anyone. Keep `false` in prod and create users via the admin UI or CLI. |
| `COOKIE_SECURE`           | `false`                                                | When `true`, the `access_token` cookie gets the `Secure` attribute. Set `true` in any HTTPS deployment. |

### LLM

| Var               | Default                                | Purpose                                                                 |
|-------------------|----------------------------------------|-------------------------------------------------------------------------|
| `LLM_API_KEY`     | `ollama`                               | Bearer token for the LLM provider (placeholder for local Ollama).       |
| `LLM_BASE_URL`    | `http://host.docker.internal:11434/v1` | OpenAI-compatible base URL.                                             |
| `LLM_MODEL`       | `gemma4:e4b`                           | Model id used for PDF extraction.                                       |

### Worker

| Var                  | Default                       | Purpose                                              |
|----------------------|-------------------------------|------------------------------------------------------|
| `PDF_CACHE_DIR`      | `/data/cache/pdfs`            | Local cache for downloaded PDFs (worker host).       |
| `MARKDOWN_CACHE_DIR` | `/data/cache/markdown`        | Local cache for extracted markdown (worker host).    |
| `SCANSCI_PDF_BIN`    | `scansci-pdf` | Path to the `scansci-pdf` CLI binary. Host-specific. |

### Network ports

| Var             | Default | Purpose                                                  |
|-----------------|---------|----------------------------------------------------------|
| `BACKEND_PORT`  | `8000`  | Host port mapped to backend container port `8000`.       |
| `FRONTEND_PORT` | `5173`  | Host port mapped to frontend container port `5173`.      |

## Production wiring

The Compose file works locally; for production you'll want:

1. **TLS termination** at a reverse proxy (Caddy, nginx, or a cloud LB). Point DNS at it,
   forward `:443` → `frontend:5173` (or a built static bundle behind nginx) and
   `:443/api/*` → `backend:8000`. Set `COOKIE_SECURE=true`.
2. **Real Postgres**: use a managed service (RDS, Cloud SQL, Neon, Supabase) or a HA
   Postgres operator. Don't run single-node Postgres in prod.
3. **Real object storage**: swap MinIO for S3 / GCS / Azure Blob. The `minio_client`
   abstraction is S3-compatible; only the endpoint URL + credentials change.
4. **JWT_SECRET**: generate with `python -c 'import secrets; print(secrets.token_urlsafe(64))'`
   and store in your secret manager. Rotate every 90 days.
5. **ALLOW_OPEN_REGISTRATION=false**: provision the first admin user via the CLI seed
   script, then use the admin UI for everything else.
6. **Backups**: schedule `pg_dump` (or rely on managed Postgres PITR). Snapshot MinIO /
   S3 bucket nightly. The MinIO `mc mirror` command can sync to S3.
7. **Observability**: ship the `audit_logs` table to your SIEM. Add Prometheus
   instrumentation via `prometheus-fastapi-instrumentator` if you need fine-grained
   request metrics.

## Scale considerations

### Read path

- The materials list query hits a flat columns + JSONB table; for > 1M rows consider
  materialising `tc_value` as a real numeric column with an index (it's already indexed
  in the seed migration). For text search, the `pg_trgm` GIN index handles substring
  search up to ~10M rows before you need to switch to a dedicated search engine.
- TanStack Query caches on the client; the SPA can serve most repeat traffic without
  hitting the backend.
- Frontend static assets should live behind a CDN; the SPA is small (< 500 KB gzipped).

### Write path

- Submissions are write-heavy only during active ingest campaigns. For large submissions,
  increase MinIO upload concurrency (already async via `UploadFile`).
- The worker is single-process by default. To scale horizontally, run multiple worker
  containers — the `FOR UPDATE SKIP LOCKED` queue handles this safely.

### Storage

- Each PDF costs ~1-5 MB in MinIO. A 10k-paper catalogue is ~50 GB; trivial for any
  object store.
- Postgres `materials` row is ~2 KB after JSONB expansion. 1M materials ≈ 2 GB;
  comfortably within a single managed Postgres instance.

### When to upgrade

- **>100k materials** → add a read replica; rewrite the materials list query to use
  the replica.
- **>1M materials** → split read path into a dedicated search engine (Meilisearch or
  Typesense) and keep Postgres as the source of truth.
- **>10 concurrent ingest workers** → add Prometheus metrics + queue depth alerting;
  consider per-job retry budgets.
- **>100 RPS on the API** → front the backend with nginx, move JWT verification into a
  sidecar (e.g. oauth2-proxy) so the FastAPI process can focus on business logic.
