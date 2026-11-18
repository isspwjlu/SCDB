"""Classify a composition into a superconductor family from its formula.

The dashboard groups materials by family, but the family column was filled by
asking the model, which usually answered ``NA`` — 55 of 56 rows were blank.
A formula determines the family far more reliably than a language model does:
``LaH10`` is a high-pressure hydride whatever the paper's wording.

The ordering matters: a compound can match more than one pattern (an
FeSe/SrTiO3 heterostructure is both a chalcogenide and a monolayer), so the
most specific family is returned first.
"""

from __future__ import annotations

import re

# Ordered most-specific first; the first match wins.
_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    # Hydrides are the highest-Tc family and are written many ways: metal
    # hydrides end in Hn (LaH10), the sulfur/selenium ones lead with H (H3S).
    (r"H\d*(?:\.\d+)?$|^[HD]\d*[a-zA-Z]?\d*$|CaH\d|LaH\d|YH\d|ScH\d|"
     r"Li\d*(?:Ca|Mg)?H\d|Na\w*H\d|ThH\d|CeH\d|"
     r"hydride|superhydride",
     "high-pressure hydride"),

    # Cuprates.
    (r"YBa|YBCO|Bi2?2?Ca|Sr2?2?(?:Ca)?Cu|Tl2?Ba2|HgBa|La2?[.-_]?[xsxX]?Sr"
     r"|LSCO|LBCO|BSCO|cuprate",
     "cuprate"),
    # Infinite-layer cuprates (Sr1-xLaxCuO2, superconductors under pressure
    # since 2019) and La2-xBaxCuO4 / La2-xSrxCuO4 written with an explicit
    # substitution variable. The plain "La...Sr...CuO4" pattern above misses
    # "La2-xBaxCuO4" because the dopant is Ba, not Sr.
    (r"La[0-9.]*[-–_ ]?x[-–_ ]?[0-9.]*(Ba|Sr|Ca)[0-9.]*x?[0-9.]*CuO4"
     r"|La[0-9.]*[-–_ ]?(Ba|Sr|Ca)[0-9.]*CuO4",
     "cuprate"),
    (r"Sr[01.]?\d*[.-]?x?[xX]?[0-9.]*La[0-9.]*CuO2"
     r"|[0-9.]*[-–]?x[-–_ ]?[0-9.]*La[0-9.]*CuO2"
     r"|(Nd|Pr|Sm|Ce)[0-9.]*[-–_ ]?x[-–_ ]?[0-9.]*(Sr|Ba|Ca)[0-9.]*CuO2",
     "cuprate"),

    # Nickelates.
    (r"NiO\d|ni[23]O|nickelate|La\d?Ni\d|Nd\d?\.?\d*Sr\d?\.?\d*NiO2|NiO2",
     "nickelate"),

    # Iron-based.
    (r"Fe[12]?[.-]?[xX]?\w*[Ss]e|Fe[12]?[.-]?[xX]?\w*[Tt]e|monolayer|"
     r"[A-Z][a-z]?FeAs|(?:Ba|Sr|Ca|Eu|Sm|La|Ce|Pr|Nd)Fe|"
     r"K2Cr3As3|ChCr3As5|pnictide|chalcogenide",
     "iron-based"),

    (r"MgB|magnesium diboride", "MgB2"),

    # A15 and other intermetallics.
    (r"Nb3(?:Sn|Al|Ge|Ga|Si)|V3(?:Si|Ga|Ge)|Cr3Si|Ti3(?:Au|Sb)|"
     r"NbTi|Nb3Pt|Nb3Sn|rutile|superconductor alloy",
     "A15 / intermetallic"),

    # Dichalcogenides and layered chalcogenides.
    (r"(?:Nb|Mo|Ta|W|WS)Se2|(?:Nb|Mo|Ta)S2|1T-|2H-|1L-|"
     r"TiSe2|WSe2",
     "layered dichalcogenide"),

    # Heavy fermions and uranium compounds.
    (r"Ce(?:Co|Rh|Ir|Cu)|URu2Si2|UPt3|UBe13|UTe2|PrOs4Sb12|"
     r"PuCoGa5|heavy.fermion|CeRh2As2|CeIrIn5",
     "heavy-fermion"),

    # Ruthenates.
    (r"Rr?u2O4|Ruthenate|Sr2RuO4|Ca2RuO4", "ruthenate"),

    # Lanthanide trichalcogenides and bismuth chalcogenides.
    (r"BiS2|ScS2|LaOBiS", "bismuth chalcogenide"),
)

# Simple elemental superconductors.
_ELEMENTAL = {
    "al", "in", "sn", "pb", "hg", "tl", "nb", "v", "la", "ti", "zr",
    "mo", "w", "re", "os", "ir", "pt", "lu", "th", "pa", "ca", "y",
    "sc", "ga", "ge", "si", "k", "rb", "cs", "ba", "sr",
}


def classify_family(composition: str | None) -> str | None:
    """Return the family for ``composition``, or ``None`` when unknown.

    A family is only asserted when the formula matches a documented pattern;
    guessing from a formula we do not recognise is worse than leaving the cell
    blank, because the dashboard and stats both aggregate on this value.
    """
    if not composition:
        return None
    text = composition.strip()
    if not text:
        return None
    joined = re.sub(r"[\s()\[\]]", "", text)

    for pattern, family in _FAMILY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE) or re.search(
            pattern, joined, re.IGNORECASE
        ):
            return family
    if joined.casefold() in _ELEMENTAL:
        return "elemental"
    return None
