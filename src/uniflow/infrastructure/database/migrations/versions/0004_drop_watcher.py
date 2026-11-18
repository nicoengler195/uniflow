"""Persistent Drop processing history.

Revision ID: 0004_drop_watcher
Revises: 0003_material_intake
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_drop_watcher"
down_revision = "0003_material_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drop_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("observed_filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(80)),
        sa.Column("source_artifact_id", sa.String(36), sa.ForeignKey("source_artifacts.id")),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("source_path", "size_bytes", "mtime_ns", name="uq_drop_observation"),
    )


def downgrade() -> None:
    raise NotImplementedError("Drop history downgrade would discard processing records")
