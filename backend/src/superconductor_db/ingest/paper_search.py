"""Paper search (spec §6.2).

Three sources, queried in parallel via :func:`asyncio.gather`:

- :func:`search_arxiv` — arXiv public Atom/RSS API.
- :func:`search_crossref` — CrossRef REST ``/works`` endpoint.
- :func:`search_semantic_scholar` — Semantic Scholar Graph ``/paper/search``.

Each returns a list of :class:`PaperMetadata`. :func:`search_all_sources` runs
all three concurrently with a 60s timeout per source (spec §6.2). The result
is deduplicated by DOI (preferred) or by normalised title.

The module also exposes :data:`SC_SEARCH_QUERIES` -- a curated list of
superconductor-flavoured search strings used by the worker bootstrap and the
admin trigger endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# SC-focused query presets (spec §6.2)                                        #
# --------------------------------------------------------------------------- #

SC_SEARCH_QUERIES: list[str] = [
    "superconducting AND (critical temperature OR Tc) AND (YBCO OR cuprate)",
    "iron-based superconductor AND (FeSe OR SmFeAsO OR BaFe2As2)",
    "MgB2 AND superconducting AND critical temperature",
    "Nb3Sn AND (critical current OR Hc2)",
    "pressure AND superconductivity AND (Tc OR transition temperature)",
    "BCS theory AND superconductor AND (Debye OR phonon)",
    "superconducting gap AND (s-wave OR d-wave) AND measurement",
    "Uemura classification AND unconventional superconductor",
    "specific heat AND superconductor AND (gamma OR Debye)",
    "MgB2 AND (coherence length OR penetration depth)",
    "LaFeAsO AND superconducting AND doping",
    "Sr2RuO4 AND unconventional superconductivity",
    "HgBa2Ca2Cu3O8 AND Tc AND high pressure",
    "NbTi AND superconducting AND magnet",
    "FeSe monolayer AND superconductivity AND STM",
    "isotope effect AND superconductor AND alpha",
    "Eliashberg theory AND superconductor AND spectral function",
    "Kondo lattice AND heavy fermion AND superconductivity",
    "PtAs2 AND spin triplet AND superconductivity",
    "borocarbide AND superconductor AND (YNi2B2C OR LuNi2B2C)",
    "underdoped cuprate AND pseudogap AND superconductivity",
    "magic angle graphene AND superconductivity AND twisted bilayer",
]


# --------------------------------------------------------------------------- #
# Public dataclass                                                            #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PaperMetadata:
    """Common paper shape produced by all searchers."""

    title: str
    doi: str | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    source: str = "unknown"
    published_date: str | None = None
    journal: str | None = None
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Keyword pre-filter                                                          #
# --------------------------------------------------------------------------- #


_SC_KEYWORDS: tuple[str, ...] = (
    "superconduct",
    "superconductor",
    "superconducting",
    "superconductivity",
    "Tc",
    "critical temperature",
    "BCS",
    "Cooper pair",
    "energy gap",
    "Meissner",
    "Cooper pairs",
    "cuprate",
    "YBCO",
    "MgB2",
    "FeSe",
    "NbTi",
    "Nb3Sn",
    "Uemura",
    "Eliashberg",
    "isotope effect",
    "phonon-mediated",
    "unconventional",
    "Kondo",
    "heavy fermion",
    "specific heat jump",
    "coherence length",
    "penetration depth",
    "critical field",
)


def is_likely_sc_paper(title: str, abstract: str | None) -> bool:
    """True iff the title/abstract hits at least one SC keyword."""
    haystack = (title or "") + "\n" + (abstract or "")
    haystack_l = haystack.lower()
    return any(k.lower() in haystack_l for k in _SC_KEYWORDS)


# --------------------------------------------------------------------------- #
# arXiv                                                                       #
# --------------------------------------------------------------------------- #


_ARXIV_API = "https://export.arxiv.org/api/query"


async def search_arxiv(
    query: str,
    *,
    max_results: int = 30,
    timeout: float = 60.0,
    client: httpx.AsyncClient | None = None,
) -> list[PaperMetadata]:
    """Query arXiv's public Atom API.

    ``query`` is sent verbatim; the caller is responsible for arXiv-syntax
    (e.g. ``cat:cond-mat.supr-con``). Non-arXiv-syntax queries are escaped.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": int(max_results),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        resp = await client.get(_ARXIV_API, params=params)
        resp.raise_for_status()
        return _parse_arxiv_atom(resp.text)
    finally:
        if owns_client:
            await client.aclose()


