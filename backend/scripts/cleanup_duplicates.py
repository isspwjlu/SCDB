"""Deduplicate the materials table and repair the papers/paper_id provenance.

The ingest loops deduplicate on ``(chemical_composition, doi)``, so two papers
reporting the same compound produce two rows — legitimate as *evidence*, but
when the LLM regurgitates textbook values (every MgB2 row carried
``hc2=14.0, gap=7.2``) the result is a pile of identical rows that inflate the
record count without adding information.

Two policies are available, selected with ``--group-by``:

``content`` (default)
    Collapses rows whose measured content is identical (ignoring id / DOI /
    timestamps). The conservative choice: it only removes rows that say
    exactly the same thing.

``composition-tc``
    Collapses every row that reports the same compound at the same Tc, however
    many papers report it. A compound that 134 papers mention at 39 K is one
    measurement with 134 sources, not 134 measurements. The rows that disagree
    about the *sample* -- synthesis method, lattice parameters, the pressure
    curve -- are not thrown away: they are written to the surviving row's
    ``meta['variants']``.

In both policies the source DOIs are merged into
``materials.meta['source_dois']`` so provenance is not lost, and the most
information-rich row is the one that survives.

This script:

1. Collapses duplicate rows under the chosen policy (see above).
2. Deletes rows that carry no measured data at all (a bare formula with no
   numeric quantity) — these are the worst offenders because they survive the
   ``has_some_data`` constraint only via a stale Tc and duplicate real data.
3. Backfills ``papers`` rows from the DOIs already present on materials and
   wires ``materials.paper_id`` to them, so every kept row is traceable.
4. Closes out ``ingest_runs`` rows stuck in ``status='running'`` (orphans left
   behind when a supervisor killed a loop).

Run with:
    cd backend
    PYTHONPATH=src .venv/Scripts/python scripts/cleanup_duplicates.py --dry-run
    PYTHONPATH=src .venv/Scripts/python scripts/cleanup_duplicates.py
    PYTHONPATH=src .venv/Scripts/python scripts/cleanup_duplicates.py \\
        --group-by composition-tc
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import func, select  # noqa: E402

import superconductor_db.db.models  # noqa: F401,E402
from superconductor_db.db import Base  # noqa: E402
from superconductor_db.db.models.ingest import IngestRun  # noqa: E402
from superconductor_db.db.models.material import Material  # noqa: E402
from superconductor_db.db.models.paper import Paper  # noqa: E402
from superconductor_db.ingest.db_setup import build_ingest_engine  # noqa: E402
from superconductor_db.ingest.provenance import upsert_paper  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _default_database_url() -> str:
    """Fall back to the configured DATABASE_URL.

    A hard-coded relative ``sqlite:///./_dev.db`` silently creates a new
    empty database whenever the process starts from a different working
    directory, which looks like total data loss.
    """
    from superconductor_db.core.config import get_settings

    return get_settings().database_url or "sqlite:///./_dev.db"


# Fields ignored when deciding whether two rows carry the same information.
_IDENTITY_EXCLUDE: frozenset[str] = frozenset(
    {
        "id",
        "doi",
        "paper_id",
        "source",
        "created_at",
        "updated_at",
        "meta",
        "notes",
        "additional_details",
    }
)


def _fingerprint(row: Material) -> tuple:
    """Content signature of a material row, excluding identity/provenance.

    Values are coerced to hashable scalars: JSON columns (e.g.
    ``tc_under_pressure``) are serialised to a canonical string so the tuple
    can be used as a dict key.
    """
    parts: list[object] = []
    for col in Material.__table__.columns:
        if col.name in _IDENTITY_EXCLUDE:
            continue
        value = getattr(row, col.name, None)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, default=str)
        parts.append(value)
    return tuple(parts)


def _composition_tc_key(row: Material) -> tuple:
    """Group key for the ``composition-tc`` policy: the measured pair."""
    return (row.chemical_composition, row.tc_value)


def _group_key(row: Material, mode: str) -> tuple:
    if mode == "composition-tc":
        return _composition_tc_key(row)
    return _fingerprint(row)


def _variants(rows: list[Material], keep: Material) -> dict[str, list]:
    """Values in the group that the kept row does not already carry.

    Merging on (composition, Tc) folds together rows that disagree about
    the *sample* -- one paper's synthesis method, another's lattice
    parameters, a third's pressure curve. Those are real measurements and
    dropping them silently would make the surviving row incomplete, so
    every value the kept row does not already carry is written to its
    ``meta['variants']`` instead.
    """
    out: dict[str, list] = {}
    for col in Material.__table__.columns:
        name = col.name
        if name in _IDENTITY_EXCLUDE or name in ("chemical_composition", "tc_value"):
            continue
        seen: list = []
        for r in rows:
            value = getattr(r, name, None)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, default=str)
            if value not in seen:
                seen.append(value)
        if not seen:
            continue
        kept_value = getattr(keep, name, None)
        if isinstance(kept_value, (dict, list)):
            kept_value = json.dumps(kept_value, sort_keys=True, default=str)
        if len(seen) == 1 and kept_value == seen[0]:
            continue
        out[name] = sorted(seen, key=str)
    return out


def _field_count(row: Material) -> int:
    """How many non-null payload fields the row carries (richness heuristic)."""
    n = 0
    for col in Material.__table__.columns:
        if col.name in _IDENTITY_EXCLUDE:
            continue
        if getattr(row, col.name, None) is not None:
            n += 1
    return n


def _has_measured_data(row: Material) -> bool:
    """True if the row carries at least one numeric quantity beyond Tc alone.

    A row with only a composition + a bare Tc is a stub: it adds no information
    beyond the compound name and tends to be a hallucinated textbook value.
    """
    numeric_beyond_tc = (
        row.t_cdw_value,
        row.hc1_value,
        row.hc2_value,
        row.energy_gap_value,
        row.coherence_length_value,
        row.penetration_depth_value,
        row.critical_current_density_value,
        row.isotope_exponent_alpha,
        row.gamma_n_value,
        row.gamma_s_value,
        row.debye_temperature_value,
        row.fermi_temperature_value,
        row.resistivity_normal_value,
        row.lattice_a_value,
        row.lattice_b_value,
        row.lattice_c_value,
        row.synthesis_temperature_value,
        row.synthesis_pressure_value,
    )
    return any(v is not None for v in numeric_beyond_tc) or bool(
        row.tc_under_pressure
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument(
        "--drop-stubs",
        action="store_true",
        help="Delete rows with no measured data beyond a bare Tc.",
    )
    parser.add_argument(
        "--group-by",
        choices=("content", "composition-tc"),
        default="content",
        help=(
            "What counts as a duplicate. 'content' collapses rows whose "
            "measured payload is identical. 'composition-tc' collapses every "
            "row that reports the same compound at the same Tc, however many "
            "papers report it -- the values that disagree (synthesis method, "
            "lattice parameters, the pressure curve) are kept in "
            "meta['variants']."
        ),
    )
    args = parser.parse_args(argv)

    engine = build_ingest_engine(args.database_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    merged = 0
    deleted_stubs = 0
    papers_created = 0
    links_fixed = 0
    runs_closed = 0

    with SessionLocal() as s:
        total_before = s.execute(select(func.count(Material.id))).scalar() or 0
        print(f"materials before: {total_before}")

        # ---- 1. collapse duplicate rows --------------------------------------
        groups: dict[tuple, list[Material]] = {}
        for row in s.execute(select(Material)).scalars():
            groups.setdefault(_group_key(row, args.group_by), []).append(row)

        for rows in groups.values():
            if len(rows) < 2:
                continue
            # Keep the richest row; tie-break deterministically by created_at.
            keep = max(
                rows,
                key=lambda r: (_field_count(r), str(r.created_at or ""), str(r.id)),
            )
            others = [r for r in rows if r is not keep]
            # Preserve every source DOI so provenance survives the merge.
            dois: list[str] = []
            for r in rows:
                if r.doi and r.doi not in dois:
                    dois.append(r.doi)
            meta = dict(keep.meta or {})
            if dois:
                existing = list(meta.get("source_dois") or [])
                for d in dois:
                    if d not in existing:
                        existing.append(d)
                meta["source_dois"] = existing
            paper_ids = [str(r.paper_id) for r in rows if r.paper_id]
            if len(paper_ids) > 1:
                meta["source_paper_ids"] = sorted(set(paper_ids))
            meta["merged_rows"] = len(rows)
            if args.group_by == "composition-tc":
                variants = _variants(rows, keep)
                if variants:
                    meta["variants"] = variants
            if meta != (keep.meta or {}):
                keep.meta = meta
            for r in others:
                s.delete(r)
                merged += 1

        # ---- 2. optionally drop stub rows ------------------------------------
        if args.drop_stubs:
            for row in s.execute(select(Material)).scalars():
                if not _has_measured_data(row):
                    s.delete(row)
                    deleted_stubs += 1

        s.flush()

        # ---- 3. rebuild papers + paper_id links ------------------------------
        dois = [
            d
            for (d,) in s.execute(
                select(Material.doi).where(Material.doi.isnot(None)).distinct()
            )
            if d
        ]
        for doi in dois:
            if s.execute(select(Paper.id).where(Paper.doi == doi).limit(1)).first() is None:
                papers_created += 1
            upsert_paper(s, doi)

        for row in s.execute(select(Material)).scalars():
            if row.doi:
                paper_id = s.execute(
                    select(Paper.id).where(Paper.doi == row.doi).limit(1)
                ).scalar_one_or_none()
                if paper_id is not None and row.paper_id != paper_id:
                    row.paper_id = paper_id
                    links_fixed += 1

        # ---- 4. close orphaned running ingest_runs ---------------------------
        for run in s.execute(
            select(IngestRun).where(IngestRun.status == "running")
        ).scalars():
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.errors = (run.errors or 0) + 1
            runs_closed += 1

        if not args.dry_run:
            s.commit()
        else:
            s.rollback()

        total_after = s.execute(select(func.count(Material.id))).scalar() or 0

    verb = "would" if args.dry_run else ""
    print(f"group-by policy:  {args.group_by}")
    print(f"materials after:  {total_after}")
    print(f"duplicate rows {verb} merge: {merged}")
    if args.drop_stubs:
        print(f"stub rows {verb} delete:   {deleted_stubs}")
    print(f"papers {verb} create:       {papers_created}")
    print(f"paper_id links {verb} fix:  {links_fixed}")
    print(f"stuck 'running' runs closed: {runs_closed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
