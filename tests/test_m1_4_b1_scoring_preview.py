"""B1 real Application fixtures plus controlled asynchronous Qt delivery."""
import json
from types import SimpleNamespace as NS

import pytest
from PySide6.QtWidgets import QPlainTextEdit

from test_learning_chat_exchange import result, write
from test_learning_results import setup as database_setup
from test_m1_4_a_gui_workflow import setup, detail
from uniflow.application.learning_chat_exchange import export_learning_chat_package
from uniflow.gui.routes import Route
from uniflow.gui.screens.session_detail import SessionDetailScreen


def prepare(c, qtbot, monkeypatch):
    c.state.route = Route.SESSION_DETAIL
    c.state.selected_session_id = 'session-1'
    c.session_detail = detail()
    screen = SessionDetailScreen(c)
    qtbot.addWidget(screen)
    screen.render(c.session_detail)
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: 'synthetic.json')
    return screen


@pytest.mark.parametrize('exam', [True, False])
def test_real_preview_read_only(setup, qtbot, monkeypatch, tmp_path, exam):
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package, exam=exam)
    body['tasks'][0]['evidence'] = '<img src=x onerror=alert(1)><b>untrusted</b>'
    path = write(tmp_path, body)
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    c.state.selected_session_id = sid
    screen.view.session.session_id = sid
    screen.view.session.current_handoff.handoff_id = handoff.handoff_id
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(path))
    before = config.database_path.read_bytes()
    for _ in range(2):
        screen._preview_scoring()
        dialog = screen.scoring_dialog
        assert 'wird geprüft' in dialog.text.toPlainText()
        screen._preview_scoring()
        assert len(runner.tasks) == 1
        runner.complete('scoring_preview')
        text = dialog.text.toPlainText()
        assert 'PENDING_HUMAN_REVIEW' in text
        assert f'Session: {sid}' in text
        assert f'Handoff: {handoff.handoff_id}' in text
        assert 'Lernphasenpunkte: 25.00' in text
        assert ('Gesamtwert: 75.00' if exam else 'Gesamtwert: Nicht bewertet') in text
        assert '<img src=x onerror=alert(1)><b>untrusted</b>' in text
        assert isinstance(dialog.text, QPlainTextEdit)
        dialog.reject()
    assert config.database_path.read_bytes() == before
    factory.kw['bind'].dispose()


@pytest.mark.parametrize('change,expected', [
    ('json', 'kein gültiges'), ('schema', 'Scoring-Vertrag'),
    ('handoff', 'aktuelle Handoff'), ('hash', 'Hash'), ('skill', 'Skill-ID')])
def test_invalid_recovery(setup, qtbot, monkeypatch, tmp_path, change, expected):
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package)
    if change == 'schema': body['contract_version'] = 'wrong'
    if change == 'handoff': body['handoff_id'] = 'foreign'
    if change == 'hash': body['handoff_artifact_sha256'] = '0'*64
    if change == 'skill': body['tasks'][0]['skill_marks'][0]['skill_id'] = 'foreign'
    path = write(tmp_path, body)
    if change == 'json': path.write_text('{')
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(path))
    before = config.database_path.read_bytes()
    screen._preview_scoring()
    runner.complete('scoring_preview')
    assert expected in screen.scoring_dialog.text.toPlainText()
    assert 'Traceback' not in screen.scoring_dialog.text.toPlainText()
    screen.scoring_dialog.reject()
    screen._preview_scoring()
    assert len(runner.tasks) == 1
    screen.scoring_dialog.reject()
    runner.complete('scoring_preview')
    assert not screen.scoring_dialog.isVisible()
    assert config.database_path.read_bytes() == before
    factory.kw['bind'].dispose()


def test_navigation_cancel_close_and_binding(setup, qtbot, monkeypatch):
    c, runner, messages = setup
    screen = prepare(c, qtbot, monkeypatch)
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: '')
    screen._preview_scoring()
    assert not runner.tasks
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: 'synthetic.json')
    screen._preview_scoring()
    old = screen.scoring_dialog
    c.select_session('Y')
    c.select_session('session-1')
    runner.complete('scoring_preview')
    assert not old.active and not old.isVisible() and not messages
    c.session_detail = screen.view
    screen._preview_scoring()
    current = screen.scoring_dialog
    operation, token, _, callback = runner.tasks.pop()
    callback(operation, token, NS(result=NS(session_id='foreign', handoff_id='handoff-7')))
    assert 'nicht zur ausgewählten Session' in current.text.toPlainText()
    current.reject()


def test_stale_visible_detail_cannot_submit(setup, qtbot, monkeypatch):
    c, runner, _ = setup
    screen = prepare(c, qtbot, monkeypatch)
    c.state.selected_session_id = 'Y'
    c.session_detail = None
    screen.sync()
    assert not screen.scoring_button.isEnabled()
    screen._preview_scoring()
    assert not runner.tasks


def test_owner_close_discards_late_success(setup, qtbot, monkeypatch):
    c, runner, _ = setup
    screen = prepare(c, qtbot, monkeypatch)
    screen.show()
    screen._preview_scoring()
    dialog = screen.scoring_dialog
    screen.close()
    assert not dialog.active
    operation, token, _, callback = runner.tasks.pop()
    callback(operation, token, NS(result=NS(session_id='session-1', handoff_id='handoff-7')))
    assert not dialog.isVisible()
