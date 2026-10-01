"""ingest_jobs.run_id linkage

Revision ID: 0002_ingest_jobs_run_id
Revises: 0001_initial
Create Date: 2026-10-02 00:00:00

Adds an explicit ``run_id`` FK on ``ingest_jobs`` pointing at ``ingest_runs``
so the worker can attribute per-job counters to the run that triggered them
without relying on "most recent running run" heuristics.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_ingest_jobs_run_id"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_jobs",
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "ingest_jobs_run_id_fkey",
        "ingest_jobs",
        "ingest_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ingest_jobs_run_id_idx",
        "ingest_jobs",
        ["run_id"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ingest_jobs_run_id_idx", table_name="ingest_jobs")
    op.drop_constraint("ingest_jobs_run_id_fkey", "ingest_jobs", type_="foreignkey")
    op.drop_column("ingest_jobs", "run_id")
