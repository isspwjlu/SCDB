"""Materials repository (spec §7).

A thin SQLAlchemy 2.0 layer that powers the ``/api/v1/materials`` and
``/api/v1/stats`` endpoints. Keeps query logic in one place so the route
handlers stay small.

Public surface:

- :class:`MaterialsRepository`
    - ``list(filters, page, per_page, sort, order) -> tuple[list[Material], int]``
    - ``get(material_id) -> Material | None``
    - ``create(record: SuperconductingRecord) -> Material``
    - ``upsert_by_doi(doi, record: SuperconductingRecord) -> Material``
    - ``aggregate_overview() -> dict``
    - ``tc_distribution(bins=30) -> list[dict]``
    - ``family_pie() -> list[dict]``
    - ``tc_vs_pressure() -> list[dict]``

Filters (for ``list``):

- ``search``       : substring match on ``chemical_composition``,
                     ``crystal_structure``, ``pairing_symmetry``,
                     ``superconductor_family`` (case-insensitive ``ILIKE``).
- ``family``       : exact match on ``superconductor_family``.
- ``tc_min``       : lower bound (inclusive) on ``tc_value``.
- ``tc_max``       : upper bound (inclusive) on ``tc_value``.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from superconductor_db.db.models.material import Material
from superconductor_db.schemas.material import SuperconductingRecord

log = logging.getLogger(__name__)


# Sortable columns (whitelist to avoid SQL injection via ``?sort=...``).
_SORTABLE: dict[str, Any] = {
    "tc_value": Material.tc_value,
    "chemical_composition": Material.chemical_composition,
    "created_at": Material.created_at,
    "updated_at": Material.updated_at,
}


def _qty_value(q: Any) -> Any:
    """Extract the float value from a Pydantic ``Quantity`` (or return as-is)."""
    if q is None:
        return None
    value = getattr(q, "value", q)
    if value is None or value == "" or (isinstance(value, str) and value.upper() == "NA"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty_unit(q: Any) -> str | None:
    if q is None:
        return None
    return getattr(q, "unit", None) or None


class MaterialsRepository:
    """Repository for the ``materials`` table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ----- Read -------------------------------------------------------------

    def list(
        self,
        filters: dict[str, Any] | None = None,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "tc_value",
        order: str = "desc",
    ) -> tuple[list[Material], int]:
        """Paginated list with filtering / sorting. Returns ``(items, total)``."""
        filters = filters or {}
        stmt = select(Material)
        count_stmt = select(func.count()).select_from(Material)

        # ---- filters -------------------------------------------------------
        if search := (filters.get("search") or "").strip():
            pattern = f"%{search}%"
            stmt = stmt.where(
                Material.chemical_composition.ilike(pattern)
                | Material.crystal_structure.ilike(pattern)
                | Material.pairing_symmetry.ilike(pattern)
                | Material.superconductor_family.ilike(pattern)
            )
            count_stmt = count_stmt.where(
                Material.chemical_composition.ilike(pattern)
                | Material.crystal_structure.ilike(pattern)
                | Material.pairing_symmetry.ilike(pattern)
                | Material.superconductor_family.ilike(pattern)
            )
        if family := filters.get("family"):
            stmt = stmt.where(Material.superconductor_family == family)
            count_stmt = count_stmt.where(Material.superconductor_family == family)
        if tc_min := filters.get("tc_min"):
            stmt = stmt.where(Material.tc_value >= float(tc_min))
            count_stmt = count_stmt.where(Material.tc_value >= float(tc_min))
        if tc_max := filters.get("tc_max"):
            stmt = stmt.where(Material.tc_value <= float(tc_max))
            count_stmt = count_stmt.where(Material.tc_value <= float(tc_max))

        # ---- sort ----------------------------------------------------------
        sort_col = _SORTABLE.get(sort, Material.tc_value)
        if order.lower() == "asc":
            stmt = stmt.order_by(sort_col.asc().nulls_last())
        else:
            stmt = stmt.order_by(sort_col.desc().nulls_last())

        # ---- pagination ----------------------------------------------------
        page = max(int(page), 1)
        per_page = max(min(int(per_page), 200), 1)
        stmt = stmt.offset((page - 1) * per_page).limit(per_page)

        items = list(self.session.scalars(stmt).all())
        total = int(self.session.execute(count_stmt).scalar_one())
        return items, total

    def get(self, material_id: uuid.UUID) -> Material | None:
        return self.session.get(Material, material_id)

    # ----- Write ------------------------------------------------------------

    def create(self, record: SuperconductingRecord) -> Material:
        """Insert a record (caller responsible for ensuring uniqueness)."""
        row = self._record_to_row(record)
        m = Material(**row)
        self.session.add(m)
        self.session.commit()
        self.session.refresh(m)
        return m

    def upsert_by_doi(self, doi: str, record: SuperconductingRecord) -> Material:
        """If a row with ``doi`` exists, update it; else insert."""
        existing = self.session.scalars(
            select(Material).where(Material.doi == doi)
        ).one_or_none()
        if existing is None:
            return self.create(record)
        row = self._record_to_row(record)
        for k, v in row.items():
            setattr(existing, k, v)
        self.session.commit()
        self.session.refresh(existing)
        return existing

    # ----- Aggregations -----------------------------------------------------

    def aggregate_overview(self) -> dict[str, Any]:
        """Counts + extremes + histograms used by ``/stats/overview``."""
        total = int(self.session.execute(select(func.count(Material.id))).scalar_one())
        max_tc = self.session.execute(select(func.max(Material.tc_value))).scalar_one()
        max_hc2 = self.session.execute(select(func.max(Material.hc2_value))).scalar_one()

        family_rows = self.session.execute(
            select(Material.superconductor_family, func.count(Material.id))
            .where(Material.superconductor_family.is_not(None))
            .group_by(Material.superconductor_family)
            .order_by(func.count(Material.id).desc())
        ).all()
        family_counts = {fam: int(cnt) for fam, cnt in family_rows}

        mech_rows = self.session.execute(
            select(Material.mechanism, func.count(Material.id))
            .where(Material.mechanism.is_not(None))
            .group_by(Material.mechanism)
            .order_by(func.count(Material.id).desc())
        ).all()
        mechanism_counts = {mech: int(cnt) for mech, cnt in mech_rows}

        return {
            "total": total,
            "max_tc": float(max_tc) if max_tc is not None else None,
            "max_hc2": float(max_hc2) if max_hc2 is not None else None,
            "family_counts": family_counts,
            "mechanism_counts": mechanism_counts,
        }

    def tc_distribution(self, bins: int = 30) -> list[dict[str, float]]:
        """Equal-width histogram bins of ``tc_value`` (in Kelvin)."""
        bins = max(min(int(bins), 200), 1)
        minmax = self.session.execute(
            select(func.min(Material.tc_value), func.max(Material.tc_value))
        ).one()
        lo, hi = minmax[0], minmax[1]
        if lo is None or hi is None:
            return []
        if lo == hi:
            return [{"bin_start": float(lo), "bin_end": float(hi), "count": int(
                self.session.execute(
                    select(func.count(Material.id)).where(Material.tc_value == lo)
                ).scalar_one()
            )}]
        width = (hi - lo) / bins
        edges = [lo + i * width for i in range(bins + 1)]
        out: list[dict[str, float]] = []
        for i in range(bins):
            start, end = edges[i], edges[i + 1]
            if i == bins - 1:
                # include the upper bound on the last bin so the max value isn't lost
                count = int(
                    self.session.execute(
                        select(func.count(Material.id)).where(
                            (Material.tc_value >= start) & (Material.tc_value <= end)
                        )
                    ).scalar_one()
                )
            else:
                count = int(
                    self.session.execute(
                        select(func.count(Material.id)).where(
                            (Material.tc_value >= start) & (Material.tc_value < end)
                        )
                    ).scalar_one()
                )
            out.append({"bin_start": float(start), "bin_end": float(end), "count": count})
        return out

    def family_pie(self) -> list[dict[str, Any]]:
        """List of ``{family, count}`` rows for pie charts."""
        rows = self.session.execute(
            select(Material.superconductor_family, func.count(Material.id))
            .where(Material.superconductor_family.is_not(None))
            .group_by(Material.superconductor_family)
            .order_by(func.count(Material.id).desc())
        ).all()
        return [{"family": fam, "count": int(cnt)} for fam, cnt in rows]

    def tc_vs_pressure(self) -> list[dict[str, Any]]:
        """For each material with ``tc_under_pressure`` JSONB, return
        ``{composition, pressures[], tcs[]}`` so the frontend can plot."""
        rows = self.session.execute(
            select(Material.chemical_composition, Material.tc_under_pressure).where(
                Material.tc_under_pressure.is_not(None)
            )
        ).all()
        out: list[dict[str, Any]] = []
        for composition, payload in rows:
            if not isinstance(payload, dict):
                continue
            pressures = payload.get("pressures") or []
            tcs = payload.get("tcs") or []
            if not pressures or not tcs:
                continue
            try:
                pressures_f = [float(p) for p in pressures]
                tcs_f = [float(t) for t in tcs]
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "composition": composition,
                    "pressures": pressures_f,
                    "tcs": tcs_f,
                }
            )
        return out

    # ----- Helpers ----------------------------------------------------------

    @staticmethod
    def _record_to_row(record: SuperconductingRecord) -> dict[str, Any]:
        """Map a :class:`SuperconductingRecord` to a ``Material`` kwargs dict."""
        sp = record.superconducting_property
        si = record.structure_information
        pm = record.preparation_method

        row: dict[str, Any] = {
            "chemical_composition": record.material_chemical_composition,
            "doi": record.doi,
            "experimental": record.experimental,
            "source": record.source,
            "tc_value": _qty_value(sp.Tc),
            "tc_unit": _qty_unit(sp.Tc),
            "t_cdw_value": _qty_value(sp.T_CDW),
            "t_cdw_unit": _qty_unit(sp.T_CDW),
            "hc1_value": _qty_value(sp.Hc1),
            "hc1_unit": _qty_unit(sp.Hc1),
            "hc2_value": _qty_value(sp.Hc2),
            "hc2_unit": _qty_unit(sp.Hc2),
            "energy_gap_value": _qty_value(sp.Energy_Gap),
            "energy_gap_unit": _qty_unit(sp.Energy_Gap),
            "coherence_length_value": _qty_value(sp.Coherence_Length),
            "coherence_length_unit": _qty_unit(sp.Coherence_Length),
            "penetration_depth_value": _qty_value(sp.Penetration_Depth),
            "penetration_depth_unit": _qty_unit(sp.Penetration_Depth),
            "critical_current_density_value": _qty_value(sp.Critical_Current_Density),
            "critical_current_density_unit": _qty_unit(sp.Critical_Current_Density),
            "pairing_symmetry": sp.Pairing_Symmetry,
            "sc_type": sp.Type,
            "mechanism": sp.Mechanism,
            "superconductor_family": sp.Superconductor_Family,
            "tc_under_pressure": (
                record.tc_under_pressure.model_dump()
                if record.tc_under_pressure
                else None
            ),
            "isotope_exponent_alpha": record.isotope_exponent_alpha,
            "uemura_class": record.uemura_class,
            "gamma_n_value": _qty_value(record.gamma_n),
            "gamma_n_unit": _qty_unit(record.gamma_n),
            "gamma_s_value": _qty_value(record.gamma_s),
            "gamma_s_unit": _qty_unit(record.gamma_s),
            "debye_temperature_value": _qty_value(record.debye_temperature),
            "debye_temperature_unit": _qty_unit(record.debye_temperature),
            "fermi_temperature_value": _qty_value(record.fermi_temperature),
            "fermi_temperature_unit": _qty_unit(record.fermi_temperature),
            "resistivity_normal_value": _qty_value(record.resistivity_normal),
            "resistivity_normal_unit": _qty_unit(record.resistivity_normal),
            "crystal_structure": si.crystal_structure,
            "lattice_structure": si.lattice_structure,
            "lattice_a_value": si.a.value if si.a else None,
            "lattice_a_unit": si.a.unit if si.a else None,
            "lattice_b_value": si.b.value if si.b else None,
            "lattice_b_unit": si.b.unit if si.b else None,
            "lattice_c_value": si.c.value if si.c else None,
            "lattice_c_unit": si.c.unit if si.c else None,
            "space_group": si.space_group,
            "notes": record.notes,
            "meta": record.meta or {},
        }
        # Optional preparation fields; only include when present.
        for src, dst in (
            (pm.Synthesis_Method, "synthesis_method"),
            (pm.Chemical_Precursors, "chemical_precursors"),
            (pm.Synthesis_Atmosphere, "synthesis_atmosphere"),
            (pm.Annealing_Atmosphere, "annealing_atmosphere"),
            (pm.Cooling_Method, "cooling_method"),
            (pm.Substrate, "substrate"),
            (pm.Additional_Details, "additional_details"),
        ):
            row[dst] = src
        for src_q, val_dst, unit_dst in (
            (pm.Synthesis_Temperature, "synthesis_temperature_value", "synthesis_temperature_unit"),
            (pm.Synthesis_Pressure, "synthesis_pressure_value", "synthesis_pressure_unit"),
            (pm.Synthesis_Time, "synthesis_time_value", "synthesis_time_unit"),
            (pm.Heating_Rate, "heating_rate_value", "heating_rate_unit"),
            (
                pm.Sintering_Temperature,
                "sintering_temperature_value",
                "sintering_temperature_unit",
            ),
            (pm.Sintering_Time, "sintering_time_value", "sintering_time_unit"),
            (
                pm.Annealing_Temperature,
                "annealing_temperature_value",
                "annealing_temperature_unit",
            ),
            (pm.Annealing_Time, "annealing_time_value", "annealing_time_unit"),
        ):
            row[val_dst] = _qty_value(src_q)
            row[unit_dst] = _qty_unit(src_q)
        # Drop any keys whose value is ``None`` so SQLAlchemy defaults apply
        # (e.g. ``meta`` jsonb default). We do NOT drop keys whose values are
        # falsy-but-valid (empty string, 0.0).
        return {k: v for k, v in row.items() if v is not None}
