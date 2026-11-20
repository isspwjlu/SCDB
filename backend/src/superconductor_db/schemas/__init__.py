"""Pydantic v2 schemas mirroring the SQL schema (spec §5, §7, §9).

The request and response models of the web API are not part of this
package: the extraction pipeline needs the material schemas and the
quantity helper, and nothing else.
"""

from __future__ import annotations

from superconductor_db.schemas.common import Quantity, _is_real
from superconductor_db.schemas.material import (
    LatticeParameter,
    PreparationMethod,
    StructureInformation,
    SuperconductingProperty,
    SuperconductingRecord,
)

__all__ = [
    "LatticeParameter",
    "PreparationMethod",
    "Quantity",
    "StructureInformation",
    "SuperconductingProperty",
    "SuperconductingRecord",
    "_is_real",
]
