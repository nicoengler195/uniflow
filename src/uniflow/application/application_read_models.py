"""Phase 4A1 read-only Home and Today projections over canonical state."""
from __future__ import annotations

import math
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application.planning import _plan_next_actions_in_transaction
from uniflow.application.planning_calendar import _project_calendar_week_in_transaction
from uniflow.application.weekly_block_materialization import _verified_execution
from uniflow.application.weekly_plans import _load_verified
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config.settings import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, AvailabilityWindow, Course, CourseSkill, Deadline, LearningSession,
    LearningSessionTargetSkill, MasteryState, Material, MaterialArtifact, ParseRun,
    PlanningProfile, Semester, SemesterAudit, Skill, SkillProposal, MaterialReleaseRule,
    SkillProposalEvidence, SemanticEvidenceSpan, ClassificationProposal, WeeklyPlan,
    WeeklyPlanBlockExecution, WeeklyPlanRevision,
)
from uniflow.schemas.application_read_v1 import (
    CourseCardV1, DeadlineItemV1, DeadlineSummaryV1, HomeDashboardV1,
    MasteryCoverageV1, MaterialFactsV1, MaterializationV1, PlanIdentityV1,
    PlannedBlockSummaryV1, ReadDiagnosticV1, SemesterSummaryV1,
    SessionInboxSummaryV1, SessionItemV1, SetupStatusV1, TodayBlockV1,
    TodayViewV1,
)

RESUMABLE = {LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
             LearningSessionState.IN_PROGRESS}
PROCESSING = {LearningSessionState.RESULT_RECEIVED, LearningSessionState.VALIDATED,
              LearningSessionState.MASTERY_UPDATED}


@contextmanager
def _read_transaction(config: AppConfig):
    # get_engine creates a parent directory; a projection must not create one.
    if not Path(config.database_path).is_file():
        raise ValueError("DATABASE_NOT_FOUND")
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db, db.begin():
        connection = db.connection()
        connection.exec_driver_sql("PRAGMA query_only=ON")
        # sqlite3's legacy transaction mode does not BEGIN on SELECT. Start the
        # database snapshot explicitly before the first projection query.
        connection.exec_driver_sql("BEGIN")
        yield db


def _context(db, semester_id: str, as_of: datetime):
    if as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    semester = db.scalar(select(Semester).where(Semester.stable_id == semester_id))
    if semester is None:
        raise ValueError("UNKNOWN_SEMESTER")
    zone = ZoneInfo(semester.timezone)
    local = as_of.astimezone(zone)
    monday = local.date() - timedelta(days=local.weekday())
    summary = SemesterSummaryV1(id=semester.id, stable_id=semester.stable_id,
        name=semester.name, state=semester.state.value, timezone=semester.timezone)
    return semester, zone, local, monday, summary


def _courses(db, semester: Semester) -> list[Course]:
    return list(db.scalars(select(Course).where(Course.semester_id == semester.id)
        .order_by(Course.stable_id, Course.id)))


def _plan(db, config: AppConfig, semester: Semester, monday: date):
    root = db.scalar(select(WeeklyPlan).where(WeeklyPlan.semester_id == semester.id,
        WeeklyPlan.week_start == monday))
    if root is None:
        return PlanIdentityV1(plan_id=None, current_revision_id=None,
            current_revision_number=None, accepted_revision_id=None,
            accepted_revision_number=None, accepted_is_current=None), None, None
    revisions = list(db.scalars(select(WeeklyPlanRevision).where(
        WeeklyPlanRevision.weekly_plan_id == root.id).order_by(
        WeeklyPlanRevision.revision_number, WeeklyPlanRevision.id)))
    if not revisions:
        raise ValueError("PLAN_REVISION_MISSING")
    current = revisions[-1]
    accepted = next((r for r in revisions if r.id == root.accepted_revision_id), None)
    if root.accepted_revision_id is not None and accepted is None:
        raise ValueError("ACCEPTANCE_HISTORY_INVALID")
    identity = PlanIdentityV1(plan_id=root.id, current_revision_id=current.id,
        current_revision_number=current.revision_number,
        accepted_revision_id=accepted.id if accepted else None,
        accepted_revision_number=accepted.revision_number if accepted else None,
        accepted_is_current=(accepted.id == current.id) if accepted else None)
    body = _load_verified(config, db, root, accepted)[3] if accepted else None
    return identity, body, (root, accepted) if accepted else None