_ARXIV_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_ARXIV_TEXT_RE = re.compile(r"<\s*(\w+)[^>]*>(.*?)</\s*\1\s*>", re.DOTALL)


def _parse_arxiv_atom(body: str) -> list[PaperMetadata]:
    out: list[PaperMetadata] = []
    for entry in _ARXIV_ENTRY_RE.findall(body):
        title = _xml_text(entry, "title") or ""
        summary = _xml_text(entry, "summary") or ""
        published = _xml_text(entry, "published")
        authors_raw = re.findall(r"<author>\s*<name>(.*?)</name>", entry, re.DOTALL)
        # arXiv ID for URL.
        arxiv_id_match = re.search(r"<id>\s*(http[^<]+)</id>", entry)
        arxiv_id = ""
        if arxiv_id_match:
            arxiv_id = arxiv_id_match.group(1).strip()
        out.append(
            PaperMetadata(
                title=title.strip().replace("\n", " "),
                abstract=summary.strip() or None,
                source="arxiv",
                published_date=published,
                authors=[a.strip() for a in authors_raw],
                url=arxiv_id or None,
                extra={"arxiv_id": arxiv_id.split("/")[-1] if arxiv_id else None},
            )
        )
    return out


def _xml_text(entry: str, tag: str) -> str | None:
    m = _ARXIV_TEXT_RE.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry)
    if not m:
        return None
    return m.group(1).strip()


# --------------------------------------------------------------------------- #
# CrossRef                                                                    #
# --------------------------------------------------------------------------- #


_CROSSREF_API = "https://api.crossref.org/works"


