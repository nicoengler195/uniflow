"""Manual semantic curation over verified, versioned local sources."""
from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    ClassificationProposal, Course, CourseSkill, Material, MaterialArtifact, ParseRun,
    ParsedDocument, ParsedUnit, RepositoryAcademicLink, RepositoryBlob,
    RepositoryFile, RepositorySnapshot, SemanticEvidenceSpan,
    SemanticExtractionRun, Skill, SkillAlias, SkillDependency, SkillProposal,
    SkillProposalEvidence, SourceArtifact, DerivedArtifact, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock
from uniflow.application.document_parsing import read_parsed_document, read_parsed_unit, _verify_file
from uniflow.application.repository_snapshots import read_repository_blob

ROLES = {"UNKNOWN", "INSTRUCTION", "LECTURE_MATERIAL", "EXERCISE", "SOLUTION", "STUDENT_CODE", "TEST", "BUILD_METADATA"}
STATES = {"HEAD": "head_blob_id", "INDEX": "index_blob_id", "WORKTREE": "working_blob_id"}
EVIDENCE_KINDS = {"NEW_SKILL", "EXISTING_SKILL_EVIDENCE", "SKILL_DEPENDENCY"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _session(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))()


def _require(row: Any, code: str):
    if row is None:
        raise ValueError(code)
    return row


def _document_scope(session, material_artifact_id: str, parse_run_id: str) -> str:
    """Shared document binding check; requires no semantic run or writes."""
    link = _require(session.get(MaterialArtifact, material_artifact_id), "ACADEMIC_SCOPE_CHANGED")
    parse = _require(session.get(ParseRun, parse_run_id), "SOURCE_CHANGED")
    if not link.is_current or link.deactivated_at is not None or parse.source_artifact_id != link.artifact_id or parse.status != "SUCCESS" or session.get(ParsedDocument, parse.id) is None:
        raise ValueError("ACADEMIC_SCOPE_CHANGED")
    if link.classification_proposal_id:
        proposal = session.get(ClassificationProposal, link.classification_proposal_id)
        if proposal is None or proposal.status != "ACCEPTED":
            raise ValueError("ACADEMIC_SCOPE_CHANGED")
        return "ACCEPTED_PROPOSAL"
    return "LEGACY_MANUAL"


def _scope(session, run: SemanticExtractionRun) -> str:
    if run.source_family == "DOCUMENT":
        return _document_scope(session, run.material_artifact_id, run.parse_run_id)
    if run.source_family == "REPOSITORY":
        link = _require(session.get(RepositoryAcademicLink, run.repository_academic_link_id), "ACADEMIC_SCOPE_CHANGED")
        snap = _require(session.get(RepositorySnapshot, run.repository_snapshot_id), "SOURCE_CHANGED")
        if not link.is_active or snap.repository_source_id != link.repository_source_id or run.repository_state not in STATES:
            raise ValueError("ACADEMIC_SCOPE_CHANGED")
        if link.classification_proposal_id:
            proposal = session.get(ClassificationProposal, link.classification_proposal_id)
            if proposal is None or proposal.status != "ACCEPTED":
                raise ValueError("ACADEMIC_SCOPE_CHANGED")
            return "ACCEPTED_PROPOSAL"
        return "LEGACY_MANUAL"
    if run.source_family == "MANUAL":
        return "MANUAL"
    raise ValueError("INVALID_SOURCE_FAMILY")


def _input_identity(session, run: SemanticExtractionRun) -> str:
    if run.source_family == "DOCUMENT":
        link = session.get(MaterialArtifact, run.material_artifact_id)
        parse = session.get(ParseRun, run.parse_run_id)
        artifact = session.get(SourceArtifact, link.artifact_id)
        doc = session.get(ParsedDocument, parse.id)
        return _digest(["DOCUMENT", link.id, artifact.id, artifact.sha256, parse.id, doc.normalized_text_sha256, run.source_role])
    if run.source_family == "REPOSITORY":
        snap = session.get(RepositorySnapshot, run.repository_snapshot_id)
        return _digest(["REPOSITORY", run.repository_academic_link_id, snap.id, snap.snapshot_identity, run.repository_state, run.source_role])
    return _digest(["MANUAL", run.source_role])


