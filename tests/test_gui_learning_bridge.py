"""Offline first material-to-handoff path in an isolated project."""
from datetime import date, datetime, timezone
import hashlib
import json
import pytest

from uniflow.application.gui_learning_bridge import (associate_skill_with_course,
    course_skills, import_and_parse, propose_document_skill,
    propose_selected_document_skill, _unique_quote_match)
from uniflow.application.initializer import initialize_project
from uniflow.application.semester_read_models import validate_project_database
from uniflow.application.learning_handoffs import generate_handoff, handoff_info, export_handoff
from uniflow.application import learning_workflow
from uniflow.application.skill_extraction_foundation import accept_skill_proposal
from uniflow.domain.enums import SemesterState
from uniflow.gui.controller import GuiController
from uniflow.gui.app import MainWindow
from uniflow.gui.screens.material_detail import ParsedSelectionDialog, _python_offset
from uniflow.gui.routes import Route
from uniflow.gui.presentation import PresentationError, present_error
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, Semester, LearningSession, CourseSkill, Skill
from uniflow.application.document_parsing import get_parse_run, read_parsed_unit
from sqlalchemy import select
from uniflow.infrastructure.database.models import MaterialArtifact, ParseRun, SkillProposal
from uniflow.infrastructure.database.models import SemanticEvidenceSpan, SkillProposalEvidence
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, wanted=None):
        index = next((i for i, task in enumerate(self.tasks) if wanted is None or task[0] == wanted), None)
        if index is None:
            raise AssertionError(f"No queued task: {wanted}")
        operation, token, fn, callback = self.tasks.pop(index)
        self.last_exception = None
        try:
            value = fn()
        except Exception as exc:
            self.last_exception = exc
            value = present_error(exc)
        callback(operation, token, value)
        return value


def test_quote_has_exactly_one_unit_local_occurrence():
    assert _unique_quote_match("graph", [("first", "A graph exists"),
        ("second", "No match")]) == ("first", 2, 7)
    for units in ([('one', 'graph and graph')],
            [('one', 'graph'), ('two', 'another graph')],
            [('one', 'nothing'), ('two', 'else')]):
        with pytest.raises(ValueError, match="SKILL_QUOTE_MUST_MATCH_ONE_PARSED_UNIT"):
            _unique_quote_match("graph", units)


