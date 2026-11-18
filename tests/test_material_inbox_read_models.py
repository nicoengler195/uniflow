"""Phase 4A2B1 reads are scoped, reproducible, and leave canonical rows alone."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from uniflow.application.initializer import initialize_project
from uniflow.application.material_inbox_read_models import get_material_inbox
from uniflow.application.material_manifest import generate_material_manifest
from uniflow.domain.enums import InformationStatus, MaterialState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, ClassificationProposal, Course, DerivedArtifact, Material, MaterialArtifact,
    MaterialExpectationLink, MaterialReleaseRule, ParseRun, RepositoryAcademicLink,
    RepositorySnapshot, RepositorySource, SemanticExtractionRun,
    Semester, SkillProposal, SourceArtifact,
)
from uniflow.schemas.material_inbox_read_v1 import canonical_material_inbox_json
from uniflow.application.academic_classification import rule_fingerprint


AT = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
DAY = date(2026, 10, 12)


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(id="sem", stable_id="S", name="Semester", state=SemesterState.ACTIVE,
                        starts_on=date(2026, 10, 1), ends_on=date(2027, 3, 31), timezone="Europe/Berlin"))
        db.add_all([
            Course(id="c1", semester_id="sem", stable_id="C1", name="One"),
            Course(id="c2", semester_id="sem", stable_id="C2", name="Two"),
            Course(id="other", semester_id="sem", stable_id="C3", name="Three"),
        ])
    yield config, factory
    factory.kw["bind"].dispose()


def _source(sid, char):
    return SourceArtifact(id=sid, sha256=char * 64, original_filename=f"{sid}.pdf",
        original_source_path=f"C:/private/{sid}.pdf", extension=".pdf",
        mime_type="application/pdf", size_bytes=10, stored_path=f"C:/private/{sid}", created_at=AT)


def _parse(artifact_id, run_id, status, *, minute, attempt=1, error=None, warnings=None):
    return ParseRun(id=run_id, source_artifact_id=artifact_id, parser_name="PDF",
        parser_version="1", dependency_version="pypdf", config_json="{}",
        config_sha256="a" * 64, attempt_number=attempt, status=status,
        started_at=AT + timedelta(minutes=minute),
        completed_at=AT + timedelta(minutes=minute, seconds=1),
        error_code=error, error_message="x" * 600 if error else None,
        warning_json=warnings, derived_artifact_id=run_id + "-derived" if status != "FAILED" else None,
        unit_count=1 if status != "FAILED" else None,
        char_count=10 if status == "SUCCESS" else (0 if status == "OCR_REQUIRED" else None))


def _populate(config, factory):
    with factory.begin() as db:
        db.add_all([_source("never", "1"), _source("old", "2"), _source("new", "3"),
                    _source("ocr", "4")])
        db.add_all([
            Material(id="m1", course_id="c1", stable_id="M1", title="Slides",
                     lifecycle_state=MaterialState.DOWNLOADED),
            Material(id="m2", course_id="c2", stable_id="M2", title="Shared",
                     lifecycle_state=MaterialState.DOWNLOADED),
            Material(id="mglobal", title="No course", lifecycle_state=MaterialState.EXPECTED),
        ])
        db.add(Assessment(id="ass", stable_id="ASS", course_id="c1", name="Assessment"))
        db.add_all([
            MaterialArtifact(id="v1", material_id="m1", artifact_id="old", version_number=1,
                is_current=False, linked_at=AT - timedelta(days=3)),
            MaterialArtifact(id="v2", material_id="m1", artifact_id="new", version_number=2,
                is_current=True, linked_at=AT - timedelta(days=2)),
            MaterialArtifact(id="v3", material_id="m2", artifact_id="new", version_number=1,
                is_current=True, linked_at=AT - timedelta(days=1)),
            MaterialArtifact(id="v4", material_id="m2", artifact_id="ocr", version_number=2,
                is_current=False, linked_at=AT),
        ])
        db.add_all([
            _parse("old", "failed", "FAILED", minute=1, error="PDF_ENCRYPTED"),
            _parse("new", "success", "SUCCESS", minute=2),
            _parse("ocr", "ocr-run", "OCR_REQUIRED", minute=3, warnings='["image only"]'),
        ])
        db.add_all([DerivedArtifact(id=sid + "-derived", sha256=char * 64,
            kind="PARSED_TEXT", stored_path=f"C:/derived/{sid}", size_bytes=10)
            for sid, char in (("success", "a"), ("ocr-run", "b"))])
        rule = MaterialReleaseRule(id="rule", stable_id="RULE", course_id="c1",
            payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","material_kind":"SLIDES"}',
            information_status=InformationStatus.CONFIRMED)
        unknown = MaterialReleaseRule(id="unknown", stable_id="UNKNOWN", course_id="c2",
            payload_json='{"cadence":"WEEKLY","material_kind":"EXERCISE"}',
            information_status=InformationStatus.UNKNOWN)
        db.add_all([rule, unknown])
        db.flush()
        fingerprint = rule_fingerprint(rule)
        db.add_all([
            MaterialExpectationLink(id="current-link", material_id="m1", release_rule_id="rule",
                occurrence_date=DAY, expectation_identity="RULE@2026-10-12",
                rule_fingerprint=fingerprint, is_active=True, created_at=AT),
            MaterialExpectationLink(id="stale-link", material_id="m1", release_rule_id="rule",
                occurrence_date=DAY - timedelta(days=7), expectation_identity="RULE@2026-10-05",
                rule_fingerprint="f" * 64, is_active=False, created_at=AT - timedelta(days=2),
                deactivated_at=AT - timedelta(days=1), replacement_link_id="current-link"),
        ])
        db.add_all([
            ClassificationProposal(id="class-pending", proposal_kind="ARTIFACT_TO_EXISTING_MATERIAL",
                status="PENDING", identity_sha256="a" * 64, created_at=AT,
                classifier_name="MANUAL", classifier_version="1",
                subject_source_artifact_id="old", proposed_material_id="m1"),
            ClassificationProposal(id="class-decided", proposal_kind="MATERIAL_EXPECTATION_LINK",
                status="ACCEPTED", identity_sha256="b" * 64, created_at=AT,
                classifier_name="MANUAL", classifier_version="1", subject_material_id="m1",
                proposed_expectation_rule_id="rule", proposed_expectation_date=DAY,
                proposed_expectation_identity="RULE@2026-10-12", decided_at=AT),
            ClassificationProposal(id="class-global", proposal_kind="ARTIFACT_TO_NEW_MATERIAL",
                status="PENDING", identity_sha256="c" * 64, created_at=AT,
                classifier_name="MANUAL", classifier_version="1",
                subject_source_artifact_id="never"),
        ])
        db.add(SemanticExtractionRun(id="run", source_family="DOCUMENT", source_role="LECTURE_MATERIAL",
            input_identity_sha256="d" * 64, material_artifact_id="v2", parse_run_id="success"))
        db.add_all([
            SkillProposal(id="skill-pending", extraction_run_id="run", proposal_kind="NEW_SKILL",
                status="PENDING", identity_sha256="e" * 64, proposed_name="Concept", created_at=AT),
            SkillProposal(id="skill-decided", extraction_run_id="run",
                proposal_kind="EXISTING_SKILL_EVIDENCE", status="REJECTED",
                identity_sha256="f" * 64, created_at=AT, decided_at=AT),
        ])
        db.add_all([
            RepositorySource(id="repo", stable_id="REPO", local_path="C:/secret/repo",
                provider_hint="LOCAL", course_id="c1", assessment_id="ass",
                created_at=AT, last_seen_at=AT),
            RepositorySource(id="unlinked-repo", stable_id="UNLINKED", local_path="C:/secret/unlinked",
                created_at=AT, last_seen_at=AT),
        ])
        db.flush()
        db.add(RepositoryAcademicLink(id="repo-active", repository_source_id="repo", course_id="c1",
            assessment_id="ass", is_active=True, created_at=AT))
        db.flush()
        db.add(RepositoryAcademicLink(id="repo-old", repository_source_id="repo", course_id="c1",
            is_active=False, created_at=AT - timedelta(days=2), deactivated_at=AT,
            replacement_link_id="repo-active"))
        db.add_all([RepositorySnapshot(id=sid, repository_source_id="repo", captured_at=AT + timedelta(minutes=i),
            snapshot_identity=char * 64, policy_version="1", include_untracked=False,
            is_detached=False, is_dirty=False, tracked_file_count=1, untracked_file_count=0,
            deleted_file_count=0, blob_count=1, build_system_hint="UNKNOWN", status="SUCCESS")
            for i, (sid, char) in enumerate((("snap1", "1"), ("snap2", "2")))])


def test_integrated_inbox_manifest_compatibility_and_scope(project):
    config, factory = project
    _populate(config, factory)
    before = generate_material_manifest(config, "S", date(2026, 10, 1), date(2026, 10, 31), AT)
    assert "old" not in {a[0] for a in before.unclassified_artifacts}
    assert len(before.unclassified_artifacts) == 1
    with factory() as db:
        counts_before = {name: db.execute(text(f"select count(*) from {name}")).scalar_one()
            for name in ("source_artifacts", "materials", "material_artifacts", "parse_runs",
                "classification_proposals", "skill_proposals", "skills", "material_expectation_links",
                "repository_sources", "repository_snapshots", "repository_academic_links", "semester_audits")}
    view = get_material_inbox(config, "S", as_of=AT, from_date=date(2026, 10, 1),
        through_date=date(2026, 10, 31))
    assert [a.artifact_id for a in view.project_wide_unassigned_artifacts] == ["never"]
    assert [v.material_artifact_id for v in view.materials[0].versions] == ["v2", "v1"]
    assert view.materials[0].versions[1].deactivated_at is None
    assert view.materials[0].versions[1].source_artifact.parse_observation.error_code == "PDF_ENCRYPTED"
    assert view.materials[0].versions[1].source_artifact.parse_observation.error_message_truncated
    assert view.materials[1].versions[1].source_artifact.parse_observation.status == "OCR_REQUIRED"
    assert [p.proposal_id for p in view.pending_classification_proposals] == ["class-pending"]
    assert [p.proposal_id for p in view.project_wide_unscoped_classification_proposals] == ["class-global"]
    assert [p.proposal_id for p in view.pending_skill_proposals] == ["skill-pending"]
    assert {x.fingerprint_status for x in view.expectation_links} == {"CURRENT", "STALE_RULE"}
    assert view.repositories[0].latest_snapshot.snapshot_id == "snap2"
    assert view.repositories[0].snapshot_count == 2
    assert view.repositories[0].primary_assessment_stable_id == "ASS"
    assert view.repositories[0].academic_links[0].assessment_stable_id == "ASS"
    assert view.project_wide_unlinked_repositories[0].stable_id == "UNLINKED"
    assert len(view.unresolved_rules) == 1
    assert any(x.explicit_link_state == "NO_EXPLICIT_UNIFLOW_LINK" for x in view.projected_expectations)
    raw = canonical_material_inbox_json(view)
    assert raw == canonical_material_inbox_json(get_material_inbox(config, "S", as_of=AT,
        from_date=date(2026, 10, 1), through_date=date(2026, 10, 31)))
    assert b"C:/secret" not in raw and b"C:/private" not in raw
    scoped = get_material_inbox(config, "S", as_of=AT, from_date=date(2026, 10, 1),
        through_date=date(2026, 10, 31), course_id="C1")
    assert [m.material_id for m in scoped.materials] == ["m1"]
    assert not scoped.project_wide_unassigned_artifacts
    assert not scoped.project_wide_unlinked_repositories
    assert not scoped.project_wide_unscoped_materials
    assert generate_material_manifest(config, "S", date(2026, 10, 1), date(2026, 10, 31), AT) == before
    with factory() as db:
        assert counts_before == {name: db.execute(text(f"select count(*) from {name}")).scalar_one()
            for name in counts_before}


def test_input_limits_and_single_connection(project):
    config, factory = project
    with factory.begin() as db:
        db.add(Semester(id="elsewhere", stable_id="OTHER", name="Other",
            state=SemesterState.ACTIVE, timezone="Europe/Berlin"))
        db.add(Course(id="foreign", semester_id="elsewhere", stable_id="FOREIGN", name="Foreign"))
    with pytest.raises(ValueError, match="AS_OF_MUST_BE_AWARE"):
        get_material_inbox(config, "S", as_of=datetime(2026, 10, 14), from_date=DAY, through_date=DAY)
    with pytest.raises(ValueError, match="INVALID_MATERIAL_INBOX_RANGE"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY - timedelta(days=1))
    with pytest.raises(ValueError, match="MATERIAL_INBOX_RANGE_TOO_LARGE"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY + timedelta(days=366))
    with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY, course_id="NOPE")
    with pytest.raises(ValueError, match="UNKNOWN_COURSE_IN_SEMESTER"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY, course_id="FOREIGN")
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_material_inbox(config, "NONE", as_of=AT, from_date=DAY, through_date=DAY)
    connections = []
    statements = []
    def track(dbapi_connection, connection_record, connection_proxy):
        connections.append(dbapi_connection)
    def sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(Engine, "checkout", track)
    event.listen(Engine, "before_cursor_execute", sql)
    try:
        get_material_inbox(config, "S", as_of=AT, from_date=DAY,
            through_date=DAY + timedelta(days=365))
    finally:
        event.remove(Engine, "checkout", track)
        event.remove(Engine, "before_cursor_execute", sql)
    assert len(connections) == 1
    assert any(s == "PRAGMA query_only=ON" for s in statements)
    assert any(s == "BEGIN" for s in statements)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)


def test_latest_parse_warning_integrity_and_version_shapes(project):
    config, factory = project
    with factory.begin() as db:
        db.add(_source("artifact", "a"))
        db.add(Material(id="material", course_id="c1", stable_id="MAT", title="A",
            lifecycle_state=MaterialState.DOWNLOADED))
        db.add(MaterialArtifact(id="link", material_id="material", artifact_id="artifact",
            version_number=1, is_current=True, linked_at=AT))
        db.add_all([
            _parse("artifact", "first", "FAILED", minute=1, error="UNSUPPORTED_ENCODING"),
            ParseRun(id="second", source_artifact_id="artifact", parser_name="OTHER",
                parser_version="2", dependency_version="stdlib", config_json="{}",
                config_sha256="b" * 64, attempt_number=1, status="SUCCESS",
                started_at=AT + timedelta(minutes=2), completed_at=AT + timedelta(minutes=2, seconds=1),
                warning_json='["checked"]', derived_artifact_id="second-derived",
                unit_count=1, char_count=10),
        ])
        db.add(DerivedArtifact(id="second-derived", sha256="c" * 64,
            kind="PARSED_TEXT", stored_path="C:/derived/second", size_bytes=10))
    view = get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY)
    assert view.materials[0].versions[0].source_artifact.parse_observation.parse_run_id == "second"
    assert view.materials[0].versions[0].source_artifact.parse_observation.warnings == ["checked"]
    with factory.begin() as db:
        db.get(ParseRun, "second").warning_json = "{broken"
    with pytest.raises(ValueError, match="PARSE_WARNINGS_INVALID"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY)
    with factory.begin() as db:
        db.get(ParseRun, "second").warning_json = None
        db.get(MaterialArtifact, "link").deactivated_at = AT
    with pytest.raises(ValueError, match="CURRENT_VERSION_DEACTIVATED"):
        get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY)


def test_proposal_variants_and_historical_expectation_links(project):
    config, factory = project
    with factory.begin() as db:
        db.add(Material(id="m", course_id="c1", stable_id="M", title="Material",
            lifecycle_state=MaterialState.EXPECTED))
        db.add(Material(id="m2", course_id="c1", stable_id="M2", title="Second",
            lifecycle_state=MaterialState.EXPECTED))
        db.add(_source("a", "d"))
        db.add(RepositorySource(id="r", stable_id="R", local_path="C:/secret/r",
            created_at=AT, last_seen_at=AT))
        db.flush()
        db.add(RepositoryAcademicLink(id="r-link", repository_source_id="r", course_id="c1",
            is_active=True, created_at=AT))
        rule = MaterialReleaseRule(id="rule", stable_id="R1", course_id="c1",
            payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","material_kind":"SLIDES"}',
            information_status=InformationStatus.CONFIRMED)
        db.add(rule)
        db.flush()
        for i, material_id in enumerate(("m", "m2")):
            db.add(MaterialExpectationLink(id=f"link-{i}", material_id=material_id,
                release_rule_id="rule", occurrence_date=DAY,
                expectation_identity="R1@2026-10-12", rule_fingerprint=rule_fingerprint(rule),
                is_active=True, created_at=AT))
        db.add(MaterialExpectationLink(id="old", material_id="m", release_rule_id="rule",
            occurrence_date=DAY - timedelta(days=7), expectation_identity="R1@2026-10-05",
            rule_fingerprint="f" * 64, is_active=False, created_at=AT - timedelta(days=2),
            deactivated_at=AT - timedelta(days=1), replacement_link_id="link-0"))
        db.add(SemanticExtractionRun(id="run", source_family="MANUAL", source_role="UNKNOWN",
            input_identity_sha256="e" * 64))
        for i, (kind, status) in enumerate((
            ("NEW_SKILL", "PENDING"), ("EXISTING_SKILL_EVIDENCE", "ACCEPTED"),
            ("SKILL_ALIAS", "REJECTED"), ("SKILL_DEPENDENCY", "SUPERSEDED"),
            ("SKILL_COURSE_ASSOCIATION", "PENDING"))):
            db.add(SkillProposal(id=f"sp{i}", extraction_run_id="run", proposal_kind=kind,
                status=status, identity_sha256=f"{i}" * 64, course_id="c1", created_at=AT,
                decided_at=AT if status != "PENDING" else None))
        for i, (kind, status, subject) in enumerate((
            ("ARTIFACT_TO_NEW_MATERIAL", "PENDING", {"subject_source_artifact_id": "a"}),
            ("ARTIFACT_TO_EXISTING_MATERIAL", "ACCEPTED", {"subject_source_artifact_id": "a"}),
            ("REPOSITORY_ACADEMIC_LINK", "REJECTED", {"subject_repository_source_id": "r"}),
            ("MATERIAL_EXPECTATION_LINK", "SUPERSEDED", {"subject_material_id": "m"}))):
            db.add(ClassificationProposal(id=f"cp{i}", proposal_kind=kind, status=status,
                identity_sha256=f"{i + 5}" * 64, classifier_name="MANUAL", classifier_version="1",
                proposed_course_id="c1", created_at=AT,
                decided_at=AT if status != "PENDING" else None, **subject))
    view = get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY)
    assert len(view.pending_classification_proposals) == 1
    assert {p.status for p in view.decided_classification_proposals} == {
        "ACCEPTED", "REJECTED", "SUPERSEDED"}
    assert {p.proposal_kind for p in view.pending_skill_proposals} == {
        "NEW_SKILL", "SKILL_COURSE_ASSOCIATION"}
    assert {p.proposal_kind for p in view.decided_skill_proposals} == {
        "EXISTING_SKILL_EVIDENCE", "SKILL_ALIAS", "SKILL_DEPENDENCY"}
    assert view.projected_expectations[0].active_link_ids == ["link-0", "link-1"]
    assert view.projected_expectations[0].explicit_link_state == "LINKED"
    assert len(view.expectation_links) == 3
    assert next(x for x in view.expectation_links if x.link_id == "old").projected_in_range is False
    filtered = get_material_inbox(config, "S", as_of=AT, from_date=DAY, through_date=DAY,
        course_id="C2")
    assert not filtered.pending_classification_proposals
    assert not filtered.pending_skill_proposals
    assert not filtered.repositories
