"""Patch the pipeline numbers in the manuscript to the frozen release.

The extraction log keeps growing, so the counts are taken at the
release (the moment of the corpus snapshot) and written into the text
from there.  Every replacement is checked.

Usage::

    python tools/_patch_frozen_numbers.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    # Abstract.
    (
        "15{,}397 extracted papers the pipeline proposed 11{,}565 records, and a\n"
        "cascade of gates rejected 379{,}315 field-level extractions, of which\n"
        "370{,}901 had no verbatim evidence in the paper at all, 7{,}505\n"
        "reported a value that the text does not attach to the material, and 909",
        "15{,}393 extracted papers the pipeline proposed 11{,}560 records, and a\n"
        "cascade of gates rejected 379{,}210 field-level extractions, of which\n"
        "370{,}799 had no verbatim evidence in the paper at all, 7{,}502\n"
        "reported a value that the text does not attach to the material, and 909",
    ),
    # The gates section.
    (
        "every superconductor paper states, lost 29{,}891.",
        "every superconductor paper states, lost 29{,}887.",
    ),
    # Table I.
    (
        "$T_{\\rm CDW}$                 & 44{,}742 & no verbatim evidence\\\\\n"
        "$H_{c1}$                      & 44{,}538 & no verbatim evidence\\\\\n"
        "$J_c$                         & 43{,}997 & no verbatim evidence\\\\\n"
        "Penetration depth             & 43{,}928 & no verbatim evidence\\\\\n"
        "Coherence length              & 43{,}288 & no verbatim evidence\\\\\n"
        "$H_{c2}$                      & 42{,}682 & no verbatim evidence\\\\\n"
        "Energy gap                    & 42{,}654 & no verbatim evidence\\\\\n"
        "$T_c$                         & 29{,}891 & no verbatim evidence\\\\\n"
        "Pairing symmetry              & 17{,}826 & no verbatim evidence\\\\\n"
        "Mechanism                     & 17{,}355 & no verbatim evidence\\\\\n"
        "$T_c$                         &  7{,}505 & not stated for this material\\\\\n"
        "$T_c$                         &    909 & a calculation\\\\\n"
        "\\midrule\n"
        "Total                         & 379{,}315 & \\\\",
        "$T_{\\rm CDW}$                 & 44{,}731 & no verbatim evidence\\\\\n"
        "$H_{c1}$                      & 44{,}527 & no verbatim evidence\\\\\n"
        "$J_c$                         & 43{,}986 & no verbatim evidence\\\\\n"
        "Penetration depth             & 43{,}917 & no verbatim evidence\\\\\n"
        "Coherence length              & 43{,}277 & no verbatim evidence\\\\\n"
        "$H_{c2}$                      & 42{,}671 & no verbatim evidence\\\\\n"
        "Energy gap                    & 42{,}644 & no verbatim evidence\\\\\n"
        "$T_c$                         & 29{,}887 & no verbatim evidence\\\\\n"
        "Pairing symmetry              & 17{,}815 & no verbatim evidence\\\\\n"
        "Mechanism                     & 17{,}344 & no verbatim evidence\\\\\n"
        "$T_c$                         &  7{,}502 & not stated for this material\\\\\n"
        "$T_c$                         &    909 & a calculation\\\\\n"
        "\\midrule\n"
        "Total                         & 379{,}210 & \\\\",
    ),
    # The database section.
    (
        "run over 15{,}397 of them and proposed 11{,}565 records; 4{,}918 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 6{,}647 that survived that filter, 4{,}009 became new rows, and",
        "run over 15{,}393 of them and proposed 11{,}560 records; 4{,}917 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 6{,}643 that survived that filter, 4{,}005 became new rows, and",
    ),
    # The cost of the extraction.
    (
        "The extraction of the corpus cost 68{,}175 calls to the model and",
        "The extraction of the corpus cost 68{,}158 calls to the model and",
    ),
]


def main() -> int:
    text = TEX.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS:
        if old not in text:
            print(f"!! 没找到：{old[:80]!r}")
            return 1
        text = text.replace(old, new, 1)
    TEX.write_text(text, encoding="utf-8")
    print(f"已更新 {len(REPLACEMENTS)} 处 → {TEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