def create_semantic_run(config: AppConfig, family: str, *, material_artifact_id: str | None = None,
                        parse_run_id: str | None = None, repository_academic_link_id: str | None = None,
                        repository_snapshot_id: str | None = None, repository_state: str | None = None,
                        role: str = "UNKNOWN") -> str:
    if role not in ROLES:
        raise ValueError("INVALID_SOURCE_ROLE")
    if family == "DOCUMENT":
        if not material_artifact_id or not parse_run_id or any((repository_academic_link_id, repository_snapshot_id, repository_state)):
            raise ValueError("INVALID_DOCUMENT_RUN")
    elif family == "REPOSITORY":
        if not repository_academic_link_id or not repository_snapshot_id or repository_state not in STATES or any((material_artifact_id, parse_run_id)):
            raise ValueError("REPOSITORY_STATE_REQUIRED")
    elif family == "MANUAL":
        if any((material_artifact_id, parse_run_id, repository_academic_link_id, repository_snapshot_id, repository_state)):
            raise ValueError("INVALID_MANUAL_RUN")
    else:
        raise ValueError("INVALID_SOURCE_FAMILY")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            return _create_semantic_run_in_session(session, config, family, material_artifact_id=material_artifact_id, parse_run_id=parse_run_id, repository_academic_link_id=repository_academic_link_id, repository_snapshot_id=repository_snapshot_id, repository_state=repository_state, role=role)


def _create_semantic_run_in_session(session, config: AppConfig, family: str, *, material_artifact_id: str | None = None,
                        parse_run_id: str | None = None, repository_academic_link_id: str | None = None,
                        repository_snapshot_id: str | None = None, repository_state: str | None = None,
                        role: str = "UNKNOWN") -> str:
    """Caller owns the project writer lock and transaction; inputs are validated."""
    run = SemanticExtractionRun(source_family=family, source_role=role, extractor_name="MANUAL", extractor_version="1",
        input_identity_sha256="", material_artifact_id=material_artifact_id, parse_run_id=parse_run_id,
        repository_academic_link_id=repository_academic_link_id, repository_snapshot_id=repository_snapshot_id,
        repository_state=repository_state)
    _scope(session, run)
    run.input_identity_sha256 = _input_identity(session, run)
    session.add(run)
    session.flush()
    return run.id


def _selected_unit_text_in_session(session, parse_run_id: str, unit: ParsedUnit) -> str:
    """Mirror verified document/unit reads using the caller's binding transaction."""
    document = _require(session.get(ParsedDocument, parse_run_id),
        f"ParseRun has no ParsedDocument: {parse_run_id}")
    derived = _require(session.get(DerivedArtifact, document.derived_artifact_id),
        "DERIVED_INTEGRITY: DerivedArtifact row missing")
    path = Path(derived.stored_path)
    _verify_file(path, derived.sha256, derived.size_bytes)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != document.normalized_text_sha256:
        raise ValueError("DERIVED_INTEGRITY: document hash mismatch")
    text = data.decode("utf-8")
    if len(text) != document.char_count:
        raise ValueError("DERIVED_INTEGRITY: document character count mismatch")
    if not (0 <= unit.char_start <= unit.char_end <= len(text)):
        raise ValueError("DERIVED_INTEGRITY: unit offsets invalid")
    value = text[unit.char_start:unit.char_end]
    if hashlib.sha256(value.encode("utf-8")).hexdigest() != unit.text_sha256 or len(value) != unit.char_count:
        raise ValueError("DERIVED_INTEGRITY: unit text mismatch")
    return value