def _active_course_skills(db, courses: list[Course]):
    ids = [c.id for c in courses]
    if not ids:
        return {}
    rows = db.execute(select(CourseSkill.course_id, CourseSkill.skill_id).join(
        Skill, Skill.id == CourseSkill.skill_id).where(
        CourseSkill.course_id.in_(ids), CourseSkill.is_active.is_(True),
        Skill.status == "ACTIVE").order_by(CourseSkill.course_id, CourseSkill.skill_id)).all()
    result: dict[str, set[str]] = defaultdict(set)
    for course_id, skill_id in rows:
        result[course_id].add(skill_id)
    return result


def _mastery_coverage(db, course_skills: dict[str, set[str]]) -> dict[str, MasteryCoverageV1]:
    skill_ids = sorted(set().union(*course_skills.values())) if course_skills else []
    latest: dict[str, float] = {}
    if skill_ids:
        rows = db.scalars(select(MasteryState).where(MasteryState.skill_id.in_(skill_ids))
            .order_by(MasteryState.skill_id, MasteryState.calculated_at.desc(),
                      MasteryState.id.desc())).all()
        for row in rows:
            if row.skill_id in latest:
                continue
            if not math.isfinite(row.value) or not 0 <= row.value <= 1:
                raise ValueError("INVALID_MASTERY_STATE")
            latest[row.skill_id] = row.value
    result = {}
    for course_id, skills in course_skills.items():
        known_values = [latest[s] for s in sorted(skills) if s in latest]
        count = len(skills)
        known = len(known_values)
        result[course_id] = MasteryCoverageV1(active_skill_count=count,
            known_mastery_count=known, unknown_mastery_count=count-known,
            known_coverage_ratio=known/count if count else None,
            mean_known_mastery=sum(known_values)/known if known else None)
    return result


def _materials(db, courses: list[Course]) -> dict[str, MaterialFactsV1]:
    ids = [c.id for c in courses]
    materials = list(db.scalars(select(Material).where(Material.course_id.in_(ids)))) if ids else []
    material_course = {m.id: m.course_id for m in materials}
    links = list(db.scalars(select(MaterialArtifact).where(
        MaterialArtifact.material_id.in_(material_course),
        MaterialArtifact.is_current.is_(True),
        MaterialArtifact.deactivated_at.is_(None)))) if materials else []
    artifact_ids = {r.artifact_id for r in links}
    parsed = set(db.scalars(select(ParseRun.source_artifact_id).where(
        ParseRun.source_artifact_id.in_(artifact_ids), ParseRun.status == "SUCCESS"))) if artifact_ids else set()
    material_counts = defaultdict(int)
    artifact_counts = defaultdict(int)
    parsed_counts = defaultdict(int)
    for material in materials:
        material_counts[material.course_id] += 1
    for link in links:
        cid = material_course[link.material_id]
        artifact_counts[cid] += 1
        if link.artifact_id in parsed:
            parsed_counts[cid] += 1
    return {c.id: MaterialFactsV1(logical_material_count=material_counts[c.id],
        current_artifact_count=artifact_counts[c.id],
        successfully_parsed_current_artifact_count=parsed_counts[c.id]) for c in courses}


