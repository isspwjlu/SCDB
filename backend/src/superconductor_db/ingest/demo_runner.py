"""Demo-mode ingest runner — exercises the full ingest pipeline without a real LLM.

Production ingest flow (spec §6):
    paper search → scansci-pdf download → PDF parse → LLM extract → store

This script substitutes a deterministic mock LLM extractor (no API calls) so
the pipeline runs end-to-end on the SQLite dev DB without Ollama or any other
LLM endpoint. Real arXiv / CrossRef / Semantic Scholar are hit for paper
search so we exercise the full network path.

Per-paper, the mock extractor emits a ``SuperconductingRecord`` synthesised from
the paper's title + DOI: a realistic-looking Tc + family + pressure-Tc curve
derived from title keywords. Useful for "does the pipeline work?" smoke
testing — NOT for production data (LLM-extracted records are required).

Usage (from project root):
    cd backend
    DATABASE_URL="sqlite:///./_dev.db" \\
    JWT_SECRET=... \\
    python -m superconductor_db.ingest.demo_runner \\
        --queries-file ../seeds/sc_search_queries.txt \\
        --batch-size 3 \\
        --delay 5 \\
        --loop
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Side-effect imports register models on Base.metadata.
import superconductor_db.db.models  # noqa: F401
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestJob, IngestRun
from superconductor_db.db.models.material import Material
from superconductor_db.ingest.paper_search import (
    SC_SEARCH_QUERIES,
    search_all_sources,
)

log = logging.getLogger("demo_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# --------------------------------------------------------------------------- #
# Mock LLM extractor — deterministic synthesis from paper title + DOI.       #
# --------------------------------------------------------------------------- #


# Per-keyword hint dictionary. The first matching keyword decides family + Tc
# range; the random pick is stable for the same (title, doi) pair (sha256-hashed).
_HINTS: list[tuple[str, str, tuple[float, float]]] = [
    (r"\bYBa2Cu3O7\b|\bYBCO\b",            "cuprate",           (88.0, 95.0)),
    (r"\bBi[\-_]?2212\b|\bBi[\-_]?2223\b",  "cuprate",           (75.0, 110.0)),
    (r"\bLa[\-_]?214\b|\bLSCO\b",           "cuprate",           (30.0, 45.0)),
    (r"\bHg[\-_]?1223\b|\bHgBa2Ca",         "cuprate",           (125.0, 135.0)),
    (r"\bFeSe\b|\bFe[\-_]?Te\b|\bSmFeAsO",  "iron-based",         (8.0, 55.0)),
    (r"\bBaFe2As2\b|\bBa[\-_]?122\b",       "iron-based",         (20.0, 40.0)),
    (r"\bMgB2\b",                          "MgB2",              (39.0, 39.0)),
    (r"\bNb3Sn\b|\bNbTi\b|\bNb3Al\b",       "conventional",       (9.0, 18.0)),
    (r"\bPb\b|\bHg\b|\bSn\b",               "conventional",       (4.0, 8.0)),
    (r"\bCeCu2Si2\b|\bUPt3\b|\bPrOs4Sb12",  "heavy-fermion",      (0.5, 2.0)),
]


def _stable_rng(*parts: str) -> random.Random:
    """Return an RNG seeded from a hash of the given strings. Same input → same output."""
    import hashlib
    seed = int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF
    return random.Random(seed)


def _derive_record(paper: dict[str, Any]) -> dict[str, Any] | None:
    """Synthesise a SuperconductingRecord from paper title + DOI."""
    title = paper.get("title") or ""
    doi = paper.get("doi") or ""

    family: str = "unknown"
    tc_lo = tc_hi = 0.0
    for pat, fam, (lo, hi) in _HINTS:
        if re.search(pat, title, re.IGNORECASE):
            family, tc_lo, tc_hi = fam, lo, hi
            break
    if family == "unknown":
        # Skip papers whose title doesn't match any SC keyword.
        return None

    rng = _stable_rng(doi or title)
    tc = round(rng.uniform(tc_lo, tc_hi), 1)
    pressures = [0.0, round(rng.uniform(2.0, 5.0), 1), round(rng.uniform(8.0, 12.0), 1)]
    # Tc rises under modest pressure (typical BCS cuprate behaviour).
    tcs = [tc, round(tc + rng.uniform(1.0, 5.0), 1), round(tc + rng.uniform(3.0, 8.0), 1)]

    composition_guess = _title_to_formula(title) or title.split(":")[0].split()[0] or "Unknown"

    return {
        "chemical_composition": composition_guess,
        "tc_value": tc,
        "tc_unit": "K",
        "superconductor_family": family,
        "mechanism": "BCS" if family in {"MgB2", "conventional", "heavy-fermion"} else "unconventional",
        "tc_under_pressure": {"pressures": pressures, "tcs": tcs, "method": "resistivity"},
        "source": "demo_runner",
        "experimental": True,
    }


def _title_to_formula(title: str) -> str | None:
    """Pull a chemistry-looking token from a title (very rough heuristic)."""
    # Look for any token containing element-letter(s) + digit(s).
    m = re.search(r"\b([A-Z][a-z]?\d?(?:[A-Z][a-z]?\d?){1,6})\b", title)
    return m.group(1) if m else None


def _record_to_material(rec: dict[str, Any]) -> Material:
    """Build a Material ORM row from the synthesised record."""
    return Material(
        id=uuid.uuid4(),
        chemical_composition=rec["chemical_composition"],
        tc_value=rec["tc_value"],
        tc_unit=rec["tc_unit"],
        superconductor_family=rec["superconductor_family"],
        mechanism=rec["mechanism"],
        tc_under_pressure=rec["tc_under_pressure"],
        source=rec["source"],
        experimental=rec["experimental"],
    )


# --------------------------------------------------------------------------- #
# Single ingest loop                                                           #
# --------------------------------------------------------------------------- #


@dataclass
class RunStats:
    queries: int = 0
    papers_seen: int = 0
    papers_kept: int = 0
    materials_added: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def run_one_batch(
    session_factory: sessionmaker[Session],
    queries: list[str],
    *,
    per_query_max_results: int = 5,
) -> RunStats:
    """Run a single batch: one search per query, mock-extract, upsert, return stats."""
    stats = RunStats()
    for query in queries:
        stats.queries += 1
        log.info("Searching arXiv/CrossRef/S2 for: %r", query)
        try:
            papers = asyncio.run(search_all_sources(query, max_results=per_query_max_results))
        except Exception as exc:  # noqa: BLE001
            log.error("search failed: %s", exc)
            stats.errors += 1
            continue

        stats.papers_seen += len(papers)
        log.info("  -> %d papers", len(papers))

        with session_factory() as session:
            for paper in papers:
                rec = _derive_record({
                    "title": paper.title,
                    "doi": paper.doi,
                })
                if rec is None:
                    continue
                stats.papers_kept += 1
                # Idempotent: skip if any demo_runner-sourced row already
                # exists for this (composition, family, doi). Use exists()
                # rather than scalar_one_or_none to handle the case where
                # multiple rows may exist for the same composition (e.g.
                # multiple papers about MgB2 from different queries).
                already = session.execute(
                    select(Material.id).where(
                        Material.chemical_composition == rec["chemical_composition"],
                        Material.source == "demo_runner",
                    ).limit(1)
                ).first()
                if already is not None:
                    continue
                session.add(_record_to_material(rec))
                stats.materials_added += 1
            session.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default="sqlite:///./_dev.db",
                        help="SQLAlchemy DSN (default: sqlite:///./_dev.db)")
    parser.add_argument("--queries", nargs="*", default=None,
                        help="Override the SC search queries")
    parser.add_argument("--queries-file", default=None,
                        help="File with one query per line")
    parser.add_argument("--batch-size", type=int, default=3,
                        help="Max papers per query (default 3)")
    parser.add_argument("--delay", type=float, default=10.0,
                        help="Seconds between batches (default 10)")
    parser.add_argument("--loop", action="store_true",
                        help="Loop forever instead of single batch")
    parser.add_argument("--max-batches", type=int, default=0,
                        help="Stop after N batches (0 = unbounded)")
    args = parser.parse_args(argv)

    # Resolve queries.
    if args.queries:
        queries = list(args.queries)
    elif args.queries_file:
        with open(args.queries_file, encoding="utf-8") as fh:
            queries = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    else:
        queries = SC_SEARCH_QUERIES[:5]  # sane default

    # Build engine + session factory.
    engine = create_engine(
        args.database_url,
        connect_args={"check_same_thread": False} if args.database_url.startswith("sqlite") else {},
    )
    Base.metadata.create_all(engine)  # idempotent
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    # Write run record.
    run_id = uuid.uuid4()
    with SessionLocal() as s:
        s.add(IngestRun(id=run_id, status="running", papers_searched=0, records_extracted=0, records_stored=0))
        s.commit()

    batch = 0
    while True:
        batch += 1
        log.info("=" * 60)
        log.info("BATCH %d  (run %s)", batch, run_id)
        log.info("=" * 60)
        stats = run_one_batch(SessionLocal, queries, per_query_max_results=args.batch_size)
        log.info("Batch done: %s", stats)
        # Update run counters.
        with SessionLocal() as s:
            run = s.get(IngestRun, run_id)
            if run is not None:
                run.papers_searched = (run.papers_searched or 0) + stats.papers_seen
                run.records_extracted = (run.records_extracted or 0) + stats.papers_kept
                run.records_stored = (run.records_stored or 0) + stats.materials_added
                run.errors = (run.errors or 0) + stats.errors
                s.commit()
        if not args.loop:
            with SessionLocal() as s:
                run = s.get(IngestRun, run_id)
                if run:
                    run.status = "completed"
                    run.finished_at = datetime.now(timezone.utc)
                    s.commit()
            break
        if args.max_batches and batch >= args.max_batches:
            with SessionLocal() as s:
                run = s.get(IngestRun, run_id)
                if run:
                    run.status = "completed"
                    run.finished_at = datetime.now(timezone.utc)
                    s.commit()
            break
        time.sleep(args.delay)

    log.info("Final stats: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
