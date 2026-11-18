"""B2 acceptance through real validation, synthetic DB and controlled Qt delivery."""
import json
import pytest
from test_m1_4_a_gui_workflow import setup
from test_m1_4_b1_scoring_preview import prepare
from test_learning_chat_exchange import result, write
from test_learning_results import setup as database_setup
from uniflow.application.learning_chat_exchange import export_learning_chat_package


@pytest.mark.parametrize('empty', [False, True])
def test_structured_review(setup, qtbot, monkeypatch, tmp_path, empty):
    c, runner, _ = setup
    config, factory, sid, _, handoff = database_setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package, exam=False)
    literal = '<b>literal & untrusted</b>'
    if empty:
        body['tasks'] = []
        body['phase_states'][0].update(status='NOT_STARTED', difficulty_rationale=None)
        body['final_exam_decision'] = None
    else:
        body['tasks'][0].update(fraction=0.0, evidence=literal, assistance='HINTS', hints=[literal], independent_solution=False)
        body['tasks'][0]['additional_skills'] = [{'provisional_skill_name': literal, 'observation': 'Qualitative only'}]
        body['tasks'].append({'task_id': 'bonus', 'task_type': 'PREVIEW_BONUS', 'observation': literal})
        body['errors'] = [{'task_id': 'learn', 'description': literal, 'context': 'First attempt', 'status': 'SUSPECTED'}]
    path = write(tmp_path, body)
    c.config = config
    screen = prepare(c, qtbot, monkeypatch)
    c.state.selected_session_id = sid
    screen.view.session.session_id = sid
    screen.view.session.current_handoff.handoff_id = handoff.handoff_id
    monkeypatch.setattr(screen, 'choose_scoring_file', lambda: str(path))
    before = config.database_path.read_bytes()
    screen._preview_scoring()
    dialog = screen.scoring_dialog
    assert not dialog.tabs.isTabEnabled(1)
    runner.complete('scoring_preview')
    assert dialog.tabs.count() == 5 and dialog.tabs.isTabEnabled(1)
    assert dialog.task_list.count() == len(body['tasks'])
    overview = dialog.overview.toPlainText()
    assert 'Gesamtwert: Nicht bewertet' in overview
    assert 'Ergebnis-ID: Im Vertrag nicht enthalten' in overview
    assert 'PENDING_HUMAN_REVIEW' in overview
    assert dialog.raw is dialog.text
    if empty:
        assert 'Keine Aufgaben' in dialog.task_detail.toPlainText()
        assert 'Lernphasenpunkte: Nicht bewertet' in overview
    else:
        assert 'Lernphasenpunkte: 0.00' in overview
        assert literal in dialog.task_detail.toPlainText()
        assert 'Originalbewertung (Anteil 0–1): 0.0' in dialog.task_detail.toPlainText()
        assert 'SUSPECTED' in dialog.task_detail.toPlainText()
        assert 'Nicht zugeordnet' in dialog.observations.toPlainText()
        assert literal in dialog.observations.toPlainText()
        dialog.task_list.setCurrentRow(1)
        bonus = dialog.task_detail.toPlainText()
        assert 'Nicht bewertet' in bonus and 'Kein Antwortbeleg im Ergebnis enthalten.' in bonus
        assert 'Rubrik: Im Ergebnis nicht enthalten' in bonus
        assert 'bonus: Nicht bewertet' in dialog.review.toPlainText()
    for view in [dialog.overview, dialog.task_detail, dialog.observations, dialog.review, dialog.raw]:
        assert view.isReadOnly()
    dialog.reject()
    assert config.database_path.read_bytes() == before
    factory.kw['bind'].dispose()