def test_gui_startup_accepts_current_audit_schema_read_only(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    validate_project_database(config)


def test_source_backed_bootstrap_and_gui_session_gate(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(id="semester", stable_id="S", name="Test", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 1), timezone="UTC"))
        db.add(Course(id="course", semester_id="semester", stable_id="C", name="Course"))
    file = tmp_path / "lecture.txt"
    file.write_text("A graph has vertices and edges.\n", encoding="utf-8")
    bad = tmp_path / "lecture.bin"
    bad.write_bytes(b"unsupported")
    with pytest.raises(ValueError, match="SUPPORTED_MATERIAL_TYPE_REQUIRED"):
        import_and_parse(config, bad, "C")
    material_id, status, error = import_and_parse(config, file, "C")
    assert status == "SUCCESS" and error is None
    again_id, again_status, _ = import_and_parse(config, file, "C")
    assert again_id == material_id and again_status == "SUCCESS"
    assert course_skills(config, "C") == []
    for name, kind, quote in (("", "CONCEPT", "graph"),
            ("Graph", "INVALID", "graph"), ("Graph", "CONCEPT", ""),
            (None, "CONCEPT", "graph"), ("Graph", "CONCEPT", None)):
        with pytest.raises(ValueError, match="SKILL_INPUT_INVALID"):
            propose_document_skill(config, material_id, name, kind, quote)
    proposal = propose_document_skill(config, material_id, "Graph basics", "CONCEPT",
        "A graph has vertices and edges.")
    decision = accept_skill_proposal(config, proposal)
    assert decision.action == "CREATE_SKILL"
    association = associate_skill_with_course(config, material_id, decision.target_id)
    assert association.action == "CREATE_COURSE_SKILL"
    assert course_skills(config, "C") == [(decision.target_id, "Graph basics")]

    runner = ManualRunner()
    semester = type("SemesterChoice", (), {"stable_id": "S", "timezone": "UTC"})()
    controller = GuiController(config, semester, runner=runner,
        clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    messages = []
    controller.feedback.connect(messages.append)
    assert controller.create_study_session("C", "Learn graphs", ["wrong"])
    assert controller.bridge_busy
    assert not controller.create_study_session("C", "Duplicate invalid", ["wrong"])
    rejected = runner.complete("study_bridge")
    assert not controller.bridge_busy
    with factory() as db:
        assert db.scalar(select(LearningSession.id)) is None
    assert isinstance(runner.last_exception, WorkflowCommandError), repr(runner.last_exception)
    assert runner.last_exception.code == "INVALID_TARGET_SKILL"
    assert isinstance(rejected, PresentationError)
    assert rejected.code == "INVALID_TARGET_SKILL"
    assert messages[-1] == rejected

    unassociated_proposal = propose_document_skill(config, material_id, "Graph structure", "CONCEPT",
        "A graph has vertices and edges.")
    unassociated = accept_skill_proposal(config, unassociated_proposal)
    assert unassociated.action == "CREATE_SKILL"
    assert unassociated.target_id != decision.target_id
    with factory() as db:
        assert db.get(Skill, unassociated.target_id).status == "ACTIVE"
        assert db.scalar(select(CourseSkill.id).where(
            CourseSkill.skill_id == unassociated.target_id)) is None
        sessions_before = list(db.scalars(select(LearningSession.id)))
        assert sessions_before == []
    assert controller.create_study_session("C", "Learn structure", [unassociated.target_id])
    assert controller.bridge_busy
    assert not controller.create_study_session("C", "Duplicate unassociated", [unassociated.target_id])
    unassociated_rejected = runner.complete("study_bridge")
    assert not controller.bridge_busy
    assert isinstance(runner.last_exception, WorkflowCommandError), repr(runner.last_exception)
    assert runner.last_exception.code == "TARGET_COURSESKILL_REQUIRED"
    assert isinstance(unassociated_rejected, PresentationError)
    assert unassociated_rejected.code == "TARGET_COURSESKILL_REQUIRED"
    assert messages[-1] == unassociated_rejected
    with factory() as db:
        assert list(db.scalars(select(LearningSession.id))) == sessions_before
        assert db.scalar(select(CourseSkill.id).where(
            CourseSkill.skill_id == unassociated.target_id)) is None
    assert course_skills(config, "C") == [(decision.target_id, "Graph basics")]

    assert controller.create_study_session("C", "Learn graphs", [decision.target_id])
    assert not controller.create_study_session("C", "Duplicate", [decision.target_id])
    created = runner.complete("study_bridge")
    assert not controller.bridge_busy
    assert created.session_id
    assert controller.state.selected_session_id == created.session_id
    assert controller.state.route == Route.SESSION_DETAIL
    handoff = generate_handoff(config, created.session_id)
    assert handoff.status == "GENERATED"
    info = handoff_info(config, created.session_id)
    assert info["session_id"] == created.session_id
    payload = LearningHandoffV2.model_validate_json(export_handoff(config, created.session_id).read_bytes())
    assert len(payload.source_data) == 1
    assert payload.source_data[0].material_id == material_id
    assert payload.source_data[0].excerpt == "A graph has vertices and edges."
    parent = tmp_path / "exports"
    parent.mkdir()
    exchange = learning_workflow.export_exchange(config, {"handoff_id": handoff.handoff_id,
        "destination_directory": parent})
    directory = parent / exchange.directory_name
    envelope = json.loads((directory / "handoff-envelope.json").read_text(encoding="utf-8"))
    assert learning_workflow.start_session(config, {"session_id": created.session_id}).outcome == "STARTED"
    result_file = tmp_path / envelope["result_filename"]
    result_file.write_text(json.dumps({
        "schema_version": payload.return_contract.expected_result_schema_version,
        "protocol_version": envelope["protocol_version"],
        "result_id": envelope["result_id"], "session_id": created.session_id,
        "handoff_id": handoff.handoff_id, "course_id": "course",
        "handoff_artifact_sha256": envelope["handoff_artifact_sha256"],
        "completed_at": "2026-10-18T13:00:00+00:00", "session_outcome": "COMPLETED",
        "skill_evidence": [{"skill_id": decision.target_id, "recall": 0.8,
            "application": 0.6}]}), encoding="utf-8")
    imported = learning_workflow.import_result_file(config, {"path": result_file})
    assert imported.outcome == "IMPORTED"
    applied = learning_workflow.apply_mastery(config, {"result_id": imported.result_id})
    assert applied.outcome == "APPLIED" and applied.created_mastery_state_ids
    factory.kw["bind"].dispose()


def test_material_import_button_uses_explicit_course_and_busy_gate(tmp_path, qtbot, monkeypatch):
    from pathlib import Path
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QPushButton
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(id="semester", stable_id="S", name="Test", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 1), timezone="UTC"))
        db.add(Course(id="course", semester_id="semester", stable_id="C", name="Course"))
    source = tmp_path / "lecture.txt"
    source.write_text("A graph has vertices and edges.\n", encoding="utf-8")
    semester = type("SemesterChoice", (), {"stable_id": "S", "timezone": "UTC", "name": "Test"})()
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner,
        clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete()  # home
    controller.navigate(Route.MATERIALS)
    runner.complete()  # materials
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(source), ""))
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("Course (C)", True))
    window.materials.import_button.click()
    assert controller.bridge_busy
    assert not window.materials.import_button.isEnabled()
    assert not controller.import_study_material(Path(source), "C")
    result = runner.complete()
    assert result[1] == "SUCCESS"
    assert not controller.bridge_busy
    assert any(task[0] == "materials" for task in runner.tasks)
    runner.complete("materials")
    assert controller.state.route == Route.MATERIAL_DETAIL
    assert controller.state.selected_material_id == result[0]
    runner.complete("material_detail")
    buttons = {b.text() for b in window.material_detail.findChildren(QPushButton)}
    assert {"Parsed text ansehen", "Skill vorschlagen"} <= buttons
    factory.kw["bind"].dispose()


