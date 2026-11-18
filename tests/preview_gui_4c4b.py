"""Actual-widget Phase 4C4B previews using only temporary canonical projects."""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest
from sqlalchemy import select

from test_gui_4c4a import NOW, ManualRunner, add_material, project
from test_gui_4c4b import add_proposals
from test_skill_extraction_foundation import setup_document
from test_review_workflow import setup_classification
from uniflow.application.academic_classification import create_manual_repository_proposal
from uniflow.application.review_read_models import get_classification_review
from uniflow.application.review_workflow import accept_classification
from uniflow.application.repository_snapshots import register_repository, snapshot_repository
from uniflow.application.semester_read_models import list_semesters
from uniflow.application.skill_extraction_foundation import create_semantic_run, create_semantic_evidence, create_skill_proposal
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.routes import Route
from uniflow.gui.state import ReviewFamily
from uniflow.gui.theme.stylesheet import stylesheet
from uniflow.infrastructure.database.models import RepositoryAcademicLink, RepositorySnapshot, RepositoryFile, Skill


OUT = Path("runtime/gui_preview")


def capture(app, window, name, width=1600, focus=None, bottom=False):
    window.resize(width, 900)
    window.show()
    window.scroll.verticalScrollBar().setValue(0)
    app.processEvents()
    window.scroll.widget().layout().activate()
    window.scroll.widget().adjustSize()
    if focus is not None:
        window.scroll.ensureWidgetVisible(focus)
    if bottom:
        window.scroll.verticalScrollBar().setValue(window.scroll.verticalScrollBar().maximum())
    QTest.qWait(100)
    app.processEvents()
    window.grab().save(str(OUT / name))


def shell(app, cfg):
    semester = next(x for x in list_semesters(cfg) if x.stable_id == "S")
    runner = ManualRunner()
    controller = GuiController(cfg, semester, runner=runner, clock=lambda: NOW)
    window = MainWindow(controller)
    runner.complete("home")
    controller.navigate(Route.MATERIALS)
    runner.complete("materials")
    return controller, window, runner


def review(controller, runner, family, pid):
    assert controller.select_proposal_review(family, pid, Route.MATERIALS)
    runner.complete("proposal_review")


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(stylesheet())
    OUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        generator = project.__wrapped__(Path(temp))
        item = next(generator)
        add_material(item)
        add_proposals(item)
        controller, window, runner = shell(app, item[0])
        inbox_review = next(b for b in window.materials.findChildren(QPushButton) if b.text() == "Review")
        capture(app, window, "materials_4c4b.png", focus=inbox_review)
        controller.select_material("ma")
        runner.complete("material_detail")
        detail_review = next(b for b in window.material_detail.findChildren(QPushButton) if b.text() == "Review")
        capture(app, window, "material_detail_4c4b.png", focus=detail_review, bottom=True)
        controller.back_to_materials()
        review(controller, runner, ReviewFamily.CLASSIFICATION, "review-one")
        capture(app, window, "classification_review_pending.png")
        capture(app, window, "classification_review_pending_1440.png", 1440)
        controller.decide_proposal("ACCEPT")
        runner.complete("review_mutation")
        for op in ("materials", "proposal_review", "home"):
            while any(x[0] == op for x in runner.tasks):
                runner.complete(op)
        capture(app, window, "classification_review_accepted.png")
        window.close()
        try:
            next(generator)
        except StopIteration:
            pass
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        cfg, factory, parse_id, (link_id, unit_id, _, _) = setup_document(Path(temp))
        run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id, parse_run_id=parse_id)
        span = create_semantic_evidence(cfg, run, unit_id, 4, 14)
        first = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
            name="Chain rule", skill_kind="CONCEPT", description="Apply the chain rule to composite functions.")
        second = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
            name="Composite derivatives", skill_kind="ABILITY")
        controller, window, runner = shell(app, cfg)
        review(controller, runner, ReviewFamily.SKILL, first)
        capture(app, window, "skill_review_new_document.png")
        capture(app, window, "skill_review_new_document_1440.png", 1440)
        controller.decide_proposal("ACCEPT_NEW")
        runner.complete("review_mutation")
        for op in ("materials", "proposal_review"):
            while any(x[0] == op for x in runner.tasks):
                runner.complete(op)
        capture(app, window, "skill_review_decided.png")
        target = controller.proposal_review.proposal.accepted_skill_id
        review(controller, runner, ReviewFamily.SKILL, second)
        window.proposal_review.picker.show()
        window.proposal_review.search_box.setText("Chain")
        controller.search_review_skills("Chain")
        runner.complete("skill_search")
        controller.select_review_skill(target)
        capture(app, window, "skill_review_resolve_picker.png", focus=window.proposal_review.picker)
        alias = create_skill_proposal(cfg, run, "SKILL_ALIAS", target_skill_id=target, alias="chain")
        controller.refresh_materials()
        runner.complete("materials")
        review(controller, runner, ReviewFamily.SKILL, alias)
        capture(app, window, "skill_review_unsupported.png")
        window.close()
        factory.kw["bind"].dispose()
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        cfg, factory = setup_classification(root / "project")
        repo_path = root / "secret-local-root"
        repo_path.mkdir()
        subprocess.run(["git", "-C", str(repo_path), "init", "-q"], check=True)
        (repo_path / "lesson.txt").write_text("SECRET_TOKEN=private_value", encoding="utf-8")
        register_repository(repo_path, cfg, stable_id="CODE")
        classification = create_manual_repository_proposal(cfg, "CODE", course="C")
        read = get_classification_review(cfg, "S", classification)
        accept_classification(cfg, {"semester_id": "S", "proposal_id": classification,
            "expected_review_context_token": read.review_context_token})
        snapshot_repository("CODE", cfg)
        with factory() as db:
            link = db.scalar(select(RepositoryAcademicLink).where(
                RepositoryAcademicLink.classification_proposal_id == classification))
            snap = db.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.repository_source_id == link.repository_source_id))
            entry = db.scalar(select(RepositoryFile).where(RepositoryFile.snapshot_id == snap.id))
            ids = link.id, snap.id, entry.id
        run = create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0],
            repository_snapshot_id=ids[1], repository_state="WORKTREE")
        span = create_semantic_evidence(cfg, run, ids[2], 0, 10)
        proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
            name="Read task", skill_kind="ABILITY")
        controller, window, runner = shell(app, cfg)
        review(controller, runner, ReviewFamily.SKILL, proposal)
        capture(app, window, "skill_review_repository_metadata.png")
        window.close()
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
