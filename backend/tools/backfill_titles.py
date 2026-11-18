"""Fill in the paper titles the ingest path never wrote.

Every row of the ``papers`` table carries the placeholder title
``(title unavailable) <doi>``: ``upsert_paper`` was called with the DOI and
the PDF path, and never with a title, so the one piece of metadata the
paper list shows was never stored.

The title was in the parsed markdown all along -- docling writes it as the
document's first heading -- so this tool reads it back out of the markdown
cache and writes it into the database. It also records the markdown path,
which was never stored either.

Usage::

    python tools/backfill_titles.py [db_path] [--dry-run]

The database is the master's when the script runs there and the local
copy otherwise; the markdown cache is taken from the settings, so the two
have to be on the same machine.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND / "src", BACKEND / ".venv" / "Lib" / "site-packages"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from superconductor_db.core.config import get_settings  # noqa: E402
from superconductor_db.ingest.markdown_title import (  # noqa: E402
    load_hash_index,
    markdown_path_for_doi,
    title_from_markdown,
)

log = logging.getLogger("backfill_titles")


def is_placeholder(title: str | None, doi: str) -> bool:
    """True for a title that was never written."""
    if not title:
        return True
    return title.strip() == f"(title unavailable) {doi}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "db_path",
        nargs="?",
        default=str(BACKEND / "_dev.db"),
        help="the SQLite database to fill in",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report without writing"
    )
    args = parser.parse_args()

    settings = get_settings()
    markdown_dir = Path(settings.markdown_cache_dir)
    hash_index = load_hash_index(markdown_dir)
    log.info(
        "markdown cache %s (%d entries), hash index %d",
        markdown_dir, len(list(markdown_dir.glob("*.md"))), len(hash_index),
    )

    db = sqlite3.connect(args.db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "select doi, title, markdown_path from papers where doi is not null"
    ).fetchall()

    filled = 0
    with_path = 0
    unresolved: list[str] = []
    for row in rows:
        doi = row["doi"]
        md_path = markdown_path_for_doi(
            doi, markdown_dir=markdown_dir, hash_index=hash_index
        )
        if md_path is None:
            unresolved.append(doi)
            continue
        if not row["markdown_path"]:
            with_path += 1
        if not is_placeholder(row["title"], doi):
            continue
        try:
            markdown = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("cannot read %s: %s", md_path, exc)
            unresolved.append(doi)
            continue
        title = title_from_markdown(markdown)
        if not title:
            unresolved.append(doi)
            continue
        filled += 1
        if not args.dry_run:
            db.execute(
                "update papers set title = ?, markdown_path = ? where doi = ?",
                (title, str(md_path), doi),
            )

    if not args.dry_run:
        db.commit()
    db.close()

    log.info(
        "papers: %d, titles filled: %d, markdown paths recorded: %d, "
        "still without a title: %d",
        len(rows), filled, with_path, len(unresolved),
    )
    for doi in unresolved[:20]:
        log.info("  no title: %s", doi)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
