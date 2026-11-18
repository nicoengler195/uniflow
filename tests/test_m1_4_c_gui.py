"""Real Qt GUI persistence, with controlled TaskRunner delivery."""
import json
import pytest
from test_m1_4_a_gui_workflow import setup
from test_m1_4_b1_scoring_preview import prepare
from test_learning_results import setup as database_setup
from test_learning_chat_exchange import result, write
from uniflow.application.learning_chat_exchange import export_learning_chat_package
from uniflow.gui.screens.scoring_preview import ScoringPreviewDialog


def test_gui_controlled_mastery_preview(setup, qtbot, monkeypatch, tmp_path):
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    path = write(tmp_path, result(package))
    raw = path.read_bytes()
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    c.state.selected_session_id = sid
    screen.view.session.session_id = sid
    screen.view.session.current_handoff.handoff_id = handoff.handoff_id
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(path))
    screen._preview_scoring()
    dialog = screen.scoring_dialog
    runner.complete('scoring_preview')
    choices = {dialog.decisions.item(row,0).text(): dialog.decisions.cellWidget(row,1) for row in range(dialog.decisions.rowCount())}
    assert all(choice.currentData() == 'PENDING_HUMAN_REVIEW' for choice in choices.values())
    assert choices['PREREQ'].findData('ACCEPTED') == -1
    choices['TARGET'].setCurrentIndex(choices['TARGET'].findData('ACCEPTED'))
    dialog.save_button.click()
    assert not runner.tasks
    dialog.confirm.setChecked(True)
    dialog.save_button.click()
    dialog._save_review()
    assert len(runner.tasks) == 1
    path.write_text('{}')  # the reviewed bytes, not the changed path, are saved
    runner.complete('scoring_review_save')
    assert dialog.review_state.version == 1
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.mastery_button.click()
    runner.complete('controlled_mastery_preview')
    qtbot.waitExposed(dialog.mastery_dialog)
    from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QCheckBox, QLabel
    mastery = dialog.mastery_dialog
    assert 'scoring-result-reviewed-mastery-v1' in mastery.findChild(QPlainTextEdit).toPlainText()
    apply = mastery.findChildren(QPushButton)[0]
    apply.click()
    assert not runner.tasks
    for check in mastery.findChildren(QCheckBox):
        check.setChecked(True)
    apply.click()
    assert not apply.isEnabled()
    runner.complete('controlled_mastery_apply')
    assert any('Atomar angewendet' in label.text() for label in mastery.findChildren(QLabel))
    from sqlalchemy import select
    from uniflow.infrastructure.database.models import MasteryState, ScoringMasteryApplication
    with factory() as db:
        assert db.scalar(select(MasteryState)).value == 0.5
        assert db.scalar(select(ScoringMasteryApplication)).review_version == 1
    qtbot.wait(250)
    import os
    if os.environ.get('UNIFLOW_C_GUI_EVIDENCE'):
        assert mastery.grab().save(os.environ['UNIFLOW_C_GUI_EVIDENCE'])
    mastery.reject()
    sha = dialog.review_state.result_sha256
    assert not dialog.confirm.isChecked()
    dialog.reject()
    path.unlink()
    reopened = ScoringPreviewDialog(c, None, sid, handoff.handoff_id, screen, stored_sha=sha)
    qtbot.addWidget(reopened)
    runner.complete('scoring_preview')
    assert reopened.preview.raw_bytes == raw
    assert reopened.review_state.version == 1
    assert 'ACCEPTED' in reopened.review.toPlainText()
    for row in range(reopened.decisions.rowCount()):
        if reopened.decisions.item(row,0).text() == 'TARGET':
            choice = reopened.decisions.cellWidget(row,1)
            choice.setCurrentIndex(choice.findData('DEFERRED'))
    reopened.reason.setText('Correction required')
    reopened.confirm.setChecked(True)
    reopened.save_button.click()
    runner.complete('scoring_review_save')
    assert reopened.review_state.version == 2
    assert len(reopened.review_state.history) == 2
    reopened.reject()
    factory.kw['bind'].dispose()
