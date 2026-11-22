"""Common Pydantic primitives shared by the material schemas."""

from __future__ import annotations

from typing import Union

from pydantic import BaseModel, ConfigDict, Field


class Quantity(BaseModel):
    """A measured value plus its unit. Mirrors spec §5.1 row format.

    ``value`` accepts ``float``, ``int``, ``str``, or ``None`` (the LLM
    extractor often emits null for absent fields). The string ``"NA"`` is
    the sentinel for missing data (spec §5.3). Use :func:`_is_real` to filter.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    value: Union[float, int, str, None] = Field(default=None)
    unit: str | None = Field(default=None)


# Sentinels treated as "no measurement". Lower-case + upper-case; both styles
# appear in published SuperCon dumps.
_MISSING_SENTINELS = frozenset({"NA", "N/A", "NULL", "NONE", "-", ""})


def _is_real(q: Quantity | None) -> bool:
    """True iff ``q`` is non-None and its value is numeric (not the ``"NA"`` sentinel).

    Used by ``SuperconductingRecord.has_meaningful_data`` (spec §5.2).
    """
    if q is None or q.value is None:
        return False
    val = q.value
    if isinstance(val, str):
        if val.strip().upper() in _MISSING_SENTINELS:
            return False
        # Try parsing as float — LLM extracts sometimes emit "92.0 K" instead
        # of splitting into value + unit.
        try:
            float(val)
            return True
        except ValueError:
            return False
    return isinstance(val, (int, float))