"""Superconductor Pydantic schemas (spec §5).

Two layers:

- ``SuperconductingProperty`` -- the 12 base + SC-specific fields, each
  represented as a :class:`Quantity` (value + unit).
- ``SuperconductingRecord`` -- top-level record that bundles everything
  needed to INSERT a ``materials`` row. Its ``has_meaningful_data()`` mirrors
  the SQL ``has_some_data`` CheckConstraint (spec §5.2).

Plus the structure and preparation schemas so the LLM extractor (Task 26)
has a typed shape to validate against.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from superconductor_db.schemas.common import Quantity, _is_real


class LatticeParameter(BaseModel):
    """One lattice axis (a/b/c) with value + unit."""

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    unit: str | None = None


class StructureInformation(BaseModel):
    """Crystal structure + lattice constants + space group."""

    model_config = ConfigDict(extra="forbid")

    crystal_structure: str | None = None
    lattice_structure: str | None = None
    a: LatticeParameter | None = None
    b: LatticeParameter | None = None
    c: LatticeParameter | None = None
    space_group: str | None = None


class SuperconductingProperty(BaseModel):
    """All 12 base + SC-specific SC fields.

    Naming follows spec §5.1 table B/C verbatim. Field names use the spec's
    CamelCase (Tc, T_CDW, ...) -- the LLM extractor produces these directly.
    """

    model_config = ConfigDict(extra="forbid")

    # Base 12 SC properties ------------------------------------------------
    Tc: Quantity | None = None  # noqa: N815 -- spec naming
    T_CDW: Quantity | None = None  # noqa: N815
    Hc1: Quantity | None = None  # noqa: N815
    Hc2: Quantity | None = None  # noqa: N815
    Energy_Gap: Quantity | None = None  # noqa: N815
    Coherence_Length: Quantity | None = None  # noqa: N815
    Penetration_Depth: Quantity | None = None  # noqa: N815
    Critical_Current_Density: Quantity | None = None  # noqa: N815
    Pairing_Symmetry: str | None = None  # noqa: N815
    Type: str | None = None  # noqa: N815  -- spec calls it "sc_type"
    Mechanism: str | None = None  # noqa: N815
    Superconductor_Family: str | None = None  # noqa: N815


class PreparationMethod(BaseModel):
    """Synthesis / preparation metadata. Free-form dict-of-fields."""

    model_config = ConfigDict(extra="allow")

    Synthesis_Method: str | None = None
    Chemical_Precursors: str | None = None
    Synthesis_Temperature: Quantity | None = None
    Synthesis_Pressure: Quantity | None = None
    Synthesis_Atmosphere: str | None = None
    Synthesis_Time: Quantity | None = None
    Heating_Rate: Quantity | None = None
    Sintering_Temperature: Quantity | None = None
    Sintering_Time: Quantity | None = None
    Annealing_Temperature: Quantity | None = None
    Annealing_Time: Quantity | None = None
    Annealing_Atmosphere: str | None = None
    Cooling_Method: str | None = None
    Substrate: str | None = None
    Additional_Details: str | None = None


class SuperconductingRecord(BaseModel):
    """Top-level record ready for INSERT into ``materials``.

    Mirrors spec §5.2 inclusion criterion via ``has_meaningful_data``.
    """

    model_config = ConfigDict(extra="forbid")

    # Identification ------------------------------------------------------
    material_chemical_composition: str = Field(..., min_length=1)
    doi: str | None = None
    experimental: bool = True
    source: str = Field(default="contributor")  # supercon | llm_extract | manual | contributor

    # SC properties --------------------------------------------------------
    superconducting_property: SuperconductingProperty = Field(
        default_factory=SuperconductingProperty
    )
    tc_under_pressure: dict[str, Any] | None = None
    isotope_exponent_alpha: float | None = None
    uemura_class: str | None = None
    gamma_n: Quantity | None = None
    gamma_s: Quantity | None = None
    debye_temperature: Quantity | None = None
    fermi_temperature: Quantity | None = None
    resistivity_normal: Quantity | None = None

    # Crystal structure ----------------------------------------------------
    structure_information: StructureInformation = Field(
        default_factory=StructureInformation
    )

    # Preparation ----------------------------------------------------------
    preparation_method: PreparationMethod = Field(default_factory=PreparationMethod)

    # Metadata -------------------------------------------------------------
    notes: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    # ----- Inclusion criterion (spec §5.2) -------------------------------

    def has_meaningful_data(self) -> bool:
        """True iff any of Tc, T_CDW, Hc1, Hc2, Energy_Gap, Coherence_Length,
        Penetration_Depth, Critical_Current_Density carries a real value.

        Equivalent to the SQL ``has_some_data`` CheckConstraint on
        ``materials``.
        """
        sp = self.superconducting_property
        return any(
            _is_real(q)
            for q in (
                sp.Tc,
                sp.T_CDW,
                sp.Hc1,
                sp.Hc2,
                sp.Energy_Gap,
                sp.Coherence_Length,
                sp.Penetration_Depth,
                sp.Critical_Current_Density,
            )
        )