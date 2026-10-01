"""LLM extraction prompts (spec §6.3, NEMAD-style 8-module pipeline).

Each prompt is a one-shot OpenAI-compatible chat with a system role that sets
the contract and a user role that asks for the JSON output. The eight prompts:

1. COMPOSITION         -- list of chemical formulas mentioned
2. BASE_SC_PROPS       -- Tc, T_CDW, Hc1, Hc2, Energy_Gap, etc.
3. PRESSURE_TC         -- tc_under_pressure curve
4. ISOTOPE_UEMURA      -- alpha + Uemura class
5. SPECIFIC_HEAT       -- gamma_n / gamma_s / Debye
6. FERMI_RESISTIVITY   -- T_F / rho_n
7. CRYSTAL_STRUCTURE   -- structure + lattice + space group
8. PREPARATION         -- synthesis recipe

Every system prompt enforces a strict-JSON contract; the user prompt embeds
the paper text + (optionally) the materials discovered in step 1. Each
``build_*_prompt`` function returns the messages list ready to send.
"""

from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# System prompts                                                              #
# --------------------------------------------------------------------------- #

COMPOSITION_SYSTEM_PROMPT: str = (
    "You are an expert materials scientist extracting superconductor "
    "compositions from a research paper.\n"
    "Return STRICT JSON (no prose, no Markdown fences). The output must be a "
    "JSON ARRAY of objects. Each object has exactly two keys:\n"
    "  - 'Material_Chemical_Composition' (string): canonical formula, e.g. "
    "'YBa2Cu3O7', 'MgB2', 'FeSe0.5Te0.5', 'Hg-1223'.\n"
    "  - 'Experimental' (boolean): true if the composition was measured "
    "experimentally, false if computed / predicted.\n"
    "Emit one object per distinct composition. Skip pure dopant variables "
    "unless they are part of the host composition. Do not invent compositions."
)

BASE_SC_PROPS_SYSTEM_PROMPT: str = (
    "You extract superconductor property measurements from a research paper. "
    "Return STRICT JSON (no prose, no commentary). The output is an OBJECT "
    "keyed by composition formula (Material_Chemical_Composition); each value "
    "is an object with optional keys: 'Tc', 'T_CDW', 'Hc1', 'Hc2', "
    "'Energy_Gap', 'Coherence_Length', 'Penetration_Depth', "
    "'Critical_Current_Density'. Each value is an OBJECT "
    "{'value': <number or 'NA'>, 'unit': <string>}. Use 'NA' for missing values. "
    "Use sensible units (K, T, meV, nm, A/cm^2). Never guess.\n\n"
    "IMPORTANT — extraction rules:\n"
    "1. Read the ENTIRE paper text including every table cell, figure "
    "   caption, and footnote. Tables are the richest source — extract "
    "   *every* numeric value you can attribute to the compound.\n"
    "2. If the paper states 'Tc = 39 K' for MgB2 in one paragraph and later "
    "   has a table giving 'Hc2(0) = 14 T', include BOTH.\n"
    "3. Look in experimental sections (Results, Discussion) and in summary "
    "   tables. Method sections (Sample preparation, Synthesis) also "
    "   yield data.\n"
    "4. Do NOT copy values from references/citations to OTHER papers — "
    "   those belong to the cited work, not to the paper being extracted.\n"
    "5. If a value truly is not in the paper (not just hard to find), use "
    "   'NA' — never fabricate.\n"
    "6. Numbers in tables sometimes lack a unit; infer from context "
    "   (e.g. column header 'T_c (K)' implies Kelvin)."
)

PRESSURE_TC_SYSTEM_PROMPT: str = (
    "You extract pressure-tuned Tc measurements from a superconductor paper. "
    "Return STRICT JSON. The output is an OBJECT keyed by composition; each "
    "value is {'pressures': [<number in GPa>], 'tcs': [<number in K>], "
    "'method': <resistivity | magnetization | susceptibility | other>}. "
    "Lists must be parallel. Use empty lists when no data."
)

ISOTOPE_UEMURA_SYSTEM_PROMPT: str = (
    "You extract isotope-effect and Uemura-classification data. Return STRICT "
    "JSON. The output is an OBJECT keyed by composition; each value is "
    "{'isotope_exponent_alpha': <number | 'NA'>, 'uemura_class': "
    "'weak-coupling' | 'strong-coupling' | 'very-strong' | 'NA'}. "
    "Use 'NA' for missing values."
)

SPECIFIC_HEAT_SYSTEM_PROMPT: str = (
    "You extract electronic specific-heat and Debye-temperature data. Return "
    "STRICT JSON. The output is an OBJECT keyed by composition; each value "
    "is {'gamma_n': {'value': <number | 'NA'>, 'unit': 'mJ/mol.K^2'}, "
    "'gamma_s': {'value': <number | 'NA'>, 'unit': 'mJ/mol.K^2'}, "
    "'debye_temperature': {'value': <number | 'NA'>, 'unit': 'K'}}. "
    "Use 'NA' for missing values; do not infer."
)

FERMI_RESISTIVITY_SYSTEM_PROMPT: str = (
    "You extract Fermi-temperature and normal-state resistivity. Return STRICT "
    "JSON. The output is an OBJECT keyed by composition; each value is "
    "{'fermi_temperature': {'value': <number | 'NA'>, 'unit': 'K'}, "
    "'resistivity_normal': {'value': <number | 'NA'>, 'unit': 'uOhm.cm'}}. "
    "Use 'NA' for missing values."
)

