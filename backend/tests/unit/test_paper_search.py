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

@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Zero-Resistance State for a Potential High-Temperature Superconducting Nickelate", True),
        ("Is La3Ni2O6.5 a Bulk Superconducting Nickelate?", True),
        ("Superconducting gap symmetry of Sr2RuO4", True),
        ("Effect of pressure on Tc in iron pnictides", True),
        # CrossRef is a general index: these must be filtered out before we
        # spend an LLM call on them.
        ("Machine learning for superconductor discovery", False),
        ("A review of cuprate superconductors", False),
        ("Chapter 4 Ginzburg-Landau Equations in Superconductivity", False),
        ("Piezoresistance of semiconductors", False),
    ],
)
def test_crossref_title_filter(title: str, expected: bool) -> None:
    """CrossRef results are title-filtered before ingest.

    CrossRef indexes all of scholarship, so a keyword query returns plenty of
    material-science noise. Filtering on title keeps the ingest loop from
    paying LLM cost for non-superconductor papers.
    """
    from superconductor_db.ingest.paper_search_loop import (
        _looks_like_sc_measurement_title as f,
    )

    assert f(title) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Papers that report a Tc value, however plainly the title states it.
        ("Pressure-induced superconductivity in La3Ni2O7 thin films", True),
        ("Superconductivity in BaFe2As2 thin films", True),
        ("Superconducting properties of CaH6", True),
        ("Magnetization and specific heat of CeCoIn5", True),
        ("Structural and superconducting properties of MgB2", True),
        # Theory and dynamics papers: on-topic, but no new Tc to verify.
        # These consumed 8 downloads for 2 already-known records.
        ("Nearly Isotropic Vortex Solid in (La,Pr)3Ni2O7 Thin Films", False),
        ("Vortex glass transition in a disordered superconductor", False),
        ("Vortex pinning and creep in type-II superconductors", False),
        ("Flux creep in cuprate superconductors", False),
        ("Magnetic skyrmions in a chiral magnet", False),
        ("Altermagnetism in twisted bilayer graphene", False),
        ("Topological order in superconductors", False),
        ("First-principles study of the pairing mechanism", False),
        ("Machine learning prediction of Tc", False),
        ("Perspective on unconventional superconductivity", False),
    ],
)
def test_measurement_title_filter(title: str, expected: bool) -> None:
    """Only measurement papers are worth an LLM call.

    A batch of eight vortex-dynamics/theory papers produced two extractable
    records, both already in the database. The filter requires measurement
    vocabulary and rejects vortex/topology/ML titles outright.
    """
    from superconductor_db.ingest.paper_search_loop import (
        _looks_like_sc_measurement_title,
    )

    assert _looks_like_sc_measurement_title(title) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.1103/PhysRevB.98.024517", True),   # APS
        ("10.1103/physrevlett.121.037001", True),  # APS
        ("10.1038/s41586-019-1201-8", True),      # Springer Nature
        ("10.1088/1361-300X/ab1234", True),        # IOP
        ("10.48550/arXiv.2107.12345", False),      # preprint
        ("10.15406/jaccoa.2016.04.00144", False),  # unrelated registry
    ],
)
def test_target_doi_prefixes(doi: str, expected: bool) -> None:
    """Venue-scoped discovery is selected by publisher DOI prefix.

    The host network reaches APS and Springer Nature directly, so their
    paywalled papers are retrievable; restricting discovery to those prefixes
    is what replaces the broad index that returned mostly arXiv preprints.
    """
    from superconductor_db.ingest.paper_search_loop import _is_target_doi

    assert _is_target_doi(doi) is expected
