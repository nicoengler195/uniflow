"""Qt application entry and project startup gate."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QPushButton)
from uniflow.application.semester_read_models import list_semesters, validate_project_database
from uniflow.gui.components import FeedbackBanner, LayeredCanvas
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.components.nav_icons import BrandMark, brand_icon, route_icon
from uniflow.gui.controller import GuiController
from uniflow.gui.routes import Route
from uniflow.gui.screens.home import HomeScreen
from uniflow.gui.screens.dashboard_summary import DashboardSummary
from uniflow.gui.screens.week import WeekScreen
from uniflow.gui.screens.calendar import CalendarScreen
from uniflow.gui.screens.learning_priorities import LearningPrioritiesScreen
from uniflow.gui.screens.sessions import SessionsScreen
from uniflow.gui.screens.session_detail import SessionDetailScreen
from uniflow.gui.screens.materials import MaterialsScreen
from uniflow.gui.screens.material_detail import MaterialDetailScreen
from uniflow.gui.screens.proposal_review import ProposalReviewScreen
from uniflow.gui.theme.stylesheet import stylesheet
from uniflow.infrastructure.config.settings import load_config


def open_project(root: Path):
    root = root.expanduser().resolve()
    if not (root / "config" / "default.yaml").is_file():
        raise ValueError("Choose a UniFlow project root with config/default.yaml.")
    config = load_config(project_root=root)
    validate_project_database(config)
    return config, list_semesters(config)


def choose_semester(choices):
    suitable = [item for item in choices if item.state == "ACTIVE"]
    if not suitable:
        return None
    if len(suitable) == 1:
        return suitable[0]
    from PySide6.QtWidgets import QInputDialog
    labels = [f"{item.name} ({item.stable_id})" for item in suitable]
    selection, ok = QInputDialog.getItem(None, "Select Semester", "Active Semester", labels, 0, False)
    return suitable[labels.index(selection)] if ok else None


class MainWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("UniFlow")
        self.setWindowIcon(brand_icon())
        self.resize(1360, 860)
        root = LayeredCanvas()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 18)
        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand_row.addWidget(BrandMark())
        brand = QLabel("UniFlow")
        brand.setObjectName("title")
        brand_row.addWidget(brand, 1)
        side.addLayout(brand_row)
        side.addSpacing(42)
        self.buttons = {}
        for route, name in ((Route.HOME, "Dashboard"), (Route.WEEK, "Week"),
                (Route.CALENDAR, "Semesterkalender"), (Route.LEARNING_PRIORITIES, "Lernprioritäten"), (Route.SESSIONS, "Sessions"), (Route.MATERIALS, "Materials")):
            button = QPushButton(name)
            button.setObjectName("nav")
            button.setIcon(route_icon(route))
            button.setIconSize(QSize(22, 22))
            button.clicked.connect(lambda _, r=route: controller.navigate(r))
            side.addWidget(button)
            self.buttons[route] = button
        side.addStretch()
        # The manifest's sidebar and generic-academic PNGs have identical bytes.
        side_art = PackagedArtwork("courses/generic_academic.png", mode="sidebar")
        side_art.setFixedHeight(135)
        side.addWidget(side_art)
        side.addSpacing(14)
        semester_label = QLabel(controller.semester.name)
        semester_label.setObjectName("muted")
        semester_label.setWordWrap(True)
        side.addWidget(semester_label)
        outer.addWidget(sidebar)
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(28, 24, 32, 20)
        main_layout.setSpacing(16)
        topbar = QHBoxLayout()
        self.title = QLabel("Dashboard")
        self.title.setObjectName("title")
        topbar.addWidget(self.title, 1)
        semester_chip = QLabel(controller.semester.name)
        semester_chip.setObjectName("chip")
        topbar.addWidget(semester_chip)
        main_layout.addLayout(topbar)
        self.feedback = FeedbackBanner()
        main_layout.addWidget(self.feedback)
        scroll = QScrollArea()
        self.scroll = scroll
        self._last_route = None
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        container = QWidget()
        container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.content = QVBoxLayout(container)
        self.dashboard_summary = DashboardSummary(controller)
        self.home = HomeScreen(controller)
        self.week = WeekScreen(controller)
        self.calendar = CalendarScreen(controller)
        self.learning_priorities = LearningPrioritiesScreen(controller)
        self.sessions = SessionsScreen(controller)
        self.session_detail = SessionDetailScreen(controller)
        self.materials = MaterialsScreen(controller)
        self.material_detail = MaterialDetailScreen(controller)
        self.proposal_review = ProposalReviewScreen(controller)
        self.content.addWidget(self.dashboard_summary)
        self.content.addWidget(self.home)
        self.content.addWidget(self.week)
        self.content.addWidget(self.calendar)
        self.content.addWidget(self.learning_priorities)
        self.content.addWidget(self.sessions)
        self.content.addWidget(self.session_detail)
        self.content.addWidget(self.materials)
        self.content.addWidget(self.material_detail)
        self.content.addWidget(self.proposal_review)
        self.content.addStretch()
        scroll.setWidget(container)
        main_layout.addWidget(scroll)
        outer.addWidget(main, 1)
        controller.home_changed.connect(self.home.render)
        controller.week_changed.connect(self.week.render)
        controller.home_changed.connect(self.feedback.hide)
        controller.week_changed.connect(self.feedback.hide)
        controller.sessions_changed.connect(self.sessions.render)
        controller.session_detail_changed.connect(self.session_detail.render)
        controller.materials_changed.connect(self.materials.render)
        controller.material_detail_changed.connect(self.material_detail.render)
        controller.proposal_review_changed.connect(self.proposal_review.render)
        controller.skill_choices_changed.connect(self.proposal_review.render_choices)
        controller.state_changed.connect(self.sync)
        controller.feedback.connect(self.feedback.show_feedback)
        controller.review_opened.connect(self.feedback.hide)
        self.sync()
        controller.refresh_home()

    def closeEvent(self, event):
        self.calendar.activate(False)
        self.learning_priorities.activate(False)
        self.dashboard_summary.activate(False)
        self.controller.close_home()
        super().closeEvent(event)

    def sync(self):
        route = self.controller.state.route
        route_changed = route != self._last_route
        self._last_route = route
        self.title.setText({Route.LEARNING_PRIORITIES: "Lernprioritäten", Route.HOME: "Dashboard", Route.WEEK: "Week", Route.CALENDAR: "Semesterkalender",
            Route.SESSIONS: "Sessions", Route.SESSION_DETAIL: "Session Detail",
            Route.MATERIALS: "Materials", Route.MATERIAL_DETAIL: "Material Detail",
            Route.PROPOSAL_REVIEW: "Proposal Review"}[route])
        self.dashboard_summary.setVisible(route == Route.HOME)
        self.dashboard_summary.activate(route == Route.HOME)
        self.home.setVisible(route == Route.HOME)
        self.week.setVisible(route == Route.WEEK)
        self.calendar.setVisible(route == Route.CALENDAR)
        self.calendar.activate(route == Route.CALENDAR)
        self.learning_priorities.setVisible(route == Route.LEARNING_PRIORITIES)
        self.learning_priorities.activate(route == Route.LEARNING_PRIORITIES)
        self.sessions.setVisible(route == Route.SESSIONS)
        self.session_detail.setVisible(route == Route.SESSION_DETAIL)
        self.materials.setVisible(route == Route.MATERIALS)
        self.material_detail.setVisible(route == Route.MATERIAL_DETAIL)
        self.proposal_review.setVisible(route == Route.PROPOSAL_REVIEW)
        self.week.sync()
        self.session_detail.sync()
        self.materials.sync()
        self.sessions.sync()
        self.proposal_review.sync()
        if route_changed:
            self.scroll.verticalScrollBar().setValue(0)
        for item, button in self.buttons.items():
            selected = item == route or (item == Route.SESSIONS and route == Route.SESSION_DETAIL) or (
                item == Route.MATERIALS and route in {Route.MATERIAL_DETAIL, Route.PROPOSAL_REVIEW})
            button.setProperty("selected", selected)
            button.setIcon(route_icon(item, active=selected))
            button.style().unpolish(button)
            button.style().polish(button)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the UniFlow desktop application")
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("UniFlow")
    app.setWindowIcon(brand_icon())
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("UniFlow.Desktop")
    app.setStyleSheet(stylesheet())
    root = args.project_root
    if root is None:
        selected = QFileDialog.getExistingDirectory(None, "Choose UniFlow project root")
        if not selected:
            return 0
        root = Path(selected)
    try:
        config, semesters = open_project(root)
        semester = choose_semester(semesters)
        if semester is None:
            QMessageBox.warning(None, "UniFlow setup", "No active Semester is available in this project.")
            return 1
    except Exception as exc:
        QMessageBox.critical(None, "UniFlow startup", str(exc))
        return 1
    window = MainWindow(GuiController(config, semester))
    window.show()
    return app.exec()
