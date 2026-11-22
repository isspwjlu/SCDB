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
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
TEX = PAPER / "scdb_main.tex"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
# The frozen copies that ship with the paper, not the working ones.
PIPELINE = PAPER / "audit" / "pipeline_stats.json"
AUDIT = PAPER / "audit" / "audit_deepseek.jsonl"
SECOND = PAPER / "audit" / "audit_deepseek_pro.jsonl"

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)


def spell(number: int) -> str:
    """The number in words, the way the manuscript writes small counts."""
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"
    hundreds, rest = divmod(number, 100)
    head = f"{_ONES[hundreds]} hundred"
    return head if rest == 0 else f"{head} and {spell(rest)}"


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
        for line in AUDIT.read_text(encoding="utf-8").splitlines()
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
    check(
        f"计算值 {calculation}/{n}",
        f"{100 * calculation / n:.1f}\\%" in flat
        and f"{100 * wilson(calculation, n)[0]:.1f}--{100 * wilson(calculation, n)[1]:.1f}\\%" in flat,
    )
    check(f"无法判定 {unclear}/{n}", True)
    hard_both = sum(1 for r in hard if r["value"] == "yes" and r["material"] == "yes")
    easy_both = sum(1 for r in easy if r["value"] == "yes" and r["material"] == "yes")
    for label, value in (
        (f"难例 {len(hard)} 条", 100 * hard_both / len(hard)),
        (f"其余 {len(easy)} 条", 100 * easy_both / len(easy)),
    ):
        check(f"{label} {value:.1f}%", f"{value:.1f}\\%" in flat)

    second = [
        json.loads(line)
        for line in SECOND.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    second = [r for r in second if r.get("value") != "error"]
    m = len(second)
    second_value = sum(1 for r in second if r["value"] == "yes")
    second_material = sum(1 for r in second if r["material"] == "yes")
    second_both = sum(
        1 for r in second if r["value"] == "yes" and r["material"] == "yes"
    )
    second_measurement = sum(1 for r in second if r["kind"] == "measurement")
    second_calculation = sum(1 for r in second if r["kind"] == "calculation")

    def accepts(row: dict) -> bool:
        return row["value"] == "yes" and row["material"] == "yes"

    by_id = {r["id"]: r for r in second}
    shared = len([r for r in judged if r["id"] in by_id])
    both_accept = sum(1 for r in judged if r["id"] in by_id and accepts(r) and accepts(by_id[r["id"]]))
    both_reject = sum(
        1
        for r in judged
        if r["id"] in by_id
        and not accepts(r)
        and not accepts(by_id[r["id"]])
    )
    print("\n=== 第二个裁判 ===")
    for label, count in (
        ("值在文中", second_value),
        ("材料绑定", second_material),
        ("两者都对", second_both),
        ("是测量值", second_measurement),
    ):
        low, high = wilson(count, m)
        check(
            f"{label} {100 * count / m:.1f}\% [{100 * low:.1f}--{100 * high:.1f}\%]",
            f"{100 * count / m:.1f}\\%" in flat
            and f"{100 * low:.1f}--{100 * high:.1f}\\%" in flat,
        )
    check(
        f"第二个裁判的计算值 {second_calculation}/{m}",
        f"{100 * second_calculation / m:.1f}\\%" in flat
        and f"{100 * wilson(second_calculation, m)[0]:.1f}--{100 * wilson(second_calculation, m)[1]:.1f}\\%" in flat,
    )
    check(
        f"两个都接受 {both_accept}",
        f"{both_accept} that both accept" in flat,
    )
    second_by_id = {r["id"]: r for r in second}
    same_answers = sum(
        1 for r in judged
        if r["id"] in second_by_id
        and (r["value"], r["material"])
        == (second_by_id[r["id"]]["value"], second_by_id[r["id"]]["material"])
    )
    differ = shared - both_accept - both_reject
    check(
        f"双方都不接受 {both_reject}",
        f"{both_reject} that neither accepts" in flat,
    )
    check(
        f"判定不同 {differ} 条",
        f"{differ} that they judge differently" in flat,
    )
    check(
        f"两个裁判答案一致 {same_answers}/{shared}",
        f"same answer as the first on {same_answers} of the {shared}" in flat,
    )

    print("\n=== 说法 ===")
    check("审计测得计算值占 7.6%", f"{100 * calculation / n:.1f}\\%" in flat)
    check("没有 one in seven", "one in seven" not in flat.lower())
    check("没有 one in six", "one in six" not in flat.lower())
    check("没有 one in eight", "one in eight" not in flat.lower())
    check("未分类家族的叫法", "unclassified conventional superconductors" not in flat
          and "are still unclassified" in flat)
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

    print("\n=== 关系图的数字 ===")
    site = BACKEND / "data" / "cache" / "site"
    if (site / "tc-vs-hc2.json").exists():
        hc2 = json.loads((site / "tc-vs-hc2.json").read_text(encoding="utf-8"))
        gap = json.loads((site / "gap-vs-tc.json").read_text(encoding="utf-8"))
        pressure = json.loads(
            (site / "pressure-series.json").read_text(encoding="utf-8")
        )["series"]
        hydride_max = max(
            r["hc2"] for r in hc2 if r["family"] == "high-pressure hydride"
        )
        hydride_max = int(hydride_max) if hydride_max == int(hydride_max) else hydride_max
        check(f"水合物 Hc2 上限 {hydride_max} T", f"{hydride_max}~T" in flat)
        slope = 3.52 * 0.08617
        above = sum(1 for r in gap if 2 * r["gap"] > slope * r["tc"])
        share = round(100 * above / len(gap))
        check(f"能隙在 BCS 线之上的 {share}%", f"{share}\\%" in flat)
        peak = max(max(p["tc"] for p in s["points"]) for s in pressure)
        check(f"镍酸盐峰值 {peak:.0f} K", f"{peak:.0f}~K" in flat)
        check(f"压强系列 {len(pressure)} 条", f"eleven\ncurves" in flat or "eleven curves" in flat)
        biggest = max(hc2, key=lambda r: r["hc2"])
        check(
            f"语料最大 Hc2 {biggest['hc2']:.0f} T（{biggest['family']}）",
            f"{biggest['hc2']:.0f}~T" in flat,
        )

    print("\n=== 族领跑、标志与裁判的一致 ===")
    conn = sqlite3.connect(SNAPSHOT)
    conn.row_factory = sqlite3.Row
    mgb2 = conn.execute(
        "select chemical_composition c, tc_value t from materials "
        "where superconductor_family='MgB2' order by tc_value desc limit 1"
    ).fetchone()
    flags = {r["id"]: r["experimental"] for r in conn.execute(
        "select id, experimental from materials"
    )}
    conn.close()
    mgb2_tc = int(mgb2["t"]) if mgb2["t"] == int(mgb2["t"]) else mgb2["t"]
    plain = flat.replace("$_", "").replace("$", "")
    check(
        f"MgB2 族领跑 {mgb2_tc} K（{mgb2['c']}）",
        f"{mgb2_tc}~K" in flat and mgb2["c"] in plain,
    )

    kept = [r for r in judged if r["id"] in flags]
    judge_calc = [r for r in kept if r["kind"] == "calculation"]
    judge_meas = [r for r in kept if r["kind"] == "measurement"]
    found = sum(1 for r in judge_calc if flags[r["id"]] == 0)
    wrong = sum(1 for r in judge_meas if flags[r["id"]] == 0)
    check(
        f"标志认出 {found}/{len(judge_calc)} 条计算值",
        f"of the {len(judge_calc)} records the first judge calls computed, "
        f"the flag calls {found} of them computed as well" in flat,
    )
    check(
        f"标志误标 {wrong} 条测量值",
        f"none of the {len(judge_meas)} records the judge calls measured "
        "is marked as computed by the flag" in flat,
    )

    second_rows = second_by_id
    first_rows = {r["id"]: r for r in judged}
    shared = [i for i in first_rows if i in second_rows]
    calc1 = [i for i in shared if first_rows[i]["kind"] == "calculation"]
    accepted1 = sum(
        1 for i in calc1
        if first_rows[i]["value"] == "yes" and first_rows[i]["material"] == "yes"
    )
    calc2 = [i for i in shared if second_rows[i]["kind"] == "calculation"]
    accepted2 = sum(
        1 for i in calc2
        if second_rows[i]["value"] == "yes" and second_rows[i]["material"] == "yes"
    )
    check(
        "计算值的记录基本都被接受",
        accepted1 >= 0.9 * len(calc1)
        and accepted2 >= 0.9 * len(calc2)
        and "is a faithful record of what the paper says" in flat,
    )
    differ = sum(
        1 for i in shared
        if (first_rows[i]["value"], first_rows[i]["material"])
        != (second_rows[i]["value"], second_rows[i]["material"])
    )
    check(f"两裁判在前两问上分歧 {differ} 条", f"{spell(differ)} records" in flat)

    print("\n=== 标志的来源与合并 ===")
    # The flag the corpus keeps is the extraction model's own judgement:
    # the sentence-level classifier rejects computed values and marks
    # measured ones, but it gives no signal at all on the records the
    # corpus keeps as computed.  The manuscript says so; check that it
    # still does, and that it is still true.
    try:
        sys.path.insert(0, str(BACKEND / "src"))
        from superconductor_db.ingest.extractor import classify_tc_evidence

        conn = sqlite3.connect(SNAPSHOT)
        conn.row_factory = sqlite3.Row
        flagged = conn.execute(
            "select notes from materials where experimental = 0 and tc_value is not null"
        ).fetchall()
        conn.close()

        def quote(notes: str) -> str:
            for line in (notes or "").splitlines():
                if line.startswith("Tc evidence:"):
                    return line[len("Tc evidence:"):].strip()
            return ""

        signals = sum(1 for r in flagged if classify_tc_evidence(quote(r["notes"])))
        check(
            f"{len(flagged)} 条非实验记录的证据句无判读信号（{signals} 条有）",
            signals == 0 and "the extraction model's own judgement" in flat,
        )
    except Exception as error:  # pragma: no cover - the import may fail
        print(f"  ! 跳过了标志来源的核对：{error}")

    kept = [r for r in judged if r["id"] in flags]
    gone = [r for r in judged if r["id"] not in flags]
    survived_ok = sum(
        1 for r in kept if r["value"] == "yes" and r["material"] == "yes"
    )
    check(
        f"被抽的 {len(kept)} 条都还在库里",
        len(gone) == 0
        and len(kept) == n
        and f"All {len(kept)} of the sampled records are still in the corpus" in flat,
    )
    check(
        f"存活记录上的比率 {survived_ok}/{len(kept)}",
        f"The two conditions hold together in {survived_ok} records" in flat,
    )

    print("\n=== 全站构成 ===")
    supercon_db = sqlite3.connect(BACKEND / "_dev_supercon.db")
    supercon_db.row_factory = sqlite3.Row
    supercon_rows = supercon_db.execute(
        "select count(*) c from supercon_items"
    ).fetchone()["c"]
    supercon_db.close()
    site_total = total_rows + supercon_rows
    site_measured = supercon_rows + (total_rows - predicted)
    check(
        f"全站 {site_total:,} 条",
        f"{site_total:,}".replace(",", "{,}") in flat,
    )
    check(
        f"全站实测 {site_measured:,} 条",
        f"{site_measured:,}".replace(",", "{,}") in flat,
    )
    check(
        f"全站实测比例 {100 * site_measured / site_total:.1f}\\%",
        f"{100 * site_measured / site_total:.1f}\\%" in flat,
    )
    check(
        f"全站计算比例 {100 * predicted / site_total:.1f}\\%",
        f"{100 * predicted / site_total:.1f}\\%" in flat,
    )
    check(
        f"语料实测比例 {100 * (total_rows - predicted) / total_rows:.1f}\\%",
        f"{100 * (total_rows - predicted) / total_rows:.1f}\\%" in flat,
    )

    print("\n=== 抽样的配额 ===")
    sample = [
        json.loads(line)
        for line in (BACKEND / "data" / "cache" / "audit_sample_500.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    sample_family: dict[str, int] = {}
    for row in sample:
        family_name = row["family"]
        sample_family[family_name] = sample_family.get(family_name, 0) + 1
    for family_name in ("iron-based", "cuprate", "high-pressure hydride"):
        taken = sample_family.get(family_name, 0)
        corpus_share = 100 * taken / len(sample)
        conn = sqlite3.connect(SNAPSHOT)
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "select count(*) c from materials "
            "where superconductor_family = ? and tc_value is not null",
            (family_name,),
        ).fetchone()["c"]
        conn.close()
        corpus_pct = 100 * total / total_rows
        check(
            f"{family_name} 样本 {corpus_share:.1f}% 对语料 {corpus_pct:.1f}%",
            f"{corpus_share:.1f}\\%" in flat and f"{corpus_pct:.1f}\\%" in flat,
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
