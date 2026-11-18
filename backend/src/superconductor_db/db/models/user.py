"""User model (spec §10 ``users`` table)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from superconductor_db.db.base import Base
from superconductor_db.db.types import GUID


class User(Base):
    """Authenticated user. Roles drive RBAC (spec §9)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=lambda: uuid.uuid4(),
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    # The person's name. It is asked for at registration; it may be
    # empty for the accounts that predate the question.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Where the person works. It is asked for at registration; it may be
    # empty for the accounts that predate the question.
    institution: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default="contributor",
    )
    is_active: Mapped[bool] = mapped_column(
        nullable=False,
        server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('contributor', 'reviewer', 'admin')",
            name="users_role_check",
        ),
        Index("users_role_idx", "role"),
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r}, role={self.role!r})"