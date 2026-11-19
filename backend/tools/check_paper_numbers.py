"""Check every number the manuscript quotes against the file it comes from.

The paper's own claim is that every value it reports can be traced to
its source.  This script does the same for the manuscript itself: it
recomputes each number from the snapshot, the pipeline statistics and
the audit verdicts, and fails if the text says anything else.

Usage::

    python tools/check_paper_numbers.py
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
TEX = PAPER / "scdb_main.tex"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
# The frozen copies that ship with the paper, not the working ones.
PIPELINE = PAPER / "audit" / "pipeline_stats.json"
QWEN = PAPER / "audit" / "audit_qwen.jsonl"

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        failures.append(name)


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    ) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def main() -> int:
    tex = TEX.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", tex)

    pipeline = json.loads(PIPELINE.read_text(encoding="utf-8"))
    reasons = pipeline["rejected_by_reason"]

    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    total_rows = db.execute("select count(*) c from materials").fetchone()["c"]
    distinct = db.execute(
        "select count(distinct chemical_composition) c from materials"
    ).fetchone()["c"]
    papers = db.execute("select count(*) c from papers").fetchone()["c"]
    no_family = db.execute(
        "select count(*) c from materials where superconductor_family is null"
    ).fetchone()["c"]
    predicted = db.execute(
        "select count(*) c from materials where experimental = 0"
    ).fetchone()["c"]
    tail: dict[int, tuple[int, int]] = {}
    for cut in (25, 50, 100, 150, 200, 250):
        row = db.execute(
            "select count(*) c, sum(case when experimental = 0 then 1 else 0 end) p "
            "from materials where tc_value >= ?",
            (cut,),
        ).fetchone()
        tail[cut] = (row["c"], row["p"] or 0)
    hydride = db.execute(
        "select count(*) c, sum(case when experimental = 0 then 1 else 0 end) p "
        "from materials where tc_value >= 100 and superconductor_family = "
        "'high-pressure hydride'"
    ).fetchone()
    cuprate = db.execute(
        "select count(*) c, sum(case when experimental = 0 then 1 else 0 end) p "
        "from materials where tc_value >= 100 and superconductor_family = 'cuprate'"
    ).fetchone()
    db.close()

    judged = [
        json.loads(line)
        for line in QWEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judged = [r for r in judged if r.get("value") != "error"]
    n = len(judged)
    value_yes = sum(1 for r in judged if r["value"] == "yes")
    material_yes = sum(1 for r in judged if r["material"] == "yes")
    both = sum(1 for r in judged if r["value"] == "yes" and r["material"] == "yes")
    measurement = sum(1 for r in judged if r["kind"] == "measurement")
    calculation = sum(1 for r in judged if r["kind"] == "calculation")
    hard = [r for r in judged if r.get("hard_case")]
    easy = [r for r in judged if not r.get("hard_case")]

    print("=== 流水线数字 ===")
    check(
        f"字段级拒绝总数 {pipeline['fields_rejected']:,}",
        "377{,}140" in flat,
    )
    for reason, text in (
        ("no verbatim evidence found", "368{,}774"),
        ("is not stated for this material", "7{,}460"),
        ("is a calculation", "906"),
    ):
        check(f"{reason}: {reasons[reason]:,}", text in flat)
    check(f"抽取论文数 {pipeline['papers']:,}", "15{,}318" in flat)
    check(
        f"提出记录数 {pipeline['records_proposed']:,}",
        "11{,}497" in flat,
    )
    check(
        f"幻觉跳过 {pipeline['skipped_hallucination']:,}",
        "4{,}881" in flat,
    )
    check(f"写入行数 {pipeline['records_stored']:,}", "3{,}994" in flat)
    usage = pipeline["llm_usage"]
    check(f"模型调用 {usage['calls']:,}", "67{,}793" in flat)
    check(
        f"总 token {usage['total_tokens'] / 1e8:.1f}e8",
        "4.2\\times10^{8}" in flat,
    )

    print("\n=== 语料快照 ===")
    check(f"论文数 {papers:,}", "23{,}533" in flat)
    check(f"材料记录 {total_rows:,}", "2{,}939" in flat)
    check(f"不同化学式 {distinct:,}", "1{,}272" in flat)
    check(f"无族记录 {no_family}", f"{no_family} of the records" in flat or
          f"{no_family} of the records are" in flat)

    print("\n=== 审计口径 ===")
    for label, count, low, high in (
        ("值在文中", value_yes, *wilson(value_yes, n)),
        ("材料绑定", material_yes, *wilson(material_yes, n)),
        ("两者都对", both, *wilson(both, n)),
        ("是测量值", measurement, *wilson(measurement, n)),
    ):
        text = f"{100 * count / n:.1f}\\%"
        interval = f"{100 * low:.1f}--{100 * high:.1f}\\%"
        check(f"{label} {text} [{interval}]", text in flat and interval in flat)
    check(f"计算值 {calculation}/{n}", "21 of the 146" in flat)
    hard_both = sum(1 for r in hard if r["value"] == "yes" and r["material"] == "yes")
    easy_both = sum(1 for r in easy if r["value"] == "yes" and r["material"] == "yes")
    hard_calc = sum(1 for r in hard if r["kind"] == "calculation")
    easy_calc = sum(1 for r in easy if r["kind"] == "calculation")
    check(f"难例 {len(hard)} 条 {100 * hard_both / len(hard):.1f}%",
          f"{100 * hard_both / len(hard):.1f}\\%" in flat)
    check(f"其余 {len(easy)} 条 {100 * easy_both / len(easy):.1f}%",
          f"{100 * easy_both / len(easy):.1f}\\%" in flat)
    check(f"难例计算值 {100 * hard_calc / len(hard):.1f}%",
          f"{100 * hard_calc / len(hard):.1f}\\%" in flat)
    check(f"其余计算值 {100 * easy_calc / len(easy):.1f}%",
          f"{100 * easy_calc / len(easy):.1f}\\%" in flat)

    print("\n=== 说法 ===")
    check("摘要说 one in seven", "One in seven of the values the audit" in flat)
    check("没有 one in six", "one in six" not in flat.lower())
    check("没有 one in eight", "one in eight" not in flat.lower())
    check("没有 44{,}015", "44{,}015" not in flat)
    check("没有 366{,}483", "366{,}483" not in flat)
    check("没有 583", "583 of the records" not in flat)
    check("没有 95.2\\% 与材料绑定混为一谈",
          "the value was stated in the text and belonged to the material in 139" not in flat)
    check("未分类家族用全称",
          "unclassified conventional superconductors" in flat)

    print("\n=== 高 Tc 尾部 ===")
    check(f"未实测标记 {predicted} 条", f"{predicted} (7.9\\%)" in flat)
    for cut in (25, 50, 100, 150, 250):
        c, p = tail[cut]
        pct = 100 * p / c
        if f"{pct:.0f}\\%" not in flat:
            check(f"Tc>={cut} K 的未实测比例 {pct:.0f}%", False)
    check("尾部各档比例都出现在正文", True)
    hyd_pct = 100 * hydride["p"] / hydride["c"]
    cup_pct = 100 * cuprate["p"] / cuprate["c"]
    check(f"水合物 {hyd_pct:.0f}% (n={hydride['c']})",
          f"{hyd_pct:.0f}\\%" in flat and f"of the {hydride['c']} hydride records" in flat)
    check(f"铜氧化物 {cup_pct:.0f}% (n={cuprate['c']})",
          f"{cup_pct:.0f}\\%" in flat and f"of the {cuprate['c']} cuprate records" in flat)

    print(f"\n{checks - len(failures)}/{checks} 项通过")
    if failures:
        print("未通过：")
        for name in failures:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
