"""Phase 4C4A read-only Materials GUI against canonical temporary data."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from uniflow.application.initializer import initialize_project
from uniflow.application.materials_context_read_models import get_materials_context
from uniflow.application.material_inbox_read_models import get_material_inbox
from uniflow.application.semester_read_models import list_semesters
from uniflow.domain.enums import InformationStatus, MaterialState, SemesterState
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.presentation import present_error
from uniflow.gui.screens.materials import safe_filename, source_kind, parse_text
from uniflow.gui.routes import Route
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (ClassificationProposal, Course, Material, MaterialArtifact,
    MaterialReleaseRule, RepositoryAcademicLink, RepositorySnapshot, RepositorySource, Semester, SourceArtifact)

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


class ManualRunner:
    def __init__(self):
        self.tasks = []

    def submit(self, operation, token, fn, callback):
        self.tasks.append((operation, token, fn, callback))

    def complete(self, operation=None):
        index = next(i for i, x in enumerate(self.tasks) if operation is None or x[0] == operation)
        op, token, fn, callback = self.tasks.pop(index)
        try:
            value = fn()
        except Exception as exc:
            value = present_error(exc)
        callback(op, token, value)
        return value


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(id="sem", stable_id="S", name="Semester", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="UTC"))
        db.add(Semester(id="other", stable_id="OTHER", name="Other", state=SemesterState.ACTIVE, timezone="UTC"))
        db.flush()
        db.add_all([Course(id="ca", semester_id="sem", stable_id="A", name="Algorithms"),
            Course(id="cb", semester_id="sem", stable_id="B", name="Biology"),
            Course(id="cx", semester_id="other", stable_id="X", name="Foreign")])
        db.flush()
        db.add(MaterialReleaseRule(id="rule", stable_id="RULE", course_id="ca",
            payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","material_kind":"LECTURE"}',
            information_status=InformationStatus.OBSERVED))
        db.add(MaterialReleaseRule(id="unresolved", stable_id="UNRESOLVED", course_id="cb",
            payload_json='{"cadence":"WEEKLY","material_kind":"EXERCISE"}',
            information_status=InformationStatus.UNKNOWN))
    yield config, next(x for x in list_semesters(config) if x.stable_id == "S"), factory
    factory.kw["bind"].dispose()


def add_material(project):
    _, _, factory = project
    with factory.begin() as db:
        db.add_all([Material(id="ma", course_id="ca", stable_id="MA", title="Actual title",
            material_type="LECTURE", lifecycle_state=MaterialState.DOWNLOADED),
            Material(id="mb", course_id="cb", stable_id="MB", title="Second Material",
            lifecycle_state=MaterialState.EXPECTED)])
        db.add(SourceArtifact(id="source", sha256="a" * 64, original_filename="misleading_solution.pdf",
            original_source_path="C:/secret/source.pdf", stored_path="C:/secret/blob", extension=".pdf",
            mime_type="application/pdf", size_bytes=42, created_at=NOW))
        db.flush()
        db.add(MaterialArtifact(id="version", material_id="ma", artifact_id="source",
            version_number=1, is_current=True, linked_at=NOW))


def setup(qtbot, project):
    config, semester, _ = project
    runner = ManualRunner()
    controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.MATERIALS)
    runner.complete("materials")
    return controller, window, runner


def words(widget):
    return " ".join(x.text() for x in widget.findChildren(QLabel))


def test_context_exact_detached_query_only(project, monkeypatch):
    config, _, _ = project
    from uniflow.application import materials_context_read_models as module
    original = module._read_transaction
    seen = []
    @contextmanager
    def checked(arg):
        with original(arg) as db:
            seen.append(db.connection().exec_driver_sql("PRAGMA query_only").scalar())
            yield db
    monkeypatch.setattr(module, "_read_transaction", checked)
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    context = get_materials_context(config, "S", as_of=NOW)
    assert seen == [1]
    assert (context.starts_on, context.ends_on, context.timezone) == (date(2026, 10, 1), date(2027, 3, 31), "UTC")
    assert [(c.stable_id, c.name) for c in context.courses] == [("A", "Algorithms"), ("B", "Biology")]
    assert "path" not in context.model_dump_json().lower()
    assert hashlib.sha256(config.database_path.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        get_materials_context(config, "S", as_of=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_materials_context(config, "missing", as_of=NOW)


def test_expected_only_filter_and_range(qtbot, project):
    controller, window, runner = setup(qtbot, project)
    assert controller.state.materials_course_stable_id is None
    assert (controller.state.materials_from_date, controller.state.materials_through_date) == (
        date(2026, 10, 1), date(2027, 3, 31))
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "Alle Materialien und Filterergebnisse").click()
    assert "Materialerwartungen" in words(window.materials)
    assert "Noch keine importierten Materialien" in words(window.materials)
    assert any("Ungeklärte Veröffentlichungsregeln" in b.text()
        for b in window.materials.findChildren(QPushButton))
    assert "Available on Moodle" not in words(window.materials)
    controller.set_materials_filter(course_id="A", from_date=date(2026, 10, 1), through_date=date(2026, 10, 31))
    runner.complete("materials")
    assert controller.materials.course_filter_stable_id == "A"
    assert controller.materials.project_wide_unassigned_artifacts == []
    assert controller.materials.from_date == window.materials.from_date.date().toPython()
    count = len(runner.tasks)
    assert not controller.set_materials_filter(course_id="A", from_date=date(2026, 11, 1), through_date=date(2026, 10, 1))
    assert len(runner.tasks) == count


def test_material_route_back_and_late_reads(qtbot, project):
    add_material(project)
    controller, window, runner = setup(qtbot, project)
    expected_navigation = [
        (Route.HOME, "Dashboard", window.home),
        (Route.WEEK, "Week", window.week),
        (Route.CALENDAR, "Semesterkalender", window.calendar),
        (Route.LEARNING_PRIORITIES, "Lernprioritäten", window.learning_priorities),
        (Route.SESSIONS, "Sessions", window.sessions),
        (Route.MATERIALS, "Materials", window.materials),
    ]
    assert list(window.buttons) == [route for route, _, _ in expected_navigation]
    assert [b.text() for b in window.buttons.values()] == [name for _, name, _ in expected_navigation]
    before_navigation = hashlib.sha256(controller.config.database_path.read_bytes()).hexdigest()
    window.show()
    for route, caption, screen in expected_navigation:
        window.buttons[route].click()
        while runner.tasks:
            runner.complete()
        assert controller.state.route == route
        assert window.title.text() == caption
        assert screen.isVisible()
        assert window.buttons[route].property("selected") is True
        assert all(other.isHidden() for other_route, _, other in expected_navigation if other_route != route)
    assert hashlib.sha256(controller.config.database_path.read_bytes()).hexdigest() == before_navigation
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "Alle Materialien und Filterergebnisse").click()
    assert "Actual title" in words(window.materials)
    assert "misleading_solution.pdf" in words(window.materials)
    controller.select_material("ma")
    old = runner.tasks.pop()
    controller.select_material("mb")
    runner.complete("material_detail")
    old[3](old[0], old[1], old[2]())
    assert controller.material_detail.material.material_id == "mb"
    assert window.buttons[Route.MATERIALS].property("selected") is True
    window.material_detail.findChildren(QPushButton)[0].click()
    assert controller.state.route == Route.MATERIALS
    assert controller.state.selected_material_id is None
    controller.set_materials_filter(course_id="A")
    late = runner.tasks.pop()
    controller.set_materials_filter(course_id="B")
    runner.complete("materials")
    late[3](late[0], late[1], late[2]())
    assert controller.materials.course_filter_stable_id == "B"


def test_filter_invalidates_selected_detail(qtbot, project):
    add_material(project)
    controller, window, runner = setup(qtbot, project)
    controller.select_material("ma")
    runner.complete("material_detail")
    assert "Actual title" in words(window.material_detail)
    controller.set_materials_filter(course_id="B")
    assert controller.state.route == Route.MATERIALS
    assert controller.state.selected_material_id is None
    runner.complete("materials")
    assert [m.material_id for m in controller.materials.materials] == ["mb"]


def test_unknown_end_and_long_semester_window(project):
    config, semester, factory = project
    context = get_materials_context(config, "S", as_of=NOW)
    assert GuiController._materials_default_range(context, NOW) == (date(2026, 10, 1), date(2027, 3, 31))
    open_end = context.model_copy(update={"ends_on": None})
    assert GuiController._materials_default_range(open_end, NOW) == (date(2026, 10, 1), date(2027, 4, 15))
    long_end = context.model_copy(update={"ends_on": date(2028, 12, 31)})
    start, end = GuiController._materials_default_range(long_end, NOW)
    assert (end - start).days <= 365


def test_source_presentation_never_uses_filename_as_academic_role():
    class Source:
        extension = ".pdf"
    assert source_kind(Source()) == "pdf"
    Source.extension = ".md"
    assert source_kind(Source()) == "markdown"
    Source.extension = ".unknown"
    assert source_kind(Source()) == "document"
    assert safe_filename("C:/private/solution.pdf") == "Filename metadata unavailable"
    assert parse_text(None) == "No parse recorded"


def test_project_wide_inspectors_and_proposal_history(qtbot, project):
    add_material(project)
    _, _, factory = project
    with factory.begin() as db:
        db.add(SourceArtifact(id="loose", sha256="b" * 64, original_filename="notes.txt",
            original_source_path="C:/secret/notes.txt", stored_path="C:/secret/blob",
            extension=".txt", mime_type="text/plain", size_bytes=12, created_at=NOW))
        db.add_all([RepositorySource(id="repo", stable_id="REPO", local_path="C:/secret/repo",
            provider_hint="LOCAL", created_at=NOW, last_seen_at=NOW),
            RepositorySource(id="unlinked", stable_id="UNLINKED", local_path="C:/secret/unlinked",
            created_at=NOW, last_seen_at=NOW)])
        db.flush()
        db.add(RepositoryAcademicLink(id="repo-link", repository_source_id="repo",
            course_id="ca", is_active=True, created_at=NOW))
        db.add(RepositorySnapshot(id="snapshot", repository_source_id="repo", captured_at=NOW,
            snapshot_identity="c" * 64, policy_version="1", include_untracked=False,
            branch_name="main", head_commit_oid="d" * 40, is_detached=False,
            is_dirty=False, tracked_file_count=5, untracked_file_count=0,
            deleted_file_count=0, blob_count=5, build_system_hint="UNKNOWN", status="SUCCESS"))
        db.add(ClassificationProposal(id="proposal", proposal_kind="MATERIAL_EXPECTATION_LINK",
            status="PENDING", identity_sha256="e" * 64, created_at=NOW,
            classifier_name="FIXTURE", classifier_version="1", subject_material_id="ma"))
    controller, window, runner = setup(qtbot, project)
    assert len(controller.materials.project_wide_unassigned_artifacts) == 1
    assert len(controller.materials.repositories) == 1
    assert len(controller.materials.project_wide_unlinked_repositories) == 1
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "Nicht zugeordnet").click()
    text = words(window.materials)
    disclosures = [x.text() for x in window.materials.findChildren(QPushButton)]
    assert any("Unassigned source" in x for x in disclosures)
    assert any("Repository" in x for x in disclosures)
    assert "C:/secret" not in text
    assert not any(button.text() in {"Accept", "Reject", "Resolve", "Open file"}
        for button in window.materials.findChildren(QPushButton))
    controller.set_materials_filter(course_id="A")
    runner.complete("materials")
    assert not controller.materials.project_wide_unassigned_artifacts
    assert not controller.materials.project_wide_unlinked_repositories


def test_library_parent_child_material_navigation_and_return(qtbot, project):
    _, _, factory = project
    with factory.begin() as db:
        db.add(Course(id="pdp", semester_id="sem", stable_id="PDP",
            name="Prinzipien der Programmierung"))
        db.flush()
        db.add(Course(id="fp", semester_id="sem", stable_id="PDP_FP",
            name="Funktionale Programmierung", parent_course_id="pdp"))
        db.add(Material(id="fp-material", course_id="fp", stable_id="FP_MAT",
            title="FP lecture notes", lifecycle_state=MaterialState.DOWNLOADED))
        db.add(Material(id="parent-material", course_id="pdp", stable_id="PDP_MAT",
            title="Shared module material", lifecycle_state=MaterialState.DOWNLOADED))
        db.add(Material(id="loose-material", course_id=None, stable_id="LOOSE_MAT",
            title="Unscoped material", lifecycle_state=MaterialState.DOWNLOADED))
        db.add(MaterialReleaseRule(id="pdp-rule", stable_id="PDP_RULE", course_id="pdp",
            payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","material_kind":"LECTURE"}',
            information_status=InformationStatus.CONFIRMED))
    controller, window, runner = setup(qtbot, project)
    context = controller.materials_context
    child = next(c for c in context.courses if c.stable_id == "PDP_FP")
    parent = next(c for c in context.courses if c.stable_id == "PDP")
    assert child.parent_course_id == parent.course_id
    root_cards = window.materials.grid_wrapper.property("libraryCourses")
    assert "PDP" in root_cards and "PDP_FP" not in root_cards
    window.show()
    for width, columns in ((1360, 3), (1920, 3), (1080, 2)):
        window.resize(width, 860)
        qtbot.wait(20)
        assert window.scroll.horizontalScrollBar().maximum() == 0
        positions = {window.materials.grid_wrapper.layout().getItemPosition(index)[1]
            for index in range(3)}
        assert len(positions) == columns
    next(b for b in window.materials.findChildren(QPushButton)
        if b.accessibleName() == parent.name + " öffnen").click()
    runner.complete("materials")
    assert controller.materials.course_filter_stable_id == "PDP"
    assert window.materials.grid_wrapper.property("libraryCourses") == ("PDP_FP",)
    assert "Shared module material" in words(window.materials)
    assert "FP lecture notes" not in words(window.materials)
    assert all(e.course_stable_id == "PDP" for e in controller.materials.projected_expectations)
    next(b for b in window.materials.findChildren(QPushButton)
        if b.accessibleName() == child.name + " öffnen").click()
    runner.complete("materials")
    assert controller.materials.course_filter_stable_id == "PDP_FP"
    assert "FP lecture notes" in words(window.materials)
    assert "Shared module material" not in words(window.materials)
    assert not controller.materials.projected_expectations
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "Material öffnen").click()
    runner.complete("material_detail")
    assert controller.material_detail.material.material_id == "fp-material"
    controller.back_to_materials()
    assert controller.state.route == Route.MATERIALS
    assert controller.materials.course_filter_stable_id == "PDP_FP"
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "← Zum Modul").click()
    runner.complete("materials")
    assert controller.materials.course_filter_stable_id == "PDP"
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "← Zur Fächerübersicht").click()
    runner.complete("materials")
    assert controller.materials.course_filter_stable_id is None
    next(b for b in window.materials.findChildren(QPushButton)
        if b.text() == "Nicht zugeordnet").click()
    assert "Unscoped material" in words(window.materials)
