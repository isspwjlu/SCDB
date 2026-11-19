"""Freeze the audit artefacts into the paper's own directory.

The paper says that the audit sample, the independent model's verdicts
and the expert sheet are included with the code, so that every number
it quotes can be recomputed.  The working copies live in
``backend/data/cache``, which is not tracked; this script copies them
into ``paper/audit``, which is, and checks that the two agree.

Usage::

    python tools/freeze_audit.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CACHE = BACKEND / "data" / "cache"
FROZEN = BACKEND.parent / "paper" / "audit"

ARTEFACTS = (
    "audit_qwen.jsonl",
    "audit_sample.jsonl",
    "pipeline_stats.json",
    "scdb_materials.csv",
)


def main() -> int:
    FROZEN.mkdir(parents=True, exist_ok=True)
    for name in ARTEFACTS:
        source = CACHE / name
        if not source.exists():
            print(f"  ! {name} 不存在，跳过")
            continue
        target = FROZEN / name
        shutil.copy2(source, target)
        same = target.read_bytes() == source.read_bytes()
        print(f"  ✓ {name}  {target.stat().st_size / 1024:.0f} KB  {'一致' if same else '不一致'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
