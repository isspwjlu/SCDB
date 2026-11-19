"""Look a candidate reference up in Crossref before it goes into the paper.

Prints the registered title, authors, journal and DOI for each query,
so that a new entry can be added to make_bib.py only if it resolves.

Usage::

    python tools/find_refs.py "machine learning assisted discovery of high-Tc hydride superconductors"
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "scdb-paper/1.0 (mailto:openalex-contact@example.org)"}


def query(text: str, rows: int = 4) -> list[dict]:
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query.bibliographic": text, "rows": rows, "select": "DOI,title,author,"
        "container-title,volume,page,issued,type"}
    )
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return payload["message"]["items"]


def main() -> int:
    for text in sys.argv[1:]:
        print(f"\n=== {text} ===")
        try:
            items = query(text)
        except Exception as exc:  # noqa: BLE001
            print(f"  query failed: {exc}")
            continue
        for item in items:
            title = (item.get("title") or ["?"])[0]
            journal = (item.get("container-title") or ["?"])[0]
            year = (item.get("issued", {}).get("date-parts") or [[None]])[0][0]
            authors = item.get("author") or []
            who = ", ".join(
                f"{a.get('family', '?')} {a.get('given', '')[:1]}." for a in authors[:3]
            )
            print(f"  {item.get('DOI')}")
            print(f"    {title}")
            print(
                f"    {journal} {item.get('volume', '')} {item.get('page', '')} "
                f"({year}) [{item.get('type')}] {who}"
            )
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
