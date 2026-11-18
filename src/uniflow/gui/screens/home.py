from uniflow.gui.safe_presentation import material_expectation
from uniflow.gui.screen_chrome import clean_event_button, compact_deadline_button
"""Dashboard composition over the detached HomeDashboardV1 snapshot."""
from zoneinfo import ZoneInfo
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import (QBoxLayout, QGridLayout, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from uniflow.gui.components import (AcademicEventButton, BaseCard, DeadlineButton,
    EmptyState, SectionHeader, StatusChip, VisualBadge, WrapButton)
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.deadline_presentation import display_title, due_day, is_past, timing_label
from uniflow.gui.assets.loader import scaled_pixmap
from uniflow.gui.presentation import format_date, format_time, information_status, mastery, label
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY
from uniflow.gui.theme import tokens as t
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual
from uniflow.gui.routes import Route


def _text(value, *, muted=False, wrap=True):
    result = QLabel(value)
    result.setWordWrap(wrap)
    if muted:
        result.setObjectName("muted")
    return result


class ImageHeroCard(BaseCard):
    """The production image fills the hero behind a readable content gradient."""

    def __init__(self, accent_family):
        super().__init__(hero=True, accent_family=accent_family)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(area, 20, 20)
        p.setClipPath(clip)
        dpr = self.devicePixelRatioF()
        image = scaled_pixmap("hero/next_best_action_study_sunset.png",
            max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        if not image.isNull():
            logical_w, logical_h = image.width() / dpr, image.height() / dpr
            p.drawPixmap(QRectF((self.width()-logical_w)/2, (self.height()-logical_h)/2,
                logical_w, logical_h), image, QRectF(image.rect()))
        fade = QLinearGradient(0, 0, self.width(), 0)
        fade.setColorAt(0, QColor(t.APP_BACKGROUND))
        fade.setColorAt(.26, QColor(8, 13, 33, 246))
        fade.setColorAt(.56, QColor(8, 13, 33, 170))
        fade.setColorAt(.82, QColor(8, 13, 33, 12))
        fade.setColorAt(1, QColor(8, 13, 33, 0))
        p.fillRect(area, fade)
        p.end()


class CourseCard(BaseCard):
    def __init__(self, course, child_names=()):
        identity = resolve_course_visual(course.course_stable_id, course.name)
        super().__init__(variant="course",
            accent_family=identity.accent_family)
        self.identity = identity
        self.setMinimumHeight(218)
        self.body.setContentsMargins(0, 0, 0, 14)
        self.body.setSpacing(7)
        accent = ACCENT_BY_FAMILY[identity.accent_family]
        art = PackagedArtwork(course_artwork_asset(identity),
            fallback_family=identity.artwork_family)
        art.setFixedHeight(108)
        self.body.addWidget(art)
        header = QHBoxLayout()
        header.setContentsMargins(16, 0, 16, 0)
        header.setSpacing(10)
        header.addWidget(VisualBadge(identity, compact=True))
        names = QVBoxLayout()
        names.setSpacing(4)
        title = _text(course.name)
        title.setObjectName("heading")
        names.addWidget(title)
        names.addWidget(_text(course.course_stable_id, muted=True))
        header.addLayout(names, 1)
        self.body.addLayout(header)
        if child_names:
            parts = _text("Teilkurse: " + " · ".join(child_names), muted=True)
            parts.setContentsMargins(16, 0, 16, 0)
            self.body.addWidget(parts)
        self.body.addStretch()
        coverage = course.mastery_coverage
        if coverage.mean_known_mastery is None:
            row = QHBoxLayout()
            row.setContentsMargins(16, 0, 16, 0)
            row.addWidget(StatusChip("UNKNOWN mastery"))
            row.addStretch()
            self.body.addLayout(row)
        else:
            value = _text(mastery(coverage.mean_known_mastery))
            value.setContentsMargins(16, 0, 16, 0)
            self.body.addWidget(value)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(round(coverage.mean_known_mastery * 100))
            progress.setTextVisible(False)
            progress.setStyleSheet(f"QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}")
            progress_row = QHBoxLayout()
            progress_row.setContentsMargins(16, 0, 16, 0)
            progress_row.addWidget(progress)
            self.body.addLayout(progress_row)
        foot = _text(f"{coverage.known_mastery_count}/{coverage.active_skill_count} skills known · {course.upcoming_authoritative_deadline_count} upcoming deadlines", muted=True)
        foot.setContentsMargins(16, 0, 16, 0)
        self.body.addWidget(foot)


class HomeScreen(QWidget):
    def __init__(self, controller=None):
        super().__init__()
        self.controller = controller
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(0)
        self.content_widget = EmptyState("Dashboard", "Loading your Semester…")
        self.layout_.addWidget(self.content_widget)
        self.course_cards = []
        self.course_grid = None
        self.top_row = None
        self.show_past_deadlines = False

    def render(self, dto):
        self.layout_.removeWidget(self.content_widget)
        self.content_widget.setParent(None)
        self.content_widget.deleteLater()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.content_widget = page
        self.layout_.addWidget(page)

        header = _text(f"Heute · {format_date(dto.local_date)}")
        header.setObjectName("heroTitle")
        layout.addWidget(header)
        if dto.next_planner_recommendation:
            layout.addWidget(_text("Nächste Lernaktion: " +
                label(dto.next_planner_recommendation.action_type), muted=True))
        self.top_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.top_row.setSpacing(16)

        layout.addLayout(self.top_row)
        # The persistent summary is mounted by MainWindow above this home content.
        layout.addWidget(self._plan(dto))
        insight_row = QHBoxLayout()
        insight_row.setSpacing(16)
        insight_row.addWidget(self._progress(dto), 2)
        layout.addLayout(insight_row)
        layout.addWidget(self._next_actions(dto))
        layout.addWidget(self._setup(dto))
        heading = SectionHeader("Your courses")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.course_grid = QGridLayout()
        self.course_grid.setSpacing(14)
        children = {}
        for course in dto.courses:
            if course.parent_course_id:
                children.setdefault(course.parent_course_id, []).append(course.name)
        self.course_cards = [CourseCard(course, children.get(course.course_id, []))
            for course in dto.courses if course.parent_course_id is None]
        if not self.course_cards:
            self.course_cards = [EmptyState("No Courses", "No Courses are recorded for this Semester.")]
        layout.addLayout(self.course_grid)
        layout.addStretch()
        self._reflow()

    def _hero(self, dto):
        recommendation = dto.next_planner_recommendation
        course = next((item for item in dto.courses if recommendation and item.course_stable_id == recommendation.course_stable_id), None)
        identity = resolve_course_visual(recommendation.course_stable_id,
            course.name if course else recommendation.course_stable_id) if recommendation else None
        hero = ImageHeroCard(identity.accent_family if identity else "CYAN")
        hero.setMinimumHeight(300)
        hero.body.setContentsMargins(0, 0, 0, 0)
        hero.body.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(0)
        copy = QVBoxLayout()
        copy.setContentsMargins(26, 22, 18, 20)
        copy.setSpacing(10)
        eyebrow = _text("NEXT BEST ACTION")
        eyebrow.setObjectName("eyebrow")
        copy.addWidget(eyebrow)
        if recommendation:
            title = _text(course.name if course else recommendation.course_stable_id)
            title.setObjectName("heroTitle")
            copy.addWidget(title)
            copy.addWidget(_text(label(recommendation.action_type), muted=True))
            copy.addStretch()
            copy.addWidget(VisualBadge(identity, compact=True), 0, Qt.AlignmentFlag.AlignLeft)
            copy.addWidget(_text(f"{len(recommendation.target_skill_ids)} target skill(s) · Rank {recommendation.rank}", muted=True))
            if recommendation.reasons:
                copy.addWidget(_text("Factors: " + ", ".join(label(f.code) for f in recommendation.reasons[:3]), muted=True))
        else:
            title = _text("Plan your next step")
            title.setObjectName("heroTitle")
            copy.addWidget(title)
            copy.addWidget(_text("No eligible recommendation is available. Review setup and planner diagnostics below.", muted=True))
            copy.addStretch()
        row.addLayout(copy, 7)
        row.addStretch(2)
        hero.body.addLayout(row, 1)
        return hero

    def _plan(self, dto):
        card = BaseCard("Current week plan", variant="secondary", icon_kind="plan")
        plan = dto.plan
        if plan.plan_id is None:
            card.body.addWidget(_text("No plan exists for this week."))
        elif plan.accepted_revision_id is None:
            card.body.addWidget(_text(f"Draft revision {plan.current_revision_number}; no accepted plan yet."))
        else:
            card.body.addWidget(_text(f"Accepted revision {plan.accepted_revision_number}" +
                (" is current." if plan.accepted_is_current else f"; draft revision {plan.current_revision_number} is newer.")))
        if dto.next_accepted_block:
            card.body.addWidget(_text("Next block · " + format_time(dto.next_accepted_block.starts_at,
                dto.semester.timezone), muted=True))
        if self.controller:
            button = QPushButton("Woche öffnen")
            button.clicked.connect(lambda: self.controller.open_week_day(dto.local_date))
            card.body.addWidget(button)
        return card

    def _weekly_drops(self):
        card = BaseCard("Materialerwartungen dieser Woche", variant="secondary", icon_kind="expectation")
        snapshot = self.controller.weekly_drops if self.controller else None
        if snapshot is None:
            card.body.addWidget(_text("Veröffentlichungen derzeit nicht verfügbar.", muted=True))
            return card
        card.body.addWidget(_text(f"Woche ab {format_date(snapshot.week_start)} · {snapshot.timezone}", muted=True))
        if not snapshot.items:
            card.body.addWidget(_text("Für diese Woche sind keine Materialveröffentlichungen erfasst.", muted=True))
        for drop in snapshot.items:
            identity = resolve_course_visual(drop.course_stable_id, drop.course_name)
            when = (format_date(drop.release_date) + (f" · {drop.release_time.strftime('%H:%M')}"
                if drop.release_time else " · Uhrzeit unbekannt")) if drop.release_date else "Release-Tag unbekannt"
            source = {"CONFIRMED": "Erwartet · bestätigte Regel", "OBSERVED": "Erwartet · beobachtete Regel",
                "INFERRED": "Prognose · abgeleitete Regel", "UNKNOWN": "Regel ungeklärt"}.get(
                    drop.information_status, drop.information_status)
            if drop.release_state == "NO_RULE":
                source = "Keine Release-Regel erfasst"
            elif drop.release_state == "UNRESOLVED_RULE":
                source = "Release-Regel ungeklärt"
            imported = {"IMPORTED": "Explizit verknüpft · importiert",
                "LINKED_NO_IMPORT": "Expliziter Link · Datei nicht importiert",
                "LINK_STALE": "Link zur aktuellen Regel ungeklärt",
                "AMBIGUOUS_LINK": "Mehrere verknüpfte Materialien",
                "NO_EXPLICIT_LINK": "Kein expliziter Material-Link"}[drop.link_state]
            caption, icon, color, certainty = material_expectation(drop.material_kind,drop.information_status,linked=drop.link_state=='IMPORTED')
            if drop.release_state in {'NO_RULE','UNRESOLVED_RULE'}:
                caption, certainty = 'Materialrhythmus offen', 'Noch keine verlässliche Erwartung'
            if drop.link_state in {'LINK_STALE','AMBIGUOUS_LINK'}: certainty='Materialzuordnung prüfen'
            button = WrapButton(f'{drop.course_name} · {caption}\n{when} · {certainty}')
            button.setToolTip(source + ' · ' + imported)
            button.setStyleSheet(f'QPushButton {{ text-align: left; border: 1px solid {color}; border-radius: 12px; }} QLabel {{ border: none; }}')
            button.clicked.connect(lambda checked=False, item=drop: self.controller.open_weekly_drop(item))
            card.body.addWidget(button)
        return card

    def _events(self, dto):
        card = BaseCard("Nächste Veranstaltungen", variant="secondary", icon_kind="calendar")
        if not dto.upcoming_academic_occurrences:
            card.body.addWidget(_text("Keine bevorstehenden Termine im aktuellen oder nächsten Wochenfenster.", muted=True))
        for event in dto.upcoming_academic_occurrences[:5]:
            title = AcademicEventButton._TYPES.get((event.title or event.activity_type or "").upper(), (event.title or label(event.activity_type or "Lehrveranstaltung"),))[0]
            when = format_time(event.starts_at, dto.semester.timezone) if event.starts_at else format_date(event.local_date)
            text = f"{when} · {event.course_name or event.course_stable_id} · {title}"
            if self.controller:
                identity = resolve_course_visual(event.course_stable_id,
                    event.course_name or event.course_stable_id)
                button = AcademicEventButton(text, accent_family=identity.accent_family,
                    activity_type=event.activity_type, location=event.location)
                clean_event_button(button, identity)
                button.clicked.connect(lambda checked=False, day=event.local_date:
                    self.controller.open_week_day(day))
                card.body.addWidget(button)
            else:
                card.body.addWidget(_text(text, muted=True))
        return card

    def _today(self, dto):
        card = BaseCard("Veranstaltungen heute", variant="secondary", icon_kind="today")
        if not dto.today_academic_occurrences:
            card.body.addWidget(_text("Heute sind keine Veranstaltungen erfasst.", muted=True))
        for event in dto.today_academic_occurrences:
            title = AcademicEventButton._TYPES.get((event.title or event.activity_type or "").upper(), (event.title or label(event.activity_type or "Lehrveranstaltung"),))[0]
            when = format_time(event.starts_at, dto.semester.timezone) if event.starts_at else "Uhrzeit unbekannt"
            value = f"{when} · {event.course_name or event.course_stable_id} · {title}"
            if self.controller:
                identity = resolve_course_visual(event.course_stable_id,
                    event.course_name or event.course_stable_id)
                button = AcademicEventButton(value, accent_family=identity.accent_family,
                    activity_type=event.activity_type, location=event.location)
                clean_event_button(button, identity)
                button.clicked.connect(lambda checked=False, day=event.local_date:
                    self.controller.open_week_day(day))
                card.body.addWidget(button)
            else:
                card.body.addWidget(_text(value))
        if self.controller:
            button = QPushButton("Heute im Kalender öffnen")
            button.clicked.connect(lambda: self.controller.open_week_day(dto.local_date))
            card.body.addWidget(button)
        return card

    def _deadlines(self, dto):
        card = BaseCard("To-dos & Deadlines", variant="secondary", icon_kind="deadline")
        count = dto.deadlines
        courses = {course.course_id: course.name for course in dto.courses}
        items = [*count.authoritative_upcoming, *count.authoritative_overdue,
            *count.due_fact_diagnostics]
        zone = ZoneInfo(dto.semester.timezone)
        def due_key(item):
            return (item.due_date or (item.due_at.astimezone(zone).date() if item.due_at else dto.local_date),
                item.due_at.isoformat() if item.due_at else "", item.deadline_id)
        current = sorted((item for item in items if not is_past(item, dto.as_of, zone)), key=due_key)
        past = sorted((item for item in items if is_past(item, dto.as_of, zone)), key=due_key, reverse=True)
        if not items:
            card.body.addWidget(_text("Keine erfassten Fristen.", muted=True))
        elif not current:
            card.body.addWidget(_text("Keine aktuellen oder künftigen Fristen erfasst.", muted=True))
        if past:
            toggle = QPushButton("Vergangene Fristen ausblenden" if self.show_past_deadlines
                else "Vergangene Fristen anzeigen")
            toggle.clicked.connect(lambda: self._toggle_past(dto))
            card.body.addWidget(toggle)
        for item in [*current, *(past if self.show_past_deadlines else [])]:
            due = (format_date(item.due_date) if item.due_date else
                f"{format_date(item.due_at.astimezone(zone).date())} · {format_time(item.due_at, dto.semester.timezone)}"
                if item.due_at else "Fälligkeit unbekannt")
            if item.due_date:
                due += " · Uhrzeit unbekannt"
            item_day = due_day(item, zone)
            timing = timing_label(item, dto.as_of, zone)
            course = next((course for course in dto.courses if course.course_id == item.course_id), None)
            course_name = courses.get(item.course_id, "Fach unbekannt")
            identity = resolve_course_visual(course.course_stable_id if course else "", course_name)
            if self.controller:
                button = DeadlineButton(display_title(item.title, item_day), identity=identity, course_name=course_name,
                    due_text=due, status=item.status,
                    information=information_status(item.information_status), timing=timing)
                compact_deadline_button(button)
                if item_day is not None:
                    button.clicked.connect(lambda checked=False, day=item_day:
                        self.controller.open_week_day(day))
                else:
                    button.clicked.connect(lambda checked=False, detail=button.text():
                        QMessageBox.information(self, "Fristdetails", detail))
                card.body.addWidget(button)
            else:
                card.body.addWidget(_text(f"{course_name} · {item.title} · {due} · "
                    f"Bearbeitung: {item.status} · {timing}", muted=True))
        return card

    def _toggle_past(self, dto):
        self.show_past_deadlines = not self.show_past_deadlines
        self.render(dto)

    def _next_actions(self, dto):
        card = BaseCard("Nächste Aktionen", variant="secondary", icon_kind="action")
        if self.controller:
            review = QPushButton(f"Offene Reviews: {dto.pending_review_count} · Materials öffnen")
            review.clicked.connect(lambda: self.controller.navigate(Route.MATERIALS))
            card.body.addWidget(review)
        if dto.sessions.resumable_and_processing:
            for session in dto.sessions.resumable_and_processing[:5]:
                course = next((c.name for c in dto.courses if c.course_id == session.course_id),
                    session.course_stable_id)
                text = f"Session · {course} · {session.goal} · {label(session.state)}"
                if self.controller:
                    button = WrapButton(text)
                    button.clicked.connect(lambda checked=False, sid=session.session_id:
                        self.controller.select_session(sid))
                    card.body.addWidget(button)
                else:
                    card.body.addWidget(_text(text, muted=True))
        elif dto.pending_review_count == 0:
            card.body.addWidget(_text("Keine laufenden Sessions oder offenen Reviews.", muted=True))
        return card

    def _setup(self, dto):
        status = dto.setup_status
        missing = bool(status.diagnostics)
        card = BaseCard(variant="diagnostic" if missing else "secondary")
        row = QHBoxLayout()
        row.addWidget(_text("Planning setup"))
        row.addSpacing(18)
        row.addWidget(_text(f"Profile: {'present' if status.planning_profile_present else 'missing'} · "
            f"Availability: {status.active_availability_window_count} · Reviewed skills: {status.active_course_skill_count}", muted=True), 1)
        card.body.addLayout(row)
        if missing:
            card.body.addWidget(_text(" · ".join(label(d.code) for d in status.diagnostics[:4]), muted=True))
        return card

    def _progress(self, dto):
        card = BaseCard("Study progress", variant="secondary", icon_kind="progress")
        card.body.setContentsMargins(18, 14, 18, 14)
        known = [course for course in dto.courses if course.mastery_coverage.mean_known_mastery is not None]
        if not known:
            card.body.addWidget(_text("No known mastery values yet.", muted=True))
        else:
            for course in known[:2]:
                coverage = course.mastery_coverage
                identity = resolve_course_visual(course.course_stable_id, course.name)
                row = QHBoxLayout()
                row.addWidget(_text(course.name, muted=True), 1)
                row.addWidget(_text(mastery(coverage.mean_known_mastery)))
                card.body.addLayout(row)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(round(coverage.mean_known_mastery * 100))
                bar.setTextVisible(False)
                bar.setStyleSheet(f"QProgressBar::chunk {{ background: {ACCENT_BY_FAMILY[identity.accent_family]}; border-radius: 5px; }}")
                card.body.addWidget(bar)
            if len(known) > 2:
                card.body.addWidget(_text(f"+{len(known) - 2} more with known mastery", muted=True))
        return card

    def _reflow(self):
        if self.course_grid is None:
            return
        columns = 4 if self.width() >= 1010 else 2 if self.width() >= 590 else 1
        while self.course_grid.count():
            self.course_grid.takeAt(0)
        for index, card in enumerate(self.course_cards):
            self.course_grid.addWidget(card, index // columns, index % columns)
        if self.top_row:
            self.top_row.setDirection(QBoxLayout.Direction.LeftToRight if self.width() >= 820
                else QBoxLayout.Direction.TopToBottom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()
