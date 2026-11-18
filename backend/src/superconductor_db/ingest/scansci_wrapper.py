"""scansci-pdf subprocess wrapper (spec §6.4).

scansci-pdf is the project-wide MCP for paper retrieval. We invoke it via
``asyncio.create_subprocess_exec`` so the heavy lifting (WebVPN, CARSI, Tor,
13+ source racing) stays in one place. The wrapper:

- Subprocesses ``scansci-pdf get <doi-or-url> --output <dir>``. The identifier
  is positional — the CLI has no ``--url`` flag.
- Caches the resulting PDF in ``settings.pdf_cache_dir`` keyed by SHA-256 of
  the DOI / URL. Cache hit returns immediately without re-running the CLI.
- Parses the JSON ``{path: ...}`` payload from stdout.
- Exposes :func:`parse_pdf_to_markdown`, which converts a downloaded PDF into
  a single markdown string with :mod:`docling` (see
  :mod:`superconductor_db.ingest.docling_parser`).

When the plain HTTP routes fail, the article page is opened in **ego lite**
(:mod:`superconductor_db.ingest.ego_fetch`) and the PDF is pulled out of the
rendered page. No Edge and no QQ Browser are involved anywhere in the
download path.

All subprocess failures raise :class:`ScanSciError` so the worker can
``SKIP LOCKED`` past them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from superconductor_db.core.config import Settings, get_settings
from superconductor_db.ingest.docling_parser import parse_pdf_async
from superconductor_db.ingest.direct_fetch import (
    fetch_arxiv_pdf,
    fetch_direct_pdf,
    fetch_iop_pdf,
)
from superconductor_db.ingest.ego_fetch import fetch_doi_pdf

log = logging.getLogger(__name__)

# Strict allowlist for DOI characters: alphanumerics, plus the DOI
# punctuation ``. / : - _``, parentheses, and the SICI separators
# ``+ ; < >``. The parentheses are part of real DOIs -- Elsevier writes
# them into its article ids, e.g. ``10.1016/S1876-3804(23)60415-X`` --
# and Wiley's SICI form uses all three of the separators, e.g.
# ``10.1002/(SICI)1521-3951(199909)215:1<587::AID-PSSB587>3.0.CO;2-P``.
# The subprocess is invoked with an argument list, never through a shell,
# so they carry no quoting risk. Everything else (control chars, shell
# metacharacters, whitespace) is still rejected before the value reaches
# the subprocess.
_SAFE_DOI = re.compile(r"^[0-9A-Za-z./:_\-()+<>;]+$")

# URL allowlist is broader: scheme + path/query characters. We still reject
# whitespace, control chars, and shell metacharacters.
_SAFE_URL = re.compile(r"^[0-9A-Za-z./:_\-?=&%#~+@!,;()]+$")


class ScanSciError(RuntimeError):
    """Raised when the scansci-pdf subprocess exits with a non-zero status
    or returns a payload we can't parse."""


# --------------------------------------------------------------------------- #
# Cache helpers                                                               #
# --------------------------------------------------------------------------- #


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _doi_cache_key(doi: str) -> str:
    """Stable SHA-256 of a DOI -> hex (used as cache filename)."""
    return hashlib.sha256(doi.strip().lower().encode("utf-8")).hexdigest()


def _url_cache_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _cached_pdf_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.pdf"


# scansci-pdf maintains ``.doi_index.json`` in the cache directory, mapping each
# DOI to the file it actually retrieved (which it names ``<doi>_<source>.pdf``
# and may have stored as several source-specific variants). The SHA-256 filename
# this module writes is only used for files it downloaded itself, so without
# reading the index every previously-downloaded paper is treated as a cache miss
# and re-fetched — and re-fetches of publisher DOIs time out behind paywalls.
_DOI_INDEX_NAME = ".doi_index.json"


def _indexed_pdf_path(cache_dir: Path, doi: str) -> Path | None:
    """Return the cached PDF for ``doi`` recorded in the scansci-pdf index.

    Returns ``None`` when the DOI is absent, the index is unreadable, or the
    recorded file no longer exists on disk.
    """
    index_path = cache_dir / _DOI_INDEX_NAME
    if not index_path.exists():
        return None
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = index.get(doi) if isinstance(index, dict) else None
    if isinstance(entry, dict):
        file_name = entry.get("file")
        if not file_name:
            # Older entries stored a bare filename.
            file_name = entry.get("path") or entry.get("name")
    else:
        file_name = entry
    if not file_name:
        return None
    candidate = Path(str(file_name))
    if candidate.exists() and candidate.stat().st_size > 0:
        return candidate
    # The index may store a bare filename relative to the cache directory.
    alt = cache_dir / candidate.name
    if alt.exists() and alt.stat().st_size > 0:
        return alt
    return None


