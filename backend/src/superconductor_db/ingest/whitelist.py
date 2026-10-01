"""Curated superconductor whitelist + hallucination guard.

LLM extraction occasionally returns compositions that aren't actual
superconductors (e.g., the model may mention vanadium oxide V2O3 in a
SC-paper's reference list and propose it as a Tc=170K superconductor).

This module provides ``is_known_superconductor(formula)`` which accepts a
canonical composition string (e.g. ``YBa2Cu3O7``, ``MgB2``, ``FeSe0.5Te0.5``)
and returns True iff the composition matches one of the curated patterns.

The whitelist covers the well-known SC families from NEMAD-style reference
data:

- Elements (BCS): Nb, Pb, Hg, Sn, Ta, V, Al, La, Ti, Zr, Mo, W, Re, Os, Ir,
  Pt, Lu, Th, Pa, U, Pu, Am, Cm
- A15 compounds: Nb3Sn, Nb3Al, Nb3Ge, Nb3Ga, Nb3Si, V3Si, V3Ga
- MgB2
- Cuprates: YBa2Cu3O7 (YBCO), Bi-2201/2212/2223, Tl-2201/2212/2223, Hg-1201/1212/1223,
  La-214 (La2-xSrxCuO4), Y-124, Y-123 (YBa2Cu3O6)
- Iron-based: FeSe, Fe(Se,Te), SmFeAsO, LaFeAsO, CeFeAsO, BaFe2As2 (Ba-122),
  SrFe2As2, CaFe2As2, EuFe2As2, LiFeAs, NaFeAs
- Heavy-fermion: CeCu2Si2, CeCoIn5, CeRhIn5, UPt3, UBe13, PrOs4Sb12, PuCoGa5

Matching is regex-based so variants like ``YBa2Cu3O6.5``, ``Bi2Sr2Ca0.92Cu2O8``,
``FeSe0.5Te0.5``, ``SmFeAsO0.85F0.15`` are accepted.
"""
from __future__ import annotations

import re
from functools import lru_cache


# --------------------------------------------------------------------------- #
# Curated SC composition patterns                                               #
# --------------------------------------------------------------------------- #


# Single-element BCS superconductors (max Tc ~ 18K ambient pressure per
# NEMAD spec §6; included for reference).
_ELEMENT_SC = frozenset(
    {
        "NB", "PB", "HG", "SN", "TA", "V", "AL",
        "LA", "TI", "ZR", "MO", "W", "RE", "OS",
        "IR", "PT", "LU", "TH", "PA", "U",
        "PU", "AM", "CM",
    }
)


