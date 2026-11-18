"""Separate physical artifacts from logical materials.

Revision ID: 0003_material_intake
Revises: 0002_semester_audit
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_material_intake"
down_revision = "0002_semester_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("original_source_path", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(40)),
        sa.Column("mime_type", sa.String(160)),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "material_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("source_artifacts.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("source_references.id")),
        sa.UniqueConstraint("material_id", "version_number", name="uq_material_version"),
        sa.UniqueConstraint("material_id", "artifact_id", name="uq_material_artifact"),
    )
    op.create_index(
        "ux_material_current_artifact", "material_artifacts", ["material_id"],
        unique=True, sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    raise NotImplementedError("Material intake downgrade would discard artifact history")
