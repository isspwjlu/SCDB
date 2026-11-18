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
import unicodedata
from contextvars import ContextVar
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
    condense_for_prompt,
)
from superconductor_db.ingest.families import classify_family
from superconductor_db.ingest.verifier import (
    formula_mentions,
    plausible_tc_range,
    value_contexts,
    value_is_bound_to,
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


# Maps each prompt builder to the dimension name used downstream. The set of
# builders varies per paper (see ``extract_all``), so the result list is
# zipped against the builders actually sent rather than a fixed name list.
_DIMENSION_NAMES: dict[Any, str] = {
    build_base_sc_props_prompt: "base_sc_props",
    build_pressure_tc_prompt: "pressure_tc",
    build_isotope_uemura_prompt: "isotope_uemura",
    build_specific_heat_prompt: "specific_heat",
    build_fermi_resistivity_prompt: "fermi_resistivity",
    build_crystal_structure_prompt: "crystal_structure",
    build_preparation_prompt: "preparation",
}

# A specialised dimension is only asked for when the paper plausibly covers
# it. In the audited corpus these three filled under 10% of rows while
# costing three of the eight model calls per paper.
_ISOTOPE_CUES = re.compile(
    r"isotope effect|isotope coefficient|isotope.*coefficient",
    re.IGNORECASE,
)
_FERMI_CUES = re.compile(
    r"fermi surface|de haas|quantum oscillation|car[e|é] density of states|"
    r"SCAS|spontaneous vortex",
    re.IGNORECASE,
)
_HEAT_CUES = re.compile(
    r"specific heat|heat capacity|C/?P.*temperature|"
    r"electronic entropy|entropy (?:per |of the )?(?:mole|formula)",
    re.IGNORECASE,
)


def _mentions(text: str, cues: re.Pattern[str]) -> bool:
    """True when ``text`` shows any of ``cues``.

    Only the opening slice is scanned: the cue usually appears in the title,
    abstract or conclusion, and scanning 30 k characters of extracted body for
    every paper costs more than the saved call is worth.
    """
    return bool(cues.search(text[:6000]))


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
        *,
        serial_prompts: bool = False,
    ) -> list[SuperconductingRecord]:
        """Run all 8 prompts and merge the result into :class:`SuperconductingRecord`s.

        Returns at least zero records; skips compositions that fail
        ``_is_valid_formula`` or ``_plausible_tc`` sanity checks or that
        fail ``has_meaningful_data``.

        When ``text`` is non-empty, every base-property value must be backed by
        an ``evidence`` quote that actually occurs in the paper
        (see :func:`_require_evidence`). Unbacked values — the model reciting
        well-known textbook numbers for a material it merely mentions — are
        dropped, which is what previously produced 30 identical MgB2 rows all
        carrying the same recalled ``Hc2=14 T``.

        ``serial_prompts`` sends the dimension prompts one at a time
        instead of fanning them out. It is slower, but a parallel burst
        gets rate-limited and a paper whose prompts fail is skipped whole.
        """
        # --- 1. composition -------------------------------------------------
        # The model reads a condensed version of the paper; the evidence
        # gate below still searches the full text. A reasoning model
        # thinks in proportion to what it reads, so feeding it the whole
        # paper costs several times the thinking -- and the time -- for
        # no more measurements.
        prompt_text = condense_for_prompt(text)
        comp_messages = build_composition_prompt(prompt_text, materials=None, doi=doi)
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
        # Every dimension used to be sent unconditionally: eight model calls
        # per paper, of which roughly two thirds are wasted. A 78-paper run
        # spent 384 calls on papers that yielded nothing, and the endpoint was
        # already returning 38% rate-limit errors — so the retries, not the
        # concurrency, set the pace.
        #
        # A specialised dimension is now only worth asking for when the paper
        # actually discusses its subject: isotope effect needs "isotope", Fermi
        # surface needs "Fermi surface" or "de Haas", and so on. The four
        # dimensions that fill reliably (base properties, pressure, crystal
        # structure, preparation) still always run.
        builders: list[Any] = [
            build_base_sc_props_prompt,
            build_pressure_tc_prompt,
            build_crystal_structure_prompt,
            build_preparation_prompt,
        ]
        if _mentions(text, _ISOTOPE_CUES):
            builders.append(build_isotope_uemura_prompt)
        if _mentions(text, _FERMI_CUES):
            builders.append(build_fermi_resistivity_prompt)
        if _mentions(text, _HEAT_CUES):
            builders.append(build_specific_heat_prompt)
        if len(builders) < 7:
            log.info(
                "extract_all: %d/%d dimension prompts sent (paper text lacks "
                "isotope/Fermi/heat content)",
                len(builders), 7,
            )

        messages_list = [
            builder(prompt_text, material_names, doi) for builder in builders
        ]
        if serial_prompts:
            # One prompt at a time. The endpoint rate-limits a burst of
            # parallel calls, and a paper whose prompts fail is skipped
            # whole -- so a serial fan-out trades speed for rows that
            # actually land.
            results = []
            for messages in messages_list:
                try:
                    results.append(await self.client.extract_json(messages))
                except Exception as exc:  # noqa: BLE001 - one prompt, one paper
                    results.append(exc)
        else:
            # Use return_exceptions so one bad prompt doesn't kill the whole run.
            results = await asyncio.gather(
                *(self.client.extract_json(m) for m in messages_list),
                return_exceptions=True,
            )
        # Index results by dimension name.
        per_dim: dict[str, list[dict[str, Any]]] = {}
        sent_dims = [_DIMENSION_NAMES[b] for b in builders]
        for dim, result in zip(sent_dims, results):
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
        # The base-props merge is evidence-gated: see _require_evidence.
        _merge_base_props(
            comp_index, per_dim.get("base_sc_props", []), paper_text=text
        )
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


