"""LLM-driven extractor (spec §6.3).

``SuperconductingExtractor(client).extract_all(text, doi) -> list[SuperconductingRecord]``

Steps:

1. **Composition extraction** (sequential) -- identify the chemical formulas
   mentioned in the paper.
2. **Dimension prompts** (parallel via :func:`asyncio.gather`) -- the remaining
   7 prompts each take the materials list and emit per-composition data.
3. **Merge** by ``Material_Chemical_Composition`` into
   :class:`SuperconductingRecord` rows.
4. **Sanity checks** -- reject obviously-wrong Tc values (``_plausible_tc``)
   and chemically-impossible formulas (``_is_valid_formula``).
5. **Inclusion filter** -- drop records that fail
   :meth:`SuperconductingRecord.has_meaningful_data`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from superconductor_db.ingest.llm_client import LLMClient
from superconductor_db.ingest.prompts import (
    build_base_sc_props_prompt,
    build_composition_prompt,
    build_crystal_structure_prompt,
    build_fermi_resistivity_prompt,
    build_isotope_uemura_prompt,
    build_pressure_tc_prompt,
    build_preparation_prompt,
    build_specific_heat_prompt,
)
from superconductor_db.schemas.common import Quantity
from superconductor_db.schemas.material import (
    LatticeParameter,
    PreparationMethod,
    StructureInformation,
    SuperconductingProperty,
    SuperconductingRecord,
    TcUnderPressure,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Element sanity data                                                         #
# --------------------------------------------------------------------------- #

# Periodic-table symbols (1-2 letters). Source: IUPAC. Includes the lanthanoids
# and actinoids but NOT artificially extended element names.
_PERIODIC_TABLE: frozenset[str] = frozenset(
    {
        "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
        "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
        "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
        "Ga", "Ge", "As", "Se", "Br", "Kr",
        "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
        "In", "Sn", "Sb", "Te", "I", "Xe",
        "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy",
        "Ho", "Er", "Tm", "Yb", "Lu",
        "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
        "Po", "At", "Rn",
        "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf",
        "Es", "Fm", "Md", "No", "Lr",
        "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc",
        "Lv", "Ts", "Og",
    }
)

# Single-element superconductors allowed even though no element token is the
# host (LLM sometimes writes ``Hg-1223`` -> ``Hg`` alone is invalid).
_KNOWN_SINGLE_ELEMENT_SCS: frozenset[str] = frozenset(
    {"Pb", "Nb", "Al", "Sn", "Hg", "V", "Ta", "La", "In", "Tl"}
)

# Regex for a composition fragment: capital letter + optional lowercase
# letters + optional digits. We accept things like ``YBa2Cu3O7`` and
# ``FeSe0.5Te0.5``.
_ELEMENT_TOKEN_RE = re.compile(r"[A-Z][a-z]?\d*(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# Extractor                                                                   #
# --------------------------------------------------------------------------- #


class SuperconductingExtractor:
    """Orchestrates the 8-prompt LLM extraction pipeline."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def extract_all(
        self,
        text: str,
        doi: str | None = None,
    ) -> list[SuperconductingRecord]:
        """Run all 8 prompts and merge the result into :class:`SuperconductingRecord`s.

        Returns at least zero records; skips compositions that fail
        ``_is_valid_formula`` or ``_plausible_tc`` sanity checks or that
        fail ``has_meaningful_data``.
        """
        # --- 1. composition -------------------------------------------------
        comp_messages = build_composition_prompt(text, materials=None, doi=doi)
        comp_payloads = await self.client.extract_json(comp_messages)
        compositions: list[dict[str, Any]] = [
            p for p in comp_payloads if isinstance(p, dict)
        ]
        material_names: list[str] = [
            str(c.get("Material_Chemical_Composition", "")).strip()
            for c in compositions
            if c.get("Material_Chemical_Composition")
        ]
        material_names = [m for m in material_names if m]
        if not material_names:
            log.info("extract_all: no compositions found in paper")
            return []

        # --- 2. dimension prompts (parallel) --------------------------------
        builders = [
            build_base_sc_props_prompt,
            build_pressure_tc_prompt,
            build_isotope_uemura_prompt,
            build_specific_heat_prompt,
            build_fermi_resistivity_prompt,
            build_crystal_structure_prompt,
            build_preparation_prompt,
        ]
        messages_list = [
            builder(text, material_names, doi) for builder in builders
        ]
        # Use return_exceptions so one bad prompt doesn't kill the whole run.
        results = await asyncio.gather(
            *(self.client.extract_json(m) for m in messages_list),
            return_exceptions=True,
        )
        # Index results by dimension name.
        per_dim: dict[str, list[dict[str, Any]]] = {}
        for dim, result in zip(
            ["base_sc_props", "pressure_tc", "isotope_uemura", "specific_heat",
             "fermi_resistivity", "crystal_structure", "preparation"],
            results,
        ):
            if isinstance(result, Exception):
                log.warning("extract_all: %s prompt failed: %s", dim, result)
                per_dim[dim] = []
            else:
                per_dim[dim] = result

        # --- 3. merge into records -----------------------------------------
        records: list[SuperconductingRecord] = []
        # Index by composition for fast lookup during merge.
        comp_index: dict[str, SuperconductingRecord] = {}

        # Seed from composition payloads (carries Experimental flag + ensures
        # every composition we discovered is represented, even if all dimension
        # prompts returned empty).
        for comp in compositions:
            name = str(comp.get("Material_Chemical_Composition", "")).strip()
            if not name:
                continue
            if not _is_valid_formula(name):
                log.debug("extract_all: dropping invalid formula %r", name)
                continue
            record = SuperconductingRecord(
                material_chemical_composition=name,
                experimental=bool(comp.get("Experimental", True)),
                doi=doi,
                source="llm_extract",
            )
            comp_index[name] = record
            records.append(record)

        # Merge dimension results into the records (keyed by composition).
        _merge_base_props(comp_index, per_dim.get("base_sc_props", []))
        _merge_pressure_tc(comp_index, per_dim.get("pressure_tc", []))
        _merge_isotope(comp_index, per_dim.get("isotope_uemura", []))
        _merge_specific_heat(comp_index, per_dim.get("specific_heat", []))
        _merge_fermi(comp_index, per_dim.get("fermi_resistivity", []))
        _merge_structure(comp_index, per_dim.get("crystal_structure", []))
        _merge_preparation(comp_index, per_dim.get("preparation", []))

        # --- 4. sanity + inclusion filter -----------------------------------
        kept: list[SuperconductingRecord] = []
        for rec in records:
            tc = rec.superconducting_property.Tc
            tc_value = _coerce_float(getattr(tc, "value", None)) if tc else None
            if not _plausible_tc(rec.material_chemical_composition, tc_value):
                log.debug(
                    "extract_all: dropping implausible Tc=%s for %s",
                    tc_value,
                    rec.material_chemical_composition,
                )
                continue
            if not rec.has_meaningful_data():
                log.debug(
                    "extract_all: dropping empty record for %s",
                    rec.material_chemical_composition,
                )
                continue
            kept.append(rec)
        return kept


