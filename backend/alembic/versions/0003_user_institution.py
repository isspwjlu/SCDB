"""users.institution

Revision ID: 0003_user_institution
Revises: 0002_ingest_jobs_run_id
Create Date: 2026-11-18 00:00:00

Asks the registrant where they work. The column is nullable: the
accounts that were created before the question was asked have no
answer, and that is fine.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_user_institution"
down_revision: str | Sequence[str] | None = "0002_ingest_jobs_run_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("institution", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "institution")
