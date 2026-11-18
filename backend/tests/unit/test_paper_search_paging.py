"""The venue search must page, and a rate-limited venue search must park.

Both were found by watching one day of the download loop: the venue query
returned the same first slice on every round (OpenAlex rejects ``offset``,
so it needs an explicit ``page``), and it did not park on 429, so a spent
daily budget produced 7071 pointless rejections.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superconductor_db.ingest.paper_search_loop import (
    _OPENALEX_PARKED_UNTIL,
    _search_venue,
)


def _venue_response(status: int = 200, results: list[dict] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = {"results": results if results is not None else []}
    return resp


@pytest.mark.unit
async def test_venue_search_sends_no_page_for_the_first_round() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response())

    await _search_venue(client, "critical temperature", "S123", max_results=8)

    params = client.get.call_args.kwargs["params"]
    assert "page" not in params
    assert params["per-page"] == 8


@pytest.mark.unit
async def test_venue_search_advances_the_page() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response())

    await _search_venue(client, "critical temperature", "S123", max_results=8, page=7)

    params = client.get.call_args.kwargs["params"]
    assert params["page"] == 7
    # The page size is what the query asked for, and the dollar cost of a
    # request does not depend on it.
    assert params["per-page"] == 8


@pytest.mark.unit
async def test_venue_search_parks_on_rate_limit() -> None:
    before = time.monotonic()
    _OPENALEX_PARKED_UNTIL[0] = 0.0
    resp = _venue_response(status=429)
    resp.headers = {"Retry-After": "1800"}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    result = await _search_venue(client, "critical temperature", "S123")

    assert result == []
    parked_for = _OPENALEX_PARKED_UNTIL[0] - before
    assert parked_for > 1000, "a 429 must park the queries, not just skip this one"
    _OPENALEX_PARKED_UNTIL[0] = 0.0


@pytest.mark.unit
async def test_venue_search_parks_on_the_daily_budget_reset() -> None:
    before = time.monotonic()
    _OPENALEX_PARKED_UNTIL[0] = 0.0
    resp = _venue_response(status=429)
    # A spent daily budget answers with the seconds until midnight.
    resp.headers = {"Retry-After": "7200"}
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    await _search_venue(client, "critical temperature", "S123")

    parked_for = _OPENALEX_PARKED_UNTIL[0] - before
    assert 7000 < parked_for < 7400
    _OPENALEX_PARKED_UNTIL[0] = 0.0


@pytest.mark.unit
async def test_venue_search_parks_on_403_as_well() -> None:
    before = time.monotonic()
    _OPENALEX_PARKED_UNTIL[0] = 0.0
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response(status=403))

    await _search_venue(client, "critical temperature", "S123")

    assert _OPENALEX_PARKED_UNTIL[0] > before
    _OPENALEX_PARKED_UNTIL[0] = 0.0


@pytest.mark.unit
async def test_venue_search_returns_sc_titles() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response(results=[
        {"title": "Superconducting transition temperature of MgB2", "doi": "https://doi.org/10.1103/PhysRevLett.86.5373"},
        {"title": "Quantum chromodynamics on the lattice", "doi": "https://doi.org/10.1103/PhysRevD.99.034001"},
    ]))

    dois = await _search_venue(client, "critical temperature", "S123")

    assert dois == ["10.1103/PhysRevLett.86.5373"]


@pytest.mark.unit
async def test_venue_search_can_ask_for_open_access_only() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response())

    await _search_venue(
        client, "critical temperature", "S4210174675", oa_only=True
    )

    params = client.get.call_args.kwargs["params"]
    assert "is_oa:true" in params["filter"]
    assert "primary_location.source.id:S4210174675" in params["filter"]


@pytest.mark.unit
async def test_venue_search_without_the_filter_asks_for_everything() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response())

    await _search_venue(client, "critical temperature", "S64187185")

    params = client.get.call_args.kwargs["params"]
    assert "is_oa:true" not in params["filter"]


@pytest.mark.unit
async def test_venue_search_sorts_by_date_not_relevance() -> None:
    """Paging a relevance-sorted result walks into the past, where the
    papers sit behind a login wall and cannot be downloaded at all."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_venue_response())

    await _search_venue(client, "critical temperature", "S4210174675")

    params = client.get.call_args.kwargs["params"]
    assert params["sort"] == "publication_date:desc"
    assert "from_publication_date:2015-01-01" in params["filter"]


@pytest.mark.unit
def test_the_venue_slice_rotates_and_the_page_advances() -> None:
    """The daily allowance fixes the queries a round may spend, so the
    venues share it: the round asks a rotating slice of the venue-and-term
    matrix and every pair walks down its own journal."""
    from superconductor_db.ingest.paper_search_loop import (
        VENUE_QUERIES,
        _TARGET_SOURCES,
        _VENUE_QUERIES_PER_ROUND,
        _venue_pairs,
        _venue_slice,
    )

    pairs = _venue_pairs()
    assert len(pairs) == len(_TARGET_SOURCES) * len(VENUE_QUERIES)

    first = _venue_slice(1)
    second = _venue_slice(2)
    assert len(first) == _VENUE_QUERIES_PER_ROUND
    # A different slice of the matrix, not the same one again.
    assert {p[:3] for p in first} != {p[:3] for p in second}

    # Every pair is asked for once per full cycle of the matrix.
    asked = []
    for round_index in range(1, len(pairs) // _VENUE_QUERIES_PER_ROUND + 2):
        asked.extend(_venue_slice(round_index))
    counts: dict[tuple[str, str, str], int] = {}
    for venue, source_id, term, _page in asked:
        counts[(venue, source_id, term)] = counts.get((venue, source_id, term), 0) + 1
    assert set(counts) == set(pairs)
    assert min(counts.values()) >= 1

    # A pair's page is how many times it has been asked: it never repeats
    # a page and never skips one.
    pages = {}
    for round_index in range(1, 40):
        for venue, source_id, term, page in _venue_slice(round_index):
            key = (venue, source_id, term)
            seen = pages.setdefault(key, [])
            assert page == len(seen) + 1
            seen.append(page)


@pytest.mark.unit
def test_the_issn_route_reads_the_journal_and_not_a_phrase() -> None:
    """The phrase the route used to ask for matches nothing in several
    journals -- their titles do not carry it -- so those journals came
    back empty for ever."""
    from superconductor_db.ingest.paper_search_loop import _TARGET_ISSN

    # The ISSNs have to be the ones CrossRef registers the journal under.
    assert _TARGET_ISSN["Physics Letters A"] == "0375-9601"
    assert _TARGET_ISSN["Journal of the Physical Society of Japan"] == "0031-9015"
    assert _TARGET_ISSN["Nature Physics"] == "1745-2473"
    assert _TARGET_ISSN["Physica C: Superconductivity"] == "0921-4534"
    # 1367-2630 is New Journal of Physics, not Superconducting Science
    # and Technology, which is what the entry used to say.
    assert "1367-2630" not in _TARGET_ISSN.values()


@pytest.mark.unit
def test_the_venue_sources_are_the_journals_and_not_their_sections() -> None:
    """OpenAlex has a source for Physical Review B's "Condensed matter"
    section, and every query against it came back empty -- a work's
    primary source is the journal, not the section."""
    from superconductor_db.ingest.paper_search_loop import _TARGET_SOURCES

    assert _TARGET_SOURCES["Physical Review B"] == "S6791298"
    # The section source is not the journal.
    assert "S4210174675" not in _TARGET_SOURCES.values()