def create_selected_document_skill(config, material_id: str, parse_run_id: str,
        parsed_unit_id: str, start: int, end: int, name: str, kind: str):
    """Validate current selection and persist its complete chain in one writer transaction.

    Public creation services acquire their own locks. Use only their session-local
    helpers here, so parse/intake writers cannot bind between validation and commit.
    Repeating this action creates fresh evidence, as before; it is not idempotent.
    """
    if not isinstance(name, str) or not name.strip() or kind not in {"CONCEPT", "ABILITY"}:
        raise ValueError("SKILL_INPUT_INVALID")
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("SKILL_SELECTION_INVALID")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as db, db.begin():
            material = db.get(Material, material_id)
            if material is None or material.course_id is None:
                raise ValueError("MATERIAL_COURSE_REQUIRED")
            link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id,
                MaterialArtifact.is_current.is_(True), MaterialArtifact.deactivated_at.is_(None)))
            parse = db.get(ParseRun, parse_run_id)
            unit = db.get(ParsedUnit, parsed_unit_id)
            # Match the material read model, including failed/OCR observations.
            current_parse_id = None if link is None else db.scalar(select(ParseRun.id).where(
                ParseRun.source_artifact_id == link.artifact_id).order_by(
                    ParseRun.completed_at.desc(), ParseRun.started_at.desc(),
                    ParseRun.attempt_number.desc(), ParseRun.id).limit(1))
            if (link is None or parse is None or unit is None or
                    parse.id != current_parse_id or
                    parse.source_artifact_id != link.artifact_id or parse.status != "SUCCESS" or
                    unit.parse_run_id != parse.id):
                raise ValueError("SKILL_SELECTION_SOURCE_CHANGED")
            link_id = link.id
            value = _selected_unit_text_in_session(db, parse_run_id, unit)
            if end > len(value) or len(value[start:end].encode("utf-8")) > config.learning_handoff_max_excerpt_utf8_bytes:
                raise ValueError("SKILL_SELECTION_INVALID")
            if not value[start:end].strip():
                raise ValueError("SKILL_SELECTION_INVALID")
            run_id = _create_semantic_run_in_session(db, config, "DOCUMENT",
                material_artifact_id=link_id, parse_run_id=parse_run_id)
            evidence_id = _create_semantic_evidence_in_session(db, config, run_id,
                parsed_unit_id, start, end, document_unit_text=value)
            return _create_skill_proposal_in_session(db, config, run_id, "NEW_SKILL",
                evidence_ids=[evidence_id], name=name.strip(), skill_kind=kind,
                document_unit_text=value)


def build_semantic_input(config: AppConfig, run_id: str) -> dict[str, Any]:
    with _session(config) as session:
        run = _require(session.get(SemanticExtractionRun, run_id), "UNKNOWN_RUN")
        provenance = _scope(session, run)
        if _input_identity(session, run) != run.input_identity_sha256:
            raise ValueError("SOURCE_CHANGED")
        result: dict[str, Any] = {"run_id": run.id, "source_family": run.source_family, "source_role": run.source_role,
                                 "input_identity_sha256": run.input_identity_sha256, "academic_provenance": provenance}
        if run.source_family == "DOCUMENT":
            units = session.scalars(select(ParsedUnit).where(ParsedUnit.parse_run_id == run.parse_run_id).order_by(ParsedUnit.ordinal)).all()
            result["material_artifact_id"] = run.material_artifact_id
            result["parse_run_id"] = run.parse_run_id
            result["text"] = read_parsed_document(run.parse_run_id, config)
            result["units"] = [{"id": u.id, "ordinal": u.ordinal, "text": read_parsed_unit(run.parse_run_id, u.ordinal, config)} for u in units]
        elif run.source_family == "REPOSITORY":
            files = session.scalars(select(RepositoryFile).where(RepositoryFile.snapshot_id == run.repository_snapshot_id).order_by(RepositoryFile.path)).all()
            result["repository_snapshot_id"] = run.repository_snapshot_id
            result["repository_state"] = run.repository_state
            result["files"] = [{"id": f.id, "path": f.path, "blob_id": getattr(f, STATES[run.repository_state]),
                                 "bytes": read_repository_blob(getattr(f, STATES[run.repository_state]), config) if getattr(f, STATES[run.repository_state]) else None}
                                for f in files]
        return result


