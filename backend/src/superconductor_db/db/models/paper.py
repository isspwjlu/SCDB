"""Paper model (spec §10 ``papers`` table)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import ARRAY, Date, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from superconductor_db.db.base import Base


class Paper(Base):
    """One row per ingested paper. Source = arxiv|crossref|semantic_scholar|manual."""

    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    doi: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    authors: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)
    abstract: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    markdown_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    published_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    journal: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ingested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
    )

    __table_args__ = (Index("papers_doi_idx", "doi"),)

    def __repr__(self) -> str:
        return f"Paper(id={self.id!r}, doi={self.doi!r}, title={self.title!r})"