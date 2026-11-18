"""Recover the papers that were downloaded but never queued.

The PDF cache is keyed by the hash of the DOI, so a PDF whose queue
entry was lost is an orphan: the file name gives nothing away. Three
routes recover them, in order of how sure they are:

1. **A DOI in the file name.** The old tool named the files it fetched
   after the DOI it fetched them for (``10.1002_adma.202006850.pdf``),
   with the source appended. The DOI is the name, so it only has to be
   put back together.
2. **A DOI in the paper.** The PDF prints its own DOI, and hashing it
   has to reproduce the file name -- which is what makes the recovery
   safe to trust.
3. **The title.** The parsed markdown carries the title, and CrossRef
   knows the DOI of a paper by its title. The hash check applies here
   too.

Usage::

    python tools/recover_orphan_pdfs.py --out recovered.json [--apply]

``--apply`` also puts a copy of each recovered paper under its hash
name, so the extraction finds it by DOI the way it finds every other
paper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND / "src", BACKEND / ".venv" / "Lib" / "site-packages"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

from superconductor_db.ingest.markdown_title import title_from_markdown  # noqa: E402

log = logging.getLogger("recover_orphan_pdfs")

# A DOI as it appears in a file name: the slashes become underscores.
_DOI_IN_NAME = re.compile(r"^(10\.\d{4,9})[_-]([^_]+)", re.IGNORECASE)

# A DOI as it appears in the paper itself.
_DOI_RE = re.compile(r"10\.\d{4,9}/[!-~]{3,60}")

_CROSSREF = "https://api.crossref.org/works"


def _doi_hash(doi: str) -> str:
    return hashlib.sha256(doi.strip().lower().encode("utf-8")).hexdigest()


def _clean_doi(raw: str) -> str:
    """Trim the punctuation and the markup a reference list leaves."""
    doi = raw.split("</")[0].split("&")[0].split("?")[0]
    return doi.rstrip(".,;:)]}>'\"*").strip()


def _doi_from_name(name: str) -> str | None:
    """The DOI a file was named after, when it was named after one."""
    match = _DOI_IN_NAME.match(name)
    if not match:
        return None
    stem, rest = match.group(1), match.group(2)
    # The name keeps the DOI's slashes as underscores, so the two halves
    # have to be joined back: "10.1002_adma.202006850" is
    # "10.1002/adma.202006850".
    if "/" not in stem and "." in rest:
        return f"{stem}/{rest}"
    return None


def _doi_from_bytes(head: str, pdf_hash: str | None) -> str | None:
    """The DOI printed in the paper, when it hashes back to the file."""
    for match in _DOI_RE.finditer(head):
        doi = _clean_doi(match.group(0))
        if not doi.lower().startswith("10."):
            continue
        if pdf_hash and _doi_hash(doi) == pdf_hash:
            return doi
    return None


def _doi_from_title(title: str, client: httpx.Client) -> str | None:
    """The DOI of the paper with this title."""
    try:
        resp = client.get(
            _CROSSREF,
            params={
                "query.bibliographic": title,
                "rows": 3,
                "select": "DOI,title",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        log.warning("crossref lookup failed: %s", exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        items = (resp.json().get("message") or {}).get("items") or []
    except ValueError:
        return None
    wanted = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        names = item.get("title") or []
        got = re.sub(r"[^a-z0-9]+", " ", (names[0] if names else "").lower()).strip()
        if got and (got == wanted or wanted.startswith(got[:60])):
            doi = item.get("DOI")
            if doi:
                return str(doi)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", required=True)
    parser.add_argument("--markdown-dir", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="also copy each recovered paper under its hash name",
    )
    parser.add_argument(
        "--title-lookups", type=int, default=0,
        help="how many orphans to try the CrossRef title route on (0 = none)",
    )
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    markdown_dir = Path(args.markdown_dir)
    queue_path = Path(args.queue)

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = {
        str(item.get("doi") or "").strip().lower()
        for item in queue
        if str(item.get("doi") or "").strip()
    }
    queued_hashes = {_doi_hash(d) for d in queued}

    index_path = markdown_dir / "pdf_hashes.json"
    hash_index: dict[str, str] = {}
    if index_path.exists():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                hash_index = raw
        except (OSError, ValueError):
            hash_index = {}

    orphans = [
        p for p in sorted(pdf_dir.glob("*.pdf"))
        if p.stem not in queued_hashes
    ]
    log.info("%d orphans to recover", len(orphans))

    recovered: list[dict[str, str]] = []
    seen: set[str] = set()
    title_route_used = 0
    client = httpx.Client(
        headers={"User-Agent": "scdb-corpus/1.0 (mailto:noreply@example.org)"},
        timeout=30.0,
    )
    for pdf in orphans:
        doi: str | None = None
        is_hash_named = len(pdf.stem) == 64 and pdf.stem.isalnum()

        # 1. the DOI the file was named after
        named = _doi_from_name(pdf.name)
        if named and named.lower() not in queued:
            doi = named

        # 2. the DOI printed in the paper. Only a file named after the
        # hash of its DOI can be checked this way -- for any other file
        # the DOI in the text may well be one of the references, and a
        # wrong DOI would put the paper under the wrong identifier.
        if doi is None and is_hash_named:
            found = _doi_from_bytes(
                pdf.read_bytes()[:400_000].decode("latin-1"), pdf.stem
            )
            if found is None:
                content_hash = (
                    hash_index.get(pdf.name)
                    or hash_index.get(str(pdf))
                    or hash_index.get(pdf.stem)
                )
                md_path = (
                    markdown_dir / f"{content_hash}.md" if content_hash else None
                )
                if md_path is not None and md_path.exists():
                    try:
                        markdown = md_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        markdown = ""
                    found = _doi_from_bytes(markdown, pdf.stem)
            if found:
                doi = found

        # 3. the title, looked up at CrossRef
        if doi is None and title_route_used < args.title_lookups:
            content_hash = (
                hash_index.get(pdf.name)
                or hash_index.get(str(pdf))
                or hash_index.get(pdf.stem)
            )
            md_path = (
                markdown_dir / f"{content_hash}.md" if content_hash else None
            )
            if md_path is not None and md_path.exists():
                try:
                    markdown = md_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    markdown = ""
                title = title_from_markdown(markdown)
                if title:
                    title_route_used += 1
                    found = _doi_from_title(title, client)
                    if found and (
                        not is_hash_named
                        or _doi_hash(found) == pdf.stem
                    ):
                        doi = found
                    time.sleep(0.4)

        if doi is None or doi.lower() in queued or doi.lower() in seen:
            continue
        seen.add(doi.lower())
        recovered.append({"doi": doi, "pdf": str(pdf)})

    log.info("recovered %d papers", len(recovered))
    Path(args.out).write_text(json.dumps(recovered, indent=1), encoding="utf-8")

    if args.apply:
        copied = 0
        for item in recovered:
            source = Path(item["pdf"])
            target = pdf_dir / f"{_doi_hash(item['doi'])}.pdf"
            if target.exists() or target == source:
                continue
            shutil.copy2(source, target)
            copied += 1
        log.info("%d papers copied under their hash name", copied)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
