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
from typing import Any

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
    fetch_aps_pdf,
    fetch_arxiv_pdf,
    fetch_direct_pdf,
    fetch_elsevier_pdf,
    fetch_frontiers_pdf,
    fetch_iop_pdf,
    fetch_oa_pdf,
)
from superconductor_db.ingest.paper_search_loop import (  # noqa: E402
    _is_target_doi,
    _search_round,
)
from superconductor_db.ingest.wos_search import WOS_QUERIES, search_wos  # noqa: E402

log = logging.getLogger("download_corpus")

QUEUE = BACKEND / "data" / "ingest_queue.json"

# The publishers whose papers this loop can actually fetch, plus arXiv.
#
# The search layer deliberately filters preprints out: it exists to cover
# the journal literature, and OpenAlex indexes repository deposits nobody
# peer-reviewed. But a preprint is the one thing that downloads reliably
# -- arXiv serves the file itself, while Elsevier answers a bot check,
# Wiley's PDF endpoint answers 403 to a plain client, and IOP sits behind a
# captcha. A preprint of a paper that later appears in a journal is
# covered by the journal DOI, which the same search also returns.
#
# The list is not a statement about which publishers exist: since the
# preprint lookup goes through OpenAlex, a paper from any of these journals
# is fetchable through its arXiv version even when the publisher's own
# endpoint refuses. Applied Physics Letters and Science were missing here
# and their papers were being dropped before they were ever tried.
_FETCHABLE_PREFIXES: tuple[str, ...] = (
    "10.48550",   # arXiv
    "10.1103",    # APS: Physical Review
    "10.1109",    # APS: Physical Review (the prefix since 2024)
    "10.1038",    # Springer Nature
    "10.1007",    # Springer
    "10.1088",    # IOP
    "10.1002",    # Wiley
    "10.1016",    # Elsevier
    "10.1134",    # Elsevier: Physics Letters A
    "10.1063",    # AIP: Applied Physics Letters
    "10.1126",    # AAAS: Science
    "10.7566",    # J. Phys. Soc. Jpn.
    "10.1143",    # J. Phys. Soc. Jpn. (the old prefix)
    "10.1039",    # Nature Physics
    "10.1021",    # ACS
    "10.1080",    # ACS: JACS
    "10.3390",    # MDPI
    "10.3389",    # Frontiers
)

# Preprint servers whose full text sits behind an upload wall. A doomed
# attempt costs a download slot and a timeout, and none of these ever
# arrive.
_SKIP_PREFIXES: tuple[str, ...] = (
    "10.21203/",   # Research Square
    "10.2139/",    # SSRN
    "10.31234/",   # Research Square
    "10.20944/",   # Preprints.org
    "10.5194/",    # EGUsphere: conference abstracts, not papers
)

# DOIs that must not come back.
#
# The first eight are rejected by MiniMax's input content filter with HTTP
# 422, so the extraction retries them for ever and never stores a row; the
# ninth is a PDF docling cannot parse at all. Dropping them from the queue
# is not enough -- the search finds them again within a round or two and
# puts them back, so they are blocked here as well.
_BLOCKED_DOIS: frozenset[str] = frozenset(
    doi.casefold()
    for doi in (
        "10.21926/jept.2504014",
        "10.48550/arXiv.1101.3704v1",
        "10.48550/arXiv.1303.3528v1",
        "10.48550/arXiv.1607.00441v1",
        "10.48550/arXiv.2101.10538v1",
        "10.48550/arXiv.2506.18117v1",
        "10.48550/arXiv.cond-mat/0602134v2",
        "10.48550/arXiv.physics/0605009v1",
        "10.48550/arXiv.1810.08981v2",
        # The placeholders the old corpus carried for papers it never had.
        "NIMS-SuperCon-006",
        "NIMS-SuperCon-007",
        "NIMS-SuperCon-008",
        "NIMS-SuperCon-010",
        # Records no channel can serve: the publisher's PDF endpoint refuses,
        # there is no preprint, and the two preprint servers sit behind an
        # upload wall. They were retried every round and never arrived.
        "10.1103/physrevb.109.064414",
        "10.21203/rs.3.rs-2569477/v1",
        "10.1038/s43246-026-01312-9",
        "10.7185/gold2023.16723",
        "10.32908/hthp.v54.1923",
        "10.1063/5.0225871",
        "10.1088/1361-6668/acbb35",
        "10.1016/j.physc.2024.1354465",
        "10.1134/s0040577924110126",
        "10.5194/egusphere-egu26-3766",
        "10.21203/rs.3.rs-10120640/v1",
        "10.21009/spektra.091.02",
        "10.3103/s1068371225700324",
        "10.3389/fimmu.2023.1302891",
    )
)

