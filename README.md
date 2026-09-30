# Superconductor Database

A research-grade superconductor database. Built standalone (no reuse from `magnetism_database`).
Backs a FastAPI service + React SPA, with Postgres, MinIO, and an ingestion worker, all running
under Docker Compose.

The seed dataset is the SuperCon catalog (NIMS). Papers are added incrementally via an
LLM-assisted ingestion pipeline.

## Quickstart

```bash
cp .env.example .env       # then edit secrets/ports as needed
make up                   # build + start full stack
make logs                 # tail logs
make test                 # run backend + frontend suites
```

Once the stack is up:

- Frontend:    <http://localhost:5173>
- Backend API: <http://localhost:8000>  (`GET /health`)
- MinIO:       <http://localhost:9001>  (console)
- Postgres:    `localhost:5432`

## Layout

```
backend/    FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2
frontend/   Vite + React 18 + TS strict + shadcn/ui + TanStack Query
seeds/      External datasets (SuperCon CSV lives here)
contracts/  Cross-service API contracts / schemas
docs/       Design + architecture notes
```

## Further reading

- Architecture: [docs/architecture.md](docs/architecture.md)
- Implementation plan: [docs/superpowers/plans/2026-09-30-superconductor-database-plan.md](docs/superpowers/plans/2026-09-30-superconductor-database-plan.md)