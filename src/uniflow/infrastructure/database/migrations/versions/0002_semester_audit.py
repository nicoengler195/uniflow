"""Phase 2 audit identity, hierarchy, and derived facts.

Revision ID: 0002_semester_audit
Revises: 0001_initial_foundation
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_semester_audit"
down_revision = "0001_initial_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def add(table: str, column: sa.Column) -> None:
        if column.name not in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
            if column.foreign_keys:
                target = next(iter(column.foreign_keys)).target_fullname
                ref_table, ref_column = target.split(".")
                op.execute(f"ALTER TABLE {table} ADD COLUMN {column.name} VARCHAR(36) REFERENCES {ref_table}({ref_column})")
            else:
                op.add_column(table, column)

    add("semesters", sa.Column("timezone", sa.String(80), nullable=False, server_default="Europe/Berlin"))
    for column in (
        sa.Column("kind", sa.String(20), nullable=False, server_default="MODULE"),
        sa.Column("parent_course_id", sa.String(36), sa.ForeignKey("courses.id")),
        sa.Column("participation", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("participation_reason", sa.Text()),
        sa.Column("unknowns_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id")),
    ):
        add("courses", column)
    for table in ("assessments", "deadlines"):
        add(table, sa.Column("stable_id", sa.String(160)))
        add(table, sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id")))
    for column in (
        sa.Column("audit_id", sa.String(160)),
        sa.Column("revision_number", sa.Integer()),
        sa.Column("original_file_sha256", sa.String(64)),
        sa.Column("canonical_content_sha256", sa.String(64)),
        sa.Column("stored_file_path", sa.Text()),
    ):
        add("semester_audits", column)
    for table in ("schedule_entries", "schedule_exceptions", "material_release_rules", "assessment_requirements"):
        if not inspector.has_table(table):
            op.create_table(
                table,
                sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("stable_id", sa.String(160), nullable=False, unique=True),
                sa.Column("course_id", sa.String(36), sa.ForeignKey("courses.id"), nullable=False),
                sa.Column("payload_json", sa.Text(), nullable=False),
                sa.Column("information_status", sa.Enum("CONFIRMED", "OBSERVED", "INFERRED", "UNKNOWN"), nullable=False),
                sa.Column("source_id", sa.String(36), sa.ForeignKey("source_references.id")),
                sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id")),
                sa.Column("created_at", sa.DateTime(), nullable=False),
                sa.Column("updated_at", sa.DateTime(), nullable=False),
            )
    if not inspector.has_table("audit_fact_records"):
        op.create_table(
            "audit_fact_records",
            sa.Column("stable_id", sa.String(160), primary_key=True),
            sa.Column("entity_type", sa.String(60), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("information_status", sa.Enum("CONFIRMED", "OBSERVED", "INFERRED", "UNKNOWN"), nullable=False),
            sa.Column("source_id", sa.String(36), sa.ForeignKey("source_references.id")),
            sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    indexes = {
        "assessments": ("ux_assessments_stable_id", ("stable_id",)),
        "deadlines": ("ux_deadlines_stable_id", ("stable_id",)),
        "semester_audits": ("ux_audit_semester_revision", ("semester_id", "revision_number")),
    }
    for table, (name, cols) in indexes.items():
        existing = {i["name"] for i in sa.inspect(bind).get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, list(cols), unique=True)
    if "canonical_content_sha256" not in {i["name"] for i in sa.inspect(bind).get_unique_constraints("semester_audits")}:
        existing = {i["name"] for i in sa.inspect(bind).get_indexes("semester_audits")}
        if "ux_audit_canonical_hash" not in existing:
            op.create_index("ux_audit_canonical_hash", "semester_audits", ["canonical_content_sha256"], unique=True)


def downgrade() -> None:
    raise NotImplementedError("Phase 2 data-preserving downgrade is intentionally unsupported")
