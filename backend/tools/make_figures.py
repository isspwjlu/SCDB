"""The paper's figures, drawn from the frozen corpus snapshot.

The workflow is the one the scipilot-figure-skill prescribes: the data
are profiled, the chart type is chosen to fit the argument each panel
makes, the journal's own specifications are applied, the result is
checked before it is exported, and the export is vector.

Every number in a figure comes out of the snapshot the paper's numbers
describe; nothing is illustrative.

Usage::

    python tools/make_figures.py --out ../paper/figures
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

SKILL = Path.home() / ".claude" / "skills" / "scipilot-figure-skill" / "scripts"
sys.path.insert(0, str(SKILL))

from export_figure import export_figure  # noqa: E402
from setup_style import setup_style  # noqa: E402
from visual_qa import audit_layout, print_report  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
GATE_STATS = BACKEND / "data" / "cache" / "pipeline_stats.json"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
AUDIT = BACKEND / "data" / "cache" / "audit_qwen.jsonl"

# Physical Review B: one column is 8.6 cm, two columns 17.2 cm, the
# type is sans-serif and never smaller than 6 pt, and the artwork is
# vector.  The figures are drawn at exactly these widths and included
# at exactly these widths, so nothing is scaled on the way in.
COL_CM = 8.6
FULL_CM = 17.2

# Okabe-Ito, the colour-blind-safe set.  One accent for the quantity
# being argued about, one neutral for the rest.
BLUE = "#0072B2"
ORANGE = "#D55E00"
GREEN = "#009E73"
GREY = "#7F7F7F"
LIGHT = "#BFBFBF"

APS = {
    "figure.dpi": 200,
    "savefig.dpi": 600,
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "legend.fontsize": 7,
    "lines.linewidth": 1.0,
    "lines.markersize": 4,
    "axes.linewidth": 0.6,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.unicode_minus": False,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}

ELEMENT = re.compile(r"[A-Z][a-z]*")
UNCLASSIFIED = "unclassified conventional\nsuperconductor"


def apply_style() -> None:
    """The skill's presets, then the journal's own numbers."""
    setup_style(journal="general", lang="en")
    plt.rcParams.update(APS)


def load_materials() -> list[dict]:
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT chemical_composition, tc_value, superconductor_family, experimental "
        "FROM materials"
    ).fetchall()
    db.close()
    return [
        {
            "composition": row["chemical_composition"],
            "tc": row["tc_value"],
            "family": row["superconductor_family"] or UNCLASSIFIED,
            "experimental": bool(row["experimental"]),
        }
        for row in rows
    ]


def gate_count(stats: dict, reason: str) -> str:
    counts = stats.get("rejected_by_reason", {})
    value = counts.get(reason)
    return f"{value:,}" if isinstance(value, int) else "--"


def check(fig, name: str) -> None:
    """The machine half of the visual self-check."""
    issues = audit_layout(fig)
    if issues:
        print(f"\n[{name}]")
        print_report(issues)
    else:
        print(f"[{name}]  程序自检通过")


def figure_database(out: Path, materials: list[dict]) -> None:
    """What the corpus holds: four distributions.

    The argument of the figure is that the corpus is broad and
    familiar, and that its high-temperature tail is a mixture of two
    kinds of statement.
    """
    fig, axes = plt.subplots(
        2, 2, figsize=(FULL_CM / 2.54, 12.0 / 2.54), constrained_layout=True
    )

    # (a) the critical temperatures, split by the extraction's own flag
    measured = np.array(
        [m["tc"] for m in materials if m["tc"] is not None and m["experimental"]]
    )
    predicted = np.array(
        [m["tc"] for m in materials if m["tc"] is not None and not m["experimental"]]
    )
    ax = axes[0][0]
    bins = np.linspace(0, 300, 61)
    ax.hist(measured, bins=bins, color=BLUE, edgecolor="none",
            label="reported as measured")
    ax.hist(predicted, bins=bins, color=ORANGE, edgecolor="none",
            label="reported as predicted")
    ax.set_yscale("log")
    ax.set_xlabel("$T_c$ (K)")
    ax.set_ylabel("records")
    ax.set_title("(a)  transition temperatures", loc="left")
    ax.axvline(273.15, color="#333333", lw=0.6, ls=(0, (4, 2)))
    ax.legend(frameon=False, loc="upper right", fontsize=6, handlelength=1.3)

    # (b) the families: one colour, because the category is on the axis
    ax = axes[0][1]
    counts = collections.Counter(m["family"] for m in materials)
    items = counts.most_common()
    names = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]
    ax.barh(names, values, color=BLUE, edgecolor="none", height=0.72)
    ax.set_xlabel("records")
    ax.set_title("(b)  material families", loc="left")
    for i, value in enumerate(values):
        ax.text(value, i, f" {value}", va="center", fontsize=6.5)
    ax.set_xlim(0, max(values) * 1.14)
    ax.tick_params(axis="y", labelsize=6.5)

    # (c) the elements
    ax = axes[1][0]
    elements: collections.Counter = collections.Counter()
    for m in materials:
        for symbol in set(ELEMENT.findall(m["composition"])):
            elements[symbol] += 1
    items = elements.most_common(14)
    names = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]
    ax.barh(names, values, color=GREEN, edgecolor="none", height=0.72)
    ax.set_xlabel("records containing the element")
    ax.set_title("(c)  the most frequent elements", loc="left")
    ax.set_xlim(0, max(values) * 1.16)

    # (d) the compound types
    ax = axes[1][1]
    types = collections.Counter(
        len(set(ELEMENT.findall(m["composition"]))) for m in materials
    )
    labels = ["1", "2", "3", "4", "5+"]
    values = [types.get(i, 0) for i in range(1, 6)]
    ax.bar(labels, values, color=ORANGE, edgecolor="none", width=0.62)
    ax.set_xlabel("distinct elements in the formula")
    ax.set_ylabel("records")
    ax.set_title("(d)  compound types", loc="left")
    for i, value in enumerate(values):
        if value:
            ax.text(i, value, f" {value}", ha="center", fontsize=6.5)
    ax.set_ylim(0, max(values) * 1.14)

    check(fig, "fig2")
    export_figure(
        fig,
        basename=str(out / "fig2_database"),
        formats=["pdf", "png"],
        size_inches=(FULL_CM / 2.54, 12.0 / 2.54),
        tight=False,
        grayscale_preview=True,
    )
    plt.close(fig)


def figure_coverage(out: Path) -> None:
    """How much of each field the extraction actually fills."""
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM materials").fetchall()
    db.close()
    total = len(rows)

    fields = [
        ("tc_value", "$T_c$"),
        ("hc2_value", "$H_{c2}$"),
        ("energy_gap_value", "$\\Delta$"),
        ("coherence_length_value", "$\\xi$"),
        ("penetration_depth_value", "$\\lambda$"),
        ("hc1_value", "$H_{c1}$"),
        ("t_cdw_value", "$T_{\\rm CDW}$"),
        ("critical_current_density_value", "$J_c$"),
        ("pairing_symmetry", "pairing symmetry"),
        ("mechanism", "mechanism"),
    ]
    labels = []
    counts = []
    for column, label in fields:
        have = sum(1 for row in rows if row[column] not in (None, ""))
        labels.append(label)
        counts.append(100.0 * have / total)

    order = np.argsort(counts)
    labels = [labels[i] for i in order]
    counts = [counts[i] for i in order]

    fig, ax = plt.subplots(
        figsize=(COL_CM / 2.54, 5.6 / 2.54), constrained_layout=True
    )
    # One colour for the common property, a neutral for the rest: the
    # two are different in kind, not in amount.
    colors = [BLUE if c >= 50 else GREY for c in counts]
    ax.barh(labels, counts, color=colors, edgecolor="none", height=0.7)
    for i, value in enumerate(counts):
        ax.text(value, i, f" {value:.1f}%", va="center", fontsize=6.5)
    ax.set_xlim(0, 118)
    ax.set_xlabel("records with the field filled (%)")
    ax.set_title("Field coverage of the extraction", loc="left")

    check(fig, "fig3")
    export_figure(
        fig,
        basename=str(out / "fig3_coverage"),
        formats=["pdf", "png"],
        size_inches=(COL_CM / 2.54, 5.6 / 2.54),
        tight=False,
        grayscale_preview=True,
    )
    plt.close(fig)


def load_audit() -> list[dict]:
    if not AUDIT.exists():
        return []
    rows = [
        json.loads(line)
        for line in AUDIT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if r.get("value") != "error"]


def figure_audit(out: Path) -> None:
    """What the independent audit found."""
    rows = load_audit()
    if not rows:
        return

    labels = ["yes", "no", "?"]
    matrix = np.zeros((3, 3))
    for row in rows:
        i = labels.index(row["value"]) if row["value"] in labels else 2
        j = labels.index(row["material"]) if row["material"] in labels else 2
        matrix[i, j] += 1

    fig, (ax, bx, cx) = plt.subplots(
        1,
        3,
        figsize=(FULL_CM / 2.54, 4.6 / 2.54),
        constrained_layout=True,
        gridspec_kw={"width_ratios": [1, 1.05, 1.1]},
    )

    image = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=matrix.max())
    for i in range(3):
        for j in range(3):
            value = int(matrix[i, j])
            if value:
                ax.text(
                    j,
                    i,
                    str(value),
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white" if value > matrix.max() / 2 else "#14141a",
                )
    ax.set_xticks(range(3), ["yes", "no", "?"])
    ax.set_yticks(range(3), ["yes", "no", "?"])
    ax.set_xlabel("the value is in the text")
    ax.set_ylabel("the text is about this material")
    ax.set_title("(a)  the independent audit", loc="left")
    bar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.03)
    bar.set_label("records", fontsize=6.5)
    bar.ax.tick_params(labelsize=6)

    kinds = ["measurement", "calculation", "unclear"]
    counts = [sum(1 for r in rows if r["kind"] == k) for k in kinds]
    bx.bar(["measured", "calculated", "?"], counts, color=[BLUE, ORANGE, LIGHT],
           edgecolor="none", width=0.6)
    for i, value in enumerate(counts):
        bx.text(i, value, f" {value}", ha="center", fontsize=7)
    bx.set_ylabel("records")
    bx.set_title("(b)  what the value is", loc="left")
    bx.set_ylim(0, max(counts) * 1.16)

    hard = [r for r in rows if r.get("hard_case")]
    easy = [r for r in rows if not r.get("hard_case")]
    agree = [
        100.0
        * sum(1 for r in group if r["value"] == "yes" and r["material"] == "yes")
        / len(group)
        for group in (easy, hard)
    ]
    calc = [
        100.0 * sum(1 for r in group if r["kind"] == "calculation") / len(group)
        for group in (easy, hard)
    ]
    x = np.arange(2)
    cx.bar(
        x - 0.19,
        agree,
        width=0.36,
        color=BLUE,
        edgecolor="none",
        label="value and material confirmed",
    )
    cx.bar(
        x + 0.19,
        calc,
        width=0.36,
        color=ORANGE,
        edgecolor="none",
        label="a calculation, not a measurement",
    )
    for xi, (a_value, c_value) in enumerate(zip(agree, calc)):
        cx.text(xi - 0.19, a_value + 2, f"{a_value:.0f}", ha="center", fontsize=6.5)
        cx.text(xi + 0.19, c_value + 2, f"{c_value:.0f}", ha="center", fontsize=6.5)
    cx.set_xticks(x, [f"not hard\n(n={len(easy)})", f"hard\n(n={len(hard)})"])
    cx.set_ylabel("% of records")
    cx.set_ylim(0, 118)
    cx.set_title("(c)  hard cases against the rest", loc="left")
    cx.legend(frameon=False, fontsize=6, loc="upper center",
              bbox_to_anchor=(0.5, -0.20), handlelength=1.2)

    check(fig, "fig4")
    export_figure(
        fig,
        basename=str(out / "fig4_audit"),
        formats=["pdf", "png"],
        size_inches=(FULL_CM / 2.54, 4.6 / 2.54),
        tight=False,
        grayscale_preview=True,
    )
    plt.close(fig)


def figure_tail(out: Path, materials: list[dict]) -> None:
    """Where the records that are not reported as measured sit."""
    tcs = np.array([m["tc"] for m in materials if m["tc"] is not None])
    predicted = np.array(
        [not m["experimental"] for m in materials if m["tc"] is not None]
    )

    thresholds = [25, 50, 100, 150, 200, 250]
    fractions = []
    counts = []
    for cut in thresholds:
        mask = tcs >= cut
        counts.append(int(mask.sum()))
        fractions.append(100.0 * predicted[mask].sum() / mask.sum())

    fig, (ax, bx) = plt.subplots(
        1,
        2,
        figsize=(FULL_CM / 2.54, 4.4 / 2.54),
        constrained_layout=True,
        gridspec_kw={"width_ratios": [1.15, 1]},
    )

    ax.bar(
        [str(t) for t in thresholds],
        fractions,
        color=ORANGE,
        edgecolor="none",
        width=0.62,
    )
    for i, (y_value, n) in enumerate(zip(fractions, counts)):
        ax.text(i, y_value + 4.4, f"{y_value:.0f}%", ha="center", fontsize=7)
        ax.text(i, y_value + 1.2, f"n={n}", ha="center", fontsize=6, color="#555555")
    ax.set_xlabel("records with $T_c \\geq$ threshold (K)")
    ax.set_ylabel("% not reported as measured")
    ax.set_title("(a)  the flag against the transition temperature", loc="left")
    ax.set_ylim(0, 58)

    by_family: dict[str, list[bool]] = {}
    for material in materials:
        if material["tc"] is None or material["tc"] < 100:
            continue
        by_family.setdefault(material["family"], []).append(
            not material["experimental"]
        )
    items = sorted(
        ((name, values) for name, values in by_family.items() if len(values) >= 10),
        key=lambda item: 100.0 * sum(item[1]) / len(item[1]),
    )
    names = [name for name, _ in items]
    values = [100.0 * sum(v) / len(v) for _, v in items]
    sizes = [len(v) for _, v in items]
    bx.barh(names, values, color=ORANGE, edgecolor="none", height=0.6)
    for i, (value, n) in enumerate(zip(values, sizes)):
        bx.text(value, i, f" {value:.0f}%  (n={n})", va="center", fontsize=6.5)
    bx.set_xlim(0, 100)
    bx.set_xlabel("% not reported as measured")
    bx.set_title("(b)  by family, for $T_c \\geq 100$ K", loc="left")

    check(fig, "fig5")
    export_figure(
        fig,
        basename=str(out / "fig5_tail"),
        formats=["pdf", "png"],
        size_inches=(FULL_CM / 2.54, 4.4 / 2.54),
        tight=False,
        grayscale_preview=True,
    )
    plt.close(fig)


def figure_pipeline(out: Path, stats: dict) -> None:
    """The pipeline and the gate cascade, drawn down the centre."""
    from matplotlib.patches import FancyBboxPatch

    fig, ax = plt.subplots(
        figsize=(COL_CM / 2.54, 11.4 / 2.54), constrained_layout=True
    )
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    def panel(x, y, w, h, face, edge, radius=0.022, lw=0.7):
        patch = FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle=f"round,pad=0,rounding_size={radius}",
            facecolor=face,
            edgecolor=edge,
            linewidth=lw,
            zorder=1,
        )
        ax.add_patch(patch)
        return patch

    def arrow(y_from, y_to):
        ax.annotate(
            "",
            xy=(0.5, y_to),
            xytext=(0.5, y_from),
            arrowprops={
                "arrowstyle": "-|>",
                "lw": 0.7,
                "color": "#444444",
                "shrinkA": 0,
                "shrinkB": 0,
            },
            zorder=2,
        )

    flow = [
        (0.955, 0.052, "Published paper (PDF)", "#ffffff", "#444444"),
        (0.866, 0.052, "docling $\\rightarrow$ markdown", "#ffffff", "#444444"),
        (
            0.755,
            0.090,
            "composition prompt\n+ 7 dimension prompts\n(MiniMax-M3.1-Flash)",
            "#ffffff",
            "#444444",
        ),
        (
            0.645,
            0.072,
            "merge by composition\n$\\rightarrow$ candidate values",
            "#ffffff",
            "#444444",
        ),
    ]
    for y, h, label, face, edge in flow:
        panel(0.5, y, 0.74, h, face, edge)
        ax.text(0.5, y, label, ha="center", va="center", fontsize=7.5,
                color="#14141a", zorder=3, linespacing=1.45)

    arrow(0.929, 0.893)
    arrow(0.840, 0.801)
    arrow(0.710, 0.682)
    arrow(0.609, 0.578)

    # The five checks sit in their own frame, so that the stack reads as
    # a list of parallel checks rather than as a sequence.
    panel(0.5, 0.335, 0.90, 0.480, "#f2f7fb", BLUE, radius=0.024)
    ax.text(
        0.5,
        0.545,
        "five checks, applied to every value",
        ha="center",
        va="center",
        fontsize=7,
        color="#3A3A3A",
        style="italic",
        zorder=3,
    )

    gates = [
        ("verbatim evidence", gate_count(stats, "no verbatim evidence found")),
        ("material binding", gate_count(stats, "is not stated for this material")),
        ("measurement, not calculation", gate_count(stats, "is a calculation")),
        ("plausible for the family", None),
        ("cross-paper consensus", None),
    ]
    y = 0.480
    for name, count in gates:
        panel(0.5, y, 0.72, 0.064, "#ffffff", BLUE, radius=0.016, lw=0.6)
        ax.text(0.5, y + 0.015, name, ha="center", va="center", fontsize=7,
                color="#14141a", zorder=3)
        if count:
            ax.text(
                0.5,
                y - 0.014,
                f"{count} rejected",
                ha="center",
                va="center",
                fontsize=6.5,
                color=ORANGE,
                zorder=3,
            )
        else:
            ax.text(
                0.5,
                y - 0.014,
                "applied to the record",
                ha="center",
                va="center",
                fontsize=6.5,
                color="#7A7A7A",
                style="italic",
                zorder=3,
            )
        y -= 0.076

    arrow(0.093, 0.086)

    panel(0.5, 0.046, 0.74, 0.070, "#e6f2e6", GREEN)
    ax.text(
        0.5,
        0.046,
        "database: every row keeps\nthe sentence it came from",
        ha="center",
        va="center",
        fontsize=7.5,
        color="#14141a",
        zorder=3,
        linespacing=1.45,
    )

    check(fig, "fig1")
    export_figure(
        fig,
        basename=str(out / "fig1_pipeline"),
        formats=["pdf", "png"],
        size_inches=(COL_CM / 2.54, 11.4 / 2.54),
        tight=False,
        grayscale_preview=True,
    )
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(BACKEND.parent / "paper" / "figures"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    apply_style()
    materials = load_materials()
    stats = json.loads(GATE_STATS.read_text(encoding="utf-8")) if GATE_STATS.exists() else {}

    print("程序自检（缺字 / 裁切 / 刻度重叠）：")
    figure_pipeline(out, stats)
    figure_database(out, materials)
    figure_coverage(out)
    figure_audit(out)
    figure_tail(out, materials)

    print(f"\n图 → {out}")
    for path in sorted(out.glob("*.pdf")):
        print(f"   {path.name}  {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
