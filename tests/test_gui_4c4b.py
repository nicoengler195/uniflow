"""Phase 4C4B review GUI against temporary canonical proposal data."""
from datetime import datetime, timezone
import subprocess

from PySide6.QtWidgets import QLabel, QPushButton
from sqlalchemy import select

from test_gui_4c4a import project, add_material, setup, words, ManualRunner
from test_skill_extraction_foundation import setup_document
from test_academic_classification import setup as setup_classification
from uniflow.application.skill_extraction_foundation import create_semantic_run, create_semantic_evidence, create_skill_proposal
from uniflow.application.semester_read_models import list_semesters
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.application.review_read_models import get_classification_review
from uniflow.application.academic_classification import create_manual_repository_proposal
from uniflow.application.review_workflow import accept_classification
from uniflow.application.repository_snapshots import register_repository, snapshot_repository
from uniflow.gui.routes import Route
from uniflow.gui.state import Busy, ReviewFamily
from uniflow.infrastructure.database.models import (ClassificationProposal, Course, CourseSkill,
    Material, RepositoryAcademicLink, RepositoryFile, RepositorySnapshot,
    SemanticEvidenceSpan, Skill, SourceArtifact)

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def add_proposals(project):
    _, _, factory = project
    with factory.begin() as db:
        db.add(SourceArtifact(id="review-source", sha256="f" * 64, original_filename="sheet.pdf",
            original_source_path="C:/secret/source.pdf", stored_path="C:/secret/blob",
            extension=".pdf", mime_type="application/pdf", size_bytes=42, created_at=NOW))
        db.flush()
        db.add_all([
            ClassificationProposal(id="review-one", proposal_kind="ARTIFACT_TO_NEW_MATERIAL",
                status="PENDING", identity_sha256="1" * 64, created_at=NOW,
                classifier_name="MANUAL", classifier_version="1",
                subject_source_artifact_id="review-source", proposed_course_id="ca",
                proposed_material_stable_id="REVIEW", proposed_material_title="Review sheet",
                proposed_material_type="SHEET"),
            ClassificationProposal(id="review-two", proposal_kind="MATERIAL_EXPECTATION_LINK",
                status="PENDING", identity_sha256="2" * 64, created_at=NOW,
                classifier_name="MANUAL", classifier_version="1", subject_material_id="ma"),
            ClassificationProposal(id="unscoped", proposal_kind="ARTIFACT_TO_NEW_MATERIAL",
                status="PENDING", identity_sha256="3" * 64, created_at=NOW,
                classifier_name="MANUAL", classifier_version="1", subject_source_artifact_id="review-source"),
        ])


def _complete_refresh(runner):
    for op in ("materials", "material_detail", "home", "week", "proposal_review"):
        while any(x[0] == op for x in runner.tasks):
            runner.complete(op)


def test_route_scope_late_read_and_back(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, window, runner = setup(qtbot, project)
    assert [b.text() for b in window.buttons.values()] == ["Dashboard", "Week", "Sessions", "Materials"]
    assert not controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "unscoped", Route.MATERIALS)
    assert not any("Review" in b.text() for b in window.materials.findChildren(QPushButton)
        if b.parentWidget() and "Unscoped" in words(b.parentWidget()))
    assert controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    late = runner.tasks.pop()
    assert controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIALS)
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING
    runner.complete("proposal_review")
    late[3](late[0], late[1], late[2]())
    assert controller.proposal_review.proposal.proposal_id == "review-two"
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.IDLE
    assert window.buttons[Route.MATERIALS].property("selected") is True
    assert Route.PROPOSAL_REVIEW not in window.buttons
    controller.back_from_proposal_review()
    assert controller.state.route == Route.MATERIALS
    controller.select_material("ma")
    runner.complete("material_detail")
    assert controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIAL_DETAIL)
    runner.complete("proposal_review")
    controller.back_from_proposal_review()
    assert controller.state.route == Route.MATERIAL_DETAIL


