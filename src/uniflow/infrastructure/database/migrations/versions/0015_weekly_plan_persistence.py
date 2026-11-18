"""Persist generated weekly plan revisions and explicit acceptance history."""
from alembic import op
import sqlalchemy as sa

revision = "0015_weekly_plan_persistence"
down_revision = "0014_planning_calendar_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 reads current ORM metadata on fresh chains; guard already-created tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "weekly_plans" not in existing:
        op.create_table("weekly_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("semester_id", sa.String(36), sa.ForeignKey("semesters.id"), nullable=False),
            sa.Column("week_start", sa.Date(), nullable=False),
            sa.Column("accepted_revision_id", sa.String(36), sa.ForeignKey("weekly_plan_revisions.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("semester_id", "week_start", name="uq_weekly_plan_semester_week"))
    if "weekly_plan_revisions" not in existing:
        op.create_table("weekly_plan_revisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("weekly_plan_id", sa.String(36), sa.ForeignKey("weekly_plans.id"), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column("parent_revision_id", sa.String(36), sa.ForeignKey("weekly_plan_revisions.id")),
            sa.Column("revision_kind", sa.String(20), nullable=False),
            sa.Column("schema_version", sa.String(20), nullable=False),
            sa.Column("policy_version", sa.String(50), nullable=False),
            sa.Column("nba_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
            sa.Column("calendar_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
            sa.Column("weekly_schedule_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
            sa.Column("revision_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id"), nullable=False),
            sa.Column("nba_payload_sha256", sa.String(64), nullable=False),
            sa.Column("calendar_payload_sha256", sa.String(64), nullable=False),
            sa.Column("weekly_schedule_payload_sha256", sa.String(64), nullable=False),
            sa.Column("revision_payload_sha256", sa.String(64), nullable=False),
            sa.Column("nba_context_fingerprint", sa.String(64), nullable=False),
            sa.Column("calendar_context_fingerprint", sa.String(64), nullable=False),
            sa.Column("scheduler_context_fingerprint", sa.String(64), nullable=False),
            sa.Column("source_as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_course_filter_id", sa.String(120)),
            sa.Column("source_limit", sa.Integer(), nullable=False),
            sa.Column("duration_overrides_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("weekly_plan_id", "revision_number", name="uq_weekly_plan_revision_number"),
            sa.CheckConstraint("revision_number >= 1", name="ck_weekly_plan_revision_positive"),
            sa.CheckConstraint("revision_kind IN ('GENERATED','MANUAL_EDIT')", name="ck_weekly_plan_revision_kind"),
            sa.CheckConstraint("source_limit BETWEEN 1 AND 100", name="ck_weekly_plan_source_limit"))
    if "weekly_plan_acceptances" not in existing:
        op.create_table("weekly_plan_acceptances",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("weekly_plan_id", sa.String(36), sa.ForeignKey("weekly_plans.id"), nullable=False),
            sa.Column("weekly_plan_revision_id", sa.String(36), sa.ForeignKey("weekly_plan_revisions.id"), nullable=False, unique=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("actor", sa.String(20), nullable=False),
            sa.CheckConstraint("actor = 'MANUAL_USER'", name="ck_weekly_plan_acceptance_actor"))


def downgrade() -> None:
    raise RuntimeError("Weekly plan history migration is intentionally not reversible")
