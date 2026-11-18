"""Week overview with separate operative and persisted draft revisions."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QBoxLayout, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QVBoxLayout, QWidget)

from uniflow.gui.components import (AcademicEventButton, BaseCard, EmptyState, DeadlineButton,
    PrimaryButton, VisualBadge, WrapButton)
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.deadline_presentation import display_title, due_day, timing_label
from uniflow.gui.components.planning_dialogs import (DurationDialog, GoalDialog,
    PlanningDialog, RegenerateDialog, RemoveDialog, TimeDialog, ProfileDialog,
    AvailabilityDialog)
from uniflow.gui.presentation import format_date, format_time, information_status, label
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual


def _text(value, *, muted=False):
    result = QLabel(value)
    result.setWordWrap(True)
    if muted:
        result.setObjectName("muted")
    return result


class DayTile(QFrame):
    clicked = Signal(object)

    def __init__(self, day, dto, *, selected=False):
        super().__init__()
        self.setObjectName("day")
        current = dto.as_of.date() == day
        self.setProperty("current", current)
        self.setProperty("selected", selected)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        body = QVBoxLayout(self)
        body.setContentsMargins(13, 10, 13, 10)
        body.setSpacing(4)
        title = _text(day.strftime("%a").upper())
        title.setObjectName("muted")
        body.addWidget(title)
        number = _text(day.strftime("%d"))
        number.setObjectName("heading")
        body.addWidget(number)
        academic = sum(item.local_date == day for item in dto.current_calendar.academic_occurrences)
        availability = sum(item.local_date == day for item in dto.current_calendar.availability_occurrences)
        accepted_blocks = dto.accepted_plan.blocks if dto.accepted_plan and dto.accepted_plan.blocks else []
        accepted = sum(block.starts_at.astimezone(ZoneInfo(dto.semester.timezone)).date() == day
            for block in accepted_blocks)
        parts = []
        if academic:
            parts.append(f"Class {academic}")
        if availability:
            parts.append(f"Free {availability}")
        if accepted:
            parts.append(f"Plan {accepted}")
        meta = _text(" · ".join(parts) if parts else "—", muted=True)
        meta.setObjectName("dayMeta")
        body.addWidget(meta)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.property("day"))
        super().mousePressEvent(event)


class PlanBlockCard(BaseCard):
    def __init__(self, block, timezone, *, accepted, on_action=None, enabled=True,
            course_names=None):
        identity = resolve_course_visual(block.course_stable_id, block.course_stable_id)
        super().__init__(variant="secondary", artwork_family=identity.artwork_family,
            accent_family=identity.accent_family)
        self.body.setContentsMargins(14, 11, 14, 11)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(VisualBadge(identity, compact=True))
        facts = QVBoxLayout()
        facts.setSpacing(3)
        facts.addWidget(_text(f"{format_time(block.starts_at, timezone)} · {block.duration_minutes} min"))
        facts.addWidget(_text(f"{(course_names or {}).get(block.course_stable_id, block.course_stable_id)} · "
            f"{label(block.action_type)}", muted=True))
        row.addLayout(facts, 1)
        art = PackagedArtwork(course_artwork_asset(identity), mode="thumbnail",
            fallback_family=identity.artwork_family)
        art.setFixedSize(90, 55)
        if on_action is None:
            row.addWidget(art)
        if on_action is not None:
            if accepted:
                text = "Create Session" if block.action_type == "START_SKILL_SESSION" else "Resume Session"
                if block.materialization and block.materialization.status == "MATERIALIZED":
                    text = "Session · " + label(block.learning_session_state or "existing")
                button = QPushButton(text)
                button.setEnabled(enabled)
                button.clicked.connect(lambda: on_action("materialize", block))
            else:
                button = QPushButton("⋯")
                button.setToolTip("Draft block actions")
                button.setAccessibleName("Draft block actions")
                button.setEnabled(enabled)
                menu = QMenu(button)
                for action, title in (("move", "Move"), ("duration", "Change duration"),
                                      ("remove", "Remove from draft")):
                    menu.addAction(title, lambda checked=False, a=action, b=block: on_action(a, b))
                button.setMenu(menu)
            row.addWidget(button)
        self.body.addLayout(row)


class RevisionPanel(BaseCard):
    def __init__(self, title, revision, timezone, *, accepted, on_action=None, enabled=True,
            course_names=None):
        super().__init__(variant="accepted" if accepted else "draft")
        eyebrow = _text("OPERATIVE PLAN" if accepted else "PERSISTED DRAFT", muted=True)
        self.body.addWidget(eyebrow)
        header = _text(title)
        header.setObjectName("sectionTitle")
        self.body.addWidget(header)
        self.body.addWidget(_text(f"Revision {revision.revision_number} · Assessment: {label(revision.revision_assessment.status)}", muted=True))
        if revision.blocks is None:
            self.body.addWidget(_text("Revision source is invalid; contents unavailable."))
        elif revision.blocks:
            for block in revision.blocks:
                self.body.addWidget(PlanBlockCard(block, timezone, accepted=accepted,
                    on_action=on_action, enabled=enabled, course_names=course_names))
        else:
            self.body.addWidget(_text("No scheduled blocks."))
        self.body.addStretch()


class WeekScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(14)
        nav = QHBoxLayout()
        self.previous = QPushButton("← Previous")
        self.next = QPushButton("Next →")
        self.week_label = QLabel("")
        self.week_label.setObjectName("heading")
        nav.addWidget(self.previous)
        nav.addWidget(self.week_label, 1)
        nav.addWidget(self.next)
        self.layout_.addLayout(nav)
        self.previous.clicked.connect(lambda: controller.select_week(controller.state.selected_week_start - timedelta(days=7)))
        self.next.clicked.connect(lambda: controller.select_week(controller.state.selected_week_start + timedelta(days=7)))
        self.content_widget = EmptyState("Week", "Loading the selected week…")
        self.layout_.addWidget(self.content_widget)
        actions = QHBoxLayout()
        self.create = PrimaryButton("Create plan")
        self.create.clicked.connect(controller.create_plan)
        actions.addWidget(self.create)
        self.profile = QPushButton("Planning Profile einrichten")
        self.profile.clicked.connect(self.open_profile)
        actions.addWidget(self.profile)
        self.availability = QPushButton("Lernzeitfenster hinzufügen")
        self.availability.clicked.connect(self.open_availability)
        actions.addWidget(self.availability)
        self.regenerate = QPushButton("Regenerate…")
        self.regenerate.clicked.connect(self.open_regenerate)
        actions.addWidget(self.regenerate)
        self.accept = PrimaryButton("Accept plan")
        self.accept.clicked.connect(self.open_accept)
        actions.addWidget(self.accept)
        actions.addStretch()
        self.layout_.addLayout(actions)
        self.layout_.addStretch()
        self.sync()

    def sync(self):
        self.week_label.setText(format_date(self.controller.state.selected_week_start))
        self.previous.setEnabled(self.controller.state.busy[Route.WEEK] != Busy.MUTATING)
        self.next.setEnabled(self.controller.state.busy[Route.WEEK] != Busy.MUTATING)
        mutating = self.controller.state.busy[Route.WEEK] != Busy.IDLE
        exists = self.controller.week is not None and self.controller.week.plan.plan_id is not None
        calendar = self.controller.week.current_calendar if self.controller.week else None
        has_profile = bool(calendar and calendar.planning_profile_id)
        has_availability = bool(calendar and calendar.availability_occurrences)
        invalid_plan = bool(self.controller.week and any(d.code == "WEEKLY_PLAN_SOURCE_INVALID"
            for d in self.controller.week.diagnostics))
        self.create.setText("Creating plan…" if mutating else "Plan already exists" if exists else "Create plan")
        self.create.setEnabled(not mutating and not exists and not invalid_plan and has_profile and has_availability)
        self.create.setToolTip("Gespeicherter Plan ist nicht verifizierbar; Planaktionen bleiben gesperrt." if
            invalid_plan else "Zuerst Planning Profile und Lernzeitfenster einrichten." if
            not (has_profile and has_availability) else "Entwurf für diese Woche erstellen.")
        self.profile.setVisible(not has_profile)
        self.profile.setEnabled(not mutating)
        self.availability.setVisible(has_profile)
        self.availability.setEnabled(not mutating)
        self.regenerate.setEnabled(not mutating and not invalid_plan and exists and has_profile and has_availability)
        draft = self.controller.week.current_draft if self.controller.week else None
        self.accept.setEnabled(not mutating and draft is not None and
            draft.revision_assessment.status == "FRESH" and bool(draft.blocks) and has_profile)
        for button in self.content_widget.findChildren(QPushButton):
            button.setEnabled(not mutating)

    def _dialog(self, dialog):
        return dialog.exec() == dialog.DialogCode.Accepted

    def _feedback(self, message):
        from uniflow.gui.presentation import PresentationError
        self.controller.feedback.emit(PresentationError("INPUT_REQUIRED", "Check input", message))

    def open_regenerate(self):
        dto = self.controller.week
        if not dto or not dto.plan.plan_id:
            return
        dialog = RegenerateDialog(self)
        if self._dialog(dialog):
            try:
                values = dialog.values()
            except ValueError as exc:
                self._feedback(str(exc))
                return
            self.controller.regenerate_plan(dto.plan.plan_id, **values)

    def open_profile(self):
        if self.controller.state.busy[Route.WEEK] != Busy.IDLE:
            return
        dialog = ProfileDialog(self)
        if self._dialog(dialog):
            self.controller.create_planning_profile(dialog.duration.value(), dialog.daily.value())

    def open_availability(self):
        if self.controller.state.busy[Route.WEEK] != Busy.IDLE:
            return
        dialog = AvailabilityDialog(self)
        if self._dialog(dialog):
            try:
                weekday, start, end = dialog.values()
            except ValueError as exc:
                self._feedback(str(exc))
                return
            self.controller.add_learning_window(weekday, start, end)

    def open_accept(self):
        dto = self.controller.week
        if not dto or not dto.current_draft:
            return
        dialog = PlanningDialog("Accept plan", f"Accept draft revision {dto.current_draft.revision_number} as the operative plan?",
            "Accept plan", self)
        if self._dialog(dialog):
            self.controller.accept_plan(dto.plan.plan_id, dto.current_draft.revision_id)

    def _block_action(self, action, block, dto, revision):
        plan_id, revision_id = dto.plan.plan_id, revision.revision_id
        if action == "materialize":
            goal = None
            if block.action_type == "START_SKILL_SESSION" and not (block.materialization and block.materialization.status == "MATERIALIZED"):
                dialog = GoalDialog(self)
                if not self._dialog(dialog):
                    return
                goal = dialog.goal.text().strip()
                if not goal:
                    self._feedback("Enter a Session goal.")
                    return
            self.controller.materialize_block(plan_id, revision_id, block.block_id, goal)
        elif action == "move":
            dialog = TimeDialog("Move block", "Choose the new start for a new draft revision.",
                block.starts_at, dto.semester.timezone, parent=self)
            if self._dialog(dialog):
                self.controller.move_block(plan_id, revision_id, block.block_id, dialog.values()[0])
        elif action == "duration":
            dialog = DurationDialog(block.duration_minutes, self)
            if self._dialog(dialog):
                self.controller.set_block_duration(plan_id, revision_id, block.block_id,
                    dialog.minutes.value())
        elif action == "remove":
            if self._dialog(RemoveDialog(self)):
                self.controller.remove_block(plan_id, revision_id, block.block_id)

    def _place_action(self, action, dto, revision):
        initial = datetime.combine(dto.week_start, datetime.min.time(),
            tzinfo=ZoneInfo(dto.semester.timezone)).replace(hour=9)
        dialog = TimeDialog("Place action", "Choose a start and optional duration for a new draft revision.",
            initial, dto.semester.timezone, optional_duration=True, parent=self)
        if self._dialog(dialog):
            start, minutes = dialog.values()
            self.controller.place_unscheduled(dto.plan.plan_id, revision.revision_id,
                action.action_id, start, minutes)

    def render(self, dto):
        self.layout_.removeWidget(self.content_widget)
        self.content_widget.hide()
        self.content_widget.deleteLater()
        page = QWidget()
        self.content_widget = page
        body = QVBoxLayout(page)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        self.layout_.insertWidget(1, page)
        self.week_label.setText(f"{format_date(dto.week_start)} – {format_date(dto.week_end_exclusive - timedelta(days=1))}")
        strip = QHBoxLayout()
        strip.setSpacing(9)
        for offset in range(7):
            day = dto.week_start + timedelta(days=offset)
            tile = DayTile(day, dto, selected=day == self.controller.week_day)
            tile.setProperty("day", day)
            tile.clicked.connect(self._select_day)
            strip.addWidget(tile, 1)
        body.addLayout(strip)
        body.addWidget(self._day_detail(dto))
        calendar = _text(f"Calendar · {len(dto.current_calendar.academic_occurrences)} academic · "
            f"{len(dto.current_calendar.availability_occurrences)} availability · "
            f"{len(dto.current_calendar.constraint_occurrences)} personal constraints", muted=True)
        body.addWidget(calendar)
        if dto.current_calendar.planning_profile_id is None:
            body.addWidget(EmptyState("Persönliche Lernzeiten noch nicht eingerichtet.",
                "Akademische Termine bleiben sichtbar. Planning Profile anlegen und danach Lernzeitfenster hinzufügen."))
        elif not dto.current_calendar.availability_occurrences:
            body.addWidget(EmptyState("Keine Lernzeitfenster in dieser Woche",
                "Ein wiederkehrendes Lernzeitfenster hinzufügen, bevor ein sinnvoller Plan erzeugt wird."))
        else:
            windows = BaseCard("Persönliche Lernzeitfenster", variant="secondary")
            seen = set()
            for occurrence in dto.current_calendar.availability_occurrences:
                if occurrence.availability_window_id in seen:
                    continue
                seen.add(occurrence.availability_window_id)
                row = QHBoxLayout()
                row.addWidget(_text(f"{format_date(occurrence.local_date)} · "
                    f"{occurrence.starts_at.astimezone(ZoneInfo(dto.semester.timezone)).strftime('%H:%M')}–"
                    f"{occurrence.ends_at.astimezone(ZoneInfo(dto.semester.timezone)).strftime('%H:%M')}"), 1)
                remove = QPushButton("Deaktivieren…")
                remove.clicked.connect(lambda checked=False, identifier=occurrence.availability_window_id:
                    self._deactivate_window(identifier))
                row.addWidget(remove)
                windows.body.addLayout(row)
            body.addWidget(windows)
        if dto.plan.plan_id is None:
            if any(d.code == "WEEKLY_PLAN_SOURCE_INVALID" for d in dto.diagnostics):
                body.addWidget(EmptyState("Wochenplan nicht lesbar",
                    "Ein gespeicherter Plan konnte nicht verifiziert werden. Der Kalender bleibt sichtbar; Planänderungen sind für diese Woche gesperrt."))
            else:
                body.addWidget(EmptyState("Noch kein Wochenplan", "Nach dem Einrichten persönlicher Lernzeiten einen Entwurf erstellen."))
        else:
            if any(d.code == "WEEKLY_PLAN_SOURCE_INVALID" for d in dto.diagnostics):
                body.addWidget(EmptyState("Wochenplan nicht verifizierbar",
                    "Der gespeicherte Plan bleibt als ungültig sichtbar. Der akademische Kalender ist lesbar; Planaktionen sind gesperrt."))
            plan_info = BaseCard("Gespeicherter Wochenplan", variant="draft")
            plan_info.body.addWidget(_text(f"Woche {format_date(dto.week_start)} bis "
                f"{format_date(dto.week_end_exclusive - timedelta(days=1))} · "
                f"aktuelle Revision {dto.plan.current_revision_number}"))
            if dto.current_draft:
                block_count = len(dto.current_draft.blocks or [])
                unscheduled_count = len(dto.current_draft.unscheduled_actions or [])
                plan_info.body.addWidget(_text(f"Entwurf: {block_count} Lernblöcke · "
                    f"{unscheduled_count} nicht eingeplante Aktionen · "
                    f"Prüfung: {label(dto.current_draft.revision_assessment.status)}"))
                if not block_count:
                    reason = ("Persönliche Lernzeiten fehlen." if not dto.current_calendar.planning_profile_id else
                        "In dieser Woche fehlen Lernzeitfenster." if not dto.current_calendar.availability_occurrences else
                        "Keine geeigneten Aktionen oder Zeitfenster für Blöcke vorhanden.")
                    plan_info.body.addWidget(_text(reason, muted=True))
            if dto.revision_history:
                plan_info.body.addWidget(_text("Revisionen: " + " · ".join(
                    f"{item.revision_number} {label(item.revision_kind)}"
                    f" · {item.block_count if item.block_count is not None else 'unbekannt'} Blöcke, "
                    f"{item.unscheduled_count if item.unscheduled_count is not None else 'unbekannt'} offen"
                    f"{' (angenommen)' if item.accepted else ''}{' (aktuell)' if item.current else ''}"
                    for item in dto.revision_history), muted=True))
            plan_info.body.addWidget(_text("Entwurf verwerfen ist derzeit nicht verfügbar: Der vorhandene Service "
                "unterstützt nur unveränderliche Revisionen und Annahmen.", muted=True))
            body.addWidget(plan_info)
            columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
            columns.setSpacing(14)
            if dto.accepted_plan:
                columns.addWidget(RevisionPanel("Accepted plan", dto.accepted_plan,
                    dto.semester.timezone, accepted=True,
                    on_action=lambda a, b, d=dto, r=dto.accepted_plan: self._block_action(a, b, d, r),
                    course_names=dto.course_names), 1)
            else:
                columns.addWidget(EmptyState("No accepted plan", "A persisted draft exists, but no revision is accepted."), 1)
            if dto.current_draft:
                columns.addWidget(RevisionPanel("Current draft", dto.current_draft,
                    dto.semester.timezone, accepted=False,
                    on_action=lambda a, b, d=dto, r=dto.current_draft: self._block_action(a, b, d, r),
                    course_names=dto.course_names), 1)
            else:
                columns.addWidget(EmptyState("Current revision", "The accepted revision is current."), 1)
            body.addLayout(columns)
            unscheduled = QHBoxLayout()
            unscheduled.setSpacing(14)
            for title, revision, variant in (("Accepted · unscheduled", dto.accepted_plan, "accepted"),
                ("Draft · unscheduled", dto.current_draft, "draft")):
                if revision is None:
                    continue
                card = BaseCard(title, variant="secondary")
                actions = revision.unscheduled_actions
                if actions is None:
                    card.body.addWidget(_text("Source details unavailable.", muted=True))
                elif not actions:
                    card.body.addWidget(_text("No unscheduled actions captured.", muted=True))
                else:
                    for action in actions:
                        row = QHBoxLayout()
                        row.addWidget(_text(f"{dto.course_names.get(action.course_stable_id, action.course_stable_id)} · "
                            f"{label(action.primary_reason)}", muted=True), 1)
                        if variant == "draft":
                            place = QPushButton("Place")
                            place.clicked.connect(lambda checked=False, a=action, d=dto, r=revision:
                                self._place_action(a, d, r))
                            row.addWidget(place)
                        card.body.addLayout(row)
                unscheduled.addWidget(card, 1)
            body.addLayout(unscheduled)
        diagnostics = [d.code for d in dto.diagnostics] + [d.code for d in dto.current_calendar.diagnostics]
        if diagnostics:
            card = BaseCard("Diagnostics", variant="diagnostic")
            card.body.addWidget(_text(" · ".join(label(code) for code in diagnostics), muted=True))
            body.addWidget(card)
        else:
            body.addWidget(_text("✓  No diagnostics", muted=True))
        body.addStretch()
        self.sync()

    def _select_day(self, day):
        if day is None:
            return
        self.controller.week_day = day
        if self.controller.week is not None:
            self.render(self.controller.week)

    def _day_detail(self, dto):
        day = self.controller.week_day
        card = BaseCard(f"{format_date(day)} · Termine und Aufgaben", variant="secondary",
            icon_kind="calendar")
        events = sorted((event for event in dto.current_calendar.academic_occurrences
            if event.local_date == day), key=lambda event: (event.starts_at.timestamp() if event.starts_at else -1,
                event.occurrence_id))
        self.event_detail = _text("Termin auswählen, um alle verfügbaren Angaben zu sehen.", muted=True)
        if events:
            for event in events:
                title = event.title or label(event.activity_type or "Lehrveranstaltung")
                time_text = event.starts_at.astimezone(ZoneInfo(dto.semester.timezone)).strftime("%H:%M") if event.starts_at else "Uhrzeit unbekannt"
                identity = resolve_course_visual(event.course_stable_id, event.course_name or event.course_stable_id)
                button = AcademicEventButton(
                    f"{time_text} · {title} · {event.course_name or event.course_stable_id}",
                    accent_family=identity.accent_family, activity_type=event.activity_type,
                    location=event.location)
                button.clicked.connect(lambda checked=False, e=event: self._show_event(e, dto.semester.timezone))
                row = QHBoxLayout()
                row.addWidget(VisualBadge(identity, compact=True))
                row.addWidget(button, 1)
                card.body.addLayout(row)
        else:
            card.body.addWidget(_text("Keine akademischen Termine an diesem Tag.", muted=True))
        card.body.addWidget(self.event_detail)
        zone = ZoneInfo(dto.semester.timezone)
        due = [item for item in dto.deadline_items if due_day(item, zone) == day]
        course_ids = {event.course_id: event.course_stable_id
            for event in dto.current_calendar.academic_occurrences}
        if getattr(self.controller, "home", None) is not None:
            course_ids.update({course.course_id: course.course_stable_id
                for course in self.controller.home.courses})
        for item in due:
            stable_id = course_ids.get(item.course_id, "")
            course_name = dto.course_names.get(stable_id, "Fach unbekannt")
            identity = resolve_course_visual(stable_id, course_name)
            due_text = (f"{format_date(day)} · {format_time(item.due_at, dto.semester.timezone)}"
                if item.due_at else f"{format_date(day)} · Uhrzeit unbekannt")
            timing = timing_label(item, dto.as_of, zone)
            deadline = DeadlineButton(display_title(item.title, day), identity=identity,
                course_name=course_name, due_text=due_text, status=item.status,
                information=information_status(item.information_status), timing=timing)
            card.body.addWidget(deadline)
        for title, revision in (("Angenommener Lernblock", dto.accepted_plan),
                ("Entwurfsblock", dto.current_draft)):
            if revision and revision.blocks:
                for block in revision.blocks:
                    if block.starts_at.astimezone(ZoneInfo(dto.semester.timezone)).date() == day:
                        card.body.addWidget(_text(f"{title} · {format_time(block.starts_at, dto.semester.timezone)} · "
                            f"{block.duration_minutes} Min. · "
                            f"{dto.course_names.get(block.course_stable_id, block.course_stable_id)}"))
        return card

    def _show_event(self, event, timezone):
        title = event.title or label(event.activity_type or "Lehrveranstaltung")
        start = event.starts_at.astimezone(ZoneInfo(timezone)).strftime("%H:%M") if event.starts_at else "unbekannt"
        end = event.ends_at.astimezone(ZoneInfo(timezone)).strftime("%H:%M") if event.ends_at else "unbekannt"
        self.event_detail.setText(f"{title} · Fach: {event.course_name or event.course_stable_id} · "
            f"{start}–{end} · Raum: {event.location or 'unbekannt'} · "
            f"Informationsstatus: {information_status(event.information_status)} · "
            f"Quelle: {label(event.source_kind)}")

    def _deactivate_window(self, identifier):
        if self.controller.state.busy[Route.WEEK] != Busy.IDLE:
            return
        dialog = PlanningDialog("Lernzeitfenster deaktivieren",
            "Dieses wiederkehrende persönliche Lernzeitfenster deaktivieren? Bestehende Planrevisionen bleiben erhalten.",
            "Deaktivieren", self)
        if self._dialog(dialog):
            self.controller.deactivate_learning_window(identifier)
