#!/bin/bash
# Supervisor: keep paper_search_loop running forever.
# Restarts it 10s after any non-zero exit, OOM kill, or signal.

set -u

WORKDIR="F:/vs_projects/superconductor_database/backend"
LOG="/tmp/scdb_paper_search_loop.log"

cd "$WORKDIR" || exit 1

# Source .env (LLM key, DB URL, scansci-pdf bin).
set -a
. ./.env
set +a

iteration=0
while true; do
    iteration=$((iteration + 1))
    echo "$(date -Iseconds) supervisor: starting paper_search_loop (iteration $iteration)" >> "$LOG"
    PYTHONPATH=src python -m superconductor_db.ingest.paper_search_loop \
        --database-url "$DATABASE_URL" \
        --per-query 8 \
        --max-per-round 8 \
        --delay 120 \
        --start-offset 0 \
        >> "$LOG" 2>&1
    exit_code=$?
    echo "$(date -Iseconds) supervisor: paper_search_loop exited with code $exit_code, restarting in 10s" >> "$LOG"
    sleep 10
done
