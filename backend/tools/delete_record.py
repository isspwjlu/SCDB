"""Delete a record from the database, with a guard and a backup.

A row is only deleted when the composition and the transition
temperature are both given and match, so that a record cannot be
removed by accident through a name that matches more than one row.  The
database is copied before anything is written.

Usage::

    python tools/delete_record.py --db _dev.db \
        --composition HgBa2Ca2Cu3O9 --tc 300 --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="_dev.db")
    parser.add_argument("--composition", required=True)
    parser.add_argument("--tc", type=float, required=True)
    parser.add_argument(
        "--expect-notes",
        default="",
        help="only delete the row whose evidence sentence contains this",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT m.id, m.chemical_composition, m.tc_value, m.tc_unit, m.experimental, "
        "       m.superconductor_family, m.notes, p.doi, p.title "
        "FROM materials m LEFT JOIN papers p ON p.id = m.paper_id "
        "WHERE m.chemical_composition = ? AND m.tc_value = ?",
        (args.composition, args.tc),
    ).fetchall()

    if not rows:
        print(f"没有这样的记录：{args.composition} @ {args.tc}")
        db.close()
        return 1
    if args.expect_notes:
        rows = [r for r in rows if args.expect_notes in (r["notes"] or "")]
        if not rows:
            print(f"没有证据句里含 {args.expect_notes!r} 的记录")
            db.close()
            return 1

    print(f"将删除 {len(rows)} 条：")
    for row in rows:
        print(f"  id={row['id']}")
        print(f"  {row['chemical_composition']}  {row['tc_value']} {row['tc_unit']}  "
              f"experimental={row['experimental']}  family={row['superconductor_family']}")
        print(f"  {row['title']}")
        print(f"  {(row['notes'] or '')[:160]}")
        print(f"  {row['doi']}")

    if not args.apply:
        print("\n（没有 --apply，未写入）")
        db.close()
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = db_path.with_name(db_path.name + f".predelete_{stamp}")
    db.close()
    shutil.copy2(db_path, backup)

    db = sqlite3.connect(db_path)
    deleted = 0
    for row in rows:
        cursor = db.execute("DELETE FROM materials WHERE id = ?", (row["id"],))
        deleted += cursor.rowcount
    db.commit()
    db.close()
    print(f"\n删了 {deleted} 条；备份在 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
