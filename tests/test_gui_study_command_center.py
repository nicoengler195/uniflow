"""Phase 2A GUI behavior over isolated canonical projects."""
from datetime import date, datetime, time
import json
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QPushButton
from sqlalchemy import select, text

from uniflow.application.initializer import initialize_project
from uniflow.application.semester_read_models import list_semesters
from uniflow.application.weekly_plans import create_weekly_plan
from uniflow.domain.enums import DeadlineStatus, InformationStatus, SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.routes import Route
from uniflow.gui.screens.week import DayTile
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (Course, Deadline, PlanningProfile,
    ScheduleEntry, ScheduleException, Semester, WeeklyPlanAcceptance, WeeklyPlanRevision)
from uniflow.application.planning_calendar import project_academic_week
from uniflow.gui.components import VisualBadge
from uniflow.gui.components import AcademicEventButton, DeadlineButton
from uniflow.gui.deadline_presentation import display_title, timing_label
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.screens.home import ImageHeroCard
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual

MON = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("Europe/Berlin"))


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, name):
        index = next(i for i, task in enumerate(self.tasks) if task[0] == name)
        operation, token, fn, callback = self.tasks.pop(index)
        callback(operation, token, fn())


def project(tmp_path, qtbot):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="S", name="Semester", state=SemesterState.ACTIVE,
            timezone="Europe/Berlin")
        db.add(semester)
        db.flush()
        course = Course(stable_id="PHYS", name="Physik", semester_id=semester.id,
            information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        course_id = course.id
    runner = ManualRunner()
    controller = GuiController(config, list_semesters(config)[0], runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    runner.complete("home")
    return config, factory, course_id, runner, controller, window


def _entry(db, course_id, stable_id, weekday, start, end, *, location=None):
    payload = dict(stable_id=stable_id, activity_type="LECTURE", weekday=weekday,
        start_time=start, end_time=end, location=location, participation="REQUIRED",
        information_status="CONFIRMED")
    db.add(ScheduleEntry(stable_id=stable_id, course_id=course_id,
        payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))


def _assert_row_geometry(row):
    assert row.height() >= row.heightForWidth(row.width())
    for child in row.findChildren(QLabel):
        assert child.isVisible()
        rect = child.rect().translated(child.mapTo(row, QPoint(0, 0)))
        assert row.rect().contains(rect)
        if child.text():
            assert child.height() >= child.heightForWidth(child.width())


def test_central_views_fit_scroll_viewport_when_resized(tmp_path, qtbot):
    config, factory, course_id, runner, controller, window = project(tmp_path, qtbot)
    with factory.begin() as db:
        semester_id = db.get(Course, course_id).semester_id
        for stable, name in (("MATH", "Mathematik"), ("NEURO", "Neuro- und Verhaltensbiologie"),
                             ("PDP", "Prinzipien der Programmierung")):
            db.add(Course(stable_id=stable, name=name, semester_id=semester_id,
                information_status=InformationStatus.CONFIRMED))
        _entry(db, course_id, "LONG", "MONDAY", "09:00", "10:00", location="Raum A")
        db.add(Deadline(stable_id="LONG_DUE", course_id=course_id,
            title="Ein ausführlicher Titel für eine heute fällige Studienleistung mit mehreren Wörtern",
            due_at=datetime(2026, 10, 19), is_date_only=True,
            status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED))
    controller.refresh_home()
    runner.complete("home")
    for width in (1360, 1920, 1080, 1360):
        window.resize(width, 860)
        qtbot.wait(20)
        assert window.scroll.horizontalScrollBar().maximum() == 0
        assert window.home.course_grid.count() == 4
        columns = {window.home.course_grid.getItemPosition(index)[1] for index in range(4)}
        assert len(columns) == (2 if width == 1080 else 4)
        summary = window.dashboard_summary
        assert len(summary.projection.classes) == 1
        assert len(summary.projection.deadlines) == 1
        rows = [*summary.findChildren(AcademicEventButton), *summary.findChildren(DeadlineButton)]
        assert len(rows) >= 2
        for row in rows:
            if row.isVisible():
                _assert_row_geometry(row)
    for route, read_kind in ((Route.WEEK, "week"), (Route.MATERIALS, "materials"),
                             (Route.SESSIONS, "sessions")):
        controller.navigate(route)
        runner.complete(read_kind)
        qtbot.wait(20)
        assert window.scroll.horizontalScrollBar().maximum() == 0
        if route == Route.WEEK:
            for row in [*window.week.findChildren(AcademicEventButton),
                        *window.week.findChildren(DeadlineButton)]:
                if row.isVisible():
                    _assert_row_geometry(row)
    factory.kw["bind"].dispose()


def test_deadline_clock_boundary_and_date_only_end_of_day(tmp_path, qtbot):
    config, factory, course_id, runner, controller, window = project(tmp_path, qtbot)
    with factory.begin() as db:
        db.add_all([
            Deadline(stable_id="CLOCK", course_id=course_id, title="Timed due",
                due_at=datetime(2026, 10, 19, 9), is_date_only=False,
                status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED),
            Deadline(stable_id="DATE", course_id=course_id, title="Date only",
                due_at=datetime(2026, 10, 19), is_date_only=True,
                status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED),
        ])
    controller.refresh_home(as_of=datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("Europe/Berlin")))
    runner.complete("home")
    assert any("Timed due" in b.text() for b in window.dashboard_summary.findChildren(QPushButton))
    today_due = next(b for b in window.dashboard_summary.findChildren(DeadlineButton)
        if "Timed due" in b.text())
    assert today_due.property("deadlineTiming") == "Heute fällig"
    assert "09:00" in today_due.text() and "Abgabestatus unbekannt" in today_due.text()
    controller.refresh_home(as_of=datetime(2026, 10, 19, 10, tzinfo=ZoneInfo("Europe/Berlin")))
    runner.complete("home")
    visible = [b.text() for b in window.dashboard_summary.findChildren(QPushButton)]
    assert any("Date only" in text and "Uhrzeit unbekannt" in text for text in visible)
    assert not any("Timed due" in text for text in visible)
    next(b for b in window.dashboard_summary.findChildren(QPushButton)
        if b.text() == "Vergangene Fristen anzeigen").click()
    visible = [b.text() for b in window.dashboard_summary.findChildren(QPushButton)]
    assert any("Timed due" in text and "Vergangen" in text and "09:00" in text
        for text in visible)
    assert any("Abgabestatus unbekannt" in text for text in visible)
    controller.open_week_day(MON)
    runner.complete("week")
    week_deadlines = window.week.findChildren(DeadlineButton)
    assert any("Timed due" in button.text() and "09:00" in button.text()
        and "Physik" in button.text() for button in week_deadlines)
    assert any("Date only" in button.text() and "Uhrzeit unbekannt" in button.text()
        for button in week_deadlines)
    factory.kw["bind"].dispose()


def test_deadline_display_title_only_removes_matching_due_suffix():
    day = date(2026, 10, 19)
    assert display_title("Übungsblatt 1 – Abgabe 19.10.2026", day) == "Übungsblatt 1 – Abgabe"
    assert display_title("Übungsblatt 1 – Abgabe 20.10.2026", day) == "Übungsblatt 1 – Abgabe 20.10.2026"
    assert display_title("Seminar am 19.10.2026", day) == "Seminar am 19.10.2026"


def test_deadline_timing_and_warning_only_for_open_overdue(qtbot):
    from types import SimpleNamespace
    from uniflow.gui.theme.tokens import DANGER, WARNING
    zone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 19, 8, tzinfo=zone)
    item = SimpleNamespace(due_date=None, due_at=datetime(2026, 10, 20, 9, tzinfo=zone))
    assert timing_label(item, now, zone) == "Anstehend"
    item.due_at = datetime(2026, 10, 19, 9, tzinfo=zone)
    assert timing_label(item, now, zone) == "Heute fällig"
    item.due_at = datetime(2026, 10, 19, 7, tzinfo=zone)
    assert timing_label(item, now, zone) == "Vergangen"
    identity = resolve_course_visual("PHYS", "Physik")
    for timing, status, expected in (("Anstehend", "OPEN", ""),
            ("Heute fällig", "OPEN", WARNING), ("Vergangen", "UNKNOWN", ""),
            ("Vergangen", "OPEN", DANGER)):
        button = DeadlineButton("Abgabe", identity=identity, course_name="Physik",
            due_text="19.10.2026", status=status, information="CONFIRMED", timing=timing)
        qtbot.addWidget(button)
        assert expected in button.styleSheet() if expected else button.styleSheet() == ""


