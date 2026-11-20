"""Building the SuperCon database from the CSV files.

The dataset is static and small (12322 rows in five files), so it is
imported into a database of its own rather than into the corpus the
extraction fills. These tests pin down the things the data actually
does: the two spellings of the composition column, the pressure series
whose low-pressure points are not superconducting at all, and the one
row that is a true duplicate.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from superconductor_db.ingest.supercon_import import (
    CATEGORY_BY_FILE,
    build_supercon_db,
)

CSV_HEADER = "formula,composition,Tc,pressure"


def _write_workbook(
    directory: Path, name: str, rows: list[tuple[str, float, float]]
) -> Path:
    """A nickelate-style workbook: no header over the composition, and
    the columns are composition, pressure, Tc."""
    import openpyxl

    path = directory / name
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([None, "Pressure", "Tc"])
    for composition, pressure, tc in rows:
        sheet.append([composition, pressure, tc])
    workbook.save(path)
    return path


def _write(directory: Path, name: str, rows: list[tuple[str, ...]]) -> Path:
    path = directory / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(row)
    return path


@pytest.mark.unit
def test_the_composition_column_is_read_under_either_name(tmp_path: Path) -> None:
    """Two of the files call it ``formula`` and three call it
    ``composition``; they are the same column."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(csv_dir, "superconductors_conventional.csv", [("formula", "Tc"), ("B2Mg", "39")])
    _write(csv_dir, "superconductors_cuprate.csv", [("composition", "Tc"), ("Hg122", "133")])

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = dict(
            db.execute("select composition, tc from supercon_items").fetchall()
        )
    finally:
        db.close()
    assert rows == {"B2Mg": 39.0, "Hg122": 133.0}


@pytest.mark.unit
def test_the_category_comes_from_the_file_name(tmp_path: Path) -> None:
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(csv_dir, "superconductors_heavyfermion.csv", [("composition", "Tc"), ("ThPd3B3C", "21.5")])
    _write_workbook(
        csv_dir,
        "不同压力下镍氧化物超导体-select.xlsx",
        [("LaNiO2", 5.0, 20.1)],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "select category, composition from supercon_items order by category"
        ).fetchall()
    finally:
        db.close()
    assert rows == [
        ("heavyfermion", "ThPd3B3C"),
        ("ni_pressure", "LaNiO2"),
    ]
    assert "conventional" in CATEGORY_BY_FILE.values()


@pytest.mark.unit
def test_the_rows_that_are_not_superconducting_are_kept(tmp_path: Path) -> None:
    """A Tc of zero is a real data point: it is the pressure at which
    the material does not superconduct yet."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(
        csv_dir,
        "nickelates.csv",
        [("formula", "pressure", "Tc"), ("La3Ni2O7", "0", "0"), ("La3Ni2O7", "13", "76.5")],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "select pressure, tc from supercon_items order by pressure"
        ).fetchall()
    finally:
        db.close()
    assert rows == [(0.0, 0.0), (13.0, 76.5)]


@pytest.mark.unit
def test_the_pressure_series_is_kept_whole(tmp_path: Path) -> None:
    """The same composition at several pressures is a series, not a
    duplicate."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(
        csv_dir,
        "nickelates.csv",
        [
            ("formula", "pressure", "Tc"),
            ("La3Ni2O7", "0", "0"),
            ("La3Ni2O7", "13", "76.5"),
            ("La3Ni2O7", "17.6", "78.5"),
        ],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        count = db.execute("select count(*) from supercon_items").fetchone()[0]
    finally:
        db.close()
    assert count == 3


@pytest.mark.unit
def test_a_row_that_is_repeated_word_for_word_is_dropped(tmp_path: Path) -> None:
    """The data has one of these: the same composition at the same
    pressure, twice."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(
        csv_dir,
        "nickelates.csv",
        [
            ("formula", "pressure", "Tc"),
            ("LaNiO2", "0", "0"),
            ("LaNiO2", "0", "0"),
            ("LaNiO2", "5", "20.1"),
        ],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "select pressure, tc from supercon_items order by pressure"
        ).fetchall()
    finally:
        db.close()
    assert rows == [(0.0, 0.0), (5.0, 20.1)]


@pytest.mark.unit
def test_the_rows_of_a_file_without_a_pressure_column_have_no_pressure(
    tmp_path: Path,
) -> None:
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(csv_dir, "superconductors_ion.csv", [("composition", "Tc"), ("GdFeAsO", "56.5")])

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "select pressure, source_file from supercon_items"
        ).fetchone()
    finally:
        db.close()
    assert row[0] is None
    assert row[1].endswith("superconductors_ion.csv")


@pytest.mark.unit
def test_the_build_can_be_run_again(tmp_path: Path) -> None:
    """The API builds the database when it is missing, so the build has
    to leave the same result behind every time."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(csv_dir, "superconductors_cuprate.csv", [("composition", "Tc"), ("Hg122", "133")])

    db_path = tmp_path / "sc.db"
    first = build_supercon_db(csv_dir, db_path)
    second = build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        count = db.execute("select count(*) from supercon_items").fetchone()[0]
    finally:
        db.close()
    assert first == second == 1
    assert count == 1


