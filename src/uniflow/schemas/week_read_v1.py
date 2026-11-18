"""Detached, versioned Week projection. Captured plan and current overlay stay separate."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from uniflow.schemas.application_read_v1 import (
    DeadlineItemV1, MaterializationV1, PlanIdentityV1, ReadDiagnosticV1, SemesterSummaryV1,
    StrictReadModel,
)
from uniflow.schemas.calendar_v1 import CalendarWeekProjectionV1
from uniflow.schemas.weekly_plan_v1 import PlanUnscheduledActionV1, WeeklyPlanAssessmentV1
from uniflow.schemas.weekly_schedule_v1 import WeeklyDeadlineConstraintV1


class PlanBlockReadV1(StrictReadModel):
    block_id: str
    source_scheduler_block_id: str | None
    action_id: str
    nba_rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str]
    existing_session_id: str | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(gt=0)
    placement_source: Literal["GENERATED", "MANUAL_EDIT"]
    duration_source: Literal["GENERATED_EXPLICIT_OVERRIDE", "GENERATED_PROFILE_DEFAULT", "MANUAL_EDIT"]
    deadline_context: list[WeeklyDeadlineConstraintV1]
    materialization: MaterializationV1 | None
    learning_session_id: str | None
    learning_session_state: str | None
    goal: str | None
    diagnostics: list[ReadDiagnosticV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("BLOCK_TIMES_MUST_BE_AWARE")
        if (self.ends_at.astimezone(timezone.utc) - self.starts_at.astimezone(timezone.utc)).total_seconds() != self.duration_minutes * 60:
            raise ValueError("BLOCK_DURATION_MISMATCH")
        return self


class SourceCalendarIdentityV1(StrictReadModel):
    artifact_id: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class WeekRevisionReadV1(StrictReadModel):
    revision_id: str
    revision_number: int = Field(ge=1)
    revision_kind: Literal["GENERATED", "MANUAL_EDIT"]
    parent_revision_id: str | None
    created_at: datetime
    source_calendar_identity: SourceCalendarIdentityV1 | None
    blocks: list[PlanBlockReadV1] | None
    unscheduled_actions: list[PlanUnscheduledActionV1] | None
    revision_assessment: WeeklyPlanAssessmentV1
    acceptance_id: str | None = None
    accepted_at: datetime | None = None
    acceptance_actor: str | None = None

    @field_validator("created_at", "accepted_at")
    @classmethod
    def aware_time(cls, value: datetime | None):
        if value is not None and value.utcoffset() is None:
            raise ValueError("REVISION_TIME_MUST_BE_AWARE")
        return value

    @model_validator(mode="after")
    def coherent(self):
        invalid = self.revision_assessment.status == "INVALID"
        details = (self.source_calendar_identity, self.blocks, self.unscheduled_actions)
        if (invalid and any(value is not None for value in details)) or (
                not invalid and any(value is None for value in details)):
            raise ValueError("INVALID_REVISION_DISPLAY")
        if (self.acceptance_id is None) != (self.accepted_at is None) or (self.acceptance_id is None) != (self.acceptance_actor is None):
            raise ValueError("INVALID_ACCEPTANCE_DISPLAY")
        return self


class RevisionHistoryItemV1(StrictReadModel):
    revision_number: int = Field(ge=1)
    revision_kind: Literal["GENERATED", "MANUAL_EDIT"]
    accepted: bool
    current: bool
    block_count: int | None = Field(default=None, ge=0)
    unscheduled_count: int | None = Field(default=None, ge=0)


class WeekViewV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["week-view-v1"] = "week-view-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    week_start: date
    week_end_exclusive: date
    plan: PlanIdentityV1
    accepted_plan: WeekRevisionReadV1 | None
    current_draft: WeekRevisionReadV1 | None
    current_calendar: CalendarWeekProjectionV1
    diagnostics: list[ReadDiagnosticV1]
    deadline_items: list[DeadlineItemV1] = Field(default_factory=list)
    revision_history: list[RevisionHistoryItemV1] = Field(default_factory=list)
    course_names: dict[str, str] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def aware(cls, value: datetime):
        if value.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        return value

    @model_validator(mode="after")
    def week(self):
        if self.week_start.weekday() != 0 or (self.week_end_exclusive - self.week_start).days != 7:
            raise ValueError("INVALID_WEEK_START")
        return self


def canonical_week_json(value: WeekViewV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
