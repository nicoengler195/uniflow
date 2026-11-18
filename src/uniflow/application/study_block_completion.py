"""C1 explicit user reports. Completion is never a mastery or scoring claim.

Session identity, course and target skills alone do not establish that a session
performed this block. Until a block/session provenance contract exists, linking
is deliberately rejected, including for existing sessions with similar content.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from uuid import uuid4
from sqlalchemy import inspect, text
from uniflow.application.application_read_models import _read_transaction
from uniflow.application.planning_calendar import _semester
from uniflow.application.study_blocks import rows, canonical, digest, stamp, write
from uniflow.application.study_block_changes import effective_blocks

VERSION = 'study-block-completion-v1'
TABLES = ('study_block_completions', 'study_block_completion_audit_events')


@dataclass(frozen=True)
class StudyBlockCompletionV1:
    id: str
    version: str
    block_id: str
    expected_revision: int
    semester_id: str
    course_id: str
    confirmed_at: str
    actor: str
    origin: str
    session_id: str | None
    evidence_type: str
    request_id: str
    payload_hash: str
    provenance_json: str


@dataclass(frozen=True)
class StudyBlockCompletionPreviewV1:
    version: str
    semester_id: str
    block_id: str
    expected_revision: int
    origin: str
    state_json: str
    fingerprint: str


@dataclass(frozen=True)
class StudyBlockProgressV1:
    version: str
    block_id: str
    semester_id: str
    course_id: str
    revision: int
    status: str
    need_id: str | None
    need_policy: str | None
    starts_at: str
    ends_at: str
    completion: StudyBlockCompletionV1 | None


def require_completion(db):
    if not set(TABLES).issubset(inspect(db.connection()).get_table_names()):
        raise ValueError('STUDY_BLOCK_COMPLETION_SCHEMA_REQUIRED: explicit migration 0022 required')


def _preview(db, semester_id, block_id, expected_revision, origin, session_id=None):
    require_completion(db)
    if type(expected_revision) is not int or expected_revision < 1 or not isinstance(block_id,str) or not block_id.strip():
        raise ValueError('INVALID_COMPLETION_COMMAND')
    if not isinstance(origin,str) or not origin.strip(): raise ValueError('INVALID_ORIGIN')
    if session_id is not None: raise ValueError('SESSION_LINK_PROVENANCE_UNAVAILABLE')
    semester = _semester(db, semester_id)
    block = next((b for b in effective_blocks(db,semester.id,include_cancelled=True) if b['id']==block_id),None)
    if block is None: raise ValueError('UNKNOWN_STUDY_BLOCK')
    if block['revision'] != expected_revision: raise ValueError('STALE_REVISION')
    if block['status'] != 'CONFIRMED': raise ValueError('BLOCK_NOT_CONFIRMED')
    body=dict(version=VERSION,semester_id=semester_id,block_id=block_id,
              expected_revision=expected_revision,origin=origin,state_json=canonical(block))
    return StudyBlockCompletionPreviewV1(**body,fingerprint=digest(body))


def preview_study_block_completion(config,semester_id,block_id,expected_revision,*,origin='semester-calendar-v1',session_id=None):
    with _read_transaction(config) as db:
        return _preview(db,semester_id,block_id,expected_revision,origin,session_id)


def confirm_study_block_completion(config,preview,*,request_id,confirmed,actor,expected_fingerprint):
    if confirmed is not True or not isinstance(actor,str) or not actor.strip() or not isinstance(request_id,str) or not request_id.strip():
        raise ValueError('EXPLICIT_CONFIRMATION_AND_REQUEST_ID_REQUIRED')
    if not isinstance(preview,StudyBlockCompletionPreviewV1) or expected_fingerprint!=preview.fingerprint:
        raise ValueError('COMPLETION_PREVIEW_RECHECK_REQUIRED')
    payload_hash=digest(dict(preview=asdict(preview),actor=actor,confirmed=True))
    with write(config) as db:
        require_completion(db)
        prior=rows(db,'SELECT * FROM study_block_completions WHERE request_id=:id',id=request_id)
        if prior:
            if prior[0]['payload_hash']!=payload_hash: raise ValueError('REQUEST_PAYLOAD_MISMATCH')
            return StudyBlockCompletionV1(**prior[0])
        fresh=_preview(db,preview.semester_id,preview.block_id,preview.expected_revision,preview.origin)
        if fresh!=preview: raise ValueError('STALE_PREVIEW_RECHECK_REQUIRED')
        b=json.loads(fresh.state_json); at=stamp(datetime.now(timezone.utc))
        evidence=StudyBlockCompletionV1(str(uuid4()),VERSION,b['id'],b['revision'],b['semester_id'],b['course_id'],
            at,actor,preview.origin,None,'USER_REPORTED',request_id,payload_hash,
            canonical(dict(block_snapshot=b,original_request_id=b['request_id'],revision=b['revision'])))
        values=asdict(evidence)
        db.execute(text('INSERT INTO study_block_completions ('+','.join(values)+') VALUES ('+','.join(':'+k for k in values)+')'),values)
        db.execute(text('INSERT INTO study_block_completion_audit_events VALUES (:id,:completion,:request,:payload,:at)'),
                   dict(id=str(uuid4()),completion=evidence.id,request=request_id,payload=canonical(values),at=at))
        return evidence


def project_study_block_progress(config,semester_id):
    with _read_transaction(config) as db:
        semester=_semester(db,semester_id)
        tables=inspect(db.connection()).get_table_names()
        if 'confirmed_study_blocks' not in tables:return ()
        completions={r['block_id']:StudyBlockCompletionV1(**r) for r in rows(db,'SELECT * FROM study_block_completions WHERE semester_id=:sid',sid=semester.id)} if TABLES[0] in tables else {}
        return tuple(StudyBlockProgressV1('study-block-progress-v1',b['id'],b['semester_id'],b['course_id'],b['revision'],
            b['status'],b.get('need_id'),b.get('need_policy'),b['starts_at'],b['ends_at'],completions.get(b['id']))
            for b in effective_blocks(db,semester.id,include_cancelled=True))
