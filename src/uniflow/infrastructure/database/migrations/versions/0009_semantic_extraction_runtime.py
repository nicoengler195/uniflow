"""Offline document semantic extraction attempts and provenance."""
from alembic import op
import sqlalchemy as sa

revision = "0009_semantic_extraction_runtime"
down_revision = "0008_skill_extraction_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "semantic_extraction_attempts" not in inspector.get_table_names():
        op.create_table("semantic_extraction_attempts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("semantic_extraction_runs.id"), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("adapter_name", sa.String(80), nullable=False),
            sa.Column("adapter_version", sa.String(40), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("effective_config_json", sa.Text(), nullable=False),
            sa.Column("effective_config_sha256", sa.String(64), nullable=False),
            sa.Column("prompt_schema_version", sa.String(80)),
            sa.Column("provider_name", sa.String(80)),
            sa.Column("model_name", sa.String(120)),
            sa.Column("input_package_sha256", sa.String(64)),
            sa.Column("input_manifest_json", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("output_sha256", sa.String(64)),
            sa.Column("output_artifact_id", sa.String(36), sa.ForeignKey("derived_artifacts.id")),
            sa.Column("error_code", sa.String(80)),
            sa.Column("error_message", sa.String(500)),
            sa.Column("external_disclosure_allowed", sa.Boolean(), nullable=False),
            sa.Column("candidate_count", sa.Integer(), nullable=False),
            sa.Column("proposal_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "attempt_number", name="uq_semantic_attempt_number"),
            sa.CheckConstraint("status IN ('PLANNED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="ck_semantic_attempt_status"))
    inspector = sa.inspect(connection)
    existing_indexes = {i["name"] for i in inspector.get_indexes("semantic_extraction_attempts")}
    if "ix_semantic_extraction_attempts_run_id" not in existing_indexes:
        op.create_index("ix_semantic_extraction_attempts_run_id", "semantic_extraction_attempts", ["run_id"])
    for table in ("semantic_evidence_spans", "skill_proposals"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "extraction_attempt_id" not in columns:
            # SQLite accepts a nullable REFERENCES column directly; Alembic's
            # add_column(ForeignKey) tries an unsupported separate ALTER.
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN extraction_attempt_id VARCHAR(36) REFERENCES semantic_extraction_attempts(id)"))


def downgrade() -> None:
    raise RuntimeError("3D2B1 attempt history migration is intentionally not reversible")
