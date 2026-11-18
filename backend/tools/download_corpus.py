"""Grow the corpus: search the literature and download the PDFs.

Discovery and downloading only -- no model calls, no parsing. The
conversion is docling's job and the extraction is the extractor's, and
neither should be slowed down by a download loop running alongside.

The PDFs land in the cache under the SHA-256 of their DOI, the same name
the extractor looks for, so a paper downloaded here is picked up without
any further bookkeeping. New DOIs are appended to the queue file that the
extraction walks.

Usage::

    python tools/download_corpus.py [--rounds 0] [--delay 120]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# A scheduled task starts in the system directory, where the .env is not
# found and the cache paths resolve to nonsense. Move to the backend first.
os.chdir(BACKEND)
for _p in (BACKEND / "src", BACKEND / ".venv" / "Lib" / "site-packages"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

from superconductor_db.core.config import get_settings  # noqa: E402
from superconductor_db.ingest.direct_fetch import (  # noqa: E402
    fetch_arxiv_pdf,
    fetch_direct_pdf,
    fetch_iop_pdf,
)
from superconductor_db.ingest.paper_search_loop import (  # noqa: E402
    _is_target_doi,
    _search_round,
)

log = logging.getLogger("download_corpus")

QUEUE = BACKEND / "data" / "ingest_queue.json"


def pdf_path_for(doi: str) -> Path:
    cache = Path(get_settings().pdf_cache_dir)
    return cache / (hashlib.sha256(doi.strip().lower().encode()).hexdigest() + ".pdf")


def load_known() -> set[str]:
    """DOIs already downloaded or already queued."""
    known: set[str] = set()
    if QUEUE.exists():
        try:
            for item in json.loads(QUEUE.read_text(encoding="utf-8")):
                doi = str(item.get("doi") or "").strip()
                if doi:
                    known.add(doi.casefold())
        except (OSError, ValueError):
            pass
    return known


def append_to_queue(entries: list[dict[str, str]]) -> None:
    existing: list[dict[str, str]] = []
    if QUEUE.exists():
        try:
            existing = json.loads(QUEUE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = []
    existing.extend(entries)
    QUEUE.write_text(json.dumps(existing, indent=1), encoding="utf-8")


async def fetch_one(doi: str, target: Path, *, browser: bool = True) -> bytes | None:
    """Get the PDF for ``doi``, or ``None``.

    The arXiv preprint comes first: it is the one route that answers for
    almost every paper from these journals, and the publisher's own PDF
    endpoint is either behind a captcha (IOP) or refuses the request
    outright (Elsevier, APS, Wiley). Trying it first also saves the ten
    seconds a captcha page costs before it gives up.

    When plain HTTP does not deliver, the article page can be opened in
    **ego lite** -- a real browser with the user's own profile, which
    clears the publisher challenges a scripted request does not. It is off
    by default: the ego CLI starts console programs of its own, and each
    one flashes a window on the desktop.
    """
    for fetch in (fetch_arxiv_pdf, fetch_direct_pdf, fetch_iop_pdf):
        try:
            data = await asyncio.to_thread(fetch, doi)
        except Exception as exc:  # noqa: BLE001 - a dead source must not stop the loop
            log.debug("fetch %s failed: %s", fetch.__name__, exc)
            continue
        if data:
            return data
    if not browser:
        return None
    try:
        from superconductor_db.ingest.ego_fetch import fetch_doi_pdf

        if await fetch_doi_pdf(doi, target):
            return target.read_bytes()
    except Exception as exc:  # noqa: BLE001 - ego may not be running
        log.debug("ego fetch failed for %s: %s", doi, exc)
    return None


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    cache = Path(settings.pdf_cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    known = load_known()
    log.info(
        "known DOIs: %d | cache: %s", len(known), cache,
    )

    async with httpx.AsyncClient(
        timeout=60.0, follow_redirects=True, headers={"User-Agent": "scdb-corpus/1.0"}
    ) as client:
        round_index = 0
        while True:
            round_index += 1
            offset = 0 if round_index == 1 else (round_index - 1) * args.per_query
            try:
                dois = await _search_round(
                    client,
                    per_query=args.per_query,
                    start_offset=offset,
                )
            except Exception as exc:  # noqa: BLE001 - a bad round must not stop the loop
                log.warning("search round failed: %s", exc)
                await asyncio.sleep(args.delay)
                continue

            fresh = [
                d for d in dois
                if _is_target_doi(d) and d.casefold() not in known
            ]
            log.info(
                "round %d: %d hits, %d new (offset %d)",
                round_index, len(dois), len(fresh), offset,
            )

            added = 0
            for doi in fresh:
                known.add(doi.casefold())
                target = pdf_path_for(doi)
                if target.exists() and target.stat().st_size > 0:
                    continue
                data = await fetch_one(doi, target, browser=args.browser)
                if not data:
                    log.info("no channel could fetch %s", doi)
                    continue
                target.write_bytes(data)
                append_to_queue([{"doi": doi, "pdf_path": str(target)}])
                added += 1
                log.info("downloaded %s -> %.1f MB", doi, len(data) / 1048576)
            log.info("round %d: %d new PDFs", round_index, added)

            if args.rounds and round_index >= args.rounds:
                log.info("rounds finished")
                return 0
            await asyncio.sleep(args.delay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rounds", type=int, default=0, help="stop after N rounds (0 = forever)"
    )
    parser.add_argument(
        "--delay", type=float, default=120.0, help="seconds between rounds"
    )
    parser.add_argument("--per-query", type=int, default=8)
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "fall back to the ego lite browser for pages that need one. Off "
            "by default: the ego CLI starts node processes of its own and "
            "each one flashes a console window on the desktop."
        ),
    )
    parser.add_argument(
        "--log", default=str(BACKEND / "data/cache/download_corpus.log"),
        help="log file; the scheduled task has no console to write to",
    )
    args = parser.parse_args()
    # A scheduled task runs without a console, so the log goes to a file.
    # basicConfig() is not enough: paper_search_loop configures the root
    # logger when it is imported, and a second basicConfig() call is then a
    # no-op -- the file handler would never be attached. Replace the root
    # handlers outright.
    handlers: list[logging.Handler] = [logging.FileHandler(args.log, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler())
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
