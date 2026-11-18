from uniflow.gui.screen_chrome import clean_event_button, compact_deadline_button
from uniflow.gui.safe_presentation import material_expectation
"""Compact dashboard sections; all database reads run through TaskRunner."""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from uniflow.gui.components import BaseCard, WrapButton, DeadlineButton, AcademicEventButton
from uniflow.gui.deadline_presentation import display_title, due_day, is_past, timing_label
from uniflow.gui.visual_identity import resolve_course_visual
from uniflow.gui.academic_presentation import academic_course_visual, local_event_sort_key
from uniflow.gui.presentation import information_status
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from uniflow.gui.presentation import PresentationError
from uniflow.gui.routes import Route
from uniflow.gui.deadline_event_presentation import deadline_event_button
from uniflow.gui.dashboard_materials import (material_kind, forecast_status, compact_clock, time_explanation,
    course_caption, provenance_explanation, material_rule_id, material_sort_key, MATERIAL_ICON_COLOR)


class DashboardSummary(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.active = False
        self.projection = None
        self.show_past_deadlines = False
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.body.addWidget(self.message)
        self.retry = QPushButton('Studienübersicht neu laden')
        self.retry.clicked.connect(self.reload)
        self.body.addWidget(self.retry)
        self.cards = QWidget()
        self.body.addWidget(self.cards)
        controller.home_loading.connect(self.loading)
        controller.dashboard_changed.connect(self.render)

    def activate(self, active):
        if active == self.active:
            return
        self.active = active

    def reload(self):
        if self.active:
            self.controller.refresh_home()

    def loading(self):
        if self.active:
            self.projection = None
            self.cards.hide()
            self.message.setText('Studienübersicht wird geladen …')

    def render(self, result):
        if not self.active or self.controller.state.route != Route.HOME:
            return
        if isinstance(result, PresentationError):
            self.cards.hide()
            self.message.setText(f'Studienübersicht konnte nicht geladen werden: {result.code} · {result.message}')
            return
        self.projection = result
        self.message.setText(f'{result.start:%d.%m.%Y} – {result.end:%d.%m.%Y} · {result.timezone}\n'
            + (f'{len(result.diagnostics)} Hinweise · Details per Mauszeiger.' if result.diagnostics else ''))
        self.message.setToolTip('\n'.join(result.diagnostics))
        self.body.removeWidget(self.cards)
        self.cards.setParent(None)
        self.cards.deleteLater()
        self.cards = QWidget()
        layout = QVBoxLayout(self.cards)
        layout.setContentsMargins(0, 0, 0, 0)
        groups = [('Nächste Veranstaltungen', result.classes, 'calendar'),
            ('Bevorstehende Fristen', result.deadlines, 'deadline'),
            ('Erwartete Materialien', result.expected_materials, 'expectation'),
            ('Kürzlich erfasste Materialien', result.recent_materials, 'document')]
        for title, events, icon in groups:
            if title == 'Erwartete Materialien': events = sorted(events, key=material_sort_key)
            card = BaseCard(title, variant='secondary', icon_kind=icon)
            if title == 'Bevorstehende Fristen':
                self.add_deadlines(card, result)
                layout.addWidget(card)
                continue
            if title == 'Kürzlich erfasste Materialien':
                self.add_text(card, 'Letzte sieben Kalendertage · Erfasst/importiert, kein Veröffentlichungsdatum.')
            if not events:
                self.add_text(card, 'Keine Einträge im Zeitraum.')
            for event in events[:4]:
                self.add_event(card, event)
            if len(events) > 4:
                self.add_text(card, f'{len(events) - 4} weitere Einträge im Semesterkalender.')
            layout.addWidget(card)
        preview = BaseCard('Vorschau · nächste sieben Kalendertage', variant='secondary', icon_kind='calendar')
        from datetime import timedelta
        for offset in range(7):
            day = result.start + timedelta(days=offset)
            self.add_text(preview, f'{day:%d.%m.%Y}')
            events = sorted((e for e in result.preview if e.day == day),
                key=lambda e: local_event_sort_key(e, result.timezone))
            if not events:
                self.add_text(preview, 'Keine Einträge.')
            for event in events[:5]:
                self.add_event(preview, event)
            if len(events) > 5:
                self.add_text(preview, f'{len(events)-5} weitere Einträge im Semesterkalender.')
        layout.addWidget(preview)
        open_calendar = QPushButton('Semesterkalender öffnen')
        open_calendar.clicked.connect(lambda: self.controller.navigate(Route.CALENDAR))
        layout.addWidget(open_calendar)
        self.body.addWidget(self.cards)
        self.cards.show()

    def toggle_past(self):
        self.show_past_deadlines = not self.show_past_deadlines
        self.render(self.projection)

    def add_deadlines(self, card, result):
        home = self.controller.home
        zone = ZoneInfo(result.timezone)
        summary = home.deadlines
        items = {item.deadline_id: item for group in (summary.authoritative_upcoming,
            summary.authoritative_overdue, summary.due_fact_diagnostics) for item in group}
        past = sorted((item for item in items.values() if is_past(item, result.as_of, zone)),
            key=lambda item: (due_day(item, zone), item.deadline_id), reverse=True)
        if not result.deadlines:
            self.add_text(card, "Keine bevorstehenden Fristen im Zeitraum.")
        if past:
            toggle = QPushButton("Vergangene Fristen ausblenden" if self.show_past_deadlines else "Vergangene Fristen anzeigen")
            toggle.clicked.connect(self.toggle_past)
            card.body.addWidget(toggle)
        # Calendar projection is untruncated; Home supplies presentation metadata where available.
        for event in result.deadlines[:4]:
            item = items.get(event.source_id)
            if item is None:
                item = SimpleNamespace(title=event.title, course_id=event.course_id,
                    due_date=event.day if event.starts_at is None else None, due_at=event.starts_at,
                    status=event.status, information_status=event.information_status)
            self.add_deadline(card, item, home, result)
        if len(result.deadlines) > 4:
            self.add_text(card, f"{len(result.deadlines)-4} weitere Fristen im Semesterkalender.")
        if self.show_past_deadlines:
            for item in past:
                self.add_deadline(card, item, home, result)

    def add_deadline(self, card, item, home, result):
        zone = ZoneInfo(result.timezone)
        day = due_day(item, zone)
        due = (f"{item.due_date:%d.%m.%Y} · Uhrzeit unbekannt" if item.due_date else
            item.due_at.astimezone(zone).strftime("%d.%m.%Y · %H:%M") if item.due_at else "Fälligkeit unbekannt")
        course = next(c for c in home.courses if c.course_id == item.course_id)
        button = DeadlineButton(display_title(item.title, day),
            identity=resolve_course_visual(course.course_stable_id, course.name), course_name=course.name,
            due_text=due, status=item.status, information=information_status(item.information_status),
            timing=timing_label(item, result.as_of, zone))
        compact_deadline_button(button)
        from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY
        identity = resolve_course_visual(course.course_stable_id, course.name)
        button.caption.setStyleSheet(f'color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 700;')
        for label in button.findChildren(QLabel):
            if label.text() == 'Bearbeitung: UNKNOWN': label.setText('Abgabestatus unbekannt')
            if label.text().startswith('Informationsstatus:') and item.information_status == 'INFERRED':
                label.setText('Planungsannahme: abgeleitete Frist')
        button.setAccessibleName(button.text())
        button.clicked.connect(lambda: self.controller.navigate(Route.CALENDAR))
        card.body.addWidget(button)

    @staticmethod
    def add_text(card, text):
        widget = QLabel(text)
        widget.setWordWrap(True)
        card.body.addWidget(widget)

    def add_material_event(self, card, event):
        from PySide6.QtCore import Qt
        from uniflow.gui.components import VisualBadge
        from uniflow.gui.components.nav_icons import content_icon
        from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY
        courses = getattr(getattr(self.controller, 'home', None), 'courses', ())
        course = next((c for c in courses if c.course_id == event.course_id), None)
        identity = academic_course_visual(course.course_stable_id if course else event.course_id, event.course_name)
        observed = event.event_type == 'MATERIAL_OBSERVED'
        caption, icon, color, certainty = forecast_status(event.material_kind, event.information_status,
            linked=event.status == 'CONFIRMED_PRESENT', ambiguous=event.status == 'AMBIGUOUS', recorded=observed)
        kind_label, kind_icon = material_kind(event.material_kind)
        if observed:
            certainty = 'Material erfasst'
            import re
            filename = re.search(r'Datei: (.*?); Herkunft:', event.notes or '')
            kind_label = filename.group(1) if filename else event.title
            if event.title != kind_label: kind_label += ' · ' + event.title
        clock = (event.starts_at.astimezone(ZoneInfo(self.projection.timezone)).strftime('%H:%M') + ' · erfasst'
            if observed and event.starts_at else compact_clock(event))
        stable_id = course.course_stable_id if course else event.course_id
        name = course_caption(stable_id, event.course_name)
        button = WrapButton(f'{name} · {kind_label} · {caption} · {event.day:%d.%m.%Y} · {clock}')
        button.setAccessibleName(f'{name} · {kind_label} · {caption} · {event.day:%d.%m.%Y} · {clock}')
        button.caption.setText(name)
        button.caption.setStyleSheet(f'color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 700;')
        layout = button.layout(); layout.removeWidget(button.caption)
        heading = QHBoxLayout(); badge = VisualBadge(identity, compact=True)
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        heading.addWidget(badge); heading.addWidget(button.caption, 1); layout.addLayout(heading)
        for text, symbol_kind, tone in [(kind_label, kind_icon, ACCENT_BY_FAMILY[identity.accent_family] if observed else MATERIAL_ICON_COLOR), ('' if observed else caption, 'slides_upload' if observed else icon, color)]:
            row = QHBoxLayout(); symbol = QLabel(); symbol.setPixmap(content_icon(symbol_kind,color=tone).pixmap(20,20))
            symbol.setAccessibleName('Material erfasst' if observed and symbol_kind == 'slides_upload' else text)
            if observed and symbol_kind == 'slides_upload': symbol.setToolTip('Material erfasst')
            row.addWidget(symbol); label = QLabel(text); label.setWordWrap(True)
            label.setStyleSheet(f'color: {tone};' + (' font-weight: 700; font-size: 15px;' if observed and text else '')); row.addWidget(label,1); layout.addLayout(row)
        when = QLabel(f'{event.day:%d.%m.%Y} · {clock}'); when.setWordWrap(True); layout.addWidget(when)
        for child in button.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        button.setStyleSheet('QPushButton { background: #172441; text-align: left; border: 1px solid #294566; border-radius: 12px; } '
            'QPushButton:hover { background: #20324c; } QLabel { border: none; background: transparent; }')
        button.setToolTip((certainty + '\n' + ('Material erfasst · lokaler Erfassungszeitpunkt; kein Veröffentlichungsdatum.' if observed else time_explanation(event)) + '\n' + provenance_explanation(stable_id,event.course_name,rule_id=material_rule_id(event) if not observed else '')).strip())
        button.clicked.connect(lambda: self.controller.navigate(Route.CALENDAR)); card.body.addWidget(button)

    def add_event(self, card, event):
        if event.event_type == 'DEADLINE':
            courses = getattr(getattr(self.controller, 'home', None), 'courses', ())
            course = next((c for c in courses if c.course_id == event.course_id), None)
            stable = course.course_stable_id if course else event.course_id
            identity = academic_course_visual(stable, event.course_name)
            button = deadline_event_button(event, identity, course_caption(stable, event.course_name), self.projection.timezone)
            button.clicked.connect(lambda: self.controller.navigate(Route.CALENDAR))
            card.body.addWidget(button)
            return
        if event.event_type in {'MATERIAL_EXPECTED', 'MATERIAL_OBSERVED'}:
            self.add_material_event(card, event)
            return
        kind, color = ('Veranstaltung', '#55c4ee')
        if event.event_type == 'DEADLINE':
            kind, color = ('Frist', '#efb45d')
        elif event.event_type == 'CANCELLATION':
            kind, color = ('Ausfallhinweis', '#f38191')
        elif event.event_type == 'MATERIAL_EXPECTED':
            kind, _, color, _ = material_expectation(event.material_kind, event.information_status, linked=event.status=='CONFIRMED_PRESENT')
            if event.status=='AMBIGUOUS': kind='Materialzuordnung prüfen'
        elif event.event_type == 'MATERIAL_OBSERVED':
            kind, color = ('Material erfasst/importiert', '#73d6ae')
        known_time = event.starts_at or event.expected_time
        clock = known_time.strftime('%H:%M') if known_time else 'Uhrzeit unbekannt'
        title = '' if event.title.upper() in AcademicEventButton._TYPES or event.event_type=='MATERIAL_EXPECTED' else event.title
        courses = getattr(getattr(self.controller, 'home', None), 'courses', ())
        course = next((c for c in courses if c.course_id == event.course_id), None)
        name = course_caption(course.course_stable_id if course else event.course_id, event.course_name)
        text = f'{name} · {clock}\n{event.day:%d.%m.%Y}' + (f' · {title}' if title else '')
        if kind != 'Veranstaltung':
            text += f' · {kind}'
        if kind == 'Veranstaltung':
            courses = getattr(getattr(self.controller, 'home', None), 'courses', ())
            course = next((c for c in courses if c.course_id==event.course_id), None)
            identity = academic_course_visual(course.course_stable_id if course else event.course_id,event.course_name)
            button = AcademicEventButton(text, accent_family=identity.accent_family,
                activity_type=event.title if event.title in AcademicEventButton._TYPES else None, location=event.location)
            clean_event_button(button, identity)
        else:
            button = WrapButton(text)
        if kind != 'Veranstaltung':
            button.setStyleSheet(f'QPushButton {{ text-align: left; border: 1px solid {color}; border-radius: 12px; }} QLabel {{ border: none; }}')
        button.setToolTip(event.notes or '')
        button.clicked.connect(lambda: self.controller.navigate(Route.CALENDAR))
        card.body.addWidget(button)
