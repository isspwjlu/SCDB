"""Print every family-dependent number the manuscript quotes.

The family classification changed the corpus, so the numbers in the
text that depend on it have to be re-read from the snapshot rather
than remembered.

Usage::

    python tools/paper_numbers.py
"""

from __future__ import annotations

import collections
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"


def main() -> int:
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    total = db.execute("select count(*) c from materials").fetchone()["c"]
    distinct = db.execute(
        "select count(distinct chemical_composition) c from materials"
    ).fetchone()["c"]
    papers = db.execute("select count(*) c from papers").fetchone()["c"]
    unclassified = db.execute(
        "select count(*) c from materials "
        "where superconductor_family is null or superconductor_family = ''"
    ).fetchone()["c"]
    predicted = db.execute(
        "select count(*) c from materials where experimental = 0"
    ).fetchone()["c"]

    print(f"记录 {total} / 化学式 {distinct} / 论文 {papers}")
    print(f"未分类 {unclassified} ({100 * unclassified / total:.1f}%)")
    print(f"标为非实测 {predicted} ({100 * predicted / total:.1f}%)")

    print("\n家族分布：")
    for row in db.execute(
        "select coalesce(superconductor_family, '(未分类)') f, count(*) n "
        "from materials group by f order by n desc"
    ):
        print(f"  {row['f']:26} {row['n']:5}")

    print("\n各族最高 Tc：")
    for row in db.execute(
        "select coalesce(superconductor_family, '(未分类)') f, "
        "       chemical_composition c, max(tc_value) tc "
        "from materials group by f order by tc desc"
    ):
        print(f"  {row['f']:26} {row['c'][:22]:24} {row['tc']}")

    print("\nTc>=100 K 的家族：")
    for row in db.execute(
        "select coalesce(superconductor_family, '(未分类)') f, count(*) n, "
        "       sum(case when experimental = 0 then 1 else 0 end) p "
        "from materials where tc_value >= 100 group by f "
        "having n >= 10 order by 1.0*p/n desc"
    ):
        print(
            f"  {row['f']:26} n={row['n']:4} 未实测 {row['p']:3} "
            f"({100 * row['p'] / row['n']:.0f}%)"
        )

    print("\n尾部阈值：")
    for cut in (25, 50, 100, 150, 200, 250):
        row = db.execute(
            "select count(*) n, sum(case when experimental = 0 then 1 else 0 end) p "
            "from materials where tc_value >= ?",
            (cut,),
        ).fetchone()
        print(f"  Tc>={cut:3}: {100 * row['p'] / row['n']:.0f}%  (n={row['n']})")

    elements = collections.Counter()
    for row in db.execute("select chemical_composition c from materials"):
        for symbol in set(re.findall(r"[A-Z][a-z]*", row["c"])):
            elements[symbol] += 1
    counts = collections.Counter(
        len(set(re.findall(r"[A-Z][a-z]*", row["c"])))
        for row in db.execute("select chemical_composition c from materials")
    )
    print(f"\n二元+三元 {counts[2] + counts[3]}/{total} "
          f"= {100 * (counts[2] + counts[3]) / total:.1f}%")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
