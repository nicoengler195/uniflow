"""Phase 4A1 offline read contracts over temporary databases only."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text

import uniflow.application.application_read_models as read
from uniflow.application.initializer import initialize_project
from uniflow.application.planning import _plan_next_actions_in_transaction, plan_next_actions
from uniflow.application.planning_calendar import (
    add_availability_window, add_calendar_constraint, create_planning_profile,
)
from uniflow.application.weekly_block_materialization import materialize_weekly_plan_block
from uniflow.application.weekly_plans import (
    accept_weekly_plan_revision, create_weekly_plan, read_weekly_plan_revision,
    set_weekly_plan_block_duration,
)
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Deadline, LearningSession, LearningSessionTargetSkill,
    MasteryState, Material, MaterialArtifact, ParseRun, Semester, SemesterAudit,
    ScheduleEntry, Skill, SourceArtifact, WeeklyPlanBlockExecution,
)
from uniflow.schemas.application_read_v1 import (
    HomeDashboardV1, MasteryCoverageV1, TodayViewV1, canonical_home_json,
    canonical_today_json,
)
from uniflow.schemas.planning_v1 import canonical_planning_json

ZONE = ZoneInfo("Europe/Berlin")
MONDAY = date(2026, 10, 19)
BEFORE = datetime(2026, 10, 19, 8, tzinfo=ZONE)
AT = datetime(2026, 10, 19, 9, 30, tzinfo=ZONE)


@pytest.fixture
def sparse(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
                            timezone="Europe/Berlin")
        db.add(semester)
        db.flush()
        db.add_all([Course(stable_id="A", name="Alpha", semester_id=semester.id),
                    Course(stable_id="B", name="Beta", semester_id=semester.id),
                    SemesterAudit(semester_id=semester.id, schema_version="1.0",
                                  audit_id="SYNTH_AUDIT", revision_number=1)])
    yield config, factory
    factory.kw["bind"].dispose()


def _counts(factory):
    names = ("semester_audits", "materials", "source_artifacts", "parse_runs",
        "skills", "course_skills", "mastery_states", "learning_sessions",
        "learning_handoffs", "learning_results", "evidence", "weekly_plans",
        "weekly_plan_revisions", "weekly_plan_acceptances",
        "weekly_plan_block_executions", "planning_profiles",
        "availability_windows", "calendar_constraints")
    with factory() as db:
        return {name: db.execute(text("SELECT count(*) FROM " + name)).scalar_one()
                for name in names}


def test_sparse_real_like_state_is_valid_deterministic_and_read_only(sparse):
    config, factory = sparse
    before = _counts(factory)
    home = read.get_home_dashboard(config, "SYNTH", as_of=AT)
    today = read.get_today_view(config, "SYNTH", as_of=AT)
    assert isinstance(home, HomeDashboardV1) and isinstance(today, TodayViewV1)
    assert home.setup_status.audit_present
    assert [x.code for x in home.setup_status.diagnostics] == [
        "NO_REVIEWED_SKILLS", "NO_PLANNING_PROFILE", "NO_AVAILABILITY",
        "NO_WEEKLY_PLAN", "NO_ACCEPTED_WEEKLY_PLAN"]
    assert home.planner_status == "NO_ELIGIBLE_ACTIONS"
    assert home.next_planner_recommendation is None and home.next_accepted_block is None
    assert all(c.mastery_coverage.mean_known_mastery is None and
               c.mastery_coverage.known_coverage_ratio is None for c in home.courses)
    assert today.blocks == [] and [x.code for x in today.diagnostics] == ["NO_ACCEPTED_WEEKLY_PLAN"]
    assert home.local_date == today.local_date == MONDAY
    assert home.week_start == today.week_start == MONDAY
    assert canonical_home_json(home) == canonical_home_json(read.get_home_dashboard(config, "SYNTH", as_of=AT))
    assert canonical_today_json(today) == canonical_today_json(read.get_today_view(config, "SYNTH", as_of=AT))
    assert before == _counts(factory)
    assert b'"schema_version":"1.0"' in canonical_home_json(home)
    assert b'"policy_version":"today-view-v1"' in canonical_today_json(today)


def test_home_today_keeps_started_events_and_local_deadlines(sparse):
    config, factory = sparse
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "A"))
        for stable, start, end in (("EARLY", "08:00", "09:00"),
                                   ("LATE", "11:00", None)):
            payload = dict(stable_id=stable, activity_type="LECTURE", weekday="MONDAY",
                start_time=start, end_time=end, participation="REQUIRED",
                information_status="CONFIRMED")
            db.add(ScheduleEntry(stable_id=stable, course_id=course.id,
                payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))
        db.add(Deadline(stable_id="TODAY", course_id=course.id, title="Today due",
            due_at=datetime(2026, 10, 19), is_date_only=True,
            status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED))
    before = _counts(factory)
    morning = read.get_home_dashboard(config, "SYNTH", as_of=BEFORE)
    afternoon = read.get_home_dashboard(config, "SYNTH", as_of=AT)
    assert [e.schedule_entry_stable_id for e in morning.today_academic_occurrences] == ["EARLY", "LATE"]
    assert [e.schedule_entry_stable_id for e in afternoon.today_academic_occurrences] == ["EARLY", "LATE"]
    assert afternoon.today_academic_occurrences[1].ends_at is None
    assert afternoon.deadlines.due_fact_diagnostics[0].due_date == MONDAY
    assert afternoon.deadlines.due_fact_diagnostics[0].status == "UNKNOWN"
    assert before == _counts(factory)


def test_aware_time_semester_local_midnight_and_dst(sparse):
    config, _ = sparse
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        read.get_home_dashboard(config, "SYNTH", as_of=datetime(2026, 10, 19, 9))
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        read.get_today_view(config, "SYNTH", as_of=datetime(2026, 10, 19, 9))
    before_midnight = read.get_today_view(config, "SYNTH", as_of=datetime(
        2026, 10, 18, 21, 59, tzinfo=timezone.utc))
    after_midnight = read.get_today_view(config, "SYNTH", as_of=datetime(
        2026, 10, 18, 22, tzinfo=timezone.utc))
    assert before_midnight.local_date == date(2026, 10, 18)
    assert before_midnight.week_start == date(2026, 10, 12)
    assert after_midnight.local_date == MONDAY
    spring = read.get_today_view(config, "SYNTH", as_of=datetime(2026, 3, 29, 1, tzinfo=timezone.utc))
    fall = read.get_today_view(config, "SYNTH", as_of=datetime(2026, 10, 25, 1, tzinfo=timezone.utc))
    assert spring.as_of.utcoffset() == timedelta(hours=2)
    assert fall.as_of.utcoffset() == timedelta(hours=1) and fall.as_of.fold == 1
    assert spring.local_date == date(2026, 3, 29) and fall.local_date == date(2026, 10, 25)


def test_mastery_coverage_exact_course_and_latest_order(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        b = db.scalar(select(Course).where(Course.stable_id == "B"))
        for sid in ("ZERO", "KNOWN", "UNKNOWN", "MERGED"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add_all([CourseSkill(course_id=a.id, skill_id="ZERO"),
                    CourseSkill(course_id=a.id, skill_id="KNOWN"),
                    CourseSkill(course_id=a.id, skill_id="UNKNOWN"),
                    CourseSkill(course_id=b.id, skill_id="KNOWN"),
                    CourseSkill(course_id=b.id, skill_id="MERGED", is_active=False)])
        db.add_all([MasteryState(id="first", skill_id="ZERO", value=.8,
                    calculation_method="test", calculated_at=datetime(2026, 10, 18)),
                    MasteryState(id="second", skill_id="ZERO", value=0.0,
                    calculation_method="test", calculated_at=datetime(2026, 10, 19)),
                    MasteryState(id="tie-a", skill_id="KNOWN", value=.2,
                    calculation_method="test", calculated_at=datetime(2026, 10, 19)),
                    MasteryState(id="tie-z", skill_id="KNOWN", value=.6,
                    calculation_method="test", calculated_at=datetime(2026, 10, 19))])
    cards = {x.course_stable_id: x for x in read.get_home_dashboard(config, "SYNTH", as_of=AT).courses}
    assert cards["A"].mastery_coverage == MasteryCoverageV1(active_skill_count=3,
        known_mastery_count=2, unknown_mastery_count=1,
        known_coverage_ratio=2/3, mean_known_mastery=.3)
    assert cards["B"].mastery_coverage == MasteryCoverageV1(active_skill_count=1,
        known_mastery_count=1, unknown_mastery_count=0,
        known_coverage_ratio=1, mean_known_mastery=.6)
    assert cards["A"].mastery_coverage.mean_known_mastery != .2


def test_deadline_authority_limit_and_course_scope(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        b = db.scalar(select(Course).where(Course.stable_id == "B"))
        db.add_all([
            Deadline(stable_id="A1", course_id=a.id, title="Due one",
                due_at=datetime(2026, 10, 20), is_date_only=True,
                status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="A2", course_id=a.id, title="Due two",
                due_at=datetime(2026, 10, 21, 10), is_date_only=False,
                status=DeadlineStatus.OPEN, information_status=InformationStatus.OBSERVED),
            Deadline(stable_id="A3", course_id=a.id, title="Old",
                due_at=datetime(2026, 10, 18), is_date_only=True,
                status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="A4", course_id=a.id, title="Unknown state",
                due_at=datetime(2026, 10, 20), is_date_only=True,
                status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="A5", course_id=a.id, title="Inferred",
                due_at=datetime(2026, 10, 20), is_date_only=True,
                status=DeadlineStatus.OPEN, information_status=InformationStatus.INFERRED),
            Deadline(stable_id="A6", course_id=a.id, title="No date",
                status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="A7", course_id=a.id, title="Date-only unknown",
                is_date_only=True, status=DeadlineStatus.OPEN,
                information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="B1", course_id=b.id, title="Beta due",
                due_at=datetime(2026, 10, 20), is_date_only=True,
                status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED)])
    home = read.get_home_dashboard(config, "SYNTH", as_of=AT, deadline_limit=1)
    assert home.deadlines.authoritative_upcoming_count == 3
    assert home.deadlines.authoritative_overdue_count == 1
    assert home.deadlines.known_due_other_status_count == 1
    assert home.deadlines.nonauthoritative_or_undated_count == 3
    assert len(home.deadlines.authoritative_upcoming) == 1
    assert home.deadlines.authoritative_upcoming[0].deadline_stable_id == "A1"
    assert home.deadlines.authoritative_overdue[0].deadline_stable_id == "A3"
    assert home.deadlines.due_fact_diagnostics[0].timing != "UPCOMING_OPEN"
    cards = {x.course_stable_id: x for x in home.courses}
    assert cards["A"].upcoming_authoritative_deadline_count == 2
    assert cards["B"].upcoming_authoritative_deadline_count == 1
    with pytest.raises(ValueError, match="INVALID_DEADLINE_LIMIT"):
        read.get_home_dashboard(config, "SYNTH", as_of=AT, deadline_limit=0)


def test_transaction_aware_planner_matches_public_and_one_connection(sparse, monkeypatch):
    config, factory = sparse
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "A"))
        db.add(Skill(id="SK", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SK"))
    public = plan_next_actions(config, "SYNTH", as_of=AT, limit=1)
    with factory() as db:
        nested = _plan_next_actions_in_transaction(db, config, "SYNTH", as_of=AT, limit=1)
    assert canonical_planning_json(public) == canonical_planning_json(nested)
    with read._read_transaction(config) as db:
        raw = db.connection().connection.driver_connection
        assert raw.in_transaction
        assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    original = read.get_engine
    opened = []
    def counted(path):
        engine = original(path)
        event.listen(engine, "connect", lambda *_: opened.append(1))
        return engine
    monkeypatch.setattr(read, "get_engine", counted)
    read.get_home_dashboard(config, "SYNTH", as_of=AT)
    assert len(opened) == 1
    opened.clear()
    read.get_today_view(config, "SYNTH", as_of=AT)
    assert len(opened) == 1


def test_plan_authority_materialization_and_nba_remain_separate(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        b = db.scalar(select(Course).where(Course.stable_id == "B"))
        db.add_all([Skill(id="A_SK", name="A skill", kind="CONCEPT", status="ACTIVE"),
                    Skill(id="B_SK", name="B skill", kind="CONCEPT", status="ACTIVE")])
        db.flush()
        db.add_all([CourseSkill(course_id=a.id, skill_id="A_SK"),
                    CourseSkill(course_id=b.id, skill_id="B_SK")])
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY",
        start_local_time=time(9), end_local_time=time(13))
    draft = create_weekly_plan(config, "SYNTH", MONDAY, as_of=BEFORE)
    body = read_weekly_plan_revision(config, draft["plan_id"])
    assert len(body.blocks) == 2
    without_acceptance = read.get_today_view(config, "SYNTH", as_of=AT)
    assert without_acceptance.blocks == [] and without_acceptance.plan.accepted_revision_id is None
    accepted = accept_weekly_plan_revision(config, draft["plan_id"],
        accepted_at=datetime(2026, 10, 19, 8, 30, tzinfo=ZONE))
    assert accepted["status"] == "ACCEPTED"
    active = read.get_today_view(config, "SYNTH", as_of=AT)
    assert [x.block_id for x in active.blocks] == [x.block_id for x in body.blocks]
    assert all(x.materialization.status == "NOT_MATERIALIZED" for x in active.blocks)
    first = body.blocks[0]
    execution = materialize_weekly_plan_block(config, draft["plan_id"], first.block_id,
        expected_accepted_revision_id=draft["revision_id"], goal="Study A",
        materialized_at=AT)
    after = read.get_today_view(config, "SYNTH", as_of=AT)
    assert after.blocks[0].materialization.status == "MATERIALIZED"
    assert after.blocks[0].materialization.execution_kind == "CREATED_SESSION"
    assert after.blocks[0].learning_session_id == execution["learning_session_id"]
    assert after.blocks[0].learning_session_state == "PLANNED"
    assert after.blocks[0].goal == "Study A"
    edited = set_weekly_plan_block_duration(config, draft["plan_id"],
        body.blocks[1].block_id, 30, expected_revision_id=draft["revision_id"])
    assert edited["revision_id"] != draft["revision_id"]
    home = read.get_home_dashboard(config, "SYNTH", as_of=AT)
    today = read.get_today_view(config, "SYNTH", as_of=AT)
    assert home.plan.accepted_revision_id == draft["revision_id"]
    assert home.plan.current_revision_id == edited["revision_id"]
    assert home.plan.accepted_is_current is False
    assert [x.block_id for x in today.blocks] == [x.block_id for x in body.blocks]
    assert home.next_accepted_block.block_id == first.block_id
    assert home.next_planner_recommendation is not None
    assert home.next_planner_recommendation.action_id != first.action_id
    assert home.courses[0].accepted_planned_block_count == 1
    assert home.sessions.resumable_count == 1
    before_read = _counts(factory)
    read.get_home_dashboard(config, "SYNTH", as_of=AT)
    read.get_today_view(config, "SYNTH", as_of=AT)
    assert before_read == _counts(factory)
    later = read.get_home_dashboard(config, "SYNTH", as_of=datetime(
        2026, 10, 19, 10, 30, tzinfo=ZONE))
    assert later.next_accepted_block.block_id == body.blocks[1].block_id
    elapsed = read.get_home_dashboard(config, "SYNTH", as_of=datetime(
        2026, 10, 19, 12, tzinfo=ZONE))
    assert elapsed.next_accepted_block is None
    with factory.begin() as db:
        row = db.scalar(select(WeeklyPlanBlockExecution).where(
            WeeklyPlanBlockExecution.block_id == first.block_id))
        row.action_id = "tampered"
    invalid = read.get_today_view(config, "SYNTH", as_of=AT).blocks[0]
    assert invalid.materialization is None
    assert invalid.learning_session_state is None
    assert [x.code for x in invalid.diagnostics] == ["MATERIALIZATION_PROVENANCE_INVALID"]


def test_today_filters_blocks_by_semester_local_start_date(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        for sid in ("SK1", "SK2"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        for sid in ("SK1", "SK2"):
            db.add(CourseSkill(course_id=a.id, skill_id=sid))
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60,
        max_daily_study_minutes=60)
    for weekday in ("MONDAY", "TUESDAY"):
        add_availability_window(config, "SYNTH", weekday=weekday,
            start_local_time=time(9), end_local_time=time(11))
    plan = create_weekly_plan(config, "SYNTH", MONDAY, as_of=BEFORE)
    body = read_weekly_plan_revision(config, plan["plan_id"])
    assert [b.starts_at.astimezone(ZONE).date() for b in body.blocks] == [
        MONDAY, MONDAY + timedelta(days=1)]
    accept_weekly_plan_revision(config, plan["plan_id"], accepted_at=datetime(
        2026, 10, 19, 8, 30, tzinfo=ZONE))
    monday = read.get_today_view(config, "SYNTH", as_of=AT)
    tuesday = read.get_today_view(config, "SYNTH", as_of=datetime(
        2026, 10, 20, 9, 30, tzinfo=ZONE))
    assert [b.block_id for b in monday.blocks] == [body.blocks[0].block_id]
    assert [b.block_id for b in tuesday.blocks] == [body.blocks[1].block_id]
    assert monday.blocks[0].target_skill_ids == body.blocks[0].target_skill_ids


def test_calendar_constraints_and_session_state_not_block_state(sparse):
    config, factory = sparse
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "A"))
        db.add(Skill(id="SK", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SK"))
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY",
        start_local_time=time(9), end_local_time=time(12))
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 19, 11, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 12, tzinfo=ZONE))
    add_calendar_constraint(config, "SYNTH", constraint_type="AVAILABLE_ADDITION",
        starts_at=datetime(2026, 10, 19, 13, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 14, tzinfo=ZONE))
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "A"))
        for stable_id, participation in (("HARD", "REQUIRED"), ("OPTIONAL", "OPTIONAL")):
            db.add(ScheduleEntry(stable_id=stable_id, course_id=course.id,
                information_status=InformationStatus.CONFIRMED,
                payload_json=json.dumps({"stable_id": stable_id, "activity_type": "LECTURE",
                    "weekday": "MONDAY", "start_time": "08:00", "end_time": "09:00",
                    "participation": participation, "information_status": "CONFIRMED"})))
    today = read.get_today_view(config, "SYNTH", as_of=AT)
    assert len(today.availability_occurrences) == 1
    assert {x.constraint_type for x in today.constraint_occurrences} == {"BLOCKED", "AVAILABLE_ADDITION"}
    assert [x.code for x in today.calendar_diagnostics] == []
    assert {x.schedule_entry_stable_id: x.hard_block for x in today.academic_occurrences} == {
        "HARD": True, "OPTIONAL": False}


def test_material_counts_current_versions_and_parse_success_only(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        b = db.scalar(select(Course).where(Course.stable_id == "B"))
        ma = Material(stable_id="MA", title="Alpha notes", course_id=a.id)
        mb = Material(stable_id="MB", title="Beta notes", course_id=b.id)
        db.add_all([ma, mb])
        db.flush()
        for sid in ("old", "current", "beta"):
            db.add(SourceArtifact(id=sid, sha256=sid.ljust(64, "0"),
                original_filename=sid + ".txt", original_source_path=sid,
                size_bytes=1, stored_path=sid))
        db.flush()
        db.add_all([MaterialArtifact(material_id=ma.id, artifact_id="old",
                    version_number=1, is_current=False),
                    MaterialArtifact(material_id=ma.id, artifact_id="current",
                    version_number=2, is_current=True),
                    MaterialArtifact(material_id=mb.id, artifact_id="beta",
                    version_number=1, is_current=True)])
        for sid, status in (("old", "SUCCESS"), ("current", "SUCCESS"), ("beta", "FAILED")):
            db.add(ParseRun(source_artifact_id=sid, parser_name="TEXT", parser_version="1",
                dependency_version="stdlib", config_json="{}", config_sha256="0"*64,
                attempt_number=1, status=status, started_at=datetime(2026, 10, 18),
                completed_at=datetime(2026, 10, 18)))
    cards = {x.course_stable_id: x for x in read.get_home_dashboard(config, "SYNTH", as_of=AT).courses}
    assert cards["A"].materials.model_dump() == {
        "logical_material_count": 1, "current_artifact_count": 1,
        "successfully_parsed_current_artifact_count": 1}
    assert cards["B"].materials.model_dump() == {
        "logical_material_count": 1, "current_artifact_count": 1,
        "successfully_parsed_current_artifact_count": 0}


def test_resume_materialization_and_processing_states_remain_separate(sparse):
    config, factory = sparse
    with factory.begin() as db:
        a = db.scalar(select(Course).where(Course.stable_id == "A"))
        db.add(Skill(id="SK", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=a.id, skill_id="SK"))
        db.add(LearningSession(id="RESUME", course_id=a.id, goal="Continue",
            state=LearningSessionState.PLANNED))
        db.flush()
        db.add(LearningSessionTargetSkill(learning_session_id="RESUME", skill_id="SK", ordinal=0))
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY",
        start_local_time=time(9), end_local_time=time(12))
    plan = create_weekly_plan(config, "SYNTH", MONDAY, as_of=BEFORE)
    body = read_weekly_plan_revision(config, plan["plan_id"])
    assert body.blocks[0].action_type == "RESUME_SESSION"
    assert accept_weekly_plan_revision(config, plan["plan_id"],
        accepted_at=datetime(2026, 10, 19, 8, 30, tzinfo=ZONE))["status"] == "ACCEPTED"
    unmaterialized = read.get_today_view(config, "SYNTH", as_of=AT).blocks[0]
    assert unmaterialized.materialization.status == "NOT_MATERIALIZED"
    assert unmaterialized.existing_session_id == "RESUME"
    assert unmaterialized.learning_session_id == "RESUME"
    assert unmaterialized.learning_session_state == "PLANNED"
    materialize_weekly_plan_block(config, plan["plan_id"], body.blocks[0].block_id,
        expected_accepted_revision_id=plan["revision_id"], materialized_at=AT)
    for state in (LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
                  LearningSessionState.IN_PROGRESS, LearningSessionState.VALIDATED,
                  LearningSessionState.COMPLETED):
        with factory.begin() as db:
            db.get(LearningSession, "RESUME").state = state
        today = read.get_today_view(config, "SYNTH", as_of=AT)
        block = today.blocks[0]
        assert block.materialization.status == "MATERIALIZED"
        assert block.materialization.execution_kind == "RESUMED_SESSION"
        assert block.learning_session_state == state.value
        assert block.goal == "Continue"
        home = read.get_home_dashboard(config, "SYNTH", as_of=AT)
        assert home.sessions.resumable_count == (1 if state in read.RESUMABLE else 0)
        assert home.sessions.processing_count == (1 if state in read.PROCESSING else 0)
        assert home.sessions.completed_count == (1 if state == LearningSessionState.COMPLETED else 0)


def test_schema_rejects_unknown_fields_and_nonfinite_mastery():
    with pytest.raises(ValidationError):
        MasteryCoverageV1(active_skill_count=0, known_mastery_count=0,
            unknown_mastery_count=0, extra="no")
    with pytest.raises(ValidationError):
        MasteryCoverageV1(active_skill_count=1, known_mastery_count=1,
            unknown_mastery_count=0, known_coverage_ratio=1, mean_known_mastery=float("nan"))