# Upper bound on Tc for ordinary (non-hydride) superconductors. The highest
# ambient-pressure Tc is ~135 K (Hg-1223 under pressure); hydrides are handled
# separately because they reach far higher under extreme pressure.
_MAX_TC_K = 200.0

# High-pressure hydrides break the 200 K ceiling: H3S reaches 203 K and the
# ternary/quaternary hydrides (LaH10, YH6, CeH10, Li2CaH16 ...) reach
# 240-290 K near 150-250 GPa. A flat 200 K cap silently discarded every one of
# them, which is why hydride papers produced extracted=1 but stored=0.
_MAX_HYDRIDE_TC_K = 300.0

# A formula is treated as a hydride when hydrogen is part of the composition,
# at either end: metal hydrides end in Hn (LaH10, Li2CaH16) while the
# high-pressure sulfur/selenium hydrides lead with H or its isotope D
# (H3S, D3S, H3Se).
_HYDRIDE_TAIL_RE = re.compile(r"H\d*(?:\.\d+)?$")
_HYDRIDE_HEAD_RE = re.compile(r"^[HD]\d*(?:\.\d+)?")


def _is_hydride(formula: str) -> bool:
    """True for hydrides like ``LaH10``, ``H3S``, ``D3S``, ``Li2CaH16``."""
    cleaned = re.sub(r"[\s()_-]", "", formula.strip())
    if not cleaned:
        return False
    return bool(_HYDRIDE_TAIL_RE.search(cleaned) or _HYDRIDE_HEAD_RE.match(cleaned))


def _max_tc_for(formula: str) -> float:
    """Plausible Tc ceiling for ``formula`` given its family."""
    return _MAX_HYDRIDE_TC_K if _is_hydride(formula) else _MAX_TC_K


def _plausible_tc(formula: str, tc: float | None) -> bool:
    """Return False if ``tc`` is implausible for ``formula``.

    Examples of what we reject:

    - ``Al`` with Tc = 100K (Al is a 1.2K superconductor).
    - ``Pb`` with Tc = 50K (Pb is 7.2K).
    - ``YBa2Cu3O7`` with Tc = 400K (no non-hydride SC is anywhere near that).
    - ``LaH10`` with Tc = 350K (hydrides top out around 280-290 K).

    Hydrides get a higher ceiling than the rest — see ``_MAX_HYDRIDE_TC_K``.

    Pass ``None`` through (no Tc reported).
    """
    if tc is None:
        return True
    if tc < 0 or tc > _max_tc_for(formula):
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


