"""Render Phase 4C4A widgets with temporary canonical rows."""
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date, datetime, timezone

from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest

from test_gui_4c4a import NOW, ManualRunner, add_material, project
from test_material_inbox_read_models import _parse
from uniflow.application.academic_classification import rule_fingerprint
from uniflow.gui.app import MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.routes import Route
from uniflow.gui.theme.stylesheet import stylesheet
from uniflow.infrastructure.database.models import (ClassificationProposal, DerivedArtifact,
    Material, MaterialArtifact, MaterialExpectationLink, MaterialReleaseRule,
    RepositoryAcademicLink, RepositorySnapshot, RepositorySource, SemanticExtractionRun,
    SkillProposal, SourceArtifact)
from uniflow.domain.enums import MaterialState


def capture(app, window, path, width=1600):
    window.resize(width, 900)
    window.show()
    app.processEvents()
    window.scroll.widget().layout().activate()
    window.scroll.widget().adjustSize()
    QTest.qWait(120)
    app.processEvents()
    window.grab().save(str(path))


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(stylesheet())
    out = Path("runtime/gui_preview")
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        generator = project.__wrapped__(Path(temp))
        item = next(generator)
        config, semester, factory = item
        runner = ManualRunner()
        controller = GuiController(config, semester, runner=runner, clock=lambda: NOW)
        window = MainWindow(controller)
        runner.complete("home")
        controller.navigate(Route.MATERIALS)
        runner.complete("materials")
        capture(app, window, out / "materials_expected_only.png")
        add_material(item)
        with factory.begin() as db:
            db.add_all([SourceArtifact(id="ocr-source", sha256="b" * 64, original_filename="scan.pdf",
                original_source_path="C:/private/scan.pdf", stored_path="C:/private/scan", extension=".pdf",
                mime_type="application/pdf", size_bytes=100, created_at=NOW),
                SourceArtifact(id="fail-source", sha256="c" * 64, original_filename="reading.pdf",
                original_source_path="C:/private/reading.pdf", stored_path="C:/private/reading", extension=".pdf",
                mime_type="application/pdf", size_bytes=100, created_at=NOW),
                SourceArtifact(id="unassigned", sha256="d" * 64, original_filename="unassigned.txt",
                original_source_path="C:/private/unassigned.txt", stored_path="C:/private/unassigned", extension=".txt",
                mime_type="text/plain", size_bytes=20, created_at=NOW),
                SourceArtifact(id="old-source", sha256="9" * 64, original_filename="older.pdf",
                original_source_path="C:/private/older.pdf", stored_path="C:/private/older", extension=".pdf",
                mime_type="application/pdf", size_bytes=30, created_at=NOW),
                Material(id="mc", course_id="cb", stable_id="MC", title="Failed parse example",
                    lifecycle_state=MaterialState.DOWNLOADED)])
            db.flush()
            db.get(MaterialArtifact, "version").version_number = 2
            db.add_all([MaterialArtifact(id="ocr-version", material_id="mb", artifact_id="ocr-source",
                version_number=1, is_current=True, linked_at=NOW),
                MaterialArtifact(id="failed-version", material_id="mc", artifact_id="fail-source",
                version_number=1, is_current=True, linked_at=NOW),
                MaterialArtifact(id="old-version", material_id="ma", artifact_id="old-source",
                version_number=1, is_current=False, linked_at=NOW),
                DerivedArtifact(id="success-derived", sha256="e" * 64, kind="PARSED_TEXT",
                    stored_path="C:/private/success", size_bytes=10),
                DerivedArtifact(id="ocr-derived", sha256="f" * 64, kind="PARSED_TEXT",
                    stored_path="C:/private/ocr", size_bytes=0)])
            db.flush()
            db.add_all([_parse("source", "success", "SUCCESS", minute=1),
                _parse("ocr-source", "ocr", "OCR_REQUIRED", minute=2),
                _parse("fail-source", "failed", "FAILED", minute=3, error="PDF_ENCRYPTED")])
            rule = db.get(MaterialReleaseRule, "rule")
            db.add(MaterialExpectationLink(id="expected-link", material_id="ma", release_rule_id="rule",
                occurrence_date=date(2026, 10, 12), expectation_identity="RULE@2026-10-12",
                rule_fingerprint=rule_fingerprint(rule), is_active=True, created_at=NOW))
            db.add_all([ClassificationProposal(id="class-pending", proposal_kind="MATERIAL_EXPECTATION_LINK",
                status="PENDING", identity_sha256="1" * 64, created_at=NOW,
                classifier_name="PREVIEW", classifier_version="1", subject_material_id="ma"),
                ClassificationProposal(id="class-accepted", proposal_kind="MATERIAL_EXPECTATION_LINK",
                status="ACCEPTED", identity_sha256="2" * 64, created_at=NOW,
                classifier_name="PREVIEW", classifier_version="1", subject_material_id="ma",
                decided_at=NOW, decision_actor="Preview"),
                ClassificationProposal(id="class-rejected", proposal_kind="MATERIAL_EXPECTATION_LINK",
                status="REJECTED", identity_sha256="3" * 64, created_at=NOW,
                classifier_name="PREVIEW", classifier_version="1", subject_material_id="ma",
                decided_at=NOW, decision_actor="Preview")])
            db.add(SemanticExtractionRun(id="extraction", source_family="DOCUMENT",
                source_role="LECTURE_MATERIAL", input_identity_sha256="4" * 64,
                material_artifact_id="version", parse_run_id="success"))
            db.flush()
            db.add_all([SkillProposal(id="skill-pending", extraction_run_id="extraction",
                proposal_kind="NEW_SKILL", status="PENDING", identity_sha256="5" * 64,
                proposed_name="Preview concept", created_at=NOW),
                SkillProposal(id="skill-rejected", extraction_run_id="extraction",
                proposal_kind="NEW_SKILL", status="REJECTED", identity_sha256="6" * 64,
                proposed_name="Old concept", created_at=NOW, decided_at=NOW)])
            db.add_all([RepositorySource(id="repo", stable_id="PREVIEW_REPO", local_path="C:/private/repo",
                provider_hint="LOCAL", created_at=NOW, last_seen_at=NOW),
                RepositorySource(id="unlinked-repo", stable_id="UNLINKED_REPO",
                local_path="C:/private/unlinked", created_at=NOW, last_seen_at=NOW)])
            db.flush()
            db.add(RepositoryAcademicLink(id="repo-link", repository_source_id="repo",
                course_id="ca", is_active=True, created_at=NOW))
            db.add(RepositorySnapshot(id="snapshot", repository_source_id="repo", captured_at=NOW,
                snapshot_identity="7" * 64, policy_version="1", include_untracked=False,
                head_commit_oid="8" * 40, branch_name="main", is_detached=False, is_dirty=False,
                tracked_file_count=12, untracked_file_count=0, deleted_file_count=0,
                blob_count=12, build_system_hint="PYTHON", status="SUCCESS"))
        controller.refresh_materials()
        runner.complete("materials")
        capture(app, window, out / "materials_1600.png")
        capture(app, window, out / "materials_1440.png", 1440)
        window.scroll.verticalScrollBar().setValue(window.scroll.verticalScrollBar().maximum())
        capture(app, window, out / "materials_project_wide.png")
        repository_button = next(b for b in window.materials.findChildren(QPushButton)
            if b.text().startswith("▸ Repository · PREVIEW_REPO"))
        repository_button.click()
        app.processEvents()
        window.scroll.ensureWidgetVisible(repository_button)
        capture(app, window, out / "repository_inspector.png")
        for material_id, filename in (("ma", "material_detail_success.png"),
                ("mb", "material_detail_ocr_required.png"),
                ("mc", "material_detail_failed_parse.png")):
            controller.select_material(material_id)
            runner.complete("material_detail")
            capture(app, window, out / filename)
            controller.back_to_materials()
        window.close()
        try:
            next(generator)
        except StopIteration:
            pass
    print("Rendered", ", ".join(p.name for p in out.glob("material*.png")))


if __name__ == "__main__":
    main()
