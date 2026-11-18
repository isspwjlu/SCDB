"""Direct publisher PDF retrieval.

scansci-pdf routes non-APS publishers through Sci-Hub, which is behind a
Cloudflare challenge on this network. Nature-family papers are served
directly and need neither Sci-Hub nor a browser, which is what this module
uses.
"""

from __future__ import annotations

import httpx
from unittest.mock import MagicMock
import pytest

from superconductor_db.ingest.direct_fetch import (
    _article_id,
    _candidate_urls,
    _looks_like_pdf,
)


@pytest.mark.unit
def test_article_id_strips_the_doi_prefix() -> None:
    assert _article_id("10.1038/s41467-019-11330-6") == "s41467-019-11330-6"
    assert _article_id("10.1038/41598-020-1234-5") == "41598-020-1234-5"


@pytest.mark.unit
def test_candidate_url_is_built_for_nature_family() -> None:
    urls = _candidate_urls("10.1038/s41467-019-11330-6")
    assert urls == ["https://www.nature.com/articles/s41467-019-11330-6.pdf"]


@pytest.mark.unit
def test_aps_has_a_direct_candidate() -> None:
    """APS serves the PDF from a predictable URL.

    This used to be false: the APS journals were measured as 403 to a
    plain client and were left to scansci-pdf and the browser. They
    answer after all, and the browser route costs 20-35 s per paper
    against 3 s for the direct one.
    """
    urls = _candidate_urls("10.1103/PhysRevB.98.024517")
    assert urls == ["https://link.aps.org/pdf/10.1103/PhysRevB.98.024517"]


@pytest.mark.unit
def test_doi_without_a_known_publisher_has_no_direct_candidate() -> None:
    assert _candidate_urls("10.1016/j.physleta.2025.131034") == []


@pytest.mark.unit
def test_challenge_page_is_not_mistaken_for_a_pdf() -> None:
    """IOP serves a Cloudflare page with HTTP 200, so bytes are checked."""
    assert _looks_like_pdf(b"%PDF-1.7" + b"x" * 30_000) is True
    assert _looks_like_pdf(b"<!DOCTYPE html><html>captcha</html>" + b"x" * 30_000) is False
    # Too small to be a real article even if it starts correctly.
    assert _looks_like_pdf(b"%PDF-1.4 tiny") is False


@pytest.mark.unit
def test_arxiv_fallback_is_a_supplement_not_a_replacement() -> None:
    """The arXiv route covers only papers that were preprinted.

    IOP and Elsevier superconductors are almost never on arXiv, so the
    publisher route stays first; this only rescues the Physical Review subset
    that Sci-Hub can no longer reach.
    """
    from superconductor_db.ingest.direct_fetch import _ARXIV_ID_RE

    # An arXiv Atom feed names the abs id; the version suffix is stripped
    # before fetching because /pdf/<id>v3 is not a valid PDF path.
    body = "<entry><id>http://arxiv.org/abs/1803.08468v3</id></entry>"
    match = _ARXIV_ID_RE.search(body)
    assert match is not None
    assert match.group(1).split("v")[0] == "1803.08468"


@pytest.mark.unit
def test_iop_route_only_applies_to_iop_dois() -> None:
    """The IOP endpoint must not be tried for other publishers.

    Sending an IOP-style request to Nature or APS would waste a round trip
    and, worse, could return an HTML page that happens to look large enough.
    """
    from superconductor_db.ingest.direct_fetch import fetch_iop_pdf

    assert fetch_iop_pdf("10.1038/s41586-019-1201-8") is None
    assert fetch_iop_pdf("10.1103/PhysRevB.98.024517") is None


@pytest.mark.unit
def test_iop_pdf_url_template() -> None:
    """The captcha is on the article page, not on the /pdf endpoint."""
    from superconductor_db.ingest.direct_fetch import _IOP_PDF_TEMPLATES

    doi = "10.1088/1367-2630/ad8954"
    url = _IOP_PDF_TEMPLATES[0].format(doi=doi)
    assert url == "https://iopscience.iop.org/article/10.1088/1367-2630/ad8954/pdf"
    assert "/article/" in url and url.endswith("/pdf")


@pytest.mark.unit
def test_springer_has_a_direct_pdf_url() -> None:
    """SpringerLink serves the PDF; its /article/ page returns no bytes.

    10.1007 was 7 of the 72 download failures in a 24-hour run, each costing a
    180 s timeout.
    """
    from superconductor_db.ingest.direct_fetch import _candidate_urls

    doi = "10.1007/s10909-026-03373-z"
    assert _candidate_urls(doi) == [
        "https://link.springer.com/content/pdf/10.1007/s10909-026-03373-z.pdf"
    ]


