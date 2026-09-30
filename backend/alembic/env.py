"""Alembic environment.

Reads the Postgres DSN from ``superconductor_db.core.config.settings`` (NO
``SCDB_`` env prefix per Phase A ruling). Uses ``Base.metadata`` from
``superconductor_db.db.base`` as the target schema.

We keep ``render_as_batch=True`` style migrations off -- Postgres handles
``ALTER`` natively. Autogenerate is available via ``alembic revision
--autogenerate``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure ``src/`` is on sys.path so ``superconductor_db`` resolves regardless
# of where alembic is invoked from.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from superconductor_db.core.config import get_settings  # noqa: E402
from superconductor_db.db.base import Base  # noqa: E402
from superconductor_db.db import models  # noqa: E402,F401  -- side-effect: register tables

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

log = logging.getLogger("alembic.env")


def _build_dsn() -> str:
    """Resolve the sync DSN from application settings."""
    settings = get_settings()
    if settings.postgres_dsn:
        # If the DSN already carries a driver, leave it; otherwise we want
        # psycopg (sync) for alembic regardless of asyncpg for the app.
        return settings.postgres_dsn
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


# Inject the DSN into the alembic config so ``engine_from_config`` works.
config.set_main_option("sqlalchemy.url", _build_dsn())

# Target metadata for autogenerate.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout; no DB connection required."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    section = config.get_section(config.config_ini_section, {})
    if "sqlalchemy.url" not in section:
        section["sqlalchemy.url"] = _build_dsn()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()