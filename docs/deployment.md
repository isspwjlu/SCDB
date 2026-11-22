# Deployment

> **Scope.** This repository holds the extraction pipeline. The web
> interface and its HTTP API are not part of it.

This guide covers the local Docker Compose stack (the supported
deployment target) and the choices you need to make for a real
environment.

## The local stack

```bash
cp .env.example .env       # the model endpoint, the cache directories
make up                     # docker compose build + up -d
make logs                   # tail the worker logs
```

Two services come up: `postgres` and `worker`. The worker is the
extraction consumer; it has no port, and it talks to postgres over the
Compose network.

`docker-compose.yml` is the base; `docker-compose.dev.yml` overrides
the bind mounts so the source is live. For a clean rebuild:

```bash
make down                 # docker compose down -v (drops the volumes)
make up
make migrate              # alembic upgrade head
make seed                 # SuperCon CSV -> materials (optional, large)
```

## Environment variables

All variables are read from `.env` (which must NOT be committed) or the
environment. The committed `.env.example` is the canonical reference.

### Database

| Var                | Default            | Purpose                                              |
|--------------------|--------------------|------------------------------------------------------|
| `POSTGRES_USER`    | `scdb`             | Postgres role name.                                  |
| `POSTGRES_PASSWORD`| `scdb_dev_pw`      | Postgres role password. **Change in prod.**          |
| `POSTGRES_DB`      | `superconductor`   | Database name.                                       |
| `POSTGRES_HOST`    | `postgres`         | Hostname inside the Compose network.                 |
| `POSTGRES_PORT`    | `5432`             | Host port (left side of the `:HOST:CONTAINER` map).  |

`DATABASE_URL` overrides all of them when it is set; a local run can
point at SQLite (`sqlite:///./_dev.db`) instead.

### The model

| Var                     | Default                                | Purpose                                                     |
|-------------------------|----------------------------------------|-------------------------------------------------------------|
| `LLM_API_KEY`           | `ollama`                               | Bearer token for the model provider.                        |
| `LLM_BASE_URL`          | `http://host.docker.internal:11434/v1` | OpenAI-compatible base URL.                                 |
| `LLM_MODEL`             | `gemma4:e4b`                           | Model id used for the extraction.                           |
| `LLM_TIMEOUT`           | `300`                                  | Seconds a single call may take.                             |
| `LLM_MAX_TOKENS`        | `32768`                                | Output budget per call, reasoning included.                 |
| `LLM_REASONING_EFFORT`  | `low`                                  | Caps the thinking of the models that always think.          |
| `LLM_MAX_CONCURRENCY`   | `1`                                    | How many calls may be in flight. One, on purpose.           |
| `OPENALEX_MAILTO`       | (empty)                                | Contact address; identifies the polite pool.                |
| `OPENALEX_API_KEY`      | (empty)                                | Raises the daily request allowance by two orders of magnitude.|
| `ELSEVIER_API_KEY`      | (empty)                                | With the institute's address, enough to get the full text.  |

The corpus this pipeline produced was extracted with
`MiniMax-M3.1-Flash`, which always reasons: `LLM_REASONING_EFFORT=low`
keeps the answer inside the token budget and cuts the cost by about a
third.

### Caches

| Var                  | Default                | Purpose                                            |
|----------------------|------------------------|----------------------------------------------------|
| `PDF_CACHE_DIR`      | `/data/cache/pdfs`     | The downloaded PDFs, keyed by the content hash.    |
| `MARKDOWN_CACHE_DIR` | `/data/cache/markdown` | The parsed markdown, keyed the same way.           |
| `SCANSCI_PDF_BIN`    | `scansci-pdf`          | Path to the `scansci-pdf` CLI binary. Host-specific. |
| `HTTP_PROXY`         | (empty)                | Proxy for the fetchers; httpx does not pick the system one up. |

Both caches are on the `cache` volume in Compose. They are the reason
a re-run over a large corpus is affordable: a paper is downloaded once
and parsed once, whatever happens to it afterwards.

### The SuperCon catalogue

| Var                | Default   | Purpose                                            |
|--------------------|-----------|----------------------------------------------------|
| `SUPERCON_CSV_DIR` | (bundled) | Where the NIMS CSV files are.                      |
| `SUPERCON_DB_PATH` | `backend/_dev_supercon.db` | The separate database the catalogue is loaded into. |

The catalogue is distributed by NIMS for research use and is not
committed here; `backend/scripts/download_supercon.py` fetches it.

## Production wiring

The Compose file works locally; for production:

1. **Real Postgres**: use a managed service (RDS, Cloud SQL, Neon) or
   a HA Postgres operator. Do not run single-node Postgres in prod.
2. **The cache on real disks**: `PDF_CACHE_DIR` and
   `MARKDOWN_CACHE_DIR` are the two directories that grow without
   bound. Put them on a volume that is backed up separately from the
   database -- the PDFs can be re-downloaded, the database cannot.
3. **Secrets**: the model key, the OpenAlex key and the Elsevier key
   belong in your secret manager, not in `.env` on a disk.
4. **Backups**: schedule `pg_dump`, or rely on managed Postgres PITR.
5. **One worker at a time is usually enough.** The queue is safe for
   more (`FOR UPDATE SKIP LOCKED`), but the model is the bottleneck,
   and a concurrent burst is what makes a rate-limited endpoint answer
   429.

## Scale considerations

### Write path

- The worker is single-process by default. To scale horizontally, run
  more worker containers -- the `ingest_jobs` queue handles this
  safely, as long as the model endpoint tolerates the concurrency.
- Each PDF costs 1-5 MB. A 10k-paper catalogue is ~50 GB.
- A materials row is ~2 KB after the JSON columns expand. 1M materials
  is ~2 GB.

### When to tune

- **A model endpoint that answers 429** -> lower `LLM_MAX_CONCURRENCY`
  (it is already 1), and lower `LLM_REASONING_EFFORT`.
- **A corpus that grows faster than the queue drains** -> run more
  workers, and watch the depth of `ingest_jobs`.
- **A cache that fills the disk** -> prune `PDF_CACHE_DIR`; the
  markdown is what the extraction reads, and it is a tenth of the
  size.
