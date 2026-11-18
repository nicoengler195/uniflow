"""Append-only explicit completion evidence; no performance or mastery writes."""
from alembic import op
revision = '0022_study_block_completion'
down_revision = '0021_study_block_changes'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE study_block_completions (
        id TEXT PRIMARY KEY, version TEXT NOT NULL CHECK(version='study-block-completion-v1'),
        block_id TEXT NOT NULL UNIQUE REFERENCES confirmed_study_blocks(id),
        expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),
        semester_id TEXT NOT NULL REFERENCES semesters(id), course_id TEXT NOT NULL REFERENCES courses(id),
        confirmed_at TEXT NOT NULL, actor TEXT NOT NULL CHECK(length(trim(actor))>0),
        origin TEXT NOT NULL CHECK(length(trim(origin))>0),
        session_id TEXT REFERENCES learning_sessions(id),
        evidence_type TEXT NOT NULL CHECK(evidence_type='USER_REPORTED' AND session_id IS NULL),
        request_id TEXT NOT NULL UNIQUE CHECK(length(trim(request_id))>0),
        payload_hash TEXT NOT NULL, provenance_json TEXT NOT NULL)''')
    op.execute('''CREATE TABLE study_block_completion_audit_events (
        id TEXT PRIMARY KEY, completion_id TEXT NOT NULL UNIQUE REFERENCES study_block_completions(id),
        request_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    for table in ('study_block_completions', 'study_block_completion_audit_events'):
        for action in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable completion record'); END")


def downgrade():
    raise RuntimeError('Completion provenance must be retained')
