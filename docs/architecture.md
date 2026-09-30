# Architecture

## Overview

Standalone research-grade superconductor database. Monorepo with a FastAPI backend, a React SPA
frontend, a Postgres data store, a MinIO object cache, and an ingestion worker. Everything
runs under Docker Compose.

## Service map

| Service  | Port (dev) | Responsibility                                                    |
|----------|-------------|-------------------------------------------------------------------|
| frontend | 5173        | React SPA (Vite dev server). Talks to backend via `/api` proxy.   |
| backend  | 8000        | FastAPI HTTP API. Pydantic v2 + SQLAlchemy 2 + Alembic.           |
| worker   | (no port)   | Pulls jobs from a Postgres `SKIP LOCKED` queue, ingests papers.   |
| postgres | 5432        | Source of truth: users, materials, papers, submissions, queues.  |
| minio    | 9000 / 9001 | Object storage for PDFs, markdown, and LLM caches.                |

## Layout

```
backend/    FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2
frontend/   Vite + React 18 + TS strict + shadcn/ui + TanStack Query
seeds/      External datasets (SuperCon CSV lives here)
contracts/  Cross-service API contracts / schemas
docs/       Design + architecture notes
```

## M1 verification

**Status:** code-level only (Docker stack not booted in this phase).

Verification commands that should be run in CI / on a developer machine after `make up`:

```bash
# All services healthy
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps

# Backend health endpoint
curl -fsS http://localhost:8000/health
# expected: {"status":"ok","db":true,"minio":true}

# Frontend root
curl -fsS http://localhost:5173/
# expected: HTML containing <div id="root"></div>

# Backend test suite
docker compose exec backend pytest --cov=superconductor_db --cov-report=term-missing

# Frontend test suite
docker compose exec frontend npm run test:coverage
```

Code-level checks performed for M1 (this batched task, no Docker boot):

- `python -m py_compile` on every backend `.py` file → OK.
- `tsc --noEmit` not run (deps not installed; deferred to CI / `make frontend-test`).
- `package.json` / `tsconfig.json` validated as well-formed JSON.
- Four Conventional Commits produced: `chore: monorepo skeleton …`, `feat(backend): fastapi skeleton …`, `feat(frontend): vite + react + ts skeleton …`, and this docs commit.

Live environment after `make up` (out of scope for this run, recorded here for the operator):

- `docker compose ps` should report `postgres`, `minio`, `backend`, `frontend`, `worker` all `healthy`.
- `curl -fsS http://localhost:8000/health` → `{"status":"ok","db":true,"minio":true}`.
- `curl -fsS http://localhost:5173/` → HTML containing `<div id="root"></div>`.
- `make test` runs `backend-test` + `frontend-test` with coverage gates (≥80% backend, ≥70% frontend).

## Future phases

See [docs/superpowers/plans/2026-09-30-superconductor-database-plan.md](superpowers/plans/2026-09-30-superconductor-database-plan.md)
for the full roadmap: SuperCon seed ingestion (M2), auth + RBAC (M3), material API + search (M4),
paper ingestion + LLM extraction (M5), audit + review queue (M6), observability (M7), E2E
coverage gate (M8).