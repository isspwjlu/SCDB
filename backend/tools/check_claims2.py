"""Second pass on the claims: the exact audit rates, and the
provenance of the highest-Tc rows."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
QWEN = BACKEND / "data" / "cache" / "audit_qwen.jsonl"


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
    total = len(judged)

    value_yes = sum(1 for r in judged if r["value"] == "yes")
    material_yes = sum(1 for r in judged if r["material"] == "yes")
    both_yes = sum(
        1 for r in judged if r["value"] == "yes" and r["material"] == "yes"
    )
    kinds: dict[str, int] = {}
    for r in judged:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("=== 审计的三个不同口径 ===")
    print(f"  值在文中            : {rate(value_yes, total)}")
    print(f"  材料绑定确认        : {rate(material_yes, total)}")
    print(f"  值对 且 材料对      : {rate(both_yes, total)}")
    print(f"  判定分布            : {kinds}")
    print(f"  计算值              : {kinds.get('calculation', 0)}/{total}"
          f" = {100 * kinds.get('calculation', 0) / total:.1f}%  (约 1/{total / max(1, kinds.get('calculation', 0)):.1f})")
    print(f"  非测量(计算+unclear): {kinds.get('calculation', 0) + kinds.get('unclear', 0)}/{total}"
          f" = {100 * (kinds.get('calculation', 0) + kinds.get('unclear', 0)) / total:.1f}%")

    print("\n=== 难例 / 其余 ===")
    for label, group in (
        ("全部", judged),
        ("难例", [r for r in judged if r.get("hard_case")]),
        ("其余", [r for r in judged if not r.get("hard_case")]),
    ):
        n = len(group)
        if not n:
            continue
        b = sum(1 for r in group if r["value"] == "yes" and r["material"] == "yes")
        c = sum(1 for r in group if r["kind"] == "calculation")
        hyd = sum(1 for r in group if "hydride" in (r.get("family") or "").lower())
        print(f"  {label:4} n={n:3} | 值与材料都对 {rate(b, n)} | 计算 {c} ({100 * c / n:.1f}%) | 水合物 {hyd}")

    print("\n=== 最高 Tc 行的证据与 experimental 标记 ===")
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    top = db.execute(
        "select chemical_composition, tc_value, tc_unit, doi, experimental, source, notes "
        "from materials where tc_value is not null order by tc_value desc limit 8"
    ).fetchall()
    for row in top:
        print(f"\n  {row['chemical_composition']:20} {row['tc_value']} {row['tc_unit']}")
        print(f"     doi={row['doi']}  experimental={row['experimental']!r}  source={row['source']!r}")
        print(f"     notes={(row['notes'] or '')[:400]}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
