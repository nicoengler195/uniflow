"""ScoringResultV1 review with explicit local, versioned decisions."""
import json
import hashlib
from uuid import uuid4

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QPushButton, QApplication, QTabWidget, QListWidget, QSplitter, QTableWidget, QTableWidgetItem, QComboBox, QLineEdit, QCheckBox, QLabel, QFileDialog

from uniflow.application import learning_workflow, scoring_review
from uniflow.application.learning_chat_exchange import SCORING_RESULT_MESSAGES
from uniflow.application.scoring_contract_v1 import display_points
from uniflow.gui.presentation import PresentationError
from uniflow.gui.screens.scoring_style import scoring_dialog_stylesheet
from uniflow.gui.screens.scoring_review import task_text, observations_text, review_text


def preview_text(preview):
    result, score = preview.result, preview.score
    def points(value):
        return "Nicht bewertet" if value is None else str(display_points(value))
    lines = ["Technisch validiert – fachlich noch nicht bestätigt.",
        "Eine formal gültige KI-Bewertung ist kein mathematischer Wahrheitsnachweis.",
        "Keine automatische fachliche Anerkennung oder Mastery-Übernahme.",
        f"Session: {result.session_id}", f"Handoff: {result.handoff_id}",
        f"Revision: {preview.handoff_revision}", f"SHA-256: {result.handoff_artifact_sha256}",
        "Validierungsstatus: Technisch validiert", f"Reviewstatus: {preview.status}",
        f"Bewertung: {score.assessment_status} · {score.completion_status}",
        f"Lernphasenpunkte: {points(score.phase_points['LEARNING'])} / 50",
        f"Abschlussprüfung: {points(score.phase_points['FINAL_EXAM'])} / 50",
        f"Gesamtwert: {points(score.session_score)} / 100", "", "Phasen und Prüfungsentscheidung"]
    # JSON is presentation only: all values have already been validated by Application.
    def show(value):
        return json.dumps(value, ensure_ascii=False, indent=2)
    lines.append(show([s.model_dump(mode="json") for s in result.phase_states]))
    lines.append(show(result.final_exam_decision.model_dump(mode="json"))
        if result.final_exam_decision else "Prüfungsentscheidung: Nicht verfügbar")
    lines.extend(["", "Skillbezogene Punkte (keine Mastery)"])
    for sid, phases in score.skill_phase_points.items():
        lines.append(f"{sid}: LEARNING {points(phases['LEARNING'])} / 50; "
            f"FINAL_EXAM {points(phases['FINAL_EXAM'])} / 50; "
            f"Gesamt {points(score.skill_session_scores[sid])} / 100")
    if not score.skill_phase_points:
        lines.append("Nicht bewertet")
    lines.extend(["", "Aufgaben, Schwierigkeitsgrade, Kriterien und Belege",
        show([t.model_dump(mode="json") for t in result.tasks]),
        "", "Hilfestellungen", show(preview.assistance),
        "", "Fehlerbeobachtungen", show(preview.errors),
        "", "Qualitative Beobachtungen", show(preview.qualitative_observations),
        "", "Unsicherheiten und offene Angaben",
        "\n".join(preview.open_information) or "Keine weiteren offenen Angaben im Vertrag.",
        "Fachliche Richtigkeit, Chronologie und Unabhängigkeit bleiben menschlich zu prüfen.",
        "S09 = NOT_VERIFIED"])
    return "\n".join(lines)


