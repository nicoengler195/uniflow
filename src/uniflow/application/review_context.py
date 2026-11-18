"""Canonical, content-free context identities for explicit proposal decisions."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from uniflow.infrastructure.database.models import (
    Assessment, ClassificationProposal, Course, Material, MaterialArtifact,
    MaterialExpectationLink, MaterialReleaseRule, ParseRun, ParsedDocument,
    RepositoryAcademicLink, RepositoryBlob, RepositoryFile, RepositorySnapshot,
    RepositorySource, SemanticEvidenceSpan, SemanticExtractionRun, Semester, Skill,
    SkillProposal, SkillProposalEvidence, SourceArtifact,
)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _fields(row, *names):
    return {name: getattr(row, name) for name in names} if row is not None else None


def classification_review_context(db, proposal: ClassificationProposal) -> str:
    """Exclude decision state/time so an exact historical replay keeps its token."""
    p = proposal
    body = {"kind": p.proposal_kind, "subject": [p.subject_source_artifact_id,
        p.subject_repository_source_id, p.subject_material_id], "targets": [
        p.proposed_course_id, p.proposed_assessment_id, p.proposed_material_id,
        p.proposed_material_stable_id, p.proposed_material_title, p.proposed_material_type,
        p.proposed_expectation_rule_id, p.proposed_expectation_date,
        p.proposed_expectation_identity, p.replaces_link_id],
        "classifier": [p.classifier_name, p.classifier_version, p.classifier_config_sha256,
            p.confidence, json.loads(p.evidence_json) if p.evidence_json else None]}
    if p.subject_source_artifact_id:
        body["artifact"] = _fields(db.get(SourceArtifact, p.subject_source_artifact_id), "id", "sha256", "size_bytes")
        if p.replaces_link_id:
            body["replacement"] = _fields(db.get(MaterialArtifact, p.replaces_link_id),
                "id", "material_id", "artifact_id", "is_current", "replacement_link_id")
    if p.proposed_material_id or p.subject_material_id:
        mid = p.proposed_material_id or p.subject_material_id
        material = db.get(Material, mid)
        body["material"] = _fields(material, "id", "stable_id", "course_id", "title", "material_type")
        body["material_links"] = [_fields(x, "id", "artifact_id", "is_current")
            for x in db.scalars(select(MaterialArtifact).where(MaterialArtifact.material_id == mid)
                .order_by(MaterialArtifact.id)) if x.classification_proposal_id != p.id]
    if p.proposed_course_id:
        body["course"] = _fields(db.get(Course, p.proposed_course_id), "id", "stable_id", "semester_id")
    if p.proposed_assessment_id:
        body["assessment"] = _fields(db.get(Assessment, p.proposed_assessment_id), "id", "course_id")
    if p.subject_repository_source_id:
        rid = p.subject_repository_source_id
        # Course/Assessment pointers mirror the accepted link and are an effect
        # of this decision, not an input reviewed before it.
        body["repository"] = _fields(db.get(RepositorySource, rid), "id", "stable_id")
        body["repository_links"] = [_fields(x, "id", "course_id", "assessment_id", "is_active", "replacement_link_id")
            for x in db.scalars(select(RepositoryAcademicLink).where(
                RepositoryAcademicLink.repository_source_id == rid).order_by(RepositoryAcademicLink.id))
            if x.classification_proposal_id != p.id]
    if p.proposed_expectation_rule_id:
        rule = db.get(MaterialReleaseRule, p.proposed_expectation_rule_id)
        body["rule"] = (_fields(rule, "id", "stable_id", "course_id", "information_status") |
            {"payload": json.loads(rule.payload_json)}) if rule else None
        course = db.get(Course, rule.course_id) if rule else None
        semester = db.get(Semester, course.semester_id) if course else None
        body["rule_semester"] = _fields(semester, "id", "stable_id", "starts_on", "ends_on", "timezone")
        if semester and p.proposed_expectation_date:
            from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
            manifest = _generate_material_manifest_in_transaction(db, semester.stable_id,
                p.proposed_expectation_date, p.proposed_expectation_date,
                datetime(2000, 1, 1, tzinfo=timezone.utc), course.stable_id)
            body["occurrence_valid"] = p.proposed_expectation_identity in {x.id for x in manifest.expectations}
        body["expectation_links"] = [_fields(x, "id", "is_active", "rule_fingerprint", "replacement_link_id")
            for x in db.scalars(select(MaterialExpectationLink).where(
                MaterialExpectationLink.material_id == p.subject_material_id,
                MaterialExpectationLink.expectation_identity == p.proposed_expectation_identity)
                .order_by(MaterialExpectationLink.id)) if x.classification_proposal_id != p.id]
    return _hash(body)


def skill_review_context(db, proposal: SkillProposal) -> str:
    p = proposal
    run = db.get(SemanticExtractionRun, p.extraction_run_id)
    body = {"kind": p.proposal_kind, "proposed": _fields(p, "proposed_skill_id", "proposed_name",
        "proposed_description", "proposed_kind", "proposed_domain", "target_skill_id",
        "prerequisite_skill_id", "dependent_skill_id", "course_id", "proposed_alias", "confidence"),
        "run": _fields(run, "id", "source_family", "source_role", "extractor_name",
            "extractor_version", "extractor_config_sha256", "input_identity_sha256",
            "material_artifact_id", "parse_run_id", "repository_academic_link_id",
            "repository_snapshot_id", "repository_state")}
    if run is not None and run.source_family == "DOCUMENT":
        link = db.get(MaterialArtifact, run.material_artifact_id)
        artifact = db.get(SourceArtifact, link.artifact_id) if link else None
        parse = db.get(ParseRun, run.parse_run_id)
        doc = db.get(ParsedDocument, run.parse_run_id)
        body["scope"] = [_fields(link, "id", "material_id", "artifact_id", "is_current",
            "classification_proposal_id"),
            _fields(artifact, "id", "sha256"), _fields(parse, "id", "source_artifact_id", "status"),
            _fields(doc, "parse_run_id", "normalized_text_sha256")]
        if link and link.classification_proposal_id:
            body["classification_status"] = _fields(db.get(ClassificationProposal,
                link.classification_proposal_id), "id", "status")
    elif run is not None and run.source_family == "REPOSITORY":
        link = db.get(RepositoryAcademicLink, run.repository_academic_link_id)
        snap = db.get(RepositorySnapshot, run.repository_snapshot_id)
        body["scope"] = [_fields(link, "id", "repository_source_id", "course_id", "assessment_id",
            "is_active", "classification_proposal_id"),
            _fields(snap, "id", "repository_source_id", "snapshot_identity")]
        if link and link.classification_proposal_id:
            body["classification_status"] = _fields(db.get(ClassificationProposal,
                link.classification_proposal_id), "id", "status")
    pairs = list(db.scalars(select(SkillProposalEvidence).where(
        SkillProposalEvidence.proposal_id == p.id).order_by(SkillProposalEvidence.evidence_span_id)))
    body["spans"] = []
    for pair in pairs:
        span = db.get(SemanticEvidenceSpan, pair.evidence_span_id)
        item = _fields(span, "id", "extraction_run_id", "material_artifact_id", "parsed_unit_id",
            "char_start", "char_end", "repository_academic_link_id", "repository_file_id",
            "repository_blob_id", "repository_state", "byte_start", "byte_end", "span_sha256")
        if span and span.repository_file_id:
            file = db.get(RepositoryFile, span.repository_file_id)
            blob = db.get(RepositoryBlob, span.repository_blob_id)
            item["file"] = _fields(file, "id", "snapshot_id", "path", "head_blob_id",
                "index_blob_id", "working_blob_id")
            item["blob"] = _fields(blob, "id", "sha256", "size_bytes")
        body["spans"].append(item)
    for sid in (p.target_skill_id, p.prerequisite_skill_id, p.dependent_skill_id):
        if sid:
            body.setdefault("skills", []).append(_fields(db.get(Skill, sid), "id", "status", "kind"))
    return _hash(body)
