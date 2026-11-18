from datetime import date, datetime, timezone
import pytest
from uniflow.application.initializer import initialize_project
from uniflow.application.session_inbox_read_models import get_session_inbox
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Semester, Course, Skill, LearningSession, LearningSessionTargetSkill
from uniflow.domain.enums import SemesterState, LearningSessionState
from uniflow.schemas.session_inbox_read_v1 import canonical_session_inbox_json

AT = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add_all([Semester(id="sem", stable_id="S", name="Semester", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="Europe/Berlin"),
            Semester(id="foreign", stable_id="F", name="Foreign", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="Europe/Berlin")])
        db.add_all([Course(id="a", semester_id="sem", stable_id="A", name="A"),
            Course(id="b", semester_id="sem", stable_id="B", name="B"),
            Course(id="f", semester_id="foreign", stable_id="F", name="F")])
        db.add(Skill(id="skill", name="Skill"))
    yield config, factory
    factory.kw["bind"].dispose()

def test_empty_inputs(project):
    config, _ = project
    value = get_session_inbox(config, "S", as_of=AT)
    assert (value.resumable_count, value.processing_count, value.completed_count) == (0, 0, 0)
    assert b'"policy_version":"session-inbox-v1"' in canonical_session_inbox_json(value)
    for limit in (1, 100):
        assert get_session_inbox(config, "S", as_of=AT, completed_limit=limit).completed_limit == limit
    for limit in (0, 101, True):
        with pytest.raises(ValueError, match="INVALID_COMPLETED_LIMIT"):
            get_session_inbox(config, "S", as_of=AT, completed_limit=limit)
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        get_session_inbox(config, "S", as_of=AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_session_inbox(config, "missing", as_of=AT)
    for course in ("missing", "F"):
        with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
            get_session_inbox(config, "S", as_of=AT, course_id=course)

def test_scope_targets_wall_time_and_no_writes(project):
    config, factory = project
    with factory.begin() as db:
        db.add_all([LearningSession(id="a1", course_id="a", goal="Study", state=LearningSessionState.PLANNED,
            planned_at=datetime(2026, 10, 25, 2, 30), created_at=AT),
            LearningSession(id="bad", course_id="b", goal="Foreign course", state=LearningSessionState.HANDOFF_CREATED,
            created_at=AT),
            LearningSession(id="unscoped", course_id=None, goal="No course", state=LearningSessionState.PLANNED,
            created_at=AT),
            LearningSession(id="foreign", course_id="f", goal="Foreign semester", state=LearningSessionState.HANDOFF_CREATED,
            created_at=AT)])
        db.add_all([LearningSessionTargetSkill(id="target", learning_session_id="a1", skill_id="skill", ordinal=0),
            LearningSessionTargetSkill(id="bad-target", learning_session_id="bad", skill_id="skill", ordinal=0)])
    value = get_session_inbox(config, "S", as_of=AT, course_id="A")
    assert value.resumable_count == 1
    assert value.resumable_sessions[0].planned_local_wall_time == "2026-10-25T02:30:00"
    assert value.resumable_sessions[0].targets[0].skill_id == "skill"
    assert "+" not in value.resumable_sessions[0].planned_local_wall_time
    with factory() as db:
        assert db.query(LearningSession).count() == 4
    with pytest.raises(ValueError, match="SESSION_HANDOFF_MISSING"):
        get_session_inbox(config, "S", as_of=AT, course_id="B")

def test_handoff_result_mastery_and_activity(project):
    import hashlib
    import json
    from uniflow.infrastructure.database.models import DerivedArtifact, LearningHandoff, LearningResult, Evidence, MasteryState
    from uniflow.domain.enums import EvidenceType
    config, factory = project
    body = json.dumps({"schema_version": "2.0", "protocol_version": "subject-learning-v1",
        "result_id": "result", "session_id": "complete", "handoff_id": "h2", "course_id": "a",
        "handoff_artifact_sha256": "a"*64, "completed_at": "2026-10-14T12:00:00+02:00",
        "session_outcome": "PARTIAL", "skill_evidence": [{"skill_id": "skill", "recall": 0.0}]},
        sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with factory.begin() as db:
        db.add_all([DerivedArtifact(id="ha", sha256="a"*64, kind="LEARNING_HANDOFF_JSON", stored_path="private", size_bytes=12),
                    DerivedArtifact(id="ra", sha256=digest, kind="LEARNING_RESULT_JSON", stored_path=str(path), size_bytes=len(body))])
        db.add(LearningSession(id="complete", course_id="a", goal="Study", state=LearningSessionState.COMPLETED,
            current_handoff_id="h2", created_at=AT))
        db.add(LearningSessionTargetSkill(id="target2", learning_session_id="complete", skill_id="skill", ordinal=0))
        db.add_all([LearningHandoff(id="h1", learning_session_id="complete", revision=1, artifact_id="ha",
            schema_version="2.0", protocol_version="subject-learning-v1", context_fingerprint_sha256="c"*64,
            generated_at=AT, supersedes_handoff_id=None),
            LearningHandoff(id="h2", learning_session_id="complete", revision=2, artifact_id="ha",
            schema_version="2.0", protocol_version="subject-learning-v1", context_fingerprint_sha256="d"*64,
            generated_at=AT, supersedes_handoff_id="h1")])
        db.add(LearningResult(id="result", learning_session_id="complete", learning_handoff_id="h2",
            artifact_id="ra", schema_version="2.0", protocol_version="subject-learning-v1",
            raw_input_sha256="e"*64, completed_at=AT, received_at=AT,
            session_outcome="PARTIAL", processing_status="MASTERY_APPLIED"))
        db.add(Evidence(id="ev", skill_id="skill", learning_session_id="complete", learning_result_id="result",
            evidence_type=EvidenceType.LEARNING_RESULT, observed_at=AT, notes="private", value_json='{"private":true}'))
    value = get_session_inbox(config, "S", as_of=AT, course_id="A")
    item = value.completed_sessions[0]
    assert [h.revision for h in item.handoff_history] == [1, 2]
    assert item.current_handoff.artifact_sha256 == "a"*64
    assert item.result.artifact_sha256 == digest
    assert item.result.completed_at.utcoffset().total_seconds() == 7200
    assert item.activity_basis == "RESULT_RECEIVED"
    assert item.mastery_application.status == "APPLIED"
    assert item.mastery_application.mastery_state_count == 0
    assert item.mastery_application.skipped_skill_ids == ["skill"]
    payload = canonical_session_inbox_json(value)
    assert b"private" not in payload and b"MASTERED" not in payload
    with factory.begin() as db:
        db.add(MasteryState(id="ms", skill_id="skill", value=0.0, calculated_at=AT,
            calculation_method="learning-result-observed-dimensions-v1", evidence_id="ev",
            learning_session_id="complete"))
    value = get_session_inbox(config, "S", as_of=AT, course_id="A")
    assert value.completed_sessions[0].mastery_application.mastery_states[0].value == 0
    assert value.completed_sessions[0].mastery_application.skipped_skill_ids == []

def test_one_query_only_transaction(project):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    config, _ = project
    connections, statements = [], []
    def checkout(dbapi_connection, connection_record, connection_proxy):
        connections.append(dbapi_connection)
    def sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(Engine, "checkout", checkout)
    event.listen(Engine, "before_cursor_execute", sql)
    try:
        get_session_inbox(config, "S", as_of=AT)
    finally:
        event.remove(Engine, "checkout", checkout)
        event.remove(Engine, "before_cursor_execute", sql)
    assert len(connections) == 1
    assert "PRAGMA query_only=ON" in statements
    assert "BEGIN" in statements
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)

def test_exact_state_groups_order_and_limit(project):
    import hashlib
    import json
    from uniflow.infrastructure.database.models import DerivedArtifact, LearningHandoff, LearningResult
    config, factory = project
    states = list(LearningSessionState)
    with factory.begin() as db:
        db.add(DerivedArtifact(id="handoff-art", sha256="f"*64, kind="LEARNING_HANDOFF_JSON",
            stored_path="private", size_bytes=1))
        for index, state in enumerate(states):
            sid = f"state-{index}"
            hid = f"handoff-{index}"
            db.add(LearningSession(id=sid, course_id="a", goal="Study", state=state,
                current_handoff_id=None if state == LearningSessionState.PLANNED else hid,
                created_at=AT))
            db.add(LearningSessionTargetSkill(id=f"target-{index}", learning_session_id=sid,
                skill_id="skill", ordinal=0))
            if state != LearningSessionState.PLANNED:
                db.add(LearningHandoff(id=hid, learning_session_id=sid, revision=1,
                    artifact_id="handoff-art", schema_version="2.0", protocol_version="subject-learning-v1",
                    context_fingerprint_sha256="c"*64, generated_at=AT, supersedes_handoff_id=None))
            if state in {LearningSessionState.RESULT_RECEIVED, LearningSessionState.VALIDATED,
                    LearningSessionState.MASTERY_UPDATED, LearningSessionState.COMPLETED}:
                body = json.dumps({"schema_version": "2.0", "protocol_version": "subject-learning-v1",
                    "result_id": f"result-{index}", "session_id": sid, "handoff_id": hid,
                    "course_id": "a", "handoff_artifact_sha256": "f"*64,
                    "completed_at": AT.isoformat(), "session_outcome": "PARTIAL",
                    "skill_evidence": []}, sort_keys=True, separators=(",", ":")).encode()
                digest = hashlib.sha256(body).hexdigest()
                path = config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                db.add(DerivedArtifact(id=f"result-art-{index}", sha256=digest,
                    kind="LEARNING_RESULT_JSON", stored_path=str(path), size_bytes=len(body)))
                db.add(LearningResult(id=f"result-{index}", learning_session_id=sid,
                    learning_handoff_id=hid, artifact_id=f"result-art-{index}",
                    schema_version="2.0", protocol_version="subject-learning-v1",
                    raw_input_sha256="e"*64, completed_at=AT, received_at=AT,
                    session_outcome="PARTIAL", processing_status=("MASTERY_APPLIED" if index == 6 else "VALIDATED")))
    value = get_session_inbox(config, "S", as_of=AT, completed_limit=1)
    assert [x.state for x in value.resumable_sessions] == ["PLANNED", "HANDOFF_CREATED", "IN_PROGRESS"]
    assert [x.state for x in value.processing_sessions] == ["RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED"]
    assert [x.state for x in value.completed_sessions] == ["COMPLETED"]
    assert (value.resumable_count, value.processing_count, value.completed_count) == (3, 3, 1)
    with factory.begin() as db:
        db.add(LearningSession(id="state-7", course_id="a", goal="More", state=LearningSessionState.COMPLETED,
            current_handoff_id="handoff-7", created_at=AT))
        db.add(LearningSessionTargetSkill(id="target-7", learning_session_id="state-7", skill_id="skill", ordinal=0))
        db.add(LearningHandoff(id="handoff-7", learning_session_id="state-7", revision=1,
            artifact_id="handoff-art", schema_version="2.0", protocol_version="subject-learning-v1",
            context_fingerprint_sha256="c"*64, generated_at=AT, supersedes_handoff_id=None))
        body = json.dumps({"schema_version": "2.0", "protocol_version": "subject-learning-v1",
            "result_id": "result-7", "session_id": "state-7", "handoff_id": "handoff-7",
            "course_id": "a", "handoff_artifact_sha256": "f"*64,
            "completed_at": AT.isoformat(), "session_outcome": "PARTIAL", "skill_evidence": []},
            sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(body).hexdigest()
        path = config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        db.add(DerivedArtifact(id="result-art-7", sha256=digest, kind="LEARNING_RESULT_JSON",
            stored_path=str(path), size_bytes=len(body)))
        db.add(LearningResult(id="result-7", learning_session_id="state-7",
            learning_handoff_id="handoff-7", artifact_id="result-art-7", schema_version="2.0",
            protocol_version="subject-learning-v1", raw_input_sha256="e"*64,
            completed_at=AT, received_at=AT, session_outcome="PARTIAL", processing_status="MASTERY_APPLIED"))
    value = get_session_inbox(config, "S", as_of=AT, completed_limit=1)
    assert value.completed_count == 2 and len(value.completed_sessions) == 1
    assert value.completed_sessions[0].session_id == "state-6"

def test_accepted_start_then_resume_same_session(tmp_path):
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import select
    from uniflow.application.planning_calendar import create_planning_profile, add_availability_window
    from uniflow.application.weekly_plans import (create_weekly_plan, read_weekly_plan_revision,
        accept_weekly_plan_revision, regenerate_weekly_plan)
    from uniflow.application.weekly_block_materialization import materialize_weekly_plan_block
    from uniflow.application.learning_handoffs import generate_handoff
    from uniflow.application.learning_results import start_learning_session
    from uniflow.infrastructure.database.models import CourseSkill, WeeklyPlanBlockExecution
    from uniflow.domain.enums import InformationStatus
    zone = ZoneInfo("Europe/Berlin")
    cfg = load_config(project_root=tmp_path / "integrated")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
            timezone="Europe/Berlin")
        course = Course(stable_id="C", name="Course", semester=semester,
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
    as_of = datetime(2026, 10, 19, 8, tzinfo=zone)
    accepted_at = datetime(2026, 10, 19, 8, 30, tzinfo=zone)
    click = datetime(2026, 10, 19, 10, 30, tzinfo=zone)
    plan = create_weekly_plan(cfg, "SYNTH", date(2026, 10, 19), as_of=as_of)
    body = read_weekly_plan_revision(cfg, plan["plan_id"])
    start_block = next(b for b in body.blocks if b.action_type == "START_SKILL_SESSION")
    accept_weekly_plan_revision(cfg, plan["plan_id"], accepted_at=accepted_at)
    created = materialize_weekly_plan_block(cfg, plan["plan_id"], start_block.block_id,
        expected_accepted_revision_id=plan["revision_id"], goal="Learn", materialized_at=click)
    assert created["execution_kind"] == "CREATED_SESSION" and created["session_state"] == "PLANNED"
    sid = created["learning_session_id"]
    generate_handoff(cfg, sid)
    assert start_learning_session(cfg, sid) == "STARTED"
    newer = regenerate_weekly_plan(cfg, plan["plan_id"], as_of=as_of)
    revised = read_weekly_plan_revision(cfg, plan["plan_id"], 2)
    resume = next(b for b in revised.blocks if b.action_type == "RESUME_SESSION" and b.existing_session_id == sid)
    accept_weekly_plan_revision(cfg, plan["plan_id"], 2, accepted_at=accepted_at)
    resumed = materialize_weekly_plan_block(cfg, plan["plan_id"], resume.block_id,
        expected_accepted_revision_id=newer["revision_id"], goal=None,
        materialized_at=click + timedelta(minutes=1))
    assert resumed["execution_kind"] == "RESUMED_SESSION"
    assert resumed["learning_session_id"] == sid
    with factory() as db:
        before = (len(list(db.scalars(select(LearningSession)))),
            len(list(db.scalars(select(WeeklyPlanBlockExecution)))))
    inbox = get_session_inbox(cfg, "SYNTH", as_of=click)
    with factory() as db:
        after = (len(list(db.scalars(select(LearningSession)))),
            len(list(db.scalars(select(WeeklyPlanBlockExecution)))))
    assert before == after == (1, 2)
    assert inbox.resumable_count == 1
    session = inbox.resumable_sessions[0]
    assert session.session_id == sid
    assert [(p.execution_id, p.execution_kind) for p in session.materialization_provenance] == [
        (created["execution_id"], "CREATED_SESSION"),
        (resumed["execution_id"], "RESUMED_SESSION")]
    assert len({p.weekly_plan_revision_id for p in session.materialization_provenance}) == 2
    third = regenerate_weekly_plan(cfg, plan["plan_id"], as_of=as_of)
    third_body = read_weekly_plan_revision(cfg, plan["plan_id"], 3)
    third_resume = next(b for b in third_body.blocks if b.action_type == "RESUME_SESSION"
        and b.existing_session_id == sid)
    accept_weekly_plan_revision(cfg, plan["plan_id"], 3, accepted_at=accepted_at)
    resumed_again = materialize_weekly_plan_block(cfg, plan["plan_id"], third_resume.block_id,
        expected_accepted_revision_id=third["revision_id"], goal=None,
        materialized_at=click + timedelta(minutes=2))
    assert resumed_again["learning_session_id"] == sid
    inbox = get_session_inbox(cfg, "SYNTH", as_of=click)
    assert [p.execution_kind for p in inbox.resumable_sessions[0].materialization_provenance] == [
        "CREATED_SESSION", "RESUMED_SESSION", "RESUMED_SESSION"]
    with factory.begin() as db:
        db.add(WeeklyPlanBlockExecution(id="forged", weekly_plan_revision_id=third["revision_id"],
            block_id="missing", action_id="missing", execution_kind="RESUMED_SESSION",
            learning_session_id=sid, materialized_at=click + timedelta(minutes=3)))
    inbox = get_session_inbox(cfg, "SYNTH", as_of=click)
    item = inbox.resumable_sessions[0]
    assert len(item.materialization_provenance) == 3
    assert [(d.code, d.refs) for d in item.diagnostics] == [
        ("MATERIALIZATION_PROVENANCE_INVALID", ["forged"])]
    factory.kw["bind"].dispose()

def _seed_validated_result(project):
    import hashlib
    import json
    from uniflow.infrastructure.database.models import DerivedArtifact, LearningHandoff, LearningResult, Evidence
    from uniflow.domain.enums import EvidenceType
    config, factory = project
    body = {"schema_version": "2.0", "protocol_version": "subject-learning-v1",
        "result_id": "r", "session_id": "s", "handoff_id": "h", "course_id": "a",
        "handoff_artifact_sha256": "a"*64, "completed_at": AT.isoformat(),
        "session_outcome": "PARTIAL", "skill_evidence": [{"skill_id": "skill", "recall": 0.0}]}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with factory.begin() as db:
        db.add_all([DerivedArtifact(id="ha", sha256="a"*64, kind="LEARNING_HANDOFF_JSON",
            stored_path="private", size_bytes=1),
            DerivedArtifact(id="ra", sha256=digest, kind="LEARNING_RESULT_JSON",
            stored_path=str(path), size_bytes=len(raw))])
        db.add(LearningSession(id="s", course_id="a", goal="Study", state=LearningSessionState.VALIDATED,
            current_handoff_id="h", created_at=AT))
        db.add(LearningSessionTargetSkill(id="t", learning_session_id="s", skill_id="skill", ordinal=0))
        db.add(LearningHandoff(id="h", learning_session_id="s", revision=1, artifact_id="ha",
            schema_version="2.0", protocol_version="subject-learning-v1",
            context_fingerprint_sha256="c"*64, generated_at=AT, supersedes_handoff_id=None))
        db.add(LearningResult(id="r", learning_session_id="s", learning_handoff_id="h",
            artifact_id="ra", schema_version="2.0", protocol_version="subject-learning-v1",
            raw_input_sha256="e"*64, completed_at=AT, received_at=AT,
            session_outcome="PARTIAL", processing_status="VALIDATED"))
        db.add(Evidence(id="e", skill_id="skill", learning_session_id="s", learning_result_id="r",
            evidence_type=EvidenceType.LEARNING_RESULT, observed_at=AT))
    return config, factory, body


def _replace_result_body(config, factory, body):
    import hashlib
    import json
    from uniflow.infrastructure.database.models import DerivedArtifact
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with factory.begin() as db:
        artifact = db.get(DerivedArtifact, "ra")
        artifact.sha256 = digest
        artifact.stored_path = str(path)
        artifact.size_bytes = len(raw)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "1.0"), ("protocol_version", "wrong"), ("session_outcome", "COMPLETED")])