class ScoringPreviewDialog(QDialog):
    def __init__(self, controller, path, session_id, handoff_id, parent=None, *, stored_sha=None):
        super().__init__(parent)
        self.setObjectName("scoringPreviewDialog")
        self.setStyleSheet(scoring_dialog_stylesheet())
        self.controller = controller
        self.identity = controller.workflow_identity()
        self.expected = (session_id, handoff_id)
        self.active = True
        self.saving = False
        self.review_state = None
        self.owner = parent.window() if parent else None
        if self.owner:
            self.owner.installEventFilter(self)
        self.setWindowTitle("Detailliertes Scoring-Review (V1)")
        self.resize(820, 680)
        layout = QVBoxLayout(self)
        self.headline = QLabel("Scoring-Ergebnis wird geprüft …")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText("Scoring-Ergebnis wird geprüft …")
        self.tabs = QTabWidget()
        self.overview = QPlainTextEdit()
        self.overview.setReadOnly(True)
        self.overview.setPlainText(self.text.toPlainText())
        self.tabs.addTab(self.overview, "Übersicht")
        self.task_list = QListWidget()
        self.task_detail = QPlainTextEdit()
        self.task_detail.setReadOnly(True)
        tasks = QSplitter()
        tasks.addWidget(self.task_list)
        tasks.addWidget(self.task_detail)
        tasks.setStretchFactor(1, 1)
        self.tabs.addTab(tasks, "Aufgabenprüfung")
        self.observations = QPlainTextEdit()
        self.observations.setReadOnly(True)
        self.tabs.addTab(self.observations, "Fehler und Skills")
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.tabs.addTab(self.review, "Prüfbedarf")
        self.raw = self.text
        self.tabs.addTab(self.raw, "Technische Details")
        for index in range(1, self.tabs.count()):
            self.tabs.setTabEnabled(index, False)
        layout.addWidget(self.tabs)
        self.task_list.currentRowChanged.connect(self._task_selected)
        self.decisions = QTableWidget(0, 2)
        self.decisions.setHorizontalHeaderLabels(["Skill-ID", "Lokale Reviewentscheidung"])
        self.decisions.setMaximumHeight(130)
        self.decisions.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.decisions)
        self.reason = QLineEdit()
        self.reason.setMaxLength(4000)
        self.reason.setPlaceholderText("Optionale Begründung (bis 4000 Zeichen)")
        layout.addWidget(self.reason)
        self.confirm = QCheckBox("Ich bestätige die gewählten Entscheidungen lokal. Kein unabhängiger Fachnachweis.")
        layout.addWidget(self.confirm)
        self.save_button = QPushButton("Reviewentscheidung dauerhaft speichern")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_review)
        layout.addWidget(self.save_button)
        self.save_status = QLabel("Noch keine Reviewentscheidung gespeichert. S09 = NOT_VERIFIED")
        self.save_status.setWordWrap(True)
        layout.addWidget(self.save_status)
        self.mastery_button = QPushButton("Mastery-Änderungsvorschau")
        self.mastery_button.setEnabled(False)
        self.mastery_button.clicked.connect(self._preview_mastery)
        layout.addWidget(self.mastery_button)
        self.other_file = QPushButton("Andere Datei auswählen")
        self.other_file.clicked.connect(self._choose_other_file)
        self.other_file.hide()
        layout.addWidget(self.other_file)
        self._review_controls(False)
        close = QPushButton("Schließen")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        controller.state_changed.connect(self._selection_changed)
        QApplication.instance().aboutToQuit.connect(self.reject)
        self.finished.connect(self._finished)
        def validate():
            try:
                preview = (scoring_review.reopen_review(controller.config, stored_sha, session_id, handoff_id)
                    if stored_sha else learning_workflow.preview_scoring_result(controller.config, path))
                sha = hashlib.sha256(preview.raw_bytes).hexdigest()
                state = scoring_review.read_review(controller.config, sha, session_id, handoff_id)
                return preview, state
            except ValueError as exc:
                code = str(exc)
                if code in SCORING_RESULT_MESSAGES:
                    return PresentationError(code, "Prüfung fehlgeschlagen", getattr(exc, "message", SCORING_RESULT_MESSAGES[code]))
                raise
        controller.runner.submit("scoring_preview", self.identity, validate, self._completed)

    def eventFilter(self, watched, event):
        if watched is self.owner and event.type() == QEvent.Type.Close:
            self.reject()
        return super().eventFilter(watched, event)

    def _finished(self, *_):
        if not self.active:
            return
        self.active = False
        if self.owner:
            self.owner.removeEventFilter(self)
        try:
            self.controller.state_changed.disconnect(self._selection_changed)
        except RuntimeError:
            pass

    def _selection_changed(self):
        if self.identity != self.controller.workflow_identity():
            self.reject()

    def _completed(self, operation, token, result):
        if not self.active or token != self.controller.workflow_identity():
            return
        if isinstance(result, PresentationError):
            self._rejected_file(result)
            return
        state = None
        if isinstance(result, tuple):
            result, state = result
        if (result.result.session_id, result.result.handoff_id) != self.expected:
            self._rejected_file(PresentationError("SCORING_RESULT_SESSION_MISMATCH", "Prüfung fehlgeschlagen",
                "Das Ergebnis gehört nicht zur ausgewählten Session und ihrem Handoff."))
            return
        self.headline.setText("Scoring-Ergebnis geprüft. Lokale Reviewentscheidungen sind jetzt möglich.")
        self._review_controls(True)
        self.other_file.hide()
        self.preview = result
        self.review_state = state
        raw = preview_text(result)
        self.raw.setPlainText(raw)
        summary = raw.split("\nPhasen und Prüfungsentscheidung", 1)[0]
        references = sum(len(getattr(t, "skill_marks", [])) + len(t.additional_skills)
                         for t in result.result.tasks)
        self.overview.setPlainText(summary + "\nErgebnis-ID: Im Vertrag nicht enthalten" +
            f"\nErfasste Aufgaben: {len(result.result.tasks)}" +
            f"\nSkillbezüge in Aufgaben (Vertragsvorkommen): {references}" +
            f"\nSkillbezüge in Fehlerbeobachtungen: {sum(e['skill_id'] is not None for e in result.errors)}" +
            "\nS09 = NOT_VERIFIED")
        self.overview.appendPlainText("Ergebnis-SHA-256: " + hashlib.sha256(result.raw_bytes).hexdigest())
        self.observations.setPlainText(observations_text(result))
        self.review.setPlainText(review_text(result))
        for task in result.result.tasks:
            self.task_list.addItem(task.task_id)
        if result.result.tasks:
            self.task_list.setCurrentRow(0)
        else:
            self.task_detail.setPlainText("Keine Aufgaben im Ergebnis enthalten.")
        for index in range(1, self.tabs.count()):
            self.tabs.setTabEnabled(index, True)
        self.decisions.setRowCount(len(result.allowed_skill_ids))
        for row, sid in enumerate(result.allowed_skill_ids):
            score = result.score.skill_session_scores.get(sid)
            item = QTableWidgetItem(sid)
            from PySide6.QtCore import Qt
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.decisions.setItem(row, 0, item)
            choice = QComboBox()
            labels = {"PENDING_HUMAN_REVIEW": "Ungeprüft", "ACCEPTED": "Akzeptiert (lokal)",
                      "DEFERRED": "Zur Korrektur zurückgestellt", "REJECTED": "Abgelehnt"}
            for status in scoring_review.STATUSES:
                if status != "ACCEPTED" or score is not None:
                    choice.addItem(labels[status], status)
            choice.setCurrentIndex(max(0, choice.findData(state.decisions.get(sid) if state else None)))
            self.decisions.setCellWidget(row, 1, choice)
        self._show_state(state)
        current = self.controller.session_detail.session.current_handoff if self.controller.session_detail else None
        self.archived = current is None or current.handoff_id != self.expected[1]
        self.save_button.setEnabled(bool(self.decisions.rowCount()) and not self.archived)
        if self.archived:
            self.save_status.setText(self.save_status.text() + " · Archiviertes Handoff: nur lesbar")

    def _review_controls(self, visible):
        for widget in (self.decisions, self.reason, self.confirm, self.save_button,
                       self.save_status, self.mastery_button):
            widget.setVisible(visible)
        self.confirm.setEnabled(visible)
        if not visible:
            self.confirm.setChecked(False)
            self.save_button.setEnabled(False)
            self.mastery_button.setEnabled(False)

    def _rejected_file(self, error):
        self._review_controls(False)
        self.other_file.show()
        draft = error.code == "SCORING_RESULT_REVIEW_DRAFT"
        headline = ("Prüfentwurf erkannt. Für diese Datei ist noch keine Reviewentscheidung möglich."
            if draft else "Datei abgelehnt. Für diese Datei ist keine Reviewentscheidung möglich.")
        self.headline.setText(headline)
        readable = error.message.partition("Technische Befunde")[0]
        if draft:
            readable = "\n".join(readable.splitlines()[1:])
            # Index labels refer to the actual returned task order, never invented task IDs.
            import re
            readable = re.sub(r"tasks\.(\d+)\.answered_at",
                lambda match: f"Antwortzeit zu Aufgabe {int(match[1]) + 1}", readable)
            readable = readable.replace("final_exam_decision.offered_at", "Zeitpunkt des Abschlussprüfungsangebots")
            readable = readable.replace("final_exam_decision.decided_at", "Zeitpunkt der Entscheidung Start")
            readable = readable.replace("difficulty_rationale", "Begründung der Schwierigkeitswahl")
            readable = readable.replace("skill_marks und additional_skills", "bewerteten und zusätzlichen Skillbeobachtungen")
        self.overview.setPlainText(headline + "\n\n" + readable.strip() +
            "\n\nNächster Schritt: Andere Datei auswählen oder Schließen. "
            "Der Originalentwurf bleibt als Prüfunterlage erhalten. Es wurde nichts gespeichert.")
        self.raw.setPlainText(error.message + "\nFehlercode: " + error.code)
        self.tabs.setTabEnabled(self.tabs.indexOf(self.raw), True)
        self.tabs.setCurrentIndex(0)

    def _choose_other_file(self):
        if not self.active or self.identity != self.controller.workflow_identity():
            return
        selected, _ = QFileDialog.getOpenFileName(self, "Detailliertes Scoring-Review (V1)", "",
            "JSON files (*.json);;All files (*)")
        if not selected or not self.active or self.identity != self.controller.workflow_identity():
            return
        from pathlib import Path
        replacement = ScoringPreviewDialog(self.controller, Path(selected), *self.expected, self.parentWidget())
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "scoring_dialog"):
            parent.scoring_dialog = replacement
        self.replacement_dialog = replacement
        self.reject()
        replacement.open()

    def _task_selected(self, row):
        if row >= 0:
            self.task_detail.setPlainText(task_text(self.preview.result.tasks[row], self.preview))


    def _show_state(self, state):
        if state is None or not state.version:
            return
        self.review_state = state
        self.mastery_button.setEnabled(True)
        self.reason.setText(state.reason)
        self.save_status.setText(f"Gespeichert: {state.result_sha256} · Version {state.version} · "
            "LOCAL_GUI_CONFIRMATION · S09 = NOT_VERIFIED")
        self.review.setPlainText("Originale KI-Aussage bleibt fachlich unbestätigt.\n" +
                                 "Aktuelle lokale Skillentscheidungen: " + json.dumps(state.decisions, ensure_ascii=False) +
                                 "\n\n" + review_text(self.preview) + "\n\nPersistenter Reviewverlauf (lokale Bestätigung):\n" +
                                 json.dumps(state.history, ensure_ascii=False, indent=2))

    def _save_review(self):
        if (not self.active or self.saving or getattr(self, "archived", False) or not hasattr(self, "preview")
                or self.identity != self.controller.workflow_identity()):
            return
        if not self.confirm.isChecked():
            self.save_status.setText("Bitte die lokale Benutzerbestätigung ausdrücklich aktivieren.")
            return
        raw = self.preview.raw_bytes
        decisions = {self.decisions.item(row, 0).text(): self.decisions.cellWidget(row, 1).currentData()
                     for row in range(self.decisions.rowCount())}
        request = dict(session_id=self.expected[0], handoff_id=self.expected[1],
            handoff_revision=self.preview.handoff_revision, result_sha256=hashlib.sha256(raw).hexdigest(),
            decisions=decisions, reason=self.reason.text(),
            expected_version=self.review_state.version if self.review_state else 0,
            request_id=str(uuid4()), confirmed=True)
        self.saving = True
        self.save_button.setEnabled(False)
        self.confirm.setChecked(False)
        self.save_status.setText("Reviewentscheidung wird gespeichert …")
        def save():
            try:
                return scoring_review.save_review(self.controller.config, raw, **request)
            except ValueError as exc:
                return PresentationError(str(exc), "Speichern fehlgeschlagen",
                    "Review konnte nicht gespeichert werden. Ergebnis und Versionsstand erneut öffnen und prüfen.")
        self.controller.runner.submit("scoring_review_save", self.identity, save, self._saved_review)

    def _saved_review(self, operation, token, result):
        self.saving = False
        if not self.active or token != self.controller.workflow_identity():
            return
        self.save_button.setEnabled(True)
        if isinstance(result, PresentationError):
            self.save_status.setText("Nicht gespeichert. Ergebnis und Versionsstand erneut öffnen und prüfen. " + result.code)
            return
        self._show_state(result)

    def _preview_mastery(self):
        if not self.active or self.saving or not hasattr(self, "preview") or self.identity != self.controller.workflow_identity():
            return
        from uniflow.application import controlled_mastery
        sha = hashlib.sha256(self.preview.raw_bytes).hexdigest()
        def load():
            try:
                return controlled_mastery.preview_mastery(self.controller.config, sha, *self.expected)
            except ValueError as exc:
                return PresentationError(str(exc), "Vorschau fehlgeschlagen", "Review erneut öffnen.")
        self.controller.runner.submit("controlled_mastery_preview", self.identity, load, self._mastery_completed)

    def _mastery_completed(self, operation, token, result):
        if not self.active or token != self.controller.workflow_identity():
            return
        from uniflow.application import controlled_mastery
        dialog = QDialog(self)
        dialog.setObjectName("scoringPreviewDialog")
        dialog.setStyleSheet(scoring_dialog_stylesheet())
        dialog.setWindowTitle("Kontrollierte Mastery-Vorschau")
        dialog.resize(820, 580)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(result.message if isinstance(result, PresentationError) else controlled_mastery.preview_text(result))
        layout.addWidget(text)
        choices = []
        if not isinstance(result, PresentationError):
            for skill in result.skills:
                if skill.proposed_value is not None:
                    before = skill.previous_value if skill.previous_value is not None else 'UNKNOWN'
                    choice = QCheckBox(f"{skill.skill_id}: {before} -> {skill.proposed_value}")
                    choices.append((skill.skill_id, choice))
                    layout.addWidget(choice)
        confirm = QCheckBox("Ich bestaetige ausdruecklich die angezeigten Mastery-Aenderungen fuer die ausgewaehlten Skills.")
        layout.addWidget(confirm)
        apply = QPushButton("Ausgewaehlte Mastery-Aenderungen anwenden")
        apply.setEnabled(bool(choices))
        status = QLabel()
        status.setWordWrap(True)
        def submit():
            selected = [sid for sid, choice in choices if choice.isChecked()]
            if not confirm.isChecked() or not selected:
                status.setText("Skills auswaehlen und Mastery-Anwendung gesondert bestaetigen.")
                return
            if not self.active or token != self.controller.workflow_identity() or not dialog.isVisible():
                return
            apply.setEnabled(False)
            confirm.setChecked(False)
            for _, choice in choices:
                choice.setEnabled(False)
            def write():
                try:
                    return controlled_mastery.apply_mastery(self.controller.config, result, selected, confirmed=True)
                except ValueError as exc:
                    return PresentationError(str(exc), "Nicht angewendet", "Vorschau schliessen und erneut pruefen.")
            def completed(operation, current_token, outcome):
                if not self.active or current_token != self.controller.workflow_identity():
                    return
                if isinstance(outcome, PresentationError):
                    status.setText("Nicht angewendet: " + outcome.code + ". Vorschau erneut oeffnen.")
                else:
                    status.setText("Atomar angewendet. Anwendungsreferenzen: " + ", ".join(outcome) + " | S09 = NOT_VERIFIED")
            self.controller.runner.submit("controlled_mastery_apply", self.identity, write, completed)
        apply.clicked.connect(submit)
        layout.addWidget(apply)
        layout.addWidget(status)
        close = QPushButton("Schließen")
        close.clicked.connect(dialog.reject)
        layout.addWidget(close)
        self.mastery_dialog = dialog
        dialog.show()
