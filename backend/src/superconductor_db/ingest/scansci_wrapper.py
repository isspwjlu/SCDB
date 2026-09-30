"""scansci-pdf subprocess wrapper (spec §6.4).

scansci-pdf is the project-wide MCP for paper retrieval. We invoke it via
``asyncio.create_subprocess_exec`` so the heavy lifting (WebVPN, CARSI, Tor,
13+ source racing) stays in one place. The wrapper:

- Subprocesses ``scansci-pdf smart_download --doi <doi> --output-dir <dir>``
  (or ``--url <url>``).
- Caches the resulting PDF in ``settings.pdf_cache_dir`` keyed by SHA-256 of
  the DOI / URL. Cache hit returns immediately without re-running the CLI.
- Parses the JSON ``{path: ...}`` payload from stdout.
- Exposes :func:`parse_pdf_to_markdown` which uses :mod:`pymupdf` to convert
  a downloaded PDF into a single markdown string (sufficient for the LLM
  extractor to ingest).

All subprocess failures raise :class:`ScanSciError` so the worker can
``SKIP LOCKED`` past them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from superconductor_db.core.config import Settings, get_settings

log = logging.getLogger(__name__)


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


# --------------------------------------------------------------------------- #
# Subprocess invocation                                                       #
# --------------------------------------------------------------------------- #


async def _run_scansci(args: list[str], *, timeout: float = 180.0) -> dict[str, Any]:
    """Run the scansci-pdf CLI with ``args`` and parse its JSON stdout.

    Returns the parsed JSON dict. Raises :class:`ScanSciError` on failure.
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

    # scansci-pdf emits one JSON object on stdout. Try to extract it even if
    # the tool wrote some leading/trailing lines (be liberal).
    payload = _extract_json(stdout)
    if payload is None:
        raise ScanSciError(
            f"scansci-pdf returned no parseable JSON. stderr={stderr[:200]}"
        )
    return payload


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
    settings = settings or get_settings()
    cache_dir = _ensure_dir(Path(settings.pdf_cache_dir))
    key = _doi_cache_key(doi)
    cached_path = _cached_pdf_path(cache_dir, key)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        log.debug("download_pdf: cache hit for doi=%s -> %s", doi, cached_path)
        return cached_path

    payload = await _run_scansci(
        ["smart_download", "--doi", doi, "--output-dir", str(cache_dir)],
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
    """Download the PDF at ``url`` via scansci-pdf (``--url`` flag)."""
    settings = settings or get_settings()
    cache_dir = _ensure_dir(Path(settings.pdf_cache_dir))
    key = _url_cache_key(url)
    cached_path = _cached_pdf_path(cache_dir, key)
    if cached_path.exists() and cached_path.stat().st_size > 0:
        log.debug("download_pdf_from_url: cache hit for url=%s -> %s", url, cached_path)
        return cached_path

    payload = await _run_scansci(
        ["smart_download", "--url", url, "--output-dir", str(cache_dir)],
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
    """Convert ``pdf_path`` to a single markdown string using PyMuPDF.

    Each page is rendered as a heading + paragraphs joined by blank lines.
    We keep page boundaries so the LLM can reference "Page 4" if needed.
    """
    try:
        import fitz  # type: ignore[import-not-found]  # PyMuPDF
    except ImportError as exc:
        raise ScanSciError(
            "PyMuPDF is required for PDF parsing; install pymupdf"
        ) from exc

    if not pdf_path.exists():
        raise ScanSciError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    parts: list[str] = []
    try:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            if not text.strip():
                continue
            parts.append(f"\n\n## Page {i}\n\n" + text.strip())
    finally:
        doc.close()
    return "".join(parts).strip()


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