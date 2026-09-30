"""Unit tests for the paper search module (Task 28).

We patch :class:`httpx.AsyncClient` to return canned JSON / XML responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superconductor_db.ingest.paper_search import (
    PaperMetadata,
    SC_SEARCH_QUERIES,
    _deduplicate,
    is_likely_sc_paper,
    search_all_sources,
)


def _mock_response(status: int, body: str, content_type: str = "application/json") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.json.return_value = _safe_json_loads(body, content_type)
    resp.raise_for_status = MagicMock()
    return resp


def _safe_json_loads(body: str, content_type: str) -> dict | list:
    import json
    if content_type == "application/json":
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}
    return {}


@pytest.mark.unit
def test_sc_search_queries_is_non_empty() -> None:
    assert len(SC_SEARCH_QUERIES) >= 15


@pytest.mark.unit
def test_is_likely_sc_paper_true_on_keywords() -> None:
    assert is_likely_sc_paper("YBCO thin film Tc measurement", "") is True
    assert is_likely_sc_paper("x", "The Meissner effect was observed in...") is True


@pytest.mark.unit
def test_is_likely_sc_paper_false_when_no_keywords() -> None:
    assert is_likely_sc_paper("Plant photosynthesis in high CO2", None) is False


@pytest.mark.unit
def test_deduplicate_by_doi_keeps_first() -> None:
    papers = [
        PaperMetadata(title="A", doi="10.1234/X", source="arxiv"),
        PaperMetadata(title="A duplicate", doi="10.1234/X", source="crossref"),
        PaperMetadata(title="B", doi="10.1234/Y", source="semantic_scholar"),
    ]
    out = _deduplicate(papers)
    assert len(out) == 2
    assert out[0].title == "A"
    assert {p.doi for p in out} == {"10.1234/X", "10.1234/Y"}


@pytest.mark.unit
def test_deduplicate_by_title_when_no_doi() -> None:
    papers = [
        PaperMetadata(title="MgB2 Superconductor Study", source="arxiv"),
        PaperMetadata(title="MgB2 Superconductor study.", source="crossref"),
        PaperMetadata(title="YBCO Tc=92K", source="semantic_scholar"),
    ]
    out = _deduplicate(papers)
    assert len(out) == 2


@pytest.mark.unit
async def test_search_all_sources_runs_in_parallel_and_dedupes() -> None:
    """All three sources return the same DOI -> result has one paper."""
    duplicate_paper = PaperMetadata(
        title="MgB2 study",
        doi="10.1234/ABC",
        source="arxiv",
    )
    crossref_paper = PaperMetadata(
        title="YBCO study",
        doi="10.5555/DEF",
        source="crossref",
    )

    async def fake_arxiv(*args, **kwargs):  # noqa: ANN001
        return [duplicate_paper]

    async def fake_crossref(*args, **kwargs):  # noqa: ANN001
        return [duplicate_paper, crossref_paper]

    async def fake_s2(*args, **kwargs):  # noqa: ANN001
        return [duplicate_paper]

    with (
        patch(
            "superconductor_db.ingest.paper_search.search_arxiv",
            new=AsyncMock(side_effect=fake_arxiv),
        ),
        patch(
            "superconductor_db.ingest.paper_search.search_crossref",
            new=AsyncMock(side_effect=fake_crossref),
        ),
        patch(
            "superconductor_db.ingest.paper_search.search_semantic_scholar",
            new=AsyncMock(side_effect=fake_s2),
        ),
    ):
        out = await search_all_sources("any", max_results=10)

    dois = {p.doi for p in out}
    assert dois == {"10.1234/ABC", "10.5555/DEF"}
    assert len(out) == 2


@pytest.mark.unit
async def test_search_all_sources_continues_when_one_source_fails() -> None:
    crossref_paper = PaperMetadata(
        title="YBCO study", doi="10.5555/DEF", source="crossref"
    )

    async def fake_arxiv(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("arxiv exploded")

    async def fake_crossref(*args, **kwargs):  # noqa: ANN001
        return [crossref_paper]

    async def fake_s2(*args, **kwargs):  # noqa: ANN001
        return []

    with (
        patch(
            "superconductor_db.ingest.paper_search.search_arxiv",
            new=AsyncMock(side_effect=fake_arxiv),
        ),
        patch(
            "superconductor_db.ingest.paper_search.search_crossref",
            new=AsyncMock(side_effect=fake_crossref),
        ),
        patch(
            "superconductor_db.ingest.paper_search.search_semantic_scholar",
            new=AsyncMock(side_effect=fake_s2),
        ),
    ):
        out = await search_all_sources("any")

    assert len(out) == 1
    assert out[0].doi == "10.5555/DEF"


@pytest.mark.unit
async def test_search_all_sources_respects_subscribed_list() -> None:
    """If caller asks only for arxiv, other sources are skipped."""
    arxiv_paper = PaperMetadata(title="MgB2", doi="10.1/X", source="arxiv")

    async def fake_arxiv(*args, **kwargs):  # noqa: ANN001
        return [arxiv_paper]

    crossref_called = False

    async def fake_crossref(*args, **kwargs):  # noqa: ANN001
        nonlocal crossref_called
        crossref_called = True
        return []

    with (
        patch(
            "superconductor_db.ingest.paper_search.search_arxiv",
            new=AsyncMock(side_effect=fake_arxiv),
        ),
        patch(
            "superconductor_db.ingest.paper_search.search_crossref",
            new=AsyncMock(side_effect=fake_crossref),
        ),
    ):
        out = await search_all_sources("any", sources=("arxiv",))
    assert len(out) == 1
    assert crossref_called is False