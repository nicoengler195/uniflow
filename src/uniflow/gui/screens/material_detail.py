"""Read-only exact logical Material detail."""
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QLabel,
    QLineEdit, QListView, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QHBoxLayout, QVBoxLayout, QWidget)

from uniflow.gui.components import BaseCard, EmptyState
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.presentation import PresentationError, format_date, format_time, information_status, label
from uniflow.gui.screens.materials import clear, line, parse_text, safe_filename, source_lines
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy, ReviewFamily


def _python_offset(text, qt_offset):
    """Convert QTextCursor's UTF-16 position to a Python character offset."""
    units = 0
    for index, character in enumerate(text):
        if units == qt_offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > qt_offset:
            return None
    return len(text) if units == qt_offset else None


class ParsedSelectionDialog(QDialog):
    def __init__(self, controller, parse_run_id, parent=None, *, allow_proposal=True):
        super().__init__(parent)
        from uniflow.application.document_parsing import get_parse_run, read_parsed_unit
        self.controller = controller
        self.parse_run_id = parse_run_id
        self.read_unit = read_parsed_unit
        self.parse_run, self.units = None, []
        self._closed = False
        self._unit_generation = 0
        self._identity = controller.workflow_identity()
        self.finished.connect(self._finished)
        self.allow_proposal = allow_proposal
        self.selected = None
        self.current_text = ""
        self.setObjectName("skillSelectionDialog")
        self.setWindowTitle("Skill aus Textauswahl vorschlagen" if allow_proposal else "Geparsten Text ansehen")
        self.resize(900, 680)
        body = QVBoxLayout(self)
        self.page_summary = QLabel(f"Parse {parse_run_id} · Quelle wird geladen …")
        body.addWidget(self.page_summary)
        body.addWidget(QLabel("Seite wählen und eine Passage innerhalb dieser Seite markieren." if allow_proposal
            else "Seite wählen. Leere Seiten benötigen gegebenenfalls OCR."))
        navigation = QHBoxLayout()
        self.unit_choice = QComboBox()
        for unit in self.units:
            suffix = " · kein Text / OCR prüfen" if unit.non_whitespace_char_count == 0 else " · Text vorhanden"
            self.unit_choice.addItem(f"{unit.unit_kind} {unit.source_label or unit.ordinal}{suffix}", unit.id)
        self.unit_choice.setView(QListView())
        self.unit_choice.setMaxVisibleItems(16)
        navigation.addWidget(self.unit_choice, 1)
        self.page_number = QSpinBox()
        self.page_number.setPrefix("Seite ")
        self.page_number.setRange(1, max(1, len(self.units)))
        self.page_number.setEnabled(bool(self.units))
        self.page_number.valueChanged.connect(lambda value: self.unit_choice.setCurrentIndex(value - 1))
        navigation.addWidget(self.page_number)
        body.addLayout(navigation)
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMinimumHeight(360)
        body.addWidget(self.editor)
        name_label = QLabel("Skill-Name")
        body.addWidget(name_label)
        self.name = QLineEdit()
        self.name.setMaxLength(240)
        body.addWidget(self.name)
        kind_label = QLabel("Skill-Art")
        body.addWidget(kind_label)
        self.kind = QComboBox()
        self.kind.addItems(["CONCEPT", "ABILITY"])
        body.addWidget(self.kind)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Markierte Quelle erscheint hier vor dem Vorschlag.")
        self.preview.setMaximumHeight(110)
        body.addWidget(self.preview)
        self.status = QLabel("Noch keine Passage ausgewählt.")
        body.addWidget(self.status)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel if allow_proposal
            else QDialogButtonBox.StandardButton.Close)
        if allow_proposal:
            actions.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        else:
            actions.button(QDialogButtonBox.StandardButton.Close).setText("Schließen")
        self.propose = QPushButton("Skill aus Auswahl vorschlagen")
        self.propose.setEnabled(False)
        actions.addButton(self.propose, QDialogButtonBox.ButtonRole.AcceptRole)
        actions.rejected.connect(self.reject)
        self.propose.clicked.connect(self._accept_selection)
        body.addWidget(actions)
        self.unit_choice.currentIndexChanged.connect(self._load_unit)
        self.editor.selectionChanged.connect(self._update_selection)
        self.name.textChanged.connect(self._update_selection)
        if not allow_proposal:
            name_label.hide()
            self.name.hide()
            kind_label.hide()
            self.kind.hide()
            self.preview.hide()
            self.status.hide()
            self.propose.hide()
        controller.runner.submit("parsed_source", self._identity,
            lambda: get_parse_run(parse_run_id, controller.config), self._source_loaded)

    def _finished(self, result):
        self._closed = True
        self._unit_generation += 1

    def _is_current(self):
        return not self._closed and self._identity == self.controller.workflow_identity()

    def _source_loaded(self, kind, token, result):
        if not self._is_current():
            return
        if isinstance(result, PresentationError):
            self.status.setText(result.message)
            return
        self.parse_run, self.units = result
        readable = sum(unit.non_whitespace_char_count > 0 for unit in self.units)
        self.page_summary.setText(f"Parse {self.parse_run_id} · {len(self.units)} Seiten · "
            f"{readable} mit Text · {len(self.units) - readable} ohne Text / OCR prüfen")
        self.unit_choice.blockSignals(True)
        for unit in self.units:
            suffix = " · kein Text / OCR prüfen" if unit.non_whitespace_char_count == 0 else " · Text vorhanden"
            self.unit_choice.addItem(f"{unit.unit_kind} {unit.source_label or unit.ordinal}{suffix}", unit.id)
        self.unit_choice.blockSignals(False)
        self.page_number.setRange(1, max(1, len(self.units)))
        self.page_number.setEnabled(bool(self.units))
        if self.units:
            self._load_unit(0)
        else:
            self.status.setText("Keine geparsten Texteinheiten verfügbar.")

    def _load_unit(self, index):
        self._unit_generation += 1
        generation = self._unit_generation
        self.selected = None
        self.current_text = ""
        self.editor.clear()
        self.propose.setEnabled(False)
        if index < 0 or index >= len(self.units):
            self.current_text = ""
            self.editor.clear()
            self._update_selection()
            return
        self.status.setText("Quelltext wird geladen …")
        ordinal = self.units[index].ordinal
        self.controller.runner.submit("parsed_unit", generation,
            lambda: self.read_unit(self.parse_run_id, ordinal, self.controller.config),
            lambda kind, token, result: self._unit_loaded(index, token, result))

    def _unit_loaded(self, index, generation, result):
        if not self._is_current() or generation != self._unit_generation:
            return
        if isinstance(result, PresentationError):
            self.status.setText(result.message)
            return
        self.current_text = result
        self.page_number.blockSignals(True)
        self.page_number.setValue(index + 1)
        self.page_number.blockSignals(False)
        self.editor.setPlainText(self.current_text)
        if not self.current_text.strip():
            self.editor.setPlaceholderText("Diese Seite enthält keinen extrahierbaren Text. OCR kann erforderlich sein.")
        else:
            self.editor.setPlaceholderText("")
        self._update_selection()

    def _update_selection(self):
        self.selected = None
        self.propose.setEnabled(False)
        if self.editor.toPlainText() != self.current_text:
            self.status.setText("Die Textdarstellung weicht von der Quelle ab. Bitte Seite erneut öffnen.")
            return
        cursor = self.editor.textCursor()
        start = _python_offset(self.current_text, cursor.selectionStart())
        end = _python_offset(self.current_text, cursor.selectionEnd())
        if start is None or end is None or start == end:
            self.preview.clear()
            self.status.setText("Kein extrahierbarer Text auf dieser Seite; OCR prüfen." if
                not self.current_text.strip() else "Eine Passage innerhalb dieser Seite markieren.")
            return
        excerpt = self.current_text[start:end]
        if not excerpt.strip() or len(excerpt.encode("utf-8")) > self.controller.config.learning_handoff_max_excerpt_utf8_bytes:
            self.preview.clear()
            self.status.setText("Die Auswahl muss Text enthalten und höchstens "
                f"{self.controller.config.learning_handoff_max_excerpt_utf8_bytes} UTF-8-Bytes umfassen.")
            return
        self.preview.setPlainText(f"Parse {self.parse_run_id} · Unit {self.unit_choice.currentData()} · "
            f"Quelle: {self.unit_choice.currentText()} · Zeichen {start}–{end}\n{excerpt}")
        self.status.setText("Skill-Namen eingeben, dann Vorschlag senden." if
            not self.name.text().strip() else "Quelle und Auswahl prüfen, dann Skill vorschlagen.")
        self.selected = (self.unit_choice.currentData(), start, end)
        self.propose.setEnabled(bool(self.name.text().strip()))

    def _accept_selection(self):
        self._update_selection()
        if not self._is_current() or not self.selected or not self.name.text().strip():
            QMessageBox.information(self, "Auswahl fehlt", "Bitte Passage markieren und Skill-Namen eingeben.")
            return
        self.accept()


class MaterialDetailScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.action_buttons = []
        controller.state_changed.connect(self.sync)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(15)
        back = QPushButton("← Back to Materials")
        back.clicked.connect(controller.back_to_materials)
        self.body.addWidget(back)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(15)
        self.body.addLayout(self.cards)
        self.body.addStretch()

    def render(self, pair):
        self._learning_generation = getattr(self, "_learning_generation", 0) + 1
        self.action_buttons = []
        if pair is None:
            clear(self.cards)
            self.cards.addWidget(EmptyState("Material unavailable", "The selected Material data needs review."))
            return
        context, dto = pair
        clear(self.cards)
        material = dto.material
        names = {c.stable_id: c.name for c in context.courses} if context else {}
        course_id = material.course_stable_id
        course = names.get(course_id, course_id or "Course unknown")
        zone = dto.semester.timezone
        header = BaseCard()
        identity = resolve_course_visual(course_id or "", course)
        art = PackagedArtwork(course_artwork_asset(identity), mode="thumbnail")
        art.setFixedHeight(100)
        header.body.addWidget(art)
        header.body.addWidget(line(course))
        header.body.addWidget(line(course_id or "Unscoped", muted=True))
        header.body.addWidget(line(material.title))
        header.body.addWidget(line(f"{label(material.material_type) if material.material_type else 'Type not recorded'} · {label(material.lifecycle_state)}"))
        if course_id not in names:
            header.body.addWidget(line("Course label unavailable; stable ID shown", muted=True))
        self.cards.addWidget(header)
        current = next((v for v in material.versions if v.is_current), None)
        if current:
            source = BaseCard("Current source")
            source.body.addWidget(line(f"Version {current.version_number} · Linked {format_time(current.linked_at, zone)}"))
            source_lines(source.body, current.source_artifact, zone)
            observation = current.source_artifact.parse_observation
            if observation:
                source.body.addWidget(line(parse_text(observation)))
            self.cards.addWidget(source)
        else:
            self.cards.addWidget(EmptyState("No current source", "This logical Material has no current physical version."))
        self.cards.addWidget(self._learning_step(material, current, dto.skill_proposals, course))
        history = BaseCard("Version history")
        if material.versions:
            for version in material.versions:
                history.body.addWidget(line(f"Version {version.version_number} · {'Current' if version.is_current else 'Historical'} · {safe_filename(version.source_artifact.original_filename)}"))
                history.body.addWidget(line(f"Linked {format_time(version.linked_at, zone)} · {parse_text(version.source_artifact.parse_observation)}", muted=True))
                if version.deactivated_at:
                    history.body.addWidget(line("Deactivated " + format_time(version.deactivated_at, zone) +
                        (f" · {version.deactivation_reason}" if version.deactivation_reason else ""), muted=True))
        else:
            history.body.addWidget(line("No versions recorded", muted=True))
        self.cards.addWidget(history)
        expectations = BaseCard("Expectations and link history")
        for occurrence in dto.projected_expectations:
            state = "Expected release ahead" if occurrence.manifest_state == "UPCOMING" else "Expected date reached"
            expectations.body.addWidget(line(f"{label(occurrence.material_kind)} · {format_date(occurrence.release_date)} · {state} · Explicit UniFlow link"))
            expectations.body.addWidget(line(information_status(occurrence.information_status), muted=True))
        for link in dto.expectation_links:
            expectations.body.addWidget(line(f"{format_date(link.occurrence_date)} · {'Active' if link.is_active else 'Historical'} link · {label(link.fingerprint_status)}"))
            if link.deactivated_at:
                expectations.body.addWidget(line("Deactivated " + format_time(link.deactivated_at, zone), muted=True))
        if not dto.projected_expectations and not dto.expectation_links:
            expectations.body.addWidget(line("No explicit expectation links recorded", muted=True))
        self.cards.addWidget(expectations)
        for title, proposals, family in (("Classification history", dto.classification_proposals, ReviewFamily.CLASSIFICATION),
                ("Skill proposal history", dto.skill_proposals, ReviewFamily.SKILL)):
            box = BaseCard(title)
            if not proposals:
                box.body.addWidget(line("No proposals recorded", muted=True))
            for p in proposals:
                target = getattr(p, "proposed_material_title", None) or getattr(p, "proposed_name", None) or getattr(p, "proposed_alias", None) or getattr(p, "target_skill_id", None) or "Target in provenance"
                box.body.addWidget(line(f"{label(p.proposal_kind)} · {label(p.status)} · {target}"))
                facts = "Created " + format_time(p.created_at, zone)
                if p.confidence is not None:
                    facts += f" · Proposal confidence {p.confidence:.0%}"
                if p.decided_at:
                    facts += " · Decided " + format_time(p.decided_at, zone)
                if p.decision_actor:
                    facts += " · By " + p.decision_actor
                box.body.addWidget(line(facts, muted=True))
                if p.decision_reason:
                    box.body.addWidget(line(p.decision_reason, muted=True))
                caption = ("Review" if p.status == "PENDING" and
                    (family == ReviewFamily.CLASSIFICATION or p.proposal_kind == "NEW_SKILL")
                    else "View decision" if p.status != "PENDING" else "View details")
                button = QPushButton(caption)
                button.clicked.connect(lambda _, fam=family, pid=p.proposal_id:
                    self.controller.select_proposal_review(fam, pid, Route.MATERIAL_DETAIL))
                box.body.addWidget(button)
            self.cards.addWidget(box)
        technical = BaseCard("Technical provenance")
        toggle = QPushButton("▸ Show IDs and hashes")
        toggle.setCheckable(True)
        technical.body.addWidget(toggle)
        inner = QWidget()
        technical_body = QVBoxLayout(inner)
        technical_body.addWidget(line("Material ID · " + material.material_id, muted=True))
        if material.stable_id:
            technical_body.addWidget(line("Stable ID · " + material.stable_id, muted=True))
        for version in material.versions:
            technical_body.addWidget(line(f"Version {version.version_number} · Source SHA-256 {version.source_artifact.sha256} · Artifact ID {version.source_artifact.artifact_id}", muted=True))
            observation = version.source_artifact.parse_observation
            if observation:
                technical_body.addWidget(line(f"Parse {observation.parse_run_id} · {observation.status}", muted=True))
        inner.hide()
        toggle.toggled.connect(lambda checked: (inner.setVisible(checked),
            toggle.setText(("▾ " if checked else "▸ ") + "Show IDs and hashes")))
        technical.body.addWidget(inner)
        self.cards.addWidget(technical)

    def _learning_step(self, material, current, proposals, course):
        box = BaseCard("Next learning step", variant="accepted")
        observation = current.source_artifact.parse_observation if current else None
        if observation and observation.status == "SUCCESS":
            topics = QPushButton("Erkannte Themen anzeigen")
            topics.clicked.connect(lambda: self._show_topics(current.material_artifact_id, observation.parse_run_id))
            box.body.addWidget(topics)
        if not material.course_stable_id:
            box.body.addWidget(line("Confirm a course for this Material before preparing learning."))
            return box
        if observation is None or observation.status != "SUCCESS":
            message = ("OCR is required before text evidence can be reviewed." if observation and observation.status == "OCR_REQUIRED"
                else "Parsing failed. Review the parse error above before proposing a Skill." if observation and observation.status == "FAILED"
                else "A parsed current source is required before proposing a Skill.")
            box.body.addWidget(line(message))
            if observation and observation.status == "OCR_REQUIRED":
                view = QPushButton("Geparste Seiten ansehen")
                view.clicked.connect(lambda: self._show_parsed_text(observation.parse_run_id,
                    material.material_id, allow_proposal=False))
                box.body.addWidget(view)
            return box
        view = QPushButton("Parsed text ansehen")
        view.clicked.connect(lambda: self._show_parsed_text(observation.parse_run_id, material.material_id,
            allow_proposal=False))
        box.body.addWidget(view)
        if not any(p.proposal_kind == "NEW_SKILL" and p.status == "ACCEPTED"
                and p.accepted_skill_id for p in proposals):
            self._learning_actions(box, material, proposals, course, set())
            return box
        from uniflow.application.gui_learning_bridge import course_skills
        self._learning_generation = getattr(self, "_learning_generation", 0) + 1
        generation = self._learning_generation
        loading = line("Course associations are loading …")
        box.body.addWidget(loading)
        def loaded(skills):
            if generation != self._learning_generation:
                return
            loading.hide()
            self._learning_actions(box, material, proposals, course, {sid for sid, _ in skills})
        self.controller.read_study_data(
            lambda: course_skills(self.controller.config, material.course_stable_id), loaded)
        return box

    def _show_topics(self, link_id, parse_run_id):
        from uniflow.gui.screens.topic_preview import TopicPreviewDialog
        TopicPreviewDialog(self.controller, link_id, parse_run_id, self).exec()

    def _learning_actions(self, box, material, proposals, course, linked):
        current = next(v for v in material.versions if v.is_current)
        observation = current.source_artifact.parse_observation
        accepted = [p for p in proposals if p.proposal_kind == "NEW_SKILL" and p.status == "ACCEPTED"
            and p.accepted_skill_id]
        linked_here = [p for p in accepted if p.accepted_skill_id in linked]
        pending = [p for p in proposals if p.proposal_kind == "NEW_SKILL" and p.status == "PENDING"]
        if linked_here:
            box.body.addWidget(line("Skill confirmed and linked to this course. Choose a learning goal next."))
            button = QPushButton("Lernsession vorbereiten")
            button.clicked.connect(lambda: self._prepare_session(material.course_stable_id, proposals))
        elif accepted:
            skill = accepted[-1]
            box.body.addWidget(line(f"Accepted Skill ID: {skill.accepted_skill_id} · "
                f"Proposal: {skill.proposed_name}. Course association is still needed."))
            button = QPushButton("Skill dem Fach zuordnen")
            button.clicked.connect(lambda: self._associate_skill(material.material_id, course,
                skill.accepted_skill_id, "Accepted Skill"))
        elif pending:
            proposal = pending[-1]
            box.body.addWidget(line("Skill proposal pending. Review the source evidence before accepting it."))
            button = QPushButton("Skill im Review bestätigen")
            button.clicked.connect(lambda: self.controller.select_proposal_review(
                ReviewFamily.SKILL, proposal.proposal_id, Route.MATERIAL_DETAIL))
        else:
            box.body.addWidget(line("Select one unique passage from the parsed text and propose a Skill."))
            button = QPushButton("Skill vorschlagen")
            button.clicked.connect(lambda: self._show_parsed_text(observation.parse_run_id, material.material_id,
                allow_proposal=True))
        box.body.addWidget(button)
        self.action_buttons.append(button)
        self.sync()

    def sync(self):
        busy = self.controller.bridge_busy or self.controller.state.busy[Route.MATERIAL_DETAIL] != Busy.IDLE
        for button in self.action_buttons:
            button.setEnabled(not busy)


    def _show_parsed_text(self, parse_run_id, material_id, *, allow_proposal=True):
        if self.controller.bridge_busy:
            return
        identity = self.controller.workflow_identity()
        dialog = ParsedSelectionDialog(self.controller, parse_run_id, self,
            allow_proposal=allow_proposal)
        if (dialog.exec() == QDialog.DialogCode.Accepted and allow_proposal and dialog.selected
                and identity == self.controller.workflow_identity()):
            unit_id, start, end = dialog.selected
            self.controller.propose_selected_study_skill(material_id, parse_run_id,
                unit_id, start, end, dialog.name.text().strip(), dialog.kind.currentText())

    def _associate_skill(self, material_id, course, skill_id=None, skill_name=None):
        from PySide6.QtWidgets import QMessageBox
        if self.controller.bridge_busy or skill_id is None:
            return
        identity = self.controller.workflow_identity()
        if (QMessageBox.question(self, "Confirm course association",
                f"Link {skill_name} ({skill_id}) to {course}?") == QMessageBox.StandardButton.Yes
                and identity == self.controller.workflow_identity()):
            self.controller.associate_study_skill(material_id, skill_id)

    def _prepare_session(self, course_stable_id, proposals):
        from uniflow.application.gui_learning_bridge import course_skills
        if self.controller.bridge_busy or getattr(self, "_preparing_session", False):
            return
        self._preparing_session = True
        def choices(skills):
            try:
                self._session_choices(course_stable_id, proposals, dict(skills))
            finally:
                self._preparing_session = False
        self.controller.read_study_data(
            lambda: course_skills(self.controller.config, course_stable_id),
            choices, finished=lambda: setattr(self, "_preparing_session", False))

    def _session_choices(self, course_stable_id, proposals, linked):
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        identity = self.controller.workflow_identity()
        skill_ids = {p.accepted_skill_id for p in proposals if p.status == "ACCEPTED"
            and p.proposal_kind == "NEW_SKILL" and p.accepted_skill_id in linked}
        choices = [(sid, linked[sid]) for sid in sorted(skill_ids)]
        if not choices:
            QMessageBox.information(self, "No linked material Skill",
                "Confirm a source-backed Skill proposal and link its accepted Skill to this course first.")
            return
        labels = [f"{name} ({sid})" for sid, name in choices]
        choice, ok = QInputDialog.getItem(self, "Choose material Skill", "Skill:", labels, 0, False)
        if not ok or identity != self.controller.workflow_identity():
            return
        goal, ok = QInputDialog.getMultiLineText(self, "Learning goal", "What do you want to learn?")
        if ok and goal.strip() and identity == self.controller.workflow_identity():
            self.controller.create_study_session(course_stable_id, goal.strip(),
                [choices[labels.index(choice)][0]])
