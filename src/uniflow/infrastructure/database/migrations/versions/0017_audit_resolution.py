"""Preserve exact human-approved semester audit replacements."""
from alembic import op
import sqlalchemy as sa

revision = "0017_audit_resolution"
down_revision = "0016_weekly_plan_block_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 creates current ORM metadata on fresh databases in this project.
    if "audit_resolution_records" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "audit_resolution_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id"), nullable=False),
            sa.Column("entity_type", sa.String(60), nullable=False),
            sa.Column("stable_id", sa.String(160), nullable=False),
            sa.Column("old_fact_sha256", sa.String(64), nullable=False),
            sa.Column("new_fact_sha256", sa.String(64), nullable=False),
            sa.Column("old_payload_json", sa.Text(), nullable=False),
            sa.Column("new_payload_json", sa.Text(), nullable=False),
            sa.Column("old_information_status", sa.Enum("UNKNOWN", "INFERRED", "OBSERVED", "CONFIRMED", name="informationstatus"), nullable=False),
            sa.Column("new_information_status", sa.Enum("UNKNOWN", "INFERRED", "OBSERVED", "CONFIRMED", name="informationstatus"), nullable=False),
            sa.Column("previous_audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id"), nullable=False),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("actor", sa.String(80), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolution_raw_sha256", sa.String(64), nullable=False),
            sa.Column("resolution_canonical_sha256", sa.String(64), nullable=False),
            sa.Column("resolution_stored_file_path", sa.Text(), nullable=False),
            sa.UniqueConstraint("audit_revision_id", "entity_type", "stable_id", name="uq_audit_resolution_revision_fact"),
        )


def downgrade() -> None:
    raise RuntimeError("Audit resolution provenance is intentionally not reversible")
