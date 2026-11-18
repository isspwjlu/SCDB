"""Provenance helpers — link ingested materials back to their source paper.

The ingest loops used to write a ``doi`` string onto every ``materials`` row and
never created a ``papers`` row, so ``materials.paper_id`` stayed NULL for the
entire database: no extracted Tc could be traced back to the paper it came from.

:func:`upsert_paper` is idempotent on DOI and returns the ``Paper`` id, which
callers assign to ``Material.paper_id``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from superconductor_db.db.models.paper import Paper

log = logging.getLogger(__name__)


def _source_from_identifier(identifier: str) -> str:
    """Classify an identifier so ``papers.source`` is meaningful."""
    lowered = identifier.lower()
    if "arxiv" in lowered:
        return "arxiv"
    if lowered.startswith("10."):
        return "doi"
    return "unknown"


def upsert_paper(
    session: Session,
    identifier: str,
    *,
    title: str | None = None,
    pdf_path: str | None = None,
    markdown_path: str | None = None,
) -> uuid.UUID:
    """Return the ``Paper.id`` for ``identifier``, creating the row if needed.

    Idempotent: a second call for the same DOI returns the existing id and only
    backfills ``title`` / ``pdf_path`` / ``markdown_path`` when they were
    previously unknown.
    ``identifier`` is the DOI (e.g. ``10.48550/arXiv.cond-mat/0212371``); when
    the paper is not yet known, a placeholder title is written so the NOT NULL
    constraint holds — the real title is filled in by a later pass.
    """
    if not identifier:
        raise ValueError("upsert_paper requires a non-empty identifier")

    existing = session.execute(
        select(Paper).where(Paper.doi == identifier).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        changed = False
        if title and existing.title in (None, "", _placeholder_title(identifier)):
            existing.title = title
            changed = True
        if pdf_path and existing.pdf_path is None:
            existing.pdf_path = pdf_path
            changed = True
        if markdown_path and existing.markdown_path is None:
            existing.markdown_path = markdown_path
            changed = True
        if changed:
            session.flush()
        return existing.id

    paper = Paper(
        id=uuid.uuid4(),
        doi=identifier,
        title=title or _placeholder_title(identifier),
        source=_source_from_identifier(identifier),
        pdf_path=pdf_path,
        markdown_path=markdown_path,
        ingested_at=datetime.now(timezone.utc),
    )
    session.add(paper)
    session.flush()  # assign paper.id before we reference it
    log.info("  created paper %s (%s)", identifier, paper.source)
    return paper.id


def _placeholder_title(identifier: str) -> str:
    return f"(title unavailable) {identifier}"


__all__ = ["upsert_paper"]
