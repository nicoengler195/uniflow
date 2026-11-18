from uniflow.gui.screen_chrome import SCREEN_STYLE
from uniflow.gui.safe_presentation import material_expectation
"""Read-only Materials Inbox from detached application DTOs."""
from datetime import date, timedelta
import textwrap
import re

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QComboBox, QDateEdit, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget)

from uniflow.gui.components import BaseCard, EmptyState, StatusChip, VisualBadge
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.presentation import format_date, format_time, information_status, label
from uniflow.gui.routes import Route
from uniflow.gui.state import ReviewFamily
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY


def line(value, *, muted=False):
    display = re.sub(r"\S{56,}", lambda m: "\u200b".join(
        m.group(0)[i:i + 48] for i in range(0, len(m.group(0)), 48)), str(value))
    widget = QLabel(display)
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
    return widget


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


def parse_text(observation):
    return {None: "No parse recorded", "SUCCESS": "Parsed", "FAILED": "Parse failed",
        "OCR_REQUIRED": "OCR required"}[observation.status if observation else None]


def safe_filename(value):
    return "Filename metadata unavailable" if any(ch in value for ch in ("/", "\\", ":", "\x00", "\n", "\r")) else value


def source_kind(source):
    ext = (source.extension or "").lower()
    return "pdf" if ext == ".pdf" else "text" if ext == ".txt" else (
        "markdown" if ext in {".md", ".markdown"} else "document")


def source_lines(body, source, zone):
    body.addWidget(line(safe_filename(source.original_filename)))
    body.addWidget(line(" · ".join(x for x in (source.extension or "Unknown format",
        source.mime_type or "", f"{source.size_bytes:,} bytes") if x), muted=True))
    body.addWidget(line(parse_text(source.parse_observation), muted=True))
    if source.parse_observation and source.parse_observation.status == "FAILED":
        p = source.parse_observation
        if p.error_code:
            body.addWidget(line(p.error_code, muted=True))
        if p.error_message_excerpt:
            excerpt = p.error_message_excerpt[:240]
            body.addWidget(line("\n".join(textwrap.wrap(excerpt, width=72, break_long_words=True,
                break_on_hyphens=False)) + ("…" if p.error_message_truncated or len(p.error_message_excerpt) > 240 else ""), muted=True))
    body.addWidget(line("Recorded " + format_time(source.created_at, zone), muted=True))


class MaterialsScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setObjectName("materialsScreen")
        self.setStyleSheet(SCREEN_STYLE)
        self.controller = controller
        self.context = None
        self.view = None
        self._syncing = False
        self.library_mode = "overview"
        self.grid_wrapper = None
        self.grid_courses = ()
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        head = BaseCard("Materialien", icon_kind="pdf")
        head.body.addWidget(line("Veröffentlichungsprognosen, importierte Materialien und Quellen", muted=True))
        self.import_button = QPushButton("Material importieren…")
        self.import_button.clicked.connect(self._import_material)
        head.body.addWidget(self.import_button)
        filters = QGridLayout()
        filters.addWidget(line("Fach"), 0, 0)
        self.course = QComboBox()
        self.course.setObjectName("materialsCourseFilter")
        filters.addWidget(self.course, 0, 1, 1, 3)
        self.from_date = QDateEdit()
        self.through_date = QDateEdit()
        for field in (self.from_date, self.through_date):
            field.setCalendarPopup(True)
            field.setDisplayFormat("dd MMM yyyy")
        filters.addWidget(line("Von"), 1, 0)
        filters.addWidget(self.from_date, 1, 1)
        filters.addWidget(line("Bis"), 1, 2)
        filters.addWidget(self.through_date, 1, 3)
        filters.setColumnStretch(1, 1)
        filters.setColumnStretch(3, 1)
        head.body.addLayout(filters)
        presets = QHBoxLayout()
        for caption, method in (("Semesterzeitraum", self.semester_window),
                ("Dieser Monat", self.this_month), ("Zurücksetzen", self.reset_range)):
            button = QPushButton(caption)
            button.clicked.connect(method)
            presets.addWidget(button)
        presets.addStretch()
        head.body.addLayout(presets)
        self.body.addWidget(head)
        self.content = QVBoxLayout()
        self.content.setSpacing(15)
        self.body.addLayout(self.content)
        self.body.addStretch()
        self.course.currentIndexChanged.connect(self._course_filter_changed)
        self.from_date.dateChanged.connect(self.apply_filter)
        self.through_date.dateChanged.connect(self.apply_filter)

    def _import_material(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
        if self.controller.bridge_busy or not self.context:
            return
        path = QFileDialog.getOpenFileName(self, "Lehrmaterial auswählen", "",
            "Documents (*.pdf *.txt *.md)")[0]
        if not path:
            return
        courses = list(self.context.courses)
        if not courses:
            QMessageBox.information(self, "No courses", "The active semester has no courses for this material.")
            return
        labels = [f"{c.name} ({c.stable_id})" for c in courses]
        choice, ok = QInputDialog.getItem(self, "Fach bestätigen", "Dieses Material gehört zu:",
            labels, 0, False)
        if ok:
            self.controller.import_study_material(Path(path), courses[labels.index(choice)].stable_id)

    def sync(self):
        self.import_button.setEnabled(not self.controller.bridge_busy)

    def _set_dates(self, start, end):
        self._syncing = True
        self.from_date.setDate(QDate(start.year, start.month, start.day))
        self.through_date.setDate(QDate(end.year, end.month, end.day))
        self._syncing = False
        self.apply_filter()

    def reset_range(self):
        if self.context:
            start, end = self.controller._materials_default_range(self.context, self.controller.clock())
            self._set_dates(start, end)

    def semester_window(self):
        if self.context:
            start, end = self.controller._materials_default_range(self.context, self.controller.clock())
            if self.context.starts_on and self.context.ends_on:
                start = self.context.starts_on
                end = min(self.context.ends_on, start + timedelta(days=365))
            self._set_dates(start, end)

    def this_month(self):
        if self.context:
            from zoneinfo import ZoneInfo
            today = self.controller.clock().astimezone(ZoneInfo(self.context.timezone)).date()
            start = today.replace(day=1)
            end = (start.replace(year=start.year + 1, month=1) if start.month == 12
                else start.replace(month=start.month + 1)) - timedelta(days=1)
            self._set_dates(start, end)

    def apply_filter(self, *_):
        if self._syncing or not self.context:
            return
        start = self.from_date.date().toPython()
        end = self.through_date.date().toPython()
        if not self.controller.set_materials_filter(course_id=self.course.currentData(),
                from_date=start, through_date=end):
            self._syncing = True
            previous_start = self.controller.state.materials_from_date
            previous_end = self.controller.state.materials_through_date
            if previous_start and previous_end:
                self.from_date.setDate(QDate(previous_start.year, previous_start.month, previous_start.day))
                self.through_date.setDate(QDate(previous_end.year, previous_end.month, previous_end.day))
            self._syncing = False

    def _course_filter_changed(self, *_):
        if self._syncing:
            return
        self.library_mode = "course" if self.course.currentData() else "overview"
        self.apply_filter()

    def _open_course(self, stable_id):
        self.library_mode = "course"
        self.controller.set_materials_filter(course_id=stable_id,
            from_date=self.from_date.date().toPython(),
            through_date=self.through_date.date().toPython())

    def _show_root(self):
        self.library_mode = "overview"
        self.controller.set_materials_filter(course_id=None,
            from_date=self.from_date.date().toPython(),
            through_date=self.through_date.date().toPython())
        if self.view and self.view.course_filter_stable_id is None:
            self.render((self.context, self.view))

    def _show_scope(self, mode):
        self.library_mode = mode
        if self.view and self.view.course_filter_stable_id is None:
            self.render((self.context, self.view))
        else:
            self.controller.set_materials_filter(course_id=None,
                from_date=self.from_date.date().toPython(),
                through_date=self.through_date.date().toPython())

    def _course_tile(self, course):
        identity = resolve_course_visual(course.stable_id, course.name)
        card = BaseCard(variant="course")
        art = PackagedArtwork(course_artwork_asset(identity), mode="library")
        art.setFixedHeight(130)
        card.body.addWidget(art)
        name = line(course.name)
        name.setStyleSheet(f"color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 600;")
        card.body.addWidget(name)
        button = QPushButton("Öffnen")
        button.setAccessibleName(course.name + " öffnen")
        button.clicked.connect(lambda _, sid=course.stable_id: self._open_course(sid))
        card.body.addWidget(button)
        return card

    def _course_grid(self, courses):
        wrapper = QWidget()
        grid = QGridLayout(wrapper)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        self.grid_wrapper = wrapper
        self.grid_courses = tuple(self._course_tile(course) for course in courses)
        self._reflow_courses()
        wrapper.setProperty("libraryCourses", tuple(c.stable_id for c in courses))
        return wrapper

    def _reflow_courses(self):
        if self.grid_wrapper is None:
            return
        grid = self.grid_wrapper.layout()
        columns = 4 if self.width() >= 1000 else 2 if self.width() >= 590 else 1
        while grid.count():
            grid.takeAt(0)
        for index, card in enumerate(self.grid_courses):
            grid.addWidget(card, index // columns, index % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_courses()

    def render(self, pair):
        if pair is None:
            self.grid_wrapper = None
            self.grid_courses = ()
            clear(self.content)
            self.content.addWidget(EmptyState("Materials unavailable", "The selected Materials data needs review."))
            return
        self.context, self.view = pair
        self._syncing = True
        self.course.clear()
        self.course.addItem("Alle Kurse", None)
        for c in self.context.courses:
            self.course.addItem(c.name, c.stable_id)
        index = self.course.findData(self.view.course_filter_stable_id)
        self.course.setCurrentIndex(max(index, 0))
        self.from_date.setDate(QDate(self.view.from_date.year, self.view.from_date.month, self.view.from_date.day))
        self.through_date.setDate(QDate(self.view.through_date.year, self.view.through_date.month, self.view.through_date.day))
        self._syncing = False
        self.grid_wrapper = None
        self.grid_courses = ()
        clear(self.content)
        dto = self.view
        names = {c.stable_id: c.name for c in self.context.courses}
        by_id = {c.course_id: c for c in self.context.courses}
        selected = next((c for c in self.context.courses
            if c.stable_id == dto.course_filter_stable_id), None)
        if selected is None and self.library_mode == "overview":
            roots = [c for c in self.context.courses if c.parent_course_id is None]
            priority = {"MATHEMATICS": 0, "PHYSICS": 1, "NEUROSCIENCE": 2,
                "COMPUTER_SCIENCE": 3}
            roots.sort(key=lambda c: (priority.get(resolve_course_visual(c.stable_id,
                c.name).domain_family, 10), c.name))
            self.content.addWidget(line("Studienbibliothek · Fach auswählen"))
            if roots:
                self.content.addWidget(self._course_grid(roots))
            else:
                self.content.addWidget(EmptyState("Keine Fächer", "Für dieses Semester sind keine Kurse erfasst."))
            for caption, mode in (("Alle Materialien und Filterergebnisse", "all"),
                                  ("Nicht zugeordnet", "unassigned")):
                button = QPushButton(caption)
                button.clicked.connect(lambda _, m=mode: self._show_scope(m))
                self.content.addWidget(button)
            return
        if selected is None and self.library_mode == "unassigned":
            back = QPushButton("← Zur Fächerübersicht")
            back.clicked.connect(self._show_root)
            self.content.addWidget(back)
            self.content.addWidget(line("Nicht zugeordnet"))
            before = self.content.count()
            self._project_wide(dto, names)
            if self.content.count() == before:
                self.content.addWidget(EmptyState("Keine unzugeordneten Einträge",
                    "In diesem Bereich liegen derzeit keine unzugeordneten Materialien oder Quellen."))
            return
        if selected is not None:
            parent = by_id.get(selected.parent_course_id)
            back = QPushButton("← Zum Modul" if parent else "← Zur Fächerübersicht")
            back.clicked.connect(lambda _, p=parent: self._open_course(p.stable_id)
                if p else self._show_root())
            self.content.addWidget(back)
            self.content.addWidget(line(selected.name))
            children = [c for c in self.context.courses if c.parent_course_id == selected.course_id]
            if children:
                self.content.addWidget(line("Teilkurse"))
                self.content.addWidget(self._course_grid(children))
                self.content.addWidget(line("Gemeinsame Materialien und Veröffentlichungsregeln des Moduls", muted=True))
        elif self.library_mode == "all":
            back = QPushButton("← Zur Fächerübersicht")
            back.clicked.connect(self._show_root)
            self.content.addWidget(back)
        summary = BaseCard("Im gewählten Zeitraum")
        pending = len(dto.pending_classification_proposals) + len(dto.pending_skill_proposals)
        summary.body.addWidget(line(f"{len(dto.materials)} Materialien  ·  {len(dto.projected_expectations)} Prognosen  ·  {pending} offene Vorschläge  ·  {len(dto.repositories)} Repositories  ·  {len(dto.unresolved_rules)} ungeklärte Regeln"))
        self.content.addWidget(summary)
        reached_unlinked = sum(e.manifest_state == "NOT_LINKED_IN_UNIFLOW" and
            e.explicit_link_state == "NO_EXPLICIT_UNIFLOW_LINK" for e in dto.projected_expectations)
        if pending or dto.unresolved_rules or reached_unlinked:
            self.content.addWidget(self._disclosure('Technische Hinweise zur Zuordnung',
                lambda body: body.addWidget(line(f'{pending} offene Vorschläge · {len(dto.unresolved_rules)} ungeklärte Regeln · {reached_unlinked} Prognosetermine ohne Materialverknüpfung. Das belegt keine fehlende PDF.')), icon='warning'))
        if dto.materials:
            self.content.addWidget(line("Importierte Materialien · Material öffnen und weiterlernen"))
            def newest_link(material):
                current = next((v for v in material.versions if v.is_current), None)
                return -(current.linked_at.timestamp() if current else 0)
            for material in sorted(dto.materials, key=lambda m: (newest_link(m), m.material_id)):
                self.content.addWidget(self._material_card(material, names))
        else:
            self.content.addWidget(EmptyState("Noch keine importierten Materialien", "Über Material importieren eine PDF-, Text- oder Markdown-Datei hinzufügen."))
        if dto.projected_expectations:
            box = BaseCard("Materialerwartungen", icon_kind="expectation")
            def add_expectations(body, items):
                for e in items:
                    caption, icon, color, certainty = material_expectation(e.material_kind,e.information_status,linked=e.explicit_link_state=='LINKED')
                    when = format_date(e.release_date) + (f" · {e.release_time.strftime('%H:%M')}" if e.release_time else " · Uhrzeit unbekannt")
                    row = self._release_row(e.course_stable_id, names, f'{caption} · {when}', certainty)
                    row.layout().insertWidget(1, self._expectation_icon(icon, color))
                    row.setToolTip('Der Rhythmus bestätigt keine einzelne Veröffentlichung.')
                    body.addWidget(row)
            box.body.addWidget(line('Erwartet oder möglich – je nach hinterlegtem Rhythmus.', muted=True))
            add_expectations(box.body, dto.projected_expectations[:12])
            if len(dto.projected_expectations) > 12:
                box.body.addWidget(self._disclosure(f"Weitere {len(dto.projected_expectations) - 12} Prognosen anzeigen",
                    lambda body: add_expectations(body, dto.projected_expectations[12:])))
            def expectation_details(body):
                for e in dto.projected_expectations:
                    body.addWidget(line(f'{e.expectation_id} · {e.rule_stable_id} · {e.information_status} · {e.manifest_state} · {e.explicit_link_state}'))
            box.body.addWidget(self._disclosure('Technische Details zu Materialerwartungen', expectation_details))
            box.setMinimumHeight(box.sizeHint().height())
            self.content.addWidget(box)
        if dto.unresolved_rules:
            def add_rules(body):
                for rule in dto.unresolved_rules:
                    body.addWidget(self._release_row(rule.course_stable_id, names,
                        f"{label(rule.material_kind)} · Veröffentlichungsregel ungeklärt",
                        f"{rule.reason} · {information_status(rule.information_status)}"))
            self.content.addWidget(self._disclosure(
                f"Ungeklärte Veröffentlichungsregeln · {len(dto.unresolved_rules)}", add_rules,
                icon="warning"))
        if dto.repositories:
            self.content.addWidget(line("Repositories"))
            for repo in dto.repositories:
                self.content.addWidget(self._repository(repo, names))
        if dto.pending_classification_proposals or dto.decided_classification_proposals:
            self.content.addWidget(self._proposals("Classification proposals",
                dto.pending_classification_proposals + dto.decided_classification_proposals,
                ReviewFamily.CLASSIFICATION, scoped=True))
        if dto.pending_skill_proposals or dto.decided_skill_proposals:
            self.content.addWidget(self._proposals("Skill proposals",
                dto.pending_skill_proposals + dto.decided_skill_proposals,
                ReviewFamily.SKILL, scoped=True))
        if dto.course_filter_stable_id is None:
            self._project_wide(dto, names)

    def _material_card(self, material, names):
        course_name = names.get(material.course_stable_id, material.course_stable_id or "Unscoped")
        identity = resolve_course_visual(material.course_stable_id or "", course_name)
        card = BaseCard()
        if material.course_stable_id:
            art = PackagedArtwork(course_artwork_asset(identity), mode="thumbnail")
            art.setFixedHeight(126)
            art.setMaximumWidth(440)
            card.body.addWidget(art)
        card.body.addWidget(line(course_name, muted=True))
        card.body.addWidget(line(material.title))
        card.body.addWidget(line(f"{label(material.material_type) if material.material_type else 'Type not recorded'} · {label(material.lifecycle_state)}"))
        current = next((v for v in material.versions if v.is_current), None)
        if current:
            card.body.addWidget(StatusChip(parse_text(current.source_artifact.parse_observation)))
            card.body.addWidget(line(f"Version {current.version_number} · {safe_filename(current.source_artifact.original_filename)}", muted=True))
        else:
            card.body.addWidget(line("Keine aktuelle Quellversion", muted=True))
        exact_pending = sum(p.subject_material_id == material.material_id or
            p.proposed_material_id == material.material_id for p in self.view.pending_classification_proposals)
        active_links = sum(x.material_id == material.material_id and x.is_active
            for x in self.view.expectation_links)
        if exact_pending or active_links:
            card.body.addWidget(line(f"{exact_pending} pending Classification proposals · {active_links} active explicit expectation links", muted=True))
        if material.course_stable_id:
            button = QPushButton("Material öffnen")
            button.clicked.connect(lambda _, mid=material.material_id: self.controller.select_material(mid))
            card.body.addWidget(button)
        return card

    @staticmethod
    def _expectation_icon(kind, color):
        symbol = QLabel()
        symbol.setPixmap(content_icon(kind, color=color).pixmap(20,20))
        symbol.setFixedSize(22,22)
        return symbol

    def _release_row(self, course_stable_id, names, title, detail):
        course_name = names.get(course_stable_id, course_stable_id)
        identity = resolve_course_visual(course_stable_id, course_name)
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 3, 0, 3)
        outer.setSpacing(9)
        outer.addWidget(VisualBadge(identity, compact=True))
        copy = QVBoxLayout()
        name = line(course_name)
        name.setStyleSheet(f"color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 600;")
        copy.addWidget(name)
        copy.addWidget(line(title))
        copy.addWidget(line(detail, muted=True))
        outer.addLayout(copy, 1)
        return row

    def _disclosure(self, title, fill, *, icon="document"):
        card = BaseCard()
        toggle = QPushButton("▸ " + title)
        toggle.setIcon(content_icon(icon))
        toggle.setCheckable(True)
        card.body.addWidget(toggle)
        inner = QWidget()
        body = QVBoxLayout(inner)
        fill(body)
        inner.hide()
        def change(checked):
            inner.setVisible(checked)
            toggle.setText(("▾ " if checked else "▸ ") + title)
        toggle.toggled.connect(change)
        card.body.addWidget(inner)
        return card

    def _repository(self, repo, names):
        def fill(body):
            body.addWidget(line(f"Provider: {repo.provider_hint or 'Unknown'} · {repo.snapshot_count} snapshots"))
            snap = repo.latest_snapshot
            if snap:
                body.addWidget(line(f"Latest snapshot {format_time(snap.captured_at, self.context.timezone)} · {snap.status}"))
                body.addWidget(line(f"Branch {snap.branch_name or 'Unknown'} · HEAD {(snap.head_commit_oid or 'Unknown')[:12]} · {'Dirty' if snap.is_dirty else 'Clean'} · {'Detached' if snap.is_detached else 'Attached'}", muted=True))
                body.addWidget(line(f"Tracked {snap.tracked_file_count} · Untracked {snap.untracked_file_count} · Deleted {snap.deleted_file_count} · Blobs {snap.blob_count}", muted=True))
            for link in repo.academic_links:
                body.addWidget(line(f"{'Active' if link.is_active else 'Historical'} academic link · {names.get(link.course_stable_id, link.course_stable_id)}", muted=True))
        return self._disclosure("Repository · " + repo.stable_id, fill, icon="repository")

    def _proposals(self, title, proposals, family=None, *, scoped=False):
        box = BaseCard(title)
        for p in proposals:
            target = getattr(p, "proposed_material_title", None) or getattr(p, "proposed_name", None) or getattr(p, "proposed_alias", None) or getattr(p, "target_skill_id", None) or "Target recorded in provenance"
            box.body.addWidget(line(f"{label(p.proposal_kind)} · {label(p.status)} · {target}"))
            note = f"Created {format_time(p.created_at, self.context.timezone)}"
            if p.confidence is not None:
                note += f" · Proposal confidence {p.confidence:.0%}"
            if p.decided_at:
                note += f" · Decided {format_time(p.decided_at, self.context.timezone)}"
            if p.decision_actor:
                note += f" · By {p.decision_actor}"
            box.body.addWidget(line(note, muted=True))
            if p.decision_reason:
                box.body.addWidget(line(p.decision_reason, muted=True))
            if scoped:
                caption = ("Review" if p.status == "PENDING" and
                    (family == ReviewFamily.CLASSIFICATION or p.proposal_kind == "NEW_SKILL")
                    else "View decision" if p.status != "PENDING" else "View details")
                button = QPushButton(caption)
                button.clicked.connect(lambda _, fam=family, pid=p.proposal_id:
                    self.controller.select_proposal_review(fam, pid, Route.MATERIALS))
                box.body.addWidget(button)
        return box

    def _project_wide(self, dto, names):
        groups = (dto.project_wide_unassigned_artifacts, dto.project_wide_unscoped_materials,
            dto.project_wide_unlinked_repositories, dto.project_wide_unscoped_classification_proposals,
            dto.project_wide_unscoped_skill_proposals)
        if not any(groups):
            return
        self.content.addWidget(line("Project-wide"))
        for source in dto.project_wide_unassigned_artifacts:
            def fill(body, source=source):
                source_lines(body, source, self.context.timezone)
                body.addWidget(line("SHA-256 " + source.sha256, muted=True))
            self.content.addWidget(self._disclosure("Unassigned source · " + safe_filename(source.original_filename),
                fill, icon=source_kind(source)))
        for material in dto.project_wide_unscoped_materials:
            self.content.addWidget(self._material_card(material, names))
        for repo in dto.project_wide_unlinked_repositories:
            self.content.addWidget(self._repository(repo, names))
        if dto.project_wide_unscoped_classification_proposals:
            self.content.addWidget(self._proposals("Unscoped Classification proposals", dto.project_wide_unscoped_classification_proposals))
        if dto.project_wide_unscoped_skill_proposals:
            self.content.addWidget(self._proposals("Unscoped Skill proposals", dto.project_wide_unscoped_skill_proposals))
