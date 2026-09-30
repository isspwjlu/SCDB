"""Unit tests for :mod:`superconductor_db.db.repository`."""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from superconductor_db.db.base import Base
from superconductor_db.db.models.material import Material
from superconductor_db.db.repository import MaterialsRepository


@pytest.fixture()
def session() -> Iterator[Session]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _seed(session: Session, composition: str, **kwargs) -> Material:
    m = Material(chemical_composition=composition, source="unit-test", **kwargs)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def test_list_paginates(session: Session) -> None:
    for i in range(7):
        _seed(session, f"Cu{i}", tc_value=float(10 + i))
    repo = MaterialsRepository(session)
    items, total = repo.list({}, page=1, per_page=3)
    assert total == 7
    assert len(items) == 3
    items_p2, _ = repo.list({}, page=2, per_page=3)
    assert len(items_p2) == 3
    items_p3, _ = repo.list({}, page=3, per_page=3)
    assert len(items_p3) == 1


def test_list_filters_by_family(session: Session) -> None:
    _seed(session, "Cu1", superconductor_family="cuprate", tc_value=90.0)
    _seed(session, "MgB2", superconductor_family="MgB2", tc_value=39.0)
    _seed(session, "FeSe", superconductor_family="iron-based", tc_value=8.0)
    repo = MaterialsRepository(session)
    items, total = repo.list({"family": "cuprate"})
    assert total == 1
    assert items[0].chemical_composition == "Cu1"


def test_list_filters_by_tc_range(session: Session) -> None:
    _seed(session, "A", tc_value=10.0)
    _seed(session, "B", tc_value=20.0)
    _seed(session, "C", tc_value=30.0)
    repo = MaterialsRepository(session)
    items, total = repo.list({"tc_min": 15, "tc_max": 25})
    assert total == 1
    assert items[0].chemical_composition == "B"


def test_list_search_ilike_matches_family(session: Session) -> None:
    _seed(session, "YBa2Cu3O7", superconductor_family="cuprate")
    _seed(session, "MgB2", superconductor_family="MgB2")
    repo = MaterialsRepository(session)
    items, total = repo.list({"search": "cuprate"})
    assert total == 1
    assert items[0].chemical_composition == "YBa2Cu3O7"


def test_aggregate_overview_returns_zero_when_empty(session: Session) -> None:
    repo = MaterialsRepository(session)
    summary = repo.aggregate_overview()
    assert summary["total"] == 0
    assert summary["max_tc"] is None
    assert summary["max_hc2"] is None
    assert summary["family_counts"] == {}


def test_aggregate_overview_counts_and_max(session: Session) -> None:
    _seed(session, "YBCO", superconductor_family="cuprate", tc_value=92.0, hc2_value=100.0)
    _seed(session, "Hg1223", superconductor_family="cuprate", tc_value=133.0, hc2_value=200.0)
    _seed(session, "MgB2", superconductor_family="MgB2", tc_value=39.0, hc2_value=50.0)
    repo = MaterialsRepository(session)
    summary = repo.aggregate_overview()
    assert summary["total"] == 3
    assert summary["max_tc"] == 133.0
    assert summary["max_hc2"] == 200.0
    assert summary["family_counts"] == {"cuprate": 2, "MgB2": 1}


def test_tc_distribution_empty_when_no_data(session: Session) -> None:
    repo = MaterialsRepository(session)
    assert repo.tc_distribution() == []


def test_tc_distribution_basic_buckets(session: Session) -> None:
    # All values fit into a single bucket when lo == hi, or several when spread.
    _seed(session, "A", tc_value=10.0)
    _seed(session, "B", tc_value=20.0)
    _seed(session, "C", tc_value=30.0)
    _seed(session, "D", tc_value=40.0)
    repo = MaterialsRepository(session)
    bins = repo.tc_distribution(bins=3)
    assert len(bins) == 3
    total = sum(b["count"] for b in bins)
    assert total == 4
    assert bins[0]["bin_start"] == 10.0


def test_family_pie_orders_by_count(session: Session) -> None:
    _seed(session, "A", superconductor_family="cuprate")
    _seed(session, "B", superconductor_family="cuprate")
    _seed(session, "C", superconductor_family="MgB2")
    repo = MaterialsRepository(session)
    pie = repo.family_pie()
    assert pie[0] == {"family": "cuprate", "count": 2}
    assert pie[1] == {"family": "MgB2", "count": 1}


def test_tc_vs_pressure_extracts_arrays(session: Session) -> None:
    _seed(
        session,
        "FeSe",
        tc_under_pressure={"pressures": [0, 1, 2], "tcs": [8, 10, 12], "method": "resistivity"},
    )
    _seed(session, "YBCO")  # no pressure data
    repo = MaterialsRepository(session)
    rows = repo.tc_vs_pressure()
    assert len(rows) == 1
    assert rows[0]["composition"] == "FeSe"
    assert rows[0]["pressures"] == [0.0, 1.0, 2.0]
    assert rows[0]["tcs"] == [8.0, 10.0, 12.0]


def test_get_returns_material_or_none(session: Session) -> None:
    m = _seed(session, "A", tc_value=1.0)
    repo = MaterialsRepository(session)
    assert repo.get(m.id) is not None
    assert repo.get(uuid.uuid4()) is None


def test_create_and_upsert_by_doi(session: Session) -> None:
    from superconductor_db.schemas.common import Quantity
    from superconductor_db.schemas.material import (
        SuperconductingProperty,
        SuperconductingRecord,
    )

    record = SuperconductingRecord(
        material_chemical_composition="FeSe",
        doi="10.1234/abc",
        superconducting_property=SuperconductingProperty(
            Tc=Quantity(value=8.0, unit="K"),
            Superconductor_Family="iron-based",
        ),
    )
    repo = MaterialsRepository(session)
    created = repo.create(record)
    assert created.id is not None

    # Upsert: same DOI -> same row updated.
    record2 = SuperconductingRecord(
        material_chemical_composition="FeSe",
        doi="10.1234/abc",
        superconducting_property=SuperconductingProperty(
            Tc=Quantity(value=9.0, unit="K"),
        ),
    )
    updated = repo.upsert_by_doi("10.1234/abc", record2)
    assert updated.id == created.id
    assert updated.tc_value == 9.0
