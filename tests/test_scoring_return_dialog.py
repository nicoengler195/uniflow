"""Draft rejection, routing and native scoring surfaces without production data."""
import json
from PySide6.QtGui import QPalette
from test_learning_chat_exchange import result, write
from test_learning_results import setup as database_setup
from test_m1_4_a_gui_workflow import setup
from test_m1_4_b1_scoring_preview import prepare
from uniflow.application.learning_chat_exchange import export_learning_chat_package
from uniflow.application.learning_workflow import import_result_file
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.gui.presentation import present_error
from uniflow.gui.theme.stylesheet import stylesheet
import pytest


def test_draft_rejection_readable_and_no_write(setup, qtbot, monkeypatch, tmp_path):
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package)
    body['tasks'][0]['answered_at'] = None
    draft = {'status': 'REVIEW_DRAFT_NOT_IMPORTABLE', 'result_draft': body}
    path = write(tmp_path, draft)
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(path))
    before = config.database_path.read_bytes()
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    old = app.styleSheet()
    app.setStyleSheet(stylesheet())
    try:
        screen._preview_scoring()
        runner.complete('scoring_preview')
        view = screen.scoring_dialog
        text = view.overview.toPlainText()
        assert 'Prüfentwurf erkannt. Für diese Datei ist noch keine Reviewentscheidung möglich.' in text
        assert 'Antwortzeit zu Aufgabe 1' in text
        assert 'tasks.0.answered_at' not in text
        assert 'SCORING_RESULT_REVIEW_DRAFT' in view.raw.toPlainText()
        assert view.tabs.isTabEnabled(view.tabs.indexOf(view.raw))
        for widget in (view.decisions, view.reason, view.confirm, view.mastery_button, view.save_button):
            assert widget.isHidden()
        assert not view.confirm.isEnabled() and not view.confirm.isChecked()
        assert not view.other_file.isHidden()
        assert not view.save_button.isEnabled() and not view.mastery_button.isEnabled()
        view.show(); app.processEvents()
        for widget in [view.overview, view.task_detail, view.decisions, view.reason]:
            widget.ensurePolished()
            assert widget.palette().color(QPalette.ColorRole.Base).name() == '#101a31'
            assert widget.palette().color(QPalette.ColorRole.Text).name() == '#edf6ff'
        assert view.palette().color(QPalette.ColorRole.Window).name() == '#172441'
        view.reject()
        with pytest.raises(WorkflowCommandError) as exc:
            import_result_file(config, {'path': path})
        assert exc.value.code == 'RESULT_SCORING_FORMAT_SELECTED'
        assert 'LearningResultV2' in present_error(exc.value).message
        assert config.database_path.read_bytes() == before
    finally:
        app.setStyleSheet(old)
        factory.kw['bind'].dispose()
import os
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import select
from test_learning_results import payload
from uniflow.application import learning_results
from uniflow.application.session_detail_read_models import get_session_detail
from uniflow.gui.screens.session_detail import SessionDetailScreen
from uniflow.infrastructure.database.models import MasteryState