def _string_field(value: Any) -> str | None:
    """The string of a field the model sent in the standard object shape.

    The prompt asks for ``{'value': ..., 'evidence': ...}`` for every
    field, so a label like the pairing symmetry arrives wrapped in that
    object. Taking ``str(value)`` of it would store the whole dict as
    text.
    """
    if isinstance(value, dict):
        value = value.get("value")
    return _coerce_str(value)


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
    paper_text: str = "",
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
            _populate_sc_properties(
                sp, prop_payload, paper_text=paper_text, composition=comp_name
            )


def _populate_sc_properties(
    sp: SuperconductingProperty,
    payload: dict[str, Any],
    paper_text: str = "",
    composition: str = "",
) -> None:
    """Map a payload to the ``SuperconductingProperty`` fields.

    When ``paper_text`` is supplied, each numeric value must be accompanied by
    an ``evidence`` quote that occurs in the paper. Values that fail the check
    are dropped so the model cannot substitute recalled textbook numbers for
    measurements it did not read.
    """
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
    ):
        if llm_key not in payload:
            continue
        value = payload[llm_key]
        if attr == "Type":
            # A coarse label (conventional / unconventional); the evidence
            # gate is for numbers.
            setattr(sp, attr, _coerce_str(value))
            continue
        evidence = _verified_evidence(value, paper_text)
        if evidence is None:
            log.info(
                "extract_all: dropping %s for %s — no verbatim evidence "
                "found in the paper",
                attr, composition or "?",
            )
            continue
        if attr in {"Pairing_Symmetry", "Mechanism"}:
            # A short label rather than a number, but it is still a claim
            # about this compound, so it needs the same quote.
            setattr(sp, attr, _string_field(value))
            continue
        qty = _make_qty(value)
        # Two further checks specific to Tc, which is the field that
        # decides whether the row is worth keeping at all.
        if attr == "Tc" and qty is not None and composition:
            if not _tc_is_theirs(composition, paper_text, qty):
                continue
            quote = value.get("evidence")
            if isinstance(quote, str) and quote.strip():
                _record_evidence(composition, attr, quote.strip())
        setattr(sp, attr, qty)

    # Family is derived from the formula, not taken from the model. It must
    # sit outside the loop above: the model omits or NA-fills the key often,
    # and when it did the dashboard showed a blank family for every row
    # ingested after the classifier was written.
    if composition and not sp.Superconductor_Family:
        sp.Superconductor_Family = classify_family(composition)


# Evidence captured during extraction, keyed by composition then field. The
# ingest layer copies these into ``materials.notes`` so a stored Tc always
# carries the sentence it came from.
#
# Held in a ContextVar rather than a module global: several papers are
# ingested concurrently, and a shared dict would leak one paper's evidence
# quotes into another's records.
_EVIDENCE: ContextVar[dict[str, dict[str, str]]] = ContextVar(
    "scdb_tc_evidence", default={}
)


def _record_evidence(composition: str | None, field: str, quote: str) -> None:
    if not composition:
        return
    # Copy-on-write: the default dict must never be mutated in place.
    current = dict(_EVIDENCE.get())
    current.setdefault(composition, {})[field] = quote
    _EVIDENCE.set(current)


def evidence_for(composition: str) -> dict[str, str]:
    """Return the evidence quotes captured for ``composition``."""
    return dict(_EVIDENCE.get().get(composition, {}))


def clear_evidence() -> None:
    """Reset the per-run evidence registry."""
    _EVIDENCE.set({})


