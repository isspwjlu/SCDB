"""The citation audit: every reference cited, every citation resolvable.

A paper with an orphan reference or a citation to nothing is a
reference error, and the ARS rules treat it as a blocker.

Usage::

    python tools/citation_audit.py
"""

from __future__ import annotations

import io
import re
from pathlib import Path

PAPER = Path(__file__).resolve().parents[2] / "paper"


def main() -> int:
    tex = (PAPER / "scdb_main.tex").read_text(encoding="utf-8")
    bib = (PAPER / "scdb_main.bib").read_text(encoding="utf-8")

    cited: set[str] = set()
    for match in re.finditer(r"\\cite\{([^}]*)\}", tex):
        for key in match.group(1).split(","):
            cited.add(key.strip())
    raw_keys = re.findall(r"@\w+\{([^,]+),", bib)
    keys = set(raw_keys)

    missing = sorted(cited - keys)
    orphan = sorted(keys - cited)
    duplicates = sorted({k for k in raw_keys if raw_keys.count(k) > 1})
    print(f"正文引用 {len(cited)} 条；bib 收录 {len(keys)} 条")
    print(f"引用了但 bib 里没有 : {missing or '无'}")
    print(f"bib 里有但正文没引 : {orphan or '无'}")
    print(f"bib 里重复的键       : {duplicates or '无'}")

    # Every reference must carry a DOI, or say where it came from.
    entries = re.findall(r"@\w+\{([^,]+),(.*?)\n\}", bib, re.S)
    without_doi = []
    for key, body in entries:
        if "doi =" not in body and "journal =" not in body:
            without_doi.append(key)
    print(f"没有 DOI 也没有出处说明 : {without_doi or '无'}")
    return 1 if (missing or orphan or without_doi or duplicates) else 0


if __name__ == "__main__":
    raise SystemExit(main())
