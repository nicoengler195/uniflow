"""Versioned result mastery lineage and v1 duplicate protection."""
from alembic import op
import sqlalchemy as sa

revision = "0013_mastery_calculation"
down_revision = "0012_learning_result_import"
branch_labels = None
depends_on = None

INDEX = "uq_mastery_result_observed_v1_evidence_method"
METHOD = "learning-result-observed-dimensions-v1"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("mastery_states")}
    if "previous_mastery_state_id" not in columns:
        bind.execute(sa.text("ALTER TABLE mastery_states ADD COLUMN previous_mastery_state_id VARCHAR(36) REFERENCES mastery_states(id)"))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("mastery_states")}
    if INDEX not in indexes:
        op.create_index(INDEX, "mastery_states", ["evidence_id", "calculation_method"], unique=True,
            sqlite_where=sa.text(f"evidence_id IS NOT NULL AND calculation_method = '{METHOD}'"))


def downgrade() -> None:
    raise RuntimeError("Mastery history migration is intentionally not reversible")