# Phrasing that marks a Tc as a *prediction* rather than a measurement.
# Superconductor papers routinely quote both in the same sentence ("a
# predicted Tc of 250 K, experimentally we observe 230 K"), and the extractor
# was storing the headline theoretical number as if it had been measured --
# e.g. LaH10 at 288 K, which is the Ashcroft prediction, not an observation.
_THEORY_MARKERS: tuple[str, ...] = (
    "predict", "predicted", "prediction", "theoretical", "theoretically",
    "calculated", "calculation", "computed", "computation", "estimated",
    "expected to", "suggested to", "proposed", "should reach",
    "no superconductivity", "above the", "exceeds the",
    # Counterfactual and conditional phrasing: "a Tc value as high as 86 K
    # would be expected if the ratio were 5.5" is an inference, not a
    # measurement.
    "would be expected", "would be obtained", "should be", "would reach",
    "can be estimated", "is expected", "were expected", "would correspond",
    "if the ratio", "in the case of", "assuming", "under the assumption",
    # Indirect announcement of a derived result. The sentence that carries
    # the number often omits any theory word: "we have therefore used the
    # Eliashberg equation to predict superconductivity ... The resulting Tc
    # values are 276 K". "resulting" here refers back to a calculation, so
    # these verbs only count as theory when the sentence offers no
    # measurement wording — which is handled by precedence in
    # ``classify_tc_evidence``.
    "resulting tc", "resulting critical", "resulting values",
    "values are", "we obtain", "we have obtained", "gives",
    # DFT / electron-phonon-calculation vocabulary. Production stored
    # "For strain-free 2H-Mo2N, the EPC constant (λ) and Tc are 1.3 and
    # 22.7 K, respectively" as a measurement — a computed pairing with the
    # coupling constant, exactly the LaH10-288K failure class. Measurement
    # markers are checked first in ``classify_tc_evidence``, so a sentence
    # that also reports a measurement still classifies as experimental.
    "epc constant", "electron-phonon coupling", "electron–phonon coupling",
    "electron phonon coupling", "eliashberg", "mcmillan", "mc millan",
    "dft", "density functional", "first-principles", "first principles",
    "ab initio", "ab-initio",
)


_MEASUREMENT_MARKERS: tuple[str, ...] = (
    "we observe", "we observed", "we measured", "we report", "we find",
    "our measurement", "measured", "observed", "resistivity drops",
    "zero resistance", "onset of superconductivity", "onset temperature",
    "diamagnetic", "magnetic shielding", "is superconducting at",
    "superconducting below", "transition at", "resistivity measurement",
    "we find that", "we show that",
    # Observation-reporting phrasing that carried no verb marker: production
    # stored La3Ni2O7's 80 K from "Signatures of superconductivity near 80 K
    # in a nickelate under high pressure" as unclassified, losing the
    # experimental flag. Same class: discovery/observation announcements.
    "signatures of superconductivity", "evidence of superconductivity",
    "discovery of superconductivity", "discovered superconductivity",
    "found to be superconducting", "resistance drop",
)


def classify_tc_evidence(quote: str) -> str:
    """Classify a Tc quote as ``"experimental"``, ``"theoretical"`` or ``""``.

    Returns an empty string when the wording gives no signal, in which case
    the LLM's own ``Experimental`` flag stands.
    """
    lowered = quote.casefold()
    if any(marker in lowered for marker in _MEASUREMENT_MARKERS):
        return "experimental"
    if any(marker in lowered for marker in _THEORY_MARKERS):
        return "theoretical"
    return ""


