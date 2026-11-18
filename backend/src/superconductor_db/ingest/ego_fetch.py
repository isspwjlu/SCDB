"""Open web pages with ego lite and pull the PDF out of them.

Some publishers answer a challenge instead of the article: a plain HTTP
request gets a Cloudflare or hCaptcha interstitial, and the article page
itself is rendered client-side. Those need a real browser.

This module drives **ego lite** for that, through its ``ego-browser nodejs``
CLI. ego lite is a full Chromium (156) with a real user profile, and the CLI
exposes a small page API -- navigate, evaluate, CDP -- which is enough to
open an article page and read the PDF link out of it. No Edge, no QQ
Browser, and no Playwright: the browser that opens a page is the one the
user actually has open.

Two operational facts shape the code:

* the CLI talks to the ego daemon, so **ego lite must be running**. When it
  is not, every call fails with "Failed to connect to ego_cli bootstrap";
  the caller therefore treats ego as an optional channel, not a hard
  dependency.
* the CLI refuses to run inside Claude Code's agent sandbox. A normal
  Windows process -- the ingest worker -- has no such restriction, so this
  module is only exercised by the worker, never from an agent shell.

The download itself is done with :mod:`httpx` rather than inside the page:
``window.fetch`` is still subject to CORS, so a cross-origin PDF link would
be unreadable from the page. Instead the browser earns its keep by getting
past the challenge, and its cookies are replayed over plain HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from superconductor_db.core.config import pdf_fetch_proxies

log = logging.getLogger(__name__)

# A challenge page served with HTTP 200 must not be mistaken for a paper.
_PDF_MAGIC = b"%PDF"
_MIN_PDF_BYTES = 10_000

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# Where ego lite installs its CLI on Windows. The ingest worker can start
# with a PATH that predates the ego installation, so the well-known location
# is checked as well as ``PATH``.
_EGO_CLI_CANDIDATES: tuple[str, ...] = (
    r"%LOCALAPPDATA%\share\ego\active_version_dir\ego-browser.exe",
    r"%USERPROFILE%\.local\share\ego\active_version_dir\ego-browser.exe",
    r"%USERPROFILE%\.local\bin\ego-browser",
)


def ego_browser_bin() -> Path | None:
    """Return the path to the ``ego-browser`` CLI, or ``None`` if absent."""
    found = shutil.which("ego-browser")
    if found:
        return Path(found)
    for raw in _EGO_CLI_CANDIDATES:
        candidate = Path(os.path.expandvars(raw))
        if candidate.exists():
            return candidate
    return None


# Landing pages for the publishers the ingest loop actually meets. The
# article page is what carries the PDF link, so the templates point at the
# page, not at the file.
#
# ``{tail}`` is the part of the DOI after the prefix, ``{doi}`` the whole
# thing -- publishers differ on which form their URLs take.
_LANDING_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("10.48550/", "https://arxiv.org/abs/{tail}"),
    ("10.1038/", "https://www.nature.com/articles/{tail}"),
    ("10.1007/", "https://link.springer.com/article/{tail}"),
    ("10.1103/", "https://link.aps.org/doi/{doi}"),
    ("10.1088/", "https://iopscience.iop.org/article/{tail}"),
    ("10.1021/", "https://pubs.acs.org/doi/{doi}"),
    ("10.1126/", "https://www.science.org/doi/{doi}"),
)


def landing_urls_for_doi(doi: str) -> list[str]:
    """Return the article pages to try for ``doi``, most specific first.

    ``https://doi.org/<doi>`` closes the list: it redirects to whichever
    publisher hosts the paper, and that publisher's article page nearly
    always carries a PDF link.
    """
    tail = doi.split("/", 1)[-1] if "/" in doi else doi
    urls: list[str] = []
    for prefix, template in _LANDING_TEMPLATES:
        if doi.casefold().startswith(prefix):
            urls.append(template.format(tail=tail, doi=doi))
            break
    urls.append(f"https://doi.org/{doi}")
    return urls


# --------------------------------------------------------------------------- #
# The node script                                                               #
# --------------------------------------------------------------------------- #

# Written to a .mjs file and run with ``ego-browser nodejs <file>``. The script
# receives its inputs inline -- the ego runtime does not inherit the caller's
# environment, so a URL cannot be passed through ``process.env``.
#
# It does three things:
#   1. opens the page and waits for the DOM to settle;
#   2. reads the PDF URL out of the rendered DOM (link, <embed>, or the
#      ``citation_pdf_url`` meta tag that publishers put there for Googlebot);
#   3. hands back the browser's cookies, so the PDF can be fetched over plain
#      HTTP with the clearance a challenge page set.
_EGO_SCRIPT = """
import {{ writeFile }} from "node:fs/promises";

