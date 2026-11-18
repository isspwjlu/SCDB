"""Shared engine construction for the ingest loops.

The ingest entry points (``paper_ingest``, ``curated_loop``,
``paper_search_loop``) each built their own engine with
``create_engine(...)``. That duplication hid a real concurrency bug: several
loops may run at once against the same SQLite file, and the default journal
mode takes a whole-database write lock, so concurrent commits intermittently
fail with ``database is locked``.

:func:`build_ingest_engine` centralises engine creation and, for SQLite, sets
``journal_mode=WAL`` (readers no longer block the writer) plus a
``busy_timeout`` so a writer waits instead of failing immediately.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


def build_ingest_engine(database_url: str, **kwargs: Any) -> Engine:
    """Create an :class:`Engine` tuned for concurrent ingest against ``database_url``.

    For SQLite URLs this enables WAL journaling and a 30s ``busy_timeout`` on
    every new connection. For any other backend the URL is passed through
    unchanged apart from ``check_same_thread`` handling.
    """
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, **kwargs)

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30.0},
        **kwargs,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        try:
            # WAL lets the API keep reading while a loop commits.
            cursor.execute("PRAGMA journal_mode=WAL")
            # Wait rather than raise when another writer holds the lock.
            cursor.execute("PRAGMA busy_timeout=30000")
            # Enforce the declared FK (materials.paper_id -> papers.id).
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    log.info(
        "sqlite engine ready for %s (WAL + busy_timeout=30000 + foreign_keys)",
        database_url,
    )
    return engine


__all__ = ["build_ingest_engine"]
