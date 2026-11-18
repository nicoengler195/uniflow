"""Strict, versioned read-only next-action response."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanningSemesterV1(StrictPlanningModel):
    id: str
    stable_id: str
    name: str
    timezone: str
    state: Literal["ACTIVE"]


class PlanningMasteryV1(StrictPlanningModel):
    skill_id: str
    status: Literal["KNOWN", "UNKNOWN"]
    mastery_state_id: str | None = None
    value: float | None = Field(default=None, ge=0, le=1)
    calculation_method: str | None = None
    calculated_at: datetime | None = None
    evidence_id: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.status == "UNKNOWN" and any(x is not None for x in (self.mastery_state_id, self.value, self.calculation_method, self.calculated_at, self.evidence_id)):
            raise ValueError("UNKNOWN mastery must have null detail")
        if self.status == "KNOWN" and (self.mastery_state_id is None or self.value is None or self.calculation_method is None or self.calculated_at is None):
            raise ValueError("KNOWN mastery requires state detail")
        if self.calculated_at is not None and self.calculated_at.utcoffset() is None:
            raise ValueError("calculated_at must be aware")
        return self


class PlanningPrerequisiteV1(StrictPlanningModel):
    skill_id: str
    dependency_ids: list[str] = Field(max_length=128)
    course_skill_id: str | None = None
    mastery: PlanningMasteryV1


class PlanningDeadlineV1(StrictPlanningModel):
    id: str
    stable_id: str | None
    relevance: Literal["COURSE", "ASSESSMENT", "SKILL_MATERIAL"]
    course_id: str | None
    assessment_id: str | None
    material_id: str | None
    information_status: str
    status: str
    is_date_only: bool
    due_date: date | None
    due_at: datetime | None
    timing: Literal["UPCOMING", "OVERDUE", "UNRESOLVED", "EXCLUDED"]
    actionable: bool

    @model_validator(mode="after")
    def date_semantics(self):
        if self.is_date_only and self.due_at is not None:
            raise ValueError("date-only deadline cannot expose due_at")
        if self.due_at is not None and self.due_at.utcoffset() is None:
            raise ValueError("due_at must be aware")
        return self


class PlanningSourceV1(StrictPlanningModel):
    status: Literal["AVAILABLE", "NO_REVIEWED_SOURCE"]
    current_reviewed_source_available: bool
    historical_reviewed_source_available: bool
    provenance_ids: list[str] = Field(max_length=512)
    material_ids: list[str] = Field(max_length=512)


class PlanningFactorV1(StrictPlanningModel):
    code: str = Field(min_length=1, max_length=80)
    refs: list[str] = Field(default_factory=list, max_length=512)


class PlanningActionV1(StrictPlanningModel):
    action_id: str
    rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    priority_class: int = Field(ge=0, le=2)
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str] = Field(min_length=1, max_length=8)
    existing_session_id: str | None
    existing_session_state: str | None
    target_mastery: PlanningMasteryV1 | None
    prerequisite_context: list[PlanningPrerequisiteV1] = Field(max_length=128)
    deadline_context: list[PlanningDeadlineV1] = Field(max_length=512)
    source_status: PlanningSourceV1 | None
    reasons: list[PlanningFactorV1] = Field(max_length=512)
    risk_factors: list[PlanningFactorV1] = Field(max_length=512)

    @model_validator(mode="after")
    def action_shape(self):
        if self.action_type == "START_SKILL_SESSION":
            if len(self.target_skill_ids) != 1 or self.existing_session_id is not None or self.existing_session_state is not None or self.target_mastery is None or self.source_status is None or self.priority_class not in (1, 2):
                raise ValueError("invalid START action")
        elif self.existing_session_id is None or self.existing_session_state is None or self.priority_class != 0:
            raise ValueError("invalid RESUME action")
        return self


class PlanningRecommendationV1(StrictPlanningModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["next-best-action-v1"] = "next-best-action-v1"
    as_of: datetime
    semester: PlanningSemesterV1
    course_filter_id: str | None
    status: Literal["OK", "NO_ELIGIBLE_ACTIONS"]
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    recommendations: list[PlanningActionV1] = Field(max_length=100)
    diagnostics: list[PlanningFactorV1] = Field(max_length=512)

    @field_validator("as_of")
    @classmethod
    def aware(cls, value: datetime):
        if value.utcoffset() is None:
            raise ValueError("as_of must be aware")
        return value

    @model_validator(mode="after")
    def ranks(self):
        if [x.rank for x in self.recommendations] != list(range(1, len(self.recommendations) + 1)):
            raise ValueError("recommendation ranks must be contiguous")
        if (self.status == "OK") != bool(self.recommendations):
            raise ValueError("status and recommendations disagree")
        return self


def canonical_planning_json(value: PlanningRecommendationV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
