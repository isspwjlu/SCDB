"""SQLAlchemy 2.0 declarative base.

We use the typed ``DeclarativeBase`` (no legacy ``declarative_base()``). All
models inherit from :class:`Base` and declare their columns with explicit
``Mapped[...]`` types so ``mypy --strict`` is happy.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base for all ORM models."""

    # Reserved for future project-wide mixins (e.g. ``created_at`` /
    # ``updated_at`` columns). Empty for now per spec §10.
    pass