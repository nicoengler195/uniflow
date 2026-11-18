"""Immutable information for human review. No approval or scoring operations."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from uniflow.schemas.evidence_validation_i2 import Finding, FindingStatus


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourceLocator(ProjectionModel):
    origin: Literal["SIDECAR", "HANDOFF", "RESULT", "CURRENT_I2", "SUPPLIED_I2"]
    source_sha256: str
    pointer: str
    original_array_index: int | None = None


class ModelSnapshot(ProjectionModel):
    source: SourceLocator
    payload_json: str


class EntityKey(ProjectionModel):
    kind: Literal["TASK", "ATTEMPT", "RUBRIC", "ASSESSMENT", "EVENT", "ARTIFACT", "REVIEW"]
    entity_id: str
    revision: int | None = None


class ArtifactIdentity(ProjectionModel):
    artifact_id: str
    sha256: str
    byte_length: int


class ProjectionDiagnostic(ProjectionModel):
    code: str
    status: FindingStatus
    reason: str
    sources: tuple[SourceLocator, ...] = ()
    entity_keys: tuple[EntityKey, ...] = ()


class ProjectionInputContext(ProjectionModel):
    sidecar_sha256: str | None
    original_artifacts: tuple[ArtifactIdentity, ...]
    handoff_artifact_id: str | None
    result_artifact_id: str | None
    caller_handoff_revision: int | None
    assessment_selection_supplied: bool
    assessment_selection: tuple[tuple[str, str], ...]
    supplied_report_sha256: str | None
    current_report_sha256: str | None
    supplied_report_handling: Literal["ABSENT", "MATCHED_CURRENT", "ARCHIVED_LEGACY", "REJECTED"]
    validator_version: str | None
    context_sha256: str


class SessionProjection(ProjectionModel):
    sidecar_declaration: ModelSnapshot | None = None
    handoff_context: ModelSnapshot | None = None
    result_context: ModelSnapshot | None = None
    declared_handoff_revision: int | None = None
    caller_handoff_revision: int | None = None
    declared_result_binding: ModelSnapshot | None = None
    binding_finding_indices: tuple[int, ...] = ()
    provenance_notice: str = "Deklarationen und technische Befunde sind keine authentifizierte Historie; Human Review bleibt erforderlich."


class FindingProjection(ProjectionModel):
    source_report_version: str
    source_report_sha256: str
    original_finding_index: int
    finding: Finding
    display_category: Literal["LOCAL_DIAGNOSTIC", "BINDING_CONFIRMATION", "TECHNICAL_CHECK", "TRUST_LIMIT", "UNKNOWN_CODE", "LEGACY_REPORT"]
    targets: tuple[EntityKey, ...] = ()
    association: Literal["EXACT", "AMBIGUOUS", "GLOBAL"] = "GLOBAL"


class ReviewQuestion(ProjectionModel):
    question_code: str
    scope: EntityKey | None = None
    text: str
    supporting_finding_indices: tuple[int, ...] = ()
    supporting_sources: tuple[SourceLocator, ...] = ()


class EvidenceProjection(ProjectionModel):
    ref_source: SourceLocator
    declaration: ModelSnapshot
    artifact_id: str
    selected_text: str | None = None
    selected_text_source: SourceLocator | None = None
    selection_finding_indices: tuple[int, ...] = ()
    message_finding_indices: tuple[int, ...] = ()
    provenance_notice: str = "Textauswahl und Nachrichtenidentität sind getrennt; Provenienz bleibt eine Deklaration."


class ArtifactProjection(ProjectionModel):
    artifact_id: str
    declaration: ModelSnapshot | None
    actual: ArtifactIdentity | None
    finding_indices: tuple[int, ...] = ()


class EventProjection(ProjectionModel):
    event_id: str
    event_type: str
    task_id: str | None
    attempt_id: str | None
    declaration: ModelSnapshot
    parent_event_ids: tuple[str, ...]
    declared_collector_sequence: int | None
    time_claim: ModelSnapshot
    source_evidence_index: int
    finding_indices: tuple[int, ...] = ()


class AwardProjection(ProjectionModel):
    criterion_id: str
    dimension: Literal["TASK", "SKILL"]
    skill_id: str | None
    criterion: ModelSnapshot
    award: ModelSnapshot
    awarded_points: str | None
    status: Literal["PENDING", "RECORDED"]
    correctness_declaration: Literal["CORRECT", "INCORRECT", "UNKNOWN"]
    evidence_indices: tuple[int, ...]
    assessed_components: tuple[ModelSnapshot, ...]
    finding_indices: tuple[int, ...] = ()


class AssessmentProjection(ProjectionModel):
    assessment_id: str
    revision: int
    attempt_id: str
    rubric_key: EntityKey
    declaration: ModelSnapshot
    awards: tuple[AwardProjection, ...]
    qualitative_observations: tuple[ModelSnapshot, ...]
    supersedes_assessment_id: str | None
    selected_for_v1: bool
    comparison_finding_indices: tuple[int, ...] = ()
    finding_indices: tuple[int, ...] = ()


class AttemptProjection(ProjectionModel):
    attempt_id: str
    task_id: str
    phase: Literal["LEARNING", "FINAL_EXAM"]
    declared_attempt_order: int
    declaration: ModelSnapshot
    answer_evidence_indices: tuple[int, ...]
    answer_time: ModelSnapshot
    assistance_before: ModelSnapshot
    feedback_event_ids: tuple[str, ...]
    correction: ModelSnapshot | None
    assessments: tuple[AssessmentProjection, ...]
    finding_indices: tuple[int, ...] = ()


class RubricProjection(ProjectionModel):
    rubric_id: str
    revision: int
    task_id: str
    phase: Literal["LEARNING", "FINAL_EXAM"]
    declaration: ModelSnapshot
    task_criteria: tuple[ModelSnapshot, ...]
    skill_criteria: tuple[ModelSnapshot, ...]
    dimension_totals: tuple[ModelSnapshot, ...]
    declaration_event_id: str | None
    finding_indices: tuple[int, ...] = ()


class TaskProjection(ProjectionModel):
    task_id: str
    phase: Literal["LEARNING", "FINAL_EXAM"]
    declaration: ModelSnapshot
    statement_evidence_index: int
    rubric_key: EntityKey
    attempts: tuple[AttemptProjection, ...]
    v1_task: ModelSnapshot | None
    selected_assessment_id: str | None
    finding_indices: tuple[int, ...] = ()
    question_indices: tuple[int, ...] = ()


class ReviewProjectionV1(ProjectionModel):
    projection_version: Literal["uniflow-review-projection-i3.0"] = "uniflow-review-projection-i3.0"
    policy_version: Literal["i3-projection-policy-1"] = "i3-projection-policy-1"
    mode: Literal["SIDECAR_EVIDENCE", "LEGACY_V1", "DIAGNOSTIC_ONLY"]
    input_context: ProjectionInputContext
    session: SessionProjection
    phase_states: tuple[ModelSnapshot, ...] = ()
    final_exam_decision: ModelSnapshot | None = None
    tasks: tuple[TaskProjection, ...] = ()
    rubrics: tuple[RubricProjection, ...] = ()
    events: tuple[EventProjection, ...] = ()
    source_artifacts: tuple[ArtifactProjection, ...] = ()
    evidence: tuple[EvidenceProjection, ...] = ()
    detached_entities: tuple[ModelSnapshot, ...] = ()
    declared_reviews: tuple[ModelSnapshot, ...] = ()
    findings: tuple[FindingProjection, ...] = ()
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()
    questions: tuple[ReviewQuestion, ...] = ()
    not_checked: tuple[str, ...] = ()
    human_review_required: Literal[True] = True
