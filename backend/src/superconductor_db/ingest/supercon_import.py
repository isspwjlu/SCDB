"""Build the SuperCon database from the CSV files.

The NIMS SuperCon dataset is five CSV files of measured transition
temperatures, split by the family the material belongs to. It is static
(the files have not changed since 2025) and small, so it goes into a
database of its own rather than into the corpus the extraction fills.

Four things the data does that a naive import gets wrong:

* the composition column is called ``formula`` in two files and
  ``composition`` in the other three;
* the same composition appears at several pressures in the nickelate
  file, which is a **series**, not a duplicate -- and the low-pressure
  points have a Tc of zero, meaning the material does not superconduct
  at that pressure. Those rows are data, and they are kept;
* one row is repeated word for word, and that one is dropped;
* the nickelate compositions carry the synthesis route in brackets --
  ``La2SmNi2O7(C6)``, ``Pr4Ni3O10(S1)`` -- and the route is not part of
  the formula. It is dropped, and the file it came from is kept in
  ``source_file``/``source_row``, so the route is still traceable.
"""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# The synthesis route at the end of a composition: ``(C6)``, ``(S1)``,
# ``(No2)``, ``(Nat.R1)``. Only a bracket at the very end is a route,
# so a formula that opens with a group -- ``(La,Sr)2CuO4`` -- keeps it.
_SAMPLE_ROUTE = re.compile(r"\s*\([^()]*\)\s*$")

# The category of each file, taken from its name.
CATEGORY_BY_FILE: dict[str, str] = {
    "superconductors_conventional.csv": "conventional",
    "superconductors_cuprate.csv": "cuprate",
    "superconductors_heavyfermion.csv": "heavyfermion",
    # The file is named "ion" but the materials in it are the iron
    # based ones -- Gd0.8Th0.2Fe1As1O1 and the rest of them.
    "superconductors_ion.csv": "febased",
    # The nickelate pressure data comes from the workbook, not from the
    # old CSV: it is the curated selection, with the synthesis route
    # written into the composition (La3Ni2O7(Nat.R1) and the like), and
    # it holds the measured points only -- the old file also listed the
    # pressures at which the material does not superconduct.
    "不同压力下镍氧化物超导体-select.xlsx": "ni_pressure",
}

# The files that are no longer part of the dataset.
#
# The nickelate pressure data comes from the workbook now. The old CSV is
# kept on disk, but importing it would put its rows into the database
# under a category of their own -- a name that nothing else uses, and
# the rows themselves are the superseded ones.
#
# The "-all" workbook is the uncurated version of the same data: it
# holds every measurement rather than the selected ones, so importing it
# would duplicate the selected rows and add the rest.
_EXCLUDED_FILES: frozenset[str] = frozenset({
    "Ni_pressure.csv",
    "不同压力下镍氧化物超导体-all.xlsx",
})

# The column the composition is written in, per spelling.
_COMPOSITION_COLUMNS = ("composition", "formula")

_SCHEMA = """
CREATE TABLE supercon_items (
    id          INTEGER PRIMARY KEY,
    category    TEXT    NOT NULL,
    composition TEXT    NOT NULL,
    tc          REAL    NOT NULL,
    pressure    REAL,
    source_file TEXT    NOT NULL,
    source_row  INTEGER NOT NULL
);
CREATE INDEX idx_supercon_category ON supercon_items(category);
CREATE INDEX idx_supercon_tc ON supercon_items(tc);
CREATE INDEX idx_supercon_composition ON supercon_items(composition);
"""


def _category_for(path: Path) -> str:
    """The category a file belongs to."""
    known = CATEGORY_BY_FILE.get(path.name)
    if known:
        return known
    # A file the mapping does not know about is still usable: the name
    # carries the family.
    stem = path.stem
    if stem.startswith("superconductors_"):
        return stem[len("superconductors_"):]
    return stem


