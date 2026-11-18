"""One exact logical Material, detached in a single read-only snapshot."""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import select

from uniflow.application.academic_classification import rule_fingerprint
from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
from uniflow.application.material_inbox_read_models import (
    _classification_dto, _material_dtos, _skill_dto, _source_dtos, _utc,
)
from uniflow.infrastructure.database.models import (
    ClassificationProposal, Course, Material, MaterialArtifact, MaterialExpectationLink,
    MaterialReleaseRule, SemanticExtractionRun, SkillProposal, SkillProposalEvidence,
    SourceArtifact,
)
from uniflow.schemas.material_detail_read_v1 import MaterialDetailV1
from uniflow.schemas.material_inbox_read_v1 import ExpectationLinkReadV1, ProjectedExpectationReadV1


def get_material_detail(config, semester_id: str, material_id: str, *, as_of: datetime,
                        from_date: date, through_date: date) -> MaterialDetailV1:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if type(from_date) is not date or type(through_date) is not date or from_date > through_date:
        raise ValueError("INVALID_MATERIAL_DETAIL_RANGE")
    if (through_date - from_date).days >= 366:
        raise ValueError("MATERIAL_DETAIL_RANGE_TOO_LARGE")
    with _read_transaction(config) as db:
        semester, _, local, _, summary = _context(db, semester_id, as_of)
        material = db.get(Material, material_id)
        if material is None:
            raise ValueError("UNKNOWN_MATERIAL")
        course = db.get(Course, material.course_id) if material.course_id else None
        if course is None or course.semester_id != semester.id:
            raise ValueError("MATERIAL_NOT_IN_SEMESTER")
        links = list(db.scalars(select(MaterialArtifact).where(
            MaterialArtifact.material_id == material.id)))
        artifacts = list(db.scalars(select(SourceArtifact).where(
            SourceArtifact.id.in_({x.artifact_id for x in links})))) if links else []
        if any(any(ch in a.original_filename for ch in ("/", "\\", ":", "\x00", "\n", "\r"))
                for a in artifacts):
            raise ValueError("UNSAFE_SOURCE_FILENAME")
        detail = _material_dtos([material], links, _source_dtos(db, artifacts), {course.id: course})[material.id]
        link_ids = {x.id for x in links}
        artifact_ids = {x.artifact_id for x in links}
        classification_ids = {x.classification_proposal_id for x in links if x.classification_proposal_id}
        classification = []
        for p in db.scalars(select(ClassificationProposal)):
            if (p.id in classification_ids or p.subject_material_id == material.id or
                    p.proposed_material_id == material.id or
                    (p.proposal_kind == "ARTIFACT_TO_NEW_MATERIAL" and p.status == "ACCEPTED" and
                     p.proposed_material_stable_id == material.stable_id and
                     p.subject_source_artifact_id in artifact_ids)):
                classification.append(_classification_dto(p))
        classification.sort(key=lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id))
        run_ids = {x.id for x in db.scalars(select(SemanticExtractionRun).where(
            SemanticExtractionRun.material_artifact_id.in_(link_ids)))} if link_ids else set()
        skills = []
        for p in db.scalars(select(SkillProposal).where(SkillProposal.extraction_run_id.in_(run_ids))):
            spans = sorted(x.evidence_span_id for x in db.scalars(select(SkillProposalEvidence).where(
                SkillProposalEvidence.proposal_id == p.id)))
            skills.append(_skill_dto(p, spans))
        skills.sort(key=lambda x: (x.decided_at or x.created_at, x.proposal_kind, x.proposal_id))
        rules = {x.id: x for x in db.scalars(select(MaterialReleaseRule).where(
            MaterialReleaseRule.course_id == course.id))}
        stored_links = list(db.scalars(select(MaterialExpectationLink).where(
            MaterialExpectationLink.material_id == material.id)))
        manifest = _generate_material_manifest_in_transaction(db, semester_id, from_date,
            through_date, as_of, course.stable_id)
        projected = {x.id: x for x in manifest.expectations}
        expectation_links = []
        active = {}
        for link in stored_links:
            rule = rules.get(link.release_rule_id)
            if rule is None or link.expectation_identity != f"{rule.stable_id}@{link.occurrence_date.isoformat()}":
                raise ValueError("EXPECTATION_LINK_PROVENANCE_INVALID")
            fingerprint = rule_fingerprint(rule)
            expectation_links.append(ExpectationLinkReadV1(link_id=link.id, material_id=material.id,
                release_rule_id=rule.id, occurrence_date=link.occurrence_date,
                expectation_identity=link.expectation_identity,
                stored_rule_fingerprint=link.rule_fingerprint,
                current_rule_fingerprint=fingerprint,
                fingerprint_status="CURRENT" if fingerprint == link.rule_fingerprint else "STALE_RULE",
                is_active=link.is_active, created_at=_utc(link.created_at),
                deactivated_at=_utc(link.deactivated_at), deactivation_reason=link.deactivation_reason,
                replacement_link_id=link.replacement_link_id, audit_revision_id=link.audit_revision_id,
                rule_source_id=link.rule_source_id, classification_proposal_id=link.classification_proposal_id,
                projected_in_range=link.expectation_identity in projected))
            if link.is_active:
                active.setdefault(link.expectation_identity, []).append(link.id)
        expectation_links.sort(key=lambda x: (x.occurrence_date, x.expectation_identity, x.link_id))
        occurrences = []
        for identity in sorted(active):
            if identity not in projected:
                continue
            e = projected[identity]
            occurrences.append(ProjectedExpectationReadV1(expectation_id=e.id, rule_stable_id=e.rule_id,
                course_stable_id=e.course_id, material_kind=e.material_kind,
                release_date=e.release_date, release_time=e.release_time,
                information_status=e.information_status, manifest_state=e.state,
                explicit_link_state="LINKED", active_link_ids=sorted(active[identity])))
        occurrences.sort(key=lambda x: (x.release_date, x.release_time or time.min, x.expectation_id))
        return MaterialDetailV1(as_of=local, semester=summary, from_date=from_date,
            through_date=through_date, material=detail, expectation_links=expectation_links,
            projected_expectations=occurrences, classification_proposals=classification,
            skill_proposals=skills)
