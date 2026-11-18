"""Keep the docling backfill running.

The conversion takes about a day, and it has died twice for reasons
outside its control -- a Ctrl+C that reached the worker's process group,
once, and a terminated console session another time. This wrapper restarts
it; the markdown cache means a restart resumes where the last run stopped
instead of starting over.

Usage::

    python tools/supervise_backfill.py [--workers 4] [--interval 60]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PDF_DIR = BACKEND / "data" / "cache" / "pdfs"
LOG = BACKEND / "data" / "cache" / "docling_backfill.log"
SUPERVISOR_LOG = BACKEND / "data" / "cache" / "supervise_backfill.log"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"

# DETACHED_PROCESS: the backfill must not put a black console on the
# user's desktop, and it must not leave a hidden conhost behind either.
# The supervisor itself runs under pythonw (a GUI subsystem interpreter),
# so it has no console at all.
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[supervisor] {stamp} {message}"
    try:
        print(line, flush=True)
    except Exception:
        # pythonw has no stdout. The file below is the real record.
        pass
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def notify(title: str, message: str) -> None:
    """Raise a desktop notification (best effort)."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = '%s';"
        "$n.BalloonTipText = '%s';"
        "$n.Visible = $true; $n.ShowBalloonTip(30000); Start-Sleep 25; $n.Dispose()"
    ) % (title, message.replace("'", ""))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # No black console window for the notification helper.
            creationflags=_DETACHED,
        )
    except Exception:
        pass


def _base_python() -> Path | None:
    """The real interpreter behind the application virtualenv.

    The virtualenv's ``Scripts/python.exe`` is a launcher that starts the
    actual interpreter as a child, and that child is a console program:
    it opens a black window on the desktop no matter which flags the
    launcher itself was given. The base interpreter's GUI-subsystem
    binary allocates no console at all.
    """
    cfg = BACKEND / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        return None
    home = ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("home"):
            home = line.split("=", 1)[1].strip()
            break
    if not home:
        return None
    for name in ("pythonw.exe", "python.exe"):
        candidate = Path(home) / name
        if candidate.exists():
            return candidate
    return None


def _child_env() -> dict[str, str]:
    """Environment for the backfill: the application on the import path."""
    env = dict(os.environ)
    parts = [
        str(BACKEND / "src"),
        str(BACKEND / ".venv" / "Lib" / "site-packages"),
    ]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help=(
            "worker processes in parallel. Each uses about 3 CPU threads, "
            "so 3 workers occupy about 9 of the 20 physical cores."
        ),
    )
    parser.add_argument(
        "--interval", type=int, default=60, help="seconds to wait before a restart"
    )
    args = parser.parse_args()

    python = _base_python() or PYTHON
    cmd = [
        str(python),
        "-u",
        "-m",
        "superconductor_db.ingest.docling_parser",
        str(PDF_DIR),
        "--workers",
        str(args.workers),
    ]
    log(f"starting backfill with {args.workers} workers (python: {python})")
    while True:
        # The backfill's own output goes to the backfill log, so the two
        # logs stay readable on their own.
        with LOG.open("a", encoding="utf-8") as out:
            code = subprocess.call(
                cmd,
                cwd=str(BACKEND),
                stdout=out,
                stderr=out,
                env=_child_env(),
                creationflags=_DETACHED,
            )
        if code == 0:
            log("backfill finished cleanly; stopping")
            notify(
                "docling backfill",
                f"finished: {len(list((BACKEND/'data/cache/markdown').glob('*.md')))} markdown files cached",
            )
            return 0
        log(f"backfill exited with {code}; restarting in {args.interval}s")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
