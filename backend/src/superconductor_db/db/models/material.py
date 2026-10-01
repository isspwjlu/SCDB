"""Material model (spec §10 ``materials`` table).

This is the centrepiece table. We mirror the SQL DDL one-for-one:

- Identification (6): id, chemical_composition, doi, paper_id, experimental, source
- Base 12 SC properties: Tc, T_CDW, Hc1, Hc2, Energy_gap, Coherence_length,
  Penetration_depth, Critical_current_density, pairing_symmetry, sc_type,
  mechanism, superconductor_family -- each split into ``_value`` + ``_unit``.
- SC-specific 7 fields: tc_under_pressure (jsonb), isotope_exponent_alpha,
  uemura_class, gamma_n, gamma_s, debye_temperature, fermi_temperature,
  resistivity_normal.
- Crystal structure (5): crystal_structure, lattice_structure, lattice_{a,b,c}
  with values + units, space_group.
- Preparation (17): synthesis_method/precursors/temperature/pressure/atmosphere
  /time, heating_rate, sintering_temperature/time, annealing_temperature/time
  /atmosphere, cooling_method, substrate, additional_details.
- Metadata (4): notes, meta (jsonb), created_at, updated_at.
- CheckConstraint ``has_some_data`` per spec §5.2.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from superconductor_db.db.types import GUID, JSONType
from sqlalchemy.orm import Mapped, mapped_column

from superconductor_db.db.base import Base


class Material(Base):
    """Superconductor material record. Central fact table."""

    __tablename__ = "materials"

    # --- Identification -----------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=lambda: uuid.uuid4(),
    )
    chemical_composition: Mapped[str] = mapped_column(Text, nullable=False)
    doi: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paper_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(),
        ForeignKey("papers.id", ondelete="SET NULL"),
        nullable=True,
    )
    experimental: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Base 12 SC properties ---------------------------------------------
    tc_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tc_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    t_cdw_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    t_cdw_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hc1_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hc1_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hc2_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hc2_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    energy_gap_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    energy_gap_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    coherence_length_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    coherence_length_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    penetration_depth_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    penetration_depth_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    critical_current_density_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    critical_current_density_unit: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    pairing_symmetry: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sc_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mechanism: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    superconductor_family: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- SC-specific 7 fields ----------------------------------------------
    tc_under_pressure: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONType, nullable=True
    )
    isotope_exponent_alpha: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    uemura_class: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gamma_n_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma_n_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gamma_s_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma_s_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    debye_temperature_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    debye_temperature_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fermi_temperature_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    fermi_temperature_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resistivity_normal_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    resistivity_normal_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Crystal structure --------------------------------------------------
    crystal_structure: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lattice_structure: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lattice_a_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lattice_a_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lattice_b_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lattice_b_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lattice_c_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lattice_c_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    space_group: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Preparation --------------------------------------------------------
    synthesis_method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chemical_precursors: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synthesis_temperature_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    synthesis_temperature_unit: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    synthesis_pressure_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    synthesis_pressure_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synthesis_atmosphere: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synthesis_time_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    synthesis_time_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    heating_rate_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    heating_rate_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sintering_temperature_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    sintering_temperature_unit: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    sintering_time_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    sintering_time_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    annealing_temperature_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    annealing_temperature_unit: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    annealing_time_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    annealing_time_unit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    annealing_atmosphere: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cooling_method: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    substrate: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    additional_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Metadata -----------------------------------------------------------
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONType, nullable=True, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "(tc_value IS NOT NULL) "
            "OR (t_cdw_value IS NOT NULL) "
            "OR (hc1_value IS NOT NULL) "
            "OR (hc2_value IS NOT NULL) "
            "OR (energy_gap_value IS NOT NULL) "
            "OR (coherence_length_value IS NOT NULL) "
            "OR (penetration_depth_value IS NOT NULL) "
            "OR (critical_current_density_value IS NOT NULL)",
            name="has_some_data",
        ),
        Index("materials_composition_idx", "chemical_composition"),
        Index(
            "materials_tc_value_idx",
            "tc_value",
            sqlite_where=text("tc_value IS NOT NULL"),
        ),
        Index(
            "materials_family_idx",
            "superconductor_family",
            sqlite_where=text("superconductor_family IS NOT NULL"),
        ),
        Index("materials_doi_idx", "doi"),
    )

    def __repr__(self) -> str:
        return (
            f"Material(id={self.id!r}, composition={self.chemical_composition!r}, "
            f"tc={self.tc_value!r} {self.tc_unit!r})"
        )