def _deadline_data(db, courses: list[Course], zone: ZoneInfo, local: datetime,
                   deadline_limit: int):
    course_ids = {c.id for c in courses}
    assessment_course = dict(db.execute(select(Assessment.id, Assessment.course_id).where(
        Assessment.course_id.in_(course_ids))).all()) if course_ids else {}
    material_course = dict(db.execute(select(Material.id, Material.course_id).where(
        Material.course_id.in_(course_ids))).all()) if course_ids else {}
    rows = list(db.scalars(select(Deadline)))
    grouped = {"UPCOMING_OPEN": [], "OVERDUE_OPEN": [], "KNOWN_DUE_OTHER_STATUS": [],
               "NONAUTHORITATIVE": [], "UNDATED": []}
    course_upcoming = defaultdict(int)
    for row in rows:
        course_id = (row.course_id if row.course_id in course_ids else
                     assessment_course.get(row.assessment_id) or material_course.get(row.material_id))
        if course_id is None:
            continue
        due_date = row.due_at.date() if row.is_date_only and row.due_at else None
        due_at = (row.due_at.replace(tzinfo=zone) if row.due_at.tzinfo is None else
                  row.due_at.astimezone(zone)) if row.due_at and not row.is_date_only else None
        authoritative = row.information_status in {InformationStatus.CONFIRMED, InformationStatus.OBSERVED}
        if row.due_at is None:
            timing = "UNDATED"
        elif not authoritative:
            timing = "NONAUTHORITATIVE"
        elif row.status != DeadlineStatus.OPEN:
            timing = "KNOWN_DUE_OTHER_STATUS"
        elif (due_date >= local.date() if row.is_date_only else due_at >= local):
            timing = "UPCOMING_OPEN"
            course_upcoming[course_id] += 1
        else:
            timing = "OVERDUE_OPEN"
        grouped[timing].append(DeadlineItemV1(deadline_id=row.id,
            deadline_stable_id=row.stable_id, course_id=course_id, title=row.title,
            status=row.status.value, information_status=row.information_status.value,
            is_date_only=row.is_date_only, due_date=due_date, due_at=due_at,
            timing=timing))
    def sort_key(item):
        due_day = item.due_date or (item.due_at.date() if item.due_at else date.max)
        due_clock = item.due_at.timetz().replace(tzinfo=None) if item.due_at else time.min
        return due_day, due_clock, item.deadline_stable_id or "", item.deadline_id
    for items in grouped.values():
        items.sort(key=sort_key)
    diagnostics = sorted([*grouped["KNOWN_DUE_OTHER_STATUS"],
        *grouped["NONAUTHORITATIVE"], *grouped["UNDATED"]], key=sort_key)
    return DeadlineSummaryV1(
        authoritative_upcoming_count=len(grouped["UPCOMING_OPEN"]),
        authoritative_overdue_count=len(grouped["OVERDUE_OPEN"]),
        known_due_other_status_count=len(grouped["KNOWN_DUE_OTHER_STATUS"]),
        nonauthoritative_or_undated_count=len(grouped["NONAUTHORITATIVE"])+len(grouped["UNDATED"]),
        authoritative_upcoming=grouped["UPCOMING_OPEN"][:deadline_limit],
        authoritative_overdue=grouped["OVERDUE_OPEN"][:deadline_limit],
        due_fact_diagnostics=diagnostics[:deadline_limit]), course_upcoming


def _sessions(db, courses: list[Course], session_limit: int):
    by_id = {c.id: c for c in courses}
    rows = list(db.scalars(select(LearningSession).where(
        LearningSession.course_id.in_(by_id)).order_by(
        LearningSession.created_at, LearningSession.id))) if by_id else []
    targets: dict[str, list[str]] = defaultdict(list)
    if rows:
        target_rows = db.execute(select(LearningSessionTargetSkill.learning_session_id,
            LearningSessionTargetSkill.skill_id).where(
            LearningSessionTargetSkill.learning_session_id.in_([r.id for r in rows]))
            .order_by(LearningSessionTargetSkill.learning_session_id,
                      LearningSessionTargetSkill.ordinal, LearningSessionTargetSkill.id)).all()
        for session_id, skill_id in target_rows:
            targets[session_id].append(skill_id)
    resumable = processing = completed = 0
    per_course_resumable = defaultdict(int)
    per_course_processing = defaultdict(int)
    items = []
    for row in rows:
        if row.state in RESUMABLE:
            resumable += 1
            per_course_resumable[row.course_id] += 1
        elif row.state in PROCESSING:
            processing += 1
            per_course_processing[row.course_id] += 1
        elif row.state == LearningSessionState.COMPLETED:
            completed += 1
        else:
            raise ValueError("INVALID_SESSION_STATE")
        if row.state in RESUMABLE | PROCESSING:
            items.append(SessionItemV1(session_id=row.id, course_id=row.course_id,
                course_stable_id=by_id[row.course_id].stable_id, goal=row.goal,
                state=row.state.value,
                planned_local_wall_time=row.planned_at.replace(tzinfo=None).isoformat() if row.planned_at else None,
                target_skill_ids=targets[row.id], current_handoff_id=row.current_handoff_id))
    summary = SessionInboxSummaryV1(resumable_count=resumable,
        processing_count=processing, completed_count=completed,
        resumable_and_processing=items[:session_limit])
    return summary, per_course_resumable, per_course_processing


