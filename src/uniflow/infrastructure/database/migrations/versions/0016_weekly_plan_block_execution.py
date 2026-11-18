"""Record explicit accepted weekly block materialization."""
from alembic import op
import sqlalchemy as sa

revision = "0016_weekly_plan_block_execution"
down_revision = "0015_weekly_plan_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical 0001 creates current ORM metadata on fresh chains.
    if "weekly_plan_block_executions" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("weekly_plan_block_executions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("weekly_plan_revision_id", sa.String(36),
                      sa.ForeignKey("weekly_plan_revisions.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("block_id", sa.Text(), nullable=False),
            sa.Column("action_id", sa.Text(), nullable=False),
            sa.Column("execution_kind", sa.String(20), nullable=False),
            sa.Column("learning_session_id", sa.String(80),
                      sa.ForeignKey("learning_sessions.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("weekly_plan_revision_id", "block_id",
                                name="uq_weekly_block_execution_revision_block"),
            sa.CheckConstraint("execution_kind IN ('CREATED_SESSION','RESUMED_SESSION')",
                               name="ck_weekly_block_execution_kind"))


def downgrade() -> None:
    raise RuntimeError("Weekly block materialization history is intentionally not reversible")