def test_dashboard_to_day_and_academic_exception_details(tmp_path, qtbot):
    config, factory, course_id, runner, controller, window = project(tmp_path, qtbot)
    with factory.begin() as db:
        _entry(db, course_id, "M", "MONDAY", "09:00", "10:00")
        _entry(db, course_id, "T", "TUESDAY", "10:00", "11:00")
        _entry(db, course_id, "TH", "THURSDAY", "12:00", None, location="R1")
        for stable, kind, day, target, start, end in (
                ("C", "CANCELLATION", "2026-10-19", "M", None, None),
                ("R", "REPLACEMENT", "2026-10-20", "T", "14:00", "15:00"),
                ("A", "ADDITION", "2026-10-21", None, "16:00", "17:00")):
            payload = dict(stable_id=stable, date=day, event_type=kind,
                title=f"{kind} Physik", replaces_schedule_id=target,
                start_time=start, end_time=end)
            db.add(ScheduleException(stable_id=stable, course_id=course_id,
                payload_json=json.dumps(payload), information_status=InformationStatus.CONFIRMED))
        db.add(Deadline(stable_id="D", course_id=course_id, title="Übungsblatt",
            due_at=datetime(2026, 10, 18), is_date_only=True,
            status=DeadlineStatus.UNKNOWN, information_status=InformationStatus.CONFIRMED))
    controller.refresh_home()
    runner.complete("home")
    assert [e.event_type for e in controller.dashboard_projection.classes[:3]] == [
        "REPLACEMENT", "ADDITION", "RECURRING"]
    summary = window.dashboard_summary
    buttons = summary.findChildren(QPushButton)
    assert not any("Übungsblatt" in button.text() for button in buttons)
    toggle = next(button for button in buttons if button.text() == "Vergangene Fristen anzeigen")
    qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    deadline = next(button for button in summary.findChildren(QPushButton) if "Übungsblatt" in button.text())
    assert "Abgabestatus unbekannt" in deadline.text()
    assert "Vergangen" in deadline.text()
    qtbot.mouseClick(next(button for button in summary.findChildren(QPushButton)
        if button.text() == "Vergangene Fristen ausblenden"), Qt.MouseButton.LeftButton)
    assert not any("Übungsblatt" in button.text() for button in summary.findChildren(QPushButton))
    next_event = next(button for button in summary.findChildren(QPushButton) if "REPLACEMENT Physik" in button.text())
    qtbot.mouseClick(next_event, Qt.MouseButton.LeftButton)
    assert controller.state.route == Route.CALENDAR
    runner.complete("semester_calendar")
    # Calendar navigation has no date parameter. Select the verified existing route's day explicitly.
    window.calendar.anchor = date(2026, 10, 20)
    window.calendar.reload()
    runner.complete("semester_calendar")
    index = next(i for i, e in enumerate(window.calendar.displayed) if e.title == "REPLACEMENT Physik")
    window.calendar.agenda.setCurrentRow(index)
    assert "REPLACEMENT Physik" in window.calendar.details.text()
    assert "Ausnahme:" in window.calendar.details.text()
    # Existing week-day navigation and detailed factual semantics remain independently guaranteed.
    controller.open_week_day(date(2026, 10, 20))
    assert controller.week_day == date(2026, 10, 20)
    runner.complete("week")
    assert len(controller.week.current_calendar.academic_occurrences) == 3
    assert any("REPLACEMENT Physik" in b.text() for b in
        window.week.content_widget.findChildren(QPushButton))
    replacement = next(button for button in window.week.content_widget.findChildren(QPushButton)
        if "REPLACEMENT Physik" in button.text())
    qtbot.mouseClick(replacement, Qt.MouseButton.LeftButton)
    assert "Raum: unbekannt" in window.week.event_detail.text()
    thursday = next(tile for tile in window.week.findChildren(DayTile)
        if tile.property("day") == date(2026, 10, 22))
    qtbot.mouseClick(thursday, Qt.MouseButton.LeftButton)
    assert controller.week_day == date(2026, 10, 22)
    lecture = next(button for button in window.week.content_widget.findChildren(QPushButton)
        if "Lecture" in button.text())
    qtbot.mouseClick(lecture, Qt.MouseButton.LeftButton)
    assert "Raum: R1" in window.week.event_detail.text()
    assert "12:00–unbekannt" in window.week.event_detail.text()
    factory.kw["bind"].dispose()


