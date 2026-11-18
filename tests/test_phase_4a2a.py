"""Offline Week and ChangeFeed contract checks."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select, text

from test_application_read_models import sparse
from test_weekly_block_materialization import project as plan_project, run

import uniflow.application.application_read_models as read_module
from uniflow.application.application_read_models import get_home_dashboard, get_today_view
from uniflow.application.initializer import initialize_project
from uniflow.application.change_feed_read_models import get_change_feed
from uniflow.application.planning_calendar import add_calendar_constraint
from uniflow.application.planning_calendar import create_planning_profile, add_availability_window
from uniflow.application.week_read_models import get_week_view
from uniflow.application.weekly_plans import (
    accept_weekly_plan_revision, assess_weekly_plan_revision, create_weekly_plan,
    regenerate_weekly_plan, remove_weekly_plan_block, set_weekly_plan_block_duration,
)
from uniflow.infrastructure.database.models import (
    ClassificationProposal, Course, DerivedArtifact, LearningHandoff, LearningResult,
    LearningSession, MasteryState, Material, MaterialArtifact, ParseRun,
    SemanticExtractionRun, SemesterAudit, SkillProposal, SourceArtifact,
    WeeklyPlanRevision, Semester, Skill, CourseSkill,
)
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.domain.enums import SemesterState
from uniflow.schemas.application_read_v1 import canonical_home_json, canonical_today_json
from uniflow.schemas.change_feed_read_v1 import canonical_change_feed_json
from uniflow.schemas.week_read_v1 import canonical_week_json

MON = date(2026, 10, 19)
ZONE = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 19, 8, tzinfo=ZONE)


def test_committed_4a1_canonical_home_today_bytes(tmp_path):
    cfg = load_config(project_root=tmp_path / "golden")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        db.add(Semester(id="sem", stable_id="SYNTH", name="Synthetic",
            state=SemesterState.ACTIVE, timezone="Europe/Berlin"))
        db.add(Course(id="course", semester_id="sem", stable_id="A", name="Alpha"))
        db.add(SemesterAudit(id="audit", semester_id="sem", schema_version="1.0",
            audit_id="A", revision_number=1,
            imported_at=datetime(2026, 10, 18, 12, tzinfo=timezone.utc)))
    at = datetime(2026, 10, 19, 9, tzinfo=ZONE)
    assert hashlib.sha256(canonical_home_json(get_home_dashboard(cfg, "SYNTH", as_of=at))).hexdigest() == \
        "1d0fea63a94a371d8ea67892bd7bde5eb0664c82b2df045be7e9c1dce1234ee1"
    assert hashlib.sha256(canonical_today_json(get_today_view(cfg, "SYNTH", as_of=at))).hexdigest() == \
        "3a1fb66b1fe003896ebf0fcf2c8276fd23a82b6798550afc5b238e5f7f3c16ca"
    assert hashlib.sha256(canonical_week_json(get_week_view(cfg, "SYNTH", MON, as_of=at))).hexdigest() == \
        "3a726d2eebdf5c9b2e9f586f8ea7858ec896b61fced456d20ba390faf3e74b6e"
    feed = get_change_feed(cfg, "SYNTH", as_of=at,
        since=datetime(2026, 10, 18, tzinfo=timezone.utc), through=at)
    assert hashlib.sha256(canonical_change_feed_json(feed)).hexdigest() == \
        "772f7d1964d379614521a20ef8b2956221a7e6be6c4b83df706033f7ed4fb089"
    factory.kw["bind"].dispose()


def test_sparse_week_time_and_zero_writes(sparse):
    cfg, factory = sparse
    with pytest.raises(ValueError, match="INVALID_WEEK_START"):
        get_week_view(cfg, "SYNTH", MON + timedelta(days=1), as_of=NOW)
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        get_week_view(cfg, "SYNTH", MON, as_of=datetime(2026, 10, 19))
    with factory() as db:
        before = db.execute(text("select count(*) from weekly_plans")).scalar_one()
    view = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert view.accepted_plan is None and view.current_draft is None
    assert view.week_end_exclusive == MON + timedelta(days=7)
    assert view.current_calendar.week_start == MON
    assert canonical_week_json(view) == canonical_week_json(get_week_view(cfg, "SYNTH", MON, as_of=NOW))
    with factory() as db:
        assert db.execute(text("select count(*) from weekly_plans")).scalar_one() == before


def test_week_accepted_draft_materialization_and_assessment(plan_project):
    cfg, factory, result, body = plan_project
    first = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert first.accepted_plan.revision_id == result["revision_id"]
    assert first.current_draft is None
    assert first.accepted_plan.revision_assessment == assess_weekly_plan_revision(
        cfg, result["plan_id"], checked_at=NOW)
    block = first.accepted_plan.blocks[0]
    assert block.materialization.status == "NOT_MATERIALIZED"
    assert block.source_scheduler_block_id is not None
    assert block.placement_source == "GENERATED"
    assert first.accepted_plan.source_calendar_identity.artifact_id
    assert first.current_calendar.week_start == MON
    outcome = run(plan_project)
    materialized = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    observed = next(b for b in materialized.accepted_plan.blocks if b.block_id == body.blocks[0].block_id)
    assert observed.materialization.execution_id == outcome["execution_id"]
    assert observed.learning_session_id == outcome["learning_session_id"]
    assert observed.learning_session_state == "PLANNED"
    regenerate_weekly_plan(cfg, result["plan_id"], as_of=NOW)
    split = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert split.accepted_plan.revision_id == result["revision_id"]
    assert split.current_draft.revision_id != result["revision_id"]
    assert all(b.materialization is None for b in split.current_draft.blocks)
    assert canonical_week_json(split) == canonical_week_json(get_week_view(cfg, "SYNTH", MON, as_of=NOW))
    assert canonical_home_json(get_home_dashboard(cfg, "SYNTH", as_of=NOW)) == canonical_home_json(
        get_home_dashboard(cfg, "SYNTH", as_of=NOW))
    assert canonical_today_json(get_today_view(cfg, "SYNTH", as_of=NOW)) == canonical_today_json(
        get_today_view(cfg, "SYNTH", as_of=NOW))


def test_generated_draft_only_and_manual_acceptance(sparse):
    cfg, factory = sparse
    with factory.begin() as db:
        course = db.scalar(select(Course).where(Course.stable_id == "A"))
        db.add(Skill(id="SK", name="Skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="SK"))
    create_planning_profile(cfg, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(cfg, "SYNTH", weekday="MONDAY",
        start_local_time=datetime(2026, 10, 19, 9).time(),
        end_local_time=datetime(2026, 10, 19, 13).time())
    created = create_weekly_plan(cfg, "SYNTH", MON, as_of=NOW)
    generated = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert generated.accepted_plan is None
    assert generated.current_draft.revision_kind == "GENERATED"
    assert generated.current_draft.revision_assessment.status == "FRESH"
    assert all(b.materialization is None for b in generated.current_draft.blocks)
    assert generated.current_draft.blocks
    edited = set_weekly_plan_block_duration(cfg, created["plan_id"],
        generated.current_draft.blocks[0].block_id, 45,
        expected_revision_id=created["revision_id"])
    draft = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert draft.accepted_plan is None and draft.current_draft.revision_kind == "MANUAL_EDIT"
    accept_weekly_plan_revision(cfg, created["plan_id"], accepted_at=NOW)
    accepted = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert accepted.accepted_plan.revision_id == edited["revision_id"]
    assert accepted.accepted_plan.revision_kind == "MANUAL_EDIT"
    assert accepted.current_draft is None


def test_user_removed_unscheduled_remains_distinct(plan_project):
    cfg, _, result, body = plan_project
    remove_weekly_plan_block(cfg, result["plan_id"], body.blocks[0].block_id,
        expected_revision_id=result["revision_id"])
    view = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert len(view.accepted_plan.blocks) == 2
    assert len(view.current_draft.blocks) == 1
    removed = next(u for u in view.current_draft.unscheduled_actions if u.action_id == body.blocks[0].action_id)
    assert removed.primary_reason == "USER_REMOVED"
    assert removed.target_skill_ids == body.blocks[0].target_skill_ids


def test_current_calendar_advisory_and_assessment_equivalence(plan_project):
    cfg, _, result, _ = plan_project
    baseline = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert all(not b.diagnostics for b in baseline.accepted_plan.blocks)
    add_calendar_constraint(cfg, "SYNTH", constraint_type="BLOCKED",
        starts_at=datetime(2026, 10, 19, 9, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 19, 10, tzinfo=ZONE), label="busy")
    changed = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert changed.accepted_plan.source_calendar_identity == baseline.accepted_plan.source_calendar_identity
    assert any(d.code == "CURRENT_PERSONAL_BLOCK_CONFLICT"
               for b in changed.accepted_plan.blocks for d in b.diagnostics)
    assert "CALENDAR_CONTEXT_CHANGED" in changed.accepted_plan.revision_assessment.factors
    assert changed.accepted_plan.revision_assessment == assess_weekly_plan_revision(
        cfg, result["plan_id"], checked_at=NOW)


def test_feed_boundaries_scope_multi_course_and_zero_writes(sparse):
    cfg, factory = sparse
    start = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    with factory.begin() as db:
        courses = {c.stable_id: c for c in db.scalars(select(Course))}
        audit = db.scalar(select(SemesterAudit))
        audit.imported_at = start.replace(tzinfo=None)
        source = SourceArtifact(id="source-one", sha256="a" * 64, original_filename="x.pdf",
            original_source_path="x.pdf", size_bytes=1, stored_path="x")
        db.add(source)
        db.flush()
        for stable in ("A", "B"):
            material = Material(title=stable, course_id=courses[stable].id)
            db.add(material)
            db.flush()
            db.add(MaterialArtifact(material_id=material.id, artifact_id=source.id,
                version_number=1, is_current=True, linked_at=start.replace(tzinfo=None)))
        db.add(ParseRun(id="parse-one", source_artifact_id=source.id,
            parser_name="PDF", parser_version="1", dependency_version="test",
            config_json="{}", config_sha256="b" * 64, attempt_number=1,
            status="SUCCESS", started_at=start.replace(tzinfo=None),
            completed_at=start.replace(tzinfo=None)))
    through = start + timedelta(hours=1)
    with factory() as db:
        before = {name: db.execute(text("select count(*) from " + name)).scalar_one()
                  for name in ("parse_runs", "material_artifacts", "semester_audits")}
    feed = get_change_feed(cfg, "SYNTH", as_of=through, since=start, through=through)
    parse = next(e for e in feed.events if e.kind == "PARSE_RUN_FINISHED")
    assert parse.event_id == "PARSE_RUN_FINISHED:parse-one"
    assert set(parse.course_stable_ids) == {"A", "B"}
    assert parse.course_ids == sorted(parse.course_ids)
    assert len([e for e in feed.events if e.kind == "PARSE_RUN_FINISHED"]) == 1
    assert feed.events == sorted(feed.events, key=lambda e: (-e.occurred_at.timestamp(), e.kind, e.entity_id))
    assert canonical_change_feed_json(feed) == canonical_change_feed_json(
        get_change_feed(cfg, "SYNTH", as_of=through, since=start, through=through))
    assert get_change_feed(cfg, "SYNTH", as_of=through, since=start, through=through,
        course_id="A").events[-1].course_stable_ids
    assert get_change_feed(cfg, "SYNTH", as_of=through, since=start, through=through,
        limit=1).returned_count == 1
    assert get_change_feed(cfg, "SYNTH", as_of=through,
        since=start-timedelta(seconds=1), through=start).returned_count == 0
    with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
        get_change_feed(cfg, "SYNTH", as_of=through, since=start, through=through,
            course_id="SIBLING")
    with factory() as db:
        assert before == {name: db.execute(text("select count(*) from " + name)).scalar_one()
                          for name in before}


@pytest.mark.parametrize("bad", [0, 201, True, 1.5])
def test_feed_limit_validation(sparse, bad):
    cfg, _ = sparse
    with pytest.raises(ValueError, match="INVALID_LIMIT"):
        get_change_feed(cfg, "SYNTH", as_of=NOW, since=NOW-timedelta(days=1),
            through=NOW, limit=bad)


def test_feed_time_validation(sparse):
    cfg, _ = sparse
    with pytest.raises(ValueError, match="SINCE_MUST_BE_AWARE"):
        get_change_feed(cfg, "SYNTH", as_of=NOW, since=datetime(2026, 10, 18), through=NOW)
    with pytest.raises(ValueError, match="INVALID_CHANGE_FEED_RANGE"):
        get_change_feed(cfg, "SYNTH", as_of=NOW, since=NOW, through=NOW)
    with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
        get_change_feed(cfg, "SYNTH", as_of=NOW, since=NOW-timedelta(days=1),
            through=NOW, course_id="missing")


def test_new_projections_use_one_query_only_snapshot(sparse, monkeypatch):
    cfg, _ = sparse
    original = read_module.get_engine
    opened = []
    def counted(path):
        engine = original(path)
        event.listen(engine, "connect", lambda *_: opened.append(1))
        return engine
    monkeypatch.setattr(read_module, "get_engine", counted)
    get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert len(opened) == 1
    opened.clear()
    get_change_feed(cfg, "SYNTH", as_of=NOW, since=NOW-timedelta(days=1), through=NOW)
    assert len(opened) == 1
    with read_module._read_transaction(cfg) as db:
        assert db.connection().connection.driver_connection.in_transaction
        assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 1


def test_plan_assessment_replay_stays_in_week_snapshot(plan_project, monkeypatch):
    cfg, _, _, _ = plan_project
    original = read_module.get_engine
    opened = []
    def counted(path):
        engine = original(path)
        event.listen(engine, "connect", lambda *_: opened.append(1))
        return engine
    monkeypatch.setattr(read_module, "get_engine", counted)
    view = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert view.accepted_plan.revision_assessment.status == "FRESH"
    assert len(opened) == 1


def test_invalid_revision_is_not_an_empty_plan(plan_project):
    cfg, factory, result, _ = plan_project
    with factory.begin() as db:
        row = db.get(WeeklyPlanRevision, result["revision_id"])
        row.revision_payload_sha256 = "0" * 64
    view = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert view.accepted_plan.revision_assessment.status == "INVALID"
    assert "SOURCE_ARTIFACT_INVALID" in view.accepted_plan.revision_assessment.factors
    assert view.accepted_plan.blocks is None and view.accepted_plan.unscheduled_actions is None
    assert view.accepted_plan.acceptance_id is not None


def test_supported_change_kinds_one_row_each(plan_project):
    cfg, factory, result, body = plan_project
    execution = run(plan_project)
    at = datetime(2026, 10, 19, 11, tzinfo=timezone.utc)
    with factory.begin() as db:
        course = db.scalar(select(Course))
        semester_id = course.semester_id
        sibling = Course(stable_id="B", name="Sibling", semester_id=semester_id)
        db.add(sibling)
        db.flush()
        db.add(SemesterAudit(semester_id=semester_id, audit_id="AUDIT", revision_number=1,
            schema_version="1.0", imported_at=at))
        source = SourceArtifact(sha256="c" * 64, original_filename="source.pdf",
            original_source_path="source.pdf", size_bytes=1, stored_path="source")
        db.add(source)
        db.flush()
        material = Material(course_id=course.id, title="Source")
        db.add(material)
        db.flush()
        link = MaterialArtifact(material_id=material.id, artifact_id=source.id,
            version_number=1, is_current=True, linked_at=at)
        db.add(link)
        db.flush()
        sibling_material = Material(course_id=sibling.id, title="Shared source")
        db.add(sibling_material)
        db.flush()
        db.add(MaterialArtifact(material_id=sibling_material.id, artifact_id=source.id,
            version_number=1, is_current=True, linked_at=at))
        db.add(ParseRun(source_artifact_id=source.id, parser_name="PDF",
            parser_version="1", dependency_version="test", config_json="{}",
            config_sha256="d" * 64, attempt_number=1, status="SUCCESS",
            started_at=at, completed_at=at))
        db.add(ClassificationProposal(proposal_kind="ARTIFACT_TO_EXISTING_MATERIAL",
            status="REJECTED", identity_sha256="e" * 64,
            subject_source_artifact_id=source.id, proposed_course_id=course.id,
            classifier_name="MANUAL", classifier_version="1", decided_at=at))
        extraction = SemanticExtractionRun(source_family="MANUAL", source_role="UNKNOWN",
            extractor_name="test", extractor_version="1", input_identity_sha256="f" * 64,
            material_artifact_id=None, parse_run_id=None)
        db.add(extraction)
        db.flush()
        db.add(SkillProposal(extraction_run_id=extraction.id,
            proposal_kind="NEW_SKILL", status="REJECTED", identity_sha256="1" * 64,
            course_id=course.id, proposed_name="X", decided_at=at))
        artifact_a = DerivedArtifact(sha256="2" * 64, kind="LEARNING_HANDOFF_JSON",
            stored_path="a", size_bytes=1)
        artifact_b = DerivedArtifact(sha256="3" * 64, kind="LEARNING_RESULT_JSON",
            stored_path="b", size_bytes=1)
        db.add_all([artifact_a, artifact_b])
        db.flush()
        session = db.get(LearningSession, execution["learning_session_id"])
        handoff = LearningHandoff(learning_session_id=session.id, revision=1,
            artifact_id=artifact_a.id, schema_version="2.0", protocol_version="subject-learning-v1",
            context_fingerprint_sha256="4" * 64, generated_at=at)
        db.add(handoff)
        db.flush()
        db.add(LearningResult(id="result-one", learning_session_id=session.id,
            learning_handoff_id=handoff.id, artifact_id=artifact_b.id,
            schema_version="2.0", protocol_version="subject-learning-v1",
            raw_input_sha256="5" * 64, completed_at=at, received_at=at,
            session_outcome="COMPLETED", processing_status="IMPORTED"))
        db.add(MasteryState(skill_id="A", value=.5, calculation_method="test",
            learning_session_id=session.id, calculated_at=at))
    set_weekly_plan_block_duration(cfg, result["plan_id"], body.blocks[1].block_id,
        45, expected_revision_id=result["revision_id"])
    week = get_week_view(cfg, "SYNTH", MON, as_of=NOW)
    assert week.accepted_plan.revision_id == result["revision_id"]
    assert week.current_draft.revision_kind == "MANUAL_EDIT"
    assert "NBA_CONTEXT_CHANGED" in week.accepted_plan.revision_assessment.factors
    # Plan revision creation uses the wall clock. Pin the synthetic event times
    # inside this test's fixed feed window so it remains stable on later dates.
    with factory.begin() as db:
        for revision in db.scalars(select(WeeklyPlanRevision)):
            revision.created_at = at
    feed = get_change_feed(cfg, "SYNTH", as_of=at + timedelta(days=1),
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        through=at + timedelta(hours=1), limit=200)
    kinds = {e.kind for e in feed.events}
    assert kinds == {"AUDIT_IMPORTED", "MATERIAL_VERSION_LINKED", "PARSE_RUN_FINISHED",
        "CLASSIFICATION_DECIDED", "SKILL_PROPOSAL_DECIDED", "PLAN_GENERATED", "PLAN_EDITED",
        "PLAN_ACCEPTED", "BLOCK_MATERIALIZED", "HANDOFF_GENERATED",
        "RESULT_IMPORTED", "MASTERY_RECORDED"}
    assert len({e.event_id for e in feed.events}) == len(feed.events)
    assert all(e.event_id == f"{e.kind}:{e.entity_id}" for e in feed.events)
    parse_events = [e for e in feed.events if e.kind == "PARSE_RUN_FINISHED"]
    assert len(parse_events) == 1 and set(parse_events[0].course_stable_ids) == {"B", "C"}