def _setup(db, semester: Semester, plan: PlanIdentityV1,
           active_course_skill_count: int) -> SetupStatusV1:
    audit_present = db.scalar(select(SemesterAudit.id).where(
        SemesterAudit.semester_id == semester.id).limit(1)) is not None
    profile = db.scalar(select(PlanningProfile).where(
        PlanningProfile.semester_id == semester.id))
    availability_count = len(list(db.scalars(select(AvailabilityWindow.id).where(
        AvailabilityWindow.planning_profile_id == profile.id,
        AvailabilityWindow.deactivated_revision.is_(None))))) if profile else 0
    diagnostics = []
    if not audit_present:
        diagnostics.append(ReadDiagnosticV1(code="NO_SEMESTER_AUDIT"))
    if active_course_skill_count == 0:
        diagnostics.append(ReadDiagnosticV1(code="NO_REVIEWED_SKILLS"))
    if profile is None:
        diagnostics.append(ReadDiagnosticV1(code="NO_PLANNING_PROFILE"))
    if availability_count == 0:
        diagnostics.append(ReadDiagnosticV1(code="NO_AVAILABILITY"))
    if plan.plan_id is None:
        diagnostics.append(ReadDiagnosticV1(code="NO_WEEKLY_PLAN"))
    if plan.accepted_revision_id is None:
        diagnostics.append(ReadDiagnosticV1(code="NO_ACCEPTED_WEEKLY_PLAN"))
    return SetupStatusV1(semester_loaded=True, audit_present=audit_present,
        planning_profile_present=profile is not None,
        active_availability_window_count=availability_count,
        active_course_skill_count=active_course_skill_count,
        reviewed_skill_available=active_course_skill_count > 0,
        weekly_plan_present=plan.plan_id is not None,
        accepted_weekly_plan_present=plan.accepted_revision_id is not None,
        diagnostics=diagnostics)


