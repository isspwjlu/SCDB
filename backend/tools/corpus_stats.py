"""Corpus-level statistics the paper quotes about the surviving records.

Everything here reads the frozen snapshot, so the figures in the
manuscript and the figures on disk cannot drift apart.

Usage::

    python tools/corpus_stats.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"


def main() -> int:
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731

    total = q("select count(*) c from materials")[0]["c"]
    with_tc = q("select count(*) c from materials where tc_value is not null")[0]["c"]

    print("=== 记录数 ===")
    print(f"  全部记录          : {total}")
    print(f"  有 Tc 的记录      : {with_tc}")

    print("\n=== experimental 标记 ===")
    for row in q(
        "select experimental, count(*) c from materials group by experimental"
    ):
        print(f"  experimental={row['experimental']!r:8} {row['c']}")

    print("\n=== experimental 标记与 Tc ===")
    for row in q(
        "select experimental, count(*) c, min(tc_value) lo, max(tc_value) hi "
        "from materials where tc_value is not null group by experimental"
    ):
        print(
            f"  experimental={row['experimental']!r:8} n={row['c']:5} "
            f"Tc {row['lo']}–{row['hi']} K"
        )

    print("\n=== Tc 高位区：计算值的占比 ===")
    for cut in (100, 200, 250):
        row = q(
            "select count(*) c, sum(case when experimental=0 then 1 else 0 end) pred "
            "from materials where tc_value >= ?",
            cut,
        )[0]
        n, pred = row["c"], row["pred"] or 0
        pct = 100 * pred / n if n else 0
        print(f"  Tc >= {cut:3} K : {n:4} 条，其中 experimental=0 的 {pred:3} 条 ({pct:.1f}%)")

    print("\n=== Tc 最高的 20 条：experimental 标记 ===")
    for row in q(
        "select chemical_composition, tc_value, experimental, doi from materials "
        "where tc_value is not null order by tc_value desc limit 20"
    ):
        mark = "预测" if row["experimental"] == 0 else "实测"
        print(f"  {row['tc_value']:7} K  {row['chemical_composition']:22} {mark}  {row['doi']}")

    print("\n=== 按族的 experimental 分布（族内 Tc>=100 K 的） ===")
    for row in q(
        "select superconductor_family f, count(*) c, "
        "sum(case when experimental=0 then 1 else 0 end) pred "
        "from materials where tc_value >= 100 group by superconductor_family "
        "order by c desc"
    ):
        c, pred = row["c"], row["pred"] or 0
        print(f"  {row['f'] or '(未分类)':22} {c:4} 条，预测 {pred:3} ({100 * pred / c:.1f}%)")

    print("\n=== 记录的构成 ===")
    row = q(
        "select sum(case when chemical_composition is not null then 1 else 0 end) comp, "
        "count(distinct chemical_composition) distinct_comp, "
        "sum(case when superconductor_family is null or superconductor_family='' then 1 else 0 end) nofam "
        "from materials"
    )[0]
    print(f"  不同化学式 : {row['distinct_comp']}")
    print(f"  无族分类   : {row['nofam']}")
    n_el = q(
        "select count(distinct e) c from materials, json_each(materials.meta) "
        "where 0"
    ) if False else None

    print("\n=== 来源（source）与文件 ===")
    for row in q("select source, count(*) c from materials group by source order by c desc"):
        print(f"  {row['source']!r:22} {row['c']}")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
