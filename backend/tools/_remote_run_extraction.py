"""Re-extract the corpus into the database with the LLM.

Walks the DOI queue exported by ``tools/prepare_reingest.py``. Every paper
is already downloaded and already parsed to markdown, so the only work
left is the model call: the PDF comes from the cache and the markdown
from the markdown cache.

The run is resumable -- a paper that already has a ``papers`` row is
skipped -- and it reports token usage as it goes, because that is what
the run actually costs.

Usage::

    python tools/run_extraction.py [--concurrency 4] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND / "src", BACKEND / ".venv" / "Lib" / "site-packages"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from superconductor_db.db import Base  # noqa: E402
from superconductor_db.db.models.paper import Paper  # noqa: E402
from superconductor_db.ingest.docling_parser import markdown_ready_dois  # noqa: E402
from superconductor_db.ingest.llm_client import LLMClient  # noqa: E402
from superconductor_db.ingest.paper_ingest import ingest_one  # noqa: E402

log = logging.getLogger("run_extraction")

QUEUE = BACKEND / "data" / "ingest_queue.json"
PRICE_IN, PRICE_OUT, PRICE_CACHED = 0.15, 0.60, 0.015


def load_queue() -> list[str]:
    if not QUEUE.exists():
        raise SystemExit(f"queue not found: {QUEUE} (run prepare_reingest.py)")
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    seen: set[str] = set()
    out: list[str] = []
    for item in data:
        doi = str(item.get("doi") or "").strip()
        if not doi or doi.casefold() in seen:
            continue
        seen.add(doi.casefold())
        out.append(doi)
    return out


async def main_async(args: argparse.Namespace) -> int:
    engine = create_engine(args.database_url, future=True)
    # The database starts empty on the server, and a run that queries
    # ``papers`` before the table exists fails with "no such table: papers".
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    queue = load_queue()
    if args.limit:
        queue = queue[: args.limit]

    with SessionLocal() as s:
        done = {d.casefold() for (d,) in s.execute(select(Paper.doi))}
    # Only a paper whose markdown exists can be extracted. The corpus also
    # holds DOIs that were never fetched (paywalled journals, placeholder
    # identifiers): sending those in makes the ingester try to download
    # them, and every attempt burns a download slot and a retry loop.
    ready = markdown_ready_dois(queue)
    todo = [
        d for d in queue
        if d.casefold() not in done and d in ready
    ]
    log.info(
        "queue: %d DOIs, %d already in the database, %d parsed, %d to extract",
        len(queue), len(done), len(ready), len(todo),
    )

    client = LLMClient()
    semaphore = asyncio.Semaphore(args.concurrency)
    stats = {"ok": 0, "failed": 0, "records": 0}
    started = time.time()
    processed = 0

    async def one(doi: str) -> None:
        nonlocal processed
        async with semaphore:
            processed += 1
            try:
                extracted, stored, _downloaded = await ingest_one(
                    SessionLocal,
                    doi,
                    skip_known_papers=True,
                    client=client,
                    serial_prompts=args.serial_prompts,
                )
                stats["ok"] += 1
                stats["records"] += stored
            except Exception as exc:  # noqa: BLE001 - one paper must not stop the run
                stats["failed"] += 1
                log.warning("extract failed for %s: %s", doi, exc)

    # The corpus keeps growing while the extraction runs: the download loop
    # on the desktop appends to the queue, and the server parses the new
    # PDFs. Re-read the queue now and then so those papers are picked up
    # too -- but only once their markdown exists.
    #
    # The loop never ends on its own. It is a daemon: the desktop keeps
    # downloading, so a run that exits when the backlog is empty would
    # leave the server idle until someone noticed.
    pending = list(todo)
    while True:
        if pending:
            batch = pending[: args.batch]
            pending = pending[args.batch :]
            await asyncio.gather(*(one(d) for d in batch))
        await asyncio.sleep(args.reread_pause)
        # The database grows while the run lasts, so the set of known papers
        # has to be re-read. A snapshot taken at startup makes every paper
        # extracted since then look new again, and it is re-queued on every
        # cycle -- the run then spends its life skipping papers it has
        # already done.
        with SessionLocal() as s:
            done = {d.casefold() for (d,) in s.execute(select(Paper.doi))}
        fresh = [
            d for d in load_queue()
            if d.casefold() not in done and d.casefold() not in {p.casefold() for p in pending}
        ]
        ready = markdown_ready_dois(fresh)
        if ready:
            log.info("queue grew: %d new papers, %d already parsed", len(fresh), len(ready))
            pending.extend(d for d in fresh if d in ready)

    elapsed = time.time() - started
    usage = client.usage
    fresh = usage["prompt_tokens"] - usage["cached_tokens"]
    cost = (
        fresh * PRICE_IN
        + usage["cached_tokens"] * PRICE_CACHED
        + usage["completion_tokens"] * PRICE_OUT
    ) / 1e6
    log.info(
        "done: %d ok, %d failed, %d records in %.1f min",
        stats["ok"], stats["failed"], stats["records"], elapsed / 60,
    )
    log.info(
        "usage: %d calls, prompt %d (cached %d), completion %d (reasoning %d), ~$%.2f",
        usage["calls"], usage["prompt_tokens"], usage["cached_tokens"],
        usage["completion_tokens"], usage["reasoning_tokens"], cost,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=f"sqlite:///{BACKEND / '_dev.db'}"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--serial-prompts",
        action="store_true",
        help=(
            "send the dimension prompts of a paper one at a time instead of "
            "fanning them out; slower, but a rate-limited burst loses the "
            "whole paper"
        ),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--batch",
        type=int,
        default=25,
        help="papers per batch; the queue is re-read between batches",
    )
    parser.add_argument(
        "--reread-pause",
        type=float,
        default=60.0,
        help="seconds between queue re-reads",
    )
    args = parser.parse_args()
    # basicConfig() is a no-op once the root logger has handlers, and
    # paper_ingest configures it at import time. Set the handlers directly.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
