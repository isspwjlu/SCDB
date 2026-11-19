"""Patch the corpus-level numbers in the manuscript.

The corpus grew while the paper was being written, so the counts that
describe it are recomputed from the new snapshot and from the master's
extraction log, and written back into the text.  Every replacement is
checked: a pattern that is not found is an error, not a silent skip.

Usage::

    python tools/_patch_corpus_numbers.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    # Abstract.
    (
        "From\n15{,}318 extracted papers the pipeline proposed 11{,}497 records, and a\n"
        "cascade of gates rejected 377{,}140 field-level extractions, of which\n"
        "368{,}774 had no verbatim evidence in the paper at all, 7{,}460\n"
        "reported a value that the text does not attach to the material, and 906\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 2{,}939 material records (1{,}272 distinct compositions)",
        "From\n15{,}397 extracted papers the pipeline proposed 11{,}565 records, and a\n"
        "cascade of gates rejected 379{,}315 field-level extractions, of which\n"
        "370{,}901 had no verbatim evidence in the paper at all, 7{,}505\n"
        "reported a value that the text does not attach to the material, and 909\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 2{,}974 material records (1{,}287 distinct compositions)",
    ),
    # The gates section.
    (
        "$H_{c2}$ and the energy gap each lost more than\n42{,}000 field-level extractions, while $T_c$, the property that nearly\n"
        "every superconductor paper states, lost 29{,}745.",
        "$H_{c2}$ and the energy gap each lost more than\n42{,}000 field-level extractions, while $T_c$, the property that nearly\n"
        "every superconductor paper states, lost 29{,}891.",
    ),
    # Table I.
    (
        "$T_{\\rm CDW}$                 & 44{,}516 & no verbatim evidence\\\\\n"
        "$H_{c1}$                      & 44{,}309 & no verbatim evidence\\\\\n"
        "$J_c$                         & 43{,}774 & no verbatim evidence\\\\\n"
        "Penetration depth             & 43{,}698 & no verbatim evidence\\\\\n"
        "Coherence length              & 43{,}067 & no verbatim evidence\\\\\n"
        "$H_{c2}$                      & 42{,}470 & no verbatim evidence\\\\\n"
        "Energy gap                    & 42{,}427 & no verbatim evidence\\\\\n"
        "$T_c$                         & 29{,}745 & no verbatim evidence\\\\\n"
        "Pairing symmetry              & 17{,}625 & no verbatim evidence\\\\\n"
        "Mechanism                     & 17{,}143 & no verbatim evidence\\\\\n"
        "$T_c$                         &  7{,}460 & not stated for this material\\\\\n"
        "$T_c$                         &    906 & a calculation\\\\\n"
        "\\midrule\n"
        "Total                         & 377{,}140 & \\\\",
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
    ),
    # The database section.
    (
        "The corpus is a snapshot of 23{,}533 papers.  The extraction has been\n"
        "run over 15{,}318 of them and proposed 11{,}497 records; 4{,}881 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 6{,}616 that survived that filter, 3{,}994 became new rows, and\n"
        "the rest were merged into rows that already existed or were dropped\n"
        "because their own evidence sentence showed a calculated transition\n"
        "temperature.  Consolidation across papers and deduplication leave\n"
        "2{,}939 records over 1{,}272 distinct compositions, each with the\n"
        "sentence its value came from.",
        "The corpus is a snapshot of 24{,}075 papers.  The extraction has been\n"
        "run over 15{,}397 of them and proposed 11{,}565 records; 4{,}918 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 6{,}647 that survived that filter, 4{,}009 became new rows, and\n"
        "the rest were merged into rows that already existed or were dropped\n"
        "because their own evidence sentence showed a calculated transition\n"
        "temperature.  Consolidation across papers and deduplication leave\n"
        "2{,}974 records over 1{,}287 distinct compositions, each with the\n"
        "sentence its value came from.",
    ),
    (
        "conventional superconductors, which is a fact about the extraction\n"
        "rather than about the physics.  Just over half of the compounds are\n"
        "binary or ternary.",
        "conventional superconductors, which is a fact about the extraction\n"
        "rather than about the physics.  Just over half of the compounds are\n"
        "binary or ternary.",
    ),
    # Figure 2's caption.
    (
        "The count is 2{,}939 records over 1{,}272 distinct compositions, from a\n"
        "corpus of 23{,}533 papers (19 November 2026).",
        "The count is 2{,}974 records over 1{,}287 distinct compositions, from a\n"
        "corpus of 24{,}075 papers (19 November 2026).",
    ),
    # Figure 3's caption.
    (
        "fraction of the 2{,}939 records in which the field is present.",
        "fraction of the 2{,}974 records in which the field is present.",
    ),
    # The section on the top of the distribution.
    (
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "2{,}939 records, 233 (7.9\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 15\\% of those from 50~K, 22\\% of those from 100~K, 29\\% of\n"
        "those from 150~K and 43\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 152 hydride records above 100~K, 56\n"
        "(37\\%) are flagged as predicted, and of the 74 cuprate records above\n"
        "100~K, 3 (4\\%) are.",
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "2{,}974 records, 238 (8.0\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 15\\% of those from 50~K, 22\\% of those from 100~K, 29\\% of\n"
        "those from 150~K and 43\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 153 hydride records above 100~K, 57\n"
        "(37\\%) are flagged as predicted, and of the 74 cuprate records above\n"
        "100~K, 3 (4\\%) are.",
    ),
    # The cost of the extraction.
    (
        "The extraction of the corpus cost 67{,}793 calls to the model and",
        "The extraction of the corpus cost 68{,}175 calls to the model and",
    ),
    # Data availability.
    (
        "The corpus snapshot used in this paper (23{,}533 papers,\n"
        "2{,}939 material records over 1{,}272 compositions) is the\n"
        "19 November 2026 release;",
        "The corpus snapshot used in this paper (24{,}075 papers,\n"
        "2{,}974 material records over 1{,}287 compositions) is the\n"
        "19 November 2026 release;",
    ),
]


def main() -> int:
    text = TEX.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS:
        if old not in text:
            print(f"!! 没找到：{old[:70]!r}")
            return 1
        text = text.replace(old, new, 1)
    TEX.write_text(text, encoding="utf-8")
    print(f"已更新 {len(REPLACEMENTS)} 处 → {TEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
