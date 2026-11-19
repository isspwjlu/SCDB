"""Dump the frozen corpus snapshot to CSV, for the figure tools.

The snapshot is the copy the paper's numbers describe; the live
database moves at every sync, so the figures must not read it.

Usage::

    python tools/export_snapshot_csv.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "data" / "cache" / "scdb_snapshot.db"
OUT = BACKEND / "data" / "cache" / "scdb_materials.csv"

COLUMNS = [
    "chemical_composition",
    "tc_value",
    "superconductor_family",
    "experimental",
    "doi",
    "hc2_value",
    "energy_gap_value",
    "coherence_length_value",
    "penetration_depth_value",
    "hc1_value",
    "t_cdw_value",
    "critical_current_density_value",
    "pairing_symmetry",
    "mechanism",
]


def main() -> int:
    db = sqlite3.connect(SNAPSHOT)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM materials"
    ).fetchall()
    db.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: ("" if row[key] is None else row[key])
                    for key in COLUMNS
                }
            )
    print(f"{len(rows)} 条 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
