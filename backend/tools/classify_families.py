"""Classify the records that the extraction left without a family.

A record reaches the database with a family only when the extraction
named one.  The rest arrive with ``superconductor_family`` empty: they
are ordinary superconductors that the model's own prompt did not file,
not a category of their own.  This script gives every one of them a
family, one record at a time, from the composition, the transition
temperature, the title of the paper and the sentence the value came
from.

The verdicts are written to an audit file first, with the reason the
model gave, so that they can be read before they are trusted.  Nothing
is written to the database unless ``--apply`` is given.

Usage::

    python tools/classify_families.py --db _dev.db \
        --report data/cache/family_audit.jsonl
    python tools/classify_families.py --db _dev.db \
        --report data/cache/family_audit.jsonl --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

from superconductor_db.core.config import get_settings  # noqa: E402
from superconductor_db.ingest.llm_client import LLMClient  # noqa: E402

# The families the database already uses, plus the one the extraction
# prompt never produced: ordinary phonon-mediated superconductors.
FAMILIES = (
    "iron-based",
    "cuprate",
    "high-pressure hydride",
    "elemental",
    "MgB2",
    "layered dichalcogenide",
    "nickelate",
    "heavy-fermion",
    "A15 / intermetallic",
    "bismuth chalcogenide",
    "ruthenate",
    "conventional",
)

PROMPT = """You are classifying superconducting materials into families.

For one material you are given its composition, its transition
temperature, the title of the paper it was extracted from, and the
sentence in that paper the value came from.

Choose exactly one family:
- iron-based: iron pnictides, iron chalcogenides and their relatives
- cuprate: copper-oxide planes, YBCO and its siblings, the Bi, Tl, Hg
  and La cuprates, the infinite-layer cuprates
- high-pressure hydride: metal hydrides that superconduct under pressure
- elemental: a single chemical element
- MgB2: magnesium diboride and its doped relatives
- layered dichalcogenide: MoS2, WS2, NbSe2, TaS2 and their 2D relatives
- nickelate: the rare-earth nickel oxides, including the infinite-layer
  ones
- heavy-fermion: the Ce, U, Pr and Yb compounds with strong correlations
- A15 / intermetallic: Nb3Sn, V3Si and their solid solutions
- bismuth chalcogenide: Bi2Se3, Bi2Te3, BiS2 and their relatives
- ruthenate: the ruthenium oxides such as Sr2RuO4
- conventional: ordinary phonon-mediated s-wave superconductors that fit
  none of the families above, such as thin films and alloys of simple
  metals, nitrides, carbides, borides and high-entropy alloys of
  refractory metals.  Do not use this label for a material whose
  superconductivity is described as unconventional, topological, or as
  arising from strong electronic correlations.

Answer "unclassified" when the material genuinely belongs to none of
them: topological or Weyl semimetals, quasicrystals, amorphous
materials, and compounds whose superconductivity is unconventional
without being one of the known heavy fermions.

Reply with one JSON object and nothing else:
{{"family": "<a family from the list, or unclassified>", "reason": "<one short sentence>"}}

Composition: {composition}
Transition temperature: {tc} {unit}
Paper title: {title}
Sentence the value came from:
{evidence}
"""


def _meta(raw: str | None) -> dict:
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


def load_records(db_path: Path) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT m.id, m.chemical_composition, m.tc_value, m.tc_unit, m.notes, "
        "       m.meta, p.title, p.abstract "
        "FROM materials m LEFT JOIN papers p ON p.id = m.paper_id "
        "WHERE m.superconductor_family IS NULL OR m.superconductor_family = ''"
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
                "unit": row["tc_unit"] or "K",
                "evidence": (row["notes"] or "").removeprefix("Tc evidence:").strip(),
                "title": row["title"] or "",
                "source_dois": meta.get("source_dois", []),
            }
        )
    return records


async def classify(records: list[dict], workers: int) -> tuple[list[dict], dict]:
    settings = get_settings()
    client = LLMClient(settings)
    semaphore = asyncio.Semaphore(workers)
    done = 0

    async def one(record: dict) -> dict:
        nonlocal done
        prompt = PROMPT.format(
            composition=record["composition"],
            tc=record["tc"],
            unit=record["unit"],
            title=record["title"] or "(no title)",
            evidence=record["evidence"] or "(no sentence)",
        )
        verdict: dict = {"family": "", "reason": "", "error": ""}
        async with semaphore:
            try:
                raw = await client.chat(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=1024,
                )
                block = _first_json(raw)
                if block:
                    family = str(block.get("family", "")).strip()
                    verdict["family"] = family
                    verdict["reason"] = str(block.get("reason", "")).strip()
                else:
                    verdict["error"] = "no json"
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
                verdict["error"] = str(exc)[:200]
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(records)}", flush=True)
        return {**record, **verdict, "known": verdict["family"] in FAMILIES}

    return list(await asyncio.gather(*(one(record) for record in records))), client.usage


def _first_json(raw: str) -> dict | None:
    import re

    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def apply(db_path: Path, verdicts: list[dict]) -> int:
    """Write the verdicts into the database, keeping a copy to fall back on."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_name(db_path.name + f".prefamily_{stamp}")
    shutil.copy2(db_path, backup)
    db = sqlite3.connect(db_path)
    written = 0
    try:
        for verdict in verdicts:
            if not verdict["known"]:
                continue
            db.execute(
                "UPDATE materials SET superconductor_family = ? WHERE id = ?",
                (verdict["family"], verdict["id"]),
            )
            written += 1
        db.commit()
    finally:
        db.close()
    print(f"写了 {written} 条；原库备份在 {backup.name}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(BACKEND / "_dev.db"))
    parser.add_argument("--report", default=str(BACKEND / "data" / "cache" / "family_audit.jsonl"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--from-report",
        default="",
        help="apply the verdicts of an audit file instead of asking the "
        "model again; the file is the one --report wrote",
    )
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.from_report:
        source = Path(args.from_report)
        verdicts = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        usage = {}
        print(f"从 {source.name} 读入 {len(verdicts)} 条判定")
        if args.apply:
            apply(db_path, verdicts)
        return 0

    records = load_records(db_path)
    if args.limit:
        records = records[: args.limit]
    print(f"待分类 {len(records)} 条（{db_path.name}）", flush=True)
    if not records:
        return 0

    verdicts, usage = asyncio.run(classify(records, args.workers))

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(json.dumps(verdict, ensure_ascii=False) + "\n")

    import collections

    counts = collections.Counter(v["family"] or "(failed)" for v in verdicts)
    print("\n判定分布：")
    for family, count in counts.most_common():
        print(f"  {family:26} {count}")
    failed = sum(1 for v in verdicts if not v["known"])
    print(f"\n无法归入已知家族的 {failed} 条；审计文件 {report}")
    print(f"调用 {usage.get('calls', 0)} 次，"
          f"输入 {usage.get('prompt_tokens', 0):,} token，"
          f"输出 {usage.get('completion_tokens', 0):,} token")

    if args.apply:
        apply(db_path, verdicts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
