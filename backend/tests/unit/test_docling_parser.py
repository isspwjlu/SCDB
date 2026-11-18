"""Unit tests for the docling PDF parser.

docling itself is never loaded here: the worker is stubbed, so the tests
cover the caching contract, the bibliography stripper and the error
handling around them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from superconductor_db.ingest import docling_parser
from superconductor_db.ingest.docling_parser import (
    _sha256_file,
    parse_pdf_to_markdown,
    strip_references,
)


@pytest.mark.unit
def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    import hashlib

    target = tmp_path / "paper.pdf"
    target.write_bytes(b"%PDF-1.7 fake")
    assert _sha256_file(target) == hashlib.sha256(b"%PDF-1.7 fake").hexdigest()


@pytest.mark.unit
def test_parse_uses_cache_without_converting(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    cache = tmp_path / "cached.md"
    cache.write_text("| material | Tc |\n|---|---|\n", encoding="utf-8")

    monkeypatch.setattr(docling_parser, "_cache_path", lambda _p: cache)

    def _boom(_jobs):
        raise AssertionError("should not convert on a cache hit")

    monkeypatch.setattr(docling_parser, "_run_worker_sync", _boom)

    assert parse_pdf_to_markdown(pdf) == "| material | Tc |\n|---|---|\n"


@pytest.mark.unit
def test_parse_converts_and_caches(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    cache = tmp_path / "cached.md"

    monkeypatch.setattr(docling_parser, "_cache_path", lambda _p: cache)

    def _worker(jobs):
        jobs[0][1].write_text("| material | Tc |\n|---|---|\n", encoding="utf-8")
        return [{"pdf": str(pdf), "out": str(cache), "ok": True, "chars": 28}]

    monkeypatch.setattr(docling_parser, "_run_worker_sync", _worker)

    markdown = parse_pdf_to_markdown(pdf)
    assert "material" in markdown
    assert cache.read_text(encoding="utf-8") == markdown


@pytest.mark.unit
def test_parse_raises_when_worker_reports_failure(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    cache = tmp_path / "cached.md"

    monkeypatch.setattr(docling_parser, "_cache_path", lambda _p: cache)
    monkeypatch.setattr(
        docling_parser,
        "_run_worker_sync",
        lambda _jobs: [{"pdf": str(pdf), "ok": False, "error": "boom"}],
    )

    with pytest.raises(RuntimeError, match="boom"):
        parse_pdf_to_markdown(pdf)


@pytest.mark.unit
def test_parse_raises_for_missing_pdf(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_pdf_to_markdown(tmp_path / "nope.pdf")


@pytest.mark.unit
def test_strip_references_removes_a_bibliography_run() -> None:
    markdown = (
        "## Results\n\nThe sample superconducts at 12 K.\n\n"
        "- [1] A. Author, Title with Tc = 39 K, Phys. Rev. B 1, 1 (2020).\n"
        "- [2] B. Author, Another title, Nature 1, 1 (2021).\n"
        "- [3] C. Author, Third title, Science 1, 1 (2022).\n"
    )
    stripped = strip_references(markdown)
    assert "The sample superconducts at 12 K." in stripped
    assert "39 K" not in stripped


@pytest.mark.unit
def test_strip_references_keeps_a_short_numbered_list() -> None:
    markdown = "## Methods\n\n- [1] first item\n- [2] second item\n"
    assert strip_references(markdown) == markdown


@pytest.mark.unit
def test_strip_references_keeps_text_after_the_list() -> None:
    markdown = (
        "- [1] one\n- [2] two\n- [3] three\n\n## Appendix\n\nmore text\n"
    )
    assert strip_references(markdown) == markdown


@pytest.mark.unit
def test_strip_references_handles_wrapped_entries() -> None:
    # docling splits a long reference across several list items, so the
    # numbered items are not consecutive.
    markdown = (
        "## Results\n\nThe sample superconducts at 12 K.\n\n"
        "- [39] A. Author, A very long title that docling wrapped onto the\n"
        "- next line of the same entry, Phys. Rev. B 1, 1 (2020).\n"
        "- [40] B. Author, Another title, Nature 1, 1 (2021).\n"
        "- continued text of entry 40\n"
        "- [41] C. Author, Third title, Science 1, 1 (2022).\n"
    )
    stripped = strip_references(markdown)
    assert "The sample superconducts at 12 K." in stripped
    assert "Phys. Rev. B 1, 1 (2020)." not in stripped


@pytest.mark.unit
def test_strip_references_leaves_a_document_without_bibliography() -> None:
    markdown = "## Abstract\n\nNo references here.\n"
    assert strip_references(markdown) == markdown


@pytest.mark.unit
def test_chunk_stops_a_worker_that_stops_writing(tmp_path: Path, monkeypatch) -> None:
    """A silent worker must not hold the chunk open forever.

    Reading the worker's pipe directly blocks until it writes something,
    and the deadline that ends a pathological file is only checked after a
    line arrives. A worker that stopped writing therefore stalled the
    parser for nine hours: the check never ran.
    """
    import sys

    script = tmp_path / "silent_worker.py"
    script.write_text(
        "import sys, time\nsys.stdin.read()\ntime.sleep(600)\n", encoding="utf-8"
    )
    monkeypatch.setattr(docling_parser, "_worker_script", lambda: script)
    monkeypatch.setattr(docling_parser, "docling_python", lambda: Path(sys.executable))
    monkeypatch.setattr(docling_parser, "_CHUNK_TIMEOUT_S", 2.0)

    pdf = tmp_path / "stuck.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    out = tmp_path / "stuck.md"

    results, _crashed_on = docling_parser._run_worker_chunk([(pdf, out)])
    assert results == []
