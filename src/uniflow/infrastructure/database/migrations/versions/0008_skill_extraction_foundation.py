"""Skill curation and exact semantic source provenance."""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa

revision = "0008_skill_extraction_foundation"
down_revision = "0007_academic_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # The committed 0001 migration reads live ORM metadata. On an empty fresh
    # install it therefore sees today's Skill columns and dependency key before
    # 0008 runs. Normalize that empty shape back to the committed 0007 layout
    # here, so the historical migration itself can remain byte-for-byte intact.
    skill_columns = {column["name"] for column in sa.inspect(connection).get_columns("skills")}
    if "kind" in skill_columns:
        for name in ("skills", "skill_dependencies"):
            if connection.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar_one():
                raise RuntimeError("Unexpected populated pre-0008 Skill schema")
        op.drop_table("skill_dependencies")
        op.drop_table("skills")
        op.create_table("skills",
            sa.Column("id", sa.String(120), primary_key=True),
            sa.Column("name", sa.String(240), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("domain", sa.String(120)),
            sa.Column("last_validated_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
        op.create_table("skill_dependencies",
            sa.Column("prerequisite_skill_id", sa.String(120), sa.ForeignKey("skills.id"), primary_key=True),
            sa.Column("dependent_skill_id", sa.String(120), sa.ForeignKey("skills.id"), primary_key=True),
            sa.Column("source_id", sa.String(36), sa.ForeignKey("source_references.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.execute("ALTER TABLE skills ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN' CHECK (kind IN ('UNKNOWN','CONCEPT','ABILITY'))")
    op.execute("ALTER TABLE skills ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','MERGED'))")
    op.execute("ALTER TABLE skills ADD COLUMN canonical_skill_id VARCHAR(120) REFERENCES skills(id)")

    # The Phase 1 composite key cannot retain inactive history. Copy every
    # legacy edge with its timestamps and SourceReference into a UUID-keyed row.
    op.rename_table("skill_dependencies", "skill_dependencies_legacy_3d2a")
    from uniflow.infrastructure.database.models import Base
    historical_shape = sa.MetaData()
    for source_table in Base.metadata.tables.values():
        source_table.to_metadata(historical_shape)
    for name in ("semantic_extraction_runs", "semantic_evidence_spans", "skill_proposals",
                 "skill_proposal_evidence", "skill_aliases", "course_skills", "skill_dependencies"):
        historical_shape.tables[name].create(bind=op.get_bind())
    rows = connection.execute(sa.text("SELECT prerequisite_skill_id, dependent_skill_id, source_id, created_at, updated_at FROM skill_dependencies_legacy_3d2a")).mappings()
    for row in rows:
        connection.execute(sa.text("INSERT INTO skill_dependencies (id, prerequisite_skill_id, dependent_skill_id, source_id, created_at, updated_at, is_active) VALUES (:id, :pre, :dep, :source, :created, :updated, 1)"),
            {"id": str(uuid.uuid4()), "pre": row["prerequisite_skill_id"], "dep": row["dependent_skill_id"],
             "source": row["source_id"], "created": row["created_at"], "updated": row["updated_at"]})
    op.drop_table("skill_dependencies_legacy_3d2a")


def downgrade() -> None:
    raise RuntimeError("3D2A provenance/history migration is intentionally not reversible")