def _gui_project(tmp_path, qtbot):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(id="semester", stable_id="S", name="Test", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 1), timezone="UTC"))
        db.add_all([Course(id="course", semester_id="semester", stable_id="C", name="Course"),
            Course(id="other", semester_id="semester", stable_id="O", name="Other")])
    semester = type("SemesterChoice", (), {"stable_id": "S", "timezone": "UTC", "name": "Test"})()
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner,
        clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.MATERIALS)
    runner.complete("materials")
    return config, factory, runner, controller, window


def _two_page_pdf(path, formulas=("F = m a", "x^2 + y = 0")):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for formula in formulas:
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data((f"BT /F1 12 Tf 14 TL 50 720 Td ({formula}) Tj T* "
            f"({'repeated line' if formula else ''}) Tj ET").encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as target:
        writer.write(target)


def test_pdf_selection_preserves_page_offsets_and_repeated_text(tmp_path, qtbot):
    config, factory, runner, controller, window = _gui_project(tmp_path, qtbot)
    source = tmp_path / "physics.pdf"
    _two_page_pdf(source)
    material_id, status, _ = import_and_parse(config, source, "C")
    assert status == "SUCCESS"
    with factory() as db:
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id))
        parse = db.scalar(select(ParseRun).where(ParseRun.source_artifact_id == link.artifact_id))
        parse_run_id = parse.id
    _, units = get_parse_run(parse_run_id, config)
    assert len(units) == 2
    first = read_parsed_unit(parse_run_id, units[0].ordinal, config)
    second = read_parsed_unit(parse_run_id, units[1].ordinal, config)
    assert first == "F = m a\nrepeated line"
    assert second == "x^2 + y = 0\nrepeated line"
    with pytest.raises(ValueError, match="SKILL_QUOTE_MUST_MATCH_ONE_PARSED_UNIT"):
        propose_document_skill(config, material_id, "Repeated", "CONCEPT", "repeated line")
    dialog = ParsedSelectionDialog(controller, parse_run_id, window.material_detail)
    qtbot.addWidget(dialog)
    runner.complete("parsed_source")
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: dialog.editor.toPlainText() == first, timeout=2000)
    assert dialog.unit_choice.currentData() == units[0].id
    dialog.unit_choice.setCurrentIndex(1)
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: dialog.current_text == second, timeout=2000)
    assert dialog.editor.toPlainText() == second
    cursor = dialog.editor.textCursor()
    cursor.setPosition(0)
    from PySide6.QtGui import QTextCursor
    cursor.setPosition(len(second), QTextCursor.MoveMode.KeepAnchor)
    dialog.editor.setTextCursor(cursor)
    dialog.name.setText("Equation")
    assert dialog.selected == (units[1].id, 0, len(second))
    assert "\u2029" in dialog.editor.textCursor().selectedText()
    assert "x^2 + y = 0\nrepeated line" in dialog.preview.toPlainText()
    for start, end in ((-1, 3), (0, len(second) + 1), (2, 2)):
        with pytest.raises(ValueError, match="SKILL_SELECTION_INVALID"):
            propose_selected_document_skill(config, material_id, parse_run_id,
                units[1].id, start, end, "Equation", "CONCEPT")
    with pytest.raises(ValueError, match="SKILL_SELECTION_SOURCE_CHANGED"):
        propose_selected_document_skill(config, material_id, parse_run_id,
            "wrong-unit", 0, 3, "Equation", "CONCEPT")
    proposal_id = propose_selected_document_skill(config, material_id, parse_run_id,
        units[1].id, 0, len(second), "Equation", "CONCEPT")
    with factory() as db:
        span = db.scalars(select(SemanticEvidenceSpan).join(SkillProposalEvidence,
            SkillProposalEvidence.evidence_span_id == SemanticEvidenceSpan.id).where(
                SkillProposalEvidence.proposal_id == proposal_id)).one()
        assert span.parsed_unit_id == units[1].id
        assert span.char_start == 0
        assert span.char_end == len(second)
        assert span.span_sha256 == hashlib.sha256(second.encode("utf-8")).hexdigest()
    decision = accept_skill_proposal(config, proposal_id)
    associate_skill_with_course(config, material_id, decision.target_id)
    created = learning_workflow.create_session(config, {"course_id": "course",
        "goal": "Understand equation", "target_skill_ids": [decision.target_id]})
    handoff = generate_handoff(config, created.session_id)
    payload = LearningHandoffV2.model_validate_json(export_handoff(config, handoff.handoff_id).read_bytes())
    assert payload.handoff_id == handoff.handoff_id
    assert payload.session_id == created.session_id
    assert payload.source_data[0].material_id == material_id
    assert payload.source_data[0].parsed_unit_id == units[1].id
    assert payload.source_data[0].excerpt == second
    assert _python_offset("𝑥 + y", 2) == 1
    assert _python_offset("𝑥 + y", 1) is None
    factory.kw["bind"].dispose()


