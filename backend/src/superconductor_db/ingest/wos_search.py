"""Web of Science as a discovery source, driven through the ego browser.

OpenAlex covers the field, but its allowance is a dollar a day, and the
searches are what spend it. Web of Science needs no allowance: the
institute's address is entitled, and the browser reaches the interface
without a sign-in. The trade is wall clock -- a search is a browser
session, not a request -- so this is a source that runs once a round
rather than fifteen times.

Two things the interface does that matter:

* the result list is virtual, so the rows are only in the DOM once they
  have been scrolled into view;
* the DOI of a record is not printed on the row. It travels in the
  citation-export link's ``rft_id=info:doi/...`` parameter, which is
  percent-encoded twice in the DOM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WOS_SEARCH_URL = "https://www.webofscience.com/wos/woscc/basic-search"

# The queries that target the journals this database covers. Each is a
# Topic search, so the records are the papers whose text mentions the
# measurement rather than any paper from a journal.
WOS_QUERIES: tuple[str, ...] = (
    "superconducting transition critical temperature",
    "upper critical field anisotropy measurement",
    "energy gap spectroscopy superconducting",
    "specific heat entropy superconductivity",
)

# The ego nodejs runtime, with the script on stdin.
_EGO_SCRIPT = """
import {{ writeFile }} from "node:fs/promises";
const TARGET = {target};
const QUERY = {query};
const RESULT = {result};
const PAGE = {page};
let out = {{ stage: "start", dois: [] }};
try {{
  const task = await taskSpace("scdb wos " + Date.now());
  try {{
    const page = await task.newPage();
    try {{
      await page.goto(TARGET);
    }} catch (e) {{
      if (!/timed out/i.test(String(e && e.message ? e.message : e))) throw e;
    }}
    try {{ await page.waitForLoadState("domcontentloaded"); }} catch (e) {{}}
    await page.waitForTimeout(4000);

    await page.fill('input[name="search-main-box"]', QUERY);
    await page.evaluate(() => {{
      const btns = Array.from(document.querySelectorAll("button"));
      const hit = btns.find(b => {{
        const t = (b.innerText || "").replace(/\\s+/g, " ").trim().toLowerCase();
        return t === "search" || t === "search search";
      }});
      if (hit) hit.click();
    }});
    await page.waitForTimeout(9000);

    if (PAGE > 1) {{
      // The results URL carries the page number:
      // summary/<session>/relevance/<page>. The sort menu is awkward to
      // drive, but the page number is right there in the URL, and walking
      // it gives a different slice of the result list every round. That
      // is what the loop needs: the same head every round would be the
      // same papers.
      const u = await page.url();
      const m = u.match(/^(.*\/relevance\/)\d+$/);
      if (m) {{
        const next = m[1] + PAGE;
        try {{ await page.goto(next); }} catch (e) {{}}
        try {{ await page.waitForLoadState("domcontentloaded"); }} catch (e) {{}}
        await page.waitForTimeout(7000);
        out.urlAfterPage = await page.url();
      }}
    }}

    // The result list is virtual: the rows are rendered only once they
    // have been scrolled into view.
    for (let i = 0; i < 12; i++) {{
      try {{ await page.evaluate(() => window.scrollBy(0, 700)); }} catch (e) {{}}
      await page.waitForTimeout(900);
    }}
    await page.waitForTimeout(2500);

    out.finalUrl = await page.url();
    out.total = await page.evaluate(() => {{
      const m = document.body.innerText.match(/([0-9,]+)\\s+results/i);
      return m ? m[1] : null;
    }});
    out.records = await page.evaluate(() => {{
      const recs = Array.from(document.querySelectorAll("app-record"));
      const out = [];
      for (const r of recs) {{
        const text = (r.innerText || "").replace(/\\s+/g, " ").trim();
        let doi = null;
        for (const a of Array.from(r.querySelectorAll("a"))) {{
          const h = a.getAttribute("href") || "";
          // In the DOM the parameter separators are encoded too:
          // rft_id%3Dinfo%3Adoi%2F10.1103%252FPhysRevB.80.212502
          let dec = h;
          try {{ dec = decodeURIComponent(decodeURIComponent(h)); }} catch (e) {{}}
          const m = dec.match(/rft_id=info:doi\\/([^&]+)/i);
          if (m) {{ doi = decodeURIComponent(m[1].replace(/%25/g, "%")); break; }}
        }}
        if (!doi) {{
          const m = text.match(/10\\.\\d{{4,9}}\\/[^\\s]{{1,60}}/);
          if (m) doi = m[0];
        }}
        out.push({{ doi: doi, head: text.slice(0, 240) }});
      }}
      return out;
    }});
    out.stage = "ok";
  }} finally {{
    await task.finish({{ keep: [] }});
  }}
}} catch (e) {{
  out.stage = "error";
  out.error = String(e && e.message ? e.message : e);
}}
await writeFile(RESULT, JSON.stringify(out), "utf8");
"""


def _ego_cli() -> Path | None:
    from superconductor_db.ingest.ego_fetch import ego_browser_bin

    return ego_browser_bin()


def _run_ego(script: str, timeout: float = 240.0) -> dict[str, Any]:
    """Run one ego script and return the JSON it wrote."""
    from superconductor_db.ingest.ego_fetch import _spawn_ego

    cli = _ego_cli()
    if cli is None:
        log.warning("wos: ego-browser CLI not found")
        return {}
    result = Path(tempfile.gettempdir()) / f"scdb_wos_{os.getpid()}_{time.time_ns()}.json"
    try:
        proc, stdout, stderr = _spawn_ego(cli, script, timeout)
        if proc.returncode != 0:
            log.warning("wos: ego exited %s: %s",
                        proc.returncode, stderr.decode("utf-8", "replace")[:300])
        if not result.exists():
            log.warning("wos: ego wrote no result file")
            return {}
        return json.loads(result.read_text(encoding="utf-8"))
    finally:
        result.unlink(missing_ok=True)


async def search_wos(
    query: str,
    *,
    page: int = 1,
    timeout: float = 240.0,
) -> list[str]:
    """Return the DOIs of one page of a Web of Science search.

    ``page`` walks the result list. The relevance order returns the same
    head every time, so without a page number the loop would keep finding
    the same papers; each round asks for a different slice.
    """
    result = Path(tempfile.gettempdir()) / f"scdb_wos_{os.getpid()}_{time.time_ns()}.json"
    script = _EGO_SCRIPT.format(
        target=json.dumps(WOS_SEARCH_URL),
        query=json.dumps(query),
        result=json.dumps(str(result)),
        page=int(page),
    )
    try:
        from superconductor_db.ingest.ego_fetch import _spawn_ego

        cli = _ego_cli()
        if cli is None:
            log.warning("wos: ego-browser CLI not found")
            return []
        proc, _stdout, stderr = await asyncio.to_thread(
            _spawn_ego, cli, script, timeout
        )
        if proc.returncode != 0:
            log.warning("wos: ego exited %s: %s",
                        proc.returncode, stderr.decode("utf-8", "replace")[:300])
            return []
        if not result.exists():
            log.warning("wos: ego wrote no result file")
            return []
        data = json.loads(result.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a failed search must not stop the loop
        log.warning("wos: search failed for %r: %s", query, exc)
        return []
    finally:
        result.unlink(missing_ok=True)

    if data.get("stage") != "ok":
        log.warning("wos: search for %r failed: %s", query, data.get("error"))
        return []
    records = data.get("records") or []
    dois: list[str] = []
    for rec in records:
        doi = (rec.get("doi") or "").strip()
        if doi and doi.lower().startswith("10.") and doi not in dois:
            dois.append(doi)
    log.info(
        "wos: %r -> %s results, %d records, %d DOIs (sorted=%s)",
        query, data.get("total"), len(records), len(dois), data.get("sorted"),
    )
    return dois
