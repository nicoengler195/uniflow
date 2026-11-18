"""Planning facade behavior against temporary canonical projects."""
from datetime import date, datetime, time
import json
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application import planning_workflow as workflow
from uniflow.application import learning_workflow
from uniflow.application.learning_workflow import generate_handoff
from uniflow.application.initializer import initialize_project
from uniflow.application.planning_calendar import add_availability_window, create_planning_profile
from uniflow.application.weekly_plans import read_weekly_plan_revision
from uniflow.application.week_read_models import get_week_view
from uniflow.application.session_inbox_read_models import get_session_inbox
from uniflow.application.course_read_models import get_course_overview
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, LearningSession, Semester, Skill, WeeklyPlanAcceptance,
    WeeklyPlanBlockExecution, WeeklyPlanRevision,
)
from uniflow.domain.enums import InformationStatus, SemesterState
from uniflow.schemas.planning_workflow_v1 import (
    CreatePlanRequestV1, GeneratedRevisionSuccessV1, EditRevisionSuccessV1,
    MaterializeBlockSuccessV1,
)

ZONE = ZoneInfo("Europe/Berlin")
MON = date(2026, 10, 19)
EARLY = datetime(2026, 10, 19, 8, tzinfo=ZONE)
ACCEPT = datetime(2026, 10, 19, 8, 30, tzinfo=ZONE)


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
        db.add(Skill(id="A", name="A", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="A"))
    create_planning_profile(cfg, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(cfg, "SYNTH", weekday="MONDAY",
                            start_local_time=time(9), end_local_time=time(12))
    yield cfg, factory
    factory.kw["bind"].dispose()


def create(cfg):
    return workflow.create_plan(cfg, {"semester_id": "SYNTH", "week_start": MON, "as_of": EARLY})


def code(exc):
    return exc.value.code


def test_strict_inputs_and_controlled_errors(project):
    cfg, _ = project
    for patch in ({"limit": True}, {"as_of": datetime(2026, 10, 19)},
                  {"duration_overrides": {"x": True}}, {"unknown": 1}):
        with pytest.raises(WorkflowCommandError) as error:
            workflow.create_plan(cfg, {"semester_id": "SYNTH", "week_start": MON, **patch})
        assert code(error) == "WORKFLOW_INPUT_INVALID"
    with pytest.raises(ValidationError):
        CreatePlanRequestV1(semester_id="SYNTH", week_start=MON, limit=True)
    with pytest.raises(WorkflowCommandError) as error:
        workflow.create_plan(cfg, {"semester_id": "SYNTH", "week_start": date(2026, 10, 20)})
    assert code(error) == "INVALID_WEEK_START"


def test_create_regenerate_modes_and_revision_concurrency(project):
    cfg, factory = project
    r1 = create(cfg)
    assert isinstance(r1, GeneratedRevisionSuccessV1)
    assert r1.command == "planning.plan.create" and r1.outcome == "CREATED"
    assert r1.revision_kind == "GENERATED"
    assert "parent_revision_id" not in r1.model_dump()
    with pytest.raises(WorkflowCommandError) as error:
        create(cfg)
    assert code(error) == "PLAN_ALREADY_EXISTS"
    body1 = read_weekly_plan_revision(cfg, r1.plan_id)
    block = body1.blocks[0]
    r2 = workflow.set_block_duration(cfg, {"plan_id": r1.plan_id, "block_id": block.block_id,
        "minutes": 45, "expected_revision_id": r1.revision_id})
    assert isinstance(r2, EditRevisionSuccessV1)
    assert r2.command == "planning.block.set_duration" and r2.outcome == "EDITED"
    assert r2.parent_revision_id == r1.revision_id and r2.revision_kind == "MANUAL_EDIT"
    assert r2.model_dump()["parent_revision_id"] == r1.revision_id
    with pytest.raises(WorkflowCommandError) as error:
        workflow.remove_block(cfg, {"plan_id": r1.plan_id, "block_id": block.block_id,
            "expected_revision_id": r1.revision_id})
    assert code(error) == "REVISION_CHANGED"
    block2 = read_weekly_plan_revision(cfg, r1.plan_id).blocks[0]
    r3 = workflow.remove_block(cfg, {"plan_id": r1.plan_id, "block_id": block2.block_id,
        "expected_revision_id": r2.revision_id})
    assert r3.parent_revision_id == r2.revision_id
    assert read_weekly_plan_revision(cfg, r1.plan_id, 1).blocks[0].block_id == block.block_id
    assert not read_weekly_plan_revision(cfg, r1.plan_id, 3).blocks
    r4 = workflow.regenerate_plan(cfg, {"plan_id": r1.plan_id, "as_of": EARLY,
        "course_filter_mode": "REPLACE", "course_id": "C",
        "duration_overrides_mode": "REPLACE", "duration_overrides": {}, "limit": 1})
    assert isinstance(r4, GeneratedRevisionSuccessV1)
    assert r4.command == "planning.plan.regenerate" and r4.outcome == "REGENERATED"
    assert r4.revision_kind == "GENERATED"
    assert "parent_revision_id" not in r4.model_dump()
    with factory() as db:
        assert db.get(WeeklyPlanRevision, r4.revision_id).parent_revision_id == r3.revision_id
    source = read_weekly_plan_revision(cfg, r1.plan_id).source
    assert source.source_course_filter_id is not None and source.source_limit == 1
    r5 = workflow.regenerate_plan(cfg, {"plan_id": r1.plan_id, "as_of": EARLY,
        "course_filter_mode": "CLEAR", "duration_overrides_mode": "CLEAR"})
    source = read_weekly_plan_revision(cfg, r1.plan_id).source
    assert source.source_course_filter_id is None and source.duration_overrides == {}
    assert source.source_limit == 1 and r5.accepted_revision_id is None
    with factory() as db:
        assert len(db.scalars(select(WeeklyPlanRevision)).all()) == 5


def test_strict_revision_success_dtos():
    shared = {"plan_id": "plan", "revision_id": "revision", "revision_number": 1,
        "revision_payload_sha256": "a" * 64, "accepted_revision_id": None}
    generated = {**shared, "command": "planning.plan.create", "outcome": "CREATED",
        "revision_kind": "GENERATED"}
    edit = {**shared, "command": "planning.block.move", "outcome": "EDITED",
        "revision_kind": "MANUAL_EDIT", "parent_revision_id": "parent"}
    assert GeneratedRevisionSuccessV1(**generated).revision_kind == "GENERATED"
    assert EditRevisionSuccessV1(**edit).parent_revision_id == "parent"
    for bad in ({**generated, "revision_kind": "MANUAL_EDIT"},
                {**generated, "parent_revision_id": "parent"},
                {**generated, "outcome": "REGENERATED"}):
        with pytest.raises(ValidationError):
            GeneratedRevisionSuccessV1(**bad)
    for bad in ({**edit, "revision_kind": "GENERATED"},
                {key: value for key, value in edit.items() if key != "parent_revision_id"},
                {**edit, "parent_revision_id": ""}):
        with pytest.raises(ValidationError):
            EditRevisionSuccessV1(**bad)


def test_materialization_session_state_vocabulary():
    facts = {"execution_id": "e", "plan_id": "p", "revision_id": "r",
        "block_id": "b", "action_id": "a", "execution_kind": "CREATED_SESSION",
        "learning_session_id": "s", "materialized_at": ACCEPT, "outcome": "MATERIALIZED"}
    for state in ("PLANNED", "HANDOFF_CREATED", "IN_PROGRESS", "RESULT_RECEIVED",
                  "VALIDATED", "MASTERY_UPDATED", "COMPLETED"):
        assert MaterializeBlockSuccessV1(**facts, session_state=state).session_state == state
    with pytest.raises(ValidationError):
        MaterializeBlockSuccessV1(**facts, session_state="RANDOM_STATE")


def test_accept_materialize_retry_and_handoff(project):
    cfg, factory = project
    r1 = create(cfg)
    body = read_weekly_plan_revision(cfg, r1.plan_id)
    accepted = workflow.accept_plan(cfg, {"plan_id": r1.plan_id,
        "revision_id": r1.revision_id, "accepted_at": ACCEPT})
    again = workflow.accept_plan(cfg, {"plan_id": r1.plan_id, "revision_id": r1.revision_id})
    assert accepted.outcome == "ACCEPTED" and again.outcome == "ALREADY_ACCEPTED"
    assert again.acceptance_id == accepted.acceptance_id
    request = {"plan_id": r1.plan_id, "block_id": body.blocks[0].block_id,
        "expected_accepted_revision_id": r1.revision_id, "goal": "Learn",
        "materialized_at": datetime(2026, 10, 19, 10, 30, tzinfo=ZONE)}
    made = workflow.materialize_block(cfg, request)
    assert made.session_state == "PLANNED"
    retry = workflow.materialize_block(cfg, {**request, "goal": None})
    assert made.outcome == "MATERIALIZED" and retry.outcome == "ALREADY_MATERIALIZED"
    assert made.execution_id == retry.execution_id
    assert made.learning_session_id == retry.learning_session_id
    handoff = generate_handoff(cfg, {"session_id": made.learning_session_id})
    assert handoff.session_id == made.learning_session_id
    week = get_week_view(cfg, "SYNTH", MON, as_of=ACCEPT)
    assert week.plan.accepted_revision_id == r1.revision_id
    assert week.plan.current_revision_id == r1.revision_id
    inbox = get_session_inbox(cfg, "SYNTH", as_of=ACCEPT)
    assert made.learning_session_id in str(inbox.model_dump(mode="json"))
    course = get_course_overview(cfg, "SYNTH", "C",
        as_of=datetime(2026, 10, 19, 11, tzinfo=ZONE),
        activity_since=EARLY, activity_through=ACCEPT)
    assert r1.revision_id in str(course.model_dump(mode="json"))
    with factory() as db:
        assert len(db.scalars(select(WeeklyPlanAcceptance)).all()) == 1
        assert len(db.scalars(select(WeeklyPlanBlockExecution)).all()) == 1
        assert len(db.scalars(select(LearningSession)).all()) == 1

    assert learning_workflow.start_session(cfg, {"session_id": made.learning_session_id}).outcome == "STARTED"
    payload = {"schema_version": "2.0", "protocol_version": "subject-learning-v1",
        "result_id": "result-facade-completed", "session_id": made.learning_session_id,
        "handoff_id": handoff.handoff_id, "course_id": body.blocks[0].course_id,
        "handoff_artifact_sha256": handoff.artifact_sha256,
        "completed_at": "2026-10-19T11:00:00+02:00", "session_outcome": "COMPLETED",
        "skill_evidence": [{"skill_id": "A", "recall": 0.8}],
        "new_gaps": [], "errors": [], "recommended_next_actions": []}
    result_file = cfg.project_root / "synthetic-result.json"
    result_file.write_text(json.dumps(payload), encoding="utf-8")
    imported = learning_workflow.import_result_file(cfg, {"path": result_file})
    assert learning_workflow.apply_mastery(cfg, {"result_id": imported.result_id}).outcome == "APPLIED"
    completed_retry = workflow.materialize_block(cfg, {**request, "goal": None})
    assert completed_retry.outcome == "ALREADY_MATERIALIZED"
    assert completed_retry.session_state == "COMPLETED"
    assert completed_retry.execution_id == made.execution_id
    assert completed_retry.learning_session_id == made.learning_session_id


def test_known_unknown_and_acceptance_factors(project, monkeypatch):
    cfg, _ = project
    r1 = create(cfg)
    for value, expected in (("PLAN_CHANGED_DURING_CAPTURE", "PLAN_CHANGED_DURING_CAPTURE"),
                            ("SOME_NEW_BUG", None)):
        def fail(*args, **kwargs):
            raise ValueError(value)
        monkeypatch.setattr(workflow.weekly_plans, "regenerate_weekly_plan", fail)
        if expected:
            with pytest.raises(WorkflowCommandError) as error:
                workflow.regenerate_plan(cfg, {"plan_id": r1.plan_id})
            assert code(error) == expected
        else:
            with pytest.raises(ValueError, match=value):
                workflow.regenerate_plan(cfg, {"plan_id": r1.plan_id})
    def stale(*args, **kwargs):
        return {"status": "STALE", "assessment": {"factors": ["WEEK_ELAPSED", "WEEK_ELAPSED"]}}
    monkeypatch.setattr(workflow.weekly_plans, "accept_weekly_plan_revision", stale)
    with pytest.raises(WorkflowCommandError) as error:
        workflow.accept_plan(cfg, {"plan_id": r1.plan_id, "revision_id": r1.revision_id})
    assert code(error) == "PLAN_ACCEPTANCE_STALE"
    assert error.value.details == {"factors": ["WEEK_ELAPSED"], "truncated": False}


def test_move_place_accept_exact_and_materialization_gates(project):
    cfg, _ = project
    r1 = create(cfg)
    block = read_weekly_plan_revision(cfg, r1.plan_id).blocks[0]
    moved = workflow.move_block(cfg, {"plan_id": r1.plan_id, "block_id": block.block_id,
        "expected_revision_id": r1.revision_id,
        "new_start": datetime(2026, 10, 19, 10, tzinfo=ZONE)})
    with pytest.raises(WorkflowCommandError) as error:
        workflow.accept_plan(cfg, {"plan_id": r1.plan_id, "revision_id": r1.revision_id})
    assert code(error) == "REVISION_NOT_CURRENT"
    with pytest.raises(WorkflowCommandError) as error:
        workflow.accept_plan(cfg, {"plan_id": r1.plan_id, "revision_id": "wrong"})
    assert code(error) == "UNKNOWN_WEEKLY_PLAN_REVISION"
    current_block = read_weekly_plan_revision(cfg, r1.plan_id).blocks[0]
    removed = workflow.remove_block(cfg, {"plan_id": r1.plan_id,
        "block_id": current_block.block_id, "expected_revision_id": moved.revision_id})
    action = read_weekly_plan_revision(cfg, r1.plan_id).unscheduled_actions[0]
    placed = workflow.place_unscheduled(cfg, {"plan_id": r1.plan_id,
        "action_id": action.action_id, "start": datetime(2026, 10, 19, 9, tzinfo=ZONE),
        "expected_revision_id": removed.revision_id})
    assert placed.parent_revision_id == removed.revision_id
    latest_block = read_weekly_plan_revision(cfg, r1.plan_id).blocks[0]
    with pytest.raises(WorkflowCommandError) as error:
        workflow.materialize_block(cfg, {"plan_id": r1.plan_id, "block_id": latest_block.block_id,
            "expected_accepted_revision_id": placed.revision_id, "goal": "Learn",
            "materialized_at": ACCEPT})
    assert code(error) == "PLAN_NOT_ACCEPTED"
    accepted = workflow.accept_plan(cfg, {"plan_id": r1.plan_id,
        "revision_id": placed.revision_id, "accepted_at": ACCEPT})
    assert accepted.outcome == "ACCEPTED"
    with pytest.raises(WorkflowCommandError) as error:
        workflow.materialize_block(cfg, {"plan_id": r1.plan_id, "block_id": block.block_id,
            "expected_accepted_revision_id": r1.revision_id, "goal": "Learn",
            "materialized_at": ACCEPT})
    assert code(error) in {"UNKNOWN_PLAN_BLOCK", "ACCEPTED_REVISION_CHANGED"}


def test_accepted_revision_and_later_draft_remain_distinct(project):
    cfg, _ = project
    r1 = create(cfg)
    block = read_weekly_plan_revision(cfg, r1.plan_id).blocks[0]
    workflow.accept_plan(cfg, {"plan_id": r1.plan_id, "revision_id": r1.revision_id,
        "accepted_at": ACCEPT})
    r2 = workflow.set_block_duration(cfg, {"plan_id": r1.plan_id,
        "block_id": block.block_id, "minutes": 45,
        "expected_revision_id": r1.revision_id})
    assert r2.accepted_revision_id == r1.revision_id
    week = get_week_view(cfg, "SYNTH", MON, as_of=ACCEPT)
    assert week.plan.accepted_revision_id == r1.revision_id
    assert week.plan.current_revision_id == r2.revision_id
    result = workflow.materialize_block(cfg, {"plan_id": r1.plan_id,
        "block_id": block.block_id, "expected_accepted_revision_id": r1.revision_id,
        "goal": "Learn", "materialized_at": datetime(2026, 10, 19, 10, 30, tzinfo=ZONE)})
    assert result.revision_id == r1.revision_id


def test_no_facade_writer_lock_or_cli_import():
    from pathlib import Path
    source = Path(workflow.__file__).read_text(encoding="utf-8")
    assert "project_write_lock" not in source
    assert "uniflow.cli" not in source
    assert "subprocess" not in source
