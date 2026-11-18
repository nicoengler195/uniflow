"""Offline 3E2A planning policy checks; all writes are temporary test fixtures."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application.initializer import initialize_project
from uniflow.application.planning import plan_next_actions
from uniflow.application.learning_result_mastery import apply_learning_result_mastery
from uniflow.application.learning_results import start_learning_session, import_learning_result
from test_learning_results import setup as learning_setup, payload as learning_payload, write as write_result
from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Deadline, LearningSession, LearningSessionTargetSkill,
    MasteryState, Semester, Skill, SkillDependency, Material, MaterialArtifact,
    SourceArtifact, ParseRun, ParsedUnit, SemanticExtractionRun, SemanticEvidenceSpan,
    SkillProposal, SkillProposalEvidence, Assessment,
)
from uniflow.schemas.planning_v1 import PlanningRecommendationV1, canonical_planning_json

NOW = datetime(2026, 10, 13, 19, tzinfo=timezone(timedelta(hours=2)))


def setup(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYNTH_2026", name="Synthetic", state=SemesterState.ACTIVE,
                            timezone="Europe/Berlin")
        math = Course(stable_id="MATH_SYNTH", name="Math", semester=semester,
                      information_status=InformationStatus.CONFIRMED)
        physics = Course(stable_id="PHYS_SYNTH", name="Physics", semester=semester,
                         information_status=InformationStatus.CONFIRMED)
        db.add_all([math, physics])
        db.flush()
        for sid in ("DERIVATIVE", "CHAIN_RULE", "KINEMATICS"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add_all([CourseSkill(course_id=math.id, skill_id="DERIVATIVE"),
            CourseSkill(course_id=math.id, skill_id="CHAIN_RULE"),
            CourseSkill(course_id=physics.id, skill_id="KINEMATICS"),
            SkillDependency(prerequisite_skill_id="DERIVATIVE", dependent_skill_id="CHAIN_RULE"),
            MasteryState(skill_id="DERIVATIVE", value=.7, calculation_method="synthetic"),
            MasteryState(skill_id="CHAIN_RULE", value=.3, calculation_method="synthetic"),
            Deadline(course_id=math.id, title="Math sheet", status=DeadlineStatus.OPEN,
                information_status=InformationStatus.CONFIRMED,
                due_at=datetime(2026, 10, 15), is_date_only=True),
            Deadline(course_id=physics.id, title="Physics sheet", status=DeadlineStatus.OPEN,
                information_status=InformationStatus.CONFIRMED,
                due_at=datetime(2026, 10, 18, 10), is_date_only=False)])
        math_id = math.id
    return config, factory, math_id


def plan(config, **kwargs):
    return plan_next_actions(config, "SYNTH_2026", as_of=NOW, **kwargs)


def ids(result):
    return [a.target_skill_ids for a in result.recommendations]


def test_synthetic_ranking_and_read_only(tmp_path):
    config, factory, _ = setup(tmp_path)
    before = {name: count(factory, name) for name in (
        "learning_sessions", "learning_handoffs", "learning_results", "evidence",
        "mastery_states", "skills", "course_skills", "deadlines", "materials", "derived_artifacts")}
    first = plan(config)
    assert ids(first) == [["CHAIN_RULE"], ["DERIVATIVE"], ["KINEMATICS"]]
    assert [a.priority_class for a in first.recommendations] == [1, 1, 1]
    assert first.recommendations[2].target_mastery.status == "UNKNOWN"
    assert first.recommendations[2].target_mastery.value is None
    assert first.recommendations[2].source_status.status == "NO_REVIEWED_SOURCE"
    assert all(a.source_status.status == "NO_REVIEWED_SOURCE" for a in first.recommendations)
    assert canonical_planning_json(first) == canonical_planning_json(plan(config))
    assert before == {name: count(factory, name) for name in before}
    assert first.recommendations[0].deadline_context[0].due_date.isoformat() == "2026-10-15"
    assert first.recommendations[0].deadline_context[0].due_at is None


def count(factory, table):
    from sqlalchemy import text
    with factory() as db:
        return db.execute(text("select count(*) from " + table)).scalar_one()


def test_resume_suppression_and_stable_action_identity(tmp_path):
    config, factory, math_id = setup(tmp_path)
    original = plan(config)
    derivative_id = next(a.action_id for a in original.recommendations if a.target_skill_ids == ["DERIVATIVE"])
    with factory.begin() as db:
        session = LearningSession(id="SESSION_SYNTH", course_id=math_id, goal="Study",
            state=LearningSessionState.IN_PROGRESS)
        db.add(session)
        db.flush()
        db.add(LearningSessionTargetSkill(learning_session_id=session.id, skill_id="DERIVATIVE", ordinal=0))
    updated = plan(config)
    assert updated.recommendations[0].action_type == "RESUME_SESSION"
    assert updated.recommendations[0].existing_session_id == "SESSION_SYNTH"
    assert ["DERIVATIVE"] not in ids(updated)[1:]
    assert updated.context_fingerprint != original.context_fingerprint
    shifted = plan_next_actions(config, "SYNTH_2026", as_of=NOW + timedelta(hours=1))
    assert shifted.recommendations[0].action_id == updated.recommendations[0].action_id
    with factory.begin() as db:
        db.get(LearningSession, "SESSION_SYNTH").state = LearningSessionState.COMPLETED
    resumed = plan(config)
    assert derivative_id == next(a.action_id for a in resumed.recommendations if a.target_skill_ids == ["DERIVATIVE"])


def test_course_scope_deadline_status_and_fingerprint(tmp_path):
    config, factory, math_id = setup(tmp_path)
    filtered = plan(config, course_id="MATH_SYNTH")
    assert ids(filtered) == [["CHAIN_RULE"], ["DERIVATIVE"]]
    with factory.begin() as db:
        db.add(Deadline(course_id=math_id, title="Old open", status=DeadlineStatus.OPEN,
            information_status=InformationStatus.OBSERVED,
            due_at=datetime(2026, 10, 12), is_date_only=True))
        db.add(Deadline(course_id=math_id, title="Cancelled", status=DeadlineStatus.CANCELLED,
            information_status=InformationStatus.CONFIRMED,
            due_at=datetime(2026, 10, 14), is_date_only=True))
    changed = plan(config, course_id="MATH_SYNTH")
    assert changed.context_fingerprint != filtered.context_fingerprint
    assert ids(changed) == ids(filtered)
    assert any(f.code == "OVERDUE_OPEN_DEADLINE" for f in changed.recommendations[0].risk_factors)
    assert changed.context_fingerprint != plan(config, course_id="MATH_SYNTH", limit=1).context_fingerprint
    assert [a.rank for a in plan(config, course_id="MATH_SYNTH", limit=1).recommendations] == [1]


@pytest.mark.parametrize("state", [SemesterState.PLANNING, SemesterState.LECTURES_FINISHED,
    SemesterState.EXAMS_PENDING, SemesterState.RESULTS_PENDING, SemesterState.COMPLETED,
    SemesterState.ARCHIVED])
def test_only_active_semester(tmp_path, state):
    config, factory, _ = setup(tmp_path)
    with factory.begin() as db:
        db.scalar(select(Semester)).state = state
    with pytest.raises(ValueError, match="UNSUPPORTED_SEMESTER_STATE"):
        plan(config)


def test_strict_schema_and_input_validation(tmp_path):
    config, _, _ = setup(tmp_path)
    body = plan(config).model_dump(mode="json")
    body["extra"] = True
    with pytest.raises(ValidationError):
        PlanningRecommendationV1.model_validate(body)
    body.pop("extra")
    body["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        PlanningRecommendationV1.model_validate(body)
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        plan_next_actions(config, "SYNTH_2026", as_of=datetime(2026, 10, 13))
    with pytest.raises(ValueError, match="INVALID_LIMIT"):
        plan(config, limit=0)


def test_unknown_global_prerequisite_and_nonranking_source(tmp_path):
    config, factory, math_id = setup(tmp_path)
    before = plan(config)
    with factory.begin() as db:
        db.add(Skill(id="FOUNDATION", name="Foundation", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(SkillDependency(prerequisite_skill_id="FOUNDATION", dependent_skill_id="CHAIN_RULE"))
    after = plan(config)
    assert ids(after) == ids(before)
    chain = after.recommendations[0]
    assert [p.skill_id for p in chain.prerequisite_context] == ["DERIVATIVE", "FOUNDATION"]
    assert chain.prerequisite_context[1].course_skill_id is None
    assert chain.prerequisite_context[1].mastery.status == "UNKNOWN"
    assert any(x.code == "PREREQUISITE_CHECK_NEEDED" for x in chain.reasons)
    assert after.context_fingerprint != before.context_fingerprint
    # A reviewed source changes availability and fingerprint, but not the ranking key.
    with factory.begin() as db:
        material = Material(course_id=math_id, title="Synthetic", lifecycle_state="PARSED")
        artifact = SourceArtifact(sha256="a" * 64, original_filename="synthetic.txt",
            original_source_path="synthetic", size_bytes=3, stored_path="synthetic")
        db.add_all([material, artifact])
        db.flush()
        ma = MaterialArtifact(material_id=material.id, artifact_id=artifact.id,
                              version_number=1, is_current=True)
        parse = ParseRun(source_artifact_id=artifact.id, parser_name="fixture",
            parser_version="1", dependency_version="stdlib", config_json="{}",
            config_sha256="b" * 64, attempt_number=1, status="SUCCEEDED",
            started_at=NOW, completed_at=NOW)
        db.add_all([ma, parse])
        db.flush()
        unit = ParsedUnit(parse_run_id=parse.id, ordinal=0, unit_kind="DOCUMENT",
            char_start=0, char_end=3, text_sha256="c" * 64, char_count=3,
            non_whitespace_char_count=3)
        run = SemanticExtractionRun(source_family="DOCUMENT", source_role="LECTURE_MATERIAL",
            input_identity_sha256="d" * 64, material_artifact_id=ma.id, parse_run_id=parse.id)
        db.add_all([unit, run])
        db.flush()
        span = SemanticEvidenceSpan(extraction_run_id=run.id, material_artifact_id=ma.id,
            parsed_unit_id=unit.id, char_start=0, char_end=3, span_sha256="e" * 64)
        db.add(span)
        db.flush()
        proposal = SkillProposal(extraction_run_id=run.id, proposal_kind="NEW_SKILL",
            status="ACCEPTED", identity_sha256="f" * 64, accepted_skill_id="CHAIN_RULE")
        db.add(proposal)
        db.flush()
        db.add(SkillProposalEvidence(proposal_id=proposal.id, evidence_span_id=span.id))
    sourced = plan(config)
    assert ids(sourced) == ids(after)
    assert sourced.context_fingerprint != after.context_fingerprint
    assert sourced.recommendations[0].source_status.status == "AVAILABLE"
    assert sourced.recommendations[0].source_status.current_reviewed_source_available
    with factory.begin() as db:
        db.scalar(select(MaterialArtifact)).is_current = False
    historical = plan(config)
    assert historical.recommendations[0].source_status.status == "AVAILABLE"
    assert historical.recommendations[0].source_status.historical_reviewed_source_available
    assert not historical.recommendations[0].source_status.current_reviewed_source_available
    assert ids(historical) == ids(sourced)
    with factory.begin() as db:
        db.scalar(select(SkillProposal)).status = "PENDING"
    pending_source = plan(config)
    assert pending_source.recommendations[0].source_status.status == "NO_REVIEWED_SOURCE"
    assert ids(pending_source) == ids(sourced)


@pytest.mark.parametrize("state,diagnostic", [
    (LearningSessionState.VALIDATED, "VALIDATED_SESSION_AWAITING_MASTERY"),
    (LearningSessionState.RESULT_RECEIVED, "UNEXPECTED_RESULT_RECEIVED_STATE"),
    (LearningSessionState.MASTERY_UPDATED, "UNEXPECTED_MASTERY_UPDATED_STATE"),
])
def test_processing_session_is_diagnostic(tmp_path, state, diagnostic):
    config, factory, math_id = setup(tmp_path)
    with factory.begin() as db:
        db.add(LearningSession(id="PROCESSING", course_id=math_id, goal="Synthetic", state=state))
    result = plan(config)
    assert all(x.action_type == "START_SKILL_SESSION" for x in result.recommendations)
    assert diagnostic in [x.code for x in result.diagnostics]


def test_unknown_mastery_and_source_gap_do_not_override_higher_keys(tmp_path):
    config, factory, _ = setup(tmp_path)
    with factory.begin() as db:
        for deadline in db.scalars(select(Deadline)):
            deadline.status = DeadlineStatus.MET
        db.add(MasteryState(skill_id="KINEMATICS", value=0.0,
                            calculation_method="synthetic"))
    result = plan(config)
    assert all(a.priority_class == 2 for a in result.recommendations)
    assert result.recommendations[0].target_skill_ids == ["KINEMATICS"]
    assert result.recommendations[0].target_mastery.status == "KNOWN"
    assert result.recommendations[0].target_mastery.value == 0.0
    with factory.begin() as db:
        for state in db.scalars(select(MasteryState).where(MasteryState.skill_id == "KINEMATICS")):
            db.delete(state)
    result = plan(config)
    assert result.recommendations[-1].target_skill_ids == ["KINEMATICS"]
    assert result.recommendations[-1].target_mastery.status == "UNKNOWN"


def test_inactive_authority_and_no_skills(tmp_path):
    config, factory, _ = setup(tmp_path)
    original = plan(config)
    with factory.begin() as db:
        link = db.scalar(select(CourseSkill).where(CourseSkill.skill_id == "CHAIN_RULE"))
        link.is_active = False
    result = plan(config)
    assert ["CHAIN_RULE"] not in ids(result)
    assert result.context_fingerprint != original.context_fingerprint
    with factory.begin() as db:
        for link in db.scalars(select(CourseSkill)):
            link.is_active = False
    empty = plan(config)
    assert empty.status == "NO_ELIGIBLE_ACTIONS"
    assert empty.recommendations == []
    assert "NO_REVIEWED_SKILLS" in [d.code for d in empty.diagnostics]


def test_assessment_deadline_exact_course_and_date_tie(tmp_path):
    config, factory, math_id = setup(tmp_path)
    with factory.begin() as db:
        assessment = Assessment(course_id=math_id, name="Exam",
            information_status=InformationStatus.CONFIRMED)
        db.add(assessment)
        db.flush()
        db.add(Deadline(assessment_id=assessment.id, title="Exam time",
            status=DeadlineStatus.OPEN, information_status=InformationStatus.OBSERVED,
            due_at=datetime(2026, 10, 15, 9), is_date_only=False))
    result = plan(config)
    assert ids(result) == [["CHAIN_RULE"], ["DERIVATIVE"], ["KINEMATICS"]]
    math_action = result.recommendations[0]
    assert {d.relevance for d in math_action.deadline_context} == {"ASSESSMENT", "COURSE"}
    assert {d.due_date or d.due_at.date() for d in math_action.deadline_context} == {datetime(2026, 10, 15).date()}


def test_multi_target_session_one_action_and_priority(tmp_path):
    config, factory, math_id = setup(tmp_path)
    with factory.begin() as db:
        for sid, state in (("A", LearningSessionState.PLANNED),
                           ("B", LearningSessionState.HANDOFF_CREATED),
                           ("C", LearningSessionState.IN_PROGRESS)):
            db.add(LearningSession(id=sid, course_id=math_id, goal=sid, state=state))
        db.flush()
        db.add_all([LearningSessionTargetSkill(learning_session_id="C", skill_id="DERIVATIVE", ordinal=0),
                    LearningSessionTargetSkill(learning_session_id="C", skill_id="CHAIN_RULE", ordinal=1),
                    LearningSessionTargetSkill(learning_session_id="B", skill_id="DERIVATIVE", ordinal=0),
                    LearningSessionTargetSkill(learning_session_id="A", skill_id="CHAIN_RULE", ordinal=0)])
    result = plan(config)
    assert [a.existing_session_id for a in result.recommendations[:3]] == ["C", "B", "A"]
    assert result.recommendations[0].target_skill_ids == ["DERIVATIVE", "CHAIN_RULE"]
    assert all(a.action_type == "RESUME_SESSION" for a in result.recommendations[:3])
    assert all(a.target_skill_ids == ["KINEMATICS"] for a in result.recommendations[3:])


def test_planner_after_explicit_learning_loop(tmp_path):
    config, factory, session_id, course_id, handoff = learning_setup(tmp_path)
    as_of = datetime(2026, 10, 13, 19, tzinfo=timezone.utc)
    before = plan_next_actions(config, "SYN", as_of=as_of)
    assert before.recommendations[0].action_type == "RESUME_SESSION"
    assert before.recommendations[0].target_mastery is None
    start_learning_session(config, session_id)
    body = learning_payload(session_id, course_id, handoff)
    import_learning_result(config, write_result(tmp_path, body))
    pending = plan_next_actions(config, "SYN", as_of=as_of)
    assert pending.status == "NO_ELIGIBLE_ACTIONS"
    assert "VALIDATED_SESSION_AWAITING_MASTERY" in [d.code for d in pending.diagnostics]
    applied = apply_learning_result_mastery(config, "result-one")
    assert applied.status == "APPLIED"
    after = plan_next_actions(config, "SYN", as_of=as_of)
    assert after.recommendations[0].target_mastery.status == "KNOWN"
    assert after.recommendations[0].target_mastery.value == .4
    assert after.context_fingerprint != before.context_fingerprint
    assert canonical_planning_json(after) == canonical_planning_json(
        plan_next_actions(config, "SYN", as_of=as_of))


def test_out_of_scope_facts_do_not_change_fingerprint(tmp_path):
    config, factory, _ = setup(tmp_path)
    before = plan(config, course_id="MATH_SYNTH")
    with factory.begin() as db:
        other = Semester(stable_id="OTHER_SEMESTER", name="Other", state=SemesterState.ACTIVE,
            timezone="Europe/Berlin")
        other_course = Course(stable_id="OTHER_COURSE", name="Other", semester=other)
        db.add(other_course)
        db.flush()
        db.add(Deadline(course_id=other_course.id, title="Other deadline",
            status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED,
            due_at=datetime(2026, 10, 14), is_date_only=True))
        physics = db.scalar(select(Course).where(Course.stable_id == "PHYS_SYNTH"))
        db.add(Deadline(course_id=physics.id, title="Physics deadline 2",
            status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED,
            due_at=datetime(2026, 10, 14), is_date_only=True))
    after = plan(config, course_id="MATH_SYNTH")
    assert canonical_planning_json(after) == canonical_planning_json(before)
    assert plan(config).context_fingerprint != plan(config, course_id="MATH_SYNTH").context_fingerprint
