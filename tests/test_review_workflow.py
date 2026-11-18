"""Phase 4B3 exact review reads and facade decisions use temporary projects."""
from __future__ import annotations

from datetime import date, datetime, timezone
import subprocess

import pytest
from sqlalchemy import select

from test_academic_classification import setup as setup_classification
from test_skill_extraction_foundation import setup_document
from uniflow.application.academic_classification import (
    accept_classification_proposal,
    create_manual_artifact_proposal, create_manual_expectation_proposal,
    create_manual_repository_proposal,
    list_unclassified_artifacts,
)
from uniflow.application.material_detail_read_models import get_material_detail
from uniflow.application.review_read_models import get_classification_review, get_skill_review
from uniflow.application.review_workflow import (
    accept_classification, accept_new_skill, reject_classification, reject_skill,
    resolve_existing_skill,
)
from uniflow.application.skill_choice_read_models import search_active_skills
from uniflow.application.skill_extraction_foundation import (
    accept_skill_proposal,
    create_semantic_evidence, create_semantic_run, create_skill_proposal,
)
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Material, MaterialArtifact, MaterialReleaseRule,
    RepositoryAcademicLink, RepositoryFile, RepositorySnapshot, Semester, Skill,
    SkillProposal, SourceArtifact,
)
from uniflow.application.document_parsing import parse_source_artifact
from uniflow.application.change_feed_read_models import get_change_feed
from uniflow.application.material_intake import import_material
from uniflow.application.repository_snapshots import register_repository, snapshot_repository


def _code(exc):
    return exc.value.code


def test_classification_detail_replay_and_expectation_stale(tmp_path):
    cfg, factory = setup_classification(tmp_path)
    artifact = list_unclassified_artifacts(cfg)[0]
    proposal = create_manual_artifact_proposal(cfg, artifact.id, course="C",
        new_material_stable_id="M", title="Sheet", material_type="SHEET")
    review = get_classification_review(cfg, "S", proposal)
    assert review.supported_actions == ["ACCEPT", "REJECT"]
    assert review.source.source_sha256 == artifact.sha256
    request = {"semester_id": "S", "proposal_id": proposal,
        "expected_review_context_token": review.review_context_token}
    assert accept_classification(cfg, request).outcome == "ACCEPTED"
    assert accept_classification(cfg, request).outcome == "ALREADY_ACCEPTED"
    assert get_classification_review(cfg, "S", proposal).review_context_token == review.review_context_token
    with factory() as db:
        material = db.scalar(select(Material).where(Material.stable_id == "M"))
        material_id = material.id
    detail = get_material_detail(cfg, "S", material_id, as_of=datetime.now(timezone.utc),
        from_date=date(2026, 10, 12), through_date=date(2026, 10, 31))
    assert detail.material.title == "Sheet"
    assert detail.material.current_version_id
    assert [x.proposal_id for x in detail.classification_proposals] == [proposal]
    assert "private" not in detail.model_dump_json().lower()
    with pytest.raises(ValueError, match="UNKNOWN_SEMESTER"):
        get_material_detail(cfg, "other", material_id, as_of=datetime.now(timezone.utc),
            from_date=date(2026, 10, 12), through_date=date(2026, 10, 31))
    expectation = create_manual_expectation_proposal(cfg, "M", "R@2026-10-16")
    old = get_classification_review(cfg, "S", expectation)
    with factory.begin() as db:
        rule = db.scalar(select(MaterialReleaseRule).where(MaterialReleaseRule.stable_id == "R"))
        rule.payload_json = '{"cadence":"WEEKLY","weekday":"FRIDAY","material_kind":"NOTES"}'
    stale = {"semester_id": "S", "proposal_id": expectation,
        "expected_review_context_token": old.review_context_token}
    with pytest.raises(WorkflowCommandError) as exc:
        accept_classification(cfg, stale)
    assert _code(exc) == "REVIEW_CONTEXT_CHANGED"
    fresh = get_classification_review(cfg, "S", expectation)
    assert fresh.review_context_token != old.review_context_token
    assert accept_classification(cfg, stale | {"expected_review_context_token": fresh.review_context_token}).outcome == "ACCEPTED"
    linked = get_material_detail(cfg, "S", material_id, as_of=datetime.now(timezone.utc),
        from_date=date(2026, 10, 12), through_date=date(2026, 10, 31))
    assert [x.expectation_id for x in linked.projected_expectations] == ["R@2026-10-16"]
    same = create_manual_artifact_proposal(cfg, artifact.id, material="M")
    same_token = get_classification_review(cfg, "S", same).review_context_token
    assert accept_classification(cfg, {"semester_id": "S", "proposal_id": same,
        "expected_review_context_token": same_token}).decision_action == "ALREADY_LINKED"
    repository = create_manual_repository_proposal(cfg, "REPO", course="C")
    repository_token = get_classification_review(cfg, "S", repository).review_context_token
    repository_request = {"semester_id": "S", "proposal_id": repository,
        "expected_review_context_token": repository_token}
    assert accept_classification(cfg, repository_request).decision_action == "LINK_REPOSITORY"
    assert accept_classification(cfg, repository_request).outcome == "ALREADY_ACCEPTED"