@pytest.mark.unit
def test_the_real_dataset_imports(tmp_path: Path) -> None:
    """The dataset as it ships: five files, and every row lands."""
    source = Path(__file__).resolve().parents[3] / "SuperCon数据"
    if not source.exists():
        pytest.skip("the SuperCon data directory is not on this machine")

    db_path = tmp_path / "sc.db"
    count = build_supercon_db(source, db_path)

    db = sqlite3.connect(db_path)
    try:
        categories = dict(
            db.execute(
                "select category, count(*) from supercon_items group by category"
            ).fetchall()
        )
        total = db.execute("select count(*) from supercon_items").fetchone()[0]
        zero = db.execute(
            "select count(*) from supercon_items where tc = 0"
        ).fetchone()[0]
        pressures = db.execute(
            "select count(*) from supercon_items where pressure is not null"
        ).fetchone()[0]
    finally:
        db.close()

    # The four CSV files hold 12196 rows, and the nickelate workbook 75.
    assert count == 12271
    assert total == 12271
    assert set(categories) == {
        "conventional",
        "cuprate",
        "febased",
        "heavyfermion",
        "ni_pressure",
    }
    # The nickelate workbook is the only file with pressures, and it
    # holds the measured points only -- the old CSV also listed the
    # pressures at which the material does not superconduct.
    assert pressures == 75
    assert zero == 0


@pytest.mark.unit
def test_a_workbook_is_read_by_position(tmp_path: Path) -> None:
    """The nickelate workbook has no header over the composition column
    and blank rows between the materials."""
    import openpyxl

    from superconductor_db.ingest.supercon_import import _read_rows

    path = tmp_path / "不同压力下镍氧化物超导体-select.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([None, "Pressure", "Tc"])
    sheet.append(["La3Ni2O7", 12.95958, 76.31246])
    sheet.append([None, None, None])
    sheet.append(["Pr4Ni3O10(S1)", 70.85877, 36.82986])
    workbook.save(path)

    rows = _read_rows(path)

    assert rows == [
        ("La3Ni2O7", 76.31246, 12.95958),
        ("Pr4Ni3O10", 36.82986, 70.85877),
    ]


@pytest.mark.unit
def test_the_synthesis_route_is_not_part_of_the_formula(tmp_path: Path) -> None:
    """The nickelate compositions carry the synthesis route in
    brackets -- La2SmNi2O7(C6), Pr4Ni3O10(S1) -- and the route belongs
    to the sample, not to the material."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write_workbook(
        csv_dir,
        "不同压力下镍氧化物超导体-select.xlsx",
        [("La2SmNi2O7(C6)", 20.0, 92.02), ("Pr4Ni3O10(S1)", 70.0, 39.36)],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "select composition from supercon_items order by composition"
        ).fetchall()
    finally:
        db.close()
    assert [row[0] for row in rows] == ["La2SmNi2O7", "Pr4Ni3O10"]


@pytest.mark.unit
def test_a_formula_that_opens_with_a_group_keeps_it(tmp_path: Path) -> None:
    """Only a bracket at the very end is a synthesis route."""
    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    _write(
        csv_dir,
        "superconductors_cuprate.csv",
        [("composition", "Tc"), ("(La,Sr)2CuO4", "38")],
    )

    db_path = tmp_path / "sc.db"
    build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        row = db.execute("select composition from supercon_items").fetchone()
    finally:
        db.close()
    assert row[0] == "(La,Sr)2CuO4"


@pytest.mark.unit
def test_the_nickelate_data_comes_from_the_workbook(tmp_path: Path) -> None:
    """The old CSV is superseded and must not be imported."""
    import openpyxl

    from superconductor_db.ingest.supercon_import import (
        _EXCLUDED_FILES,
        CATEGORY_BY_FILE,
        build_supercon_db,
    )

    assert "Ni_pressure.csv" in _EXCLUDED_FILES
    assert CATEGORY_BY_FILE["不同压力下镍氧化物超导体-select.xlsx"] == "ni_pressure"

    csv_dir = tmp_path / "SuperCon数据"
    csv_dir.mkdir()
    (csv_dir / "Ni_pressure.csv").write_text(
        "formula,pressure,Tc\nLa3Ni2O7,0,0\nLa3Ni2O7,13,76.5\n", encoding="utf-8"
    )
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([None, "Pressure", "Tc"])
    sheet.append(["La3Ni2O7(Nat.R1)", 12.95958, 76.31246])
    workbook.save(csv_dir / "不同压力下镍氧化物超导体-select.xlsx")

    db_path = tmp_path / "sc.db"
    count = build_supercon_db(csv_dir, db_path)

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "select category, composition, tc, pressure, source_file "
            "from supercon_items"
        ).fetchall()
    finally:
        db.close()

    assert count == 1
    assert rows == [
        ("ni_pressure", "La3Ni2O7", 76.31246, 12.95958,
         "不同压力下镍氧化物超导体-select.xlsx"),
    ]
