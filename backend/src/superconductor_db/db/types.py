"""Portable SQLAlchemy types — work on both Postgres and SQLite.

The project's spec is Postgres 16 (JSONB, native UUID, ARRAY). Tests run
without Docker on SQLite for fast feedback. These ``TypeDecorator`` shims
let the ORM models be written once and run on either dialect.

- ``GUID`` stores ``uuid.UUID`` as ``CHAR(36)`` on SQLite, native ``UUID``
  on Postgres.
- ``JSONType`` stores Python ``dict | list`` as JSON text on SQLite, JSONB
  on Postgres.

Trade-off: SQLite uses JSON1 extension text columns, so indexing is
limited compared to JSONB's GIN indexes. Production (Postgres) keeps full
JSONB semantics.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import JSON, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID


class GUID(TypeDecorator):
    """Cross-dialect UUID column.

    - Postgres: native ``UUID`` column.
    - SQLite: ``CHAR(36)`` storing canonical string form.
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONType(TypeDecorator):
    """Cross-dialect JSON column.

    - Postgres: ``JSONB``.
    - SQLite: ``JSON`` (text under the hood, JSON1 extension for ops).
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, default=str)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


__all__ = ["GUID", "JSONType"]
