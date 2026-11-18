"""Versioned outbound contract for an explicit Skill learning session."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from uniflow.domain.enums import DeadlineStatus, InformationStatus, SemesterState
from uniflow.schemas.common import StrictModel

Id = str
Short = str


class SemesterV2(StrictModel):
    id: Id = Field(min_length=1, max_length=80)
    stable_id: Id = Field(min_length=1, max_length=80)
    name: Short = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=80)
    state: SemesterState


class CourseV2(StrictModel):
    id: Id = Field(min_length=1, max_length=80)
    stable_id: Id = Field(min_length=1, max_length=120)
    name: Short = Field(min_length=1, max_length=200)
    parent_course_id: Id | None = None
    information_status: InformationStatus


class SkillV2(StrictModel):
    skill_id: Id = Field(min_length=1, max_length=120)
    name: Short = Field(min_length=1, max_length=240)
    kind: Literal["CONCEPT", "ABILITY", "UNKNOWN"]
    description: str | None = Field(default=None, max_length=10000)
    domain: str | None = Field(default=None, max_length=120)
    ordinal: int | None = Field(default=None, ge=0)
    source_status: Literal["AVAILABLE", "NO_REVIEWED_SOURCE"]


class DependencyV2(StrictModel):
    dependency_id: Id
    prerequisite_skill_id: Id
    dependent_skill_id: Id


class MasteryV2(StrictModel):
    skill_id: Id
    status: Literal["KNOWN", "UNKNOWN"]
    mastery_state_id: Id | None = None
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    calculation_method: str | None = Field(default=None, max_length=120)
    calculated_at: datetime | None = None
    evidence_id: Id | None = None

    @model_validator(mode="after")
    def coherent(self):
        fields = (self.mastery_state_id, self.value, self.calculation_method, self.calculated_at)
        if self.status == "UNKNOWN" and any(v is not None for v in (*fields, self.evidence_id)):
            raise ValueError("unknown mastery cannot contain a state")
        if self.status == "KNOWN" and any(v is None for v in fields):
            raise ValueError("known mastery requires a state")
        if self.calculated_at is not None and self.calculated_at.utcoffset() is None:
            raise ValueError("calculated_at must be timezone aware")
        return self


class DocumentSourceV2(StrictModel):
    source_type: Literal["DOCUMENT"] = "DOCUMENT"
    skill_id: Id
    supports_skill_ids: list[Id] = Field(min_length=1, max_length=40)
    semantic_evidence_span_id: Id
    material_id: Id
    material_artifact_id: Id
    source_artifact_id: Id
    source_artifact_sha256: str = Field(min_length=64, max_length=64)
    parse_run_id: Id
    parsed_unit_id: Id
    source_label: str | None = Field(default=None, max_length=80)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    span_sha256: str = Field(min_length=64, max_length=64)
    current_material_artifact: bool
    source_role: str = Field(max_length=30)
    excerpt: str = Field(max_length=4096)


class RepositorySourceV2(StrictModel):
    source_type: Literal["REPOSITORY"] = "REPOSITORY"
    skill_id: Id
    supports_skill_ids: list[Id] = Field(min_length=1, max_length=40)
    semantic_evidence_span_id: Id
    repository_source_id: Id
    repository_snapshot_id: Id
    repository_file_id: Id
    relative_path: str = Field(min_length=1, max_length=1000)
    repository_state: Literal["HEAD", "INDEX", "WORKTREE"]
    repository_blob_id: Id
    repository_blob_sha256: str = Field(min_length=64, max_length=64)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    span_sha256: str = Field(min_length=64, max_length=64)
    source_role: str = Field(max_length=30)
    excerpt: str = Field(max_length=4096)


class DeadlineV2(StrictModel):
    id: Id
    stable_id: Id | None = None
    title: str = Field(max_length=240)
    course_id: Id | None = None
    material_id: Id | None = None
    assessment_id: Id | None = None
    information_status: InformationStatus
    status: DeadlineStatus
    source_id: Id | None = None
    is_date_only: bool
    due_date: date | None = None
    due_at: datetime | None = None

    @model_validator(mode="after")
    def date_shape(self):
        if self.is_date_only and (self.due_date is None or self.due_at is not None):
            raise ValueError("date-only deadline requires only due_date")
        if not self.is_date_only and self.due_date is not None:
            raise ValueError("timed deadline cannot have due_date")
        if self.due_at is not None and self.due_at.utcoffset() is None:
            raise ValueError("timed deadline must be timezone aware")
        return self


class AssessmentV2(StrictModel):
    id: Id
    stable_id: Id | None = None
    name: str = Field(max_length=240)
    assessment_type: str = Field(max_length=40)
    information_status: InformationStatus
    rules_summary: str | None = Field(default=None, max_length=10000)
    source_id: Id | None = None


class RequirementV2(StrictModel):
    id: Id
    stable_id: Id
    information_status: InformationStatus
    source_id: Id | None = None
    payload: dict


class AcademicContextV2(StrictModel):
    deadlines: list[DeadlineV2] = Field(default_factory=list, max_length=16)
    assessments: list[AssessmentV2] = Field(default_factory=list, max_length=8)
    assessment_requirements: list[RequirementV2] = Field(default_factory=list, max_length=64)


class ReturnContractV2(StrictModel):
    expected_result_schema_version: Literal["2.0"] = "2.0"
    session_id: Id
    handoff_id: Id
    course_id: Id
    allowed_skill_ids: list[Id] = Field(max_length=40)
    expected_filename: str = Field(min_length=1, max_length=255)


class LearningHandoffV2(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    protocol_version: Literal["subject-learning-v1"] = "subject-learning-v1"
    handoff_id: Id
    session_id: Id
    generated_at: datetime
    context_fingerprint: str = Field(min_length=64, max_length=64)
    semester: SemesterV2
    course: CourseV2
    goal: str = Field(min_length=1, max_length=4000)
    target_skills: list[SkillV2] = Field(min_length=1, max_length=8)
    prerequisites: list[SkillV2] = Field(max_length=32)
    dependency_edges: list[DependencyV2] = Field(max_length=256)
    mastery_snapshot: list[MasteryV2] = Field(max_length=40)
    academic_context: AcademicContextV2
    instructions: list[str] = Field(min_length=1, max_length=20)
    source_data: list[DocumentSourceV2 | RepositorySourceV2] = Field(max_length=16)
    return_contract: ReturnContractV2

    @field_validator("generated_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("generated_at must be timezone aware")
        return value
