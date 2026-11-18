"""Separate technical findings. These records never grant human approval."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

FindingStatus = Literal["PASS", "CONFLICT", "MISSING", "UNKNOWN", "UNVERIFIED", "ERROR"]


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Finding(ReportModel):
    status: FindingStatus
    code: str
    object_ids: tuple[str, ...]
    location: str
    reason: str
    # Exact values and JSON paths preserve both sides without changing inputs.
    values: tuple[tuple[str, str], ...] = ()


class CheckedArtifact(ReportModel):
    artifact_id: str
    sha256: str
    byte_length: int


class EvidenceValidationReport(ReportModel):
    validator_version: Literal["uniflow-evidence-validator-i2.0", "uniflow-evidence-validator-i2.1"] = "uniflow-evidence-validator-i2.1"
    sidecar_id: str | None
    sidecar_version: str | None
    sidecar_sha256: str
    declared_result_binding: tuple[tuple[str, str | None], ...]
    checked_artifacts: tuple[CheckedArtifact, ...]
    findings: tuple[Finding, ...]
    not_checked: tuple[str, ...]
    human_review_required: Literal[True] = True