def _verify_span(session, config: AppConfig, span: SemanticEvidenceSpan, run: SemanticExtractionRun,
                 *, document_unit_text: str | None = None) -> None:
    if span.extraction_run_id != run.id:
        raise ValueError("EVIDENCE_RUN_MISMATCH")
    if run.source_family == "DOCUMENT":
        unit = _require(session.get(ParsedUnit, span.parsed_unit_id), "UNKNOWN_UNIT")
        if span.material_artifact_id != run.material_artifact_id or unit.parse_run_id != run.parse_run_id:
            raise ValueError("EVIDENCE_SOURCE_MISMATCH")
        value = read_parsed_unit(run.parse_run_id, unit.ordinal, config) if document_unit_text is None else document_unit_text
        if span.char_start is None or span.char_end is None or not 0 <= span.char_start < span.char_end <= len(value):
            raise ValueError("INVALID_DOCUMENT_RANGE")
        selected = value[span.char_start:span.char_end].encode("utf-8")
    elif run.source_family == "REPOSITORY":
        file = _require(session.get(RepositoryFile, span.repository_file_id), "UNKNOWN_REPOSITORY_FILE")
        if file.snapshot_id != run.repository_snapshot_id or span.repository_academic_link_id != run.repository_academic_link_id or span.repository_state != run.repository_state or getattr(file, STATES[run.repository_state]) != span.repository_blob_id:
            raise ValueError("EVIDENCE_SOURCE_MISMATCH")
        data = read_repository_blob(span.repository_blob_id, config)
        if span.byte_start is None or span.byte_end is None or not 0 <= span.byte_start <= span.byte_end <= len(data):
            raise ValueError("INVALID_REPOSITORY_RANGE")
        selected = data[span.byte_start:span.byte_end]
    else:
        raise ValueError("MANUAL_RUN_HAS_NO_SOURCE")
    if hashlib.sha256(selected).hexdigest() != span.span_sha256:
        raise ValueError("EVIDENCE_HASH_MISMATCH")


def create_semantic_evidence(config: AppConfig, run_id: str, source_id: str, start: int, end: int) -> str:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            return _create_semantic_evidence_in_session(session, config, run_id, source_id, start, end)


def _create_semantic_evidence_in_session(session, config: AppConfig, run_id: str, source_id: str, start: int, end: int,
                                        *, document_unit_text: str | None = None) -> str:
    """Caller owns the project writer lock and transaction."""
    run = _require(session.get(SemanticExtractionRun, run_id), "UNKNOWN_RUN")
    _scope(session, run)
    if run.source_family == "DOCUMENT":
        unit = _require(session.get(ParsedUnit, source_id), "UNKNOWN_UNIT")
        span = SemanticEvidenceSpan(extraction_run_id=run.id, material_artifact_id=run.material_artifact_id,
            parsed_unit_id=unit.id, char_start=start, char_end=end, span_sha256="")
        if unit.parse_run_id != run.parse_run_id:
            raise ValueError("EVIDENCE_SOURCE_MISMATCH")
        value = read_parsed_unit(run.parse_run_id, unit.ordinal, config) if document_unit_text is None else document_unit_text
        if not 0 <= start < end <= len(value):
            raise ValueError("INVALID_DOCUMENT_RANGE")
        selected = value[start:end].encode("utf-8")
    elif run.source_family == "REPOSITORY":
        file = _require(session.get(RepositoryFile, source_id), "UNKNOWN_REPOSITORY_FILE")
        if file.snapshot_id != run.repository_snapshot_id:
            raise ValueError("EVIDENCE_SOURCE_MISMATCH")
        blob_id = _require(getattr(file, STATES[run.repository_state]), "REPOSITORY_STATE_MISSING")
        data = read_repository_blob(blob_id, config)
        if not 0 <= start <= end <= len(data):
            raise ValueError("INVALID_REPOSITORY_RANGE")
        span = SemanticEvidenceSpan(extraction_run_id=run.id, repository_academic_link_id=run.repository_academic_link_id,
            repository_file_id=file.id, repository_blob_id=blob_id, repository_state=run.repository_state,
            byte_start=start, byte_end=end, span_sha256="")
        selected = data[start:end]
    else:
        raise ValueError("MANUAL_RUN_HAS_NO_SOURCE")
    span.span_sha256 = hashlib.sha256(selected).hexdigest()
    session.add(span)
    session.flush()
    return span.id


