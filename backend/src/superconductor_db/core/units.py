"""Unit normalisation for the measured quantities.

The extractor records the unit the paper used, and papers use whatever their
field uses -- meV, eV, cm^-1, THz, kB*Tc, kelvin. The charts put those raw
values on a single axis, which mixes unit systems by a factor of 1000 and
makes the plot meaningless. The model also writes something that is not a
unit at all now and then: "s^-1" for a resonance frequency, "% of maximal
gap", or a whole phrase such as "T (in-plane); 0.57 T (out-of-plane)". One
such row reached the gap chart and stretched its log axis over ten decades,
which made the whole chart look empty.

These helpers convert to a single unit and refuse the values that are not
physically what the field is meant to hold.
"""

from __future__ import annotations

import re

# Physical constants, in the units the conversion factors need.
_KB_MEV_PER_K = 0.08617333          # k_B in meV/K
_MEV_PER_EV = 1000.0
_MEV_PER_THZ = 4.135667696         # h·ν for ν in THz
_MEV_PER_CM1 = 0.123984198         # h·c·ν̃ for ν̃ in cm^-1

# The gap of a superconductor reaches 70 meV in the best cuprates and
# 0.6 eV in a heavily overdoped one; past 1 eV the number is a mis-parsed
# unit rather than a measurement.
_MAX_PLAUSIBLE_GAP_MEV = 1000.0

# No known superconductor has an upper critical field above ~250 T.
_MAX_PLAUSIBLE_FIELD_T = 1000.0

_ENERGY_UNIT_TO_MEV: dict[str, float] = {
    "mev": 1.0,
    "ev": _MEV_PER_EV,
    "µev": 0.001,
    "μev": 0.001,
    "mv": _MEV_PER_EV,
    "v": _MEV_PER_EV * 1_000.0,
    # 1 uV is a thousandth of a meV, not a meV: the gaps in a heavy
    # fermion are quoted as 500-700 uV.
    "µv": 0.001,
    "μv": 0.001,
    "cm^-1": _MEV_PER_CM1,
    "cm-1": _MEV_PER_CM1,
    "thz": _MEV_PER_THZ,
    "ghz": _MEV_PER_THZ / 1000.0,
    # A temperature standing in for an energy, i.e. k_B*T
    "k": _KB_MEV_PER_K,
}

_FIELD_UNIT_TO_TESLA: dict[str, float] = {
    "t": 1.0,
    "mt": 1e-3,
    "µt": 1e-6,
    "μt": 1e-6,
    "koe": 0.1,          # 1 kOe = 1000 Oe = 0.1 T
    "oe": 1e-4,
    "kgauss": 0.1,
    "gauss": 1e-4,
    "g": 1e-4,
}


def _normalise(unit: str | None) -> str:
    """Fold the spellings the extractor produces onto one form."""
    u = (unit or "").strip().lower()
    u = u.replace("∆", "d").replace("Δ", "d")
    u = u.replace("μ", "µ").replace("−", "-")
    return u


def energy_gap_to_mev(
    value: float | None,
    unit: str | None,
    tc: float | None = None,
) -> float | None:
    """Convert a stored energy gap to meV.

    Returns ``None`` when the unit is not an energy -- a frequency in s^-1,
    a percentage, a phrase the model wrote instead of a unit -- or when the
    converted value is not physically a gap.

    A unit that means "a multiple of k_B*Tc" is dimensionless, so it needs
    Tc to become an energy: ``2∆0/kBTc = 3.5`` becomes 3.5·k_B·Tc in meV.
    """
    if value is None or value <= 0:
        return None
    u = _normalise(unit)
    if not u:
        return None

    # "k_B T_c", "kBTc", "2∆0/kBTc" are all the same thing; the spacing and
    # the underscore vary with whoever wrote the paper.
    compact = re.sub(r"[\s_]+", "", u)
    if "kbtc" in compact or "kbt" in compact or compact.startswith("dimensionless"):
        if tc is None or tc <= 0:
            return None
        mev = value * _KB_MEV_PER_K * tc
    else:
        factor = _ENERGY_UNIT_TO_MEV.get(u)
        if factor is None and u.startswith("k") and "energy" in u:
            # "K (k_B energy)" -- a temperature standing in for k_B*T
            factor = _KB_MEV_PER_K
        if factor is None:
            return None
        mev = value * factor

    if mev <= 0 or mev > _MAX_PLAUSIBLE_GAP_MEV:
        return None
    return mev


def magnetic_field_to_tesla(
    value: float | None,
    unit: str | None,
) -> float | None:
    """Convert a stored magnetic field (Hc1, Hc2) to tesla.

    Returns ``None`` for a unit that is not one -- a phrase such as
    "T (H || c; H || ab: 65 T)" carries two numbers and cannot be reduced
    to one axis -- and for values outside the physical range.
    """
    if value is None or value <= 0:
        return None
    u = _normalise(unit)
    factor = _FIELD_UNIT_TO_TESLA.get(u)
    if factor is None:
        return None
    tesla = value * factor
    if tesla <= 0 or tesla > _MAX_PLAUSIBLE_FIELD_T:
        return None
    return tesla
