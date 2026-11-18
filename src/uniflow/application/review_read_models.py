"""Exact, metadata-safe proposal review reads in one query-only transaction."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.document_parsing import _verify_file
from uniflow.application.material_inbox_read_models import _classification_dto, _skill_dto
from uniflow.application.review_context import classification_review_context, skill_review_context
from uniflow.infrastructure.database.models import (
    Assessment, ClassificationProposal, Course, DerivedArtifact, Material,
    MaterialArtifact, MaterialReleaseRule, ParsedDocument, ParsedUnit, ParseRun,
    RepositoryAcademicLink, RepositorySnapshot, RepositorySource, SemanticEvidenceSpan,
    SemanticExtractionRun, SemanticExtractionAttempt, SkillProposal, SkillProposalEvidence, SourceArtifact,
)
from uniflow.schemas.review_read_v1 import (
    ClassificationReviewV1, EvidencePreviewV1, ReviewSourceV1, SkillReviewV1,
)

DOCUMENT_QUOTE_MAX_CHARS = 240


def _safe_filename(value: str) -> str | None:
    return value if value and not any(ch in value for ch in ("/", "\\", ":", "\x00", "\n", "\r")) else None


def _scope(db, semester_id, candidate_course_ids):
    semester, _, _, _, summary = _context(db, semester_id, datetime.now(timezone.utc))
    known = {db.get(Course, cid).semester_id for cid in candidate_course_ids if cid and db.get(Course, cid)}
    if known and (semester.id not in known or len(known) != 1):
        raise ValueError("PROPOSAL_NOT_IN_SEMESTER")
    return summary


def _classification_courses(db, p):
    ids = set()
    for cid in (p.proposed_course_id,
                db.get(Material, p.subject_material_id).course_id if p.subject_material_id and db.get(Material, p.subject_material_id) else None,
                db.get(Material, p.proposed_material_id).course_id if p.proposed_material_id and db.get(Material, p.proposed_material_id) else None,
                db.get(MaterialReleaseRule, p.proposed_expectation_rule_id).course_id if p.proposed_expectation_rule_id and db.get(MaterialReleaseRule, p.proposed_expectation_rule_id) else None,
                db.get(Assessment, p.proposed_assessment_id).course_id if p.proposed_assessment_id and db.get(Assessment, p.proposed_assessment_id) else None):
        if cid:
            ids.add(cid)
    if p.subject_source_artifact_id:
        ids.update(x.course_id for x in db.scalars(select(Material).join(MaterialArtifact,
            MaterialArtifact.material_id == Material.id).where(
            MaterialArtifact.artifact_id == p.subject_source_artifact_id)) if x.course_id)
    if p.subject_repository_source_id:
        ids.update(x.course_id for x in db.scalars(select(RepositoryAcademicLink).where(
            RepositoryAcademicLink.repository_source_id == p.subject_repository_source_id)))
    return ids


def _classification_source(db, p):
    if p.subject_source_artifact_id:
        a = db.get(SourceArtifact, p.subject_source_artifact_id)
        if a is None:
            raise ValueError("REVIEW_SOURCE_MISSING")
        return ReviewSourceV1(source_family="ARTIFACT", source_artifact_id=a.id,
            source_sha256=a.sha256, original_filename=_safe_filename(a.original_filename))
    if p.subject_repository_source_id:
        repo = db.get(RepositorySource, p.subject_repository_source_id)
        if repo is None:
            raise ValueError("REVIEW_SOURCE_MISSING")
        snap = db.scalar(select(RepositorySnapshot).where(
            RepositorySnapshot.repository_source_id == repo.id).order_by(
            RepositorySnapshot.captured_at.desc(), RepositorySnapshot.id).limit(1))
        return ReviewSourceV1(source_family="REPOSITORY", repository_source_id=repo.id,
            repository_stable_id=repo.stable_id, repository_snapshot_id=snap.id if snap else None,
            repository_snapshot_identity=snap.snapshot_identity if snap else None)
    if p.subject_material_id:
        if db.get(Material, p.subject_material_id) is None:
            raise ValueError("REVIEW_SOURCE_MISSING")
        return ReviewSourceV1(source_family="MATERIAL", material_id=p.subject_material_id)
    raise ValueError("REVIEW_SOURCE_MISSING")


def get_classification_review(config, semester_id: str, proposal_id: str) -> ClassificationReviewV1:
    with _read_transaction(config) as db:
        p = db.get(ClassificationProposal, proposal_id)
        if p is None:
            raise ValueError("REVIEW_PROPOSAL_NOT_FOUND")
        summary = _scope(db, semester_id, _classification_courses(db, p))
        return ClassificationReviewV1(semester=summary, proposal=_classification_dto(p),
            review_context_token=classification_review_context(db, p),
            supported_actions=["ACCEPT", "REJECT"] if p.status == "PENDING" else [],
            source=_classification_source(db, p))


def _verified_document_unit(db, config, unit: ParsedUnit) -> str:
    document = db.get(ParsedDocument, unit.parse_run_id)
    if document is None:
        raise ValueError("REVIEW_EVIDENCE_INVALID")
    derived = db.get(DerivedArtifact, document.derived_artifact_id)
    if derived is None:
        raise ValueError("REVIEW_EVIDENCE_INVALID")
    path = Path(derived.stored_path)
    try:
        _verify_file(path, derived.sha256, derived.size_bytes)
    except ValueError as exc:
        raise ValueError("REVIEW_EVIDENCE_INVALID") from exc
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != document.normalized_text_sha256:
        raise ValueError("REVIEW_EVIDENCE_INVALID")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("REVIEW_EVIDENCE_INVALID") from exc
    if len(text) != document.char_count or not 0 <= unit.char_start <= unit.char_end <= len(text):
        raise ValueError("REVIEW_EVIDENCE_INVALID")
    value = text[unit.char_start:unit.char_end]
    if hashlib.sha256(value.encode("utf-8")).hexdigest() != unit.text_sha256 or len(value) != unit.char_count:
        raise ValueError("REVIEW_EVIDENCE_INVALID")
    return value


def _skill_evidence(db, config, p, run):
    result = []
    for pair in db.scalars(select(SkillProposalEvidence).where(
            SkillProposalEvidence.proposal_id == p.id).order_by(SkillProposalEvidence.evidence_span_id)):
        span = db.get(SemanticEvidenceSpan, pair.evidence_span_id)
        if span is None or span.extraction_run_id != run.id:
            raise ValueError("REVIEW_EVIDENCE_INVALID")
        if run.source_family == "DOCUMENT":
            unit = db.get(ParsedUnit, span.parsed_unit_id)
            link = db.get(MaterialArtifact, run.material_artifact_id)
            parse = db.get(ParseRun, run.parse_run_id)
            if (unit is None or link is None or parse is None or
                    unit.parse_run_id != run.parse_run_id or span.material_artifact_id != link.id or
                    parse.source_artifact_id != link.artifact_id):
                raise ValueError("REVIEW_EVIDENCE_INVALID")
            value = _verified_document_unit(db, config, unit)
            if span.char_start is None or span.char_end is None or not 0 <= span.char_start < span.char_end <= len(value):
                raise ValueError("REVIEW_EVIDENCE_INVALID")
            full = value[span.char_start:span.char_end]
            if hashlib.sha256(full.encode("utf-8")).hexdigest() != span.span_sha256:
                raise ValueError("REVIEW_EVIDENCE_INVALID")
            result.append(EvidencePreviewV1(span_id=span.id, source_family="DOCUMENT",
                span_sha256=span.span_sha256, parsed_unit_id=unit.id,
                source_label=unit.source_label, char_start=span.char_start,
                char_end=span.char_end, quote_excerpt=full[:DOCUMENT_QUOTE_MAX_CHARS],
                quote_truncated=len(full) > DOCUMENT_QUOTE_MAX_CHARS))
        elif run.source_family == "REPOSITORY":
            if (span.repository_academic_link_id != run.repository_academic_link_id or
                    span.repository_state != run.repository_state):
                raise ValueError("REVIEW_EVIDENCE_INVALID")
            result.append(EvidencePreviewV1(span_id=span.id, source_family="REPOSITORY",
                span_sha256=span.span_sha256, repository_file_id=span.repository_file_id,
                repository_blob_id=span.repository_blob_id, repository_state=span.repository_state,
                byte_start=span.byte_start, byte_end=span.byte_end))
        else:
            raise ValueError("REVIEW_EVIDENCE_INVALID")
    return result


def get_skill_review(config, semester_id: str, proposal_id: str) -> SkillReviewV1:
    with _read_transaction(config) as db:
        p = db.get(SkillProposal, proposal_id)
        if p is None:
            raise ValueError("REVIEW_PROPOSAL_NOT_FOUND")
        run = db.get(SemanticExtractionRun, p.extraction_run_id)
        if run is None:
            raise ValueError("REVIEW_RUN_MISSING")
        ids = {p.course_id} if p.course_id else set()
        if run.material_artifact_id:
            link = db.get(MaterialArtifact, run.material_artifact_id)
            material = db.get(Material, link.material_id) if link else None
            if material and material.course_id:
                ids.add(material.course_id)
        if run.repository_academic_link_id:
            link = db.get(RepositoryAcademicLink, run.repository_academic_link_id)
            if link:
                ids.add(link.course_id)
        summary = _scope(db, semester_id, ids)
        if run.source_family == "DOCUMENT":
            link = db.get(MaterialArtifact, run.material_artifact_id)
            artifact = db.get(SourceArtifact, link.artifact_id) if link else None
            if artifact is None:
                raise ValueError("REVIEW_SOURCE_MISSING")
            source = ReviewSourceV1(source_family="DOCUMENT", source_artifact_id=artifact.id,
                source_sha256=artifact.sha256, original_filename=_safe_filename(artifact.original_filename),
                material_id=link.material_id, parse_run_id=run.parse_run_id)
        elif run.source_family == "REPOSITORY":
            link = db.get(RepositoryAcademicLink, run.repository_academic_link_id)
            repo = db.get(RepositorySource, link.repository_source_id) if link else None
            snap = db.get(RepositorySnapshot, run.repository_snapshot_id)
            if repo is None or snap is None or snap.repository_source_id != repo.id:
                raise ValueError("REVIEW_SOURCE_MISSING")
            source = ReviewSourceV1(source_family="REPOSITORY", repository_source_id=repo.id,
                repository_stable_id=repo.stable_id, repository_snapshot_id=snap.id,
                repository_snapshot_identity=snap.snapshot_identity)
        else:
            source = ReviewSourceV1(source_family="MANUAL")
        attempt = db.get(SemanticExtractionAttempt, p.extraction_attempt_id) if p.extraction_attempt_id else None
        if p.extraction_attempt_id and (attempt is None or attempt.run_id != run.id):
            raise ValueError("REVIEW_ATTEMPT_INVALID")
        source = source.model_copy(update={"extraction_run_id": run.id,
            "extraction_attempt_id": p.extraction_attempt_id, "source_role": run.source_role,
            "extractor_name": run.extractor_name, "extractor_version": run.extractor_version,
            "provider_name": attempt.provider_name if attempt else run.provider_name,
            "model_name": attempt.model_name if attempt else run.model_name,
            "adapter_name": attempt.adapter_name if attempt else None,
            "adapter_version": attempt.adapter_version if attempt else None,
            "attempt_status": attempt.status if attempt else None,
            "input_identity_sha256": run.input_identity_sha256})
        evidence = _skill_evidence(db, config, p, run)
        return SkillReviewV1(semester=summary,
            proposal=_skill_dto(p, [x.span_id for x in evidence]),
            review_context_token=skill_review_context(db, p),
            supported_actions=["ACCEPT_NEW", "RESOLVE_EXISTING", "REJECT"]
                if p.status == "PENDING" and p.proposal_kind == "NEW_SKILL" else [],
            source=source, evidence=evidence)