def test_result_row_artifact_metadata_conflict(project, field, value):
    from uniflow.infrastructure.database.models import LearningResult
    config, factory, _ = _seed_validated_result(project)
    with factory.begin() as db:
        setattr(db.get(LearningResult, "r"), field, value)
    with pytest.raises(ValueError, match="RESULT_ARTIFACT_CONFLICT"):
        get_session_inbox(config, "S", as_of=AT)


def test_result_body_handoff_sha_conflict(project):
    config, factory, body = _seed_validated_result(project)
    body["handoff_artifact_sha256"] = "f"*64
    _replace_result_body(config, factory, body)
    with pytest.raises(ValueError, match="RESULT_ARTIFACT_CONFLICT"):
        get_session_inbox(config, "S", as_of=AT)


@pytest.mark.parametrize("case", ["extra", "missing"])
def test_result_evidence_set_conflict(project, case):
    from uniflow.infrastructure.database.models import Evidence
    from uniflow.domain.enums import EvidenceType
    config, factory, body = _seed_validated_result(project)
    if case == "extra":
        with factory.begin() as db:
            db.add(Skill(id="other", name="Other"))
            db.add(Evidence(id="extra", skill_id="other", learning_session_id="s",
                learning_result_id="r", evidence_type=EvidenceType.LEARNING_RESULT, observed_at=AT))
    else:
        with factory.begin() as db:
            db.delete(db.get(Evidence, "e"))
    with pytest.raises(ValueError, match="RESULT_EVIDENCE_INVALID"):
        get_session_inbox(config, "S", as_of=AT)


