"""Audit log model (spec §10 ``audit_log`` table).

BigSerial PK keeps writes cheap; reads are time-desc for the admin viewer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from superconductor_db.db.base import Base


class AuditLog(Base):
    """Append-only audit log. Every state-changing endpoint writes a row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_table: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "audit_log_created_idx",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index(
            "audit_log_user_idx",
            "user_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
    )

    def __repr__(self) -> str:
        return f"AuditLog(id={self.id!r}, action={self.action!r})"