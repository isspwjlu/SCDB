"""Integration test for the ingest worker (Task 29).

This test is gated behind ``RUN_INTEGRATION=1`` because it requires
Testcontainers Postgres. With deps not installed in this environment, the
test file is committed for CI to pick up; it is not run locally.

Behaviour under test:

1. Insert one ``ingest_jobs`` row whose query string maps to a single paper.
2. Patch the ``scansci-pdf`` subprocess and the ``LLMClient`` to deterministic
   stubs returning one superconductor record.
3. Run :func:`superconductor_db.ingest.runner.run_job` directly (avoids
   needing a full worker process) and assert a row landed in ``materials``.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from superconductor_db.db.models.ingest import IngestJob, IngestRun
from superconductor_db.db.models.material import Material
from superconductor_db.ingest.runner import run_job


def _stub_records() -> list[dict[str, Any]]:
    """A single synthetic record mimicking what the LLM extractor would return."""
    return [
        {
            "material_chemical_composition": "YBa2Cu3O7",
            "experimental": True,
            "source": "llm_extract",
            "superconducting_property": {
                "Tc": {"value": 92, "unit": "K"},
                "Superconductor_Family": "cuprate",
            },
        }
    ]


@pytest.mark.integration
async def test_worker_processes_one_job_and_inserts_material(db_session) -> None:
    """Given 1 queued job, run_job inserts a material row."""
    # 1. Seed a single ingest job.
    run = IngestRun(status="running")
    db_session.add(run)
    db_session.flush()
    job = IngestJob(query="superconducting AND (Tc OR cuprate)", status="pending")
    db_session.add(job)
    db_session.commit()

    # 2. Build a stub LLM client that always returns the synthetic record.
    from superconductor_db.ingest.llm_client import LLMClient

    async def fake_extract_json(messages):  # noqa: ANN001
        # First call (composition) returns one formula; subsequent dimension
        # prompts return its data.
        from superconductor_db.ingest.llm_client import LLMClient as _LC
        if not hasattr(fake_extract_json, "called"):
            fake_extract_json.called = 0
        fake_extract_json.called += 1
        if fake_extract_json.called == 1:
            return [{"Material_Chemical_Composition": "YBa2Cu3O7", "Experimental": True}]
        return [{"YBa2Cu3O7": {"Tc": {"value": 92, "unit": "K"}, "Superconductor_Family": "cuprate"}}]

    client = object.__new__(LLMClient)
    client.extract_json = fake_extract_json  # type: ignore[method-assign]

    # 3. Patch scansci-pdf download + PDF parse.
    tmp_pdf = Path("/tmp/stub.pdf")
    tmp_pdf.write_bytes(b"%PDF-1.4 stub")

    with (
        patch(
            "superconductor_db.ingest.runner.download_pdf",
            new=AsyncMock(return_value=tmp_pdf),
        ),
        patch(
            "superconductor_db.ingest.runner.parse_pdf_to_markdown",
            new=AsyncMock(return_value="YBa2Cu3O7 Tc = 92 K cuprate"),
        ),
        patch(
            "superconductor_db.ingest.runner.search_all_sources",
            new=AsyncMock(
                return_value=[
                    MagicMock(
                        title="YBCO Tc measurement",
                        doi="10.1234/test",
                        abstract="We measured Tc...",
                        source="arxiv",
                        authors=[],
                        published_date=None,
                        journal=None,
                        url=None,
                    )
                ]
            ),
        ),
    ):
        result = await run_job(
            job.query,
            db=db_session,
            client=client,
            run_id=job.id,
        )

    assert result.papers_searched == 1
    assert result.papers_downloaded == 1
    assert result.records_extracted >= 1
    assert result.records_stored >= 1

    # 4. Verify a material row landed.
    rows = db_session.scalars(select(Material)).all()
    assert any(r.chemical_composition == "YBa2Cu3O7" for r in rows), [r.chemical_composition for r in rows]