"""Explicit, atomic materialization of an accepted weekly block."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from uniflow.application.learning_handoffs import _create_learning_session_in_transaction
from uniflow.application.planning import _deadlines, _source
from uniflow.application.planning_calendar import _project_calendar_week_in_transaction
from uniflow.application.weekly_plan_editing import _overlap
from uniflow.application.weekly_plans import _db, _load_verified
from uniflow.application.weekly_scheduler import _instant, _local_midnight, _union
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.database.models import (
    Course, Deadline, LearningSession, LearningSessionTargetSkill, Semester,
    WeeklyPlan, WeeklyPlanAcceptance, WeeklyPlanBlockExecution, WeeklyPlanRevision,
)
from uniflow.infrastructure.write_lock import project_write_lock

RESUMABLE = {LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
             LearningSessionState.IN_PROGRESS}


def _fail(code: str) -> None:
    raise ValueError(code)


def _targets(db, session_id: str) -> list[str]:
    return list(db.scalars(select(LearningSessionTargetSkill.skill_id).where(
        LearningSessionTargetSkill.learning_session_id == session_id).order_by(
        LearningSessionTargetSkill.ordinal, LearningSessionTargetSkill.id)))


def _verified_execution(config, db, execution: WeeklyPlanBlockExecution, *,
                        root=None, revision=None, block=None) -> dict:
    try:
        revision = revision or db.get(WeeklyPlanRevision, execution.weekly_plan_revision_id)
        if revision is None or revision.id != execution.weekly_plan_revision_id:
            _fail("INVALID")
        root = root or db.get(WeeklyPlan, revision.weekly_plan_id)
        if root is None or revision.weekly_plan_id != root.id:
            _fail("INVALID")
        body = _load_verified(config, db, root, revision)[3]
        block = next((b for b in body.blocks if b.block_id == execution.block_id), None)
        if block is None or block.action_id != execution.action_id:
            _fail("INVALID")
        kind = "CREATED_SESSION" if block.action_type == "START_SKILL_SESSION" else "RESUMED_SESSION"
        if execution.execution_kind != kind:
            _fail("INVALID")
        session = db.get(LearningSession, execution.learning_session_id)
        if session is None or session.course_id != block.course_id or _targets(db, session.id) != block.target_skill_ids:
            _fail("INVALID")
        if kind == "CREATED_SESSION":
            # SQLite retains the wall time but drops the offset/fold. The artifact is
            # the authority for the intended instant and ambiguous DST fold.
            if (block.existing_session_id is not None or session.planned_at is None or
                    session.planned_at.replace(tzinfo=None) != block.starts_at.replace(tzinfo=None)):
                _fail("INVALID")
        elif session.id != block.existing_session_id:
            _fail("INVALID")
        at = execution.materialized_at
        if at is None:
            _fail("INVALID")
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return {"execution_id": execution.id, "plan_id": root.id, "revision_id": revision.id,
                "block_id": block.block_id, "action_id": block.action_id,
                "execution_kind": kind, "learning_session_id": session.id,
                "materialized_at": at.astimezone(timezone.utc).isoformat(),
                "session_state": session.state.value}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("MATERIALIZATION_PROVENANCE_INVALID") from exc


def weekly_block_materialization_info(config, execution_id: str) -> dict:
    with _db(config) as db:
        row = db.get(WeeklyPlanBlockExecution, execution_id)
        if row is None:
            _fail("UNKNOWN_BLOCK_MATERIALIZATION")
        return _verified_execution(config, db, row)


def assess_current_calendar_block(body, block, calendar) -> tuple[str, ...]:
    """Pure advisory geometry shared with the materialization gate."""
    codes = []
    if calendar.planning_profile_id is None:
        return ("CURRENT_NO_PLANNING_PROFILE",)
    if (body.week_start != calendar.week_start or body.timezone != calendar.timezone or
            body.semester.id != calendar.semester_id):
        return ("CURRENT_CALENDAR_INVALID",)
    interval = (_instant(block.starts_at), _instant(block.ends_at))
    legal = _union([(_instant(x.starts_at), _instant(x.ends_at))
                    for x in calendar.availability_occurrences] +
                   [(_instant(x.starts_at), _instant(x.ends_at))
                    for x in calendar.constraint_occurrences if x.constraint_type == "AVAILABLE_ADDITION"])
    if not any(start <= interval[0] and interval[1] <= end for start, end in legal):
        codes.append("CURRENT_OUTSIDE_LEGAL_AVAILABILITY")
    if any(_overlap(interval, (_instant(x.starts_at), _instant(x.ends_at)))
           for x in calendar.constraint_occurrences if x.constraint_type == "BLOCKED"):
        codes.append("CURRENT_PERSONAL_BLOCK_CONFLICT")
    if any(_overlap(interval, (_instant(x.starts_at), _instant(x.ends_at)))
           for x in calendar.academic_occurrences if x.hard_block):
        codes.append("CURRENT_ACADEMIC_BLOCK_CONFLICT")
    cap = calendar.max_daily_study_minutes
    if cap is not None:
        zone = ZoneInfo(body.timezone)
        day = block.starts_at.astimezone(zone).date()
        total = sum(b.duration_minutes for b in body.blocks if b.starts_at.astimezone(zone).date() == day)
        if total > cap:
            codes.append("CURRENT_DAILY_LIMIT_EXCEEDED")
    return tuple(codes)


def _validate_calendar(body, block, calendar) -> None:
    codes = assess_current_calendar_block(body, block, calendar)
    if codes:
        _fail(codes[0])


def _validate_deadlines(db, body, block) -> None:
    zone = ZoneInfo(body.timezone)
    day = block.starts_at.astimezone(zone).date()
    end = _instant(block.ends_at)
    for captured in block.deadline_constraints:
        if ((captured.kind == "TIMED" and end > _instant(captured.due_at)) or
                (captured.kind == "DATE_ONLY" and day >= captured.due_date)):
            _fail("MATERIALIZATION_PROVENANCE_INVALID")
    material_ids: set[str] = set()
    for skill_id in block.target_skill_ids:
        material_ids.update(_source(db, block.course_id, skill_id).material_ids)
    # _deadlines is the planner's exact Course, Assessment, and reviewed Material
    # relevance implementation. Its timing classification is advisory here.
    relevant = {d.id for d in _deadlines(db, block.course_id, material_ids, zone, block.starts_at)}
    relevant.update(c.deadline_id for c in block.deadline_constraints)
    for deadline_id in relevant:
        row = db.get(Deadline, deadline_id)
        if (row is None or row.status != DeadlineStatus.OPEN or
                row.information_status not in {InformationStatus.CONFIRMED, InformationStatus.OBSERVED} or
                row.due_at is None):
            continue
        if row.is_date_only:
            if day >= row.due_at.date():
                _fail("CURRENT_DEADLINE_CONFLICT")
        elif end > _instant(row.due_at if row.due_at.tzinfo else row.due_at.replace(tzinfo=zone)):
            _fail("CURRENT_DEADLINE_CONFLICT")


def materialize_weekly_plan_block(config, plan_id: str, block_id: str, *,
                                  expected_accepted_revision_id: str, goal: str | None = None,
                                  materialized_at: datetime | None = None) -> dict:
    if materialized_at is not None and materialized_at.utcoffset() is None:
        _fail("MATERIALIZED_AT_MUST_BE_AWARE")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            root = db.get(WeeklyPlan, plan_id)
            if root is None:
                _fail("UNKNOWN_WEEKLY_PLAN")
            revision = db.get(WeeklyPlanRevision, expected_accepted_revision_id)
            if revision is None or revision.weekly_plan_id != root.id:
                _fail("UNKNOWN_WEEKLY_PLAN_REVISION")
            body = _load_verified(config, db, root, revision)[3]
            block = next((b for b in body.blocks if b.block_id == block_id), None)
            if block is None:
                _fail("UNKNOWN_PLAN_BLOCK")
            prior = db.scalar(select(WeeklyPlanBlockExecution).where(
                WeeklyPlanBlockExecution.weekly_plan_revision_id == revision.id,
                WeeklyPlanBlockExecution.block_id == block_id))
            if prior is not None:
                return {"status": "ALREADY_MATERIALIZED", **_verified_execution(config, db, prior,
                    root=root, revision=revision, block=block)}
            if root.accepted_revision_id is None:
                _fail("PLAN_NOT_ACCEPTED")
            if root.accepted_revision_id != revision.id:
                _fail("ACCEPTED_REVISION_CHANGED")
            acceptance = db.scalar(select(WeeklyPlanAcceptance).where(
                WeeklyPlanAcceptance.weekly_plan_revision_id == revision.id))
            if acceptance is None or acceptance.weekly_plan_id != root.id:
                _fail("ACCEPTANCE_HISTORY_INVALID")
            now = materialized_at or datetime.now(timezone.utc)
            zone = ZoneInfo(body.timezone)
            if _instant(now) >= _instant(_local_midnight(body.week_end_exclusive, zone)):
                _fail("WEEK_ELAPSED")
            semester = db.get(Semester, root.semester_id)
            course = db.get(Course, block.course_id)
            if (semester is None or course is None or course.semester_id != root.semester_id or
                    course.stable_id != block.course_stable_id):
                _fail("BLOCK_COURSE_INVALID")
            if semester.state in {SemesterState.COMPLETED, SemesterState.ARCHIVED}:
                _fail("INVALID_SEMESTER_STATE")
            calendar = _project_calendar_week_in_transaction(db, semester.stable_id, root.week_start)
            _validate_calendar(body, block, calendar)
            _validate_deadlines(db, body, block)
            if block.action_type == "START_SKILL_SESSION":
                if block.existing_session_id is not None:
                    _fail("MATERIALIZATION_PROVENANCE_INVALID")
                # Leave the manual session API's duplicate policy unchanged.
                for existing in db.scalars(select(LearningSession).where(
                        LearningSession.course_id == course.id,
                        LearningSession.state != LearningSessionState.COMPLETED)):
                    if set(_targets(db, existing.id)).intersection(block.target_skill_ids):
                        _fail("ACTIVE_SESSION_CONFLICT")
                session_id = _create_learning_session_in_transaction(db, config, course.id,
                    goal, list(block.target_skill_ids), block.starts_at)
                kind = "CREATED_SESSION"
            else:
                if goal is not None:
                    _fail("GOAL_NOT_ALLOWED_FOR_RESUME")
                session = db.get(LearningSession, block.existing_session_id) if block.existing_session_id else None
                if session is None or session.course_id != course.id or _targets(db, session.id) != block.target_skill_ids:
                    _fail("SESSION_IDENTITY_INVALID")
                if session.state not in RESUMABLE:
                    _fail("SESSION_NOT_RESUMABLE")
                session_id = session.id
                kind = "RESUMED_SESSION"
            execution = WeeklyPlanBlockExecution(id=str(uuid.uuid4()),
                weekly_plan_revision_id=revision.id, block_id=block.block_id,
                action_id=block.action_id, execution_kind=kind,
                learning_session_id=session_id,
                materialized_at=now.astimezone(timezone.utc).replace(tzinfo=None))
            db.add(execution)
            db.flush()
            return {"status": "MATERIALIZED", **_verified_execution(config, db, execution,
                root=root, revision=revision, block=block)}
