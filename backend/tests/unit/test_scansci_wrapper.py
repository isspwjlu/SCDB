"""Unit tests for the scansci-pdf wrapper (Task 27).

We patch ``asyncio.create_subprocess_exec`` so the test doesn't need the real
CLI binary installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superconductor_db.core.config import Settings
from superconductor_db.ingest.scansci_wrapper import (
    ScanSciError,
    _extract_json,
    _sanitize_doi,
    download_pdf,
    download_pdf_from_url,
)


def _settings(tmp_dir: Path) -> Settings:
    return Settings(
        pdf_cache_dir=str(tmp_dir),
        markdown_cache_dir=str(tmp_dir / "md"),
        scansci_pdf_bin="scansci-pdf",
        llm_api_key="k",
        llm_base_url="http://localhost:11434/v1",
        llm_model="x",
    )


def _make_proc(*, returncode: int, stdout: bytes, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


@pytest.mark.unit
def test_extract_json_whole_string() -> None:
    payload = _extract_json('{"path": "/tmp/a.pdf"}')
    assert payload == {"path": "/tmp/a.pdf"}


@pytest.mark.unit
def test_extract_json_with_surrounding_text() -> None:
    raw = "Download started\n{\"path\": \"/tmp/a.pdf\", \"ok\": true}\nDone."
    assert _extract_json(raw) == {"path": "/tmp/a.pdf", "ok": True}


@pytest.mark.unit
def test_extract_json_returns_none_on_garbage() -> None:
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None


@pytest.mark.unit
async def test_download_pdf_caches_and_returns(tmp_path: Path) -> None:
    """Cache hit: existing file -> no subprocess call."""
    settings = _settings(tmp_path)
    # Pre-populate cache.
    cached = tmp_path / f"{__import__('hashlib').sha256(b'10.1234/test').hexdigest()}.pdf"
    cached.write_bytes(b"%PDF-1.4 stub")

    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec"
    ) as fake_exec:
        path = await download_pdf("10.1234/test", settings=settings)
    assert path == cached
    fake_exec.assert_not_called()


@pytest.mark.unit
async def test_download_pdf_invokes_subprocess_on_cache_miss(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake_pdf_path = tmp_path / "downloaded.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.4 stub")

    payload = json.dumps({"path": str(fake_pdf_path)}).encode("utf-8")
    proc = _make_proc(returncode=0, stdout=payload)
    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        path = await download_pdf("10.5555/NEW", settings=settings)

    # Cached under DOI key.
    expected_key = __import__("hashlib").sha256(b"10.5555/new").hexdigest()
    expected = tmp_path / f"{expected_key}.pdf"
    assert path == expected
    assert expected.exists()


@pytest.mark.unit
async def test_download_pdf_raises_on_nonzero_exit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    proc = _make_proc(returncode=2, stdout=b"", stderr=b"not found")
    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(ScanSciError):
            await download_pdf("10.5555/MISSING", settings=settings)


@pytest.mark.unit
async def test_download_pdf_raises_when_path_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    proc = _make_proc(
        returncode=0,
        stdout=json.dumps({"path": str(tmp_path / "does-not-exist.pdf")}).encode("utf-8"),
    )
    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(ScanSciError):
            await download_pdf("10.5555/X", settings=settings)


@pytest.mark.unit
async def test_download_pdf_from_url_passes_url_as_identifier(tmp_path: Path) -> None:
    """``scansci-pdf get`` takes the identifier positionally, not via ``--url``.

    The CLI signature is ``scansci-pdf get IDENTIFIER --output DIR``; there is
    no ``--url`` flag. This test previously asserted a ``--url`` flag that the
    wrapper never passed and the CLI does not accept.
    """
    settings = _settings(tmp_path)
    fake_pdf = tmp_path / "u.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 stub")
    payload = json.dumps({"path": str(fake_pdf)}).encode("utf-8")
    proc = _make_proc(returncode=0, stdout=payload)

    captured_cmd: dict[str, list[str]] = {}

    async def fake_exec(*args, **kwargs):  # noqa: ANN001
        captured_cmd["args"] = list(args)
        return proc

    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        path = await download_pdf_from_url(
            "https://example.com/paper.pdf", settings=settings
        )

    cmd = captured_cmd["args"]
    assert "get" in cmd
    assert "https://example.com/paper.pdf" in cmd
    assert "--output" in cmd
    assert path.exists()

@pytest.mark.unit
async def test_download_pdf_uses_scansci_index_cache(tmp_path: Path) -> None:
    """A PDF recorded in scansci-pdf's own index must be reused offline.

    scansci-pdf names the files it retrieves ``<doi>_<source>.pdf`` (e.g.
    ``Angst2002_Temperature.pdf``) and records the mapping in
    ``.doi_index.json``. The module's own SHA-256 filename only covers files it
    downloaded itself, so without consulting the index every previously
    downloaded journal paper looked like a cache miss — and re-fetching a
    publisher DOI times out behind the paywall. This is what made journal
    ingestion (PRB/PRL/Nature) unusable while the cache held 700+ of them.
    """
    settings = _settings(tmp_path)
    cache_dir = Path(settings.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cached = cache_dir / "Angst2002_Temperature.pdf"
    cached.write_bytes(b"%PDF-1.4 journal article")
    (cache_dir / ".doi_index.json").write_text(
        json.dumps({"10.1103/PhysRevLett.88.167004": {"file": str(cached)}}),
        encoding="utf-8",
    )

    with patch(
        "superconductor_db.ingest.scansci_wrapper._run_scansci",
        new=AsyncMock(side_effect=AssertionError("must not hit the network")),
    ):
        path = await download_pdf("10.1103/PhysRevLett.88.167004", settings=settings)

    assert path == cached
    assert path.read_bytes().startswith(b"%PDF")


@pytest.mark.unit
async def test_download_pdf_index_handles_stale_entry(tmp_path: Path) -> None:
    """An index entry pointing at a missing file must not be trusted."""
    settings = _settings(tmp_path)
    cache_dir = Path(settings.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ".doi_index.json").write_text(
        json.dumps({"10.1103/PhysRevB.1.1": {"file": str(cache_dir / "gone.pdf")}}),
        encoding="utf-8",
    )
    proc = _make_proc(returncode=1, stdout=b"FAILED: unknown")
    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(ScanSciError):
            await download_pdf("10.1103/PhysRevB.1.1", settings=settings)


@pytest.mark.unit
async def test_download_pdf_index_handles_corrupt_json(tmp_path: Path) -> None:
    """A corrupt index must degrade to a normal download, not crash."""
    settings = _settings(tmp_path)
    cache_dir = Path(settings.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ".doi_index.json").write_text("{not json", encoding="utf-8")
    proc = _make_proc(returncode=1, stdout=b"FAILED: unknown")
    with patch(
        "superconductor_db.ingest.scansci_wrapper.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(ScanSciError):
            await download_pdf("10.1103/PhysRevB.2.2", settings=settings)


@pytest.mark.unit
def test_sanitize_doi_accepts_an_elsevier_doi_with_parentheses() -> None:
    # Elsevier puts parentheses into its article ids, and the ingest loop
    # met one of these as an "unsafe DOI identifier" -- a real paper that
    # could never be extracted.
    assert _sanitize_doi("10.1016/S1876-3804(23)60415-X") == (
        "10.1016/S1876-3804(23)60415-X"
    )


@pytest.mark.unit
def test_sanitize_doi_strips_control_characters() -> None:
    assert _sanitize_doi("10.1038/s41467-019") == "10.1038/s41467-019"


@pytest.mark.unit
@pytest.mark.parametrize(
    "doi",
    [
        "10.1038/s41467 019",      # whitespace
        "10.1038/s41467&019",       # shell metacharacter
        "10.1038/s41467`019",       # shell metacharacter
        "10.1038/s41467|019",       # shell metacharacter
        "10.1038/$(whoami)",
        "../../etc/passwd",
        "",
    ],
)
def test_sanitize_doi_rejects_anything_outside_the_allowlist(doi: str) -> None:
    with pytest.raises(ValueError):
        _sanitize_doi(doi)


@pytest.mark.unit
def test_sanitise_doi_accepts_a_wiley_sici_doi() -> None:
    """Wiley's SICI form uses ``; < >`` and the parentheses. Rejecting it
    made the extraction retry one paper every minute for ever."""
    from superconductor_db.ingest.scansci_wrapper import _sanitize_doi

    doi = "10.1002/(SICI)1521-3951(199909)215:1<587::AID-PSSB587>3.0.CO;2-P"
    assert _sanitize_doi(doi) == doi


@pytest.mark.unit
def test_sanitise_doi_still_rejects_shell_metacharacters() -> None:
    from superconductor_db.ingest.scansci_wrapper import _sanitize_doi

    # Control characters are stripped rather than rejected, so they are not
    # in this list.
    for bad in ("10.1038/x`whoami`", "10.1038/x|y", "10.1038/x y", "10.1038/x&y",
                "10.1038/x$(id)"):
        with pytest.raises(ValueError):
            _sanitize_doi(bad)
