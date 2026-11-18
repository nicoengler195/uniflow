"""Initial UniFlow foundation schema.

Revision ID: 0001_initial_foundation
Revises:
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import MetaData, Table, UniqueConstraint

from uniflow.infrastructure.database.models import Base

revision = "0001_initial_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Freeze the original table/column shape while retaining the Phase 1 revision.
    added = {
        "semesters": {"timezone"},
        "courses": {"kind", "parent_course_id", "participation", "participation_reason", "unknowns_json", "audit_revision_id"},
        "assessments": {"stable_id", "audit_revision_id"},
        "deadlines": {"stable_id", "audit_revision_id"},
        "semester_audits": {"audit_id", "revision_number", "original_file_sha256",
                            "canonical_content_sha256", "stored_file_path"},
    }
    original_tables = {
        "source_references", "semesters", "courses", "materials", "assessments",
        "assessment_attempts", "deadlines", "skills", "skill_dependencies",
        "learning_sessions", "evidence", "mastery_states", "official_results", "semester_audits",
    }
    historical = MetaData()
    for name in original_tables:
        current = Base.metadata.tables[name]
        columns = [column._copy() for column in current.columns if column.name not in added.get(name, set())]
        constraints = []
        if name == "materials":
            constraints = [UniqueConstraint("content_hash", name="uq_materials_content_hash"),
                           UniqueConstraint("stable_id", name="uq_materials_stable_id")]
        Table(name, historical, *columns, *constraints)
    historical.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise NotImplementedError("Foundation downgrade is intentionally unsupported")