@pytest.mark.parametrize("page_count", [31, 101])
def test_large_pdf_navigation_view_mode_and_cancel_are_nonmutating(tmp_path, qtbot, page_count):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    config, factory, runner, controller, window = _gui_project(tmp_path, qtbot)
    source = tmp_path / "long.pdf"
    formulas = [f"Page {number}" for number in range(1, page_count + 1)]
    formulas[9] = ""
    _two_page_pdf(source, formulas)
    material_id, status, _ = import_and_parse(config, source, "C")
    assert status == "SUCCESS"
    with factory() as db:
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id))
        parse = db.scalar(select(ParseRun).where(ParseRun.source_artifact_id == link.artifact_id))
    view = ParsedSelectionDialog(controller, parse.id, window.material_detail,
        allow_proposal=False)
    qtbot.addWidget(view)
    runner.complete("parsed_source")
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: view.current_text == "Page 1\nrepeated line", timeout=2000)
    view.show()
    qtbot.waitUntil(view.isVisible, timeout=2000)
    assert f"{page_count} Seiten" in view.page_summary.text()
    assert "1 ohne Text / OCR prüfen" in view.page_summary.text()
    assert view.unit_choice.count() == page_count
    assert not view.propose.isVisible()
    assert view.propose.isHidden()
    view.page_number.setValue(page_count)
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: view.current_text == f"Page {page_count}\nrepeated line", timeout=2000)
    assert view.unit_choice.currentIndex() == page_count - 1
    assert f"Page {page_count}" in view.editor.toPlainText()
    view.page_number.setValue(10)
    assert runner.complete("parsed_unit") == ""
    qtbot.waitUntil(lambda: "OCR" in view.status.text(), timeout=2000)
    assert "OCR" in view.unit_choice.currentText()
    assert not view.editor.toPlainText().strip()
    view.reject()
    assert view.result() == QDialog.DialogCode.Rejected
    proposal = ParsedSelectionDialog(controller, parse.id, window.material_detail)
    qtbot.addWidget(proposal)
    runner.complete("parsed_source")
    runner.complete("parsed_unit")
    proposal.page_number.setValue(page_count)
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: proposal.current_text == f"Page {page_count}\nrepeated line", timeout=2000)
    proposal.show()
    assert not proposal.propose.isEnabled()
    cursor = proposal.editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    proposal.editor.setTextCursor(cursor)
    assert not proposal.propose.isEnabled()
    proposal.name.setText(f"Page {page_count} concept")
    assert proposal.propose.isEnabled()
    proposal.page_number.setValue(10)
    assert runner.complete("parsed_unit") == ""
    qtbot.waitUntil(lambda: "OCR" in proposal.status.text(), timeout=2000)
    assert proposal.unit_choice.currentIndex() == 9
    assert proposal.current_text == ""
    assert proposal.editor.toPlainText() == ""
    assert proposal.selected is None
    assert not proposal.editor.textCursor().hasSelection()
    assert proposal.name.text() == f"Page {page_count} concept"
    assert not proposal.propose.isEnabled()
    proposal.page_number.setValue(page_count)
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: proposal.current_text == f"Page {page_count}\nrepeated line", timeout=2000)
    assert proposal.unit_choice.currentIndex() == page_count - 1
    assert proposal.editor.toPlainText() == f"Page {page_count}\nrepeated line"
    assert proposal.selected is None
    assert not proposal.propose.isEnabled()
    cursor = proposal.editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    proposal.editor.setTextCursor(cursor)
    assert proposal.selected == (proposal.unit_choice.currentData(), 0, len(proposal.current_text))
    assert proposal.name.text() == f"Page {page_count} concept"
    assert proposal.propose.isEnabled()
    cancelled = ParsedSelectionDialog(controller, parse.id, window.material_detail)
    qtbot.addWidget(cancelled)
    runner.complete("parsed_source")
    runner.complete("parsed_unit")
    cancelled.page_number.setValue(page_count)
    runner.complete("parsed_unit")
    qtbot.waitUntil(lambda: cancelled.current_text == f"Page {page_count}\nrepeated line", timeout=2000)
    cancelled.name.setText("Cancelled skill")
    cancelled.reject()
    assert cancelled.result() == QDialog.DialogCode.Rejected
    qtbot.mouseClick(proposal.propose, Qt.MouseButton.LeftButton)
    assert proposal.result() == QDialog.DialogCode.Accepted
    assert proposal.selected[0] == proposal.unit_choice.currentData()
    with factory() as db:
        assert db.scalar(select(SkillProposal.id)) is None
    assert not window.windowIcon().isNull()
    factory.kw["bind"].dispose()


