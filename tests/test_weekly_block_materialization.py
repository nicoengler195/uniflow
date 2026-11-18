"""Accepted-block materialization uses temporary projects only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
import json

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from alembic import command
from alembic.config import Config
from pathlib import Path
from typer.testing import CliRunner

from uniflow.application.initializer import initialize_project
from uniflow.application.learning_handoffs import (
    _create_learning_session_in_transaction, create_learning_session, generate_handoff,
)
from uniflow.application.learning_results import start_learning_session, import_learning_result
from uniflow.application.learning_result_mastery import apply_learning_result_mastery
from uniflow.application.planning_calendar import (
    add_availability_window, add_calendar_constraint, create_planning_profile,
    update_planning_profile,
)
from uniflow.application.weekly_plans import (
    accept_weekly_plan_revision, create_weekly_plan, read_weekly_plan_revision,
    regenerate_weekly_plan,
)
from uniflow.application.weekly_block_materialization import (
    materialize_weekly_plan_block, weekly_block_materialization_info,
)
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, Course, CourseSkill, Deadline, LearningSession, LearningSessionTargetSkill,
    Semester, Skill, WeeklyPlanBlockExecution, ScheduleEntry,
)
from uniflow.cli.main import app

ZONE = ZoneInfo("Europe/Berlin")
MON = date(2026, 10, 19)
EARLY = datetime(2026, 10, 19, 8, tzinfo=ZONE)
ACCEPT = datetime(2026, 10, 19, 8, 30, tzinfo=ZONE)
CLICK = datetime(2026, 10, 19, 10, 30, tzinfo=ZONE)


@pytest.fixture
def project(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        sem = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
                       timezone="Europe/Berlin")
        course = Course(stable_id="C", name="Course", semester=sem,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        for sid in ("A", "B"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        for sid in ("A", "B"):
            db.add(CourseSkill(course_id=course.id, skill_id=sid))
    create_planning_profile(cfg, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(cfg, "SYNTH", weekday="MONDAY",
                            start_local_time=time(9), end_local_time=time(13))
    result = create_weekly_plan(cfg, "SYNTH", MON, as_of=EARLY)
    body = read_weekly_plan_revision(cfg, result["plan_id"])
    assert len(body.blocks) == 2
    accept_weekly_plan_revision(cfg, result["plan_id"], accepted_at=ACCEPT)
    yield cfg, factory, result, body
    factory.kw["bind"].dispose()


def run(project, block_index=0, *, goal="Learn", when=CLICK, revision_id=None):
    cfg, _, result, body = project
    return materialize_weekly_plan_block(cfg, result["plan_id"], body.blocks[block_index].block_id,
        expected_accepted_revision_id=revision_id or result["revision_id"],
        goal=goal, materialized_at=when)


def test_start_retry_and_zero_automatic_pipeline(project):
    cfg, factory, result, body = project
    first = run(project)
    assert first["status"] == "MATERIALIZED"
    assert first["execution_kind"] == "CREATED_SESSION"
    assert first["session_state"] == "PLANNED"
    second = run(project, goal=None, when=datetime(2026, 10, 27, tzinfo=ZONE))
    assert second == {**first, "status": "ALREADY_MATERIALIZED"}
    assert weekly_block_materialization_info(cfg, first["execution_id"]) == {k: v for k, v in first.items() if k != "status"}
    with factory() as db:
        session = db.get(LearningSession, first["learning_session_id"])
        assert session.course_id == body.blocks[0].course_id
        assert session.goal == "Learn" and session.state == LearningSessionState.PLANNED
        assert session.planned_at == body.blocks[0].starts_at.replace(tzinfo=None)
        assert [x.skill_id for x in session.target_skills] == body.blocks[0].target_skill_ids
        for table, count in (("learning_sessions", 1), ("learning_session_target_skills", 1),
                             ("weekly_plan_block_executions", 1), ("learning_handoffs", 0),
                             ("learning_results", 0), ("evidence", 0), ("mastery_states", 0)):
            assert db.connection().exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == count


@pytest.mark.parametrize("goal", [None, " ", "x" * 4001])
def test_goal_validation_rolls_back(project, goal):
    with pytest.raises(ValueError, match="INVALID_SESSION_GOAL"):
        run(project, goal=goal)
    with project[1]() as db:
        assert db.scalar(select(WeeklyPlanBlockExecution)) is None
        assert db.scalar(select(LearningSession)) is None


def test_acceptance_token_and_time(project):
    cfg, factory, result, body = project
    with pytest.raises(ValueError, match="WEEK_ELAPSED"):
        run(project, when=datetime(2026, 10, 26, tzinfo=ZONE))
    with pytest.raises(ValueError, match="UNKNOWN_PLAN_BLOCK"):
        materialize_weekly_plan_block(cfg, result["plan_id"], "missing",
            expected_accepted_revision_id=result["revision_id"], goal="Learn", materialized_at=CLICK)
    draft = regenerate_weekly_plan(cfg, result["plan_id"], as_of=EARLY)
    assert draft["revision_id"] != result["revision_id"]
    assert run(project)["status"] == "MATERIALIZED"  # accepted older than current
    with pytest.raises(ValueError, match="ACCEPTED_REVISION_CHANGED"):
        materialize_weekly_plan_block(cfg, result["plan_id"],
            read_weekly_plan_revision(cfg, result["plan_id"], 2).blocks[1].block_id,
            expected_accepted_revision_id=draft["revision_id"], goal="Learn", materialized_at=CLICK)


def test_personal_calendar_conflict(project):
    cfg, factory, result, body = project
    block = body.blocks[0]
    add_calendar_constraint(cfg, "SYNTH", constraint_type="BLOCKED",
        starts_at=block.starts_at, ends_at=block.ends_at)
    with pytest.raises(ValueError, match="CURRENT_PERSONAL_BLOCK_CONFLICT"):
        run(project)


def test_deadline_drift_and_duplicate_unfinished(project):
    cfg, factory, result, body = project
    block = body.blocks[0]
    with factory.begin() as db:
        db.add(Deadline(stable_id="D", course_id=block.course_id, title="Due",
            due_at=datetime(2026, 10, 19, 9, 30), is_date_only=False,
            status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED))
    with pytest.raises(ValueError, match="CURRENT_DEADLINE_CONFLICT"):
        run(project)
    with factory.begin() as db:
        db.scalar(select(Deadline).where(Deadline.stable_id == "D")).due_at = datetime(2026, 10, 19, 13)
    duplicate = create_learning_session(cfg, block.course_id, "Manual", block.target_skill_ids)
    with pytest.raises(ValueError, match="ACTIVE_SESSION_CONFLICT"):
        run(project)
    with factory.begin() as db:
        db.get(LearningSession, duplicate).state = LearningSessionState.COMPLETED
    assert run(project)["status"] == "MATERIALIZED"


def test_concurrent_retry_and_primitive_rollback(project):
    cfg, factory, result, body = project
    with factory() as db:
        with pytest.raises(RuntimeError):
            with db.begin():
                _create_learning_session_in_transaction(db, cfg, body.blocks[0].course_id,
                    "Rolled back", body.blocks[0].target_skill_ids, body.blocks[0].starts_at)
                raise RuntimeError()
    with factory() as db:
        assert db.scalar(select(LearningSession)) is None
        assert db.scalar(select(LearningSessionTargetSkill)) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(project), range(2)))
    assert {r["status"] for r in results} == {"MATERIALIZED", "ALREADY_MATERIALIZED"}
    assert len({r["execution_id"] for r in results}) == 1
    with factory() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM learning_sessions").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM weekly_plan_block_executions").scalar_one() == 1


@pytest.mark.parametrize("state", [LearningSessionState.PLANNED,
    LearningSessionState.HANDOFF_CREATED, LearningSessionState.IN_PROGRESS,
    LearningSessionState.RESULT_RECEIVED, LearningSessionState.VALIDATED,
    LearningSessionState.MASTERY_UPDATED, LearningSessionState.COMPLETED])
def test_resume_states_and_goal(project, state):
    cfg, factory, result, body = project
    original = body.blocks[0]
    sid = create_learning_session(cfg, original.course_id, "Original",
        original.target_skill_ids, datetime(2026, 10, 18, 12, tzinfo=ZONE))
    with factory.begin() as db:
        db.get(LearningSession, sid).state = state
    if state not in {LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
                     LearningSessionState.IN_PROGRESS}:
        # NBA treats processing sessions as diagnostics, so create a resumable
        # revision first, then advance its session before first materialization.
        with factory.begin() as db:
            db.get(LearningSession, sid).state = LearningSessionState.PLANNED
    newer = regenerate_weekly_plan(cfg, result["plan_id"], as_of=EARLY)
    resume = next(b for b in read_weekly_plan_revision(cfg, result["plan_id"], 2).blocks
                  if b.action_type == "RESUME_SESSION")
    accept_weekly_plan_revision(cfg, result["plan_id"], 2, accepted_at=ACCEPT)
    with factory.begin() as db:
        db.get(LearningSession, sid).state = state
    call = lambda goal=None: materialize_weekly_plan_block(cfg, result["plan_id"], resume.block_id,
        expected_accepted_revision_id=newer["revision_id"], goal=goal, materialized_at=CLICK)
    with pytest.raises(ValueError, match="GOAL_NOT_ALLOWED_FOR_RESUME"):
        call("Surprise")
    if state in {LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
                 LearningSessionState.IN_PROGRESS}:
        first = call()
        assert first["execution_kind"] == "RESUMED_SESSION" and first["learning_session_id"] == sid
        with factory.begin() as db:
            db.get(LearningSession, sid).state = LearningSessionState.COMPLETED
        assert call()["status"] == "ALREADY_MATERIALIZED"
        with factory() as db:
            session = db.get(LearningSession, sid)
            assert session.goal == "Original"
            assert session.planned_at == datetime(2026, 10, 18, 12)
    else:
        with pytest.raises(ValueError, match="SESSION_NOT_RESUMABLE"):
            call()


def test_cross_revision_completed_start_is_new_study(project):
    cfg, factory, result, body = project
    first = run(project)
    with factory.begin() as db:
        db.get(LearningSession, first["learning_session_id"]).state = LearningSessionState.COMPLETED
    newer = regenerate_weekly_plan(cfg, result["plan_id"], as_of=EARLY)
    revised = read_weekly_plan_revision(cfg, result["plan_id"], 2)
    same_action = next(b for b in revised.blocks if b.action_id == body.blocks[0].action_id)
    accept_weekly_plan_revision(cfg, result["plan_id"], 2, accepted_at=ACCEPT)
    second = materialize_weekly_plan_block(cfg, result["plan_id"], same_action.block_id,
        expected_accepted_revision_id=newer["revision_id"], goal="Study again", materialized_at=CLICK)
    assert second["status"] == "MATERIALIZED"
    assert second["learning_session_id"] != first["learning_session_id"]
    assert second["action_id"] == first["action_id"]


def test_current_daily_cap_and_unrelated_calendar_drift(project):
    cfg, factory, result, body = project
    # Changing availability on a different date does not veto Monday.
    add_availability_window(cfg, "SYNTH", weekday="FRIDAY",
                            start_local_time=time(9), end_local_time=time(10))
    update_planning_profile(cfg, "SYNTH", max_daily_study_minutes=120)
    assert run(project)["status"] == "MATERIALIZED"
    update_planning_profile(cfg, "SYNTH", max_daily_study_minutes=119)
    with pytest.raises(ValueError, match="CURRENT_DAILY_LIMIT_EXCEEDED"):
        run(project, 1)


def test_populated_0015_to_0016_preserves_rows(project):
    cfg, factory, result, body = project
    sid = create_learning_session(cfg, body.blocks[0].course_id, "Existing", ["A"])
    with factory.begin() as db:
        # This fixture starts at head; remove later empty B3 tables before stamping.
        db.execute(text("DROP TABLE scoring_mastery_applications"))
        db.execute(text("DROP TABLE scoring_review_events"))
        db.execute(text("DROP TABLE scoring_result_snapshots"))
        db.execute(text("DROP TABLE weekly_plan_block_executions"))
        db.execute(text("UPDATE alembic_version SET version_num='0015_weekly_plan_persistence'"))
    alembic = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic.set_main_option("script_location", str(Path(__file__).resolve().parents[1] /
        "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", get_engine(cfg.database_path).url.render_as_string(
        hide_password=False).replace("%", "%%"))
    command.upgrade(alembic, "head")
    with factory() as db:
        connection = db.connection()
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert connection.execute(text("SELECT count(*) FROM sqlite_master WHERE type='table'")).scalar_one() == 56
        assert db.get(LearningSession, sid).goal == "Existing"
        assert connection.execute(text("SELECT count(*) FROM weekly_plans")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM weekly_plan_revisions")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM weekly_plan_acceptances")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM weekly_plan_block_executions")).scalar_one() == 0
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_provenance_corruption_is_controlled(project):
    cfg, factory, result, body = project
    first = run(project)
    with factory.begin() as db:
        db.get(WeeklyPlanBlockExecution, first["execution_id"]).action_id = "tampered"
    with pytest.raises(ValueError, match="MATERIALIZATION_PROVENANCE_INVALID"):
        weekly_block_materialization_info(cfg, first["execution_id"])
    with pytest.raises(ValueError, match="MATERIALIZATION_PROVENANCE_INVALID"):
        run(project)


def test_exact_week_end_and_late_click(project):
    assert run(project, when=datetime(2026, 10, 25, 23, 59, tzinfo=ZONE))["status"] == "MATERIALIZED"
    with pytest.raises(ValueError, match="WEEK_ELAPSED"):
        run(project, 1, when=datetime(2026, 10, 26, 0, 0, tzinfo=ZONE))


def test_full_learning_loop_does_not_veto_later_accepted_block(project):
    cfg, factory, result, body = project
    first = run(project)
    handoff = generate_handoff(cfg, first["learning_session_id"])
    assert start_learning_session(cfg, first["learning_session_id"]) == "STARTED"
    payload = {
        "schema_version": "2.0", "protocol_version": "subject-learning-v1",
        "result_id": "result-materialized-a", "session_id": first["learning_session_id"],
        "handoff_id": handoff.handoff_id, "course_id": body.blocks[0].course_id,
        "handoff_artifact_sha256": handoff.artifact_sha256,
        "completed_at": "2026-10-19T11:00:00+02:00", "session_outcome": "COMPLETED",
        "skill_evidence": [{"skill_id": body.blocks[0].target_skill_ids[0], "recall": 0.8}],
        "new_gaps": [], "errors": [], "recommended_next_actions": [],
    }
    path = cfg.project_root / "synthetic-result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    import_learning_result(cfg, path)
    applied = apply_learning_result_mastery(cfg, "result-materialized-a")
    assert applied.status == "APPLIED"
    with factory() as db:
        assert db.get(LearningSession, first["learning_session_id"]).state == LearningSessionState.COMPLETED
    # Completion changes NBA context; the accepted B block still has its own
    # valid Course, Skill, Calendar, and deadline authority.
    second = run(project, 1, goal="Learn B")
    assert second["status"] == "MATERIALIZED"


def test_current_availability_profile_and_academic_block(project):
    cfg, factory, result, body = project
    with factory.begin() as db:
        db.execute(text("DELETE FROM availability_windows"))
        db.execute(text("DELETE FROM planning_profiles"))
    with pytest.raises(ValueError, match="CURRENT_NO_PLANNING_PROFILE"):
        run(project)
    create_planning_profile(cfg, "SYNTH", default_action_duration_minutes=60)
    with pytest.raises(ValueError, match="CURRENT_OUTSIDE_LEGAL_AVAILABILITY"):
        run(project)
    add_availability_window(cfg, "SYNTH", weekday="MONDAY",
                            start_local_time=time(9), end_local_time=time(13))
    payload = {"stable_id": "NEW_CLASS", "activity_type": "LECTURE",
               "weekday": "MONDAY", "start_time": "09:00", "end_time": "10:00",
               "participation": "REQUIRED", "information_status": "CONFIRMED"}
    with factory.begin() as db:
        db.add(ScheduleEntry(stable_id="NEW_CLASS", course_id=body.blocks[0].course_id,
            payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))
    with pytest.raises(ValueError, match="CURRENT_ACADEMIC_BLOCK_CONFLICT"):
        run(project)


@pytest.mark.parametrize("scope,information,status,dated,conflict", [
    ("ASSESSMENT", InformationStatus.CONFIRMED, DeadlineStatus.OPEN, True, True),
    ("COURSE", InformationStatus.OBSERVED, DeadlineStatus.OPEN, True, True),
    ("UNRELATED", InformationStatus.CONFIRMED, DeadlineStatus.OPEN, True, False),
    ("COURSE", InformationStatus.INFERRED, DeadlineStatus.OPEN, True, False),
    ("COURSE", InformationStatus.UNKNOWN, DeadlineStatus.OPEN, True, False),
    ("COURSE", InformationStatus.CONFIRMED, DeadlineStatus.CANCELLED, True, False),
    ("COURSE", InformationStatus.CONFIRMED, DeadlineStatus.OPEN, False, False),
])
def test_current_deadline_authority_matrix(project, scope, information, status, dated, conflict):
    cfg, factory, result, body = project
    with factory.begin() as db:
        if scope == "ASSESSMENT":
            assessment = Assessment(stable_id="EXAM", course_id=body.blocks[0].course_id,
                                    name="Exam", information_status=InformationStatus.CONFIRMED)
            db.add(assessment)
            db.flush()
            course_id, assessment_id = None, assessment.id
        elif scope == "UNRELATED":
            other = Course(stable_id="OTHER", name="Other", semester_id=db.get(Course,
                body.blocks[0].course_id).semester_id,
                information_status=InformationStatus.CONFIRMED)
            db.add(other)
            db.flush()
            course_id, assessment_id = other.id, None
        else:
            course_id, assessment_id = body.blocks[0].course_id, None
        db.add(Deadline(stable_id="MATRIX", course_id=course_id, assessment_id=assessment_id,
            title="Deadline", due_at=datetime(2026, 10, 19, 9, 30) if dated else None,
            is_date_only=False, status=status, information_status=information))
    if conflict:
        with pytest.raises(ValueError, match="CURRENT_DEADLINE_CONFLICT"):
            run(project)
    else:
        assert run(project)["status"] == "MATERIALIZED"


def test_cli_materialize_and_info(project):
    cfg, factory, result, body = project
    runner = CliRunner()
    first = run(project)
    args = ["weekly-block-materialize", result["plan_id"], body.blocks[0].block_id,
            "--revision-id", result["revision_id"], "--goal", "CLI goal",
            "--project-root", str(cfg.project_root)]
    retried = runner.invoke(app, args)
    assert retried.exit_code == 0
    assert json.loads(retried.stdout)["status"] == "ALREADY_MATERIALIZED"
    info = runner.invoke(app, ["weekly-block-materialization-info", first["execution_id"],
        "--project-root", str(cfg.project_root)])
    assert info.exit_code == 0
    assert json.loads(info.stdout)["learning_session_id"] == first["learning_session_id"]


@pytest.mark.parametrize("date_only,due_at,conflict", [
    (False, datetime(2026, 10, 19, 10), False),
    (False, datetime(2026, 10, 19, 9, 59), True),
    (True, datetime(2026, 10, 19), True),
    (True, datetime(2026, 10, 20), False),
])
def test_deadline_equality_and_date_only(project, date_only, due_at, conflict):
    cfg, factory, result, body = project
    with factory.begin() as db:
        db.add(Deadline(stable_id="BOUNDARY", course_id=body.blocks[0].course_id,
            title="Boundary", due_at=due_at, is_date_only=date_only,
            status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED))
    if conflict:
        with pytest.raises(ValueError, match="CURRENT_DEADLINE_CONFLICT"):
            run(project)
    else:
        assert run(project)["status"] == "MATERIALIZED"


def test_dst_fall_planned_instant_remains_in_revision(tmp_path):
    cfg = load_config(project_root=tmp_path / "dst-project")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        sem = Semester(stable_id="DST", name="DST", state=SemesterState.ACTIVE,
                       timezone="Europe/Berlin")
        course = Course(stable_id="C", name="Course", semester=sem,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        db.add(Skill(id="DST_SKILL", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="DST_SKILL"))
    create_planning_profile(cfg, "DST", default_action_duration_minutes=30)
    add_availability_window(cfg, "DST", weekday="SUNDAY",
        start_local_time=time(2), end_local_time=time(3), start_fold=1)
    created = create_weekly_plan(cfg, "DST", MON, as_of=EARLY)
    body = read_weekly_plan_revision(cfg, created["plan_id"])
    block = body.blocks[0]
    assert block.starts_at.hour == 2 and block.starts_at.utcoffset().total_seconds() == 3600
    accept_weekly_plan_revision(cfg, created["plan_id"], accepted_at=ACCEPT)
    outcome = materialize_weekly_plan_block(cfg, created["plan_id"], block.block_id,
        expected_accepted_revision_id=created["revision_id"], goal="DST study",
        materialized_at=CLICK)
    with factory() as db:
        session = db.get(LearningSession, outcome["learning_session_id"])
        assert session.planned_at == block.starts_at.replace(tzinfo=None)
        assert session.planned_at.tzinfo is None
    assert weekly_block_materialization_info(cfg, outcome["execution_id"])["block_id"] == block.block_id
    factory.kw["bind"].dispose()


def test_execution_insert_failure_rolls_back_session_and_targets(project, monkeypatch):
    cfg, factory, result, body = project
    engine = factory.kw["bind"]
    monkeypatch.setattr("uniflow.application.weekly_plans.get_engine", lambda _path: engine)

    def fail_execution_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "INSERT INTO weekly_plan_block_executions" in statement:
            raise RuntimeError("synthetic execution write failure")

    event.listen(engine, "before_cursor_execute", fail_execution_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic execution write failure"):
            run(project)
    finally:
        event.remove(engine, "before_cursor_execute", fail_execution_insert)
    with factory() as db:
        for table in ("learning_sessions", "learning_session_target_skills", "weekly_plan_block_executions"):
            assert db.connection().exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0


def test_database_unique_revision_block_is_final_defense(project):
    cfg, factory, result, body = project
    first = run(project)
    with pytest.raises(IntegrityError):
        with factory.begin() as db:
            db.add(WeeklyPlanBlockExecution(id="duplicate", weekly_plan_revision_id=result["revision_id"],
                block_id=body.blocks[0].block_id, action_id=body.blocks[0].action_id,
                execution_kind="CREATED_SESSION", learning_session_id=first["learning_session_id"],
                materialized_at=datetime(2026, 10, 19, 8, tzinfo=timezone.utc)))


def test_retry_precedes_new_accepted_pointer_and_completion(project):
    cfg, factory, result, body = project
    first = run(project)
    revised = regenerate_weekly_plan(cfg, result["plan_id"], as_of=EARLY)
    accept_weekly_plan_revision(cfg, result["plan_id"], 2, accepted_at=ACCEPT)
    with factory.begin() as db:
        db.get(LearningSession, first["learning_session_id"]).state = LearningSessionState.COMPLETED
    again = run(project, goal=None, when=datetime(2026, 10, 27, tzinfo=ZONE))
    assert again["status"] == "ALREADY_MATERIALIZED"
    assert again["execution_id"] == first["execution_id"]
    assert again["learning_session_id"] == first["learning_session_id"]