# Wall-clock budget for one source of one paper. The httpx clients inside
# the fetchers have their own timeouts, but a connection that is accepted
# and then goes silent is not covered by them, and the loop would wait on it
# for good -- the scheduled task would then look "running" and never
# restart it.
#
# 60 s is generous: through the proxy every paper that arrives at all
# comes down in 2-35 s. A paper that has not answered in 60 s is not
# coming, and a round of thirty such candidates is where the wall clock
# went.
_FETCH_TIMEOUT_S = 60.0

# Wall-clock budget for one search round.
#
# The round fires two dozen CrossRef queries, four arXiv queries and -- when
# the OpenAlex budget allows it -- thirty venue queries, each with its own
# pacing gap. Measured on a round with the OpenAlex searches skipped, that
# is 202 s, so a 300 s budget was cutting the round off and throwing its
# results away.
_ROUND_TIMEOUT_S = 600.0

# Wall-clock budget for the ego lite fallback of one paper.
_EGO_TIMEOUT_S = 180.0

# The publishers a real browser reaches and a plain client does not.
#
# The browser clears the challenge -- IOP's hCaptcha, AIP's and RSC's
# Cloudflare -- in about five seconds, and the address it ends on
# carries a token rather than a session, so the file itself can then be
# fetched by anything.
#
# Wiley, IEEE, Taylor & Francis and the Journal of the Physical Society
# of Japan are left out: their challenges did not clear in the time the
# route allows, and trying them would cost a minute a paper for
# nothing.
_BROWSER_PREFIXES: tuple[str, str, ...] = (
    "10.3390",   # MDPI
    "10.1088",   # IOP
    "10.1063",   # AIP
    "10.1039",   # RSC
)


def _is_fetchable_doi(doi: str) -> bool:
    """True when this loop has a chance of downloading the paper.

    A blocked DOI is not fetchable for this pipeline: it is on the
    blocklist because re-downloading it only produces a paper the
    extractor or the parser cannot handle.
    """
    key = doi.casefold()
    if key in _BLOCKED_DOIS:
        return False
    if any(key.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return False
    return key.startswith(_FETCHABLE_PREFIXES)


def pdf_path_for(doi: str) -> Path:
    cache = Path(get_settings().pdf_cache_dir)
    return cache / (hashlib.sha256(doi.strip().lower().encode()).hexdigest() + ".pdf")


def pdf_count(cache: Path) -> int:
    """How many PDFs the cache holds -- the corpus size a target counts."""
    return sum(1 for _ in cache.glob("*.pdf"))


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
    # Write it beside the queue and rename it over: writing in place
    # truncates the file first, and the uploader reads it while the
    # download loop is rewriting it -- it copied a half-written queue to
    # the master, and the extraction there died on it.
    temp = QUEUE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(existing, indent=1), encoding="utf-8")
    os.replace(temp, QUEUE)