@pytest.mark.parametrize("case", ["result_received_no_result", "mastery_updated_no_result",
    "validated_no_result", "completed_no_result",
    "completed_validated_result", "not_completed_applied_result", "early_result"])
def test_result_session_state_conflict(project, case):
    from uniflow.infrastructure.database.models import LearningResult
    config, factory, _ = _seed_validated_result(project)
    with factory.begin() as db:
        session = db.get(LearningSession, "s")
        result = db.get(LearningResult, "r")
        if case.endswith("no_result"):
            session.state = {
                "result_received_no_result": LearningSessionState.RESULT_RECEIVED,
                "mastery_updated_no_result": LearningSessionState.MASTERY_UPDATED,
                "validated_no_result": LearningSessionState.VALIDATED,
                "completed_no_result": LearningSessionState.COMPLETED,
            }[case]
            db.delete(result)
        elif case == "completed_validated_result":
            session.state = LearningSessionState.COMPLETED
        elif case == "not_completed_applied_result":
            result.processing_status = "MASTERY_APPLIED"
        else:
            session.state = LearningSessionState.IN_PROGRESS
    with pytest.raises(ValueError, match="RESULT_SESSION_STATE_INVALID"):
        get_session_inbox(config, "S", as_of=AT)


def test_not_applied_rejects_f2_output(project):
    from uniflow.infrastructure.database.models import MasteryState
    config, factory, _ = _seed_validated_result(project)
    with factory.begin() as db:
        db.add(MasteryState(id="m", skill_id="skill", value=0.0, calculated_at=AT,
            calculation_method="learning-result-observed-dimensions-v1", evidence_id="e",
            learning_session_id="s"))
    with pytest.raises(ValueError, match="RESULT_MASTERY_INVALID"):
        get_session_inbox(config, "S", as_of=AT)


