"""Explicit learning targets and immutable handoff revision metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0011_learning_handoff_generation"
down_revision = "0010_external_semantic_adapter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "learning_session_target_skills" not in existing:
        op.create_table("learning_session_target_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("learning_session_id", sa.String(80), sa.ForeignKey("learning_sessions.id"), nullable=False),
        sa.Column("skill_id", sa.String(120), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("learning_session_id", "skill_id", name="uq_session_target_skill"),
        sa.UniqueConstraint("learning_session_id", "ordinal", name="uq_session_target_ordinal"))
    if "learning_handoffs" not in existing:
        op.create_table("learning_handoffs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("learning_session_id", sa.String(80), sa.ForeignKey("learning_sessions.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("protocol_version", sa.String(40), nullable=False),
        sa.Column("context_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_handoff_id", sa.String(36), sa.ForeignKey("learning_handoffs.id")),
        sa.UniqueConstraint("learning_session_id", "revision", name="uq_learning_handoff_revision"))
    # SQLite supports adding nullable REFERENCES columns directly.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("learning_sessions")}
    if "current_handoff_id" not in columns:
        op.get_bind().execute(sa.text("ALTER TABLE learning_sessions ADD COLUMN current_handoff_id VARCHAR(36) REFERENCES learning_handoffs(id)"))


def downgrade() -> None:
    raise RuntimeError("Learning handoff history migration is intentionally not reversible")