async def fetch_one(
    doi: str, target: Path, *, browser: bool = True
) -> tuple[bytes | None, str]:
    """Get the PDF for ``doi``, or ``None``, and the channel that got it.

    The channel is reported because it is the only way to tell which
    route is carrying the corpus: the log line says so on every paper,
    and the monitor counts them.

    The Elsevier API comes first, because it answers for the publisher
    whose papers nothing else reaches: the PDF endpoint answers 403 with
    a Cloudflare challenge page even from the institute's address, and
    only two thirds of the Elsevier papers have a preprint to fall back
    on. It returns ``None`` at once for any other publisher, so it costs
    the other routes nothing.

    After that the arXiv preprint: it is the one route that answers for
    almost every paper from the remaining journals, and the publisher's
    own PDF endpoint is either behind a captcha (IOP) or refuses the
    request outright (APS, Wiley). Trying it early also saves the ten
    seconds a captcha page costs before it gives up.

    When plain HTTP does not deliver, the article page can be opened in
    **ego lite** -- a real browser with the user's own profile, which
    clears the publisher challenges a scripted request does not. It is off
    by default: the ego CLI starts console programs of its own, and each
    one flashes a window on the desktop.
    """
    for fetch in (
        fetch_elsevier_pdf,
        fetch_aps_pdf,
        fetch_arxiv_pdf,
        fetch_direct_pdf,
        fetch_frontiers_pdf,
        fetch_iop_pdf,
        fetch_oa_pdf,
    ):
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(fetch, doi), timeout=_FETCH_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            # A publisher that accepts the connection and then never answers
            # holds the whole loop: the socket sits there with zero traffic
            # and the scheduled task waits forever, because a task that is
            # still "running" is never restarted by its own trigger.
            log.warning("fetch %s timed out for %s", fetch.__name__, doi)
            continue
        except Exception as exc:  # noqa: BLE001 - a dead source must not stop the loop
            log.debug("fetch %s failed: %s", fetch.__name__, exc)
            continue
        if data:
            return data, _channel_name(fetch)
    if not browser:
        return None, ""
    if not doi.casefold().startswith(_BROWSER_PREFIXES):
        # A real browser costs 20-35 s a paper, and it only gets through
        # at some publishers. Measured: MDPI serves the file to the
        # browser, while AIP, IOP, RSC, T&F, Wiley and APS block the
        # browser as well -- so trying it for them would spend half a
        # minute a paper for nothing.
        return None, ""
    try:
        from superconductor_db.ingest.ego_fetch import fetch_doi_pdf

        if await asyncio.wait_for(
            fetch_doi_pdf(doi, target), timeout=_EGO_TIMEOUT_S
        ):
            return target.read_bytes(), "ego"
    except asyncio.TimeoutError:
        log.warning("ego fetch timed out for %s", doi)
    except Exception as exc:  # noqa: BLE001 - ego may not be running
        log.debug("ego fetch failed for %s: %s", doi, exc)
    return None, ""