def test_exact_displayed_token_accept_and_authoritative_readback(qtbot, project, monkeypatch):
    add_material(project)
    add_proposals(project)
    controller, window, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    displayed = controller.proposal_review.review_context_token
    from uniflow.application import review_workflow
    original = review_workflow.accept_classification
    requests = []
    def checked(config, request):
        requests.append(request)
        return original(config, request)
    monkeypatch.setattr(review_workflow, "accept_classification", checked)
    assert controller.decide_proposal("ACCEPT")
    assert not controller.decide_proposal("ACCEPT")
    assert [x[0] for x in runner.tasks].count("review_mutation") == 1
    assert controller.proposal_review.proposal.status == "PENDING"
    assert not any(x[0] == "proposal_review" for x in runner.tasks)
    runner.complete("review_mutation")
    assert requests[0].expected_review_context_token == displayed
    assert controller.review_mutation_busy
    _complete_refresh(runner)
    assert not controller.review_mutation_busy
    assert controller.proposal_review.proposal.status == "ACCEPTED"
    assert any(x.proposal_id == "review-one" for x in controller.materials.decided_classification_proposals)
    assert not any(x.proposal_id == "review-one" for x in controller.materials.pending_classification_proposals)
    assert "C:/secret" not in words(window.proposal_review)


def test_stale_context_no_retry_and_refresh(qtbot, project, monkeypatch):
    add_material(project)
    add_proposals(project)
    controller, window, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    old = controller.proposal_review.review_context_token
    _, _, factory = project
    with factory.begin() as db:
        db.get(Course, "ca").name = "Changed name"
        db.get(Course, "ca").stable_id = "A2"
    from uniflow.application import review_workflow
    original = review_workflow.accept_classification
    calls = []
    def checked(config, request):
        calls.append(request)
        return original(config, request)
    monkeypatch.setattr(review_workflow, "accept_classification", checked)
    assert controller.decide_proposal("ACCEPT")
    error = runner.complete("review_mutation")
    assert error.code == "REVIEW_CONTEXT_CHANGED"
    assert len(calls) == 1
    _complete_refresh(runner)
    assert controller.proposal_review.review_context_token != old
    assert controller.proposal_review.proposal.status == "PENDING"
    assert len(calls) == 1


def test_no_phase3_mutation_imports_and_error_copy():
    from pathlib import Path
    from uniflow.application.workflow_errors import WorkflowCommandError
    from uniflow.gui.presentation import present_error
    root = Path(__file__).parents[1] / "src" / "uniflow" / "gui"
    text = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py"))
    assert "academic_classification" not in text
    assert "skill_extraction_foundation" not in text
    for code in ("REVIEW_CONTEXT_CHANGED", "REVIEW_TARGET_CONFLICT", "REVIEW_REASON_CONFLICT",
            "REVIEW_EVIDENCE_INVALID", "REVIEW_SKILL_NOT_ACTIVE", "REVIEW_PROPOSAL_NOT_PENDING",
            "REVIEW_ACTION_UNSUPPORTED"):
        error = present_error(WorkflowCommandError(code))
        assert error.code == code
        assert "workflow." not in error.message


def _skill_gui(qtbot, tmp_path):
    cfg, factory, parse_id, (link_id, unit_id, _, _) = setup_document(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id, parse_run_id=parse_id)
    span = create_semantic_evidence(cfg, run, unit_id, 4, 14)
    first = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Chain rule", skill_kind="CONCEPT")
    second = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Composition", skill_kind="ABILITY")
    runner = ManualRunner()
    semester = next(x for x in list_semesters(cfg) if x.stable_id == "S")
    controller = GuiController(cfg, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.MATERIALS)
    runner.complete("materials")
    return cfg, factory, controller, window, runner, first, second, run, span