@pytest.mark.parametrize('numeric', [False, True])
def test_v2_confirmation_cancel_then_complete_without_hidden_mastery(setup, qtbot, tmp_path, numeric):
    c, runner, _ = setup
    config, factory, sid, course, handoff = database_setup(tmp_path)
    learning_results.start_learning_session(config, sid)
    body = payload(sid, course, handoff)
    body['skill_evidence'] = [{'skill_id': 'TARGET', 'independent_solution': False}]
    if numeric:
        body['skill_evidence'][0]['application'] = 0.0  # Known zero still creates numeric mastery.
    learning_results.import_learning_result(config, write(tmp_path, body))
    c.config = config
    c.state.selected_session_id = sid
    c.session_detail = get_session_detail(config, 'SYN', sid, as_of=datetime.now(timezone.utc))
    screen = SessionDetailScreen(c)
    qtbot.addWidget(screen)
    screen.render(c.session_detail)
    assert ('Mastery-Werte anwenden' if numeric else 'ohne Mastery-Änderung') in screen.primary.text()
    before = config.database_path.read_bytes()
    screen.primary.click()
    dialog = screen.completion_dialog
    assert not dialog.confirm_button.isEnabled()
    runner.complete('session_completion_preview')
    assert dialog.confirm_button.isEnabled()
    assert ('neuer Mastery-Wert 0.00%' if numeric else 'kein neuer numerischer Mastery-Wert') in dialog.details.toPlainText()
    dialog.reject()
    assert config.database_path.read_bytes() == before
    assert not any(task[0] == 'learning_mutation' for task in runner.tasks)
    screen.primary.click()
    dialog = screen.completion_dialog
    runner.complete('session_completion_preview')
    dialog.show()
    from PySide6.QtWidgets import QApplication
    QApplication.instance().processEvents()
    capture = os.environ.get('UNIFLOW_CAPTURE_DIR')
    if capture:
        Path(capture).mkdir(parents=True, exist_ok=True)
        dialog.grab().save(str(Path(capture) / ('v2-numeric-confirmation.png' if numeric else 'v2-no-mastery-confirmation.png')))
    dialog.confirm_button.click()
    runner.complete('learning_mutation')
    reopened = get_session_detail(config, 'SYN', sid, as_of=datetime.now(timezone.utc))
    assert reopened.session.state == 'COMPLETED'
    screen.render(reopened)
    assert screen.primary is None
    with factory() as db:
        states = list(db.scalars(select(MasteryState)))
        assert len(states) == int(numeric)
        if numeric:
            assert states[0].value == 0.0
    factory.kw['bind'].dispose()


def test_completion_preview_invalidates_when_selection_changes(setup, qtbot, tmp_path):
    from uniflow.gui.screens.session_completion import SessionCompletionDialog
    c, runner, _ = setup
    config, factory, sid, course, handoff = database_setup(tmp_path)
    learning_results.start_learning_session(config, sid)
    body = payload(sid, course, handoff)
    learning_results.import_learning_result(config, write(tmp_path, body))
    c.config = config
    c.state.selected_session_id = sid
    c.session_detail = get_session_detail(config, 'SYN', sid, as_of=datetime.now(timezone.utc))
    before = config.database_path.read_bytes()
    dialog = SessionCompletionDialog(c, c.session_detail)
    qtbot.addWidget(dialog)
    dialog.open()
    c.select_session('other')
    runner.complete('session_completion_preview')
    assert not dialog.active and not dialog.isVisible()
    dialog.confirm_button.click()
    assert not any(task[0] == 'learning_mutation' for task in runner.tasks)
    assert config.database_path.read_bytes() == before
    factory.kw['bind'].dispose()

def test_rejected_draft_can_choose_another_file_without_saving(setup, qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package)
    valid = tmp_path / 'valid-scoring.json'
    valid.write_text(json.dumps(body), encoding='utf-8')
    draft = write(tmp_path, {'status': 'REVIEW_DRAFT_NOT_IMPORTABLE', 'result_draft': body})
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    c.state.selected_session_id = sid
    screen.view.session.session_id = sid
    screen.view.session.current_handoff.handoff_id = handoff.handoff_id
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(draft))
    before = config.database_path.read_bytes()
    screen._preview_scoring()
    runner.complete('scoring_preview')
    old = screen.scoring_dialog
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: ('', ''))
    old.other_file.click()
    assert old.active and not runner.tasks
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(valid), 'JSON'))
    old.other_file.click()
    new = screen.scoring_dialog
    assert new is not old and not old.active
    runner.complete('scoring_preview')
    assert hasattr(new, 'preview') and not new.confirm.isHidden() and new.confirm.isEnabled()
    assert not new.save_button.isHidden() and new.other_file.isHidden()
    new.reject()
    assert config.database_path.read_bytes() == before
    factory.kw['bind'].dispose()
