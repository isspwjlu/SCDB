"""Whitelist tests — must accept real SCs and reject non-SCs.

The whitelist is the hallucination guard, so it is wrong in both directions:
accepting a non-superconductor corrupts the database, while rejecting a real
one silently discards a valid measurement. Both directions are asserted here.
"""

from __future__ import annotations

import pytest

from superconductor_db.ingest.whitelist import is_known_superconductor


# Established superconductors across families. Every one of these must be
# accepted — ScH6/ScH9, Ti3Au and Cr3Si were previously missing and were being
# discarded as "LLM hallucinations".
REAL_SUPERCONDUCTORS = [
    "MgB2",
    "YBa2Cu3O7",
    "Bi2Sr2CaCu2O8",
    "Hg-1223",
    "FeSe",
    "FeSe0.5Te0.5",
    "SmFeAsO",
    "LiFeAs",
    "Nb3Sn",
    "Sr2RuO4",
    "CeCoIn5",
    "PrOs4Sb12",
    # High-pressure hydrides
    "LaH9",
    "LaH10",
    "YH3",
    "YH6",
    "CeH10",
    "CaH6",
    "H3S",
    "ScH6",
    "ScH9",
    "TiH6",
    "ZrH9",
    "YbH10",
    "TmH10",
    "CsH",
    "LaCaH11",
    # Nickelates
    "La3Ni2O7",
    "La4Ni3O10",
    "LaNiO2",
    "NdNiO2",
    "SrNiO2",
    # A15 additions
    "Ti3Au",
    "Cr3Si",
    "Mo3Si",
    # Layered dichalcogenides
    "1H-NbSe2",
    "1T-TiSe2",
    # Binary / alloy superconductors. Nb-Ti was being rejected as a
    # "hallucination" even though it is the standard MRI/LHC wire alloy.
    "NbTi",
    "Nb-Ti",
    "Nb0.62Ti0.38",
    "NbTiSn",
    "Nb-Ti-Sn",
    "V3Ga",
    "Ti2O3",
    "Ni3Al",
    # Substituted perovskites written with an explicit doping variable.
    "La0.7Ca0.3CuO4",
    "La2-xSrxCuO4",
    "La2-xBaxCuO4",
    "LBCO",
    # Heavy fermions not previously listed.
    "UCoGe",
    "UCu2Si2",
    "UTe2",
    "PrCoIn5",
    "CeRhIn5",
    "CeCu2Si2",
    # Double perovskites (327 family).
    "Ba2YRuO6",
    "Sr2YRuO6",
    "Sr2LuRuO6",
    # Fulleride superconductors.
    "K3C60",
    "Rb3C60",
    # Doped iron-based SCs, the commonest way papers write them.
    "(Nb,Ta,Ti)3Sn",
    "SmFe0.85Co0.15AsO",
    "SrFe2-xCoxAs2",
    "Ba1-xKxFe2As2",
    "Ca1-xSrxFe2As2",
    "BaFe2(As1-xPx)2",
    "BaFe2(Se1-xSx)2",
    "SrFeAsO",
    "LaFe0.9Ni0.1AsO",
    "BaMg2As2",
    # La2LnNi2O7 substitution series. A paper on La2PrNi2O7 reports its
    # Nd/Sm/Dy siblings in the same tables; enumerating one formula at a time
    # kept rejecting those siblings as hallucinations.
    "La2NdNi2O7",
    "La2SmNi2O7",
    "La2DyNi2O7",
    "La2EuNi2O7",
    "Nd4Ni3O10",
    # Infinite-layer cuprates (superconducting under pressure since 2019).
    "Sr0.9La0.1CuO2",
    "Sr0.8La0.2CuO2",
    "Sr1-xLaxCuO2",
    "Pr2CuO4",
    # Rejected in production on 2026-10-05/06 and verified real via OpenAlex
    # before being added: every entry here carries published superconductivity.
    "PdBi2",
    "β-PdBi2",
    "Y2O2Bi",
    "La5Ni3O11",
    "Zr4Pd2O",
    "Ti2GeC",
    "Ti2InN",
    "Ti4Ir2O",
    "Mo8Ga41",
    # Doped / mixed-site nickelate writings the integer patterns missed.
    "La2.85Pr0.15Ni2O7",
    "(La,Pr)3Ni2O7",
    "Tl1.9Cr0.1Ba2CaCu2O8",
]


# Non-superconductors the LLM has previously emitted. Cr2Ge2Te6 is a van der
# Waals ferromagnet, not a superconductor. La3Ni2O6 is kept out deliberately:
# OpenAlex (2026-10-06) shows only a *theoretical* proposal of hole-doped
# superconductivity plus density-wave physics — no established measurement.
NON_SUPERCONDUCTORS = [
    "V2O3",
    "V2O5",
    "Cr2Ge2Te6",
    "Fe2O3",
    "CrO2",
    "NiO",
    "Cu2O",
    "SiO2",
    "NaCl",
    "BaTiO3",
    "GaAs",
    "InP",
    "CoO",
    "La3Ni2O6",
    "V/MgO/Fe",
]


@pytest.mark.unit
@pytest.mark.parametrize("formula", REAL_SUPERCONDUCTORS)
def test_known_superconductors_are_accepted(formula: str) -> None:
    assert is_known_superconductor(formula) is True, (
        f"{formula} is a real superconductor but the whitelist rejects it — "
        f"valid measurements are being discarded as hallucinations"
    )


@pytest.mark.unit
@pytest.mark.parametrize("formula", NON_SUPERCONDUCTORS)
def test_non_superconductors_are_rejected(formula: str) -> None:
    assert is_known_superconductor(formula) is False, (
        f"{formula} is not a superconductor but the whitelist accepts it — "
        f"hallucinated rows are entering the database"
    )


@pytest.mark.unit
def test_empty_and_garbage_rejected() -> None:
    assert is_known_superconductor("") is False
    assert is_known_superconductor("   ") is False
    assert is_known_superconductor("XxYyZz") is False
    assert is_known_superconductor("not a formula at all") is False
