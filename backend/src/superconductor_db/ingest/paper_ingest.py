"""Single-paper ingest script — arXiv ID → scansci-pdf → MiniMax extract → DB.

Standalone CLI that exercises the full production ingest pipeline for one
paper at a time (no search step; arXiv ID passed directly). Useful when:

- arXiv / CrossRef / S2 anonymous rate limits block the search step
- you want to backfill a known paper
- debugging the LLM extractor against a fixed input

Usage:
  cd backend
  DATABASE_URL="sqlite:///./_dev.db" \\
  LLM_BASE_URL=https://api.minimaxi.com/v1 \\
  python -m superconductor_db.ingest.paper_ingest <arxiv-id-or-doi>

  # Multiple papers:
  python -m superconductor_db.ingest.paper_ingest \\
      10.48550/arXiv.cond-mat/0212371 \\
      10.48550/arXiv.cond-mat/0307128 \\
      10.48550/arXiv.cond-mat/0404562

Each paper's records are upserted into the materials table (dedup on
doi + composition). Failed papers log to stderr and continue.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import superconductor_db.db.models  # noqa: F401 — register tables
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.db.models.material import Material
from superconductor_db.db.models.paper import Paper
from superconductor_db.db.repository import MaterialsRepository
from superconductor_db.ingest.db_setup import build_ingest_engine
from superconductor_db.ingest.docling_parser import markdown_cache_path
from superconductor_db.ingest.extractor import (
    SuperconductingExtractor,
    classify_tc_evidence,
    evidence_for,
)
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.markdown_title import title_from_markdown
from superconductor_db.ingest.netresilience import with_network_retry
from superconductor_db.ingest.direct_fetch import is_downloadable
from superconductor_db.ingest.provenance import upsert_paper
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    download_pdf,
    parse_pdf_to_markdown,
)
from superconductor_db.ingest.whitelist import is_known_superconductor


def _default_database_url() -> str:
    """Fall back to the configured DATABASE_URL.

    A hard-coded relative ``sqlite:///./_dev.db`` silently creates a new
    empty database whenever the process starts from a different working
    directory, which looks like total data loss.
    """
    from superconductor_db.core.config import get_settings

    return get_settings().database_url or "sqlite:///./_dev.db"


log = logging.getLogger("paper_ingest")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# --------------------------------------------------------------------------- #
# Per-paper pipeline                                                          #
# --------------------------------------------------------------------------- #



def _apply_evidence(row: dict[str, Any], record: Any, comp: str) -> str:
    """Attach the Tc evidence sentence and decide whether the row is a measurement.

    Returns a short status for logging, or the empty string when the evidence
    shows a calculated value and the row must be rejected. The evidence quote
    is stored in ``notes`` so a reviewer can check the number, and its wording
    decides measurement vs prediction — the LLM's own ``Experimental`` flag is
    unreliable.

    A calculated Tc is refused outright rather than stored with a false flag.
    PRL 119, 107001 contributed LaH10 at 288 K and YH9 at 276 K that way, and
    because the dashboard sorts by Tc they took the top two rows, presenting
    theory as the highest recorded transition in the database.
    """
    quotes = evidence_for(comp)
    tc_quote = quotes.get("Tc", "")
    if not tc_quote:
        return "no evidence"
    row["notes"] = f"Tc evidence: {tc_quote}"
    verdict = classify_tc_evidence(tc_quote)
    if verdict == "theoretical":
        return ""
    if verdict == "experimental":
        row["experimental"] = True
    return verdict or "unclassified"


async def ingest_one(
    session_factory: sessionmaker[Session],
    identifier: str,
    *,
    markdown_char_limit: int = 60000,
    skip_known_papers: bool = False,
    client: LLMClient | None = None,
    serial_prompts: bool = False,
) -> tuple[int, int, bool]:
    """Run the full ingest pipeline for one identifier.

    Returns ``(records_extracted, records_stored, pdf_downloaded)`` — the third
    element is True when a PDF was fetched and parsed, which the loops use to
    keep ``IngestRun.papers_downloaded`` accurate.

    ``client`` can be passed in so that a caller running many papers can
    read one accumulated token count instead of one per paper.
    """
    log.info("--- ingest %s ---", identifier)
    client = client or LLMClient()
    extractor = SuperconductingExtractor(client)

    # 0. Skip DOIs no channel here can retrieve. A doomed attempt costs a
    # 180 s timeout and a download slot; 7 of 72 failures in 24 h were SSRN
    # (10.2139), an author-posting platform whose PDFs sit behind an upload wall.
    if not is_downloadable(identifier):
        log.info("skip %s — no retrievable full text for this prefix", identifier)
        return 0, 0, False

    # 0.5 Skip identifiers that were already processed to the extraction stage
    # at least once. ``upsert_paper`` only creates the Paper row *after* a
    # successful download + extraction, so an existing row is a durable
    # "already swept" marker that survives restarts. The curated list is
    # static and fully swept; without this guard a supervisor restart
    # re-downloaded and re-extracted every paper (~8 LLM calls each) only to
    # hit the per-row dedup at the end. Deliberately row-count-agnostic:
    # papers whose extraction yielded nothing stay skipped too — re-running
    # the loop is not the backfill path, ``paper_ingest`` CLI (no skip) is.
    if skip_known_papers:
        with session_factory() as s:
            known = s.execute(
                select(Paper.id).where(Paper.doi == identifier).limit(1)
            ).scalar_one_or_none()
            if known is not None:
                log.info("skip %s — already ingested (paper row exists)", identifier)
                return 0, 0, False

    # 1. PDF. A transport failure means the machine is offline, not that the
    # paper is unavailable, so it is waited out rather than counted as a
    # failed download; only a genuine ScanSciError means "no such PDF".
    try:
        pdf_path = await with_network_retry(
            lambda: download_pdf(identifier, timeout=180),
            description=f"download {identifier}",
        )
    except ScanSciError as exc:
        log.error("download failed: %s", exc)
        return 0, 0, False
    if pdf_path is None:
        log.error("download abandoned while offline: %s", identifier)
        return 0, 0, False

    # 2. Markdown via docling (tables come out as markdown tables).
    # The limit is generous because a docling markdown carries the paper's
    # tables -- the richest source of Tc values -- and cutting them off
    # would lose exactly what the extractor is looking for.
    md = await parse_pdf_to_markdown(pdf_path)
    log.info("PDF %d bytes, markdown %d chars", pdf_path.stat().st_size, len(md))
    md = md[:markdown_char_limit]

    # 3. LLM extract (MiniMax). Same reasoning: an outage here must not
    # silently drop a paper that has already been downloaded and parsed.
    records = await with_network_retry(
        lambda: extractor.extract_all(
            md, doi=identifier, serial_prompts=serial_prompts
        ),
        description=f"extract {identifier}",
    )
    if records is None:
        log.error("extraction abandoned while offline: %s", identifier)
        return 0, 0, False
    log.info("LLM extracted %d records", len(records))
    usage = client.usage
    log.info(
        "LLM usage: %d calls, prompt %d (cached %d), completion %d "
        "(reasoning %d)",
        usage["calls"],
        usage["prompt_tokens"],
        usage["cached_tokens"],
        usage["completion_tokens"],
        usage["reasoning_tokens"],
    )

    # 4. Upsert into materials table.
    with session_factory() as s:
        result = store_records(
            s,
            identifier,
            records,
            source="paper_ingest",
            title=title_from_markdown(md),
            pdf_path=str(pdf_path),
            markdown_path=str(markdown_cache_path(pdf_path)),
        )
    log.info(
        "  extracted=%d stored=%d skipped_hallucination=%d",
        len(records), result.stored, result.skipped_whitelist,
    )
    return len(records), result.stored, True


@dataclass(frozen=True)
class StoreResult:
    """What writing a set of records into the corpus did."""

    stored: int = 0
    merged: int = 0
    skipped_no_data: int = 0
    skipped_whitelist: int = 0
    skipped_no_tc: int = 0
    skipped_no_doi: int = 0
    skipped_calculated: int = 0
    skipped_duplicate: int = 0
    paper_id: uuid.UUID | None = None


def store_records(
    session: Session,
    identifier: str,
    records: list[Any],
    *,
    source: str,
    title: str | None = None,
    pdf_path: str | None = None,
    markdown_path: str | None = None,
    apply_whitelist: bool = True,
    evidence_already_applied: bool = False,
    extra_notes: str | None = None,
) -> StoreResult:
    """Write extracted records into the corpus.

    This is the one place where a record becomes a row: the corpus
    pipeline and the path a reader's paper takes both come through here,
    so the two cannot drift apart.

    ``apply_whitelist`` is off for a reader's paper: the whitelist is a
    guard against the model reciting a material from a reference list,
    and a human who sent the paper is a better judge of it than the
    list is.  The records the list turned away are shown to the
    reviewer instead of being dropped unseen.

    ``evidence_already_applied`` says that the records carry their
    evidence sentence already -- which is the case for a reader's paper,
    because the registry that holds the quotes does not survive the
    process that read the paper.

    ``extra_notes`` is appended to the evidence sentence, so that a row
    that came from a reader can be told from one the corpus found.
    """
    stored = 0
    merged = 0
    skipped_no_data = 0
    skipped_whitelist = 0
    skipped_no_tc = 0
    skipped_no_doi = 0
    skipped_calculated = 0
    skipped_duplicate = 0

    # Create the source Paper row so every Material can be traced back to
    # the paper it was extracted from (materials.paper_id). The title
    # comes from the parsed markdown: docling writes it as the
    # document's first heading, and without it every paper was stored
    # under the placeholder "(title unavailable) <doi>".
    paper_id = upsert_paper(
        session,
        identifier,
        title=title,
        pdf_path=pdf_path,
        markdown_path=markdown_path,
    )

    for r in records:
        if not r.has_meaningful_data():
            log.info("  skip %s — no SC quantity", r.material_chemical_composition)
            skipped_no_data += 1
            continue
        comp = r.material_chemical_composition
        # Whitelist guard: LLM sometimes hallucinates non-SC
        # compositions (e.g. V2O3 mentioned in a reference list).
        if apply_whitelist and not is_known_superconductor(comp):
            log.warning(
                "  skip %s — not on SC whitelist (likely LLM hallucination)",
                comp,
            )
            skipped_whitelist += 1
            continue
        sp = r.superconducting_property
        tc = sp.Tc.value if sp.Tc else None
        if tc is None or not isinstance(tc, (int, float)):
            # INFO, not DEBUG: an all-silent round (extracted=11 stored=0)
            # is undiagnosable from the log when the dominant skip reason
            # is invisible.
            log.info("  skip %s — no numeric Tc (skipping)", comp)
            skipped_no_tc += 1
            continue
        doi = (r.doi or "").strip()
        # Dedup by DOI only. Multiple papers reporting the same
        # compound at the same Tc are distinct evidence rows
        # (independent measurements, different papers) and must
        # be preserved. Re-extracting the SAME paper on the same
        # compound collapses via the DOI match.
        if not doi:
            log.info(
                "  skip %s — paper has no DOI (skipping to avoid dedup gap)",
                comp,
            )
            skipped_no_doi += 1
            continue
        existing_row = session.execute(
            select(Material).where(
                Material.chemical_composition == comp,
                Material.doi == doi,
            ).limit(1)
        ).first()
        if existing_row is not None:
            # Field-level merge: NULL columns in the existing row are
            # filled by the freshly-extracted values. Non-NULL values
            # are preserved (later extraction may be less complete than
            # earlier — we keep the best). This means re-running an
            # extraction over the same paper *fills in* missing fields
            # rather than overwriting good data.
            #
            # Uses the same ``_record_to_row`` mapper as the insert path so
            # every extracted field participates. The previous version
            # probed attributes like ``r.hc2_value`` that do not exist on
            # ``SuperconductingRecord``, so ``hasattr`` was always False and
            # the merge silently filled nothing.
            existing = existing_row[0]
            changed = False
            for attr, new_val in MaterialsRepository._record_to_row(r).items():
                if attr in {"chemical_composition", "doi", "source"}:
                    continue
                cur = getattr(existing, attr, None)
                if cur is None and new_val is not None:
                    setattr(existing, attr, new_val)
                    changed = True
            if existing.paper_id is None and paper_id is not None:
                existing.paper_id = paper_id
                changed = True
            if changed:
                existing.updated_at = datetime.now(timezone.utc)
                session.commit()
                merged += 1
                log.info(
                    "  merged fields for %s (DOI %s) — no new row",
                    comp, doi,
                )
            else:
                skipped_duplicate += 1
                log.info(
                    "  skip %s DOI=%s — already complete in DB",
                    comp, doi,
                )
            continue
        # ``_record_to_row`` already carries ``source`` (the record's own
        # provenance, e.g. "llm_extract"), so override it here rather than
        # passing it twice — a duplicate keyword raises TypeError and the
        # insert is lost.
        row = MaterialsRepository._record_to_row(r)
        row["source"] = source
        if extra_notes:
            row["notes"] = f"{row.get('notes')} | {extra_notes}" if row.get(
                "notes"
            ) else extra_notes
        # Persist the evidence sentence and let its wording decide whether
        # this is a measurement. A calculated Tc is dropped: it would
        # otherwise sort to the top of the dashboard as if it were observed.
        # A reader's paper has been through this already -- the quote is
        # in the record's notes -- so it is not asked twice here.
        if evidence_already_applied:
            evidence = r.notes or ""
        else:
            evidence = _apply_evidence(row, r, comp)
        if not evidence:
            log.info("  skip %s — evidence shows a calculated Tc", comp)
            skipped_calculated += 1
            continue
        m = Material(id=uuid.uuid4(), paper_id=paper_id, **row)
        session.add(m)
        stored += 1
        log.info("  + %s Tc=%s (%s)", comp, tc, evidence or "no evidence")
    session.commit()
    return StoreResult(
        stored=stored,
        merged=merged,
        skipped_no_data=skipped_no_data,
        skipped_whitelist=skipped_whitelist,
        skipped_no_tc=skipped_no_tc,
        skipped_no_doi=skipped_no_doi,
        skipped_calculated=skipped_calculated,
        skipped_duplicate=skipped_duplicate,
        paper_id=paper_id,
    )


# --------------------------------------------------------------------------- #
# CLI                                                                        #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "identifiers", nargs="+",
        help="One or more arXiv IDs (e.g. 10.48550/arXiv.cond-mat/0212371) or DOIs",
    )
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument("--run-id", default=None,
                        help="Optional explicit IngestRun UUID; new one is created per call if omitted")
    args = parser.parse_args(argv)

    engine = build_ingest_engine(args.database_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    with SessionLocal() as s:
        s.add(IngestRun(id=run_id, status="running"))
        s.commit()

    total_extracted = 0
    total_stored = 0
    total_downloaded = 0
    failures = 0
    t0 = time.time()
    for ident in args.identifiers:
        downloaded = False
        try:
            extracted, stored, downloaded = asyncio.run(
                ingest_one(SessionLocal, ident)
            )
            total_extracted += extracted
            total_stored += stored
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest_one(%s) crashed: %s", ident, exc)
            failures += 1
        if downloaded:
            total_downloaded += 1
        # Update run counters incrementally.
        with SessionLocal() as s:
            run = s.get(IngestRun, run_id)
            if run is not None:
                run.papers_searched = (run.papers_searched or 0) + 1
                run.papers_downloaded = (run.papers_downloaded or 0) + (
                    1 if downloaded else 0
                )
                run.records_extracted = (run.records_extracted or 0) + total_extracted
                run.records_stored = (run.records_stored or 0) + total_stored
                run.errors = (run.errors or 0) + failures
                s.commit()

    elapsed = time.time() - t0
    log.info(
        "DONE: %d papers in %.1fs — extracted=%d stored=%d failures=%d",
        len(args.identifiers), elapsed, total_extracted, total_stored, failures,
    )
    # Mark run completed.
    with SessionLocal() as s:
        run = s.get(IngestRun, run_id)
        if run is not None:
            run.status = "completed" if failures == 0 else "failed"
            run.finished_at = datetime.now(timezone.utc)
            s.commit()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
