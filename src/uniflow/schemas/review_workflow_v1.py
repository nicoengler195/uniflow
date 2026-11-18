"""Strict Phase 4B3 review commands."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.workflow_v1 import RequestV1, SuccessV1


class ReviewRequestV1(RequestV1):
    semester_id: str = Field(min_length=1, max_length=80)
    proposal_id: str = Field(min_length=1, max_length=80)
    expected_review_context_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClassificationAcceptRequestV1(ReviewRequestV1):
    reason: str | None = Field(default=None, max_length=1000)


class ClassificationRejectRequestV1(ReviewRequestV1):
    reason: str | None = Field(default=None, max_length=1000)


class SkillAcceptNewRequestV1(ReviewRequestV1):
    pass


class SkillResolveExistingRequestV1(ReviewRequestV1):
    existing_skill_id: str = Field(min_length=1, max_length=80)


class SkillRejectRequestV1(ReviewRequestV1):
    reason: str | None = Field(default=None, max_length=1000)


class ReviewSuccessV1(SuccessV1):
    command: Literal["review.classification.accept", "review.classification.reject",
        "review.skill.accept_new", "review.skill.resolve_existing", "review.skill.reject"]
    outcome: Literal["ACCEPTED", "ALREADY_ACCEPTED", "REJECTED", "ALREADY_REJECTED"]
    proposal_id: str
    status: Literal["ACCEPTED", "REJECTED"]
    decision_action: str | None = None
    accepted_skill_id: str | None = None
    association_id: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        accepting = self.command.endswith(("accept", "accept_new", "resolve_existing"))
        if accepting != (self.status == "ACCEPTED"):
            raise ValueError("REVIEW_SUCCESS_STATUS_INVALID")
        if accepting != (self.outcome in {"ACCEPTED", "ALREADY_ACCEPTED"}):
            raise ValueError("REVIEW_SUCCESS_OUTCOME_INVALID")
        if self.command.startswith("review.skill.") and accepting and self.accepted_skill_id is None:
            raise ValueError("REVIEW_SKILL_ID_MISSING")
        if not self.command.startswith("review.skill.") and self.accepted_skill_id is not None:
            raise ValueError("REVIEW_CLASSIFICATION_SKILL_ID_INVALID")
        return self
