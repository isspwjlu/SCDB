"""Check every number the manuscript quotes about the corpus.

The corpus moves whenever the master extracts more, and a number left
behind in the paper is a number a reader cannot reproduce.  This reads
the manuscript back against the snapshot and the pipeline statistics.

Usage::

    python tools/check_corpus_claims.py
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
TEX = PAPER / "scdb_main.tex"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
STATS = BACKEND / "data" / "cache" / "pipeline_stats.json"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{('  ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    text = TEX.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", text)
    stats = json.loads(STATS.read_text(encoding="utf-8"))

    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    papers = db.execute("select count(*) from papers").fetchone()[0]
    records = db.execute("select count(*) from materials").fetchone()[0]
    compositions = db.execute(
        "select count(distinct chemical_composition) from materials"
    ).fetchone()[0]
    unclassified = db.execute(
        "select count(*) from materials where superconductor_family is null "
        "or superconductor_family=''"
    ).fetchone()[0]
    flagged = db.execute(
        "select count(*) from materials where experimental=0"
    ).fetchone()[0]
    rows = db.execute(
        "select tc_value, experimental from materials where tc_value is not null"
    ).fetchall()
    db.close()

    def family(count: str) -> tuple[int, int]:
        db = sqlite3.connect(SNAPSHOT)
        out = db.execute(
            "select count(*), sum(case when experimental=0 then 1 else 0 end) "
            "from materials where superconductor_family=? and tc_value>=100",
            (count,),
        ).fetchone()
        db.close()
        return out[0], out[1] or 0

    print("=== 语料 ===")
    check(f"论文数 {papers:,}", f"{papers:,}".replace(",", "{,}") in flat)
    check(
        f"材料记录 {records:,}", f"{records:,}".replace(",", "{,}") in flat
    )
    check(
        f"不同化学式 {compositions:,}",
        f"{compositions:,}".replace(",", "{,}") in flat,
    )
    check(
        f"未分类 {unclassified}",
        f"{unclassified} of the records are still" in flat,
    )
    check(
        f"未实测 {flagged} ({100 * flagged / records:.1f}%)",
        f"{flagged} ({100 * flagged / records:.1f}\\%)" in flat,
    )

    print("\n=== 抽取 ===")
    for key, label in (
        ("papers", "抽过的论文"),
        ("records_proposed", "提出的记录"),
        ("skipped_hallucination", "幻觉丢弃"),
        ("records_stored", "入库"),
        ("fields_rejected", "字段级拒绝"),
    ):
        value = stats[key]
        check(
            f"{label} {value:,}",
            f"{value:,}".replace(",", "{,}") in flat,
        )
    survived = stats["records_proposed"] - stats["skipped_hallucination"]
    check(
        f"通过材料过滤 {survived:,}",
        f"{survived:,}".replace(",", "{,}") in flat,
    )
    for key, label in (
        ("no verbatim evidence found", "无逐字证据"),
        ("is not stated for this material", "非本材料"),
        ("is a calculation", "是计算值"),
    ):
        value = stats["rejected_by_reason"][key]
        check(
            f"{label} {value:,}",
            f"{value:,}".replace(",", "{,}") in flat,
        )

    print("\n=== 门与成本 ===")
    for key, count in stats["rejected_by_field"]:
        check(
            f"{key} {count:,}",
            f"{count:,}".replace(",", "{,}") in flat,
        )
    usage = stats["llm_usage"]
    check(f"调用 {usage['calls']:,}", f"{usage['calls']:,}".replace(",", "{,}") in flat)
    total = usage["total_tokens"] / 1e8
    check(
        f"token {total:.1f}e8",
        f"{total:.1f}\\times10^{{8}}" in flat,
    )
    completion = usage["completion"] / 1e8
    check(
        f"完成 token {completion:.1f}e8",
        f"{completion:.1f}\\times10^{{8}}" in flat,
    )
    share = 100 * usage["reasoning"] / usage["completion"]
    check(f"推理占完成 {share:.0f}%", f"{share:.0f}\\%" in flat)

    print("\n=== 尾部 ===")
    for cut in (25, 50, 100, 150, 250):
        group = [r for r in rows if r["tc_value"] >= cut]
        pct = 100 * sum(1 for r in group if not r["experimental"]) / len(group)
        check(f"Tc>={cut} {pct:.0f}%", f"{pct:.0f}\\%" in flat)
    hydride_n, hydride_p = family("high-pressure hydride")
    cuprate_n, cuprate_p = family("cuprate")
    check(
        f"水合物 {hydride_n} 条 {hydride_p} ({100 * hydride_p / hydride_n:.0f}%)",
        f"of the {hydride_n} hydride records above" in flat
        and f"({100 * hydride_p / hydride_n:.0f}\\%)" in flat,
    )
    check(
        f"铜氧化物 {cuprate_n} 条 {cuprate_p} ({100 * cuprate_p / cuprate_n:.0f}%)",
        f"of the {cuprate_n} cuprate records above" in flat
        and f"{cuprate_p} ({100 * cuprate_p / cuprate_n:.0f}\%)" in flat,
    )
    top = sorted(rows, key=lambda r: -r["tc_value"])[:5]
    computed = sum(1 for r in top if not r["experimental"])
    words = ("zero", "one", "two", "three", "four", "five")
    check(
        f"最高的五条里 {computed} 条是计算值",
        f"{words[computed]} of the five records above" in flat.lower(),
    )
    above = sum(1 for r in rows if r["tc_value"] > 290)
    check(
        f"290 K 以上 {above} 条",
        f"records above 290~K" in flat and above == 5,
    )

    print()
    if failures:
        print(f"!! {len(failures)} 处对不上：")
        for name in failures:
            print(f"   {name}")
        return 1
    print("论文里关于语料与抽取的每一个数字都与数据一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
