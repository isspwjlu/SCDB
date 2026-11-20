"""Pull a file down from the master.

Usage::

    D:\\anaconda3\\python.exe tools/fetch_master_file.py <remote path> <local path>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\42033\.claude")
from ssh_login import connect  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    remote, local = sys.argv[1], Path(sys.argv[2]).resolve()
    local.parent.mkdir(parents=True, exist_ok=True)
    ssh = connect()
    try:
        sftp = ssh.open_sftp()
        sftp.get(remote, str(local))
        sftp.close()
    finally:
        ssh.close()
    print(f"{remote} → {local}  {local.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
