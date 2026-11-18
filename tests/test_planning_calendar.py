"""Offline Phase 3E2B1 calendar foundation checks."""
from datetime import date, datetime, time, timedelta, timezone
import json

import pytest
from sqlalchemy import select
from sqlalchemy import text
from alembic import command
from alembic.config import Config
from pathlib import Path

from uniflow.application.initializer import initialize_project
from uniflow.application.planning_calendar import (
    add_availability_window, add_calendar_constraint, create_planning_profile,
    deactivate_availability_window, deactivate_calendar_constraint,
    planning_profile_info, project_academic_week, project_calendar_week,
    resolve_local, update_planning_profile,
)
from uniflow.domain.enums import InformationStatus, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    CalendarConstraint, Course, LearningSession, MasteryState, PlanningProfile, ScheduleEntry,
    ScheduleException, Semester,
)
from uniflow.schemas.calendar_v1 import canonical_calendar_json
from zoneinfo import ZoneInfo

MON = date(2026, 10, 12)


@pytest.fixture
def setup(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
                            timezone="Europe/Berlin")
        course = Course(stable_id="MATH_SYNTH", name="Math", semester=semester,
                        participation="REQUIRED", information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        course_id = course.id
    return config, factory, course_id


def entry(factory, course_id, stable_id="MON", **changes):
    payload = {"stable_id": stable_id, "activity_type": "LECTURE", "weekday": "MONDAY",
               "start_time": "10:00", "end_time": "12:00", "participation": "REQUIRED",
               "information_status": "CONFIRMED"}
    payload.update(changes)
    with factory.begin() as db:
        row = ScheduleEntry(stable_id=stable_id, course_id=course_id,
            payload_json=json.dumps(payload), information_status=InformationStatus(payload["information_status"]))
        db.add(row)
        db.flush()
        return row.id


def exception(factory, course_id, stable_id, kind, day=MON, **changes):
    payload = {"stable_id": stable_id, "date": day.isoformat(), "event_type": kind,
               "title": "Title has no authority", "replaces_schedule_id": "MON"}
    payload.update(changes)
    with factory.begin() as db:
        row = ScheduleException(stable_id=stable_id, course_id=course_id,
            payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED)
        db.add(row)
        db.flush()


def test_profile_revision_and_personal_projection(setup):
    config, factory, _ = setup
    assert project_calendar_week(config, "SYNTH", MON).planning_profile_id is None
    assert project_calendar_week(config, "SYNTH", MON).diagnostics[0].code == "NO_PLANNING_PROFILE"
    p = create_planning_profile(config, "SYNTH", default_action_duration_minutes=45)
    assert p["revision"] == 1
    with pytest.raises(ValueError, match="PROFILE_ALREADY_EXISTS"):
        create_planning_profile(config, "SYNTH")
    assert update_planning_profile(config, "SYNTH", default_action_duration_minutes=45)["revision"] == 1
    assert update_planning_profile(config, "SYNTH", default_action_duration_minutes=None,
        max_daily_study_minutes=180)["revision"] == 2
    with pytest.raises(ValueError, match="INVALID_PROFILE_MINUTES"):
        update_planning_profile(config, "SYNTH", max_daily_study_minutes=0)
    with pytest.raises(ValueError, match="INVALID_AVAILABILITY_WINDOW"):
        add_availability_window(config, "SYNTH", weekday="MONDAY", start_local_time=time(22), end_local_time=time(1))
    w = add_availability_window(config, "SYNTH", weekday="MONDAY", start_local_time=time(9), end_local_time=time(15))
    assert w["created_revision"] == 3
    c = add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 12, 12, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        ends_at=datetime(2026, 10, 12, 13, 30, tzinfo=ZoneInfo("Europe/Berlin")), label="private")
    assert c["created_revision"] == 4
    a = add_calendar_constraint(config, "SYNTH", constraint_type="AVAILABLE_ADDITION",
        local_date=date(2026, 10, 16))
    assert a["created_revision"] == 5
    result = project_calendar_week(config, "SYNTH", MON)
    assert len(result.availability_occurrences) == 1
    assert len(result.constraint_occurrences) == 2
    assert canonical_calendar_json(result) == canonical_calendar_json(project_calendar_week(config, "SYNTH", MON))
    with factory.begin() as db:
        row = db.get(CalendarConstraint, c["id"])
        row.label = "changed private label"
    assert project_calendar_week(config, "SYNTH", MON).context_fingerprint == result.context_fingerprint
    assert deactivate_availability_window(config, w["id"]) == "DEACTIVATED"
    assert deactivate_availability_window(config, w["id"]) == "ALREADY_INACTIVE"
    assert deactivate_calendar_constraint(config, a["id"]) == "DEACTIVATED"
    assert planning_profile_info(config, "SYNTH")["revision"] == 7
    assert not project_calendar_week(config, "SYNTH", MON).availability_occurrences


