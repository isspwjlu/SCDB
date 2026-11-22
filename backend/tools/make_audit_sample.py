"""Draw the stratified sample the database audits are run on.

The audit asks a question the site cannot answer by itself: of the
values that passed every gate, how many are right?  The sample has to
be drawn so that the answer means something, so it is stratified:

* by **family** and by **Tc band** -- a database of hydrides and one of
  room-temperature compounds have different failure modes -- with the
  allocation proportional to the corpus, so that the rates the audit
  gives are the rates of the database;
* plus a **hard case** stratum -- the compositions the audit already
  knows are trouble, and the rows that many papers reported (the ones
  the consensus pass had to arbitrate).

The draw is seeded, so the same sample can be drawn again.

Usage::

    python tools/make_audit_sample.py --n 150 --out data/cache/audit_sample.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
MARKDOWN = BACKEND / "data" / "cache" / "markdown"

# The Tc bands the site itself uses (below liquid He, up to liquid
# nitrogen, and so on).
BANDS = (
    ("<2.2 K", 0.0, 2.2),
    ("2.2-30 K", 2.2, 30.0),
    ("30-77 K", 30.0, 77.0),
    ("77-150 K", 77.0, 150.0),
    (">150 K", 150.0, float("inf")),
)

# Compositions the audit knows are trouble: they are in the database
# because a paper reported them, and they are where a number is most
# likely to belong to a different material.
HARD_CASES = (
    "FeSe",
    "LaH10",
    "MgB2",
    "YBa2Cu3O7",
    "H3S",
    "CeH9",
    "LaBH8",
    "Sr2RuO4",
    "LaH6",
    "Nb",
)


def band_of(tc: float | None) -> str:
    if tc is None:
        return "no Tc"
    for name, low, high in BANDS:
        if low <= tc < high:
            return name
    return "no Tc"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def context_for(evidence: str, markdown_path: str | None, window: int = 700) -> str:
    """The evidence sentence with its neighbours, as a reader sees it."""
    if not markdown_path:
        return ""
    name = os.path.basename(markdown_path)
    local = MARKDOWN / name
    if not local.exists():
        return ""
    text = normalise(local.read_text(encoding="utf-8", errors="replace"))
    needle = normalise(evidence)
    for length in (120, 80, 50, 30):
        key = needle[:length]
        if len(key) < 12:
            break
        at = text.find(key)
        if at != -1:
            start = max(0, at - window // 2)
            return text[start : at + len(key) + window]
    return ""


def _meta(raw: str | None) -> dict:
    """The meta column, which the ORM writes double-encoded."""
    value: object = raw or "{}"
    for _ in range(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        else:
            break
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument(
        "--out", default=str(BACKEND / "data" / "cache" / "audit_sample.jsonl")
    )
    parser.add_argument(
        "--include",
        default="",
        help="a file of record ids that must be in the sample (one per line, "
        "or a JSONL whose 'id' field is read); used to keep the records "
        "the expert already judged in the machine judge's sample",
    )
    args = parser.parse_args()

    db = sqlite3.connect(BACKEND / "_dev.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT m.id, m.chemical_composition, m.tc_value, m.tc_unit, "
        "       m.superconductor_family, m.notes, m.meta, m.experimental, "
        "       p.doi AS paper_doi, p.title AS paper_title, "
        "       p.markdown_path AS markdown_path "
        "FROM materials m LEFT JOIN papers p ON p.id = m.paper_id"
    ).fetchall()
    db.close()

    records = []
    for row in rows:
        meta = _meta(row["meta"])
        records.append(
            {
                "id": str(row["id"]),
                "composition": row["chemical_composition"],
                "tc": row["tc_value"],
                "tc_unit": row["tc_unit"],
                "family": row["superconductor_family"] or "unclassified",
                "experimental": bool(row["experimental"]),
                "evidence": (row["notes"] or "").removeprefix("Tc evidence:").strip(),
                "source_dois": meta.get("source_dois", []),
                "paper_doi": row["paper_doi"],
                "paper_title": row["paper_title"],
                "markdown_path": row["markdown_path"],
                "band": band_of(row["tc_value"]),
            }
        )

    rng = random.Random(args.seed)
    for record in records:
        record["hard_case"] = any(
            case.lower() in record["composition"].lower() for case in HARD_CASES
        )
        record["multi_source"] = len(record["source_dois"]) > 1

    # The strata: family x Tc band.
    strata: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for record in records:
        strata[(record["family"], record["band"])].append(record)
    for group in strata.values():
        rng.shuffle(group)

    # The records that must be in the sample: the ones the expert has
    # already judged, so that the two judges still meet on the same
    # records after the corpus has moved on.
    forced: list[dict] = []
    if args.include:
        wanted = set()
        path = Path(args.include)
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                wanted.add(json.loads(line).get("id"))
            else:
                wanted.add(line)
        by_id = {record["id"]: record for record in records}
        for identifier in wanted:
            record = by_id.get(identifier)
            if record is None:
                print(f"  ! 记录 {identifier} 已不在库里，跳过")
                continue
            forced.append(record)
        for record in forced:
            strata[(record["family"], record["band"])].remove(record)

    # The allocation is proportional to the corpus: every cell of the
    # family x band table gets a share of the sample in proportion to
    # its share of the database, so that the rates the audit gives are
    # the rates of the database and not the rates of a sample that
    # over-weighs the rare families.  Every cell that is not empty
    # still gets one record, so that no family and no band is left
    # unseen; what that floor costs is taken back from the largest
    # cells.
    picked: list[dict] = list(forced)
    universe = sum(len(group) for group in strata.values())
    quota = {
        key: max(1, round(args.n * len(group) / universe))
        for key, group in strata.items()
        if group
    }
    # The forced records are already in the sample; take them off the
    # cells they came from before the cells are filled.
    for record in forced:
        key = (record["family"], record["band"])
        if key in quota:
            quota[key] = max(0, quota[key] - 1)
    over = sum(quota.values()) - (args.n - len(picked))
    if over > 0:
        for key in sorted(quota, key=lambda k: (-quota[k], k)):
            if over <= 0:
                break
            if quota[key] > 1:
                quota[key] -= 1
                over -= 1
    elif over < 0:
        for key in sorted(quota, key=lambda k: (-len(strata[k]), k)):
            if over >= 0:
                break
            room = len(strata[key]) - quota[key]
            add = min(over, room)
            quota[key] += add
            over -= add

    for key in sorted(quota, key=lambda k: (-quota[k], k)):
        group = strata[key]
        rng.shuffle(group)
        picked.extend(group[: quota[key]])

    for record in picked:
        record["context"] = context_for(
            record["evidence"], record["markdown_path"]
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in picked:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"抽出 {len(picked)} 条 → {out}")
    print("按家族:", dict(collections.Counter(r["family"] for r in picked)))
    print("按 Tc 段:", dict(collections.Counter(r["band"] for r in picked)))
    print("有上下文:", sum(1 for r in picked if r["context"]))
    print("难例:", sum(1 for r in picked if r["hard_case"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
