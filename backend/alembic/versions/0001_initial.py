"""initial schema (8 tables)

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-01 00:00:00

This is a hand-written translation of ``docs/superpowers/specs/2026-09-30-
superconductor-database-design.md`` §10 (Database Schema) into Alembic ops.

The migration does NOT use autogenerate because the source of truth is the
spec, not the SQLAlchemy models; autogenerate is fine for incremental
migrations later.

Tables created (spec §10):

  1. users
  2. papers
  3. materials     (includes the ``has_some_data`` CheckConstraint)
  4. submissions
  5. ingest_runs
  6. ingest_jobs
  7. audit_log
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _pg_uuid_pk(server_default: bool = True) -> sa.Column:
    """UUID primary key column with ``gen_random_uuid()`` default.

    We mirror the SQL ``DEFAULT gen_random_uuid()`` so Postgres fills the id
    on INSERT; the application code can also set it explicitly when needed
    (e.g. the seed loader derives a deterministic UUID from a row hash).
    """
    col = sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    if server_default:
        col.server_default = sa.text("gen_random_uuid()")
    return col


def _pg_uuid_fk(name: str, target: str, ondelete: str | None = None) -> sa.Column:
    """UUID foreign-key column referencing ``target``."""
    kwargs: dict[str, str] = {}
    if ondelete is not None:
        kwargs["ondelete"] = ondelete
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, **kwargs),
        nullable=False,
    )


def _timestamptz_now() -> sa.Column:
    """``TIMESTAMPTZ NOT NULL DEFAULT NOW()``."""
    return sa.Column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


def _timestamptz_nullable() -> sa.Column:
    """``TIMESTAMPTZ`` (no default)."""
    return sa.Column(sa.DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# upgrade                                                                     #
# --------------------------------------------------------------------------- #


def upgrade() -> None:
    # Need pgcrypto for ``gen_random_uuid()``. Cheap & idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---- users ----------------------------------------------------------
    op.create_table(
        "users",
        _pg_uuid_pk(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'contributor'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        _timestamptz_now().copy(),
        _timestamptz_nullable().copy(),
        sa.UniqueConstraint("email", name="users_email_key"),
        sa.CheckConstraint(
            "role IN ('contributor', 'reviewer', 'admin')",
            name="users_role_check",
        ),
    )
    op.create_index("users_role_idx", "users", ["role"])

    # ---- papers ---------------------------------------------------------
    op.create_table(
        "papers",
        _pg_uuid_pk(),
        sa.Column("doi", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("pdf_path", sa.Text(), nullable=True),
        sa.Column("markdown_path", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("journal", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("doi", name="papers_doi_key"),
    )
    op.create_index("papers_doi_idx", "papers", ["doi"])

    # ---- materials ------------------------------------------------------
    # Mirrors spec §10. ~60 columns; we annotate the sections in-line.
    op.create_table(
        "materials",
        # Identification
        _pg_uuid_pk(),
        sa.Column("chemical_composition", sa.Text(), nullable=False),
        sa.Column("doi", sa.Text(), nullable=True),
        _pg_uuid_fk("paper_id", "papers.id", ondelete="SET NULL"),
        sa.Column(
            "experimental",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        # Base 12 SC properties (value + unit pairs)
        sa.Column("tc_value", sa.Float(), nullable=True),
        sa.Column("tc_unit", sa.Text(), nullable=True),
        sa.Column("t_cdw_value", sa.Float(), nullable=True),
        sa.Column("t_cdw_unit", sa.Text(), nullable=True),
        sa.Column("hc1_value", sa.Float(), nullable=True),
        sa.Column("hc1_unit", sa.Text(), nullable=True),
        sa.Column("hc2_value", sa.Float(), nullable=True),
        sa.Column("hc2_unit", sa.Text(), nullable=True),
        sa.Column("energy_gap_value", sa.Float(), nullable=True),
        sa.Column("energy_gap_unit", sa.Text(), nullable=True),
        sa.Column("coherence_length_value", sa.Float(), nullable=True),
        sa.Column("coherence_length_unit", sa.Text(), nullable=True),
        sa.Column("penetration_depth_value", sa.Float(), nullable=True),
        sa.Column("penetration_depth_unit", sa.Text(), nullable=True),
        sa.Column("critical_current_density_value", sa.Float(), nullable=True),
        sa.Column("critical_current_density_unit", sa.Text(), nullable=True),
        sa.Column("pairing_symmetry", sa.Text(), nullable=True),
        sa.Column("sc_type", sa.Text(), nullable=True),
        sa.Column("mechanism", sa.Text(), nullable=True),
        sa.Column("superconductor_family", sa.Text(), nullable=True),
        # SC-specific 7 fields
        sa.Column("tc_under_pressure", postgresql.JSONB(), nullable=True),
        sa.Column("isotope_exponent_alpha", sa.Float(), nullable=True),
        sa.Column("uemura_class", sa.Text(), nullable=True),
        sa.Column("gamma_n_value", sa.Float(), nullable=True),
        sa.Column("gamma_n_unit", sa.Text(), nullable=True),
        sa.Column("gamma_s_value", sa.Float(), nullable=True),
        sa.Column("gamma_s_unit", sa.Text(), nullable=True),
        sa.Column("debye_temperature_value", sa.Float(), nullable=True),
        sa.Column("debye_temperature_unit", sa.Text(), nullable=True),
        sa.Column("fermi_temperature_value", sa.Float(), nullable=True),
        sa.Column("fermi_temperature_unit", sa.Text(), nullable=True),
        sa.Column("resistivity_normal_value", sa.Float(), nullable=True),
        sa.Column("resistivity_normal_unit", sa.Text(), nullable=True),
        # Crystal structure
        sa.Column("crystal_structure", sa.Text(), nullable=True),
        sa.Column("lattice_structure", sa.Text(), nullable=True),
        sa.Column("lattice_a_value", sa.Float(), nullable=True),
        sa.Column("lattice_a_unit", sa.Text(), nullable=True),
        sa.Column("lattice_b_value", sa.Float(), nullable=True),
        sa.Column("lattice_b_unit", sa.Text(), nullable=True),
        sa.Column("lattice_c_value", sa.Float(), nullable=True),
        sa.Column("lattice_c_unit", sa.Text(), nullable=True),
        sa.Column("space_group", sa.Text(), nullable=True),
        # Preparation
        sa.Column("synthesis_method", sa.Text(), nullable=True),
        sa.Column("chemical_precursors", sa.Text(), nullable=True),
        sa.Column("synthesis_temperature_value", sa.Float(), nullable=True),
        sa.Column("synthesis_temperature_unit", sa.Text(), nullable=True),
        sa.Column("synthesis_pressure_value", sa.Float(), nullable=True),
        sa.Column("synthesis_pressure_unit", sa.Text(), nullable=True),
        sa.Column("synthesis_atmosphere", sa.Text(), nullable=True),
        sa.Column("synthesis_time_value", sa.Float(), nullable=True),
        sa.Column("synthesis_time_unit", sa.Text(), nullable=True),
        sa.Column("heating_rate_value", sa.Float(), nullable=True),
        sa.Column("heating_rate_unit", sa.Text(), nullable=True),
        sa.Column("sintering_temperature_value", sa.Float(), nullable=True),
        sa.Column("sintering_temperature_unit", sa.Text(), nullable=True),
        sa.Column("sintering_time_value", sa.Float(), nullable=True),
        sa.Column("sintering_time_unit", sa.Text(), nullable=True),
        sa.Column("annealing_temperature_value", sa.Float(), nullable=True),
        sa.Column("annealing_temperature_unit", sa.Text(), nullable=True),
        sa.Column("annealing_time_value", sa.Float(), nullable=True),
        sa.Column("annealing_time_unit", sa.Text(), nullable=True),
        sa.Column("annealing_atmosphere", sa.Text(), nullable=True),
        sa.Column("cooling_method", sa.Text(), nullable=True),
        sa.Column("substrate", sa.Text(), nullable=True),
        sa.Column("additional_details", sa.Text(), nullable=True),
        # Metadata
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _timestamptz_now().copy(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "(tc_value IS NOT NULL) "
            "OR (t_cdw_value IS NOT NULL) "
            "OR (hc1_value IS NOT NULL) "
            "OR (hc2_value IS NOT NULL) "
            "OR (energy_gap_value IS NOT NULL) "
            "OR (coherence_length_value IS NOT NULL) "
            "OR (penetration_depth_value IS NOT NULL) "
            "OR (critical_current_density_value IS NOT NULL)",
            name="has_some_data",
        ),
    )
    op.create_index(
        "materials_composition_idx", "materials", ["chemical_composition"]
    )
    op.create_index(
        "materials_tc_value_idx",
        "materials",
        ["tc_value"],
        postgresql_where=sa.text("tc_value IS NOT NULL"),
    )
    op.create_index(
        "materials_family_idx",
        "materials",
        ["superconductor_family"],
        postgresql_where=sa.text("superconductor_family IS NOT NULL"),
    )
    op.create_index("materials_doi_idx", "materials", ["doi"])

    # ---- submissions ----------------------------------------------------
    op.create_table(
        "submissions",
        _pg_uuid_pk(),
        _pg_uuid_fk("user_id", "users.id", ondelete="CASCADE"),
        sa.Column("pdf_path", sa.Text(), nullable=False),
        sa.Column("extracted_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        _timestamptz_now().copy(),
        _timestamptz_nullable().copy(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'failed')",
            name="submissions_status_check",
        ),
    )
    op.create_index(
        "submissions_status_idx",
        "submissions",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index(
        "submissions_user_idx",
        "submissions",
        ["user_id", sa.text("created_at DESC")],
    )

    # ---- ingest_runs ----------------------------------------------------
    op.create_table(
        "ingest_runs",
        _pg_uuid_pk(),
        sa.Column(
            "triggered_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        _timestamptz_now().copy(),
        _timestamptz_nullable().copy(),
        sa.Column(
            "papers_searched",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "papers_downloaded",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "records_extracted",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "records_stored",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "errors", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ingest_runs_status_check",
        ),
    )

    # ---- ingest_jobs ----------------------------------------------------
    op.create_table(
        "ingest_jobs",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        _timestamptz_nullable().copy().copy(),  # locked_at
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        _timestamptz_now().copy(),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'failed')",
            name="ingest_jobs_status_check",
        ),
    )
    op.create_index(
        "ingest_jobs_pending_idx",
        "ingest_jobs",
        ["status", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # ---- audit_log ------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_table", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        _timestamptz_now().copy(),
    )
    op.create_index(
        "audit_log_created_idx",
        "audit_log",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "audit_log_user_idx",
        "audit_log",
        ["user_id", sa.text("created_at DESC")],
    )


# --------------------------------------------------------------------------- #
# downgrade                                                                   #
# --------------------------------------------------------------------------- #


def downgrade() -> None:
    # Drop in reverse FK order so we don't violate any constraints.
    op.drop_index("audit_log_user_idx", table_name="audit_log")
    op.drop_index("audit_log_created_idx", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ingest_jobs_pending_idx", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")

    op.drop_table("ingest_runs")

    op.drop_index("submissions_user_idx", table_name="submissions")
    op.drop_index("submissions_status_idx", table_name="submissions")
    op.drop_table("submissions")

    op.drop_index("materials_doi_idx", table_name="materials")
    op.drop_index("materials_family_idx", table_name="materials")
    op.drop_index("materials_tc_value_idx", table_name="materials")
    op.drop_index("materials_composition_idx", table_name="materials")
    op.drop_table("materials")

    op.drop_index("papers_doi_idx", table_name="papers")
    op.drop_table("papers")

    op.drop_index("users_role_idx", table_name="users")
    op.drop_table("users")