# --------------------------------------------------------------------------- #
# Sanity checks                                                               #
# --------------------------------------------------------------------------- #


# Generous upper bound on Tc. Highest reported ambient-pressure Tc (~135 K
# in Hg-1223 under pressure); well above that is implausible.
_MAX_TC_K = 200.0


def _plausible_tc(formula: str, tc: float | None) -> bool:
    """Return False if ``tc`` is implausible for ``formula``.

    Examples of what we reject:

    - ``Al`` with Tc = 100K (Al is a 1.2K superconductor).
    - ``Pb`` with Tc = 50K (Pb is 7.2K).
    - Anything with Tc > 200K (no known ambient-pressure SC above ~135K).

    Pass ``None`` through (no Tc reported).
    """
    if tc is None:
        return True
    if tc < 0 or tc > _MAX_TC_K:
        return False
    # Single-element SCs have well-known Tc values; reject obvious mismatches.
    element = _single_element(formula)
    if element is not None:
        # Allow ~5x headroom to catch dotted alloys and LLM-extract noise.
        max_single = _MAX_SINGLE_ELEMENT_TC.get(element, 200.0)
        if tc > max_single * 1.5:
            return False
    return True


# Max Tc (K) for known single-element SCs. Used to reject obvious garbage
# extractions.
_MAX_SINGLE_ELEMENT_TC: dict[str, float] = {
    "Pb": 7.2,
    "Nb": 9.3,
    "Al": 1.2,
    "Sn": 3.7,
    "Hg": 4.2,
    "V": 5.4,
    "Ta": 4.5,
    "La": 6.0,
    "In": 3.4,
    "Tl": 2.4,
}


