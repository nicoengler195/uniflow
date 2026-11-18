"""Attempt-level external semantic authorization provenance."""
from alembic import op
import sqlalchemy as sa

revision = "0010_external_semantic_adapter"
down_revision = "0009_semantic_extraction_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("semantic_extraction_attempts")}
    additions = (
        sa.Column("authorization_actor", sa.String(40)),
        sa.Column("authorization_at", sa.DateTime(timezone=True)),
        sa.Column("authorization_policy_version", sa.String(40)),
        sa.Column("authorized_item_count", sa.Integer()),
        sa.Column("authorized_utf8_bytes", sa.Integer()),
        sa.Column("external_request_started_at", sa.DateTime(timezone=True)),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("semantic_extraction_attempts", column)


def downgrade() -> None:
    raise RuntimeError("External authorization history migration is intentionally not reversible")
