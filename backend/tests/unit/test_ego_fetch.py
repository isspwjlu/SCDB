"""The browser route: clearing the challenge and getting the bytes out.

A publisher that refuses a script still serves the browser, and the
browser's answer to "where is the file" is often a *signed* address --
one that carries a token rather than a session, so an ordinary client
can fetch it. That is the route this file covers.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superconductor_db.ingest import ego_fetch


@pytest.mark.unit
def test_a_signed_address_is_fetched_on_its_own(tmp_path: Path) -> None:
    """The browser resolved the file to an address with a token in it."""
    dest = tmp_path / "paper.pdf"
    payload = {
        "stage": "ok",
        "finalUrl": "https://journals.example/article",
        "pdfMeta": "https://journals.example/article-pdf/123",
        "fetchOk": False,
        "finalPdfUrl": "https://cdn.example/paper.pdf?token=abc",
        "links": [],
    }

    async def fake_script(script: Path, timeout: float) -> bytes:
        # The result is read from the file the script was told to write,
        # so the file has to carry the answer.
        source = script.read_text(encoding="utf-8")
        result_path = Path(json.loads(source.split("const RESULT = ")[1].split(";")[0]))
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return b""

    signed = MagicMock()
    signed.return_value = b"%PDF-1.7" + b"x" * 30_000

    with (
        patch.object(ego_fetch, "_run_ego_script", fake_script),
        patch.object(ego_fetch, "_fetch_signed_pdf", MagicMock(return_value=signed.return_value)) as fetch,
        patch.object(ego_fetch, "ego_browser_bin", MagicMock(return_value=Path("ego"))),
    ):
        ok = asyncio_run(ego_fetch.fetch_pdf_via_ego("https://x.example/a", dest))

    assert ok is True
    assert fetch.called
    assert dest.read_bytes()[:4] == b"%PDF"


@pytest.mark.unit
def test_nothing_is_written_when_the_address_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "paper.pdf"
    payload = {
        "stage": "ok",
        "finalUrl": "https://journals.example/article",
        "pdfMeta": "https://journals.example/article-pdf/123",
        "fetchOk": False,
        "finalPdfUrl": "https://cdn.example/paper.pdf?token=abc",
        "links": [],
    }

    async def fake_script(script: Path, timeout: float) -> bytes:
        source = script.read_text(encoding="utf-8")
        result_path = Path(json.loads(source.split("const RESULT = ")[1].split(";")[0]))
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return b""

    # The signed address is refused, and so is the plain replay.
    refused = MagicMock()
    refused.get = MagicMock(
        return_value=MagicMock(status_code=403, content=b"<html>challenge</html>")
    )
    client = MagicMock()
    client.get = refused.get
    client.close = MagicMock()

    with (
        patch.object(ego_fetch, "_run_ego_script", fake_script),
        patch.object(ego_fetch, "_fetch_signed_pdf", MagicMock(return_value=None)),
        patch.object(ego_fetch, "ego_browser_bin", MagicMock(return_value=Path("ego"))),
        patch.object(ego_fetch.httpx, "Client", MagicMock(return_value=client)),
    ):
        ok = asyncio_run(ego_fetch.fetch_pdf_via_ego("https://x.example/a", dest))

    assert ok is False
    assert not dest.exists()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
