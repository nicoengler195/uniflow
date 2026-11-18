"""Presentation scale acceptance using detached synthetic projections only."""
import hashlib
from PySide6.QtWidgets import QPushButton
from test_gui_4c4a import project, setup


def test_185_forecasts_preserved_and_collapsed(qtbot, project):
    config, _, _ = project
    controller, window, runner = setup(qtbot, project)
    screen = window.materials
    original = screen.view
    seed = original.projected_expectations[0]
    forecasts = [seed.model_copy(update={
        'rule_stable_id': f'SYNTHETIC_{i}',
        'expectation_id': f'SYNTHETIC_{i}@{seed.release_date.isoformat()}'}, deep=True)
        for i in range(185)]
    view = original.model_copy(update={'projected_expectations': forecasts})
    before_view = view.model_dump_json()
    before_db = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    screen.library_mode = 'all'
    screen.render((screen.context, view))
    toggle = next(b for b in screen.findChildren(QPushButton)
                  if 'Weitere 173 Prognosen' in b.text())
    assert not toggle.isChecked()
    toggle.click()
    assert toggle.isChecked()
    toggle.click()
    assert not toggle.isChecked()
    assert view.model_dump_json() == before_view
    assert len(screen.view.projected_expectations) == 185
    assert hashlib.sha256(config.database_path.read_bytes()).hexdigest() == before_db
    window.close()
