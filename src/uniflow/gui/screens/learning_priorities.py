from uniflow.gui.screen_chrome import SCREEN_STYLE, browser_visuals
"""Read-only learning priorities with generation-guarded one-shot reads."""
from datetime import datetime
from zoneinfo import ZoneInfo
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, QTextBrowser, QPushButton
from uniflow.application.learning_needs import project_learning_needs, PRIORITIES
from uniflow.application.semester_read_models import list_semesters
from uniflow.application.study_replanning import project_study_plan_health
from uniflow.gui.safe_presentation import PRIORITY_LABELS, needs_html, health_html, active_skill_needs


class LearningPrioritiesScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setStyleSheet(SCREEN_STYLE)
        self.controller = controller
        self.active = False
        self.generation = 0
        self.projection = None
        self.health = None
        body = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.semester = QComboBox()
        self.semester.addItem(controller.semester.name, controller.semester.stable_id)
        self.course = QComboBox(); self.course.addItem('Alle Kurse', None)
        self.priority = QComboBox(); self.priority.addItem('Alle Prioritäten', None)
        for p in PRIORITIES: self.priority.addItem(PRIORITY_LABELS.get(p, p), p)
        for w in (self.semester, self.course, self.priority): filters.addWidget(w)
        body.addLayout(filters)
        self.refresh = QPushButton('Aktualisieren'); body.addWidget(self.refresh)
        self.message = QLabel(); self.message.setWordWrap(True); self.message.setTextFormat(Qt.TextFormat.PlainText); body.addWidget(self.message)
        self.technical = QPushButton('Technische Details anzeigen'); self.technical.setCheckable(True); body.addWidget(self.technical)
        self.technical.toggled.connect(self.render)
        self.details = QTextBrowser(); self.details.setMinimumHeight(420); body.addWidget(self.details)
        self.semester.currentIndexChanged.connect(self.change_semester)
        self.course.currentIndexChanged.connect(self.reload)
        self.priority.currentIndexChanged.connect(self.render)
        self.refresh.clicked.connect(self.reload)
        self.study_blocks = QPushButton('Lernblöcke planen und bestätigen'); body.addWidget(self.study_blocks)
        self.study_blocks.clicked.connect(self.open_study_blocks)
        self.health_toggle = QPushButton('Lernplan überprüfen'); self.health_toggle.setCheckable(True)
        body.addWidget(self.health_toggle)
        self.health_details = QTextBrowser(); body.addWidget(self.health_details)
        self.health_details.hide()
        self.health_toggle.toggled.connect(self.health_details.setVisible)
        self.open_calendar = QPushButton('Semesterkalender öffnen'); body.addWidget(self.open_calendar)
        self.open_calendar.clicked.connect(self.show_calendar)

    def activate(self, active):
        if self.active == active: return
        self.active = active
        self.generation += 1
        if active: self.reload()

    def change_semester(self):
        self.course.blockSignals(True); self.course.clear(); self.course.addItem('Alle Kurse', None); self.course.blockSignals(False)
        self.reload()

    def reload(self):
        self.generation += 1
        if not self.active: return
        token = self.generation
        semester, course = self.semester.currentData(), self.course.currentData()
        config = self.controller.config
        # Capture all UI inputs on the GUI thread, before scheduling work.
        clock = getattr(self.controller, 'clock', None)
        instant = clock() if clock else datetime.now(ZoneInfo(self.controller.semester.timezone))
        self.health = None; self.health_details.clear(); self.projection = None; self.details.clear(); self.message.setText('Lernprioritäten werden geladen …')
        def read():
            return list_semesters(config), project_learning_needs(config, semester, instant, course), project_study_plan_health(config, semester, instant, course)
        self.controller.runner.submit('learning_priorities', token, read, self.completed)

    def completed(self, operation, token, result):
        if not self.active or token != self.generation: return
        if not isinstance(result, tuple):
            self.message.setText(f'Lernprioritäten konnten nicht geladen werden: {getattr(result, "message", result)}'); return
        if len(result) < 2: return
        choices, projection = result[:2]
        if projection.semester_id != self.semester.currentData(): return
        if len(result) != 3: return
        health = result[2]
        self.projection = projection
        self.health = health
        selected = self.semester.currentData()
        self.semester.blockSignals(True); self.semester.clear()
        for s in choices: self.semester.addItem(s.name, s.stable_id)
        self.semester.setCurrentIndex(self.semester.findData(selected)); self.semester.blockSignals(False)
        selected = self.course.currentData()
        self.course.blockSignals(True); self.course.clear(); self.course.addItem('Alle Kurse', None)
        for cid, name in self.projection.courses: self.course.addItem(name, cid)
        self.course.setCurrentIndex(max(0, self.course.findData(selected))); self.course.blockSignals(False)
        self.render()

    def render(self):
        self.technical.setText('Technische Details ausblenden' if self.technical.isChecked() else 'Technische Details anzeigen')
        if self.projection is None: return
        needs = [n for n in self.projection.needs if self.priority.currentData() is None or n.priority == self.priority.currentData()]
        if not self.technical.isChecked():
            needs = active_skill_needs(needs)
            self.message.setText(f'{len(needs)} Lernpriorität{"" if len(needs)==1 else "en"} · {self.projection.horizon_days} Kalendertage\n'
                f'Bezugszeitpunkt: {self.projection.as_of:%d.%m.%Y · %H:%M} · {self.projection.as_of.tzname()}')
            self.open_calendar.setVisible(self.semester.currentData() == self.controller.semester.stable_id)
            hints = getattr(getattr(self.controller, 'home', None), 'courses', ())
            visuals = browser_visuals(self.details, self.projection.courses, hints)
            self.details.setHtml(needs_html(needs, visuals))
            advice = [a for a in self.health.advice if self.priority.currentData() is None or a.priority == self.priority.currentData()] if self.health else []
            health_visuals = browser_visuals(self.health_details, self.projection.courses, hints)
            self.health_details.setHtml(health_html(advice, dict(self.projection.courses), health_visuals))
            return
        self.message.setText(f'{len(needs)} Lernbedarfe · {self.projection.horizon_days} Kalendertage · {self.projection.policy_version}\nBezugszeitpunkt: {self.projection.as_of.isoformat()}\n' + '\n'.join(self.projection.diagnostics))
        self.open_calendar.setVisible(self.semester.currentData() == self.controller.semester.stable_id)
        health_lines = []
        if self.health:
            health_lines.extend([f'Bezugszeitpunkt: {self.health.as_of.isoformat()}', *self.health.diagnostics,
                'Aktionen öffnen bestehende Ansichten ohne Vorauswahl. Kalender: aktives Hauptsemester.'])
            for a in self.health.advice:
                if self.priority.currentData() is not None and a.priority != self.priority.currentData(): continue
                health_lines.extend([f'{a.advice_type} · {a.priority}',
                    f'Kurs: {dict(self.projection.courses).get(a.course_id, "Alle Kurse")} · Block: {a.block_id or "—"} · Revision: {a.block_revision or "—"}',
                    *a.reasons, 'Quellen: ' + ', '.join(a.sources), 'Evidenz: ' + a.evidence_quality,
                    'Fehlend: ' + ', '.join(a.missing_information), 'Nächste Aktion: ' + a.user_action, ''])
        self.health_details.setPlainText('\n'.join(health_lines))
        lines = []
        for n in needs:
            lines.extend([f'{n.priority} · {n.course_name}', n.action, 'Begründung: ' + '\n'.join(n.reasons),
                          'Quellen: ' + ', '.join(n.sources), 'Evidenz: ' + n.evidence_quality,
                          'Fehlend: ' + ', '.join(n.missing_information), f'Identität: {n.recommendation_id}', ''])
        self.details.setPlainText('\n'.join(lines) if lines else 'Keine Lernbedarfe für diese Auswahl. Das ist kein Nachweis vollständiger Beherrschung.')

    def show_calendar(self):
        # Existing calendar uses controller semester, so permit navigation only for it.
        if self.semester.currentData() != self.controller.semester.stable_id: return
        from uniflow.gui.routes import Route
        self.controller.navigate(Route.CALENDAR)

    def open_study_blocks(self):
        if self.projection is None or self.projection.semester_id != self.semester.currentData(): return
        from uniflow.gui.study_block_confirmation import StudyBlockConfirmationDialog
        dialog = StudyBlockConfirmationDialog(self.controller, self.projection, self)
        dialog.exec()
        self.reload()

    def closeEvent(self, event):
        self.activate(False)
        super().closeEvent(event)