CRYSTAL_STRUCTURE_SYSTEM_PROMPT: str = (
    "You extract crystal-structure data. Return STRICT JSON. The output is an "
    "OBJECT keyed by composition; each value is "
    "{'Crystal_Structure': <string | 'NA'>, "
    "'Lattice_Parameter_a': {'value': <number | 'NA'>, 'unit': 'angstrom'}, "
    "'Lattice_Parameter_b': {'value': <number | 'NA'>, 'unit': 'angstrom'}, "
    "'Lattice_Parameter_c': {'value': <number | 'NA'>, 'unit': 'angstrom'}, "
    "'Space_Group': <string | 'NA'>}. Use 'NA' for missing values."
)

PREPARATION_SYSTEM_PROMPT: str = (
    "You extract synthesis/preparation metadata. Return STRICT JSON. The "
    "output is an OBJECT keyed by composition; each value is "
    "{'Synthesis_Method': <string | 'NA'>, "
    "'Chemical_Precursors': <string | 'NA'>, "
    "'Synthesis_Temperature': {'value': <number | 'NA'>, 'unit': 'C'}, "
    "'Synthesis_Pressure': {'value': <number | 'NA'>, 'unit': 'atm'}, "
    "'Synthesis_Atmosphere': <string | 'NA'>, "
    "'Synthesis_Time': {'value': <number | 'NA'>, 'unit': 'h'}, "
    "'Heating_Rate': {'value': <number | 'NA'>, 'unit': 'C/h'}, "
    "'Sintering_Temperature': {'value': <number | 'NA'>, 'unit': 'C'}, "
    "'Sintering_Time': {'value': <number | 'NA'>, 'unit': 'h'}, "
    "'Annealing_Temperature': {'value': <number | 'NA'>, 'unit': 'C'}, "
    "'Annealing_Time': {'value': <number | 'NA'>, 'unit': 'h'}, "
    "'Annealing_Atmosphere': <string | 'NA'>, "
    "'Cooling_Method': <string | 'NA'>, "
    "'Substrate': <string | 'NA'>, "
    "'Additional_Details': <string | 'NA'>}. Use 'NA' for missing values."
)


# --------------------------------------------------------------------------- #
# Builders                                                                    #
# --------------------------------------------------------------------------- #


def _truncate(text: str, *, max_chars: int = 60000) -> str:
    """Truncate paper text to avoid blowing past LLM context windows."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...truncated...]"


def _compositions_block(materials: list[str] | None) -> str:
    """Format the materials list as a hint for downstream prompts."""
    if not materials:
        return ""
    return "Materials in this paper:\n- " + "\n- ".join(materials)


def build_composition_prompt(
    text: str,
    materials: list[str] | None = None,  # noqa: ARG001 - included for parity
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 1: identify compositions."""
    user = (
        "Read the paper text and list every distinct superconductor composition "
        "it describes. For each, indicate whether the measurement was "
        "experimental or theoretical.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": COMPOSITION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_base_sc_props_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 2: base SC property measurements."""
    user = (
        "Extract the base superconductor measurements (Tc, T_CDW, Hc1, Hc2, "
        "Energy_Gap, Coherence_Length, Penetration_Depth, "
        "Critical_Current_Density) for every composition listed. If a value is "
        "missing, write 'NA'.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": BASE_SC_PROPS_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_pressure_tc_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 3: Tc(P) measurements."""
    user = (
        "Extract pressure-tuned Tc curves (parallel pressures in GPa and tcs "
        "in K arrays) for every composition. Method describes how Tc was "
        "determined.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": PRESSURE_TC_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_isotope_uemura_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 4: isotope + Uemura class."""
    user = (
        "Extract the isotope-effect exponent alpha and the Uemura "
        "classification for every composition. 'NA' for missing values.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": ISOTOPE_UEMURA_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_specific_heat_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 5: specific-heat + Debye."""
    user = (
        "Extract gamma_n (normal-state electronic specific heat coefficient), "
        "gamma_s (jump at Tc) and the Debye temperature for every composition. "
        "Use 'NA' for missing values.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": SPECIFIC_HEAT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_fermi_resistivity_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 6: Fermi temperature + normal-state resistivity."""
    user = (
        "Extract the Fermi temperature T_F (in K) and the normal-state "
        "resistivity (in uOhm.cm) for every composition. Use 'NA' when "
        "missing.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": FERMI_RESISTIVITY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_crystal_structure_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 7: crystal structure + lattice + space group."""
    user = (
        "Extract the crystal structure description, lattice constants (a, b, "
        "c in angstrom) and space group for every composition. Use 'NA' when "
        "missing.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": CRYSTAL_STRUCTURE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_preparation_prompt(
    text: str,
    materials: list[str] | None,
    doi: str | None = None,
) -> list[dict[str, str]]:
    """Prompt 8: synthesis / preparation recipe."""
    user = (
        "Extract synthesis / preparation recipe metadata for every "
        "composition. Use 'NA' when a field is missing.\n\n"
        f"DOI: {doi or '(unknown)'}\n\n"
        f"{_compositions_block(materials)}\n\n"
        f"Paper text:\n{_truncate(text)}"
    )
    return [
        {"role": "system", "content": PREPARATION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# Mapping of dimension → builder. Used by the extractor to fan out calls.
PROMPT_BUILDERS: dict[str, Any] = {
    "composition": build_composition_prompt,
    "base_sc_props": build_base_sc_props_prompt,
    "pressure_tc": build_pressure_tc_prompt,
    "isotope_uemura": build_isotope_uemura_prompt,
    "specific_heat": build_specific_heat_prompt,
    "fermi_resistivity": build_fermi_resistivity_prompt,
    "crystal_structure": build_crystal_structure_prompt,
    "preparation": build_preparation_prompt,
}