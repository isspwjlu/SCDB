"""Ingest run + job queue (spec §10 ``ingest_runs`` and ``ingest_jobs`` tables).

Two tables because:
- ``ingest_runs`` is the top-level grouping (admin visibility, status).
- ``ingest_jobs`` is the per-query worker queue with ``SKIP LOCKED`` semantics.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from superconductor_db.db.base import Base


class IngestRun(Base):
    """A single ingest operation (admin-triggered or scheduled)."""

    __tablename__ = "ingest_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    triggered_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    papers_searched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    papers_downloaded: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    records_extracted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    records_stored: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ingest_runs_status_check",
        ),
    )

    def __repr__(self) -> str:
        return f"IngestRun(id={self.id!r}, status={self.status!r})"


class IngestJob(Base):
    """Worker queue row. Uses ``SKIP LOCKED`` so multiple workers are safe."""

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'failed')",
            name="ingest_jobs_status_check",
        ),
        Index(
            "ingest_jobs_pending_idx",
            "status",
            "created_at",
            postgresql_where=("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"IngestJob(id={self.id!r}, status={self.status!r}, query={self.query!r})"