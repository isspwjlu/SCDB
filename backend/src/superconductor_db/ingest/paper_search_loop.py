"""Continuous paper-discovery loop.

Unlike ``curated_loop`` (which cycles a fixed list of 52 arXiv IDs
until dedup'd out), this script queries the arXiv public Atom API for
*new* SC preprints each round and feeds them through the same
``paper_ingest.ingest_one`` path. Because dedup keys on
``(compound, doi)``, two papers reporting the same compound at
different Tcs produce two rows — so the DB grows continuously.

Search strategy:
  Each round we query arXiv with 5 highly-recall SC queries sorted
  by ``submittedDate`` descending. We slide the ``start`` cursor so
  the same papers don't re-appear, and we dedupe across rounds via
  an in-memory set of DOIs seen this process lifetime.

Run alongside the curated loop:
  cd backend
  python -m superconductor_db.ingest.paper_search_loop --delay 120
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

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.ingest.paper_ingest import ingest_one

log = logging.getLogger("paper_search_loop")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# SC-paper search queries — high-recall, biased toward the kinds of
# compounds our whitelist recognises.
SC_SEARCH_QUERIES: tuple[str, ...] = (
    'cat:cond-mat.supr-con AND abs:(superconduct* OR Tc OR "critical temperature")',
    'all:"superconductor" AND all:"critical temperature" AND cat:cond-mat.supr-con',
    'all:"superconductor" AND (all:cuprate OR all:"iron pnictide" OR all:"MgB2")',
    'all:"superconducting" AND (all:Hg OR all:YBCO OR all:NdNiO OR all:hydride)',
    'cat:cond-mat.supr-con AND (all:La OR all:SmFeAsO OR all:Bi2212 OR all:Hg1223)',
    # nickelate family — RP bilayer/trilayer / infinite-layer / Pr-substituted
    'all:(nickelate OR NiO2 OR "La2NiO4" OR "La3Ni2O7" OR "La4Ni3O10" OR SrNiO2 OR nickelate)',
    # more families
    'all:superconduct* AND (all:Ruddlesden-Popper OR all:pyrochlore OR all:spin-triplet)',
    # strontium ruthenate + pnictide-specific
    'cat:cond-mat.supr-con AND (all:heavy.fermion OR all:1222 OR all:Kondo)',
)


def _parse_atom_dois(body: str) -> list[str]:
    """Parse arXiv Atom XML and return ``10.48550/arXiv.<id>`` for each entry."""
    out: list[str] = []
    in_entry = False
    entry_id: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("<entry>"):
            in_entry = True
            entry_id = None
            continue
        if line.startswith("</entry>"):
            if entry_id and "/abs/" in entry_id:
                arxiv_id = entry_id.rsplit("/abs/", 1)[-1]
                if arxiv_id:
                    out.append(f"10.48550/arXiv.{arxiv_id}")
            in_entry = False
            entry_id = None
            continue
        if in_entry and line.startswith("<id>") and line.endswith("</id>"):
            entry_id = line[4:-5]
    return out


async def _search_arxiv(
    client: httpx.AsyncClient,
    query: str,
    *,
    start: int,
    max_results: int,
) -> list[str]:
    try:
        resp = await client.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": query,
                "start": start,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            timeout=30,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("arxiv query failed (%s): %s", query, exc)
        return []
    if resp.status_code != 200:
        log.warning("arxiv returned %s for query %r", resp.status_code, query)
        return []
    return _parse_atom_dois(resp.text)


async def _search_round(
    client: httpx.AsyncClient,
    *,
    per_query: int,
    start_offset: int,
) -> list[str]:
    """One discovery round: a few SC queries, return union of DOIs."""
    all_dois: list[str] = []
    for q in SC_SEARCH_QUERIES:
        for offset in (start_offset, start_offset + per_query):
            ids = await _search_arxiv(client, q, start=offset, max_results=per_query)
            all_dois.extend(ids)
    seen: set[str] = set()
    deduped: list[str] = []
    for d in all_dois:
        if d not in seen:
            seen.add(d)
            deduped.append(d)
    return deduped


async def _async_main(args: argparse.Namespace) -> int:
    engine = create_engine(
        args.database_url,
        connect_args={"check_same_thread": False} if args.database_url.startswith("sqlite") else {},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    log.info(
        "paper_search_loop starting: per-query=%d max/round=%d delay=%ds",
        args.per_query, args.max_per_round, int(args.delay),
    )
    log.info("database: %s", args.database_url)

    seen_dois: set[str] = set()
    round_idx = 0
    offset = args.start_offset

    async with httpx.AsyncClient() as client:
        while True:
            round_idx += 1
            run_id = uuid.uuid4()
            with SessionLocal() as s:
                s.add(IngestRun(id=run_id, status="running"))
                s.commit()

            try:
                dois = await _search_round(
                    client,
                    per_query=args.per_query,
                    start_offset=offset,
                )
                log.info(
                    "round %d: arXiv returned %d DOIs (offset=%d)",
                    round_idx, len(dois), offset,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("round %d: search crashed: %s", round_idx, exc)
                dois = []

            new_dois = [d for d in dois if d not in seen_dois]
            for d in new_dois:
                seen_dois.add(d)

            ingest_targets = new_dois[: args.max_per_round]

            total_extracted = 0
            total_stored = 0
            for ident in ingest_targets:
                try:
                    # ``ingest_one`` is ``async def``; await directly.
                    extracted, stored = await ingest_one(SessionLocal, ident)
                    total_extracted += extracted
                    total_stored += stored
                except Exception as exc:  # noqa: BLE001
                    log.warning("ingest %s failed: %s", ident, exc)

            with SessionLocal() as s:
                run = s.get(IngestRun, run_id)
                if run is not None:
                    run.papers_searched = len(dois)
                    run.records_extracted = (run.records_extracted or 0) + total_extracted
                    run.records_stored = (run.records_stored or 0) + total_stored
                    run.status = "completed"
                    run.finished_at = datetime.now(timezone.utc)
                    s.commit()

            log.info(
                "round %d done: search=%d new=%d ingested=%d extracted=%d stored=%d",
                round_idx, len(dois), len(new_dois),
                len(ingest_targets), total_extracted, total_stored,
            )

            if args.max_rounds and round_idx >= args.max_rounds:
                break

            offset += args.per_query
            time.sleep(args.delay)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default="sqlite:///./_dev.db")
    parser.add_argument("--per-query", type=int, default=8,
                        help="arXiv results per (query, offset) pair (default 8)")
    parser.add_argument("--max-per-round", type=int, default=8,
                        help="Cap papers ingested per round (default 8)")
    parser.add_argument("--delay", type=float, default=120.0,
                        help="Seconds between rounds (default 120)")
    parser.add_argument("--start-offset", type=int, default=0,
                        help="Initial arXiv start offset")
    parser.add_argument("--max-rounds", type=int, default=0,
                        help="Stop after N rounds (0 = unbounded)")
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
