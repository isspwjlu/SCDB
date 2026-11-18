"""PDF → markdown with docling.

The ingest loop used to flatten PDFs with PyMuPDF, which throws the table
structure away: a critical-temperature table came out as a run of numbers
with no row/column binding, and the extractor -- whose prompt asks it to
"read the entire paper text including every table cell" -- had nothing to
read. docling reconstructs the layout, so tables arrive as markdown tables
and every cell keeps its row header.

docling runs in its own virtualenv (``.venv-docling``) through
``tools/docling_worker.py``: the application venv's base interpreter is an
Anaconda Python and the torch wheels docling needs refuse to load there.
The worker is a batch process, so a run of papers pays the model-loading
cost once.

Two things happen to the markdown on the way out:

* **The reference list is dropped.** Bibliography entries are titles of
  other papers, and those titles carry numbers -- "MgB2 with Tc = 39 K" --
  which is exactly the shape of value the extractor has been caught
  reciting for a material it only mentions. Removing the list removes the
  temptation.
* **The result is cached on disk**, keyed by the PDF's SHA-256, so
  re-running the loop never re-parses a paper it has already read.

The markdown returned here is the corpus the LLM extractor reads *and* the
corpus the evidence gate checks quotes against, so the two can never drift
apart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from superconductor_db.core.config import get_settings

log = logging.getLogger(__name__)

# The virtualenv that holds docling. Resolved next to the backend package
# (backend/.venv-docling) unless the environment says otherwise.
_VENV_DIR_NAME = ".venv-docling"

# Where the docling models are fetched from. huggingface.co is unreachable
# from this network, so the mirror is used by default.
_DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

# Threads per worker. ONNX would otherwise size its pool from the core
# count, and several workers then fight over the same cores. Four workers
# at four threads each is 32 threads on 28 logical cores -- that saturated
# the machine and the desktop became unusable, so the budget is now much
# smaller than the hardware.
_WORKER_THREADS = 3

# Address space a single worker may use. A pathological PDF made one
# worker allocate 228 GB, which starved a 251 GB machine and stalled the
# whole run; at this limit the allocation fails instead and the worker
# reports the file and carries on.
_WORKER_MEMORY_LIMIT = 8 * 1024 ** 3

# Wall-clock budget for one chunk of files. A file that stalls the parser
# for longer than this stops the chunk rather than the run.
_CHUNK_TIMEOUT_S = 1800


def _limit_memory() -> None:
    """Cap the worker's address space (Linux only)."""
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS, (_WORKER_MEMORY_LIMIT, _WORKER_MEMORY_LIMIT)
    )

# A bibliography entry as docling exports it: a list item starting with a
# bracketed number. docling also splits a long entry across several list
# items, so a numbered entry may be followed by unnumbered continuations.
_REFERENCE_ITEM = re.compile(r"^\s*[-*]\s*\[\d+\]\s")
_LIST_ITEM = re.compile(r"^\s*(?:[-*]\s|\d+[.)]\s)")


def _venv_root() -> Path:
    return Path(__file__).resolve().parents[3] / _VENV_DIR_NAME


