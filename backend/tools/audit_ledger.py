"""The audit's exact distribution, for the paper's tables.

Everything the manuscript says about the audit is computed here:
the confusion matrix, the undecided records, and the head-to-head
comparison with the expert.

Usage::

    python tools/audit_ledger.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
QWEN = BACKEND / "data" / "cache" / "audit_qwen.jsonl"
SHEET = BACKEND.parent / "paper" / "audit" / "expert_audit.html"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
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
    return f"{100 * successes / total:.1f}% [{100 * low:.1f}, {100 * high:.1f}] ({successes}/{total})"


def main() -> int:
    rows = [
        json.loads(line)
        for line in QWEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judged = [r for r in rows if r.get("value") != "error"]
    errors = [r for r in rows if r.get("value") == "error"]

    print(f"抽样 {len(rows)} 条；判出 {len(judged)} 条；未判出 {len(errors)} 条")
    print("\n=== 混淆矩阵（行=值在文中，列=文本关于该材料） ===")
    labels = ["yes", "no", "?"]
    for value in labels:
        line = []
        for material in labels:
            line.append(
                sum(
                    1
                    for r in judged
                    if (r["value"] if r["value"] in labels else "?") == value
                    and (r["material"] if r["material"] in labels else "?") == material
                )
            )
        print(f"  value={value:4} -> material " + "  ".join(f"{n:3}" for n in line))

    print("\n=== 未判出（error）===")
    for r in errors:
        print(f"  {r.get('composition', '?')}  Tc={r.get('tc')}  [{r.get('family')}]")
        print(f"     {str(r.get('reason', ''))[:160]}")

    print("\n=== 判为 unsure 的记录 ===")
    for r in judged:
        if r["value"] not in labels or r["material"] not in labels:
            print(
                f"  {r.get('composition', '?'):22} Tc={r.get('tc'):<8} "
                f"value={r.get('value')} material={r.get('material')} kind={r.get('kind')}"
            )

    print("\n=== 三个口径 ===")
    value_yes = sum(1 for r in judged if r["value"] == "yes")
    material_yes = sum(1 for r in judged if r["material"] == "yes")
    both = sum(1 for r in judged if r["value"] == "yes" and r["material"] == "yes")
    measurement = sum(1 for r in judged if r["kind"] == "measurement")
    calculation = sum(1 for r in judged if r["kind"] == "calculation")
    unclear = sum(1 for r in judged if r["kind"] == "unclear")
    total = len(judged)
    print(f"  值在文中        : {rate(value_yes, total)}")
    print(f"  材料绑定        : {rate(material_yes, total)}")
    print(f"  两者都对        : {rate(both, total)}")
    print(f"  是测量值        : {rate(measurement, total)}")
    print(f"  是计算值        : {calculation}/{total}")
    print(f"  无法判定        : {unclear}/{total}")

    print("\n=== 难例 / 其余 ===")
    for name, group in (
        ("难例", [r for r in judged if r.get("hard_case")]),
        ("其余", [r for r in judged if not r.get("hard_case")]),
    ):
        b = sum(1 for r in group if r["value"] == "yes" and r["material"] == "yes")
        c = sum(1 for r in group if r["kind"] == "calculation")
        print(f"  {name} n={len(group)}: 两者都对 {rate(b, len(group))} | 计算 {c} ({100 * c / len(group):.1f}%)")

    print("\n=== 水合物 ===")
    hyd = [r for r in judged if "hydride" in (r.get("family") or "").lower()]
    hyd_calc = sum(1 for r in hyd if r["kind"] == "calculation")
    print(f"  水合物 {len(hyd)} 条，其中计算值 {hyd_calc} 条")

    print("\n=== 与专家的重叠 ===")
    sheet = SHEET.read_text(encoding="utf-8")
    import re

    ids = list(dict.fromkeys(re.findall(r'"id":\s*"([0-9a-f-]{36})"', sheet)))
    by_id = {r["id"]: r for r in judged}
    shared = [by_id[i] for i in ids if i in by_id]
    machine_accept = sum(
        1 for r in shared if r["value"] == "yes" and r["material"] == "yes"
    )
    print(f"  专家卷 {len(ids)} 条，机器判过 {len(shared)} 条")
    print(f"  机器接受（值与材料都对）: {rate(machine_accept, len(shared))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
