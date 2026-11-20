# Superconductor Database

A research-grade superconductor database. Built standalone (no reuse from `magnetism_database`).
Backs a FastAPI service + React SPA, with Postgres, MinIO, and an ingestion worker, all running
under Docker Compose.

The seed dataset is the SuperCon catalog (NIMS). Papers are added incrementally via an
LLM-assisted ingestion pipeline.

## Status

| Milestone | Scope                                                         | Status |
|-----------|---------------------------------------------------------------|--------|
| M1        | Monorepo skeleton + Docker stack                              | done   |
| M2        | SuperCon seed ingestion                                       | done   |
| M3        | Auth (JWT cookie) + RBAC (reader/contributor/reviewer/admin)  | done   |
| M4        | Material REST API + search                                    | done   |
| M5        | Paper ingestion + LLM extraction                              | done   |
| M6        | Audit log + reviewer queue                                    | done   |
| M7        | Observability + stats dashboard                               | done   |
| M8        | Admin UI (users / ingest / audit)                             | done   |
| M9        | Submission UI (submit / mine / review)                        | done   |
| M10       | Polish + Playwright E2E + contract test + docs                | done   |

## Quickstart

```bash
cp .env.example .env       # then edit secrets/ports as needed
make up                    # build + start the full stack
make test                  # run backend + frontend + contract suites
```

That's three commands. After the stack is up:

- Frontend:    <http://localhost:5173>
- Backend API: <http://localhost:8000>  (`GET /health` returns `{"status":"ok",...}`)
- MinIO:       <http://localhost:9001>  (console, `scdb` / `scdb_dev_pw`)
- Postgres:    `localhost:5432`

## Layout

```
backend/    FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2
frontend/   Vite + React 18 + TS strict + shadcn/ui + TanStack Query
seeds/      External datasets (SuperCon CSV lives here)
contracts/  Cross-service API contracts / schemas (JSON Schema)
docs/       Design + architecture notes
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — system diagram, process topology,
  data flow, 8-table ERD, services summary.
- [docs/api.md](docs/api.md) — every endpoint with example curl + response shape.
- [docs/deployment.md](docs/deployment.md) — local stack, env-var reference, scale
  considerations, production wiring.
- [docs/development.md](docs/development.md) — TDD workflow, commit conventions, PR
  review process, troubleshooting.
- [docs/superpowers/plans/2026-09-30-superconductor-database-plan.md](docs/superpowers/plans/2026-09-30-superconductor-database-plan.md)
  — full implementation plan and milestone history.

## Verification cheat-sheet

```bash
make backend-test       # pytest with 80% coverage gate
make frontend-test      # vitest with 70% coverage gate
make contract-test      # CI gate: committed JSON Schemas match Pydantic output
npm run e2e             # Playwright critical flows (run inside frontend/)
```

## License

Copyright 2026 Institute of Solid State Physics, Chinese Academy of Sciences.

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
