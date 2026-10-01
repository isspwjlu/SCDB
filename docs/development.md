# Development

How to work on the superconductor database day-to-day: TDD workflow, commit conventions,
PR review process, and troubleshooting the common gotchas.

## Setup

```bash
# Clone
git clone <repo-url>
cd superconductor_database

# Configure
cp .env.example .env       # edit secrets / ports

# Boot the stack
make up
make migrate               # one-time
make seed                  # optional, populates SuperCon materials

# Verify
curl -fsS http://localhost:8000/health   # {"status":"ok","db":true,"minio":true}
```

For convenience, the backend source is bind-mounted in dev mode; FastAPI auto-reloads on
file save. The frontend Vite server HMRs on save. Save any file, see the change in
your browser within a second.

## TDD workflow

The project follows red → green → refactor for every behavior change.

### Backend (Python + pytest)

1. Write a failing test in `backend/tests/`. Use `pytest` + `httpx.AsyncClient` against
   the FastAPI app + a transactional test DB (see `backend/tests/conftest.py`).
2. Run it:

   ```bash
   docker compose exec backend pytest tests/<file>::<test> -x
   ```

   Confirm it fails for the right reason (not an import error).
3. Write the minimum code in `backend/src/superconductor_db/...` to make the test pass.
4. Re-run; iterate.
5. Refactor. Coverage gate is 80% (see `pyproject.toml`); if you drop below, the CI fails.
6. Add an integration test when a behavior crosses two modules.

### Frontend (TypeScript + Vitest)

1. Write a failing component test in `frontend/tests/unit/`. Use `@testing-library/react`
   + jsdom + the setup file `frontend/tests/setup.ts`.
2. Run it:

   ```bash
   docker compose exec frontend npm test -- <file>
   ```

3. Implement in `frontend/src/...`. Re-run until green.
4. Refactor; the coverage gate is 70%.
5. Add an E2E test in `frontend/tests/e2e/` when the flow crosses multiple routes
   (e.g. register → login → see nav).

### Contract tests

`contracts/diff.py` is the gate. Whenever you change a Pydantic schema, regenerate:

```bash
make contract-gen           # regenerates contracts/<Name>.schema.json
git add contracts/
git commit -m "chore(contracts): regenerate after <schema> change"
```

CI will block PRs that change a model without committing the regenerated schema.

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

Common scopes: `backend`, `frontend`, `worker`, `ingest`, `api`, `auth`,
`submissions`, `admin`, `e2e`, `contracts`, `docs`.

Examples:

```
feat(api): paginated materials list with Tc range filter
fix(auth): open-redirect on login redirect-from validation
refactor(submissions): extract PDF upload to minio_client.upload_submission_pdf
test(backend): cover submission approve creating materials rows
chore(contracts): regenerate after UserRole extraction
ci: contract test workflow
docs: architecture, api, deployment, development guides
```

### Rules

- One logical change per commit. If your commit message has "and" in it, split it.
- Commit messages use the imperative mood ("add", not "added").
- Reference the plan / issue when relevant: `Refs: Phase J Task 36.`

## PR review process

1. **Open a PR** against `main`. CI runs:
   - `make contract-test` — committed schemas match Pydantic models.
   - `make backend-test` — pytest with 80% coverage gate.
   - `make frontend-test` — vitest with 70% coverage gate.
   - On `main` pushes, Playwright E2E also runs (when secrets are configured).
2. **Self-review**: re-read your diff as if you were the reviewer. Remove debug prints,
   fix typos, simplify before requesting review.
3. **One reviewer required** for `feat` and `fix` PRs; `chore` and `docs` can merge with
   just CI green.
4. **Reviewer checklist** (see `~/.claude/rules/common/code-review.md` for the full one):
   - [ ] Tests exist for the change.
   - [ ] No hardcoded secrets.
   - [ ] No mutation of ORM objects outside the session.
   - [ ] Errors handled explicitly (no bare `except Exception: pass`).
   - [ ] No `console.log` / `print()` left in production code.
   - [ ] Coverage didn't drop.
5. **Address feedback** in new commits (not force-pushes) until approval — this keeps
   review history intact. Squash on merge.
6. **Squash-merge** with the PR title as the commit message (Conventional Commits
   format).

## Branching

- Default branch: `main`. Always deployable; never commit broken code.
- Feature branches: `feat/<short-slug>` or `fix/<short-slug>`. Branch from latest `main`.
- Hotfix branches: `hotfix/<short-slug>` from `main`. Merge via PR + cherry-pick if a
  release branch exists.

## Troubleshooting

### `docker compose up` fails with port already in use

Something on `:5173`, `:8000`, `:5432`, `:9000`, or `:9001` is already listening. On
Windows:

```bash
netstat -ano | findstr :5432          # find the PID
taskkill /F /PID <pid>                # kill it
```

Or change the host-side port via `.env` (`POSTGRES_PORT=55432`, etc.).

### Backend can't reach the database

`make logs` will show `psycopg2.OperationalError: ... connection refused`. Either
postgres isn't healthy yet (wait 10s and retry) or `POSTGRES_HOST` is wrong. Inside the
Compose network the host is `postgres`, not `localhost`.

### Login returns 401 even with correct creds

Likely cookie domain mismatch. The dev cookie has `SameSite=Lax` and no `Domain`
attribute, so it stays on `localhost:8000`. Make sure `curl` persists cookies with
`-c cookies.txt -b cookies.txt` and the SPA uses the same origin.

### Frontend shows "Network Error" on every API call

Check that Vite's `/api` proxy is configured. Default target is `http://localhost:8000`
in `frontend/vite.config.ts`. If you changed `BACKEND_PORT`, set `VITE_API_PROXY` in
`.env` and restart the frontend container.

### Coverage dropped below gate

Two options:

1. Write the missing tests (preferred — coverage gaps are usually real untested paths).
2. Add a `# pragma: no cover` comment with a one-line justification on the line you're
   skipping. Never blanket-skip modules.

### Playwright spec times out waiting for `getByRole('button', { name: ... })`

Two common causes:

1. The frontend is built against a different backend URL. Set `E2E_BASE_URL` or fix
   `make up` to bring up the backend first.
2. The button label changed. Update the spec to match the new label — the suite is a
   canary for accidental UX breakage.

### `make contract-test` fails on a schema you didn't touch

Someone else committed a model change without regenerating `contracts/`. Run
`make contract-gen` and commit the regenerated files in a separate `chore(contracts):`
commit.

## Code style

- **Python**: PEP 8, type hints on all public functions. Imports sorted by ruff.
- **TypeScript**: strict mode. No `any` outside test fixtures. Imports sorted by the
  project's ESLint config.
- **Immutability**: prefer `update(original, field, value)`-style patterns over
  in-place mutation. Pydantic models and React Query responses are immutable by
  default; preserve that.
- **Error handling**: catch at boundaries, not deep in helper functions. Re-raise with
  context (`raise RuntimeError("failed to extract PDF") from exc`).

See `~/.claude/rules/common/coding-style.md` for the full guide.