@pytest.mark.parametrize("leave_detail", [False, True])
def test_proposal_opens_review_after_readback_unless_user_navigates(tmp_path, qtbot, leave_detail):
    config, factory, runner, controller, _ = _gui_project(tmp_path, qtbot)
    source = tmp_path / "note.txt"
    source.write_text("Force equals mass times acceleration", encoding="utf-8")
    material_id, status, _ = import_and_parse(config, source, "C")
    assert status == "SUCCESS"
    controller.refresh_materials()
    runner.complete("materials")
    controller.select_material(material_id)
    runner.complete("material_detail")
    with factory() as db:
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id))
        parse = db.scalar(select(ParseRun).where(ParseRun.source_artifact_id == link.artifact_id))
    _, units = get_parse_run(parse.id, config)
    assert controller.propose_selected_study_skill(material_id, parse.id, units[0].id,
        0, 5, "Force", "CONCEPT")
    proposal_id = runner.complete("study_bridge")
    if leave_detail:
        controller.navigate(Route.HOME)
    runner.complete("materials")
    while any(task[0] == "material_detail" for task in runner.tasks):
        runner.complete("material_detail")
    if leave_detail:
        assert controller.state.route == Route.HOME
    else:
        assert controller.state.route == Route.PROPOSAL_REVIEW
        assert controller.state.selected_proposal_id == proposal_id
        runner.complete("proposal_review")
    factory.kw["bind"].dispose()


