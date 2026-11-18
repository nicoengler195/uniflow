"""Exact scoring snapshots and append-only local human review events."""
from alembic import op
import sqlalchemy as sa

revision = "0018_scoring_review"
down_revision = "0017_audit_resolution"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scoring_result_snapshots",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("learning_sessions.id"), nullable=False),
        sa.Column("handoff_id", sa.String(36), sa.ForeignKey("learning_handoffs.id"), nullable=False),
        sa.Column("handoff_revision", sa.Integer(), nullable=False),
        sa.Column("handoff_sha256", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(40), nullable=False),
        sa.Column("raw_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("allowed_skill_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("scoring_review_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_sha256", sa.String(64), sa.ForeignKey("scoring_result_snapshots.sha256"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decisions_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("result_sha256", "version", name="uq_scoring_review_version"),
        sa.CheckConstraint("version > 0", name="ck_scoring_review_version"),
        sa.CheckConstraint("origin = 'LOCAL_GUI_CONFIRMATION'", name="ck_scoring_review_origin"))
    for table in ("scoring_result_snapshots", "scoring_review_events"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'immutable scoring review record'); END")


def downgrade():
    raise RuntimeError("Persistent human review history must be retained")
