"""Reusable, detached material detail contracts for application reads."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.application_read_v1 import StrictReadModel


def _aware(value: datetime | None) -> bool:
    return value is None or value.utcoffset() is not None


class ParseObservationV1(StrictReadModel):
    source_artifact_id: str
    parse_run_id: str
    parser_name: str
    parser_version: str
    dependency_version: str
    attempt_number: int = Field(ge=1)
    status: Literal["SUCCESS", "FAILED", "OCR_REQUIRED"]
    started_at: datetime
    completed_at: datetime
    page_count: int | None = Field(default=None, ge=0)
    unit_count: int | None = Field(default=None, ge=0)
    char_count: int | None = Field(default=None, ge=0)
    error_code: str | None
    error_message_excerpt: str | None = Field(max_length=500)
    error_message_truncated: bool
    warnings: list[str] | None
    derived_artifact_id: str | None

    @model_validator(mode="after")
    def coherent(self):
        if not _aware(self.started_at) or not _aware(self.completed_at):
            raise ValueError("PARSE_TIME_MUST_BE_AWARE")
        if self.completed_at < self.started_at:
            raise ValueError("PARSE_TIME_INVALID")
        if self.status == "FAILED":
            if not self.error_code:
                raise ValueError("PARSE_FAILURE_CODE_MISSING")
        elif self.error_code is not None:
            raise ValueError("PARSE_SUCCESS_HAS_ERROR")
        elif self.derived_artifact_id is None or self.unit_count is None or self.char_count is None:
            raise ValueError("PARSE_OUTPUT_METADATA_MISSING")
        if self.error_message_excerpt is None and self.error_message_truncated:
            raise ValueError("PARSE_ERROR_EXCERPT_INVALID")
        return self


class SourceArtifactReadV1(StrictReadModel):
    artifact_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_filename: str
    extension: str | None
    mime_type: str | None
    size_bytes: int = Field(ge=0)
    created_at: datetime
    parse_observation: ParseObservationV1 | None

    @model_validator(mode="after")
    def coherent(self):
        if not _aware(self.created_at):
            raise ValueError("ARTIFACT_TIME_MUST_BE_AWARE")
        if self.parse_observation and self.parse_observation.source_artifact_id != self.artifact_id:
            raise ValueError("PARSE_ARTIFACT_MISMATCH")
        return self


class MaterialVersionReadV1(StrictReadModel):
    material_artifact_id: str
    version_number: int = Field(ge=1)
    is_current: bool
    linked_at: datetime
    deactivated_at: datetime | None
    deactivation_reason: str | None
    replacement_link_id: str | None
    classification_proposal_id: str | None
    source_reference_id: str | None
    source_artifact: SourceArtifactReadV1

    @model_validator(mode="after")
    def coherent(self):
        if not _aware(self.linked_at) or not _aware(self.deactivated_at):
            raise ValueError("MATERIAL_VERSION_TIME_MUST_BE_AWARE")
        if self.is_current and self.deactivated_at is not None:
            raise ValueError("CURRENT_VERSION_DEACTIVATED")
        return self


class MaterialReadV1(StrictReadModel):
    material_id: str
    stable_id: str | None
    course_id: str | None
    course_stable_id: str | None
    title: str
    material_type: str | None
    lifecycle_state: str
    source_reference_id: str | None
    current_version_id: str | None
    versions: list[MaterialVersionReadV1]

    @model_validator(mode="after")
    def coherent(self):
        if (self.course_id is None) != (self.course_stable_id is None):
            raise ValueError("MATERIAL_COURSE_SHAPE_INVALID")
        ids = [v.material_artifact_id for v in self.versions]
        numbers = [v.version_number for v in self.versions]
        if len(ids) != len(set(ids)) or len(numbers) != len(set(numbers)):
            raise ValueError("MATERIAL_VERSION_DUPLICATE")
        current = [v.material_artifact_id for v in self.versions if v.is_current]
        if len(current) > 1 or (current[0] if current else None) != self.current_version_id:
            raise ValueError("MATERIAL_CURRENT_VERSION_INVALID")
        if self.versions != sorted(self.versions, key=lambda v: (
            not v.is_current, -v.version_number, -v.linked_at.timestamp(), v.material_artifact_id)):
            raise ValueError("MATERIAL_VERSION_ORDER_INVALID")
        return self

# Phase 4A2B2 reusable session detail metadata.
import re
from uniflow.schemas.application_read_v1 import ReadDiagnosticV1


class SessionTargetReadV1(StrictReadModel):
    target_link_id: str
    skill_id: str
    ordinal: int = Field(ge=0)


class HandoffReadV1(StrictReadModel):
    handoff_id: str
    revision: int = Field(ge=1)
    schema_version: str
    protocol_version: str
    context_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    supersedes_handoff_id: str | None
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def aware(self):
        if not _aware(self.generated_at):
            raise ValueError("HANDOFF_TIME_MUST_BE_AWARE")
        return self


class ResultReadV1(StrictReadModel):
    result_id: str
    learning_session_id: str
    learning_handoff_id: str
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(ge=0)
    schema_version: str
    protocol_version: str
    raw_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    received_at: datetime
    session_outcome: Literal["COMPLETED", "PARTIAL"]
    processing_status: Literal["VALIDATED", "MASTERY_APPLIED"]
    has_numeric_skill_values: bool | None = None

    @model_validator(mode="after")
    def aware(self):
        if not _aware(self.completed_at) or not _aware(self.received_at):
            raise ValueError("RESULT_TIME_MUST_BE_AWARE")
        return self


class ResultEvidenceReadV1(StrictReadModel):
    evidence_id: str
    skill_id: str
    evidence_type: Literal["LEARNING_RESULT"]
    observed_at: datetime
    source_reference_id: str | None

    @model_validator(mode="after")
    def aware(self):
        if not _aware(self.observed_at):
            raise ValueError("EVIDENCE_TIME_MUST_BE_AWARE")
        return self


class ResultMasteryReadV1(StrictReadModel):
    mastery_state_id: str
    skill_id: str
    value: float = Field(ge=0, le=1)
    calculated_at: datetime
    calculation_method: Literal["learning-result-observed-dimensions-v1"]
    evidence_id: str
    previous_mastery_state_id: str | None

    @model_validator(mode="after")
    def aware(self):
        if not _aware(self.calculated_at):
            raise ValueError("MASTERY_TIME_MUST_BE_AWARE")
        return self


class MasteryApplicationReadV1(StrictReadModel):
    status: Literal["NOT_APPLIED", "APPLIED"]
    mastery_states: list[ResultMasteryReadV1]
    mastery_state_ids: list[str]
    mastery_state_count: int = Field(ge=0)
    skipped_skill_ids: list[str] | None

    @model_validator(mode="after")
    def coherent(self):
        ids = [s.mastery_state_id for s in self.mastery_states]
        if ids != self.mastery_state_ids or len(ids) != len(set(ids)) or len(ids) != self.mastery_state_count:
            raise ValueError("MASTERY_SUMMARY_INVALID")
        if self.status == "NOT_APPLIED" and (self.skipped_skill_ids is not None or ids):
            raise ValueError("MASTERY_NOT_APPLIED_INVALID")
        if self.status == "APPLIED" and (self.skipped_skill_ids is None or self.skipped_skill_ids != sorted(set(self.skipped_skill_ids))):
            raise ValueError("MASTERY_SKIPPED_INVALID")
        return self


class SessionMaterializationReadV1(StrictReadModel):
    execution_id: str
    plan_id: str
    weekly_plan_revision_id: str
    block_id: str
    action_id: str
    execution_kind: Literal["CREATED_SESSION", "RESUMED_SESSION"]
    materialized_at: datetime

    @model_validator(mode="after")
    def aware(self):
        if not _aware(self.materialized_at):
            raise ValueError("MATERIALIZATION_TIME_MUST_BE_AWARE")
        return self


class SessionReadV1(StrictReadModel):
    session_id: str
    course_id: str
    course_stable_id: str
    goal: str
    state: Literal["PLANNED", "HANDOFF_CREATED", "IN_PROGRESS", "RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED", "COMPLETED"]
    created_at: datetime
    planned_local_wall_time: str | None
    targets: list[SessionTargetReadV1]
    current_handoff: HandoffReadV1 | None
    handoff_history: list[HandoffReadV1]
    result: ResultReadV1 | None
    result_evidence: list[ResultEvidenceReadV1]
    mastery_application: MasteryApplicationReadV1
    materialization_provenance: list[SessionMaterializationReadV1]
    activity_at: datetime
    activity_basis: Literal["RESULT_RECEIVED", "HANDOFF_GENERATED", "SESSION_CREATED"]
    diagnostics: list[ReadDiagnosticV1]

    @model_validator(mode="after")
    def coherent(self):
        if not _aware(self.created_at) or not _aware(self.activity_at):
            raise ValueError("SESSION_TIME_MUST_BE_AWARE")
        if self.planned_local_wall_time is not None and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?", self.planned_local_wall_time):
            raise ValueError("PLANNED_WALL_TIME_INVALID")
        targets = [(x.ordinal, x.target_link_id) for x in self.targets]
        if (targets != sorted(targets) or [x.ordinal for x in self.targets] != list(range(len(targets)))
                or not targets or len({x.skill_id for x in self.targets}) != len(targets)):
            raise ValueError("SESSION_TARGETS_INVALID")
        history = self.handoff_history
        if len({x.handoff_id for x in history}) != len(history) or [x.revision for x in history] != list(range(1, len(history)+1)):
            raise ValueError("HANDOFF_HISTORY_INVALID")
        if any(x.supersedes_handoff_id != (history[i-1].handoff_id if i else None) for i, x in enumerate(history)):
            raise ValueError("HANDOFF_CHAIN_INVALID")
        if (self.current_handoff.handoff_id if self.current_handoff else None) != (history[-1].handoff_id if history else None):
            raise ValueError("CURRENT_HANDOFF_INVALID")
        if self.state != "PLANNED" and self.current_handoff is None:
            raise ValueError("SESSION_HANDOFF_MISSING")
        if self.result is not None and (self.current_handoff is None or self.result.learning_session_id != self.session_id or self.result.learning_handoff_id != self.current_handoff.handoff_id):
            raise ValueError("RESULT_RELATION_INVALID")
        if self.result is None and (self.result_evidence or self.mastery_application.status == "APPLIED"):
            raise ValueError("RESULT_DETAIL_WITHOUT_RESULT")
        if len({e.evidence_id for e in self.result_evidence}) != len(self.result_evidence) or len({e.skill_id for e in self.result_evidence}) != len(self.result_evidence):
            raise ValueError("RESULT_EVIDENCE_DUPLICATE")
        evidence = {e.evidence_id: e.skill_id for e in self.result_evidence}
        if any(m.evidence_id not in evidence or evidence[m.evidence_id] != m.skill_id for m in self.mastery_application.mastery_states):
            raise ValueError("RESULT_MASTERY_RELATION_INVALID")
        if self.result and (self.result.processing_status == "MASTERY_APPLIED") != (self.mastery_application.status == "APPLIED"):
            raise ValueError("MASTERY_STATUS_INVALID")
        if len({x.execution_id for x in self.materialization_provenance}) != len(self.materialization_provenance) or sum(x.execution_kind == "CREATED_SESSION" for x in self.materialization_provenance) > 1:
            raise ValueError("SESSION_MATERIALIZATION_INVALID")
        if self.materialization_provenance != sorted(self.materialization_provenance, key=lambda x: (x.materialized_at, x.execution_id)):
            raise ValueError("SESSION_MATERIALIZATION_ORDER_INVALID")
        expected_at, expected_basis = ((self.result.received_at, "RESULT_RECEIVED") if self.result else
            (max(h.generated_at for h in history), "HANDOFF_GENERATED") if history else (self.created_at, "SESSION_CREATED"))
        if (self.activity_at, self.activity_basis) != (expected_at, expected_basis):
            raise ValueError("SESSION_ACTIVITY_INVALID")
        return self
