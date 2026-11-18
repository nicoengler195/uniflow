"""Phase 4B1 in-process commands and deterministic external exchange."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application import learning_handoffs, learning_workflow
from uniflow.application import learning_exchange
from uniflow.application.initializer import initialize_project
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.application.session_inbox_read_models import get_session_inbox
from uniflow.application.course_read_models import get_course_overview
from uniflow.domain.enums import LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, DerivedArtifact, Evidence, LearningHandoff, LearningResult,
    LearningSession, MasteryState, Semester, Skill,
)
from uniflow.schemas.learning_exchange_v1 import (
    LearningExchangeEnvelopeV1, canonical_learning_exchange_envelope_json, result_id_for_handoff,
)
from uniflow.schemas.workflow_v1 import CreateSessionRequestV1

AT = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYN", name="Synthetic", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="UTC")
        course = Course(stable_id="C", name="Course", semester=semester)
        db.add(course)
        db.flush()
        db.add_all([Skill(id="TARGET", name="Target", kind="CONCEPT"),
                    Skill(id="OTHER", name="Other", kind="CONCEPT")])
        db.flush()
        db.add_all([CourseSkill(course_id=course.id, skill_id="TARGET"),
                    CourseSkill(course_id=course.id, skill_id="OTHER")])
        course_id = course.id
    yield config, factory, course_id
    factory.kw["bind"].dispose()


def _create_generate(config, course_id):
    created = learning_workflow.create_session(config, CreateSessionRequestV1(
        course_id=course_id, target_skill_ids=["TARGET", "OTHER"], goal="Learn",
        planned_for=AT))
    generated = learning_workflow.generate_handoff(config, {"session_id": created.session_id})
    return created, generated


def _result_from_exchange(directory: Path):
    handoff = json.loads((directory / "handoff.json").read_bytes())
    envelope = json.loads((directory / "handoff-envelope.json").read_bytes())
    return {"schema_version": handoff["return_contract"]["expected_result_schema_version"],
        "protocol_version": envelope["protocol_version"], "result_id": envelope["result_id"],
        "session_id": envelope["session_id"], "handoff_id": envelope["handoff_id"],
        "course_id": handoff["course"]["id"],
        "handoff_artifact_sha256": envelope["handoff_artifact_sha256"],
        "completed_at": "2026-10-15T13:00:00+00:00", "session_outcome": "COMPLETED",
        "skill_evidence": [{"skill_id": "TARGET", "recall": 0.0, "application": 0.8},
                           {"skill_id": "OTHER", "independent_solution": False}]}


def test_integrated_learning_exchange_and_reads(project, tmp_path):
    config, factory, course_id = project
    created, generated = _create_generate(config, course_id)
    assert created.outcome == "CREATED"
    assert learning_workflow.generate_handoff(config, {"session_id": created.session_id}).outcome == "ALREADY_GENERATED"
    parent = tmp_path / "exports"
    parent.mkdir()
    exported = learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
        "destination_directory": parent})
    assert exported.outcome == "EXPORTED"
    directory = parent / exported.directory_name
    assert {p.name for p in directory.iterdir()} == {"handoff.json", "handoff-envelope.json"}
    with factory() as db:
        handoff = db.get(LearningHandoff, generated.handoff_id)
        artifact = db.get(DerivedArtifact, handoff.artifact_id)
        assert (directory / "handoff.json").read_bytes() == Path(artifact.stored_path).read_bytes()
    assert hashlib.sha256((directory / "handoff.json").read_bytes()).hexdigest() == generated.artifact_sha256
    envelope = LearningExchangeEnvelopeV1.model_validate_json((directory / "handoff-envelope.json").read_bytes())
    assert (directory / "handoff-envelope.json").read_bytes() == canonical_learning_exchange_envelope_json(envelope)
    assert envelope.result_id == str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"uniflow:learning-result-v1:{generated.handoff_id}"))
    assert envelope.result_filename == json.loads((directory / "handoff.json").read_bytes())[
        "return_contract"]["expected_filename"]
    assert learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
        "destination_directory": parent}).outcome == "ALREADY_EXPORTED"
    assert learning_workflow.start_session(config, {"session_id": created.session_id}).outcome == "STARTED"
    assert learning_workflow.start_session(config, {"session_id": created.session_id}).outcome == "ALREADY_STARTED"
    result_file = tmp_path / envelope.result_filename
    result_file.write_text(json.dumps(_result_from_exchange(directory)), encoding="utf-8")
    imported = learning_workflow.import_result_file(config, {"path": result_file})
    assert imported.outcome == "IMPORTED" and imported.evidence_count == 2
    assert learning_workflow.import_result_file(config, {"path": result_file}).outcome == "ALREADY_IMPORTED"
    applied = learning_workflow.apply_mastery(config, {"result_id": imported.result_id})
    assert applied.outcome == "APPLIED" and applied.skipped_skill_ids == ("OTHER",)
    assert learning_workflow.apply_mastery(config, {"result_id": imported.result_id}).outcome == "ALREADY_APPLIED"
    with factory() as db:
        assert db.get(LearningSession, created.session_id).state == LearningSessionState.COMPLETED
        assert db.get(LearningResult, imported.result_id).processing_status == "MASTERY_APPLIED"
        assert len(list(db.scalars(select(Evidence).where(Evidence.learning_result_id == imported.result_id)))) == 2
        states = list(db.scalars(select(MasteryState).where(MasteryState.learning_session_id == created.session_id)))
        assert len(states) == 1 and states[0].skill_id == "TARGET" and states[0].value == 0.4
    inbox = get_session_inbox(config, "SYN", as_of=AT)
    assert inbox.completed_count == 1
    assert inbox.completed_sessions[0].result.processing_status == "MASTERY_APPLIED"
    assert len(inbox.completed_sessions[0].result_evidence) == 2
    overview = get_course_overview(config, "SYN", "C", as_of=AT,
        activity_since=AT.replace(day=1), activity_through=AT)
    assert overview.completed_count == 1
    target = next(skill for skill in overview.skills if skill.skill_id == "TARGET")
    assert target.mastery.mastery_status == "KNOWN" and target.mastery.value == 0.4


def test_create_errors_and_non_idempotence(project):
    config, factory, course_id = project
    first = learning_workflow.create_session(config, {"course_id": course_id,
        "target_skill_ids": ["OTHER", "TARGET"], "goal": "Learn", "planned_for": AT})
    second = learning_workflow.create_session(config, {"course_id": course_id,
        "target_skill_ids": ["OTHER", "TARGET"], "goal": "Learn", "planned_for": AT})
    assert first.session_id != second.session_id
    with factory() as db:
        assert [x.skill_id for x in db.get(LearningSession, first.session_id).target_skills] == ["OTHER", "TARGET"]
    for skills, code in [(["TARGET", "TARGET"], "DUPLICATE_TARGET_SKILL"),
                         (["missing"], "INVALID_TARGET_SKILL")]:
        with pytest.raises(WorkflowCommandError) as caught:
            learning_workflow.create_session(config, {"course_id": course_id,
                "target_skill_ids": skills, "goal": "Learn"})
        assert caught.value.code == code
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.create_session(config, {"course_id": course_id,
            "target_skill_ids": ["TARGET"], "goal": "Learn", "planned_for": AT.replace(tzinfo=None)})
    assert caught.value.code == "WORKFLOW_INPUT_INVALID"


def test_exchange_current_only_conflict_and_invalid_parent(project, tmp_path):
    config, factory, course_id = project
    created, old = _create_generate(config, course_id)
    parent = tmp_path / "exports"
    parent.mkdir()
    first = learning_workflow.export_exchange(config, {"handoff_id": old.handoff_id,
        "destination_directory": parent})
    directory = parent / first.directory_name
    (directory / "handoff-envelope.json").write_text("unrelated", encoding="utf-8")
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": old.handoff_id,
            "destination_directory": parent})
    assert caught.value.code == "EXCHANGE_DESTINATION_CONFLICT"
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": old.handoff_id,
            "destination_directory": tmp_path / "missing"})
    assert caught.value.code == "EXCHANGE_DESTINATION_INVALID"
    with factory.begin() as db:
        db.get(Skill, "TARGET").name = "Changed target"
    new = learning_workflow.generate_handoff(config, {"session_id": created.session_id})
    assert new.handoff_id != old.handoff_id
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": old.handoff_id,
            "destination_directory": parent})
    assert caught.value.code == "EXCHANGE_HANDOFF_NOT_CURRENT"
    with factory() as db:
        artifact = db.get(DerivedArtifact, db.get(LearningHandoff, new.handoff_id).artifact_id)
        artifact_path = Path(artifact.stored_path)
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": new.handoff_id,
            "destination_directory": parent})
    assert caught.value.code == "HANDOFF_ARTIFACT_CONFLICT"
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.start_session(config, {"session_id": str(uuid.uuid4())})
    assert caught.value.code == "UNKNOWN_LEARNING_SESSION"


def test_import_file_boundary_and_unknown_exceptions(project, tmp_path, monkeypatch):
    config, _, course_id = project
    created, _ = _create_generate(config, course_id)
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.import_result_file(config, {"path": tmp_path / "missing.json"})
    assert caught.value.code == "RESULT_FILE_NOT_FOUND"
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.import_result_file(config, {"path": tmp_path})
    assert caught.value.code == "RESULT_FILE_INVALID"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.import_result_file(config, {"path": invalid})
    assert caught.value.code == "RESULT_INVALID_JSON"
    assert caught.value.message_key == "workflow.result_invalid_json"
    assert str(tmp_path) not in json.dumps(caught.value.public_dict())
    def unknown(*args):
        raise ValueError("PROGRAMMER_BUG")
    monkeypatch.setattr(learning_handoffs, "generate_handoff", unknown)
    with pytest.raises(ValueError, match="PROGRAMMER_BUG"):
        learning_workflow.generate_handoff(config, {"session_id": created.session_id})
    def runtime(*args):
        raise RuntimeError("BUG")
    monkeypatch.setattr(learning_handoffs, "generate_handoff", runtime)
    with pytest.raises(RuntimeError, match="BUG"):
        learning_workflow.generate_handoff(config, {"session_id": created.session_id})
    def validation(*args):
        from uniflow.schemas.learning_result_v2 import LearningResultV2
        LearningResultV2.model_validate({})
    monkeypatch.setattr(learning_handoffs, "generate_handoff", validation)
    with pytest.raises(ValidationError):
        learning_workflow.generate_handoff(config, {"session_id": created.session_id})


def test_strict_envelope_and_request_boundary():
    one = str(uuid.uuid4())
    two = str(uuid.uuid4())
    assert result_id_for_handoff(one) == result_id_for_handoff(one)
    assert result_id_for_handoff(one) != result_id_for_handoff(two)
    with pytest.raises(ValidationError):
        LearningExchangeEnvelopeV1(protocol_version="subject-learning-v1", session_id=one,
            handoff_id=two, handoff_artifact_sha256="a" * 64, result_id=result_id_for_handoff(two),
            result_filename=f"{one}--{two}.json", extra="forbidden")
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.create_session(None, {"course_id": "x", "target_skill_ids": [], "goal": "x"})
    assert caught.value.code == "WORKFLOW_INPUT_INVALID"
    assert caught.value.public_dict()["details"]["errors"][0]["field"] == "target_skill_ids"
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.create_session(None, {"course_id": "x", "target_skill_ids": ["x"],
            "goal": "x", "C:\\private\\token": "secret"})
    assert "private" not in json.dumps(caught.value.public_dict())


def test_windows_directory_publish_and_race_cleanup(project, tmp_path, monkeypatch):
    config, _, course_id = project
    _, generated = _create_generate(config, course_id)
    parent = tmp_path / "exports"
    parent.mkdir()
    final = parent / f"{generated.session_id}--{generated.handoff_id}"
    stage = parent / "manual-stage"
    stage.mkdir()
    final.mkdir()
    (final / "unrelated").write_text("keep", encoding="utf-8")
    with pytest.raises(OSError):
        os.rename(stage, final)
    assert (final / "unrelated").read_text(encoding="utf-8") == "keep"
    stage.rmdir()
    (final / "unrelated").unlink()
    final.rmdir()
    original = learning_exchange.os.rename
    def race(source, destination):
        final.mkdir()
        (final / "unrelated").write_text("keep", encoding="utf-8")
        raise FileExistsError("simulated race")
    monkeypatch.setattr(learning_exchange.os, "rename", race)
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
            "destination_directory": parent})
    assert caught.value.code == "EXCHANGE_DESTINATION_CONFLICT"
    assert (final / "unrelated").read_text(encoding="utf-8") == "keep"
    assert not any(p.name.startswith(".") for p in parent.iterdir())
    monkeypatch.setattr(learning_exchange.os, "rename", original)


@pytest.mark.parametrize("existing,expected", [
    ("exact", "ALREADY_EXPORTED"),
    ("different", "EXCHANGE_DESTINATION_CONFLICT"),
    ("unreadable", "EXCHANGE_IO_FAILED"),
])
def test_existing_export_inspection_semantics(project, tmp_path, monkeypatch, existing, expected):
    config, _, course_id = project
    _, generated = _create_generate(config, course_id)
    parent = tmp_path / "exports"
    parent.mkdir()
    first = learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
        "destination_directory": parent})
    final = parent / first.directory_name
    if existing == "different":
        (final / "handoff-envelope.json").write_bytes(b"different")
    elif existing == "unreadable":
        def inspect_failure(*args):
            raise PermissionError("transient inspection failure")
        monkeypatch.setattr(learning_exchange, "_exact_existing", inspect_failure)
    if expected == "ALREADY_EXPORTED":
        assert learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
            "destination_directory": parent}).outcome == expected
    else:
        with pytest.raises(WorkflowCommandError) as caught:
            learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
                "destination_directory": parent})
        assert caught.value.code == expected


@pytest.mark.parametrize("concurrent,expected", [
    ("exact", "ALREADY_EXPORTED"),
    ("different", "EXCHANGE_DESTINATION_CONFLICT"),
    ("unreadable", "EXCHANGE_IO_FAILED"),
])
def test_concurrent_final_inspection_semantics(project, tmp_path, monkeypatch, concurrent, expected):
    config, _, course_id = project
    _, generated = _create_generate(config, course_id)
    parent = tmp_path / "exports"
    parent.mkdir()
    final = parent / f"{generated.session_id}--{generated.handoff_id}"
    def competing_publish(source, destination):
        if concurrent == "exact":
            shutil.copytree(source, destination)
        else:
            final.mkdir()
            (final / "unrelated").write_text("keep", encoding="utf-8")
        raise FileExistsError("simulated concurrent publication")
    monkeypatch.setattr(learning_exchange.os, "rename", competing_publish)
    if concurrent == "unreadable":
        def inspect_failure(*args):
            raise OSError("transient inspection failure")
        monkeypatch.setattr(learning_exchange, "_exact_existing", inspect_failure)
    if expected == "ALREADY_EXPORTED":
        assert learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
            "destination_directory": parent}).outcome == expected
    else:
        with pytest.raises(WorkflowCommandError) as caught:
            learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
                "destination_directory": parent})
        assert caught.value.code == expected
    assert not any(p.name.startswith(".") for p in parent.iterdir())


def test_failed_staging_cleanup_does_not_mask_publication_error(project, tmp_path, monkeypatch):
    config, _, course_id = project
    _, generated = _create_generate(config, course_id)
    parent = tmp_path / "exports"
    parent.mkdir()
    def publication_failure(*args):
        raise OSError("publication failed")
    def cleanup_failure(*args):
        raise PermissionError("cleanup failed")
    monkeypatch.setattr(learning_exchange.os, "rename", publication_failure)
    monkeypatch.setattr(learning_exchange.shutil, "rmtree", cleanup_failure)
    with pytest.raises(WorkflowCommandError) as caught:
        learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
            "destination_directory": parent})
    assert caught.value.code == "EXCHANGE_IO_FAILED"
    assert len(list(parent.iterdir())) == 1  # Orphan staging is possible, not success.
