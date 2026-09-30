"""Pydantic v2 schemas mirroring the SQL schema (spec §5, §7, §9)."""

from __future__ import annotations

from superconductor_db.schemas.auth import (
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserRead,
    UserRole,
)
from superconductor_db.schemas.common import Quantity, _is_real
from superconductor_db.schemas.material import (
    LatticeParameter,
    PreparationMethod,
    StructureInformation,
    SuperconductingProperty,
    SuperconductingRecord,
)
from superconductor_db.schemas.submission import (
    SubmissionCreate,
    SubmissionRead,
    SubmissionStatus,
)

__all__ = [
    "LatticeParameter",
    "LoginRequest",
    "PreparationMethod",
    "Quantity",
    "StructureInformation",
    "SubmissionCreate",
    "SubmissionRead",
    "SubmissionStatus",
    "SuperconductingProperty",
    "SuperconductingRecord",
    "TokenResponse",
    "UserCreate",
    "UserRead",
    "UserRole",
    "_is_real",
]