def _verified_evidence(value: Any, paper_text: str) -> str | None:
    """Return the evidence quote when it is backed by the paper, else ``None``.

    Same acceptance rule as before, but the accepted quote is returned instead
    of discarded so it can be stored alongside the value.
    """
    if not paper_text:
        return ""
    if not isinstance(value, dict):
        return None
    evidence = value.get("evidence")
    if not isinstance(evidence, str):
        return None
    quote = _normalise_quote(evidence)
    if not quote or quote in _MISSING_EVIDENCE_SENTINELS:
        return None
    haystack = _normalise_quote(paper_text)
    # A short quote carries too little context to confirm by substring alone:
    # "NA" or "Tc 200 K" would match any superconductivity paper that happens
    # to print that number, which is exactly the textbook-recall failure the
    # gate exists to stop. Short quotes must clear the stricter word-level
    # check in ``_numeric_fragment_supported``.
    if len(quote) >= _MIN_EVIDENCE_CHARS:
        if quote in haystack:
            return evidence.strip()
        # Second pass: compare with all whitespace removed. Extracted PDF
        # text breaks words across lines and injects stray characters
        # mid-phrase, so even after hyphen repair the token boundaries can
        # differ from the model's clean quote.
        squashed = quote.replace(" ", "")
        if squashed and squashed in haystack.replace(" ", ""):
            return evidence.strip()
    if _numeric_fragment_supported(quote, haystack):
        return evidence.strip()
    return None


def _tc_is_theirs(composition: str, paper_text: str, qty: Any) -> bool:
    """True when a reported Tc is this material's own *measurement*.

    This replaced a five-gate chain (range check, signature-value check,
    sentence binding, paragraph context, shadowing) that cut the corpus from
    500 rows to 64 while still letting a reference-list title through. The
    chain's real failure was in the gates that rested on a model of expected
    Tc per family, which is exactly the kind of knowledge the ingest loop is
    supposed to avoid importing.

    Two conditions remain, both checked against the paper itself:

    1. The value must occur in a sentence that names the material (directly,
       by alias, or through the surrounding text). This is what keeps
       ``YBa2Cu3O7 = 39 K`` — MgB2's number — out.
    2. That sentence must not read as a calculation. This is what keeps
       LaH10 at 288 K (a DFT prediction quoted as a headline) out.

    A value that fails is dropped rather than corrected, and the reason is
    logged so the decision can be audited against the source.
    """
    value = getattr(qty, "value", None)
    if value is None or not paper_text:
        return True

    bound, why = value_is_bound_to(composition, paper_text, float(value), "K")
    if not bound:
        log.info(
            "extract_all: dropping Tc for %s — %.4g K is not stated for this "
            "material (%s)",
            composition, value, why,
        )
        return False

    for sentence in value_contexts(paper_text, float(value), "K"):
        if not formula_mentions(composition, sentence):
            continue
        if classify_tc_evidence(sentence) == "theoretical":
            log.info(
                "extract_all: dropping Tc for %s — %.4g K is a calculation: %s",
                composition, value, sentence[:90],
            )
            return False
    return True


def _has_supporting_evidence(value: Any, paper_text: str) -> bool:
    """True when ``value`` is backed by text that really occurs in ``paper_text``.

    A quote passes on either of two grounds:

    1. **Direct quote** — the normalised evidence string appears verbatim in
       the normalised paper. This is the common case for prose results.
    2. **Numeric context** — the evidence string is itself short and numeric
       (typically a table row reconstructed from the PDF, e.g. ``"Tc 39 K"``
       or ``"0.92 T"``), and the *significant* words of that fragment appear
       in the paper. This keeps table-sourced measurements while still
       refusing a bare textbook number, which has no accompanying words to
       match.

    The gate is skipped when no paper text is available, so unit tests and
    callers that pass a pre-truncated excerpt keep working.
    """
    if not paper_text:
        return True
    return _verified_evidence(value, paper_text) is not None


# A fragment at least this long is specific enough to stand on its own as a
# direct quote. Below it we require the numeric-context path.
_MIN_EVIDENCE_CHARS = 24

# Values the model emits for "the paper did not state this".
_MISSING_EVIDENCE_SENTINELS = frozenset({"na", "n a", "none", "null", "unknown"})

# Numeric fragments (table rows) up to this length are accepted via word match.
_MAX_NUMERIC_FRAGMENT_CHARS = 80

# Below this many words a fragment carries too little context to trust.
_MIN_FRAGMENT_WORDS = 2

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "in", "at", "to", "is", "are", "was", "were",
        "for", "on", "and", "or", "by", "with", "from", "as", "we", "this",
        "that", "it", "be", "has", "have", "had", "not", "no", "which",
        "k", "t", "kev", "mev", "gpa", "pa", "mpa", "ev", "kbar", "bar",
    }
)


