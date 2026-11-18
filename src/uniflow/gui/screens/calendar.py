from uniflow.gui.safe_presentation import material_expectation
from uniflow.gui.screen_chrome import SCREEN_STYLE
"""Semester calendar widgets using the existing one-shot TaskRunner."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import calendar
from PySide6.QtCore import QDate, Qt, QSize
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QComboBox, QCalendarWidget, QListWidget, QListWidgetItem)
from uniflow.application.semester_calendar import project_semester_calendar, CalendarProjectionV1
from uniflow.application.semester_read_models import list_semesters
from uniflow.gui.routes import Route
from uniflow.gui.components.visual_badge import badge_icon
from uniflow.gui.academic_presentation import academic_course_visual, course_caption
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY
from uniflow.gui.presentation import information_status
from uniflow.gui.deadline_event_presentation import DeadlineSummary
from uniflow.gui.calendar_material_presentation import calendar_material_icon, calendar_material_kind


_DAY_NAMES = ('Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag')
_ACTIVITY_NAMES = {'LECTURE': 'Vorlesung', 'EXERCISE': 'Übung', 'TUTORIAL': 'Tutorium',
                   'PRACTICAL': 'Praktikum', 'LAB': 'Praktikum'}


def event_clock(event, timezone):
    zone = ZoneInfo(timezone)
    start = event.starts_at.astimezone(zone) if event.starts_at else event.expected_time
    end = event.ends_at.astimezone(zone) if event.ends_at else None
    return (start.strftime('%H:%M') if start else 'Uhrzeit unbekannt') + (end.strftime('–%H:%M') if end else '')


def event_kind(event):
    if event.event_type == 'DEADLINE': return 'Frist'
    if event.event_type == 'CANCELLATION': return 'Ausfallhinweis'
    if event.event_type == 'STUDY_BLOCK_CONFIRMED':
        return 'Erledigter Lernblock' if event.status == 'COMPLETED' else 'Bestätigter Lernblock'
    if event.event_type == 'MATERIAL_EXPECTED':
        return 'Materialzuordnung prüfen' if event.status=='AMBIGUOUS' else material_expectation(event.material_kind,event.information_status,linked=event.status=='CONFIRMED_PRESENT')[0]
    if event.event_type == 'MATERIAL_OBSERVED': return 'Material erfasst/importiert'
    # Only an explicit activity code in the DTO title is translated. Custom titles
    # do not prove lecture/exercise type; no academic classification is inferred.
    return _ACTIVITY_NAMES.get(event.title.upper(), 'Veranstaltung')

class CalendarScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setStyleSheet(SCREEN_STYLE)
        self.controller = controller
        self.generation = 0
        self.active = False
        self.anchor = self.current_date()
        self.projection = None
        body = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.semester = QComboBox(); self.semester.addItem(controller.semester.name, controller.semester.stable_id)
        self.course = QComboBox(); self.course.addItem('Alle Kurse', None)
        self.kind = QComboBox(); self.kind.addItems(['Alle Ereignisse', 'Veranstaltungen', 'Fristen', 'Material', 'Lernblöcke'])
        self.mode = QComboBox(); self.mode.addItems(['Woche', 'Monat'])
        for widget in (self.semester, self.course, self.kind, self.mode): filters.addWidget(widget)
        body.addLayout(filters)
        nav = QHBoxLayout()
        self.previous = QPushButton('← Zurück'); self.today = QPushButton('Heute'); self.next = QPushButton('Weiter →')
        self.period = QLabel()
        for w in (self.previous, self.today, self.period, self.next): nav.addWidget(w)
        body.addLayout(nav)
        self.message = QLabel(); self.message.setWordWrap(True); body.addWidget(self.message)
        self.month = QCalendarWidget(); self.month.setFirstDayOfWeek(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.DayOfWeek.Monday)
        from PySide6.QtGui import QTextCharFormat, QColor
        header = QTextCharFormat(); header.setForeground(QColor('#17243c')); header.setBackground(QColor('#dbe5ef'))
        self.month.setHeaderTextFormat(header)
        self.month.setNavigationBarVisible(False); body.addWidget(self.month)
        self.agenda = QListWidget(); self.agenda.setMinimumHeight(280); self.agenda.setWordWrap(True); self.agenda.setIconSize(QSize(58, 48)); body.addWidget(self.agenda)
        self.edit_study = QPushButton('Lernblock bearbeiten / Änderungshistorie'); body.addWidget(self.edit_study)
        self.edit_study.clicked.connect(self.open_study_changes)
        self.complete_study = QPushButton('Lernblock abschließen / Abschlussnachweis'); body.addWidget(self.complete_study)
        self.complete_study.clicked.connect(self.open_study_completion)
        self.technical = QPushButton('Technische Details anzeigen'); self.technical.setCheckable(True); body.addWidget(self.technical)
        self.technical.toggled.connect(lambda: self.show_details(self.agenda.currentRow()))
        self.details = QLabel(); self.details.setTextFormat(Qt.TextFormat.PlainText); self.details.setWordWrap(True); self.details.setTextInteractionFlags(__import__('PySide6.QtCore', fromlist=['Qt']).Qt.TextInteractionFlag.TextSelectableByMouse); body.addWidget(self.details)
        self.previous.clicked.connect(lambda: self.move(-1)); self.next.clicked.connect(lambda: self.move(1))
        self.today.clicked.connect(self.go_today)
        self.mode.currentIndexChanged.connect(self.reload)
        self.semester.currentIndexChanged.connect(self.change_semester)
        self.course.currentIndexChanged.connect(self.reload)
        self.kind.currentIndexChanged.connect(self.render)
        self.month.selectionChanged.connect(self.select_day)
        self.agenda.currentRowChanged.connect(self.show_details)

    def activate(self, active):
        if active == self.active: return
        self.active = active; self.generation += 1
        if active: self.reload()

    def current_instant(self):
        zone = ZoneInfo(self.controller.semester.timezone)
        clock = getattr(self.controller, 'clock', None)
        return (clock() if clock else datetime.now(zone)).astimezone(zone)

    def current_date(self):
        return self.current_instant().date()

    def go_today(self):
        self.anchor = self.current_date(); self.reload()

    def change_semester(self):
        self.course.blockSignals(True); self.course.clear(); self.course.addItem('Alle Kurse', None); self.course.blockSignals(False)
        self.reload()

    def move(self, direction):
        if self.mode.currentIndex() == 0: self.anchor += timedelta(days=7*direction)
        else:
            index = self.anchor.year*12+self.anchor.month-1+direction
            year, month = divmod(index, 12); month += 1
            self.anchor = date(year, month, min(self.anchor.day, calendar.monthrange(year, month)[1]))
        self.reload()

    def reload(self):
        self.generation += 1
        if not self.active: return
        token = self.generation
        start = self.anchor-timedelta(days=self.anchor.weekday()) if self.mode.currentIndex()==0 else self.anchor.replace(day=1)
        end = start+timedelta(days=6) if self.mode.currentIndex()==0 else start.replace(day=calendar.monthrange(start.year,start.month)[1])
        semester, course = self.semester.currentData(), self.course.currentData()
        instant = self.current_instant()
        self.period.setText(f'{start:%d.%m.%Y} – {end:%d.%m.%Y}')
        self.agenda.clear(); self.details.clear(); self.projection = None; self.message.setText('Kalender wird geladen …')
        self.month.setVisible(self.mode.currentIndex()==1)
        def read():
            return list_semesters(self.controller.config), project_semester_calendar(self.controller.config, semester, start, end, course, as_of=instant)
        self.controller.runner.submit('semester_calendar', token, read, self.completed)

    def completed(self, operation, token, result):
        if token != self.generation or not self.active: return
        if not isinstance(result, tuple):
            self.message.setText(f'Kalender konnte nicht geladen werden: {getattr(result,"message",result)}'); return
        choices, self.projection = result
        selected = self.semester.currentData()
        self.semester.blockSignals(True); self.semester.clear()
        for s in choices: self.semester.addItem(s.name, s.stable_id)
        self.semester.setCurrentIndex(self.semester.findData(selected)); self.semester.blockSignals(False)
        course = self.course.currentData(); self.course.blockSignals(True); self.course.clear(); self.course.addItem('Alle Kurse', None)
        for cid, name in self.projection.courses: self.course.addItem(name, cid)
        self.course.setCurrentIndex(max(0,self.course.findData(course))); self.course.blockSignals(False)
        self.month.blockSignals(True); self.month.setSelectedDate(QDate(self.anchor.year,self.anchor.month,self.anchor.day)); self.month.blockSignals(False)
        self.render()

    def select_day(self):
        selected = self.month.selectedDate().toPython()
        if (selected.year, selected.month) != (self.anchor.year,self.anchor.month):
            self.anchor=selected; self.reload()
        else: self.anchor=selected; self.render()

    def matches_kind(self, event):
        kind = self.kind.currentIndex()
        return (kind == 0 or
            (kind == 1 and event.event_type != 'STUDY_BLOCK_CONFIRMED' and event.event_type != 'DEADLINE' and not event.event_type.startswith('MATERIAL_')) or
            (kind == 2 and event.event_type == 'DEADLINE') or
            (kind == 3 and event.event_type.startswith('MATERIAL_')) or
            (kind == 4 and event.event_type == 'STUDY_BLOCK_CONFIRMED'))

    def render(self):
        self.agenda.clear(); self.details.clear(); self.displayed=[]
        self.agenda.setMinimumHeight(400 if self.kind.currentIndex()==2 else 280)
        if self.projection is None: return
        from PySide6.QtGui import QTextCharFormat, QColor, QIcon
        filtered = [e for e in self.projection.events if self.matches_kind(e)]
        self.month.setDateTextFormat(QDate(), QTextCharFormat())
        for day in {e.day for e in filtered}:
            fmt = QTextCharFormat(); fmt.setBackground(QColor('#356078'))
            fmt.setToolTip('\n'.join(e.title for e in filtered if e.day == day))
            self.month.setDateTextFormat(QDate(day.year, day.month, day.day), fmt)
        last_day = None
        for e in filtered:
            if self.mode.currentIndex()==1 and e.day != self.anchor: continue
            self.displayed.append(e)
            day_heading = f'{_DAY_NAMES[e.day.weekday()]} · {e.day:%d.%m.%Y}\n' if e.day != last_day else ''
            last_day = e.day
            courses = getattr(getattr(self.controller, 'home', None), 'courses', ())
            course = next((c for c in courses if c.course_id == e.course_id), None)
            identity = academic_course_visual(course.course_stable_id if course else e.course_id, e.course_name)
            name = course_caption(course.course_stable_id if course else e.course_id, e.course_name)
            icon = calendar_material_icon(identity,e) if e.event_type=='MATERIAL_EXPECTED' else badge_icon(identity)
            kind_text = event_kind(e)
            if e.event_type=='MATERIAL_EXPECTED': kind_text = calendar_material_kind(e.material_kind)[0] + ' · ' + kind_text
            item = QListWidgetItem(icon,
                day_heading + f'{event_clock(e, self.projection.timezone)} · {name}\n'
                + (kind_text if e.title.upper() in _ACTIVITY_NAMES or e.event_type=='MATERIAL_EXPECTED' else f'{event_kind(e)} · {e.title}') + (f' · {e.location}' if e.location else ''))
            item.setForeground(QColor(ACCENT_BY_FAMILY[identity.accent_family]))
            item.setToolTip(name + ' · ' + kind_text)
            self.agenda.addItem(item)
            if e.event_type=='DEADLINE':
                item.setIcon(QIcon())
                summary=DeadlineSummary(e,identity,name,self.projection.timezone)
                item.setSizeHint(QSize(0,112))
                item.setData(Qt.ItemDataRole.AccessibleTextRole,summary.accessibleName())
                item.setToolTip(summary.accessibleName())
                self.agenda.setItemWidget(item,summary)
        self.message.setText(f'{len(self.displayed)} Ereignisse' + (' · Keine Ereignisse im gewählten Zeitraum.' if not self.displayed else '')
            + (f' · {len(self.projection.diagnostics)} technische Hinweise in den Details.' if self.projection.diagnostics else ''))

    def show_details(self, index):
        self.technical.setText('Technische Details ausblenden' if self.technical.isChecked() else 'Technische Details anzeigen')
        if index<0 or index>=len(getattr(self,'displayed',[])):
            self.details.setText('\n'.join(self.projection.diagnostics)
                if self.technical.isChecked() and self.projection else '')
            return
        e=self.displayed[index]
        if not self.technical.isChecked():
            self.details.setText(f'{e.course_name} · {event_kind(e)}\n{e.title}\n'
                f'{e.day:%d.%m.%Y} · {event_clock(e, self.projection.timezone)}\n'
                f'Ort: {e.location or "nicht angegeben"}\nInformationsstand: {information_status(e.information_status)}\n'
                'Quelle: über „Technische Details anzeigen“ erreichbar.')
            return
        self.details.setText(f'{e.title}\nTyp: {e.event_type} · Status: {e.status} · Informationsstand: {e.information_status}\n'
            f'{e.day:%d.%m.%Y} · Beginn: {e.starts_at.isoformat() if e.starts_at else "unbekannt"} · Ende: {e.ends_at.isoformat() if e.ends_at else "unbekannt"}\n'
            f'{e.course_name} ({e.course_id}) · {self.projection.semester_name} ({e.semester_id})\n'
            f'Materialtyp: {e.material_kind or "unbekannt"} · Materialdatensätze: {", ".join(e.material_ids) or "keine"}\n'
            f'Datumsbasis: {e.date_basis or "Termin/Frist"} · Artefakt: {e.artifact_id or "keines"}\n'
            f'Ort: {e.location or "nicht angegeben"}\nQuelle: {e.source_table}/{e.source_id}\nAusnahme: {e.exception_id or "keine"}\n{e.notes or ""}\n' + "\n".join(self.projection.diagnostics))

    def open_study_changes(self):
        if self.projection is None or self.projection.semester_id != self.semester.currentData(): return
        from uniflow.gui.study_block_changes import StudyBlockChangeDialog
        index=self.agenda.currentRow()
        event=self.displayed[index] if 0<=index<len(getattr(self,'displayed',[])) else None
        block_id=event.source_id if event and event.event_type=='STUDY_BLOCK_CONFIRMED' else None
        dialog=StudyBlockChangeDialog(self.controller,self.projection.semester_id,self.projection.timezone,self,block_id)
        dialog.setModal(False);dialog.show()
        self.study_change_dialog=dialog

    def open_study_completion(self):
        if self.projection is None or self.projection.semester_id != self.semester.currentData(): return
        from uniflow.gui.study_block_completion import StudyBlockCompletionDialog
        index=self.agenda.currentRow()
        event=self.displayed[index] if 0<=index<len(getattr(self,'displayed',[])) else None
        block_id=event.source_id if event and event.event_type=='STUDY_BLOCK_CONFIRMED' else None
        dialog=StudyBlockCompletionDialog(self.controller,self.projection.semester_id,self.projection.timezone,self,block_id)
        dialog.setModal(False);dialog.show()
        self.study_completion_dialog=dialog