def test_two_imports_are_visible_and_newest_opens_after_readback(tmp_path, qtbot):
    from PySide6.QtWidgets import QLabel, QPushButton
    config, factory, runner, controller, window = _gui_project(tmp_path, qtbot)
    ids = []
    for name in ("one.txt", "two.txt"):
        file = tmp_path / name
        file.write_text(name + " content", encoding="utf-8")
        assert controller.import_study_material(file, "C")
        result = runner.complete("study_bridge")
        ids.append(result[0])
        assert controller.state.route == Route.MATERIALS
        runner.complete("materials")
        assert controller.state.selected_material_id == result[0]
        assert controller.state.route == Route.MATERIAL_DETAIL
        runner.complete("material_detail")
        controller.back_to_materials()
    assert len({m.material_id for m in controller.materials.materials}) == 2
    next(button for button in window.materials.findChildren(QPushButton)
        if button.text() == "Alle Materialien und Filterergebnisse").click()
    labels = [x.text() for x in window.materials.findChildren(QLabel)]
    assert "one.txt" in labels and "two.txt" in labels
    assert ids[0] != ids[1]
    factory.kw["bind"].dispose()


def test_failed_parse_opens_detail_with_prerequisite_explanation(tmp_path, qtbot):
    from PySide6.QtWidgets import QLabel, QPushButton
    config, factory, runner, controller, window = _gui_project(tmp_path, qtbot)
    file = tmp_path / "broken.pdf"
    file.write_bytes(b"not a PDF")
    assert controller.import_study_material(file, "C")
    result = runner.complete("study_bridge")
    assert result[1] != "SUCCESS"
    runner.complete("materials")
    runner.complete("material_detail")
    assert controller.state.route == Route.MATERIAL_DETAIL
    words = " ".join(x.text() for x in window.material_detail.findChildren(QLabel))
    assert "Parsing failed" in words or "OCR is required" in words
    assert "Skill vorschlagen" not in {b.text() for b in window.material_detail.findChildren(QPushButton)}
    factory.kw["bind"].dispose()


def test_navigation_change_during_import_does_not_open_late_detail(tmp_path, qtbot):
    config, factory, runner, controller, _ = _gui_project(tmp_path, qtbot)
    file = tmp_path / "lecture.txt"
    file.write_text("A graph has vertices.", encoding="utf-8")
    assert controller.import_study_material(file, "C")
    controller.navigate(Route.HOME)
    runner.complete("study_bridge")
    runner.complete("materials")
    assert controller.state.route == Route.HOME
    assert controller.state.selected_material_id is None
    controller.navigate(Route.MATERIALS)
    assert controller.state.route == Route.MATERIALS
    factory.kw["bind"].dispose()


def test_late_readback_respects_navigation_and_existing_filter(tmp_path, qtbot):
    config, factory, runner, controller, _ = _gui_project(tmp_path, qtbot)
    assert controller.set_materials_filter(course_id="O")
    runner.complete("materials")
    file = tmp_path / "lecture.txt"
    file.write_text("A graph has vertices.", encoding="utf-8")
    assert controller.import_study_material(file, "C")
    imported = runner.complete("study_bridge")
    assert controller.state.materials_course_stable_id is None
    runner.complete("materials")
    assert controller.state.selected_material_id == imported[0]
    runner.complete("material_detail")
    controller.back_to_materials()
    assert controller.import_study_material(file, "C")
    runner.complete("study_bridge")
    controller.navigate(Route.HOME)
    runner.complete("materials")
    assert controller.state.route == Route.HOME
    assert controller.state.selected_material_id is None
    factory.kw["bind"].dispose()


