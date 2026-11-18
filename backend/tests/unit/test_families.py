"""Family classification from the chemical formula.

The family column used to be filled by asking the model, which answered "NA"
for 55 of 56 rows. A formula determines the family outright.
"""

from __future__ import annotations

import pytest

from superconductor_db.ingest.families import classify_family


@pytest.mark.unit
@pytest.mark.parametrize(
    ("composition", "expected"),
    [
        ("LaH10", "high-pressure hydride"),
        ("H3S", "high-pressure hydride"),
        ("YBa2Cu3O7", "cuprate"),
        # Infinite-layer and doped cuprates written with a substitution variable.
        ("Sr0.9La0.1CuO2", "cuprate"),
        ("Sr0.9La0.1CuO2/KTaO3", "cuprate"),
        ("La2-xBaxCuO4", "cuprate"),
        ("La2-xSrxCuO4", "cuprate"),
        ("La3Ni2O7", "nickelate"),
        ("La2PrNi2O7", "nickelate"),
        ("FeSe", "iron-based"),
        ("BaFe2As2", "iron-based"),
        ("MgB2", "MgB2"),
        ("CeCoIn5", "heavy-fermion"),
        ("PrOs4Sb12", "heavy-fermion"),
        ("NbSe2", "layered dichalcogenide"),
        ("Nb", "elemental"),
        ("Al", "elemental"),
    ],
)
def test_classify_family(composition: str, expected: str) -> None:
    assert classify_family(composition) == expected


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", None, "Xyzzy999"])
def test_unknown_formulas_are_not_guessed(value) -> None:
    """A blank cell is better than a wrong family; the dashboard aggregates on it."""
    assert classify_family(value) is None
