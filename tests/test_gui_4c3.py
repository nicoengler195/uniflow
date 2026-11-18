"""Session detail read and GUI lifecycle on an isolated canonical project."""
from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from uniflow.application import learning_workflow
from uniflow.application.initializer import initialize_project
from uniflow.application.session_detail_read_models import get_session_detail
from uniflow.application import session_detail_read_models
from uniflow.application.semester_read_models import list_semesters
from uniflow.domain.enums import SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.presentation import present_error
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.gui.routes import Route
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, CourseSkill, DerivedArtifact, LearningResult, Semester, Skill

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, operation=None):
        index = next(i for i, task in enumerate(self.tasks) if operation is None or task[0] == operation)
        name, token, fn, callback = self.tasks.pop(index)
        try:
            result = fn()
        except Exception as exc:
            result = present_error(exc)
        callback(name, token, result)
        return result

    def drain(self):
        while self.tasks:
            self.complete()


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYN", name="Synthetic", state=SemesterState.ACTIVE,
            timezone="UTC")
        other = Semester(stable_id="OTHER", name="Other", state=SemesterState.ACTIVE,
            timezone="UTC")
        course = Course(stable_id="MATH", name="Mathematics", semester=semester)
        db.add_all([course, other])
        db.flush()
        db.add_all([Skill(id="A", name="Algebra", kind="CONCEPT"),
            Skill(id="B", name="Proof", kind="ABILITY")])
        db.flush()
        db.add_all([CourseSkill(course_id=course.id, skill_id="A"),
            CourseSkill(course_id=course.id, skill_id="B")])
        course_id = course.id
    yield config, next(x for x in list_semesters(config) if x.stable_id == "SYN"), course_id, tmp_path
    factory.kw["bind"].dispose()


def create_session(project, goal="Study"):
    config, _, course_id, _ = project
    return learning_workflow.create_session(config, {"course_id": course_id,
        "target_skill_ids": ["A", "B"], "goal": goal}).session_id


def setup_gui(qtbot, project):
    config, semester, _, _ = project
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.drain()
    controller.navigate(Route.SESSIONS)
    runner.drain()
    return controller, window, runner


def text_of(widget):
    return " ".join(x.text() for x in widget.findChildren(QLabel))


def test_single_session_read_scope_labels_and_detachment(project):
    config, _, _, _ = project
    sid = create_session(project)
    detail = get_session_detail(config, "SYN", sid, as_of=NOW)
    assert detail.course_name == "Mathematics"
    assert [(x.skill_id, x.name, x.ordinal) for x in detail.target_labels] == [
        ("A", "Algebra", 0), ("B", "Proof", 1)]
    assert detail.session.state == "PLANNED"
    assert "stored_path" not in detail.model_dump_json()
    assert detail.session.session_id == sid  # DTO is usable after the transaction closed.
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        get_session_detail(config, "SYN", sid, as_of=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_session_detail(config, "MISSING", sid, as_of=NOW)
    with pytest.raises(ValueError, match="UNKNOWN_LEARNING_SESSION"):
        get_session_detail(config, "SYN", "missing", as_of=NOW)
    with pytest.raises(ValueError, match="SESSION_NOT_IN_SEMESTER"):
        get_session_detail(config, "OTHER", sid, as_of=NOW)


def test_detail_one_query_only_snapshot_and_no_write(project, monkeypatch):
    config, _, _, _ = project
    sid = create_session(project)
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    original = session_detail_read_models._read_transaction
    seen = []
    @contextmanager
    def checked(config_arg):
        seen.append(True)
        with original(config_arg) as db:
            assert db.connection().exec_driver_sql("PRAGMA query_only").scalar() == 1
            yield db
    monkeypatch.setattr(session_detail_read_models, "_read_transaction", checked)
    assert get_session_detail(config, "SYN", sid, as_of=NOW).session.session_id == sid
    assert len(seen) == 1
    assert hashlib.sha256(config.database_path.read_bytes()).hexdigest() == before


def test_detail_result_verification_and_artifact_conflict(project):
    config, _, _, tmp_path = project
    sid = create_session(project)
    generated = learning_workflow.generate_handoff(config, {"session_id": sid})
    parent = tmp_path / "exports"
    parent.mkdir()
    exported = learning_workflow.export_exchange(config, {"handoff_id": generated.handoff_id,
        "destination_directory": parent})
    learning_workflow.start_session(config, {"session_id": sid})
    child = parent / exported.directory_name
    handoff = json.loads((child / "handoff.json").read_bytes())
    envelope = json.loads((child / "handoff-envelope.json").read_bytes())
    result_file = tmp_path / envelope["result_filename"]
    result_file.write_text(json.dumps({"schema_version": "2.0", "protocol_version": envelope["protocol_version"],
        "result_id": envelope["result_id"], "session_id": sid, "handoff_id": generated.handoff_id,
        "course_id": handoff["course"]["id"], "handoff_artifact_sha256": envelope["handoff_artifact_sha256"],
        "completed_at": "2026-10-15T13:00:00+00:00", "session_outcome": "PARTIAL",
        "skill_evidence": [{"skill_id": "A", "recall": 0.2}]}), encoding="utf-8")
    imported = learning_workflow.import_result_file(config, {"path": result_file})
    detail = get_session_detail(config, "SYN", sid, as_of=NOW)
    assert detail.session.result.result_id == imported.result_id
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:
        artifact = db.get(DerivedArtifact, db.get(LearningResult, imported.result_id).artifact_id)
        stored = Path(artifact.stored_path)
    stored.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="RESULT_ARTIFACT_CONFLICT"):
        get_session_detail(config, "SYN", sid, as_of=NOW)
    factory.kw["bind"].dispose()


