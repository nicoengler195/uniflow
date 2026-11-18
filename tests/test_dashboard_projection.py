"""Dashboard acceptance on fresh 0017 and 0019 databases only."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import os
import sys
from pathlib import Path
import pytest
from test_semester_calendar import calendar_data
from test_material_calendar import drops
from uniflow.application.dashboard_projection import project_dashboard
from uniflow.infrastructure.database.models import Semester, MaterialArtifact, MaterialReleaseRule


def instant(day=19, hour=13):
    return datetime(2026, 10, day, hour, tzinfo=ZoneInfo('Europe/Berlin'))


def test_sources_and_read_only(drops):
    config, factory, cid, _ = drops
    with factory.begin() as db:
        db.add(MaterialArtifact(id='duplicate', material_id='m1', artifact_id='a0', version_number=2, is_current=False))
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    p = project_dashboard(config, 'SYNTH', instant(19), cid)
    assert p.start == date(2026, 10, 19) and p.end == date(2026, 10, 25)
    assert len(p.classes) == 1 and p.classes[0].event_type == 'REPLACEMENT'
    assert p.classes[0].starts_at.hour == 14
    assert len(p.recent_materials) == 3
    assert len({e.artifact_id for e in p.recent_materials}) == 3
    assert all(e.day == date(2026, 10, 16) and e.date_basis == 'ARTIFACT_RECORDED_AT' for e in p.recent_materials)
    assert all(e.course_id == cid for e in (*p.preview, *p.recent_materials))
    late = project_dashboard(config, 'SYNTH', instant(19, 15), cid)
    assert not late.classes
    early = project_dashboard(config, 'SYNTH', instant(12, 8), cid)
    assert not early.classes  # cancellation removes the recurring occurrence
    assert early.deadlines[0].title == 'Date' and early.deadlines[0].starts_at is None
    assert early.expected_materials[0].status == 'CONFIRMED_PRESENT'
    assert not early.recent_materials  # future acquisition timestamps are excluded
    assert not any(e.event_type == 'MATERIAL_OBSERVED' for e in early.preview)
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()


@pytest.mark.parametrize('day', [date(2026, 10, 30), date(2026, 12, 29), date(2026, 3, 27)])
def test_local_calendar_window(calendar_data, day, monkeypatch):
    config, factory, cid, _ = calendar_data
    with factory.begin() as db:
        semester = db.query(Semester).one()
        semester.starts_on = date(2026, 1, 1)
        semester.ends_on = date(2027, 2, 1)
    from uniflow.application import dashboard_projection as module
    real = module.project_semester_calendar
    calls = []
    def capture(*args, **kwargs):
        calls.append((args[2], args[3]))
        return real(*args, **kwargs)
    monkeypatch.setattr(module, 'project_semester_calendar', capture)
    stamp = datetime(day.year, day.month, day.day, 23, 30, tzinfo=ZoneInfo('UTC'))
    p = project_dashboard(config, 'SYNTH', stamp, cid)
    assert p.start == stamp.astimezone(ZoneInfo('Europe/Berlin')).date()
    assert p.end == p.start + timedelta(days=6)
    assert calls == [(p.start-timedelta(days=6), p.end)]
    assert all(p.start <= e.day <= p.end for e in p.preview)
    with pytest.raises(ValueError, match='TIMEZONE_AWARE'):
        project_dashboard(config, 'SYNTH', datetime(2026, 1, 1))


def test_material_clock(drops):
    config, factory, cid, _ = drops
    with factory.begin() as db:
        db.get(MaterialReleaseRule, 'slides').payload_json = '{"cadence":"WEEKLY","weekday":"MONDAY","time":"09:00","material_kind":"SLIDES"}'
    early = project_dashboard(config, 'SYNTH', instant(19, 8), cid)
    late = project_dashboard(config, 'SYNTH', instant(19, 18), cid)
    assert early.expected_materials[0].status == 'EXPECTED'
    assert early.expected_materials[0].expected_time.hour == 9
    assert late.expected_materials[0].status == 'UNCONFIRMED'
    assert 'Keine Aussage über Nichtveröffentlichung' in late.expected_materials[0].notes


def test_widget_lifecycle_navigation_and_errors(drops, qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.dashboard_summary import DashboardSummary
    from uniflow.gui.routes import Route
    from uniflow.gui.presentation import PresentationError
    from PySide6.QtWidgets import QPushButton
    config, _, _, _ = drops
    controller = GuiController(config, list_semesters(config)[0], clock=lambda: instant(19))
    screen = DashboardSummary(controller)
    qtbot.addWidget(screen)
    screen.resize(560, 800)
    screen.show()
    screen.activate(True)
    controller.refresh_home()
    qtbot.waitUntil(lambda: screen.projection is not None, timeout=10000)
    current = screen.projection
    token = (controller._generation['home'], 'SYNTH', None, None)
    controller._completed('home', (token[0]-1, 'SYNTH', None, None), None)
    assert screen.projection is current
    controller.state.semester_stable_id = 'OTHER'
    controller._completed('home', token, None)
    assert screen.projection is current
    controller.state.semester_stable_id = 'SYNTH'
    controller._completed('home', token, PresentationError('TEST', 'Test', 'Synthetic failure'))
    assert 'Synthetic failure' in screen.message.text()
    screen.reload()
    qtbot.waitUntil(lambda: screen.projection is not None, timeout=10000)
    button = next(b for b in screen.findChildren(QPushButton) if b.text() == 'Semesterkalender öffnen')
    button.click()
    assert controller.state.route == Route.CALENDAR
    screen.activate(False)
    controller._completed('home', token, None)
    controller.runner.pool.waitForDone(10000)


def test_main_window(drops, qtbot, request):
    from uniflow.gui.app import MainWindow
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.theme.stylesheet import stylesheet
    from uniflow.gui.routes import Route
    config, _, _, _ = drops
    revision = request.node.callspec.params["calendar_data"]
    print(f"WINDOWS_QA pid={os.getpid()} executable={sys.executable} backend={os.environ.get('QT_QPA_PLATFORM')} schema={revision}", flush=True)
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    controller = GuiController(config, list_semesters(config)[0], clock=lambda: instant(19))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.setStyleSheet(stylesheet())
    window.resize(1000, 760)
    window.show()
    qtbot.waitUntil(lambda: window.dashboard_summary.projection is not None, timeout=10000)
    qtbot.waitUntil(lambda: controller.home is not None, timeout=10000)
    qtbot.waitUntil(lambda: window.dashboard_summary.cards.isVisible(), timeout=10000)
    if os.environ.get('DASHBOARD_QA_DIR'):
        target = Path(os.environ['DASHBOARD_QA_DIR'])
        target.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(target / ('dashboard-' + os.environ.get('QT_QPA_PLATFORM', 'windows') + '-' + revision + '.png')))
    if os.environ.get('DASHBOARD_HOLD_MS'):
        qtbot.wait(int(os.environ['DASHBOARD_HOLD_MS']))
    window.resize(760, 640)
    qtbot.waitUntil(lambda: window.scroll.verticalScrollBar().maximum() > 0)
    assert window.scroll.horizontalScrollBar().maximum() == 0
    if os.environ.get('DASHBOARD_QA_DIR'):
        window.grab().save(str(Path(os.environ['DASHBOARD_QA_DIR']) / ('dashboard-small-' + os.environ.get('QT_QPA_PLATFORM', 'windows') + '-' + revision + '.png')))
    window.dashboard_summary.reload()
    token = (controller._generation['home'], 'SYNTH', None, None)
    controller.navigate(Route.CALENDAR)
    assert not window.dashboard_summary.active
    controller._completed('home', token, None)
    controller.navigate(Route.HOME)
    qtbot.waitUntil(lambda: window.dashboard_summary.projection is not None, timeout=10000)
    window.close()
    assert not window.dashboard_summary.active
    controller.runner.pool.waitForDone(10000)
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()


def test_refresh_home_updates_changed_sources_without_navigation(drops, qtbot, monkeypatch):
    from uniflow.gui.app import MainWindow
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.infrastructure.database.models import Deadline
    from uniflow.domain.enums import DeadlineStatus, InformationStatus
    from uniflow.gui.routes import Route
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    import uniflow.gui.controller as module
    real_project = module.project_dashboard
    threads = []
    def read(*args, **kwargs):
        threads.append(QThread.currentThread() == QApplication.instance().thread())
        return real_project(*args, **kwargs)
    monkeypatch.setattr(module, 'project_dashboard', read)
    config, factory, cid, _ = drops
    controller = GuiController(config, list_semesters(config)[0], clock=lambda: instant(19))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    summary = window.dashboard_summary
    qtbot.waitUntil(lambda: summary.projection is not None, timeout=10000)
    assert not summary.projection.deadlines
    with factory.begin() as db:
        db.add(Deadline(course_id=cid, title='Refresh proof', due_at=datetime(2026,10,20),
            is_date_only=True, status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED))
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    with qtbot.waitSignal(controller.dashboard_changed, timeout=10000):
        controller.refresh_home(as_of=instant(19, 16))
    assert controller.state.route == Route.HOME
    assert summary.projection is controller.dashboard_projection
    assert summary.projection.as_of == controller.home.as_of == instant(19, 16)
    assert [e.title for e in summary.projection.deadlines] == ['Refresh proof']
    from PySide6.QtWidgets import QPushButton
    assert any('Refresh proof' in b.text() and 'Uhrzeit unbekannt' in b.text()
        for b in summary.findChildren(QPushButton))
    assert threads == [False, False]
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    window.close()
    controller.runner.pool.waitForDone(10000)


def test_home_generation_order_route_close_and_read_only(drops, qtbot):
    from uniflow.gui.app import MainWindow
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.routes import Route
    from uniflow.gui.presentation import PresentationError
    class ManualRunner:
        def __init__(self):
            self.tasks = []
        def submit(self, operation, token, fn, callback):
            self.tasks.append((operation, token, fn, callback))
        def take_home(self, generation):
            index = next(i for i, t in enumerate(self.tasks) if t[0] == 'home' and t[1][0] == generation)
            return self.tasks.pop(index)
        @staticmethod
        def finish(task):
            op, token, fn, callback = task
            callback(op, token, fn())
    config, factory, _, _ = drops
    # A second valid semester allows a real A -> B -> A race, without fabricated data.
    with factory.begin() as db:
        from uniflow.domain.enums import SemesterState
        s = Semester(stable_id='SECOND', name='Second', timezone='Europe/Berlin', state=SemesterState.ACTIVE)
        db.add(s)
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    runner = ManualRunner()
    controller = GuiController(config, next(s for s in list_semesters(config) if s.stable_id == 'SYNTH'), runner=runner, clock=lambda: instant(19))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    summary = window.dashboard_summary
    assert [t[0] for t in runner.tasks] == ['home']
    initial = runner.take_home(controller._generation['home'])
    runner.finish(initial)
    controller.refresh_home(as_of=instant(19, 14))
    old = runner.take_home(controller._generation['home'])
    controller.refresh_home(as_of=instant(19, 15))
    newest = runner.take_home(controller._generation['home'])
    runner.finish(newest)
    accepted = summary.projection
    runner.finish(old)
    assert summary.projection is accepted and accepted.as_of == instant(19, 15)
    controller.refresh_home()
    leaving = runner.take_home(controller._generation['home'])
    controller.navigate(Route.CALENDAR)
    signals = []
    controller.dashboard_changed.connect(signals.append)
    runner.finish(leaving)
    assert (summary.projection is None and summary.cards.isHidden()
            and summary.message.text() == 'Studienübersicht wird geladen …' and not signals)
    # Background refresh may update the cache, but cannot emit a hidden Home UI update.
    controller.refresh_home()
    background = runner.take_home(controller._generation['home'])
    runner.finish(background)
    assert (summary.projection is None and summary.cards.isHidden()
            and summary.message.text() == 'Studienübersicht wird geladen …' and not signals)
    controller.navigate(Route.HOME)
    returning = runner.take_home(controller._generation['home'])
    runner.finish(returning)
    assert len(signals) == 1
    controller.refresh_home()
    wrong_semester = runner.take_home(controller._generation['home'])
    controller.state.semester_stable_id = 'SECOND'
    runner.finish(wrong_semester)
    assert len(signals) == 1
    controller.refresh_home()
    other = runner.take_home(controller._generation['home'])
    controller.state.semester_stable_id = 'SYNTH'
    controller.refresh_home()
    latest = runner.take_home(controller._generation['home'])
    runner.finish(latest)
    runner.finish(other)
    assert len(signals) == 2 and summary.projection.semester_id == 'SYNTH'
    controller.refresh_home()
    failing = runner.take_home(controller._generation['home'])
    failing[3]('home', failing[1], PresentationError('SYNTHETIC', 'Failure', 'Visible error'))
    assert 'Visible error' in summary.message.text()
    controller.refresh_home()
    closing = runner.take_home(controller._generation['home'])
    window.close()
    old_message = summary.message.text()
    runner.finish(closing)
    assert summary.message.text() == old_message
    assert len(signals) == 3  # Last accepted response is the visible error, never the closed callback.
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()