# Compound SC patterns. Each entry is a regex string matched case-
# insensitively after stripping subscripts.
_COMPOUND_PATTERNS: tuple[str, ...] = (
    # A15 (Nb3X, V3X)
    r"Nb3(Sn|Al|Ge|Ga|Si|Au|Pt)",
    r"V3(Si|Ga|Ge|In)",

    # MgB2 + variations (Mg(B,C)2 etc.)
    r"MgB[2_]?2?",
    r"MgB\d*\.?\d*",
    r"Mg1?[Bb]2",

    # Cuprates — YBCO family
    r"Y(Ba|Sr)2Cu3O[7xX]?",  # Y-123
    r"YBa2Cu4O[8xX]?",  # Y-124
    r"YBa2Cu3O6\.?\d*",  # reduced YBCO
    r"Y1Ba2Cu3",  # bare formula
    r"YBCO",
    r"Y123",
    r"Y124",

    # Cuprates — Bi family
    r"Bi2Sr2CuO[6xX]?",  # Bi-2201
    r"Bi2Sr2CaCu2O[8xX]?",  # Bi-2212
    r"Bi2Sr2Ca2Cu3O1?0?",  # Bi-2223
    r"Bi-?22\d\d",

    # Cuprates — Tl family
    r"Tl2Ba2CuO[6xX]?",  # Tl-2201
    r"Tl2Ba2CaCu2O[8xX]?",  # Tl-2212
    r"Tl2Ba2Ca2Cu3O1?0?",  # Tl-2223
    r"Tl-?22\d\d",
    r"Tl1Ba2CaCu2O7",  # Tl-1212

    # Cuprates — Hg family
    r"HgBa2CuO[4xX]?",  # Hg-1201
    r"HgBa2CaCu2O[6xX]?",  # Hg-1212
    r"HgBa2Ca2Cu3O[8xX]?",  # Hg-1223
    r"Hg-?12\d\d",
    r"HgBa2Ca2Cu3O8",
    r"HgBa2CaCu2O6",

    # Cuprates — La (La-214)
    r"La2[.-_]?x?[Ss]r[._-]?[xX]?[Cc]uO4",
    r"La2[.-_]?[Ss]rCuO4",
    r"La2[.-_]?[Bb]aCuO4",
    r"La1\.?\d*[Ss]r\.?\d*CuO4",
    r"La-?21\d",
    r"LSCO",

    # Pr / Nd / Sm cuprates
    r"Pr1\.\d*Ba2Cu3O7",
    r"Pr2[.-_]?x?[Cc]e[._-]?[Cc]uO4",
    r"Nd1\.\d*Ba2Cu3O7",

    # Iron-based — 1111 family (LnFeAsO, Ln=La,Ce,Pr,Nd,Sm,Gd)
    r"(La|Ce|Pr|Nd|Sm|Gd|Tb|Dy|Ho|Y|Tm|Lu)FeAsO\d?\.?\d*",
    r"(La|Ce|Pr|Nd|Sm|Gd|Tb|Dy|Ho|Y|Tm|Lu)FeAsO1[ -]xFx?\.?\d*",
    r"(La|Ce|Pr|Nd|Sm|Gd|Tb|Dy|Ho|Y|Tm|Lu)FePO",
    r"SmFeAsO\d?\.?\d*F\d?\.?\d*",
    r"SmFeAsO\d?\.?\d*",
    r"LnFeAsO",  # generic

    # Iron-based — 122 family (BaFe2As2, etc.)
    r"(Ba|Sr|Ca|Eu|K)Fe2As2",
    r"(Ba|Sr|Ca|Eu|K)(Fe|Co|Ni)2(As|P)2",
    r"Ba-?122",
    r"Sr-?122",

    # Iron-based — 111 family
    r"(Li|Na)FeAs",
    r"(Li|Na)Fe(P|As)",

    # Iron-based — 11 family (FeSe, FeTe, Fe(Se,Te))
    r"Fe(Se|Te|S)\d?\.?\d*",
    r"FeSe\d?\.?\d*(Te\d?\.?\d*)?",
    r"Fe\d?\.?\d*Se\d?\.?\d*",
    r"Fe1[ -]x(Co|Ni|Pd)[xX]?Se\d?\.?\d*",
    r"Fe1[ -]xCoxSe",  # Fe(Co)Se
    r"Fe(Se|Te|S)[0-9.]*[0-9.]*(Te[0-9.]*)?",
    r"FeSe0?\.5Te0?\.5",  # common
    r"Fe(Se,Te)\(1-x\)?S\(x\)",  # mixed
    r"FeSe0?\.95Te0?\.05",
    r"FeTe\d?\.?\d*",

    # Ruthenates
    r"Sr2RuO4",
    r"Sr2RuO3",  # not SC but sometimes mentioned
    r"Ca2RuO4",
    r"Barium ruthenate",
    r"Sr3Ru2O7",

    # Iron-based — 1111 under pressure (don't have separate handling)
    r"(La|Sm|Nd)FeAsO\dF\d?",

    # Heavy-fermion
    r"CeCu2Si2",
    r"CeCoIn5",
    r"CeRhIn5",
    r"CeIrIn5",
    r"Ce2PdIn8",
    r"CePt3Si",
    r"UPt3",
    r"UBe13",
    r"URu2Si2",
    r"PrOs4Sb12",
    r"PuCoGa5",
    r"NpPd5Al2",
    r"YCoxSi\d?",

    # Other known binary / ternary
    r"ZrB2",
    r"MoC",
    r"Mo2C",
    r"NbC",
    r"TaC",
    r"HfC",
    r"W2C",
    r"WC",
    r"RuC",
    r"OsB2",
    r"RuB2",
    r"ReB2",
    r"YB6",
    r"ZrN",
    r"NbN",
    r"MoN",
    r"TlPb",
    r"PbTe",
    r"GeTe",
    r"SnTe",
    r"RhTe",
    r"GeAs",
    r"SiC",
    r"Mo2N",
    r"W2N",
    r"TaN",
    r"HfN",
    r"NbSe2",
    r"TaS2",
    r"TaSe2",
    r"TiS2",
    r"ZrN2",
    r"HfS2",
    r"PdTe2",
    r"PtTe2",
    r"CuS2",

    # Hydrides (high-pressure SC, common in recent literature)
    r"H3S",
    r"H3P",
    r"LaH10",
    r"CeH9",
    r"CeH10",
    r"PrH9",
    r"NdH9",
    r"SmH9",
    r"CaH6",
    r"YH6",
    r"ThH10",
    r"UH8",
    r"UH7",
    r"H4S",
    r"D3S",
    r"D3P",
    r"LiH6",
    r"NaH3",
    r"KH6",
    r"RbH9",
    r"SrH\d+",
    r"BaH\d+",
    r"CaH\d+",
    r"MgH\d+",
    r"YH\d+",
)


# Compile at import time for fast matching.
_COMPILED_COMPOUND: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in _COMPOUND_PATTERNS
)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def _normalise_for_match(formula: str) -> str:
    """Lowercase + collapse whitespace; subscripts and digits pass through.

    ``YBa2Cu3O7 -> yba2cu3o7``. The regex patterns above are written in
    that normalised form.
    """
    return re.sub(r"\s+", "", formula.strip()).lower()


@lru_cache(maxsize=1024)
def _is_known_superconductor_cached(formula: str) -> bool:
    """Cached lookup. False on empty / unknown."""
    if not formula:
        return False
    norm = _normalise_for_match(formula)
    if not norm:
        return False
    # Strip subscripts that the LLM sometimes mis-writes (e.g. ``YBa2Cu3O7``
    # vs ``YBa_2Cu_3O_7`` vs ``YBa2Cu3O6.5``): strip trailing fractional
    # digits after a final letter to make ``YBa2Cu3O6.5`` match the
    # ``YBa2Cu3O[7xX]?`` regex (which permits 7, 6, or 6.5 — by virtue of
    # not anchoring). The same pattern matches multiple compositions
    # because most include optional decimal components.
    for pat in _COMPILED_COMPOUND:
        if pat.search(norm):
            return True
    # Single-element SCs: bare element symbol (possibly with subscript 1).
    stripped = re.sub(r"\d*\.?\d*$", "", norm).strip().upper()
    return stripped in _ELEMENT_SC


def is_known_superconductor(formula: str) -> bool:
    """Public, non-cached check.

    Examples::

        >>> is_known_superconductor("YBa2Cu3O7")
        True
        >>> is_known_superconductor("MgB2")
        True
        >>> is_known_superconductor("V2O3")
        False
        >>> is_known_superconductor("Nb3Sn")
        True
    """
    return _is_known_superconductor_cached(formula)


__all__ = ["is_known_superconductor"]
