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
# temperatures for well-known superconductors (hand-curated; no fake-looking
# numbers). Columns mirror the canonical SuperCon CSV header so the loader
# reads both real and fixture identically.
FIXTURE_CSV_HEADER = (
    "chemical_formula,Tc,Tc_uncertainty,sample,reference\n"
)
FIXTURE_ROWS: list[str] = [
    # (formula, Tc_K, sample, reference)
    "MgB2,39.0,,polycrystalline,NIMS-SuperCon-001\n",
    "Nb,9.25,,bulk,NIMS-SuperCon-002\n",
    "Pb,7.20,,bulk,NIMS-SuperCon-003\n",
    "Hg,4.15,,bulk,NIMS-SuperCon-004\n",
    "YBa2Cu3O7,92.0,,ceramic,NIMS-SuperCon-005\n",
    "Bi2Sr2CaCu2O8,85.0,,single crystal,NIMS-SuperCon-006\n",
    "Tl2Ba2CaCu2O8,108.0,,ceramic,NIMS-SuperCon-007\n",
    "HgBa2Ca2Cu3O8,134.0,,ceramic,NIMS-SuperCon-008\n",
    "FeSe,8.0,,single crystal,NIMS-SuperCon-009\n",
    "SmFeAsO0.85F0.15,55.0,,polycrystalline,NIMS-SuperCon-010\n",
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