def _base_python() -> Path | None:
    """The real interpreter behind the virtualenv, if it can be found.

    The virtualenv's ``Scripts/python.exe`` is a 47 kB launcher: it starts
    the actual interpreter as a *child* process, and that child is a
    console program -- so it opens a black window on the desktop no matter
    which flags the launcher itself was given. Calling the base
    interpreter's GUI-subsystem binary (``pythonw.exe``) directly is the
    only way to get a worker that allocates no console at all.
    """
    cfg = _venv_root() / "pyvenv.cfg"
    if not cfg.exists():
        return None
    home = ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("home"):
            home = line.split("=", 1)[1].strip()
            break
    if not home:
        return None
    base = Path(home)
    for name in ("pythonw.exe", "python.exe"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def docling_python() -> Path | None:
    """Return the interpreter to run the docling worker with.

    The GUI-subsystem binary is preferred: a console subsystem worker
    puts a black window on the user's desktop for every paper it
    converts.
    """
    configured = os.environ.get("SCDB_DOCLING_PYTHON", "").strip()
    if configured and Path(configured).exists():
        return Path(configured)

    base = _base_python()
    if base is not None:
        return base

    for relative in (
        Path("Scripts") / "pythonw.exe",
        Path("bin") / "python",
        Path("Scripts") / "python.exe",
    ):
        candidate = _venv_root() / relative
        if candidate.exists():
            return candidate
    return None


def _worker_script() -> Path:
    """Return the path of the batch worker script."""
    return Path(__file__).resolve().parents[3] / "tools" / "docling_worker.py"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_path(pdf_path: Path) -> Path:
    cache_dir = Path(get_settings().markdown_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{_sha256_file(pdf_path)}.md"


def markdown_cache_path(pdf_path: Path) -> Path:
    """Where the markdown of a PDF lives in the cache.

    The name is the hash of the PDF's *content*, not of its DOI, so a
    caller that wants to record the path has to ask for it here.
    """
    return _cache_path(pdf_path)


def strip_references(markdown: str) -> str:
    """Drop the bibliography from ``markdown``.

    docling exports bibliography entries as a list of ``- [n] ...`` items,
    and a long entry continues on the next list item without a number. The
    bibliography is therefore the run of list items that reaches the end of
    the document, and it is only cut when that run holds at least three
    numbered entries -- so a bullet list in the body is left alone.
    """
    lines = markdown.splitlines()
    start: int | None = None
    numbered = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not line.strip():
            continue
        if not _LIST_ITEM.match(line):
            break
        if _REFERENCE_ITEM.match(line):
            numbered += 1
        start = index
    if start is None or numbered < 3:
        return markdown
    return "\n".join(lines[:start]).rstrip() + "\n"


def _worker_env() -> dict[str, str]:
    """Environment for the docling worker.

    ``PYTHONPATH`` points at the docling virtualenv's site-packages: the
    worker runs the *base* interpreter (so that it allocates no console),
    and that is how it finds docling. The Anaconda standard library this
    shell exports must not be on the path -- it breaks the interpreter
    outright ("SRE module mismatch").

    The thread limits matter when several workers run at once. ONNX sizes
    its thread pool from the core count, so four workers each try to use
    every core: a hundred threads fighting over twenty cores, with the
    machine only 22 % busy and every paper taking five times as long as
    it should. Four threads per worker is the share that keeps the cores
    busy without the contention.
    """
    env = dict(os.environ)
    site_packages = _venv_root() / "Lib" / "site-packages"
    if site_packages.exists():
        env["PYTHONPATH"] = str(site_packages)
    else:
        env.pop("PYTHONPATH", None)
    env.setdefault("HF_ENDPOINT", _DEFAULT_HF_ENDPOINT)
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    threads = str(_WORKER_THREADS)
    env.setdefault("OMP_NUM_THREADS", threads)
    env.setdefault("OMP_THREAD_LIMIT", threads)
    env.setdefault("MKL_NUM_THREADS", threads)
    env.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    return env


def _run_worker_sync(jobs: list[tuple[Path, Path]]) -> list[dict]:
    """Convert ``jobs`` with one worker process and return its results."""
    results, _crashed = _run_worker_chunk(jobs)
    return results


def parse_pdf_to_markdown(pdf_path: Path) -> str:
    """Convert ``pdf_path`` to markdown, using the on-disk cache.

    Blocking: the conversion runs in a worker process and takes seconds.
    Use :func:`parse_pdf_async` from async code.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    cache = _cache_path(pdf_path)
    if cache.exists():
        cached = cache.read_text(encoding="utf-8")
        if cached.strip():
            log.debug("docling: cache hit for %s", pdf_path.name)
            return cached

    results = _run_worker_sync([(pdf_path, cache)])
    if not results or not results[0].get("ok"):
        error = results[0].get("error") if results else "no result"
        raise RuntimeError(f"docling failed on {pdf_path.name}: {error}")

    markdown = cache.read_text(encoding="utf-8") if cache.exists() else ""
    if not markdown.strip():
        log.warning("docling: %s produced no text", pdf_path.name)
        return markdown

    trimmed = strip_references(markdown)
    if trimmed != markdown:
        # Cache the trimmed text: the bibliography never comes back.
        cache.write_text(trimmed, encoding="utf-8")
    return trimmed


async def parse_pdf_async(pdf_path: Path) -> str:
    """Convert ``pdf_path`` to markdown without blocking the event loop."""
    return await asyncio.to_thread(parse_pdf_to_markdown, pdf_path)


def _run_worker_chunk(
    jobs: list[tuple[Path, Path]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the docling worker over ``jobs`` and stream its results.

    Returns ``(results, crashed_on)``. ``crashed_on`` is the PDF the worker
    died on, if it died: a native crash in a parser takes the process with
    it, and the caller has to skip that file to make progress.

    The worker's stdout is read line by line rather than captured whole:
    a run over the whole cache lasts hours, and the progress has to be
    visible while it happens.
    """
    python = docling_python()
    if python is None:
        raise RuntimeError(
            "docling virtualenv not found. Create it with "
            "'uv venv --python 3.12 backend/.venv-docling' and install "
            "'docling-slim[standard]' into it, or set SCDB_DOCLING_PYTHON."
        )
    script = _worker_script()
    if not script.exists():
        raise RuntimeError(f"docling worker script missing: {script}")

    payload = "".join(
        json.dumps({"pdf": str(pdf), "out": str(out)}) + "\n" for pdf, out in jobs
    )
    proc = subprocess.Popen(
        [str(python), str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # The worker inherits stderr so its tracebacks land in the run log
        # instead of filling a pipe nobody reads.
        stderr=None,
        env=_worker_env(),
        text=True,
        encoding="utf-8",
        bufsize=1,
        # No console at all, and its own process group. CREATE_NO_WINDOW
        # would only hide the window and still leave a conhost per
        # worker; DETACHED_PROCESS gives the worker no console at all,
        # so nothing can appear on the desktop. The own process group
        # keeps it out of the launching shell's console, which is what
        # killed a whole backfill once with STATUS_CONTROL_C_EXIT.
        creationflags=(
            (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            if sys.platform == "win32"
            else 0
        ),
        # A single pathological PDF can make the parser allocate without
        # bound: one worker reached 228 GB and stalled the whole run on a
        # 251 GB machine. The address-space limit turns that into a
        # MemoryError inside the worker, which it reports per file and
        # carries on from.
        preexec_fn=_limit_memory if sys.platform != "win32" else None,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(payload)
    proc.stdin.close()

    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + _CHUNK_TIMEOUT_S

    # The worker's output is read by a thread and handed over through a
    # queue, because reading the pipe directly blocks: a worker that stops
    # writing but keeps the pipe open would sit in that read forever, and
    # the deadline check below -- which is the only thing that ends a
    # pathological file -- would never be reached. One such worker stalled
    # the parser for nine hours.
    lines: queue.Queue[str | None] = queue.Queue()

    def _read_stdout() -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                lines.put(raw)
        finally:
            lines.put(None)

    threading.Thread(target=_read_stdout, daemon=True).start()

    while True:
        try:
            raw = lines.get(timeout=1.0)
        except queue.Empty:
            if time.monotonic() > deadline:
                # A file that takes longer than this is pathological. Stop
                # the worker and keep what it managed to convert.
                log.error(
                    "backfill: chunk exceeded %.0f s after %d files; stopping the worker",
                    _CHUNK_TIMEOUT_S, len(results),
                )
                proc.kill()
                break
            continue
        if raw is None:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            result = json.loads(line)
        except ValueError:
            log.warning("backfill: unparseable worker line: %s", line[:200])
            continue
        results.append(result)
        log.info(
            "backfill %s %s",
            "ok" if result.get("ok") else "FAIL",
            Path(str(result.get("pdf", ""))).name,
        )
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    crashed_on: str | None = None
    if len(results) < len(jobs) and proc.returncode != 0:
        # The worker stopped early: the file it never reported on is the
        # one that took it down.
        reported = {str(r.get("pdf")) for r in results}
        for pdf, _out in jobs:
            if str(pdf) not in reported:
                crashed_on = str(pdf)
                break
        log.error(
            "backfill: worker died (exit %s) on %s after %d/%d files",
            proc.returncode,
            Path(crashed_on).name if crashed_on else "?",
            len(results),
            len(jobs),
        )
    return results, crashed_on


def _hash_index_path() -> Path:
    """Path of the pdf-path -> content-hash index."""
    return Path(get_settings().markdown_cache_dir) / "pdf_hashes.json"


def _load_hash_index() -> dict[str, str]:
    """Load the pdf-path -> content-hash index, or an empty one.

    Hashing the cache means reading 40 GB, which takes twenty minutes at
    the disk's speed -- pure dead time before the first conversion. The
    index makes a restart skip it.
    """
    path = _hash_index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def markdown_ready_dois(dois: Iterable[str]) -> set[str]:
    """The subset of ``dois`` whose paper has already been parsed.

    The two caches are keyed differently: the PDFs are named after the hash
    of their DOI, while the markdown is named after the hash of the PDF's
    *content*. This bridges the two through the hash index the backfill
    keeps, so a caller can ask about a DOI without hashing 37 GB of PDFs.

    A DOI with no entry in the index has not been seen by the backfill yet,
    so its markdown does not exist either. The whole set is answered in one
    pass: the markdown directory is listed once and the index is read once.
    """
    index = _load_hash_index()
    cache_dir = Path(get_settings().markdown_cache_dir)
    parsed = {path.stem for path in cache_dir.glob("*.md")}
    ready: set[str] = set()
    for doi in dois:
        key = doi.strip()
        digest = hashlib.sha256(key.lower().encode("utf-8")).hexdigest()
        content_hash = index.get(f"{digest}.pdf")
        if content_hash and content_hash in parsed:
            ready.add(key)
    return ready


def _save_hash_index(index: dict[str, str]) -> None:
    """Write the hash index atomically."""
    path = _hash_index_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not write the hash index: %s", exc)


def _backfill(
    pdf_dir: Path,
    limit: int | None = None,
    chunk_size: int = 40,
    workers: int = 4,
) -> tuple[int, int]:
    """Convert every PDF under ``pdf_dir`` that is not cached yet.

    Returns ``(converted, skipped)``.

    The work is done in chunks, one worker process each, and the chunks
    are spread over several worker processes. docling uses about four
    cores per worker, so one worker leaves most of the machine idle; four
    of them turn a two-and-a-half-day run into about sixteen hours. Each
    worker needs roughly 1.7 GB.

    A chunk is small enough that losing it to a crash costs minutes rather
    than hours, and a file that takes the worker down is skipped so the
    run keeps going.
    """
    pdfs = sorted(pdf_dir.rglob("*.pdf"))
    if limit is not None:
        pdfs = pdfs[:limit]

    jobs: list[tuple[Path, Path]] = []
    skipped = 0
    seen_hashes: set[str] = set()
    hash_index = _load_hash_index()
    known = 0
    for pdf in pdfs:
        # Keyed by file name, not by path: the index has to survive the
        # corpus moving to another machine, where the directory differs.
        content_hash = hash_index.get(pdf.name) or hash_index.get(str(pdf))
        if content_hash is None:
            content_hash = _sha256_file(pdf)
            hash_index[pdf.name] = content_hash
            hash_index[str(pdf)] = content_hash
        else:
            known += 1
        cache = Path(get_settings().markdown_cache_dir) / f"{content_hash}.md"
        if cache.exists() and cache.read_text(encoding="utf-8").strip():
            skipped += 1
            continue
        # The same paper is cached under several names (one per download
        # source). The cache is keyed by content, so convert it once.
        if content_hash in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(content_hash)
        jobs.append((pdf, cache))
    if known:
        log.info("backfill: %d files hashed from the index, %d read from disk", known, len(pdfs) - known)
    _save_hash_index(hash_index)

    if not jobs:
        log.info("backfill: nothing to do (%d already cached)", skipped)
        return 0, skipped

    log.info(
        "backfill: %d files to convert, %d already cached, %d workers",
        len(jobs),
        skipped,
        workers,
    )

    def run_shard(shard: list[tuple[Path, Path]]) -> int:
        converted = 0
        for start in range(0, len(shard), chunk_size):
            chunk = shard[start : start + chunk_size]
            try:
                results, crashed_on = _run_worker_chunk(chunk)
            except Exception as exc:  # noqa: BLE001 - one chunk must not end the run
                log.error("backfill: chunk failed: %s", exc)
                continue
            for result in results:
                if not result.get("ok"):
                    log.warning(
                        "backfill failed for %s: %s",
                        result.get("pdf"),
                        result.get("error"),
                    )
                    continue
                converted += 1
                out = Path(str(result.get("out", "")))
                try:
                    markdown = out.read_text(encoding="utf-8")
                except OSError:
                    continue
                trimmed = strip_references(markdown)
                if trimmed != markdown:
                    out.write_text(trimmed, encoding="utf-8")
            if crashed_on:
                log.warning("backfill: skipping %s", Path(crashed_on).name)
        return converted

    shards = [jobs[index::workers] for index in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        converted = sum(pool.map(run_shard, shards))
    log.info("backfill done: %d converted, %d already cached", converted, skipped)
    return converted, skipped


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse

    parser = argparse.ArgumentParser(description="Convert PDFs to markdown with docling")
    parser.add_argument(
        "pdf_dir",
        nargs="?",
        default=get_settings().pdf_cache_dir,
        help="directory of PDFs to convert (default: the PDF cache)",
    )
    parser.add_argument("--limit", type=int, default=None, help="convert at most N files")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="worker processes in parallel (each uses about 4 cores and 1.7 GB)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _done, _skipped = _backfill(
        Path(args.pdf_dir), args.limit, workers=max(1, args.workers)
    )
    sys.exit(0)
