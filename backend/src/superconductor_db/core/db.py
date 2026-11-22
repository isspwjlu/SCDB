"""Database engine, session factory, and session helper.

Spec §10 uses Postgres + SQLAlchemy 2.0 typed mappers. This module exposes:

- ``get_engine(dsn)`` -- synchronous engine (used by Alembic and the seed loader).
- ``get_session_factory(engine)`` -- a configured ``sessionmaker``.
- ``get_db()`` -- a generator yielding a ``Session`` that commits on success
  and rolls back on exception.

Async engine wiring lives in ``get_async_engine`` for the worker; the
extraction loops are sync because Alembic is sync-only.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)


def get_engine(dsn: str, **kwargs: Any) -> Engine:
    """Build a synchronous SQLAlchemy engine for the given DSN.

    We turn on ``pool_pre_ping`` so stale connections (Postgres restart) are
    detected and recycled without surfacing ``OperationalError`` to callers.
    """
    if not dsn:
        raise ValueError("get_engine requires a non-empty DSN")
    cfg: dict[str, Any] = {
        "pool_pre_ping": True,
        "future": True,
    }
    cfg.update(kwargs)
    log.debug("creating engine for dsn=%s", dsn.split("@")[-1])
    return create_engine(dsn, **cfg)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured ``sessionmaker`` bound to ``engine``."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped ``Session`` and close it afterwards.

    The extraction loops open their own sessions; this is the generator
    for the callers that want a session with a lifetime they do not
    manage themselves.  On exception we roll back; we never leave a
    session in an unusable state for the next caller (the pool returns
    the connection on close).
    """
    from superconductor_db.core.config import get_settings

    settings = get_settings()
    dsn = (
        settings.database_url
        or settings.postgres_dsn
        or _dsn_from_settings(settings)
    )
    engine = get_engine(
        dsn,
        connect_args={"check_same_thread": False} if dsn.startswith("sqlite") else {},
    )
    SessionLocal = get_session_factory(engine)
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _dsn_from_settings(settings: Any) -> str:
    """Build a sync DSN from settings (psycopg/psycopg2 compatible)."""
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )