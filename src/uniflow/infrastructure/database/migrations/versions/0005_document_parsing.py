"""Derived document text and versioned parse history.

Revision ID: 0005_document_parsing
Revises: 0004_drop_watcher
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_document_parsing"
down_revision = "0004_drop_watcher"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("derived_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("parse_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_artifact_id", sa.String(36), sa.ForeignKey("source_artifacts.id"), nullable=False),
        sa.Column("parser_name", sa.String(80), nullable=False),
        sa.Column("parser_version", sa.String(40), nullable=False),
        sa.Column("dependency_version", sa.String(80), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("derived_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id")),
        sa.Column("page_count", sa.Integer()),
        sa.Column("unit_count", sa.Integer()),
        sa.Column("char_count", sa.Integer()),
        sa.Column("warning_json", sa.Text()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("source_artifact_id", "parser_name", "parser_version", "dependency_version",
                            "config_sha256", "attempt_number", name="uq_parse_attempt"),
    )
    op.create_index("ix_parse_runs_source", "parse_runs", ["source_artifact_id"])
    op.create_table("parsed_documents",
        sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), primary_key=True),
        sa.Column("format", sa.String(20), nullable=False),
        sa.Column("derived_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
        sa.Column("normalized_text_sha256", sa.String(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("non_whitespace_char_count", sa.Integer(), nullable=False),
        sa.Column("unit_count", sa.Integer(), nullable=False),
    )
    op.create_table("parsed_units",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("unit_kind", sa.String(20), nullable=False),
        sa.Column("source_label", sa.String(80)),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("non_whitespace_char_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("parse_run_id", "ordinal", name="uq_parsed_unit_ordinal"),
    )


def downgrade() -> None:
    raise NotImplementedError("Document parsing downgrade would discard parse provenance")
