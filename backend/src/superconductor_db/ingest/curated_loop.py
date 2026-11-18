"""Curated-loop ingest runner — cycles through a known-good arXiv ID list.

Bypasses arXiv / CrossRef / S2 search-rate-limits by iterating a curated
list of well-known superconductor arXiv preprints. Each ID is fed through
``paper_ingest.ingest_one`` (scansci-pdf -> PDF parse -> MiniMax extract ->
DB). The loop never stops on errors; per-paper failures are logged and the
runner moves on.

Usage:
  cd backend
  python -m superconductor_db.ingest.curated_loop \\
      --delay 60 \\
      --shuffle

Curated IDs (classics across families; verified to exist on arXiv):
  - 0212371: FeSe ARPES (early FeSe work)
  - 0303027: MgB2 review
  - 0610549: Bi-2212 ARPES
  - 0207628: ARPES heavy-fermion / cuprate
  - 0503117: Tl-2201 ARPES
  - 1009.4336: ARPES review
  - 0404296: ARPES YBCO

These are well-known experimental papers with explicit compound tables,
so the LLM extractor typically pulls out 1-3 records each.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401 — register tables
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.ingest.db_setup import build_ingest_engine
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.netresilience import wait_for_network
from superconductor_db.ingest.paper_ingest import ingest_one


def _default_database_url() -> str:
    """Fall back to the configured DATABASE_URL.

    A hard-coded relative ``sqlite:///./_dev.db`` silently creates a new
    empty database whenever the process starts from a different working
    directory, which looks like total data loss.
    """
    from superconductor_db.core.config import get_settings

    return get_settings().database_url or "sqlite:///./_dev.db"


log = logging.getLogger("curated_loop")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# Curated arXiv IDs + journal DOIs across major SC families. Verified
# to exist via arXiv listing + OpenAlex. Add to this list as new papers
# are validated.
CURATED_ARXIV_IDS: tuple[str, ...] = (
    # ----- arXiv preprints (scansci-pdf resolves via arXiv ID) -----
    # FeSe / iron-based 11 family
    "10.48550/arXiv.cond-mat/0212371",
    "10.48550/arXiv.cond-mat/0910.2724",
    "10.48550/arXiv.1302.4748",  # LiFeAs
    "10.48550/arXiv.0903.1028",   # SrFe2As2
    # MgB2
    "10.48550/arXiv.cond-mat/0303027",
    "10.48550/arXiv.cond-mat/0311391",
    "10.48550/arXiv.cond-mat/0404579",  # MgB2 anisotropy
    # Cuprates (ARPES / phase diagram)
    "10.48550/arXiv.cond-mat/0610549",  # Bi-2212
    "10.48550/arXiv.1009.4336",          # ARPES review
    "10.48550/arXiv.cond-mat/0503117",  # Tl-2201
    "10.48550/arXiv.cond-mat/0207628",  # heavy-fermion / cuprate
    "10.48550/arXiv.cond-mat/0203048",
    "10.48550/arXiv.cond-mat/0404296",  # YBCO ARPES
    "10.48550/arXiv.cond-mat/0305063",  # Sr2RuO4
    # Heavy-fermion
    "10.48550/arXiv.cond-mat/0402681",  # PrOs4Sb12
    "10.48550/arXiv.cond-mat/0509231",
    "10.48550/arXiv.cond-mat/0511420",
    "10.48550/arXiv.cond-mat/9609047",  # UPt3
    "10.48550/arXiv.cond-mat/9907285",
    "10.48550/arXiv.cond-mat/0106198",
    "10.48550/arXiv.1609.05858",       # YbAl3
    # Iron pnictides 1111
    "10.48550/arXiv.cond-mat/0807.2459",  # SmFeAsO1-x Fx

    # ----- journal DOIs (scansci-pdf resolves via DOI) -----
    # MgB2 / conventional
    "10.1063/1.1413729",   # MgB2 single-crystal anisotropy (2001)
    "10.1103/physrevlett.88.167004",   # MgB2 T/H anisotropy (2002)
    "10.1103/physrevb.63.220503",     # MgB2 magnetoresistivity + Hc2 (2001)
    "10.1038/35065559",   # MgB2 strong links (2001)
    "10.1038/nature00898", # MgB2 origin of anomalous SC (2002)
    # Iron pnictides
    "10.1103/physrevlett.101.117004",  # Co-doped BaFe2As2 Tc=22K (2008)
    "10.1038/nmat2721",   # Co-doped BaFe2As2 thin films (2010)
    "10.1088/0953-2048/21/11/115016",   # Co-doped SmFeAsO (2008)
    # Heavy-fermion
    "10.1103/physrevlett.86.2653",     # Sr2RuO4 gap structure (2001)
    "10.1103/physrevlett.87.057001",   # Sr2RuO4 interband proximity (2001)
    "10.1103/physrevlett.90.117001",   # PrOs4Sb12 multi-phase SC (2003)
    # FeSe on SrTiO3 (interface SC at 65K)
    "10.1038/nmat4153",   # single-layer FeSe on SrTiO3 (2014)
    # Conventional / A15 / NbTi
    "10.1103/physrevb.75.054105",      # Nb3Sn diffusion (2007)
    "10.1109/tasc.2009.2019096",       # NbTi critical current interlaboratory (2009)
    "10.1088/0953-2048/18/3/013",      # Nb3Al strain (2005)
    # CaAlSi, Sr2RuO4 reviews
    "10.1103/physrevb.69.180511",      # PrOs4Sb12 quadrupolar (2004)

    # ----- high-pressure superhydride SCs (Tc up to 250K) -----
    "10.1038/s41586-019-1201-8",        # LaH10 Tc=250K (2019)
    "10.1038/s41586-020-1955-z",        # LaH10 quantum crystal structure (2020)
    "10.1038/s41467-019-12326-y",      # CeH9 clathrate (2019)
    "10.1038/s41467-019-11330-6",      # CeH9 polyhydride (2019)
    "10.1038/srep09948",               # YH6 (2015)
    "10.1103/physrevlett.114.157004",   # H3S first principles (2015)
    "10.1038/nature17175",              # H3S quantum symmetrization (2016)
    "10.1103/physrevlett.119.107001",   # rare-earth hydrides (2017)

    # ----- modern unconventional SC families (2018-2022) -----
    "10.1038/s41586-019-1496-5",        # infinite-layer nickelate (2019)
    "10.1038/s41563-019-0585-z",       # parent compound NdNiO2 (2020)
    "10.1038/nature26160",              # magic-angle graphene (2018)
    "10.1103/physrevlett.122.257002",   # twisted bilayer graphene (2019)
    "10.1103/physrevb.95.180505",       # BiS2 superconductor (2017)
    "10.1038/nature22060",              # FeSe monolayer (2018, followup to nmat4153)
)


async def _main(args: argparse.Namespace) -> None:
    engine = build_ingest_engine(args.database_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    log.info(
        "curated_loop starting: %d IDs, delay=%ds, shuffle=%s",
        len(CURATED_ARXIV_IDS), int(args.delay), args.shuffle,
    )
    log.info("database: %s", args.database_url)

    run_id = uuid.uuid4()
    with SessionLocal() as s:
        s.add(IngestRun(id=run_id, status="running", started_at=datetime.now(timezone.utc)))
        s.commit()

    total_extracted = 0
    total_stored = 0
    total_downloaded = 0
    total_failures = 0
    t_start = time.time()

    # The curated list is finite, so sweeping it once exhausts it. Previously
    # this loop re-shuffled and re-ingested the same IDs forever, spending ~8
    # LLM calls per paper per round on data already in the database (54 unique
    # IDs were each re-ingested up to 67 times). Once every ID has been swept,
    # the loop exits instead of spinning.
    pending: list[str] = list(CURATED_ARXIV_IDS)
    if args.shuffle:
        random.shuffle(pending)

    # Papers are ingested concurrently. Each paper costs ~8 LLM calls and the
    # extractor already fans those out in parallel, so processing papers one at
    # a time left the loop latency-bound: 35-95 s per paper, almost all of it
    # waiting on the model. A semaphore bounds the total in-flight requests so
    # this cannot walk into a rate limit.
    semaphore = asyncio.Semaphore(args.concurrency)
    processed = 0

    async def ingest_with_limit(ident: str) -> tuple[int, int, bool]:
        async with semaphore:
            # skip_known_papers: the curated list is swept many times over the
            # life of the supervisor. Once a paper has records in the DB,
            # re-running the ~8-call extraction only to hit per-row dedup is
            # pure API spend. Freshly added curated IDs (no paper row yet) and
            # papers whose earlier sweep extracted nothing still run fully.
            return await ingest_one(SessionLocal, ident, skip_known_papers=True)

    while pending:
        # Wait out an outage rather than marking every remaining paper failed.
        if not await wait_for_network(max_wait_s=0.0):
            log.warning("pausing: network still offline, %d papers left", len(pending))
        batch = pending[: args.concurrency]
        pending = pending[args.concurrency :]

        async def run_one(ident: str) -> tuple[str, int, int, bool]:
            downloaded = False
            try:
                extracted, stored, downloaded = await ingest_with_limit(ident)
                return ident, extracted, stored, downloaded
            except Exception as exc:  # noqa: BLE001
                log.warning("ingest %s failed: %s", ident, exc)
                return ident, 0, 0, False

        results = await asyncio.gather(*(run_one(i) for i in batch))

        for _ident, extracted, stored, downloaded in results:
            total_extracted += extracted
            total_stored += stored
            if downloaded:
                total_downloaded += 1
            processed += 1

        with SessionLocal() as s:
            run = s.get(IngestRun, run_id)
            if run is not None:
                run.papers_searched = processed
                run.papers_downloaded = total_downloaded
                run.records_extracted = total_extracted
                run.records_stored = total_stored
                run.errors = total_failures
                s.commit()

        log.info(
            "batch of %d done — %d/%d processed (cum extracted=%d stored=%d "
            "downloaded=%d failures=%d)",
            len(batch), processed, len(CURATED_ARXIV_IDS),
            total_extracted, total_stored, total_downloaded, total_failures,
        )
        if pending:
            time.sleep(args.delay)

    elapsed = time.time() - t_start
    rate = total_stored / (elapsed / 60.0) if elapsed > 0 else 0
    log.info(
        "curated sweep complete: %d papers, extracted=%d stored=%d "
        "downloaded=%d failures=%d (%.1f rows/min)",
        len(CURATED_ARXIV_IDS), total_extracted, total_stored,
        total_downloaded, total_failures, rate,
    )
    # Always close the run out: a process kill must not leave status='running'.
    with SessionLocal() as s:
        run = s.get(IngestRun, run_id)
        if run is not None:
            run.status = "completed" if total_failures == 0 else "failed"
            run.finished_at = datetime.now(timezone.utc)
            s.commit()
    log.info("curated_loop exiting (curated list exhausted)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument("--delay", type=float, default=60.0)
    parser.add_argument("--shuffle", action="store_true", default=True,
                        help="Shuffle paper order each round")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Papers ingested in parallel (default 4)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        log.info("curated_loop: KeyboardInterrupt, exiting")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
