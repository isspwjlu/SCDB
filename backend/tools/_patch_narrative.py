"""Rewrite the two passages that the new corpus changed in substance.

The family classification and the larger corpus moved the top of the
transition-temperature distribution and the family ranking, so the two
passages that describe them are rewritten rather than renumbered.

Usage::

    python tools/_patch_narrative.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    (
        "The highest transition temperatures in the database are therefore a\n"
        "mixture of two kinds of statement.  The record at 298~K,\n"
        "LaSc$_2$H$_{24}$, is an observation: its sentence records that\n"
        "superconductivity was confirmed both by zero resistance and by the\n"
        "suppression of $T_c$ under an external magnetic field.  The other\n"
        "record at 298~K, Sr$_2$Sc$_4$H$_{33}$, comes from a paper on predicted\n"
        "structures, and the sentence that carries it is a structure label.\n"
        "Four of the five records above 290~K are of that second kind.",
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
    ),
    (
        "The third panel is the ranking a database naturally produces, and it is\n"
        "the ranking the audit is about.  Two of the eleven records that head\n"
        "their family are the errors this paper has already shown: the\n"
        "cuprate's 175~K is the number ``$T_c = 0.15$ corresponds to about\n"
        "175~K'', a scaling result stored as a transition temperature, and the\n"
        "iron-based 109~K belongs to a FeSe monolayer and was recorded for bulk\n"
        "FeSe.  A third, the MgB$_2$ record at 51~K, is flagged by the\n"
        "extraction itself as predicted, its sentence being one about a\n"
        "computed gap.  A reader who took the ranking at face value would put a\n"
        "cuprate above every measured cuprate.  The record that really heads\n"
        "the hydride family, 298~K, is the one whose sentence reports a\n"
        "measurement.",
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
    ),
    (
        "The extraction's own flag marks\n"
        "7.9\\% of the corpus as not experimental; the audit, reading the same\n"
        "sentences afresh, finds 14.4\\%.",
        "The extraction's own flag marks\n"
        "8.1\\% of the corpus as not experimental; the audit, reading the same\n"
        "sentences afresh, finds 14.4\\%.",
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
