"""Safe exact proposal and active-Skill selection projections."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from uniflow.schemas.application_read_v1 import SemesterSummaryV1, StrictReadModel
from uniflow.schemas.material_inbox_read_v1 import ClassificationProposalReadV1, SkillProposalReadV1


class ReviewSourceV1(StrictReadModel):
    source_family: Literal["DOCUMENT", "REPOSITORY", "MANUAL", "ARTIFACT", "MATERIAL"]
    source_artifact_id: str | None = None
    source_sha256: str | None = None
    original_filename: str | None = None
    material_id: str | None = None
    repository_source_id: str | None = None
    repository_stable_id: str | None = None
    repository_snapshot_id: str | None = None
    repository_snapshot_identity: str | None = None
    parse_run_id: str | None = None
    extraction_run_id: str | None = None
    extraction_attempt_id: str | None = None
    source_role: str | None = None
    extractor_name: str | None = None
    extractor_version: str | None = None
    adapter_name: str | None = None
    adapter_version: str | None = None
    attempt_status: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    input_identity_sha256: str | None = None


class EvidencePreviewV1(StrictReadModel):
    span_id: str
    source_family: Literal["DOCUMENT", "REPOSITORY"]
    span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parsed_unit_id: str | None = None
    source_label: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    repository_file_id: str | None = None
    repository_blob_id: str | None = None
    repository_state: str | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    quote_excerpt: str | None = None
    quote_truncated: bool = False


class ClassificationReviewV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["classification-review-v1"] = "classification-review-v1"
    semester: SemesterSummaryV1
    proposal: ClassificationProposalReadV1
    review_context_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported_actions: list[Literal["ACCEPT", "REJECT"]]
    source: ReviewSourceV1


class SkillReviewV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["skill-review-v1"] = "skill-review-v1"
    semester: SemesterSummaryV1
    proposal: SkillProposalReadV1
    review_context_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported_actions: list[Literal["ACCEPT_NEW", "RESOLVE_EXISTING", "REJECT"]]
    source: ReviewSourceV1
    evidence: list[EvidencePreviewV1]


class SkillChoiceV1(StrictReadModel):
    skill_id: str
    name: str
    kind: str
    domain: str | None
    description_excerpt: str | None


class SkillChoicesV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["active-skill-choices-v1"] = "active-skill-choices-v1"
    query: str
    limit: int
    choices: list[SkillChoiceV1]
