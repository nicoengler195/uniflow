"""Detached presentation labels around the verified Session read contract."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.application_read_v1 import StrictReadModel, SemesterSummaryV1
from uniflow.schemas.application_read_detail_v1 import SessionReadV1


class SessionTargetLabelV1(StrictReadModel):
    skill_id: str
    name: str
    ordinal: int = Field(ge=0)


class SessionDetailV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["session-detail-v1"] = "session-detail-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    course_name: str
    session: SessionReadV1
    target_labels: list[SessionTargetLabelV1]

    @model_validator(mode="after")
    def coherent(self):
        if self.as_of.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        if [(x.skill_id, x.ordinal) for x in self.target_labels] != [
            (x.skill_id, x.ordinal) for x in self.session.targets
        ]:
            raise ValueError("SESSION_TARGET_LABELS_INVALID")
        return self
