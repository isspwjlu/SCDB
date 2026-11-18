"""Direct publisher PDF retrieval for hosts that answer a plain HTTP request.

``scansci-pdf`` covers APS and Nature Communications, but it routes the
remaining publishers through Sci-Hub, which now sits behind a Cloudflare
challenge it cannot clear without CloakBrowser. Measured on this network:

    APS (link.aps.org)          403 to a script, fine through scansci-pdf
    Nature / Nature Commun.     200, PDF links present in the page
    IOP                         redirected to validate.perfdrive.com (challenge)
    ScienceDirect (Elsevier)    403
    Wiley                       403

So one class of publisher is reachable without any of that: fetch the article
page, read the PDF link out of it, and download it. That covers Nature
Communications, Nature, Scientific Reports and the npj family — a large slice
of the open-access superconductivity literature — without Sci-Hub, Tor or a
browser.

A request is only accepted when the response starts with the ``%PDF`` magic
bytes and clears a minimum size; a challenge page served with HTTP 200 is
rejected, which is what happened to IOP.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote
from typing import Any, Iterable

import httpx

from superconductor_db.core.config import pdf_fetch_proxies

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# A challenge page can be served with HTTP 200, so the bytes must be checked,
# not just the status.
_PDF_MAGIC = b"%PDF"
_MIN_PDF_BYTES = 20_000

# Publisher landing pages, keyed by the prefix in the DOI.
_LANDING: tuple[tuple[str, str], ...] = (
    ("10.1038/", "https://www.nature.com/articles/{article}"),
    ("10.1038/", "https://www.nature.com/articles/{doi}"),
)

# Publishers known to answer a direct request with the PDF itself.
_DIRECT_PDF: tuple[tuple[str, str], ...] = (
    # Nature family: /articles/<id>.pdf works for Nature and Nat Commun.
    ("10.1038/s4", "https://www.nature.com/articles/{tail}.pdf"),
    # SpringerLink serves the PDF directly; its /article/ page is a redirect
    # that does not return bytes. 7 of 72 download failures were 10.1007.
    ("10.1007/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    # APS serves the PDF from a predictable URL. The browser reaches it
    # too, but it costs 20-35 s per paper against 3 s here, and the
    # browser is the last resort rather than the first.
    ("10.1103/", "https://link.aps.org/pdf/{aps}"),
)

# APS spells its journals in the modern article code (``PhysRevB.106.184516``)
# and answers a request for the old lowercase form with a 404 -- the queue
# carries every APS DOI in the lowercase form, because OpenAlex returns them
# that way. 383 of the 383 APS papers in the queue are lowercase, and none
# of them could be fetched until the case was put back.
_APS_JOURNAL_CASE: dict[str, str] = {
    "physrevb": "PhysRevB", "prb": "PhysRevB",
    "physrevlett": "PhysRevLett", "prl": "PhysRevLett",
    "physrevx": "PhysRevX", "prx": "PhysRevX",
    "physrev": "PhysRev", "pre": "PhysRev",
    "physrevresearch": "PhysRevResearch", "prresearch": "PhysRevResearch",
    "physrevapplied": "PhysRevApplied", "prapplied": "PhysRevApplied",
    "physrevaccelbeams": "PhysRevAccelBeams", "prab": "PhysRevAccelBeams",
    "physrevfluids": "PhysRevFluids", "prf": "PhysRevFluids",
    "physrevsolidum": "PhysRevSolidum", "prsu": "PhysRevSolidum",
    "physrevd": "PhysRevD", "prd": "PhysRevD",
    "physrevc": "PhysRevC", "prc": "PhysRevC",
    "physrevmaterials": "PhysRevMaterials", "prmaterials": "PhysRevMaterials",
    "physreve": "PhysRevE", "pre": "PhysRevE",
    "physics": "Physics",
    "revmodphys": "RevModPhys", "rmp": "RevModPhys",
}


def aps_modern_doi(doi: str) -> str:
    """An APS DOI in the capitalisation APS's URLs use.

    The modern opaque article codes (``10.1103/k5r2-q11w``) are already
    in the right form and are returned unchanged; the older ones need the
    journal abbreviation capitalised. A DOI whose journal is not in the
    table is returned unchanged, which costs nothing but a 404.
    """
    if not doi.casefold().startswith("10.1103/"):
        return doi
    tail = doi.split("/", 1)[1]
    if "." not in tail:
        return doi
    parts = tail.split(".")
    journal = _APS_JOURNAL_CASE.get(parts[0].lower())
    if not journal:
        return doi
    return "10.1103/" + ".".join([journal] + parts[1:])


# The journal code APS uses in its own URLs.
_APS_URL_CODES: dict[str, str] = {
    "physrevb": "prb",
    "physrevlett": "prl",
    "physrevx": "prx",
    "physrevc": "prc",
    "physrevapplied": "prapplied",
    "physrevmaterials": "prmaterials",
}

# And the same, by the journal's name, for the DOIs that carry no
# abbreviation: the modern opaque codes say only which article they
# are, so the journal has to be looked up.
_APS_CODES_BY_TITLE: dict[str, str] = {
    "physical review b": "prb",
    "physical review letters": "prl",
    "physical review x": "prx",
    "physical review c": "prc",
    "physical review applied": "prapplied",
    "physical review materials": "prmaterials",
    "physical review": "prx",
}

# What a DOI resolves to, once looked up: the opaque codes are looked up
# more than once, and the answer does not change.
_APS_CODE_CACHE: dict[str, str] = {}

# The same cache under the name the tests use.
_aps_code_cache = _APS_CODE_CACHE


def _aps_journal_code(doi: str, client: httpx.Client) -> str | None:
    """The code APS uses for this DOI's journal.

    The older DOIs name their journal (``PhysRevB.63.220503``), so the
    code is in the DOI. The modern ones do not (``10.1103/bnw9-xx7v``),
    and their journal has to be asked for -- CrossRef answers with the
    journal's name, and the answer is remembered.
    """
    cached = _APS_CODE_CACHE.get(doi)
    if cached:
        return cached
    modern = aps_modern_doi(doi)
    tail = modern.split("/", 1)[1] if "/" in modern else modern
    if "." in tail:
        code = _APS_URL_CODES.get(tail.split(".", 1)[0].casefold())
        if code:
            _APS_CODE_CACHE[doi] = code
            return code
    # The DOI does not say: ask which journal it belongs to.
    #
    # OpenAlex first: a single-work lookup there is free and its limit
    # is far above what the loop asks of it. CrossRef answers the same
    # question, but it is shared with the rest of the loop and an
    # unpaced lookup from eight concurrent fetches gets refused.
    #
    # No ``select`` on the CrossRef request: it answers a single-work
    # lookup with HTTP 400 when a field list is given.
    title = _aps_journal_title_openalex(doi, client)
    if title is None:
        title = _aps_journal_title_crossref(doi, client)
    if not title:
        return None
    code = _aps_code_for_title(title)
    if code:
        _APS_CODE_CACHE[doi] = code
    return code


def _aps_code_for_title(title: str) -> str | None:
    """The journal code for a journal's name.

    The name is not always the plain one: OpenAlex calls Physical
    Review B "Physical review. B./Physical review. B" for one of its
    sources. So the names are compared with the punctuation taken out
    and the longest match wins, which keeps "physical review b" from
    being swallowed by "physical review".
    """
    key = re.sub(r"[^a-z0-9]+", "", title.casefold())
    best: tuple[int, str] | None = None
    for name, code in _APS_CODES_BY_TITLE.items():
        plain = re.sub(r"[^a-z0-9]+", "", name)
        if plain and plain in key and (best is None or len(plain) > best[0]):
            best = (len(plain), code)
    return best[1] if best else None


def _aps_journal_title_openalex(doi: str, client: httpx.Client) -> str | None:
    """The journal's name, from OpenAlex."""
    try:
        resp = client.get(
            f"https://api.openalex.org/works/doi:{doi}",
            timeout=30.0,
        )
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    source = (payload.get("primary_location") or {}).get("source") or {}
    name = source.get("display_name")
    return str(name) if name else None


