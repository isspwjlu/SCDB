#!/bin/bash
# Supervisor: keep curated_loop running forever.
# Restarts it if it dies (any non-zero exit, OOM kill, signal).
# Logs restart events to /tmp/scdb_curated_supervisor.log.

set -u

WORKDIR="F:/vs_projects/superconductor_database/backend"
LOG="/tmp/scdb_curated_loop.log"
SUP_LOG="/tmp/scdb_curated_supervisor.log"

cd "$WORKDIR" || exit 1

# Source .env so the worker inherits LLM_API_KEY etc.
set -a
. ./.env
set +a

iteration=0
while true; do
    iteration=$((iteration + 1))
    echo "$(date -Iseconds) supervisor: starting curated_loop (iteration $iteration)" >> "$SUP_LOG"
    PYTHONPATH=src python -m superconductor_db.ingest.curated_loop \
        --database-url "$DATABASE_URL" \
        --delay 45 \
        --shuffle \
        >> "$LOG" 2>&1
    exit_code=$?
    echo "$(date -Iseconds) supervisor: curated_loop exited with code $exit_code, restarting in 10s" >> "$SUP_LOG"
    sleep 10
done
