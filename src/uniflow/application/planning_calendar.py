"""Semester-scoped personal calendar state and read-only week projection."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    AvailabilityWindow, CalendarConstraint, Course, PlanningProfile,
    ScheduleEntry, ScheduleException, Semester, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock
from uniflow.schemas.semester_audit import ScheduleEntryV1, ScheduleExceptionV1
from uniflow.schemas.calendar_v1 import (
    AcademicOccurrenceV1, AvailabilityOccurrenceV1, CalendarDiagnosticV1,
    CalendarWeekProjectionV1, ConstraintOccurrenceV1,
)

POLICY = "calendar-foundation-v1"
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
UNSET = object()


def _db(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _diag(code: str, *refs: str) -> CalendarDiagnosticV1:
    return CalendarDiagnosticV1(code=code, refs=list(refs))


def _semester(db, stable_id: str) -> Semester:
    row = db.scalar(select(Semester).where(Semester.stable_id == stable_id))
    if row is None:
        raise ValueError("UNKNOWN_SEMESTER")
    return row


def _profile(db, semester_id: str) -> PlanningProfile:
    semester = _semester(db, semester_id)
    row = db.scalar(select(PlanningProfile).where(PlanningProfile.semester_id == semester.id))
    if row is None:
        raise ValueError("NO_PLANNING_PROFILE")
    return row


def _positive(value: int | None) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("INVALID_PROFILE_MINUTES")


def _profile_data(row: PlanningProfile) -> dict:
    return {"id": row.id, "semester_id": row.semester_id, "revision": row.revision,
        "default_action_duration_minutes": row.default_action_duration_minutes,
        "max_daily_study_minutes": row.max_daily_study_minutes}


def create_planning_profile(config: AppConfig, semester_id: str, *,
    default_action_duration_minutes: int | None = None,
    max_daily_study_minutes: int | None = None) -> dict:
    _positive(default_action_duration_minutes)
    _positive(max_daily_study_minutes)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            semester = _semester(db, semester_id)
            if db.scalar(select(PlanningProfile).where(PlanningProfile.semester_id == semester.id)):
                raise ValueError("PROFILE_ALREADY_EXISTS")
            row = PlanningProfile(semester_id=semester.id, revision=1,
                default_action_duration_minutes=default_action_duration_minutes,
                max_daily_study_minutes=max_daily_study_minutes)
            db.add(row)
            db.flush()
            return _profile_data(row)


def planning_profile_info(config: AppConfig, semester_id: str) -> dict:
    with _db(config) as db:
        return _profile_data(_profile(db, semester_id))


def update_planning_profile(config: AppConfig, semester_id: str, *,
    default_action_duration_minutes=UNSET, max_daily_study_minutes=UNSET) -> dict:
    if default_action_duration_minutes is not UNSET:
        _positive(default_action_duration_minutes)
    if max_daily_study_minutes is not UNSET:
        _positive(max_daily_study_minutes)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            row = _profile(db, semester_id)
            changed = False
            for key, value in (("default_action_duration_minutes", default_action_duration_minutes),
                               ("max_daily_study_minutes", max_daily_study_minutes)):
                if value is not UNSET and getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                row.revision += 1
            db.flush()
            return _profile_data(row)


def _availability_data(row: AvailabilityWindow) -> dict:
    return {"id": row.id, "planning_profile_id": row.planning_profile_id,
        "weekday": row.weekday, "start_local_time": row.start_local_time.isoformat(),
        "end_local_time": row.end_local_time.isoformat(), "start_fold": row.start_fold,
        "end_fold": row.end_fold, "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "created_revision": row.created_revision, "deactivated_revision": row.deactivated_revision}


def add_availability_window(config: AppConfig, semester_id: str, *, weekday: str,
    start_local_time: time, end_local_time: time, valid_from: date | None = None,
    valid_until: date | None = None, start_fold: int | None = None,
    end_fold: int | None = None) -> dict:
    if weekday not in WEEKDAYS or not isinstance(start_local_time, time) or not isinstance(end_local_time, time) \
            or start_local_time.tzinfo or end_local_time.tzinfo or start_local_time >= end_local_time \
            or (valid_from and valid_until and valid_from > valid_until) \
            or start_fold not in (None, 0, 1) or end_fold not in (None, 0, 1):
        raise ValueError("INVALID_AVAILABILITY_WINDOW")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            profile = _profile(db, semester_id)
            profile.revision += 1
            row = AvailabilityWindow(planning_profile_id=profile.id, weekday=weekday,
                start_local_time=start_local_time, end_local_time=end_local_time,
                valid_from=valid_from, valid_until=valid_until, start_fold=start_fold,
                end_fold=end_fold, created_revision=profile.revision)
            db.add(row)
            db.flush()
            return _availability_data(row)


def list_availability_windows(config: AppConfig, semester_id: str, *, include_inactive: bool = False) -> list[dict]:
    with _db(config) as db:
        profile = _profile(db, semester_id)
        rows = db.scalars(select(AvailabilityWindow).where(AvailabilityWindow.planning_profile_id == profile.id)
            .order_by(AvailabilityWindow.created_revision, AvailabilityWindow.id)).all()
        return [_availability_data(r) for r in rows if include_inactive or r.deactivated_revision is None]


def deactivate_availability_window(config: AppConfig, window_id: str) -> str:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            row = db.get(AvailabilityWindow, window_id)
            if row is None:
                raise ValueError("UNKNOWN_AVAILABILITY_WINDOW")
            if row.deactivated_revision is not None:
                return "ALREADY_INACTIVE"
            profile = db.get(PlanningProfile, row.planning_profile_id)
            profile.revision += 1
            row.deactivated_revision = profile.revision
            row.deactivated_at = utc_now()
            return "DEACTIVATED"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("AWARE_DATETIME_REQUIRED")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    # SQLite DateTime discards the offset. These columns are always stored in UTC.
    return value.replace(tzinfo=timezone.utc)


def _constraint_data(row: CalendarConstraint) -> dict:
    return {"id": row.id, "planning_profile_id": row.planning_profile_id,
        "constraint_type": row.constraint_type,
        "starts_at": _stored_utc(row.starts_at).isoformat() if row.starts_at else None,
        "ends_at": _stored_utc(row.ends_at).isoformat() if row.ends_at else None,
        "local_date": row.local_date.isoformat() if row.local_date else None,
        "label": row.label, "created_revision": row.created_revision,
        "deactivated_revision": row.deactivated_revision}


def add_calendar_constraint(config: AppConfig, semester_id: str, *, constraint_type: str,
    starts_at: datetime | None = None, ends_at: datetime | None = None,
    local_date: date | None = None, label: str | None = None) -> dict:
    if constraint_type not in ("BLOCKED", "AVAILABLE_ADDITION"):
        raise ValueError("INVALID_CONSTRAINT_TYPE")
    timed = starts_at is not None and ends_at is not None and local_date is None
    all_day = starts_at is None and ends_at is None and type(local_date) is date
    if not (timed or all_day):
        raise ValueError("INVALID_CONSTRAINT_SHAPE")
    if timed:
        starts_at, ends_at = _utc(starts_at), _utc(ends_at)
        if starts_at >= ends_at:
            raise ValueError("INVALID_CONSTRAINT_INTERVAL")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            profile = _profile(db, semester_id)
            profile.revision += 1
            row = CalendarConstraint(planning_profile_id=profile.id,
                constraint_type=constraint_type, starts_at=starts_at, ends_at=ends_at,
                local_date=local_date, label=label, created_revision=profile.revision)
            db.add(row)
            db.flush()
            return _constraint_data(row)


def list_calendar_constraints(config: AppConfig, semester_id: str, *, include_inactive: bool = False) -> list[dict]:
    with _db(config) as db:
        profile = _profile(db, semester_id)
        rows = db.scalars(select(CalendarConstraint).where(CalendarConstraint.planning_profile_id == profile.id)
            .order_by(CalendarConstraint.created_revision, CalendarConstraint.id)).all()
        return [_constraint_data(r) for r in rows if include_inactive or r.deactivated_revision is None]


def deactivate_calendar_constraint(config: AppConfig, constraint_id: str) -> str:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            row = db.get(CalendarConstraint, constraint_id)
            if row is None:
                raise ValueError("UNKNOWN_CALENDAR_CONSTRAINT")
            if row.deactivated_revision is not None:
                return "ALREADY_INACTIVE"
            profile = db.get(PlanningProfile, row.planning_profile_id)
            profile.revision += 1
            row.deactivated_revision = profile.revision
            row.deactivated_at = utc_now()
            return "DEACTIVATED"


def resolve_local(local_date: date, wall_time: time, zone: ZoneInfo,
    fold: int | None = None, *, source: str = "PERSONAL") -> datetime:
    """Resolve a wall clock only after validating both UTC round trips."""
    naive = datetime.combine(local_date, wall_time)
    candidates = []
    for choice in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=choice)
        back = candidate.astimezone(timezone.utc).astimezone(zone)
        if back.replace(tzinfo=None) == naive and back.fold == choice:
            candidates.append(candidate)
    if not candidates:
        raise ValueError(f"NONEXISTENT_{source}_LOCAL_TIME")
    if len(candidates) == 1:
        if fold not in (None, 0, 1):
            raise ValueError("INVALID_FOLD")
        return candidates[0]
    if fold not in (0, 1):
        raise ValueError(f"AMBIGUOUS_{source}_LOCAL_TIME")
    return candidates[fold]


def _week(db, semester_id: str, week_start: date):
    if type(week_start) is not date or week_start.weekday() != 0:
        raise ValueError("INVALID_WEEK_START")
    semester = _semester(db, semester_id)
    zone = ZoneInfo(semester.timezone)
    week_end = week_start + timedelta(days=7)
    start = resolve_local(week_start, time.min, zone, source="WEEK")
    end = resolve_local(week_end, time.min, zone, source="WEEK")
    return semester, zone, week_end, start, end


def _academic(db, semester: Semester, zone: ZoneInfo, week_start: date, *,
              calendar_start: date | None = None, calendar_end: date | None = None,
              calendar_course_id: str | None = None):
    courses = {c.id: c for c in db.scalars(select(Course).where(Course.semester_id == semester.id)).all()}
    if calendar_course_id is not None:
        courses = {k: v for k, v in courses.items() if k == calendar_course_id}
    entries = []
    for row in db.scalars(select(ScheduleEntry).where(ScheduleEntry.course_id.in_(courses))).all():
        payload = ScheduleEntryV1.model_validate_json(row.payload_json)
        entries.append((row, payload))
    exceptions = []
    for row in db.scalars(select(ScheduleException).where(ScheduleException.course_id.in_(courses))).all():
        payload = ScheduleExceptionV1.model_validate_json(row.payload_json)
        if week_start <= payload.date < week_start + timedelta(days=7) and \
                (calendar_start is None or payload.date >= calendar_start) and \
                (calendar_end is None or payload.date <= calendar_end):
            exceptions.append((row, payload))
    entries.sort(key=lambda x: (x[0].stable_id, x[0].id))
    exceptions.sort(key=lambda x: (x[0].stable_id, x[0].id))
    by_stable = {row.stable_id: (row, payload) for row, payload in entries}
    base = {}
    for row, payload in entries:
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            if (calendar_start is not None and day < calendar_start) or (calendar_end is not None and day > calendar_end):
                continue
            if WEEKDAYS[day.weekday()] != payload.weekday or (payload.valid_from and day < payload.valid_from) \
                    or (payload.valid_until and day > payload.valid_until):
                continue
            if payload.calendar_week_parity != "ALL" and ((day.isocalendar().week % 2 == 1) != (payload.calendar_week_parity == "ODD")):
                continue
            base[(row.stable_id, day)] = (row, payload)
    transforms: dict[tuple[str, date], tuple] = {}
    diagnostics = []
    additions = []
    for row, payload in exceptions:
        kind = payload.event_type.strip().upper()
        if kind not in ("CANCELLATION", "REPLACEMENT", "ADDITION"):
            diagnostics.append(_diag("UNSUPPORTED_SCHEDULE_EXCEPTION_TYPE", row.id))
            continue
        if row.information_status.value not in ("CONFIRMED", "OBSERVED"):
            diagnostics.append(_diag("NONAUTHORITATIVE_SCHEDULE_EXCEPTION", row.id))
            continue
        if kind == "ADDITION":
            if payload.replaces_schedule_id is not None or payload.start_time is None or payload.end_time is None \
                    or payload.end_time <= payload.start_time:
                diagnostics.append(_diag("INVALID_SCHEDULE_ADDITION", row.id))
            else:
                additions.append((row, payload))
            continue
        if not payload.replaces_schedule_id or payload.replaces_schedule_id not in by_stable \
                or by_stable[payload.replaces_schedule_id][0].course_id != row.course_id:
            diagnostics.append(_diag("INVALID_SCHEDULE_EXCEPTION_LINK", row.id))
            continue
        key = (payload.replaces_schedule_id, payload.date)
        if key in transforms:
            raise ValueError("SCHEDULE_EXCEPTION_CONFLICT")
        if key not in base:
            diagnostics.append(_diag("CANCELLATION_WITHOUT_OCCURRENCE" if kind == "CANCELLATION"
                else "REPLACEMENT_WITHOUT_OCCURRENCE", row.id))
            continue
        if kind == "REPLACEMENT" and (payload.start_time is None or payload.end_time is None
                or payload.end_time <= payload.start_time):
            diagnostics.append(_diag("INVALID_SCHEDULE_REPLACEMENT", row.id))
            continue
        transforms[key] = (kind, row, payload)

    def occurrence(kind: str, day: date, entry, exception=None):
        entry_row, entry_payload = entry if entry else (None, None)
        exception_row, exception_payload = exception if exception else (None, None)
        course = courses[entry_row.course_id if entry_row else exception_row.course_id]
        begin = entry_payload.start_time if kind == "RECURRING" else exception_payload.start_time
        finish = entry_payload.end_time if kind == "RECURRING" else exception_payload.end_time
        participation = entry_payload.participation if entry_payload else "UNKNOWN"
        status = exception_row.information_status.value if exception_row else entry_row.information_status.value
        reasons = []
        starts_at = resolve_local(day, begin, zone, source="ACADEMIC")
        ends_at = None
        if finish is None:
            reasons.append(_diag("MISSING_END_TIME", entry_row.id if entry_row else exception_row.id))
        elif finish <= begin:
            reasons.append(_diag("INVALID_ACADEMIC_INTERVAL", entry_row.id if entry_row else exception_row.id))
        else:
            ends_at = resolve_local(day, finish, zone, source="ACADEMIC")
            if ends_at.astimezone(timezone.utc) <= starts_at.astimezone(timezone.utc):
                reasons.append(_diag("INVALID_ACADEMIC_INTERVAL", entry_row.id if entry_row else exception_row.id))
                ends_at = None
        hard = bool(starts_at and ends_at and participation == "REQUIRED" and status == "CONFIRMED"
            and (entry_row is None or entry_row.information_status.value == "CONFIRMED"))
        identity = {"policy": POLICY, "kind": kind, "date": day.isoformat(),
            "entry": entry_row.id if entry_row else None,
            "exception": exception_row.id if exception_row else None}
        return AcademicOccurrenceV1(occurrence_id="academic:" + _digest(identity),
            course_id=course.id, course_stable_id=course.stable_id, source_kind=kind,
            schedule_entry_id=entry_row.id if entry_row else None,
            schedule_entry_stable_id=entry_row.stable_id if entry_row else None,
            schedule_exception_id=exception_row.id if exception_row else None,
            schedule_exception_stable_id=exception_row.stable_id if exception_row else None,
            local_date=day, starts_at=starts_at, ends_at=ends_at,
            activity_type=entry_payload.activity_type if entry_payload else None,
            title=exception_payload.title if exception_payload else None,
            course_name=course.name,
            location=entry_payload.location if kind == "RECURRING" else None,
            information_status=status, participation=participation,
            hard_block=hard, diagnostics=reasons)

    occurrences = []
    for key, entry in sorted(base.items()):
        transform = transforms.get(key)
        if transform:
            kind, row, payload = transform
            if kind == "REPLACEMENT":
                occurrences.append(occurrence("REPLACEMENT", key[1], entry, (row, payload)))
        else:
            occurrences.append(occurrence("RECURRING", key[1], entry))
    for row, payload in additions:
        occurrences.append(occurrence("ADDITION", payload.date, None, (row, payload)))
    occurrences.sort(key=lambda x: (x.local_date, x.starts_at or datetime.min.replace(tzinfo=timezone.utc), x.occurrence_id))
    diagnostics.sort(key=lambda x: (x.code, x.refs))
    context_entries = [{"id": r.id, "stable_id": r.stable_id, "course_id": r.course_id,
        "status": r.information_status.value, "audit_revision_id": r.audit_revision_id,
        "scheduling": {k: getattr(p, k).isoformat() if isinstance(getattr(p, k), (date, time)) else getattr(p, k)
            for k in ("weekday", "start_time", "end_time", "valid_from", "valid_until",
                      "calendar_week_parity", "participation")}}
        for r, p in entries if any(k[0] == r.stable_id for k in base)]
    context_exceptions = [{"id": r.id, "stable_id": r.stable_id, "course_id": r.course_id,
        "status": r.information_status.value, "audit_revision_id": r.audit_revision_id,
        "date": p.date.isoformat(), "start": p.start_time.isoformat() if p.start_time else None,
        "end": p.end_time.isoformat() if p.end_time else None,
        "event_type": p.event_type.strip().upper(), "replaces_schedule_id": p.replaces_schedule_id}
        for r, p in exceptions]
    return occurrences, diagnostics, context_entries, context_exceptions


def project_academic_week(config: AppConfig, semester_id: str, week_start: date) -> list[AcademicOccurrenceV1]:
    with _db(config) as db:
        semester, zone, _, _, _ = _week(db, semester_id, week_start)
        return _academic(db, semester, zone, week_start)[0]


def project_calendar_week(config: AppConfig, semester_id: str, week_start: date) -> CalendarWeekProjectionV1:
    with _db(config) as db:
        return _project_calendar_week_in_transaction(db, semester_id, week_start)


def _project_calendar_week_in_transaction(db, semester_id: str, week_start: date) -> CalendarWeekProjectionV1:
    semester, zone, week_end, week_begin_at, week_end_at = _week(db, semester_id, week_start)
    academic, diagnostics, entry_context, exception_context = _academic(db, semester, zone, week_start)
    profile = db.scalar(select(PlanningProfile).where(PlanningProfile.semester_id == semester.id))
    windows = []
    constraints = []
    available = []
    occupied = []
    if profile is None:
        diagnostics.append(_diag("NO_PLANNING_PROFILE"))
    else:
        windows = db.scalars(select(AvailabilityWindow).where(
            AvailabilityWindow.planning_profile_id == profile.id,
            AvailabilityWindow.deactivated_revision.is_(None))).all()
        constraints = db.scalars(select(CalendarConstraint).where(
            CalendarConstraint.planning_profile_id == profile.id,
            CalendarConstraint.deactivated_revision.is_(None))).all()
        for row in windows:
            day = week_start + timedelta(days=WEEKDAYS.index(row.weekday))
            if (row.valid_from and day < row.valid_from) or (row.valid_until and day > row.valid_until):
                continue
            start = resolve_local(day, row.start_local_time, zone, row.start_fold)
            end = resolve_local(day, row.end_local_time, zone, row.end_fold)
            if start.astimezone(timezone.utc) >= end.astimezone(timezone.utc):
                raise ValueError("INVALID_AVAILABILITY_WINDOW")
            available.append(AvailabilityOccurrenceV1(availability_window_id=row.id,
                local_date=day, starts_at=start, ends_at=end))
        for row in constraints:
            if row.local_date:
                start = resolve_local(row.local_date, time.min, zone)
                end = resolve_local(row.local_date + timedelta(days=1), time.min, zone)
            else:
                start, end = _stored_utc(row.starts_at), _stored_utc(row.ends_at)
            if start < week_end_at and end > week_begin_at:
                occupied.append(ConstraintOccurrenceV1(calendar_constraint_id=row.id,
                    constraint_type=row.constraint_type, starts_at=start, ends_at=end,
                    all_day=row.local_date is not None))
    available.sort(key=lambda x: (x.starts_at, x.ends_at, x.availability_window_id))
    occupied.sort(key=lambda x: (x.starts_at, x.ends_at, x.calendar_constraint_id))
    diagnostics.sort(key=lambda x: (x.code, x.refs))
    context = {"schema_version": "1.0", "policy_version": POLICY,
        "semester": {"id": semester.id, "stable_id": semester.stable_id,
                     "state": semester.state.value, "timezone": semester.timezone},
        "week_start": week_start.isoformat(),
        "profile": {"id": profile.id, "revision": profile.revision,
            "default_action_duration_minutes": profile.default_action_duration_minutes,
            "max_daily_study_minutes": profile.max_daily_study_minutes} if profile else None,
        "windows": [{"id": r.id, "weekday": r.weekday,
            "start": r.start_local_time.isoformat(), "end": r.end_local_time.isoformat(),
            "start_fold": r.start_fold, "end_fold": r.end_fold,
            "valid_from": r.valid_from.isoformat() if r.valid_from else None,
            "valid_until": r.valid_until.isoformat() if r.valid_until else None,
            "created_revision": r.created_revision} for r in sorted(windows, key=lambda r: r.id)],
        "constraints": [{"id": r.id, "type": r.constraint_type,
            "starts_at": _stored_utc(r.starts_at).isoformat() if r.starts_at else None,
            "ends_at": _stored_utc(r.ends_at).isoformat() if r.ends_at else None,
            "local_date": r.local_date.isoformat() if r.local_date else None,
            "created_revision": r.created_revision} for r in sorted(constraints, key=lambda r: r.id)],
        "entries": entry_context, "exceptions": exception_context}
    return CalendarWeekProjectionV1(week_start=week_start, week_end_exclusive=week_end,
        timezone=semester.timezone, semester_id=semester.id, semester_stable_id=semester.stable_id,
        planning_profile_id=profile.id if profile else None,
        profile_revision=profile.revision if profile else None,
        default_action_duration_minutes=profile.default_action_duration_minutes if profile else None,
        max_daily_study_minutes=profile.max_daily_study_minutes if profile else None,
        availability_occurrences=available, constraint_occurrences=occupied,
        academic_occurrences=academic, diagnostics=diagnostics,
        context_fingerprint=_digest(context))