def _numeric_fragment_supported(quote: str, haystack: str) -> bool:
    """True when a short numeric fragment's significant words occur in the paper.

    This is the fallback for quotes too short to confirm by substring. It
    requires the fragment to carry at least one *word* beyond the numeric
    tokens and units, and every meaningful token to be present in the paper.
    A bare ``"39 K"`` or ``"Tc 39 K"`` therefore fails (no prose word), while
    a reconstructed table row such as ``"MgB2 Tc 39 K measured 0.92 T"``
    passes only if those words really are in the paper.
    """
    if len(quote) > _MAX_NUMERIC_FRAGMENT_CHARS:
        return False
    tokens = [t for t in quote.split() if t and t not in _STOPWORDS]
    if len(tokens) < _MIN_FRAGMENT_WORDS:
        return False
    # Require at least one prose word: numbers, units and quantity symbols
    # (e.g. "Tc 200 K") are the hallmark of a recalled textbook value rather
    # than a quoted measurement, so they are not enough on their own.
    prose = [t for t in tokens if not _NUMERIC_TOKEN_RE.match(t) and t not in _SYMBOL_TOKENS]
    if not prose:
        return False
    hits = sum(1 for t in tokens if t in haystack)
    # Every meaningful word must appear; a table row the LLM assembled will,
    # a textbook value invented wholesale will not.
    return hits == len(tokens)


_NON_ALNUM_RE = re.compile(r"[^a-z0-9.]+")
_WHITESPACE_RE = re.compile(r"\s+")

# Purely numeric tokens (optionally signed / decimal) carry no textual context.
_NUMERIC_TOKEN_RE = re.compile(r"^[<>~±+\-]?\d+(?:\.\d+)?$")

# Quantity symbols: they name *what* was measured but say nothing about this
# particular paper, so "Tc 200 K" is not evidence of anything.
_SYMBOL_TOKENS = frozenset(
    {
        "tc", "tcdw", "hc", "hc1", "hc2", "gap", "egap", "energygap",
        "coherence", "length", "penetration", "depth", "critical", "current",
        "density", "j", "alpha", "gamma", "n", "s", "debye", "tf", "fermi",
        "rho", "a", "b", "c", "lattice", "p", "t",
    }
)

# PDF line-wrapping hyphenates words across a column break ("cova-lent",
# "Fi-nally"). A naive punctuation strip turns those into two tokens and the
# paper text can then never match a correctly-spelled quote, so genuine
# measurements get rejected by the evidence gate. Joining them first restores
# the original word.
_HYPHEN_BREAK_RE = re.compile(r"(?<=[a-z0-9])-\s+(?=[a-z0-9])")
# Ligatures and long-s that OCR/PDF extraction renders inconsistently.
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi"})


def _normalise_quote(text: str) -> str:
    """Normalise text so a quote from the model matches the extracted PDF text.

    Handles, in order: compatibility folding, ligatures, unicode
    dashes/minus, line-break hyphenation, casefolding, and
    punctuation/whitespace collapsing. Each step reflects a real artefact
    observed in extracted superconductor PDFs — without them the evidence
    gate rejects real measurements.

    The compatibility fold matters most after the switch to docling: docling
    renders mathematics in Unicode italic ("𝑇𝑐", "𝐾"), while the model
    writes the plain letters ("Tc", "K"). Without folding, the italic
    letters are stripped as punctuation and the quote can never match.
    """
    folded = unicodedata.normalize("NFKC", text)
    lowered = folded.translate(_LIGATURES).casefold()
    lowered = lowered.replace("−", "-").replace("–", "-").replace("—", "-")
    # Rejoin words split across a line/column break.
    lowered = _HYPHEN_BREAK_RE.sub("", lowered)
    return _WHITESPACE_RE.sub(" ", _NON_ALNUM_RE.sub(" ", lowered)).strip()


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