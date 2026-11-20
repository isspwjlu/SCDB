# Architecture

> **Scope.** This repository holds the extraction pipeline. The web
> interface and its HTTP API are not part of it.

## Overview

Standalone research-grade superconductor database. Monorepo with a FastAPI backend, a React SPA
frontend, a Postgres data store, a MinIO object cache, and an ingestion worker. Everything
runs under Docker Compose.

The system ingests the NIMS SuperCon catalogue as a seed corpus, then accepts incremental
PDF submissions from contributors. A reviewer queue turns submissions into materials records
via an LLM-assisted extraction pipeline. All state changes are written to an append-only
`audit_logs` table.

## Service map

| Service  | Port (dev) | Responsibility                                                    |
|----------|-------------|-------------------------------------------------------------------|
| frontend | 5173        | React SPA (Vite dev server). Talks to backend via `/api` proxy.   |
| backend  | 8000        | FastAPI HTTP API. Pydantic v2 + SQLAlchemy 2 + Alembic.           |
| worker   | (no port)   | Pulls jobs from a Postgres `SKIP LOCKED` queue, ingests papers.   |
| postgres | 5432        | Source of truth: users, materials, papers, submissions, queues.  |
| minio    | 9000 / 9001 | Object storage for PDFs, markdown, and LLM caches.                |

## System diagram

```
                                 +-----------------+
                                 |   Web Browser   |
                                 +--------+--------+
                                          |
                                          v   (HTTPS in prod)
                            +-------------+-------------+
                            |        frontend :5173     |
                            |   Vite + React 18 + TS    |
                            +-------------+-------------+
                                          |
                                          |   /api/* proxied
                                          v
   +-------------------+         +-------+--------+        +------------------+
   |  ingest worker    |  polls  |   backend :8000|  SQL   |  postgres :5432  |
   |  (no port; SKIP   |<------->|  FastAPI +      |<------>|  source of truth |
   |   LOCKED queue)   |         |  SQLAlchemy 2   |        |  8 tables        |
   +---------+---------+         +-------+--------+        +------------------+
             |                           |
             |  PDFs / markdown          |  object put/get
             v                           v
         +---+---------------------------+---+
         |          minio :9000 / :9001      |
         |  submissions/<id>/<file>.pdf     |
         |  papers/<doi>.pdf                |
         |  markdown/<doi>.md               |
         +-----------------------------------+
```

## Process topology

The stack runs five containerised processes in one Compose project:

1. **postgres** — primary store. Holds users, materials, papers, submissions, the
   ingest queue (`ingest_jobs`), and the audit trail.
2. **minio** — S3-compatible object store for binary artefacts. The API and the
   worker both read/write here.
3. **backend** — FastAPI app. Stateless; reads secrets from env and connects to
   postgres + minio at startup. Also serves the audit SSE stream (`/api/v1/admin/ingest/runs/{id}/logs`).
4. **worker** — long-running consumer. Polls `ingest_jobs` with `FOR UPDATE SKIP LOCKED`,
   downloads PDFs via `scansci-pdf`, runs LLM extraction, writes a `Paper` + `Material` rows,
   then marks the job complete. Crashing mid-job is safe: the row stays locked until the
   process dies, and the next worker re-picks it.
5. **frontend** — Vite dev server (dev) or static bundle behind a reverse proxy (prod).
   All HTTP traffic stays on the same origin as the backend via the `/api` proxy in dev
   or a reverse proxy in prod.

There are no side-car message brokers. The queue is a Postgres table — small enough
not to need Kafka/RabbitMQ, large enough to support crash recovery via `SKIP LOCKED`.

## Data flow

