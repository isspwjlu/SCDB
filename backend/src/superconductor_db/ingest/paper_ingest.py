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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import superconductor_db.db.models  # noqa: F401 — register tables
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.db.models.material import Material
from superconductor_db.ingest.extractor import SuperconductingExtractor
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    download_pdf,
    parse_pdf_to_markdown,
)
from superconductor_db.ingest.whitelist import is_known_superconductor

log = logging.getLogger("paper_ingest")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# --------------------------------------------------------------------------- #
# Per-paper pipeline                                                          #
# --------------------------------------------------------------------------- #


async def ingest_one(
    session_factory: sessionmaker[Session],
    identifier: str,
    *,
    markdown_char_limit: int = 28000,
) -> tuple[int, int]:
    """Run the full ingest pipeline for one identifier.

    Returns ``(records_extracted, records_stored)``.
    """
    log.info("--- ingest %s ---", identifier)
    client = LLMClient()
    extractor = SuperconductingExtractor(client)

    # 1. PDF via scansci-pdf
    try:
        pdf_path = await download_pdf(identifier, timeout=180)
    except ScanSciError as exc:
        log.error("download failed: %s", exc)
        return 0, 0

    # 2. Markdown via PyMuPDF
    md = await parse_pdf_to_markdown(pdf_path)
    log.info("PDF %d bytes, markdown %d chars", pdf_path.stat().st_size, len(md))
    md = md[:markdown_char_limit]

    # 3. LLM extract (MiniMax)
    records = await extractor.extract_all(md, doi=identifier)
    log.info("LLM extracted %d records", len(records))

    # 4. Upsert into materials table.
    stored = 0
    skipped_hallucination = 0
    with session_factory() as s:
        for r in records:
            if not r.has_meaningful_data():
                log.info("  skip %s — no SC quantity", r.material_chemical_composition)
                continue
            comp = r.material_chemical_composition
            # Whitelist guard: LLM sometimes hallucinates non-SC
            # compositions (e.g. V2O3 mentioned in a reference list).
            if not is_known_superconductor(comp):
                log.warning(
                    "  skip %s — not on SC whitelist (likely LLM hallucination)",
                    comp,
                )
                skipped_hallucination += 1
                continue
            sp = r.superconducting_property
            tc = sp.Tc.value if sp.Tc else None
            if tc is None or not isinstance(tc, (int, float)):
                log.debug("  skip %s — no numeric Tc (skipping)", comp)
                continue
            doi = (r.doi or "").strip()
            # Dedup by DOI only. Multiple papers reporting the same
            # compound at the same Tc are distinct evidence rows
            # (independent measurements, different papers) and must
            # be preserved. Re-extracting the SAME paper on the same
            # compound collapses via the DOI match.
            if not doi:
                log.debug(
                    "  skip %s — paper has no DOI (skipping to avoid dedup gap)",
                    comp,
                )
                continue
            existing_row = s.execute(
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
                existing = existing_row[0]
                changed = False
                new_pair = {
                    "tc_unit": (sp.Tc.unit if sp.Tc else None) or None,
                    "superconductor_family": (
                        sp.Superconductor_Family
                        if sp.Superconductor_Family and sp.Superconductor_Family != "NA"
                        else None
                    ),
                    "mechanism": (
                        sp.Mechanism if sp.Mechanism and sp.Mechanism != "NA" else None
                    ),
                    "pairing_symmetry": (
                        sp.Pairing_Symmetry
                        if sp.Pairing_Symmetry and sp.Pairing_Symmetry != "NA"
                        else None
                    ),
                    "sc_type": sp.Type if sp.Type and sp.Type != "NA" else None,
                    "hc2_value": (r.hc2_value or None) if hasattr(r, "hc2_value") else None,
                    "hc2_unit": (r.hc2_unit or None) if hasattr(r, "hc2_unit") else None,
                    "critical_current_density_value": (
                        (r.critical_current_density_value or None)
                        if hasattr(r, "critical_current_density_value")
                        else None
                    ),
                    "critical_current_density_unit": (
                        (r.critical_current_density_unit or None)
                        if hasattr(r, "critical_current_density_unit")
                        else None
                    ),
                    "crystal_structure": r.crystal_structure if hasattr(r, "crystal_structure") else None,
                    "space_group": r.space_group if hasattr(r, "space_group") else None,
                    "lattice_a_value": (
                        r.lattice_a_value if hasattr(r, "lattice_a_value") else None
                    ),
                    "lattice_a_unit": (
                        r.lattice_a_unit if hasattr(r, "lattice_a_unit") else None
                    ),
                    "lattice_b_value": (
                        r.lattice_b_value if hasattr(r, "lattice_b_value") else None
                    ),
                    "lattice_b_unit": (
                        r.lattice_b_unit if hasattr(r, "lattice_b_unit") else None
                    ),
                    "lattice_c_value": (
                        r.lattice_c_value if hasattr(r, "lattice_c_value") else None
                    ),
                    "lattice_c_unit": (
                        r.lattice_c_unit if hasattr(r, "lattice_c_unit") else None
                    ),
                    "synthesis_method": (
                        r.synthesis_method if hasattr(r, "synthesis_method") else None
                    ),
                    "chemical_precursors": (
                        r.chemical_precursors if hasattr(r, "chemical_precursors") else None
                    ),
                    "synthesis_temperature_value": (
                        r.synthesis_temperature_value
                        if hasattr(r, "synthesis_temperature_value")
                        else None
                    ),
                    "synthesis_temperature_unit": (
                        r.synthesis_temperature_unit
                        if hasattr(r, "synthesis_temperature_unit")
                        else None
                    ),
                    "synthesis_atmosphere": (
                        r.synthesis_atmosphere if hasattr(r, "synthesis_atmosphere") else None
                    ),
                    "substrate": r.substrate if hasattr(r, "substrate") else None,
                    "isotope_exponent_alpha": (
                        r.isotope_exponent_alpha
                        if hasattr(r, "isotope_exponent_alpha")
                        else None
                    ),
                    "tc_under_pressure": (
                        r.tc_under_pressure if hasattr(r, "tc_under_pressure") else None
                    ),
                    "additional_details": (
                        r.additional_details if hasattr(r, "additional_details") else None
                    ),
                    "updated_at": datetime.now(timezone.utc),
                }
                for attr, new_val in new_pair.items():
                    cur = getattr(existing, attr)
                    if cur is None and new_val is not None:
                        setattr(existing, attr, new_val)
                        changed = True
                if changed:
                    s.commit()
                    log.info(
                        "  merged fields for %s (DOI %s) — no new row",
                        comp, doi,
                    )
                else:
                    log.info(
                        "  skip %s DOI=%s — already complete in DB",
                        comp, doi,
                    )
                continue
            m = Material(
                id=uuid.uuid4(),
                chemical_composition=comp,
                tc_value=float(tc) if isinstance(tc, (int, float)) else None,
                tc_unit=sp.Tc.unit,
                superconductor_family=(
                    sp.Superconductor_Family
                    if sp.Superconductor_Family and sp.Superconductor_Family != "NA"
                    else None
                ),
                mechanism=(
                    sp.Mechanism if sp.Mechanism and sp.Mechanism != "NA" else None
                ),
                pairing_symmetry=(
                    sp.Pairing_Symmetry
                    if sp.Pairing_Symmetry and sp.Pairing_Symmetry != "NA"
                    else None
                ),
                sc_type=sp.Type if sp.Type and sp.Type != "NA" else None,
                source="paper_ingest",
                experimental=True,
                doi=doi or None,
            )
            s.add(m)
            stored += 1
            log.info("  + %s Tc=%s", comp, tc)
        s.commit()
    log.info(
        "  extracted=%d stored=%d skipped_hallucination=%d",
        len(records), stored, skipped_hallucination,
    )
    return len(records), stored


# --------------------------------------------------------------------------- #
# CLI                                                                        #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "identifiers", nargs="+",
        help="One or more arXiv IDs (e.g. 10.48550/arXiv.cond-mat/0212371) or DOIs",
    )
    parser.add_argument("--database-url", default="sqlite:///./_dev.db")
    parser.add_argument("--run-id", default=None,
                        help="Optional explicit IngestRun UUID; new one is created per call if omitted")
    args = parser.parse_args(argv)

    engine = create_engine(
        args.database_url,
        connect_args={"check_same_thread": False} if args.database_url.startswith("sqlite") else {},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    with SessionLocal() as s:
        s.add(IngestRun(id=run_id, status="running"))
        s.commit()

    total_extracted = 0
    total_stored = 0
    failures = 0
    t0 = time.time()
    for ident in args.identifiers:
        try:
            extracted, stored = asyncio.run(
                ingest_one(SessionLocal, ident)
            )
            total_extracted += extracted
            total_stored += stored
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest_one(%s) crashed: %s", ident, exc)
            failures += 1
        # Update run counters incrementally.
        with SessionLocal() as s:
            run = s.get(IngestRun, run_id)
            if run is not None:
                run.papers_searched = (run.papers_searched or 0) + 1
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
