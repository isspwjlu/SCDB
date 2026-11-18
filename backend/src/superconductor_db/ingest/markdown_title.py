"""The paper title from a parsed markdown document.

docling writes the paper's title as the document's first heading, but it
is not always the first line: a journal banner (``RESEARCH``, ``ORIGINAL
ARTICLE``) or a figure can precede it, and in a few papers the title never
becomes a heading at all.

This matters because the ingest path never passed a title to
``upsert_paper`` -- every row of the ``papers`` table carries the
placeholder ``(title unavailable) <doi>`` -- while the title was in the
markdown the whole time.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*$")
_NUMBERING_RE = re.compile(r"^(?:[IVXLC]+|\d+)[.)]\s*")

# Headings that name a section or a journal banner rather than the paper.
# The word count and length rules below catch most of them; these are the
# ones that would slip through.
_NON_TITLE_HEADINGS: frozenset[str] = frozenset(
    h.lower()
    for h in (
        "abstract",
        "introduction",
        "contents",
        "references",
        "acknowledgements",
        "acknowledgments",
        "author contributions",
        "conflict of interest",
        "data availability",
        "supplementary",
        "supplementary information",
        "appendix",
        "conclusion",
        "conclusions",
        "discussion",
        "results",
        "methods",
        "highlights",
        "download details",
        "related content",
        "additional information",
        "letters to nature",
        "king's research portal",
        "paper · open access",
        "open access",
        "article open",
    )
)

# A title is a sentence, not a label. These bounds keep the banners and
# the page furniture out.
_MIN_CHARS = 20
_MAX_CHARS = 400
_MIN_WORDS = 3

# A title does not run to a dozen comma-separated names, which is what an
# author line looks like once the affiliations have been dropped.
_MAX_COMMAS = 2


def _normalise(text: str) -> str:
    """Strip the emphasis, the section numbering and the trailing colon."""
    cleaned = text.strip().strip("*_`").strip()
    cleaned = _NUMBERING_RE.sub("", cleaned)
    return cleaned.rstrip(":.").strip()


def _is_title_like(text: str) -> bool:
    """True when a line reads like a paper title."""
    cleaned = _normalise(text)
    if not cleaned or len(cleaned) > _MAX_CHARS:
        return False
    if cleaned.lower() in _NON_TITLE_HEADINGS:
        return False
    if len(cleaned) < _MIN_CHARS:
        return False
    if len(cleaned.split()) < _MIN_WORDS:
        return False
    return True


def _is_date_line(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith(
        ("received", "accepted", "published", "submitted", "copyright")
    )


def title_from_markdown(md: str) -> str | None:
    """The paper's title, or ``None`` when the document does not carry one."""
    lines = md.splitlines()
    seen_heading = False

    for line in lines[:120]:
        match = _HEADING_RE.match(line)
        if match:
            seen_heading = True
            if _is_title_like(match.group(2)):
                return _normalise(match.group(2))
            continue
        if seen_heading:
            # The document is structured, so the title was meant to be a
            # heading. Nothing above qualified, and the body text below
            # must not be mistaken for it.
            continue
        stripped = line.strip()
        if not stripped or _COMMENT_RE.match(stripped):
            continue
        # The title can be a plain line -- a few papers carry it that way
        # -- but only the first line of the document is ever one.
        if _is_date_line(stripped) or stripped.startswith(("http", "www.")):
            return None
        if stripped.count(",") > _MAX_COMMAS:
            # An author line, not a title.
            return None
        if _is_title_like(stripped):
            return _normalise(stripped)
        # Anything else -- an affiliation, a running head -- is not the
        # title, but the heading further down may still be.
    return None


__all__ = ["title_from_markdown", "markdown_path_for_doi", "load_hash_index"]


def load_hash_index(markdown_dir: Path) -> dict[str, str]:
    """The index that bridges the PDF cache to the markdown cache.

    The PDFs are named after the hash of their DOI and the markdown after
    the hash of the PDF's *content*, so the two namespaces only meet
    through this index.
    """
    import json

    path = Path(markdown_dir) / "pdf_hashes.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def markdown_path_for_doi(
    doi: str,
    *,
    markdown_dir: Path,
    hash_index: dict[str, str],
) -> Path | None:
    """The markdown file of a paper, or ``None`` when it has none."""
    digest = hashlib.sha256(doi.strip().lower().encode("utf-8")).hexdigest()
    content_hash = hash_index.get(f"{digest}.pdf")
    if not content_hash:
        return None
    path = Path(markdown_dir) / f"{content_hash}.md"
    return path if path.exists() else None