def _single_element(formula: str) -> str | None:
    """Return the symbol if ``formula`` names a single known SC element."""
    stripped = formula.strip()
    if stripped in _KNOWN_SINGLE_ELEMENT_SCS:
        return stripped
    return None


def _is_valid_formula(formula: str) -> bool:
    """True iff ``formula`` decomposes into known element tokens.

    Accepts compositions with digits, decimal subscripts, parentheses, and
    dashes (e.g. ``YBa2Cu3O7``, ``FeSe0.5Te0.5``, ``Hg-1223``). All element
    tokens must be in the periodic table.
    """
    if not formula:
        return False
    if len(formula) > 200:
        return False
    # Strip non-element noise (parens, dashes, whitespace).
    cleaned = re.sub(r"[\s()_\-]", "", formula)
    if not cleaned:
        return False
    tokens = _ELEMENT_TOKEN_RE.findall(cleaned)
    if not tokens:
        return False
    # Every extracted element token must resolve to a known element symbol.
    for tok in tokens:
        # Tokens look like "Y" or "Ba2" or "Se0.5". Strip digits + dot.
        m = re.match(r"([A-Z][a-z]?)", tok)
        if not m:
            return False
        sym = m.group(1)
        if sym not in _PERIODIC_TABLE:
            return False
    return True