def test_classification_rejection_reason(tmp_path):
    cfg, _ = setup_classification(tmp_path)
    artifact = list_unclassified_artifacts(cfg)[0]
    pid = create_manual_artifact_proposal(cfg, artifact.id, course="C",
        new_material_stable_id="M", title="Sheet", material_type="SHEET")
    token = get_classification_review(cfg, "S", pid).review_context_token
    request = {"semester_id": "S", "proposal_id": pid,
        "expected_review_context_token": token, "reason": "not this course"}
    assert reject_classification(cfg, request).outcome == "REJECTED"
    assert reject_classification(cfg, request).outcome == "ALREADY_REJECTED"
    with pytest.raises(WorkflowCommandError) as exc:
        reject_classification(cfg, request | {"reason": "different"})
    assert _code(exc) == "REVIEW_REASON_CONFLICT"


def test_owning_services_recheck_stale_tokens_inside_mutation(tmp_path):
    cfg, factory = setup_classification(tmp_path / "classification")
    artifact = list_unclassified_artifacts(cfg)[0]
    proposal = create_manual_artifact_proposal(cfg, artifact.id, course="C",
        new_material_stable_id="M", title="Sheet", material_type="SHEET")
    token = get_classification_review(cfg, "S", proposal).review_context_token
    with factory.begin() as db:
        db.get(Course, db.scalar(select(Course.id).where(Course.stable_id == "C"))).stable_id = "C2"
    with pytest.raises(ValueError, match="REVIEW_CONTEXT_CHANGED"):
        accept_classification_proposal(cfg, proposal, expected_review_context_token=token)
    with factory() as db:
        from uniflow.infrastructure.database.models import ClassificationProposal
        assert db.get(ClassificationProposal, proposal).status == "PENDING"

    cfg, factory, run, span, link_id = _skill_setup(tmp_path / "skill")
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Chain rule", skill_kind="CONCEPT")
    token = get_skill_review(cfg, "S", proposal).review_context_token
    with factory.begin() as db:
        db.get(MaterialArtifact, link_id).is_current = False
    with pytest.raises(ValueError, match="REVIEW_CONTEXT_CHANGED"):
        accept_skill_proposal(cfg, proposal, expected_review_context_token=token)
    with factory() as db:
        assert db.get(SkillProposal, proposal).status == "PENDING"


def _skill_setup(tmp_path):
    cfg, factory, parse_id, (link_id, unit_id, _, _) = setup_document(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id,
        parse_run_id=parse_id)
    span = create_semantic_evidence(cfg, run, unit_id, 4, 14)
    return cfg, factory, run, span, link_id


def test_skill_review_quote_create_resolve_replay_and_choice(tmp_path):
    cfg, factory, run, span, _ = _skill_setup(tmp_path)
    first = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Chain rule", skill_kind="CONCEPT")
    review = get_skill_review(cfg, "S", first)
    assert get_skill_review(cfg, "S", first).review_context_token == review.review_context_token
    assert review.evidence[0].quote_excerpt == "chain rule"
    assert review.evidence[0].quote_truncated is False
    assert review.supported_actions == ["ACCEPT_NEW", "RESOLVE_EXISTING", "REJECT"]
    request = {"semester_id": "S", "proposal_id": first,
        "expected_review_context_token": review.review_context_token}
    accepted = accept_new_skill(cfg, request)
    assert accepted.outcome == "ACCEPTED" and accepted.accepted_skill_id
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    feed = get_change_feed(cfg, "S", as_of=now + timedelta(seconds=1),
        since=datetime(2020, 1, 1, tzinfo=timezone.utc), through=now + timedelta(seconds=1))
    kinds = {x.kind for x in feed.events}
    assert {"CLASSIFICATION_DECIDED", "SKILL_PROPOSAL_DECIDED"} <= kinds
    assert accept_new_skill(cfg, request).outcome == "ALREADY_ACCEPTED"
    with factory() as db:
        assert db.query(CourseSkill).count() == 0
    choices = search_active_skills(cfg, query="Chain", limit=1)
    assert [x.skill_id for x in choices.choices] == [accepted.accepted_skill_id]
    second = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Derivative", skill_kind="ABILITY")
    token = get_skill_review(cfg, "S", second).review_context_token
    resolve = {"semester_id": "S", "proposal_id": second,
        "expected_review_context_token": token, "existing_skill_id": accepted.accepted_skill_id}
    assert resolve_existing_skill(cfg, resolve).decision_action == "RESOLVE_TO_EXISTING_SKILL"
    assert resolve_existing_skill(cfg, resolve).outcome == "ALREADY_ACCEPTED"
    with factory.begin() as db:
        db.add(Skill(id="different-active", name="Different active", kind="CONCEPT", status="ACTIVE"))
    with pytest.raises(WorkflowCommandError) as exc:
        resolve_existing_skill(cfg, resolve | {"existing_skill_id": "different-active"})
    assert _code(exc) == "REVIEW_TARGET_CONFLICT"
    with pytest.raises(WorkflowCommandError) as exc:
        accept_new_skill(cfg, {k: v for k, v in resolve.items() if k != "existing_skill_id"})
    assert _code(exc) == "REVIEW_TARGET_CONFLICT"
    with factory() as db:
        assert db.query(Skill).count() == 2


