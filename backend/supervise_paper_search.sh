#!/bin/bash
# Supervisor: keep paper_search_loop running.
# Restarts it 10s after any non-zero exit, OOM kill, or signal.
#
# Notes:
#  - Uses the project venv interpreter explicitly. A bare `python` resolved to
#    a different (Anaconda) install, so the loop ran against a stale copy of
#    the code and a different set of dependencies.
#  - Forces an absolute SQLite path. A relative DATABASE_URL resolves against
#    the process working directory and silently creates a fresh empty database.
#  - Advances the arXiv cursor across restarts (via a state file) so a restart
#    does not re-fetch and re-spend LLM calls on the same papers.

set -u

WORKDIR="F:/vs_projects/superconductor_database/backend"
LOG="/tmp/scdb_paper_search_loop.log"
STATE="/tmp/scdb_paper_search_offset"
PY="$WORKDIR/.venv/Scripts/python.exe"
DB_URL="sqlite:///F:/vs_projects/superconductor_database/backend/_dev.db"

cd "$WORKDIR" || exit 1

[ -x "$PY" ] || { echo "FATAL: venv python not found at $PY" | tee -a "$LOG"; exit 1; }

# Source .env for the LLM key / scansci binary, then force the DB URL so an
# inherited or stale value cannot point the loop at the wrong database.
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

# Resume the discovery cursor where the previous process stopped.
if [ -f "$STATE" ]; then
    START_OFFSET=$(cat "$STATE" 2>/dev/null || echo 0)
else
    START_OFFSET=0
fi
[ -n "$START_OFFSET" ] || START_OFFSET=0

iteration=0
while true; do
    iteration=$((iteration + 1))
    echo "$(date -Iseconds) supervisor: starting paper_search_loop (iteration $iteration, start-offset=$START_OFFSET)" >> "$LOG"
    "$PY" -m superconductor_db.ingest.paper_search_loop \
        --database-url "$DB_URL" \
        --per-query 10 \
        --max-per-round 24 \
        --concurrency 4 \
        --delay 30 \
        --start-offset "$START_OFFSET" \
        --max-empty-rounds 3 \
        \
        >> "$LOG" 2>&1
    # (OpenAlex + arXiv are enabled; CrossRef is disabled because ~92% of its
    #  hits are paywalled and unretrievable.)
    exit_code=$?
    # Advance the cursor so the next run fetches fresh preprints.
    START_OFFSET=$((START_OFFSET + 16))
    echo "$START_OFFSET" > "$STATE"
    echo "$(date -Iseconds) supervisor: paper_search_loop exited with code $exit_code (next start-offset=$START_OFFSET), restarting in 15s" >> "$LOG"
    if [ "$exit_code" = "0" ]; then
        echo "$(date -Iseconds) supervisor: clean exit (search exhausted), restarting in 15s" >> "$LOG"
    fi
    sleep 15
done
