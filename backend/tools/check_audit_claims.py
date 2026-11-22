"""Check every claim the manuscript makes about the two audits.

The section is written by hand; this reads it back against the verdict
files, so that a number in the paper cannot drift from the file it
came from without the checks saying so.

Usage::

    python tools/check_audit_claims.py
"""

from __future__ import annotations

import collections
import json
import math
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
TEX = PAPER / "scdb_main.tex"
FIRST = PAPER / "audit" / "audit_deepseek.jsonl"
SECOND = PAPER / "audit" / "audit_deepseek_pro.jsonl"
SAMPLE = PAPER / "audit" / "audit_sample.jsonl"

failures: list[str] = []


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


def load(path: Path) -> dict[str, dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {r["id"]: r for r in rows if r.get("value") != "error"}


def accepts(row: dict) -> bool:
    return row["value"] == "yes" and row["material"] == "yes"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{('  ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    text = TEX.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", text)
    bare = plain(flat)
    first = load(FIRST)
    second = load(SECOND)
    sample = load(SAMPLE)
    ids = [i for i in first if i in second]
    n = len(ids)

    print("=== 第一个裁判 ===")
    for label, count in (
        ("值在文中", sum(1 for i in ids if first[i]["value"] == "yes")),
        ("材料绑定", sum(1 for i in ids if first[i]["material"] == "yes")),
        ("两者都对", sum(1 for i in ids if accepts(first[i]))),
        ("是测量值", sum(1 for i in ids if first[i]["kind"] == "measurement")),
    ):
        low, high = wilson(count, n)
        rate = f"{100 * count / n:.1f}"
        interval = f"{100 * low:.1f}--{100 * high:.1f}"
        check(
            f"{label} {rate}\\% [{interval}\\%]",
            f"{rate}\\%" in flat and f"{interval}\\%" in flat,
        )
    calculation = sum(1 for i in ids if first[i]["kind"] == "calculation")
    check(
        f"计算值 {calculation}/{n}",
        f"of the {calculation} records the first judge calls computed" in flat,
    )
    hydrides = [
        i for i in ids
        if first[i]["family"] == "high-pressure hydride" and first[i]["kind"] == "calculation"
    ]
    hydride_total = sum(1 for i in ids if first[i]["family"] == "high-pressure hydride")
    check(
        f"计算值里水合物 {len(hydrides)}/{calculation}（样本水合物 {hydride_total} 条）",
        len(hydrides) > calculation / 2 and "sit mostly in the hydrides" in flat,
    )

    print("\n=== 第二个裁判 ===")
    for label, count in (
        ("值在文中", sum(1 for i in ids if second[i]["value"] == "yes")),
        ("材料绑定", sum(1 for i in ids if second[i]["material"] == "yes")),
        ("两者都对", sum(1 for i in ids if accepts(second[i]))),
        ("是测量值", sum(1 for i in ids if second[i]["kind"] == "measurement")),
    ):
        low, high = wilson(count, n)
        rate = f"{100 * count / n:.1f}"
        interval = f"{100 * low:.1f}--{100 * high:.1f}"
        check(
            f"{label} {rate}\\% [{interval}\\%]",
            f"{rate}\\%" in flat and f"{interval}\\%" in flat,
        )
    second_calculation = sum(1 for i in ids if second[i]["kind"] == "calculation")
    second_low, second_high = wilson(second_calculation, n)
    check(
        f"计算值 {second_calculation}（第一个 {calculation}）",
        f"{100 * second_calculation / n:.1f}\\%" in flat
        and f"{100 * second_low:.1f}--{100 * second_high:.1f}\\%" in flat,
    )

    print("\n=== 两个裁判的一致 ===")
    both_accept = sum(1 for i in ids if accepts(first[i]) and accepts(second[i]))
    both_reject = sum(
        1 for i in ids if not accepts(first[i]) and not accepts(second[i])
    )
    differ = n - both_accept - both_reject
    same_answers = sum(
        1
        for i in ids
        if (first[i]["value"], first[i]["material"])
        == (second[i]["value"], second[i]["material"])
    )
    differ_answers = n - same_answers
    third_answers = sum(
        1 for i in ids if first[i]["kind"] != second[i]["kind"]
    )
    check(f"都接受 {both_accept}", f"{both_accept} that both accept" in flat)
    check(
        f"答案一致 {same_answers}",
        f"same answer as the first on {same_answers} of the {n}" in flat,
    )
    check(
        f"前两问分歧 {differ_answers}",
        f"differ on {spell(differ_answers)} records" in flat,
    )
    check(
        f"第三问分歧 {third_answers}",
        f"on the third on {spell(third_answers)}" in flat,
    )
    check(
        f"判定不同 {differ}",
        f"{differ} that they judge differently" in flat,
    )
    check(
        f"都不接受 {both_reject}",
        f"{both_reject} that neither accepts" in flat,
    )

    print("\n=== 难例与其余 ===")
    hard = [i for i in ids if first[i].get("hard_case")]
    easy = [i for i in ids if not first[i].get("hard_case")]
    for label, group in (("难例", hard), ("其余", easy)):
        count = sum(1 for i in group if accepts(first[i]))
        check(
            f"{label} n={len(group)} 两者都对 {100 * count / len(group):.1f}%",
            f"{100 * count / len(group):.1f}\\%" in flat,
        )

    print("\n=== 论文举例的记录 ===")
    for name, tc in (("ScH6", 100.0), ("LiCaH12", 288.0)):
        rows = [
            i for i in ids
            if first[i]["composition"] == name and first[i]["tc"] == tc
        ]
        check(
            f"{name} {tc} 被判为计算值",
            bool(rows)
            and first[rows[0]]["kind"] == "calculation"
            and second[rows[0]]["kind"] == "calculation",
        )

    print("\n=== 第一个裁判拒掉的记录 ===")
    rejected = [i for i in ids if not accepts(first[i])]
    check(
        f"共 {len(rejected)} 条",
        len(rejected) == 33 and "Thirty-three records" in flat,
    )
    not_in_text = [i for i in rejected if first[i]["value"] == "no"]
    about_other = [
        i for i in rejected
        if first[i]["value"] == "yes" and first[i]["material"] != "yes"
    ]
    undecided = [
        i for i in about_other if first[i]["material"] == "unclear"
    ]
    check(
        f"文中找不到 {len(not_in_text)} 条",
        len(not_in_text) == 16
        and f"Sixteen are values the judge cannot find in" in flat,
    )
    check(
        f"材料对不上 {len(about_other)} 条（其中拿不准 {len(undecided)} 条）",
        len(about_other) == 17
        and f"The other seventeen" in flat
        and f"seven of them" in flat,
    )
    named_rejected = (
        ("BaFe2As2", 29.0), ("ZrNCl", 26.0), ("CeIrIn5", 2.0),
        ("Nb", 0.55), ("Nb3Sn", 9.0), ("MgB2-GaP", 0.8),
        ("CuH2", 40.0), ("CsxMoS2", 5.0), ("FeSe", 6.5),
    )
    for name, tc in named_rejected:
        rows = [
            i for i in rejected
            if first[i]["composition"] == name and first[i]["tc"] == tc
        ]
        check(
            f"{name} {tc} 确实被拒",
            bool(rows) and first[rows[0]]["value"] == "no",
        )

    print("\n=== 分歧的记录 ===")
    differing = [i for i in ids if accepts(first[i]) != accepts(second[i])]
    check(f"判定不同 {len(differing)} 条", len(differing) == 20)
    named_differing = (
        ("FeSe", 70.0), ("BaFe2As2", 29.0), ("FeSe0.83S0.17", 10.0),
        ("(Li1-xFex)OHFeSe", 41.9), ("YBa2Cu3O7-δ", 92.0),
    )
    for name, tc in named_differing:
        rows = [
            i for i in differing
            if first[i]["composition"] == name and first[i]["tc"] == tc
        ]
        check(f"{name} {tc} 确实有分歧", bool(rows))
        if rows:
            check(
                f"{name} {tc} 写在论文里",
                plain(first[rows[0]]["composition"]) in bare,
            )

    print("\n=== 表格 ===")
    for label, count in (
        ("First & value is in the text", sum(1 for i in ids if first[i]["value"] == "yes")),
        ("First & text is about the material", sum(1 for i in ids if first[i]["material"] == "yes")),
        ("First & both", sum(1 for i in ids if accepts(first[i]))),
        ("First & value is a measurement", sum(1 for i in ids if first[i]["kind"] == "measurement")),
        ("Second & value is in the text", sum(1 for i in ids if second[i]["value"] == "yes")),
        ("Second & text is about the material", sum(1 for i in ids if second[i]["material"] == "yes")),
        ("Second & both", sum(1 for i in ids if accepts(second[i]))),
        ("Second & value is a measurement", sum(1 for i in ids if second[i]["kind"] == "measurement")),
    ):
        low, high = wilson(count, n)
        row = f"{label} & {100 * count / n:.1f}\\% & {100 * low:.1f}--{100 * high:.1f}\\%"
        check(row.replace("\\", ""), row in text)

    print()
    if failures:
        print(f"!! {len(failures)} 处对不上：")
        for name in failures:
            print(f"   {name}")
        return 1
    print("论文中关于两个模型审核的每一处都与判定文件一致")
    return 0


def plain(text: str) -> str:
    """The text without the LaTeX markup, so that ``MgB$_2$-GaP`` reads
    as ``MgB2-GaP``."""
    for mark in ("$", "{", "}", "\\", "_", " "):
        text = text.replace(mark, "")
    # The manuscript writes the Greek letter as a LaTeX command.
    text = text.replace("\\delta", "delta").replace("δ", "delta")
    return text


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


if __name__ == "__main__":
    raise SystemExit(main())