```
                    +---------------------- ingest pipeline (worker) -----------------------+
                    |                                                                     |
   SuperCon CSV --> | seed script  --> materials rows                                     |
                    |                                                                     |
   Search query  --> | scansci-pdf  --> PDF in MinIO --> LLM extractor --> Paper + Material |
                    |                                                                     |
                    +---------------------------------------------------------------------+

                    +---------------------- submission pipeline (humans) ------------------+
                    |                                                                     |
   Contributor -->  | upload PDF + meta --> Submission(status=pending) --> MinIO          |
   Reviewer     -->  | approve / reject --> if approve: extract JSON --> Material rows    |
                    |                       status=approved/rejected, reviewer_id, notes   |
                    +---------------------------------------------------------------------+

                    +--------------------------- read path (SPA) ----------------------------+
                    |                                                                     |
   Browser   -->  | React Query --> /api/v1/materials --> SQLAlchemy --> postgres          |
              -->  |              --> /api/v1/stats/overview --> aggregated SQL             |
              -->  |              --> /api/v1/submissions/mine (auth) --> filtered SQL     |
                    +---------------------------------------------------------------------+
```

Every state-changing write goes through one FastAPI endpoint, which:

1. Validates the request with a Pydantic schema.
2. Acquires a session, performs the write inside a transaction.
3. Emits an `audit_logs` row with `actor_id`, `action`, `target_id`, `timestamp`.
4. Returns a Pydantic-typed response.

There is no direct database access from the frontend; all reads go through the API and
all writes are audited.

## Data model (8 tables)

The full schema lives in `backend/src/superconductor_db/db/models/` and the migration history
in `backend/alembic/versions/`. The eight tables:

| #  | Table         | Purpose                                                                 |
|----|---------------|-------------------------------------------------------------------------|
| 1  | `users`       | Account records. `role` ∈ {reader, contributor, reviewer, admin}. Hashed passwords (bcrypt). |
| 2  | `materials`   | One row per superconducting material record. Carries Tc, H_c2, J_c, λ, ξ, Δ, plus structural fields. Flat columns — quantities are JSON blobs `{value, unit}`. |
| 3  | `papers`      | Source paper metadata (DOI, title, authors, year). Linked to materials by DOI. |
| 4  | `submissions` | Contributor uploads pending review. `status` ∈ {pending, approved, rejected}; stores `pdf_path` (MinIO key), optional `extracted_json`, `reviewer_id`, `review_notes`. |
| 5  | `audit_logs`  | Append-only trail of every state change. `actor_id`, `action`, `target_id`, `metadata` (JSONB), `created_at`. Indexed by `(actor_id, created_at)` and `(target_id)`. |
| 6  | `ingest_runs` | One row per `POST /admin/ingest/trigger` call. Tracks counters (queued / completed / failed). |
| 7  | `ingest_jobs` | The work queue. One row per SC search query. Status: `queued`, `running`, `done`, `failed`. Workers claim with `FOR UPDATE SKIP LOCKED`. |
| 8  | `alembic_version` | Standard Alembic migration pointer. |

### ERD summary

```
                 +-----------+
                 |   users   |
                 +-----+-----+
                       |
                       | actor_id / reviewer_id
                       v
+-----------+   +------+------+   +-------------+
| papers    |<--+ submissions +-->| audit_logs  |
+-----+-----+   +-------------+   +-------------+
      |               |
      | DOI           | extracted_json
      v               v
+-----------+   +-----------+
| materials |   |  minio    |
+-----------+   | (objects) |
                +-----------+

+-----------+         +-------------+
|ingest_runs+---1:N-->| ingest_jobs |
+-----------+         +------+------+
                              |  SKIP LOCKED
                              v
                         +----+----+
                         | worker  |
                         +---------+
```

Relations:

- `submissions.user_id` → `users.id` (contributor)
- `submissions.reviewer_id` → `users.id` (reviewer; nullable until reviewed)
- `materials.paper_doi` → `papers.doi` (optional; some materials are seed-only)
- `audit_logs.actor_id` → `users.id` (nullable for system events)
- `ingest_jobs.run_id` → `ingest_runs.id`

## Backend module layout

