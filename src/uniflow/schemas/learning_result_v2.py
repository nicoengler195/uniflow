"""Strict, bounded observations returned for one immutable learning handoff."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, field_validator, model_validator

from uniflow.schemas.common import StrictModel

Score = StrictFloat


class SkillEvidenceV2(StrictModel):
    skill_id: str = Field(min_length=1, max_length=120)
    recall: Score | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    application: Score | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    transfer: Score | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    independent_solution: StrictBool | None = None
    suggested_mastery: Score | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def observed(self):
        if all(getattr(self, key) is None for key in
               ("recall", "application", "transfer", "independent_solution")):
            raise ValueError("RESULT_UNOBSERVED_SKILL")
        return self


class LearningResultV2(StrictModel):
    schema_version: Literal["2.0"]
    protocol_version: Literal["subject-learning-v1"]
    result_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=80)
    handoff_id: str = Field(min_length=1, max_length=80)
    course_id: str = Field(min_length=1, max_length=80)
    handoff_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    session_outcome: Literal["COMPLETED", "PARTIAL"]
    skill_evidence: list[SkillEvidenceV2] = Field(max_length=40)
    new_gaps: list[str] = Field(default_factory=list, max_length=32)
    errors: list[str] = Field(default_factory=list, max_length=32)
    recommended_next_actions: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("completed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("RESULT_NAIVE_COMPLETED_AT")
        return value

    @field_validator("completed_at", mode="before")
    @classmethod
    def timestamp_is_text(cls, value):
        if not isinstance(value, str):
            raise ValueError("RESULT_INVALID_COMPLETED_AT")
        return value

    @field_validator("new_gaps", "errors", "recommended_next_actions")
    @classmethod
    def bounded_text(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("RESULT_TEXT_INVALID")
        return values

    @model_validator(mode="after")
    def coherent(self):
        ids = [item.skill_id for item in self.skill_evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("RESULT_DUPLICATE_SKILL")
        if self.session_outcome == "COMPLETED" and not ids:
            raise ValueError("RESULT_COMPLETED_WITHOUT_EVIDENCE")
        return self
