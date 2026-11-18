"""Read-only semester calendar, inclusive local date range; no personal planning state."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from uniflow.application.application_read_models import _read_transaction, _deadline_data
from uniflow.application.planning_calendar import _academic, _semester
from uniflow.infrastructure.database.models import Course, ScheduleException
from uniflow.schemas.semester_audit import ScheduleExceptionV1

@dataclass(frozen=True)
class CalendarEventV1:
    event_id: str
    event_type: str
    source_id: str
    source_table: str
    semester_id: str
    course_id: str
    course_name: str
    title: str
    day: date
    starts_at: datetime | None
    ends_at: datetime | None
    status: str
    information_status: str
    location: str | None = None
    exception_id: str | None = None
    notes: str | None = None
    material_kind: str | None = None
    material_ids: tuple[str, ...] = ()
    artifact_id: str | None = None
    date_basis: str | None = None
    expectation_ids: tuple[str, ...] = ()
    expected_time: time | None = None

@dataclass(frozen=True)
class CalendarProjectionV1:
    semester_id: str
    semester_name: str
    timezone: str
    courses: tuple
    events: tuple[CalendarEventV1, ...]
    diagnostics: tuple[str, ...]


def project_semester_calendar(config, semester_id: str, start: date, end: date,
                              course_id: str | None = None, *, as_of: datetime | None = None) -> CalendarProjectionV1:
    if type(start) is not date or type(end) is not date or end < start or (end-start).days > 366:
        raise ValueError("INVALID_CALENDAR_RANGE")
    with _read_transaction(config) as db:
        semester = _semester(db, semester_id)
        zone = ZoneInfo(semester.timezone)
        if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
            raise ValueError("AS_OF_MUST_BE_TIMEZONE_AWARE")
        instant = as_of.astimezone(zone) if as_of is not None else datetime.now(zone)
        courses = list(db.scalars(select(Course).where(Course.semester_id == semester.id).order_by(Course.name, Course.id)))
        selected = [c for c in courses if course_id is None or c.id == course_id]
        names = {c.id: c.name for c in selected}
        lower = max(start, semester.starts_on) if semester.starts_on else start
        upper = min(end, semester.ends_on) if semester.ends_on else end
        exception_payloads = {r.id: ScheduleExceptionV1.model_validate_json(r.payload_json)
            for r in db.scalars(select(ScheduleException).where(ScheduleException.course_id.in_(names)))}
        events, diagnostics = [], []
        monday = lower - timedelta(days=lower.weekday())
        while monday <= upper:
            # Existing authoritative exception/parity rules, in the same read snapshot.
            try:
                academic, issues, _, _ = _academic(db, semester, zone, monday, calendar_start=lower, calendar_end=upper, calendar_course_id=course_id)
            except ValueError as exc:
                # Never silently drop a week on an unresolved local time or conflict.
                raise ValueError(f"Kalenderwoche {monday}: {exc}") from exc
            diagnostics.extend(f"{d.code}: {', '.join(d.refs)}" for d in issues)
            for item in academic:
                if item.course_id not in names or not lower <= item.local_date <= upper:
                    continue
                ep = exception_payloads.get(item.schedule_exception_id)
                events.append(CalendarEventV1(item.occurrence_id, item.source_kind,
                    item.schedule_exception_id or item.schedule_entry_id,
                    "schedule_exceptions" if item.schedule_exception_id else "schedule_entries",
                    semester.stable_id, item.course_id, names[item.course_id],
                    item.title or item.activity_type or names[item.course_id], item.local_date,
                    item.starts_at, item.ends_at, item.source_kind, item.information_status,
                    item.location, item.schedule_exception_id,
                    "; ".join([*(d.code for d in item.diagnostics), *([f"Bezug: {ep.replaces_schedule_id}; {ep.description or ''}"] if ep else [])]) or None))
            monday += timedelta(days=7)
        # Cancellations remain visible as notices, never as active classes.
        for row in db.scalars(select(ScheduleException).where(ScheduleException.course_id.in_(names))):
            p = ScheduleExceptionV1.model_validate_json(row.payload_json)
            if lower <= p.date <= upper and p.event_type.strip().upper() == "CANCELLATION":
                events.append(CalendarEventV1(f"exception:{row.id}", "CANCELLATION", row.id,
                    "schedule_exceptions", semester.stable_id, row.course_id, names[row.course_id],
                    p.title, p.date, None, None, "CANCELLATION_NOTICE", row.information_status.value,
                    exception_id=row.id, notes=f"Bezug: {p.replaces_schedule_id}; {p.description or ''}"))
        # Read model groups can exceed its dashboard limit: query each group with no truncation.
        # The helper accepts an integer slicing bound; use the total row count as bound.
        from uniflow.infrastructure.database.models import Deadline
        from sqlalchemy import func
        count = db.scalar(select(func.count()).select_from(Deadline))
        summary, _ = _deadline_data(db, selected, zone, instant, max(1, count))
        for group in (summary.authoritative_upcoming, summary.authoritative_overdue,
                      summary.due_fact_diagnostics):
            for item in group:
                day = item.due_date or (item.due_at.date() if item.due_at else None)
                if day is not None and lower <= day <= upper:
                    events.append(CalendarEventV1(f"deadline:{item.deadline_id}", "DEADLINE",
                        item.deadline_id, "deadlines", semester.stable_id, item.course_id,
                        names[item.course_id], item.title, day, item.due_at, None, item.status,
                        item.information_status, notes="Datum ohne bekannte Uhrzeit" if item.is_date_only else None))
        from uniflow.application.material_calendar import project_material_events
        material_events, material_diagnostics = project_material_events(
            db, semester, selected, start, end, instant)
        events.extend(material_events)
        diagnostics.extend(material_diagnostics)
        # B1 is optional on legacy schemas; this projection remains read-only.
        from sqlalchemy import inspect
        if 'confirmed_study_blocks' in inspect(db.connection()).get_table_names():
            from uniflow.application.study_block_changes import effective_blocks
            for block in effective_blocks(db, semester.id, include_cancelled=True):
                if block['status']=='CANCELLED': continue
                begin = datetime.fromisoformat(block['starts_at']).astimezone(zone)
                finish = datetime.fromisoformat(block['ends_at']).astimezone(zone)
                if block['course_id'] in names and lower <= begin.date() <= upper:
                    events.append(CalendarEventV1('study:'+block['id'], 'STUDY_BLOCK_CONFIRMED',
                        block['id'], 'confirmed_study_blocks', semester.stable_id, block['course_id'],
                        names[block['course_id']], block['title'], begin.date(), begin, finish,
                        block['status'], 'CONFIRMED', notes=f"Lernblock · {block['status']} · Revision {block['revision']} · Abschluss ist keine Mastery-Bewertung"))
        events.sort(key=lambda e: (e.day, e.starts_at.timetz().replace(tzinfo=None) if e.starts_at else time.min, e.event_id))
        return CalendarProjectionV1(semester.stable_id, semester.name, semester.timezone,
            tuple((c.id, c.name) for c in courses), tuple(events), tuple(sorted(set(diagnostics))))
