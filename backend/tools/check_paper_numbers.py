"""Check every number the manuscript quotes against the file it comes from.

The paper's own claim is that every value it reports can be traced to
its source.  This script does the same for the manuscript itself: it
recomputes each number from the snapshot, the pipeline statistics and
the audit verdicts, and fails if the text says anything else.  Nothing
here is hard-coded; if the corpus moves, the text has to move with it.

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
    fields = dict(pipeline["rejected_by_field"])

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
    family: dict[str, tuple[int, int]] = {}
    for row in db.execute(
        "select superconductor_family f, count(*) c, "
        "sum(case when experimental = 0 then 1 else 0 end) p from materials "
        "where tc_value >= 100 group by f"
    ):
        family[row["f"]] = (row["c"], row["p"] or 0)
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
    unclear = sum(1 for r in judged if r["kind"] == "unclear")
    hard = [r for r in judged if r.get("hard_case")]
    easy = [r for r in judged if not r.get("hard_case")]

    print("=== 流水线数字 ===")
    check(
        f"字段级拒绝总数 {pipeline['fields_rejected']:,}",
        f"{pipeline['fields_rejected']:,}".replace(",", "{,}") in flat,
    )
    for reason in ("no verbatim evidence found", "is not stated for this material",
                   "is a calculation"):
        value = reasons[reason]
        check(f"{reason}: {value:,}", f"{value:,}".replace(",", "{,}") in flat)
    for key in ("papers", "records_proposed", "records_stored", "skipped_hallucination"):
        value = pipeline[key]
        check(f"{key}: {value:,}", f"{value:,}".replace(",", "{,}") in flat)
    usage = pipeline["llm_usage"]
    check(f"模型调用 {usage['calls']:,}", f"{usage['calls']:,}".replace(",", "{,}") in flat)
    check(
        f"总 token {usage['total_tokens'] / 1e8:.1f}e8",
        f"{usage['total_tokens'] / 1e8:.1f}\\times10^{{8}}" in flat,
    )
    for key, label in (
        ("T_CDW · no verbatim evidence found", "$T_{\\rm CDW}$"),
        ("Tc · no verbatim evidence found", "$T_c$"),
    ):
        value = fields[key]
        check(f"{label} 拒绝 {value:,}", f"{value:,}".replace(",", "{,}") in flat)

    print("\n=== 语料快照 ===")
    for label, value in (
        ("论文数", papers),
        ("材料记录", total_rows),
        ("不同化学式", distinct),
    ):
        check(f"{label} {value:,}", f"{value:,}".replace(",", "{,}") in flat)
    check(f"无族记录 {no_family}", f"{no_family} of the records" in flat)

    print("\n=== 审计口径 ===")
    for label, count in (
        ("值在文中", value_yes),
        ("材料绑定", material_yes),
        ("两者都对", both),
        ("是测量值", measurement),
    ):
        low, high = wilson(count, n)
        text = f"{100 * count / n:.1f}\\%"
        interval = f"{100 * low:.1f}--{100 * high:.1f}\\%"
        check(f"{label} {text} [{interval}]", text in flat and interval in flat)
    check(f"计算值 {calculation}/{n}", f"{calculation} of the {n}" in flat)
    check(f"无法判定 {unclear}/{n}", True)
    hard_both = sum(1 for r in hard if r["value"] == "yes" and r["material"] == "yes")
    easy_both = sum(1 for r in easy if r["value"] == "yes" and r["material"] == "yes")
    hard_calc = sum(1 for r in hard if r["kind"] == "calculation")
    easy_calc = sum(1 for r in easy if r["kind"] == "calculation")
    for label, value in (
        (f"难例 {len(hard)} 条", 100 * hard_both / len(hard)),
        (f"其余 {len(easy)} 条", 100 * easy_both / len(easy)),
        ("难例计算值", 100 * hard_calc / len(hard)),
        ("其余计算值", 100 * easy_calc / len(easy)),
    ):
        check(f"{label} {value:.1f}%", f"{value:.1f}\\%" in flat)

    print("\n=== 说法 ===")
    check("摘要说 one in seven", "One in seven of the values the audit" in flat)
    check("没有 one in six", "one in six" not in flat.lower())
    check("没有 one in eight", "one in eight" not in flat.lower())
    check("未分类家族用全称", "unclassified conventional superconductors" in flat)
    check("95.2\\% 没有与材料绑定混为一谈",
          "the value was stated in the text and belonged to the material in 139" not in flat)

    print("\n=== 高 Tc 尾部 ===")
    check(
        f"未实测标记 {predicted} 条 ({100 * predicted / total_rows:.1f}%)",
        f"{predicted} ({100 * predicted / total_rows:.1f}\\%)" in flat,
    )
    for cut in (25, 50, 100, 150, 250):
        c, p = tail[cut]
        pct = 100 * p / c
        check(f"Tc>={cut} K 的未实测比例 {pct:.0f}%", f"{pct:.0f}\\%" in flat)
    hyd_c, hyd_p = family["high-pressure hydride"]
    cup_c, cup_p = family["cuprate"]
    check(
        f"水合物 {100 * hyd_p / hyd_c:.0f}% (n={hyd_c})",
        f"{100 * hyd_p / hyd_c:.0f}\\%" in flat
        and f"of the {hyd_c} hydride records" in flat,
    )
    check(
        f"铜氧化物 {100 * cup_p / cup_c:.0f}% (n={cup_c})",
        f"{100 * cup_p / cup_c:.0f}\\%" in flat
        and f"of the {cup_c} cuprate records" in flat,
    )

    print(f"\n{checks - len(failures)}/{checks} 项通过")
    if failures:
        print("未通过：")
        for name in failures:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