def test_activity_uses_latest_generated_time_not_revision(project):
    from datetime import timedelta
    from uniflow.infrastructure.database.models import DerivedArtifact, LearningHandoff
    config, factory = project
    with factory.begin() as db:
        db.add(DerivedArtifact(id="ha", sha256="a"*64, kind="LEARNING_HANDOFF_JSON",
            stored_path="private", size_bytes=1))
        db.add(LearningSession(id="s", course_id="a", goal="Study", state=LearningSessionState.IN_PROGRESS,
            current_handoff_id="h2", created_at=AT))
        db.add(LearningSessionTargetSkill(id="t", learning_session_id="s", skill_id="skill", ordinal=0))
        db.add_all([LearningHandoff(id="h1", learning_session_id="s", revision=1, artifact_id="ha",
            schema_version="2.0", protocol_version="subject-learning-v1",
            context_fingerprint_sha256="b"*64, generated_at=AT + timedelta(hours=2)),
            LearningHandoff(id="h2", learning_session_id="s", revision=2, artifact_id="ha",
            schema_version="2.0", protocol_version="subject-learning-v1",
            context_fingerprint_sha256="c"*64, generated_at=AT + timedelta(hours=1),
            supersedes_handoff_id="h1")])
    item = get_session_inbox(config, "S", as_of=AT).resumable_sessions[0]
    assert item.activity_basis == "HANDOFF_GENERATED"
    assert item.activity_at == AT + timedelta(hours=2)


