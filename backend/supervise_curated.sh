#!/bin/bash
# Supervisor: keep curated_loop running.
# Restarts it if it dies (any non-zero exit, OOM kill, signal).
# Logs restart events to /tmp/scdb_curated_supervisor.log.
#
# Notes:
#  - Uses the project venv interpreter explicitly; a bare `python` resolved to
#    a different (Anaconda) install with stale code and different deps.
#  - Forces an absolute SQLite path — a relative DATABASE_URL silently creates a
#    fresh empty database when the working directory differs.
#  - curated_loop now sweeps its finite ID list once and exits by design, so a
#    restart re-sweeps the list. Re-ingesting an already-complete paper is
#    idempotent (field-level merge, no new row), but it still costs LLM calls —
#    the long-term fix is a persistent "already swept" marker, tracked in
#    docs/development.md.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$ROOT/backend"
LOG="/tmp/scdb_curated_loop.log"
SUP_LOG="/tmp/scdb_curated_supervisor.log"
PY="$WORKDIR/.venv/Scripts/python.exe"
DB_URL="sqlite:///$WORKDIR/_dev.db"

cd "$WORKDIR" || exit 1

[ -x "$PY" ] || { echo "FATAL: venv python not found at $PY" | tee -a "$SUP_LOG"; exit 1; }

# Source .env so the worker inherits LLM_API_KEY etc., then force the DB URL.
set -a
. ./.env
set +a

# Scrub proxy variables: the desktop VPN client exports a stale local proxy
# (accepts TCP, fails every CONNECT with 502) which killed every scansci
# download from ~13:00 on 2026-10-05. Direct egress works for arXiv / Nature /
# APS / Sci-Hub on this network, and the LLM client already bypasses env
# proxies (trust_env=False), so nothing here needs one.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

export DATABASE_URL="$DB_URL"
export PYTHONPATH="src"

iteration=0
while true; do
    iteration=$((iteration + 1))
    echo "$(date -Iseconds) supervisor: starting curated_loop (iteration $iteration)" >> "$SUP_LOG"
    "$PY" -m superconductor_db.ingest.curated_loop \
        --database-url "$DB_URL" \
        --delay 5 \
        --concurrency 3 \
        --shuffle \
        >> "$LOG" 2>&1
    exit_code=$?
    echo "$(date -Iseconds) supervisor: curated_loop exited with code $exit_code, restarting in 30s" >> "$SUP_LOG"
    sleep 30
done
