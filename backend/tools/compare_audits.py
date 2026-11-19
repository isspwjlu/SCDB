"""Why the machine and the expert disagree: the evidence of the
records the machine rejected but the expert accepted.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PAPER = BACKEND.parent / "paper"
QWEN = BACKEND / "data" / "cache" / "audit_qwen.jsonl"
SHEET = PAPER / "audit" / "expert_audit.html"
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"


def main() -> int:
    rows = [
        json.loads(line)
        for line in QWEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judged = {r["id"]: r for r in rows if r.get("value") != "error"}
    ids = list(
        dict.fromkeys(re.findall(r'"id":\s*"([0-9a-f-]{36})"', SHEET.read_text(encoding="utf-8")))
    )
    shared = [judged[i] for i in ids if i in judged]

    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    rejected = [
        r
        for r in shared
        if not (r["value"] == "yes" and r["material"] == "yes")
    ]
    print(f"机器判为不对的 {len(rejected)} 条（专家判 4 条不对）：\n")
    for r in rejected:
        row = db.execute(
            "select chemical_composition, tc_value, notes from materials where id = ?",
            (r["id"],),
        ).fetchone()
        evidence = (row["notes"] if row else "") or ""
        print(f"{r['composition']:22} Tc={r['tc']:<8} [{r['family']}]")
        print(f"   {evidence[:190]}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
