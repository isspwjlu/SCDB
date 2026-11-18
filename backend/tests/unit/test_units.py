"""Unit normalisation for the charted quantities.

The charts put every stored value on one axis, so the units have to be
brought onto a common scale first -- and the values whose unit is not an
energy at all have to be refused, because one of them was enough to empty
the whole gap chart.
"""

from __future__ import annotations

import pytest

from superconductor_db.core.units import (
    energy_gap_to_mev,
    magnetic_field_to_tesla,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (35.0, "meV", 35.0),
        (0.035, "eV", 35.0),
        (1200.0, "cm^-1", pytest.approx(1200.0 * 0.123984198)),
        (1200.0, "cm-1", pytest.approx(1200.0 * 0.123984198)),
        (2.5, "THz", pytest.approx(2.5 * 4.135667696)),
        (220.0, "µeV", pytest.approx(0.220)),
        (570.0, "K", pytest.approx(570.0 * 0.08617333)),
        (223.4, "K (k_B energy)", pytest.approx(223.4 * 0.08617333)),
    ],
)
def test_energy_gap_is_converted_to_mev(value, unit, expected) -> None:
    assert energy_gap_to_mev(value, unit, tc=50.0) == expected


@pytest.mark.unit
def test_a_multiple_of_kb_tc_uses_tc_to_become_an_energy() -> None:
    # 2∆0/kBTc = 3.52 is 3.52 * k_B * Tc in meV
    assert energy_gap_to_mev(3.52, "kBTc", tc=10.0) == pytest.approx(
        3.52 * 0.08617333 * 10.0
    )
    assert energy_gap_to_mev(2.4, "2∆0/kBTc", tc=14.7) == pytest.approx(
        2.4 * 0.08617333 * 14.7
    )
    assert energy_gap_to_mev(2.4, "dimensionless (2∆(0)/kBTc)", tc=14.7) == pytest.approx(
        2.4 * 0.08617333 * 14.7
    )


@pytest.mark.unit
def test_a_multiple_of_kb_tc_without_tc_is_refused() -> None:
    assert energy_gap_to_mev(2.4, "kBTc", tc=None) is None
    assert energy_gap_to_mev(2.4, "kBTc", tc=0.0) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit",
    [
        "s^-1",              # a resonance frequency, not an energy
        "% of maximal gap",  # a percentage
        "Tc",                # a temperature
        "meV (two-gap Δ1); 0.13 meV (two-gap Δ2); 0.38 meV (single-gap)",
        "",
        None,
    ],
)
def test_units_that_are_not_energies_are_refused(unit) -> None:
    assert energy_gap_to_mev(10.0, unit, tc=50.0) is None


@pytest.mark.unit
def test_zero_and_negative_gaps_are_refused() -> None:
    assert energy_gap_to_mev(0.0, "meV", tc=50.0) is None
    assert energy_gap_to_mev(-1.0, "meV", tc=50.0) is None
    assert energy_gap_to_mev(None, "meV", tc=50.0) is None


@pytest.mark.unit
def test_a_gap_beyond_the_record_is_refused() -> None:
    # 2.14e12 s^-1 once reached the chart and stretched the log axis over
    # ten decades; anything past the physical maximum is a mis-parsed unit.
    assert energy_gap_to_mev(2.14e12, "s^-1", tc=9.26) is None
    assert energy_gap_to_mev(5000.0, "meV", tc=50.0) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (14.0, "T", 14.0),
        (12.0, "mT", pytest.approx(0.012)),
        (30.0, "kOe", pytest.approx(3.0)),
        (100.0, "Oe", pytest.approx(0.01)),
    ],
)
def test_magnetic_field_is_converted_to_tesla(value, unit, expected) -> None:
    assert magnetic_field_to_tesla(value, unit) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit",
    [
        "T (in-plane); 0.57 T (out-of-plane)",
        "T (H || c; H || ab: 65 T)",
        "Oe (lower bound of reported 70-80 Oe range)",
        "",
        None,
    ],
)
def test_magnetic_field_units_that_carry_two_numbers_are_refused(unit) -> None:
    assert magnetic_field_to_tesla(10.0, unit) is None


@pytest.mark.unit
def test_magnetic_field_outside_the_physical_range_is_refused() -> None:
    assert magnetic_field_to_tesla(0.0, "T") is None
    assert magnetic_field_to_tesla(-1.0, "T") is None
    assert magnetic_field_to_tesla(5000.0, "T") is None


@pytest.mark.unit
def test_microvolt_is_a_thousandth_of_a_millivolt() -> None:
    """The gaps of a heavy fermion are quoted as 500-700 uV; treating uV as
    a millivolt made CeCoIn5's gap a hundred times too large."""
    assert energy_gap_to_mev(535.0, "µV", tc=2.3) == pytest.approx(0.535)
    assert energy_gap_to_mev(700.0, "µeV", tc=2.3) == pytest.approx(0.700)


@pytest.mark.unit
def test_an_electronvolt_gap_survives() -> None:
    """A heavily overdoped cuprate has a gap of 0.6 eV."""
    assert energy_gap_to_mev(0.623, "eV", tc=10.0) == pytest.approx(623.0)


@pytest.mark.unit
def test_a_kelvin_written_as_an_energy() -> None:
    """Papers write the gap as the temperature that carries it."""
    assert energy_gap_to_mev(570.0, "K", tc=57.5) == pytest.approx(570.0 * 0.08617333)
    assert energy_gap_to_mev(223.4, "K (k_B energy)", tc=88.8) == pytest.approx(
        223.4 * 0.08617333
    )


@pytest.mark.unit
def test_a_multiple_of_kb_tc_written_with_spaces() -> None:
    """The papers spell it "k_B T_c" as often as "kBTc"."""
    assert energy_gap_to_mev(3.52, "k_B T_c", tc=100.0) == pytest.approx(
        3.52 * 0.08617333 * 100.0
    )
