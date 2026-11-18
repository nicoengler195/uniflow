"""Temporary-project checks for persisted weekly plan provenance and acceptance."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from uniflow.application.initializer import initialize_project
from uniflow.application.planning import plan_next_actions
from uniflow.application.planning_calendar import (
    add_availability_window, add_calendar_constraint, create_planning_profile,
    update_planning_profile,
)
from uniflow.application.weekly_plans import (
    KINDS, accept_weekly_plan_revision, assess_weekly_plan_revision,
    create_weekly_plan, read_weekly_plan_revision, regenerate_weekly_plan,
    weekly_plan_history, weekly_plan_info,
    move_weekly_plan_block, set_weekly_plan_block_duration,
    remove_weekly_plan_block, place_weekly_plan_action,
)
import uniflow.application.weekly_plans as service
from uniflow.domain.enums import DeadlineStatus, InformationStatus, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Deadline, DerivedArtifact, Semester, Skill,
    WeeklyPlan, WeeklyPlanAcceptance, WeeklyPlanRevision,
)
from uniflow.schemas.weekly_plan_v1 import canonical_weekly_plan_revision_json
from uniflow.schemas.calendar_v1 import ConstraintOccurrenceV1, AcademicOccurrenceV1
from uniflow.application.weekly_plan_editing import (
    transform_move_block, verify_single_edit, validate_weekly_plan_layout,
)
from uniflow.cli.main import app

ZONE = ZoneInfo("Europe/Berlin")
MON = date(2026, 10, 19)
BEFORE = datetime(2026, 10, 19, 8, tzinfo=ZONE)
CHECK = datetime(2026, 10, 19, 8, 30, tzinfo=ZONE)


@pytest.fixture
def setup(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        sem = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
                       timezone="Europe/Berlin")
        course = Course(stable_id="COURSE", name="Course", semester=sem,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        db.add(Skill(id="SKILL", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SKILL"))
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY",
                            start_local_time=time(9), end_local_time=time(12))
    yield config, factory
    factory.kw["bind"].dispose()


def create(config, **kwargs):
    return create_weekly_plan(config, "SYNTH", MON, as_of=BEFORE, **kwargs)


def test_create_artifacts_correspondence_and_no_learning_write(setup):
    config, factory = setup
    result = create(config)
    body = read_weekly_plan_revision(config, result["plan_id"])
    assert body.revision_number == 1 and body.revision_kind == "GENERATED"
    assert body.parent_revision_id is None
    assert len(body.blocks) == 1 and not body.unscheduled_actions
    assert body.blocks[0].source_scheduler_block_id
    assert "revision_payload_sha256" not in body.model_dump()
    assert "revision_artifact_id" not in body.model_dump()
    assert assess_weekly_plan_revision(config, result["plan_id"], checked_at=CHECK).status == "FRESH"
    with factory() as db:
        assert db.scalar(select(WeeklyPlan).where(WeeklyPlan.id == result["plan_id"])).accepted_revision_id is None
        row = db.get(WeeklyPlanRevision, result["revision_id"])
        assert set(db.scalars(select(DerivedArtifact.kind)).all()) == set(KINDS)
        assert db.get(DerivedArtifact, row.revision_artifact_id).sha256 == row.revision_payload_sha256
        assert db.get(DerivedArtifact, row.revision_artifact_id).size_bytes == len(canonical_weekly_plan_revision_json(body))
        for table in ("learning_sessions", "learning_session_target_skills", "learning_handoffs",
                      "learning_results", "evidence", "mastery_states"):
            assert db.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar_one() == 0
    with pytest.raises(ValueError, match="PLAN_ALREADY_EXISTS"):
        create(config)


def test_accept_regenerate_history_and_stale_calendar(setup):
    config, factory = setup
    first = create(config)
    pid = first["plan_id"]
    accepted = accept_weekly_plan_revision(config, pid, accepted_at=CHECK)
    assert accepted["status"] == "ACCEPTED"
    assert accept_weekly_plan_revision(config, pid,
        accepted_at=datetime(2026, 10, 20, tzinfo=ZONE))["status"] == "ALREADY_ACCEPTED"
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 19, 10, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 11, tzinfo=ZONE))
    assessment = assess_weekly_plan_revision(config, pid, checked_at=CHECK)
    assert assessment.status == "STALE" and "CALENDAR_CONTEXT_CHANGED" in assessment.factors
    second = regenerate_weekly_plan(config, pid, as_of=BEFORE)
    assert second["revision_number"] == 2
    assert second["accepted_revision_id"] == first["revision_id"]
    assert read_weekly_plan_revision(config, pid, 1).revision_id == first["revision_id"]
    assert read_weekly_plan_revision(config, pid, 2).parent_revision_id == first["revision_id"]
    assert weekly_plan_info(config, pid)["current_revision_id"] == second["revision_id"]
    assert weekly_plan_info(config, pid)["accepted_revision_id"] == first["revision_id"]
    assert accept_weekly_plan_revision(config, pid, 1, accepted_at=CHECK)["status"] == "ALREADY_ACCEPTED"
    assert accept_weekly_plan_revision(config, pid, 2, accepted_at=CHECK)["status"] == "ACCEPTED"
    assert len(weekly_plan_history(config, pid)["acceptances"]) == 2
    with factory() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM learning_sessions").scalar_one() == 0


def test_stale_nba_past_and_invalid_artifact(setup):
    config, factory = setup
    first = create(config)
    pid = first["plan_id"]
    assert "BLOCK_IN_PAST" in assess_weekly_plan_revision(config, pid,
        checked_at=datetime(2026, 10, 19, 10, tzinfo=ZONE)).factors
    assert "WEEK_ELAPSED" in assess_weekly_plan_revision(config, pid,
        checked_at=datetime(2026, 10, 26, tzinfo=ZONE)).factors
    with factory.begin() as db:
        db.get(Skill, "SKILL").status = "MERGED"
    stale = assess_weekly_plan_revision(config, pid, checked_at=CHECK)
    assert "NBA_CONTEXT_CHANGED" in stale.factors
    assert "ACTION_NO_LONGER_VALID" in stale.factors
    assert accept_weekly_plan_revision(config, pid, accepted_at=CHECK)["status"] == "STALE"
    with factory() as db:
        artifact = db.get(DerivedArtifact, db.get(WeeklyPlanRevision, first["revision_id"]).nba_artifact_id)
        path = artifact.stored_path
    from pathlib import Path
    Path(path).unlink()
    assert assess_weekly_plan_revision(config, pid, checked_at=CHECK).status == "INVALID"
    with pytest.raises(ValueError, match="SOURCE_ARTIFACT_INVALID"):
        read_weekly_plan_revision(config, pid)


def test_query_provenance_and_override_replacement(setup):
    config, factory = setup
    action = plan_next_actions(config, "SYNTH", as_of=BEFORE, course_id="COURSE", limit=1).recommendations[0]
    first = create(config, course_id="COURSE", limit=1, durations={action.action_id: 45})
    pid = first["plan_id"]
    body = read_weekly_plan_revision(config, pid)
    assert body.source.source_course_filter_id == "COURSE"
    assert body.source.source_limit == 1
    assert body.source.duration_overrides == {action.action_id: 45}
    assert body.source.source_as_of == BEFORE
    second = regenerate_weekly_plan(config, pid, as_of=BEFORE)
    assert read_weekly_plan_revision(config, pid, 2).source.duration_overrides == {action.action_id: 45}
    third = regenerate_weekly_plan(config, pid, as_of=BEFORE, course_id=None,
                                   limit=2, durations={})
    body3 = read_weekly_plan_revision(config, pid, 3)
    assert body3.source.source_course_filter_id is None
    assert body3.source.source_limit == 2 and body3.source.duration_overrides == {}


def test_monday_and_fresh_migration(setup):
    config, factory = setup
    with pytest.raises(ValueError, match="INVALID_WEEK_START"):
        create_weekly_plan(config, "SYNTH", date(2026, 10, 20), as_of=BEFORE)
    with factory() as db:
        assert db.connection().exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0019_scoring_mastery"
        assert db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table'").scalar_one() == 56
        assert db.connection().exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert db.connection().exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize("changed", ["nba", "calendar"])
def test_capture_race_persists_nothing(setup, monkeypatch, changed):
    config, factory = setup
    name = "plan_next_actions" if changed == "nba" else "project_calendar_week"
    original = getattr(service, name)
    calls = 0

    def replacement(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        return result.model_copy(update={"context_fingerprint": "f" * 64}) if calls == 2 else result

    monkeypatch.setattr(service, name, replacement)
    with pytest.raises(ValueError, match="SOURCE_CHANGED_DURING_CAPTURE"):
        create(config)
    assert calls == 2
    with factory() as db:
        for table in ("weekly_plans", "weekly_plan_revisions", "weekly_plan_acceptances", "derived_artifacts"):
            assert db.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar_one() == 0


@pytest.mark.parametrize("failure", ["wrong_kind", "missing_file", "corrupt_file", "wrong_size"])
def test_artifact_failure_is_invalid(setup, failure):
    config, factory = setup
    result = create(config)
    with factory.begin() as db:
        row = db.get(WeeklyPlanRevision, result["revision_id"])
        artifact = db.get(DerivedArtifact, row.revision_artifact_id)
        path = Path(artifact.stored_path)
        if failure == "wrong_kind":
            artifact.kind = "LEARNING_HANDOFF_JSON"
        elif failure == "wrong_size":
            artifact.size_bytes += 1
    if failure == "missing_file":
        path.unlink()
    if failure == "corrupt_file":
        path.write_bytes(b"bad")
    assessment = assess_weekly_plan_revision(config, result["plan_id"], checked_at=CHECK)
    assert assessment.status == "INVALID"
    assert assessment.factors == ["SOURCE_ARTIFACT_INVALID"]
    assert accept_weekly_plan_revision(config, result["plan_id"], accepted_at=CHECK)["status"] == "INVALID"


def test_inherited_override_now_unknown_is_controlled(setup):
    config, factory = setup
    action = plan_next_actions(config, "SYNTH", as_of=BEFORE).recommendations[0]
    first = create(config, durations={action.action_id: 45})
    with factory.begin() as db:
        from uniflow.infrastructure.database.models import LearningSession, LearningSessionTargetSkill
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        db.add(LearningSession(id="already-planned", course_id=course.id, goal="Study", state="PLANNED"))
        db.flush()
        db.add(LearningSessionTargetSkill(learning_session_id="already-planned", skill_id="SKILL", ordinal=0))
    with pytest.raises(ValueError, match="STALE_DURATION_OVERRIDE"):
        regenerate_weekly_plan(config, first["plan_id"], as_of=BEFORE)
    assert len(weekly_plan_history(config, first["plan_id"])["revisions"]) == 1
    second = regenerate_weekly_plan(config, first["plan_id"], as_of=BEFORE, durations={})
    assert second["revision_number"] == 2


def test_populated_0014_upgrade_preserves_rows(tmp_path):
    db_path = tmp_path / "populated.sqlite3"
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0014_planning_calendar_foundation")
    engine = get_engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,timezone,created_at,updated_at) VALUES ('s','S','Semester','ACTIVE','Europe/Berlin',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO courses (id,semester_id,stable_id,name,kind,participation,unknowns_json,information_status,created_at,updated_at) VALUES ('c','s','C','Course','MODULE','UNKNOWN','[]','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO schedule_entries (id,stable_id,course_id,payload_json,information_status,created_at,updated_at) VALUES ('e','E','c','{}','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO schedule_exceptions (id,stable_id,course_id,payload_json,information_status,created_at,updated_at) VALUES ('x','X','c','{}','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO planning_profiles (id,semester_id,revision,default_action_duration_minutes,created_at,updated_at) VALUES ('p','s',1,60,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO availability_windows (id,planning_profile_id,weekday,start_local_time,end_local_time,created_revision,created_at) VALUES ('a','p','MONDAY','09:00','12:00',2,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO calendar_constraints (id,planning_profile_id,constraint_type,local_date,created_revision,created_at) VALUES ('k','p','BLOCKED','2026-10-19',2,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO skills (id,name,kind,status,created_at,updated_at) VALUES ('sk','Skill','CONCEPT','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO course_skills (id,course_id,skill_id,is_active,created_at) VALUES ('cs','c','sk',1,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO mastery_states (id,skill_id,value,calculated_at,calculation_method,created_at,updated_at) VALUES ('m','sk',0.5,CURRENT_TIMESTAMP,'manual',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO learning_sessions (id,course_id,goal,state,created_at,updated_at) VALUES ('l','c','Goal','PLANNED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = get_engine(db_path)
    with engine.connect() as conn:
        for table in ("semesters", "courses", "schedule_entries", "schedule_exceptions",
                      "planning_profiles", "availability_windows", "calendar_constraints",
                      "skills", "course_skills", "mastery_states", "learning_sessions"):
            assert conn.execute(text("SELECT count(*) FROM " + table)).scalar_one() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_concurrent_create_one_root(setup):
    config, factory = setup
    def attempt():
        try:
            return create(config)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "PLAN_ALREADY_EXISTS" in results
    with factory() as db:
        assert db.scalar(select(WeeklyPlan).limit(1)) is not None
        assert db.connection().exec_driver_sql("SELECT count(*) FROM weekly_plans").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM weekly_plan_revisions").scalar_one() == 1


def test_artifact_kind_conflict_does_not_create_plan(setup):
    config, factory = setup
    from uniflow.schemas.planning_v1 import canonical_planning_json
    import hashlib
    nba = plan_next_actions(config, "SYNTH", as_of=BEFORE)
    digest = hashlib.sha256(canonical_planning_json(nba)).hexdigest()
    with factory.begin() as db:
        db.add(DerivedArtifact(sha256=digest, kind="LEARNING_HANDOFF_JSON",
                               stored_path="unused", size_bytes=1))
    with pytest.raises(ValueError, match="ARTIFACT_KIND_CONFLICT"):
        create(config)
    with factory() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM weekly_plans").scalar_one() == 0


def test_manual_edit_lifecycle_and_source_lineage(setup):
    config, factory = setup
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        for sid in ("SKILL_B", "SKILL_C"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        for sid in ("SKILL_B", "SKILL_C"):
            db.add(CourseSkill(course_id=course.id, skill_id=sid))
    add_availability_window(config, "SYNTH", weekday="TUESDAY",
                            start_local_time=time(9), end_local_time=time(12))
    first = create(config, course_id=None, limit=10)
    pid = first["plan_id"]
    source = read_weekly_plan_revision(config, pid)
    assert len(source.blocks) == 3
    assert accept_weekly_plan_revision(config, pid, accepted_at=CHECK)["status"] == "ACCEPTED"
    artifacts_before = {}
    with factory() as db:
        row = db.get(WeeklyPlanRevision, first["revision_id"])
        artifacts_before = {kind: getattr(row, kind) for kind in (
            "nba_artifact_id", "calendar_artifact_id", "weekly_schedule_artifact_id")}
        original_sha = row.revision_payload_sha256
    a, b, c = source.blocks
    moved = move_weekly_plan_block(config, pid, a.block_id,
        datetime(2026, 10, 20, 9, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    assert moved["parent_revision_id"] == first["revision_id"]
    assert moved["accepted_revision_id"] == first["revision_id"]
    body2 = read_weekly_plan_revision(config, pid)
    moved_a = next(x for x in body2.blocks if x.action_id == a.action_id)
    assert moved_a.placement_source == "MANUAL_EDIT"
    assert moved_a.duration_source == a.duration_source
    assert moved_a.source_scheduler_block_id == a.source_scheduler_block_id
    assert all(x.block_id not in {a.block_id, b.block_id, c.block_id} for x in body2.blocks)
    current_b = next(x for x in body2.blocks if x.action_id == b.action_id)
    resized = set_weekly_plan_block_duration(config, pid, current_b.block_id, 30,
        expected_revision_id=moved["revision_id"])
    body3 = read_weekly_plan_revision(config, pid)
    resized_b = next(x for x in body3.blocks if x.action_id == b.action_id)
    assert resized_b.starts_at == current_b.starts_at
    assert resized_b.placement_source == current_b.placement_source
    assert resized_b.duration_source == "MANUAL_EDIT"
    current_c = next(x for x in body3.blocks if x.action_id == c.action_id)
    removed = remove_weekly_plan_block(config, pid, current_c.block_id,
        expected_revision_id=resized["revision_id"])
    body4 = read_weekly_plan_revision(config, pid)
    pending = next(x for x in body4.unscheduled_actions if x.action_id == c.action_id)
    assert pending.primary_reason == "USER_REMOVED"
    assert pending.resolved_duration_minutes == c.duration_minutes
    placed = place_weekly_plan_action(config, pid, c.action_id,
        datetime(2026, 10, 20, 10, tzinfo=ZONE), expected_revision_id=removed["revision_id"])
    body5 = read_weekly_plan_revision(config, pid)
    placed_c = next(x for x in body5.blocks if x.action_id == c.action_id)
    assert placed_c.source_scheduler_block_id == c.source_scheduler_block_id
    assert {x.action_id for x in body5.blocks} == {a.action_id, b.action_id, c.action_id}
    assert not body5.unscheduled_actions
    assert read_weekly_plan_revision(config, pid, 1) == source
    assert weekly_plan_info(config, pid)["accepted_revision_id"] == first["revision_id"]
    assert [x["revision_kind"] for x in weekly_plan_history(config, pid)["revisions"]] == [
        "GENERATED", "MANUAL_EDIT", "MANUAL_EDIT", "MANUAL_EDIT", "MANUAL_EDIT"]
    assert assess_weekly_plan_revision(config, pid, checked_at=CHECK).status == "FRESH"
    assert accept_weekly_plan_revision(config, pid, accepted_at=CHECK)["status"] == "ACCEPTED"
    with factory() as db:
        rows = list(db.scalars(select(WeeklyPlanRevision).where(WeeklyPlanRevision.weekly_plan_id == pid)))
        assert len(rows) == 5
        assert all(all(getattr(row, kind) == value for kind, value in artifacts_before.items()) for row in rows)
        assert rows[0].revision_payload_sha256 == original_sha
        assert db.connection().exec_driver_sql("SELECT count(*) FROM derived_artifacts").scalar_one() == 8
        for table in ("learning_sessions", "learning_session_target_skills", "learning_handoffs",
                      "learning_results", "evidence", "mastery_states"):
            assert db.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar_one() == 0
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 19, 11, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 12, tzinfo=ZONE))
    assert "CALENDAR_CONTEXT_CHANGED" in assess_weekly_plan_revision(config, pid, checked_at=CHECK).factors
    current = next(x for x in body5.blocks if x.action_id == c.action_id)
    stale_edit = set_weekly_plan_block_duration(config, pid, current.block_id, 45,
        expected_revision_id=placed["revision_id"])
    assert assess_weekly_plan_revision(config, pid, checked_at=CHECK).status == "STALE"
    assert accept_weekly_plan_revision(config, pid, accepted_at=CHECK)["status"] == "STALE"
    regenerated = regenerate_weekly_plan(config, pid, as_of=BEFORE)
    assert regenerated["revision_number"] == 7
    assert read_weekly_plan_revision(config, pid).revision_kind == "GENERATED"
    assert weekly_plan_info(config, pid)["accepted_revision_id"] == placed["revision_id"]


def test_manual_edit_rejects_stale_token_and_invalid_geometry(setup):
    config, factory = setup
    first = create(config)
    pid = first["plan_id"]
    parent = read_weekly_plan_revision(config, pid)
    block = parent.blocks[0]
    with pytest.raises(ValueError, match="BLOCK_BEFORE_SOURCE_AS_OF"):
        move_weekly_plan_block(config, pid, block.block_id,
            datetime(2026, 10, 19, 7, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="INVALID_BLOCK_START"):
        move_weekly_plan_block(config, pid, block.block_id,
            datetime(2026, 10, 19, 9), expected_revision_id=first["revision_id"])
    for minutes in (0, -1, True, 1.5, 10**100):
        with pytest.raises(ValueError, match="INVALID_BLOCK_DURATION"):
            set_weekly_plan_block_duration(config, pid, block.block_id, minutes,
                expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="OUTSIDE_LEGAL_AVAILABILITY"):
        move_weekly_plan_block(config, pid, block.block_id,
            datetime(2026, 10, 19, 12, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="UNKNOWN_WEEKLY_PLAN_REVISION"):
        remove_weekly_plan_block(config, pid, block.block_id, expected_revision_id="absent")
    edit = remove_weekly_plan_block(config, pid, block.block_id,
        expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="REVISION_CHANGED"):
        remove_weekly_plan_block(config, pid, block.block_id,
            expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="UNKNOWN_UNSCHEDULED_ACTION"):
        place_weekly_plan_action(config, pid, "foreign", datetime(2026, 10, 19, 9, tzinfo=ZONE),
            expected_revision_id=edit["revision_id"])
    assert read_weekly_plan_revision(config, pid).unscheduled_actions[0].primary_reason == "USER_REMOVED"
    with factory() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM weekly_plan_revisions").scalar_one() == 2


def test_place_missing_duration_and_manual_readback_tamper(setup):
    config, factory = setup
    update_planning_profile(config, "SYNTH", default_action_duration_minutes=None)
    first = create(config)
    pid = first["plan_id"]
    parent = read_weekly_plan_revision(config, pid)
    item = parent.unscheduled_actions[0]
    assert item.primary_reason == "MISSING_DURATION"
    with pytest.raises(ValueError, match="INVALID_BLOCK_DURATION"):
        place_weekly_plan_action(config, pid, item.action_id,
            datetime(2026, 10, 19, 9, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    placed = place_weekly_plan_action(config, pid, item.action_id,
        datetime(2026, 10, 19, 9, tzinfo=ZONE), duration_minutes=45,
        expected_revision_id=first["revision_id"])
    body = read_weekly_plan_revision(config, pid)
    assert body.blocks[0].source_scheduler_block_id is None
    assert body.blocks[0].duration_source == "MANUAL_EDIT"
    assert body.blocks[0].placement_source == "MANUAL_EDIT"
    assert placed["revision_number"] == 2
    with factory.begin() as db:
        root = db.get(WeeklyPlan, pid)
        row = db.get(WeeklyPlanRevision, placed["revision_id"])
        _, calendar, schedule, _ = service._load_verified(config, db, root, row)
        tampered = body.model_copy(update={"blocks": [body.blocks[0].model_copy(
            update={"course_stable_id": "FOREIGN"})]})
        with pytest.raises(ValueError, match="SOURCE_ARTIFACT_INVALID"):
            validate_weekly_plan_layout(tampered, calendar, schedule)
        artifact = service._publish(config, db, canonical_weekly_plan_revision_json(tampered),
            "WEEKLY_PLAN_REVISION_JSON", [])
        row.revision_artifact_id = artifact.id
        row.revision_payload_sha256 = artifact.sha256
    with pytest.raises(ValueError, match="SOURCE_ARTIFACT_INVALID"):
        read_weekly_plan_revision(config, pid)


def test_pure_transformer_is_deterministic_and_rejects_two_edits(setup):
    config, factory = setup
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        db.add(Skill(id="SECOND", name="Second", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SECOND"))
    first = create(config)
    parent = read_weekly_plan_revision(config, first["plan_id"])
    with factory() as db:
        root = db.get(WeeklyPlan, first["plan_id"])
        row = db.get(WeeklyPlanRevision, first["revision_id"])
        _, calendar, schedule, _ = service._load_verified(config, db, root, row)
    at = datetime(2026, 10, 14, 12, tzinfo=ZONE)
    a = transform_move_block(parent, "fixed-revision", 2, at, parent.blocks[0].block_id,
        datetime(2026, 10, 19, 11, tzinfo=ZONE))
    b = transform_move_block(parent, "fixed-revision", 2, at, parent.blocks[0].block_id,
        datetime(2026, 10, 19, 11, tzinfo=ZONE))
    assert canonical_weekly_plan_revision_json(a) == canonical_weekly_plan_revision_json(b)
    validate_weekly_plan_layout(a, calendar, schedule)
    verify_single_edit(parent, a, schedule)
    extra = a.blocks[0].model_copy(update={"duration_minutes": 30,
        "ends_at": datetime(2026, 10, 19, 10, 30, tzinfo=ZONE), "duration_source": "MANUAL_EDIT"})
    changed_two = a.model_copy(update={"blocks": [extra, *a.blocks[1:]]})
    with pytest.raises(ValueError, match="SOURCE_ARTIFACT_INVALID"):
        verify_single_edit(parent, changed_two, schedule)


def test_manual_edit_cli_requires_revision_token(setup):
    config, _ = setup
    first = create(config)
    pid = first["plan_id"]
    block = read_weekly_plan_revision(config, pid).blocks[0]
    runner = CliRunner()
    missing = runner.invoke(app, ["weekly-plan-remove-block", pid, block.block_id,
        "--project-root", str(config.project_root)])
    assert missing.exit_code != 0
    result = runner.invoke(app, ["weekly-plan-remove-block", pid, block.block_id,
        "--revision-id", first["revision_id"], "--project-root", str(config.project_root)])
    assert result.exit_code == 0, result.output
    assert read_weekly_plan_revision(config, pid).unscheduled_actions[0].primary_reason == "USER_REMOVED"


def test_layout_validator_conflict_types_and_daily_cap(setup):
    config, factory = setup
    first = create(config)
    parent = read_weekly_plan_revision(config, first["plan_id"])
    with factory() as db:
        root = db.get(WeeklyPlan, first["plan_id"])
        row = db.get(WeeklyPlanRevision, first["revision_id"])
        _, calendar, schedule, _ = service._load_verified(config, db, root, row)
    candidate = transform_move_block(parent, "candidate", 2,
        datetime(2026, 10, 14, 12, tzinfo=ZONE), parent.blocks[0].block_id,
        datetime(2026, 10, 19, 10, tzinfo=ZONE))
    blocked = ConstraintOccurrenceV1(calendar_constraint_id="personal", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 19, 10, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 11, tzinfo=ZONE), all_day=False)
    with pytest.raises(ValueError, match="PERSONAL_BLOCK_CONFLICT"):
        validate_weekly_plan_layout(candidate, calendar.model_copy(update={
            "constraint_occurrences": [blocked]}), schedule)
    academic = AcademicOccurrenceV1(occurrence_id="academic", course_id="course",
        course_stable_id="COURSE", source_kind="RECURRING", local_date=MON,
        starts_at=datetime(2026, 10, 19, 10, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 11, tzinfo=ZONE),
        information_status="CONFIRMED", participation="REQUIRED", hard_block=True)
    with pytest.raises(ValueError, match="ACADEMIC_BLOCK_CONFLICT"):
        validate_weekly_plan_layout(candidate, calendar.model_copy(update={
            "academic_occurrences": [academic]}), schedule)
    with pytest.raises(ValueError, match="DAILY_LIMIT_EXCEEDED"):
        validate_weekly_plan_layout(candidate, calendar.model_copy(update={
            "max_daily_study_minutes": 30}), schedule)
    with pytest.raises(ValueError, match="OUTSIDE_LEGAL_AVAILABILITY"):
        validate_weekly_plan_layout(candidate, calendar.model_copy(update={
            "availability_occurrences": []}), schedule)


def test_manual_move_dst_fold_and_nonexistent_wall_time(setup):
    config, _ = setup
    add_availability_window(config, "SYNTH", weekday="SUNDAY",
        start_local_time=time(1), end_local_time=time(4))
    first = create(config)
    parent = read_weekly_plan_revision(config, first["plan_id"])
    moved = move_weekly_plan_block(config, first["plan_id"], parent.blocks[0].block_id,
        datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=0),
        expected_revision_id=first["revision_id"])
    block = read_weekly_plan_revision(config, first["plan_id"]).blocks[0]
    assert block.starts_at.isoformat().endswith("+02:00")
    assert block.ends_at.isoformat().endswith("+01:00")
    assert block.duration_minutes == 60
    with pytest.raises(ValueError, match="INVALID_BLOCK_START"):
        move_weekly_plan_block(config, first["plan_id"], block.block_id,
            datetime(2026, 3, 29, 2, 30, tzinfo=ZONE), expected_revision_id=moved["revision_id"])


def test_regeneration_keeps_original_query_and_not_manual_duration(setup):
    config, factory = setup
    action_id = plan_next_actions(config, "SYNTH", as_of=BEFORE).recommendations[0].action_id
    course_id = "COURSE"
    first = create(config, course_id=course_id, limit=1, durations={action_id: 45})
    pid = first["plan_id"]
    original = read_weekly_plan_revision(config, pid)
    manual = set_weekly_plan_block_duration(config, pid, original.blocks[0].block_id, 30,
        expected_revision_id=first["revision_id"])
    edited = read_weekly_plan_revision(config, pid)
    assert edited.source.duration_overrides == {action_id: 45}
    regenerated = regenerate_weekly_plan(config, pid, as_of=BEFORE)
    result = read_weekly_plan_revision(config, pid)
    assert regenerated["revision_number"] == 3
    assert result.parent_revision_id == manual["revision_id"]
    assert result.revision_kind == "GENERATED"
    assert result.blocks[0].duration_minutes == 45
    assert result.source.source_course_filter_id == course_id
    assert result.source.source_limit == 1
    assert result.source.duration_overrides == {action_id: 45}


@pytest.mark.parametrize("date_only", [False, True])
def test_manual_move_respects_captured_deadline(setup, date_only):
    config, factory = setup
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        db.add(Deadline(course_id=course.id, title="Due", status=DeadlineStatus.OPEN,
            information_status=InformationStatus.CONFIRMED,
            due_at=(datetime(2026, 10, 20) if date_only else datetime(2026, 10, 19, 11)),
            is_date_only=date_only))
    add_availability_window(config, "SYNTH", weekday="TUESDAY",
        start_local_time=time(9), end_local_time=time(12))
    first = create(config)
    block = read_weekly_plan_revision(config, first["plan_id"]).blocks[0]
    assert block.deadline_constraints
    new_start = (datetime(2026, 10, 20, 9, tzinfo=ZONE) if date_only else
        datetime(2026, 10, 19, 10, 30, tzinfo=ZONE))
    with pytest.raises(ValueError, match="DEADLINE_VIOLATION"):
        move_weekly_plan_block(config, first["plan_id"], block.block_id, new_start,
            expected_revision_id=first["revision_id"])


def test_manual_move_rejects_plan_overlap_and_allows_adjacency(setup):
    config, factory = setup
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        db.add(Skill(id="SECOND", name="Second", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SECOND"))
    first = create(config)
    pid = first["plan_id"]
    parent = read_weekly_plan_revision(config, pid)
    with pytest.raises(ValueError, match="PLAN_BLOCK_OVERLAP"):
        move_weekly_plan_block(config, pid, parent.blocks[0].block_id,
            datetime(2026, 10, 19, 10, 30, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    moved = move_weekly_plan_block(config, pid, parent.blocks[0].block_id,
        datetime(2026, 10, 19, 11, tzinfo=ZONE), expected_revision_id=first["revision_id"])
    assert moved["revision_number"] == 2


def test_manual_move_week_and_local_midnight_bounds(setup):
    config, _ = setup
    add_calendar_constraint(config, "SYNTH", constraint_type="AVAILABLE_ADDITION",
        starts_at=datetime(2026, 10, 20, 23, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 21, 1, tzinfo=ZONE))
    first = create(config)
    pid = first["plan_id"]
    block = read_weekly_plan_revision(config, pid).blocks[0]
    with pytest.raises(ValueError, match="BLOCK_CROSSES_MIDNIGHT"):
        move_weekly_plan_block(config, pid, block.block_id,
            datetime(2026, 10, 20, 23, 30, tzinfo=ZONE),
            expected_revision_id=first["revision_id"])
    with pytest.raises(ValueError, match="BLOCK_OUTSIDE_WEEK"):
        move_weekly_plan_block(config, pid, block.block_id,
            datetime(2026, 10, 26, 9, tzinfo=ZONE),
            expected_revision_id=first["revision_id"])


def test_duration_change_validates_overlap_and_daily_cap(setup):
    config, factory = setup
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "COURSE"))
        db.add(Skill(id="SECOND", name="Second", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SECOND"))
    first = create(config)
    pid = first["plan_id"]
    block = read_weekly_plan_revision(config, pid).blocks[0]
    with pytest.raises(ValueError, match="PLAN_BLOCK_OVERLAP"):
        set_weekly_plan_block_duration(config, pid, block.block_id, 90,
            expected_revision_id=first["revision_id"])
    update_planning_profile(config, "SYNTH", max_daily_study_minutes=120)
    # New source capture is required to make this limit part of edit geometry.
    regenerated = regenerate_weekly_plan(config, pid, as_of=BEFORE)
    second = read_weekly_plan_revision(config, pid).blocks[1]
    removed = remove_weekly_plan_block(config, pid, second.block_id,
        expected_revision_id=regenerated["revision_id"])
    current = read_weekly_plan_revision(config, pid).blocks[0]
    with pytest.raises(ValueError, match="DAILY_LIMIT_EXCEEDED"):
        set_weekly_plan_block_duration(config, pid, current.block_id, 130,
            expected_revision_id=removed["revision_id"])
