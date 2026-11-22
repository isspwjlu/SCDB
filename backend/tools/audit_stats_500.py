"""Everything the papers quote about the 500-record audit.

One place, so that the numbers in the manuscripts and in the checks
come from the same computation: the two judges' rates, where they
differ, how the hard cases compare with the rest, and how the
database's own flag lines up with what the judges found.

    python tools/audit_stats_500.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
FIRST = PAPER / "audit" / "audit_deepseek.jsonl"
SECOND = PAPER / "audit" / "audit_deepseek_pro.jsonl"
SAMPLE = BACKEND / "data" / "cache" / "audit_sample_500.jsonl"


def wilson(successes: int, total: int, z: float = 1.959964) -> tuple[float, float]:
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def load(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("value") != "error":
                rows[row["id"]] = row
    return rows


def rate(successes: int, total: int) -> str:
    low, high = wilson(successes, total)
    return (
        f"{successes}/{total} = {100 * successes / total:.1f}% "
        f"({100 * low:.1f}--{100 * high:.1f}%)"
    )


def main() -> int:
    first = load(FIRST)
    second = load(SECOND)
    sample = {
        json.loads(line)["id"]: json.loads(line)
        for line in SAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    shared = [i for i in first if i in second and i in sample]
    print(f"样本 {len(sample)} 条，两裁判都判了 {len(shared)} 条\n")

    for label, rows in (("第一位", first), ("第二位", second)):
        both = sum(
            1 for i in shared
            if rows[i]["value"] == "yes" and rows[i]["material"] == "yes"
        )
        print(f"=== {label}裁判 ===")
        print(f"  值在文中   {rate(sum(1 for i in shared if rows[i]['value'] == 'yes'), len(shared))}")
        print(f"  文中即该材料 {rate(sum(1 for i in shared if rows[i]['material'] == 'yes'), len(shared))}")
        print(f"  两个条件同时成立 {rate(both, len(shared))}")
        kinds = {}
        for i in shared:
            kinds[rows[i]["kind"]] = kinds.get(rows[i]["kind"], 0) + 1
        print(f"  值的种类 {kinds}")
        calc = kinds.get("calculation", 0)
        print(f"  计算值 {rate(calc, len(shared))}")
        for group, name in ((True, "难例"), (False, "其余")):
            ids = [i for i in shared if bool(sample[i].get("hard_case")) == group]
            n = sum(
                1 for i in ids
                if rows[i]["value"] == "yes" and rows[i]["material"] == "yes"
            )
            print(f"  {name}（n={len(ids)}）两个条件同时成立 {rate(n, len(ids))}")

    print("\n=== 两位裁判的一致 ===")
    accept = sum(
        1 for i in shared
        if first[i]["value"] == "yes" and first[i]["material"] == "yes"
        and second[i]["value"] == "yes" and second[i]["material"] == "yes"
    )
    reject = sum(
        1 for i in shared
        if not (first[i]["value"] == "yes" and first[i]["material"] == "yes")
        and not (second[i]["value"] == "yes" and second[i]["material"] == "yes")
    )
    differ = len(shared) - accept - reject
    print(f"  都接受 {accept}，都不接受 {reject}，判定不同 {differ}")
    same_answers = sum(
        1 for i in shared
        if (first[i]["value"], first[i]["material"]) == (second[i]["value"], second[i]["material"])
    )
    print(f"  前两问答案相同 {same_answers}，不同 {len(shared) - same_answers}")
    for i in shared:
        if (first[i]["value"], first[i]["material"]) != (second[i]["value"], second[i]["material"]):
            print(f"    {first[i]['composition']:>16} {first[i]['tc']:>6} K  "
                  f"一号 {first[i]['value']}/{first[i]['material']}  "
                  f"二号 {second[i]['value']}/{second[i]['material']}")

    print("\n=== 数据库自己的标志 vs 裁判 ===")
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    flags = {r["id"]: r["experimental"] for r in db.execute("select id, experimental from materials")}
    for label, rows in (("第一位", first), ("第二位", second)):
        ids = [i for i in shared if i in flags]
        calc = [i for i in ids if rows[i]["kind"] == "calculation"]
        found = sum(1 for i in calc if flags[i] == 0)
        meas = [i for i in ids if rows[i]["kind"] == "measurement"]
        wrong = sum(1 for i in meas if flags[i] == 0)
        print(f"  {label}：裁判判为计算值 {len(calc)} 条，其中标志也为计算 {found} 条，"
              f"漏标 {len(calc) - found} 条；裁判判为测量值 {len(meas)} 条，"
              f"其中被误标为计算 {wrong} 条")
        if calc:
            print(f"    召回率 {found}/{len(calc)} = {100 * found / len(calc):.1f}%")
    db.close()

    print("\n=== 语料与全站 ===")
    db = sqlite3.connect(SNAPSHOT)
    n = db.execute("select count(*) from materials where tc_value is not null").fetchone()[0]
    e = db.execute(
        "select count(*) from materials where tc_value is not null and experimental=1"
    ).fetchone()[0]
    db.close()
    s = sqlite3.connect(BACKEND / "_dev_supercon.db")
    supercon = s.execute("select count(*) from supercon_items").fetchone()[0]
    s.close()
    print(f"  语料 {n} 条：实测 {e}（{100 * e / n:.2f}%），预测 {n - e}（{100 * (n - e) / n:.2f}%）")
    print(f"  SuperCon 配套集 {supercon} 条（全部实测）")
    print(f"  全站 {n + supercon} 条：实测 {e + supercon}（{100 * (e + supercon) / (n + supercon):.2f}%），"
          f"预测 {n - e}（{100 * (n - e) / (n + supercon):.2f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