def get_home_dashboard(config: AppConfig, semester_id: str, *, as_of: datetime,
                       deadline_limit: int = 10, session_limit: int = 10,
                       include_academic: bool = True) -> HomeDashboardV1:
    if as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if type(deadline_limit) is not int or not 1 <= deadline_limit <= 100:
        raise ValueError("INVALID_DEADLINE_LIMIT")
    if type(session_limit) is not int or not 1 <= session_limit <= 100:
        raise ValueError("INVALID_SESSION_LIMIT")
    with _read_transaction(config) as db:
        semester, zone, local, monday, semester_summary = _context(db, semester_id, as_of)
        courses = _courses(db, semester)
        release_counts = defaultdict(int)
        for course_id in db.scalars(select(MaterialReleaseRule.course_id).where(
                MaterialReleaseRule.course_id.in_([course.id for course in courses]))):
            release_counts[course_id] += 1
        plan, accepted_body, _ = _plan(db, config, semester, monday)
        course_skills = _active_course_skills(db, courses)
        coverage = _mastery_coverage(db, course_skills)
        materials = _materials(db, courses)
        deadlines, course_upcoming = _deadline_data(db, courses, zone, local, deadline_limit)
        sessions, course_resumable, course_processing = _sessions(db, courses, session_limit)
        setup = _setup(db, semester, plan, sum(len(x) for x in course_skills.values()))
        accepted_blocks = accepted_body.blocks if accepted_body else []
        block_counts = defaultdict(int)
        for block in accepted_blocks:
            block_counts[block.course_id] += 1
        candidates = [b for b in accepted_blocks if b.ends_at.astimezone(timezone.utc) > as_of.astimezone(timezone.utc)]
        next_block = min(candidates, key=lambda b: (b.starts_at.astimezone(timezone.utc),
            b.ends_at.astimezone(timezone.utc), b.nba_rank, b.block_id)) if candidates else None
        next_summary = PlannedBlockSummaryV1(block_id=next_block.block_id,
            action_id=next_block.action_id, course_id=next_block.course_id,
            course_stable_id=next_block.course_stable_id, starts_at=next_block.starts_at,
            ends_at=next_block.ends_at) if next_block else None
        planner_status = "UNSUPPORTED_SEMESTER_STATE"
        planner_diagnostics = [ReadDiagnosticV1(code="UNSUPPORTED_SEMESTER_STATE")]
        recommendation = None
        if semester.state == SemesterState.ACTIVE:
            try:
                planner = _plan_next_actions_in_transaction(db, config, semester_id, as_of=as_of, limit=1)
            except ValidationError as exc:
                if exc.errors() and all("target_skill_ids" in error["loc"] and
                    error["type"] == "too_long" for error in exc.errors()):
                    planner_status = "PLANNER_COMPATIBILITY_DIAGNOSTIC"
                    planner_diagnostics = [ReadDiagnosticV1(code="SESSION_TARGET_LIMIT_EXCEEDED")]
                else:
                    raise
            else:
                planner_status = planner.status
                planner_diagnostics = planner.diagnostics
                recommendation = planner.recommendations[0] if planner.recommendations else None
        cards = [CourseCardV1(course_id=c.id, course_stable_id=c.stable_id,
            name=c.name, parent_course_id=c.parent_course_id,
            release_rule_count=release_counts[c.id],
            mastery_coverage=coverage.get(c.id, MasteryCoverageV1(
                active_skill_count=0, known_mastery_count=0, unknown_mastery_count=0)),
            materials=materials[c.id],
            upcoming_authoritative_deadline_count=course_upcoming[c.id],
            resumable_session_count=course_resumable[c.id],
            processing_session_count=course_processing[c.id],
            accepted_planned_block_count=block_counts[c.id]) for c in courses]
        upcoming = []
        for start in ((monday, monday + timedelta(days=7)) if include_academic else ()):
            upcoming.extend(_project_calendar_week_in_transaction(db, semester_id, start).academic_occurrences)
        today_occurrences = sorted((event for event in upcoming if event.local_date == local.date()),
            key=lambda event: (event.starts_at or datetime.min.replace(tzinfo=timezone.utc),
                event.occurrence_id))
        upcoming = [event for event in upcoming if event.starts_at is None or
            event.starts_at.astimezone(timezone.utc) >= as_of.astimezone(timezone.utc)]
        upcoming.sort(key=lambda event: (event.local_date,
            event.starts_at or datetime.min.replace(tzinfo=timezone.utc), event.occurrence_id))
        course_ids = [course.id for course in courses]
        pending_reviews = 0
        if course_ids:
            skill_ids = set(db.scalars(select(SkillProposal.id).where(
                SkillProposal.status == "PENDING", SkillProposal.course_id.in_(course_ids))))
            skill_ids.update(db.scalars(select(SkillProposal.id).join(SkillProposalEvidence,
                SkillProposalEvidence.proposal_id == SkillProposal.id).join(SemanticEvidenceSpan,
                SemanticEvidenceSpan.id == SkillProposalEvidence.evidence_span_id).join(
                MaterialArtifact, MaterialArtifact.id == SemanticEvidenceSpan.material_artifact_id).join(
                Material, Material.id == MaterialArtifact.material_id).where(
                SkillProposal.status == "PENDING", Material.course_id.in_(course_ids))))
            classification_ids = set(db.scalars(select(ClassificationProposal.id).where(
                ClassificationProposal.status == "PENDING",
                ClassificationProposal.proposed_course_id.in_(course_ids))))
            classification_ids.update(db.scalars(select(ClassificationProposal.id).join(Material,
                Material.id == ClassificationProposal.subject_material_id).where(
                ClassificationProposal.status == "PENDING", Material.course_id.in_(course_ids))))
            pending_reviews = len(skill_ids) + len(classification_ids)
        return HomeDashboardV1(as_of=local, local_date=local.date(), week_start=monday,
            semester=semester_summary, setup_status=setup, plan=plan,
            next_accepted_block=next_summary, next_planner_recommendation=recommendation,
            planner_status=planner_status, planner_diagnostics=planner_diagnostics,
            deadlines=deadlines, sessions=sessions, courses=cards,
            today_academic_occurrences=today_occurrences,
            upcoming_academic_occurrences=upcoming[:10], pending_review_count=pending_reviews)


