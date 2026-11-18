"""Offline, temporary-state checks for the read-only weekly scheduler."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from uniflow.application.initializer import initialize_project
from uniflow.application.planning import plan_next_actions
from uniflow.application.planning_calendar import (
    add_availability_window, add_calendar_constraint, create_planning_profile,
    project_calendar_week,
)
from uniflow.application.weekly_scheduler import (
    _split_days, _subtract, _union, schedule_captured_week, schedule_week,
)
from uniflow.cli.main import _parse_duration_overrides, app
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Deadline, LearningSession, LearningSessionTargetSkill,
    PlanningProfile, ScheduleEntry, Semester, Skill,
)
from uniflow.schemas.calendar_v1 import (
    AcademicOccurrenceV1, AvailabilityOccurrenceV1, CalendarDiagnosticV1,
    CalendarWeekProjectionV1, ConstraintOccurrenceV1, canonical_calendar_json,
)
from uniflow.schemas.planning_v1 import (
    PlanningActionV1, PlanningDeadlineV1, PlanningMasteryV1,
    PlanningRecommendationV1, PlanningSemesterV1, PlanningSourceV1,
    canonical_planning_json,
)
from uniflow.schemas.weekly_schedule_v1 import canonical_weekly_schedule_json

ZONE = ZoneInfo("Europe/Berlin")
MON = date(2026, 10, 12)
BEFORE = datetime(2026, 10, 12, 8, tzinfo=ZONE)


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def action(rank: int, *, kind="START_SKILL_SESSION", deadlines=None, targets=None):
    targets = targets or [f"SKILL_{rank}"]
    return PlanningActionV1(action_id=f"nba:action-{rank}", rank=rank, action_type=kind,
        priority_class=0 if kind == "RESUME_SESSION" else 2,
        course_id="course", course_stable_id="COURSE", target_skill_ids=targets,
        existing_session_id=f"session-{rank}" if kind == "RESUME_SESSION" else None,
        existing_session_state="PLANNED" if kind == "RESUME_SESSION" else None,
        target_mastery=None if kind == "RESUME_SESSION" else PlanningMasteryV1(skill_id=targets[0], status="UNKNOWN"),
        prerequisite_context=[], deadline_context=deadlines or [],
        source_status=None if kind == "RESUME_SESSION" else PlanningSourceV1(
            status="NO_REVIEWED_SOURCE", current_reviewed_source_available=False,
            historical_reviewed_source_available=False, provenance_ids=[], material_ids=[]),
        reasons=[], risk_factors=[])


def deadline(*, day=15, hour=None, actionable=True, identity="d"):
    return PlanningDeadlineV1(id=identity, stable_id=identity, relevance="COURSE", course_id="course",
        assessment_id=None, material_id=None, information_status="CONFIRMED", status="OPEN",
        is_date_only=hour is None, due_date=date(2026, 10, day) if hour is None else None,
        due_at=dt(day, hour) if hour is not None else None,
        timing="UPCOMING" if actionable else "OVERDUE", actionable=actionable)


def nba(actions=None, *, as_of=BEFORE):
    actions = actions if actions is not None else [action(1)]
    return PlanningRecommendationV1(as_of=as_of,
        semester=PlanningSemesterV1(id="semester", stable_id="SYNTH_WEEK", name="Synthetic",
            timezone="Europe/Berlin", state="ACTIVE"), course_filter_id=None,
        status="OK" if actions else "NO_ELIGIBLE_ACTIONS", context_fingerprint="1" * 64,
        recommendations=actions, diagnostics=[])


def available(day=12, start=9, end=12, *, minute=0):
    return AvailabilityOccurrenceV1(availability_window_id=f"w-{day}-{start}-{minute}",
        local_date=date(2026, 10, day), starts_at=dt(day, start, minute), ends_at=dt(day, end))


def constraint(kind, day=12, start=10, end=11):
    return ConstraintOccurrenceV1(calendar_constraint_id=f"c-{kind}-{day}-{start}",
        constraint_type=kind, starts_at=dt(day, start), ends_at=dt(day, end), all_day=False)


def academic(*, hard=True, day=12, start=10, end=11):
    return AcademicOccurrenceV1(occurrence_id=f"academic-{day}-{start}", course_id="course",
        course_stable_id="COURSE", source_kind="RECURRING", local_date=date(2026, 10, day),
        starts_at=dt(day, start), ends_at=dt(day, end), information_status="CONFIRMED",
        participation="REQUIRED" if hard else "OPTIONAL", hard_block=hard)


def calendar(*, windows=None, constraints=None, academics=None, profile=True,
             default=None, cap=None, week=MON):
    return CalendarWeekProjectionV1(week_start=week, week_end_exclusive=week + timedelta(days=7),
        timezone="Europe/Berlin", semester_id="semester", semester_stable_id="SYNTH_WEEK",
        planning_profile_id="profile" if profile else None, profile_revision=1 if profile else None,
        default_action_duration_minutes=default, max_daily_study_minutes=cap,
        availability_occurrences=windows if windows is not None else ([available()] if profile else []),
        constraint_occurrences=constraints or [], academic_occurrences=academics or [],
        diagnostics=[CalendarDiagnosticV1(code="NO_PLANNING_PROFILE")] if not profile else [],
        context_fingerprint="2" * 64)


def run(*, actions=None, cal=None, durations=None, as_of=BEFORE):
    return schedule_captured_week(nba(actions, as_of=as_of), cal or calendar(), durations)


def starts(result):
    return [(b.nba_rank, b.starts_at.strftime("%a %H:%M"), b.ends_at.strftime("%a %H:%M"))
            for b in result.scheduled_blocks]


def test_core_replay_provenance_and_exact_placement():
    a = nba([action(1), action(2)])
    c = calendar(windows=[available(12, 9, 10), available(12, 10, 12)])
    x = schedule_captured_week(a, c, {"nba:action-1": 60, "nba:action-2": 90})
    y = schedule_captured_week(a, c, {"nba:action-2": 90, "nba:action-1": 60})
    assert starts(x) == [(1, "Mon 09:00", "Mon 10:00"), (2, "Mon 10:00", "Mon 11:30")]
    assert x.status == "OK" and canonical_weekly_schedule_json(x) == canonical_weekly_schedule_json(y)
    assert x.nba_payload_sha256 == hashlib.sha256(canonical_planning_json(a)).hexdigest()
    assert x.calendar_payload_sha256 == hashlib.sha256(canonical_calendar_json(c)).hexdigest()
    assert all(b.block_id.startswith("weekly-block:") for b in x.scheduled_blocks)
    changed = schedule_captured_week(a, c, {"nba:action-1": 61, "nba:action-2": 90})
    assert changed.context_fingerprint != x.context_fingerprint
    assert changed.scheduled_blocks[0].block_id != x.scheduled_blocks[0].block_id


def test_interval_union_subtraction_and_half_open():
    z = timezone.utc
    t = datetime(2026, 10, 12, tzinfo=z)
    m = lambda n: t + timedelta(minutes=n)
    assert _union([(m(60), m(120)), (m(0), m(60)), (m(90), m(150))]) == [(m(0), m(150))]
    assert _subtract([(m(0), m(150))], _union([(m(30), m(60)), (m(60), m(90))])) == [
        (m(0), m(30)), (m(90), m(150))]


def test_additions_blocks_academic_and_order_independent_placement():
    c = calendar(windows=[available(12, 9, 11)], constraints=[
        constraint("AVAILABLE_ADDITION", 12, 11, 13), constraint("BLOCKED", 12, 10, 11)],
        academics=[academic(hard=True, start=11, end=12), academic(hard=False, start=9, end=10)])
    x = run(cal=c, durations={"nba:action-1": 60})
    assert starts(x) == [(1, "Mon 09:00", "Mon 10:00")]
    c2 = c.model_copy(update={"constraint_occurrences": list(reversed(c.constraint_occurrences))})
    assert starts(run(cal=c2, durations={"nba:action-1": 60})) == starts(x)
    extra = run(cal=calendar(windows=[], constraints=[constraint("AVAILABLE_ADDITION", 16, 10, 12)]),
                durations={"nba:action-1": 60})
    assert starts(extra) == [(1, "Fri 10:00", "Fri 11:00")]


def test_week_as_of_and_elapsed():
    c = calendar(windows=[available(12, 9, 12)])
    exact = run(cal=c, durations={"nba:action-1": 60}, as_of=dt(12, 9, 7))
    assert exact.scheduled_blocks[0].starts_at == dt(12, 9, 7)
    future = run(cal=c, durations={"nba:action-1": 60}, as_of=dt(11, 12))
    assert future.scheduled_blocks[0].starts_at == dt(12, 9)
    elapsed = run(cal=c, durations={"nba:action-1": 60}, as_of=dt(19, 0))
    assert elapsed.status == "NO_SCHEDULED_ACTIONS"
    assert elapsed.scheduler_diagnostics[0].code == "WEEK_ELAPSED"
    assert elapsed.unscheduled_actions[0].primary_reason == "NO_AVAILABLE_WINDOW"


def test_midnight_and_dst_elapsed_minutes():
    start = datetime(2026, 10, 24, 23, tzinfo=ZONE)
    end = datetime(2026, 10, 25, 4, tzinfo=ZONE)
    c = calendar(week=date(2026, 10, 19), windows=[], constraints=[ConstraintOccurrenceV1(
        calendar_constraint_id="night", constraint_type="AVAILABLE_ADDITION",
        starts_at=start, ends_at=end, all_day=False)])
    x = run(cal=c, durations={"nba:action-1": 180}, as_of=dt(19, 8))
    assert x.scheduled_blocks[0].starts_at.date() == date(2026, 10, 25)
    assert x.scheduled_blocks[0].ends_at.date() == date(2026, 10, 25)
    assert x.scheduled_blocks[0].ends_at.astimezone(timezone.utc) - x.scheduled_blocks[0].starts_at.astimezone(timezone.utc) == timedelta(minutes=180)
    assert len(_split_days([(start.astimezone(timezone.utc), end.astimezone(timezone.utc))], date(2026, 10, 19), ZONE)) == 2
    spring_start = datetime(2026, 3, 29, 0, tzinfo=ZONE)
    spring_end = datetime(2026, 3, 30, 0, tzinfo=ZONE)
    assert spring_end.astimezone(timezone.utc) - spring_start.astimezone(timezone.utc) == timedelta(hours=23)
    spring = calendar(week=date(2026, 3, 23), windows=[], constraints=[ConstraintOccurrenceV1(
        calendar_constraint_id="spring", constraint_type="AVAILABLE_ADDITION",
        starts_at=spring_start, ends_at=datetime(2026, 3, 29, 4, tzinfo=ZONE), all_day=False)])
    spring_result = run(cal=spring, durations={"nba:action-1": 180}, as_of=spring_start)
    assert spring_result.scheduled_blocks[0].ends_at.hour == 4
    assert spring_result.scheduled_blocks[0].duration_minutes == 180


@pytest.mark.parametrize("kwargs,error", [
    ({"durations": {"wrong": 60}}, "UNKNOWN_DURATION_ACTION"),
    ({"durations": {"nba:action-1": 0}}, "INVALID_ACTION_DURATION"),
    ({"durations": {"nba:action-1": -1}}, "INVALID_ACTION_DURATION"),
    ({"durations": {"nba:action-1": True}}, "INVALID_ACTION_DURATION"),
    ({"durations": {"nba:action-1": 1.5}}, "INVALID_ACTION_DURATION"),
])
def test_invalid_durations(kwargs, error):
    with pytest.raises(ValueError, match=error):
        run(**kwargs)


def test_duration_precedence_missing_large_and_resume():
    c = calendar(default=45)
    x = run(cal=c, durations={"nba:action-1": 60})
    assert x.scheduled_blocks[0].duration_source == "EXPLICIT_OVERRIDE"
    assert run(cal=c).scheduled_blocks[0].duration_source == "PROFILE_DEFAULT"
    assert run(cal=calendar()).unscheduled_actions[0].primary_reason == "MISSING_DURATION"
    huge = run(durations={"nba:action-1": 10**100})
    assert huge.unscheduled_actions[0].primary_reason == "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL"
    resume = run(actions=[action(1, kind="RESUME_SESSION", targets=["A", "B"])],
                 durations={"nba:action-1": 45})
    assert resume.scheduled_blocks[0].duration_minutes == 45
    assert resume.scheduled_blocks[0].existing_session_id == "session-1"


def test_no_profile_no_window_and_reason_priority():
    x = run(cal=calendar(profile=False), durations={"nba:action-1": 60})
    assert x.unscheduled_actions[0].primary_reason == "NO_PLANNING_PROFILE"
    assert x.calendar_diagnostics[0].code == "NO_PLANNING_PROFILE"
    assert run(cal=calendar(profile=False)).unscheduled_actions[0].primary_reason == "NO_PLANNING_PROFILE"
    assert run(cal=calendar(windows=[]), durations={"nba:action-1": 60}).unscheduled_actions[0].primary_reason == "NO_AVAILABLE_WINDOW"


def test_daily_cap_greedy_and_unscheduled_order():
    actions = [action(1), action(2), action(3)]
    c = calendar(windows=[available(12, 9, 15), available(14, 9, 12)], cap=120)
    x = run(actions=actions, cal=c, durations={"nba:action-1": 60,
                                                 "nba:action-2": 90, "nba:action-3": 10**6})
    assert starts(x) == [(1, "Mon 09:00", "Mon 10:00"), (2, "Wed 09:00", "Wed 10:30")]
    assert x.status == "PARTIAL"
    assert [u.nba_rank for u in x.unscheduled_actions] == [3]
    assert run(cal=calendar(cap=30), durations={"nba:action-1": 60}).unscheduled_actions[0].primary_reason == "DAILY_LIMIT_REACHED"


def test_deadline_rules_and_no_late_fallback():
    c = calendar(windows=[available(12, 9, 12), available(15, 9, 12)])
    timed = action(1, deadlines=[deadline(day=12, hour=10)])
    assert starts(run(actions=[timed], cal=c, durations={"nba:action-1": 60})) == [(1, "Mon 09:00", "Mon 10:00")]
    assert run(actions=[timed], cal=c, durations={"nba:action-1": 61}).unscheduled_actions[0].primary_reason == "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL"
    date_only = action(1, deadlines=[deadline(day=12)])
    assert run(actions=[date_only], cal=c, durations={"nba:action-1": 60}).unscheduled_actions[0].primary_reason == "NO_PRE_DEADLINE_SLOT"
    overdue = action(1, deadlines=[deadline(day=12, actionable=False)])
    assert run(actions=[overdue], cal=c, durations={"nba:action-1": 60}).status == "OK"
    mixed = action(1, deadlines=[deadline(day=15), deadline(day=12, hour=9, identity="timed")])
    assert run(actions=[mixed], cal=c, durations={"nba:action-1": 60}).unscheduled_actions[0].primary_reason == "NO_PRE_DEADLINE_SLOT"


def test_deadline_before_week_after_week_and_malformed():
    before = action(1, deadlines=[deadline(day=11)])
    assert run(actions=[before], durations={"nba:action-1": 60}).unscheduled_actions[0].primary_reason == "NO_PRE_DEADLINE_SLOT"
    after = action(1, deadlines=[deadline(day=19)])
    assert starts(run(actions=[after], durations={"nba:action-1": 60})) == [(1, "Mon 09:00", "Mon 10:00")]
    bad_date = deadline().model_copy(update={"due_date": None})
    bad_time = deadline(hour=12).model_copy(update={"due_at": None})
    for bad in (bad_date, bad_time):
        with pytest.raises(ValueError, match="INVALID_ACTIONABLE_DEADLINE"):
            run(actions=[action(1, deadlines=[bad])], durations={"nba:action-1": 60})


def test_higher_rank_failure_does_not_block_lower_and_no_backtracking():
    actions = [action(1), action(2), action(3)]
    c = calendar(windows=[available(12, 9, 10), available(12, 11, 12)])
    x = run(actions=actions, cal=c, durations={"nba:action-1": 90,
        "nba:action-2": 60, "nba:action-3": 60})
    assert x.unscheduled_actions[0].nba_rank == 1
    assert x.unscheduled_actions[0].primary_reason == "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL"
    assert starts(x) == [(2, "Mon 09:00", "Mon 10:00"), (3, "Mon 11:00", "Mon 12:00")]
    y = run(actions=[action(1), action(2)],
        cal=calendar(windows=[available(12, 9, 10), available(12, 10, 11)]),
        durations={"nba:action-1": 60, "nba:action-2": 90})
    assert starts(y) == [(1, "Mon 09:00", "Mon 10:00")]
    assert y.unscheduled_actions[0].primary_reason == "NO_FREE_INTERVAL"


def test_chronological_output_can_differ_from_placement_rank():
    x = run(actions=[action(1), action(2)],
        cal=calendar(windows=[available(12, 9, 10), available(14, 9, 11)]),
        durations={"nba:action-1": 90, "nba:action-2": 45})
    assert [b.nba_rank for b in x.scheduled_blocks] == [2, 1]
    assert starts(x) == [(2, "Mon 09:00", "Mon 09:45"), (1, "Wed 09:00", "Wed 10:30")]


def test_occupancy_only_yields_no_free_interval():
    x = run(cal=calendar(windows=[available(12, 9, 10)],
                         constraints=[constraint("BLOCKED", 12, 9, 10)]),
            durations={"nba:action-1": 30})
    assert x.unscheduled_actions[0].primary_reason == "NO_FREE_INTERVAL"


def test_empty_nba_status_and_calendar_diagnostic_preservation():
    c = calendar(profile=False)
    x = schedule_captured_week(nba([]), c)
    assert x.status == "NO_ELIGIBLE_ACTIONS" and not x.scheduled_blocks and not x.unscheduled_actions
    assert x.calendar_diagnostics[0].code == "NO_PLANNING_PROFILE"


def test_daily_capacity_counts_only_generated_study():
    c = calendar(windows=[available(12, 9, 13)],
        constraints=[constraint("BLOCKED", 12, 10, 11)],
        academics=[academic(start=11, end=12)], cap=60)
    x = run(cal=c, durations={"nba:action-1": 60})
    assert x.status == "OK" and starts(x) == [(1, "Mon 09:00", "Mon 10:00")]
    exact = run(actions=[action(1), action(2)], cal=calendar(cap=120),
                durations={"nba:action-1": 60, "nba:action-2": 60})
    assert exact.status == "OK"
    exhausted = run(actions=[action(1), action(2)], cal=calendar(cap=60),
                    durations={"nba:action-1": 60, "nba:action-2": 60})
    assert exhausted.unscheduled_actions[0].primary_reason == "DAILY_LIMIT_REACHED"


def test_input_contract_revalidation():
    base_nba, base_cal = nba(), calendar()
    cases = [
        (base_nba.model_copy(update={"policy_version": "wrong"}), base_cal, "UNSUPPORTED_NBA_CONTRACT"),
        (base_nba, base_cal.model_copy(update={"policy_version": "wrong"}), "UNSUPPORTED_CALENDAR_CONTRACT"),
        (base_nba, base_cal.model_copy(update={"semester_id": "other"}), "SCHEDULER_SEMESTER_MISMATCH"),
        (base_nba, base_cal.model_copy(update={"semester_stable_id": "other"}), "SCHEDULER_SEMESTER_MISMATCH"),
        (base_nba, base_cal.model_copy(update={"timezone": "UTC"}), "SCHEDULER_SEMESTER_MISMATCH"),
        (base_nba, base_cal.model_copy(update={"week_end_exclusive": MON + timedelta(days=8)}), "INVALID_CALENDAR_WEEK"),
        (base_nba, base_cal.model_copy(update={"profile_revision": None}), "INVALID_PROFILE_SNAPSHOT"),
        (base_nba.model_copy(update={"as_of": datetime(2026, 10, 12)}), base_cal, "INVALID_SCHEDULER_SNAPSHOT"),
        (base_nba.model_copy(update={"recommendations": [action(1), action(1)]}), base_cal, "INVALID_SCHEDULER_SNAPSHOT|INVALID_NBA_ACTIONS"),
        (base_nba.model_copy(update={"recommendations": [action(1), action(2).model_copy(update={"action_id": "nba:action-1"})]}),
         base_cal, "INVALID_NBA_ACTIONS"),
    ]
    for n, c, error in cases:
        with pytest.raises(ValueError, match=error):
            schedule_captured_week(n, c, {"nba:action-1": 60})
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        with pytest.raises(ValueError, match="INVALID_SCHEDULER_SNAPSHOT"):
            schedule_captured_week(base_nba,
                base_cal.model_copy(update={"default_action_duration_minutes": 60.0}),
                {"nba:action-1": 60})


def test_cli_duration_parser():
    assert _parse_duration_overrides(["nba:abc=37"]) == {"nba:abc": 37}
    with pytest.raises(ValueError, match="DUPLICATE_DURATION_ACTION"):
        _parse_duration_overrides(["nba:abc=37", "nba:abc=37"])
    for bad in ("=30", "nba:abc=0", "nba:abc=1.5", "nba:abc=-1", "nba:abc=٣", "nba:abc"):
        with pytest.raises(ValueError, match="INVALID_ACTION_DURATION"):
            _parse_duration_overrides([bad])


def test_orchestration_calls_each_producer_once(monkeypatch):
    import uniflow.application.planning as planning
    import uniflow.application.planning_calendar as planning_calendar
    calls = []
    captured_nba, captured_calendar = nba(), calendar()
    def fake_nba(*args, **kwargs):
        calls.append(("nba", kwargs["as_of"]))
        return captured_nba
    def fake_calendar(*args, **kwargs):
        calls.append(("calendar", args[2]))
        return captured_calendar
    monkeypatch.setattr(planning, "plan_next_actions", fake_nba)
    monkeypatch.setattr(planning_calendar, "project_calendar_week", fake_calendar)
    result = schedule_week(object(), "SYNTH_WEEK", MON, as_of=BEFORE, durations={"nba:action-1": 60})
    assert calls == [("nba", BEFORE), ("calendar", MON)]
    assert result.nba_payload_sha256 == hashlib.sha256(canonical_planning_json(captured_nba)).hexdigest()


def test_temporary_integration_and_zero_writes(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        sem = Semester(stable_id="SYNTH_WEEK", name="Synthetic", state=SemesterState.ACTIVE,
                       timezone="Europe/Berlin")
        courses = [Course(stable_id=f"COURSE_{sid}", name=sid, semester=sem,
                          information_status=InformationStatus.CONFIRMED) for sid in ("A", "B", "C")]
        db.add_all(courses)
        db.flush()
        for sid in ("A", "B", "C"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        for sid, course in zip(("A", "B", "C"), courses):
            db.add(CourseSkill(course_id=course.id, skill_id=sid))
        db.add_all([
            Deadline(course_id=courses[0].id, title="Timed", status=DeadlineStatus.OPEN,
                     information_status=InformationStatus.CONFIRMED,
                     due_at=datetime(2026, 10, 13, 12), is_date_only=False),
            Deadline(course_id=courses[1].id, title="Date", status=DeadlineStatus.OPEN,
                     information_status=InformationStatus.CONFIRMED,
                     due_at=datetime(2026, 10, 15), is_date_only=True),
            ScheduleEntry(stable_id="MON_BLOCK", course_id=courses[0].id,
                payload_json=json.dumps({"stable_id": "MON_BLOCK", "activity_type": "LECTURE",
                    "weekday": "MONDAY", "start_time": "10:00", "end_time": "12:00",
                    "participation": "REQUIRED", "information_status": "CONFIRMED"}),
                information_status=InformationStatus.CONFIRMED),
        ])
    create_planning_profile(config, "SYNTH_WEEK", max_daily_study_minutes=120)
    add_availability_window(config, "SYNTH_WEEK", weekday="MONDAY", start_local_time=time(9), end_local_time=time(15))
    add_availability_window(config, "SYNTH_WEEK", weekday="WEDNESDAY", start_local_time=time(9), end_local_time=time(18))
    add_calendar_constraint(config, "SYNTH_WEEK", constraint_type="BLOCKED", starts_at=dt(12, 12, 30), ends_at=dt(12, 13, 30))
    add_calendar_constraint(config, "SYNTH_WEEK", constraint_type="AVAILABLE_ADDITION", starts_at=dt(16, 10), ends_at=dt(16, 12))
    before = {}
    tables = ("learning_sessions", "learning_handoffs", "learning_results", "evidence", "mastery_states",
              "planning_profiles", "availability_windows", "calendar_constraints", "schedule_entries",
              "schedule_exceptions", "deadlines", "derived_artifacts")
    with factory() as db:
        for table in tables:
            before[table] = db.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar_one()
    captured = plan_next_actions(config, "SYNTH_WEEK", as_of=BEFORE)
    projected = project_calendar_week(config, "SYNTH_WEEK", MON)
    assert len(captured.recommendations) == 3
    assert sum(o.hard_block for o in projected.academic_occurrences) == 1
    durations = {a.action_id: 60 if a.rank == 1 else 90 for a in captured.recommendations[:2]}
    result = schedule_captured_week(captured, projected, durations)
    assert result.status == "PARTIAL"
    assert starts(result) == [(1, "Mon 09:00", "Mon 10:00"), (2, "Wed 09:00", "Wed 10:30")]
    assert result.unscheduled_actions[0].primary_reason == "MISSING_DURATION"
    assert canonical_weekly_schedule_json(result) == canonical_weekly_schedule_json(schedule_captured_week(captured, projected, durations))
    with factory() as db:
        after = {table: db.connection().exec_driver_sql("SELECT count(*) FROM " + table).scalar_one() for table in tables}
    assert before == after


def test_realistic_resume_ranks_first_in_temporary_project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        sem = Semester(stable_id="SYNTH_WEEK", name="Synthetic", state=SemesterState.ACTIVE, timezone="Europe/Berlin")
        course = Course(stable_id="COURSE", name="Course", semester=sem,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        db.add_all([Skill(id="A", name="A", kind="CONCEPT", status="ACTIVE"),
                    Skill(id="B", name="B", kind="CONCEPT", status="ACTIVE")])
        db.flush()
        db.add_all([CourseSkill(course_id=course.id, skill_id="A"), CourseSkill(course_id=course.id, skill_id="B")])
        db.flush()
        session = LearningSession(id="SESSION_A", course_id=course.id, goal="Resume A", state=LearningSessionState.PLANNED)
        db.add(session)
        db.flush()
        db.add(LearningSessionTargetSkill(learning_session_id=session.id, skill_id="A", ordinal=1))
    create_planning_profile(config, "SYNTH_WEEK", default_action_duration_minutes=45)
    add_availability_window(config, "SYNTH_WEEK", weekday="MONDAY", start_local_time=time(9), end_local_time=time(12))
    captured = plan_next_actions(config, "SYNTH_WEEK", as_of=BEFORE)
    assert [a.action_type for a in captured.recommendations] == ["RESUME_SESSION", "START_SKILL_SESSION"]
    result = schedule_captured_week(captured, project_calendar_week(config, "SYNTH_WEEK", MON))
    assert [b.action_type for b in result.scheduled_blocks] == ["RESUME_SESSION", "START_SKILL_SESSION"]
    assert result.scheduled_blocks[0].existing_session_id == session.id


def test_cli_read_only_empty_temp_project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(stable_id="SYNTH_WEEK", name="Synthetic", state=SemesterState.ACTIVE,
                        timezone="Europe/Berlin"))
    result = CliRunner().invoke(app, ["schedule-week", "--semester", "SYNTH_WEEK",
        "--week-start", "2026-10-12", "--as-of", BEFORE.isoformat(),
        "--project-root", str(config.project_root)])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "NO_ELIGIBLE_ACTIONS"
    assert body["scheduled_blocks"] == []
