"""Re-verify stored Tc values against the source papers.

The first ingest pass stored whatever number the model quoted, and the number
kept its model-assigned ``experimental`` flag. Two failure modes followed:

1. **Theory stored as measurement.** Papers quote both a predicted and a
   measured Tc in the same paragraph; the headline theoretical value won.
   LaH10 entered the database at 288 K, which is the Ashcroft-era prediction,
   and YH9 at 276 K the same way.
2. **One number copied across materials.** A single pressure point was
   assigned to every composition mentioned nearby, so CeH9, LaH9, CeH10 and
   PrH9 all came back with Tc = 56 K.

This module re-runs extraction for every DOI already in the database, then
compares the fresh evidence sentence and its experimental/theoretical
classification against the stored row:

* ``corrected``  — the value was a prediction and is now flagged theoretical
* ``disputed``   — a fresh value disagrees with the stored one
* ``unverifiable`` — the paper could not be re-read
* ``ok``         — re-read, and the stored value is consistent

Run with ``--apply`` to write the corrections; without it the module only
reports, so a first pass is always safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401 — register tables
from superconductor_db.db import Base
from superconductor_db.db.models.material import Material
from superconductor_db.db.models.paper import Paper
from superconductor_db.ingest.db_setup import build_ingest_engine
from superconductor_db.ingest.extractor import (
    SuperconductingExtractor,
    classify_tc_evidence,
    clear_evidence,
    evidence_for,
)
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    download_pdf,
    parse_pdf_to_markdown,
)

log = logging.getLogger("reverify")

# Two formulas that differ only by phase prefix, oxidation state, or a trailing
# qualifier denote the same material. Such a pair sharing a Tc is a
# registration duplicate, not a value-copying error.
_ALIAS_STRIP_RE = re.compile(r"[0-9.]+")


def _normalise_formula(formula: str) -> str:
    return re.sub(r"[\s()_\-]", "", formula).casefold()


def _is_same_material(a: str, b: str) -> bool:
    """True when two formula strings denote the same material."""
    na, nb = _normalise_formula(a), _normalise_formula(b)
    if na == nb:
        return True
    # "2H-NbSe2" vs "NbSe2", "LaO0.5F0.5BiS2" vs "La(O,F)BiS2": drop the
    # stoichiometric digits and compare what is left.
    return _ALIAS_STRIP_RE.sub("", na) == _ALIAS_STRIP_RE.sub("", nb)


async def _re_read(identifier: str, extractor: SuperconductingExtractor) -> dict[str, Any]:
    """Re-extract one paper and return ``{composition: record}``."""
    try:
        pdf_path = await download_pdf(identifier, timeout=180)
    except ScanSciError as exc:
        log.warning("re-verify: cannot fetch %s: %s", identifier, str(exc)[:80])
        return {}
    md = await parse_pdf_to_markdown(pdf_path)
    clear_evidence()
    records = await extractor.extract_all(md[:28000], doi=identifier)
    return {r.material_chemical_composition: r for r in records}


async def reverify(
    database_url: str,
    *,
    apply: bool = False,
    concurrency: int = 3,
    limit: int | None = None,
) -> dict[str, int]:
    """Re-read every paper behind a stored material row.

    With ``apply=False`` (the default) nothing is written: the caller gets a
    report of what would change. With ``apply=True`` the ``experimental`` flag
    is corrected from the evidence wording and the evidence sentence is stored
    in ``notes``; the numeric ``tc_value`` is left untouched, because
    overwriting it with a fresh model reading could introduce a *worse* value
    than the one already verified against this paper.
    """
    engine = build_ingest_engine(database_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # Group stored rows by the paper that produced them.
    with Session() as s:
        doi_by_paper = {
            p.id: p.doi
            for p in s.execute(select(Paper)).scalars()
            if p.doi
        }
        grouped: dict[str, list[Material]] = defaultdict(list)
        for m in s.execute(select(Material)).scalars():
            if m.doi:
                grouped[m.doi].append(m)
            elif m.paper_id in doi_by_paper:
                grouped[doi_by_paper[m.paper_id]].append(m)

    dois = sorted(d for d in grouped if not _NON_ARTICLE_RE.search(d))
    skipped = len(grouped) - len(dois)
    if skipped:
        log.info("re-verify: skipping %d non-article DOIs (book chapters)", skipped)
    if limit:
        dois = dois[:limit]
    log.info("re-verify: %d papers queued", len(dois))

    settings = _load_settings()
    extractor = SuperconductingExtractor(LLMClient(settings))
    semaphore = asyncio.Semaphore(concurrency)
    stats = {"ok": 0, "corrected": 0, "disputed": 0, "unverifiable": 0, "papers": 0}

    async def one(doi: str) -> dict[str, Any]:
        async with semaphore:
            try:
                pdf_path = await download_pdf(doi, timeout=180)
            except ScanSciError as exc:
                log.info("  %s unverifiable: %s", doi, str(exc)[:60])
                return {"doi": doi, "verdict": "unverifiable", "rows": []}
            md = await parse_pdf_to_markdown(pdf_path)
            clear_evidence()
            try:
                records = await extractor.extract_all(md[:28000], doi=doi)
            except Exception as exc:  # noqa: BLE001
                log.warning("  %s extraction failed: %s", doi, exc)
                return {"doi": doi, "verdict": "unverifiable", "rows": []}

            fresh = {
                r.material_chemical_composition: r
                for r in records
            }

            # Materials this paper put on one Tc: either aliases of the same
            # substance (2H-NbSe2 / NbSe2 — a registration duplicate) or one
            # number copied across genuinely different compounds (CeH9, LaH9,
            # CeH10, PrH9 all at 56 K — an extraction error). Only the second
            # kind is a defect worth reporting.
            by_tc: dict[float, list[str]] = defaultdict(list)
            for m in grouped[doi]:
                if m.tc_value is not None:
                    by_tc[float(m.tc_value)].append(m.chemical_composition)
            copied: set[str] = set()
            for _tc, comps in by_tc.items():
                distinct: list[str] = []
                for comp in comps:
                    if not any(_is_same_material(comp, d) for d in distinct):
                        distinct.append(comp)
                if len(distinct) > 1:
                    copied.update(distinct)
                    log.warning(
                        "  %s: Tc=%s shared by distinct materials %s",
                        doi, _tc, distinct,
                    )

            results = []
            for m in grouped[doi]:
                rec = fresh.get(m.chemical_composition)
                quote = evidence_for(m.chemical_composition).get("Tc", "")
                verdict_klass = classify_tc_evidence(quote) if quote else ""
                if rec is None:
                    results.append(
                        {"material": m, "quote": quote, "kind": verdict_klass,
                         "status": "unverifiable"}
                    )
                    continue
                new_tc = (
                    rec.superconducting_property.Tc.value
                    if rec.superconducting_property.Tc
                    else None
                )
                if m.chemical_composition in copied:
                    status = "disputed"
                elif verdict_klass == "theoretical" and m.experimental:
                    status = "corrected"
                elif (
                    new_tc is not None
                    and m.tc_value is not None
                    and abs(new_tc - m.tc_value) > 0.5
                ):
                    status = "disputed"
                else:
                    status = "ok"
                results.append(
                    {"material": m, "quote": quote, "kind": verdict_klass,
                     "status": status, "new_tc": new_tc}
                )
            return {"doi": doi, "verdict": "done", "rows": results}

    for doi in dois:
        out = await one(doi)
        stats["papers"] += 1
        for row in out["rows"]:
            stats[row["status"]] = stats.get(row["status"], 0) + 1
            if row["status"] in {"corrected", "disputed"}:
                m = row["material"]
                log.info(
                    "  %-9s %-22s Tc=%-8s %s",
                    row["status"], m.chemical_composition, m.tc_value,
                    (row.get("quote") or "")[:60],
                )
        if apply:
            with Session() as s:
                for row in out["rows"]:
                    if row["status"] != "corrected":
                        continue
                    m = s.get(Material, row["material"].id)
                    if m is not None:
                        m.experimental = False
                        if row["quote"]:
                            m.notes = f"Tc evidence: {row['quote']}"
                s.commit()
    return stats


# Book chapters and other non-article records. A Wiley textbook chapter
# (10.1002/9781118696644.ch9) reached the database and its 40-page body made
# the model return an empty message, which then counted as an extraction
# failure. Excluded from re-verification and worth deleting from the corpus.
_NON_ARTICLE_RE = re.compile(
    r"10\.1002/978|10\.1016/b978|10\.1038/978|/978[0-9]{6,}|\.ch[0-9]+$",
    re.IGNORECASE,
)


def _load_settings():  # noqa: ANN202
    from superconductor_db.core.config import get_settings

    return get_settings()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default="")
    parser.add_argument("--apply", action="store_true",
                        help="Write corrections (default: report only)")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None,
                        help="Only re-read the first N papers (smoke test)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from superconductor_db.ingest.paper_search_loop import _default_database_url

    url = args.database_url or _default_database_url()
    stats = asyncio.run(
        reverify(
            url, apply=args.apply, concurrency=args.concurrency, limit=args.limit
        )
    )
    log.info("summary: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
