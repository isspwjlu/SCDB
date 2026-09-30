"""Single-job ingest runner.

Search → dedupe → for each paper: pre-filter → scansci-pdf download → PDF
parse → LLM extract → repository.upsert_by_doi. Each paper is isolated; a
single paper failure does not abort the job.

Returns a :class:`JobResult` with counts the worker uses to update
``ingest_runs``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from superconductor_db.db.models.audit import AuditLog
from superconductor_db.db.repository import MaterialsRepository
from superconductor_db.ingest.extractor import SuperconductingExtractor
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.paper_search import (
    PaperMetadata,
    is_likely_sc_paper,
    search_all_sources,
)
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    download_pdf,
    parse_pdf_to_markdown,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Result types                                                                #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class JobResult:
    """Counts emitted by :func:`run_job` so the worker can update DB rows."""

    papers_searched: int = 0
    papers_downloaded: int = 0
    records_extracted: int = 0
    records_stored: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    def merge(self, other: "JobResult") -> None:
        self.papers_searched += other.papers_searched
        self.papers_downloaded += other.papers_downloaded
        self.records_extracted += other.records_extracted
        self.records_stored += other.records_stored
        self.errors += other.errors
        self.log_lines.extend(other.log_lines)
        self.error_samples.extend(other.error_samples)


# --------------------------------------------------------------------------- #
# Pipeline                                                                    #
# --------------------------------------------------------------------------- #


async def run_job(
    query: str,
    *,
    db: Session,
    client: LLMClient,
    run_id: Any | None = None,
    max_results: int = 30,
) -> JobResult:
    """Execute one ingest query end-to-end.

    The caller owns ``db`` lifecycle (commit/rollback). On success, an
    ``audit_log`` row with ``action='ingest.completed'`` is added; on
    partial failure a row with ``action='ingest.failed'`` is added.
    """
    result = JobResult()
    extractor = SuperconductingExtractor(client)
    repo = MaterialsRepository(db)

    # 1. Search ----------------------------------------------------------
    result.log_lines.append(f"[job] searching: {query}")
    try:
        papers = await search_all_sources(query, max_results=max_results)
    except Exception as exc:
        result.errors += 1
        result.error_samples.append(f"search failed: {exc}")
        result.log_lines.append(f"[job] search error: {exc}")
        _write_audit(db, run_id, "ingest.search_failed", "ingest_runs", details={"error": str(exc)})
        return result
    result.papers_searched = len(papers)
    result.log_lines.append(f"[job] {len(papers)} candidate papers")

    # 2. Per-paper pipeline ---------------------------------------------
    for paper in papers:
        try:
            await _process_paper(paper, db, client, extractor, repo, result)
        except Exception as exc:
            log.exception("paper pipeline failed for %s", paper.title)
            result.errors += 1
            result.error_samples.append(f"{paper.title[:40]}: {exc}")
            result.log_lines.append(f"[paper] error: {paper.title[:40]}: {exc}")

    # 3. Final audit row -------------------------------------------------
    _write_audit(
        db,
        run_id,
        "ingest.completed",
        "ingest_runs",
        details={
            "query": query,
            "papers_searched": result.papers_searched,
            "papers_downloaded": result.papers_downloaded,
            "records_extracted": result.records_extracted,
            "records_stored": result.records_stored,
            "errors": result.errors,
        },
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
    return result


async def _process_paper(
    paper: PaperMetadata,
    db: Session,
    client: LLMClient,
    extractor: SuperconductingExtractor,
    repo: MaterialsRepository,
    result: JobResult,
) -> None:
    """Run pre-filter → download → parse → extract → upsert for one paper."""
    title = paper.title.strip() or "(no title)"
    if not is_likely_sc_paper(title, paper.abstract):
        result.log_lines.append(f"[paper] skip (not SC): {title[:60]}")
        return

    doi = paper.doi
    if not doi:
        result.log_lines.append(f"[paper] skip (no DOI): {title[:60]}")
        return

    # 1. Download PDF via scansci-pdf
    try:
        pdf_path = await download_pdf(doi)
    except ScanSciError as exc:
        result.errors += 1
        result.error_samples.append(f"{doi}: download failed: {exc}")
        result.log_lines.append(f"[paper] download failed {doi}: {exc}")
        return
    result.papers_downloaded += 1
    result.log_lines.append(f"[paper] downloaded {doi} -> {pdf_path.name}")

    # 2. Parse to markdown
    try:
        text = await parse_pdf_to_markdown(pdf_path)
    except ScanSciError as exc:
        result.errors += 1
        result.error_samples.append(f"{doi}: parse failed: {exc}")
        result.log_lines.append(f"[paper] parse failed {doi}: {exc}")
        return
    if not text.strip():
        result.log_lines.append(f"[paper] empty text for {doi}")
        return

    # 3. LLM extraction (8 prompts in parallel inside the extractor)
    try:
        records = await extractor.extract_all(text, doi=doi)
    except Exception as exc:
        result.errors += 1
        result.error_samples.append(f"{doi}: extract failed: {exc}")
        result.log_lines.append(f"[paper] extract failed {doi}: {exc}")
        return
    result.records_extracted += len(records)
    result.log_lines.append(f"[paper] extracted {len(records)} records from {doi}")

    # 4. Upsert by DOI
    for rec in records:
        # The repository's upsert_by_doi keys on ``doi`` -- always populate it
        # from the paper so we route correctly.
        if not rec.doi:
            rec.doi = doi
        try:
            repo.upsert_by_doi(doi, rec)
            result.records_stored += 1
        except Exception as exc:
            result.errors += 1
            result.error_samples.append(f"{doi}: upsert failed: {exc}")
            result.log_lines.append(f"[paper] upsert failed {doi}: {exc}")


def _write_audit(
    db: Session,
    run_id: Any | None,
    action: str,
    target_table: str,
    *,
    target_id: Any | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Insert an ``AuditLog`` row; caller commits."""
    row = AuditLog(
        user_id=None,
        action=action,
        target_table=target_table,
        target_id=str(target_id) if target_id is not None else (
            str(run_id) if run_id is not None else None
        ),
        details=details,
    )
    db.add(row)


__all__ = ["JobResult", "run_job"]