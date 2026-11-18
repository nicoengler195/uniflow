"""Semester-scoped personal calendar foundation."""
from alembic import op
import sqlalchemy as sa

revision = "0014_planning_calendar_foundation"
down_revision = "0013_mastery_calculation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 historically creates from current ORM metadata. Guard fresh chains.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "planning_profiles" not in existing:
        op.create_table("planning_profiles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("semester_id", sa.String(36), sa.ForeignKey("semesters.id"), nullable=False, unique=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("default_action_duration_minutes", sa.Integer()),
            sa.Column("max_daily_study_minutes", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("revision >= 1", name="ck_planning_profile_revision"),
            sa.CheckConstraint("default_action_duration_minutes IS NULL OR default_action_duration_minutes > 0", name="ck_planning_default_duration"),
            sa.CheckConstraint("max_daily_study_minutes IS NULL OR max_daily_study_minutes > 0", name="ck_planning_daily_limit"))
    if "availability_windows" not in existing:
        op.create_table("availability_windows",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("planning_profile_id", sa.String(36), sa.ForeignKey("planning_profiles.id"), nullable=False),
            sa.Column("weekday", sa.String(9), nullable=False),
            sa.Column("start_local_time", sa.Time(), nullable=False),
            sa.Column("end_local_time", sa.Time(), nullable=False),
            sa.Column("start_fold", sa.Integer()), sa.Column("end_fold", sa.Integer()),
            sa.Column("valid_from", sa.Date()), sa.Column("valid_until", sa.Date()),
            sa.Column("created_revision", sa.Integer(), nullable=False),
            sa.Column("deactivated_revision", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deactivated_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint("start_local_time < end_local_time", name="ck_availability_order"),
            sa.CheckConstraint("valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until", name="ck_availability_validity"),
            sa.CheckConstraint("start_fold IS NULL OR start_fold IN (0,1)", name="ck_availability_start_fold"),
            sa.CheckConstraint("end_fold IS NULL OR end_fold IN (0,1)", name="ck_availability_end_fold"),
            sa.CheckConstraint("created_revision >= 2", name="ck_availability_created_revision"),
            sa.CheckConstraint("deactivated_revision IS NULL OR deactivated_revision > created_revision", name="ck_availability_deactivated_revision"))
    if "calendar_constraints" not in existing:
        op.create_table("calendar_constraints",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("planning_profile_id", sa.String(36), sa.ForeignKey("planning_profiles.id"), nullable=False),
            sa.Column("constraint_type", sa.String(24), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True)), sa.Column("ends_at", sa.DateTime(timezone=True)),
            sa.Column("local_date", sa.Date()), sa.Column("label", sa.Text()),
            sa.Column("created_revision", sa.Integer(), nullable=False),
            sa.Column("deactivated_revision", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deactivated_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint("constraint_type IN ('BLOCKED','AVAILABLE_ADDITION')", name="ck_calendar_constraint_type"),
            sa.CheckConstraint("(starts_at IS NOT NULL AND ends_at IS NOT NULL AND local_date IS NULL) OR (starts_at IS NULL AND ends_at IS NULL AND local_date IS NOT NULL)", name="ck_calendar_constraint_shape"),
            sa.CheckConstraint("starts_at IS NULL OR starts_at < ends_at", name="ck_calendar_constraint_order"),
            sa.CheckConstraint("created_revision >= 2", name="ck_calendar_constraint_created_revision"),
            sa.CheckConstraint("deactivated_revision IS NULL OR deactivated_revision > created_revision", name="ck_calendar_constraint_deactivated_revision"))


def downgrade() -> None:
    raise RuntimeError("Personal calendar history migration is intentionally not reversible")
