"""Render the paper's figure PDFs to PNG, so they can be looked at.

Usage::

    python tools/preview_figures.py [--out ../paper/figures/_preview]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

BACKEND = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=str(BACKEND.parent / "paper" / "figures"))
    parser.add_argument("--out", default=str(BACKEND.parent / "paper" / "figures" / "_preview"))
    parser.add_argument("--dpi", type=int, default=140)
    args = parser.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(src.glob("*.pdf")):
        doc = pymupdf.open(pdf)
        pix = doc[0].get_pixmap(dpi=args.dpi)
        target = out / f"{pdf.stem}.png"
        pix.save(target)
        doc.close()
        print(f"{pdf.name} → {target.name}  {pix.width}x{pix.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
