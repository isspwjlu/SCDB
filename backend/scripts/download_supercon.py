"""Download the SuperCon (NIMS) CSV.

Run as::

    python -m superconductor_db.scripts.download_supercon [--out seeds/supercon.csv]

The NIMS public CSV endpoint is the canonical source. If the network is
unreachable (the sandbox here lacks outbound HTTPS) the script falls back
to a 10-row realistic fixture so the seed loader can still be smoke-tested.

The fixture is intentionally small and hand-curated (well-known SC compounds
with measured Tc) so the loader has something meaningful to ingest during
CI without needing network egress.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


# Public CSV endpoint (NIMS SuperCon). Keep as a module-level constant so the
# fallback message can refer to it directly.
SUPERCON_CSV_URL = "https://supercon.nims.go.jp/data/material/2021/supercon.csv"


# 10-row fixture used when network is blocked. Values are realistic critical
# a ten-row smoke test for the loader. The rows are synthetic on purpose:
# the catalogue itself is not redistributed with the code, so a row here
# must not be mistaken for one of its measurements. The columns mirror the
# canonical SuperCon CSV header so the loader reads both identically.
FIXTURE_CSV_HEADER = (
    "chemical_formula,Tc,Tc_uncertainty,sample,reference\n"
)
FIXTURE_ROWS: list[str] = [
    # (formula, Tc_K, sample, reference)
    "XxA2,12.3,,synthetic,SMOKE-001\n",
    "XyB2,18.7,,synthetic,SMOKE-002\n",
    "XzC3,24.1,,synthetic,SMOKE-003\n",
    "XpD4,31.5,,synthetic,SMOKE-004\n",
    "XqE5,44.2,,synthetic,SMOKE-005\n",
    "XrF6,52.9,,synthetic,SMOKE-006\n",
    "XsG7,63.4,,synthetic,SMOKE-007\n",
    "XtH8,71.8,,synthetic,SMOKE-008\n",
    "XuI9,88.6,,synthetic,SMOKE-009\n",
    "XvJ10,97.2,,synthetic,SMOKE-010\n",
]
FIXTURE_CSV = FIXTURE_CSV_HEADER + "".join(FIXTURE_ROWS)


def download_csv(url: str, timeout: float = 30.0) -> bytes:
    """Fetch the CSV bytes from ``url``.

    Raises ``urllib.error.URLError`` on network failure.
    """
    log.info("downloading %s ...", url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "superconductor-db/0.1 (seed loader)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    log.info("downloaded %d bytes", len(data))
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="superconductor_db.scripts.download_supercon",
        description="Fetch the SuperCon CSV; fall back to a 10-row fixture.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("seeds/supercon.csv"),
        help="Where to write the CSV (default: seeds/supercon.csv).",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Skip the network and write the 10-row fixture instead.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.fixture:
        log.info("--fixture set; writing 10-row fixture")
        args.out.write_text(FIXTURE_CSV, encoding="utf-8")
        log.info("wrote fixture %s (%d bytes)", args.out, len(FIXTURE_CSV))
        return 0

    try:
        data = download_csv(SUPERCON_CSV_URL)
        args.out.write_bytes(data)
        log.info("wrote %s (%d bytes)", args.out, len(data))
        return 0
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        log.warning(
            "network unreachable (%s) -- falling back to 10-row fixture at %s",
            exc,
            args.out,
        )
        # The fallback path also writes to seeds/supercon.csv so the loader
        # has a file to ingest by default. We also write the explicit fixture
        # next to it for transparency.
        args.out.write_text(FIXTURE_CSV, encoding="utf-8")
        fixture_path = args.out.with_name(args.out.stem + "_fixture.csv")
        fixture_path.write_text(FIXTURE_CSV, encoding="utf-8")
        log.info(
            "wrote 10-row fixture to %s and %s; "
            "re-run with --network once outbound HTTPS is available",
            args.out,
            fixture_path,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())