def test_skill_reject_unsupported_and_stale(tmp_path):
    cfg, factory, run, span, link_id = _skill_setup(tmp_path)
    pid = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Unused", skill_kind="CONCEPT")
    token = get_skill_review(cfg, "S", pid).review_context_token
    req = {"semester_id": "S", "proposal_id": pid,
        "expected_review_context_token": token, "reason": "not useful"}
    assert reject_skill(cfg, req).outcome == "REJECTED"
    assert reject_skill(cfg, req).outcome == "ALREADY_REJECTED"
    with pytest.raises(WorkflowCommandError) as exc:
        reject_skill(cfg, req | {"reason": "different"})
    assert _code(exc) == "REVIEW_REASON_CONFLICT"
    other = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Still pending", skill_kind="CONCEPT")
    old = get_skill_review(cfg, "S", other)
    with factory.begin() as db:
        db.get(MaterialArtifact, link_id).is_current = False
    with pytest.raises(WorkflowCommandError) as exc:
        accept_new_skill(cfg, {"semester_id": "S", "proposal_id": other,
            "expected_review_context_token": old.review_context_token})
    assert _code(exc) == "REVIEW_CONTEXT_CHANGED"
    with factory() as db:
        assert db.get(SkillProposal, other).status == "PENDING"


def test_unsupported_skill_kind_and_choice_bounds(tmp_path):
    cfg, factory, run, span, _ = _skill_setup(tmp_path)
    first = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span],
        name="Chain", skill_kind="CONCEPT")
    token = get_skill_review(cfg, "S", first).review_context_token
    skill_id = accept_new_skill(cfg, {"semester_id": "S", "proposal_id": first,
        "expected_review_context_token": token}).accepted_skill_id
    alias_run = create_semantic_run(cfg, "MANUAL")
    alias = create_skill_proposal(cfg, alias_run, "SKILL_ALIAS", target_skill_id=skill_id,
        alias="Other wording")
    review = get_skill_review(cfg, "S", alias)
    assert review.supported_actions == []
    with pytest.raises(WorkflowCommandError) as exc:
        accept_new_skill(cfg, {"semester_id": "S", "proposal_id": alias,
            "expected_review_context_token": review.review_context_token})
    assert _code(exc) == "REVIEW_ACTION_UNSUPPORTED"
    with factory.begin() as db:
        db.add(Skill(id="inactive", name="Archived", kind="CONCEPT", status="MERGED"))
    assert "inactive" not in {x.skill_id for x in search_active_skills(cfg, limit=50).choices}
    for kwargs in ({"limit": 51}, {"limit": 0}, {"query": "x" * 121}, {"query": " space "}):
        with pytest.raises(ValueError):
            search_active_skills(cfg, **kwargs)