def get_today_view(config: AppConfig, semester_id: str, *, as_of: datetime) -> TodayViewV1:
    if as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    with _read_transaction(config) as db:
        semester, zone, local, monday, semester_summary = _context(db, semester_id, as_of)
        plan, accepted_body, accepted_refs = _plan(db, config, semester, monday)
        calendar = _project_calendar_week_in_transaction(db, semester_id, monday)
        today = local.date()
        chosen = [b for b in accepted_body.blocks if b.starts_at.astimezone(zone).date() == today] if accepted_body else []
        chosen.sort(key=lambda b: (b.starts_at.astimezone(timezone.utc),
            b.ends_at.astimezone(timezone.utc), b.nba_rank, b.block_id))
        execution_by_block = {}
        if chosen:
            executions = db.scalars(select(WeeklyPlanBlockExecution).where(
                WeeklyPlanBlockExecution.weekly_plan_revision_id == plan.accepted_revision_id)).all()
            execution_by_block = {e.block_id: e for e in executions}
        blocks = []
        for block in chosen:
            execution = execution_by_block.get(block.block_id)
            diagnostics = []
            materialization = MaterializationV1(status="NOT_MATERIALIZED")
            session_id = block.existing_session_id
            session_state = goal = None
            if session_id is not None:
                existing = db.get(LearningSession, session_id)
                if existing is None:
                    diagnostics.append(ReadDiagnosticV1(code="PLANNED_SESSION_MISSING",
                        refs=[session_id]))
                else:
                    session_state, goal = existing.state.value, existing.goal
            if execution is not None:
                try:
                    verified = _verified_execution(config, db, execution,
                        root=accepted_refs[0], revision=accepted_refs[1], block=block)
                except ValueError as exc:
                    if str(exc) != "MATERIALIZATION_PROVENANCE_INVALID":
                        raise
                    materialization = None
                    session_id = session_state = goal = None
                    diagnostics.append(ReadDiagnosticV1(code="MATERIALIZATION_PROVENANCE_INVALID",
                        refs=[execution.id]))
                else:
                    materialization = MaterializationV1(status="MATERIALIZED",
                        execution_kind=verified["execution_kind"],
                        execution_id=verified["execution_id"],
                        materialized_at=datetime.fromisoformat(verified["materialized_at"]))
                    session_id = verified["learning_session_id"]
                    session = db.get(LearningSession, session_id)
                    session_state, goal = session.state.value, session.goal
            blocks.append(TodayBlockV1(block_id=block.block_id, action_id=block.action_id,
                action_type=block.action_type, nba_rank=block.nba_rank,
                course_id=block.course_id, course_stable_id=block.course_stable_id,
                target_skill_ids=list(block.target_skill_ids),
                existing_session_id=block.existing_session_id, starts_at=block.starts_at,
                ends_at=block.ends_at, duration_minutes=block.duration_minutes,
                deadline_context=list(block.deadline_constraints), materialization=materialization,
                learning_session_id=session_id, learning_session_state=session_state,
                goal=goal, diagnostics=diagnostics))
        day_start = datetime.combine(today, time.min, zone).astimezone(timezone.utc)
        day_end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        def overlaps(start, end):
            return start.astimezone(timezone.utc) < day_end and end.astimezone(timezone.utc) > day_start
        return TodayViewV1(as_of=local, local_date=today, week_start=monday,
            semester=semester_summary, plan=plan, blocks=blocks,
            academic_occurrences=[x for x in calendar.academic_occurrences if x.local_date == today],
            availability_occurrences=[x for x in calendar.availability_occurrences if x.local_date == today],
            constraint_occurrences=[x for x in calendar.constraint_occurrences if overlaps(x.starts_at, x.ends_at)],
            calendar_diagnostics=calendar.diagnostics,
            diagnostics=[] if plan.accepted_revision_id else [ReadDiagnosticV1(code="NO_ACCEPTED_WEEKLY_PLAN")])
