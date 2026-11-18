from __future__ import annotations

from datetime import datetime

from pydantic import Field

from uniflow.schemas.common import CourseRef, SchemaVersion, StrictModel


class SkillEvidenceV1(StrictModel):
    skill_id: str
    suggested_mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    application: float | None = Field(default=None, ge=0.0, le=1.0)
    transfer: float | None = Field(default=None, ge=0.0, le=1.0)
    independent_solution: bool | None = None
    notes: str | None = None


class LearningResultV1(StrictModel):
    schema_version: SchemaVersion = "1.0"
    session_id: str
    course: CourseRef
    completed_at: datetime
    skill_evidence: list[SkillEvidenceV1] = Field(default_factory=list)
    new_gaps: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)
