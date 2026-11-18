"""B1: deterministic ephemeral proposals and explicit atomic confirmation.

Hard policy: half-open UTC intervals; uncertain academic data blocks the affected
availability day. Date deadlines and material events never reserve time. Existing
weekly-plan blocks are also reserved; no existing event is moved.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import inspect, text
from uniflow.application.application_read_models import _read_transaction
from uniflow.application.learning_needs import project_learning_needs
from uniflow.application.planning_calendar import _db, _semester, _academic, resolve_local
from uniflow.infrastructure.write_lock import project_write_lock

VERSION = 'study-block-preview-v1'
TABLES = ('study_availability_windows', 'confirmed_study_blocks',
          'study_confirmation_requests', 'study_block_audit_events')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def rows(db, sql, **params):
    return [dict(r) for r in db.execute(text(sql), params).mappings()]


def require_schema(db):
    if not set(TABLES).issubset(inspect(db.connection()).get_table_names()):
        raise ValueError('STUDY_BLOCK_SCHEMA_REQUIRED: explicit migration 0020 required')


@contextmanager
def write(config):
    if not config.database_path.is_file():
        raise ValueError('DATABASE_NOT_FOUND')
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                require_schema(db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('AWARE_DATETIME_REQUIRED')
    return value.astimezone(timezone.utc)


def stamp(value):
    return utc(value).isoformat()


def local_interval(semester, day, begin, end):
    if type(day) is not date or type(begin) is not time or type(end) is not time or begin.tzinfo or end.tzinfo:
        raise ValueError('INVALID_LOCAL_INTERVAL')
    if begin >= end or semester.starts_on and day < semester.starts_on or semester.ends_on and day > semester.ends_on:
        raise ValueError('OUTSIDE_SEMESTER_OR_INVALID_INTERVAL')
    zone = ZoneInfo(semester.timezone)
    # V1 rejects ambiguous wall times, even if a caller could choose a fold.
    return resolve_local(day, begin, zone), resolve_local(day, end, zone)


def confirm_availability(config, semester_id, day, begin, end, *, confirmed, actor, as_of):
    if confirmed is not True or not isinstance(actor, str) or not actor.strip():
        raise ValueError('EXPLICIT_CONFIRMATION_REQUIRED')
    instant = stamp(as_of)
    with write(config) as db:
        semester = _semester(db, semester_id)
        start, finish = local_interval(semester, day, begin, end)
        identity = str(uuid4())
        db.execute(text('''INSERT INTO study_availability_windows
            (id,semester_id,timezone,local_day,starts_at,ends_at,revision,confirmed_at,confirmed_by)
            VALUES (:id,:semester,:zone,:day,:start,:end,1,:at,:actor)'''),
            dict(id=identity, semester=semester.id, zone=semester.timezone, day=day.isoformat(),
                 start=stamp(start), end=stamp(finish), at=instant, actor=actor))
        return identity


@dataclass(frozen=True)
class StudyBlockProposalV1:
    need_id: str
    course_id: str
    skill_ids: tuple[str, ...]
    need_policy: str
    title: str
    starts_at: str
    ends_at: str
    minutes: int
    availability_id: str


@dataclass(frozen=True)
class StudyBlockPreviewV1:
    version: str
    semester_id: str
    as_of: str
    horizon_days: int
    durations: tuple[tuple[str, int], ...]
    proposals: tuple[StudyBlockProposalV1, ...]
    diagnostics: tuple[str, ...]
    context_hash: str
    fingerprint: str


def ceil_minute(value):
    value = utc(value)
    return value.replace(second=0, microsecond=0) + (timedelta(minutes=1) if value.second or value.microsecond else timedelta())


def selection_fingerprint(preview, selected_need_ids):
    selected = tuple(sorted(selected_need_ids))
    if not selected or len(set(selected)) != len(selected) or not set(selected).issubset({p.need_id for p in preview.proposals}):
        raise ValueError('INVALID_SELECTION')
    return digest(dict(version='study-block-selection-v1', preview=preview.fingerprint,
                       blocks=[asdict(p) for p in preview.proposals if p.need_id in selected]))


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _context_hash(db, needs, semester):
    # Existing calendar/source contracts do not have global revisions. Hash actual
    # stored row contents instead of inventing a revision counter. Conservative:
    # changes in other semesters can invalidate a preview, never validate a stale one.
    tables = ('semesters','courses','schedule_entries','schedule_exceptions',
              'planning_profiles','calendar_constraints','weekly_plans','weekly_plan_revisions',
              'weekly_plan_block_executions',*TABLES[:2], 'study_block_revisions')
    existing = set(inspect(db.connection()).get_table_names())
    content = {}
    for name in tables:
        if name in existing:
            content[name] = sorted(rows(db, f'SELECT * FROM "{name}"'), key=canonical)
    return digest(dict(rows=content, needs=[asdict(n) for n in needs], semester=semester.stable_id))


def _preview(db, config, semester_id, as_of, durations, horizon_days):
    utc(as_of)
    semester = _semester(db, semester_id)
    zone = ZoneInfo(semester.timezone)
    projection = project_learning_needs(config, semester_id, as_of, horizon_days=horizon_days, _session=db)
    needs = projection.needs
    known_ids = {n.recommendation_id for n in needs}
    if any(k not in known_ids or type(v) is not int or not 1 <= v <= 1440 for k,v in durations.items()):
        raise ValueError('INVALID_NEED_OR_DURATION')
    require_schema(db)
    windows = rows(db, 'SELECT * FROM study_availability_windows WHERE semester_id=:sid ORDER BY starts_at,id', sid=semester.id)
    from uniflow.application.study_block_changes import effective_blocks
    blocks = effective_blocks(db, semester.id)
    busy = [(datetime.fromisoformat(b['starts_at']),datetime.fromisoformat(b['ends_at'])) for b in blocks]
    diagnostics, proposals = [], []
    if not windows: diagnostics.append('Keine bestätigte Verfügbarkeit; keine konkreten Zeitvorschläge.')
    day_cache = {}
    def day_busy(day):
        if day not in day_cache:
            day_cache[day] = hard_day_context(db, config, semester, day)
        return day_cache[day]
    for need in needs:
        minutes = durations.get(need.recommendation_id, need.known_minutes)
        if minutes is None:
            diagnostics.append(f'Dauer auswählen: {need.recommendation_id}')
            continue
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError('INVALID_DURATION')
        for w in windows:
            day = date.fromisoformat(w['local_day'])
            if not as_of.astimezone(zone).date() <= day <= as_of.astimezone(zone).date()+timedelta(days=horizon_days): continue
            if w['timezone'] != semester.timezone: raise ValueError('AVAILABILITY_TIMEZONE_CHANGED')
            start, end = datetime.fromisoformat(w['starts_at']),datetime.fromisoformat(w['ends_at'])
            # Revalidate stored provenance and wall times, including semester bounds.
            local_interval(semester, day, start.astimezone(zone).time(), end.astimezone(zone).time())
            uncertain, reserved = day_busy(day)
            if uncertain:
                diagnostics.append(f'Ungeklärte Termindaten: {day}')
                continue
            candidate = ceil_minute(max(start,utc(as_of)))
            for a,b in sorted(busy+reserved):
                if overlap((candidate,candidate+timedelta(minutes=minutes)),(a,b)):
                    candidate = ceil_minute(b)
            finish = candidate+timedelta(minutes=minutes)
            if finish <= end:
                # Reject DST ambiguity of generated endpoints as well.
                local_interval(semester, day, candidate.astimezone(zone).time(), finish.astimezone(zone).time())
                proposals.append(StudyBlockProposalV1(need.recommendation_id,need.course_id,need.skill_ids,
                    need.policy_version,need.action,stamp(candidate),stamp(finish),minutes,w['id']))
                busy.append((candidate,finish))
                break
        else: diagnostics.append(f'Kein konfliktfreier Platz: {need.recommendation_id}')
    context = _context_hash(db, needs, semester)
    payload = dict(version=VERSION, semester_id=semester_id, as_of=stamp(as_of),horizon_days=horizon_days,
                   durations=tuple(sorted(durations.items())),proposals=tuple(proposals),diagnostics=tuple(sorted(set(diagnostics))),context_hash=context)
    fingerprint = digest({**payload,'proposals':[asdict(p) for p in proposals]})
    return StudyBlockPreviewV1(**payload,fingerprint=fingerprint)


def preview_study_blocks(config, semester_id, as_of, durations=None, horizon_days=30):
    with _read_transaction(config) as db:
        return _preview(db,config,semester_id,as_of,durations or {},horizon_days)


def request_status(config, request_id):
    with _read_transaction(config) as db:
        require_schema(db)
        found = rows(db,'SELECT * FROM study_confirmation_requests WHERE id=:id',id=request_id)
        return found[0] if found else None


def confirm_study_blocks(config, preview, selected_need_ids, *, request_id, confirmed, actor, expected_selection_fingerprint=None):
    if confirmed is not True or not isinstance(actor,str) or not actor.strip() or not isinstance(request_id,str) or not request_id.strip():
        raise ValueError('EXPLICIT_CONFIRMATION_AND_REQUEST_ID_REQUIRED')
    selected = tuple(sorted(selected_need_ids))
    if not selected or len(set(selected)) != len(selected) or not set(selected).issubset({p.need_id for p in preview.proposals}):
        raise ValueError('INVALID_SELECTION')
    chosen_fingerprint = selection_fingerprint(preview, selected)
    if expected_selection_fingerprint != chosen_fingerprint:
        raise ValueError('SELECTION_PREVIEW_RECHECK_REQUIRED')
    payload_hash = digest(dict(preview=asdict(preview),selected=selected,actor=actor,selection_fingerprint=chosen_fingerprint))
    with write(config) as db:
        previous = rows(db,'SELECT * FROM study_confirmation_requests WHERE id=:id',id=request_id)
        if previous:
            if previous[0]['payload_hash'] != payload_hash: raise ValueError('REQUEST_PAYLOAD_MISMATCH')
            return json.loads(previous[0]['result_json'])
        fresh = _preview(db,config,preview.semester_id,datetime.fromisoformat(preview.as_of),dict(preview.durations),preview.horizon_days)
        if fresh != preview: raise ValueError('STALE_PREVIEW_RECHECK_REQUIRED')
        semester = _semester(db,preview.semester_id)
        chosen = [p for p in fresh.proposals if p.need_id in selected]
        identities = [str(uuid4()) for _ in chosen]
        at = stamp(datetime.now(timezone.utc))
        db.execute(text('INSERT INTO study_confirmation_requests VALUES (:id,:hash,:fingerprint,:at,:actor,:result)'),
            dict(id=request_id,hash=payload_hash,fingerprint=chosen_fingerprint,at=at,actor=actor,result=canonical(identities)))
        for identity,p in zip(identities,chosen):
            data = dict(id=identity,semester=semester.id,course=p.course_id,skills=canonical(p.skill_ids),need=p.need_id,
                policy=p.need_policy,title=p.title,start=p.starts_at,end=p.ends_at,minutes=p.minutes,zone=semester.timezone,
                availability=p.availability_id,request=request_id)
            db.execute(text('''INSERT INTO confirmed_study_blocks VALUES
                (:id,:semester,:course,:skills,:need,:policy,:title,:start,:end,:minutes,:zone,:availability,'CONFIRMED',1,:request)'''),data)
            db.execute(text("INSERT INTO study_block_audit_events VALUES (:id,:block,'CREATED',:payload,:at,:request)"),
                dict(id=str(uuid4()),block=identity,payload=canonical(data),at=at,request=request_id))
        return identities


def hard_day_context(db, config, semester, day):
    """Shared B1/B2 hard conflict policy; caller supplies the same read snapshot."""
    zone = ZoneInfo(semester.timezone)
    academic, issues, _, _ = _academic(db, semester, zone, day-timedelta(days=day.weekday()), calendar_start=day, calendar_end=day)
    uncertain = bool(issues) or any(a.diagnostics or a.participation == 'UNKNOWN' or a.information_status not in ('CONFIRMED',) for a in academic)
    # Legacy personal constraints are authoritative too; weekly confirmed
    # revisions are handled conservatively below until their slots resolve.
    from uniflow.application.planning_calendar import _project_calendar_week_in_transaction
    week = _project_calendar_week_in_transaction(db, semester.stable_id, day-timedelta(days=day.weekday()))
    reserved = [(utc(a.starts_at),utc(a.ends_at)) for a in academic if a.hard_block]
    reserved += [(utc(c.starts_at),utc(c.ends_at)) for c in week.constraint_occurrences if c.constraint_type == 'BLOCKED']
    from uniflow.infrastructure.database.models import WeeklyPlan, WeeklyPlanRevision
    from sqlalchemy import select
    plans = list(db.scalars(select(WeeklyPlan).where(WeeklyPlan.semester_id == semester.id, WeeklyPlan.week_start == day-timedelta(days=day.weekday()))))
    from uniflow.application.weekly_plans import _load_verified
    for plan in plans:
        if plan.accepted_revision_id is None: continue
        revision = db.get(WeeklyPlanRevision, plan.accepted_revision_id)
        if revision is None or revision.weekly_plan_id != plan.id:
            raise ValueError('SOURCE_ARTIFACT_INVALID')
        body = _load_verified(config, db, plan, revision)[3]
        reserved.extend((utc(b.starts_at), utc(b.ends_at)) for b in body.blocks)
    return uncertain, reserved
