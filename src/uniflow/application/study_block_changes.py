"""Versioned, explicitly confirmed changes in one locked transaction.

Revision 1 is always the immutable B1 row. Only the central effective_blocks
projection decides which snapshot reserves time. No recommendation writes here.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import inspect, text
from uniflow.application.application_read_models import _read_transaction
from uniflow.application.planning_calendar import _semester
from uniflow.application.study_blocks import (rows, canonical, digest, stamp, write,
    local_interval, hard_day_context, overlap, _context_hash, utc)

VERSION = 'study-block-change-preview-v1'
TABLES = ('study_block_change_requests', 'study_block_revisions', 'study_block_change_audit_events')


def require_changes(db):
    if not set(TABLES).issubset(inspect(db.connection()).get_table_names()):
        raise ValueError('STUDY_BLOCK_CHANGE_SCHEMA_REQUIRED: explicit migration 0021 required')


def effective_blocks(db, semester_id, *, include_cancelled=False):
    """One full current state per original block, also usable on schema 0020."""
    originals = rows(db, 'SELECT * FROM confirmed_study_blocks WHERE semester_id=:sid ORDER BY id', sid=semester_id)
    if 'study_block_revisions' in inspect(db.connection()).get_table_names():
        for block in originals:
            latest = rows(db, '''SELECT state_json FROM study_block_revisions
                WHERE block_id=:id ORDER BY revision DESC LIMIT 1''', id=block['id'])
            if latest:
                block.update(json.loads(latest[0]['state_json']))
    if 'study_block_completions' in inspect(db.connection()).get_table_names():
        completed={r['block_id'] for r in rows(db,'SELECT block_id FROM study_block_completions WHERE semester_id=:sid',sid=semester_id)}
        for block in originals:
            if block['id'] in completed: block['status']='COMPLETED'
    return [b for b in originals if include_cancelled or b['status'] in ('CONFIRMED','COMPLETED')]


@dataclass(frozen=True)
class StudyBlockRevisionV1:
    block_id: str
    revision: int
    previous_revision: int | None
    change_kind: str
    state_json: str
    confirmed_at: str
    confirmed_by: str
    origin: str
    request_id: str

    @property
    def state(self):
        return json.loads(self.state_json)


@dataclass(frozen=True)
class StudyBlockChangeCommandV1:
    block_id: str
    expected_revision: int
    day: date
    begin: time
    minutes: int
    title: str
    cancel: bool = False
    origin: str = 'semester-calendar-v1'


@dataclass(frozen=True)
class StudyBlockChangePreviewV1:
    version: str
    semester_id: str
    timezone: str
    command: StudyBlockChangeCommandV1
    before_json: str
    after_json: str
    change_kind: str
    context_hash: str
    fingerprint: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class StudyBlockChangeReceiptV1:
    request_id: str
    block_id: str
    revision: int
    status: str
    confirmed_at: str


def _history(db, semester_id, block_id):
    semester = _semester(db, semester_id)
    original = rows(db, 'SELECT * FROM confirmed_study_blocks WHERE id=:id AND semester_id=:sid', id=block_id, sid=semester.id)
    if not original: raise ValueError('UNKNOWN_STUDY_BLOCK')
    b=original[0]
    request=rows(db,'SELECT * FROM study_confirmation_requests WHERE id=:id',id=b['request_id'])[0]
    history=[StudyBlockRevisionV1(b['id'],1,None,'CREATED',canonical(b),request['confirmed_at'],request['confirmed_by'],'B1',b['request_id'])]
    if 'study_block_revisions' in inspect(db.connection()).get_table_names():
        history.extend(StudyBlockRevisionV1(**r) for r in rows(db,'SELECT * FROM study_block_revisions WHERE block_id=:id ORDER BY revision',id=block_id))
    return tuple(history)


def study_block_history(config, semester_id, block_id):
    with _read_transaction(config) as db:
        return _history(db,semester_id,block_id)


def list_study_block_states(config, semester_id):
    with _read_transaction(config) as db:
        semester=_semester(db,semester_id)
        if 'confirmed_study_blocks' not in inspect(db.connection()).get_table_names():return ()
        return tuple(effective_blocks(db,semester.id,include_cancelled=True))


def _preview(db, config, semester_id, command):
    require_changes(db)
    if not isinstance(command,StudyBlockChangeCommandV1) or type(command.expected_revision) is not int or type(command.cancel) is not bool:
        raise ValueError('INVALID_CHANGE_COMMAND')
    if not isinstance(command.origin,str) or not command.origin.strip(): raise ValueError('INVALID_ORIGIN')
    semester=_semester(db,semester_id)
    current=next((b for b in effective_blocks(db,semester.id,include_cancelled=True) if b['id']==command.block_id),None)
    if current is None: raise ValueError('UNKNOWN_STUDY_BLOCK')
    if current['revision']!=command.expected_revision: raise ValueError('STALE_REVISION')
    if current['status']!='CONFIRMED': raise ValueError('BLOCK_NOT_CONFIRMED')
    if not isinstance(command.title,str) or not command.title.strip() or len(command.title)>2000: raise ValueError('INVALID_TITLE')
    if type(command.minutes) is not int or not 1<=command.minutes<=1440: raise ValueError('INVALID_DURATION')
    zone=ZoneInfo(semester.timezone)
    if current['timezone']!=semester.timezone: raise ValueError('BLOCK_TIMEZONE_CHANGED')
    after=dict(current,revision=current['revision']+1)
    kind='CANCEL' if command.cancel else 'ADJUST'
    if command.cancel:
        # Cancellation never smuggles an additional edit into the cancelled state.
        old=datetime.fromisoformat(current['starts_at']).astimezone(zone)
        if (command.day,command.begin,command.minutes,command.title)!=(old.date(),old.time(),current['minutes'],current['title']):
            raise ValueError('CANCELLATION_MUST_RETAIN_STATE')
        after['status']='CANCELLED'
        diagnostics=('Absage: Die bisher reservierte Zeit wird frei, sofern keine anderen Einschränkungen bestehen.',)
    else:
        if type(command.day) is not date or type(command.begin) is not time or command.begin.tzinfo:
            raise ValueError('INVALID_LOCAL_INTERVAL')
        finish_local=datetime.combine(command.day,command.begin)+timedelta(minutes=command.minutes)
        if finish_local.date()!=command.day: raise ValueError('INVALID_LOCAL_INTERVAL')
        start,finish=local_interval(semester,command.day,command.begin,finish_local.time())
        if utc(finish)-utc(start)!=timedelta(minutes=command.minutes): raise ValueError('DST_DURATION_MISMATCH')
        windows=rows(db,'SELECT * FROM study_availability_windows WHERE semester_id=:sid ORDER BY id',sid=semester.id)
        valid=[]
        for w in windows:
            if w['timezone']!=semester.timezone or w['local_day']!=command.day.isoformat(): continue
            a,b=datetime.fromisoformat(w['starts_at']),datetime.fromisoformat(w['ends_at'])
            checked=local_interval(semester,command.day,a.astimezone(zone).time(),b.astimezone(zone).time())
            if checked!=(a,b): raise ValueError('INVALID_AVAILABILITY_PROVENANCE')
            if a<=start and finish<=b:valid.append(w)
        if not valid: raise ValueError('CONFIRMED_AVAILABILITY_REQUIRED')
        uncertain,reserved=hard_day_context(db,config,semester,command.day)
        if uncertain: raise ValueError('UNCERTAIN_CALENDAR_CONTEXT')
        others=effective_blocks(db,semester.id)
        reserved.extend((datetime.fromisoformat(b['starts_at']),datetime.fromisoformat(b['ends_at'])) for b in others if b['id']!=command.block_id)
        if any(overlap((start,finish),r) for r in reserved): raise ValueError('HARD_CONFLICT')
        after.update(title=command.title,minutes=command.minutes,starts_at=stamp(start),ends_at=stamp(finish),availability_id=valid[0]['id'])
        if all(after[k]==current[k] for k in ('title','minutes','starts_at','ends_at','status')):raise ValueError('NO_CHANGE')
        diagnostics=(f'Konfliktprüfung erfolgreich. Reservierte Lernzeit: {current["minutes"]} → {after["minutes"]} Minuten; Änderung {after["minutes"]-current["minutes"]:+d} Minuten.',)
    context=_context_hash(db,(),semester)
    payload=dict(version=VERSION,semester_id=semester_id,timezone=semester.timezone,command=command,
        before_json=canonical(current),after_json=canonical(after),change_kind=kind,context_hash=context,diagnostics=diagnostics)
    serialized={**payload,'command':{**asdict(command),'day':command.day.isoformat(),'begin':command.begin.isoformat()}}
    return StudyBlockChangePreviewV1(**payload,fingerprint=digest(serialized))


def preview_study_block_change(config,semester_id,command):
    with _read_transaction(config) as db:return _preview(db,config,semester_id,command)


def _payload(preview,actor):
    body=asdict(preview)
    body['command']['day']=preview.command.day.isoformat()
    body['command']['begin']=preview.command.begin.isoformat()
    return digest(dict(preview=body,actor=actor,confirmed=True))


def confirm_study_block_change(config,preview,*,request_id,confirmed,actor,expected_fingerprint):
    if confirmed is not True or not isinstance(actor,str) or not actor.strip() or not isinstance(request_id,str) or not request_id.strip():
        raise ValueError('EXPLICIT_CONFIRMATION_AND_REQUEST_ID_REQUIRED')
    if expected_fingerprint!=preview.fingerprint:raise ValueError('CHANGE_PREVIEW_RECHECK_REQUIRED')
    payload_hash=_payload(preview,actor)
    with write(config) as db:
        require_changes(db)
        prior=rows(db,'SELECT * FROM study_block_change_requests WHERE id=:id',id=request_id)
        if prior:
            if prior[0]['payload_hash']!=payload_hash:raise ValueError('REQUEST_PAYLOAD_MISMATCH')
            return StudyBlockChangeReceiptV1(**json.loads(prior[0]['result_json']))
        fresh=_preview(db,config,preview.semester_id,preview.command)
        if fresh!=preview:raise ValueError('STALE_PREVIEW_RECHECK_REQUIRED')
        at=stamp(datetime.now(timezone.utc));state=json.loads(fresh.after_json)
        receipt=StudyBlockChangeReceiptV1(request_id,state['id'],state['revision'],state['status'],at)
        db.execute(text('INSERT INTO study_block_change_requests VALUES (:id,:hash,:fingerprint,:expected,:at,:actor,:origin,:result)'),
            dict(id=request_id,hash=payload_hash,fingerprint=fresh.fingerprint,expected=preview.command.expected_revision,at=at,actor=actor,origin=preview.command.origin,result=canonical(asdict(receipt))))
        db.execute(text('INSERT INTO study_block_revisions VALUES (:block,:revision,:previous,:kind,:state,:at,:actor,:origin,:request)'),
            dict(block=state['id'],revision=state['revision'],previous=state['revision']-1,kind=fresh.change_kind,state=fresh.after_json,at=at,actor=actor,origin=preview.command.origin,request=request_id))
        db.execute(text('INSERT INTO study_block_change_audit_events VALUES (:id,:block,:revision,:request,:payload,:at)'),
            dict(id=str(uuid4()),block=state['id'],revision=state['revision'],request=request_id,payload=canonical(dict(before=json.loads(fresh.before_json),after=state,fingerprint=fresh.fingerprint)),at=at))
        return receipt
