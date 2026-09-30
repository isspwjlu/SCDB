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
async def test_download_pdf_from_url_uses_url_flag(tmp_path: Path) -> None:
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

    # Ensure the --url flag was used (not --doi).
    cmd = captured_cmd["args"]
    assert "--url" in cmd
    assert "https://example.com/paper.pdf" in cmd
    assert path.exists()