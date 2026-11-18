"""Prepare a re-extraction: export the DOI queue, back up, and clear.

The queue has to leave the database before the tables are emptied -- the
DOI list *is* the record of which papers were fetched, and it is what
ties a paper to the PDF already sitting in the cache. With it in hand the
re-extraction can walk the corpus without searching the internet again.

Usage::

    python tools/prepare_reingest.py --database-url <url> [--apply]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
QUEUE = BACKEND / "data" / "ingest_queue.json"

# Tables emptied before a re-extraction. ``materials`` is the extracted
# data; ``papers`` is the provenance the loop uses to decide what it has
# already done, so it has to go too or nothing is re-ingested.
# ``ingest_runs`` is the bookkeeping of the old runs. The user accounts,
# the audit trail and the health check are left alone.
CLEARED_TABLES = ("materials", "papers", "ingest_runs")


def export_queue(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = list(
        conn.execute("select doi, pdf_path from papers where doi is not null")
    )
    queue = [{"doi": doi, "pdf_path": pdf_path} for doi, pdf_path in rows]
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps(queue, indent=1), encoding="utf-8")
    return queue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=f"sqlite:///{BACKEND / '_dev.db'}",
        help="sqlite database to clear",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually clear the tables"
    )
    args = parser.parse_args()

    path = args.database_url.replace("sqlite:///", "").replace("sqlite://", "")
    conn = sqlite3.connect(path)
    try:
        queue = export_queue(conn)
        print(f"queue: {len(queue)} DOIs -> {QUEUE}")

        if not args.apply:
            print("dry run; pass --apply to clear")
            return 0

        backup = path.replace(".db", f".preclear_{time.strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")

        for table in CLEARED_TABLES:
            before = conn.execute(f"select count(*) from [{table}]").fetchone()[0]
            conn.execute(f"delete from [{table}]")
            print(f"cleared {table}: {before} rows")
        conn.commit()
        conn.execute("vacuum")
        conn.commit()
        for table in CLEARED_TABLES:
            after = conn.execute(f"select count(*) from [{table}]").fetchone()[0]
            print(f"{table}: {after} rows now")
        kept = conn.execute("select count(*) from users").fetchone()[0]
        print(f"users kept: {kept}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