def test_empty_draft_and_profile_setup_are_explicit(tmp_path, qtbot):
    config, factory, _, runner, controller, window = project(tmp_path, qtbot)
    created = create_weekly_plan(config, "S", MON, as_of=NOW)
    with factory() as db:
        counts_before = tuple(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("planning_profiles", "weekly_plans", "weekly_plan_acceptances"))
    controller.navigate(Route.WEEK)
    runner.complete("week")
    assert controller.week.plan.plan_id == created["plan_id"]
    assert controller.week.current_calendar.planning_profile_id is None
    assert not window.week.create.isEnabled() and not window.week.accept.isEnabled()
    labels = " ".join(label.text() for label in window.week.findChildren(type(window.title)))
    assert "Persönliche Lernzeiten noch nicht eingerichtet" in labels
    assert "Persönliche Lernzeiten fehlen" in labels
    assert "aktuelle Revision 1" in labels
    assert "Entwurf verwerfen ist derzeit nicht verfügbar" in labels
    assert controller.week.revision_history[0].current
    controller.navigate(Route.HOME)
    with factory() as db:
        counts_after = tuple(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("planning_profiles", "weekly_plans", "weekly_plan_acceptances"))
    assert counts_after == counts_before
    assert not controller.create_plan()
    controller.navigate(Route.WEEK)
    assert controller.create_planning_profile(60, 180)
    runner.complete("mutation")
    runner.complete("week")
    runner.complete("home")
    assert controller.week.current_calendar.planning_profile_id is not None
    assert controller.add_learning_window("MONDAY", time(16), time(18))
    runner.complete("mutation")
    runner.complete("week")
    runner.complete("home")
    assert len(controller.week.current_calendar.availability_occurrences) == 1
    window_id = controller.week.current_calendar.availability_occurrences[0].availability_window_id
    assert controller.deactivate_learning_window(window_id)
    runner.complete("mutation")
    runner.complete("week")
    runner.complete("home")
    assert not controller.week.current_calendar.availability_occurrences
    with factory() as db:
        assert db.scalar(select(PlanningProfile.id)) is not None
        assert db.scalar(select(WeeklyPlanAcceptance.id)) is None
    factory.kw["bind"].dispose()