def test_routes_inbox_and_late_selection(qtbot, project):
    a = create_session(project, "Session A")
    b = create_session(project, "Session B")
    controller, window, runner = setup_gui(qtbot, project)
    assert [button.text() for button in window.buttons.values()] == ["Dashboard", "Week", "Sessions", "Materials"]
    assert controller.sessions.resumable_count == 2
    assert "Session A" in text_of(window.sessions)
    controller.select_session(a)
    old = runner.tasks.pop()
    controller.select_session(b)
    runner.complete("session_detail")
    old[3](old[0], old[1], old[2]())
    assert controller.session_detail.session.session_id == b
    assert "Session B" in text_of(window.session_detail)
    assert window.session_detail.primary.text() == "Generate learning package"
    window.session_detail.back.click()
    runner.drain()
    assert controller.state.route == Route.SESSIONS
    controller.navigate(Route.WEEK)
    runner.drain()
    assert controller.state.route == Route.WEEK
    controller.navigate(Route.HOME)
    assert controller.state.route == Route.HOME


def test_gui_full_lifecycle_and_idempotency(qtbot, project):
    config, _, _, tmp_path = project
    sid = create_session(project)
    controller, window, runner = setup_gui(qtbot, project)
    controller.select_session(sid)
    runner.drain()
    assert window.session_detail.primary.text() == "Generate learning package"
    assert controller.generate_learning_package()
    assert not controller.generate_learning_package()  # Global learning flight.
    result = runner.complete("learning_mutation")
    assert result.outcome == "GENERATED"
    runner.drain()
    assert window.session_detail.primary.text() == "Start Session"
    assert controller.generate_learning_package()
    assert runner.complete("learning_mutation").outcome == "ALREADY_GENERATED"
    runner.drain()
    parent = tmp_path / "exports"
    parent.mkdir()
    window.session_detail.choose_export_parent = lambda: str(parent)
    window.session_detail.export_button.click()
    exported = runner.complete("learning_mutation")
    assert exported.outcome == "EXPORTED"
    assert runner.tasks == []  # Export does not refresh application reads.
    assert controller.export_learning_package(parent)
    assert runner.complete("learning_mutation").outcome == "ALREADY_EXPORTED"
    assert controller.start_learning_session()
    assert runner.complete("learning_mutation").outcome == "STARTED"
    runner.drain()
    assert window.session_detail.primary.text() == "Sessionabschluss vorbereiten · V2-Ergebnis importieren"
    assert controller.start_learning_session()
    assert runner.complete("learning_mutation").outcome == "ALREADY_STARTED"
    runner.drain()

    directory = parent / exported.directory_name
    handoff = json.loads((directory / "handoff.json").read_bytes())
    envelope = json.loads((directory / "handoff-envelope.json").read_bytes())
    payload = {"schema_version": "2.0", "protocol_version": envelope["protocol_version"],
        "result_id": envelope["result_id"], "session_id": envelope["session_id"],
        "handoff_id": envelope["handoff_id"], "course_id": handoff["course"]["id"],
        "handoff_artifact_sha256": envelope["handoff_artifact_sha256"],
        "completed_at": "2026-10-15T13:00:00+00:00", "session_outcome": "PARTIAL",
        "skill_evidence": [{"skill_id": "A", "recall": 0.0, "application": 0.8},
            {"skill_id": "B", "independent_solution": False}]}
    result_file = tmp_path / envelope["result_filename"]
    result_file.write_text(json.dumps(payload), encoding="utf-8")
    window.session_detail.choose_result_file = lambda: str(result_file)
    window.session_detail.primary.click()
    assert runner.complete("learning_mutation").outcome == "IMPORTED"
    runner.drain()
    assert window.session_detail.primary.text() == "Session abschließen und Mastery-Werte anwenden"
    assert "Evidence for 2 Skill(s)" in text_of(window.session_detail)
    assert controller.import_learning_result(result_file)
    assert runner.complete("learning_mutation").outcome == "ALREADY_IMPORTED"
    runner.drain()
    changed = dict(payload)
    changed["session_outcome"] = "COMPLETED"
    result_file.write_text(json.dumps(changed), encoding="utf-8")
    assert controller.import_learning_result(result_file)
    conflict = runner.complete("learning_mutation")
    assert conflict.code == "RESULT_CONFLICT"
    runner.drain()
    assert controller.apply_learning_mastery()
    applied = runner.complete("learning_mutation")
    assert applied.outcome == "APPLIED" and applied.skipped_skill_ids == ("B",)
    runner.drain()
    assert controller.session_detail.session.state == "COMPLETED"
    assert window.session_detail.primary is None
    displayed = text_of(window.session_detail)
    assert "40% known mastery" in displayed
    assert "No new mastery value recorded for: Proof" in displayed
    assert "100%" not in displayed and "All Skills mastered" not in displayed
    assert controller.apply_learning_mastery()
    assert runner.complete("learning_mutation").outcome == "ALREADY_APPLIED"


