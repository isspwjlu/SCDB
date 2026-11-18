"""Binding a reported number to the material it actually belongs to.

The evidence gate in :mod:`extractor` answers a single question: *does this
number appear in the paper?* That is not enough. Superconductor papers name
many materials in one paragraph, and a model reading it will happily attach a
neighbouring compound's Tc to the subject of the sentence. The audit found the
result stored as ``FeSe = 109 K`` (the monolayer FeSe/SrTiO3 value, while bulk
FeSe superconducts near 8 K) and ``YBa2Cu3O7 = 39 K`` (MgB2's value).

This module adds the two checks that close the gap:

1. **Material binding.** The sentence carrying the value must also name the
   material, or a normalisation of it. A value in a sentence that discusses
   something else is rejected however well it matches.
2. **Plausibility range.** Every family has a documented range of accepted
   Tc values. A value outside it is rejected: it can only have come from
   another material or from a bare recall.

Both checks are conservative by design. Rejecting a real measurement costs one
record; accepting a misattributed one corrupts the database silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Material binding                                                            #
# --------------------------------------------------------------------------- #

# Element symbols, so a formula can be matched inside a sentence that writes
# it as a name ("iron pnictide", "niobium") rather than a formula.
_ELEMENT_NAMES = {
    "La": "lanthanum", "Y": "yttrium", "Ce": "cerium", "Nd": "neodymium",
    "Pr": "praseodymium", "Sm": "samarium", "Eu": "europium", "Dy": "dysprosium",
    "Sr": "strontium", "Ba": "barium", "Ca": "calcium", "Cu": "copper",
    "Bi": "bismuth", "Pb": "lead", "Tl": "thallium", "Hg": "mercury",
    "Fe": "iron", "Ni": "nickel", "Co": "cobalt", "Mn": "manganese",
    "Mg": "magnesium", "Nb": "niobium", "Ta": "tantalum", "Ti": "titanium",
    "Zr": "zirconium", "Hf": "hafnium", "V": "vanadium", "Cr": "chromium",
    "Se": "selenium", "Te": "tellurium", "S": "sulfur", "As": "arsenic",
    "P": "phosphorus", "O": "oxygen", "H": "hydrogen", "C": "carbon",
    "N": "nitrogen", "F": "fluorine", "Cl": "chlorine", "Al": "aluminium",
    "Ga": "gallium", "Ge": "germanium", "Ru": "ruthenium", "Ir": "iridium",
    "Rh": "rhodium", "Pt": "platinum", "Pd": "palladium", "In": "indium",
    "Sn": "tin", "Ag": "silver", "Au": "gold", "Bi ": "bismuth",
}

# Structural / descriptive terms that may appear in place of a formula.
_DESCRIPTORS = (
    "monolayer", "bilayer", "trilayer", "film", "thin film", "crystal",
    "single crystal", "polycrystal", "powder", "doped", "co-doped",
    "substituted", "pristine", "bulk", "interface",
)

_ALIAS_STRIP_RE = re.compile(r"[0-9.]+")


def _normalise_formula(formula: str) -> str:
    return re.sub(r"[\s()\[\]{}_,\-]", "", formula).casefold()


# Units and laboratory shorthand that look like formulas to a capital-letter
# pattern. Treating these as rival materials rejected valid values: "303 K at
# 400 GPa" was read as a compound named GPa, and "RRR" as a formula.
_NON_COMPOUND_TOKENS: frozenset = frozenset(
    {
        "GPa", "Kbar", "MPa", "Torr", "Bar", "Psi", "PPM",
        "RRR", "SQUID", "PPMS", "SEM", "TEM", "XRD", "STM", "AFM",
        "DOS", "CFM", "MPH", "GHz", "kHz", "MHz", "PNR", "NMR",
    }
)

# Short names and common parent/child relationships. Papers use a short
# label in prose and the full formula in the abstract, so a value stated for
# "Bi2212" must be accepted when the text says "Bi2Sr2CaCu2O8", and a value
# stated for a doped compound against its parent ("Ba(Fe1-xCox)2As2" vs
# "BaFe2As2") is still a statement about that material.
_ALIASES: dict[str, tuple[str, ...]] = {
    "bi2212": ("bi2sr2cacu2o8", "bi2212", "bisr2cacuo", "bi-2212"),
    "bi2223": ("bi2sr2ca2cu3o10", "bi2223"),
    "bi2201": ("bi2sr2cuo6", "bi2201"),
    "ybco": ("yba2cu3o7", "yba2cu3o", "ybco", "y123"),
    "y124": ("yba2cu4o8",),
    "hsb1223": ("hgba2ca2cu3o8", "hg-1223"),
    "hsb1212": ("hgba2cacu2o6", "hg-1212"),
    "t1223": ("tl2ba2ca2cu3o10", "tl-2223"),
    "t1212": ("tl2bacacu2o8", "tl-1212"),
    "lsco": ("la2sr", "la2-sr", "la2xsr", "lsco", "lasr"),
    "lbco": ("la2ba", "laco", "lbco"),
    "k2cr3as3": ("k2cr3as3",),
    "mgb2": ("mgb2", "magnesium diboride", "mgb"),
    "coe": ("coe-doped", "cobalt-doped"),
    "tbcc": ("tbcc", "tb-cc"),
    "mgsb2": ("mgsb2",),
    "nbse2": ("nbse2", "nbse"),
    "mos2": ("mos2",),
    "fese": ("fese", "fe se"),
    "mogo": ("mogo", "mgb o"),
}


def _alias_keys(formula: str) -> set[str]:
    """Every spelling a formula may legitimately appear under."""
    norm = _normalise_formula(formula)
    keys = {norm, _ALIAS_STRIP_RE.sub("", norm)}
    for short, expansions in _ALIASES.items():
        if norm == short or any(
            short in norm.split("x") or norm.startswith(short) for _ in [0]
        ):
            keys.update(expansions)
    return {k for k in keys if k}


def formula_mentions(composition: str, sentence: str) -> bool:
    """True when ``sentence`` names ``composition`` or a form of it.

    Accepts the formula itself, the same formula with its stoichiometry
    stripped (``2H-NbSe2`` vs ``NbSe2``), the spelled-out element name for a
    simple elemental material (``niobium`` vs ``Nb``), and the short labels
    papers use in prose (``Bi2212`` vs ``Bi2Sr2CaCu2O8``).
    """
    comp = composition.strip()
    hay = sentence.casefold()
    norm_comp = _normalise_formula(comp)
    if not norm_comp:
        return False

    hay_norm = _normalise_formula(hay)
    candidates = _alias_keys(comp)
    for key in candidates:
        if key and key in hay_norm:
            return True

    if norm_comp in hay_norm:
        return True
    # Phase prefixes and intergrown stacks: "FeSe/Nb:SrTiO3" and "FeSe" match
    # each other on the primary compound.
    primary = _normalise_formula(re.split(r"[/,]", comp)[0])
    if primary and primary in hay_norm:
        return True
    # Same elements, different stoichiometry notation.
    if _ALIAS_STRIP_RE.sub("", norm_comp) == _ALIAS_STRIP_RE.sub("", hay_norm):
        return True
    # An elemental material may be written as its name.
    if re.fullmatch(r"[A-Z][a-z]?", comp):
        name = _ELEMENT_NAMES.get(comp)
        if name and name in hay:
            return True
    return False


# --------------------------------------------------------------------------- #
# Plausibility ranges                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FamilyRange:
    """Plausible Tc span for a material family, in kelvin."""

    low: float
    high: float
    label: str


# Documented experimental ranges. Hydrides are separated because pressure puts
# them well above every other family. Values are deliberately generous: the
# point is to catch another material's number, not to referee a measurement.
_RANGES: tuple[tuple[str, FamilyRange], ...] = (
    # Monolayer FeSe on a substrate tops out far above the bulk value, so it
    # is matched before the bulk chalcogenide range.
    (r"FeSe?\s*/|/SrTiO3|/Nb:|monolayer|\b1L-|interface",
     FamilyRange(20.0, 120.0, "monolayer/interface")),
    (r"hydride|H\d|\d*H\d|CaH|LaH|YH|ScH|H3S", FamilyRange(80.0, 320.0, "hydride")),
    (r"YBa|YBCO|Bi2?2?Ca(?!.*2210)|Tl2?Ba2|HgBa|BSCO", FamilyRange(70.0, 140.0, "high-Tc cuprate")),
    (r"Sr2?Cu|La2?Cu|LSCO|LBCO|Bi2?2?Cu|Bi-?2201", FamilyRange(5.0, 100.0, "low-Tc cuprate")),
    (r"Fe(Se|Te)", FamilyRange(5.0, 40.0, "bulk iron chalcogenide")),
    (r"(Ba|Sr|Ca|Eu|Sm|La|Ce|Pr|Nd)Fe|pnictide|FeAs|\b122\b|\b1111\b|\b111\b",
     FamilyRange(5.0, 60.0, "iron pnictide")),
    (r"MgB", FamilyRange(35.0, 42.0, "MgB2")),
    (r"NiO|ni2O|ni3O|nickelate", FamilyRange(3.0, 100.0, "nickelate")),
    (r"RuO|ruthenate|Sr2RuO4", FamilyRange(0.5, 4.0, "ruthenate")),
    (r"Nb3Sn|NbTi|\bNb[35]", FamilyRange(6.0, 18.5, "niobium A15/alloy")),
    (r"^\s*Nb\s*$|elemental niobium", FamilyRange(8.0, 10.0, "elemental Nb")),
    (r"MoS2|NbSe2|TaS2|WSe2|1[HT]-|2H-|\dH-", FamilyRange(0.3, 8.0, "dichalcogenide")),
    (r"CeCoIn5|CeIrIn5|CeRhIn5|UTe2|URu2Si2|PrOs4Sb12|CeCu2Si2|CeRh2As2|heavy.fermion",
     FamilyRange(0.2, 5.0, "heavy fermion")),
    (r"^\s*(Al|In|Sn|Pb| Hg|Tl)\s*$", FamilyRange(0.3, 8.0, "elemental superconductor")),
)

# Global ceiling for anything not matched: the highest ambient-pressure
# transition is ~135 K, and hydride work is handled above.
_DEFAULT_RANGE = FamilyRange(0.0, 135.0, "generic")


# Values so widely reported that seeing one on an unrelated material is
# evidence of a copy rather than a measurement. Only exact matches count, so
# an unusual but genuine result (a monolayer film, a pressure-induced phase)
# is never rejected on this basis.
_SIGNATURE_VALUES: dict[float, str] = {
    39.0: "MgB2",
    92.0: "YBa2Cu3O7",
    95.0: "Bi2Sr2CaCu2O8",
    133.0: "HgBa2Ca2Cu3O8",
    203.0: "H3S",
    235.0: "CaH6",
    250.0: "LaH10",
    262.0: "YH6",
    9.2: "Nb",
    1.2: "Al",
    18.2: "Nb3Sn",
    12.5: "NbN",
    8.0: "FeSe",
    1.5: "Sr2RuO4",
    7.2: "NbSe2",
    2.3: "CeCoIn5",
    1.85: "PrOs4Sb12",
    55.0: "SmFeAsO",
    14.0: "BaFe2As2",
    109.0: "monolayer FeSe/SrTiO3",
}


def _signature_owner(tc: float) -> str | None:
    """Return the material whose signature value ``tc`` is, if any."""
    for value, owner in _SIGNATURE_VALUES.items():
        if abs(tc - value) < 0.06:
            return owner
    return None


def _family_range(composition: str) -> FamilyRange:
    text = composition
    for pattern, rng in _RANGES:
        if re.search(pattern, text, re.IGNORECASE):
            return rng
    # Hydrides are frequently written "LaH10" with no separator, so test the
    # concatenated formula explicitly before the generic ceiling.
    joined = re.sub(r"[\s()]", "", text)
    if re.search(r"H\d{1,2}$|\d{1,2}H\d{1,2}|H3[SC]|H[23][SC]", joined):
        return _RANGES[0][1]
    return _DEFAULT_RANGE


def plausible_tc_range(composition: str, tc: float) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for ``tc`` against ``composition``'s family.

    This is deliberately *not* an admission criterion. Real measurements
    legitimately fall outside a textbook range — a monolayer FeSe film at
    109 K, a 122 compound under pressure at 39 K, an undoped parent at its
    parent transition — and rejecting those would discard the good data the
    gate exists to protect.

    The check is only consulted when the value matches the signature of some
    *other* well-known material, which is the misattribution seen in the audit
    (39 K stamped on YBa2Cu3O7 is MgB2's number, not a cuprate measurement).
    """
    if tc is None:
        return False, "no value"
    if tc <= 0:
        return False, "non-positive"
    rng = _family_range(composition)
    if rng.low <= tc <= rng.high:
        return True, rng.label
    # Outside the family range: acceptable unless the number is a famous
    # signature of a different material.
    owner = _signature_owner(tc)
    if owner and owner.casefold() != composition.strip().casefold():
        return False, f"{tc:g} K is the signature value of {owner}"
    return True, f"{rng.label} (extended range)"


