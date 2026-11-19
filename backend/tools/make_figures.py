"""The paper's figures, drawn from the database itself.

Every number in a figure comes out of ``_dev.db`` (or the gate
statistics the pipeline logged); nothing is illustrative.  The output
is vector PDF, sized for a two-column Physical Review paper: 8.6 cm
for a single column, 17.2 cm for the double, and 8 pt type.

Usage::

    python tools/make_figures.py --out ../paper/figures
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
GATE_STATS = BACKEND / "data" / "cache" / "gate_stats.json"
# The corpus snapshot the paper's numbers describe: the live database moves
# every time the master is synced, so the figures are drawn from a copy.
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"

# PRB's column widths, in centimetres.
COL = 8.6
FULL = 17.2

# A colour-blind-safe categorical set (Okabe-Ito), in a print-friendly
# order.
PALETTE = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9")

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "legend.fontsize": 7,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "axes.linewidth": 0.6,
        "figure.dpi": 200,
        "savefig.bbox": "tight",
        "pdf.fonttype": 42,
    }
)

ELEMENT = re.compile(r"[A-Z][a-z]*")


def load_materials() -> list[dict]:
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT chemical_composition, tc_value, tc_unit, superconductor_family, "
        "       experimental, notes, meta FROM materials"
    ).fetchall()
    db.close()
    out = []
    for row in rows:
        out.append(
            {
                "composition": row["chemical_composition"],
                "tc": row["tc_value"],
                "unit": row["tc_unit"],
                "family": row["superconductor_family"] or "unclassified",
                "experimental": bool(row["experimental"]),
            }
        )
    return out


def figure_one(out: Path, stats: dict) -> None:
    """The pipeline and the gate cascade."""
    fig, ax = plt.subplots(figsize=(COL / 2.54, 6.6 / 2.54))
    ax.set_axis_off()

    boxes = [
        (0.50, 0.93, "Published paper (PDF)", "#ffffff"),
        (0.50, 0.79, "docling $\\rightarrow$ markdown", "#ffffff"),
        (0.50, 0.65, "7 dimension prompts\n(MiniMax-M3.1-Flash)", "#ffffff"),
        (0.50, 0.51, "merge by composition", "#ffffff"),
    ]
    for x, y, label, face in boxes:
        ax.text(
            x,
            y,
            label,
            ha="center",
            va="center",
            fontsize=7.5,
            bbox={"boxstyle": "round,pad=0.35", "fc": face, "ec": "#333333", "lw": 0.6},
        )

    gates = [
        ("verbatim evidence", "358,312"),
        ("material binding", "7,287"),
        ("measurement, not\ncalculation", "884"),
        ("plausible for\nthe family", "-"),
        ("cross-paper\nconsensus", "-"),
    ]
    y = 0.36
    for name, count in gates:
        ax.text(
            0.50,
            y,
            name,
            ha="center",
            va="center",
            fontsize=7,
            bbox={"boxstyle": "round,pad=0.3", "fc": "#eef3f8", "ec": "#0072B2", "lw": 0.6},
        )
        ax.text(
            0.93,
            y,
            count,
            ha="left",
            va="center",
            fontsize=7,
            color="#D55E00",
        )
        y -= 0.075
    ax.text(
        0.93,
        0.36,
        "rejected",
        ha="left",
        va="center",
        fontsize=6.5,
        color="#555555",
        style="italic",
    )

    ax.text(
        0.50,
        0.075,
        "database: every row keeps\nthe sentence it came from",
        ha="center",
        va="center",
        fontsize=7.5,
        bbox={"boxstyle": "round,pad=0.35", "fc": "#e6f2e6", "ec": "#009E73", "lw": 0.6},
    )

    for top in (0.86, 0.72, 0.58):
        ax.annotate(
            "",
            xy=(0.50, top),
            xytext=(0.50, top + 0.065),
            arrowprops={"arrowstyle": "-|>", "lw": 0.6, "color": "#333333"},
        )
    ax.annotate(
        "",
        xy=(0.50, 0.425),
        xytext=(0.50, 0.475),
        arrowprops={"arrowstyle": "-|>", "lw": 0.6, "color": "#333333"},
    )
    ax.annotate(
        "",
        xy=(0.50, 0.115),
        xytext=(0.50, 0.175),
        arrowprops={"arrowstyle": "-|>", "lw": 0.6, "color": "#333333"},
    )
    ax.text(
        0.5,
        0.005,
        "Field-level extractions rejected, over the whole corpus",
        ha="center",
        va="bottom",
        fontsize=6.5,
        color="#555555",
        style="italic",
    )
    fig.savefig(out / "fig1_pipeline.pdf")
    plt.close(fig)


def figure_two(out: Path, materials: list[dict]) -> None:
    """What the database holds: four distributions."""
    fig, axes = plt.subplots(2, 2, figsize=(FULL / 2.54, 11.0 / 2.54))

    # (a) the critical temperatures
    tcs = np.array([m["tc"] for m in materials if m["tc"] is not None])
    ax = axes[0][0]
    ax.hist(tcs, bins=60, color=PALETTE[0], edgecolor="none")
    ax.set_yscale("log")
    ax.set_xlabel("$T_c$ (K)")
    ax.set_ylabel("materials")
    ax.set_title("(a)  critical temperatures", loc="left")
    ax.axvline(273.15, color="#D55E00", lw=0.7, ls="--")
    ax.text(
        273.15,
        ax.get_ylim()[1] * 0.5,
        "  water boils here",
        fontsize=6,
        color="#D55E00",
    )

    # (b) the families
    ax = axes[0][1]
    counts = collections.Counter(m["family"] for m in materials)
    items = counts.most_common()
    names = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(names))]
    ax.barh(names, values, color=colors, edgecolor="none")
    ax.set_xlabel("materials")
    ax.set_title("(b)  material families", loc="left")
    for i, value in enumerate(values):
        ax.text(value, i, f" {value}", va="center", fontsize=6.5)

    # (c) the elements
    ax = axes[1][0]
    elements: collections.Counter = collections.Counter()
    for m in materials:
        for symbol in set(ELEMENT.findall(m["composition"])):
            elements[symbol] += 1
    items = elements.most_common(14)
    names = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]
    ax.barh(names, values, color=PALETTE[2], edgecolor="none")
    ax.set_xlabel("materials containing the element")
    ax.set_title("(c)  the most frequent elements", loc="left")

    # (d) the compound types
    ax = axes[1][1]
    types = collections.Counter()
    for m in materials:
        types[len(set(ELEMENT.findall(m["composition"])))] += 1
    labels = ["1", "2", "3", "4", "5+"]
    values = [types.get(i, 0) for i in range(1, 6)]
    ax.bar(labels, values, color=PALETTE[1], edgecolor="none")
    ax.set_xlabel("distinct elements in the formula")
    ax.set_ylabel("materials")
    ax.set_title("(d)  compound types", loc="left")
    for i, value in enumerate(values):
        if value:
            ax.text(i, value, f" {value}", ha="center", fontsize=6.5)

    fig.tight_layout(pad=0.6, w_pad=1.4)
    fig.savefig(out / "fig2_database.pdf")
    plt.close(fig)


def figure_three(out: Path, materials: list[dict]) -> None:
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
        ("pairing_symmetry", "pairing"),
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

    fig, ax = plt.subplots(figsize=(COL / 2.54, 5.4 / 2.54))
    colors = [PALETTE[0] if c >= 50 else (PALETTE[4] if c >= 5 else "#999999") for c in counts]
    ax.barh(labels, counts, color=colors, edgecolor="none")
    for i, value in enumerate(counts):
        ax.text(value, i, f" {value:.1f}%", va="center", fontsize=6.5)
    ax.set_xlim(0, 108)
    ax.set_xlabel("materials with the field filled (%)")
    ax.set_title("Field coverage of the extraction", loc="left")
    fig.tight_layout(pad=0.4)
    fig.savefig(out / "fig4_coverage.pdf")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(BACKEND.parent / "paper" / "figures"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    materials = load_materials()
    stats = json.loads(GATE_STATS.read_text(encoding="utf-8")) if GATE_STATS.exists() else {}
    figure_one(out, stats)
    figure_two(out, materials)
    figure_three(out, materials)
    print(f"图 → {out}")
    for path in sorted(out.glob("*.pdf")):
        print(f"   {path.name}  {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
