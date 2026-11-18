"""users.name

Revision ID: 0004_user_name
Revises: 0003_user_institution
Create Date: 2026-11-18 00:00:00

Asks the registrant their name. The column is nullable: the accounts
that were created before the question was asked have no answer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_user_name"
down_revision: str | Sequence[str] | None = "0003_user_institution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "name")