def _alias_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def create_skill_proposal(config: AppConfig, run_id: str, kind: str, *, evidence_ids: list[str] | None = None,
                          name: str | None = None, skill_kind: str | None = None, description: str | None = None,
                          domain: str | None = None, proposed_skill_id: str | None = None, target_skill_id: str | None = None,
                          prerequisite_skill_id: str | None = None, dependent_skill_id: str | None = None,
                          course_id: str | None = None, alias: str | None = None, confidence: float | None = None) -> str:
    evidence_ids = evidence_ids or []
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("INVALID_CONFIDENCE")
    if kind not in EVIDENCE_KINDS | {"SKILL_ALIAS", "SKILL_COURSE_ASSOCIATION"}:
        raise ValueError("INVALID_PROPOSAL_KIND")
    if kind in EVIDENCE_KINDS and not evidence_ids:
        raise ValueError("EVIDENCE_REQUIRED")
    if kind == "NEW_SKILL" and (not name or skill_kind not in {"CONCEPT", "ABILITY"}):
        raise ValueError("NEW_SKILL_REQUIRES_NAME_AND_KIND")
    if kind in {"EXISTING_SKILL_EVIDENCE", "SKILL_ALIAS", "SKILL_COURSE_ASSOCIATION"} and not target_skill_id:
        raise ValueError("TARGET_SKILL_REQUIRED")
    if kind == "SKILL_DEPENDENCY" and (not prerequisite_skill_id or not dependent_skill_id):
        raise ValueError("DEPENDENCY_TARGETS_REQUIRED")
    if kind == "SKILL_ALIAS" and (not alias or not alias.strip()):
        raise ValueError("ALIAS_REQUIRED")
    if kind == "SKILL_COURSE_ASSOCIATION" and not course_id:
        raise ValueError("COURSE_REQUIRED")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            return _create_skill_proposal_in_session(session, config, run_id, kind, evidence_ids=evidence_ids, name=name, skill_kind=skill_kind, description=description, domain=domain, proposed_skill_id=proposed_skill_id, target_skill_id=target_skill_id, prerequisite_skill_id=prerequisite_skill_id, dependent_skill_id=dependent_skill_id, course_id=course_id, alias=alias, confidence=confidence)


def _create_skill_proposal_in_session(session, config: AppConfig, run_id: str, kind: str, *, evidence_ids: list[str] | None = None,
                          name: str | None = None, skill_kind: str | None = None, description: str | None = None,
                          domain: str | None = None, proposed_skill_id: str | None = None, target_skill_id: str | None = None,
                          prerequisite_skill_id: str | None = None, dependent_skill_id: str | None = None,
                          course_id: str | None = None, alias: str | None = None, confidence: float | None = None,
                          document_unit_text: str | None = None) -> str:
    """Caller owns the project writer lock and transaction; inputs are validated."""
    run = _require(session.get(SemanticExtractionRun, run_id), "UNKNOWN_RUN")
    _scope(session, run)
    if kind in EVIDENCE_KINDS and run.source_family == "MANUAL":
        raise ValueError("EVIDENCE_REQUIRED")
    evidence = [_require(session.get(SemanticEvidenceSpan, eid), "UNKNOWN_EVIDENCE") for eid in sorted(set(evidence_ids))]
    for span in evidence:
        _verify_span(session, config, span, run, document_unit_text=document_unit_text)
    for sid in (target_skill_id, prerequisite_skill_id, dependent_skill_id):
        if sid:
            _active_skill(session, sid)
    if course_id:
        _require(session.get(Course, course_id), "UNKNOWN_COURSE")
    values = dict(extraction_run_id=run.id, proposal_kind=kind, proposed_name=name, proposed_kind=skill_kind,
        proposed_description=description, proposed_domain=domain, proposed_skill_id=proposed_skill_id,
        target_skill_id=target_skill_id, prerequisite_skill_id=prerequisite_skill_id,
        dependent_skill_id=dependent_skill_id, course_id=course_id, proposed_alias=alias, confidence=confidence)
    identity = _digest([kind, name, skill_kind, description, domain, proposed_skill_id, target_skill_id,
        prerequisite_skill_id, dependent_skill_id, course_id, _alias_key(alias) if alias else None,
        sorted([(s.id, s.span_sha256, s.char_start, s.char_end, s.byte_start, s.byte_end) for s in evidence]),
        run.input_identity_sha256, run.extractor_name, run.extractor_version, run.extractor_config_sha256])
    prior = session.scalar(select(SkillProposal).where(SkillProposal.identity_sha256 == identity, SkillProposal.status == "PENDING"))
    if prior:
        return prior.id
    proposal = SkillProposal(**values, identity_sha256=identity, status="PENDING")
    session.add(proposal)
    session.flush()
    for span in evidence:
        session.add(SkillProposalEvidence(proposal_id=proposal.id, evidence_span_id=span.id))
    return proposal.id


