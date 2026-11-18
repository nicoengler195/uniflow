"""Offscreen desktop slice over temporary canonical projects."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtWidgets import QProgressBar, QPushButton

from uniflow.application.initializer import initialize_project
from uniflow.application.planning_calendar import add_availability_window, create_planning_profile
from uniflow.application.planning_workflow import accept_plan, create_plan, regenerate_plan
from uniflow.application.semester_read_models import list_semesters
from uniflow.application.week_read_models import get_week_view
from uniflow.domain.enums import InformationStatus, SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.components import VisualBadge
from uniflow.gui.controller import GuiController
from uniflow.gui.presentation import information_status, mastery, present_error
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy
from uniflow.gui.screens.home import CourseCard
from uniflow.gui.screens.week import DayTile, RevisionPanel
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, CourseSkill, MasteryState, Semester, Skill
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.write_lock import WriterLockTimeout

MON = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("Europe/Berlin"))


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, operation):
        index = next(i for i, task in enumerate(self.tasks) if task[0] == operation)
        operation, token, fn, callback = self.tasks.pop(index)
        callback(operation, token, fn())


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYNTH", name="Synthetic", state=SemesterState.ACTIVE,
                            timezone="Europe/Berlin")
        course = Course(stable_id="C", name="Course", semester=semester,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        db.add(Skill(id="A", name="A", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=course.id, skill_id="A"))
    yield config, list_semesters(config)[0]
    factory.kw["bind"].dispose()


def test_shell_routes_empty_and_semantics(qtbot, project):
    config, semester = project
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    assert controller.state.route == Route.HOME
    runner.complete("home")
    assert controller.home.plan.plan_id is None
    controller.navigate(Route.WEEK)
    runner.complete("week")
    assert controller.week.plan.plan_id is None
    assert not window.week.create.isEnabled()
    assert window.week.profile.isVisibleTo(window.week)
    assert mastery(None) == "UNKNOWN"
    assert mastery(0.0) == "0% known mastery"
    assert len({information_status(x) for x in ("CONFIRMED", "OBSERVED", "INFERRED", "UNKNOWN")}) == 4


def test_create_refresh_and_late_result(qtbot, project):
    config, semester = project
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY", start_local_time=time(9), end_local_time=time(12))
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.WEEK)
    runner.complete("week")
    window.week.create.click()
    assert controller.state.busy[Route.WEEK] == Busy.MUTATING
    assert not window.week.create.isEnabled()
    window.week.create.click()
    assert len(runner.tasks) == 1
    runner.complete("mutation")
    assert sorted(task[0] for task in runner.tasks) == ["home", "week"]
    runner.complete("week")
    runner.complete("home")
    assert controller.week.plan.current_revision_id is not None
    assert controller.home.plan.current_revision_id == controller.week.plan.current_revision_id
    assert window.week.findChildren(VisualBadge)
    controller.select_week(MON + date.resolution * 7)
    old = runner.tasks.pop()
    controller.select_week(MON)
    old[3](old[0], old[1], old[2]())
    assert controller.state.selected_week_start == MON
    runner.complete("week")


def test_safe_errors():
    assert present_error(WorkflowCommandError("PLAN_ALREADY_EXISTS")).code == "PLAN_ALREADY_EXISTS"
    assert present_error(RuntimeError("secret")).message.find("secret") == -1
    assert "C:\\private" not in present_error(WriterLockTimeout("C:\\private")).message


def test_populated_home_unknown_differs_from_known_zero(qtbot, project):
    config, semester = project
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        second = Course(stable_id="ZERO", name="Zero mastery course", semester_id=db.query(Semester.id).filter_by(stable_id="SYNTH").scalar(),
                        information_status=InformationStatus.OBSERVED)
        db.add(second)
        db.add(Skill(id="Z", name="Zero skill", kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add(CourseSkill(course_id=second.id, skill_id="Z"))
        db.add(MasteryState(skill_id="Z", value=0.0, calculation_method="test", calculated_at=NOW))
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    cards = {item.course_stable_id: item for item in controller.home.courses}
    assert cards["C"].mastery_coverage.mean_known_mastery is None
    assert cards["ZERO"].mastery_coverage.mean_known_mastery == 0.0
    text = " ".join(item.text() for item in window.home.findChildren(type(window.title)))
    assert "UNKNOWN" in text and "0% known mastery" in text
    assert len(window.home.findChildren(CourseCard)) == 2
    assert sum(len(card.findChildren(QProgressBar)) for card in window.home.findChildren(CourseCard)) == 1
    assert len(window.home.findChildren(QProgressBar)) == 2  # Same DTO value is also shown in Study progress.
    assert any(button.text() == "Woche öffnen" for button in window.home.findChildren(QPushButton))
    factory.kw["bind"].dispose()


def test_actual_worker_delivers_detached_home(qtbot, project):
    config, semester = project
    controller = GuiController(config, semester, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: controller.home is not None, timeout=10000)
    assert controller.home.semester.stable_id == "SYNTH"
    assert controller.state.busy[Route.HOME] == Busy.IDLE


def test_week_visuals_keep_accepted_and_draft_separate(qtbot, project):
    config, semester = project
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY", start_local_time=time(9), end_local_time=time(12))
    created = create_plan(config, {"semester_id": "SYNTH", "week_start": MON, "as_of": NOW})
    accept_plan(config, {"plan_id": created.plan_id, "revision_id": created.revision_id,
        "accepted_at": datetime(2026, 10, 19, 8, 5, tzinfo=ZoneInfo("Europe/Berlin"))})
    regenerate_plan(config, {"plan_id": created.plan_id,
        "as_of": datetime(2026, 10, 19, 8, 10, tzinfo=ZoneInfo("Europe/Berlin"))})
    dto = get_week_view(config, "SYNTH", MON, as_of=NOW)
    assert dto.accepted_plan.revision_id != dto.current_draft.revision_id
    controller = GuiController(config, semester, runner=ManualRunner(), clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    controller.week = dto
    window.week.render(dto)
    assert len(window.week.findChildren(DayTile)) == 7
    panels = window.week.findChildren(RevisionPanel)
    assert {panel.objectName() for panel in panels} == {"accepted", "draft"}
    button_texts = [button.text() for button in window.week.findChildren(QPushButton)]
    assert button_texts[:3] == ["← Previous", "Next →", "Plan already exists"]
    assert "Regenerate…" in button_texts and "Accept plan" in button_texts
