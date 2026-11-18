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
    # A15 (Nb3X, V3X). Cr3Si and Ti3Au are established A15 superconductors
    # that were missing here and were being rejected as hallucinations.
    r"Nb3(Sn|Al|Ge|Ga|Si|Au|Pt)",
    r"V3(Si|Ga|Ge|In|Au)",
    r"Cr3(Si|Ge|As|Al)",
    r"Ti3(Au|Sb|In|Si|Ge)",
    r"Mo3(Si|Au|Ge|Ir|Pb)",
    r"W3(Si|Ge)",
    # Layered dichalcogenide / moebius superconductors.
    r"1H-TiSe2",
    r"1H-NbSe2",
    r"1T-TiSe2",
    r"2H-NbSe2",
    r"1H-TaS2",

    # ---- Binary/alloy superconductors (industrial + classical) ----
    # Nb-Ti is the workhorse MRI/LHC wire; Nb-Ti-Sn the high-field ternary.
    # Matched loosely because papers write them as "NbTi", "Nb-Ti", "Nb0.62Ti"
    # and in alloy notation.
    r"Nb\d*\.?\d*[-_]?Ti\d*\.?\d*",
    r"Nb[-_]?Ti[-_]?Sn",
    r"Nb[-_]?Ti[-_]?Ge",
    r"Nb[-_]?Sn[-_]?Ti",
    r"Ti[-_]?Nb",
    r"V3[ -]?Ga",          # V3Ga with a space/underscore
    r"Ti2[ -]?O3",         # Ti2O3 structural transition
    r"Ni3[ -]?Al",          # L1_2 ordered Ni3Al
    r"Ni3[ -]?Ga",
    # ---- Perovskite / doped-oxide superconductors ----
    # LSCO and LBCO written with an explicit substitution variable, e.g.
    # "La2-xSrxCuO4", "La0.7Ca0.3CuO4", "La2-xBaxCuO4".
    r"La[0-9.]*[-–_ ]?[xX][-–_ ]?([Ss]r|[Cc]a|[Bb]a)[0-9.]*[-–_ ]?[xX]?[0-9.]*CuO4",
    r"La[0-9.]*([-–_ ]?[Ss]r|[Cc]a|[Bb]a)[0-9.]*CuO4",
    r"La[0-9.]*CuO[34]",
    r"L(S|B)CO",
    # ---- Double-perovskite / 327 superconductors ----
    # Ba2YRuO6 / Sr2YRuO6 / Sr2LuRuO6 family (Os/Ru ordered perovskites).
    r"(Ba|Sr)2(Lu|Y)RuO6",
    r"(Ba|Sr)2(Lu|Y)(Ru|Ir)O6",
    # ---- Fulleride superconductors (A3C60) ----
    r"(K|Rb|Cs)[0-9.]*[-–_ ]?[xX]?[0-9.]*C60",
    r"A3C60",
    r"BC60",
    # ---- Additional uranium / heavy-fermion superconductors ----
    r"U(Co|Ge|Cu|Be|Ru|Ni|Pd|Th|Si)[0-9.]*",  # UCoGe, UBe13, UCu2Si2 ...
    r"(Ce|La|Pr|Nd|Pm|Sm|Eu|Gd)Co(In|Pt)5",
    r"(Ce|La|Nd)RhIn5",
    r"(Ce|La)(Cu|Rh|In)[0-9.]*(Si|Ge)2",  # CeCu2Si2, CeRh2Si2
    r"PuCoGa5",
    r"UTe2",
    r"URu2Si2",

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
    # Doped Tl-12xx written with site fractions, e.g. Tl1.9Cr0.1Ba2CaCu2O8.
    r"Tl[0-9.]*[A-Za-z]{0,2}[0-9.]*Ba2Ca[0-9.]*Cu[23]O",

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
    # 1111s with the Fe site substituted: SmFe0.85Co0.15AsO, LaFe0.9Ni0.1AsO.
    r"(La|Ce|Pr|Nd|Sm|Gd|Tb|Dy|Ho|Er|Y|Tm|Lu)Fe[0-9.]*[-–_ ]?[xX][-–_ ]?[0-9.]*(Co|Ni|Cu|Mn)[0-9.]*[-–_ ]?[xX]?[0-9.]*(As|P)O",
    r"(La|Ce|Pr|Nd|Sm|Gd|Tb|Dy|Ho|Er|Y|Tm|Lu)Fe[0-9.]*(Co|Ni|Cu|Mn)[0-9.]*(As|P)O",
    # SrFeAsO and the La/Sr/Ca 1111s the pattern above did not cover.
    r"(La|Sr|Ca)Fe(As|P)O",
    r"(La,Pr|Sr,Ca|Ba,Sr)\(?([A-Z][a-z]?)?\)?Fe(As|P)O",
    r"\(\s*(La|Pr|Nd|Sm|Ce|Sr)[^)]*\)\s*Fe(As|P)O",

    # Iron-based — 122 family (BaFe2As2, etc.)
    r"(Ba|Sr|Ca|Eu|K)Fe2As2",
    r"(Ba|Sr|Ca|Eu|K)(Fe|Co|Ni)2(As|P)2",
    r"Ba-?122",
    r"Sr-?122",
    # Doped 122s are usually written with the dopant and its amount, e.g.
    # SrFe2-xCoxAs2, Ba1-xKxFe2As2, Ca1-xSrxFe2As2, BaFe2(As1-xPx)2. Without
    # these the whitelist rejected the entire most-studied iron family.
    r"(Ba|Sr|Ca|Eu)[0-9.]*(?:[-–_ ]?xX?[-–_ ]?[KNaCaSr][0-9.]*)?[-–_ ]?[xX]?[-–_ ]?Fe2(As|P)2",
    r"(Ba|Sr|Ca|Eu)Fe2[-–_ ]?[xX][-–_ ]?(Co|Ni|Pd|Mn|Cu)[xX]?[0-9.]*(As|P)2",
    # Same family written with an explicit substitution amount and no "x",
    # e.g. BaFe1.8Co0.2As2, SrFe0.9Ni0.1AsO.
    r"(Ba|Sr|Ca|Eu)Fe[0-9.]+(Co|Ni|Cu|Mn|Zn)[0-9.]*(As|P)[0-9]*O?",
    r"(Ba|Sr|Ca|Eu)Fe2\((As|P|Se)[0-9.]*[-–_ ]?[xX][-–_ ]?[xX]?[0-9.]*(P|As|Sb|Ge|Se)[0-9.]*\)2",
    r"(Ba|Sr|Ca|Eu)Fe2\((As|P|Se)[0-9.]*\)2",
    # Anion solid solutions written with a substitution variable inside the
    # parentheses, e.g. BaFe2(As1-xPx)2, BaFe2(Se1-xSx)2.
    r"(Ba|Sr|Ca|Eu)Fe2\([A-Za-z]+[^)]{0,24}\)2",
    r"(Ba|Sr|Ca|Eu)Mg2(As|P)2",  # BaMg2As2 / SrMg2As2 1111
    # Ternary/quaternary A15 written as a solid solution, e.g. (Nb,Ta,Ti)3Sn.
    r"\(\s*Nb[^)]*\)\s*3\s*Sn",
    r"\(\s*Nb[^)]*\)\s*3\s*[A-Z][a-z]?\d?",
    r"Nb\d?\.?\d*[ -]?\d?\.?\d*(Ta|Ti)[0-9.]*[-–_ ]?[0-9.]*Sn",

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
    # Verified via OpenAlex (2026-10-06) after production rejections:
    # β-PdBi2 nodeless SC (2020), multiphase SC in PdBi2 (2025);
    # Y2O2Bi SC by oxygen doping (2017); η-carbide Zr4Pd2O SC (2023);
    # MAX phases Ti2GeC (9.5 K, 2012) and Ti2InN (7.3 K, 2010);
    # Ti4Ir2O fully-gapped unconventional SC (2024);
    # endohedral gallide Mo8Ga41 single-gap SC (2019).
    r"PdBi2",
    r"Y2O2Bi",
    r"Zr4Pd2O",
    r"Ti2GeC",
    r"Ti2InN",
    r"Ti4Ir2O",
    r"Mo8Ga41",

    # Modern unconventional SC families (nickelates, magic-angle graphene, BiS2)
    # Nickelates: Ruddlesden-Popper series (n=1,2,3,4), infinite-layer (n=∞),
    # reduced / oxygen-deficient variants, RE substitutions (La, Pr, Nd, Sm).
    r"Nd1-xSrxNiO2",   # infinite-layer nickelate
    r"NdNiO2",
    r"Nd0\.8Sr0\.2NiO2",
    r"PrNiO2",
    r"Pr1-xSrxNiO2",
    r"La2NiO4",         # RP n=1
    r"La3Ni2O7",        # RP n=2 (bilayer nickelate)
    r"La3Ni2O7-\d",      # reduced La3Ni2O7 (oxygen-deficient)
    r"La2PrNi2O7",      # La/Pr-substituted bilayer (under pressure, ~80K)
    # A2RE'Ni2O7 trilayer/bilayer nickelates: the La2LnNi2O7 family
    # (La2PrNi2O7, La2NdNi2O7, La2SmNi2O7, La2EuNi2O7, La2DyNi2O7 ...). Papers
    # routinely report the whole substitution series, and enumerating one
    # formula at a time kept rejecting the siblings of an accepted compound.
    r"(La|Sr|Nd|Pr|sm)?2(La|Nd|Pr|Sm|Eu|Dy|Y|Gd|Tb|Ho|Er|Tm|Yb|Lu|Ce)Ni2O7",
    # Same idea for the n=1 and n=3 members of those series.
    r"(La|Nd|Pr|Sm|Ce)2(La|Nd|Pr|Sm|Eu|Dy|Y|Gd)NiO4",
    r"(La|Nd|Pr|Sm|Ce)[45]?(La|Nd|Pr|Sm|Eu|Dy|Y|Gd)Ni[35]O1[02]",
    r"(La|Nd|Pr|Sm|Ce)(2|3|4|5)(La|Nd|Pr|Sm|Eu|Dy|Y|Gd|Tb|Ho|Er|Tm|Yb|Lu)Ni[0-9]O[0-9]",
    r"La2-xPrxNiO4",
    r"La4Ni3O10",       # RP n=3 (trilayer nickelate)
    r"La4Ni3O10-\d",
    # Fractionally doped bilayer nickelates, e.g. La2.85Pr0.15Ni2O7 — the
    # integer form above (La2PrNi2O7) missed the decimal-doped writing and
    # every Pr-doping paper was rejected as a hallucination.
    r"La[0-9.]+[A-Za-z]+[0-9.]+Ni2O7",
    # Parenthesised mixed-site writing, e.g. (La,Pr)3Ni2O7.
    r"\(\s*(La|Pr|Nd|Sm)[^)]{0,12}\)[2345]Ni[23]O[467]",
    # Hybrid RP n=3 member, pressure-induced SC (OpenAlex-verified 2025).
    r"La5Ni3O11",
    r"Nd2NiO4",
    r"Nd3Ni2O7",
    r"Nd4Ni3O10",
    r"Pr4Ni3O10",
    r"SrNiO2",          # infinite-layer strontium nickelate
    # Infinite-layer cuprates: Sr1-xLaxCuO2 and its rare-earth analogues
    # (Nd1-xSrxCuO2 etc.) became superconductors under pressure after 2019 and
    # were absent here, so every such paper was rejected as a hallucination.
    r"Sr[01.]?\d*[.-]?x?[xX]?[0-9.]*La[0-9.]*CuO2",
    r"[0-9.]*[-–]?[xX][-–_ ]?[0-9.]*La[0-9.]*CuO2",
    r"(Nd|Pr|Sm|Ce|La)[0-9.]*[-–_ ]?[xX][-–_ ]?[0-9.]*(Sr|Ba|Ca)[0-9.]*CuO2",
    r"Pr2CuO4|Pr4CuO5|La2CuO4|Nd2CuO4|Sr2CuO4|Ca2CuO4",
    r"[A-Z][a-z]?CuO2$",   # any ABO2 cuprate form
    r"Sr0\.8La0\.2NiO2",
    r"Nd0\.8Sr0\.2Ni1-xCuxO2",
    r"NdNiO3",          # rare-earth nickelate perovskite
    r"PrNiO3",
    r"Nd5NiO10",        # reduced rare-earth nickelate
    r"Nd6Ni5O12",
    r"Ruddlesden-Popper.*nickelate",
    r"NdNiO2",
    r"PrNiO2",
    r"LaNiO2",
    r"Nd0\.8Sr0\.2NiO2",
    r"BiS2",
    r"RbBi2Se4",
    r"CsBi4Se4",      # topological SC
    r"Bi4Se4",
    r"KBi2Se3",       # rhombohedral BiS2 family
    r"MoS2",
    r"WS2",
    r"WSe2",

    # Hydrides (high-pressure SC, common in recent literature)
    r"H3S",
    r"H3P",
    r"H4S",
    r"D3S",
    r"D3P",
    r"H3Se",          # hydrogen selenide
    r"LaH10",
    r"LaH\d+",
    r"CeH9",
    r"CeH10",
    r"PrH9",
    r"NdH9",
    r"SmH9",
    r"CaH6",
    r"CaH\d+",
    r"YH6",
    r"YH\d+",
    r"ThH10",
    r"ThH\d+",
    r"UH8",
    r"UH7",
    r"UHU",
    r"LiH6",
    r"NaH3",
    r"KH6",
    r"RbH9",
    r"SrH\d+",
    r"BaH\d+",
    r"MgH\d+",
    r"BaH\d+",
    r"CaH\d+",
    r"MgH\d+",
    r"YH\d+",
    # Scandium hydrides — ScH6 (Tc ~13.5 K) and ScH9 were reported in
    # Nature 2022 as the highest-Tc binary hydrides. They were missing from
    # the per-element list and were being discarded as hallucinations.
    r"ScH\d+",
    # Alkali / alkaline-earth hydrides not covered above. The subscript is
    # optional so bare ``CsH`` (Tc ~1.5 K at pressure) is accepted.
    r"CsH\d*",
    r"KH\d+",
    r"LiH\d+",
    r"NaH\d+",
    r"LiBH4",
    r"NaBH4",
    # Binary metal hydrides generally: any element symbol followed by H and a
    # stoichiometry. Covers CeH9/H10, TmH9/H10, ZrH9/H10, TiH6/H7, VHx, NbHx,
    # NdH9/10, PmH10, PrH11, SmH9/10, HoH9/10, ErH9/10, YbH9/10, LuH9/10
    # and future binary-hydride discoveries without another edit here.
    r"[A-Z][a-z]?H\d+",
    # Ternary/quaternary hydrides: LaCaF6-ish ternary hydrides, (La,Y)H10,
    # La3Ni2O7-delta style oxygen nickelates, and the high-entropy hydrides.
    r"[A-Z][a-z]?[0-9.]*[A-Z][a-z]?H\d+",
    # Rare-earth / transition-metal ternaries such as ScH6, LaH10 and their
    # solid-solution variants (La0.5Y0.5H10, Ce0.5La0.5H10).
    r"[A-Z][a-z]?[0-9.]*[A-Z][a-z]?[0-9.]*H\d+",
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