def _active_skill(session, skill_id: str) -> Skill:
    skill = _require(session.get(Skill, skill_id), "UNKNOWN_SKILL")
    if skill.status != "ACTIVE":
        raise ValueError("SKILL_NOT_ACTIVE")
    return skill


def _evidence(session, proposal_id: str) -> list[SemanticEvidenceSpan]:
    return list(session.scalars(select(SemanticEvidenceSpan).join(SkillProposalEvidence,
        SemanticEvidenceSpan.id == SkillProposalEvidence.evidence_span_id).where(SkillProposalEvidence.proposal_id == proposal_id)))


def _cycle(session, prerequisite: str, dependent: str) -> bool:
    if prerequisite == dependent:
        return True
    edges = session.scalars(select(SkillDependency).where(SkillDependency.is_active == True)).all()
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.prerequisite_skill_id, set()).add(edge.dependent_skill_id)
    stack = [dependent]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == prerequisite:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return False


@dataclass(frozen=True)
class SkillDecision:
    proposal_id: str
    action: str
    target_id: str | None
    evidence_ids: tuple[str, ...]
    academic_provenance: str


def accept_skill_proposal(config: AppConfig, proposal_id: str, *, existing_skill_id: str | None = None,
                          dry_run: bool = False,
                          expected_review_context_token: str | None = None) -> SkillDecision:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            proposal = _require(session.get(SkillProposal, proposal_id), "UNKNOWN_PROPOSAL")
            if proposal.status == "ACCEPTED" and not dry_run:
                return SkillDecision(proposal.id, proposal.decision_action, proposal.accepted_skill_id or proposal.accepted_alias_id or proposal.accepted_dependency_id or proposal.accepted_course_skill_id,
                    tuple(s.id for s in _evidence(session, proposal.id)), "HISTORICAL")
            if proposal.status != "PENDING":
                raise ValueError("PROPOSAL_NOT_PENDING")
            if expected_review_context_token is not None:
                from uniflow.application.review_context import skill_review_context
                if skill_review_context(session, proposal) != expected_review_context_token:
                    raise ValueError("REVIEW_CONTEXT_CHANGED")
            run = _require(session.get(SemanticExtractionRun, proposal.extraction_run_id), "UNKNOWN_RUN")
            provenance = _scope(session, run)
            if _input_identity(session, run) != run.input_identity_sha256:
                raise ValueError("SOURCE_CHANGED")
            evidence = _evidence(session, proposal.id)
            if proposal.proposal_kind in EVIDENCE_KINDS and not evidence:
                raise ValueError("EVIDENCE_REQUIRED")
            for span in evidence:
                _verify_span(session, config, span, run)
            kind = proposal.proposal_kind
            target = None
            if kind == "NEW_SKILL":
                if existing_skill_id:
                    target = _active_skill(session, existing_skill_id).id
                    action = "RESOLVE_TO_EXISTING_SKILL"
                else:
                    if proposal.proposed_kind not in {"CONCEPT", "ABILITY"}:
                        raise ValueError("INVALID_SKILL_KIND")
                    target = proposal.proposed_skill_id or str(uuid.uuid4())
                    if session.get(Skill, target):
                        raise ValueError("SKILL_ID_CONFLICT")
                    action = "CREATE_SKILL"
            elif existing_skill_id:
                raise ValueError("EXISTING_TARGET_ONLY_FOR_NEW_SKILL")
            elif kind == "EXISTING_SKILL_EVIDENCE":
                target = _active_skill(session, proposal.target_skill_id).id
                action = "ATTACH_EVIDENCE"
            elif kind == "SKILL_ALIAS":
                _active_skill(session, proposal.target_skill_id)
                key = _alias_key(proposal.proposed_alias)
                prior = session.scalar(select(SkillAlias).where(SkillAlias.skill_id == proposal.target_skill_id,
                    SkillAlias.normalized_alias == key, SkillAlias.is_active == True))
                target = prior.id if prior else None
                action = "REUSE_ALIAS" if prior else "CREATE_ALIAS"
            elif kind == "SKILL_COURSE_ASSOCIATION":
                _active_skill(session, proposal.target_skill_id)
                _require(session.get(Course, proposal.course_id), "UNKNOWN_COURSE")
                prior = session.scalar(select(CourseSkill).where(CourseSkill.course_id == proposal.course_id,
                    CourseSkill.skill_id == proposal.target_skill_id, CourseSkill.is_active == True))
                target = prior.id if prior else None
                action = "REUSE_COURSE_SKILL" if prior else "CREATE_COURSE_SKILL"
            elif kind == "SKILL_DEPENDENCY":
                _active_skill(session, proposal.prerequisite_skill_id)
                _active_skill(session, proposal.dependent_skill_id)
                if proposal.prerequisite_skill_id == proposal.dependent_skill_id:
                    raise ValueError("SELF_DEPENDENCY")
                prior = session.scalar(select(SkillDependency).where(SkillDependency.prerequisite_skill_id == proposal.prerequisite_skill_id,
                    SkillDependency.dependent_skill_id == proposal.dependent_skill_id, SkillDependency.is_active == True))
                if prior:
                    target, action = prior.id, "ALREADY_LINKED"
                elif _cycle(session, proposal.prerequisite_skill_id, proposal.dependent_skill_id):
                    raise ValueError("CYCLE_CONFLICT")
                else:
                    action = "CREATE_DEPENDENCY"
            else:
                raise ValueError("INVALID_PROPOSAL_KIND")
            if dry_run:
                return SkillDecision(proposal.id, action, target, tuple(s.id for s in evidence), provenance)
            if action == "CREATE_SKILL":
                session.add(Skill(id=target, name=proposal.proposed_name, description=proposal.proposed_description,
                    domain=proposal.proposed_domain, kind=proposal.proposed_kind, status="ACTIVE"))
                session.flush()
            elif action == "CREATE_ALIAS":
                row = SkillAlias(skill_id=proposal.target_skill_id, alias=proposal.proposed_alias,
                    normalized_alias=_alias_key(proposal.proposed_alias), skill_proposal_id=proposal.id, is_active=True)
                session.add(row); session.flush(); target = row.id
            elif action == "CREATE_COURSE_SKILL":
                row = CourseSkill(course_id=proposal.course_id, skill_id=proposal.target_skill_id,
                    skill_proposal_id=proposal.id, is_active=True)
                session.add(row); session.flush(); target = row.id
            elif action == "CREATE_DEPENDENCY":
                row = SkillDependency(prerequisite_skill_id=proposal.prerequisite_skill_id,
                    dependent_skill_id=proposal.dependent_skill_id, skill_proposal_id=proposal.id, is_active=True)
                session.add(row); session.flush(); target = row.id
            proposal.status = "ACCEPTED"
            proposal.decision_action = action
            proposal.decision_actor = "MANUAL_USER"
            proposal.decided_at = utc_now()
            if kind in {"NEW_SKILL", "EXISTING_SKILL_EVIDENCE"}:
                proposal.accepted_skill_id = target
            elif kind == "SKILL_ALIAS":
                proposal.accepted_alias_id = target
            elif kind == "SKILL_COURSE_ASSOCIATION":
                proposal.accepted_course_skill_id = target
            else:
                proposal.accepted_dependency_id = target
            return SkillDecision(proposal.id, action, target, tuple(s.id for s in evidence), provenance)


