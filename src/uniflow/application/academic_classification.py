"""Manual proposals and explicit, auditable academic associations."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import select

from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
from uniflow.domain.enums import MaterialState
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, ClassificationProposal, Course, Material, MaterialArtifact,
    MaterialExpectationLink, MaterialReleaseRule, RepositoryAcademicLink,
    RepositorySource, SourceArtifact, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock


class ClassificationConflict(ValueError):
    pass


def _session(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))


def _find(session, model, identifier):
    row = session.get(model, identifier)
    if row is None and hasattr(model, "stable_id"):
        row = session.scalar(select(model).where(model.stable_id == identifier))
    if row is None and model is SourceArtifact and len(identifier) == 64:
        row = session.scalar(select(model).where(model.sha256 == identifier))
    if row is None:
        raise ValueError(f"UNKNOWN_{model.__name__.upper()}: {identifier}")
    return row


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def rule_fingerprint(rule: MaterialReleaseRule) -> str:
    return _digest({"stable_id": rule.stable_id, "course_id": rule.course_id,
                    "payload": json.loads(rule.payload_json), "information_status": rule.information_status.value})


def _expectation(session, config, identity: str):
    try:
        rule_stable_id, day_text = identity.rsplit("@", 1)
        day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise ClassificationConflict("INVALID_EXPECTATION_IDENTITY") from exc
    rule = _find(session, MaterialReleaseRule, rule_stable_id)
    course = session.get(Course, rule.course_id)
    semester = course.semester
    from datetime import datetime, timezone
    manifest = _generate_material_manifest_in_transaction(session, semester.stable_id,
        day, day, datetime.now(timezone.utc))
    if identity not in {item.id for item in manifest.expectations}:
        raise ClassificationConflict("INVALID_EXPECTATION_OCCURRENCE")
    return rule, day


def _create(config, kind: str, *, artifact=None, repository=None, material=None,
            course=None, assessment=None, target_material=None, stable_id=None, title=None,
            material_type=None, expectation=None, replaces_link_id=None):
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        Session = _session(config)
        with Session.begin() as session:
            a = _find(session, SourceArtifact, artifact) if artifact else None
            r = _find(session, RepositorySource, repository) if repository else None
            m = _find(session, Material, material) if material else None
            c = _find(session, Course, course) if course else None
            ass = _find(session, Assessment, assessment) if assessment else None
            target = _find(session, Material, target_material) if target_material else None
            rule = day = None
            if expectation:
                rule, day = _expectation(session, config, expectation)
            if kind == "ARTIFACT_TO_EXISTING_MATERIAL":
                if not (a and target) or any((r, m, ass, stable_id, title, material_type, rule)):
                    raise ValueError("INVALID_PROPOSAL_TARGETS")
                if c and target.course_id != c.id:
                    raise ClassificationConflict("CONFLICT_REQUIRES_CORRECTION")
            elif kind == "ARTIFACT_TO_NEW_MATERIAL":
                if not (a and c and stable_id and title and material_type) or any((r, m, ass, target, rule)):
                    raise ValueError("INVALID_PROPOSAL_TARGETS")
            elif kind == "REPOSITORY_ACADEMIC_LINK":
                if not (r and (c or ass)) or any((a, m, target, stable_id, title, material_type, rule)):
                    raise ValueError("INVALID_PROPOSAL_TARGETS")
                if ass and c and ass.course_id != c.id:
                    raise ClassificationConflict("ASSESSMENT_COURSE_MISMATCH")
                if ass and not c:
                    c = session.get(Course, ass.course_id)
            elif kind == "MATERIAL_EXPECTATION_LINK":
                if not (m and rule) or any((a, r, target, ass, stable_id, title, material_type)):
                    raise ValueError("INVALID_PROPOSAL_TARGETS")
                if m.course_id is None or m.course_id != rule.course_id:
                    raise ClassificationConflict("MATERIAL_COURSE_MISMATCH")
            else:
                raise ValueError("INVALID_PROPOSAL_KIND")
            if replaces_link_id and kind not in ("ARTIFACT_TO_EXISTING_MATERIAL", "ARTIFACT_TO_NEW_MATERIAL", "REPOSITORY_ACADEMIC_LINK", "MATERIAL_EXPECTATION_LINK"):
                raise ValueError("INVALID_REPLACEMENT")
            values = dict(proposal_kind=kind, subject_source_artifact_id=a.id if a else None,
                subject_repository_source_id=r.id if r else None, subject_material_id=m.id if m else None,
                proposed_course_id=c.id if c else None, proposed_assessment_id=ass.id if ass else None,
                proposed_material_id=target.id if target else None, proposed_material_stable_id=stable_id,
                proposed_material_title=title, proposed_material_type=material_type,
                proposed_expectation_rule_id=rule.id if rule else None,
                proposed_expectation_date=day, proposed_expectation_identity=expectation,
                replaces_link_id=replaces_link_id)
            identity = _digest({k: v.isoformat() if isinstance(v, date) else v for k, v in values.items()} | {"classifier_name": "MANUAL", "classifier_version": "1"})
            prior = session.scalar(select(ClassificationProposal).where(
                ClassificationProposal.identity_sha256 == identity, ClassificationProposal.status == "PENDING"))
            if prior:
                return prior.id
            proposal = ClassificationProposal(**values, identity_sha256=identity, status="PENDING",
                classifier_name="MANUAL", classifier_version="1")
            session.add(proposal)
            session.flush()
            return proposal.id


def create_manual_artifact_proposal(config, artifact, *, course=None, material=None,
                                    new_material_stable_id=None, title=None, material_type=None,
                                    replaces_link_id=None):
    kind = "ARTIFACT_TO_EXISTING_MATERIAL" if material else "ARTIFACT_TO_NEW_MATERIAL"
    return _create(config, kind, artifact=artifact, course=course, target_material=material,
                   stable_id=new_material_stable_id, title=title, material_type=material_type,
                   replaces_link_id=replaces_link_id)


def create_manual_repository_proposal(config, repository, *, course=None, assessment=None, replaces_link_id=None):
    return _create(config, "REPOSITORY_ACADEMIC_LINK", repository=repository, course=course,
                   assessment=assessment, replaces_link_id=replaces_link_id)


def create_manual_expectation_proposal(config, material, expectation, *, replaces_link_id=None):
    return _create(config, "MATERIAL_EXPECTATION_LINK", material=material,
                   expectation=expectation, replaces_link_id=replaces_link_id)


def _plan(session, config, p):
    if p.status != "PENDING":
        return p.status
    if p.proposal_kind.startswith("ARTIFACT_"):
        artifact = session.get(SourceArtifact, p.subject_source_artifact_id)
        if artifact is None:
            raise ClassificationConflict("MISSING_ARTIFACT")
        if p.replaces_link_id:
            old = session.get(MaterialArtifact, p.replaces_link_id)
            if old is None or not old.is_current or old.artifact_id != artifact.id:
                raise ClassificationConflict("REPLACEMENT_LINK_NOT_ACTIVE")
        if p.proposal_kind == "ARTIFACT_TO_NEW_MATERIAL":
            material = session.scalar(select(Material).where(Material.stable_id == p.proposed_material_stable_id))
            if material:
                raise ClassificationConflict("MATERIAL_STABLE_ID_EXISTS_REQUIRES_EXPLICIT_TRANSITION")
            if session.get(Course, p.proposed_course_id) is None:
                raise ClassificationConflict("MISSING_COURSE")
            return "CREATE_MATERIAL_AND_LINK_ARTIFACT"
        material = session.get(Material, p.proposed_material_id)
        if material is None or (p.proposed_course_id and material.course_id != p.proposed_course_id):
            raise ClassificationConflict("CONFLICT_REQUIRES_CORRECTION")
        links = session.scalars(select(MaterialArtifact).where(MaterialArtifact.material_id == material.id)).all()
        if any(link.artifact_id == artifact.id and link.is_current for link in links):
            return "ALREADY_LINKED"
        if any(link.artifact_id == artifact.id for link in links):
            raise ClassificationConflict("ARTIFACT_ALREADY_IN_HISTORY")
        if any(link.is_current for link in links) or (material.content_hash and material.content_hash != artifact.sha256):
            raise ClassificationConflict("CONFLICT_VERSION_REQUIRED")
        return "LINK_ARTIFACT"
    if p.proposal_kind == "REPOSITORY_ACADEMIC_LINK":
        repo = session.get(RepositorySource, p.subject_repository_source_id)
        course = session.get(Course, p.proposed_course_id)
        assessment = session.get(Assessment, p.proposed_assessment_id) if p.proposed_assessment_id else None
        if repo is None or course is None or (assessment and assessment.course_id != course.id):
            raise ClassificationConflict("REPOSITORY_TARGET_CONFLICT")
        active = session.scalar(select(RepositoryAcademicLink).where(
            RepositoryAcademicLink.repository_source_id == repo.id, RepositoryAcademicLink.is_active.is_(True)))
        if p.replaces_link_id:
            if active is None or active.id != p.replaces_link_id:
                raise ClassificationConflict("REPLACEMENT_LINK_NOT_ACTIVE")
            return "REPLACE_REPOSITORY_LINK"
        if active:
            if active.course_id == course.id and active.assessment_id == p.proposed_assessment_id:
                return "ALREADY_LINKED"
            raise ClassificationConflict("CONFLICT_REQUIRES_CORRECTION")
        if repo.course_id not in (None, course.id) or repo.assessment_id not in (None, p.proposed_assessment_id):
            raise ClassificationConflict("CONFLICT_REQUIRES_CORRECTION")
        return "LINK_REPOSITORY"
    if p.proposal_kind == "MATERIAL_EXPECTATION_LINK":
        material = session.get(Material, p.subject_material_id)
        rule, day = _expectation(session, config, p.proposed_expectation_identity)
        if material is None or material.course_id is None or material.course_id != rule.course_id or rule.id != p.proposed_expectation_rule_id or day != p.proposed_expectation_date:
            raise ClassificationConflict("EXPECTATION_TARGET_CONFLICT")
        active = session.scalar(select(MaterialExpectationLink).where(
            MaterialExpectationLink.material_id == material.id,
            MaterialExpectationLink.expectation_identity == p.proposed_expectation_identity,
            MaterialExpectationLink.is_active.is_(True)))
        if p.replaces_link_id:
            if active is None or active.id != p.replaces_link_id:
                raise ClassificationConflict("REPLACEMENT_LINK_NOT_ACTIVE")
            return "REPLACE_EXPECTATION_LINK"
        return "ALREADY_LINKED" if active else "LINK_EXPECTATION"
    raise ValueError("INVALID_PROPOSAL_KIND")


def accept_classification_proposal(config: AppConfig, proposal_id: str, *, dry_run=False, reason=None,
                                   expected_review_context_token: str | None = None):
    def execute():
        Session = _session(config)
        with Session.begin() as session:
            p = _find(session, ClassificationProposal, proposal_id)
            if expected_review_context_token is not None and p.status == "PENDING":
                from uniflow.application.review_context import classification_review_context
                if classification_review_context(session, p) != expected_review_context_token:
                    raise ClassificationConflict("REVIEW_CONTEXT_CHANGED")
            action = _plan(session, config, p)
            if dry_run or p.status == "ACCEPTED":
                return action
            if p.status != "PENDING":
                raise ClassificationConflict(f"PROPOSAL_{p.status}")
            if p.proposal_kind.startswith("ARTIFACT_"):
                old = session.get(MaterialArtifact, p.replaces_link_id) if p.replaces_link_id else None
                if action == "CREATE_MATERIAL_AND_LINK_ARTIFACT":
                    material = Material(stable_id=p.proposed_material_stable_id,
                        title=p.proposed_material_title, material_type=p.proposed_material_type,
                        course_id=p.proposed_course_id, lifecycle_state=MaterialState.DOWNLOADED)
                    session.add(material)
                    session.flush()
                else:
                    material = session.get(Material, p.proposed_material_id)
                if action != "ALREADY_LINKED":
                    number = max((x.version_number for x in material.artifacts), default=0) + 1
                    if old:
                        old.is_current = False
                        old.deactivated_at = utc_now()
                        old.deactivation_reason = reason or "MANUAL_CORRECTION"
                        session.flush()
                    link = MaterialArtifact(material_id=material.id, artifact_id=p.subject_source_artifact_id,
                        version_number=number, is_current=True, classification_proposal_id=p.id)
                    session.add(link)
                    session.flush()
                    if old:
                        old.replacement_link_id = link.id
            elif p.proposal_kind == "REPOSITORY_ACADEMIC_LINK" and action != "ALREADY_LINKED":
                repo = session.get(RepositorySource, p.subject_repository_source_id)
                old = session.get(RepositoryAcademicLink, p.replaces_link_id) if p.replaces_link_id else None
                if old:
                    old.is_active = False
                    old.deactivated_at = utc_now()
                    old.deactivation_reason = reason or "MANUAL_CORRECTION"
                    session.flush()
                link = RepositoryAcademicLink(repository_source_id=repo.id, course_id=p.proposed_course_id,
                    assessment_id=p.proposed_assessment_id, classification_proposal_id=p.id, is_active=True)
                session.add(link)
                session.flush()
                if old:
                    old.replacement_link_id = link.id
                repo.course_id = link.course_id
                repo.assessment_id = link.assessment_id
            elif p.proposal_kind == "MATERIAL_EXPECTATION_LINK" and action != "ALREADY_LINKED":
                rule = session.get(MaterialReleaseRule, p.proposed_expectation_rule_id)
                old = session.get(MaterialExpectationLink, p.replaces_link_id) if p.replaces_link_id else None
                if old:
                    old.is_active = False
                    old.deactivated_at = utc_now()
                    old.deactivation_reason = reason or "MANUAL_CORRECTION"
                    session.flush()
                link = MaterialExpectationLink(material_id=p.subject_material_id, release_rule_id=rule.id,
                    occurrence_date=p.proposed_expectation_date,
                    expectation_identity=p.proposed_expectation_identity,
                    rule_fingerprint=rule_fingerprint(rule), audit_revision_id=rule.audit_revision_id,
                    rule_source_id=rule.source_id, classification_proposal_id=p.id, is_active=True)
                session.add(link)
                session.flush()
                if old:
                    old.replacement_link_id = link.id
            p.status = "ACCEPTED"
            p.decided_at = utc_now()
            p.decision_actor = "MANUAL_USER"
            p.decision_reason = reason
            return action
    if dry_run:
        return execute()
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        return execute()


def reject_classification_proposal(config, proposal_id, *, reason=None,
                                   expected_review_context_token: str | None = None):
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        Session = _session(config)
        with Session.begin() as session:
            p = _find(session, ClassificationProposal, proposal_id)
            if p.status == "REJECTED":
                if p.decision_reason == reason:
                    return "ALREADY_REJECTED"
                raise ClassificationConflict("REJECTION_REASON_CONFLICT")
            if expected_review_context_token is not None and p.status == "PENDING":
                from uniflow.application.review_context import classification_review_context
                if classification_review_context(session, p) != expected_review_context_token:
                    raise ClassificationConflict("REVIEW_CONTEXT_CHANGED")
            if p.status != "PENDING":
                raise ClassificationConflict(f"PROPOSAL_{p.status}")
            p.status = "REJECTED"
            p.decided_at = utc_now()
            p.decision_actor = "MANUAL_USER"
            p.decision_reason = reason
            return "REJECTED"


def supersede_classification_proposal(config, old_id, new_id, *, reason=None):
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        Session = _session(config)
        with Session.begin() as session:
            old = _find(session, ClassificationProposal, old_id)
            new = _find(session, ClassificationProposal, new_id)
            if old.id == new.id or old.status != "PENDING" or new.status != "PENDING":
                raise ClassificationConflict("INVALID_SUPERSESSION")
            old.status = "SUPERSEDED"
            old.superseded_by_proposal_id = new.id
            old.decided_at = utc_now()
            old.decision_actor = "MANUAL_USER"
            old.decision_reason = reason
            return "SUPERSEDED"


def correct_academic_link(config, link_type, link_id, *, course=None, assessment=None,
                          expectation=None, material=None, new_material_stable_id=None,
                          title=None, material_type=None):
    Session = _session(config)
    with Session() as session:
        if link_type == "repository":
            old = _find(session, RepositoryAcademicLink, link_id)
            if not old.is_active:
                raise ClassificationConflict("LINK_NOT_ACTIVE")
            identifier = old.repository_source_id
        elif link_type == "expectation":
            old = _find(session, MaterialExpectationLink, link_id)
            if not old.is_active:
                raise ClassificationConflict("LINK_NOT_ACTIVE")
            identifier = old.material_id
        elif link_type == "artifact":
            old = _find(session, MaterialArtifact, link_id)
            if not old.is_current:
                raise ClassificationConflict("LINK_NOT_ACTIVE")
            identifier = old.artifact_id
        else:
            raise ValueError("INVALID_LINK_TYPE")
    if link_type == "repository":
        return create_manual_repository_proposal(config, identifier, course=course,
                                                 assessment=assessment, replaces_link_id=link_id)
    if link_type == "expectation":
        return create_manual_expectation_proposal(config, identifier, expectation, replaces_link_id=link_id)
    return create_manual_artifact_proposal(config, identifier, course=course, material=material,
        new_material_stable_id=new_material_stable_id, title=title, material_type=material_type,
        replaces_link_id=link_id)


def list_classification_proposals(config, status=None):
    Session = _session(config)
    with Session() as session:
        query = select(ClassificationProposal).order_by(ClassificationProposal.created_at, ClassificationProposal.id)
        if status:
            query = query.where(ClassificationProposal.status == status)
        return session.scalars(query).all()


def get_classification_proposal(config, proposal_id):
    Session = _session(config)
    with Session() as session:
        return _find(session, ClassificationProposal, proposal_id)


def list_unclassified_artifacts(config):
    Session = _session(config)
    with Session() as session:
        return session.scalars(select(SourceArtifact).where(~SourceArtifact.materials.any(
            MaterialArtifact.is_current.is_(True))).order_by(SourceArtifact.sha256)).all()


def list_unlinked_repositories(config):
    Session = _session(config)
    with Session() as session:
        return session.scalars(select(RepositorySource).where(RepositorySource.course_id.is_(None),
            RepositorySource.assessment_id.is_(None)).order_by(RepositorySource.stable_id)).all()


def get_material_expectation_links(config, material_id):
    Session = _session(config)
    with Session() as session:
        material = _find(session, Material, material_id)
        links = session.scalars(select(MaterialExpectationLink).where(
            MaterialExpectationLink.material_id == material.id).order_by(MaterialExpectationLink.created_at)).all()
        return [(link, "CURRENT" if rule_fingerprint(session.get(MaterialReleaseRule, link.release_rule_id)) == link.rule_fingerprint else "STALE_RULE") for link in links]


def get_link_history(config, link_type, identifier):
    Session = _session(config)
    with Session() as session:
        if link_type == "repository":
            repo = _find(session, RepositorySource, identifier)
            return session.scalars(select(RepositoryAcademicLink).where(
                RepositoryAcademicLink.repository_source_id == repo.id).order_by(RepositoryAcademicLink.created_at)).all()
        if link_type == "expectation":
            return [link for link, _ in get_material_expectation_links(config, identifier)]
        if link_type == "artifact":
            artifact = _find(session, SourceArtifact, identifier)
            return session.scalars(select(MaterialArtifact).where(
                MaterialArtifact.artifact_id == artifact.id).order_by(MaterialArtifact.linked_at)).all()
        raise ValueError("INVALID_LINK_TYPE")
