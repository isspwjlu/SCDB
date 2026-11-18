"""Launch the docling backfill as a truly detached Windows process.

A shell background job (``nohup ... &``) does not survive the agent shell
that started it: the process group is cleaned up when the tool call ends,
and the job dies silently with an empty log. Windows' own detached-process
flag is what makes a job outlive its parent.

Usage::

    python tools/launch_detached.py [--log <path>] <command> [args...]

The child's stdout and stderr go to ``data/cache/docling_backfill.log``
unless ``--log`` says otherwise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
DETACHED = 0x00000008 | 0x00000200


def main(argv: list[str]) -> int:
    log_name = "docling_backfill.log"
    if argv and argv[0] == "--log":
        if len(argv) < 2:
            print("usage: launch_detached.py [--log <path>] <command> [args...]", file=sys.stderr)
            return 2
        log_name = argv[1]
        argv = argv[2:]
    if not argv:
        print("usage: launch_detached.py [--log <path>] <command> [args...]", file=sys.stderr)
        return 2
    backend = Path(__file__).resolve().parents[1]
    log_path = backend / "data" / "cache" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            cwd=str(backend),
            creationflags=DETACHED,
            close_fds=True,
        )
    print(f"launched pid={proc.pid} log={log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
