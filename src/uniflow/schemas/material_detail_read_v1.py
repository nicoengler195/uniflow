"""Exact detached Material detail."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import model_validator

from uniflow.schemas.application_read_v1 import SemesterSummaryV1, StrictReadModel
from uniflow.schemas.application_read_detail_v1 import MaterialReadV1
from uniflow.schemas.material_inbox_read_v1 import (
    ClassificationProposalReadV1, ExpectationLinkReadV1,
    ProjectedExpectationReadV1, SkillProposalReadV1,
)


class MaterialDetailV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["material-detail-v1"] = "material-detail-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    from_date: date
    through_date: date
    material: MaterialReadV1
    expectation_links: list[ExpectationLinkReadV1]
    projected_expectations: list[ProjectedExpectationReadV1]
    classification_proposals: list[ClassificationProposalReadV1]
    skill_proposals: list[SkillProposalReadV1]

    @model_validator(mode="after")
    def coherent(self):
        if self.as_of.utcoffset() is None or type(self.from_date) is not date or type(self.through_date) is not date:
            raise ValueError("MATERIAL_DETAIL_TIME_INVALID")
        if self.from_date > self.through_date or (self.through_date - self.from_date).days >= 366:
            raise ValueError("INVALID_MATERIAL_DETAIL_RANGE")
        if len({x.proposal_id for x in self.classification_proposals}) != len(self.classification_proposals):
            raise ValueError("CLASSIFICATION_DUPLICATE")
        if len({x.proposal_id for x in self.skill_proposals}) != len(self.skill_proposals):
            raise ValueError("SKILL_PROPOSAL_DUPLICATE")
        return self
