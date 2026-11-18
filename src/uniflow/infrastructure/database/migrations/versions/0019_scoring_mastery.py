"""Reviewed scoring mastery origin and per-snapshot/skill replay protection."""
from alembic import op
import sqlalchemy as sa

revision = "0019_scoring_mastery"
down_revision = "0018_scoring_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scoring_mastery_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_sha256", sa.String(64), sa.ForeignKey("scoring_result_snapshots.sha256"), nullable=False),
        sa.Column("review_event_id", sa.String(36), sa.ForeignKey("scoring_review_events.id"), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("learning_sessions.id"), nullable=False),
        sa.Column("handoff_id", sa.String(36), sa.ForeignKey("learning_handoffs.id"), nullable=False),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("policy_id", sa.String(80), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("score_points", sa.String(80), nullable=False),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("evidence.id"), nullable=False),
        sa.Column("previous_state_id", sa.String(36), sa.ForeignKey("mastery_states.id")),
        sa.Column("new_state_id", sa.String(36), sa.ForeignKey("mastery_states.id"), nullable=False, unique=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.UniqueConstraint("result_sha256", "skill_id", name="uq_scoring_mastery_origin_skill"))
    for action in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER scoring_mastery_no_{action.lower()} BEFORE {action} ON scoring_mastery_applications "
                   "BEGIN SELECT RAISE(ABORT, 'immutable mastery application'); END")


def downgrade():
    raise RuntimeError("Mastery application provenance must be retained")
