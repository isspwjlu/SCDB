"""Compare the two audits of the sampled records.

Both judges read the whole paper and answered the same three
questions, so the comparison is record by record: what each of them
accepted, where they differ, and what the records they both reject
have in common.

Usage::

    python tools/audit_comparison.py
"""

from __future__ import annotations

import collections
import json
import math
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CACHE = BACKEND / "data" / "cache"
FIRST = CACHE / "audit_deepseek_full.jsonl"
SECOND = CACHE / "audit_deepseek_pro_full.jsonl"
SAMPLE = CACHE / "audit_sample.jsonl"


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


def load(path: Path) -> dict[str, dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {r["id"]: r for r in rows if r.get("value") != "error"}


def main() -> int:
    first = load(FIRST)
    second = load(SECOND)
    sample = {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in SAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    ids = [i for i in first if i in second]
    n = len(ids)
    print(f"两个裁判都判了 {n} 条\n")

    for name, rows in (("第一个裁判", first), ("第二个裁判", second)):
        value = sum(1 for i in ids if rows[i]["value"] == "yes")
        material = sum(1 for i in ids if rows[i]["material"] == "yes")
        both = sum(
            1 for i in ids if rows[i]["value"] == "yes" and rows[i]["material"] == "yes"
        )
        measurement = sum(1 for i in ids if rows[i]["kind"] == "measurement")
        calculation = sum(1 for i in ids if rows[i]["kind"] == "calculation")
        print(f"{name} ({rows[ids[0]]['model']}):")
        print(f"  值在文中   {rate(value, n)}")
        print(f"  材料绑定   {rate(material, n)}")
        print(f"  两者都对   {rate(both, n)}")
        print(f"  是测量值   {rate(measurement, n)}")
        print(f"  是计算值   {calculation}/{n} ({100 * calculation / n:.1f}%)")
        for label, group in (
            ("难例", [i for i in ids if first[i].get("hard_case")]),
            ("其余", [i for i in ids if not first[i].get("hard_case")]),
        ):
            accept = sum(
                1
                for i in group
                if rows[i]["value"] == "yes" and rows[i]["material"] == "yes"
            )
            calc = sum(1 for i in group if rows[i]["kind"] == "calculation")
            print(f"  {label} n={len(group)}: 两者都对 {rate(accept, len(group))}, 计算值 {calc}/{len(group)}")
        print()

    def accepts(rows: dict, record: str) -> bool:
        return rows[record]["value"] == "yes" and rows[record]["material"] == "yes"

    both_accept = [i for i in ids if accepts(first, i) and accepts(second, i)]
    both_reject = [i for i in ids if not accepts(first, i) and not accepts(second, i)]
    first_only = [i for i in ids if accepts(first, i) and not accepts(second, i)]
    second_only = [i for i in ids if not accepts(first, i) and accepts(second, i)]
    print(f"两个都接受      {len(both_accept)}")
    print(f"两个都拒绝      {len(both_reject)}")
    print(f"只有第一个接受  {len(first_only)}")
    print(f"只有第二个接受  {len(second_only)}")
    print(f"一致            {len(both_accept) + len(both_reject)}/{n}")

    print("\n两个都拒绝的记录:")
    for record in both_reject:
        row = first[record]
        print(
            f"  {row['composition']:22} {row['tc']:<8} [{row['family']}] "
            f"value={row['value']} material={row['material']} kind={row['kind']}"
        )
        print(f"     {sample[record]['evidence'][:120]}")

    print("\n两个裁判的判定不一致（值或材料）:")
    for record in ids:
        if first[record]["value"] != second[record]["value"] or first[record][
            "material"
        ] != second[record]["material"]:
            row = first[record]
            print(
                f"  {row['composition']:22} {row['tc']:<8} "
                f"第一个 value={row['value']} material={row['material']} | "
                f"第二个 value={second[record]['value']} material={second[record]['material']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
