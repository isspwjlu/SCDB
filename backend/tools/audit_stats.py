"""The audit numbers, as the paper quotes them.

The expert audit is reported by the domain expert on the sheet; the
machine audit is read from its verdicts.  This prints the rates and
the Wilson intervals, so that the numbers in the paper come from the
files rather than from a recollection.

Usage::

    python tools/audit_stats.py [--expert-correct 96] [--expert-total 100]
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
    parser.add_argument("--expert-correct", type=int, default=0)
    parser.add_argument("--expert-total", type=int, default=0)
    parser.add_argument("--expert-csv", default="")
    args = parser.parse_args()

    qwen = [
        json.loads(line)
        for line in QWEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judged = [r for r in qwen if r.get("value") != "error"]
    total = len(judged)
    value_yes = sum(1 for r in judged if r["value"] == "yes")
    material_yes = sum(1 for r in judged if r["material"] == "yes")
    measurement = sum(1 for r in judged if r["kind"] == "measurement")
    calculation = sum(1 for r in judged if r["kind"] == "calculation")
    print(f"独立模型审计：{len(judged)} 条判出（{len(qwen) - len(judged)} 条未判出）")
    print(f"  值在文中且属于该材料 : {rate(value_yes, total)}  ({value_yes}/{total})")
    print(f"  材料绑定确认          : {rate(material_yes, total)}  ({material_yes}/{total})")
    print(f"  是测量值              : {rate(measurement, total)}  ({measurement}/{total})")
    print(f"  是计算值              : {calculation}/{total}")

    if args.expert_total:
        print(
            f"\n专家审计：{args.expert_correct}/{args.expert_total} 判为正确  "
            f"{rate(args.expert_correct, args.expert_total)}"
        )

    if args.expert_csv:
        path = Path(args.expert_csv)
        if not path.exists():
            print(f"\n专家抽检还没有结果：{path}")
            return 0
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        done = [r for r in rows if (r.get("verdict") or "").strip()]
        print(f"\n专家抽检 CSV：{len(done)}/{len(rows)} 条已判")
        print("  判定分布:", dict(collections.Counter(r["verdict"] for r in done)))
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
