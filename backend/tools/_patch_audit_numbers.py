"""Patch the audit numbers in the manuscript after a new audit run.

Every replacement is checked: a pattern that is not found is an error,
not a silent skip, so the script cannot quietly leave a stale number
in the text.

Usage::

    python tools/_patch_audit_numbers.py
"""

from __future__ import annotations

from pathlib import Path

TEX = Path(__file__).resolve().parents[2] / "paper" / "scdb_main.tex"

REPLACEMENTS = [
    # The three rates, and what the judge could not settle.
    (
        "Of the 146 records it judged, the value was stated in the text in 139\n"
        "cases (95.2\\%, 95\\% Wilson interval 90.4--97.7\\%), and the sentence was\n"
        "about the material the record names in 133 (91.1\\%, 85.4--94.7\\%).  The\n"
        "two conditions hold together in 129 records (88.4\\%, 82.1--92.6\\%), and\n"
        "that is the rate the rest of this paper uses: the value is in the text,\n"
        "and the text is about the compound the record says it is.  The judge\n"
        "never rejected the material binding outright; on the other thirteen\n"
        "records it could not settle whether the sentence was about the same\n"
        "material, and those are the ones whose formulas are not clean\n"
        "stoichiometries: $(La,Pr,Sm)_3$Ni$_2$O$_7$, MgB$_{1.87}$C$_{0.13}$,\n"
        "FeSe/SrTiO$_3$(110), YBa$_2$Cu$_3$O$_{6.6}$ and\n"
        "Nd$_{\\mathrm{0.8}}$Sr$_{\\mathrm{0.2}}$NiO$_2$.",
        "Of the 149 records it judged, the value was stated in the text in 143\n"
        "cases (96.0\\%, 95\\% Wilson interval 91.5--98.1\\%), and the sentence was\n"
        "about the material the record names in 129 (86.6\\%, 80.2--91.1\\%).  The\n"
        "two conditions hold together in 127 records (85.2\\%, 78.7--90.0\\%), and\n"
        "that is the rate the rest of this paper uses: the value is in the text,\n"
        "and the text is about the compound the record says it is.  On seventeen\n"
        "records it could not settle whether the sentence was about the same\n"
        "material, and those are largely the ones whose formulas are not clean\n"
        "stoichiometries: $(La,Pr,Sm)_3$Ni$_2$O$_7$, MgB$_{1.87}$C$_{0.13}$,\n"
        "FeSe/SrTiO$_3$(110), YBa$_2$Cu$_3$O$_{6.95}$, Na$_x$CoO$_2\\cdot\n"
        "$y$H$_2$O and Nd$_{\\mathrm{0.8}}$Sr$_{\\mathrm{0.2}}$NiO$_2$.",
    ),
    # The failures.
    (
        "Two records carried a value the text does not support in the form the\n"
        "record gives it: MgB$_2$-GaP at 0.8~K, where the paper describes an\n"
        "increase of 0.8~K, and La$_3$Ni$_2$O$_{7-\\delta}$ at 80~K, where the\n"
        "paper says the transition \\emph{exceeds} 80~K.  On five further\n"
        "records the judge could not settle whether the value was in the text at\n"
        "all, and on four of the 150 records it could not decide anything: these\n"
        "were the elemental aluminium at 1.47~K, Sr$_2$RuO$_4$ at 2.9~K, FeSe\n"
        "at 38.3~K and MgB$_2$ with 1.0 wt\\% GaN at 38.6~K.",
        "Three records carried a value the text does not attach to the material:\n"
        "MgO/MgB$_2$ at 38.5~K, YScH$_8$ at 113~K and YBa$_2$Cu$_3$O$_{6.7}$ at\n"
        "7.2~K.  Two records carried a value the text does not support in the\n"
        "form the record gives it: MgB$_2$-GaP at 0.8~K, where the paper\n"
        "describes an increase of 0.8~K, and La$_3$Ni$_2$O$_{7-\\delta}$ at\n"
        "80~K, where the paper says the transition \\emph{exceeds} 80~K.  On four\n"
        "further records the judge could not settle whether the value was in the\n"
        "text at all, and on one record of the 150 it could not decide anything:\n"
        "SH$_3$ at 200~K.",
    ),
    # The measurement rate.
    (
        "The third question gave the uncomfortable answer.  Only 81.5\\% (74.4--87.0\\%)\n"
        "of the values were measurements, and 21 of the 146 --- one in seven ---\n"
        "were, in the judge's reading, calculated values filed as observations\n"
        "(Fig.~\\ref{fig:audit}).  They are not spread evenly: ten of the\n"
        "twenty-one are hydrides, and half of the twenty hydride records in the\n"
        "sample are among them.  Reading those records\n"
        "by hand confirms the judge: YH$_6$ is recorded at 226~K from a\n"
        "sentence that reads ``Yttrium hydrides YH$_6$ ($T_c$ = 218--226~K) and\n"
        "YH$_9$ ($T_c$ = 235--243~K)'', which is a computed range from a\n"
        "first-principles study, stored as a single point.",
        "The third question gave the uncomfortable answer.  Only 83.2\\% (76.4--88.4\\%)\n"
        "of the values were measurements, and 21 of the 149 --- one in seven ---\n"
        "were, in the judge's reading, calculated values filed as observations\n"
        "(Fig.~\\ref{fig:audit}).  They are not spread evenly: nine of the\n"
        "twenty-one are hydrides, and half of the nineteen hydride records in the\n"
        "sample are among them.  Reading those records\n"
        "by hand confirms the judge: YH$_6$ is recorded at 212~K from a\n"
        "sentence that reads ``we find $T_c$ to be 212~K for YH$_6$ and 240~K for\n"
        "YH$_9$'', which is a computed value from a first-principles study.",
    ),
    # Table II.
    (
        "Model & value is in the text & 95.2\\% & 90.4--97.7\\%\\\\\n"
        "Model & text is about the material & 91.1\\% & 85.4--94.7\\%\\\\\n"
        "Model & both & 88.4\\% & 82.1--92.6\\%\\\\\n"
        "Model & value is a measurement & 81.5\\% & 74.4--87.0\\%\\\\\n"
        "Expert & record is correct & 96.0\\% & 90.2--98.4\\%\\\\\n"
        "\\midrule\n"
        "Both & model accepts, of 97 shared & 87.6\\% & 79.6--92.8\\%\\\\\n"
        "Both & expert accepts, of 97 shared & 95.9\\% & 89.9--98.4\\%\\\\",
        "Model & value is in the text & 96.0\\% & 91.5--98.1\\%\\\\\n"
        "Model & text is about the material & 86.6\\% & 80.2--91.1\\%\\\\\n"
        "Model & both & 85.2\\% & 78.7--90.0\\%\\\\\n"
        "Model & value is a measurement & 83.2\\% & 76.4--88.4\\%\\\\\n"
        "Expert & record is correct & 96.0\\% & 90.2--98.4\\%\\\\\n"
        "\\midrule\n"
        "Both & model accepts, of 99 shared & 83.8\\% & 75.3--89.8\\%\\\\\n"
        "Both & expert accepts, of 99 shared & 96.0\\% & 90.1--98.4\\%\\\\",
    ),
    # The head-to-head in the expert section.
    (
        "Of the 100 records, 96 were judged correct: 96.0\\% (95\\% Wilson\n"
        "interval 90.2--98.4\\%).  On the 97 of them the machine also judged,\n"
        "the machine accepted 85 (87.6\\%, 79.6--92.8\\%) and the expert 93\n"
        "(95.9\\%, 89.9--98.4\\%).",
        "Of the 100 records, 96 were judged correct: 96.0\\% (95\\% Wilson\n"
        "interval 90.2--98.4\\%).  On the 99 of them the machine also judged,\n"
        "the machine accepted 83 (83.8\\%, 75.3--89.8\\%); the expert rejected\n"
        "four records in all, so on those ninety-nine it accepted at least 95\n"
        "(96.0\\%, 90.1--98.4\\%).",
    ),
    # The sample's hard cases.
    (
        "at once, and both are reported below: the hard\n"
        "records agree with the judge less often than the rest (80.0\\% against\n"
        "91.0\\%), and they are less often calculations (8.6\\% against 16.2\\%).",
        "at once, and both are reported below: the hard\n"
        "records agree with the judge less often than the rest (76.3\\% against\n"
        "88.3\\%), and they are less often calculations (5.3\\% against 17.1\\%).",
    ),
    # Figure 4's caption.
    (
        "against the text being about the material; the judge confirmed both in\n"
        "129 of the 146 records it judged.",
        "against the text being about the material; the judge confirmed both in\n"
        "127 of the 149 records it judged.",
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
