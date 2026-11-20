"""Write the new release's numbers into the manuscript.

The corpus was pulled from the master again after the family
classification, so every count that describes it has moved.  Each
replacement is checked; a pattern that is not found stops the script
rather than being skipped.

Usage::

    python tools/_patch_release_numbers.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    # Abstract.
    (
        "15{,}393 extracted papers the pipeline proposed 11{,}560 records, and a\n"
        "cascade of gates rejected 379{,}210 field-level extractions, of which\n"
        "370{,}799 had no verbatim evidence in the paper at all, 7{,}502\n"
        "reported a value that the text does not attach to the material, and 909\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 2{,}974 material records (1{,}287 distinct compositions)",
        "16{,}673 extracted papers the pipeline proposed 12{,}538 records, and a\n"
        "cascade of gates rejected 414{,}732 field-level extractions, of which\n"
        "405{,}470 had no verbatim evidence in the paper at all, 8{,}286\n"
        "reported a value that the text does not attach to the material, and 976\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 3{,}116 material records (1{,}345 distinct compositions)",
    ),
    # Table I.
    (
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
        "$T_{\\rm CDW}$                 & 48{,}521 & no verbatim evidence\\\\\n"
        "$H_{c1}$                      & 48{,}306 & no verbatim evidence\\\\\n"
        "$J_c$                         & 47{,}639 & no verbatim evidence\\\\\n"
        "Penetration depth             & 47{,}637 & no verbatim evidence\\\\\n"
        "Coherence length              & 46{,}954 & no verbatim evidence\\\\\n"
        "$H_{c2}$                      & 46{,}279 & no verbatim evidence\\\\\n"
        "Energy gap                    & 46{,}282 & no verbatim evidence\\\\\n"
        "$T_c$                         & 32{,}258 & no verbatim evidence\\\\\n"
        "Pairing symmetry              & 21{,}050 & no verbatim evidence\\\\\n"
        "Mechanism                     & 20{,}544 & no verbatim evidence\\\\\n"
        "$T_c$                         &  8{,}286 & not stated for this material\\\\\n"
        "$T_c$                         &    976 & a calculation\\\\\n"
        "\\midrule\n"
        "Total                         & 414{,}732 & \\\\",
    ),
    # Figure 2's caption.
    (
        "2{,}974 records over 1{,}287 distinct compositions, from a corpus of\n"
        "24{,}075 papers (19 November 2026).}\\label{fig:database}",
        "3{,}116 records over 1{,}345 distinct compositions, from a corpus of\n"
        "25{,}354 papers (20 November 2026).}\\label{fig:database}",
    ),
    # The database section.
    (
        "The corpus is a snapshot of 24{,}075 papers.  The extraction has been\n"
        "run over 15{,}393 of them and proposed 11{,}560 records; 4{,}917 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 6{,}643 that survived that filter, 4{,}005 became new rows, and\n"
        "the rest were merged into rows that already existed or were dropped\n"
        "because their own evidence sentence showed a calculated transition\n"
        "temperature.  Consolidation across papers and deduplication leave\n"
        "2{,}974 records over 1{,}287 distinct compositions, each with the\n"
        "sentence its value came from.",
        "The corpus is a snapshot of 25{,}354 papers.  The extraction has been\n"
        "run over 16{,}673 of them and proposed 12{,}538 records; 5{,}363 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 7{,}175 that survived that filter, 4{,}334 became new rows, and\n"
        "the rest were merged into rows that already existed or were dropped\n"
        "because their own evidence sentence showed a calculated transition\n"
        "temperature.  Consolidation across papers and deduplication leave\n"
        "3{,}116 records over 1{,}345 distinct compositions, each with the\n"
        "sentence its value came from.",
    ),
    (
        "temperatures spread over three decades and a half, from sub-kelvin to\n"
        "298~K, and the distribution is the one the literature gives: a dense\n"
        "band below 30~K and a long tail of high-$T_c$ compounds.  The families\n"
        "are the familiar ones (iron-based and cuprate materials dominate, with\n"
        "a substantial hydride family), and 623 of the records are unclassified\n"
        "superconductors, which is a fact about the extraction\n"
        "rather than about the physics.",
        "temperatures spread over three decades and a half, from sub-kelvin to\n"
        "300~K, and the distribution is the one the literature gives: a dense\n"
        "band below 30~K and a long tail of high-$T_c$ compounds.  The families\n"
        "are the familiar ones (iron-based and cuprate materials dominate, with\n"
        "a substantial hydride family), and 78 of the records are still\n"
        "unclassified, which is a fact about the extraction rather than about\n"
        "the physics.",
    ),
    # Figure 3's caption.
    (
        "fraction of the 2{,}974 records in which the field is present.}",
        "fraction of the 3{,}116 records in which the field is present.}",
    ),
    # The section on the top of the distribution.
    (
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "2{,}974 records, 238 (8.0\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 15\\% of those from 50~K, 22\\% of those from 100~K, 29\\% of\n"
        "those from 150~K and 43\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 153 hydride records above 100~K, 57\n"
        "(37\\%) are flagged as predicted, and of the 74 cuprate records above\n"
        "100~K, 3 (4\\%) are.",
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "3{,}116 records, 252 (8.1\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 16\\% of those from 50~K, 24\\% of those from 100~K, 32\\% of\n"
        "those from 150~K and 47\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 161 hydride records above 100~K, 63\n"
        "(39\\%) are flagged as predicted, and of the 124 cuprate records above\n"
        "100~K, 6 (5\\%) are.",
    ),
    # The cost of the extraction.
    (
        "The extraction of the corpus cost 68{,}158 calls to the model and\n"
        "$4.2\\times10^{8}$ tokens, of which $6.0\\times10^{7}$ were completion",
        "The extraction of the corpus cost 74{,}151 calls to the model and\n"
        "$4.6\\times10^{8}$ tokens, of which $6.6\\times10^{7}$ were completion",
    ),
    # Data availability.
    (
        "The corpus snapshot used in this paper (24{,}075 papers,\n"
        "2{,}974 material records over 1{,}287 compositions) is the\n"
        "19 November 2026 release;",
        "The corpus snapshot used in this paper (25{,}354 papers,\n"
        "3{,}116 material records over 1{,}345 compositions) is the\n"
        "20 November 2026 release;",
    ),
    # The appendix.
    (
        "The site is live and the extraction continues, so the counts it shows\n"
        "grow: it read 2{,}979 records when Figure~\\ref{fig:site} was made,\n"
        "five more than the 2{,}974 of the release this paper describes.",
        "The site is live and the extraction continues, so the counts it shows\n"
        "grow; Figure~\\ref{fig:site} was made on the release this paper\n"
        "describes.",
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
