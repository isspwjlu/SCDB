"""Tests for ``paper_ingest.ingest_one`` — the per-paper insert path.

These exist because the insert path builds ``Material(**row)`` from the shared
mapper, and a key collision between the mapper output and the explicit kwargs
raises ``TypeError: got multiple values for keyword argument`` at runtime. That
regression silently lost every new row (the paper was created, then the insert
raised), which is invisible in unit tests that only exercise the mapper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401
from superconductor_db.db import Base
from superconductor_db.db.models.material import Material
from superconductor_db.db.models.paper import Paper
from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.paper_ingest import ingest_one
from superconductor_db.schemas.common import Quantity
from superconductor_db.schemas.material import (
    SuperconductingProperty,
    SuperconductingRecord,
    TcUnderPressure,
)

DOI = "10.1234/test-paper"
PAPER_TEXT = (
    "LaH10 becomes superconducting at Tc = 250 K under 170 GPa pressure. "
    "The upper critical field reaches 220 T."
)


def _record() -> SuperconductingRecord:
    """A record with a value in most column families."""
    sp = SuperconductingProperty(
        Tc=Quantity(value=250.0, unit="K"),
        Hc2=Quantity(value=220.0, unit="T"),
        Superconductor_Family="high-pressure hydride",
        Mechanism="s-wave BCS",
    )
    return SuperconductingRecord(
        material_chemical_composition="LaH10",
        experimental=True,
        doi=DOI,
        source="llm_extract",
        superconducting_property=sp,
        tc_under_pressure=TcUnderPressure(
            pressures=[0.0, 170.0], tcs=[0.0, 250.0], method="resistivity"
        ),
    )


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.integration
async def test_ingest_one_inserts_row_and_paper(session_factory, tmp_path) -> None:
    """A new record lands in materials AND gets linked to a papers row."""
    fake_pdf = tmp_path / "stub.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 stub")

    client = object.__new__(LLMClient)

    # Patched unbound, so ``self`` (the extractor) binds to the first
    # positional parameter.
    async def fake_extract_all(
        self: Any, text: str, doi: str | None = None, **kwargs: Any
    ) -> list[Any]:
        return [_record()]

    with (
        patch(
            "superconductor_db.ingest.paper_ingest.download_pdf",
            new=AsyncMock(return_value=fake_pdf),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.parse_pdf_to_markdown",
            new=AsyncMock(return_value=PAPER_TEXT),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.SuperconductingExtractor.extract_all",
            new=fake_extract_all,
        ),
    ):
        extracted, stored, downloaded = await ingest_one(session_factory, DOI)

    assert (extracted, stored, downloaded) == (1, 1, True)

    with session_factory() as s:
        # The provenance chain: papers row exists and material points at it.
        paper = s.execute(select(Paper).where(Paper.doi == DOI)).scalar_one()
        row = s.execute(select(Material)).scalar_one()
        assert row.paper_id == paper.id
        assert row.source == "paper_ingest"
        # Fields beyond composition/Tc must survive the insert.
        assert row.hc2_value == 220.0
        assert row.superconductor_family == "high-pressure hydride"
        assert row.tc_under_pressure is not None
        assert row.tc_under_pressure["pressures"] == [0.0, 170.0]


@pytest.mark.integration
async def test_ingest_one_merges_into_existing_row(session_factory, tmp_path) -> None:
    """Re-ingesting the same paper fills NULL fields instead of duplicating."""
    fake_pdf = tmp_path / "stub.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 stub")

    # Seed a row with only composition + Tc, as the pre-fix insert produced.
    with session_factory() as s:
        s.add(
            Material(
                chemical_composition="LaH10",
                doi=DOI,
                source="paper_ingest",
                tc_value=250.0,
                tc_unit="K",
            )
        )
        s.commit()

    client = object.__new__(LLMClient)

    # Patched unbound, so ``self`` (the extractor) binds to the first
    # positional parameter.
    async def fake_extract_all(
        self: Any, text: str, doi: str | None = None, **kwargs: Any
    ) -> list[Any]:
        return [_record()]

    with (
        patch(
            "superconductor_db.ingest.paper_ingest.download_pdf",
            new=AsyncMock(return_value=fake_pdf),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.parse_pdf_to_markdown",
            new=AsyncMock(return_value=PAPER_TEXT),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.SuperconductingExtractor.extract_all",
            new=fake_extract_all,
        ),
    ):
        _, stored, _ = await ingest_one(session_factory, DOI)

    assert stored == 0
    with session_factory() as s:
        rows = s.execute(select(Material)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        # The merge path is what made Hc2/family work; it used to silently
        # do nothing because it probed attributes that do not exist.
        assert row.hc2_value == 220.0
        assert row.superconductor_family == "high-pressure hydride"
        assert row.paper_id is not None


@pytest.mark.integration
async def test_ingest_one_skips_known_paper_when_asked(
    session_factory, tmp_path
) -> None:
    """skip_known_papers=True skips identifiers that already have a Paper row.

    The curated loop passes this flag: an existing Paper row means the paper
    was downloaded and extracted before, so the ~8-call re-extraction would
    only hit per-row dedup. The download must never be attempted.
    """
    with session_factory() as s:
        s.add(
            Paper(
                id=__import__("uuid").uuid4(),
                doi=DOI,
                title="seeded",
                source="doi",
            )
        )
        s.commit()

    download = AsyncMock(side_effect=AssertionError("download must not run"))
    with patch(
        "superconductor_db.ingest.paper_ingest.download_pdf", new=download
    ):
        extracted, stored, downloaded = await ingest_one(
            session_factory, DOI, skip_known_papers=True
        )

    assert (extracted, stored, downloaded) == (0, 0, False)
    download.assert_not_called()


@pytest.mark.integration
async def test_ingest_one_default_still_processes_known_paper(
    session_factory, tmp_path
) -> None:
    """Without the flag an existing Paper row does not block re-ingestion.

    This is the backfill path: a fresh extraction must still run and merge
    into the existing rows even though the paper was ingested before.
    """
    fake_pdf = tmp_path / "stub.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 stub")
    with session_factory() as s:
        s.add(
            Paper(
                id=__import__("uuid").uuid4(),
                doi=DOI,
                title="seeded",
                source="doi",
            )
        )
        s.commit()

    async def fake_extract_all(
        self: Any, text: str, doi: str | None = None, **kwargs: Any
    ) -> list[Any]:
        return [_record()]

    with (
        patch(
            "superconductor_db.ingest.paper_ingest.download_pdf",
            new=AsyncMock(return_value=fake_pdf),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.parse_pdf_to_markdown",
            new=AsyncMock(return_value=PAPER_TEXT),
        ),
        patch(
            "superconductor_db.ingest.paper_ingest.SuperconductingExtractor.extract_all",
            new=fake_extract_all,
        ),
    ):
        extracted, stored, downloaded = await ingest_one(session_factory, DOI)

    assert (extracted, stored, downloaded) == (1, 1, True)


@pytest.mark.unit
def test_record_to_row_keys_do_not_collide_with_insert_kwargs() -> None:
    """The insert kwargs must not overlap the mapper's output keys.

    A duplicate key would raise ``TypeError: got multiple values for keyword
    argument`` at runtime and lose the row.
    """
    from superconductor_db.db.repository import MaterialsRepository

    mapper_keys = set(MaterialsRepository._record_to_row(_record()))
    insert_kwargs = {"id", "paper_id", "source"}
    # ``source`` is assigned via the row dict (not a duplicate kwarg), so the
    # constructor only receives id + paper_id explicitly.
    constructor_kwargs = {"id", "paper_id"}
    assert not (constructor_kwargs & mapper_keys)
    # And ``source`` must be present in the mapper output so it can be
    # overridden without duplication.
    assert "source" in mapper_keys
    assert insert_kwargs - {"source"} == {"id", "paper_id"}
