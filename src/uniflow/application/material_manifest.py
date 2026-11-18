"""Read-only projection of weekly and schedule-linked material release rules."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, MaterialReleaseRule, ScheduleEntry, ScheduleException, Semester, SourceArtifact

WEEKDAYS = {name: number for number, name in enumerate(
    ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"))}


@dataclass(frozen=True)
class Expectation:
    id: str
    rule_id: str
    course_id: str
    material_kind: str
    release_date: date
    release_time: time | None
    information_status: str
    state: str


@dataclass(frozen=True)
class UnresolvedRule:
    rule_id: str
    course_id: str
    material_kind: str
    information_status: str
    reason: str
    state: str = "UNRESOLVED_RULE"


@dataclass(frozen=True)
class Manifest:
    expectations: tuple[Expectation, ...]
    unresolved_rules: tuple[UnresolvedRule, ...]
    unclassified_artifacts: tuple[tuple[str, str], ...]


def generate_material_manifest(config: AppConfig, semester_id: str, from_date: date,
                               through_date: date, as_of: datetime) -> Manifest:
    if from_date > through_date:
        raise ValueError("from_date must be on or before through_date")
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return _generate_material_manifest_in_transaction(
            session, semester_id, from_date, through_date, as_of)


def _schedule_release_days(session, semester, rule, payload, lower, upper, zone):
    # Reuse the same authoritative occurrence projection as the semester calendar.
    # Do not call the full calendar here: it also projects material expectations.
    from uniflow.application.planning_calendar import _academic

    schedule_id = payload.get("schedule_stable_id")
    schedule = session.scalar(select(ScheduleEntry).where(
        ScheduleEntry.stable_id == schedule_id, ScheduleEntry.course_id == rule.course_id))
    if schedule is None or schedule.information_status.value == "UNKNOWN":
        return (), "same-course schedule_stable_id and known schedule information status are required"
    linked_exceptions = {}
    for row in session.scalars(select(ScheduleException).where(ScheduleException.course_id == rule.course_id)):
        exception = json.loads(row.payload_json)
        if exception.get("replaces_schedule_id") == schedule_id:
            linked_exceptions[row.id] = date.fromisoformat(exception["date"])
    days, blocked_days, issues = {}, set(), set()
    monday = lower - timedelta(days=lower.weekday())
    while monday <= upper:
        occurrences, diagnostics, _, _ = _academic(
            session, semester, zone, monday, calendar_start=lower,
            calendar_end=upper, calendar_course_id=rule.course_id)
        for diagnostic in diagnostics:
            for ref in diagnostic.refs:
                if ref in linked_exceptions:
                    # Only this linked date is uncertain; keep unrelated occurrences.
                    day = linked_exceptions[ref]
                    blocked_days.add(day)
                    issues.add(f"{diagnostic.code}@{day.isoformat()}")
        days.update((item.local_date, item.starts_at.astimezone(zone).time())
                    for item in occurrences
                    if item.schedule_entry_stable_id == schedule_id and item.starts_at is not None)
        monday += timedelta(days=7)
    reason = "schedule calendar diagnostics: " + ", ".join(sorted(issues)) if issues else None
    return tuple((day, days[day]) for day in sorted(days.keys() - blocked_days)), reason


def _generate_material_manifest_in_transaction(session, semester_id: str, from_date: date,
                                                through_date: date, as_of: datetime,
                                                course_stable_id: str | None = None) -> Manifest:
    if from_date > through_date:
        raise ValueError("from_date must be on or before through_date")
    semester = session.scalar(select(Semester).where(Semester.stable_id == semester_id))
    if semester is None:
        raise ValueError(f"Unknown semester: {semester_id}")
    zone = ZoneInfo(semester.timezone)
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone offset")
    local_now = as_of.astimezone(zone)
    query = select(MaterialReleaseRule, Course.stable_id).join(
        Course, MaterialReleaseRule.course_id == Course.id).where(
        Course.semester_id == semester.id)
    if course_stable_id is not None:
        query = query.where(Course.stable_id == course_stable_id)
    rows = session.execute(query.order_by(MaterialReleaseRule.stable_id)).all()
    expectations = []
    unresolved = []
    lower = max(from_date, semester.starts_on) if semester.starts_on else from_date
    upper = min(through_date, semester.ends_on) if semester.ends_on else through_date
    for rule, course_id in rows:
        payload = json.loads(rule.payload_json)
        kind = payload.get("material_kind", "UNKNOWN")
        status = rule.information_status.value
        weekday = payload.get("weekday")
        cadence = (payload.get("cadence") or "").upper()
        if cadence == "EACH_SCHEDULE_OCCURRENCE" and status != "UNKNOWN":
            releases, reason = _schedule_release_days(
                session, semester, rule, payload, lower, upper, zone)
            if reason:
                unresolved.append(UnresolvedRule(rule.stable_id, course_id, kind, status, reason))
        elif cadence == "WEEKLY" and weekday in WEEKDAYS and status != "UNKNOWN":
            day = lower + timedelta(days=(WEEKDAYS[weekday] - lower.weekday()) % 7)
            releases = []
            release_time = time.fromisoformat(payload["time"]) if payload.get("time") else None
            while day <= upper:
                releases.append((day, release_time))
                day += timedelta(days=7)
        else:
            unresolved.append(UnresolvedRule(rule.stable_id, course_id, kind, status,
                "weekly cadence, weekday, and known information status are required"))
            continue
        for day, release_time in releases:
            if release_time is None:
                future = day > local_now.date()
            else:
                future = datetime.combine(day, release_time, zone) > local_now
            expectations.append(Expectation(f"{rule.stable_id}@{day.isoformat()}", rule.stable_id,
                course_id, kind, day, release_time, status,
                "UPCOMING" if future else "NOT_LINKED_IN_UNIFLOW"))
    artifacts = session.scalars(select(SourceArtifact).where(~SourceArtifact.materials.any()).order_by(
        SourceArtifact.sha256)).all()
    return Manifest(tuple(sorted(expectations, key=lambda e: (e.release_date,
        e.release_time or time.min, e.id))), tuple(unresolved),
        tuple((a.sha256, a.original_filename) for a in artifacts))
