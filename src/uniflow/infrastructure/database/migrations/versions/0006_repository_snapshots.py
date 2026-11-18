"""Repository source, snapshot, file, remote, and immutable blob lineage."""
from alembic import op
import sqlalchemy as sa

revision = "0006_repository_snapshots"
down_revision = "0005_document_parsing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("repository_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("stable_id", sa.String(160), nullable=False, unique=True),
        sa.Column("local_path", sa.Text(), nullable=False, unique=True),
        sa.Column("provider_hint", sa.String(80)),
        sa.Column("course_id", sa.String(36), sa.ForeignKey("courses.id")),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("assessments.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("repository_remotes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), sa.ForeignKey("repository_sources.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("sanitized_identity", sa.Text(), nullable=False),
        sa.UniqueConstraint("repository_source_id", "name", name="uq_repository_remote_name"),
    )
    op.create_table("repository_blobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("repository_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), sa.ForeignKey("repository_sources.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_identity", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(20), nullable=False),
        sa.Column("include_untracked", sa.Boolean(), nullable=False),
        sa.Column("head_commit_oid", sa.String(64)),
        sa.Column("branch_name", sa.Text()),
        sa.Column("is_detached", sa.Boolean(), nullable=False),
        sa.Column("is_dirty", sa.Boolean(), nullable=False),
        sa.Column("tracked_file_count", sa.Integer(), nullable=False),
        sa.Column("untracked_file_count", sa.Integer(), nullable=False),
        sa.Column("deleted_file_count", sa.Integer(), nullable=False),
        sa.Column("blob_count", sa.Integer(), nullable=False),
        sa.Column("build_system_hint", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.UniqueConstraint("repository_source_id", "snapshot_identity", name="uq_repository_snapshot_identity"),
    )
    op.create_table("repository_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("repository_snapshots.id"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("original_path", sa.Text()),
        sa.Column("entry_kind", sa.String(30), nullable=False),
        sa.Column("tracked_state", sa.String(30), nullable=False),
        sa.Column("status_x", sa.String(2)),
        sa.Column("status_y", sa.String(2)),
        sa.Column("head_git_oid", sa.String(64)),
        sa.Column("index_git_oid", sa.String(64)),
        sa.Column("head_blob_id", sa.String(36), sa.ForeignKey("repository_blobs.id")),
        sa.Column("index_blob_id", sa.String(36), sa.ForeignKey("repository_blobs.id")),
        sa.Column("working_blob_id", sa.String(36), sa.ForeignKey("repository_blobs.id")),
        sa.Column("head_mode", sa.String(10)),
        sa.Column("index_mode", sa.String(10)),
        sa.Column("working_size_bytes", sa.BigInteger()),
        sa.Column("language_hint", sa.String(30), nullable=False),
        sa.UniqueConstraint("snapshot_id", "path", name="uq_repository_file_path"),
    )


def downgrade() -> None:
    raise NotImplementedError("Repository snapshot downgrade would discard history")
