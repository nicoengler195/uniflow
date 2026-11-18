"""Bounded, detached dashboard view over the authoritative semester calendar."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from uniflow.application.application_read_models import _read_transaction
from uniflow.application.planning_calendar import _semester
from uniflow.application.semester_calendar import CalendarEventV1, project_semester_calendar


@dataclass(frozen=True)
class DashboardProjectionV1:
    semester_id: str
    timezone: str
    as_of: datetime
    start: date
    end: date
    classes: tuple[CalendarEventV1, ...]
    deadlines: tuple[CalendarEventV1, ...]
    expected_materials: tuple[CalendarEventV1, ...]
    recent_materials: tuple[CalendarEventV1, ...]
    preview: tuple[CalendarEventV1, ...]
    diagnostics: tuple[str, ...]


def project_dashboard(config, semester_id, as_of, course_id=None):
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_TIMEZONE_AWARE")
    with _read_transaction(config) as db:
        zone = ZoneInfo(_semester(db, semester_id).timezone)
    instant = as_of.astimezone(zone)
    today = instant.date()
    end = today + timedelta(days=6)
    # One bounded expansion for both the seven-day lookback and forward view.
    calendar = project_semester_calendar(config, semester_id,
        today - timedelta(days=6), end, course_id, as_of=instant)
    future = tuple(e for e in calendar.events if today <= e.day <= end
        and e.event_type != "MATERIAL_OBSERVED"
        and (e.starts_at is None or e.starts_at >= instant))
    classes = tuple(e for e in future if e.event_type not in
        {"DEADLINE", "CANCELLATION", "MATERIAL_EXPECTED", "MATERIAL_OBSERVED"})
    deadlines = tuple(e for e in future if e.event_type == "DEADLINE")
    expected = tuple(e for e in future if e.event_type == "MATERIAL_EXPECTED")
    seen = set()
    recent = []
    for event in reversed(calendar.events):
        if (event.event_type == "MATERIAL_OBSERVED" and event.day <= today
                and event.starts_at is not None and event.starts_at <= instant
                and event.artifact_id not in seen):
            seen.add(event.artifact_id)
            recent.append(event)
    return DashboardProjectionV1(semester_id, zone.key, instant, today, end,
        classes, deadlines, expected, tuple(recent), future, calendar.diagnostics)
