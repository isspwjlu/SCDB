"""Check the claims the manuscript makes against the files it cites."""

from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
QWEN = BACKEND / "data" / "cache" / "audit_qwen.jsonl"


def main() -> int:
    rows = [
        json.loads(line)
        for line in QWEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judged = [r for r in rows if r.get("value") != "error"]
    kinds = collections.Counter(r["kind"] for r in judged)
    print("审计判定:", dict(kinds), f"共 {len(judged)} 条")
    calc = kinds["calculation"]
    print(f"  计算值比例: {calc}/{len(judged)} = {100 * calc / len(judged):.1f}%  → 约 1/{len(judged) / calc:.1f}")

    # The hard cases were put into the sample on purpose, so the rate
    # over the whole sample is not the rate over the corpus.
    hard = [r for r in judged if r.get("hard_case")]
    easy = [r for r in judged if not r.get("hard_case")]
    print(f"\n难例 {len(hard)} 条 / 其余 {len(easy)} 条")
    for name, group in (("难例", hard), ("其余", easy)):
        if not group:
            continue
        calc = sum(1 for r in group if r["kind"] == "calculation")
        meas = sum(1 for r in group if r["kind"] == "measurement")
        ok = sum(
            1
            for r in group
            if r["value"] == "yes" and r["material"] == "yes"
        )
        print(
            f"  {name}: 值与材料都对 {ok}/{len(group)} ({100 * ok / len(group):.1f}%)"
            f" | 测量 {meas} | 计算 {calc} ({100 * calc / len(group):.1f}%)"
        )

    # The highest Tc in the corpus, and what its evidence says.
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    top = db.execute(
        "select chemical_composition, tc_value, tc_unit, notes, doi, "
        "       superconductor_family from materials "
        "where tc_value is not null order by tc_value desc limit 6"
    ).fetchall()
    print("\n库里 Tc 最高的六条：")
    for row in top:
        print(f"  {row['chemical_composition']:22} {row['tc_value']} {row['tc_unit']}  {row['doi']}")
        print(f"     {(row['notes'] or '')[:170]}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