def _sanitize_doi(doi: str) -> str:
    """Strip control chars + reject any character outside the DOI allowlist.

    The sanitised value is safe to pass as a ``--doi`` argument to the
    scansci-pdf subprocess (no shell quoting issues, no path traversal).
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", doi)
    if not _SAFE_DOI.match(cleaned):
        raise ValueError(f"unsafe DOI identifier: {doi!r}")
    # "." and "/" are DOI punctuation, so the allowlist cannot keep them
    # out on its own. A pair of dots is a path traversal, and no real DOI
    # carries one.
    if ".." in cleaned:
        raise ValueError(f"unsafe DOI identifier: {doi!r}")
    return cleaned


def _sanitize_url(url: str) -> str:
    """Strip control chars + reject any character outside the URL allowlist.

    The URL form is broader than the DOI form (allows ``? & = % #`` etc.)
    but still rejects whitespace and shell metacharacters.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", url)
    if not _SAFE_URL.match(cleaned):
        raise ValueError(f"unsafe URL identifier: {url!r}")
    return cleaned


# --------------------------------------------------------------------------- #
# Subprocess invocation                                                       #
# --------------------------------------------------------------------------- #


async def _run_scansci(args: list[str], *, timeout: float = 180.0) -> dict[str, Any]:
    """Run the scansci-pdf CLI with ``args`` and parse its output.

    The CLI's ``get`` command writes ``OK: <absolute-path>`` on success
    (no JSON). Older revisions emitted JSON. We accept both formats.
    """
    settings = get_settings()
    bin_path = settings.scansci_pdf_bin
    if not bin_path:
        raise ScanSciError("scansci_pdf_bin is not configured in settings")

    cmd = [bin_path, *args]
    log.info("scansci-pdf subprocess: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ScanSciError(f"scansci-pdf timed out after {timeout}s") from exc

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise ScanSciError(
            f"scansci-pdf exited {proc.returncode}: {stderr or stdout[:200]}"
        )

    # Try JSON first (older revisions), then fall back to the
    # "OK: <path>" text format used by the current CLI.
    payload = _extract_json(stdout)
    if payload is None:
        payload = _parse_ok(stdout)
    if payload is None:
        raise ScanSciError(
            f"scansci-pdf returned no parseable output. stdout={stdout[:200]} stderr={stderr[:200]}"
        )
    return payload


_OK_PATH_RE = re.compile(r"OK:\s*(?P<path>\S+)", re.IGNORECASE)


def _parse_ok(stdout: str) -> dict[str, Any] | None:
    """Parse the current scansci-pdf ``get`` text output: ``OK: <path> ...``."""
    if not stdout:
        return None
    m = _OK_PATH_RE.search(stdout)
    if not m:
        return None
    return {"path": m.group("path"), "raw": stdout}


def _extract_json(stdout: str) -> dict[str, Any] | None:
    """Find and parse the first JSON object in ``stdout``."""
    if not stdout:
        return None
    # Try whole-string parse first.
    try:
        obj = json.loads(stdout)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fall back to scanning for the first ``{`` and matching ``}``.
    start = stdout.find("{")
    if start == -1:
        return None
    # Use a permissive scan for the matching closing brace.
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stdout)):
        ch = stdout[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = stdout[start : i + 1]
                try:
                    obj = json.loads(chunk)
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


async def download_pdf(
    doi: str,
    *,
    settings: Settings | None = None,
    timeout: float = 180.0,
) -> Path:
    """Download the PDF for ``doi`` via scansci-pdf.

    Returns the local cache path. Reuses an existing cached file when present
    (DOI-keyed SHA-256 in ``settings.pdf_cache_dir``).
    """
    doi = _sanitize_doi(doi)
    settings = settings or get_settings()
    cache_dir = _ensure_dir(Path(settings.pdf_cache_dir))
    key = _doi_cache_key(doi)
    cached_path = _cached_pdf_path(cache_dir, key)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        log.debug("download_pdf: cache hit for doi=%s -> %s", doi, cached_path)
        return cached_path
    # scansci-pdf's own index knows about PDFs retrieved by earlier runs, which
    # it names differently. Check it before hitting the network.
    indexed = _indexed_pdf_path(cache_dir, doi)
    if indexed is not None:
        log.info("download_pdf: index cache hit for doi=%s -> %s", doi, indexed.name)
        return indexed

    # Direct publisher fetch first for the venues that answer a plain request.
    # scansci-pdf routes everything else through Sci-Hub, which sits behind a
    # Cloudflare challenge it cannot clear here; IOP redirects to
    # validate.perfdrive.com and Elsevier/Wiley answer 403. Nature-family
    # papers are served directly and need none of that.
    direct = await asyncio.to_thread(fetch_direct_pdf, doi)
    if direct:
        cached_path.write_bytes(direct)
        return cached_path

    # IOP's article page is behind an hCaptcha, but its PDF endpoint answers a
    # plain request with a Referer. This is the route to Superconducting
    # Science and Technology, which arXiv does not cover.
    iop = await asyncio.to_thread(fetch_iop_pdf, doi)
    if iop:
        cached_path.write_bytes(iop)
        return cached_path

    # Then the arXiv preprint, when the journal paper has one. This covers a
    # good share of Physical Review papers, whose content is identical.
    preprint = await asyncio.to_thread(fetch_arxiv_pdf, doi)
    if preprint:
        cached_path.write_bytes(preprint)
        return cached_path

    # Then ego lite: open the article page in a real browser, read the PDF
    # link out of it and fetch the file with the browser's cookies. This is
    # the channel for publishers that answer a challenge instead of the
    # article, or that render the link client-side.
    if await fetch_doi_pdf(doi, cached_path):
        return cached_path

    payload = await _run_scansci(
        ["get", doi, "--output", str(cache_dir)],
        timeout=timeout,
    )
    path_str = payload.get("path")
    if not path_str:
        # scansci-pdf sometimes returns "file" or "local_path"; fall back.
        path_str = payload.get("file") or payload.get("local_path")
    if not path_str:
        raise ScanSciError(
            f"scansci-pdf response missing 'path' for doi={doi}: {payload}"
        )
    src = Path(str(path_str))
    if not src.exists():
        raise ScanSciError(f"scansci-pdf reported path {src} but file is missing")
    # Move (or copy if cross-device) into the cache.
    if src.resolve() != cached_path.resolve():
        try:
            shutil.move(str(src), str(cached_path))
        except OSError:
            shutil.copy2(str(src), str(cached_path))
            try:
                os.remove(str(src))
            except OSError:
                pass
    return cached_path


async def download_pdf_from_url(
    url: str,
    *,
    settings: Settings | None = None,
    timeout: float = 180.0,
) -> Path:
    """Download the PDF at ``url`` via ``scansci-pdf get <url>``."""
    url = _sanitize_url(url)
    settings = settings or get_settings()
    cache_dir = _ensure_dir(Path(settings.pdf_cache_dir))
    key = _url_cache_key(url)
    cached_path = _cached_pdf_path(cache_dir, key)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        log.debug("download_pdf_from_url: cache hit for url=%s -> %s", url, cached_path)
        return cached_path

    payload = await _run_scansci(
        ["get", url, "--output", str(cache_dir)],
        timeout=timeout,
    )
    path_str = payload.get("path") or payload.get("file") or payload.get("local_path")
    if not path_str:
        raise ScanSciError(
            f"scansci-pdf response missing 'path' for url={url}: {payload}"
        )
    src = Path(str(path_str))
    if not src.exists():
        raise ScanSciError(f"scansci-pdf reported path {src} but file is missing")
    if src.resolve() != cached_path.resolve():
        try:
            shutil.move(str(src), str(cached_path))
        except OSError:
            shutil.copy2(str(src), str(cached_path))
            try:
                os.remove(str(src))
            except OSError:
                pass
    return cached_path


async def parse_pdf_to_markdown(pdf_path: Path) -> str:
    """Convert ``pdf_path`` to a single markdown string using docling.

    docling reconstructs the page layout, so tables arrive as markdown
    tables instead of a flat run of numbers. The result is cached on disk
    (see :mod:`superconductor_db.ingest.docling_parser`), because a
    conversion costs seconds and the loop may revisit the same paper.
    """
    try:
        return await parse_pdf_async(pdf_path)
    except FileNotFoundError as exc:
        raise ScanSciError(str(exc)) from exc


# Re-exported for tests so they can stub subprocesses easily.
__all__ = [
    "ScanSciError",
    "download_pdf",
    "download_pdf_from_url",
    "parse_pdf_to_markdown",
    "_run_scansci",
]


def run_scansci_sync(args: list[str], *, timeout: float = 180.0) -> dict[str, Any]:
    """Synchronous wrapper around :func:`_run_scansci` for legacy callers.

    Used in places where an event loop isn't available. Internally calls the
    subprocess directly without :mod:`asyncio`.
    """
    settings = get_settings()
    bin_path = settings.scansci_pdf_bin
    if not bin_path:
        raise ScanSciError("scansci_pdf_bin is not configured in settings")
    cmd = [bin_path, *args]
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise ScanSciError(
            f"scansci-pdf exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout)[:200]}"
        )
    payload = _extract_json(completed.stdout or "")
    if payload is None:
        raise ScanSciError(
            f"scansci-pdf returned no parseable JSON: {(completed.stderr or '')[:200]}"
        )
    return payload