def test_invalid_saved_plan_keeps_early_week_calendar_clickable(tmp_path, qtbot):
    config, factory, course_id, runner, controller, window = project(tmp_path, qtbot)
    with factory.begin() as db:
        _entry(db, course_id, "EARLY", "WEDNESDAY", "08:00", "11:00")
    create_weekly_plan(config, "S", MON, as_of=NOW)
    with factory.begin() as db:
        revision = db.scalar(select(WeeklyPlanRevision))
        revision.revision_payload_sha256 = "0" * 64
    controller.navigate(Route.WEEK)
    runner.complete("week")
    assert not window.feedback.isVisible()
    assert any(d.code == "WEEKLY_PLAN_SOURCE_INVALID" for d in controller.week.diagnostics)
    assert controller.week.plan.plan_id is not None
    assert controller.week.current_draft.revision_assessment.status == "INVALID"
    assert not window.week.create.isEnabled()
    assert "nicht verifizierbar" in window.week.create.toolTip()
    assert len(controller.week.current_calendar.academic_occurrences) == 1
    for day in (date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 25)):
        tile = next(t for t in window.week.findChildren(DayTile) if t.property("day") == day)
        qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
        assert controller.week_day == day
    controller.select_week(date(2026, 10, 26))
    runner.complete("week")
    assert not window.feedback.isVisible()
    tile = next(t for t in window.week.findChildren(DayTile)
        if t.property("day") == date(2026, 10, 28))
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    assert controller.week_day == date(2026, 10, 28)
    factory.kw["bind"].dispose()


