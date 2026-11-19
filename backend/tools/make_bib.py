"""Build the paper's bibliography from verified DOIs.

Every entry is fetched from Crossref, so the metadata in the .bib is
Crossref's, not ours: the title, the authors, the journal, the volume,
the pages and the year are what the publisher registered.  A DOI that
does not resolve is an error, not a warning -- a reference that cannot
be checked must not reach the paper.

The entries that are not journal articles (a database's web site, a
preprint) are written out by hand and marked as such.

Usage::

    python tools/make_bib.py --out paper/scdb_main.bib
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "scdb-paper/1.0 (mailto:openalex-contact@example.org)"}

# The references, in the order the paper will cite them.
DOIS = (
    # The prior databases, and the prior extraction work on superconductors.
    "10.1038/s41597-023-02721-y",
    "10.1016/j.aei.2022.101768",
    "10.1080/27660400.2022.2153633",
    "10.1016/j.commatsci.2018.07.052",
    # Databases.
    "10.1093/nar/gkr900",
    "10.1063/1.4812323",
    # LLM-built databases and LLM extraction.
    "10.1016/j.jmmm.2024.172001",
    "10.1016/j.commatsci.2025.113855",
    "10.1038/s41467-025-64458-z",
    "10.1038/s41467-024-45914-8",
    "10.1039/d4cs00913d",
    "10.1039/d4dd00074a",
    "10.1021/acs.chemmater.7b03500",
    "10.1002/cl2.70073",
    "10.1016/j.commatsci.2026.114862",
    "10.1038/s43246-025-00994-x",
    "10.18653/v1/2025.findings-emnlp.128",
    "10.1016/j.cosrev.2026.100970",
    "10.1145/3820755.3830337",
    # Superconductivity physics.
    "10.1038/nature14964",
    "10.1038/s41586-019-1201-8",
    # Machine learning for superconductors.
    "10.1038/s41524-018-0085-8",
    "10.1021/acs.jcim.4c01137",
    "10.1038/s41598-024-54440-y",
    "10.1103/physrevb.108.174514",
    "10.1016/j.physc.2022.1354031",
    "10.3390/molecules26010008",
    # This group's own work.
    "10.1103/wn98-9yc7",
)

# The references that are not journal articles.
MANUAL = {
    "supercon": {
        "author": "{National Institute for Materials Science}",
        "title": "SuperCon: a database of superconducting materials",
        "journal": "National Institute for Materials Science, Japan",
        "year": "2024",
        "note": "Available at https://supercon.nims.go.jp",
    },
    "onnes": {
        "author": "Onnes, H. K.",
        "title": "Discovery of superconductivity",
        "journal": "Reports from the Laboratory of Physics at the University of Leiden",
        "year": "1911",
        "volume": "11",
    },
    "bc": {
        "author": "Bardeen, J. and Cooper, L. N. and Schrieffer, J. R.",
        "title": "Theory of superconductivity",
        "journal": "Phys. Rev.",
        "year": "1957",
        "volume": "108",
        "pages": "1175",
    },
}


def crossref(doi: str, tries: int = 4) -> dict | None:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    for _ in range(tries):
        try:
            request = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))["message"]
        except Exception:
            time.sleep(2)
    return None


def ascii_text(text: str) -> str:
    """Fold the Latin letters with marks onto ASCII.

    BibTeX reads the .bib as 8-bit text, and an accented name breaks
    the author parser.  The spelling loses its accent; the paper
    itself keeps it.
    """
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def initials(given: str) -> str:
    """``Yibo`` -> ``Y.`` -- the initials apsrev4-2 expects."""
    parts = [p for p in re.split(r"[\s\-]+", ascii_text(given)) if p]
    return " ".join(f"{p[0]}." for p in parts)


def author_name(author: dict) -> str:
    """One author as apsrev4-2 wants them: initials first, then family."""
    family = ascii_text(author.get("family", "")).strip()
    given = initials(author.get("given", ""))
    return f"{given} {family}".strip()


def clean(text: str) -> str:
    text = ascii_text(re.sub(r"<[^>]+>", "", text))
    text = text.replace("\\", "").replace("&", r"\&")
    text = text.replace("%", r"\%").replace("#", r"\#")
    text = text.replace("_", r"\_")
    return re.sub(r"\s+", " ", text).strip()


def bib_entry(key: str, meta: dict) -> str:
    authors = [author_name(a) for a in meta.get("author", [])]
    year = (meta.get("issued", {}).get("date-parts") or [[None]])[0][0]
    lines = [f"@article{{{key},"]
    lines.append("  author = {" + " and ".join(authors) + "},")
    lines.append(f"  title = {{{clean((meta.get('title') or ['?'])[0])}}},")
    lines.append(f"  journal = {{{clean((meta.get('container-title') or ['?'])[0])}}},")
    # apsrev4-2 wants the volume and the issue as numbers, not as
    # braced strings.
    if meta.get("volume"):
        lines.append(f"  volume = {meta['volume']},")
    if meta.get("issue"):
        lines.append(f"  number = {meta['issue']},")
    if meta.get("page"):
        lines.append(f"  pages = {{{meta['page']}}},")
    lines.append(f"  year = {{{year}}},")
    lines.append(f"  doi = {{{meta['DOI']}}},")
    if meta.get("publisher"):
        lines.append(f"  publisher = {{{clean(meta['publisher'])}}},")
    lines.append("}")
    return "\n".join(lines)


def manual_entry(key: str, fields: dict) -> str:
    lines = [f"@misc{{{key},"]
    for name, value in fields.items():
        lines.append(f"  {name} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(BACKEND.parent / "paper" / "scdb_main.bib"))
    args = parser.parse_args()

    entries: list[str] = []
    failures: list[str] = []
    for doi in DOIS:
        meta = crossref(doi)
        if meta is None:
            failures.append(doi)
            continue
        title = (meta.get("title") or [""])[0] or doi
        key = re.sub(r"[^A-Za-z0-9]", "", title)[:12]
        key = f"{key}{meta.get('issued', {}).get('date-parts', [[None]])[0][0]}"
        entries.append(bib_entry(key.lower(), meta))
        print(f"  ✓ {doi}  →  {key.lower()}")

    for key, fields in MANUAL.items():
        entries.append(manual_entry(key, fields))
        print(f"  · {key}（手工条目）")

    if failures:
        raise SystemExit(f"这些 DOI 没能核实：{', '.join(failures)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    print(f"\n{len(entries)} 条 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
