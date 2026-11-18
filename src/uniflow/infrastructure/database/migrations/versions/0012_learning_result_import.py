"""Validated learning result provenance; no mastery calculation."""
from alembic import op
import sqlalchemy as sa

revision = "0012_learning_result_import"
down_revision = "0011_learning_handoff_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "learning_results" not in existing:
        op.create_table("learning_results",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("learning_session_id", sa.String(80), sa.ForeignKey("learning_sessions.id"), nullable=False),
            sa.Column("learning_handoff_id", sa.String(36), sa.ForeignKey("learning_handoffs.id"), nullable=False, unique=True),
            sa.Column("artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
            sa.Column("schema_version", sa.String(20), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
            sa.Column("raw_input_sha256", sa.String(64), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("session_outcome", sa.String(20), nullable=False),
            sa.Column("processing_status", sa.String(30), nullable=False))
    columns = {col["name"] for col in sa.inspect(bind).get_columns("evidence")}
    if "learning_result_id" not in columns:
        # SQLite permits a nullable REFERENCES column through direct ALTER.
        bind.execute(sa.text("ALTER TABLE evidence ADD COLUMN learning_result_id VARCHAR(80) REFERENCES learning_results(id)"))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("evidence")}
    if "uq_evidence_result_skill" not in indexes:
        op.create_index("uq_evidence_result_skill", "evidence", ["learning_result_id", "skill_id"], unique=True)


def downgrade() -> None:
    raise RuntimeError("Learning result history migration is intentionally not reversible")
