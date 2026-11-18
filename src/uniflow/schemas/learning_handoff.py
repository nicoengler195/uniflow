from __future__ import annotations

from datetime import datetime

from pydantic import Field

from uniflow.schemas.common import CourseRef, MaterialRef, SchemaVersion, SkillRef, StrictModel


class PrerequisiteRef(StrictModel):
    skill_id: str
    prerequisite_skill_id: str


class LearningHandoffV1(StrictModel):
    schema_version: SchemaVersion = "1.0"
    session_id: str
    course: CourseRef
    goal: str
    deadline: datetime | None = None
    materials: list[MaterialRef] = Field(default_factory=list)
    skills: list[SkillRef] = Field(default_factory=list)
    prerequisites: list[PrerequisiteRef] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
