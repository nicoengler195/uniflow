"""Explicit study confirmations; append-only provenance, no startup migration."""
from alembic import op
revision = '0020_study_blocks'
down_revision = '0019_scoring_mastery'
branch_labels = None
depends_on = None


def upgrade():
    for sql in (
        '''CREATE TABLE study_availability_windows (
        id TEXT PRIMARY KEY, semester_id TEXT NOT NULL REFERENCES semesters(id),
        timezone TEXT NOT NULL, local_day TEXT NOT NULL, starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision = 1),
        confirmed_at TEXT NOT NULL, confirmed_by TEXT NOT NULL CHECK(length(confirmed_by)>0),
        CHECK(starts_at < ends_at))''',
        '''CREATE TABLE study_confirmation_requests (
        id TEXT PRIMARY KEY CHECK(length(id)>0), payload_hash TEXT NOT NULL,
        preview_fingerprint TEXT NOT NULL, confirmed_at TEXT NOT NULL,
        confirmed_by TEXT NOT NULL CHECK(length(confirmed_by)>0), result_json TEXT NOT NULL)''',
        '''CREATE TABLE confirmed_study_blocks (
        id TEXT PRIMARY KEY, semester_id TEXT NOT NULL REFERENCES semesters(id),
        course_id TEXT NOT NULL REFERENCES courses(id), skill_ids_json TEXT NOT NULL,
        need_id TEXT NOT NULL, need_policy TEXT NOT NULL, title TEXT NOT NULL,
        starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, minutes INTEGER NOT NULL CHECK(minutes>0),
        timezone TEXT NOT NULL, availability_id TEXT NOT NULL REFERENCES study_availability_windows(id),
        status TEXT NOT NULL CHECK(status='CONFIRMED'), revision INTEGER NOT NULL CHECK(revision=1),
        request_id TEXT NOT NULL REFERENCES study_confirmation_requests(id),
        CHECK(starts_at < ends_at), UNIQUE(request_id,need_id))''',
        '''CREATE TABLE study_block_audit_events (
        id TEXT PRIMARY KEY, block_id TEXT NOT NULL UNIQUE REFERENCES confirmed_study_blocks(id),
        event_type TEXT NOT NULL CHECK(event_type='CREATED'), payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES study_confirmation_requests(id))''',
        'CREATE INDEX ix_study_blocks_semester_time ON confirmed_study_blocks(semester_id,starts_at)',
        'CREATE INDEX ix_study_availability_semester_day ON study_availability_windows(semester_id,local_day)',
    ): op.execute(sql)
    for table in ('study_availability_windows','study_confirmation_requests','confirmed_study_blocks','study_block_audit_events'):
        for action in ('UPDATE','DELETE'):
            op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable B1 record'); END")


def downgrade():
    raise RuntimeError('Study confirmation provenance must be retained')
