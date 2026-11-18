"""Explicit confirmation of the existing V2 completion and mastery policy."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPlainTextEdit, QPushButton
from uniflow.application.learning_result_mastery import preview_learning_result_mastery
from uniflow.gui.presentation import PresentationError
from uniflow.gui.screens.scoring_style import scoring_dialog_stylesheet


class SessionCompletionDialog(QDialog):
    def __init__(self, controller, detail, parent=None):
        super().__init__(parent)
        self.setObjectName('scoringPreviewDialog')
        self.setStyleSheet(scoring_dialog_stylesheet())
        self.setWindowTitle('Sessionabschluss bestätigen')
        self.resize(740, 440)
        self.controller = controller
        self.identity = controller.workflow_identity()
        self.session_id = detail.session.session_id
        self.result_id = detail.session.result.result_id
        self.active = True
        self.ready = False
        body = QVBoxLayout(self)
        self.summary = QLabel('Abschluss und mögliche Mastery-Änderungen werden geprüft …')
        self.summary.setWordWrap(True)
        body.addWidget(self.summary)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        body.addWidget(self.details)
        self.confirm_button = QPushButton('Sessionabschluss bestätigen')
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self._confirm)
        body.addWidget(self.confirm_button)
        cancel = QPushButton('Abbrechen')
        cancel.clicked.connect(self.reject)
        body.addWidget(cancel)
        self.finished.connect(self._finished)
        controller.state_changed.connect(self._selection_changed)
        controller.runner.submit('session_completion_preview', self.identity,
            lambda: preview_learning_result_mastery(controller.config, self.result_id), self._loaded)

    def _current(self):
        detail = self.controller.session_detail
        return (self.active and self.identity == self.controller.workflow_identity()
            and detail is not None and detail.session.session_id == self.session_id
            and detail.session.state == 'VALIDATED' and detail.session.result is not None
            and detail.session.result.result_id == self.result_id)

    def _loaded(self, operation, token, result):
        if token != self.identity or not self._current():
            return
        if isinstance(result, PresentationError):
            self.summary.setText('Abschluss nicht möglich. Session aktualisieren und erneut prüfen.')
            self.details.setPlainText(result.message + '\nFehlercode: ' + result.code)
            return
        numeric = {sid: value for sid, value in result.items() if value is not None}
        if numeric:
            self.summary.setText('Session abschließen und die folgenden Mastery-Werte anwenden?')
            self.confirm_button.setText('Session abschließen und Mastery-Werte anwenden')
            text = '\n'.join(f'{sid}: neuer Mastery-Wert {value:.2%}' for sid, value in numeric.items())
        else:
            self.summary.setText('Session ohne Mastery-Änderung abschließen?')
            self.confirm_button.setText('Session ohne Mastery-Änderung abschließen')
            text = ('Das V2-Ergebnis enthält keine numerischen Skillwerte. '
                'Es wird kein neuer numerischer Mastery-Wert angelegt. Vorhandene Werte bleiben erhalten.')
        self.details.setPlainText(text + '\n\nSession: ' + self.session_id + '\nErgebnis: ' + self.result_id +
            '\n\nNach Bestätigung steht die Session auf Completed. '
            'Die vorhandenen Aufgaben und ihr Scoring-Prüfentwurf bleiben erhalten; '
            'dieser Schritt bestätigt keine historische Chat-Chronologie und kein V1-Scoringreview.')
        self.ready = True
        self.confirm_button.setEnabled(True)

    def _confirm(self):
        if not self.ready or not self._current():
            return
        self.ready = False
        self.confirm_button.setEnabled(False)
        if self.controller.apply_learning_mastery():
            self.accept()
        else:
            self.summary.setText('Abschluss nicht gestartet. Session aktualisieren.')

    def _selection_changed(self):
        if not self._current():
            self.reject()

    def _finished(self, *_):
        self.active = False
        try:
            self.controller.state_changed.disconnect(self._selection_changed)
        except RuntimeError:
            pass
