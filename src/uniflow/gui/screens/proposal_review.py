"""Evidence-first exact proposal review, driven only by detached 4B3 DTOs."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QLineEdit, QPushButton, QVBoxLayout, QWidget

from uniflow.gui.components import BaseCard, EmptyState, PrimaryButton, StatusChip
from uniflow.gui.components.artwork import PackagedArtwork
from uniflow.gui.components.review_dialogs import ReviewConfirmDialog, ReviewRejectDialog
from uniflow.gui.presentation import format_time, label
from uniflow.gui.routes import Route
from uniflow.gui.screens.materials import clear, line
from uniflow.gui.state import ReviewFamily
from uniflow.gui.visual_identity import course_artwork_asset, resolve_course_visual


def _fact(body, title, value):
    if value is not None and value != "":
        body.addWidget(line(f"{title} · {value}"))


class ProposalReviewScreen(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setObjectName("proposalReviewScreen")
        self.controller = controller
        self.review = None
        self.search_box = None
        self.picker = None
        self.action_buttons = []
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(280)
        self.search_timer.timeout.connect(self._search)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(15)
        back = QPushButton("← Back")
        back.clicked.connect(controller.back_from_proposal_review)
        self.body.addWidget(back)
        self.cards = QVBoxLayout()
        self.cards.setSpacing(15)
        self.body.addLayout(self.cards)
        self.body.addStretch()

    def _search(self):
        if self.search_box is not None:
            self.controller.search_review_skills(self.search_box.text())

    def _query_changed(self):
        self.search_timer.stop()
        self.controller.invalidate_review_skill_search()
        if self.search_box and self.search_box.text():
            self.search_timer.start()

    def sync(self):
        busy = self.controller.review_mutation_busy
        for button in self.action_buttons:
            button.setEnabled(not busy)
        if self.search_box:
            self.search_box.setEnabled(not busy)

    def render(self, review):
        self.review = review
        self.search_timer.stop()
        self.search_box = None
        self.picker = None
        self.action_buttons = []
        clear(self.cards)
        if review is None:
            self.cards.addWidget(EmptyState("Loading proposal", "The exact review and evidence are being loaded."))
            return
        p, source = review.proposal, review.source
        zone = review.semester.timezone
        family = self.controller.state.selected_review_family
        header = BaseCard(hero=True)
        course_stable = None
        if self.controller.material_detail and self.controller.state.review_return_route == Route.MATERIAL_DETAIL:
            course_stable = self.controller.material_detail.material.course_stable_id
        elif self.controller.materials:
            material_id = source.material_id or getattr(p, "subject_material_id", None) or getattr(p, "proposed_material_id", None)
            material = next((m for m in self.controller.materials.materials if m.material_id == material_id), None)
            course_stable = material.course_stable_id if material else None
        if course_stable:
            art = PackagedArtwork(course_artwork_asset(resolve_course_visual(course_stable, course_stable)), mode="thumbnail")
            art.setFixedHeight(70)
            header.body.addWidget(art)
        header.body.addWidget(line(f"{label(family.value)} proposal · {label(p.proposal_kind)}"))
        header.body.addWidget(StatusChip(label(p.status)))
        _fact(header.body, "Semester", review.semester.stable_id)
        _fact(header.body, "Course", course_stable)
        self.cards.addWidget(header)

        facts = BaseCard("Proposed decision")
        if family == ReviewFamily.CLASSIFICATION:
            for title, value in (("Source artifact", p.subject_source_artifact_id),
                    ("Repository", source.repository_stable_id), ("Material", p.subject_material_id),
                    ("Course ID", p.proposed_course_id), ("Assessment ID", p.proposed_assessment_id),
                    ("Existing Material ID", p.proposed_material_id),
                    ("New Material", p.proposed_material_title), ("Material type", p.proposed_material_type),
                    ("Expectation", p.proposed_expectation_identity),
                    ("Expectation date", p.proposed_expectation_date),
                    ("Replacement link", p.replaces_link_id)):
                _fact(facts.body, title, value)
        else:
            for title, value in (("Proposed Skill", p.proposed_name), ("Kind", p.proposed_kind),
                    ("Domain", p.proposed_domain), ("Description", p.proposed_description),
                    ("Target Skill ID", p.target_skill_id), ("Alias", p.proposed_alias),
                    ("Prerequisite Skill ID", p.prerequisite_skill_id),
                    ("Dependent Skill ID", p.dependent_skill_id), ("Course ID", p.course_id)):
                _fact(facts.body, title, value)
        if p.confidence is not None:
            facts.body.addWidget(line(f"Proposal confidence {p.confidence:.0%}", muted=True))
        self.cards.addWidget(facts)

        evidence = BaseCard("Evidence and source")
        if source.source_family == "REPOSITORY":
            evidence.body.addWidget(line("Repository evidence metadata"))
            _fact(evidence.body, "Repository", source.repository_stable_id)
            _fact(evidence.body, "Snapshot", source.repository_snapshot_identity)
            evidence.body.addWidget(line("Verified source bytes are intentionally not displayed.", muted=True))
        elif source.source_family == "MANUAL":
            evidence.body.addWidget(line("Manual provenance"))
        else:
            _fact(evidence.body, "Source", source.original_filename or source.source_family.title())
        if family == ReviewFamily.SKILL:
            if review.evidence:
                for index, item in enumerate(review.evidence, 1):
                    card = BaseCard(f"Evidence {index}", variant="secondary")
                    if item.source_family == "DOCUMENT":
                        quote = line(item.quote_excerpt or "")
                        card.body.addWidget(quote)
                        _fact(card.body, "Source", item.source_label)
                        if item.quote_truncated:
                            card.body.addWidget(line("Excerpt truncated", muted=True))
                    else:
                        card.body.addWidget(line("Repository evidence metadata"))
                        _fact(card.body, "State", item.repository_state)
                        if item.byte_start is not None and item.byte_end is not None:
                            card.body.addWidget(line(f"Byte range {item.byte_start}–{item.byte_end}", muted=True))
                        card.body.addWidget(line("Verified source bytes are intentionally not displayed.", muted=True))
                    evidence.body.addWidget(card)
            else:
                evidence.body.addWidget(line("No quoted Evidence is attached to this proposal.", muted=True))
        else:
            evidence.body.addWidget(line("Classification source metadata is available; no raw classifier evidence is displayed.", muted=True))
        self.cards.addWidget(evidence)

        if p.status == "PENDING" and review.supported_actions:
            decision = BaseCard("Your decision")
            if family == ReviewFamily.CLASSIFICATION:
                self._add_button(decision.body, "Accept proposal", "ACCEPT", primary=True)
                self._add_button(decision.body, "Reject", "REJECT")
            else:
                decision.body.addWidget(line("Creating or resolving this Skill does not attach it to the Course. Course association is reviewed separately.", muted=True))
                self._add_button(decision.body, "Create new Skill", "ACCEPT_NEW", primary=True)
                reveal = QPushButton("Resolve to existing Skill")
                decision.body.addWidget(reveal)
                self.action_buttons.append(reveal)
                self.picker = QWidget()
                picker_body = QVBoxLayout(self.picker)
                self.search_box = QLineEdit()
                self.search_box.setMaxLength(120)
                self.search_box.setPlaceholderText("Search for an existing Skill")
                self.search_box.textChanged.connect(self._query_changed)
                picker_body.addWidget(self.search_box)
                self.choices_body = QVBoxLayout()
                picker_body.addLayout(self.choices_body)
                self.picker.hide()
                reveal.clicked.connect(lambda: self.picker.setVisible(not self.picker.isVisible()))
                decision.body.addWidget(self.picker)
                self._add_button(decision.body, "Reject", "REJECT")
            self.cards.addWidget(decision)
        elif p.status == "PENDING":
            self.cards.addWidget(EmptyState("Details only", "This proposal type can be viewed here. No decision is available for it."))

        history = BaseCard("Decision history")
        history.body.addWidget(line(label(p.status)))
        for title, value in (("Action", getattr(p, "decision_action", None)),
                ("Accepted Skill ID", getattr(p, "accepted_skill_id", None)),
                ("Reason", p.decision_reason), ("Actor", p.decision_actor),
                ("Decided", format_time(p.decided_at, zone) if p.decided_at else None),
                ("Superseded by", p.superseded_by_proposal_id)):
            _fact(history.body, title, value)
        self.cards.addWidget(history)

        technical = BaseCard("Technical provenance")
        toggle = QPushButton("▸ Show IDs and hashes")
        toggle.setCheckable(True)
        technical.body.addWidget(toggle)
        inner = QWidget()
        details = QVBoxLayout(inner)
        for title, value in (("Proposal ID", p.proposal_id),
                ("Review context identity", review.review_context_token),
                ("Source SHA-256", source.source_sha256),
                ("Classifier", getattr(p, "classifier_name", None)),
                ("Classifier version", getattr(p, "classifier_version", None)),
                ("Evidence JSON SHA-256", getattr(p, "evidence_json_sha256", None)),
                ("Extraction run", source.extraction_run_id),
                ("Extraction attempt", source.extraction_attempt_id),
                ("Extractor", source.extractor_name), ("Extractor version", source.extractor_version),
                ("Provider", source.provider_name), ("Model", source.model_name),
                ("Adapter", source.adapter_name), ("Adapter version", source.adapter_version)):
            _fact(details, title, value)
        if family == ReviewFamily.SKILL:
            for item in review.evidence:
                _fact(details, "Evidence span", item.span_id)
                _fact(details, "Span SHA-256", item.span_sha256)
        inner.hide()
        toggle.toggled.connect(lambda checked: (inner.setVisible(checked),
            toggle.setText(("▾ " if checked else "▸ ") + "Show IDs and hashes")))
        technical.body.addWidget(inner)
        self.cards.addWidget(technical)
        self.sync()

    def _add_button(self, body, title, action, *, primary=False):
        button = PrimaryButton(title) if primary else QPushButton(title)
        button.clicked.connect(lambda: self._decide(action))
        body.addWidget(button)
        self.action_buttons.append(button)

    def render_choices(self, choices):
        if self.picker is None:
            return
        clear(self.choices_body)
        if choices is None:
            self.choices_body.addWidget(line("Search for an existing Skill.", muted=True))
            return
        if not choices.choices:
            self.choices_body.addWidget(line("No active Skills found.", muted=True))
        for choice in choices.choices:
            selected = self.controller.selected_review_skill_id == choice.skill_id
            button = QPushButton(("✓ " if selected else "") + choice.name +
                f" · {choice.kind}" + (f" · {choice.domain}" if choice.domain else ""))
            button.clicked.connect(lambda _, sid=choice.skill_id: self.controller.select_review_skill(sid))
            self.choices_body.addWidget(button)
            if choice.description_excerpt:
                self.choices_body.addWidget(line(choice.description_excerpt, muted=True))
        chosen = next((c for c in choices.choices if c.skill_id == self.controller.selected_review_skill_id), None)
        if chosen:
            self.choices_body.addWidget(line(f"Selected: {chosen.name} · {chosen.skill_id}"))
            resolve = PrimaryButton("Resolve to selected Skill")
            resolve.clicked.connect(lambda: self._decide("RESOLVE_EXISTING"))
            resolve.setEnabled(not self.controller.review_mutation_busy)
            self.choices_body.addWidget(resolve)
            self.action_buttons.append(resolve)

    def _decide(self, action):
        if self.controller.review_mutation_busy or self.review is None:
            return
        identity = self.controller.workflow_identity()
        review = self.review
        selected_skill = self.controller.selected_review_skill_id
        def still_current():
            return (identity == self.controller.workflow_identity() and self.review is review
                and self.controller.proposal_review is review
                and selected_skill == self.controller.selected_review_skill_id)
        if action == "REJECT":
            dialog = ReviewRejectDialog(self)
            if dialog.exec() == QDialog.Accepted and still_current():
                self.controller.decide_proposal(action, reason=dialog.value())
            return
        target = self.review.proposal.proposed_name or self.review.proposal.proposed_material_title or label(self.review.proposal.proposal_kind)
        if action == "RESOLVE_EXISTING":
            choices = self.controller.skill_choices.choices if self.controller.skill_choices else []
            chosen = next((c for c in choices if c.skill_id == self.controller.selected_review_skill_id), None)
            if chosen is None:
                return
            explanation = f"Resolve {target} to the existing Skill {chosen.name} ({chosen.skill_id})?"
            title = "Resolve proposal"
        elif action == "ACCEPT_NEW":
            title, explanation = "Create new Skill", f"Create the proposed Skill {target}?"
        else:
            title, explanation = "Accept proposal", f"Accept the displayed {target} proposal?"
        dialog = ReviewConfirmDialog(title, explanation, title, self)
        if dialog.exec() == QDialog.Accepted and still_current():
            self.controller.decide_proposal(action)
