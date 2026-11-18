from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uniflow.domain.enums import InformationStatus

SchemaVersion = Literal["1.0"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvenanceModel(StrictModel):
    source_type: str
    uri: str | None = None
    location: str | None = None
    information_status: InformationStatus = InformationStatus.UNKNOWN
    description: str | None = None


class CourseRef(StrictModel):
    stable_id: str
    name: str


class MaterialRef(StrictModel):
    stable_id: str | None = None
    title: str
    uri: str | None = None
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    provenance: ProvenanceModel | None = None


class SkillRef(StrictModel):
    skill_id: str
    name: str
    current_mastery: float | None = Field(default=None, ge=0.0, le=1.0)
    target_mastery: float | None = Field(default=None, ge=0.0, le=1.0)


class TimestampedModel(StrictModel):
    created_at: datetime | None = None
