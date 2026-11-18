"""Strict bounded What Changed projection."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from uniflow.schemas.application_read_v1 import SemesterSummaryV1, StrictReadModel

ChangeKind = Literal[
    "AUDIT_IMPORTED", "MATERIAL_VERSION_LINKED", "PARSE_RUN_FINISHED",
    "CLASSIFICATION_DECIDED", "SKILL_PROPOSAL_DECIDED", "PLAN_GENERATED",
    "PLAN_EDITED", "PLAN_ACCEPTED", "BLOCK_MATERIALIZED", "HANDOFF_GENERATED",
    "RESULT_IMPORTED", "MASTERY_RECORDED",
]


class ChangeEventV1(StrictReadModel):
    event_id: str
    kind: ChangeKind
    occurred_at: datetime
    semester_id: str
    semester_stable_id: str
    course_ids: list[str]
    course_stable_ids: list[str]
    entity_type: str
    entity_id: str
    status: str | None = None
    refs: list[str] = Field(default_factory=list)

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value: datetime):
        if value.utcoffset() is None:
            raise ValueError("EVENT_TIME_MUST_BE_AWARE")
        return value

    @model_validator(mode="after")
    def identity(self):
        if self.event_id != f"{self.kind}:{self.entity_id}":
            raise ValueError("INVALID_EVENT_ID")
        if len(self.course_ids) != len(self.course_stable_ids):
            raise ValueError("INVALID_COURSE_SCOPE")
        if self.course_ids != sorted(set(self.course_ids)) or len(set(self.course_stable_ids)) != len(self.course_stable_ids):
            raise ValueError("INVALID_COURSE_SCOPE")
        return self


class ChangeFeedV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["change-feed-v1"] = "change-feed-v1"
    as_of: datetime
    since: datetime
    through: datetime
    semester: SemesterSummaryV1
    course_filter_stable_id: str | None
    events: list[ChangeEventV1]
    returned_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)

    @field_validator("as_of", "since", "through")
    @classmethod
    def aware(cls, value: datetime):
        if value.utcoffset() is None:
            raise ValueError("TIME_MUST_BE_AWARE")
        return value

    @model_validator(mode="after")
    def bounds(self):
        if (not self.since < self.through <= self.as_of or
                self.returned_count != len(self.events) or len(self.events) > self.limit):
            raise ValueError("INVALID_FEED_RANGE_OR_COUNT")
        since = self.since.astimezone(timezone.utc)
        through = self.through.astimezone(timezone.utc)
        seen: set[str] = set()
        previous: ChangeEventV1 | None = None
        for event in self.events:
            instant = event.occurred_at.astimezone(timezone.utc)
            if (not since <= instant < through or event.event_id in seen or
                    event.semester_id != self.semester.id or
                    event.semester_stable_id != self.semester.stable_id):
                raise ValueError("INVALID_FEED_EVENTS")
            if previous is not None:
                prior_instant = previous.occurred_at.astimezone(timezone.utc)
                if (prior_instant < instant or
                        (prior_instant == instant and
                         (previous.kind, previous.entity_id) > (event.kind, event.entity_id))):
                    raise ValueError("INVALID_FEED_EVENTS")
            seen.add(event.event_id)
            previous = event
        return self


def canonical_change_feed_json(value: ChangeFeedV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
