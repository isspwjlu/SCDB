"""The audit's numbers, in the form the paper quotes them.

Two audits are run on the same stratified sample: one by an
independent model, one by domain experts.  This reads both verdicts and
prints the counts, the rates and the confidence intervals, so that the
numbers in the paper come from the files rather than from a
recollection.

Usage::

    python tools/audit_stats.py [--expert paper/audit/expert_audit_verdicts.csv]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import math
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
QWEN = BACKEND / "data" / "cache" / "audit_qwen.jsonl"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """A Wilson score interval for a binomial proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    ) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def rate(successes: int, total: int) -> str:
    low, high = wilson(successes, total)
    return f"{100 * successes / total:.1f}% [{100 * low:.1f}, {100 * high:.1f}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expert", default="")
    args = parser.parse_args()

    qwen = [json.loads(line) for line in QWEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    judged = [r for r in qwen if r["value"] != "error"]
    print(f"独立模型审计：{len(judged)} 条（{len(qwen) - len(judged)} 条未判出）")

    total = len(judged)
    value_yes = sum(1 for r in judged if r["value"] == "yes")
    value_no = sum(1 for r in judged if r["value"] == "no")
    material_yes = sum(1 for r in judged if r["material"] == "yes")
    measurement = sum(1 for r in judged if r["kind"] == "measurement")
    calculation = sum(1 for r in judged if r["kind"] == "calculation")
    print(f"  值在文中且属于该材料 : {rate(value_yes, total)}  ({value_yes}/{total})")
    print(f"  材料对不上            : {value_no}/{total}")
    print(f"  材料绑定确认          : {rate(material_yes, total)}  ({material_yes}/{total})")
    print(f"  是测量值              : {rate(measurement, total)}  ({measurement}/{total})")
    print(f"  是计算值              : {calculation}/{total}")
    by_family = collections.Counter(r["family"] for r in judged)
    print("  按家族:", dict(by_family))
    hard = [r for r in judged if r.get("hard_case")]
    hard_calc = sum(1 for r in hard if r["kind"] == "calculation")
    print(f"  难例 {len(hard)} 条，其中被判计算值 {hard_calc} 条")
    bands = collections.Counter(r["band"] for r in judged)
    print("  按 Tc 段:", dict(bands))

    if args.expert:
        path = Path(args.expert)
        if not path.exists():
            print(f"\n专家抽检还没有结果：{path}")
            return 0
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        done = [r for r in rows if (r.get("verdict") or "").strip()]
        print(f"\n专家抽检：{len(done)}/{len(rows)} 条已判")
        counts = collections.Counter(r["verdict"] for r in done)
        print("  判定分布:", dict(counts))
        correct = counts.get("correct", 0)
        print(f"  正确率    : {rate(correct, len(done))}")
        # The overlap with the machine audit.
        by_id = {r["id"]: r for r in judged}
        shared = [r for r in done if r["id"] in by_id]
        agree = sum(
            1
            for r in shared
            if (r["verdict"] == "correct")
            == (by_id[r["id"]]["value"] == "yes" and by_id[r["id"]]["material"] == "yes")
        )
        if shared:
            print(f"  与机器审计重叠 {len(shared)} 条，一致 {agree} 条 → {rate(agree, len(shared))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