def _channel_name(fetch: Any) -> str:
    """The name of the channel that delivered a paper."""
    name = getattr(fetch, "__name__", "")
    for prefix in ("fetch_", "_fetch_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    for suffix in ("_pdf",):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or "unknown"


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
            if args.target and pdf_count(cache) >= args.target:
                log.info(
                    "target of %d PDFs reached (%d) - stopping",
                    args.target, pdf_count(cache),
                )
                return 0
            round_index += 1
            offset = 0 if round_index == 1 else (round_index - 1) * args.per_query
            try:
                dois = await asyncio.wait_for(
                    _search_round(
                        client,
                        per_query=args.per_query,
                        start_offset=offset,
                    ),
                    timeout=_ROUND_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning("search round timed out after %.0f s", _ROUND_TIMEOUT_S)
                await asyncio.sleep(args.delay)
                continue
            except Exception as exc:  # noqa: BLE001 - a bad round must not stop the loop
                log.warning("search round failed: %s", exc)
                await asyncio.sleep(args.delay)
                continue

            fresh = [
                d for d in dois
                if _is_fetchable_doi(d) and d.casefold() not in known
            ]
            log.info(
                "round %d: %d hits, %d new (offset %d)",
                round_index, len(dois), len(fresh), offset,
            )

            # Web of Science, through the ego browser. The institute's
            # address is entitled to the Core Collection and the interface
            # needs no sign-in, so it costs no allowance -- unlike
            # OpenAlex, whose searches are what spend the daily dollar. A
            # search is a browser session rather than a request, so it
            # runs once a round, and the page number walks the result list
            # so every round sees different papers.
            if not args.no_wos:
                query = WOS_QUERIES[round_index % len(WOS_QUERIES)]
                page = round_index // len(WOS_QUERIES) + 1
                wos_dois = await search_wos(query, page=page)
                if wos_dois:
                    wos_fresh = [
                        d for d in wos_dois
                        if _is_fetchable_doi(d) and d.casefold() not in known
                    ]
                    log.info(
                        "round %d: wos %r page %d -> %d DOIs (%d new)",
                        round_index, query, page, len(wos_dois), len(wos_fresh),
                    )
                    fresh.extend(wos_fresh)

            added = 0
            for doi in fresh:
                known.add(doi.casefold())

            # The fetches run concurrently. A paper that no channel can
            # reach spends up to three HTTP timeouts and a browser timeout
            # in sequence, so doing them one at a time made the round's
            # wall clock the sum of every timeout -- the loop spent most of
            # its time waiting on papers it was never going to get.
            semaphore = asyncio.Semaphore(max(1, args.fetch_concurrency))

            async def fetch_bounded(doi: str) -> tuple[str, bytes | None, str]:
                async with semaphore:
                    target = pdf_path_for(doi)
                    if target.exists() and target.stat().st_size > 0:
                        return doi, None, ""
                    data, channel = await fetch_one(
                        doi, target, browser=not args.no_browser
                    )
                    if not data:
                        # Logged here rather than after the gather, so the
                        # log shows what the round is doing while it is
                        # still doing it.
                        log.info("no channel could fetch %s", doi)
                    return doi, data, channel

            results = await asyncio.gather(
                *(fetch_bounded(d) for d in fresh), return_exceptions=True
            )
            for doi, data, channel in results:
                if isinstance(data, BaseException):
                    log.warning("fetch failed for %s: %s", doi, data)
                    continue
                if not data:
                    continue
                target = pdf_path_for(doi)
                target.write_bytes(data)
                # The queue file is read-modify-written, so the appends stay
                # sequential even though the fetches did not.
                append_to_queue([{"doi": doi, "pdf_path": str(target)}])
                added += 1
                log.info(
                    "downloaded %s via %s -> %.1f MB",
                    doi, channel or "?", len(data) / 1048576,
                )
            log.info("round %d: %d new PDFs", round_index, added)

            if args.target and pdf_count(cache) >= args.target:
                log.info(
                    "target of %d PDFs reached (%d) - stopping",
                    args.target, pdf_count(cache),
                )
                return 0

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
        "--fetch-concurrency",
        type=int,
        default=8,
        help=(
            "how many papers to fetch at once inside a round. One paper can "
            "sit in a publisher's timeout for minutes, so fetching them one "
            "at a time made the round's wall clock the sum of every timeout."
        ),
    )
    parser.add_argument(
        "--target", type=int, default=0,
        help=(
            "stop once the PDF cache holds this many files (0 = no target). "
            "Checked before every search round and again after every round, "
            "so the loop stops itself once the corpus reaches the goal."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "do not fall back to the ego lite browser for pages that need "
            "one. The fallback is windowless -- the ego CLI is started with "
            " CREATE_NO_WINDOW and the node processes it spawns inherit "
            "that console -- so it is on by default: it is the only route "
            "to the Elsevier, IOP and APS papers, which have no plain-HTTP "
            "PDF endpoint."
        ),
    )
    parser.add_argument(
        "--no-wos",
        action="store_true",
        help=(
            "do not search Web of Science. It is reached through the ego "
            "browser -- a browser session per search, not a request -- and "
            "it needs the desktop, so it is on only here."
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
    # The timestamp is what makes a round measurable: the log says what
    # happened, but not how long it took, and the round's wall clock is
    # what decides how many rounds a day the corpus gets.
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.FileHandler(args.log, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(fmt)
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