def test_skill_create_search_resolve_and_unsupported(qtbot, tmp_path):
    cfg, factory, controller, window, runner, first, second, run, span = _skill_gui(qtbot, tmp_path)
    assert controller.select_proposal_review(ReviewFamily.SKILL, first, Route.MATERIALS)
    runner.complete("proposal_review")
    assert "chain rule" in words(window.proposal_review)
    assert "Creating or resolving this Skill does not attach" in words(window.proposal_review)
    assert controller.decide_proposal("ACCEPT_NEW")
    runner.complete("review_mutation")
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.status == "ACCEPTED"
    skill_id = controller.proposal_review.proposal.accepted_skill_id
    with factory() as db:
        assert db.query(CourseSkill).count() == 0
        db.add(Skill(id="inactive", name="Chain inactive", kind="CONCEPT", status="MERGED",
            canonical_skill_id=skill_id))
        db.commit()
    assert controller.select_proposal_review(ReviewFamily.SKILL, second, Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.search_review_skills("Chain")
    runner.complete("skill_search")
    assert [c.skill_id for c in controller.skill_choices.choices] == [skill_id]
    assert controller.select_review_skill(skill_id)
    displayed = controller.proposal_review.review_context_token
    assert controller.decide_proposal("RESOLVE_EXISTING")
    runner.complete("review_mutation")
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.accepted_skill_id == skill_id
    assert controller.proposal_review.proposal.decision_action == "RESOLVE_TO_EXISTING_SKILL"
    assert controller.proposal_review.review_context_token == displayed
    with factory() as db:
        assert db.query(CourseSkill).count() == 0
    alias = create_skill_proposal(cfg, run, "SKILL_ALIAS", target_skill_id=skill_id, alias="calculus alias")
    controller.refresh_materials()
    runner.complete("materials")
    assert controller.select_proposal_review(ReviewFamily.SKILL, alias, Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.proposal_review.supported_actions == []
    assert not controller.decide_proposal("ACCEPT_NEW")
    assert not any(b.text() == "Create new Skill" for b in window.proposal_review.action_buttons)


def test_skill_late_search_and_rejection(qtbot, tmp_path):
    _, _, controller, window, runner, first, second, _, _ = _skill_gui(qtbot, tmp_path)
    controller.select_proposal_review(ReviewFamily.SKILL, first, Route.MATERIALS)
    runner.complete("proposal_review")
    controller.search_review_skills("Cha")
    late = runner.tasks.pop()
    controller.search_review_skills("Chain")
    runner.complete("skill_search")
    late[3](late[0], late[1], late[2]())
    assert controller.skill_choices.query == "Chain"
    controller.search_review_skills("Cha")
    late = runner.tasks.pop()
    controller.select_proposal_review(ReviewFamily.SKILL, second, Route.MATERIALS)
    runner.complete("proposal_review")
    late[3](late[0], late[1], late[2]())
    assert controller.skill_choices is None
    assert controller.decide_proposal("REJECT", reason="Not needed")
    runner.complete("review_mutation")
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.status == "REJECTED"
    assert controller.proposal_review.proposal.decision_reason == "Not needed"


def test_reason_conflict_and_idempotent_rejection(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    from uniflow.application.review_workflow import reject_classification
    old = controller.proposal_review
    request = dict(semester_id="S", proposal_id="review-one",
        expected_review_context_token=old.review_context_token, reason="original")
    reject_classification(project[0], request)
    assert controller.decide_proposal("REJECT", reason="different")
    error = runner.complete("review_mutation")
    assert error.code == "REVIEW_REASON_CONFLICT"
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.decision_reason == "original"
    # A fresh pending view can race with an exact same-reason external decision.
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIALS)
    runner.complete("proposal_review")
    token = controller.proposal_review.review_context_token
    reject_classification(project[0], dict(semester_id="S", proposal_id="review-two",
        expected_review_context_token=token, reason="same"))
    assert controller.decide_proposal("REJECT", reason="same")
    result = runner.complete("review_mutation")
    assert result.outcome == "ALREADY_REJECTED"
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.status == "REJECTED"


def test_navigation_during_mutation_preserves_other_review(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.decide_proposal("ACCEPT")
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIALS)
    runner.complete("proposal_review")
    runner.complete("review_mutation")
    _complete_refresh(runner)
    assert controller.state.route == Route.PROPOSAL_REVIEW
    assert controller.proposal_review.proposal.proposal_id == "review-two"
    assert controller.materials is not None


def test_skill_target_conflict_keeps_canonical_target(qtbot, tmp_path):
    cfg, factory, controller, _, runner, first, second, _, _ = _skill_gui(qtbot, tmp_path)
    controller.select_proposal_review(ReviewFamily.SKILL, first, Route.MATERIALS)
    runner.complete("proposal_review")
    controller.decide_proposal("ACCEPT_NEW")
    runner.complete("review_mutation")
    _complete_refresh(runner)
    canonical_id = controller.proposal_review.proposal.accepted_skill_id
    with factory.begin() as db:
        db.add(Skill(id="different-active", name="Different active", kind="CONCEPT", status="ACTIVE"))
    controller.select_proposal_review(ReviewFamily.SKILL, second, Route.MATERIALS)
    runner.complete("proposal_review")
    controller.search_review_skills("Different")
    runner.complete("skill_search")
    controller.select_review_skill("different-active")
    from uniflow.application.review_workflow import resolve_existing_skill
    resolve_existing_skill(cfg, dict(semester_id="S", proposal_id=second,
        expected_review_context_token=controller.proposal_review.review_context_token,
        existing_skill_id=canonical_id))
    assert controller.decide_proposal("RESOLVE_EXISTING")
    error = runner.complete("review_mutation")
    assert error.code == "REVIEW_TARGET_CONFLICT"
    _complete_refresh(runner)
    assert controller.proposal_review.proposal.accepted_skill_id == canonical_id
    assert controller.selected_review_skill_id is None
    assert controller.proposal_review.proposal.status == "ACCEPTED"


def test_multiple_document_evidence_cards_and_truncation_label(qtbot, tmp_path):
    cfg, factory, controller, window, runner, _, _, run, span = _skill_gui(qtbot, tmp_path)
    with factory() as db:
        unit_id = db.get(SemanticEvidenceSpan, span).parsed_unit_id
    other = create_semantic_evidence(cfg, run, unit_id, 0, 3)
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span, other],
        name="Two spans", skill_kind="CONCEPT")
    controller.refresh_materials()
    runner.complete("materials")
    controller.select_proposal_review(ReviewFamily.SKILL, proposal, Route.MATERIALS)
    runner.complete("proposal_review")
    assert "Evidence 1" in words(window.proposal_review)
    assert "Evidence 2" in words(window.proposal_review)
    evidence = controller.proposal_review.evidence
    changed = controller.proposal_review.model_copy(update={"evidence": [
        evidence[0].model_copy(update={"quote_truncated": True}), evidence[1]]})
    window.proposal_review.render(changed)
    assert "Excerpt truncated" in words(window.proposal_review)


