"""Strict, human-authored decisions for one exact semester audit candidate."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from uniflow.schemas.common import StrictModel


class AuditResolutionDecisionV1(StrictModel):
    entity_type: Literal["semester", "course", "assessment", "deadline", "requirement", "schedule", "exception", "material_release"]
    stable_id: str = Field(min_length=1)
    expected_current_fact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_fact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_current_revision_id: str = Field(min_length=1)
    action: Literal["ACCEPT_REPLACEMENT"]
    reason: str = Field(min_length=1)
    actor: Literal["MANUAL_USER"]
    decided_at: datetime

    @field_validator("stable_id", "expected_current_revision_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("value must be nonblank and trimmed")
        return value

    @field_validator("decided_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must have a timezone")
        return value


class SemesterAuditResolutionV1(StrictModel):
    schema_version: Literal["1.0"]
    kind: Literal["semester_audit_resolution"]
    semester: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
    candidate_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolutions: list[AuditResolutionDecisionV1] = Field(min_length=1)

    @field_validator("semester", "audit_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("value must be nonblank and trimmed")
        return value

    @model_validator(mode="after")
    def unique_decisions(self) -> SemesterAuditResolutionV1:
        identities = [(item.entity_type, item.stable_id) for item in self.resolutions]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate resolution decision")
        return self