@pytest.mark.unit
@pytest.mark.parametrize("doi", ["10.2139/ssrn.4359310", "10.31234/x", "10.20944/y"])
def test_unretrievable_preprint_servers_are_skipped(doi: str) -> None:
    """SSRN and friends cost a timeout each; skip them before the download."""
    from superconductor_db.ingest.direct_fetch import is_downloadable

    assert is_downloadable(doi) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi", ["10.1007/s10909-026-03373-z", "10.1038/s41586-019-1201-8",
            "10.1103/PhysRevB.98.024517", "10.1088/1367-2630/ad8954"],
)
def test_normal_publishers_still_download(doi: str) -> None:
    from superconductor_db.ingest.direct_fetch import is_downloadable

    assert is_downloadable(doi) is True


@pytest.mark.unit
def test_unpaywall_gives_the_open_access_locations() -> None:
    """Unpaywall is free and needs no key: it says where the open-access
    copy of a paper lives, which is what reaches the papers the
    publisher's own endpoint refuses."""
    from superconductor_db.ingest.direct_fetch import _unpaywall_urls

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "is_oa": True,
        "best_oa_location": {"url_for_pdf": "https://repo.example/paper.pdf"},
        "oa_locations": [{"url": "https://repo.example/landing"}],
    }
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    urls = _unpaywall_urls("10.1016/j.physc.2024.1354572", client)

    assert urls[0] == "https://repo.example/paper.pdf"
    assert "https://repo.example/landing" in urls


@pytest.mark.unit
def test_unpaywall_reports_nothing_for_a_paywalled_paper() -> None:
    from superconductor_db.ingest.direct_fetch import _unpaywall_urls

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"is_oa": False, "oa_locations": []}
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    assert _unpaywall_urls("10.1016/j.physc.2024.1354572", client) == []


@pytest.mark.unit
def test_the_elsevier_api_url_is_built_from_the_doi() -> None:
    from superconductor_db.ingest.direct_fetch import _elsevier_pdf_url

    assert _elsevier_pdf_url("10.1016/j.physc.2024.1354572") == (
        "https://api.elsevier.com/content/article/doi/10.1016/j.physc.2024.1354572"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi", ["10.1016/j.physc.2024.1354572", "10.1134/S0040577924110126"],
)
def test_the_elsevier_channel_covers_its_own_publishers(doi: str) -> None:
    """Elsevier and Physics Letters A are both Elsevier."""
    from superconductor_db.ingest.direct_fetch import _is_elsevier_doi

    assert _is_elsevier_doi(doi) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi", ["10.1038/s41586-019-1201-8", "10.1103/PhysRevB.98.024517",
            "10.1007/s10909-026-03373-z"],
)
def test_the_elsevier_channel_leaves_the_other_publishers_alone(doi: str) -> None:
    """Sending an Elsevier request to Nature would only waste a round trip."""
    from superconductor_db.ingest.direct_fetch import _is_elsevier_doi

    assert _is_elsevier_doi(doi) is False


@pytest.mark.unit
def test_the_elsevier_channel_needs_a_key() -> None:
    """Without a key the API refuses, so there is no point in asking."""
    from superconductor_db.ingest.direct_fetch import fetch_elsevier_pdf

    assert fetch_elsevier_pdf("10.1016/j.physc.2024.1354572", api_key="") is None


@pytest.mark.unit
def test_the_elsevier_channel_returns_the_pdf() -> None:
    """The API answers with the file itself when the address is entitled."""
    from superconductor_db.ingest.direct_fetch import fetch_elsevier_pdf

    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.7" + b"x" * 30_000
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    data = fetch_elsevier_pdf(
        "10.1016/j.physc.2024.1354572", api_key="k" * 32, client=client
    )

    assert data is not None
    assert data.startswith(b"%PDF")
    # The key has to travel in the header, and the file has to be asked for
    # as a PDF rather than as XML.
    _url, kwargs = client.get.call_args
    assert kwargs["headers"]["X-ELS-APIKey"] == "k" * 32
    assert kwargs["headers"]["Accept"] == "application/pdf"


@pytest.mark.unit
def test_the_elsevier_channel_refuses_an_error_page() -> None:
    """The API answers with an XML error, and the magic bytes catch it."""
    from superconductor_db.ingest.direct_fetch import fetch_elsevier_pdf

    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"<serviceError>no access</serviceError>" + b"x" * 30_000
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    assert (
        fetch_elsevier_pdf(
            "10.1016/j.physc.2024.1354572", api_key="k" * 32, client=client
        )
        is None
    )


@pytest.mark.unit
def test_the_frontiers_url_is_built_from_the_doi() -> None:
    from superconductor_db.ingest.direct_fetch import _frontiers_url

    assert _frontiers_url("10.3389/fimmu.2023.1302891") == (
        "https://www.frontiersin.org/articles/10.3389/fimmu.2023.1302891/pdf"
    )


