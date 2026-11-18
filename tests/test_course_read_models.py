from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select
from sqlalchemy import event
from sqlalchemy.engine import Engine

from test_weekly_block_materialization import project as plan_project, run as materialize_block

from uniflow.application.course_read_models import get_course_overview
from uniflow.application.material_inbox_read_models import get_material_inbox
from uniflow.application.session_inbox_read_models import get_session_inbox
from uniflow.application.change_feed_read_models import get_change_feed
from uniflow.application.week_read_models import get_week_view
from uniflow.application.weekly_plans import regenerate_weekly_plan
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, AssessmentAttempt, AssessmentRequirement, Course, CourseSkill,
    Deadline, LearningSession, LearningSessionTargetSkill, MasteryState,
    Material, MaterialArtifact, OfficialResult, Semester, Skill, SkillDependency,
    WeeklyPlan, WeeklyPlanRevision, WeeklyPlanAcceptance,
)
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.schemas.course_read_v1 import canonical_course_overview_json
from uniflow.schemas.course_read_v1 import (
    CourseMasteryReadV1, CourseOverviewV1, CourseSkillReadV1,
    DirectPrerequisiteV1,
)
from uniflow.schemas.change_feed_read_v1 import ChangeEventV1


AT = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def requirement_payload(stable_id, *, assessment_stable_id="EXAM",
                        requirement_type="presentations", parameters=None,
                        human_summary="Exactly 2 presentations"):
    return json.dumps({"stable_id": stable_id, "information_status": "CONFIRMED",
        "provenance": None, "requirement_type": requirement_type,
        "parameters": {"minimum": 2} if parameters is None else parameters,
        "human_summary": human_summary,
        "assessment_stable_id": assessment_stable_id}, ensure_ascii=False)


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add_all([
            Semester(id="sem", stable_id="S", name="Semester", state=SemesterState.ACTIVE,
                starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="Europe/Berlin"),
            Semester(id="other", stable_id="T", name="Other", state=SemesterState.ACTIVE,
                timezone="Europe/Berlin"),
            Course(id="a", semester_id="sem", stable_id="A", name="Alpha"),
            Course(id="b", semester_id="sem", stable_id="B", name="Beta"),
            Course(id="c", semester_id="other", stable_id="C", name="Gamma"),
        ])
    yield config, factory
    factory.kw["bind"].dispose()


def read(config, course="A", **kwargs):
    return get_course_overview(config, "S", course, as_of=AT,
        activity_since=AT-timedelta(days=1), activity_through=AT, **kwargs)


def test_input_and_empty_course(project):
    config, _ = project
    value = read(config)
    assert value.course.course_id == "a"
    assert value.semester.stable_id == "S"
    assert not value.skills and not value.materials and not value.accepted_course_blocks
    assert canonical_course_overview_json(value) == canonical_course_overview_json(read(config))
    for limit in (1, 200):
        assert read(config, activity_limit=limit).activity_limit == limit
    for limit in (0, 201, True):
        with pytest.raises(ValueError, match="INVALID_ACTIVITY_LIMIT"):
            read(config, activity_limit=limit)
    for limit in (1, 100):
        assert read(config, completed_limit=limit).completed_limit == limit
    for limit in (0, 101, True):
        with pytest.raises(ValueError, match="INVALID_COMPLETED_LIMIT"):
            read(config, completed_limit=limit)
    for course in ("missing", "C"):
        with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
            read(config, course)
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_course_overview(config, "missing", "A", as_of=AT,
            activity_since=AT-timedelta(days=1), activity_through=AT)
    for name in ("as_of", "activity_since", "activity_through"):
        args = dict(as_of=AT, activity_since=AT-timedelta(days=1), activity_through=AT)
        args[name] = args[name].replace(tzinfo=None)
        with pytest.raises(ValueError, match="MUST_BE_AWARE"):
            get_course_overview(config, "S", "A", **args)
    for since, through in ((AT, AT), (AT, AT-timedelta(days=1)),
                           (AT-timedelta(days=1), AT+timedelta(seconds=1))):
        with pytest.raises(ValueError, match="INVALID_COURSE_ACTIVITY_RANGE"):
            get_course_overview(config, "S", "A", as_of=AT,
                activity_since=since, activity_through=through)