def test_academic_exceptions_and_authority(setup):
    config, factory, course_id = setup
    entry(factory, course_id)
    initial = project_calendar_week(config, "SYNTH", MON)
    assert len(initial.academic_occurrences) == 1
    assert initial.academic_occurrences[0].hard_block
    exception(factory, course_id, "CANCEL", "CANCELLATION")
    cancelled = project_calendar_week(config, "SYNTH", MON)
    assert not cancelled.academic_occurrences
    assert cancelled.context_fingerprint != initial.context_fingerprint
    with factory.begin() as db:
        db.scalar(select(ScheduleException).where(ScheduleException.stable_id == "CANCEL")).information_status = InformationStatus.INFERRED
    assert project_academic_week(config, "SYNTH", MON)[0].hard_block
    with factory.begin() as db:
        db.scalar(select(ScheduleException).where(ScheduleException.stable_id == "CANCEL")).information_status = InformationStatus.CONFIRMED
        db.delete(db.scalar(select(ScheduleException).where(ScheduleException.stable_id == "CANCEL")))
    exception(factory, course_id, "REPLACE", "REPLACEMENT", start_time="13:00", end_time="14:00")
    replaced = project_academic_week(config, "SYNTH", MON)
    assert len(replaced) == 1 and replaced[0].source_kind == "REPLACEMENT" and replaced[0].hard_block
    exception(factory, course_id, "ADD", "ADDITION", replaces_schedule_id=None,
              start_time="16:00", end_time="17:00")
    added = project_academic_week(config, "SYNTH", MON)
    assert len(added) == 2 and not next(x for x in added if x.source_kind == "ADDITION").hard_block
    exception(factory, course_id, "CONFLICT", "CANCELLATION")
    with pytest.raises(ValueError, match="SCHEDULE_EXCEPTION_CONFLICT"):
        project_academic_week(config, "SYNTH", MON)


def test_optional_unknown_missing_end_and_week(setup):
    config, factory, course_id = setup
    entry(factory, course_id, participation="UNKNOWN", end_time=None)
    result = project_academic_week(config, "SYNTH", MON)
    assert not result[0].hard_block
    assert result[0].diagnostics[0].code == "MISSING_END_TIME"
    with pytest.raises(ValueError, match="INVALID_WEEK_START"):
        project_calendar_week(config, "SYNTH", MON + timedelta(days=1))
    with factory() as db:
        assert db.query(LearningSession).count() == db.query(MasteryState).count() == 0


