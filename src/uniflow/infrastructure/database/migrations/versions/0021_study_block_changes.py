"""Explicit append-only study block changes; B1 records and triggers stay intact."""
from alembic import op
revision = '0021_study_block_changes'
down_revision = '0020_study_blocks'
branch_labels = None
depends_on = None


def upgrade():
    for sql in (
        '''CREATE TABLE study_block_change_requests (
        id TEXT PRIMARY KEY CHECK(length(id)>0), payload_hash TEXT NOT NULL,
        preview_fingerprint TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision>=1),
        confirmed_at TEXT NOT NULL, confirmed_by TEXT NOT NULL CHECK(length(trim(confirmed_by))>0),
        origin TEXT NOT NULL CHECK(length(trim(origin))>0), result_json TEXT NOT NULL)''',
        '''CREATE TABLE study_block_revisions (
        block_id TEXT NOT NULL REFERENCES confirmed_study_blocks(id),
        revision INTEGER NOT NULL CHECK(revision>=2), previous_revision INTEGER NOT NULL,
        change_kind TEXT NOT NULL CHECK(change_kind IN ('ADJUST','CANCEL')),
        state_json TEXT NOT NULL, confirmed_at TEXT NOT NULL, confirmed_by TEXT NOT NULL,
        origin TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE REFERENCES study_block_change_requests(id),
        PRIMARY KEY(block_id,revision), CHECK(previous_revision=revision-1))''',
        '''CREATE TABLE study_block_change_audit_events (
        id TEXT PRIMARY KEY, block_id TEXT NOT NULL, revision INTEGER NOT NULL,
        request_id TEXT NOT NULL UNIQUE REFERENCES study_block_change_requests(id),
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(block_id,revision) REFERENCES study_block_revisions(block_id,revision))''',
        '''CREATE TRIGGER study_block_revision_chain BEFORE INSERT ON study_block_revisions
        WHEN NEW.previous_revision != COALESCE((SELECT MAX(revision) FROM study_block_revisions WHERE block_id=NEW.block_id),1)
        BEGIN SELECT RAISE(ABORT,'invalid study revision chain'); END''',
    ): op.execute(sql)
    for table in ('study_block_change_requests','study_block_revisions','study_block_change_audit_events'):
        for action in ('UPDATE','DELETE'):
            op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable B2 record'); END")


def downgrade():
    raise RuntimeError('Study change provenance must be retained')
