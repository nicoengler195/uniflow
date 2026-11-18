"""Read-only, one-snapshot learning session inbox."""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from uniflow.application.application_read_models import _read_transaction, _context
from uniflow.application.weekly_block_materialization import _verified_execution
from uniflow.application.learning_results import _result_path
from uniflow.infrastructure.database.models import (Course, LearningSession, LearningSessionTargetSkill, Skill,
    LearningHandoff, LearningResult, Evidence, MasteryState, DerivedArtifact, WeeklyPlanBlockExecution)
from uniflow.schemas.application_read_v1 import ReadDiagnosticV1
from uniflow.schemas.application_read_detail_v1 import (SessionReadV1, SessionTargetReadV1, HandoffReadV1,
    ResultReadV1, ResultEvidenceReadV1, ResultMasteryReadV1, MasteryApplicationReadV1,
    SessionMaterializationReadV1)
from uniflow.schemas.session_inbox_read_v1 import SessionInboxV1, RESUMABLE, PROCESSING
from uniflow.schemas.learning_result_v2 import LearningResultV2

POLICY = "learning-result-observed-dimensions-v1"

def _utc(value):
    if value is None:
        return None
    return (value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def _artifact(db, artifact_id, kind):
    row = db.get(DerivedArtifact, artifact_id)
    if row is None or row.kind != kind:
        raise ValueError("SESSION_ARTIFACT_INVALID")
    return row

def _verified_result_body(config, artifact, row, current_handoff):
    path = Path(artifact.stored_path)
    if (path != _result_path(config, artifact.sha256) or
            artifact.size_bytes > config.learning_result_max_json_bytes or not path.is_file()):
        raise ValueError("RESULT_ARTIFACT_CONFLICT")
    body = path.read_bytes()
    if len(body) != artifact.size_bytes or hashlib.sha256(body).hexdigest() != artifact.sha256:
        raise ValueError("RESULT_ARTIFACT_CONFLICT")
    parsed = LearningResultV2.model_validate_json(body)
    if (parsed.result_id != row.id or parsed.session_id != row.learning_session_id or
            parsed.handoff_id != row.learning_handoff_id or
            parsed.schema_version != row.schema_version or
            parsed.protocol_version != row.protocol_version or
            parsed.session_outcome != row.session_outcome or
            parsed.handoff_artifact_sha256 != current_handoff.artifact_sha256 or
            parsed.completed_at.replace(tzinfo=None) != row.completed_at.replace(tzinfo=None)):
        raise ValueError("RESULT_ARTIFACT_CONFLICT")
    return parsed

def _session(db, config, row, course):
    targets = list(db.scalars(select(LearningSessionTargetSkill).where(
        LearningSessionTargetSkill.learning_session_id == row.id).order_by(
        LearningSessionTargetSkill.ordinal, LearningSessionTargetSkill.id)))
    if any(db.get(Skill, t.skill_id) is None for t in targets):
        raise ValueError("SESSION_TARGET_SKILL_MISSING")
    target_dtos = [SessionTargetReadV1(target_link_id=t.id, skill_id=t.skill_id, ordinal=t.ordinal) for t in targets]
    handoffs = list(db.scalars(select(LearningHandoff).where(LearningHandoff.learning_session_id == row.id)
        .order_by(LearningHandoff.revision, LearningHandoff.id)))
    history = []
    for h in handoffs:
        a = _artifact(db, h.artifact_id, "LEARNING_HANDOFF_JSON")
        history.append(HandoffReadV1(handoff_id=h.id, revision=h.revision, schema_version=h.schema_version,
            protocol_version=h.protocol_version, context_fingerprint_sha256=h.context_fingerprint_sha256,
            generated_at=_utc(h.generated_at), supersedes_handoff_id=h.supersedes_handoff_id,
            artifact_id=a.id, artifact_sha256=a.sha256, artifact_size_bytes=a.size_bytes))
    current = next((h for h in history if h.handoff_id == row.current_handoff_id), None)
    results = list(db.scalars(select(LearningResult).where(LearningResult.learning_session_id == row.id)))
    # Also catch a result attached through one of this session's handoffs with a forged session ID.
    if handoffs:
        foreign = list(db.scalars(select(LearningResult).where(LearningResult.learning_handoff_id.in_([h.id for h in handoffs]))))
        if any(r.learning_session_id != row.id for r in foreign):
            raise ValueError("RESULT_SESSION_MISMATCH")
    if len(results) > 1:
        raise ValueError("MULTIPLE_SESSION_RESULTS")
    result = results[0] if results else None
    if row.state.value in {"PLANNED", "HANDOFF_CREATED", "IN_PROGRESS"} and result:
        raise ValueError("RESULT_SESSION_STATE_INVALID")
    if row.state.value in {"RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED", "COMPLETED"} and result is None:
        raise ValueError("RESULT_SESSION_STATE_INVALID")
    if result and ((row.state.value == "VALIDATED" and result.processing_status != "VALIDATED") or
                   (row.state.value == "COMPLETED" and result.processing_status != "MASTERY_APPLIED") or
                   (result.processing_status == "MASTERY_APPLIED" and row.state.value != "COMPLETED")):
        raise ValueError("RESULT_SESSION_STATE_INVALID")
    result_dto = None
    evidence_dtos = []
    mastery_dtos = []
    skipped = None
    if result:
        if current is None or result.learning_handoff_id != current.handoff_id:
            raise ValueError("RESULT_HANDOFF_INVALID")
        a = _artifact(db, result.artifact_id, "LEARNING_RESULT_JSON")
        parsed = _verified_result_body(config, a, result, current)
        result_dto = ResultReadV1(result_id=result.id, learning_session_id=result.learning_session_id,
            learning_handoff_id=result.learning_handoff_id, artifact_id=a.id, artifact_sha256=a.sha256,
            artifact_size_bytes=a.size_bytes, schema_version=result.schema_version,
            protocol_version=result.protocol_version, raw_input_sha256=result.raw_input_sha256,
            completed_at=parsed.completed_at, received_at=_utc(result.received_at),
            session_outcome=result.session_outcome, processing_status=result.processing_status,
            has_numeric_skill_values=any(getattr(item, dim) is not None
                for item in parsed.skill_evidence for dim in ("recall", "application", "transfer")))
        evidence = list(db.scalars(select(Evidence).where(Evidence.learning_result_id == result.id)
            .order_by(Evidence.skill_id, Evidence.id)))
        if {e.skill_id for e in evidence} != {item.skill_id for item in parsed.skill_evidence}:
            raise ValueError("RESULT_EVIDENCE_INVALID")
        for e in evidence:
            if e.learning_session_id != row.id or e.evidence_type.value != "LEARNING_RESULT" or db.get(Skill, e.skill_id) is None:
                raise ValueError("RESULT_EVIDENCE_INVALID")
            evidence_dtos.append(ResultEvidenceReadV1(evidence_id=e.id, skill_id=e.skill_id,
                evidence_type=e.evidence_type.value, observed_at=_utc(e.observed_at), source_reference_id=e.source_id))
        for m in db.scalars(select(MasteryState).where(MasteryState.evidence_id.in_([e.id for e in evidence]),
                MasteryState.calculation_method == POLICY).order_by(MasteryState.skill_id, MasteryState.id)) if evidence else []:
            matching = next((e for e in evidence if e.id == m.evidence_id), None)
            if matching is None or m.skill_id != matching.skill_id or m.learning_session_id != row.id:
                raise ValueError("RESULT_MASTERY_INVALID")
            mastery_dtos.append(ResultMasteryReadV1(mastery_state_id=m.id, skill_id=m.skill_id,
                value=m.value, calculated_at=_utc(m.calculated_at), calculation_method=m.calculation_method,
                evidence_id=m.evidence_id, previous_mastery_state_id=m.previous_mastery_state_id))
        if result.processing_status == "MASTERY_APPLIED":
            skipped = sorted({e.skill_id for e in evidence} - {m.skill_id for m in mastery_dtos})
        elif mastery_dtos:
            raise ValueError("RESULT_MASTERY_INVALID")
    application = MasteryApplicationReadV1(status="APPLIED" if skipped is not None else "NOT_APPLIED",
        mastery_states=mastery_dtos, mastery_state_ids=[m.mastery_state_id for m in mastery_dtos],
        mastery_state_count=len(mastery_dtos), skipped_skill_ids=skipped)
    provenance, diagnostics = [], []
    for x in db.scalars(select(WeeklyPlanBlockExecution).where(WeeklyPlanBlockExecution.learning_session_id == row.id)):
        try:
            verified = _verified_execution(config, db, x)
            provenance.append(SessionMaterializationReadV1(execution_id=x.id, plan_id=verified["plan_id"],
                weekly_plan_revision_id=verified["revision_id"], block_id=verified["block_id"],
                action_id=verified["action_id"], execution_kind=verified["execution_kind"],
                materialized_at=datetime.fromisoformat(verified["materialized_at"])))
        except ValueError:
            diagnostics.append(ReadDiagnosticV1(code="MATERIALIZATION_PROVENANCE_INVALID", refs=[x.id]))
    provenance.sort(key=lambda x: (x.materialized_at, x.execution_id))
    if sum(x.execution_kind == "CREATED_SESSION" for x in provenance) > 1:
        raise ValueError("MULTIPLE_CREATED_SESSION_EXECUTIONS")
    activity_at, basis = ((result_dto.received_at, "RESULT_RECEIVED") if result_dto else
        (max(h.generated_at for h in history), "HANDOFF_GENERATED") if history else (_utc(row.created_at), "SESSION_CREATED"))
    return SessionReadV1(session_id=row.id, course_id=course.id, course_stable_id=course.stable_id,
        goal=row.goal, state=row.state.value, created_at=_utc(row.created_at),
        planned_local_wall_time=row.planned_at.replace(tzinfo=None).isoformat() if row.planned_at else None,
        targets=target_dtos, current_handoff=current, handoff_history=history, result=result_dto,
        result_evidence=evidence_dtos, mastery_application=application,
        materialization_provenance=provenance, activity_at=activity_at, activity_basis=basis,
        diagnostics=diagnostics)

def get_session_inbox(config, semester_id: str, *, as_of: datetime, course_id: str | None = None,
                      completed_limit: int = 20) -> SessionInboxV1:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if type(completed_limit) is not int or not 1 <= completed_limit <= 100:
        raise ValueError("INVALID_COMPLETED_LIMIT")
    with _read_transaction(config) as db:
        semester, _, local, _, summary = _context(db, semester_id, as_of)
        courses = {c.id: c for c in db.scalars(select(Course).where(Course.semester_id == semester.id))}
        selected = next((c for c in courses.values() if c.stable_id == course_id), None) if course_id is not None else None
        if course_id is not None and selected is None:
            raise ValueError("UNKNOWN_COURSE_IN_SEMESTER")
        ids = [selected.id] if selected else list(courses)
        groups = {"resumable": [], "processing": [], "completed": []}
        if ids:
            for row in db.scalars(select(LearningSession).where(LearningSession.course_id.in_(ids))):
                dto = _session(db, config, row, courses[row.course_id])
                group = "resumable" if dto.state in RESUMABLE else "processing" if dto.state in PROCESSING else "completed"
                groups[group].append(dto)
        for values in groups.values():
            values.sort(key=lambda s: (-s.activity_at.timestamp(), s.session_id))
        return SessionInboxV1(as_of=local, semester=summary, course_filter_stable_id=course_id,
            resumable_count=len(groups["resumable"]), processing_count=len(groups["processing"]),
            completed_count=len(groups["completed"]), resumable_sessions=groups["resumable"],
            processing_sessions=groups["processing"], completed_sessions=groups["completed"][:completed_limit],
            completed_limit=completed_limit, diagnostics=[])
