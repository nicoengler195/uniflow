"""Session Inbox over detached application read DTOs."""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from uniflow.gui.components import BaseCard, EmptyState, StatusChip, VisualBadge
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.presentation import format_time, label
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual


def _line(value, *, muted=False):
    widget = QLabel(value)
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
    return widget


class SessionsScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.view = None
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(15)

    def render(self, dto):
        self.view = dto
        self.setMinimumHeight(0)
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        intro = BaseCard("Learning Sessions", hero=True, icon_kind="session")
        intro.body.addWidget(_line("Continue a Session, process a returned Result, or review completed learning workflows."))
        self.create_button = QPushButton("Prepare learning session…")
        self.create_button.clicked.connect(self._create_session)
        intro.body.addWidget(self.create_button)
        self.body.addWidget(intro)
        for title, count, sessions, empty in (
            ("Resumable", dto.resumable_count, dto.resumable_sessions, "No resumable Sessions"),
            ("Processing", dto.processing_count, dto.processing_sessions, "No Results waiting for processing"),
            ("Completed", dto.completed_count, dto.completed_sessions, "No completed Sessions yet"),
        ):
            heading = BaseCard(f"{title.upper()} · {count}", variant="secondary")
            if title == "Completed" and count > dto.completed_limit:
                heading.body.addWidget(_line(f"Showing the latest {dto.completed_limit} of {count} completed Sessions.", muted=True))
            self.body.addWidget(heading)
            if not sessions:
                self.body.addWidget(EmptyState(empty, "Sessions will appear here when they reach this state."))
            for session in sessions:
                self.body.addWidget(self._card(session, dto.semester.timezone))
        self.body.addStretch()
        self.setMinimumHeight(self.sizeHint().height())

    def _create_session(self):
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        from uniflow.application.gui_learning_bridge import semester_courses, course_skills
        if self.controller.bridge_busy:
            return
        courses = semester_courses(self.controller.config, self.controller.state.semester_stable_id)
        labels = [f"{name} ({stable_id})" for stable_id, name in courses]
        if not labels:
            QMessageBox.information(self, "No courses", "The active semester has no courses.")
            return
        choice, ok = QInputDialog.getItem(self, "Choose course", "Course:", labels, 0, False)
        if not ok:
            return
        stable_id = courses[labels.index(choice)][0]
        skills = course_skills(self.controller.config, stable_id)
        if not skills:
            QMessageBox.information(self, "No linked Skill", "Confirm a Skill proposal from parsed material, then link the accepted Skill to this course in Material Detail.")
            return
        skill_labels = [f"{name} ({sid})" for sid, name in skills]
        selected, ok = QInputDialog.getItem(self, "Choose target Skill", "Skill:", skill_labels, 0, False)
        if not ok:
            return
        goal, ok = QInputDialog.getMultiLineText(self, "Learning goal", "What do you want to learn?")
        if ok and goal.strip():
            self.controller.create_study_session(stable_id, goal.strip(),
                [skills[skill_labels.index(selected)][0]])

    def sync(self):
        if hasattr(self, "create_button"):
            self.create_button.setEnabled(not self.controller.bridge_busy)

    def _card(self, session, zone):
        card = BaseCard(variant="card")
        card.body.setContentsMargins(15, 11, 15, 11)
        identity = resolve_course_visual(session.course_stable_id, session.course_stable_id)
        content = QHBoxLayout()
        content.setSpacing(14)
        art = PackagedArtwork(course_artwork_asset(identity), mode="thumbnail",
            fallback_family=identity.artwork_family)
        art.setFixedSize(104, 104)
        content.addWidget(art)
        facts = QVBoxLayout()
        facts.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(VisualBadge(identity, compact=True))
        course = _line(session.course_stable_id)
        course.setObjectName("heading")
        row.addWidget(course, 1)
        row.addWidget(StatusChip(label(session.state)))
        facts.addLayout(row)
        facts.addWidget(_line(session.goal))
        basis = {"RESULT_RECEIVED": "Result received", "HANDOFF_GENERATED": "Package generated",
                 "SESSION_CREATED": "Session created"}[session.activity_basis]
        facts.addWidget(_line("Targets: " + ", ".join(x.skill_id for x in session.targets), muted=True))
        facts.addWidget(_line(basis + " · " + format_time(session.activity_at, zone), muted=True))
        handoff = f"Package revision {session.current_handoff.revision}" if session.current_handoff else "No package yet"
        result = (f" · Result {label(session.result.processing_status)}" if session.result else "")
        origin = " · Weekly plan origin" if session.materialization_provenance else ""
        facts.addWidget(_line(handoff + result + origin, muted=True))
        if session.diagnostics:
            facts.addWidget(_line("Provenance needs review", muted=True))
        button = QPushButton("Open Session →")
        button.setObjectName("sessionLink")
        button.setFixedWidth(145)
        button.clicked.connect(lambda _, sid=session.session_id: self.controller.select_session(sid))
        facts.addWidget(button)
        content.addLayout(facts, 1)
        card.body.addLayout(content)
        card.setMinimumHeight(card.sizeHint().height())
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return card