```
backend/src/superconductor_db/
├── api/                FastAPI routers (one per resource)
│   ├── auth.py         /api/v1/auth/{register,login,logout,me}
│   ├── materials.py    /api/v1/materials, /materials/{id}
│   ├── stats.py        /api/v1/stats/{overview,charts/*}
│   ├── submissions.py  /api/v1/submissions/* (auth required)
│   ├── admin_users.py  /api/v1/admin/users (admin only)
│   ├── admin_ingest.py /api/v1/admin/ingest/{trigger,runs/*} (admin only)
│   └── admin_audit.py  /api/v1/admin/audit (admin only)
├── core/               Settings, db session, deps, security, minio, audit
├── db/                 SQLAlchemy models + repository
├── schemas/            Pydantic request/response models (== JSON contracts)
├── ingest/             SuperCon seed + paper ingestion pipeline
└── main.py             App factory; mounts routers under /api/v1/*
```

## Frontend module layout

```
frontend/src/
├── api/                Typed fetchers per resource (returns typed shapes)
├── components/         Shared UI (auth, layout, materials, charts, ui primitives)
├── hooks/              React Query wrappers (useMaterials, useAuth, ...)
├── pages/              One file per route (Login, Register, MaterialsList,
│                        MaterialDetail, Submit, MySubmissions, ReviewQueue,
│                        Dashboard, AdminUsers, AdminIngest, AdminAudit)
├── lib/                Pure utilities (cn, formatting)
└── App.tsx + main.tsx  Router + query client providers
```

## Services summary

### `frontend` (React SPA)
- Vite dev server (port 5173) with `/api` proxy to `http://localhost:8000`.
- React Router 6, TanStack Query 5, React Hook Form + Zod, Recharts for charts.
- TypeScript strict mode. Vitest + Testing Library for unit tests.
- Playwright for E2E (Phase J).

### `backend` (FastAPI)
- FastAPI 0.110+, Pydantic v2, SQLAlchemy 2 (mapped_column style), Alembic.
- JWT auth via HTTP-only cookie (`access_token`); CSRF-safe because SameSite=Lax + non-GET state changes.
- Service-layer: routers → repository → DB session. No ORM leakage into responses.
- SSE endpoint for live ingest-run logs (`/api/v1/admin/ingest/runs/{id}/logs`).

### `worker` (ingest consumer)
- Long-running Python process. Boots with the same DB session factory as the backend.
- Polls `ingest_jobs` every 5s with `FOR UPDATE SKIP LOCKED LIMIT 5`.
- Calls `scansci-pdf` (CLI) to download PDFs; writes PDFs + extracted markdown to MinIO.
- Calls the LLM (`LLM_BASE_URL`) to extract Tc / structure / references; persists Paper + Material rows.

### `postgres` (data store)
- Postgres 16. Default `pg_trgm` extension for substring search.
- All migrations live under `backend/alembic/versions/`. Run with `make migrate`.

### `minio` (object store)
- MinIO latest, console on 9001.
- Bucket `scdb-cache` (configurable). Object layout:
  - `submissions/<submission_id>/<filename>.pdf`
  - `papers/<doi>.pdf`
  - `markdown/<doi>.md`
  - `llm-cache/<hash>.json`

## Verification

After `make up`:

```bash
docker compose ps                                 # all services healthy
curl -fsS http://localhost:8000/health            # {"status":"ok","db":true,"minio":true}
curl -fsS http://localhost:5173/                  # HTML containing <div id="root"></div>
make backend-test                                 # backend pytest with coverage gate
make frontend-test                                # vitest with coverage gate
make contract-test                                # CI gate: committed JSON Schemas match Pydantic output
```

See [deployment.md](deployment.md) for production wiring, and
[development.md](development.md) for the TDD workflow.

## Roadmap

See [docs/superpowers/plans/2026-09-30-superconductor-database-plan.md](superpowers/plans/2026-09-30-superconductor-database-plan.md)
for the full milestone history.
