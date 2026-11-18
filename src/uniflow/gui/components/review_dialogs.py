"""Focused, styled confirmations for explicit human review decisions."""
from PySide6.QtWidgets import QDialog, QLabel, QPlainTextEdit

from uniflow.gui.components.planning_dialogs import PlanningDialog


class ReviewConfirmDialog(PlanningDialog):
    def __init__(self, title, explanation, confirm, parent=None):
        super().__init__(title, explanation, confirm, parent)


class ReviewRejectDialog(PlanningDialog):
    def __init__(self, parent=None):
        super().__init__("Reject proposal", "This decision is kept in proposal history. A reason is optional.",
            "Reject proposal", parent)
        self.reason = QPlainTextEdit()
        self.reason.setObjectName("reviewReason")
        self.reason.setPlaceholderText("Optional reason")
        self.reason.textChanged.connect(self._limit)
        self.form.addRow("Reason", self.reason)
        self.count = QLabel("0 / 1000")
        self.count.setObjectName("muted")
        self.form.addRow("", self.count)

    def _limit(self):
        value = self.reason.toPlainText()
        if len(value) > 1000:
            cursor = self.reason.textCursor()
            self.reason.blockSignals(True)
            self.reason.setPlainText(value[:1000])
            self.reason.setTextCursor(cursor)
            self.reason.blockSignals(False)
            value = self.reason.toPlainText()
        self.count.setText(f"{len(value)} / 1000")

    def value(self):
        return self.reason.toPlainText() or None
