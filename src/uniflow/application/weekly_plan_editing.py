"""Pure transformations and validation for immutable manual weekly plans."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from uniflow.application.weekly_scheduler import _instant, _local_midnight, _union
from uniflow.schemas.calendar_v1 import CalendarWeekProjectionV1
from uniflow.schemas.weekly_plan_v1 import (
    PlanBlockV1, PlanUnscheduledActionV1, WeeklyPlanRevisionV1, canonical_weekly_plan_revision_json,
)
from uniflow.schemas.weekly_schedule_v1 import WeeklyScheduleV1

POLICY = "weekly-plan-revision-v1"


def plan_block_id(revision_id: str, action_id: str) -> str:
    data = json.dumps({"policy_version": POLICY, "revision_id": revision_id, "action_id": action_id},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "plan-block:" + hashlib.sha256(data).hexdigest()


def _positive(minutes: int) -> int:
    if type(minutes) is not int or minutes <= 0:
        raise ValueError("INVALID_BLOCK_DURATION")
    return minutes


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("SOURCE_ARTIFACT_INVALID") from exc


def _start(value: datetime, zone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("INVALID_BLOCK_START")
    local = value.astimezone(timezone.utc).astimezone(zone)
    # ISO input carries an offset, not a zone name. Its wall time and offset
    # must describe a real instant in the Semester zone, including DST folds.
    if local.replace(tzinfo=None) != value.replace(tzinfo=None) or local.utcoffset() != value.utcoffset():
        raise ValueError("INVALID_BLOCK_START")
    return local


def _end(start: datetime, minutes: int, zone: ZoneInfo) -> datetime:
    _positive(minutes)
    try:
        return (start.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(zone)
    except (OverflowError, ValueError) as exc:
        raise ValueError("INVALID_BLOCK_DURATION") from exc


def _child(parent: WeeklyPlanRevisionV1, revision_id: str, revision_number: int,
           created_at: datetime, blocks: list[PlanBlockV1],
           unscheduled: list[PlanUnscheduledActionV1]) -> WeeklyPlanRevisionV1:
    if revision_number != parent.revision_number + 1 or created_at.utcoffset() is None:
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    copied = [b.model_copy(update={"block_id": plan_block_id(revision_id, b.action_id)}) for b in blocks]
    copied.sort(key=lambda b: (_instant(b.starts_at), _instant(b.ends_at), b.nba_rank, b.block_id))
    unscheduled.sort(key=lambda u: (u.nba_rank, u.action_id))
    return parent.model_copy(update={"revision_id": revision_id, "revision_number": revision_number,
        "revision_kind": "MANUAL_EDIT", "parent_revision_id": parent.revision_id,
        "created_at": created_at, "blocks": copied, "unscheduled_actions": unscheduled})


def _block(parent: WeeklyPlanRevisionV1, block_id: str) -> PlanBlockV1:
    for block in parent.blocks:
        if block.block_id == block_id:
            return block
    raise ValueError("UNKNOWN_PLAN_BLOCK")


def transform_move_block(parent: WeeklyPlanRevisionV1, revision_id: str, revision_number: int,
                         created_at: datetime, block_id: str, new_start: datetime) -> WeeklyPlanRevisionV1:
    target = _block(parent, block_id)
    zone = _zone(parent.timezone)
    start = _start(new_start, zone)
    if _instant(start) == _instant(target.starts_at):
        raise ValueError("INVALID_BLOCK_START")
    replacement = target.model_copy(update={"starts_at": start,
        "ends_at": _end(start, target.duration_minutes, zone), "placement_source": "MANUAL_EDIT"})
    return _child(parent, revision_id, revision_number, created_at,
        [replacement if b.block_id == block_id else b for b in parent.blocks], list(parent.unscheduled_actions))


def transform_set_block_duration(parent: WeeklyPlanRevisionV1, revision_id: str, revision_number: int,
                                 created_at: datetime, block_id: str, minutes: int) -> WeeklyPlanRevisionV1:
    target = _block(parent, block_id)
    _positive(minutes)
    if minutes == target.duration_minutes:
        raise ValueError("INVALID_BLOCK_DURATION")
    zone = _zone(parent.timezone)
    replacement = target.model_copy(update={"ends_at": _end(target.starts_at, minutes, zone),
        "duration_minutes": minutes, "duration_source": "MANUAL_EDIT"})
    return _child(parent, revision_id, revision_number, created_at,
        [replacement if b.block_id == block_id else b for b in parent.blocks], list(parent.unscheduled_actions))


def transform_remove_block(parent: WeeklyPlanRevisionV1, revision_id: str, revision_number: int,
                           created_at: datetime, block_id: str) -> WeeklyPlanRevisionV1:
    target = _block(parent, block_id)
    item = PlanUnscheduledActionV1(action_id=target.action_id, nba_rank=target.nba_rank,
        action_type=target.action_type, course_id=target.course_id,
        course_stable_id=target.course_stable_id, target_skill_ids=list(target.target_skill_ids),
        existing_session_id=target.existing_session_id,
        resolved_duration_minutes=target.duration_minutes, duration_source=target.duration_source,
        primary_reason="USER_REMOVED", deadline_constraints=list(target.deadline_constraints),
        references=sorted({x.deadline_id for x in target.deadline_constraints}))
    return _child(parent, revision_id, revision_number, created_at,
        [b for b in parent.blocks if b.block_id != block_id], [*parent.unscheduled_actions, item])


def transform_place_action(parent: WeeklyPlanRevisionV1, source_schedule: WeeklyScheduleV1,
                           revision_id: str, revision_number: int, created_at: datetime,
                           action_id: str, start: datetime, duration_minutes: int | None = None) -> WeeklyPlanRevisionV1:
    if any(b.action_id == action_id for b in parent.blocks):
        raise ValueError("ACTION_ALREADY_SCHEDULED")
    item = next((u for u in parent.unscheduled_actions if u.action_id == action_id), None)
    if item is None:
        raise ValueError("UNKNOWN_UNSCHEDULED_ACTION")
    minutes = item.resolved_duration_minutes if duration_minutes is None else duration_minutes
    _positive(minutes)
    zone = _zone(parent.timezone)
    local = _start(start, zone)
    original = next((b.block_id for b in source_schedule.scheduled_blocks if b.action_id == action_id), None)
    block = PlanBlockV1(block_id=plan_block_id(revision_id, action_id),
        source_scheduler_block_id=original, action_id=action_id, nba_rank=item.nba_rank,
        action_type=item.action_type, course_id=item.course_id, course_stable_id=item.course_stable_id,
        target_skill_ids=list(item.target_skill_ids), existing_session_id=item.existing_session_id,
        starts_at=local, ends_at=_end(local, minutes, zone), duration_minutes=minutes,
        duration_source="MANUAL_EDIT" if duration_minutes is not None else item.duration_source,
        deadline_constraints=list(item.deadline_constraints), placement_source="MANUAL_EDIT")
    return _child(parent, revision_id, revision_number, created_at,
        [*parent.blocks, block], [u for u in parent.unscheduled_actions if u.action_id != action_id])


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def validate_weekly_plan_layout(revision: WeeklyPlanRevisionV1, calendar: CalendarWeekProjectionV1,
                                source_schedule: WeeklyScheduleV1) -> None:
    """Check a complete revision against its immutable source snapshots."""
    if (revision.week_start != calendar.week_start or revision.week_start != source_schedule.week_start
            or revision.week_end_exclusive != calendar.week_end_exclusive
            or revision.week_end_exclusive != source_schedule.week_end_exclusive
            or revision.timezone != calendar.timezone or revision.timezone != source_schedule.timezone):
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    source = {x.action_id: x for x in [*source_schedule.scheduled_blocks, *source_schedule.unscheduled_actions]}
    all_items = [*revision.blocks, *revision.unscheduled_actions]
    if len(source) != len(source_schedule.scheduled_blocks) + len(source_schedule.unscheduled_actions) \
            or len(all_items) != len(source) or {x.action_id for x in all_items} != set(source):
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    original_ids = {b.action_id: b.block_id for b in source_schedule.scheduled_blocks}
    for item in all_items:
        orig = source[item.action_id]
        for name in ("action_id", "nba_rank", "action_type", "course_id", "course_stable_id",
                     "target_skill_ids", "existing_session_id", "deadline_constraints"):
            if getattr(item, name) != getattr(orig, name):
                raise ValueError("SOURCE_ARTIFACT_INVALID")
    for block in revision.blocks:
        if block.block_id != plan_block_id(revision.revision_id, block.action_id) \
                or block.source_scheduler_block_id != original_ids.get(block.action_id):
            raise ValueError("SOURCE_ARTIFACT_INVALID")
    if revision.blocks != sorted(revision.blocks, key=lambda b: (
            _instant(b.starts_at), _instant(b.ends_at), b.nba_rank, b.block_id)) \
            or revision.unscheduled_actions != sorted(revision.unscheduled_actions, key=lambda u: (u.nba_rank, u.action_id)):
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    zone = _zone(revision.timezone)
    week_start = _instant(_local_midnight(revision.week_start, zone))
    week_end = _instant(_local_midnight(revision.week_end_exclusive, zone))
    legal = _union([(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.availability_occurrences]
        + [(_instant(x.starts_at), _instant(x.ends_at)) for x in calendar.constraint_occurrences
           if x.constraint_type == "AVAILABLE_ADDITION"])
    personal = [((_instant(x.starts_at), _instant(x.ends_at)), x.calendar_constraint_id)
        for x in calendar.constraint_occurrences if x.constraint_type == "BLOCKED"]
    academic = [((_instant(x.starts_at), _instant(x.ends_at)), x.occurrence_id)
        for x in calendar.academic_occurrences if x.hard_block]
    intervals: list[tuple[datetime, datetime]] = []
    used: dict[object, int] = {}
    for block in revision.blocks:
        _start(block.starts_at, zone)
        _start(block.ends_at, zone)
        start, end = _instant(block.starts_at), _instant(block.ends_at)
        if block.starts_at.utcoffset() is None or block.ends_at.utcoffset() is None \
                or end <= start or (end - start).total_seconds() != block.duration_minutes * 60:
            raise ValueError("INVALID_BLOCK_DURATION")
        if start < _instant(revision.source.source_as_of):
            raise ValueError("BLOCK_BEFORE_SOURCE_AS_OF")
        if start < week_start or end > week_end or start >= week_end:
            raise ValueError("BLOCK_OUTSIDE_WEEK")
        local_start, local_end = block.starts_at.astimezone(zone), block.ends_at.astimezone(zone)
        if local_start.date() != local_end.date():
            raise ValueError("BLOCK_CROSSES_MIDNIGHT")
        if not any(a <= start and end <= b for a, b in legal):
            raise ValueError("OUTSIDE_LEGAL_AVAILABILITY")
        if any(_overlap((start, end), interval) for interval, _ in personal):
            raise ValueError("PERSONAL_BLOCK_CONFLICT")
        if any(_overlap((start, end), interval) for interval, _ in academic):
            raise ValueError("ACADEMIC_BLOCK_CONFLICT")
        if any(_overlap((start, end), other) for other in intervals):
            raise ValueError("PLAN_BLOCK_OVERLAP")
        intervals.append((start, end))
        day = local_start.date()
        used[day] = used.get(day, 0) + block.duration_minutes
        if calendar.max_daily_study_minutes is not None and used[day] > calendar.max_daily_study_minutes:
            raise ValueError("DAILY_LIMIT_EXCEEDED")
        for constraint in block.deadline_constraints:
            if (constraint.kind == "TIMED" and end > _instant(constraint.due_at)) \
                    or (constraint.kind == "DATE_ONLY" and day >= constraint.due_date):
                raise ValueError("DEADLINE_VIOLATION")


def verify_single_edit(parent: WeeklyPlanRevisionV1, child: WeeklyPlanRevisionV1,
                       source_schedule: WeeklyScheduleV1) -> None:
    """Prove the child is exactly one supported operation on its parent."""
    old_b = {b.action_id: b for b in parent.blocks}
    new_b = {b.action_id: b for b in child.blocks}
    old_u = {u.action_id: u for u in parent.unscheduled_actions}
    new_u = {u.action_id: u for u in child.unscheduled_actions}
    removed = set(old_b) - set(new_b)
    placed = set(new_b) - set(old_b)
    candidates = []
    args = (parent, child.revision_id, child.revision_number, child.created_at)
    if len(removed) == 1 and not placed:
        candidates.append(lambda: transform_remove_block(*args, old_b[next(iter(removed))].block_id))
    elif len(placed) == 1 and not removed:
        action = next(iter(placed))
        block = new_b[action]
        supplied = block.duration_minutes if block.duration_source == "MANUAL_EDIT" else None
        candidates.append(lambda: transform_place_action(parent, source_schedule, child.revision_id,
            child.revision_number, child.created_at, action, block.starts_at, supplied))
    elif not removed and not placed:
        changes = [a for a in old_b if old_b[a].model_dump(exclude={"block_id"}) !=
            new_b[a].model_dump(exclude={"block_id"})]
        if len(changes) == 1:
            action = changes[0]
            old, new = old_b[action], new_b[action]
            if _instant(old.starts_at) != _instant(new.starts_at):
                candidates.append(lambda: transform_move_block(*args, old.block_id, new.starts_at))
            if old.duration_minutes != new.duration_minutes:
                candidates.append(lambda: transform_set_block_duration(*args, old.block_id, new.duration_minutes))
    for make in candidates:
        try:
            if canonical_weekly_plan_revision_json(make()) == canonical_weekly_plan_revision_json(child):
                return
        except (ValueError, OverflowError):
            pass
    raise ValueError("SOURCE_ARTIFACT_INVALID")