def test_repository_review_renders_metadata_without_content(qtbot, tmp_path):
    cfg, factory = setup_classification(tmp_path / "project")
    repository = tmp_path / "secret-local-root"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    (repository / "lesson.txt").write_text("SECRET_TOKEN=private_value", encoding="utf-8")
    register_repository(repository, cfg, stable_id="CODE")
    classification = create_manual_repository_proposal(cfg, "CODE", course="C")
    review = get_classification_review(cfg, "S", classification)
    accept_classification(cfg, dict(semester_id="S", proposal_id=classification,
        expected_review_context_token=review.review_context_token))
    snapshot_repository("CODE", cfg)
    with factory() as db:
        link = db.scalar(select(RepositoryAcademicLink).where(
            RepositoryAcademicLink.classification_proposal_id == classification))
        snapshot = db.scalar(select(RepositorySnapshot).where(
            RepositorySnapshot.repository_source_id == link.repository_source_id))
        entry = db.scalar(select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot.id))
        ids = link.id, snapshot.id, entry.id
    run = create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0],
        repository_snapshot_id=ids[1], repository_state="WORKTREE")
    span = create_semantic_evidence(cfg, run, ids[2], 0, 10)
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Read task", skill_kind="ABILITY")
    runner = ManualRunner()
    semester = next(x for x in list_semesters(cfg) if x.stable_id == "S")
    controller = GuiController(cfg, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    runner.complete("home")
    controller.navigate(Route.MATERIALS)
    runner.complete("materials")
    controller.select_proposal_review(ReviewFamily.SKILL, proposal, Route.MATERIALS)
    runner.complete("proposal_review")
    visible = words(window.proposal_review)
    assert "Repository evidence metadata" in visible
    assert "Verified source bytes are intentionally not displayed" in visible
    for secret in ("secret-local-root", "lesson.txt", "SECRET_TOKEN", "private_value", str(repository)):
        assert secret not in visible


def test_classification_accept_refreshes_loaded_home_week_only(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.refresh_week()
    runner.complete("week")
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    controller.decide_proposal("ACCEPT")
    runner.complete("review_mutation")
    operations = [task[0] for task in runner.tasks]
    assert "materials" in operations and "proposal_review" in operations
    assert "home" in operations and "week" in operations
    assert "sessions" not in operations


def test_authoritative_readback_releases_after_navigating_away(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.decide_proposal("ACCEPT")
    runner.complete("review_mutation")
    old_readback = next(x for x in runner.tasks if x[0] == "proposal_review")
    runner.tasks.remove(old_readback)
    assert controller.review_mutation_busy
    assert controller._review_readback_identity == old_readback[1]
    controller.back_from_proposal_review()
    assert controller.state.route == Route.MATERIALS
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.IDLE
    old_readback[3](old_readback[0], old_readback[1], old_readback[2]())
    assert controller.state.route == Route.MATERIALS
    assert controller.proposal_review is None
    assert not controller.review_mutation_busy
    assert controller._review_readback_identity is None
    runner.complete("materials")
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.decide_proposal("REJECT")
    assert [x[0] for x in runner.tasks].count("review_mutation") == 1


def test_stale_authoritative_readback_cannot_clear_newer_review_read(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    assert controller.decide_proposal("ACCEPT")
    runner.complete("review_mutation")
    old_readback = next(x for x in runner.tasks if x[0] == "proposal_review")
    runner.tasks.remove(old_readback)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-two", Route.MATERIALS)
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING
    old_readback[3](old_readback[0], old_readback[1], old_readback[2]())
    assert not controller.review_mutation_busy
    assert controller._review_readback_identity is None
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING
    assert controller.proposal_review is None
    runner.complete("proposal_review")
    assert controller.proposal_review.proposal.proposal_id == "review-two"
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.IDLE
    assert controller.decide_proposal("REJECT")


def test_conflict_readback_releases_after_navigation(qtbot, project):
    add_material(project)
    add_proposals(project)
    controller, _, runner = setup(qtbot, project)
    controller.select_proposal_review(ReviewFamily.CLASSIFICATION, "review-one", Route.MATERIALS)
    runner.complete("proposal_review")
    with project[2].begin() as db:
        db.get(Course, "ca").stable_id = "A2"
    assert controller.decide_proposal("ACCEPT")
    error = runner.complete("review_mutation")
    assert error.code == "REVIEW_CONTEXT_CHANGED"
    old_readback = next(x for x in runner.tasks if x[0] == "proposal_review")
    runner.tasks.remove(old_readback)
    controller.back_from_proposal_review()
    old_readback[3](old_readback[0], old_readback[1], old_readback[2]())
    assert not controller.review_mutation_busy
    assert controller._review_readback_identity is None
    assert controller.state.busy[Route.PROPOSAL_REVIEW] == Busy.IDLE
    assert not any(x[0] == "review_mutation" for x in runner.tasks)
