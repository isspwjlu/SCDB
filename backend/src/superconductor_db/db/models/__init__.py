"""ORM model package.

Importing this package registers every table on ``Base.metadata``. Order is
not significant (SQLAlchemy resolves FKs lazily), but we import parents
before children for readability.
"""

from __future__ import annotations

from superconductor_db.db.models.ingest import IngestJob, IngestRun
from superconductor_db.db.models.material import Material
from superconductor_db.db.models.paper import Paper

__all__ = [
    "IngestJob",
    "IngestRun",
    "Material",
    "Paper",
]