def test_mutation_survives_navigation_and_safe_failure(qtbot, project):
    sid = create_session(project)
    controller, window, runner = setup_gui(qtbot, project)
    controller.select_session(sid)
    runner.drain()
    assert controller.generate_learning_package()
    controller.back_to_sessions()
    runner.complete("learning_mutation")
    runner.drain()
    assert controller.sessions.resumable_sessions[0].state == "HANDOFF_CREATED"
    assert controller.state.route == Route.SESSIONS
    assert "Traceback" not in text_of(window.feedback)


def test_controlled_and_unexpected_errors_clear_busy(qtbot, project, monkeypatch):
    sid = create_session(project)
    controller, window, runner = setup_gui(qtbot, project)
    controller.select_session(sid)
    runner.drain()
    def stale(*_):
        raise WorkflowCommandError("INVALID_SESSION_STATE")
    monkeypatch.setattr(learning_workflow, "generate_handoff", stale)
    assert controller.generate_learning_package()
    error = runner.complete("learning_mutation")
    assert error.code == "INVALID_SESSION_STATE"
    assert not controller.learning_mutation_busy
    runner.drain()
    assert "Traceback" not in text_of(window.feedback)
    def unexpected(*_):
        raise RuntimeError("private diagnostic")
    monkeypatch.setattr(learning_workflow, "generate_handoff", unexpected)
    assert controller.generate_learning_package()
    error = runner.complete("learning_mutation")
    assert error.code == "APPLICATION_FAILURE"
    assert not controller.learning_mutation_busy
    assert "private diagnostic" not in text_of(window.feedback)
    assert window.session_detail.primary.isEnabled()


@pytest.mark.parametrize("code,title", [
    ("RESULT_INVALID_JSON", "Result file invalid"),
    ("RESULT_CONFLICT", "Result does not match"),
    ("EXCHANGE_DESTINATION_CONFLICT", "Export destination unavailable"),
    ("HANDOFF_ARTIFACT_CONFLICT", "Stored learning data needs review"),
])
def test_learning_error_categories_are_safe(code, title):
    error = present_error(WorkflowCommandError(code))
    assert error.title == title
    assert "Traceback" not in error.message
