"""State-driven Session detail; all facts come from the detached read DTO."""
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from uniflow.gui.components import BaseCard, PrimaryButton, StatusChip, VisualBadge
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.presentation import format_time, label
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy


def _line(value, *, muted=False):
    widget = QLabel(str(value))
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
    return widget


class SessionDetailScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.view = None
        self.scoring_dialog = None
        self._scoring_picker_open = False
        self.scoring_button = None
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(15)
        self.back = QPushButton("← Back to Sessions")
        self.back.clicked.connect(controller.back_to_sessions)
        self.body.addWidget(self.back)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(15)
        self.body.addLayout(self.cards)
        self.body.addStretch()
        self.primary = None
        self.export_button = None
        self.chat_export_button = None

    def choose_export_parent(self):
        return QFileDialog.getExistingDirectory(self, "Choose existing export parent directory")

    def choose_result_file(self):
        return QFileDialog.getOpenFileName(self, "Sessionabschluss: V2-Ergebnisdatei auswählen", "",
            "JSON files (*.json);;All files (*)")[0]

    def render(self, dto):
        self.view = dto
        self.setMinimumHeight(0)
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        session = dto.session
        zone = dto.semester.timezone
        identity = resolve_course_visual(session.course_stable_id, dto.course_name)
        header = BaseCard(hero=True)
        header.body.setSpacing(8)
        art = PackagedArtwork(course_artwork_asset(identity), mode="thumbnail")
        art.setFixedHeight(105)
        header.body.addWidget(art)
        course_row = QHBoxLayout()
        course_row.setSpacing(10)
        course_row.addWidget(VisualBadge(identity, compact=True))
        names = QVBoxLayout()
        names.setSpacing(2)
        course_name = _line(dto.course_name)
        course_name.setObjectName("sectionTitle")
        names.addWidget(course_name)
        names.addWidget(_line(session.course_stable_id, muted=True))
        course_row.addLayout(names, 1)
        course_row.addWidget(StatusChip(label(session.state)))
        header.body.addLayout(course_row)
        goal = _line(session.goal)
        goal.setObjectName("heading")
        header.body.addWidget(goal)
        basis = {"RESULT_RECEIVED": "Result received", "HANDOFF_GENERATED": "Package generated",
                 "SESSION_CREATED": "Session created"}[session.activity_basis]
        header.body.addWidget(_line("Created · " + format_time(session.created_at, zone) +
            "   ·   " + basis + " · " + format_time(session.activity_at, zone), muted=True))
        if session.planned_local_wall_time:
            header.body.addWidget(_line("Planned local time · " + session.planned_local_wall_time, muted=True))
        header.setMinimumHeight(header.sizeHint().height())
        header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.cards.addWidget(header)

        targets = BaseCard("Target Skills")
        names = {x.skill_id: x.name for x in dto.target_labels}
        for target in dto.target_labels:
            targets.body.addWidget(_line(f"{target.ordinal + 1}. {target.name} · {target.skill_id}"))
        self.cards.addWidget(targets)
        targets.setMinimumHeight(targets.sizeHint().height())

        next_action = BaseCard("Next action", variant="accepted")
        self.primary = None
        no_numeric = bool(session.result is not None and
            getattr(session.result, "has_numeric_skill_values", None) is False)
        completion_label = ("Session ohne Mastery-Änderung abschließen" if no_numeric
            else "Session abschließen und Mastery-Werte anwenden")
        action = {"PLANNED": ("Generate learning package", lambda: self.controller.generate_learning_package()),
                  "HANDOFF_CREATED": ("Start Session", lambda: self.controller.start_learning_session()),
                  "IN_PROGRESS": ("Sessionabschluss vorbereiten · V2-Ergebnis importieren", self._import_result),
                  "VALIDATED": (completion_label, self._confirm_completion)}.get(session.state)
        if action:
            self.primary = PrimaryButton(action[0])
            self.primary.clicked.connect(action[1])
            next_action.body.addWidget(self.primary)
        elif session.state == "COMPLETED":
            next_action.body.addWidget(_line("Result processing complete. This does not mean every Skill is mastered."))
        else:
            next_action.body.addWidget(_line("This Session state needs refresh or integrity review before another action."))
        if session.state == "IN_PROGRESS":
            next_action.body.addWidget(_line("Für den Sessionabschluss: V2-Ergebnis importieren und anschließend den Abschluss bestätigen. "
                "Das detaillierte Scoring-Review ist ein eigener Ablauf."))
        self.cards.addWidget(next_action)
        next_action.setMinimumHeight(next_action.sizeHint().height())

        self.scoring_button = None
        package = BaseCard("Learning package", variant="secondary")
        self.export_button = None
        self.chat_export_button = None
        if session.current_handoff:
            self.scoring_button = QPushButton("Detailliertes Scoring-Review öffnen (V1)")
            self.scoring_button.clicked.connect(self._preview_scoring)
            package.body.addWidget(self.scoring_button)
            package.body.addWidget(_line("Aufgabenbewertungen prüfen und lokale Reviewentscheidungen speichern. "
                "Dieser Ablauf schließt die Session nicht ab.", muted=True))
            saved_reviews = QPushButton("Gespeicherte Scoring-Reviews öffnen")
            saved_reviews.clicked.connect(self._open_scoring_reviews)
            package.body.addWidget(saved_reviews)
            h = session.current_handoff
            package.body.addWidget(_line(f"Current revision {h.revision} · Generated {format_time(h.generated_at, zone)}"))
            package.body.addWidget(_line(f"Session {session.session_id} · Handoff {h.handoff_id}"))
            package.body.addWidget(_line(f"SHA-256 {h.artifact_sha256}"))
            self.chat_export_button = QPushButton("Export learning chat · single JSON file")
            self.chat_export_button.clicked.connect(self._export_chat)
            package.body.addWidget(self.chat_export_button)
            self.export_button = QPushButton("Export V2 learning package · two files")
            self.export_button.clicked.connect(self._export)
            package.body.addWidget(self.export_button)
            if len(session.handoff_history) > 1:
                package.body.addWidget(_line(f"{len(session.handoff_history) - 1} earlier immutable revision(s)", muted=True))
        else:
            package.body.addWidget(_line("No package generated yet."))
        self.cards.addWidget(package)
        package.setMinimumHeight(package.sizeHint().height())

        result_card = BaseCard("Result", variant="secondary")
        if session.result:
            result = session.result
            result_card.body.addWidget(_line(f"{label(result.session_outcome)} · {label(result.processing_status)}"))
            result_card.body.addWidget(_line("Received · " + format_time(result.received_at, zone), muted=True))
            result_card.body.addWidget(_line("Dokumentierter Abschlusszeitpunkt · " + format_time(result.completed_at, zone), muted=True))
            result_card.body.addWidget(_line(f"Evidence for {len(session.result_evidence)} Skill(s): " +
                ", ".join(names.get(e.skill_id, e.skill_id) for e in session.result_evidence)))
            result_card.body.addWidget(_line("Evidence is an observation; canonical mastery is shown separately.", muted=True))
        else:
            result_card.body.addWidget(_line("No Result imported."))
        self.cards.addWidget(result_card)
        result_card.setMinimumHeight(result_card.sizeHint().height())

        mastery = BaseCard("Mastery application", variant="secondary")
        application = session.mastery_application
        mastery.body.addWidget(_line("Result processed" if application.status == "APPLIED" else "Not applied yet"))
        for item in application.mastery_states:
            mastery.body.addWidget(_line(f"{names.get(item.skill_id, item.skill_id)} · {item.value:.0%} known mastery"))
        if application.skipped_skill_ids:
            mastery.body.addWidget(_line("No new mastery value recorded for: " +
                ", ".join(names.get(sid, sid) for sid in application.skipped_skill_ids) +
                ". The Result had no numeric observation for these Skills."))
        self.cards.addWidget(mastery)
        mastery.setMinimumHeight(mastery.sizeHint().height())

        provenance = BaseCard("Provenance and technical details", variant="secondary")
        for entry in session.materialization_provenance:
            provenance.body.addWidget(_line(label(entry.execution_kind) + " from weekly plan · " +
                format_time(entry.materialized_at, zone), muted=True))
        for diagnostic in session.diagnostics:
            provenance.body.addWidget(_line(label(diagnostic.code), muted=True))
        provenance.body.addWidget(_line("Session ID · " + session.session_id, muted=True))
        if session.current_handoff:
            provenance.body.addWidget(_line("Handoff ID · " + session.current_handoff.handoff_id, muted=True))
            provenance.body.addWidget(_line("Handoff SHA-256 · " + session.current_handoff.artifact_sha256, muted=True))
        if session.result:
            provenance.body.addWidget(_line("Result ID · " + session.result.result_id, muted=True))
        provenance.setVisible(True)
        self.cards.addWidget(provenance)
        self.setMinimumHeight(self.sizeHint().height())
        self.sync()

    def _export(self):
        identity = self.controller.workflow_identity()
        parent = self.choose_export_parent()
        if parent and identity == self.controller.workflow_identity():
            self.controller.export_learning_package(Path(parent))

    def _export_chat(self):
        if self.controller.learning_mutation_busy:
            return
        identity = self.controller.workflow_identity()
        handoff = self.controller.session_detail.session.current_handoff if self.controller.session_detail else None
        parent = self.choose_export_parent()
        current = self.controller.session_detail.session.current_handoff if self.controller.session_detail else None
        if parent and identity == self.controller.workflow_identity() and handoff == current:
            self.controller.export_learning_chat(Path(parent))

    def _preview_scoring(self):
        from uniflow.gui.screens.scoring_preview import ScoringPreviewDialog
        if self.scoring_dialog is not None and self.scoring_dialog.active:
            self.scoring_dialog.raise_()
            return
        if (self._scoring_picker_open or self.controller.learning_mutation_busy
                or self.controller.state.busy[Route.SESSION_DETAIL] != Busy.IDLE
                or self.controller.session_detail is None
                or self.view is None
                or self.view.session.session_id != self.controller.state.selected_session_id
                or self.controller.session_detail.session != self.view.session):
            return
        identity = self.controller.workflow_identity()
        session = self.view.session
        handoff = session.current_handoff
        if not handoff:
            return
        self._scoring_picker_open = True
        try:
            selected = self.choose_scoring_file()
        finally:
            self._scoring_picker_open = False
        if selected and identity == self.controller.workflow_identity():
            self.scoring_dialog = ScoringPreviewDialog(self.controller, Path(selected),
                session.session_id, handoff.handoff_id, self)
            self.scoring_dialog.open()

    def choose_scoring_file(self):
        return QFileDialog.getOpenFileName(self, "Detailliertes Scoring-Review (V1)", "",
            "JSON files (*.json);;All files (*)")[0]

    def _import_result(self):
        selected = self.choose_result_file()
        if selected:
            self.controller.import_learning_result(Path(selected))

    def _confirm_completion(self):
        from uniflow.gui.screens.session_completion import SessionCompletionDialog
        if (self.controller.learning_mutation_busy or self.controller.session_detail is None
                or self.view is None or self.controller.session_detail.session != self.view.session
                or self.view.session.state != "VALIDATED"):
            return
        if getattr(self, "completion_dialog", None) is not None and self.completion_dialog.isVisible():
            self.completion_dialog.raise_()
            return
        self.completion_dialog = SessionCompletionDialog(self.controller, self.view, self)
        self.completion_dialog.open()

    def sync(self):
        busy = (self.controller.learning_mutation_busy or
            self.controller.state.busy[Route.SESSION_DETAIL] != Busy.IDLE)
        if self.primary:
            self.primary.setEnabled(not busy)
        if self.export_button:
            self.export_button.setEnabled(not busy)
        if self.chat_export_button:
            self.chat_export_button.setEnabled(not busy)
        if self.scoring_button:
            self.scoring_button.setEnabled(not busy and self.controller.session_detail is not None
                and self.view.session.session_id == self.controller.state.selected_session_id
                and self.controller.session_detail.session == self.view.session)


    def _open_scoring_reviews(self):
        from uniflow.application.scoring_review import list_reviews
        from uniflow.gui.screens.scoring_preview import ScoringPreviewDialog
        from uniflow.gui.presentation import PresentationError
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        if (self._scoring_picker_open or self.controller.learning_mutation_busy
                or self.controller.state.busy[Route.SESSION_DETAIL] != Busy.IDLE
                or self.view is None or self.controller.session_detail is None
                or self.view.session.session_id != self.controller.state.selected_session_id
                or self.controller.session_detail.session != self.view.session):
            return
        if self.scoring_dialog is not None and self.scoring_dialog.active:
            self.scoring_dialog.raise_()
            return
        session = self.view.session
        if not session.current_handoff:
            return
        identity = self.controller.workflow_identity()
        sid, hid = session.session_id, session.current_handoff.handoff_id
        def loaded(op, token, values):
            if token != self.controller.workflow_identity():
                return
            if isinstance(values, PresentationError):
                QMessageBox.warning(self, "Reviews", "Reviews konnten nicht gelesen werden.")
                return
            if not values:
                QMessageBox.information(self, "Reviews", "Keine gespeicherten Reviews für diese Session.")
                return
            self._scoring_picker_open = True
            try:
                labels = [f"{sha} · Handoff {hid}" for sha, hid in values]
                chosen, ok = QInputDialog.getItem(self, "Gespeichertes Review", "Ergebnis-SHA-256 / Handoff", labels, 0, False)
            finally:
                self._scoring_picker_open = False
            if ok and identity == self.controller.workflow_identity():
                sha, saved_hid = values[labels.index(chosen)]
                self.scoring_dialog = ScoringPreviewDialog(self.controller, None, sid, saved_hid, self, stored_sha=sha)
                self.scoring_dialog.open()
        self.controller.runner.submit("scoring_review_list", identity,
            lambda: list_reviews(self.controller.config, sid), loaded)
