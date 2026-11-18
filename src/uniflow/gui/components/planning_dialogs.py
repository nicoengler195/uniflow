"""Small native input dialogs for the existing planning workflow requests."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDateTime, QTime
from PySide6.QtWidgets import (QComboBox, QDateTimeEdit, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTimeEdit, QVBoxLayout)

from uniflow.gui.components import PrimaryButton


class PlanningDialog(QDialog):
    def __init__(self, title, explanation, confirm="Confirm", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("planningDialog")
        self.setMinimumWidth(380)
        body = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        body.addWidget(heading)
        info = QLabel(explanation)
        info.setWordWrap(True)
        info.setObjectName("muted")
        body.addWidget(info)
        self.form = QFormLayout()
        body.addLayout(self.form)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.confirm = PrimaryButton(confirm)
        self.confirm.setAutoDefault(False)
        self.confirm.clicked.connect(self.accept)
        actions.addWidget(self.confirm)
        body.addLayout(actions)


class RegenerateDialog(PlanningDialog):
    def __init__(self, parent=None):
        super().__init__("Regenerate plan", "Choose how the next generated revision uses the current source options.",
            "Regenerate", parent)
        self.course_mode = QComboBox()
        self.course_mode.addItems(["INHERIT", "CLEAR", "REPLACE"])
        self.course_id = QLineEdit()
        self.course_id.setPlaceholderText("Course ID")
        self.duration_mode = QComboBox()
        self.duration_mode.addItems(["INHERIT", "CLEAR", "REPLACE"])
        self.overrides = QLineEdit()
        self.overrides.setPlaceholderText("ACTION_ID=minutes, …")
        self.limit = QSpinBox()
        self.limit.setRange(0, 100)
        self.limit.setSpecialValueText("Inherit")
        self.form.addRow("Course filter", self.course_mode)
        self.form.addRow("Course ID", self.course_id)
        self.form.addRow("Duration overrides", self.duration_mode)
        self.form.addRow("Overrides", self.overrides)
        self.form.addRow("Action limit", self.limit)
        self.course_mode.currentTextChanged.connect(self._sync)
        self.duration_mode.currentTextChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.course_id.setEnabled(self.course_mode.currentText() == "REPLACE")
        self.overrides.setEnabled(self.duration_mode.currentText() == "REPLACE")

    def values(self):
        course_mode = self.course_mode.currentText()
        duration_mode = self.duration_mode.currentText()
        course_id = self.course_id.text().strip() if course_mode == "REPLACE" else None
        if course_mode == "REPLACE" and not course_id:
            raise ValueError("Enter a Course ID for Replace.")
        overrides = None
        if duration_mode == "REPLACE":
            overrides = {}
            for part in self.overrides.text().split(","):
                if not part.strip():
                    continue
                key, sep, value = part.strip().partition("=")
                if not sep or not key or not value.isdecimal() or int(value) <= 0:
                    raise ValueError("Use ACTION_ID=positive_minutes for each override.")
                overrides[key] = int(value)
        return dict(course_filter_mode=course_mode, course_id=course_id,
            duration_overrides_mode=duration_mode, duration_overrides=overrides,
            limit=self.limit.value() or None)


class TimeDialog(PlanningDialog):
    def __init__(self, title, explanation, initial: datetime, timezone: str,
                 *, duration: int | None = None, optional_duration=False, parent=None):
        super().__init__(title, explanation, title, parent)
        self.timezone = timezone
        self.start = QDateTimeEdit()
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("dd MMM yyyy  HH:mm")
        local = initial.astimezone(ZoneInfo(timezone))
        self.start.setDateTime(QDateTime(local.year, local.month, local.day,
            local.hour, local.minute, 0))
        self.form.addRow("Start", self.start)
        self.duration = None
        if optional_duration:
            self.duration = QSpinBox()
            self.duration.setRange(0, 2147483647)
            self.duration.setSpecialValueText("Use captured duration")
            self.duration.setValue(duration or 0)
            self.form.addRow("Duration (minutes)", self.duration)

    def values(self):
        value = self.start.dateTime().toPython()
        local = value.replace(tzinfo=ZoneInfo(self.timezone))
        # Aware DTO validation and backend calendar checks remain authoritative.
        return local, (self.duration.value() or None) if self.duration else None


class DurationDialog(PlanningDialog):
    def __init__(self, minutes, parent=None):
        super().__init__("Change duration", "Create a new draft revision with this block duration.",
            "Change duration", parent)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 2147483647)
        self.minutes.setValue(minutes)
        self.form.addRow("Minutes", self.minutes)


class GoalDialog(PlanningDialog):
    def __init__(self, parent=None):
        super().__init__("Create Session", "Enter a goal for this accepted plan block.",
            "Create Session", parent)
        self.goal = QLineEdit()
        self.goal.setMaxLength(4000)
        self.form.addRow("Goal", self.goal)


class RemoveDialog(PlanningDialog):
    def __init__(self, parent=None):
        super().__init__("Remove block", "Remove this block from the new draft revision?",
            "Remove from draft", parent)
        self.confirm.setDefault(False)


class ProfileDialog(PlanningDialog):
    def __init__(self, parent=None):
        super().__init__("Persönliche Lernzeiten", "Planning Profile anlegen. Danach Lernzeitfenster eintragen.",
            "Profil anlegen", parent)
        self.duration = QSpinBox()
        self.duration.setRange(15, 480)
        self.duration.setValue(60)
        self.daily = QSpinBox()
        self.daily.setRange(15, 1440)
        self.daily.setValue(180)
        self.form.addRow("Standardblock (Min.)", self.duration)
        self.form.addRow("Maximal pro Tag (Min.)", self.daily)


class AvailabilityDialog(PlanningDialog):
    WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

    def __init__(self, parent=None):
        super().__init__("Lernzeitfenster", "Wiederkehrendes persönliches Zeitfenster für Lernblöcke eintragen.",
            "Zeitfenster speichern", parent)
        self.weekday = QComboBox()
        self.weekday.addItems(("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"))
        self.start = QTimeEdit(QTime(16, 0))
        self.end = QTimeEdit(QTime(18, 0))
        self.start.setDisplayFormat("HH:mm")
        self.end.setDisplayFormat("HH:mm")
        self.form.addRow("Wochentag", self.weekday)
        self.form.addRow("Beginn", self.start)
        self.form.addRow("Ende", self.end)

    def values(self):
        start, end = self.start.time().toPython(), self.end.time().toPython()
        if start >= end:
            raise ValueError("Das Ende muss nach dem Beginn liegen.")
        return self.WEEKDAYS[self.weekday.currentIndex()], start, end