def test_material_detail_explains_next_skill_step(tmp_path, qtbot, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton, QMessageBox, QInputDialog
    config, factory, runner, controller, window = _gui_project(tmp_path, qtbot)
    window.show()
    qtbot.waitUntil(window.isVisible)
    file = tmp_path / "lecture.txt"
    file.write_text("A graph has vertices and edges.", encoding="utf-8")
    assert controller.import_study_material(file, "C")
    material_id = runner.complete("study_bridge")[0]
    runner.complete("materials")
    runner.complete("material_detail")
    runner.complete("home")  # Actual import refresh, not an abandoned queued result.
    def buttons():
        return {b.text() for b in window.material_detail.findChildren(QPushButton)}
    assert "Skill vorschlagen" in buttons()
    assert controller.propose_study_skill(material_id, "Graph basics", "CONCEPT",
        "A graph has vertices and edges.")
    proposal_id = runner.complete("study_bridge")
    runner.complete("material_detail")
    assert "Skill im Review bestätigen" in buttons()
    assert controller.state.route == Route.PROPOSAL_REVIEW
    assert controller.state.selected_proposal_id == proposal_id
    runner.complete("materials")  # Review is active, so this does not queue detail again.
    runner.complete("proposal_review")
    assert runner.tasks == []
    assert course_skills(config, "C") == []
    decision = accept_skill_proposal(config, proposal_id)
    assert course_skills(config, "C") == []
    controller.back_from_proposal_review()  # navigate() itself queues the detail read.
    assert controller.state.route == Route.MATERIAL_DETAIL
    assert [task[0] for task in runner.tasks] == ["material_detail"]
    runner.complete("material_detail")
    assert runner.complete("study_read") == []
    assert runner.tasks == []
    qtbot.waitUntil(lambda: "Skill dem Fach zuordnen" in buttons(), timeout=2000)
    assert "Skill dem Fach zuordnen" in buttons()
    associate_button = next(b for b in window.material_detail.action_buttons
        if b.text() == "Skill dem Fach zuordnen")
    qtbot.waitUntil(associate_button.isVisible, timeout=2000)
    assert controller.state.route == Route.MATERIAL_DETAIL
    assert associate_button.isVisible() and associate_button.isEnabled()
    with factory() as db:
        before = list(db.scalars(select(SkillProposal.id)))
        assert db.scalar(select(CourseSkill.id)) is None
    # Invalid actual service input exercises the real failure path without a shim.
    assert controller.associate_study_skill(material_id, "unknown-skill")
    assert controller.bridge_busy
    failed = runner.complete("study_bridge")
    assert isinstance(runner.last_exception, ValueError)
    assert isinstance(failed, PresentationError) and failed.code == "SKILL_COURSE_INVALID"
    assert not controller.bridge_busy
    with factory() as db:
        assert list(db.scalars(select(SkillProposal.id))) == before
        assert db.scalar(select(CourseSkill.id)) is None
    assert course_skills(config, "C") == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    assert controller.state.route == Route.MATERIAL_DETAIL
    assert associate_button.isVisible() and associate_button.isEnabled()
    window.scroll.ensureWidgetVisible(associate_button)
    qtbot.mouseClick(associate_button, Qt.MouseButton.LeftButton)
    assert controller.bridge_busy
    associated = runner.complete("study_bridge")
    assert associated.action == "CREATE_COURSE_SKILL"
    assert not controller.bridge_busy
    runner.complete("material_detail")
    assert runner.complete("study_read") == [(decision.target_id, "Graph basics")]
    # Complete the separate materials refresh only after the first detail callback:
    # it queues a second real detail read, without invalidating an unconsumed first.
    runner.complete("materials")
    assert [task[0] for task in runner.tasks].count("material_detail") == 1
    runner.complete("material_detail")
    assert runner.complete("study_read") == [(decision.target_id, "Graph basics")]
    runner.complete("home")
    runner.complete("week")
    assert runner.tasks == []
    with factory() as db:
        assert db.scalar(select(CourseSkill.skill_id).where(
            CourseSkill.skill_id == decision.target_id)) == decision.target_id
    qtbot.waitUntil(lambda: "Lernsession vorbereiten" in buttons(), timeout=2000)
    assert "Lernsession vorbereiten" in buttons()
    prepare_button = next(b for b in window.material_detail.action_buttons
        if b.text() == "Lernsession vorbereiten")
    qtbot.waitUntil(prepare_button.isVisible, timeout=2000)
    assert controller.state.route == Route.MATERIAL_DETAIL
    assert prepare_button.isVisible() and prepare_button.isEnabled()
    prompts = []
    def choose_skill(parent, title, label, choices, *args):
        prompts.append(choices)
        return choices[0], True
    def cancel_goal(*args):
        prompts.append("goal")
        return "", False
    monkeypatch.setattr(QInputDialog, "getItem", choose_skill)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", cancel_goal)
    window.scroll.ensureWidgetVisible(prepare_button)
    qtbot.mouseClick(prepare_button, Qt.MouseButton.LeftButton)
    assert prompts == []
    assert runner.complete("study_read") == [(decision.target_id, "Graph basics")]
    assert prompts == [[f"Graph basics ({decision.target_id})"], "goal"]
    assert not window.material_detail._preparing_session and not controller.bridge_busy
    with factory() as db:
        assert db.scalar(select(LearningSession.id)) is None
    factory.kw["bind"].dispose()