def _aps_journal_title_crossref(doi: str, client: httpx.Client) -> str | None:
    """The journal's name, from CrossRef."""
    try:
        resp = client.get(
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            timeout=30.0,
        )
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if resp.status_code != 200:
        return None
    try:
        titles = (resp.json().get("message") or {}).get("container-title") or []
    except ValueError:
        return None
    return str(titles[0]) if titles else None


def fetch_aps_pdf(
    doi: str, *, client: httpx.Client | None = None, timeout: float = 60.0
) -> bytes | None:
    """Fetch an APS paper's PDF from the journal's own PDF path.

    ``link.aps.org`` answers 403 for a paywalled paper, and a redirect
    to the abstract for some of them, but the journal's own ``/pdf/``
    address serves the file. Measured on fourteen of the corpus's own APS
    papers -- from 1992 to 2024, paywalled or not -- all fourteen came
    back as a PDF.
    """
    if not doi.casefold().startswith("10.1103/"):
        return None
    template = "https://journals.aps.org/{code}/pdf/{doi}"
    if client is not None:
        return _fetch_aps_via(doi, template, client)
    for cli in _fetch_clients(timeout):
        data = _fetch_aps_via(doi, template, cli)
        if data:
            return data
    return None


def _fetch_aps_via(
    doi: str, template: str, client: httpx.Client
) -> bytes | None:
    """One attempt at the APS PDF path."""
    code = _aps_journal_code(doi, client)
    if not code:
        return None
    modern = aps_modern_doi(doi)
    url = template.format(code=code, doi=modern)
    try:
        resp = client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("aps fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    if not _looks_like_pdf(resp.content):
        return None
    return resp.content


_PDF_LINK_RE = re.compile(r'https?://[^\s"\'<>]+?\.pdf', re.IGNORECASE)


def _article_id(doi: str) -> str | None:
    """The article slug a Springer Nature DOI maps to.

    ``10.1038/s41467-019-11330-6`` -> ``s41467-019-11330-6``;
    ``10.1038/41598-020-1234-5``   -> ``41598-020-1234-5``.
    """
    tail = doi.split("/", 1)[-1] if "/" in doi else doi
    if not tail:
        return None
    return tail


def _candidate_urls(doi: str) -> list[str]:
    urls: list[str] = []
    for prefix, template in _DIRECT_PDF:
        if doi.casefold().startswith(prefix):
            article = _article_id(doi)
            if article:
                urls.append(template.format(
                    tail=article, doi=doi, article=article,
                    aps=aps_modern_doi(doi),
                ))
            break
    return urls


# DOI prefixes whose full text is not retrievable here and which therefore cost
# a download slot (and ~180 s on timeout) every time they are tried. SSRN in
# particular is an author-posting platform: 7 of the 24-hour failures were it.
_SKIP_DOWNLOAD_PREFIXES: tuple[str, ...] = (
    "10.2139/",   # SSRN — preprints behind an upload wall
    "10.31234/",  # Research Square
    "10.20944/",  # Preprints.org
)


def is_downloadable(doi: str) -> bool:
    """False for DOIs no channel here can retrieve.

    Checked before spending a download slot: a skipped DOI costs nothing,
    where a doomed attempt costs a timeout.
    """
    return not doi.casefold().startswith(_SKIP_DOWNLOAD_PREFIXES)


def _looks_like_pdf(payload: bytes) -> bool:
    return payload[:4] == _PDF_MAGIC and len(payload) >= _MIN_PDF_BYTES


def _fetch_clients(timeout: float, **kwargs: Any) -> list[httpx.Client]:
    """One client per route: the proxy first, then direct egress.

    See :func:`pdf_fetch_proxies` for why the proxy comes first and why the
    direct route is still tried.
    """
    return [
        httpx.Client(timeout=timeout, trust_env=False, proxy=proxy, **kwargs)
        for proxy in pdf_fetch_proxies()
    ]


def fetch_direct_pdf(
    doi: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> bytes | None:
    """Return the PDF bytes for ``doi``, or ``None`` when not directly served.

    Tries the deterministic ``/articles/<id>.pdf`` URL first, then falls back
    to reading a PDF link out of the article page. Both paths verify the magic
    bytes, so a challenge or paywall page cannot masquerade as a paper.
    """
    if client is not None:
        data, _ = _fetch_direct_via(doi, client)
        return data
    for cli in _fetch_clients(
        timeout, headers={"User-Agent": _UA}, follow_redirects=True
    ):
        try:
            data, answered = _fetch_direct_via(doi, cli)
        finally:
            cli.close()
        if data:
            return data
        if answered:
            # The route answered, it just did not have the file. The other
            # route would answer the same way, and a paywalled paper costs
            # a full timeout on each attempt -- trying both doubled the
            # time a hopeless candidate took.
            break
    return None


def _fetch_direct_via(doi: str, client: httpx.Client) -> tuple[bytes | None, bool]:
    """The direct-fetch routes for one client.

    Returns the PDF (or ``None``) and whether the route answered at all.
    """
    answered = False
    for url in _candidate_urls(doi):
        try:
            resp = client.get(url)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.debug("direct fetch failed for %s: %s", url, exc)
            continue
        answered = True
        if resp.status_code == 200 and _looks_like_pdf(resp.content):
            log.info("direct fetch: %s -> %d KB", doi, len(resp.content) // 1024)
            return resp.content, True

    # Fall back to the landing page and follow a PDF link from it.
    article = _article_id(doi)
    if not article:
        return None, answered
    for _prefix, template in _LANDING:
        landing = template.format(article=article, doi=doi)
        try:
            resp = client.get(landing)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.debug("landing fetch failed for %s: %s", landing, exc)
            continue
        answered = True
        if resp.status_code != 200:
            continue
        if "validate.perfdrive.com" in str(resp.url) or "captcha" in resp.text[:3000].lower():
            log.info("direct fetch: %s hit a challenge page", doi)
            return None, True
        for link in _unique(_PDF_LINK_RE.findall(resp.text)):
            if "esm/" in link or "static" in link:
                # Supplementary files, not the article.
                continue
            try:
                pdf = client.get(link)
            except (httpx.HTTPError, httpx.TimeoutException):
                continue
            answered = True
            if pdf.status_code == 200 and _looks_like_pdf(pdf.content):
                log.info("direct fetch: %s via page link -> %d KB",
                         doi, len(pdf.content) // 1024)
                return pdf.content, True
    return None, answered


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Elsevier                                                                    #
# --------------------------------------------------------------------------- #

# Elsevier's own PDF endpoint answers a plain request with a Cloudflare
# challenge page (403, 833 KB of HTML) -- from the institute's address
# too -- so the Elsevier papers were unreachable, and only about two
# thirds of them have a preprint to fall back on.
#
# The API is the way in. With an API key and the institute's own address
# the API serves the full text as a PDF; no insttoken is needed. Measured
# on four Physics Letters A / Physics C papers: 200, application/pdf,
# the file itself.
_ELSEVIER_API = "https://api.elsevier.com/content/article/doi/{doi}"

# Elsevier's own DOIs, and Physics Letters A's.
_ELSEVIER_PREFIXES: tuple[str, ...] = ("10.1016", "10.1134")


def _is_elsevier_doi(doi: str) -> bool:
    """True for a DOI Elsevier registered."""
    return doi.casefold().startswith(_ELSEVIER_PREFIXES)


def _elsevier_pdf_url(doi: str) -> str:
    """The API URL that serves the article for this DOI."""
    return _ELSEVIER_API.format(doi=doi)


def _elsevier_api_key() -> str:
    """The API key, if one is configured."""
    try:
        from superconductor_db.core.config import get_settings

        return (get_settings().elsevier_api_key or "").strip()
    except Exception:  # noqa: BLE001 - the setting is optional
        return ""


def fetch_elsevier_pdf(
    doi: str,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> bytes | None:
    """Fetch an Elsevier paper's PDF through the Elsevier API.

    Returns ``None`` for a DOI Elsevier does not own, when no API key is
    configured, and when the API answers with anything but the file.
    """
    if not _is_elsevier_doi(doi):
        return None
    key = api_key if api_key is not None else _elsevier_api_key()
    if not key:
        return None
    headers = {"X-ELS-APIKey": key, "Accept": "application/pdf"}
    url = _elsevier_pdf_url(doi)
    if client is not None:
        return _fetch_elsevier_via(url, headers, client)
    for cli in _fetch_clients(timeout):
        data = _fetch_elsevier_via(url, headers, cli)
        if data:
            return data
    return None


def _fetch_elsevier_via(
    url: str, headers: dict[str, str], client: httpx.Client
) -> bytes | None:
    """One attempt at the Elsevier API."""
    try:
        resp = client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("elsevier fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.debug("elsevier returned %s for %s", resp.status_code, url)
        return None
    if not _looks_like_pdf(resp.content):
        return None
    return resp.content


# --------------------------------------------------------------------------- #
# Frontiers                                                                    #
# --------------------------------------------------------------------------- #

# Frontiers is fully open access and serves the file to a plain client:
# the article id in the URL is the DOI itself. Measured: 200,
# application/pdf, 1.8 MB and 2.6 MB for two papers.
_FRONTIERS_PDF = "https://www.frontiersin.org/articles/{doi}/pdf"
_FRONTIERS_PREFIXES: tuple[str, ...] = ("10.3389",)


def _frontiers_url(doi: str) -> str:
    """The PDF URL for a Frontiers article."""
    return _FRONTIERS_PDF.format(doi=doi)


def _is_frontiers_doi(doi: str) -> bool:
    """True for a DOI Frontiers registered."""
    return doi.casefold().startswith(_FRONTIERS_PREFIXES)


def fetch_frontiers_pdf(
    doi: str, *, client: httpx.Client | None = None, timeout: float = 60.0
) -> bytes | None:
    """Fetch a Frontiers paper's PDF.

    Returns ``None`` for a DOI Frontiers does not own and for anything
    that is not the file itself.
    """
    if not _is_frontiers_doi(doi):
        return None
    url = _frontiers_url(doi)
    if client is not None:
        return _fetch_frontiers_via(url, client)
    for cli in _fetch_clients(timeout):
        data = _fetch_frontiers_via(url, cli)
        if data:
            return data
    return None


def _fetch_frontiers_via(url: str, client: httpx.Client) -> bytes | None:
    """One attempt at the Frontiers PDF."""
    try:
        resp = client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("frontiers fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    if not _looks_like_pdf(resp.content):
        return None
    return resp.content


# --------------------------------------------------------------------------- #
# IOP                                                                         #
# --------------------------------------------------------------------------- #

# IOP (Superconducting Science and Technology, J. Phys.: Condens. Matter, New
# J. Phys.) serves an hCaptcha on every *page* request, which is why a plain
# fetch of the article page lands on validate.perfdrive.com. The PDF endpoint
# is not behind it: sending a same-origin Referer returns the file directly.
# Measured: article page -> captcha, /pdf with Referer -> 1821 KB %PDF.
_IOP_PDF_TEMPLATES: tuple[str, ...] = (
    "https://iopscience.iop.org/article/{doi}/pdf",
)


def fetch_iop_pdf(
    doi: str, *, client: httpx.Client | None = None, timeout: float = 60.0
) -> bytes | None:
    """Fetch an IOP paper's PDF, which needs a Referer but not a browser.

    Returns ``None`` for non-IOP DOIs, for DOIs whose full text is not openly
    available, and for any response that is not a real PDF.
    """
    if not doi.casefold().startswith("10.1088/"):
        return None
    if client is not None:
        data, _ = _fetch_iop_via(doi, client)
        return data
    for cli in _fetch_clients(
        timeout,
        headers={
            "User-Agent": _UA,
            "Referer": "https://iopscience.iop.org/",
            "Accept": "application/pdf,*/*",
        },
        follow_redirects=True,
    ):
        try:
            data, answered = _fetch_iop_via(doi, cli)
        finally:
            cli.close()
        if data:
            return data
        if answered:
            break
    return None


def _fetch_iop_via(doi: str, client: httpx.Client) -> tuple[bytes | None, bool]:
    """The IOP routes for one client."""
    answered = False
    for template in _IOP_PDF_TEMPLATES:
        url = template.format(doi=doi)
        try:
            resp = client.get(url)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.debug("iop fetch failed for %s: %s", doi, exc)
            continue
        answered = True
        if resp.status_code == 200 and _looks_like_pdf(resp.content):
            log.info("iop fetch: %s -> %d KB", doi, len(resp.content) // 1024)
            return resp.content, True
        # A captcha page arrives with HTTP 200; record why so the log shows
        # the difference between "paywalled" and "blocked".
        if "validate.perfdrive.com" in str(resp.url):
            log.info("iop fetch: %s hit a captcha even with a Referer", doi)
    return None, answered


# --------------------------------------------------------------------------- #
# arXiv preprint fallback                                                       #
# --------------------------------------------------------------------------- #

_ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_ID_RE = re.compile(r"<id>https?://arxiv\.org/abs/([^<]+)</id>")

# arXiv's own metadata API answers 429 for this machine's address -- the
# proxy's exit IP is throttled -- so nearly every lookup failed and the
# preprint channel produced almost nothing. OpenAlex records the arXiv
# landing page as a *location* of the published paper, answers a lookup by
# DOI without charging for it, and is not throttled. Measured hit rates for
# the arXiv preprint: 100 % for the APS journals, Nature and APL, 83 % for
# Wiley, 66 % for Elsevier, 50 % for IOP.
_OPENALEX_WORK = "https://api.openalex.org/works/doi:{doi}"
# The identifier itself contains a slash for the old-style ids
# (``cond-mat/0105271v1``), so the capture runs to the end of the path.
_ARXIV_LOC_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(.+?)(?:[?#]|$)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Unpaywall: where the open-access copy of a paper lives                       #
# --------------------------------------------------------------------------- #

# The publisher's own endpoint refuses the script and the paper has no
# preprint, so it was never fetched -- and the search kept finding it. Most
# of those papers have an open-access copy somewhere: a repository, a
# preprint server, or the publisher's own OA deal. Unpaywall knows where,
# needs no key, and does not charge for the answer.
_UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}?email={email}"


def _unpaywall_urls(doi: str, client: httpx.Client) -> list[str]:
    """The open-access copies Unpaywall knows for this DOI."""
    try:
        from superconductor_db.core.config import get_settings

        email = get_settings().openalex_mailto
    except Exception:  # noqa: BLE001 - the setting is optional
        email = ""
    if not email:
        return []
    try:
        resp = client.get(
            _UNPAYWALL_API.format(doi=doi, email=email), timeout=30.0
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("unpaywall lookup failed for %s: %s", doi, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    urls: list[str] = []
    locations = list(data.get("oa_locations") or [])
    best = data.get("best_oa_location")
    if isinstance(best, dict):
        locations.insert(0, best)
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for key in ("url_for_pdf", "url"):
            url = loc.get(key)
            if isinstance(url, str) and url.startswith("http") and url not in urls:
                urls.append(url)
    return urls


def fetch_oa_pdf(
    doi: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> bytes | None:
    """Fetch an open-access copy of ``doi`` from wherever one exists.

    This is the route that reaches the papers the publisher's own endpoint
    refuses and that have no preprint: the open-access copy in a
    repository, or the publisher's OA version.
    """
    owns = client is None
    if owns:
        client = httpx.Client(
            headers={"User-Agent": _UA},
            follow_redirects=True,
            timeout=timeout,
            trust_env=False,
            proxy=pdf_fetch_proxies()[0],
        )
    try:
        for url in _unpaywall_urls(doi, client):
            try:
                resp = client.get(url, timeout=timeout)
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                log.debug("oa fetch failed for %s: %s", url, exc)
                continue
            if resp.status_code == 200 and _looks_like_pdf(resp.content):
                log.info("oa fetch: %s -> %d KB", doi, len(resp.content) // 1024)
                return resp.content
    finally:
        if owns:
            client.close()
    return None


def _clean_arxiv_id(raw: str) -> str:
    """Normalise an arXiv identifier taken from a landing-page URL."""
    ident = raw.strip().rstrip("/")
    if ident.endswith("/pdf"):
        ident = ident[: -len("/pdf")]
    if ident.endswith(".pdf"):
        ident = ident[: -len(".pdf")]
    return ident


def _openalex_arxiv_id(doi: str, client: httpx.Client) -> str | None:
    """The arXiv identifier OpenAlex records for this paper, or ``None``."""
    try:
        from superconductor_db.core.config import get_settings

        settings = get_settings()
        headers = {"User-Agent": _UA}
        if settings.openalex_api_key:
            headers["api_key"] = settings.openalex_api_key
        if settings.openalex_mailto:
            headers["mailto"] = settings.openalex_mailto
        resp = client.get(_OPENALEX_WORK.format(doi=doi), headers=headers)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("openalex lookup failed for %s: %s", doi, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        work = resp.json()
    except ValueError:
        return None
    if not isinstance(work, dict):
        return None
    for loc in work.get("locations") or []:
        url = (loc or {}).get("landing_page_url") or ""
        match = _ARXIV_LOC_RE.search(url)
        if match:
            ident = _clean_arxiv_id(match.group(1))
            if ident:
                return ident
    return None


def arxiv_id_for_doi(doi: str, *, client: httpx.Client | None = None) -> str | None:
    """Return the arXiv identifier a journal DOI was preprinted as, if any.

    A large share of Physical Review papers appear on arXiv first, and the
    preprint is byte-identical in content. IOP and Elsevier papers are usually
    absent, so this is a supplement to the publisher route rather than a
    replacement for it.
    """
    owns = client is None
    if owns:
        client = httpx.Client(headers={"User-Agent": _UA}, timeout=40,
                              follow_redirects=True, trust_env=False,
                              proxy=pdf_fetch_proxies()[0])
    try:
        found = _openalex_arxiv_id(doi, client)
        if found:
            return found
        resp = client.get(
            _ARXIV_API,
            params={"search_query": f"doi:{doi}", "max_results": 1},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        match = _ARXIV_ID_RE.search(resp.text)
        return match.group(1) if match else None
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.debug("arxiv lookup failed for %s: %s", doi, exc)
        return None
    finally:
        if owns:
            client.close()


def _arxiv_id_from_doi(doi: str) -> str | None:
    """Return the arXiv identifier when the DOI already is one.

    ``10.48550/arXiv.cond-mat/0303027`` names the preprint itself; the
    file is at ``arxiv.org/pdf/cond-mat/0303027``.
    """
    lowered = doi.strip().lower()
    marker = "10.48550/arxiv."
    if not lowered.startswith(marker):
        return None
    arxiv_id = doi.strip()[len("10.48550/") :]
    # "arXiv.cond-mat/0303027" is the DOI spelling; the file lives at
    # "cond-mat/0303027".
    if arxiv_id.lower().startswith("arxiv."):
        arxiv_id = arxiv_id[len("arxiv.") :]
    return arxiv_id


def fetch_arxiv_pdf(
    doi: str, *, client: httpx.Client | None = None, timeout: float = 60.0
) -> bytes | None:
    """Fetch a paper's arXiv preprint, or ``None`` when it has none.

    Two cases. A journal DOI is looked up in arXiv's metadata to find
    the preprint that carries the same measurement. A DOI that *is* an
    arXiv identifier needs no lookup: the arXiv API's ``doi:`` search
    does not index the preprint DOIs, so asking it for one returns
    nothing, while the PDF is right there under its own number.
    """
    if client is not None:
        data, _ = _fetch_arxiv_via(doi, client)
        return data
    for cli in _fetch_clients(
        timeout, headers={"User-Agent": _UA}, follow_redirects=True
    ):
        try:
            data, answered = _fetch_arxiv_via(doi, cli)
        finally:
            cli.close()
        if data:
            return data
        if answered:
            break
    return None


def _fetch_arxiv_via(doi: str, client: httpx.Client) -> tuple[bytes | None, bool]:
    """The arXiv routes for one client."""
    answered = False
    arxiv_id = _arxiv_id_from_doi(doi) or arxiv_id_for_doi(doi, client=client)
    if not arxiv_id:
        return None, answered
    base = arxiv_id.split("v")[0]
    for url in (
        f"https://arxiv.org/pdf/{base}",
        f"https://export.arxiv.org/pdf/{base}",
    ):
        try:
            resp = client.get(url)
        except (httpx.HTTPError, httpx.TimeoutException):
            continue
        answered = True
        if resp.status_code == 200 and _looks_like_pdf(resp.content):
            log.info("arxiv fallback: %s -> %s (%d KB)",
                     doi, base, len(resp.content) // 1024)
            return resp.content, True
    return None, answered