def _formula(composition: str) -> str:
    """The chemical formula of a composition.

    The nickelate workbook writes the synthesis route after the
    formula -- ``La2SmNi2O7(C6)`` -- and the route belongs to the
    sample, not to the material.
    """
    return _SAMPLE_ROUTE.sub("", composition).strip()


def _read_rows(path: Path) -> list[tuple[str, float, float | None]]:
    """The rows of one file, as (composition, Tc, pressure)."""
    if path.suffix.casefold() == ".xlsx":
        return _read_xlsx_rows(path)
    rows: list[tuple[str, float, float | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        column = next(
            (name for name in _COMPOSITION_COLUMNS if name in fieldnames), None
        )
        if column is None:
            log.warning("%s: no composition column, skipped", path.name)
            return rows
        has_pressure = "pressure" in fieldnames
        for record in reader:
            composition = _formula((record.get(column) or "").strip())
            if not composition:
                continue
            raw_tc = (record.get("Tc") or "").strip()
            if not raw_tc:
                continue
            try:
                tc = float(raw_tc)
            except ValueError:
                log.warning("%s: Tc %r is not a number, skipped", path.name, raw_tc)
                continue
            pressure: float | None = None
            if has_pressure:
                raw_pressure = (record.get("pressure") or "").strip()
                if raw_pressure:
                    try:
                        pressure = float(raw_pressure)
                    except ValueError:
                        pressure = None
            rows.append((composition, tc, pressure))
    return rows


def _read_xlsx_rows(path: Path) -> list[tuple[str, float, float | None]]:
    """The rows of a workbook, as (composition, Tc, pressure).

    The workbook has no header over the composition column, and it has
    blank rows between the materials, so the columns are read by
    position: composition, pressure, Tc.
    """
    import openpyxl

    rows: list[tuple[str, float, float | None]] = []
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        for row in sheet.iter_rows(values_only=True):
            if not row:
                continue
            composition = _formula(
                str(row[0]).strip() if row[0] is not None else ""
            )
            if not composition:
                # A blank row, or the header.
                continue
            tc = _as_float(row[2] if len(row) > 2 else None)
            if tc is None:
                continue
            pressure = _as_float(row[1] if len(row) > 1 else None)
            rows.append((composition, tc, pressure))
    finally:
        workbook.close()
    log.info("%s: %d rows", path.name, len(rows))
    return rows


def _as_float(value: object) -> float | None:
    """A cell as a number, or None when it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def build_supercon_db(csv_dir: Path, db_path: Path) -> int:
    """Build the SuperCon database and return the number of rows.

    The build is idempotent: the table is replaced, so running it again
    leaves exactly the same database behind.
    """
    csv_dir = Path(csv_dir)
    db_path = Path(db_path)
    files = sorted(
        p for p in csv_dir.iterdir()
        if p.is_file()
        and p.suffix.casefold() in (".csv", ".xlsx")
        and p.name not in _EXCLUDED_FILES
    )
    if not files:
        log.warning("no data files under %s", csv_dir)
        return 0

    records: list[tuple[str, str, float, float | None, str, int]] = []
    seen: set[tuple[str, str, float, float | None]] = set()
    for path in files:
        category = _category_for(path)
        for index, (composition, tc, pressure) in enumerate(_read_rows(path), start=2):
            key = (category, composition, tc, pressure)
            if key in seen:
                # The same measurement, written twice.
                continue
            seen.add(key)
            records.append((category, composition, tc, pressure, path.name, index))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.execute("DROP TABLE IF EXISTS supercon_items")
        db.executescript(_SCHEMA)
        db.executemany(
            "insert into supercon_items "
            "(category, composition, tc, pressure, source_file, source_row) "
            "values (?, ?, ?, ?, ?, ?)",
            records,
        )
        db.commit()
    finally:
        db.close()

    log.info(
        "supercon: %d rows from %d files -> %s", len(records), len(files), db_path
    )
    return len(records)


__all__ = ["CATEGORY_BY_FILE", "build_supercon_db"]
