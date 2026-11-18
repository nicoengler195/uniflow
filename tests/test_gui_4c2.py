"""Facade-backed Week interaction checks on an isolated canonical project."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtWidgets import QPushButton

from uniflow.application.initializer import initialize_project
from uniflow.application.planning_calendar import add_availability_window, create_planning_profile
from uniflow.application import planning_workflow
from uniflow.application.semester_read_models import list_semesters
from uniflow.domain.enums import InformationStatus, SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.components.planning_dialogs import RegenerateDialog, RemoveDialog, TimeDialog
from uniflow.gui.presentation import PresentationError
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, CourseSkill, Semester, Skill


MON = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("Europe/Berlin"))


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, operation=None):
        index = next((i for i, task in enumerate(self.tasks) if operation is None or task[0] == operation), None)
        assert index is not None
        name, token, fn, callback = self.tasks.pop(index)
        try:
            result = fn()
        except Exception as exc:
            from uniflow.gui.presentation import present_error
            result = present_error(exc)
        callback(name, token, result)
        return result


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
    create_planning_profile(config, "SYNTH", default_action_duration_minutes=60)
    add_availability_window(config, "SYNTH", weekday="MONDAY",
                            start_local_time=time(9), end_local_time=time(12))
    yield config, list_semesters(config)[0]
    factory.kw["bind"].dispose()


def setup_controller(qtbot, project):
    config, semester = project
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.WEEK)
    runner.complete("week")
    return controller, window, runner


def complete_mutation(runner):
    result = runner.complete("mutation")
    runner.complete("week")
    runner.complete("home")
    return result


def test_edit_chain_exact_revision_and_accepted_pointer(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    assert controller.create_plan()
    created = complete_mutation(runner)
    block = controller.week.current_draft.blocks[0]
    assert controller.accept_plan(created.plan_id, created.revision_id)
    complete_mutation(runner)
    assert controller.regenerate_plan(created.plan_id, course_filter_mode="INHERIT",
        duration_overrides_mode="INHERIT")
    complete_mutation(runner)
    draft = controller.week.current_draft
    assert draft.revision_id != created.revision_id
    block = draft.blocks[0]
    assert controller.move_block(created.plan_id, draft.revision_id, block.block_id,
        datetime(2026, 10, 19, 10, tzinfo=ZoneInfo("Europe/Berlin")))
    moved = complete_mutation(runner)
    assert moved.parent_revision_id == draft.revision_id
    assert controller.week.plan.current_revision_id == moved.revision_id
    assert controller.week.plan.accepted_revision_id == created.revision_id
    block = controller.week.current_draft.blocks[0]
    assert controller.set_block_duration(created.plan_id, moved.revision_id, block.block_id, 45)
    resized = complete_mutation(runner)
    assert resized.parent_revision_id == moved.revision_id
    assert controller.week.current_draft.blocks[0].duration_minutes == 45
    assert controller.week.plan.accepted_revision_id == created.revision_id
    assert controller.home.plan.current_revision_id == resized.revision_id
    block = controller.week.current_draft.blocks[0]
    assert controller.remove_block(created.plan_id, resized.revision_id, block.block_id)
    removed = runner.complete("mutation")
    assert not isinstance(removed, PresentationError), removed
    runner.complete("week")
    runner.complete("home")
    assert removed.parent_revision_id == resized.revision_id
    assert controller.week.current_draft.blocks == []
    assert controller.week.plan.accepted_revision_id == created.revision_id
    assert controller.week.current_draft.unscheduled_actions
    action = controller.week.current_draft.unscheduled_actions[0]
    assert controller.place_unscheduled(created.plan_id, removed.revision_id, action.action_id,
        datetime(2026, 10, 19, 9, tzinfo=ZoneInfo("Europe/Berlin")))
    placed = complete_mutation(runner)
    assert placed.parent_revision_id == removed.revision_id
    assert controller.week.current_draft.blocks
    assert controller.week.plan.accepted_revision_id == created.revision_id
    assert controller.accept_plan(created.plan_id, placed.revision_id)
    accepted = complete_mutation(runner)
    assert accepted.revision_id == placed.revision_id
    assert controller.week.plan.accepted_revision_id == placed.revision_id
    assert controller.home.plan.accepted_revision_id == placed.revision_id


def test_stale_token_refreshes_without_replay(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    stale = controller.week.current_draft
    block = stale.blocks[0]
    outside = planning_workflow.set_block_duration(project[0], {
        "plan_id": created.plan_id, "expected_revision_id": stale.revision_id,
        "block_id": block.block_id, "minutes": 45})
    assert controller.remove_block(created.plan_id, stale.revision_id, block.block_id)
    error = runner.complete("mutation")
    assert isinstance(error, PresentationError) and error.code == "REVISION_CHANGED"
    assert sorted(task[0] for task in runner.tasks) == ["home", "week"]
    runner.complete("week")
    runner.complete("home")
    assert controller.week.plan.current_revision_id == outside.revision_id
    assert controller.week.current_draft.blocks[0].duration_minutes == 45
    assert not runner.tasks


def test_single_flight_and_unexpected_error(qtbot, project, monkeypatch):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    block = controller.week.current_draft.blocks[0]
    def fail(config, request):
        raise RuntimeError("private failure")
    monkeypatch.setattr(planning_workflow, "set_block_duration", fail)
    assert controller.set_block_duration(created.plan_id, created.revision_id, block.block_id, 45)
    assert not controller.remove_block(created.plan_id, created.revision_id, block.block_id)
    assert len(runner.tasks) == 1 and controller.state.busy[Route.WEEK] == Busy.MUTATING
    error = runner.complete("mutation")
    assert error.code == "APPLICATION_FAILURE" and "private" not in error.message
    assert controller.state.busy[Route.WEEK] == Busy.IDLE
    assert controller.week.plan.current_revision_id == created.revision_id
    assert window.week.regenerate.isEnabled()


def test_controlled_planning_error_keeps_week_usable(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    assert controller.remove_block(created.plan_id, created.revision_id, "missing-block")
    error = runner.complete("mutation")
    assert error.code == "UNKNOWN_PLAN_BLOCK"
    assert "Traceback" not in error.message
    assert controller.state.busy[Route.WEEK] == Busy.IDLE
    assert controller.week.plan.current_revision_id == created.revision_id
    assert window.week.regenerate.isEnabled()


def test_materialize_accepted_and_idempotent(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    block = controller.week.current_draft.blocks[0]
    controller.accept_plan(created.plan_id, created.revision_id)
    complete_mutation(runner)
    controller.regenerate_plan(created.plan_id, course_filter_mode="INHERIT",
        duration_overrides_mode="INHERIT")
    complete_mutation(runner)
    assert controller.week.current_draft.revision_id != controller.week.accepted_plan.revision_id
    accepted_block = controller.week.accepted_plan.blocks[0]
    assert controller.materialize_block(created.plan_id, created.revision_id,
        accepted_block.block_id, "Study A")
    first = complete_mutation(runner)
    assert first.outcome == "MATERIALIZED"
    assert controller.week.accepted_plan.blocks[0].learning_session_id == first.learning_session_id
    assert controller.materialize_block(created.plan_id, created.revision_id,
        accepted_block.block_id)
    retry = complete_mutation(runner)
    assert retry.outcome == "ALREADY_MATERIALIZED"
    assert retry.learning_session_id == first.learning_session_id
    assert retry.execution_id == first.execution_id
    assert controller.week.plan.accepted_revision_id == created.revision_id
    assert controller.home is not None
    buttons = [button.text() for button in window.week.findChildren(QPushButton)]
    assert "Session · Planned" in buttons


def test_dialog_modes_and_explicit_remove(qtbot, project, monkeypatch):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    dialog = RegenerateDialog(window)
    qtbot.addWidget(dialog)
    assert dialog.values() == {"course_filter_mode": "INHERIT", "course_id": None,
        "duration_overrides_mode": "INHERIT", "duration_overrides": None, "limit": None}
    assert runner.tasks == []
    monkeypatch.setattr(window.week, "_dialog", lambda dialog: False)
    window.week.open_regenerate()
    block = controller.week.current_draft.blocks[0]
    window.week._block_action("remove", block, controller.week, controller.week.current_draft)
    assert runner.tasks == []
    assert controller.week.plan.current_revision_id == created.revision_id
    assert RemoveDialog(window).confirm.isDefault() is False
    dialog.course_mode.setCurrentText("REPLACE")
    dialog.course_id.setText("C")
    dialog.duration_mode.setCurrentText("REPLACE")
    dialog.overrides.setText("A=45")
    assert dialog.values()["duration_overrides"] == {"A": 45}
    assert dialog.values()["course_id"] == "C"
    time_dialog = TimeDialog("Move block", "Choose start", NOW,
        "Europe/Berlin", parent=window)
    qtbot.addWidget(time_dialog)
    assert time_dialog.values()[0].utcoffset() is not None


def test_draft_materialization_not_offered(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    complete_mutation(runner)
    draft = controller.week.current_draft
    assert draft.blocks
    buttons = [button.text() for button in window.week.findChildren(QPushButton)]
    assert "Create Session" not in buttons and "Resume Session" not in buttons
    assert window.week.accept.isEnabled()


def test_accept_plan_requires_explicit_dialog_confirmation(qtbot, project, monkeypatch):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    monkeypatch.setattr(window.week, "_dialog", lambda dialog: False)
    window.week.open_accept()
    assert runner.tasks == []
    assert controller.week.plan.accepted_revision_id is None
    monkeypatch.setattr(window.week, "_dialog", lambda dialog: True)
    window.week.open_accept()
    assert controller.state.busy[Route.WEEK] == Busy.MUTATING
    complete_mutation(runner)
    assert controller.week.plan.accepted_revision_id == created.revision_id


def test_invalid_dialog_request_is_safe(qtbot, project):
    controller, window, runner = setup_controller(qtbot, project)
    controller.create_plan()
    created = complete_mutation(runner)
    assert not controller.regenerate_plan(created.plan_id, course_filter_mode="REPLACE",
        course_id="X" * 81, duration_overrides_mode="INHERIT")
    assert controller.state.last_presentation_error.code == "WORKFLOW_INPUT_INVALID"
    assert runner.tasks == []
    assert controller.state.busy[Route.WEEK] == Busy.IDLE
