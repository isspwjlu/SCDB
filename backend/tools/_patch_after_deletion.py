"""Write the numbers of the corpus without the projected record.

The record whose evidence sentence said that a transition temperature
"could attain" 300 K was removed from the corpus, the corpus was
pulled again, and every count that describes it has moved.

Usage::

    python tools/_patch_after_deletion.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    # Abstract.
    (
        "16{,}673 extracted papers the pipeline proposed 12{,}538 records, and a\n"
        "cascade of gates rejected 414{,}732 field-level extractions, of which\n"
        "405{,}470 had no verbatim evidence in the paper at all, 8{,}286\n"
        "reported a value that the text does not attach to the material, and 976\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 3{,}116 material records (1{,}345 distinct compositions)",
        "16{,}684 extracted papers the pipeline proposed 12{,}544 records, and a\n"
        "cascade of gates rejected 414{,}995 field-level extractions, of which\n"
        "405{,}726 had no verbatim evidence in the paper at all, 8{,}291\n"
        "reported a value that the text does not attach to the material, and 978\n"
        "reported a computed value as if it were measured.  The database that\n"
        "survives holds 3{,}117 material records (1{,}347 distinct compositions)",
    ),
    # Table I.
    (
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
        "$T_{\\rm CDW}$                 & 48{,}549 & no verbatim evidence\\\\\n"
        "$H_{c1}$                      & 48{,}334 & no verbatim evidence\\\\\n"
        "$J_c$                         & 47{,}667 & no verbatim evidence\\\\\n"
        "Penetration depth             & 47{,}665 & no verbatim evidence\\\\\n"
        "Coherence length              & 46{,}982 & no verbatim evidence\\\\\n"
        "$H_{c2}$                      & 46{,}306 & no verbatim evidence\\\\\n"
        "Energy gap                    & 46{,}305 & no verbatim evidence\\\\\n"
        "$T_c$                         & 32{,}276 & no verbatim evidence\\\\\n"
        "Pairing symmetry              & 21{,}072 & no verbatim evidence\\\\\n"
        "Mechanism                     & 20{,}570 & no verbatim evidence\\\\\n"
        "$T_c$                         &  8{,}291 & not stated for this material\\\\\n"
        "$T_c$                         &    978 & a calculation\\\\\n"
        "\\midrule\n"
        "Total                         & 414{,}995 & \\\\",
    ),
    # Figure 2's caption.
    (
        "3{,}116 records over 1{,}345 distinct compositions, from a corpus of\n"
        "25{,}354 papers (20 November 2026).}\\label{fig:database}",
        "3{,}117 records over 1{,}347 distinct compositions, from a corpus of\n"
        "25{,}366 papers (20 November 2026).}\\label{fig:database}",
    ),
    # The database section.
    (
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
        "The corpus is a snapshot of 25{,}366 papers.  The extraction has been\n"
        "run over 16{,}684 of them and proposed 12{,}544 records; 5{,}365 of\n"
        "those named a composition that is not a superconductor, which is the\n"
        "characteristic hallucination of a model reading a reference list.  Of\n"
        "the 7{,}179 that survived that filter, 4{,}335 became new rows, and\n"
        "the rest were merged into rows that already existed or were dropped\n"
        "because their own evidence sentence showed a calculated transition\n"
        "temperature.  Consolidation across papers and deduplication leave\n"
        "3{,}117 records over 1{,}347 distinct compositions, each with the\n"
        "sentence its value came from.",
    ),
    (
        "temperatures spread over three decades and a half, from sub-kelvin to\n"
        "300~K, and the distribution is the one the literature gives: a dense",
        "temperatures spread over three decades and a half, from sub-kelvin to\n"
        "298~K, and the distribution is the one the literature gives: a dense",
    ),
    (
        "a substantial hydride family), and 78 of the records are still\n"
        "unclassified, which is a fact about the extraction rather than about\n"
        "the physics.",
        "a substantial hydride family), and 79 of the records are still\n"
        "unclassified, which is a fact about the extraction rather than about\n"
        "the physics.",
    ),
    # Figure 3's caption.
    (
        "fraction of the 3{,}116 records in which the field is present.}",
        "fraction of the 3{,}117 records in which the field is present.}",
    ),
    # The top of the distribution.
    (
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "3{,}116 records, 252 (8.1\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 16\\% of those from 50~K, 24\\% of those from 100~K, 32\\% of\n"
        "those from 150~K and 47\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 161 hydride records above 100~K, 63\n"
        "(39\\%) are flagged as predicted, and of the 124 cuprate records above\n"
        "100~K, 6 (5\\%) are.",
        "Figure~\\ref{fig:tail} uses that flag over the whole corpus.  Of the\n"
        "3{,}117 records, 251 (8.1\\%) are flagged as not experimental, but the\n"
        "flag is not spread evenly.  It marks 12\\% of the records from 25~K\n"
        "upwards, 15\\% of those from 50~K, 24\\% of those from 100~K, 31\\% of\n"
        "those from 150~K and 45\\% of those from 250~K.  The families differ by\n"
        "an order of magnitude: of the 161 hydride records above 100~K, 63\n"
        "(39\\%) are flagged as predicted, and of the 123 cuprate records above\n"
        "100~K, 5 (4\\%) are.",
    ),
    # The two records at the top.
    (
        "The highest transition temperatures in the database are therefore a\n"
        "mixture of two kinds of statement.  The record that heads the corpus\n"
        "is not a hydride but a cuprate: HgBa$_2$Ca$_2$Cu$_3$O$_9$ at 300~K,\n"
        "and the sentence that carries it says the transition temperature\n"
        "\\emph{could attain} that level, which is a projection rather than an\n"
        "observation.  The two records just below it are hydrides at 298~K,\n"
        "and they are of two different kinds: LaSc$_2$H$_{24}$, whose sentence\n"
        "records that superconductivity was confirmed both by zero resistance\n"
        "and by the suppression of $T_c$ under an external magnetic field,\n"
        "and Sr$_2$Sc$_4$H$_{33}$, whose sentence is a structure label from a\n"
        "paper on predicted structures.  Five of the six records above\n"
        "290~K are computations.",
        "The highest transition temperatures in the database are therefore a\n"
        "mixture of two kinds of statement.  The two records at the top are\n"
        "both hydrides at 298~K, and they are of two different kinds:\n"
        "LaSc$_2$H$_{24}$, whose sentence records that superconductivity was\n"
        "confirmed both by zero resistance and by the suppression of $T_c$\n"
        "under an external magnetic field, and Sr$_2$Sc$_4$H$_{33}$, whose\n"
        "sentence is a structure label from a paper on predicted structures.\n"
        "Four of the five records above 290~K are computations.",
    ),
    # The ranking of the families.
    (
        "The third panel is the ranking a database naturally produces, and it is\n"
        "the ranking the audit is about.  Three of the twelve records that head\n"
        "their family are the errors this paper has already shown.  The\n"
        "cuprate's 300~K is a projection, the sentence saying the transition\n"
        "temperature \\emph{could attain} that level, and a reader who took the\n"
        "ranking at face value would put a cuprate above every measured\n"
        "cuprate.  The iron-based 109~K belongs to a FeSe monolayer and was\n"
        "recorded for bulk FeSe.  And the MgB$_2$ record at 51~K is flagged by\n"
        "the extraction itself as predicted, its sentence being one about a\n"
        "computed gap.  The record that really heads the hydride family,\n"
        "298~K, is the one whose sentence reports a measurement.",
        "The third panel is the ranking a database naturally produces, and it is\n"
        "the ranking the audit is about.  Two of the records that head their\n"
        "family are the errors this paper has already shown: the cuprate's\n"
        "175~K is the number ``$T_c = 0.15$ corresponds to about 175~K'', a\n"
        "scaling result stored as a transition temperature, and the\n"
        "iron-based 109~K belongs to a FeSe monolayer and was recorded for\n"
        "bulk FeSe.  A third, the MgB$_2$ record at 51~K, is flagged by the\n"
        "extraction itself as predicted, its sentence being one about a\n"
        "computed gap.  A reader who took the ranking at face value would\n"
        "put a cuprate above every measured cuprate.  The record that really\n"
        "heads the hydride family, 298~K, is the one whose sentence reports\n"
        "a measurement.",
    ),
    # The cost of the extraction.
    (
        "The extraction of the corpus cost 74{,}151 calls to the model and",
        "The extraction of the corpus cost 74{,}205 calls to the model and",
    ),
    # Data availability.
    (
        "The corpus snapshot used in this paper (25{,}354 papers,\n"
        "3{,}116 material records over 1{,}345 compositions) is the\n"
        "20 November 2026 release;",
        "The corpus snapshot used in this paper (25{,}366 papers,\n"
        "3{,}117 material records over 1{,}347 compositions) is the\n"
        "20 November 2026 release;",
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
