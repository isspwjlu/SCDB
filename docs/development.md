# Development

> **Scope.** This repository holds the extraction pipeline. The web
> interface and its HTTP API are not part of it.

How to work on the pipeline day-to-day: the setup, the test loop, the
commit conventions, and the traps that cost the most time.

## Setup

```bash
git clone <repo-url>
cd superconductor_database

cp .env.example .env       # the model endpoint, the cache directories

make up                    # postgres + the worker
make migrate               # the schema
make seed                  # optional, populates the SuperCon materials
```

The backend source is bind-mounted in dev mode, so a saved file is the
file that runs.

### The docling virtualenv

PDF parsing runs on [docling](https://github.com/docling-project/docling),
which lives in **its own virtualenv** at `backend/.venv-docling` -- not
in the application venv. The application venv's base interpreter is an
Anaconda Python, and the torch wheels docling needs refuse to load
there (`Error loading c10.dll`).

```bash
uv venv --python 3.12 backend/.venv-docling
uv pip install --python backend/.venv-docling/Scripts/python.exe "docling-slim[standard]>=2.142,<3"
```

`backend/tools/docling_worker.py` is the bridge: it reads
`{"pdf": ..., "out": ...}` requests as JSON lines on stdin and writes
one result line per request, so a batch of papers pays the
model-loading cost once. `ingest/docling_parser.py` drives it, caches
the markdown by the PDF's SHA-256, and strips the bibliography
(reference titles are a rich source of numbers the extractor must not
borrow).

Convert everything already in the PDF cache:

```bash
cd backend && .venv/Scripts/python.exe -m superconductor_db.ingest.docling_parser
```

The docling models are fetched from Hugging Face; `HF_ENDPOINT`
defaults to a mirror, override it if the network allows the real host.

## Test loop

The project follows red -> green -> refactor for every behaviour
change.

```bash
cd backend
python -m pytest tests/<file>::<test> -x
```

or, in the container:

```bash
docker compose exec worker pytest tests/<file>::<test> -x
```

1. Write the failing test in `backend/tests/`.
2. Run it and confirm it fails for the right reason -- an import error
   is not a failing test.
3. Write the minimum code in `backend/src/superconductor_db/...`.
4. Re-run until green, then refactor.
5. Add an integration test when a behaviour crosses two modules.

The coverage gate is 80% (see `pyproject.toml`); the CI fails below it.
A `# pragma: no cover` is allowed with a one-line justification on the
line you skip, and never as a blanket over a module.

## Commit conventions

Conventional Commits. Format:

```
<type>(<scope>): <short description>

<optional body wrapped at 72 cols>

<optional footer with Refs/Closes>
```

| Type       | Use for                                                          |
|------------|------------------------------------------------------------------|
| `feat`     | A user-visible new feature.                                      |
| `fix`      | A bug fix.                                                       |
| `refactor` | Code change that neither fixes a bug nor adds a feature.         |
| `docs`     | Documentation only.                                              |
| `test`     | Adding or fixing tests, no production code change.               |
| `chore`    | Tooling, deps, CI, configs.                                      |
| `perf`     | Performance improvement.                                         |
| `ci`       | CI workflow changes only.                                        |

Common scopes: `backend`, `ingest`, `gates`, `db`, `tools`, `docs`.

### Rules

- One logical change per commit. If your commit message has "and" in
  it, split it.
- Commit messages use the imperative mood ("add", not "added").
- Reference the plan / issue when relevant: `Refs: Phase J Task 36.`

## PR review process

1. **Open a PR** against `main`. CI runs the backend test suite with
   the coverage gate.
2. **Self-review**: re-read your diff as if you were the reviewer.
   Remove debug prints, fix typos, simplify before requesting review.
3. **One reviewer required** for `feat` and `fix` PRs; `chore` and
   `docs` can merge with just CI green.
4. **Reviewer checklist**:
   - [ ] Tests exist for the change.
   - [ ] No hardcoded secrets.
   - [ ] No mutation of ORM objects outside the session.
   - [ ] Errors handled explicitly (no bare `except Exception: pass`).
   - [ ] No `print()` left in production code.
   - [ ] Coverage didn't drop.
5. **Address feedback** in new commits (not force-pushes) until
   approval. Squash on merge.

## Branching

- Default branch: `main`. Always deployable; never commit broken code.
- Feature branches: `feat/<short-slug>` or `fix/<short-slug>`. Branch
  from the latest `main`.

## Troubleshooting

### `docker compose up` fails with the port already in use

Something on `:5432` is already listening. On Windows:

```bash
netstat -ano | findstr :5432          # find the PID
taskkill /F /PID <pid>                # kill it
```

Or change the host-side port via `.env` (`POSTGRES_PORT=55432`).

### The worker can't reach the database

`make logs` will show a connection error. Either postgres isn't
healthy yet (wait 10s and retry) or `POSTGRES_HOST` is wrong. Inside
the Compose network the host is `postgres`, not `localhost`.

### The model answers 429

The LLM calls are serial on purpose, and `LLM_MAX_CONCURRENCY`
defaults to 1. A parallel burst is what makes a rate-limited endpoint
answer 429, and a paper that failed is a paper that was not extracted.
If you raise the concurrency, lower `LLM_REASONING_EFFORT` first.

### The model answers with empty content

MiniMax-M3.1-Flash always reasons, and it thinks for 13-20 k tokens
before it writes the records of a long paper. `LLM_MAX_TOKENS` has to
cover the thinking as well as the answer, or the answer comes back
empty. `LLM_REASONING_EFFORT=low` caps the thinking and cuts the cost
by about a third.

### The extracted values are nonsense

Check the gate that refused them. The gates are the point of the
pipeline: a value that cannot be traced to a sentence is not stored,
whatever the model says about it. `ingest/extractor.py` holds them.

### The extraction reads numbers from the bibliography

`ingest/docling_parser.py` strips the references; check that the
markdown in `MARKDOWN_CACHE_DIR` was produced with that stripping, and
re-convert the paper if it was not.

### Coverage dropped below the gate

Two options:

1. Write the missing tests (preferred -- coverage gaps are usually
   real untested paths).
2. Add a `# pragma: no cover` comment with a one-line justification on
   the line you are skipping. Never blanket-skip modules.

## Code style

- **Python**: PEP 8, type hints on all public functions. Imports
  sorted by ruff.
- **Immutability**: prefer `update(original, field, value)`-style
  patterns over in-place mutation. Pydantic models are immutable by
  default; preserve that.
- **Error handling**: catch at boundaries, not deep in helper
  functions. Re-raise with context
  (`raise RuntimeError("failed to extract PDF") from exc`).
