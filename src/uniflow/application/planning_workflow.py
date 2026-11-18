"""GUI-independent planning mutation facade; services own locks and transactions."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from uniflow.application import weekly_plans, weekly_block_materialization
from uniflow.application.workflow_errors import WorkflowCommandError, normalize_known, parse_request
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import WeeklyPlanRevision
from uniflow.schemas.planning_workflow_v1 import (
    AcceptPlanRequestV1, AcceptPlanSuccessV1, CreatePlanRequestV1, MaterializeBlockRequestV1,
    EditRevisionSuccessV1, GeneratedRevisionSuccessV1, MaterializeBlockSuccessV1,
    MoveBlockRequestV1, PlaceUnscheduledRequestV1, RegeneratePlanRequestV1,
    RemoveBlockRequestV1, SetBlockDurationRequestV1,
)

CAPTURE_ERRORS = frozenset({
    "INVALID_ACTION_DURATION", "INVALID_WEEK_START", "AS_OF_MUST_BE_AWARE", "INVALID_LIMIT",
    "UNKNOWN_SEMESTER", "UNSUPPORTED_SEMESTER_STATE", "UNKNOWN_COURSE_IN_SEMESTER",
    "INVALID_MASTERY_STATE", "SKILL_GRAPH_INVALID", "INVALID_SESSION_STATE",
    "UNKNOWN_DURATION_ACTION", "INVALID_DURATION_MAPPING", "INVALID_ACTIONABLE_DEADLINE",
    "INVALID_LOCAL_MIDNIGHT", "AMBIGUOUS_LOCAL_MIDNIGHT", "INVALID_AVAILABILITY_WINDOW",
    "SCHEDULE_EXCEPTION_CONFLICT", "NONEXISTENT_PERSONAL_LOCAL_TIME",
    "AMBIGUOUS_PERSONAL_LOCAL_TIME", "NONEXISTENT_WEEK_LOCAL_TIME",
    "AMBIGUOUS_WEEK_LOCAL_TIME", "NONEXISTENT_ACADEMIC_LOCAL_TIME",
    "AMBIGUOUS_ACADEMIC_LOCAL_TIME", "INVALID_FOLD", "SOURCE_CHANGED_DURING_CAPTURE",
    "SOURCE_ARTIFACT_INVALID", "ARTIFACT_KIND_CONFLICT", "UNSUPPORTED_NBA_CONTRACT",
    "UNSUPPORTED_CALENDAR_CONTRACT", "INVALID_SCHEDULER_SNAPSHOT",
    "SCHEDULER_SEMESTER_MISMATCH", "INVALID_CALENDAR_WEEK", "INVALID_PROFILE_SNAPSHOT",
    "INVALID_SCHEDULER_TIMEZONE", "INVALID_NBA_ACTIONS", "SCHEDULER_MIDNIGHT_INVARIANT",
})
CREATE_ERRORS = CAPTURE_ERRORS | {"PLAN_ALREADY_EXISTS"}
REGENERATE_ERRORS = CAPTURE_ERRORS | {"UNKNOWN_WEEKLY_PLAN", "PLAN_WITHOUT_REVISION",
    "STALE_DURATION_OVERRIDE", "PLAN_CHANGED_DURING_CAPTURE"}
EDIT_SHARED_ERRORS = frozenset({"UNKNOWN_WEEKLY_PLAN", "UNKNOWN_WEEKLY_PLAN_REVISION",
    "REVISION_CHANGED", "SOURCE_ARTIFACT_INVALID", "ARTIFACT_KIND_CONFLICT",
    "INVALID_BLOCK_START", "INVALID_BLOCK_DURATION", "BLOCK_BEFORE_SOURCE_AS_OF",
    "BLOCK_OUTSIDE_WEEK", "BLOCK_CROSSES_MIDNIGHT", "OUTSIDE_LEGAL_AVAILABILITY",
    "PERSONAL_BLOCK_CONFLICT", "ACADEMIC_BLOCK_CONFLICT", "PLAN_BLOCK_OVERLAP",
    "DAILY_LIMIT_EXCEEDED", "DEADLINE_VIOLATION", "INVALID_LOCAL_MIDNIGHT",
    "AMBIGUOUS_LOCAL_MIDNIGHT"})
MOVE_ERRORS = EDIT_SHARED_ERRORS | {"UNKNOWN_PLAN_BLOCK"}
SET_DURATION_ERRORS = EDIT_SHARED_ERRORS | {"UNKNOWN_PLAN_BLOCK"}
REMOVE_ERRORS = EDIT_SHARED_ERRORS | {"UNKNOWN_PLAN_BLOCK"}
PLACE_ERRORS = EDIT_SHARED_ERRORS | {"ACTION_ALREADY_SCHEDULED", "UNKNOWN_UNSCHEDULED_ACTION"}
ACCEPT_ERRORS = frozenset({"ACCEPTED_AT_MUST_BE_AWARE", "UNKNOWN_WEEKLY_PLAN",
    "UNKNOWN_WEEKLY_PLAN_REVISION", "SOURCE_ARTIFACT_INVALID", "ACCEPTANCE_HISTORY_INVALID",
    "REVISION_NOT_CURRENT"})
MATERIALIZE_ERRORS = frozenset({"MATERIALIZED_AT_MUST_BE_AWARE", "UNKNOWN_WEEKLY_PLAN",
    "UNKNOWN_WEEKLY_PLAN_REVISION", "UNKNOWN_PLAN_BLOCK", "PLAN_NOT_ACCEPTED",
    "ACCEPTED_REVISION_CHANGED", "ACCEPTANCE_HISTORY_INVALID", "WEEK_ELAPSED",
    "BLOCK_COURSE_INVALID", "INVALID_SEMESTER_STATE", "CURRENT_NO_PLANNING_PROFILE",
    "CURRENT_CALENDAR_INVALID", "CURRENT_OUTSIDE_LEGAL_AVAILABILITY",
    "CURRENT_PERSONAL_BLOCK_CONFLICT", "CURRENT_ACADEMIC_BLOCK_CONFLICT",
    "CURRENT_DAILY_LIMIT_EXCEEDED", "CURRENT_DEADLINE_CONFLICT",
    "MATERIALIZATION_PROVENANCE_INVALID", "ACTIVE_SESSION_CONFLICT",
    "INVALID_SESSION_GOAL", "INVALID_TARGET_SKILL", "HANDOFF_TOO_LARGE",
    "DUPLICATE_TARGET_SKILL", "TARGET_COURSESKILL_REQUIRED", "INVALID_SESSION_COURSE",
    "GOAL_NOT_ALLOWED_FOR_RESUME", "SESSION_IDENTITY_INVALID", "SESSION_NOT_RESUMABLE",
    "SOURCE_ARTIFACT_INVALID", "UNKNOWN_SEMESTER", "INVALID_WEEK_START",
    "INVALID_AVAILABILITY_WINDOW", "SCHEDULE_EXCEPTION_CONFLICT",
    "NONEXISTENT_PERSONAL_LOCAL_TIME", "AMBIGUOUS_PERSONAL_LOCAL_TIME",
    "NONEXISTENT_WEEK_LOCAL_TIME", "AMBIGUOUS_WEEK_LOCAL_TIME",
    "NONEXISTENT_ACADEMIC_LOCAL_TIME", "AMBIGUOUS_ACADEMIC_LOCAL_TIME", "INVALID_FOLD"})


def _delegate(operation, errors):
    try:
        return operation()
    except ValueError as exc:
        normalize_known(exc, errors)


def _generated_revision(command, outcome, result):
    return GeneratedRevisionSuccessV1(command=command, outcome=outcome, **result)


def _edit_revision(command, result):
    return EditRevisionSuccessV1(command=command, **result)


def create_plan(config, request: CreatePlanRequestV1 | dict) -> GeneratedRevisionSuccessV1:
    request = parse_request(CreatePlanRequestV1, request)
    result = _delegate(lambda: weekly_plans.create_weekly_plan(config, request.semester_id,
        request.week_start, as_of=request.as_of, course_id=request.course_id, limit=request.limit,
        durations=request.duration_overrides), CREATE_ERRORS)
    return _generated_revision("planning.plan.create", "CREATED", result)


def regenerate_plan(config, request: RegeneratePlanRequestV1 | dict) -> GeneratedRevisionSuccessV1:
    request = parse_request(RegeneratePlanRequestV1, request)
    course = (weekly_plans.INHERIT if request.course_filter_mode == "INHERIT" else
              None if request.course_filter_mode == "CLEAR" else request.course_id)
    durations = (weekly_plans.INHERIT if request.duration_overrides_mode == "INHERIT" else
                 {} if request.duration_overrides_mode == "CLEAR" else request.duration_overrides)
    result = _delegate(lambda: weekly_plans.regenerate_weekly_plan(config, request.plan_id,
        as_of=request.as_of, course_id=course, limit=request.limit, durations=durations),
        REGENERATE_ERRORS)
    return _generated_revision("planning.plan.regenerate", "REGENERATED", result)


def move_block(config, request: MoveBlockRequestV1 | dict) -> EditRevisionSuccessV1:
    request = parse_request(MoveBlockRequestV1, request)
    result = _delegate(lambda: weekly_plans.move_weekly_plan_block(config, request.plan_id,
        request.block_id, request.new_start, expected_revision_id=request.expected_revision_id), MOVE_ERRORS)
    return _edit_revision("planning.block.move", result)


def set_block_duration(config, request: SetBlockDurationRequestV1 | dict) -> EditRevisionSuccessV1:
    request = parse_request(SetBlockDurationRequestV1, request)
    result = _delegate(lambda: weekly_plans.set_weekly_plan_block_duration(config, request.plan_id,
        request.block_id, request.minutes, expected_revision_id=request.expected_revision_id),
        SET_DURATION_ERRORS)
    return _edit_revision("planning.block.set_duration", result)


def remove_block(config, request: RemoveBlockRequestV1 | dict) -> EditRevisionSuccessV1:
    request = parse_request(RemoveBlockRequestV1, request)
    result = _delegate(lambda: weekly_plans.remove_weekly_plan_block(config, request.plan_id,
        request.block_id, expected_revision_id=request.expected_revision_id), REMOVE_ERRORS)
    return _edit_revision("planning.block.remove", result)


def place_unscheduled(config, request: PlaceUnscheduledRequestV1 | dict) -> EditRevisionSuccessV1:
    request = parse_request(PlaceUnscheduledRequestV1, request)
    result = _delegate(lambda: weekly_plans.place_weekly_plan_action(config, request.plan_id,
        request.action_id, request.start, duration_minutes=request.duration_minutes,
        expected_revision_id=request.expected_revision_id), PLACE_ERRORS)
    return _edit_revision("planning.unscheduled.place", result)


def accept_plan(config, request: AcceptPlanRequestV1 | dict) -> AcceptPlanSuccessV1:
    request = parse_request(AcceptPlanRequestV1, request)
    with create_session_factory(get_engine(config.database_path))() as db:
        row = db.get(WeeklyPlanRevision, request.revision_id)
        if row is None or row.weekly_plan_id != request.plan_id:
            raise WorkflowCommandError("UNKNOWN_WEEKLY_PLAN_REVISION")
        revision_number = row.revision_number
    accepted_at = request.accepted_at.astimezone(timezone.utc) if request.accepted_at else None
    result = _delegate(lambda: weekly_plans.accept_weekly_plan_revision(config, request.plan_id,
        revision_number=revision_number, accepted_at=accepted_at), ACCEPT_ERRORS)
    if result["status"] in {"STALE", "INVALID"}:
        factors = result["assessment"]["factors"]
        safe = sorted({x for x in factors if isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", x)})
        refs = tuple(value for value in (request.plan_id, request.revision_id)
                     if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value))
        raise WorkflowCommandError("PLAN_ACCEPTANCE_" + result["status"],
            refs=refs,
            details={"factors": safe[:12], "truncated": len(safe) > 12})
    returned_at = datetime.fromisoformat(result["accepted_at"])
    if returned_at.utcoffset() is None:
        returned_at = returned_at.replace(tzinfo=timezone.utc)
    return AcceptPlanSuccessV1(outcome=result["status"], plan_id=result["plan_id"],
        revision_id=result["revision_id"], acceptance_id=result["acceptance_id"],
        accepted_at=returned_at)


def materialize_block(config, request: MaterializeBlockRequestV1 | dict) -> MaterializeBlockSuccessV1:
    request = parse_request(MaterializeBlockRequestV1, request)
    result = _delegate(lambda: weekly_block_materialization.materialize_weekly_plan_block(
        config, request.plan_id, request.block_id,
        expected_accepted_revision_id=request.expected_accepted_revision_id,
        goal=request.goal, materialized_at=request.materialized_at), MATERIALIZE_ERRORS)
    return MaterializeBlockSuccessV1(outcome=result["status"],
        **{**{key: value for key, value in result.items() if key != "status"},
           "materialized_at": datetime.fromisoformat(result["materialized_at"])})