def reject_skill_proposal(config: AppConfig, proposal_id: str, reason: str | None = None,
                          *, expected_review_context_token: str | None = None) -> str:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            p = _require(session.get(SkillProposal, proposal_id), "UNKNOWN_PROPOSAL")
            if p.status == "REJECTED" and p.decision_reason == reason:
                return "ALREADY_REJECTED"
            if p.status != "PENDING":
                raise ValueError("PROPOSAL_NOT_PENDING")
            if expected_review_context_token is not None:
                from uniflow.application.review_context import skill_review_context
                if skill_review_context(session, p) != expected_review_context_token:
                    raise ValueError("REVIEW_CONTEXT_CHANGED")
            p.status, p.decision_actor, p.decided_at, p.decision_reason = "REJECTED", "MANUAL_USER", utc_now(), reason
            return "REJECTED"


def supersede_skill_proposal(config: AppConfig, old_id: str, replacement_id: str) -> None:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            old = _require(session.get(SkillProposal, old_id), "UNKNOWN_PROPOSAL")
            new = _require(session.get(SkillProposal, replacement_id), "UNKNOWN_REPLACEMENT")
            if old.id == new.id or old.status != "PENDING" or new.status != "PENDING":
                raise ValueError("INVALID_SUPERSESSION")
            old.status, old.superseded_by_proposal_id = "SUPERSEDED", new.id
            old.decision_actor, old.decided_at = "MANUAL_USER", utc_now()


