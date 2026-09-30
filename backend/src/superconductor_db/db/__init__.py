"""SQLAlchemy ORM package.

Importing this package (or any model module) is what registers the tables with
``Base.metadata`` so Alembic can autogenerate from it.
"""

from __future__ import annotations

from superconductor_db.db.base import Base

__all__ = ["Base"]