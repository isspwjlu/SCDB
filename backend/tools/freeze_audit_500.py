"""Freeze the audit over the 500-record sample.

The two judges' verdicts are collected in the working cache; this takes
the records that are in the sample, keeps the last verdict written for
each of them, and writes the frozen copies the paper is checked
against.

    python tools/freeze_audit_500.py
"""

from __future__ import annotations

import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
SAMPLE = BACKEND / "data" / "cache" / "audit_sample_500.jsonl"
WORK = BACKEND / "data" / "cache"
FROZEN = PAPER / "audit"

PAIRS = (
    ("audit_deepseek_full.jsonl", "audit_deepseek.jsonl"),
    ("audit_deepseek_pro_full.jsonl", "audit_deepseek_pro.jsonl"),
)


def main() -> int:
    wanted = {
        json.loads(line)["id"]
        for line in SAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    for source, target in PAIRS:
        path = WORK / source
        latest: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["id"] in wanted and row.get("value") != "error":
                latest[row["id"]] = row
        out = FROZEN / target
        with out.open("w", encoding="utf-8") as handle:
            for row in latest.values():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{target}: {len(latest)} / {len(wanted)} 条 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