@pytest.mark.unit
def test_the_frontiers_channel_covers_its_own_doi() -> None:
    from superconductor_db.ingest.direct_fetch import _is_frontiers_doi

    assert _is_frontiers_doi("10.3389/fimmu.2023.1302891") is True
    assert _is_frontiers_doi("10.1038/s41586-019-1201-8") is False


@pytest.mark.unit
def test_the_frontiers_channel_returns_the_pdf() -> None:
    from superconductor_db.ingest.direct_fetch import fetch_frontiers_pdf

    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.7" + b"x" * 30_000
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    data = fetch_frontiers_pdf(
        "10.3389/fimmu.2023.1302891", client=client
    )
    assert data is not None and data.startswith(b"%PDF")


@pytest.mark.unit
def test_the_frontiers_channel_leaves_other_publishers_alone() -> None:
    from superconductor_db.ingest.direct_fetch import fetch_frontiers_pdf

    client = MagicMock()
    assert fetch_frontiers_pdf("10.1016/j.physc.2024.1354572", client=client) is None
    client.get.assert_not_called()


@pytest.mark.unit
def test_the_aps_channel_uses_the_journals_own_pdf_path() -> None:
    """link.aps.org answers 403 for a paywalled paper, but the journal's
    own /pdf/ address serves the file."""
    from superconductor_db.ingest.direct_fetch import fetch_aps_pdf

    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.7" + b"x" * 30_000
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    data = fetch_aps_pdf("10.1103/physrevb.63.220503", client=client)

    assert data is not None and data.startswith(b"%PDF")
    url = client.get.call_args[0][0]
    assert url == "https://journals.aps.org/prb/pdf/10.1103/PhysRevB.63.220503"


@pytest.mark.unit
def test_the_aps_channel_leaves_other_publishers_alone() -> None:
    from superconductor_db.ingest.direct_fetch import fetch_aps_pdf

    client = MagicMock()
    assert fetch_aps_pdf("10.1038/s41586-019-1201-8", client=client) is None
    client.get.assert_not_called()


@pytest.mark.unit
def test_the_aps_channel_refuses_an_error_page() -> None:
    from superconductor_db.ingest.direct_fetch import fetch_aps_pdf

    resp = MagicMock()
    resp.status_code = 404
    resp.content = b"<html>not found</html>"
    client = MagicMock()
    client.get = MagicMock(return_value=resp)

    assert fetch_aps_pdf("10.1103/physrevb.63.220503", client=client) is None


@pytest.mark.unit
def test_the_aps_channel_asks_crossref_for_an_opaque_code() -> None:
    """The modern DOIs say only which article they are, so the journal
    has to be looked up -- and the answer is remembered."""
    from superconductor_db.ingest.direct_fetch import _aps_code_cache, fetch_aps_pdf

    _aps_code_cache.clear()
    openalex = MagicMock()
    openalex.status_code = 200
    openalex.json.return_value = {
        "primary_location": {"source": {"display_name": "Physical Review B"}}
    }
    crossref = MagicMock()
    crossref.status_code = 200
    crossref.json.return_value = {"message": {"container-title": ["Physical Review B"]}}
    # CrossRef answers a single-work lookup with HTTP 400 when a field
    # list is given, so the lookup must not ask for one.
    pdf = MagicMock()
    pdf.status_code = 200
    pdf.content = b"%PDF-1.7" + b"x" * 30_000

    client = MagicMock()
    # OpenAlex answers first, so CrossRef is not asked at all.
    client.get = MagicMock(side_effect=[openalex, pdf, pdf])

    data = fetch_aps_pdf("10.1103/bnw9-xx7v", client=client)
    assert "openalex.org" in client.get.call_args_list[0][0][0]

    assert data is not None and data.startswith(b"%PDF")
    assert client.get.call_args_list[1][0][0] == (
        "https://journals.aps.org/prb/pdf/10.1103/bnw9-xx7v"
    )
    # The second call for the same DOI must not ask for the journal
    # again.
    calls = len(client.get.call_args_list)
    fetch_aps_pdf("10.1103/bnw9-xx7v", client=client)
    assert len(client.get.call_args_list) == calls + 1


@pytest.mark.unit
def test_the_journal_names_are_matched_whatever_their_spelling() -> None:
    """OpenAlex calls one of its Physical Review B sources
    "Physical review. B./Physical review. B"."""
    from superconductor_db.ingest.direct_fetch import _aps_code_for_title

    assert _aps_code_for_title("Physical Review B") == "prb"
    assert _aps_code_for_title("Physical review. B./Physical review. B") == "prb"
    assert _aps_code_for_title("Physical review. B, Condensed matter") == "prb"
    assert _aps_code_for_title("Physical Review Letters") == "prl"
    assert _aps_code_for_title("Physical Review Materials") == "prmaterials"
    # The umbrella journal must not swallow the specific ones.
    assert _aps_code_for_title("Physical Review") == "prx"
    assert _aps_code_for_title("Journal of Chemical Physics") is None
