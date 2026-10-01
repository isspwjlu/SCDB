"""Real-LLM ingest runner — production-style search → scansci-pdf → MiniMax extract.

Unlike ``demo_runner`` (which synthesises SC records from paper titles),
this runner exercises the full production pipeline:

  paper search → scansci-pdf download (subprocess) → PyMuPDF parse →
  LLMClient (MiniMax) → SuperconductingExtractor → repository.upsert_by_doi

Uses the same SC search query list as ``paper_search.SC_SEARCH_QUERIES``,
plus a configurable batch size and per-query rate limit (default 2 to stay
under arXiv / CrossRef / S2 anonymous rate limits).

Usage:
  cd backend
  DATABASE_URL="sqlite:///./_dev.db" LLM_BASE_URL=https://api.minimaxi.com/v1 \\
  python -m superconductor_db.ingest.real_runner \\
      --batch-size 2 --delay 15 --loop

Each batch runs one ingest run (creates IngestRun + IngestJobs, then runs
``run_job`` directly). Records persist in the materials table.
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

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Side-effect imports register tables on Base.metadata.
import superconductor_db.db.models  # noqa: F401
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.paper_search import SC_SEARCH_QUERIES
from superconductor_db.ingest.runner import JobResult, run_job

log = logging.getLogger("real_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


async def _run_batch(
    session_factory: sessionmaker[Session],
    queries: list[str],
    per_query_max: int,
) -> JobResult:
    """Run a single batch: searches each query, processes papers via the
    full production pipeline (LLM + scansci-pdf + repository).
    """
    # Build dependencies per batch (cheap, isolated).
    client = LLMClient()

    # One ingest run per batch.
    run_id = uuid.uuid4()
    with session_factory() as s:
        s.add(IngestRun(id=run_id, status="running"))
        s.commit()

    total = JobResult()
    for query in queries:
        log.info("[run %s] searching: %r", run_id, query)
        try:
            # run_job takes a Session (keyword ``db``), not db_session.
            with session_factory() as s:
                result = await run_job(
                    query=query,
                    max_results=per_query_max,
                    db=s,
                    client=client,
                    run_id=run_id,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("run_job crashed for %r: %s", query, exc)
            total.errors += 1
            continue
        total.merge(result)
        log.info(
            "  -> papers=%d downloaded=%d extracted=%d stored=%d errors=%d",
            result.papers_searched,
            result.papers_downloaded,
            result.records_extracted,
            result.records_stored,
            result.errors,
        )

    # Update run row with batch totals.
    with session_factory() as s:
        run = s.get(IngestRun, run_id)
        if run is not None:
            run.papers_searched = (run.papers_searched or 0) + total.papers_searched
            run.records_extracted = (run.records_extracted or 0) + total.records_extracted
            run.records_stored = (run.records_stored or 0) + total.records_stored
            run.errors = (run.errors or 0) + total.errors
            run.status = "completed" if total.errors == 0 else "failed"
            run.finished_at = datetime.now(timezone.utc)
            s.commit()
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default="sqlite:///./_dev.db")
    parser.add_argument("--queries", nargs="*", default=None)
    parser.add_argument("--queries-file", default=None)
    parser.add_argument("--batch-size", type=int, default=2,
                        help="Max papers per query (default 2 — keeps below rate limits)")
    parser.add_argument("--delay", type=float, default=15.0,
                        help="Seconds between batches (default 15)")
    parser.add_argument("--loop", action="store_true",
                        help="Loop forever")
    parser.add_argument("--max-batches", type=int, default=0)
    args = parser.parse_args(argv)

    if args.queries:
        queries = list(args.queries)
    elif args.queries_file:
        with open(args.queries_file, encoding="utf-8") as fh:
            queries = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    else:
        queries = SC_SEARCH_QUERIES[:5]

    engine = create_engine(
        args.database_url,
        connect_args={"check_same_thread": False} if args.database_url.startswith("sqlite") else {},
    )
    Base.metadata.create_all(engine)  # idempotent
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    log.info("=" * 60)
    log.info("real_runner starting (queries=%d, batch-size=%d)", len(queries), args.batch_size)
    log.info("database: %s", args.database_url)
    log.info("=" * 60)

    batch = 0
    while True:
        batch += 1
        log.info("BATCH %d", batch)
        try:
            total = asyncio.run(
                _run_batch(SessionLocal, queries, args.batch_size)
            )
            log.info("Batch %d done: papers=%d extracted=%d stored=%d errors=%d",
                     batch, total.papers_searched, total.records_extracted,
                     total.records_stored, total.errors)
        except Exception as exc:  # noqa: BLE001
            log.exception("Batch %d crashed: %s", batch, exc)
        if not args.loop:
            break
        if args.max_batches and batch >= args.max_batches:
            break
        log.info("Sleeping %.0fs before next batch...", args.delay)
        time.sleep(args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