def test_dst_resolution_and_all_day(setup):
    zone = ZoneInfo("Europe/Berlin")
    with pytest.raises(ValueError, match="NONEXISTENT_PERSONAL_LOCAL_TIME"):
        resolve_local(date(2026, 3, 29), time(2, 30), zone)
    with pytest.raises(ValueError, match="AMBIGUOUS_PERSONAL_LOCAL_TIME"):
        resolve_local(date(2026, 10, 25), time(2, 30), zone)
    first = resolve_local(date(2026, 10, 25), time(2, 30), zone, 0)
    second = resolve_local(date(2026, 10, 25), time(2, 30), zone, 1)
    assert second.astimezone(timezone.utc) - first.astimezone(timezone.utc) == timedelta(hours=1)
    config, _, _ = setup
    create_planning_profile(config, "SYNTH")
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED", local_date=date(2026, 10, 25))
    fall = project_calendar_week(config, "SYNTH", date(2026, 10, 19)).constraint_occurrences[0]
    assert fall.ends_at.astimezone(timezone.utc) - fall.starts_at.astimezone(timezone.utc) == timedelta(hours=25)
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED", local_date=date(2026, 3, 29))
    spring = project_calendar_week(config, "SYNTH", date(2026, 3, 23)).constraint_occurrences[0]
    assert spring.ends_at.astimezone(timezone.utc) - spring.starts_at.astimezone(timezone.utc) == timedelta(hours=23)


