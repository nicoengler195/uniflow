"""Pure deterministic weekly placement, with an optional read-only capture wrapper."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from uniflow.schemas.calendar_v1 import CalendarWeekProjectionV1, canonical_calendar_json
from uniflow.schemas.planning_v1 import PlanningRecommendationV1, canonical_planning_json
from uniflow.schemas.weekly_schedule_v1 import (
    WeeklyBlockV1, WeeklyDeadlineConstraintV1, WeeklyDiagnosticV1,
    WeeklyScheduleV1, WeeklySemesterV1, WeeklyUnscheduledActionV1,
)

POLICY = "weekly-scheduler-v1"
UTC = timezone.utc
Interval = tuple[datetime, datetime]  # UTC-aware, half-open [start, end)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _instant(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    """Resolve a local midnight by round trip; never assume a 24-hour day."""
    naive = datetime.combine(day, time.min)
    matches = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        back = candidate.astimezone(UTC).astimezone(zone)
        if back.replace(tzinfo=None) == naive and back.fold == fold:
            matches.append(candidate)
    if not matches:
        raise ValueError("INVALID_LOCAL_MIDNIGHT")
    if len(matches) > 1:
        raise ValueError("AMBIGUOUS_LOCAL_MIDNIGHT")
    return matches[0]


def _clip(interval: Interval, lower: datetime, upper: datetime) -> Interval | None:
    start, end = max(interval[0], lower), min(interval[1], upper)
    return (start, end) if start < end else None


def _union(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _subtract(legal: list[Interval], occupied: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in legal:
        cursor = start
        for blocked_start, blocked_end in occupied:
            if blocked_end <= cursor:
                continue
            if blocked_start >= end:
                break
            if cursor < blocked_start:
                result.append((cursor, min(blocked_start, end)))
            cursor = max(cursor, min(blocked_end, end))
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def _split_days(intervals: list[Interval], week_start: date, zone: ZoneInfo) -> list[Interval]:
    midnights = [_instant(_local_midnight(week_start + timedelta(days=i), zone))
                 for i in range(1, 8)]
    result: list[Interval] = []
    for start, end in intervals:
        cursor = start
        for boundary in midnights:
            if cursor < boundary < end:
                result.append((cursor, boundary))
                cursor = boundary
        if cursor < end:
            result.append((cursor, end))
    return result


def _deadline_intervals(intervals: list[Interval], constraints: list[WeeklyDeadlineConstraintV1],
                        zone: ZoneInfo) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        day = start.astimezone(zone).date()
        if any(c.kind == "DATE_ONLY" and day >= c.due_date for c in constraints):
            continue
        for c in constraints:
            if c.kind == "TIMED":
                end = min(end, _instant(c.due_at))
        if start < end:
            result.append((start, end))
    return result


def _validate(nba: PlanningRecommendationV1, calendar: CalendarWeekProjectionV1,
              durations: Mapping[str, int] | None) -> tuple[PlanningRecommendationV1, CalendarWeekProjectionV1, dict[str, int], ZoneInfo]:
    if not isinstance(nba, PlanningRecommendationV1) or nba.schema_version != "1.0" or nba.policy_version != "next-best-action-v1":
        raise ValueError("UNSUPPORTED_NBA_CONTRACT")
    if not isinstance(calendar, CalendarWeekProjectionV1) or calendar.schema_version != "1.0" or calendar.policy_version != "calendar-foundation-v1":
        raise ValueError("UNSUPPORTED_CALENDAR_CONTRACT")
    try:
        nba = PlanningRecommendationV1.model_validate(nba.model_dump(mode="python"), strict=True)
        calendar = CalendarWeekProjectionV1.model_validate(calendar.model_dump(mode="python"), strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValueError("INVALID_SCHEDULER_SNAPSHOT") from exc
    if (nba.semester.id != calendar.semester_id or nba.semester.stable_id != calendar.semester_stable_id
            or nba.semester.timezone != calendar.timezone):
        raise ValueError("SCHEDULER_SEMESTER_MISMATCH")
    if calendar.week_start.weekday() != 0 or calendar.week_end_exclusive != calendar.week_start + timedelta(days=7):
        raise ValueError("INVALID_CALENDAR_WEEK")
    if calendar.planning_profile_id is None:
        if any(v is not None for v in (calendar.profile_revision, calendar.default_action_duration_minutes,
                                       calendar.max_daily_study_minutes)) or calendar.availability_occurrences or calendar.constraint_occurrences:
            raise ValueError("INVALID_PROFILE_SNAPSHOT")
    elif type(calendar.profile_revision) is not int or calendar.profile_revision < 1:
        raise ValueError("INVALID_PROFILE_SNAPSHOT")
    for value in (calendar.default_action_duration_minutes, calendar.max_daily_study_minutes):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("INVALID_PROFILE_SNAPSHOT")
    try:
        zone = ZoneInfo(calendar.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("INVALID_SCHEDULER_TIMEZONE") from exc
    actions = nba.recommendations
    ids = [a.action_id for a in actions]
    if any(not x for x in ids) or len(ids) != len(set(ids)) or [a.rank for a in actions] != list(range(1, len(actions) + 1)):
        raise ValueError("INVALID_NBA_ACTIONS")
    if durations is None:
        provided: dict[str, int] = {}
    elif isinstance(durations, Mapping):
        provided = dict(durations)
    else:
        raise ValueError("INVALID_DURATION_MAPPING")
    if set(provided) - set(ids):
        raise ValueError("UNKNOWN_DURATION_ACTION")
    if any(type(v) is not int or v <= 0 for v in provided.values()):
        raise ValueError("INVALID_ACTION_DURATION")
    for action in actions:
        for d in action.deadline_context:
            if d.actionable and ((d.is_date_only and d.due_date is None)
                                 or (not d.is_date_only and d.due_at is None)):
                raise ValueError("INVALID_ACTIONABLE_DEADLINE")
    return nba, calendar, provided, zone


def _constraints(action) -> list[WeeklyDeadlineConstraintV1]:
    result = [WeeklyDeadlineConstraintV1(deadline_id=d.id, deadline_stable_id=d.stable_id,
        kind="DATE_ONLY" if d.is_date_only else "TIMED",
        due_at=None if d.is_date_only else d.due_at,
        due_date=d.due_date if d.is_date_only else None)
        for d in action.deadline_context if d.actionable]
    return sorted(result, key=lambda c: (c.kind, c.due_date.isoformat() if c.due_date else c.due_at.isoformat(), c.deadline_id))


def schedule_captured_week(nba: PlanningRecommendationV1, calendar: CalendarWeekProjectionV1,
                           durations: Mapping[str, int] | None = None) -> WeeklyScheduleV1:
    """Place captured NBA actions without database access, I/O, or a clock read."""
    nba, calendar, provided, zone = _validate(nba, calendar, durations)
    week_start = _instant(_local_midnight(calendar.week_start, zone))
    week_end = _instant(_local_midnight(calendar.week_end_exclusive, zone))
    as_of = _instant(nba.as_of)
    floor = max(week_start, as_of)
    elapsed = floor >= week_end
    legal_raw = [(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.availability_occurrences]
    legal_raw += [(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.constraint_occurrences
                  if x.constraint_type == "AVAILABLE_ADDITION"]
    occupied_raw = [(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.constraint_occurrences
                    if x.constraint_type == "BLOCKED"]
    occupied_raw += [(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.academic_occurrences
                     if x.hard_block]
    legal = _union([v for raw in legal_raw if (v := _clip(raw, floor, week_end)) is not None]) if not elapsed else []
    occupied = _union([v for raw in occupied_raw if (v := _clip(raw, week_start, week_end)) is not None])
    free_base = _split_days(_subtract(legal, occupied), calendar.week_start, zone)
    remaining = list(free_base)
    used: dict[date, int] = {}
    resolved = []
    for action in nba.recommendations:
        if action.action_id in provided:
            resolved.append((action, provided[action.action_id], "EXPLICIT_OVERRIDE"))
        elif calendar.default_action_duration_minutes is not None:
            resolved.append((action, calendar.default_action_duration_minutes, "PROFILE_DEFAULT"))
        else:
            resolved.append((action, None, "MISSING"))
    nba_sha = hashlib.sha256(canonical_planning_json(nba)).hexdigest()
    calendar_sha = hashlib.sha256(canonical_calendar_json(calendar)).hexdigest()
    context = _digest({"schema_version": "1.0", "policy_version": POLICY,
        "week_start": calendar.week_start.isoformat(), "week_end_exclusive": calendar.week_end_exclusive.isoformat(),
        "timezone": calendar.timezone, "as_of": nba.as_of.isoformat(),
        "nba_payload_sha256": nba_sha, "nba_policy_version": nba.policy_version,
        "nba_context_fingerprint": nba.context_fingerprint,
        "calendar_payload_sha256": calendar_sha, "calendar_policy_version": calendar.policy_version,
        "calendar_context_fingerprint": calendar.context_fingerprint,
        "planning_profile_id": calendar.planning_profile_id, "profile_revision": calendar.profile_revision,
        "default_action_duration_minutes": calendar.default_action_duration_minutes,
        "max_daily_study_minutes": calendar.max_daily_study_minutes,
        "durations": sorted(({"action_id": a.action_id, "duration_source": source, "minutes": minutes}
                             for a, minutes, source in resolved), key=lambda item: item["action_id"])})
    blocks: list[WeeklyBlockV1] = []
    unscheduled: list[WeeklyUnscheduledActionV1] = []
    for action, minutes, source in resolved:
        constraints = _constraints(action)
        reason = None
        chosen: Interval | None = None
        if calendar.planning_profile_id is None:
            reason = "NO_PLANNING_PROFILE"
        elif minutes is None:
            reason = "MISSING_DURATION"
        elif not legal:
            reason = "NO_AVAILABLE_WINDOW"
        else:
            eligible_base = _deadline_intervals(free_base, constraints, zone)
            eligible_current = _deadline_intervals(remaining, constraints, zone)
            deadline_already_passed = any(
                (_instant(c.due_at) <= floor if c.kind == "TIMED" else
                 c.due_date <= floor.astimezone(zone).date()) for c in constraints)
            if constraints and (deadline_already_passed or (free_base and not eligible_base)):
                reason = "NO_PRE_DEADLINE_SLOT"
            elif not any((end - start).total_seconds() >= minutes * 60 for start, end in eligible_base):
                reason = "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL" if eligible_base else "NO_FREE_INTERVAL"
            else:
                spatial = False
                for start, end in eligible_current:
                    if (end - start).total_seconds() < minutes * 60:
                        continue
                    spatial = True
                    day = start.astimezone(zone).date()
                    cap = calendar.max_daily_study_minutes
                    if cap is not None and used.get(day, 0) + minutes > cap:
                        continue
                    chosen = (start, start + timedelta(minutes=minutes))
                    break
                if chosen is None:
                    reason = "DAILY_LIMIT_REACHED" if spatial else "NO_FREE_INTERVAL"
        if chosen is None:
            unscheduled.append(WeeklyUnscheduledActionV1(action_id=action.action_id,
                nba_rank=action.rank, action_type=action.action_type,
                course_id=action.course_id, course_stable_id=action.course_stable_id,
                target_skill_ids=action.target_skill_ids, existing_session_id=action.existing_session_id,
                resolved_duration_minutes=minutes, duration_source=source, primary_reason=reason,
                deadline_constraints=constraints,
                references=sorted({c.deadline_id for c in constraints})))
            continue
        start, end = chosen
        day = start.astimezone(zone).date()
        if end.astimezone(zone).date() != day:
            raise ValueError("SCHEDULER_MIDNIGHT_INVARIANT")
        used[day] = used.get(day, 0) + minutes
        remaining = _subtract(remaining, [chosen])
        block_id = "weekly-block:" + _digest({"policy_version": POLICY,
            "scheduler_context_fingerprint": context, "action_id": action.action_id,
            "starts_at_utc": start.isoformat(), "ends_at_utc": end.isoformat()})
        blocks.append(WeeklyBlockV1(block_id=block_id, action_id=action.action_id,
            nba_rank=action.rank, action_type=action.action_type,
            course_id=action.course_id, course_stable_id=action.course_stable_id,
            target_skill_ids=action.target_skill_ids, existing_session_id=action.existing_session_id,
            starts_at=start.astimezone(zone), ends_at=end.astimezone(zone),
            duration_minutes=minutes, duration_source=source, deadline_constraints=constraints))
    blocks.sort(key=lambda b: (_instant(b.starts_at), _instant(b.ends_at), b.nba_rank, b.block_id))
    status = ("NO_ELIGIBLE_ACTIONS" if not nba.recommendations else "OK" if not unscheduled else
              "NO_SCHEDULED_ACTIONS" if not blocks else "PARTIAL")
    return WeeklyScheduleV1(week_start=calendar.week_start,
        week_end_exclusive=calendar.week_end_exclusive, timezone=calendar.timezone,
        as_of=nba.as_of, semester=WeeklySemesterV1(id=nba.semester.id, stable_id=nba.semester.stable_id),
        nba_policy_version=nba.policy_version, nba_context_fingerprint=nba.context_fingerprint,
        nba_payload_sha256=nba_sha, calendar_policy_version=calendar.policy_version,
        calendar_context_fingerprint=calendar.context_fingerprint, calendar_payload_sha256=calendar_sha,
        planning_profile_id=calendar.planning_profile_id, profile_revision=calendar.profile_revision,
        default_action_duration_minutes=calendar.default_action_duration_minutes,
        max_daily_study_minutes=calendar.max_daily_study_minutes, status=status,
        context_fingerprint=context, scheduled_blocks=blocks, unscheduled_actions=unscheduled,
        nba_diagnostics=sorted(nba.diagnostics, key=lambda d: (d.code, d.refs)),
        calendar_diagnostics=sorted(calendar.diagnostics, key=lambda d: (d.code, d.refs)),
        scheduler_diagnostics=[WeeklyDiagnosticV1(code="WEEK_ELAPSED")] if elapsed else [])


def schedule_week(config, semester_id: str, week_start: date, *, as_of: datetime | None = None,
                  course_id: str | None = None, limit: int = 10,
                  durations: Mapping[str, int] | None = None) -> WeeklyScheduleV1:
    """Capture each existing read model once, then invoke the pure scheduler."""
    from uniflow.application.planning import plan_next_actions
    from uniflow.application.planning_calendar import project_calendar_week

    captured_as_of = as_of if as_of is not None else datetime.now(UTC)
    if captured_as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    nba = plan_next_actions(config, semester_id, as_of=captured_as_of,
                            course_id=course_id, limit=limit)
    calendar = project_calendar_week(config, semester_id, week_start)
    return schedule_captured_week(nba, calendar, durations)
