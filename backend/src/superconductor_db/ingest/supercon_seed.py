"""SuperCon (NIMS) one-shot seed loader.

CLI::

    python -m superconductor_db.ingest.supercon_seed \\
        --path seeds/supercon.csv --batch-size 1000

Behaviour:

- Reads SuperCon CSV (canonical columns: ``chemical_formula``, ``Tc``,
  ``Tc_uncertainty``, ``sample``, ``reference``).
- Maps to ``materials`` row layout per spec §10.
- UUID derived from SHA-256 of ``(chemical_formula|Tc|sample)`` for
  idempotency — re-running the loader yields the same ids and ``INSERT ...
  ON CONFLICT DO NOTHING`` (or ``DO UPDATE``) leaves the table unchanged.
- Uses bulk inserts in ``--batch-size`` chunks for speed.

The NIMS SuperCon CSV header format is documented in their official spec
(``supercon.nims.go.jp``). The loader tolerates extra columns by stashing
them in ``materials.meta`` (jsonb).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from superconductor_db.core.config import get_settings
from superconductor_db.core.db import get_engine, get_session_factory

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# CSV -> dict mapping                                                         #
# --------------------------------------------------------------------------- #


# Canonical SuperCon CSV columns we know how to map. Anything else goes to meta.
_SCC_TO_MATERIAL: dict[str, str] = {
    "chemical_formula": "chemical_composition",
    "Tc": "tc_value",
    "Tc_uncertainty": "tc_uncertainty",  # -> meta
    "sample": "sample",  # -> meta
    "reference": "reference",  # -> meta
}


def _derive_uuid(chemical_formula: str, tc: str, sample: str) -> uuid.UUID:
    """Deterministic UUID v5 from a SuperCon row's key fields.

    Using v5 (namespace) makes the ids stable across runs and seeds, so
    re-imports are idempotent.
    """
    seed = f"{chemical_formula}|{tc}|{sample}".encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return uuid.UUID(bytes=digest[:16], version=4)


def _coerce_float(value: str) -> float | None:
    """Parse a string to float, returning None for ``NA`` / unparsable."""
    if value is None:
        return None
    s = value.strip()
    if not s or s.upper() in {"NA", "N/A", "NULL", "NONE", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _row_to_mapped_dict(row: dict[str, str]) -> dict[str, Any]:
    """Convert a SuperCon CSV row to a ``materials`` insert dict.

    Returns ``None``-valued fields for non-numeric measurements; the SQL
    ``has_some_data`` CheckConstraint will reject rows with no SC quantity
    anyway, so the caller can pre-filter.
    """
    composition = row.get("chemical_formula", "").strip()
    if not composition:
        # Can't store without a composition -- skip.
        raise ValueError("row missing chemical_formula")

    tc_raw = row.get("Tc", "")
    tc_value = _coerce_float(tc_raw)
    sample = row.get("sample", "").strip()
    reference = row.get("reference", "").strip()

    mapped: dict[str, Any] = {
        "id": _derive_uuid(composition, tc_raw, sample),
        "chemical_composition": composition,
        "experimental": True,
        "source": "supercon",
        "tc_value": tc_value,
        "tc_unit": "K" if tc_value is not None else None,
        "doi": reference or None,
    }

    # Stash the unmapped SuperCon columns in meta for provenance.
    meta: dict[str, Any] = {}
    for src_key, src_val in row.items():
        if src_key in _SCC_TO_MATERIAL:
            continue
        if src_val:
            meta[src_key] = src_val
    if sample:
        meta["supercon_sample"] = sample
    mapped["meta"] = meta

    return mapped


def _iter_csv(path: Path) -> Iterator[dict[str, Any]]:
    """Yield mapped ``materials`` rows from ``path``.

    Rows lacking ``tc_value`` AND no other SC property are dropped
    (spec §5.2 inclusion criterion). The SuperCon dump only carries Tc, so
    in practice we filter on ``tc_value IS NOT NULL``.
    """
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                mapped = _row_to_mapped_dict(row)
            except ValueError:
                continue
            if mapped.get("tc_value") is None:
                # Drop rows without Tc (spec §5.2).
                continue
            yield mapped


# --------------------------------------------------------------------------- #
# Bulk insert                                                                 #
# --------------------------------------------------------------------------- #


def _bulk_insert(engine: Engine, rows: list[dict[str, Any]]) -> int:
    """Insert ``rows`` via ``INSERT ... ON CONFLICT (id) DO NOTHING``.

    Returns the number of new rows actually inserted.
    """
    if not rows:
        return 0
    # We need the underlying SQLAlchemy Table for ON CONFLICT. Import lazily
    # so the module can be loaded without the full model stack (e.g. from
    # tests that monkey-patch the model).
    from superconductor_db.db.base import Base
    from superconductor_db.db.models.material import Material

    table = Material.__table__
    # Sync metadata if the engine's dialect differs (paranoia for greenfield).
    Base.metadata.bind = engine

    stmt = pg_insert(table).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="superconductor_db.ingest.supercon_seed",
        description="Load the SuperCon (NIMS) CSV into the materials table.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path("seeds/supercon.csv"),
        help="Path to the SuperCon CSV (default: seeds/supercon.csv).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of rows per bulk insert (default: 1000).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    if not args.path.exists():
        log.error("CSV not found at %s", args.path)
        log.error(
            "Tip: run `python backend/scripts/download_supercon.py --out "
            "seeds/supercon.csv` first; add --fixture there for a 10-row "
            "synthetic smoke test."
        )
        return 2

    settings = get_settings()
    # Honour DATABASE_URL override (SQLite dev) before falling back to
    # the legacy postgres_* fields. This mirrors ``get_db()`` in
    # ``core.db`` so the seed loader connects to the same DB as the API.
    dsn = (
        settings.database_url
        or settings.postgres_dsn
        or _sync_dsn(settings)
    )
    engine = get_engine(
        dsn,
        connect_args={"check_same_thread": False} if dsn.startswith("sqlite") else {},
    )

    total_inserted = 0
    total_seen = 0
    batch: list[dict[str, Any]] = []
    for mapped in _iter_csv(args.path):
        total_seen += 1
        batch.append(mapped)
        if len(batch) >= args.batch_size:
            total_inserted += _bulk_insert(engine, batch)
            batch.clear()
            log.info("loaded %d rows so far (inserted: %d)", total_seen, total_inserted)

    if batch:
        total_inserted += _bulk_insert(engine, batch)

    log.info(
        "done: %d rows seen, %d new rows inserted (idempotent re-runs add 0)",
        total_seen,
        total_inserted,
    )
    return 0


def _sync_dsn(settings: Any) -> str:
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


if __name__ == "__main__":
    sys.exit(main())