def test_populated_0013_upgrade_preserves_canonical_rows(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "populated.sqlite3"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "0013_mastery_calculation")
    with get_engine(path).begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,timezone,created_at,updated_at) VALUES ('s','S','Semester','ACTIVE','Europe/Berlin',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO courses (id,semester_id,stable_id,name,kind,participation,unknowns_json,information_status,created_at,updated_at) VALUES ('c','s','C','Course','MODULE','UNKNOWN','[]','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO schedule_entries (id,stable_id,course_id,payload_json,information_status,created_at,updated_at) VALUES ('e','E','c','{}','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO schedule_exceptions (id,stable_id,course_id,payload_json,information_status,created_at,updated_at) VALUES ('x','X','c','{}','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO learning_sessions (id,course_id,goal,state,created_at,updated_at) VALUES ('l','c','Goal','PLANNED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO skills (id,name,kind,status,created_at,updated_at) VALUES ('k','Skill','CONCEPT','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO mastery_states (id,skill_id,value,calculated_at,calculation_method,created_at,updated_at) VALUES ('m','k',0.5,CURRENT_TIMESTAMP,'synthetic',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(cfg, "head")
    with get_engine(path).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert len(conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()) == 56
        for table in ("semesters", "courses", "schedule_entries", "schedule_exceptions", "learning_sessions", "mastery_states"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 1
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []


def test_synthetic_foundation_closed_example(setup):
    config, factory, course_id = setup
    entry(factory, course_id)
    entry(factory, course_id, "WED", weekday="WEDNESDAY", start_time="14:00",
          end_time="16:00", participation="OPTIONAL")
    create_planning_profile(config, "SYNTH")
    add_availability_window(config, "SYNTH", weekday="MONDAY",
        start_local_time=time(9), end_local_time=time(15))
    add_availability_window(config, "SYNTH", weekday="WEDNESDAY",
        start_local_time=time(9), end_local_time=time(18))
    add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 12, 12, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        ends_at=datetime(2026, 10, 12, 13, 30, tzinfo=ZoneInfo("Europe/Berlin")))
    add_calendar_constraint(config, "SYNTH", constraint_type="AVAILABLE_ADDITION",
        starts_at=datetime(2026, 10, 16, 10, tzinfo=ZoneInfo("Europe/Berlin")),
        ends_at=datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("Europe/Berlin")))
    initial = project_calendar_week(config, "SYNTH", MON)
    assert len(initial.academic_occurrences) == 2
    assert sum(x.hard_block for x in initial.academic_occurrences) == 1
    assert len(initial.availability_occurrences) == len(initial.constraint_occurrences) == 2
    assert canonical_calendar_json(initial) == canonical_calendar_json(project_calendar_week(config, "SYNTH", MON))
    exception(factory, course_id, "CANCEL", "CANCELLATION")
    cancelled = project_calendar_week(config, "SYNTH", MON)
    assert len(cancelled.academic_occurrences) == 1
    assert not cancelled.academic_occurrences[0].hard_block
    assert cancelled.context_fingerprint != initial.context_fingerprint
    with factory.begin() as db:
        db.delete(db.scalar(select(ScheduleException).where(ScheduleException.stable_id == "CANCEL")))
    exception(factory, course_id, "REPLACE", "REPLACEMENT", start_time="11:00", end_time="13:00")
    replacement = project_calendar_week(config, "SYNTH", MON)
    assert next(x for x in replacement.academic_occurrences if x.source_kind == "REPLACEMENT").hard_block
    with factory() as db:
        assert db.query(LearningSession).count() == db.query(MasteryState).count() == 0


def test_academic_parity_validity_status_and_course_nonprecedence(setup):
    config, factory, course_id = setup
    entry(factory, course_id, "ODD", calendar_week_parity="ODD", participation="REQUIRED")
    entry(factory, course_id, "EVEN", calendar_week_parity="EVEN", participation="UNKNOWN")
    entry(factory, course_id, "LATER", valid_from="2026-10-19", participation="REQUIRED")
    entry(factory, course_id, "EXPIRED", valid_until="2026-10-11", participation="REQUIRED")
    entry(factory, course_id, "OBSERVED", information_status="OBSERVED", participation="REQUIRED")
    projected = project_academic_week(config, "SYNTH", MON)  # ISO week 42, even.
    assert {x.schedule_entry_stable_id for x in projected} == {"EVEN", "OBSERVED"}
    assert not any(x.hard_block for x in projected)
    next_week = project_academic_week(config, "SYNTH", MON + timedelta(days=7))
    assert {x.schedule_entry_stable_id for x in next_week} == {"ODD", "LATER", "OBSERVED", "EVEN"} - {"EVEN"}
    assert sum(x.hard_block for x in next_week) == 2


def test_academic_dst_and_unsupported_exception(setup):
    config, factory, course_id = setup
    entry(factory, course_id, "SUN", weekday="SUNDAY", start_time="02:30", end_time="03:30")
    with pytest.raises(ValueError, match="AMBIGUOUS_ACADEMIC_LOCAL_TIME"):
        project_academic_week(config, "SYNTH", date(2026, 10, 19))
    with factory.begin() as db:
        row = db.scalar(select(ScheduleEntry).where(ScheduleEntry.stable_id == "SUN"))
        payload = json.loads(row.payload_json)
        payload["start_time"] = "04:00"
        payload["end_time"] = "05:00"
        row.payload_json = json.dumps(payload)
    exception(factory, course_id, "OTHER", "moved", day=MON)
    result = project_calendar_week(config, "SYNTH", MON)
    assert any(d.code == "UNSUPPORTED_SCHEDULE_EXCEPTION_TYPE" for d in result.diagnostics)


def test_constraint_validation(setup):
    config, _, _ = setup
    create_planning_profile(config, "SYNTH")
    with pytest.raises(ValueError, match="INVALID_CONSTRAINT_SHAPE"):
        add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
            starts_at=datetime(2026, 10, 12, tzinfo=timezone.utc), local_date=MON)
    with pytest.raises(ValueError, match="AWARE_DATETIME_REQUIRED"):
        add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
            starts_at=datetime(2026, 10, 12), ends_at=datetime(2026, 10, 13))
    with pytest.raises(ValueError, match="INVALID_CONSTRAINT_INTERVAL"):
        add_calendar_constraint(config, "SYNTH", constraint_type="BLOCKED",
            starts_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert planning_profile_info(config, "SYNTH")["revision"] == 1


def test_fold_order_uses_instants(setup):
    config, _, _ = setup
    create_planning_profile(config, "SYNTH")
    add_availability_window(config, "SYNTH", weekday="SUNDAY",
        start_local_time=time(2, 15), end_local_time=time(2, 30),
        start_fold=1, end_fold=0,
        valid_from=date(2026, 10, 25), valid_until=date(2026, 10, 25))
    with pytest.raises(ValueError, match="INVALID_AVAILABILITY_WINDOW"):
        project_calendar_week(config, "SYNTH", date(2026, 10, 19))
