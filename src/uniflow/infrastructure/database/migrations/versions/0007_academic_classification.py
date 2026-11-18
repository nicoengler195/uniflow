"""Manual academic proposals and history-preserving confirmed links."""
from alembic import op
import sqlalchemy as sa

revision = "0007_academic_classification"
down_revision = "0006_repository_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("classification_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("proposal_kind", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("identity_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject_source_artifact_id", sa.String(36), sa.ForeignKey("source_artifacts.id")),
        sa.Column("subject_repository_source_id", sa.String(36), sa.ForeignKey("repository_sources.id")),
        sa.Column("subject_material_id", sa.String(36), sa.ForeignKey("materials.id")),
        sa.Column("classifier_name", sa.String(80), nullable=False),
        sa.Column("classifier_version", sa.String(40), nullable=False),
        sa.Column("classifier_config_sha256", sa.String(64)),
        sa.Column("confidence", sa.Float()),
        sa.Column("evidence_json", sa.Text()),
        sa.Column("proposed_course_id", sa.String(36), sa.ForeignKey("courses.id")),
        sa.Column("proposed_assessment_id", sa.String(36), sa.ForeignKey("assessments.id")),
        sa.Column("proposed_material_id", sa.String(36), sa.ForeignKey("materials.id")),
        sa.Column("proposed_material_stable_id", sa.String(160)),
        sa.Column("proposed_material_title", sa.String(240)),
        sa.Column("proposed_material_type", sa.String(80)),
        sa.Column("proposed_expectation_rule_id", sa.String(36), sa.ForeignKey("material_release_rules.id")),
        sa.Column("proposed_expectation_date", sa.Date()),
        sa.Column("proposed_expectation_identity", sa.String(192)),
        sa.Column("replaces_link_id", sa.String(36)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_actor", sa.String(40)),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("superseded_by_proposal_id", sa.String(36), sa.ForeignKey("classification_proposals.id")),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_classification_confidence"),
        sa.CheckConstraint("(subject_source_artifact_id IS NOT NULL) + (subject_repository_source_id IS NOT NULL) + (subject_material_id IS NOT NULL) = 1", name="ck_classification_one_subject"),
    )
    op.create_index("ix_classification_pending_identity", "classification_proposals", ["identity_sha256"], unique=True, sqlite_where=sa.text("status = 'PENDING'"))
    with op.batch_alter_table("material_artifacts") as batch:
        batch.add_column(sa.Column("classification_proposal_id", sa.String(36), sa.ForeignKey("classification_proposals.id", name="fk_material_artifact_classification_proposal")))
        batch.add_column(sa.Column("deactivated_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("deactivation_reason", sa.Text()))
        batch.add_column(sa.Column("replacement_link_id", sa.String(36), sa.ForeignKey("material_artifacts.id", name="fk_material_artifact_replacement")))
    op.create_table("repository_academic_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), sa.ForeignKey("repository_sources.id"), nullable=False),
        sa.Column("course_id", sa.String(36), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("assessments.id")),
        sa.Column("classification_proposal_id", sa.String(36), sa.ForeignKey("classification_proposals.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivation_reason", sa.Text()),
        sa.Column("replacement_link_id", sa.String(36), sa.ForeignKey("repository_academic_links.id")),
    )
    op.create_index("ix_repository_active_link", "repository_academic_links", ["repository_source_id"], unique=True, sqlite_where=sa.text("is_active = 1"))
    # Existing explicitly registered academic pointers remain authoritative and
    # receive a provenance-marked legacy history row without changing the pointer.
    op.execute(sa.text("""INSERT INTO repository_academic_links
        (id, repository_source_id, course_id, assessment_id, classification_proposal_id, created_at, is_active)
        SELECT lower(hex(randomblob(16))), id, course_id, assessment_id, NULL, created_at, 1
        FROM repository_sources WHERE course_id IS NOT NULL"""))
    op.create_table("material_expectation_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("materials.id"), nullable=False),
        sa.Column("release_rule_id", sa.String(36), sa.ForeignKey("material_release_rules.id"), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("expectation_identity", sa.String(192), nullable=False),
        sa.Column("rule_fingerprint", sa.String(64), nullable=False),
        sa.Column("audit_revision_id", sa.String(36), sa.ForeignKey("semester_audits.id")),
        sa.Column("rule_source_id", sa.String(36), sa.ForeignKey("source_references.id")),
        sa.Column("classification_proposal_id", sa.String(36), sa.ForeignKey("classification_proposals.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivation_reason", sa.Text()),
        sa.Column("replacement_link_id", sa.String(36), sa.ForeignKey("material_expectation_links.id")),
    )
    op.create_index("ix_material_expectation_active", "material_expectation_links", ["material_id", "expectation_identity"], unique=True, sqlite_where=sa.text("is_active = 1"))


def downgrade() -> None:
    raise NotImplementedError("Academic classification downgrade would discard history")