async def search_crossref(
    query: str,
    *,
    max_results: int = 30,
    timeout: float = 60.0,
    client: httpx.AsyncClient | None = None,
) -> list[PaperMetadata]:
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(
            _CROSSREF_API,
            params={"query": query, "rows": int(max_results)},
            headers={"User-Agent": "superconductor-database/0.1 (mailto:research@example.org)"},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    items = ((data or {}).get("message") or {}).get("items") or []
    out: list[PaperMetadata] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title_list = item.get("title") or []
        title = title_list[0] if title_list else ""
        authors_raw = item.get("author") or []
        authors = [a.get("family", "") for a in authors_raw if isinstance(a, dict)]
        doi = item.get("DOI")
        abstract = item.get("abstract")
        # Strip JATS XML tags from CrossRef abstracts.
        if isinstance(abstract, str):
            abstract = re.sub(r"<[^>]+>", "", abstract)
        issued = (item.get("issued") or {}).get("date-parts") or []
        published = None
        if issued and isinstance(issued[0], list) and issued[0]:
            parts = issued[0]
            try:
                published = "-".join(str(p) for p in parts if p is not None)
            except Exception:
                published = None
        out.append(
            PaperMetadata(
                title=title,
                doi=doi,
                abstract=abstract,
                source="crossref",
                published_date=published,
                authors=authors,
                journal=(item.get("container-title") or [None])[0],
                url=item.get("URL"),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Semantic Scholar                                                            #
# --------------------------------------------------------------------------- #


_S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"


async def search_semantic_scholar(
    query: str,
    *,
    max_results: int = 30,
    timeout: float = 60.0,
    client: httpx.AsyncClient | None = None,
) -> list[PaperMetadata]:
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(
            _S2_API,
            params={
                "query": query,
                "limit": int(max_results),
                "fields": "title,abstract,authors,year,externalIds,venue,openAccessPdf",
            },
            headers={"User-Agent": "superconductor-database/0.1"},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    items = (data or {}).get("data") or []
    out: list[PaperMetadata] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        authors_raw = item.get("authors") or []
        authors = [
            a.get("name", "")
            for a in authors_raw
            if isinstance(a, dict) and a.get("name")
        ]
        ext = item.get("externalIds") or {}
        doi = ext.get("DOI") if isinstance(ext, dict) else None
        oa = item.get("openAccessPdf") or {}
        url = oa.get("url") if isinstance(oa, dict) else None
        year = item.get("year")
        published_date = str(year) if year else None
        out.append(
            PaperMetadata(
                title=title,
                doi=doi,
                abstract=item.get("abstract"),
                source="semantic_scholar",
                published_date=published_date,
                authors=authors,
                journal=item.get("venue"),
                url=url,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #


_SOURCE_NAMES = frozenset({"arxiv", "crossref", "semantic_scholar"})


async def search_all_sources(
    query: str,
    *,
    max_results: int = 30,
    sources: tuple[str, ...] | list[str] | None = None,
    timeout_per_source: float = 60.0,
) -> list[PaperMetadata]:
    """Run ``sources`` (default: all three) in parallel and return deduped papers.

    Each source has an independent 60s timeout. Errors from a single source
    are logged and that source's results dropped, so a flaky endpoint can't
    block the worker.

    Functions are looked up at *call time* via ``getattr`` so unit tests can
    patch ``search_arxiv`` etc. on the module without the dict snapshot
    going stale.
    """
    if sources is None:
        sources = ("arxiv", "crossref", "semantic_scholar")
    selected = [s for s in sources if s in _SOURCE_NAMES]
    if not selected:
        return []

    tasks = []
    for src in selected:
        func = getattr(sys.modules[__name__], f"search_{src}", None)
        if func is None:
            continue
        tasks.append(_guarded_search(func, query, max_results, timeout_per_source, src))
    raw_results = await asyncio.gather(*tasks, return_exceptions=False)
    flat: list[PaperMetadata] = []
    for r in raw_results:
        flat.extend(r)
    return _deduplicate(flat)


async def _guarded_search(
    func,
    query: str,
    max_results: int,
    timeout: float,
    source_name: str,
) -> list[PaperMetadata]:
    try:
        return await func(query, max_results=max_results, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — guarded orchestration; log and skip.
        log.warning("search_all_sources: %s failed: %s", source_name, exc)
        return []


# --------------------------------------------------------------------------- #
# Deduplication                                                               #
# --------------------------------------------------------------------------- #


def _deduplicate(papers: list[PaperMetadata]) -> list[PaperMetadata]:
    """Deduplicate by DOI when present, else by normalised title."""
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    out: list[PaperMetadata] = []
    for p in papers:
        if p.doi:
            doi_key = p.doi.strip().lower()
            if doi_key in seen_doi:
                continue
            seen_doi.add(doi_key)
            out.append(p)
            continue
        title_key = _normalise_title(p.title)
        if not title_key or title_key in seen_title:
            continue
        seen_title.add(title_key)
        out.append(p)
    return out


def _normalise_title(title: str) -> str:
    """Canonicalise a paper title for fuzzy dedup.

    Strips trailing punctuation, collapses whitespace, lowercases. Treats
    "MgB2 Superconductor Study" and "MgB2 Superconductor study." as the
    same title.
    """
    if not title:
        return ""
    cleaned = re.sub(r"\s+", " ", title.strip().lower())
    return cleaned.rstrip(".!?,;:")


# Re-export for tests.
__all__ = [
    "PaperMetadata",
    "SC_SEARCH_QUERIES",
    "is_likely_sc_paper",
    "search_all_sources",
    "search_arxiv",
    "search_crossref",
    "search_semantic_scholar",
    "_deduplicate",
]


def _quote(s: str) -> str:  # noqa: ARG001 - kept for back-compat
    """Tiny shim used by older callers; delegates to :func:`urllib.parse.quote_plus`."""
    return quote_plus(s)