def deactivate_skill_relation(config: AppConfig, kind: str, relation_id: str, reason: str,
                              replacement_id: str | None = None) -> None:
    mapping = {"ALIAS": (SkillAlias, "replacement_alias_id"), "COURSE": (CourseSkill, "replacement_link_id"),
               "DEPENDENCY": (SkillDependency, "replacement_dependency_id")}
    if kind not in mapping or not reason.strip():
        raise ValueError("INVALID_DEACTIVATION")
    model, replacement_field = mapping[kind]
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            row = _require(session.get(model, relation_id), "UNKNOWN_RELATION")
            if not row.is_active:
                raise ValueError("RELATION_INACTIVE")
            if replacement_id:
                replacement = _require(session.get(model, replacement_id), "UNKNOWN_REPLACEMENT")
                if replacement.id == row.id or not replacement.is_active:
                    raise ValueError("INVALID_REPLACEMENT")
                setattr(row, replacement_field, replacement.id)
            row.is_active, row.deactivated_at, row.deactivation_reason = False, utc_now(), reason


def get_skill_proposal(config: AppConfig, proposal_id: str) -> tuple[SkillProposal, list[SemanticEvidenceSpan]]:
    with _session(config) as session:
        p = _require(session.get(SkillProposal, proposal_id), "UNKNOWN_PROPOSAL")
        evidence = _evidence(session, p.id)
        session.expunge(p)
        for e in evidence: session.expunge(e)
        return p, evidence


def list_skill_proposals(config: AppConfig) -> list[SkillProposal]:
    with _session(config) as session:
        return list(session.scalars(select(SkillProposal).order_by(SkillProposal.created_at)))


def list_skill_evidence(config: AppConfig, skill_id: str) -> list[SemanticEvidenceSpan]:
    with _session(config) as session:
        proposals = session.scalars(select(SkillProposal).where(SkillProposal.accepted_skill_id == skill_id,
            SkillProposal.status == "ACCEPTED")).all()
        return [span for p in proposals for span in _evidence(session, p.id)]


def list_skills(config: AppConfig) -> list[Skill]:
    with _session(config) as session:
        return list(session.scalars(select(Skill).order_by(Skill.id)))


def get_skill(config: AppConfig, skill_id: str) -> Skill:
    with _session(config) as session:
        return _require(session.get(Skill, skill_id), "UNKNOWN_SKILL")


def list_skill_aliases(config: AppConfig, skill_id: str) -> list[SkillAlias]:
    with _session(config) as session:
        return list(session.scalars(select(SkillAlias).where(SkillAlias.skill_id == skill_id).order_by(SkillAlias.created_at)))


def list_course_skills(config: AppConfig, course_id: str) -> list[CourseSkill]:
    with _session(config) as session:
        return list(session.scalars(select(CourseSkill).where(CourseSkill.course_id == course_id).order_by(CourseSkill.created_at)))


def list_skill_dependencies(config: AppConfig, skill_id: str | None = None) -> list[SkillDependency]:
    with _session(config) as session:
        rows = session.scalars(select(SkillDependency).order_by(SkillDependency.created_at)).all()
        return [r for r in rows if skill_id is None or skill_id in (r.prerequisite_skill_id, r.dependent_skill_id)]
