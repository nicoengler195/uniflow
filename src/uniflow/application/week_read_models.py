"""One-snapshot Week view over verified revisions and current Calendar facts."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _courses, _deadline_data, _read_transaction
from uniflow.application.planning_calendar import _project_calendar_week_in_transaction
from uniflow.application.weekly_block_materialization import (
    _verified_execution, assess_current_calendar_block,
)
from uniflow.application.weekly_plans import _assess, _load_verified
from uniflow.infrastructure.database.models import (
    LearningSession, WeeklyPlan, WeeklyPlanAcceptance, WeeklyPlanBlockExecution,
    WeeklyPlanRevision,
)
from uniflow.schemas.application_read_v1 import MaterializationV1, PlanIdentityV1, ReadDiagnosticV1
from uniflow.schemas.week_read_v1 import (
    PlanBlockReadV1, RevisionHistoryItemV1, SourceCalendarIdentityV1, WeekRevisionReadV1, WeekViewV1,
)


def _aware_utc(value: datetime) -> datetime:
    return value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


def _revision(db, config, root, row, as_of, calendar, *, accepted: bool,
              course_filter_id: str | None = None):
    assessment = _assess(config, db, root, row, as_of)
    acceptance = db.scalar(select(WeeklyPlanAcceptance).where(
        WeeklyPlanAcceptance.weekly_plan_revision_id == row.id)) if accepted else None
    if accepted and (acceptance is None or acceptance.weekly_plan_id != root.id):
        raise ValueError("ACCEPTANCE_HISTORY_INVALID")
    acceptance_fields = dict(acceptance_id=acceptance.id if acceptance else None,
        accepted_at=_aware_utc(acceptance.accepted_at) if acceptance else None,
        acceptance_actor=acceptance.actor if acceptance else None)
    if assessment.status == "INVALID":
        return WeekRevisionReadV1(revision_id=row.id, revision_number=row.revision_number,
            revision_kind=row.revision_kind, parent_revision_id=row.parent_revision_id,
            created_at=_aware_utc(row.created_at), source_calendar_identity=None,
            blocks=None, unscheduled_actions=None, revision_assessment=assessment,
            **acceptance_fields)
    body = _load_verified(config, db, root, row)[3]
    executions = {e.block_id: e for e in db.scalars(select(WeeklyPlanBlockExecution).where(
        WeeklyPlanBlockExecution.weekly_plan_revision_id == row.id))} if accepted else {}
    blocks = []
    for block in body.blocks:
        if course_filter_id is not None and block.course_id != course_filter_id:
            continue
        diagnostics = []
        materialization = MaterializationV1(status="NOT_MATERIALIZED") if accepted else None
        session_id = block.existing_session_id if accepted else None
        session_state = goal = None
        if accepted:
            for code in assess_current_calendar_block(body, block, calendar):
                diagnostics.append(ReadDiagnosticV1(code=code))
            if session_id:
                session = db.get(LearningSession, session_id)
                if session is None:
                    diagnostics.append(ReadDiagnosticV1(code="PLANNED_SESSION_MISSING", refs=[session_id]))
                else:
                    session_state, goal = session.state.value, session.goal
            execution = executions.get(block.block_id)
            if execution is not None:
                try:
                    verified = _verified_execution(config, db, execution,
                        root=root, revision=row, block=block)
                except ValueError as exc:
                    if str(exc) != "MATERIALIZATION_PROVENANCE_INVALID":
                        raise
                    materialization = None
                    session_id = session_state = goal = None
                    diagnostics.append(ReadDiagnosticV1(code="MATERIALIZATION_PROVENANCE_INVALID",
                        refs=[execution.id]))
                else:
                    materialization = MaterializationV1(status="MATERIALIZED",
                        execution_kind=verified["execution_kind"], execution_id=execution.id,
                        materialized_at=datetime.fromisoformat(verified["materialized_at"]))
                    session_id = verified["learning_session_id"]
                    session = db.get(LearningSession, session_id)
                    session_state, goal = session.state.value, session.goal
        blocks.append(PlanBlockReadV1(block_id=block.block_id,
            source_scheduler_block_id=block.source_scheduler_block_id,
            action_id=block.action_id, nba_rank=block.nba_rank, action_type=block.action_type,
            course_id=block.course_id, course_stable_id=block.course_stable_id,
            target_skill_ids=list(block.target_skill_ids), existing_session_id=block.existing_session_id,
            starts_at=block.starts_at, ends_at=block.ends_at, duration_minutes=block.duration_minutes,
            placement_source=block.placement_source, duration_source=block.duration_source,
            deadline_context=list(block.deadline_constraints), materialization=materialization,
            learning_session_id=session_id, learning_session_state=session_state, goal=goal,
            diagnostics=diagnostics))
    return WeekRevisionReadV1(revision_id=row.id, revision_number=row.revision_number,
        revision_kind=row.revision_kind, parent_revision_id=row.parent_revision_id,
        created_at=body.created_at,
        source_calendar_identity=SourceCalendarIdentityV1(artifact_id=body.source.calendar_artifact_id,
            payload_sha256=body.source.calendar_payload_sha256,
            context_fingerprint=body.source.calendar_context_fingerprint),
        blocks=blocks, unscheduled_actions=list(body.unscheduled_actions),
        revision_assessment=assessment, **acceptance_fields)


def get_week_view(config, semester_id: str, week_start: date, *, as_of: datetime) -> WeekViewV1:
    if type(week_start) is not date or week_start.weekday() != 0:
        raise ValueError("INVALID_WEEK_START")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    with _read_transaction(config) as db:
        semester, zone, local, _, summary = _context(db, semester_id, as_of)
        courses = _courses(db, semester)
        course_names = {course.stable_id: course.name for course in courses}
        calendar = _project_calendar_week_in_transaction(db, semester_id, week_start)
        deadline_summary, _ = _deadline_data(db, courses, zone, local, 100)
        deadlines = [item for item in [*deadline_summary.authoritative_upcoming,
            *deadline_summary.authoritative_overdue, *deadline_summary.due_fact_diagnostics]
            if week_start <= (item.due_date or (item.due_at.date() if item.due_at else date.max))
                < week_start + timedelta(days=7)]
        root = db.scalar(select(WeeklyPlan).where(WeeklyPlan.semester_id == semester.id,
            WeeklyPlan.week_start == week_start))
        empty = PlanIdentityV1(plan_id=None, current_revision_id=None,
            current_revision_number=None, accepted_revision_id=None,
            accepted_revision_number=None, accepted_is_current=None)
        if root is None:
            return WeekViewV1(as_of=local, semester=summary, week_start=week_start,
                week_end_exclusive=week_start + timedelta(days=7), plan=empty,
                accepted_plan=None, current_draft=None, current_calendar=calendar,
                diagnostics=[ReadDiagnosticV1(code="NO_WEEKLY_PLAN")], deadline_items=deadlines,
                course_names=course_names)
        revisions = list(db.scalars(select(WeeklyPlanRevision).where(
            WeeklyPlanRevision.weekly_plan_id == root.id).order_by(
            WeeklyPlanRevision.revision_number, WeeklyPlanRevision.id)))
        if not revisions:
            raise ValueError("PLAN_REVISION_MISSING")
        current = revisions[-1]
        history = []
        invalid_source = False
        for row in revisions:
            try:
                body = _load_verified(config, db, root, row)[3]
            except ValueError as exc:
                if str(exc) != "SOURCE_ARTIFACT_INVALID":
                    raise
                body = None
                invalid_source = True
            history.append(RevisionHistoryItemV1(revision_number=row.revision_number,
                revision_kind=row.revision_kind, accepted=row.id == root.accepted_revision_id,
                current=row.id == current.id, block_count=len(body.blocks) if body else None,
                unscheduled_count=len(body.unscheduled_actions) if body else None))
        accepted = next((r for r in revisions if r.id == root.accepted_revision_id), None)
        if root.accepted_revision_id and accepted is None:
            raise ValueError("ACCEPTANCE_HISTORY_INVALID")
        identity = PlanIdentityV1(plan_id=root.id, current_revision_id=current.id,
            current_revision_number=current.revision_number,
            accepted_revision_id=accepted.id if accepted else None,
            accepted_revision_number=accepted.revision_number if accepted else None,
            accepted_is_current=(accepted.id == current.id) if accepted else None)
        return WeekViewV1(as_of=local, semester=summary, week_start=week_start,
            week_end_exclusive=week_start + timedelta(days=7), plan=identity,
            accepted_plan=_revision(db, config, root, accepted, as_of, calendar, accepted=True) if accepted else None,
            current_draft=_revision(db, config, root, current, as_of, calendar, accepted=False)
                if not accepted or accepted.id != current.id else None,
            current_calendar=calendar, diagnostics=([ReadDiagnosticV1(code="WEEKLY_PLAN_SOURCE_INVALID")]
                if invalid_source else []) + ([] if accepted else [
                ReadDiagnosticV1(code="NO_ACCEPTED_WEEKLY_PLAN")]), deadline_items=deadlines,
            revision_history=history, course_names=course_names)
