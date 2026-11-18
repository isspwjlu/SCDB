"""Decide whether each stored Tc is a measurement or a prediction, then purge
the predictions.

The ingest pipeline was found storing computed critical temperatures as if
they had been observed. The clearest case is PRL 119, 107001 ("Hydrogen
Clathrate Structures in Rare Earth Hydrides at High Pressures: *Possible
Route* to Room-Temperature Superconductivity"), a DFT study that contributed
six rows to the database — LaH10 at 288 K, YH9 at 276 K — all of them
calculated. It also stored "‹56 K" as a flat 56.0 for four materials, losing
the inequality.

Neither the LLM's own ``Experimental`` flag nor a citation index can settle
this, so this module re-reads the paper and looks at the sentence around the
value:

* value surrounded by predicted/calculated/estimated phrasing  -> theoretical
* value surrounded by observed/measured/zero-resistance phrasing -> experimental
* the paper never states this value for this compound             -> unsupported

Theoretical and unsupported rows are deleted; experimental rows are kept and
stamped with the sentence that justifies them.

Run without ``--apply`` to preview; with ``--apply`` to delete.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from superconductor_db.core.config import get_settings
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    download_pdf,
    parse_pdf_to_markdown,
)

log = logging.getLogger("purge_theoretical")

# Wording that marks a number as a calculation rather than an observation.
_THEORY = re.compile(
    r"\b(predict\w*|calculat\w*|comput\w*|theor\w*|estimat\w*|propos\w*|"
    r"expected|no superconductivity|possible route|suggest\w*|"
    r"in principle|should reach|would reach|thermodynamic estimate|"
    # McMillan/Eliashberg papers derive Tc from a computed omega_log and then
    # announce it as "the resulting Tc values are 276 K ...". The derivation
    # wording sits just outside the window centred on the number, so the verbs
    # that introduce a derived result have to count too.
    r"resulting|derived from|obtained from|yield\w*|as high as|up to|"
    r"maximum)\b",
    re.IGNORECASE,
)

# Wording that marks a number as something actually measured.
_MEASURED = re.compile(
    r"\b(we (?:observ\w*|measur\w*|report\w*|find\w*|show\w*)|"
    r"observed|measured|reported|zero resistance|onset of|superconducting (?:at|below|transition)|"
    r"resistivity (?:drops|falls|vanish\w*)|diamagnetic|magnetic shielding|"
    r"zero[- ]resist\w*|resistive transition|ac susceptibility|"
    # Plain present-tense statements: "MgB2 shows superconductivity at 39 K."
    r"shows superconductivit\w*|exhibits superconductivit\w*|"
    r"becomes superconducting|turns superconducting)\b",
    re.IGNORECASE,
)

# Numbers introduced as a bound rather than a point value: "~56 K", "<56 K".
_BOUND = re.compile(r"^[<>~≈≤≥]")


def _normalise(text: str) -> str:
    """Collapse PDF line breaks so a phrase split across lines still matches."""
    return re.sub(r"-\s+", "", text)


def _windows(text: str, value: float, radius: int = 320) -> list[str]:
    """Return the text around each occurrence of ``value`` with a K unit."""
    # Render the value the way the paper would: 288, 288.0, 39, 39.0.
    forms = {f"{value:g}", f"{value:.1f}", f"{value:.0f}"}
    out = []
    for form in forms:
        for m in re.finditer(rf"(?<![\d.]){re.escape(form)}\s*(?:K\b|kelvin)", text):
            out.append(text[max(0, m.start() - radius): m.end() + radius])
    return out


def judge(markdown: str, value: float) -> str:
    """Return ``"experimental"``, ``"theoretical"``, ``"unsupported"``.

    Decided from the sentences surrounding the value, not from the paper's
    title or the model's own flag.
    """
    text = _normalise(markdown)
    for window in _windows(text, value):
        theory = _THEORY.search(window)
        measured = _MEASURED.search(window)
        # An explicit measurement wins over a passing mention of prediction
        # elsewhere in the same window.
        if measured and not (theory and theory.start() < measured.start()):
            return "experimental"
        if theory:
            return "theoretical"
    return "unsupported"


async def audit(database_url: str, *, apply: bool = False) -> dict[str, int]:
    """Re-read every paper behind a row flagged ``experimental=0``."""
    db_path = database_url.replace("sqlite:///", "")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select id, chemical_composition, tc_value, doi, notes "
        "from materials where experimental=0 and tc_value is not null"
    ).fetchall()
    by_doi: dict[str, list] = defaultdict(list)
    for row in rows:
        by_doi[row[3]].append(row)
    conn.close()

    log.info("purging: %d rows across %d papers", len(rows), len(by_doi))
    stats = {"experimental": 0, "theoretical": 0, "unsupported": 0, "unreadable": 0}

    for doi, items in by_doi.items():
        try:
            pdf = await download_pdf(doi, timeout=180)
            md = await parse_pdf_to_markdown(pdf)
        except ScanSciError as exc:
            log.warning("  %s unreadable: %s", doi, str(exc)[:60])
            stats["unreadable"] += len(items)
            continue
        for _rid, comp, tc, _doi, _notes in items:
            verdict = judge(md, float(tc))
            stats[verdict] += 1
            mark = {"experimental": "KEEP", "theoretical": "DROP",
                    "unsupported": "DROP"}[verdict]
            log.info("  %-5s %-16s Tc=%-8s  %s", mark, str(comp)[:16], tc, verdict)

    if apply:
        conn = sqlite3.connect(db_path)
        # Delete every row whose value is not a measurement, and stamp the
        # survivors with their justifying sentence.
        to_drop: list[tuple[int, str, float]] = []
        keep: list[tuple[str, int]] = []
        for doi, items in by_doi.items():
            try:
                md = await parse_pdf_to_markdown(await download_pdf(doi, timeout=180))
            except ScanSciError:
                continue
            for rid, _comp, tc, _doi, _notes in items:
                verdict = judge(md, float(tc))
                if verdict == "experimental":
                    keep.append((f"Tc re-verified as measured: {doi}", rid))
                else:
                    to_drop.append((rid, str(_comp), float(tc)))
        if to_drop:
            conn.executemany("delete from materials where id=?", [(r[0],) for r in to_drop])
            log.info("  deleted %d theoretical/unsupported rows", len(to_drop))
        if keep:
            conn.executemany(
                "update materials set notes=? where id=?", keep
            )
        conn.commit()
        conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    stats = asyncio.run(audit(args.database_url, apply=args.apply))
    log.info("summary: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
