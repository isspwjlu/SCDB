"""The download loop's blocklist and its publisher allowlist.

Twenty-seven papers are refused by the pipeline and must not come back.
Eight are rejected by MiniMax's input content filter (HTTP 422), so the
extraction retries them for ever and never stores a row, and the ninth is
a PDF docling cannot parse at all. The other seventeen are records no
channel can serve: the publisher's PDF endpoint refuses, there is no
preprint, and the two preprint servers sit behind an upload wall.
Dropping them from the queue is not enough -- the search finds them
again and puts them back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from download_papers import (  # noqa: E402
    _BLOCKED_DOIS,
    _channel_name,
    _is_fetchable_doi,
)

# The nine papers purged on 2026-11-05: eight refused by MiniMax's input
# filter, one PDF docling cannot parse.
PURGED = (
    "10.21926/jept.2504014",
    "10.48550/arXiv.1101.3704v1",
    "10.48550/arXiv.1303.3528v1",
    "10.48550/arXiv.1607.00441v1",
    "10.48550/arXiv.2101.10538v1",
    "10.48550/arXiv.2506.18117v1",
    "10.48550/arXiv.cond-mat/0602134v2",
    "10.48550/arXiv.physics/0605009v1",
    "10.48550/arXiv.1810.08981v2",
)

# The eighteen orphans purged on 2026-11-09: four placeholders the old
# corpus carried for papers it never had, and fourteen records no channel
# can serve.
UNREACHABLE = (
    "NIMS-SuperCon-006",
    "NIMS-SuperCon-007",
    "NIMS-SuperCon-008",
    "NIMS-SuperCon-010",
    "10.1103/physrevb.109.064414",
    "10.21203/rs.3.rs-2569477/v1",
    "10.1038/s43246-026-01312-9",
    "10.7185/gold2023.16723",
    "10.32908/hthp.v54.1923",
    "10.1063/5.0225871",
    "10.1088/1361-6668/acbb35",
    "10.1016/j.physc.2024.1354465",
    "10.1134/s0040577924110126",
    "10.5194/egusphere-egu26-3766",
    "10.21203/rs.3.rs-10120640/v1",
    "10.21009/spektra.091.02",
    "10.3103/s1068371225700324",
    "10.3389/fimmu.2023.1302891",
)

# Nine of the twenty-seven turned out to be fetchable once the publisher
# routes were fixed; they were downloaded rather than purged.
RECOVERED = (
    "10.1088/1361-648x/add6fc",
    "10.1103/physrevlett.80.3364",
    "10.1134/s0021364024602409",
    "10.1134/s0021364024600046",
    "10.48550/arXiv.2511.01148v1",
    "10.1088/1361-648x/ace410",
    "10.1088/1361-6668/adb5cc",
    "10.7566/jpsj.93.104705",
    "10.1103/physrevb.108.184509",
)


@pytest.mark.unit
@pytest.mark.parametrize("doi", PURGED + UNREACHABLE)
def test_blocked_doi_is_not_fetchable(doi: str) -> None:
    assert _is_fetchable_doi(doi) is False


@pytest.mark.unit
def test_blocklist_is_case_insensitive() -> None:
    assert _is_fetchable_doi("10.48550/ARXIV.1101.3704V1") is False
    assert "10.48550/arxiv.1101.3704v1" in _BLOCKED_DOIS


@pytest.mark.unit
def test_ordinary_papers_are_still_fetchable() -> None:
    assert _is_fetchable_doi("10.48550/arXiv.2611.02469v1") is True
    assert _is_fetchable_doi("10.1038/s41467-019-11330-6") is True


@pytest.mark.unit
def test_blocklist_holds_exactly_the_purged_papers() -> None:
    assert _BLOCKED_DOIS == frozenset(
        doi.casefold() for doi in PURGED + UNREACHABLE
    )


@pytest.mark.unit
@pytest.mark.parametrize("doi", RECOVERED)
def test_recovered_papers_are_not_blocked(doi: str) -> None:
    """They were fetched after all -- the blocklist must not hold them."""
    assert _is_fetchable_doi(doi) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi",
    [
        "10.1063/1.1413729",       # Applied Physics Letters
        "10.1126/science.abq1234",  # Science
        "10.1109/physrevb.108.184509",  # Physical Review, the new prefix
        "10.1134/s0021364024602409",  # Physics Letters A
        "10.7566/jpsj.93.104705",  # J. Phys. Soc. Jpn.
    ],
)
def test_mainstream_journals_are_reachable(doi: str) -> None:
    """The preprint route makes these fetchable even when the publisher's
    own endpoint refuses, so the allowlist has to cover them."""
    assert _is_fetchable_doi(doi) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi",
    [
        "10.21203/rs.3.rs-9999999/v1",
        "10.2139/2001.99999",
        "10.31234/10.1103/xyz",
        "10.5194/egusphere-egu26-9999",
    ],
)
def test_upload_wall_preprint_servers_are_skipped(doi: str) -> None:
    assert _is_fetchable_doi(doi) is False


@pytest.mark.unit
def test_the_channel_is_named_after_the_fetcher_that_delivered_the_paper() -> None:
    """The log line says which route earned the paper, and the monitor
    counts them -- without that there is no way to tell which channel is
    carrying the corpus."""
    from superconductor_db.ingest.direct_fetch import (
        fetch_arxiv_pdf,
        fetch_direct_pdf,
        fetch_elsevier_pdf,
        fetch_iop_pdf,
        fetch_oa_pdf,
    )

    assert _channel_name(fetch_elsevier_pdf) == "elsevier"
    assert _channel_name(fetch_arxiv_pdf) == "arxiv"
    assert _channel_name(fetch_direct_pdf) == "direct"
    assert _channel_name(fetch_iop_pdf) == "iop"
    assert _channel_name(fetch_oa_pdf) == "oa"


@pytest.mark.unit
def test_the_browser_is_only_asked_where_it_works() -> None:
    """The browser costs a minute a paper, and it clears the challenge
    only at some publishers: Wiley, IEEE, Taylor & Francis and the
    Journal of the Physical Society of Japan are left out."""
    from download_papers import _BROWSER_PREFIXES

    for reachable in ("10.3390", "10.1088", "10.1063", "10.1039"):
        assert any(p.startswith(reachable) for p in _BROWSER_PREFIXES)
    for blocked in ("10.1002", "10.1109", "10.1080", "10.7566"):
        assert not any(p.startswith(blocked) for p in _BROWSER_PREFIXES)
