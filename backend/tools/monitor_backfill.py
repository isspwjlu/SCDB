"""Watch the docling backfill and record how it is doing.

Appends one line to ``data/cache/docling_backfill.progress.log`` every
interval, and raises a desktop notification when the run ends -- finished
or dead -- so the outcome is visible without watching the terminal.

Usage::

    python tools/monitor_backfill.py [--interval 1800]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LOG = BACKEND / "data" / "cache" / "docling_backfill.log"
PROGRESS = BACKEND / "data" / "cache" / "docling_backfill.progress.log"
MARKDOWN = BACKEND / "data" / "cache" / "markdown"


def count_in_log(needle: str) -> int:
    if not LOG.exists():
        return 0
    total = 0
    with LOG.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if needle in line:
                total += 1
    return total


def markdown_count() -> int:
    return len(list(MARKDOWN.glob("*.md")))


def backfill_running() -> bool:
    """True while a docling worker process is alive."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object {$_.CommandLine -match 'docling_parser'}).Count"],
            capture_output=True, text=True, timeout=30, check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
        return int(out.stdout.strip() or 0) > 0
    except Exception:
        return True  # cannot tell: assume still running


def notify(title: str, message: str) -> None:
    """Raise a desktop notification (best effort)."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = '%s';"
        "$n.BalloonTipText = '%s';"
        "$n.Visible = $true; $n.ShowBalloonTip(20000); Start-Sleep 20; $n.Dispose()"
    ) % (title, message.replace("'", ""))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # No black console window for the notification helper.
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
    except Exception:
        pass


def snapshot() -> str:
    ok = count_in_log("backfill ok")
    fail = count_in_log("backfill FAIL")
    md = markdown_count()
    return f"ok={ok} fail={fail} markdown={md}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=1800, help="seconds between checks")
    args = parser.parse_args()

    start_ok = count_in_log("backfill ok")
    last = time.time()
    notified = False
    while True:
        time.sleep(min(60, args.interval))
        ok = count_in_log("backfill ok")
        md = markdown_count()
        running = backfill_running()
        elapsed = max(1.0, time.time() - last)
        rate = (ok - start_ok) / elapsed * 3600 if ok > start_ok else 0.0
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"{stamp} running={running} ok={ok} fail={count_in_log('backfill FAIL')} "
            f"markdown={md} rate={rate:.0f}/h"
        )
        with PROGRESS.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)
        last = time.time()
        if not running and not notified:
            notified = True
            final = f"docling backfill ended: {snapshot()}"
            with PROGRESS.open("a", encoding="utf-8") as handle:
                handle.write(stamp + " " + final + "\n")
            notify("docling backfill", final)
            return 0
        if ok and (ok - start_ok) % 500 < 5:
            notify(
                "docling backfill",
                f"{ok} papers converted, {md} markdown files cached",
            )


if __name__ == "__main__":
    sys.exit(main())