const TARGET = {target};
const RESULT = {result};
// Where the browser writes the PDF it fetches. This has to be a real
// path: the script used to name an undefined DEST, which threw before
// the download started, so the browser never fetched anything and the
// plain HTTP replay was the only route -- and it answers 403.
const DEST = {dest};
// A task space keeps its name, and a space that the user has taken control
// of refuses every command. A fresh name per fetch avoids inheriting one.
const STAMP = {stamp};

let out = {{ ok: false, stage: "start", target: TARGET }};
try {{
  const task = await taskSpace("scdb pdf fetch " + STAMP);
  try {{
    let page = task.page("p1");
    try {{
      await page.url();
    }} catch {{
      page = await task.newPage();
    }}
    // A slow page times the navigation out even though the navigation
    // itself committed and the DOM is usable -- the browser says so in the
    // error. Carry on rather than losing the page.
    try {{
      await page.goto(TARGET);
    }} catch (e) {{
      if (!/timed out/i.test(String(e && e.message ? e.message : e))) throw e;
    }}
    try {{
      await page.waitForLoadState("domcontentloaded");
    }} catch (e) {{
      if (!/timed out/i.test(String(e && e.message ? e.message : e))) throw e;
    }}
    // Wait for the challenge to clear.
    //
    // IOP answers a plain client with an hCaptcha and AIP, Wiley and RSC
    // with a Cloudflare page -- but a real Chromium with a real profile
    // clears them in a few seconds. The wait has to be long enough for
    // that: reading the DOM after a fixed two and a half seconds found
    // no PDF link, because the challenge was still on the page.
    let settled = 0;
    for (let i = 0; i < 12; i++) {{
      await page.waitForTimeout(5000);
      const title = String(await page.title());
      if (!/请稍候|just a moment|attention required|checking your browser|verify you are human|checking/i.test(title)) {{
        settled = (i + 1) * 5;
        break;
      }}
    }}
    out.settledAfter = settled;
    // The PDF link is often injected by client-side script, so give the page
    // a moment to settle before reading the DOM.
    await page.waitForTimeout(1500);
    out.finalUrl = await page.url();
    out.title = await page.title();

    out.links = await page.evaluate(() => {{
      const found = [];
      const push = (u) => {{
        if (typeof u !== "string" || !u) return;
        if (!found.includes(u)) found.push(u);
      }};
      // arXiv links to "/pdf/2408.05021" with no file extension, so the
      // extension alone is not enough to recognise a PDF link.
      const looksPdf = (u) => /\\.pdf(\\?|#|$)/i.test(u) || /\\/pdf(\\/|\\?|#|$)/i.test(u);
      // Publishers that serve the article from a script-built page use
      // their own URL shape: ScienceDirect hides the file behind "pdfft",
      // Wiley behind "pdfdirect", IOP and APS behind "/article/.../pdf".
      const looksPdfish = (u) => looksPdf(u)
        || /\\/pdfft/i.test(u)
        || /\\/pdfdirect/i.test(u)
        || /\\/article\\/[^/]+\\/pdf/i.test(u)
        || /\\/doi\\/pdf/i.test(u)
        || /\\/doi\\/epdf/i.test(u)
        || /\\/doi\\/full/i.test(u);
      document.querySelectorAll("a[href]").forEach((a) => {{
        if (looksPdf(a.href) || looksPdfish(a.href)) push(a.href);
      }});
      document.querySelectorAll("embed[type='application/pdf'], iframe[src]").forEach((e) => {{
        if (looksPdf(e.src) || looksPdfish(e.src)) push(e.src);
      }});
      return found;
    }});

    // Publishers put the article PDF in a meta tag for Googlebot; it is the
    // most reliable link on the page, so it wins over anything scraped.
    out.pdfMeta = await page.evaluate(() => {{
      const meta = document.querySelector("meta[name='citation_pdf_url']");
      return meta ? meta.content : null;
    }});

    // Download the PDF from inside the page when we can. The publisher's
    // PDF endpoint rejects a plain HTTP client with 403 even when the
    // cookies are right, but the browser itself is welcome -- and the
    // article page and its PDF are usually the same origin, so the
    // browser's CORS rules do not stand in the way.
    const pdfUrl = out.pdfMeta || (out.links && out.links[0]) || null;
    out.pdfUrl = pdfUrl;
    if (pdfUrl) {{
      try {{
        const resp = await page.fetch(pdfUrl, {{ saveAs: DEST, timeout: 90000 }});
        out.fetchStatus = resp.status;
        out.fetchOk = !!resp.ok;
      }} catch (e) {{
        out.fetchError = String(e && e.message ? e.message : e);
      }}
    }}

    // When the in-page fetch was refused, let the browser *navigate* to
    // the file. A navigation is not a cross-origin request, so the
    // publisher's CORS policy does not apply, and the browser answers
    // with the file -- usually by redirecting to a signed address that
    // carries no cookie and can be fetched by anything.
    if (!out.fetchOk) {{
      try {{
        await page.goto(pdfUrl);
      }} catch (e) {{
        out.pdfGotoError = String(e && e.message ? e.message : e).slice(0, 160);
      }}
      try {{ await page.waitForTimeout(6000); }} catch (e) {{}}
      try {{
        out.finalPdfUrl = await page.url();
      }} catch (e) {{}}
    }}

    out.cookies = await page.cdp("Network.getAllCookies", {{}});
    out.stage = "ok";
  }} finally {{
    await task.finish({{ keep: [] }});
  }}
}} catch (e) {{
  out.stage = "error";
  out.error = String(e && e.message ? e.message : e);
}}
await writeFile(RESULT, JSON.stringify(out), "utf8");
console.log(JSON.stringify(out));
"""


def _write_script(target_url: str, result_path: Path, dest: Path) -> Path:
    body = _EGO_SCRIPT.format(
        target=json.dumps(target_url),
        result=json.dumps(result_path.as_posix()),
        dest=json.dumps(dest.as_posix()),
        stamp=json.dumps(f"{os.getpid()}-{time.time_ns()}"),
    )
    tmp = Path(tempfile.gettempdir()) / f"scdb_ego_{os.getpid()}_{abs(hash(target_url))}.mjs"
    tmp.write_text(body, encoding="utf-8")
    return tmp


def _fetch_signed_pdf(url: str) -> bytes | None:
    """Fetch a PDF from an address the browser resolved.

    The address carries a token rather than a session, so an ordinary
    client can ask for it -- which is what makes the difference between
    a publisher that refuses a script and one that does not.
    """
    try:
        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            trust_env=False,
            proxy=pdf_fetch_proxies()[0],
            headers={"User-Agent": _UA, "Accept": "application/pdf,*/*;q=0.8"},
        ) as client:
            resp = client.get(url)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.info("ego fetch: the resolved address refused: %s", exc)
        return None
    if resp.status_code != 200 or resp.content[:4] != _PDF_MAGIC:
        log.info(
            "ego fetch: the resolved address answered HTTP %s (%d bytes)",
            resp.status_code,
            len(resp.content),
        )
        return None
    return resp.content


def _cookies_from_result(result: dict[str, Any]) -> dict[str, str]:
    """Turn the CDP cookie jar into a header dict for httpx."""
    jar: dict[str, str] = {}
    raw = result.get("cookies")
    if not isinstance(raw, dict):
        return jar
    for cookie in raw.get("cookies", []) or []:
        if not isinstance(cookie, dict):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if isinstance(name, str) and isinstance(value, str):
            jar[name] = value
    return jar


def _pick_pdf_link(result: dict[str, Any]) -> str | None:
    """Choose the article PDF out of the links found on the page.

    The ``citation_pdf_url`` meta tag wins -- publishers set it to the
    article PDF precisely for machines. Failing that, a scraped link is
    used, skipping supplementary material (``esm``, ``supp``), which is not
    the paper.
    """
    meta = result.get("pdfMeta")
    if isinstance(meta, str) and meta.strip():
        return meta.strip()
    links = result.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, str) and not any(
            marker in link.casefold() for marker in ("/esm", "supp", "suppl")
        ):
            return link
    for link in links:
        if isinstance(link, str):
            return link
    return None


def _spawn_ego(cli: Path, source: str, timeout: float):
    """Run ``ego-browser nodejs`` with the script on stdin.

    The CLI is a console program and it starts a node process of its own.
    A plain CREATE_NO_WINDOW leaves that node child to allocate a console
    of its own -- which is the black window that flashes on the desktop.
    Giving the CLI a console of its own and hiding it fixes the whole
    tree: the children inherit the hidden console instead of making a
    visible one.
    """
    startupinfo = None
    flags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [str(cli), "nodejs"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=startupinfo,
        creationflags=flags,
    )
    try:
        stdout, stderr = proc.communicate(source.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return proc, stdout, stderr


async def _run_ego_script(script: Path, timeout: float) -> bytes:
    """Run the ego script and return its stdout.

    ``ego-browser nodejs`` reads the script from **stdin** -- passing a file
    path as an argument makes node try to evaluate the path as JavaScript.
    """
    cli = ego_browser_bin()
    if cli is None:
        raise FileNotFoundError("ego-browser CLI not found")
    source = script.read_text(encoding="utf-8")
    proc, stdout, stderr = await asyncio.to_thread(_spawn_ego, cli, source, timeout)
    if stderr:
        log.debug("ego-browser stderr: %s", stderr.decode("utf-8", "replace")[:400])
    if proc.returncode != 0:
        raise RuntimeError(
            f"ego-browser exited {proc.returncode}: "
            f"{stderr.decode('utf-8', 'replace')[:300] or stdout.decode('utf-8', 'replace')[:300]}"
        )
    return stdout


async def fetch_pdf_via_ego(
    url: str,
    dest: Path,
    *,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> bool:
    """Open ``url`` in ego lite and save the PDF it points at to ``dest``.

    Returns ``True`` when a real PDF was written. Returns ``False`` for every
    failure -- ego lite not running, a page with no PDF link, a paywall --
    so the caller can fall through to the next channel.
    """
    cli = ego_browser_bin()
    if cli is None:
        log.info("ego fetch: ego-browser CLI not on PATH, skipping")
        return False

    result_path = Path(tempfile.gettempdir()) / f"scdb_ego_result_{os.getpid()}_{abs(hash(url))}.json"
    script = _write_script(url, result_path, dest)
    try:
        try:
            await _run_ego_script(script, timeout)
        except (FileNotFoundError, RuntimeError, asyncio.TimeoutError) as exc:
            log.info("ego fetch: ego-browser failed for %s: %s", url, exc)
            return False

        if not result_path.exists():
            log.info("ego fetch: ego produced no result file for %s", url)
            return False
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.info("ego fetch: unreadable result for %s: %s", url, exc)
            return False
        if not isinstance(result, dict):
            return False
        if result.get("stage") == "error":
            log.info("ego fetch: ego error for %s: %s", url, result.get("error"))
            return False

        link = _pick_pdf_link(result)
        if link is None:
            # The target may itself be the PDF: Chromium renders it in its
            # viewer, so no anchor exists to find.
            final = result.get("finalUrl")
            if isinstance(final, str) and re.search(
                r"(\.pdf|/pdf)(/|\?|#|$)", final, re.I
            ):
                link = final
        if link is None:
            log.info("ego fetch: no PDF link on %s", url)
            return False

        # The browser may already have fetched the PDF into the
        # destination -- it does that when the PDF endpoint answers the
        # browser but rejects a plain HTTP client.
        if dest.exists() and dest.stat().st_size > 0:
            try:
                if dest.read_bytes()[:4] == _PDF_MAGIC:
                    log.info(
                        "ego fetch: %s -> %.0f KB (fetched in the browser)",
                        link, dest.stat().st_size / 1024,
                    )
                    return True
            except OSError:
                pass

        cookies = _cookies_from_result(result)
        headers = {"User-Agent": _UA, "Referer": result.get("finalUrl") or url}

        # The browser may have resolved the file to a signed address while
        # it navigated there: publishers like AIP and RSC redirect to a
        # link that carries a token instead of the file. Such an address
        # needs no cookie and no browser, so it is fetched on its own --
        # and it is the only way to get the bytes out of a publisher
        # whose server sends no CORS header.
        signed = result.get("finalPdfUrl")
        if isinstance(signed, str) and signed.startswith("http"):
            data = _fetch_signed_pdf(signed)
            if data:
                try:
                    dest.write_bytes(data)
                except OSError as exc:
                    log.info("ego fetch: cannot write %s: %s", dest, exc)
                    return False
                log.info(
                    "ego fetch: %s -> %.0f KB (the browser resolved the address)",
                    signed[:120], len(data) / 1024,
                )
                return True

        owns = client is None
        if owns:
            client = httpx.Client(
                headers=headers,
                cookies=cookies,
                follow_redirects=True,
                timeout=60.0,
                # The browser that found the link went through the machine's
                # proxy, so the replay does too: the two have to look like
                # the same client or the publisher serves the challenge
                # page instead of the file.
                trust_env=False,
                proxy=pdf_fetch_proxies()[0],
            )
        try:
            resp = client.get(link)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.info("ego fetch: download of %s failed: %s", link, exc)
            return False
        finally:
            if owns:
                client.close()

        if resp.status_code != 200 or resp.content[:4] != _PDF_MAGIC:
            log.info(
                "ego fetch: %s -> HTTP %s, not a PDF (%d bytes)",
                link,
                resp.status_code,
                len(resp.content),
            )
            return False
        if len(resp.content) < _MIN_PDF_BYTES:
            log.info("ego fetch: %s -> only %d bytes, too small", link, len(resp.content))
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        log.info("ego fetch: %s -> %d KB", link, len(resp.content) // 1024)
        return True
    finally:
        for path in (script, result_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


async def fetch_doi_pdf(
    doi: str,
    dest: Path,
    *,
    timeout: float = 120.0,
) -> bool:
    """Fetch the PDF for ``doi`` by opening its article page in ego lite.

    Tries the publisher-specific landing page first and the DOI resolver
    second. Returns ``False`` when no page yields a PDF, so the caller can
    fall through to the next channel.
    """
    for landing in landing_urls_for_doi(doi):
        if await fetch_pdf_via_ego(landing, dest, timeout=timeout):
            return True
    return False
