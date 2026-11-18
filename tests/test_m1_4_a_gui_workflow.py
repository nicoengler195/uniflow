"""M1.4-A Qt workflow with controlled services; no foreign bridge implementation."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QInputDialog, QMessageBox

from uniflow.application import gui_learning_bridge as bridge, learning_workflow, review_workflow
from uniflow.application import document_parsing
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.gui.controller import GuiController
from uniflow.gui.presentation import PresentationError, present_error
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy, ReviewFamily
from uniflow.gui.screens.material_detail import ParsedSelectionDialog, MaterialDetailScreen
from uniflow.gui.screens.proposal_review import ProposalReviewScreen
from uniflow.gui.screens.session_detail import SessionDetailScreen

# Run through the reviewed wrapper with explicit `-p pytestqt.plugin`.
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class Runner:
    def __init__(self):
        self.tasks = []
        self.running = False

    def submit(self, kind, token, fn, callback):
        self.tasks.append((kind, token, fn, callback))

    def complete(self, kind):
        index = next(i for i, task in enumerate(self.tasks) if task[0] == kind)
        operation, token, fn, callback = self.tasks.pop(index)
        self.running = True
        try:
            result = fn()
        except Exception as exc:
            result = present_error(exc)
        finally:
            self.running = False
        callback(operation, token, result)
        return result


@pytest.fixture
def setup(tmp_path, qapp, monkeypatch):
    runner = Runner()
    controller = GuiController(NS(project_root=tmp_path, learning_handoff_max_excerpt_utf8_bytes=4096),
        NS(stable_id="semester-stable", timezone="UTC"), runner=runner, clock=lambda: NOW)
    # Reads outside the operation under test are observable, but never touch a DB.
    for name in ("refresh_materials", "refresh_material_detail", "refresh_sessions",
                 "refresh_session_detail", "refresh_home", "refresh_week"):
        monkeypatch.setattr(controller, name, lambda **kw: None)
    messages = []
    controller.feedback.connect(messages.append)
    return controller, runner, messages


def source_dialog(controller, runner, monkeypatch, qtbot, text="A😀 graph and e\u0301."):
    def parse(*args):
        assert runner.running
        return NS(page_count=2), [NS(id="unit-1", ordinal=0, unit_kind="PAGE", source_label="1",
            non_whitespace_char_count=12), NS(id="unit-2", ordinal=1, unit_kind="PAGE", source_label="2",
            non_whitespace_char_count=0)]
    def read(parse_id, ordinal, config):
        assert runner.running and parse_id == "parse-current"
        return text if ordinal == 0 else ""
    monkeypatch.setattr(document_parsing, "get_parse_run", parse)
    monkeypatch.setattr(document_parsing, "read_parsed_unit", read)
    dialog = ParsedSelectionDialog(controller, "parse-current")
    qtbot.addWidget(dialog)
    assert dialog.editor.toPlainText() == ""
    runner.complete("parsed_source")
    runner.complete("parsed_unit")
    return dialog


def select_graph(dialog):
    # Qt positions count the emoji as two UTF-16 units; Python offsets are 3..8.
    cursor = dialog.editor.textCursor()
    cursor.setPosition(4)
    cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
    dialog.editor.setTextCursor(cursor)
    dialog.name.setText("Graph")
    assert dialog.selected == ("unit-1", 3, 8)
    assert dialog.preview.toPlainText().endswith("graph")


def review(controller, status="PENDING", token="a" * 64):
    controller.state.route = Route.PROPOSAL_REVIEW
    controller.state.selected_review_family = ReviewFamily.SKILL
    controller.state.selected_proposal_id = "proposal"
    controller.proposal_review = NS(proposal=NS(proposal_id="proposal", status=status,
        proposal_kind="NEW_SKILL"), supported_actions=["ACCEPT_NEW", "RESOLVE_EXISTING", "REJECT"],
        review_context_token=token)


def detail(handoff=True):
    h = NS(handoff_id="handoff-7", revision=7, generated_at=NOW, artifact_sha256="b" * 64)
    return NS(course_name="Mathematics", semester=NS(timezone="UTC"), target_labels=[
        NS(skill_id="skill-confirmed", name="Graph", ordinal=0)], session=NS(
        session_id="session-1", course_stable_id="course-stable", state="HANDOFF_CREATED" if handoff else "PLANNED",
        goal="Explain graphs independently", activity_basis="HANDOFF_GENERATED" if handoff else "SESSION_CREATED",
        activity_at=NOW, created_at=NOW, planned_local_wall_time=None,
        current_handoff=h if handoff else None, handoff_history=[h] if handoff else [], result=None,
        materialization_provenance=[], diagnostics=[], mastery_application=NS(
            status="NOT_APPLIED", mastery_states=[], skipped_skill_ids=[])))


def test_explicit_full_workflow_and_repeated_chat_export(setup, monkeypatch, qtbot, tmp_path):
    c, r, messages = setup
    c.state.route = Route.MATERIAL_DETAIL
    c.state.selected_material_id = "material"
    dialog = source_dialog(c, r, monkeypatch, qtbot)
    select_graph(dialog)
    calls = []
    def propose(*args):
        assert r.running
        calls.append(args[1:])
        return "proposal"
    monkeypatch.setattr(bridge, "propose_selected_document_skill", propose)
    dialog.propose.click()
    assert dialog.result() == QDialog.Accepted
    assert c.propose_selected_study_skill("material", "parse-current", *dialog.selected, "Graph", "CONCEPT")
    assert not c.propose_selected_study_skill("material", "parse-current", *dialog.selected, "Graph", "CONCEPT")
    r.complete("study_bridge")
    assert calls == [("material", "parse-current", "unit-1", 3, 8, "Graph", "CONCEPT")]
    assert not any(task[0] == "review_mutation" for task in r.tasks)
    review(c)
    monkeypatch.setattr(c, "refresh_proposal_review", lambda **kw: None)
    def accept(config, request):
        assert r.running and request.expected_review_context_token == "a" * 64
        return NS(outcome="ACCEPTED", accepted_skill_id="skill-confirmed")
    monkeypatch.setattr(review_workflow, "accept_new_skill", accept)
    assert c.decide_proposal("ACCEPT_NEW")
    assert not c.decide_proposal("ACCEPT_NEW")
    r.complete("review_mutation")
    c.state.route = Route.MATERIAL_DETAIL
    c.review_mutation_busy = False
    association = []
    monkeypatch.setattr(bridge, "associate_skill_with_course",
        lambda config, material, skill: association.append((material, skill)))
    assert c.associate_study_skill("material", "skill-confirmed")
    r.complete("study_bridge")
    assert association == [("material", "skill-confirmed")]
    def create(config, semester, course, goal, skills):
        assert r.running
        assert (semester, course, goal, skills) == ("semester-stable", "course-stable",
            "Explain graphs independently", ["skill-confirmed"])
        return NS(session_id="session-1")
    monkeypatch.setattr(bridge, "create_course_study_session", create, raising=False)
    assert c.create_study_session("course-stable", "Explain graphs independently", ["skill-confirmed"])
    assert not c.create_study_session("course-stable", "Duplicate", ["skill-confirmed"])
    r.complete("study_bridge")
    assert c.state.selected_session_id == "session-1"
    c.session_detail = detail(False)
    def generate(config, request):
        assert r.running and request.session_id == "session-1"
        return NS(outcome="GENERATED", session_id="session-1", handoff_id="handoff-7", revision=7,
            artifact_sha256="b" * 64)
    monkeypatch.setattr(learning_workflow, "generate_handoff", generate)
    assert c.generate_learning_package()
    r.complete("learning_mutation")
    c.session_detail = detail()
    screen = SessionDetailScreen(c)
    qtbot.addWidget(screen)
    screen.render(c.session_detail)
    words = " ".join(w.text() for w in screen.findChildren(QLabel))
    assert all(value in words for value in ("session-1", "handoff-7", "revision 7", "b" * 64))
    destination = tmp_path / "chosen"
    destination.mkdir()
    output = destination / "learning-chat.json"
    exported = []
    def export(config, request):
        assert r.running and request.handoff_id == "handoff-7"
        assert request.destination_directory == destination
        status = "ALREADY_EXPORTED" if output.exists() else "EXPORTED"
        output.write_text('synthetic controlled service package', encoding="utf-8")
        exported.append(status)
        return NS(status=status, path=output, session_id="session-1", handoff_id="handoff-7",
            handoff_artifact_sha256="b" * 64, result_filename="return.json")
    monkeypatch.setattr(learning_workflow, "export_learning_chat", export)
    monkeypatch.setattr(screen, "choose_export_parent", lambda: str(destination))
    for _ in range(2):
        screen.chat_export_button.click()
        assert not c.export_learning_chat(destination)
        r.complete("learning_mutation")
    assert exported == ["EXPORTED", "ALREADY_EXPORTED"]
    assert list(destination.iterdir()) == [output]
    assert all(x in messages[-1] for x in (str(output), "session-1", "handoff-7", "b" * 64, "return.json"))


@pytest.mark.parametrize("code", ["INVALID_SESSION_COURSE", "TARGET_COURSESKILL_REQUIRED", "INVALID_SESSION_GOAL"])
def test_bridge_validation_is_deferred_and_recoverable(setup, monkeypatch, code):
    c, r, messages = setup
    def create(*args):
        assert r.running
        raise WorkflowCommandError(code)
    monkeypatch.setattr(bridge, "create_course_study_session", create, raising=False)
    assert c.create_study_session("wrong-course", "", ["unassociated"])
    assert not messages
    r.complete("study_bridge")
    assert messages[-1].code == code and not c.bridge_busy
    assert c.state.selected_session_id is None
    assert c.create_study_session("course-stable", "Recovery", ["confirmed"])


@pytest.mark.parametrize("operation", ["session", "proposal", "association"])
def test_navigation_away_and_back_blocks_late_bridge_followup(setup, operation):
    c, r, messages = setup
    c.state.route = Route.MATERIAL_DETAIL
    c.state.selected_material_id = "original"
    assert c._bridge(operation, lambda: NS(session_id="late") if operation == "session" else "late")
    c.navigate(Route.HOME)
    c.navigate(Route.MATERIAL_DETAIL)
    r.complete("study_bridge")
    assert c.state.route == Route.MATERIAL_DETAIL
    assert c.state.selected_session_id is None and c._pending_proposal_review is None
    assert not c.bridge_busy


def test_source_unicode_empty_selection_and_late_unit_cancel(setup, monkeypatch, qtbot):
    c, r, _ = setup
    d = source_dialog(c, r, monkeypatch, qtbot)
    assert not d.propose.isEnabled() and d.selected is None
    select_graph(d)
    d.unit_choice.setCurrentIndex(1)
    assert d.selected is None and not d.propose.isEnabled()
    d.unit_choice.setCurrentIndex(0)
    r.complete("parsed_unit")  # old blank unit must not replace new selection
    assert d.editor.toPlainText() == ""
    r.complete("parsed_unit")
    assert "graph" in d.editor.toPlainText()
    d.unit_choice.setCurrentIndex(1)
    d.reject()
    r.complete("parsed_unit")
    assert d.editor.toPlainText() == "" and d.selected is None


def test_parse_change_error_never_substitutes_source(setup, monkeypatch):
    c, r, messages = setup
    def propose(*args):
        assert args[2] == "stale-parse"
        raise ValueError("SKILL_SELECTION_SOURCE_CHANGED")
    monkeypatch.setattr(bridge, "propose_selected_document_skill", propose)
    c.propose_selected_study_skill("material", "stale-parse", "unit", 0, 4, "Name", "CONCEPT")
    r.complete("study_bridge")
    assert messages[-1].code == "SKILL_SELECTION_SOURCE_CHANGED"
    assert c._pending_proposal_review is None and not c.bridge_busy


@pytest.mark.parametrize("action", ["ACCEPT_NEW", "RESOLVE_EXISTING", "REJECT"])
def test_explicit_decision_uses_displayed_token_and_target(setup, monkeypatch, action):
    c, r, _ = setup
    review(c)
    c.selected_review_skill_id = "existing-skill"
    monkeypatch.setattr(c, "refresh_proposal_review", lambda **kw: None)
    received = []
    def decide(config, request):
        assert r.running
        received.append(request)
        return NS(outcome="REJECTED" if action == "REJECT" else "ACCEPTED")
    name = {"ACCEPT_NEW": "accept_new_skill", "RESOLVE_EXISTING": "resolve_existing_skill", "REJECT": "reject_skill"}[action]
    monkeypatch.setattr(review_workflow, name, decide)
    assert c.decide_proposal(action, reason="Not relevant" if action == "REJECT" else None)
    assert not c.decide_proposal(action)
    r.complete("review_mutation")
    assert received[0].proposal_id == "proposal" and received[0].expected_review_context_token == "a" * 64
    if action == "RESOLVE_EXISTING":
        assert received[0].existing_skill_id == "existing-skill"
    if action == "REJECT":
        assert received[0].reason == "Not relevant"


def test_stale_review_token_and_rejected_proposal_block(setup, monkeypatch):
    c, r, messages = setup
    review(c)
    refreshed = []
    monkeypatch.setattr(c, "refresh_proposal_review", lambda **kw: refreshed.append(kw))
    def stale(*args):
        raise WorkflowCommandError("REVIEW_CONTEXT_CHANGED")
    monkeypatch.setattr(review_workflow, "accept_new_skill", stale)
    assert c.decide_proposal("ACCEPT_NEW")
    r.complete("review_mutation")
    assert messages[-1].code == "REVIEW_CONTEXT_CHANGED" and refreshed == [{"authoritative": True}]
    c.review_mutation_busy = False
    review(c, status="REJECTED")
    assert not c.decide_proposal("ACCEPT_NEW")


@pytest.mark.parametrize("code", ["EXCHANGE_DESTINATION_INVALID", "EXCHANGE_HANDOFF_NOT_CURRENT", "HANDOFF_NOT_FOUND", "HANDOFF_ARTIFACT_CONFLICT"])
def test_chat_errors_recover_and_keep_exact_handoff(setup, monkeypatch, tmp_path, code):
    c, r, messages = setup
    c.state.route, c.state.selected_session_id = Route.SESSION_DETAIL, "session-1"
    c.session_detail = detail()
    def fail(config, request):
        assert request.handoff_id == "handoff-7" and r.running
        raise WorkflowCommandError(code)
    monkeypatch.setattr(learning_workflow, "export_learning_chat", fail)
    assert c.export_learning_chat(tmp_path / "absent")
    r.complete("learning_mutation")
    assert messages[-1].code == code and not c.learning_mutation_busy
    assert c.export_learning_chat(tmp_path)


def test_export_cancel_navigation_and_missing_handoff(setup, monkeypatch, qtbot, tmp_path):
    c, r, _ = setup
    c.state.route, c.state.selected_session_id = Route.SESSION_DETAIL, "session-1"
    c.session_detail = detail()
    screen = SessionDetailScreen(c)
    qtbot.addWidget(screen)
    screen.render(c.session_detail)
    monkeypatch.setattr(screen, "choose_export_parent", lambda: "")
    screen.chat_export_button.click()
    assert not r.tasks
    def navigate():
        c.navigate(Route.HOME)
        return str(tmp_path)
    monkeypatch.setattr(screen, "choose_export_parent", navigate)
    screen.chat_export_button.click()
    assert not r.tasks
    c.session_detail = detail(False)
    assert not c.export_learning_chat(tmp_path)
    c.session_detail = detail()
    c.state.selected_session_id = "other-session"
    assert not c.export_learning_chat(tmp_path)


def test_preparing_session_requires_accepted_association_and_cancel_is_nonmutating(setup, monkeypatch, qtbot):
    c, r, _ = setup
    screen = MaterialDetailScreen(c)
    qtbot.addWidget(screen)
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(bridge, "course_skills", lambda *a: [("skill", "Graph")])
    for status, accepted in (("REJECTED", "skill"), ("PENDING", None), ("ACCEPTED", "unlinked")):
        p = NS(status=status, proposal_kind="NEW_SKILL", accepted_skill_id=accepted)
        screen._prepare_session("course", [p])
        screen._prepare_session("course", [p])
        assert len(r.tasks) == 1
        r.complete("study_read")
        assert not r.tasks
    p = NS(status="ACCEPTED", proposal_kind="NEW_SKILL", accepted_skill_id="skill")
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a: ("", False))
    screen._prepare_session("course", [p])
    r.complete("study_read")
    assert not r.tasks and not c.bridge_busy


def test_unknown_error_prose_is_never_a_contract():
    for exc in (ValueError("MATERIAL_secret-path-and-prose"), WorkflowCommandError("UNKNOWN_SENSITIVE_PROSE")):
        error = present_error(exc)
        assert error.code == "APPLICATION_FAILURE"
        assert "secret" not in error.message and "sensitive" not in error.message


@pytest.mark.parametrize("cancel,navigate", [(True, False), (False, True)])
def test_review_confirmation_cancel_or_navigation_never_decides(setup, monkeypatch, qtbot, cancel, navigate):
    from uniflow.gui.screens import proposal_review
    c, r, _ = setup
    review(c)
    c.proposal_review.proposal.proposed_name = "Graph"
    c.proposal_review.proposal.proposed_material_title = None
    screen = ProposalReviewScreen(c)
    qtbot.addWidget(screen)
    screen.review = c.proposal_review
    def execute(dialog):
        if navigate:
            c.navigate(Route.HOME)
        return QDialog.Rejected if cancel else QDialog.Accepted
    monkeypatch.setattr(proposal_review.ReviewConfirmDialog, "exec", execute)
    screen._decide("ACCEPT_NEW")
    assert not r.tasks and not c.review_mutation_busy


def test_association_confirmation_cancel_and_navigation(setup, monkeypatch, qtbot):
    c, r, _ = setup
    c.state.route = Route.MATERIAL_DETAIL
    screen = MaterialDetailScreen(c)
    qtbot.addWidget(screen)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    screen._associate_skill("material", "Math", "accepted", "Graph")
    assert not r.tasks
    def navigate(*args):
        c.navigate(Route.HOME)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", navigate)
    screen._associate_skill("material", "Math", "accepted", "Graph")
    assert not r.tasks


def test_learning_worker_after_navigation_cannot_refresh_other_detail(setup, monkeypatch):
    c, r, _ = setup
    c.state.route, c.state.selected_session_id = Route.SESSION_DETAIL, "session-1"
    c.session_detail = detail(False)
    refreshed = []
    monkeypatch.setattr(c, "refresh_session_detail", lambda **kw: refreshed.append(kw))
    monkeypatch.setattr(learning_workflow, "generate_handoff", lambda *a: NS(outcome="GENERATED"))
    assert c.generate_learning_package()
    c.navigate(Route.HOME)
    c.state.selected_session_id = "other"
    r.complete("learning_mutation")
    assert not refreshed and not c.learning_mutation_busy
    assert c.state.route == Route.HOME and c.state.selected_session_id == "other"


def test_source_callback_after_navigation_is_ignored(setup, monkeypatch, qtbot):
    c, r, _ = setup
    d = source_dialog(c, r, monkeypatch, qtbot)
    d.unit_choice.setCurrentIndex(1)
    c.navigate(Route.SESSIONS)
    r.complete("parsed_unit")
    assert d.editor.toPlainText() == "" and not d.propose.isEnabled()


def test_oversized_and_blank_source_passages_block_proposal(setup, monkeypatch, qtbot):
    c, r, _ = setup
    c.config.learning_handoff_max_excerpt_utf8_bytes = 4
    d = source_dialog(c, r, monkeypatch, qtbot, text="😀😀   ")
    cursor = d.editor.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    d.editor.setTextCursor(cursor)
    d.name.setText("Too large")
    assert d.selected is None and not d.propose.isEnabled()
    cursor.setPosition(4)
    cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
    d.editor.setTextCursor(cursor)
    assert d.selected is None and not d.propose.isEnabled()


def test_real_task_runner_runs_bridge_off_qt_thread(setup, monkeypatch, qtbot):
    from PySide6.QtCore import QThread
    from uniflow.gui.workers import TaskRunner
    c, _, messages = setup
    c.runner = TaskRunner()
    seen = []
    completions = []
    original = c._bridge_completed
    def completed(*args):
        completions.append(QThread.currentThread() == QApplication.instance().thread())
        original(*args)
    monkeypatch.setattr(c, "_bridge_completed", completed)
    def create(*args):
        seen.append(QThread.currentThread() != QApplication.instance().thread())
        raise WorkflowCommandError("TARGET_COURSESKILL_REQUIRED")
    monkeypatch.setattr(bridge, "create_course_study_session", create, raising=False)
    c.create_study_session("course", "Goal", ["skill"])
    qtbot.waitUntil(lambda: not c.bridge_busy, timeout=5000)
    assert seen == [True] and messages[-1].code == "TARGET_COURSESKILL_REQUIRED"
    assert completions == [True]


def test_material_session_dialog_sends_accepted_linked_skill_and_user_goal(setup, monkeypatch, qtbot):
    c, r, _ = setup
    c.state.route, c.state.selected_material_id = Route.MATERIAL_DETAIL, "material"
    screen = MaterialDetailScreen(c)
    qtbot.addWidget(screen)
    def linked(*args):
        assert r.running
        return [("accepted", "Graph"), ("unrelated", "Other")]
    monkeypatch.setattr(bridge, "course_skills", linked)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a: ("Graph (accepted)", True))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a: ("Erkläre Graphen 😀", True))
    requests = []
    def create(*args):
        assert r.running
        requests.append(args[1:])
        return NS(session_id="created")
    monkeypatch.setattr(bridge, "create_course_study_session", create, raising=False)
    screen._prepare_session("course-stable", [NS(status="ACCEPTED", proposal_kind="NEW_SKILL",
        accepted_skill_id="accepted"), NS(status="REJECTED", proposal_kind="NEW_SKILL", accepted_skill_id="unrelated")])
    assert not requests
    r.complete("study_read")
    assert c.bridge_busy
    r.complete("study_bridge")
    assert requests == [("semester-stable", "course-stable", "Erkläre Graphen 😀", ["accepted"])]
    assert c.state.selected_session_id == "created"


def test_cancelled_source_dialog_cannot_receive_initial_load(setup, monkeypatch, qtbot):
    c, r, _ = setup
    monkeypatch.setattr(document_parsing, "get_parse_run", lambda *a: (NS(page_count=1), [
        NS(id="late-unit", ordinal=1, unit_kind="PAGE", source_label="1", non_whitespace_char_count=10)]))
    d = ParsedSelectionDialog(c, "parse")
    qtbot.addWidget(d)
    d.reject()
    r.complete("parsed_source")
    assert not d.units and not d.editor.toPlainText() and not d.selected
    assert not r.tasks


def selection_rows(c):
    """Controlled inbox DTOs; transitions still use the public controller API."""
    c.materials = NS(materials=[NS(material_id=x, course_stable_id="course") for x in ("X", "Y")],
        pending_skill_proposals=[NS(proposal_id=x) for x in ("X", "Y")],
        decided_skill_proposals=[], pending_classification_proposals=[], decided_classification_proposals=[])


@pytest.mark.parametrize("repeat", [False, True])
def test_public_material_selection_invalidates_source_and_recovers(setup, monkeypatch, qtbot, repeat):
    c, r, _ = setup
    selection_rows(c)
    c.select_material("X")
    original = c.workflow_identity()
    d = source_dialog(c, r, monkeypatch, qtbot)
    d.unit_choice.setCurrentIndex(1)
    if not repeat:
        c.select_material("Y")
    c.select_material("X")
    assert c.state.route == Route.MATERIAL_DETAIL and c.workflow_identity() != original
    r.complete("parsed_unit")
    assert not d.editor.toPlainText() and not d.propose.isEnabled()
    # A new source dialog can complete and submit after the old result is ignored.
    fresh = source_dialog(c, r, monkeypatch, qtbot)
    select_graph(fresh)
    monkeypatch.setattr(bridge, "propose_selected_document_skill", lambda *a: "fresh-proposal")
    fresh.propose.click()
    assert fresh.result() == QDialog.Accepted
    assert c.propose_selected_study_skill("X", "parse-current", *fresh.selected, "Graph", "CONCEPT")
    assert not c.propose_selected_study_skill("X", "parse-current", *fresh.selected, "Graph", "CONCEPT")
    r.complete("study_bridge")
    assert not c.bridge_busy and c._pending_proposal_review[0] == "fresh-proposal"


def test_public_material_switch_discards_initial_source_and_closed_dialog(setup, monkeypatch, qtbot):
    c, r, _ = setup
    selection_rows(c)
    c.select_material("X")
    monkeypatch.setattr(document_parsing, "get_parse_run", lambda *a: (NS(page_count=1), [
        NS(id="unit", ordinal=0, unit_kind="PAGE", source_label="1", non_whitespace_char_count=1)]))
    old = ParsedSelectionDialog(c, "parse")
    qtbot.addWidget(old)
    c.select_material("Y")
    c.select_material("X")
    r.complete("parsed_source")
    assert old.units == [] and not r.tasks
    closed = ParsedSelectionDialog(c, "parse")
    qtbot.addWidget(closed)
    closed.reject()
    r.complete("parsed_source")
    assert closed.units == [] and not r.tasks


def test_public_session_roundtrip_blocks_creation_navigation_and_duplicate_callback(setup, monkeypatch):
    c, r, messages = setup
    c.select_session("X")
    monkeypatch.setattr(bridge, "create_course_study_session", lambda *a: NS(session_id="old-created"), raising=False)
    assert c.create_study_session("course", "Old goal", ["skill"])
    assert not c.create_study_session("course", "Double click", ["skill"])
    old = r.tasks[0]
    c.select_session("Y")
    c.select_session("X")
    r.complete("study_bridge")
    assert c.state.selected_session_id == "X" and c.state.route == Route.SESSION_DETAIL
    assert "previous selection" in messages[-1] and not c.bridge_busy
    monkeypatch.setattr(bridge, "create_course_study_session", lambda *a: NS(session_id="fresh-created"), raising=False)
    assert c.create_study_session("course", "Fresh goal", ["skill"])
    old[3](old[0], old[1], NS(session_id="old-created"))
    assert c.bridge_busy and c.state.selected_session_id == "X"
    r.complete("study_bridge")
    assert not c.bridge_busy and c.state.selected_session_id == "fresh-created"


def test_session_reads_bind_complete_selection_identity(setup, monkeypatch):
    from uniflow.gui import controller as module
    c, r, _ = setup
    monkeypatch.setattr(c, "refresh_session_detail", GuiController.refresh_session_detail.__get__(c))
    monkeypatch.setattr(module, "get_session_detail", lambda config, semester, sid, **kw: NS(session=NS(session_id=sid)))
    c.select_session("X")
    old = r.tasks.pop()
    c.select_session("Y")
    c.select_session("X")
    old[3](old[0], old[1], NS(session=NS(session_id="stale-X")))
    assert c.session_detail is None and c.state.busy[Route.SESSION_DETAIL] == Busy.READING
    r.complete("session_detail")  # obsolete Y
    assert c.session_detail is None and c.state.busy[Route.SESSION_DETAIL] == Busy.READING
    r.complete("session_detail")
    assert c.session_detail.session.session_id == "X" and c.state.busy[Route.SESSION_DETAIL] == Busy.IDLE
    c.back_to_sessions()
    assert c.state.selected_session_id is None


def public_review(c, r, monkeypatch):
    from uniflow.gui import controller as module
    selection_rows(c)
    def read(config, semester, pid):
        return NS(proposal=NS(proposal_id=pid, status="PENDING", proposal_kind="NEW_SKILL"),
            supported_actions=["ACCEPT_NEW", "REJECT"], review_context_token="a" * 64)
    monkeypatch.setattr(module, "get_skill_review", read)
    assert c.select_proposal_review(ReviewFamily.SKILL, "X")
    r.complete("proposal_review")


def test_public_proposal_roundtrip_discards_mutation_then_completes_fresh_decision(setup, monkeypatch):
    c, r, _ = setup
    public_review(c, r, monkeypatch)
    monkeypatch.setattr(review_workflow, "reject_skill", lambda *a: NS(outcome="REJECTED"))
    assert c.decide_proposal("REJECT")
    assert not c.decide_proposal("REJECT")
    assert c.select_proposal_review(ReviewFamily.SKILL, "Y")
    assert c.select_proposal_review(ReviewFamily.SKILL, "X")
    r.complete("review_mutation")
    assert not c.review_mutation_busy and c.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING
    r.complete("proposal_review")
    assert c.proposal_review is None
    r.complete("proposal_review")
    assert c.proposal_review.proposal.proposal_id == "X"
    assert c.decide_proposal("REJECT")
    r.complete("review_mutation")
    assert c.review_mutation_busy
    r.complete("proposal_review")
    assert not c.review_mutation_busy and c.state.busy[Route.PROPOSAL_REVIEW] == Busy.IDLE


def test_public_proposal_roundtrip_old_readback_cannot_release_new_readback(setup, monkeypatch):
    c, r, _ = setup
    public_review(c, r, monkeypatch)
    monkeypatch.setattr(review_workflow, "reject_skill", lambda *a: NS(outcome="REJECTED"))
    assert c.decide_proposal("REJECT")
    r.complete("review_mutation")
    old = r.tasks.pop()
    assert c.review_mutation_busy
    c.select_proposal_review(ReviewFamily.SKILL, "Y")
    c.select_proposal_review(ReviewFamily.SKILL, "X")
    assert not c.review_mutation_busy
    r.complete("proposal_review")
    r.complete("proposal_review")
    assert c.decide_proposal("REJECT")
    r.complete("review_mutation")
    new_identity = c._review_readback_identity
    old[3](old[0], old[1], old[2]())
    assert c.review_mutation_busy and c._review_readback_identity == new_identity
    assert c.proposal_review is None and c.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING
    r.complete("proposal_review")
    assert not c.review_mutation_busy and c.proposal_review.proposal.proposal_id == "X"
    identity = c.workflow_identity()
    c.back_from_proposal_review()
    assert c.state.selected_proposal_id is None and c.workflow_identity() != identity


def test_material_automatic_filter_and_inbox_clear_invalidate_identity(setup):
    c, r, _ = setup
    selection_rows(c)
    c.select_material("X")
    identity = c.workflow_identity()
    assert c.set_materials_filter(course_id="other-course")
    assert c.state.selected_material_id is None and c.workflow_identity() != identity
    selection_rows(c)
    c.select_material("X")
    identity = c.workflow_identity()
    c.back_to_materials()
    assert c.state.selected_material_id is None and c.workflow_identity() != identity
    selection_rows(c)
    c.select_material("X")
    identity = c.workflow_identity()
    # An inbox refresh can automatically remove a formerly selected row.
    token = (c._generation["materials"], c.state.semester_stable_id,
        c.state.materials_course_stable_id, c.state.materials_from_date, c.state.materials_through_date)
    c._materials_completed("materials", token, (NS(), NS(materials=[], from_date=NOW.date(), through_date=NOW.date()), NOW))
    assert c.state.selected_material_id is None and c.workflow_identity() != identity


def test_study_read_repeat_callback_cannot_clear_new_preparation(setup):
    c, r, _ = setup
    selection_rows(c)
    c.select_material("X")
    values, finished = [], []
    c.read_study_data(lambda: "old", values.append, finished=lambda: finished.append("old"))
    old = r.tasks[0]
    c.select_material("Y")
    c.select_material("X")
    r.complete("study_read")
    assert values == [] and finished == ["old"]
    c.read_study_data(lambda: "fresh", values.append, finished=lambda: finished.append("fresh"))
    old[3](old[0], old[1], "old")
    assert finished == ["old"] and values == []
    r.complete("study_read")
    assert values == ["fresh"] and finished == ["old", "fresh"]