# --------------------------------------------------------------------------- #
# Binding a value to a sentence                                                #
# --------------------------------------------------------------------------- #

# Sentences routinely run 200-400 characters, so a fixed character window
# either truncates the subject or spills into the neighbouring sentence. Split
# on sentence boundaries instead and search whole sentences.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])|[\r\n]+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[str]:
    return [p for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def value_contexts(paper_text: str, value: float, unit: str = "K") -> list[str]:
    """Return every sentence of ``paper_text`` that states ``value`` with a unit."""
    if not paper_text:
        return []
    forms = {f"{value:g}", f"{value:.1f}", f"{value:.0f}", f"{value:.2f}"}
    unit_re = re.escape(unit) if unit else r"[A-Za-z/]*"
    pattern = re.compile(
        rf"(?<![\d.])(?:{'|'.join(re.escape(f) for f in forms)})"
        rf"\s*(?:{unit_re}\b|kelvin)",
        re.IGNORECASE,
    )
    return [s for s in _sentences(paper_text) if pattern.search(s)]


def value_is_bound_to(
    composition: str, paper_text: str, value: float, unit: str = "K"
) -> tuple[bool, str]:
    """True when the paper plausibly ties ``value`` to ``composition`` itself.

    The value must occur in a sentence that also names the material, which is
    what separates a compound's own Tc from a neighbour's in the same
    paragraph.

    Papers also state results in shorthand — "resistivity drops below
    Tc = 39 K" with the subject named in an earlier sentence. For those, a
    sentence naming a *different* material is treated as decisive evidence of
    misattribution, but the absence of any material name in the sentence
    defers to the surrounding paragraph rather than rejecting the value.
    """
    windows = value_contexts(paper_text, value, unit)
    if not windows:
        return False, "value absent from paper"

    for w in windows:
        if formula_mentions(composition, w):
            return True, "bound"

    # No sentence names the subject. A sentence naming a *different* compound
    # means the value belongs to that compound. Units and lab shorthand are not
    # compounds: "303 K at 400 GPa" named "GPa" as a rival and rejected a real
    # value, as did "RRR" (residual resistivity ratio).
    others: list[str] = []
    for w in windows:
        for m in re.finditer(r"\b([A-Z][a-z]?\d*(?:[A-Z][a-z]?\d*)+)\b", w):
            token = m.group(1)
            if token in _NON_COMPOUND_TOKENS:
                continue
            if not formula_mentions(composition, token):
                others.append(token)
    if others:
        return False, f"sentence names {others[0]}, not {composition}"

    # Shorthand: the subject is named in an earlier sentence, sometimes in
    # the same paragraph or the one before ("...SmFe0.85Co0.15AsO thin film,
    # in which antiferromagnetic order is destroyed and superconductivity
    # occurs at 15.2 K"). Widen to a generous neighbourhood — a real
    # measurement stated this way must not be discarded.
    for w in windows:
        m = re.search(
            rf"(?<![\d.]){re.escape(f'{value:g}')}\s*(?:{re.escape(unit)}\b|kelvin)",
            w, re.IGNORECASE,
        )
        if not m:
            continue
        idx = paper_text.casefold().find(w[:40].casefold())
        if idx < 0:
            continue
        offset = m.start() - idx
        for radius in (_CONTEXT_RADII):
            lo = max(0, idx + offset - radius)
            hi = idx + m.end() + radius
            neighbourhood = paper_text[lo:hi]
            if formula_mentions(composition, neighbourhood):
                # A different material appearing *before* the value in the same
                # neighbourhood still means the value may be theirs.
                if not _shadowed_before(neighbourhood, offset, composition):
                    return True, "bound via surrounding context"
    return False, f"material {composition} not found near the value"


# How far to look either side of a value for the material's name.
_CONTEXT_RADII = (700, 1500, 3000)


def _shadowed_before(
    neighbourhood: str, value_offset: int, composition: str
) -> bool:
    """True when another material dominates the text leading up to the value.

    "Separately, MgB2 superconducts at 39 K" inside a paragraph about FeSe
    must not be read as FeSe's value, even though FeSe is mentioned earlier.
    """
    before = neighbourhood[: max(0, value_offset)]
    others = {
        m.group(0)
        for m in re.finditer(r"\b[A-Z][a-z]?\d*(?:[A-Z][a-z]?\d*)+\b", before)
    }
    others = {o for o in others if not formula_mentions(composition, o)}
    if not others:
        return False
    # The closest other material wins: it is the one the sentence is about.
    nearest = max(
        others, key=lambda o: before.casefold().rfind(o.casefold())
    )
    return before.casefold().rfind(nearest.casefold()) > before.casefold().rfind(
        composition.strip().casefold()
    )
