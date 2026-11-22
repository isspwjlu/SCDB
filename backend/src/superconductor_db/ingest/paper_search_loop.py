"""Continuous paper-discovery loop.

Unlike ``curated_loop`` (which cycles a fixed list of 52 arXiv IDs
until dedup'd out), this script queries the arXiv public Atom API for
*new* SC preprints each round and feeds them through the same
``paper_ingest.ingest_one`` path. Because dedup keys on
``(compound, doi)``, two papers reporting the same compound at
different Tcs produce two rows — so the DB grows continuously.

Search strategy:
  Each round we query arXiv with 5 highly-recall SC queries sorted
  by ``submittedDate`` descending. We slide the ``start`` cursor so
  the same papers don't re-appear, and we dedupe across rounds via
  an in-memory set of DOIs seen this process lifetime.

Run alongside the curated loop:
  cd backend
  python -m superconductor_db.ingest.paper_search_loop --delay 120
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import superconductor_db.db.models  # noqa: F401
from superconductor_db.db import Base
from superconductor_db.db.models.ingest import IngestRun
from superconductor_db.db.models.paper import Paper
from superconductor_db.ingest.netresilience import wait_for_network
from superconductor_db.ingest.db_setup import build_ingest_engine
from superconductor_db.ingest.paper_ingest import ingest_one


def _default_database_url() -> str:
    """Fall back to the configured DATABASE_URL.

    A hard-coded relative ``sqlite:///./_dev.db`` silently creates a new
    empty database whenever the process starts from a different working
    directory, which looks like total data loss.
    """
    from superconductor_db.core.config import get_settings

    return get_settings().database_url or "sqlite:///./_dev.db"


log = logging.getLogger("paper_search_loop")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Monotonic timestamp of the last arXiv API call, used to pace requests.
_ARXIV_LAST_REQUEST: list[float] = [0.0]

# CrossRef REST API — primary discovery source.
_CROSSREF_API = "https://api.crossref.org/works"

# OpenAlex — open scholarly index (no API key required). Its key advantage is
# the ``is_oa:true`` filter, which restricts results to open-access papers. The
# project needs real journal literature (Physical Review B/C, Nature, Science
# and their sister journals, Superconducting Science and Technology), and those
# are normally paywalled — CrossRef returned ~92% unretrievable papers, while
# OpenAlex with the OA filter returns exactly the subset we can actually read.
_OPENALEX_API = "https://api.openalex.org/works"


def _openalex_mailto() -> str:
    """Contact address sent with every OpenAlex request.

    OpenAlex uses it for the polite pool and for abuse follow-up, so it
    has to be an address somebody reads.  It is not written down here:
    set ``OPENALEX_MAILTO`` to the address that should get the
    follow-up, and the requests join the polite pool.  Without it the
    requests still work, they just get the smaller allowance.
    """
    try:
        from superconductor_db.core.config import get_settings

        return (get_settings().openalex_mailto or "").strip()
    except Exception:
        return ""


def _openalex_params(**extra: Any) -> dict[str, Any]:
    """Query parameters shared by every OpenAlex call.

    An API key raises the daily allowance by two orders of magnitude; the
    free pool without one was exhausted within a day of this loop running.
    """
    params: dict[str, Any] = dict(extra)
    mailto = _openalex_mailto()
    if mailto:
        params["mailto"] = mailto
    key = ""
    try:
        from superconductor_db.core.config import get_settings

        key = (get_settings().openalex_api_key or "").strip()
    except Exception:
        key = ""
    if key:
        params["api_key"] = key
    return params

# DOI prefixes for preprint servers and repositories. OpenAlex indexes these
# alongside journals, but ingesting them defeats the purpose of this source
# (peer-reviewed journal coverage) and they rarely contain a measurement
# table to extract.
_PREPRINT_DOI_MARKERS: tuple[str, ...] = (
    "10.5281/",    # Zenodo
    "10.21203/",   # Research Square
    "10.31234/",   # Authorea
    "10.20944/",   # Preprints.org
    "10.1101/",    # bioRxiv / arXiv-hosted DOIs
    "10.48550/",   # arXiv
    "10.26434/",   # ChemRxiv
)

# Journals the project is meant to cover. OpenAlex does not need this to
# filter, but it lets the logs state which venues are landing.
_TARGET_VENUES: tuple[str, ...] = (
    "physical review",
    "nature",
    "science",
    "superconducting science",
    "physical review letters",
    "physical review x",
    "review of modern physics",
    "journal of physics: condensed matter",
    "journal of superconductivity",
)

# CrossRef asks sustained clients to stay under ~1 request/second. A
# 1.5 s gap honours that and costs a third of what 4 s did: the round
# issues two dozen CrossRef queries, so the gap was a minute of the
# round's budget.
_CROSSREF_MIN_INTERVAL_S = 1.5
_CROSSREF_BACKOFF_S = 30.0
_CROSSREF_LAST_REQUEST: list[float] = [0.0]


# CrossRef queries — plain English, sorted by publication date so each round
# surfaces fresh papers rather than re-finding the same ones. CrossRef is the
# primary source because the arXiv and Semantic Scholar APIs are frequently
# rate-limited (HTTP 429) from this network, which previously left the search
# loop with nothing to ingest.
# Queries aimed at the target journals. Kept short and measurement-led: a
# long phrase narrows an already venue-scoped search to nothing.
VENUE_QUERIES: tuple[str, ...] = (
    "superconducting transition critical temperature",
    "resistivity pressure superconductivity",
    "upper critical field anisotropy",
    "energy gap spectroscopy",
    "specific heat entropy",
)

# ISSNs of the journals this database covers. CrossRef accepts one per query,
# which is the cheapest way to stay on-topic: it returns nothing outside the
# venue, so no title filtering is doing the heavy lifting. **It costs no
# OpenAlex budget**, so this is the route to widen the coverage on.
#
# Every ISSN here was verified two ways: the journal record CrossRef holds
# for it, and a query of the journal's newest papers with the
# superconductor title gate applied. The counts in brackets are how many
# of the fifty newest papers of that journal passed the gate.
#
# Two things were wrong before and cost the journals entirely:
# * **The ISSN has to be the one CrossRef registers the journal under**,
#   which is not the print ISSN in every case: Physics Letters A is
#   0375-9601 and not 0379-9691, the Journal of the Physical Society of
#   Japan is 0031-9015 and not 1340-7410. An ISSN CrossRef does not know
#   answers every query with an empty list.
# * **1367-2630 is New Journal of Physics**, not Superconducting Science
#   and Technology. The entry that used to carry that name was querying
#   the wrong journal all along.
_TARGET_ISSN: dict[str, str] = {
    "Physical Review B": "1098-0121",
    "Physical Review C": "0556-2813",
    "Physical Review Letters": "0031-9007",
    "Physical Review X": "2160-3308",
    "Physical Review Materials": "2475-9953",      # 2 of the newest 50
    "Physical Review Applied": "2331-7019",         # 1
    "Nature Communications": "2041-1723",            # 1
    "Nature Physics": "1745-2473",                  # 4
    "Scientific Reports": "2045-2322",
    "npj Quantum Materials": "2397-4648",           # 3
    "Journal of Physics: Condensed Matter": "0953-8984",  # 1
    "Physica C: Superconductivity": "0921-4534",     # 12 -- the SC journal
    "Solid State Communications": "0038-1098",       # 5
    "Chinese Physics B": "1674-1056",               # 3
    "Applied Physics Letters": "0003-6951",          # 1
    "Journal of Applied Physics": "0021-8979",       # 1
    "Journal of the Physical Society of Japan": "0031-9015",  # 2
    "Physics Letters A": "0375-9601",
    # The journals that were missing, and the ones that were there under
    # the wrong identifier.
    #
    # "Superconducting Science and Technology" was filed under
    # 1367-2630, which is **New Journal of Physics** -- so the flagship
    # superconducting journal was never read at all. Its own eISSN is
    # 1361-6668 (and it shares 0953-2048 with the journal it absorbed).
    "Superconducting Science and Technology": "1361-6668",
    "Superconductivity": "2772-8307",               # the Elsevier SC journal
    "Science Advances": "2375-2548",                # open access, wide
    "Journal of Physics and Chemistry of Solids": "0022-3697",
    "Journal of Superconductivity and Novel Magnetism": "1557-1939",
    "npj Computational Materials": "2057-3960",
}

SC_SEARCH_QUERIES: tuple[str, ...] = (
    # Queries are phrased around a *measurement* plus a compound family, not
    # around the word "superconductor" alone. Searches keyed on the latter kept
    # returning vortex-dynamics and theory papers with no Tc value in them.
    "nickelate critical temperature resistivity measurement",
    "cuprate critical temperature zero resistance transport",
    "iron pnictide superconducting transition specific heat",
    "high pressure hydride Tc onset GPa resistivity",
    "MgB2 critical temperature upper critical field",
    "heavy fermion specific heat superconducting transition",
    "superconducting gap energy STM spectroscopy",
    "Ruddlesden-Popper nickelate resistivity pressure",
    "penetration depth lambda temperature dependence measurement",
    "upper critical field anisotropy Hc2 measurement",
    "niobium titanium alloy critical temperature",
    "two-gap s-wave superconductivity spectroscopy",
    "bismuth strontium calcium copper oxide critical temperature",
    "superconducting onset resistivity magnetic field",
    "magnetization hysteresis critical temperature superconductor",
)


# arXiv queries kept as a secondary source: they target preprints and family
# names that CrossRef's title/abstract search matches less reliably, but the
# endpoint is often 429-limited, so a failing arXiv query is not fatal.
ARXIV_SEARCH_QUERIES: tuple[str, ...] = (
    'cat:cond-mat.supr-con AND abs:(superconduct* OR Tc OR "critical temperature")',
    'all:"superconductor" AND (all:cuprate OR all:"iron pnictide" OR all:"MgB2")',
    'all:(nickelate OR NiO2 OR "La3Ni2O7" OR "La4Ni3O10" OR SrNiO2)',
    'all:superconduct* AND (all:Ruddlesden-Popper OR all:pyrochlore OR all:spin-triplet)',
)


def _parse_atom_dois(body: str) -> list[str]:
    """Parse arXiv Atom XML and return ``10.48550/arXiv.<id>`` for each entry."""
    out: list[str] = []
    in_entry = False
    entry_id: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("<entry>"):
            in_entry = True
            entry_id = None
            continue
        if line.startswith("</entry>"):
            if entry_id and "/abs/" in entry_id:
                arxiv_id = entry_id.rsplit("/abs/", 1)[-1]
                if arxiv_id:
                    out.append(f"10.48550/arXiv.{arxiv_id}")
            in_entry = False
            entry_id = None
            continue
        if in_entry and line.startswith("<id>") and line.endswith("</id>"):
            entry_id = line[4:-5]
    return out


async def _search_arxiv(
    client: httpx.AsyncClient,
    query: str,
    *,
    start: int,
    max_results: int,
    min_interval: float = 3.5,
) -> list[str]:
    """Query the arXiv Atom API, honouring its published rate limit.

    arXiv asks for >=3 s between requests and answers bursts with HTTP 429.
    The caller issues one request per (query, offset) pair, so without this
    throttle a round fires ~18 requests back-to-back and every one is rejected.
    """
    # Client-side pacing: sleep so consecutive calls are >= min_interval apart.
    now = time.monotonic()
    wait = _ARXIV_LAST_REQUEST[0] + min_interval - now
    if wait > 0:
        await asyncio.sleep(wait)
    _ARXIV_LAST_REQUEST[0] = time.monotonic()

    # arXiv drops the occasional connection when a run issues many requests
    # in a row, and httpx's exception carries no message of its own -- so the
    # log showed an empty reason and the query looked broken when it was
    # transient. Retry, and say what actually went wrong.
    resp = None
    for attempt in range(1, 4):
        try:
            resp = await client.get(
                "https://export.arxiv.org/api/query",
                params={
                    "search_query": query,
                    "start": start,
                    "max_results": max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
                timeout=30,
            )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            if attempt < 3:
                await asyncio.sleep(3.0 * attempt)
                continue
            log.warning(
                "arxiv query failed (%s): %s: %s",
                query, type(exc).__name__, exc or "no detail",
            )
            return []
        break
    if resp is None:
        return []
    if resp.status_code == 429:
        log.warning("arxiv rate-limited (429) for %r — backing off", query)
        await asyncio.sleep(15.0)
        return []
    if resp.status_code != 200:
        log.warning("arxiv returned %s for query %r", resp.status_code, query)
        return []
    return _parse_atom_dois(resp.text)


async def _search_crossref(
    client: httpx.AsyncClient,
    query: str = "",
    *,
    max_results: int,
    offset: int = 0,
    issn: str | None = None,
    from_date: str | None = "2023-01-01",
    newest_first: bool = False,
) -> list[str]:
    """Query CrossRef, optionally scoped to one journal by ISSN, and return DOIs.

    Scoping by ISSN is what makes this a journal pipeline rather than a
    general index, and it costs no OpenAlex budget, so this source keeps
    working when the venue queries there are rate-limited.

    An empty ``query`` with ``newest_first`` asks for the journal's own
    papers in publication order. That is the right way to read a journal
    whose titles do not share a phrase: a query of "superconducting
    transition" matched nothing at all in Physics Letters A, the Journal
    of the Physical Society of Japan or Europhysics Letters, because that
    phrase is not in their titles. The superconductor title gate below
    picks the papers worth having out of the journal instead.
    """
    # Pacing: keep at least _CROSSREF_MIN_INTERVAL_S between requests.
    wait = _CROSSREF_LAST_REQUEST[0] + _CROSSREF_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _CROSSREF_LAST_REQUEST[0] = time.monotonic()

    try:
        filters = []
        if issn:
            filters.append(f"issn:{issn}")
        if from_date:
            filters.append(f"from-pub-date:{from_date}")
        # Only the papers whose publisher offers a full-text link are worth
        # a download slot. Measured on one query: 25 candidates gave 2
        # downloadable papers without this filter and 8 with it -- the
        # rest are paywalled records with no preprint and no PDF, and each
        # one costs a timeout. It costs nothing: CrossRef is free.
        filters.append("has-full-text:true")
        params: dict[str, Any] = {
            "rows": int(max_results),
            "offset": int(offset),
            "select": "DOI,title,issued,container-title",
            **({"filter": ",".join(filters)} if filters else {}),
        }
        if query:
            params["query"] = query
        if newest_first:
            params["sort"] = "published"
            params["order"] = "desc"
        resp = await client.get(
            _CROSSREF_API,
            params=params,
            headers={"User-Agent": f"superconductor-database/0.1 (mailto:{_openalex_mailto()})"},
            timeout=45,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("crossref query failed (%s): %s", query, exc)
        return []
    if resp.status_code == 429:
        log.warning("crossref rate-limited (429) for %r — backing off", query)
        # CrossRef's polite-pool wants sustained <1 req/s. Backing off hard
        # rather than immediately retrying is what keeps the pool alive.
        await asyncio.sleep(_CROSSREF_BACKOFF_S)
        return []
    if resp.status_code != 200:
        log.warning("crossref returned %s for %r", resp.status_code, query)
        return []
    try:
        items = (resp.json().get("message") or {}).get("items") or []
    except ValueError:
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or [""])[0]
        if not isinstance(title, str):
            continue
        # CrossRef is a general scholarly index, so keyword queries surface
        # plenty of material-science noise. Requiring superconductor vocabulary
        # in the title cuts the ingest cost of non-SC papers dramatically.
        if not _looks_like_sc_measurement_title(title):
            continue
        doi = item.get("DOI")
        if doi:
            out.append(str(doi))
    return out


# Vocabulary that marks a paper as about superconductivity. Matched against the
# title, which CrossRef always provides.
_SC_TITLE_RE = re.compile(
    # ``\s*`` between the letter and its subscript: PDFs and indexes write
    # "T c", "T_C" and "Tc" interchangeably, and a strict word boundary drops
    # every one of those.
    r"superconduct|critical temperature|\bT\s*c\b|\bH\s*c2?\b|"
    r"pairing|Meissner|\bBCS\b|niobium|Nb3Sn|cuprate|"
    # Application-named titles: "critical current density of an NbTi wire"
    # never says "superconducting" but reports a Tc-bearing measurement.
    r"\bNbTi\b|Nb3Sn|V3Ga|\bREBCO\b|123 superconductor|"
    r"high[- ]Tc|superconducting (?:wire|coil|tape|cable|bulk|film|monolayer)|"
    r"nickelate|pnictide|hydride|chalcogenide|MgB?2|"
    # The bismuth cuprate is written Bi-2212 or Bi2212; a bare "Bi" must
    # not count, or "Orbital" matches it and a spintronics paper with
    # "magnetization" in its title passes the superconductor gate.
    r"YBCO|Bi-?2[0-9]{0,3}|iron-based|p-?wave|s-?wave|d-?wave|triplet|"
    r"upper critical field|energy gap|penetration depth|coherence length|"
    # Papers titled after the material alone still report its critical
    # temperature in the text, so the compound name is enough of a hook.
    r"CeCoIn5|CeCu2Si2|URu2Si2|UPt3|UBe13|CeRhIn5|YbRh2Si2|"
    r"CaH6|LaH10|YH[0-9]|ScH[0-9]|H3S|"
    r"BaFe2As2|SmFeAsO|LaFeAsO|NdFeAsO|CeCoIn5|FeSe|Sr2RuO4|YBa2Cu3O7|"
    r"La3Ni2O7|La4Ni3O10|LaNiO3|NdNiO2|PrNiO2|"
    r"Bi-?2[0-9]{3}|Y-?1[0-9]{2}|Hg-?1[0-9]{3}|Tl-?2[0-9]{3}",
    re.IGNORECASE,
)

# Titles that are clearly not measurements even though they mention Tc.
_SC_TITLE_BLOCKLIST_RE = re.compile(
    r"\b(review|book|chapter|editorial|erratum|corrigendum|retraction|"
    r"preface|index|machine learning|first-principles study of the|"
    r"applications of)\b",
    re.IGNORECASE,
)

# Titles reporting a *measurement* — these are the papers that carry a Tc
# number the extractor can verify against the text. Searches kept surfacing
# vortex-dynamics, magnetic-domain and topological-theory papers whose only
# mention of "superconducting" is qualitative: 8 such papers yielded 2
# extractable records, and those 2 already existed in the database. Requiring
# measurement vocabulary costs some recall but is where the yield is.
_SC_MEASUREMENT_RE = re.compile(
    r"critical temperature|\bT\s*c\b|zero[\s-]?resistance|resistivit|"
    r"transport|specific heat|heat capacit|magnetisation|magnetization|"
    # Naming a measured quantity is enough even without the word "Tc": a paper
    # titled "critical current density of an NbTi wire" reports a Tc-bearing
    # measurement, and was being discarded before the extractor ever saw it.
    r"critical current|meissner|magnetic hysteresis|"
    r"penetration depth|upper critical field|\bH\s*c2?\b|"
    r"energy gap|\b2.?\s*Δ/|gap symmetr|superconducting transition|"
    r"pressure[\s-]?induced superconduct|induced superconductiv|"
    r"\bonset\b|polymorph|susceptibilit|thermodynamic|"
    r"\bmeasure(?:ment|d)\b|\bdetermin(?:e|ed|ing)\b|\breport(?:s|ed)?\b|"
    r"\bpropert(?:y|ies)\b|characteri[sz]ation|investigation|observation|"
    # "Superconductivity in BaFe2As2 thin films" names no technique, but such
    # papers still report a transport curve. Treat a bare claim of
    # superconductivity in a named material as a measurement paper.
    r"superconductivity in|"
    r"superconducting (?:state|phase|transition|gap|order)|"
    # Question-form titles still report a measurement: "Is La3Ni2O6.5 a bulk
    # superconducting nickelate?" resolves to a zero-resistance measurement.
    r"\bbulk\b|\bthin film|\bsingle crystal|\bpolycrystal|\bsintered",
    re.IGNORECASE,
)

# Titles that are on-topic but structurally unlikely to contain a new,
# verifiable Tc value for a named compound.
_SC_NO_MEASUREMENT_RE = re.compile(
    # The word boundaries matter: "creep" sits inside "screep", and
    # "isotropic" inside "anisotropic" -- and the anisotropic
    # upper-critical-field papers are measurements, which is what this
    # filter is for.
    #
    # Vortex dynamics and flux flow are measurements too, and were on this
    # list by mistake. Measured on the corpus, the papers they rejected
    # produced records more often (35% and 40%) than the papers that
    # survived the veto (23%), so keeping them cost more than it saved.
    r"\bcreep\b|pinning cent|quantum vortex|vortex matter|"
    r"magnetic (domain|skyrmion|skyrmionium|monopole|order)|"
    r"altermagnet|altermagnetic|"
    r"topological (order|phase|transition|insulat|phase trans)|"
    r"machine learning|neural network|"
    r"\bDFT\b|first principles|ab initio|theoretical (study|model|modeling)|"
    r"arXiv preprint|we propose (a|an|the) (theory|model|mechanism)|"
    # "Emergent superconductivity" names a transition temperature, so the
    # word only vetoes the titles that use it for something else.
    r"\bisotropic\b|emergent (?!superconduct)|unconventional mechanism|"
    r"perspective|roadmap|overview|comment(?:ary)? on",
    re.IGNORECASE,
)


def _looks_like_sc_measurement_title(title: str) -> bool:
    """True for a superconductor paper that probably reports a Tc value.

    Applied after ``_looks_like_sc_title``. A title passes when it carries
    measurement vocabulary; an explicitly theoretical title is rejected even
    if it does.
    """
    if not _looks_like_sc_title(title):
        return False
    if _SC_NO_MEASUREMENT_RE.search(title):
        return False
    return bool(_SC_MEASUREMENT_RE.search(title))


def _looks_like_sc_title(title: str) -> bool:
    """True when a CrossRef title looks like a superconductor measurement paper."""
    if not _SC_TITLE_RE.search(title):
        return False
    return not _SC_TITLE_BLOCKLIST_RE.search(title)


async def _search_openalex(
    client: httpx.AsyncClient,
    query: str,
    *,
    max_results: int,
    offset: int = 0,
    oa_only: bool = True,
) -> list[str]:
    """Query OpenAlex and return DOIs of open-access superconductor papers.

    OpenAlex is the source that makes journal ingestion viable: with
    ``is_oa:true`` every hit is a paper whose full text can actually be
    fetched, which is what the ingest loop needs. Returns ``[]`` on any
    failure — OpenAlex is a best-effort source, not a dependency.
    """
    filters = ["from_publication_date:2015-01-01"]
    if oa_only:
        filters.append("is_oa:true")
    if _openalex_parked():
        # OpenAlex is in a cooldown; asking anyway only produces another
        # 429 and the free sources in the round must not wait for it.
        return []
    try:
        resp = await client.get(
            _OPENALEX_API,
            params=_openalex_params(
                **{
                    "search": query,
                    "per-page": min(int(max_results), 50),
                    # OpenAlex paginates with an opaque ``cursor`` token. It
                    # rejects ``offset`` outright, and since ~2026-10-05 also
                    # rejects the numeric ``cursor=*N`` form used here before
                    # (HTTP 400 — every main search failed and the loop ran on
                    # arXiv + venue queries alone). ``page`` is still accepted;
                    # with per-page equal to the per-round offset advance the
                    # page number maps exactly, and off-by-slice overlaps are
                    # absorbed by the caller's seen-DOI dedup.
                    **(
                        {"page": int(offset) // min(int(max_results), 50) + 1}
                        if offset
                        else {}
                    ),
                    "filter": ",".join(filters),
                    "sort": "publication_date:desc",
                    # No ``select``: OpenAlex rejects the field list with HTTP 400.
                }
            ),
            timeout=45,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("openalex query failed (%s): %s", query, exc)
        return []
    if resp.status_code in (403, 429):
        # A round fires 15 venue queries back to back. Once the daily or
        # per-second budget is spent the rest all fail, so park every
        # subsequent query rather than burning the round on 429s.
        _openalex_park(resp.status_code, resp)
        return []
    if resp.status_code != 200:
        log.warning("openalex returned %s for %r", resp.status_code, query)
        return []
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return []
    out: list[str] = []
    for work in results:
        if not isinstance(work, dict):
            continue
        title = work.get("title")
        if not isinstance(title, str) or not _looks_like_sc_measurement_title(title):
            continue
        doi = work.get("doi") or ""
        if doi.startswith("http://dx.doi.org/"):
            doi = doi[len("http://dx.doi.org/") :]
        elif doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/") :]
        if not doi.lower().startswith("10."):
            continue
        # Skip preprint/repositor records: this source exists to cover the
        # journal literature, and OpenAlex indexes Zenodo/figshare deposits
        # that no peer reviewer ever saw.
        lowered = doi.lower()
        if any(bad in lowered for bad in _PREPRINT_DOI_MARKERS):
            continue
        out.append(doi)
    if out:
        log.info("openalex: %r -> %d SC papers (of %d hits)",
                 query, len(out), len(results))
    return out



# --------------------------------------------------------------------------- #
# Venue-targeted discovery                                                     #
# --------------------------------------------------------------------------- #

# OpenAlex source IDs for the journals this database is meant to cover.
# Discovered via ``/sources?search=<journal name>``. Restricting the search to
# these venues is what turns the generic index into a journal pipeline: a
# broad "superconductivity" query returns mostly arXiv theory preprints,
# while a venue-scoped one returns the PRB papers whose measurements the
# extractor can actually read.
#
# The list grew once the daily allowance made a query per venue and term
# unaffordable: the round no longer asks every venue every time, but a
# slice of the venue-and-term matrix that rotates (see
# :func:`_venue_slice`). Widening the list therefore costs nothing --
# the number of queries a round spends is fixed, and the venues share it.
#
# Not every journal is here: OpenAlex has no source for Superconducting
# Science and Technology, npj Superconductivity or Nature Communications
# Materials, so those are reached through the CrossRef ISSN route instead.
_TARGET_SOURCES: dict[str, str] = {
    # Physical Review B is S6791298. There is also a source for the
    # journal's "Condensed matter" section (S4210174675) and it looks
    # right, but a work's *primary* source is the journal and not the
    # section, so every query against it came back empty: the flagship
    # journal of the list was dark for the whole run.
    "Physical Review B": "S6791298",
    "Physical Review Letters": "S24807848",
    "Nature Communications": "S64187185",
    "Applied Physics Letters": "S105243760",
    "Journal of Applied Physics": "S187991544",
    "Journal of the Physical Society of Japan": "S119212824",
    "Chinese Physics B": "S33442941",
    "Europhysics Letters": "S161185981",
    "Nature Physics": "S156274416",
    "npj Quantum Materials": "S4210221747",
    "Communications Physics": "S4210200995",
    "Scientific Reports": "S196734849",
    "Physica C: Superconductivity": "S139340696",
    "Physica B: Condensed Matter": "S34296711",
    "Physics Letters A": "S104875562",
    "Solid State Communications": "S82088436",
    "Physical Chemistry Chemical Physics": "S181332294",
    "Frontiers in Physics": "S2596760093",
    "Journal of Physics: Condensed Matter": "S190924190",
    "Materials Today Physics": "S2898609469",
    "Journal of Superconductivity and Novel Magnetism": "S89963981",
    "Communications in Theoretical Physics": "S165969127",
}

# Venues whose paywalled papers cannot be fetched at all.
#
# Springer Nature serves its PDF from a deterministic URL, so a paywalled
# Nature Communications paper is still downloadable. The APS journals are
# not: their PDF endpoint refuses a plain client with 403 and the browser
# gets a login page instead of the file. Asking for their paywalled
# papers therefore spends the budget and the download budget on papers
# that can never arrive -- a whole round of them produced nothing but
# 129 "no PDF link" messages. Their *open-access* papers can be fetched
# (the preprint is on arXiv), so those are what the query should ask for.
#
# The same holds for the other publishers that refuse a script: IOP puts
# a captcha on its PDF endpoint, AIP and RSC answer 403. The journals
# that serve their file to a plain client -- the Nature family, Elsevier
# (through its API), Springer, Frontiers, the Journal of the Physical
# Society of Japan -- are left out of this set, because their paywalled
# papers are still downloadable.
_VENUE_OA_ONLY: frozenset[str] = frozenset({
    "Physical Review B",
    "Physical Review Letters",
    "Applied Physics Letters",
    "Journal of Applied Physics",
    "Chinese Physics B",
    "Europhysics Letters",
    "Journal of Physics: Condensed Matter",
    "Physical Chemistry Chemical Physics",
    "Journal of Superconductivity and Novel Magnetism",
    "Communications in Theoretical Physics",
})

# How many venue queries one round spends.
#
# The daily allowance is the binding constraint: a dollar buys a
# thousand queries, and the round has to keep the other sources going
# as well. So the number is fixed, and the venues share it -- a round
# asks a slice of the venue-and-term matrix and the next round asks the
# next slice. Widening the venue list is therefore free.
_VENUE_QUERIES_PER_ROUND = 20

# How much wider than the round the ISSN route reads each journal.
# The rows are free; the title gate is what makes the window matter.
_ISSN_WINDOW_FACTOR = 4

# How many of the general searches go to OpenAlex. The rest are free on
# the CrossRef route.
_GENERAL_OPENALEX_QUERIES = 5

# CrossRef registration prefixes for the same publishers. Prefix matching
# reaches sibling journals the OpenAlex source list does not enumerate
# (Physical Review C, Scientific Reports, ...).
_TARGET_PREFIXES: tuple[str, ...] = (
    "10.1103",   # APS: Phys. Rev. B / C / Lett. / X / Applied / Materials
    "10.1038",   # Springer Nature: Nat. Commun. / Sci. Rep. / npj / Nat. Phys.
    "10.1088",   # IOP: Supercond. Sci. Technol. / J. Phys.: Condens. Matter
    "10.1002",   # Wiley
    "10.1016",   # Elsevier
)


def _is_target_doi(doi: str) -> bool:
    return doi.casefold().startswith(_TARGET_PREFIXES)


# OpenAlex throttle. A round issues one query per (venue x term); firing all
# of them back to back reliably trips the rate limiter, which then costs the
# whole round. Spacing them out costs a few seconds and keeps the round
# productive.
_OPENALEX_MIN_INTERVAL_S = 1.5
_OPENALEX_LAST_REQUEST: list[float] = [0.0]
_OPENALEX_PARKED_UNTIL: list[float] = [0.0]
_OPENALEX_PARK_S = 30.0
# A spent daily budget answers with the seconds until the budget resets.
# Parking for that long is worth it -- the loop keeps working on arXiv and
# CrossRef meanwhile, and every retry against a spent budget is one more
# 429. The cap is generous enough to cover the gap to the reset (it was
# 11.5 h when measured), so the loop wakes once and finds the budget
# renewed instead of waking twice to be refused again.
_OPENALEX_MAX_PARK_S = 24 * 3600.0


def _openalex_park(status_code: int, resp: Any = None) -> None:
    """Hold subsequent OpenAlex queries after a rate-limit response.

    ``resp`` carries the headers that say how long the limit lasts:
    ``Retry-After`` (seconds until midnight) and ``X-RateLimit-Reset``.
    """
    import time as _time

    seconds = _OPENALEX_PARK_S
    if resp is not None:
        for header in ("Retry-After", "X-RateLimit-Reset"):
            raw = resp.headers.get(header)
            if not raw:
                continue
            try:
                suggested = float(raw)
            except (TypeError, ValueError):
                continue
            if suggested > seconds:
                seconds = min(suggested, _OPENALEX_MAX_PARK_S)
            break
    _OPENALEX_PARKED_UNTIL[0] = max(
        _OPENALEX_PARKED_UNTIL[0], _time.monotonic() + seconds
    )
    log.warning(
        "openalex %s — pausing queries for %.0fs", status_code, seconds
    )


async def _openalex_throttle() -> None:
    """Wait out any active cooldown, then keep at least one gap per request."""
    import time as _time

    if _openalex_parked():
        # A spent budget is not a reason to hold the round: the free
        # sources (arXiv, CrossRef) come later in it and must not wait
        # hours for OpenAlex to come back.
        await asyncio.sleep(5.0)
        return
    gap = _OPENALEX_LAST_REQUEST[0] + _OPENALEX_MIN_INTERVAL_S - _time.monotonic()
    if gap > 0:
        await asyncio.sleep(gap)
    _OPENALEX_LAST_REQUEST[0] = _time.monotonic()


def _openalex_parked() -> bool:
    """True while OpenAlex is in a cooldown after a rate-limit response."""
    import time as _time

    return _OPENALEX_PARKED_UNTIL[0] > _time.monotonic()


async def _search_venue(
    client: httpx.AsyncClient,
    query: str,
    source_id: str,
    *,
    max_results: int = 10,
    page: int = 1,
    oa_only: bool = False,
) -> list[str]:
    """Search OpenAlex within one journal.

    The open-access filter is dropped on purpose for the publishers that
    serve their PDF to a plain client: a paywalled Nature paper is still
    retrievable, and excluding non-OA papers would discard the bulk of
    the literature. For the publishers that refuse (``oa_only=True``)
    the filter is what keeps the loop from spending its budget on papers
    that can never be downloaded.

    ``page`` is what makes this source worth its dollar. OpenAlex rejects
    ``offset``, so a venue query without a page number returns the *same*
    first slice on every round: the loop kept re-finding the papers it
    already had and paid for the privilege 15 times a round. Each round
    now advances one page, which costs exactly the same.

    The sort is by publication date, not by relevance. Paging through a
    relevance-sorted result walks backwards into the past, and the older
    the paper the less likely it is to be downloadable: every APS paper
    before 2020 sits behind a login wall that the browser cannot get
    past, while the ones from 2020 on come through. All 29 papers the
    browser ever fetched came from that later window.
    """
    filters = [
        f"primary_location.source.id:{source_id}",
        "from_publication_date:2015-01-01",
    ]
    if oa_only:
        filters.append("is_oa:true")
    if _openalex_parked():
        # OpenAlex is in a cooldown. Asking anyway only produces another
        # 429; the free sources in the round must not wait for it.
        return []
    try:
        resp = await client.get(
            _OPENALEX_API,
            params=_openalex_params(**{
                "search": query,
                "filter": ",".join(filters),
                "per-page": min(int(max_results), 50),
                **({"page": int(page)} if int(page) > 1 else {}),
                "sort": "publication_date:desc",
            }),
            timeout=45,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("venue query failed (%s/%s): %s", query, source_id, exc)
        return []
    if resp.status_code in (403, 429):
        # Parking matters here as much as it does in the main search: a
        # venue query that is not parked keeps hammering a spent budget,
        # and one round produced 7071 of those.
        _openalex_park(resp.status_code, resp)
        return []
    if resp.status_code != 200:
        return []
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return []
    out: list[str] = []
    for work in results:
        if not isinstance(work, dict):
            continue
        title = work.get("title")
        if not isinstance(title, str) or not _looks_like_sc_measurement_title(title):
            continue
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        if doi.lower().startswith("10."):
            out.append(doi)
    if out:
        log.info("venue %s: %r -> %d papers", source_id, query, len(out))
    return out


def _venue_pairs() -> list[tuple[str, str, str]]:
    """Every (venue, source id, term) the round could ask for."""
    return [
        (venue, source_id, term)
        for venue, source_id in _TARGET_SOURCES.items()
        for term in VENUE_QUERIES
    ]


def _venue_slice(round_index: int) -> list[tuple[str, str, str, int]]:
    """The venue-and-term pairs this round asks for, and the page for each.

    The daily allowance fixes the number of queries a round may spend, so
    the venues share it: the round asks a slice of the matrix that
    rotates, and every pair walks through its journal's list at its own
    pace. The pairs are laid out venue by venue, so a slice is a few
    whole journals with all of their terms.

    The page of a pair is how many times it has been asked before. The
    rotation is a contiguous window over a virtual index that wraps
    around the matrix, so a pair at position ``p`` is asked for once per
    lap of the matrix and the number of laps it has made by round ``r``
    is ``ceil((r * count - p) / total)``.
    """
    pairs = _venue_pairs()
    total = len(pairs)
    if not total:
        return []
    count = min(_VENUE_QUERIES_PER_ROUND, total)
    step = max(0, round_index - 1) * count
    start = step % total
    out: list[tuple[str, str, str, int]] = []
    for offset in range(count):
        index = (start + offset) % total
        venue, source_id, term = pairs[index]
        laps = 0
        if step > index:
            laps = -(-(step - index) // total)
        out.append((venue, source_id, term, laps + 1))
    return out


async def _search_round(
    client: httpx.AsyncClient,
    *,
    per_query: int,
    start_offset: int,
    use_arxiv: bool = True,
    use_crossref: bool = True,
    use_openalex: bool = True,
    use_venues: bool = True,
) -> list[str]:
    """One discovery round across the configured sources, returning DOIs.

    OpenAlex leads: it indexes the real journal literature this database is
    meant to cover (Physical Review B/C, Nature, Science and their sister
    journals, Superconducting Science and Technology) and its ``is_oa:true``
    filter restricts results to papers whose full text can actually be
    downloaded. arXiv supplies preprints; CrossRef runs last and mostly
    returns paywalled titles that waste the download budget.
    """
    all_dois: list[str] = []
    venue_dois: list[str] = []
    round_index = start_offset // max(1, per_query) + 1
    if use_venues and not _openalex_parked():
        # Venue-scoped searches first: the host network reaches APS and Nature
        # directly, so their paywalled papers are retrievable and are where the
        # measurements are.
        #
        # The round asks a rotating slice of the venue-and-term matrix, and
        # every pair carries its own page. The number of queries a round
        # spends is fixed by the daily allowance, so this is how the venue
        # list got wider without the allowance getting smaller.
        for venue, source_id, q, page in _venue_slice(round_index):
            dois = await _search_venue(
                client, q, source_id, max_results=per_query, page=page,
                oa_only=venue in _VENUE_OA_ONLY,
            )
            venue_dois.extend(dois)
            # OpenAlex allows ~10 req/s but also enforces a daily budget;
            # 15 queries in a burst spends the per-second share and then
            # trips the sustained limiter for the rest of the round.
            await _openalex_throttle()
    all_dois.extend(venue_dois)
    if use_crossref:
        # Independent quota: keeps the loop productive when OpenAlex is
        # rate-limited, and an ISSN keeps every hit inside a target journal.
        #
        # No query phrase here on purpose. "superconducting transition"
        # matched nothing at all in Physics Letters A, the Journal of the
        # Physical Society of Japan or Europhysics Letters -- that phrase
        # is not in their titles -- so those journals returned an empty
        # list every round for ever. The journal's own newest papers, with
        # the superconductor title gate applied, are what it should read.
        #
        # The offset advances with the round, so the route walks down the
        # journal instead of returning the same first slice for ever.
        for issn in _TARGET_ISSN.values():
            # A wider window than the round needs. The superconductor title
            # gate throws away most of what a journal publishes -- the
            # newest papers in a superconducting journal are often about
            # magnets and electronics rather than a measured transition
            # temperature -- so a window of fifty can come back with
            # nothing at all. CrossRef does not charge for the rows, only
            # the round trip, so the window is four times the round's
            # width and the offset advances with it.
            dois = await _search_crossref(
                client, max_results=per_query * _ISSN_WINDOW_FACTOR,
                issn=issn,
                offset=start_offset * _ISSN_WINDOW_FACTOR, newest_first=True,
                # Not the usual 2023: APS records the publication date
                # wrongly (sorted by it, their newest papers read as
                # 2015), so a date filter from 2023 returns **nothing**
                # for half the journals on this list. From 2010 they are
                # all back, and the offset does the walking anyway.
                from_date="2010-01-01",
            )
            all_dois.extend(dois)
            await asyncio.sleep(_CROSSREF_MIN_INTERVAL_S)
    if not venue_dois:
        # Every venue query was rate-limited. arXiv is independent of the
        # OpenAlex budget, so fall through to it rather than returning an
        # empty round and idling until the next delay.
        log.warning("no venue results (OpenAlex rate-limited); falling back to arXiv")
    if use_openalex and not _openalex_parked():
        # Only a few of the general searches. They have no open-access
        # filter, so they hand the download loop paywalled papers from
        # publishers that refuse a script -- and they cost the same
        # dollar as a venue query, which is scoped to a journal and *is*
        # filtered. Measured: these fifteen searches were two fifths of
        # the day's allowance and almost none of its papers. The free
        # CrossRef route below still runs all fifteen.
        for q in SC_SEARCH_QUERIES[:_GENERAL_OPENALEX_QUERIES]:
            dois = await _search_openalex(
                client, q, max_results=per_query, offset=start_offset
            )
            all_dois.extend(dois)
    if use_arxiv:
        for q in ARXIV_SEARCH_QUERIES:
            ids = await _search_arxiv(
                client, q, start=start_offset, max_results=per_query
            )
            all_dois.extend(ids)
    if use_crossref:
        for q in SC_SEARCH_QUERIES:
            dois = await _search_crossref(
                client, q, max_results=per_query, offset=start_offset
            )
            all_dois.extend(dois)
    seen: set[str] = set()
    deduped: list[str] = []
    for d in all_dois:
        if d not in seen:
            seen.add(d)
            deduped.append(d)
    return deduped


async def _async_main(args: argparse.Namespace) -> int:
    engine = build_ingest_engine(args.database_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    log.info(
        "paper_search_loop starting: per-query=%d max/round=%d delay=%ds",
        args.per_query, args.max_per_round, int(args.delay),
    )
    log.info("database: %s", args.database_url)

    # Seed the seen-set from the database. It used to start empty in memory,
    # so every supervisor restart re-ingested the same head of the CrossRef
    # result list — 242 DOIs were already in `materials`, and the loop spent
    # its download and model budget re-reading them.
    seen_dois: set[str] = set()
    with SessionLocal() as s:
        for (doi,) in s.execute(select(Paper.doi).where(Paper.doi.is_not(None))):
            seen_dois.add(doi.casefold())
    log.info("already ingested: %d papers", len(seen_dois))

    round_idx = 0
    offset = args.start_offset
    consecutive_stalled_rounds = 0
    # Deepest offset that ever downloaded a paper. The listing head is drained
    # after a few days of sweeping (every paper it lists is already in the
    # database), so a stall there must NOT rewind to 0 — that traps the sweep
    # in the drained head forever (measured 2026-10-07: 16 rewind cycles, new=0
    # every round). Un-ingested history lies beyond the frontier.
    max_productive_offset = 0

    async with httpx.AsyncClient() as client:
        while True:
            round_idx += 1
            run_id = uuid.uuid4()
            with SessionLocal() as s:
                s.add(
                    IngestRun(
                        id=run_id,
                        status="running",
                        started_at=datetime.now(timezone.utc),
                    )
                )
                s.commit()

            # Wait out an outage before searching: without this the loop
            # burns a round on a dead link and then sleeps the short --delay,
            # so recovery is noticed late and noisily.
            if not await wait_for_network(max_wait_s=0.0):
                log.warning("round %d skipped: still offline", round_idx)
            try:
                dois = await _search_round(
                    client,
                    per_query=args.per_query,
                    start_offset=offset,
                    use_arxiv=args.use_arxiv,
                    use_crossref=args.use_crossref,
                    use_openalex=args.use_openalex,
                    use_venues=args.use_venues,
                )
                log.info(
                    "round %d: %d new DOIs (offset=%d)",
                    round_idx, len(dois), offset,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("round %d: search crashed: %s", round_idx, exc)
                dois = []

            new_dois = [d for d in dois if d.casefold() not in seen_dois]
            seen_dois.update(new_dois)
            ingest_targets = new_dois[: args.max_per_round]

            total_extracted = 0
            total_stored = 0
            total_downloaded = 0
            total_failures = 0

            # Ingest the round's papers concurrently. Each paper is ~8 LLM
            # calls and takes 35-95 s almost entirely waiting on the model, so
            # serial processing made the round latency-bound. The semaphore
            # caps in-flight requests so this cannot trigger a rate limit.
            semaphore = asyncio.Semaphore(args.concurrency)

            async def ingest_one_limited(ident: str) -> tuple[int, int, bool]:
                async with semaphore:
                    return await ingest_one(SessionLocal, ident)

            async def safe_ingest(ident: str) -> tuple[int, int, bool]:
                try:
                    return await ingest_one_limited(ident)
                except Exception as exc:  # noqa: BLE001
                    log.exception("ingest %s failed: %s", ident, exc)
                    return 0, 0, False

            results = await asyncio.gather(
                *(safe_ingest(i) for i in ingest_targets)
            )
            for extracted, stored, downloaded in results:
                total_extracted += extracted
                total_stored += stored
                if downloaded:
                    total_downloaded += 1
            total_failures = len(ingest_targets) - sum(
                1 for e, _s, _d in results if e or _s or _d
            )

            with SessionLocal() as s:
                run = s.get(IngestRun, run_id)
                if run is not None:
                    run.papers_searched = len(dois)
                    run.papers_downloaded = total_downloaded
                    run.records_extracted = total_extracted
                    run.records_stored = total_stored
                    run.errors = total_failures
                    run.status = "completed" if total_failures == 0 else "failed"
                    run.finished_at = datetime.now(timezone.utc)
                    s.commit()

            log.info(
                "round %d done: search=%d new=%d ingested=%d downloaded=%d "
                "extracted=%d stored=%d",
                round_idx, len(dois), len(new_dois), len(ingest_targets),
                total_downloaded, total_extracted, total_stored,
            )

            if args.max_rounds and round_idx >= args.max_rounds:
                break

            # Stop when the search window is exhausted instead of polling the
            # same empty pages forever (which burned an LLM-free but pointless
            # request every cycle, and previously masked arXiv outages).
            #
            # A round is *stalled* in two ways: no new candidates at all, or
            # new candidates that produced zero downloads. The second form is
            # the trap: DOIs whose download fails never enter the database, so
            # they reappear as "new" every round and a new==0 rewind can never
            # fire — the cursor sinks deeper into stale, paywalled result
            # pages and stays there for hours (measured 2026-10-07: ~24 "new"
            # DOIs per round, 0-4 downloadable). Counting download-less
            # rounds toward the same rewind restores the fresh-results
            # equilibrium; the caller's seen-DOI dedup makes a rewind cheap.
            stalled = not new_dois or (
                bool(ingest_targets) and total_downloaded == 0
            )
            # A round that actually downloaded something marks its offset as
            # productive — downloads are the only proof a result page is worth
            # sweeping. "new but undownloadable" does not count: DOIs whose
            # download fails never enter the database, so they reappear as
            # "new" every round and would drag the frontier into stale,
            # paywalled result pages.
            if total_downloaded > 0:
                max_productive_offset = max(max_productive_offset, offset)
            if stalled:
                consecutive_stalled_rounds += 1
                if args.max_empty_rounds and (
                    consecutive_stalled_rounds >= args.max_empty_rounds
                ):
                    consecutive_stalled_rounds = 0
                    if offset < max_productive_offset:
                        # Stalling below the frontier means the listing head is
                        # fully drained — everything it lists is already in the
                        # database. Rewinding to 0 here would trap the sweep in
                        # the drained head forever (measured 2026-10-07: 16
                        # rewind cycles with new=0 every round). Jump to the
                        # frontier instead; un-ingested history lies beyond it.
                        log.info(
                            "head drained at offset %d — jumping to productive "
                            "frontier %d",
                            offset, max_productive_offset,
                        )
                        offset = max_productive_offset
                    else:
                        # The frontier itself is exhausted. Fall back to the
                        # head to pick up anything published since, and dig
                        # the frontier deeper for the next jump.
                        log.info(
                            "frontier %d exhausted — rewinding to head",
                            max_productive_offset,
                        )
                        offset = 0
                        # Dig deep per cycle: arXiv history has no staleness
                        # (any depth yields downloadable preprints), so a big
                        # step just skips ahead; ~200 entries per frontier
                        # exhaustion crosses the multi-hundred-entry drained
                        # head in a few cycles instead of a dozen.
                        max_productive_offset += 20 * args.per_query
                    # A source that returns nothing this many times running is
                    # probably rate-limited, not exhausted.
                    await asyncio.sleep(max(args.delay, 120))
            else:
                consecutive_stalled_rounds = 0

            offset += args.per_query
            # ``time.sleep`` blocks the whole event loop, so the concurrency
            # this loop is configured with collapses to one paper at a time for
            # the whole delay. Yielding instead lets the other workers finish
            # their in-flight downloads while this round rests.
            await asyncio.sleep(args.delay)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument("--per-query", type=int, default=8,
                        help="arXiv results per (query, offset) pair (default 8)")
    parser.add_argument("--max-per-round", type=int, default=8,
                        help="Cap papers ingested per round (default 8)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Papers ingested in parallel per round (default 4)")
    parser.add_argument("--delay", type=float, default=120.0,
                        help="Seconds between rounds (default 120)")
    parser.add_argument("--start-offset", type=int, default=0,
                        help="Initial arXiv start offset")
    parser.add_argument("--max-rounds", type=int, default=0,
                        help="Stop after N rounds (0 = unbounded)")
    parser.add_argument("--max-empty-rounds", type=int, default=5,
                        help="Stop after N consecutive rounds yielding no new papers (0 = never)")
    parser.add_argument("--no-arxiv", dest="use_arxiv", action="store_false",
                        help="Skip the arXiv source (it is frequently rate-limited)")
    parser.add_argument("--no-crossref", dest="use_crossref", action="store_false",
                        help="Skip CrossRef (most of its hits are paywalled)")
    parser.add_argument("--no-openalex", dest="use_openalex", action="store_false",
                        help="Skip OpenAlex (the open-access journal source)")
    parser.add_argument("--no-venues", dest="use_venues", action="store_false",
                        help="Skip venue-scoped searches (PRB / PRL / Nat. Commun.)")
    parser.set_defaults(
        use_arxiv=True, use_crossref=True, use_openalex=True, use_venues=True
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