def test_skills_facts_and_scope(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([Skill(id="prereq", name="Prerequisite"), Skill(id="known", name="Known"),
            Skill(id="unknown", name="Unknown"), Skill(id="foreign", name="Foreign")])
        db.flush()
        db.add_all([CourseSkill(id="a-known", course_id="a", skill_id="known", is_active=True),
            CourseSkill(id="a-unknown", course_id="a", skill_id="unknown", is_active=True),
            CourseSkill(id="b-foreign", course_id="b", skill_id="foreign", is_active=True),
            CourseSkill(id="inactive", course_id="a", skill_id="foreign", is_active=False),
            SkillDependency(id="edge", prerequisite_skill_id="prereq", dependent_skill_id="known", is_active=True),
            MasteryState(id="old", skill_id="known", value=0.7, calculated_at=AT-timedelta(days=1),
                calculation_method="manual"),
            MasteryState(id="latest", skill_id="known", value=0.0, calculated_at=AT,
                calculation_method="manual")])
    value = read(config)
    assert [s.skill_id for s in value.skills] == ["known", "unknown"]
    assert value.skills[0].mastery.mastery_status == "KNOWN"
    assert value.skills[0].mastery.value == 0.0
    assert value.skills[0].mastery.mastery_state_id == "latest"
    assert value.skills[1].mastery.mastery_status == "UNKNOWN"
    assert value.skills[1].mastery.value is None
    assert value.skills[0].direct_prerequisites[0].prerequisite_skill_id == "prereq"
    assert all(s.reviewed_source_status == "NONE" for s in value.skills)
    assert b'"readiness"' not in canonical_course_overview_json(value)


def test_requirements_are_rules_and_foreign_corruption_isolated(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([
            Assessment(id="assessment", stable_id="EXAM", course_id="a", name="Exam"),
            Assessment(id="foreign", stable_id="OTHER", course_id="b", name="Other"),
            AssessmentAttempt(id="attempt", assessment_id="assessment", attempt_number=1,
                score=0.0, grade=None, passed=None),
            AssessmentRequirement(id="req", stable_id="REQ", course_id="a",
                payload_json=requirement_payload("REQ"),
                information_status=InformationStatus.CONFIRMED),
            AssessmentRequirement(id="tasks", stable_id="TASKS", course_id="a",
                payload_json=requirement_payload("TASKS", requirement_type="tasks",
                    parameters={"minimum": 9}, human_summary="Exactly 9 tasks"),
                information_status=InformationStatus.CONFIRMED),
            AssessmentRequirement(id="bad", stable_id="BAD", course_id="b", payload_json="invalid",
                information_status=InformationStatus.CONFIRMED),
            OfficialResult(id="result", course_id="a", assessment_id="assessment",
                status="RECORDED", grade="2.0", credits=5.0),
            Deadline(id="due", course_id="a", assessment_id="assessment", title="Due",
                due_at=datetime(2026, 10, 16), is_date_only=True),
        ])
    value = read(config)
    assert value.assessments[0].attempts[0].passed is None
    assert {r.progress_status for r in value.requirements} == {"UNKNOWN"}
    assert {r.assessment_id for r in value.requirements} == {"assessment"}
    assert {r.human_summary for r in value.requirements} == {"Exactly 2 presentations", "Exactly 9 tasks"}
    assert b'"0/2"' not in canonical_course_overview_json(value)
    assert b'"0/9"' not in canonical_course_overview_json(value)
    assert value.official_results[0].status == "RECORDED"
    assert value.deadlines[0].relevance_kinds == ["COURSE", "ASSESSMENT"]
    assert value.deadlines[0].due_at is None
    assert value.deadlines[0].due_date == date(2026, 10, 16)
    with factory.begin() as db:
        db.get(AssessmentRequirement, "req").payload_json = "invalid"
    with pytest.raises(ValueError, match="REQUIREMENT_PAYLOAD_INVALID"):
        read(config)


def test_one_query_only_transaction_and_zero_writes(project):
    config, factory = project
    counts = {}
    classes = (CourseSkill, Skill, MasteryState, Material, MaterialArtifact,
        Assessment, AssessmentAttempt, AssessmentRequirement, OfficialResult,
        LearningSession, WeeklyPlan, WeeklyPlanRevision, WeeklyPlanAcceptance)
    with factory() as db:
        for cls in classes:
            counts[cls] = len(list(db.scalars(select(cls))))
    observed = []

    def statement(conn, cursor, statement, parameters, context, executemany):
        if statement == "PRAGMA query_only=ON" or statement == "BEGIN":
            observed.append((id(conn), statement))

    event.listen(Engine, "before_cursor_execute", statement)
    try:
        read(config)
    finally:
        event.remove(Engine, "before_cursor_execute", statement)
    assert [s for _, s in observed] == ["PRAGMA query_only=ON", "BEGIN"]
    assert len({cid for cid, _ in observed}) == 1
    with factory() as db:
        assert {cls: len(list(db.scalars(select(cls)))) for cls in classes} == counts


def test_material_and_session_detail_reuse(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([Skill(id="skill", name="Skill"),
            Material(id="material", course_id="a", stable_id="MAT", title="Notes"),
            Material(id="foreign-material", course_id="b", stable_id="MAT-B", title="Other"),
            LearningSession(id="session", course_id="a", goal="Study",
                state=LearningSessionState.PLANNED, created_at=AT),
            LearningSession(id="foreign-session", course_id="b", goal="Other",
                state=LearningSessionState.PLANNED, created_at=AT)])
        db.flush()
        db.add_all([LearningSessionTargetSkill(id="target", learning_session_id="session",
            skill_id="skill", ordinal=0),
            LearningSessionTargetSkill(id="foreign-target", learning_session_id="foreign-session",
            skill_id="skill", ordinal=0)])
    overview = read(config)
    inbox = get_material_inbox(config, "S", as_of=AT,
        from_date=date(2026, 10, 1), through_date=date(2026, 10, 31), course_id="A")
    sessions = get_session_inbox(config, "S", as_of=AT, course_id="A")
    assert overview.materials == inbox.materials
    assert overview.resumable_sessions == sessions.resumable_sessions
    assert [m.material_id for m in overview.materials] == ["material"]
    assert [s.session_id for s in overview.resumable_sessions] == ["session"]


def test_all_deadline_relevance_paths_and_sibling_exclusion(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([Assessment(id="assessment", course_id="a", name="Exam"),
            Material(id="material", course_id="a", title="Notes"),
            Assessment(id="sibling-assessment", course_id="b", name="Other"),
            Material(id="sibling-material", course_id="b", title="Other")])
        db.flush()
        db.add_all([
            Deadline(id="course", course_id="a", title="Course due",
                due_at=datetime(2026, 10, 16), is_date_only=True,
                information_status=InformationStatus.CONFIRMED, status=DeadlineStatus.OPEN),
            Deadline(id="assessment-due", assessment_id="assessment", title="Exam due",
                due_at=datetime(2026, 10, 14), is_date_only=True,
                information_status=InformationStatus.CONFIRMED, status=DeadlineStatus.OPEN),
            Deadline(id="material-due", material_id="material", title="Notes due",
                due_at=datetime(2026, 10, 17), is_date_only=True,
                information_status=InformationStatus.CONFIRMED, status=DeadlineStatus.OPEN),
            Deadline(id="all", course_id="a", assessment_id="assessment",
                material_id="material", title="All", due_at=datetime(2026, 10, 18),
                is_date_only=True, information_status=InformationStatus.CONFIRMED,
                status=DeadlineStatus.OPEN),
            Deadline(id="sibling", course_id="b", assessment_id="sibling-assessment",
                material_id="sibling-material", title="Other"),
        ])
    value = read(config)
    assert {d.deadline_id for d in value.deadlines} == {"course", "assessment-due", "material-due", "all"}
    assert next(d for d in value.deadlines if d.deadline_id == "all").relevance_kinds == [
        "COURSE", "ASSESSMENT", "MATERIAL"]
    assert next(d for d in value.deadlines if d.deadline_id == "assessment-due").timing == "OVERDUE_OPEN"


def test_accepted_plan_and_activity_match_committed_reads(plan_project):
    cfg, factory, result, body = plan_project
    at = datetime(2026, 10, 19, 11, tzinfo=timezone.utc)
    since = at - timedelta(days=1)

    def overview():
        return get_course_overview(cfg, "SYNTH", "C", as_of=at,
            activity_since=since, activity_through=at)

    initial = overview()
    week = get_week_view(cfg, "SYNTH", date(2026, 10, 19), as_of=at)
    assert initial.plan.accepted_revision_id == result["revision_id"]
    assert initial.accepted_course_blocks == week.accepted_plan.blocks
    assert len(initial.accepted_course_blocks) == 2
    assert all(b.materialization.status == "NOT_MATERIALIZED" for b in initial.accepted_course_blocks)
    materialize_block(plan_project)
    regenerate_weekly_plan(cfg, result["plan_id"], as_of=at)
    changed = overview()
    week = get_week_view(cfg, "SYNTH", date(2026, 10, 19), as_of=at)
    feed = get_change_feed(cfg, "SYNTH", as_of=at, since=since,
        through=at, course_id="C")
    assert changed.plan.accepted_is_current is False
    assert changed.accepted_course_blocks == week.accepted_plan.blocks
    assert changed.accepted_course_blocks != week.current_draft.blocks
    assert any(b.materialization.status == "MATERIALIZED" for b in changed.accepted_course_blocks)
    assert changed.activity == feed.events


@pytest.mark.parametrize("change", [
    {"human_summary": None},
    {"requirement_type": None},
    {"requirement_type": {}},
    {"requirement_type": []},
    {"requirement_type": ""},
    {"parameters": []},
])
def test_invalid_requirement_payload_fields(project, change):
    config, factory = project
    with factory.begin() as db:
        db.add(Assessment(id="assessment", stable_id="EXAM", course_id="a", name="Exam"))
        payload = json.loads(requirement_payload("REQ"))
        payload.update(change)
        db.add(AssessmentRequirement(id="req", stable_id="REQ", course_id="a",
            payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))
    with pytest.raises(ValueError, match="REQUIREMENT_PAYLOAD_INVALID"):
        read(config)


@pytest.mark.parametrize("missing", ["human_summary", "requirement_type"])
def test_missing_requirement_fields(project, missing):
    config, factory = project
    payload = json.loads(requirement_payload("REQ"))
    payload.pop(missing)
    with factory.begin() as db:
        db.add(AssessmentRequirement(id="req", stable_id="REQ", course_id="a",
            payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))
    with pytest.raises(ValueError, match="REQUIREMENT_PAYLOAD_INVALID"):
        read(config)


def test_requirement_parameters_and_assessment_integrity(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([Assessment(id="assessment", stable_id="EXAM", course_id="a", name="Exam"),
            Assessment(id="foreign", stable_id="FOREIGN", course_id="b", name="Other"),
            AssessmentRequirement(id="req", stable_id="REQ", course_id="a",
                payload_json=requirement_payload("REQ", human_summary="  Keep this exactly.  "),
                information_status=InformationStatus.CONFIRMED)])
    item = read(config).requirements[0]
    assert item.requirement_type == "presentations"
    assert item.parameters_json == '{"minimum":2}'
    assert item.human_summary == "  Keep this exactly.  "
    assert item.assessment_id == "assessment" and item.progress_status == "UNKNOWN"
    for stable in ("MISSING", "FOREIGN"):
        with factory.begin() as db:
            db.get(AssessmentRequirement, "req").payload_json = requirement_payload(
                "REQ", assessment_stable_id=stable)
        with pytest.raises(ValueError, match="REQUIREMENT_ASSESSMENT_INVALID"):
            read(config)
    for payload in (
            requirement_payload("REQ", parameters={"bad": float("nan")}),
            requirement_payload("REQ", parameters={"large": "x" * 17000})):
        with factory.begin() as db:
            db.get(AssessmentRequirement, "req").payload_json = payload
        with pytest.raises(ValueError, match="REQUIREMENT_"):
            read(config)


def test_direct_prerequisite_identity_uniqueness():
    base = dict(course_skill_id="link", skill_id="skill", name="Skill", kind="CONCEPT",
        status="ACTIVE", reviewed_source_status="NONE", reviewed_source_material_ids=[],
        reviewed_source_repository_ids=[], mastery=CourseMasteryReadV1(mastery_status="UNKNOWN"))
    first = DirectPrerequisiteV1(dependency_id="one", prerequisite_skill_id="A")
    assert CourseSkillReadV1(**base, direct_prerequisites=[first]).direct_prerequisites == [first]
    for second in (DirectPrerequisiteV1(dependency_id="one", prerequisite_skill_id="B"),
                   DirectPrerequisiteV1(dependency_id="two", prerequisite_skill_id="A")):
        with pytest.raises(ValueError, match="PREREQUISITE_DUPLICATE"):
            CourseSkillReadV1(**base, direct_prerequisites=[first, second])


def test_deadline_order_contract(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([Deadline(id="later", course_id="a", title="Later",
                due_at=datetime(2026, 10, 20), is_date_only=True),
            Deadline(id="early", course_id="a", title="Early",
                due_at=datetime(2026, 10, 16), is_date_only=True)])
    value = read(config)
    assert [d.deadline_id for d in value.deadlines] == ["early", "later"]
    CourseOverviewV1.model_validate(value.model_dump())
    changed = value.model_dump()
    changed["deadlines"].reverse()
    with pytest.raises(ValueError, match="COURSE_DEADLINE_ORDER_INVALID"):
        CourseOverviewV1.model_validate(changed)


def test_activity_semester_coherence(project):
    config, _ = project
    value = read(config)
    event = ChangeEventV1(event_id="AUDIT_IMPORTED:row", kind="AUDIT_IMPORTED",
        occurred_at=AT-timedelta(hours=1), semester_id="sem", semester_stable_id="S",
        course_ids=["a", "b"], course_stable_ids=["A", "B"],
        entity_type="SemesterAudit", entity_id="row")
    payload = value.model_dump()
    payload["activity"] = [event.model_dump()]
    assert CourseOverviewV1.model_validate(payload).activity[0].course_ids == ["a", "b"]
    for field, foreign in (("semester_id", "other"), ("semester_stable_id", "T")):
        payload["activity"][0][field] = foreign
        with pytest.raises(ValueError, match="COURSE_ACTIVITY_SEMESTER_INVALID"):
            CourseOverviewV1.model_validate(payload)
        payload["activity"][0][field] = "sem" if field == "semester_id" else "S"


def test_corrupted_accepted_plan_artifact_fails(plan_project):
    cfg, factory, result, _ = plan_project
    at = datetime(2026, 10, 19, 11, tzinfo=timezone.utc)
    with factory.begin() as db:
        db.get(WeeklyPlanRevision, result["revision_id"]).revision_payload_sha256 = "0" * 64
    with pytest.raises(ValueError, match="SOURCE_ARTIFACT_INVALID"):
        get_course_overview(cfg, "SYNTH", "C", as_of=at,
            activity_since=at-timedelta(days=1), activity_through=at)