def test_independent_wednesday_tutorial_and_course_hierarchy(tmp_path, qtbot):
    config, factory, course_id, runner, controller, window = project(tmp_path, qtbot)
    with factory.begin() as db:
        math = Course(stable_id="MATH1", name="Mathematik für Informatik", semester_id=db.get(Course, course_id).semester_id,
            information_status=InformationStatus.CONFIRMED)
        module = Course(stable_id="PDP", name="Prinzipien der Programmierung", semester_id=math.semester_id,
            information_status=InformationStatus.CONFIRMED)
        db.add_all([math, module])
        db.flush()
        for stable, name in (("FP", "Funktionale Programmierung"),
                ("OOP", "Objektorientierte Programmierung"), ("LP", "Linux-Praktikum")):
            db.add(Course(stable_id=stable, name=name, semester_id=math.semester_id,
                parent_course_id=module.id, information_status=InformationStatus.CONFIRMED))
        _entry(db, math.id, "LECTURE_ODD", "WEDNESDAY", "14:00", "16:00")
        _entry(db, math.id, "TUTORIAL_ALL", "WEDNESDAY", "08:00", "11:00")
        lecture = db.scalar(select(ScheduleEntry).where(ScheduleEntry.stable_id == "LECTURE_ODD"))
        payload = json.loads(lecture.payload_json)
        payload["calendar_week_parity"] = "ODD"
        lecture.payload_json = json.dumps(payload)
    odd = project_academic_week(config, "S", MON)
    even = project_academic_week(config, "S", date(2026, 10, 26))
    assert {e.schedule_entry_stable_id for e in even} == {"TUTORIAL_ALL"}
    assert {e.schedule_entry_stable_id for e in odd} == {"TUTORIAL_ALL", "LECTURE_ODD"}
    controller.refresh_home()
    runner.complete("home")
    assert not window.feedback.isVisible()
    assert len(window.home.course_cards) == 3
    labels = " ".join(label.text() for label in window.home.findChildren(type(window.title)))
    assert all(name in labels for name in ("Funktionale Programmierung", "Objektorientierte Programmierung", "Linux-Praktikum"))
    identity = resolve_course_visual("MATH1", "Mathematik für Informatik")
    assert course_artwork_asset(identity) == "courses/mathematics.png"
    assert any(a.asset_key == "courses/mathematics.png" for a in window.home.findChildren(PackagedArtwork))
    assert window.home.findChild(ImageHeroCard) is None
    controller.open_week_day(date(2026, 10, 21))
    runner.complete("week")
    assert controller.week_day == date(2026, 10, 21)
    assert window.week.content_widget.findChildren(VisualBadge)
    factory.kw["bind"].dispose()
