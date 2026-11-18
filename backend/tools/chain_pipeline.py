"""Run the corpus pipeline in order: parse first, then extract.

Phase 1 converts every cached PDF to markdown with docling. Phase 2 walks
the DOI queue and extracts records with the LLM.

**The phases are strictly ordered.** Phase 2 does not start until phase 1
has converted every PDF it can; there is no overlap, because a paper that
is parsed mid-flight would be extracted from a half-written markdown, and
because the two phases together saturate the machine.

The script restarts whichever phase died, so a crash or a reboot costs the
current chunk and not the run.

It is meant to be started by a scheduled task, which is what makes it
survive a session ending; it must therefore not need a console of its
own, and the interpreters it spawns are the GUI-subsystem ones.

Usage::

    python tools/chain_pipeline.py [--workers 3] [--concurrency 8]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND / "src", BACKEND / ".venv" / "Lib" / "site-packages"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

LOG_DIR = BACKEND / "data" / "cache"
BACKFILL_LOG = LOG_DIR / "docling_backfill.log"
EXTRACT_LOG = LOG_DIR / "extraction.log"
CHAIN_LOG = LOG_DIR / "chain_pipeline.log"

# The console-subsystem interpreters of a virtualenv are launchers that
# start the real interpreter as a child, and that child opens a window.
# The base interpreter's GUI-subsystem binary does not.
_NO_WINDOW = getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[chain] {stamp} {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with CHAIN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _base_python(venv: Path) -> Path:
    """The real interpreter behind a virtualenv, GUI-subsystem on Windows."""
    cfg = venv / "pyvenv.cfg"
    home = ""
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().lower().startswith("home"):
                home = line.split("=", 1)[1].strip()
                break
    if home:
        for name in ("pythonw.exe", "python.exe"):
            candidate = Path(home) / name
            if candidate.exists():
                return candidate
    for name in ("pythonw.exe", "python.exe"):
        candidate = venv / "Scripts" / name
        if candidate.exists():
            return candidate
    return venv / "Scripts" / "python.exe"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [
        str(BACKEND / "src"),
        str(BACKEND / ".venv" / "Lib" / "site-packages"),
    ]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_until_done(
    name: str,
    cmd: list[str],
    log_path: Path,
    env: dict[str, str],
    *,
    interval: int = 30,
) -> None:
    """Run a phase, restarting it while it fails."""
    while True:
        with log_path.open("a", encoding="utf-8") as out:
            code = subprocess.call(
                cmd, cwd=str(BACKEND), stdout=out, stderr=out,
                env=env, creationflags=_NO_WINDOW,
            )
        if code == 0:
            log(f"{name} finished cleanly")
            return
        log(f"{name} exited with {code}; restarting in {interval}s")
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    app_python = _base_python(BACKEND / ".venv")
    env = _child_env()

    # --- phase 1: parse every cached PDF -------------------------------- #
    backfill_cmd = [
        str(app_python), "-u", "-m",
        "superconductor_db.ingest.docling_parser",
        str(BACKEND / "data" / "cache" / "pdfs"),
        "--workers", str(args.workers),
    ]
    log(f"phase 1: docling backfill, {args.workers} workers")
    _run_until_done("docling backfill", backfill_cmd, BACKFILL_LOG, env,
                    interval=args.interval)

    # --- phase 2: only now does the model see anything ------------------- #
    extract_cmd = [
        str(app_python), "-u", str(BACKEND / "tools" / "run_extraction.py"),
        "--concurrency", str(args.concurrency),
    ]
    log(f"phase 2: LLM extraction, concurrency {args.concurrency}")
    _run_until_done("extraction", extract_cmd, EXTRACT_LOG, env,
                    interval=args.interval)
    log("pipeline complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
