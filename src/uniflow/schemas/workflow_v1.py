"""Strict in-process learning workflow command contracts."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkflowModelV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RequestV1(WorkflowModelV1):
    schema_version: Literal["1.0"] = "1.0"


class SuccessV1(WorkflowModelV1):
    schema_version: Literal["1.0"] = "1.0"


class CreateSessionRequestV1(RequestV1):
    course_id: str = Field(min_length=1, max_length=80)
    target_skill_ids: list[str] = Field(min_length=1, max_length=8)
    goal: str = Field(min_length=1, max_length=4000)
    planned_for: datetime | None = None

    @field_validator("planned_for")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("planned_for must be aware")
        return value


class GenerateHandoffRequestV1(RequestV1):
    session_id: str = Field(min_length=1, max_length=80)


class ExportExchangeRequestV1(RequestV1):
    handoff_id: str = Field(min_length=1, max_length=80)
    destination_directory: Path = Field(exclude=True)


class StartSessionRequestV1(RequestV1):
    session_id: str = Field(min_length=1, max_length=80)


class ImportResultFileRequestV1(RequestV1):
    path: Path = Field(exclude=True)


class ApplyMasteryRequestV1(RequestV1):
    result_id: str = Field(min_length=1, max_length=80)


Sha256 = str


class CreateSessionSuccessV1(SuccessV1):
    command: Literal["learning.session.create"] = "learning.session.create"
    outcome: Literal["CREATED"] = "CREATED"
    session_id: str


class GenerateHandoffSuccessV1(SuccessV1):
    command: Literal["learning.handoff.generate"] = "learning.handoff.generate"
    outcome: Literal["GENERATED", "ALREADY_GENERATED"]
    handoff_id: str
    session_id: str
    revision: int = Field(ge=1)
    artifact_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class ExportExchangeSuccessV1(SuccessV1):
    command: Literal["learning.handoff.export_exchange"] = "learning.handoff.export_exchange"
    outcome: Literal["EXPORTED", "ALREADY_EXPORTED"]
    session_id: str
    handoff_id: str
    handoff_artifact_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    result_id: str
    directory_name: str
    handoff_filename: Literal["handoff.json"] = "handoff.json"
    envelope_filename: Literal["handoff-envelope.json"] = "handoff-envelope.json"
    result_filename: str


class StartSessionSuccessV1(SuccessV1):
    command: Literal["learning.session.start"] = "learning.session.start"
    outcome: Literal["STARTED", "ALREADY_STARTED"]
    session_id: str


class ImportResultFileSuccessV1(SuccessV1):
    command: Literal["learning.result.import_file"] = "learning.result.import_file"
    outcome: Literal["IMPORTED", "ALREADY_IMPORTED"]
    result_id: str
    artifact_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_count: int = Field(ge=0)


class ApplyMasterySuccessV1(SuccessV1):
    command: Literal["learning.mastery.apply"] = "learning.mastery.apply"
    outcome: Literal["APPLIED", "ALREADY_APPLIED"]
    result_id: str
    session_id: str
    created_mastery_state_ids: tuple[str, ...]
    skipped_skill_ids: tuple[str, ...]