# --------------------------------------------------------------------------- #
# Per-dimension merge helpers                                                 #
# --------------------------------------------------------------------------- #


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion; returns None for ``"NA"`` / unparsable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() in {"NA", "N/A", "NULL", "NONE", "-"}:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    """Return ``None`` for missing / sentinel strings."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() in {"NA", "N/A", "NULL", "NONE", "-"}:
            return None
        return s
    return str(value)


def _make_qty(payload: Any) -> Quantity | None:
    """Build a ``Quantity`` from a payload like ``{"value": 92, "unit": "K"}``.

    Returns ``None`` if ``value`` is missing / sentinel.
    """
    if not isinstance(payload, dict):
        # The LLM might just emit a bare number for some fields.
        if isinstance(payload, (int, float)):
            return Quantity(value=float(payload), unit=None)
        if isinstance(payload, str):
            coerced = _coerce_float(payload)
            if coerced is None:
                return None
            return Quantity(value=coerced, unit=None)
        return None
    val = _coerce_float(payload.get("value"))
    if val is None:
        return None
    unit = payload.get("unit")
    unit_str = _coerce_str(unit)
    return Quantity(value=val, unit=unit_str)


def _merge_base_props(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        # Payloads may be either {"Material_Chemical_Composition": {...}} or
        # {"YBa2Cu3O7": {...}}. Try both keys.
        for comp_name, prop_payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(prop_payload, dict):
                continue
            sp = rec.superconducting_property
            _populate_sc_properties(sp, prop_payload)


def _populate_sc_properties(sp: SuperconductingProperty, payload: dict[str, Any]) -> None:
    """Map a payload to the ``SuperconductingProperty`` fields."""
    for llm_key, attr in (
        ("Tc", "Tc"),
        ("T_CDW", "T_CDW"),
        ("Hc1", "Hc1"),
        ("Hc2", "Hc2"),
        ("Energy_Gap", "Energy_Gap"),
        ("Coherence_Length", "Coherence_Length"),
        ("Penetration_Depth", "Penetration_Depth"),
        ("Critical_Current_Density", "Critical_Current_Density"),
        ("Pairing_Symmetry", "Pairing_Symmetry"),
        ("Type", "Type"),
        ("Mechanism", "Mechanism"),
        ("Superconductor_Family", "Superconductor_Family"),
    ):
        if llm_key not in payload:
            continue
        value = payload[llm_key]
        if attr in {"Pairing_Symmetry", "Type", "Mechanism", "Superconductor_Family"}:
            setattr(sp, attr, _coerce_str(value))
        else:
            setattr(sp, attr, _make_qty(value))


def _merge_pressure_tc(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            pressures_raw = payload.get("pressures") or []
            tcs_raw = payload.get("tcs") or []
            if not isinstance(pressures_raw, list) or not isinstance(tcs_raw, list):
                continue
            pressures = [float(x) for x in pressures_raw if _coerce_float(x) is not None]
            tcs = [float(x) for x in tcs_raw if _coerce_float(x) is not None]
            if not pressures or not tcs:
                continue
            method = _coerce_str(payload.get("method")) or ""
            rec.tc_under_pressure = TcUnderPressure(
                pressures=pressures, tcs=tcs, method=method
            )


def _merge_isotope(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            alpha = _coerce_float(payload.get("isotope_exponent_alpha"))
            if alpha is not None:
                rec.isotope_exponent_alpha = alpha
            uemura = _coerce_str(payload.get("uemura_class"))
            if uemura is not None:
                rec.uemura_class = uemura


def _merge_specific_heat(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            gamma_n = _make_qty(payload.get("gamma_n"))
            if gamma_n is not None:
                rec.gamma_n = gamma_n
            gamma_s = _make_qty(payload.get("gamma_s"))
            if gamma_s is not None:
                rec.gamma_s = gamma_s
            debye = _make_qty(payload.get("debye_temperature"))
            if debye is not None:
                rec.debye_temperature = debye


def _merge_fermi(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            tf = _make_qty(payload.get("fermi_temperature"))
            if tf is not None:
                rec.fermi_temperature = tf
            rho = _make_qty(payload.get("resistivity_normal"))
            if rho is not None:
                rec.resistivity_normal = rho


def _merge_structure(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            si = rec.structure_information
            si.crystal_structure = _coerce_str(payload.get("Crystal_Structure"))
            for axis, key in (("a", "Lattice_Parameter_a"),
                              ("b", "Lattice_Parameter_b"),
                              ("c", "Lattice_Parameter_c")):
                lp = _make_lp(payload.get(key))
                if lp is not None:
                    setattr(si, axis, lp)
            si.space_group = _coerce_str(payload.get("Space_Group"))


def _make_lp(payload: Any) -> LatticeParameter | None:
    if not isinstance(payload, dict):
        return None
    value = _coerce_float(payload.get("value"))
    if value is None:
        return None
    unit = _coerce_str(payload.get("unit"))
    return LatticeParameter(value=value, unit=unit)


def _merge_preparation(
    comp_index: dict[str, SuperconductingRecord],
    payloads: list[dict[str, Any]],
) -> None:
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for comp_name, payload in _iter_comp_entries(p):
            rec = comp_index.get(comp_name)
            if rec is None or not isinstance(payload, dict):
                continue
            pm = rec.preparation_method
            for llm_key, attr in (
                ("Synthesis_Method", "Synthesis_Method"),
                ("Chemical_Precursors", "Chemical_Precursors"),
                ("Synthesis_Atmosphere", "Synthesis_Atmosphere"),
                ("Annealing_Atmosphere", "Annealing_Atmosphere"),
                ("Cooling_Method", "Cooling_Method"),
                ("Substrate", "Substrate"),
                ("Additional_Details", "Additional_Details"),
            ):
                v = _coerce_str(payload.get(llm_key))
                if v is not None:
                    setattr(pm, attr, v)
            for llm_key, attr in (
                ("Synthesis_Temperature", "Synthesis_Temperature"),
                ("Synthesis_Pressure", "Synthesis_Pressure"),
                ("Synthesis_Time", "Synthesis_Time"),
                ("Heating_Rate", "Heating_Rate"),
                ("Sintering_Temperature", "Sintering_Temperature"),
                ("Sintering_Time", "Sintering_Time"),
                ("Annealing_Temperature", "Annealing_Temperature"),
                ("Annealing_Time", "Annealing_Time"),
            ):
                q = _make_qty(payload.get(llm_key))
                if q is not None:
                    setattr(pm, attr, q)


def _iter_comp_entries(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    """Yield ``(composition_name, sub_payload)`` from a dimension payload.

    LLM responses can be either:

    - ``{"YBa2Cu3O7": {...}, "MgB2": {...}}``  (object keyed by composition)
    - ``{"Material_Chemical_Composition": "YBa2Cu3O7", ...}``  (flat record)

    We detect the latter by looking for the canonical key.
    """
    if "Material_Chemical_Composition" in payload:
        return [(str(payload["Material_Chemical_Composition"]), payload)]
    out: list[tuple[str, Any]] = []
    for k, v in payload.items():
        if isinstance(v, dict):
            out.append((str(k), v))
    return out


# Re-export for tests.
__all__ = [
    "SuperconductingExtractor",
    "_is_valid_formula",
    "_plausible_tc",
]