def test_completed_count_direct_contract(project):
    from pydantic import ValidationError
    from uniflow.schemas.session_inbox_read_v1 import SessionInboxV1
    config, _ = project
    payload = get_session_inbox(config, "S", as_of=AT).model_dump()
    payload["completed_count"] = 1
    with pytest.raises(ValidationError, match="COMPLETED_COUNT_INVALID"):
        SessionInboxV1.model_validate(payload)

def test_direct_detail_contract_uniqueness_and_mastery_shapes(project):
    from pydantic import ValidationError
    from uniflow.schemas.application_read_detail_v1 import SessionReadV1, MasteryApplicationReadV1
    config, factory, _ = _seed_validated_result(project)
    item = get_session_inbox(config, "S", as_of=AT).processing_sessions[0]
    payload = item.model_dump()
    payload["result_evidence"].append(dict(payload["result_evidence"][0]))
    with pytest.raises(ValidationError, match="RESULT_EVIDENCE_DUPLICATE"):
        SessionReadV1.model_validate(payload)
    payload = item.model_dump()
    common = {"plan_id": "p", "weekly_plan_revision_id": "rev", "block_id": "b",
        "action_id": "a", "execution_kind": "CREATED_SESSION", "materialized_at": AT}
    payload["materialization_provenance"] = [{"execution_id": "one", **common},
        {"execution_id": "two", **{**common, "block_id": "b2"}}]
    with pytest.raises(ValidationError, match="SESSION_MATERIALIZATION_INVALID"):
        SessionReadV1.model_validate(payload)
    payload["materialization_provenance"][1]["execution_kind"] = "RESUMED_SESSION"
    payload["materialization_provenance"][1]["execution_id"] = "one"
    with pytest.raises(ValidationError, match="SESSION_MATERIALIZATION_INVALID"):
        SessionReadV1.model_validate(payload)
    payload["materialization_provenance"][1]["execution_id"] = "two"
    payload["materialization_provenance"][1]["materialized_at"] = AT.replace(hour=11)
    with pytest.raises(ValidationError, match="SESSION_MATERIALIZATION_ORDER_INVALID"):
        SessionReadV1.model_validate(payload)
    with pytest.raises(ValidationError, match="MASTERY_NOT_APPLIED_INVALID"):
        MasteryApplicationReadV1(status="NOT_APPLIED", mastery_states=[], mastery_state_ids=[],
            mastery_state_count=0, skipped_skill_ids=[])
    with pytest.raises(ValidationError, match="MASTERY_SKIPPED_INVALID"):
        MasteryApplicationReadV1(status="APPLIED", mastery_states=[], mastery_state_ids=[],
            mastery_state_count=0, skipped_skill_ids=None)