def test_bounded_quote_verifies_full_span(tmp_path):
    cfg, factory = setup_classification(tmp_path)
    source = tmp_path / "long.txt"
    source.write_text("A" * 300, encoding="utf-8")
    imported = import_material(source, cfg)
    parsed = parse_source_artifact(imported.sha256, cfg)
    p = create_manual_artifact_proposal(cfg, parsed.source_artifact_id, course="C",
        new_material_stable_id="LONG", title="Long", material_type="LECTURE")
    token = get_classification_review(cfg, "S", p).review_context_token
    accept_classification(cfg, {"semester_id": "S", "proposal_id": p,
        "expected_review_context_token": token})
    with factory() as db:
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.classification_proposal_id == p))
        from uniflow.infrastructure.database.models import ParsedUnit, SemanticEvidenceSpan
        unit = db.scalar(select(ParsedUnit).where(ParsedUnit.parse_run_id == parsed.parse_run_id))
        link_id, unit_id = link.id, unit.id
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id,
        parse_run_id=parsed.parse_run_id)
    span_id = create_semantic_evidence(cfg, run, unit_id, 0, 300)
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span_id],
        name="Long fact", skill_kind="CONCEPT")
    review = get_skill_review(cfg, "S", proposal)
    assert review.evidence[0].quote_excerpt == "A" * 240
    assert review.evidence[0].quote_truncated
    with factory.begin() as db:
        db.get(SemanticEvidenceSpan, span_id).span_sha256 = "0" * 64
    with pytest.raises(ValueError, match="REVIEW_EVIDENCE_INVALID"):
        get_skill_review(cfg, "S", proposal)


def test_repository_review_is_metadata_only(tmp_path):
    cfg, factory = setup_classification(tmp_path / "project")
    repo_path = tmp_path / "secret-local-root"
    repo_path.mkdir()
    subprocess.run(["git", "-C", str(repo_path), "init", "-q"], check=True)
    (repo_path / "lesson.txt").write_text("SECRET_TOKEN=private_value", encoding="utf-8")
    register_repository(repo_path, cfg, stable_id="CODE")
    classification = create_manual_repository_proposal(cfg, "CODE", course="C")
    token = get_classification_review(cfg, "S", classification).review_context_token
    accept_classification(cfg, {"semester_id": "S", "proposal_id": classification,
        "expected_review_context_token": token})
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
    read = get_skill_review(cfg, "S", proposal)
    body = read.model_dump_json()
    assert read.evidence[0].quote_excerpt is None
    assert read.source.repository_snapshot_identity
    assert "SECRET_TOKEN" not in body and "private_value" not in body
    assert "secret-local-root" not in body and str(repo_path) not in body


def test_material_detail_isolates_unrelated_corruption(tmp_path):
    cfg, factory, _, (link_id, _, _, _) = setup_document(tmp_path)
    with factory() as db:
        selected = db.get(MaterialArtifact, link_id).material_id
    with factory.begin() as db:
        foreign = Semester(stable_id="FOREIGN", name="Foreign")
        course = Course(stable_id="FOREIGN_C", name="Foreign course", semester=foreign)
        db.add(course)
        db.flush()
        source = SourceArtifact(id="bad-source", sha256="a" * 64,
            original_filename="bad.pdf", original_source_path="C:/private/bad.pdf",
            stored_path="C:/private/bad", size_bytes=1)
        material = Material(id="bad-material", stable_id="BAD", course_id=course.id, title="Bad")
        db.add_all([source, material])
        db.flush()
        db.add(MaterialArtifact(material_id=material.id, artifact_id=source.id,
            version_number=1, is_current=True, deactivated_at=datetime.now(timezone.utc)))
    good = get_material_detail(cfg, "S", selected, as_of=datetime.now(timezone.utc),
        from_date=date(2026, 10, 1), through_date=date(2026, 10, 31))
    assert good.material.material_id == selected
    with pytest.raises(ValueError, match="MATERIAL_NOT_IN_SEMESTER"):
        get_material_detail(cfg, "FOREIGN", selected, as_of=datetime.now(timezone.utc),
            from_date=date(2026, 10, 1), through_date=date(2026, 10, 31))


def test_unknown_service_value_error_propagates(tmp_path, monkeypatch):
    cfg, _ = setup_classification(tmp_path)
    artifact = list_unclassified_artifacts(cfg)[0]
    p = create_manual_artifact_proposal(cfg, artifact.id, course="C",
        new_material_stable_id="M", title="Sheet", material_type="SHEET")
    token = get_classification_review(cfg, "S", p).review_context_token
    from uniflow.application import academic_classification
    def surprise(*args, **kwargs):
        raise ValueError("UNEXPECTED_INTERNAL_CODE")
    monkeypatch.setattr(academic_classification, "accept_classification_proposal", surprise)
    with pytest.raises(ValueError, match="UNEXPECTED_INTERNAL_CODE"):
        accept_classification(cfg, {"semester_id": "S", "proposal_id": p,
            "expected_review_context_token": token})


def test_strict_review_request(tmp_path):
    cfg, _ = setup_classification(tmp_path)
    with pytest.raises(WorkflowCommandError) as exc:
        accept_new_skill(cfg, {"semester_id": "S", "proposal_id": "p",
            "expected_review_context_token": "0" * 64, "unexpected": True})
    assert _code(exc) == "REVIEW_INPUT_INVALID"
