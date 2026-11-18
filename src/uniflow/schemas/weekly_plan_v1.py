"""Strict immutable weekly plan revision and assessment contracts."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uniflow.schemas.weekly_schedule_v1 import WeeklyDeadlineConstraintV1, WeeklySemesterV1


class StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanSourceV1(StrictPlanModel):
    nba_artifact_id: str
    nba_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nba_context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_artifact_id: str
    calendar_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    weekly_schedule_artifact_id: str
    weekly_schedule_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduler_context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_as_of: datetime
    source_course_filter_id: str | None
    source_limit: int = Field(ge=1, le=100)
    duration_overrides: dict[str, int]

    @field_validator("source_as_of")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("SOURCE_AS_OF_MUST_BE_AWARE")
        return value

    @field_validator("duration_overrides")
    @classmethod
    def valid_durations(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or type(minutes) is not int or minutes <= 0 for key, minutes in value.items()):
            raise ValueError("INVALID_ACTION_DURATION")
        return value


class PlanBlockV1(StrictPlanModel):
    block_id: str
    source_scheduler_block_id: str | None
    action_id: str
    nba_rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str] = Field(min_length=1)
    existing_session_id: str | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(strict=True, gt=0)
    duration_source: Literal["GENERATED_EXPLICIT_OVERRIDE", "GENERATED_PROFILE_DEFAULT", "MANUAL_EDIT"]
    deadline_constraints: list[WeeklyDeadlineConstraintV1]
    placement_source: Literal["GENERATED", "MANUAL_EDIT"]

    @model_validator(mode="after")
    def interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("BLOCK_TIMES_MUST_BE_AWARE")
        if (self.ends_at.astimezone(timezone.utc) - self.starts_at.astimezone(timezone.utc)).total_seconds() != self.duration_minutes * 60:
            raise ValueError("BLOCK_DURATION_MISMATCH")
        return self


class PlanUnscheduledActionV1(StrictPlanModel):
    action_id: str
    nba_rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str] = Field(min_length=1)
    existing_session_id: str | None
    resolved_duration_minutes: int | None = Field(strict=True)
    duration_source: Literal["GENERATED_EXPLICIT_OVERRIDE", "GENERATED_PROFILE_DEFAULT", "MISSING", "MANUAL_EDIT"]
    primary_reason: Literal["NO_PLANNING_PROFILE", "MISSING_DURATION", "NO_AVAILABLE_WINDOW",
        "NO_PRE_DEADLINE_SLOT", "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL", "DAILY_LIMIT_REACHED",
        "NO_FREE_INTERVAL", "USER_REMOVED"]
    deadline_constraints: list[WeeklyDeadlineConstraintV1]
    references: list[str]

    @model_validator(mode="after")
    def duration_shape(self):
        if self.resolved_duration_minutes is not None and (type(self.resolved_duration_minutes) is not int or self.resolved_duration_minutes <= 0):
            raise ValueError("INVALID_BLOCK_DURATION")
        if (self.duration_source == "MISSING") != (self.resolved_duration_minutes is None):
            raise ValueError("INCOHERENT_UNSCHEDULED_DURATION")
        return self


class WeeklyPlanRevisionV1(StrictPlanModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["weekly-plan-revision-v1"] = "weekly-plan-revision-v1"
    plan_id: str
    revision_id: str
    revision_number: int = Field(ge=1)
    revision_kind: Literal["GENERATED", "MANUAL_EDIT"]
    parent_revision_id: str | None
    created_at: datetime
    semester: WeeklySemesterV1
    week_start: date
    week_end_exclusive: date
    timezone: str
    source: PlanSourceV1
    blocks: list[PlanBlockV1]
    unscheduled_actions: list[PlanUnscheduledActionV1]

    @model_validator(mode="after")
    def shape(self):
        if self.created_at.utcoffset() is None or self.week_start.weekday() != 0 or self.week_end_exclusive != self.week_start.fromordinal(self.week_start.toordinal() + 7):
            raise ValueError("INVALID_PLAN_REVISION")
        if len({b.block_id for b in self.blocks}) != len(self.blocks):
            raise ValueError("DUPLICATE_PLAN_BLOCK")
        ids = [b.action_id for b in self.blocks] + [u.action_id for u in self.unscheduled_actions]
        if len(ids) != len(set(ids)):
            raise ValueError("DUPLICATE_PLAN_ACTION")
        if self.blocks != sorted(self.blocks, key=lambda b: (
                b.starts_at.astimezone(timezone.utc), b.ends_at.astimezone(timezone.utc), b.nba_rank, b.block_id)):
            raise ValueError("NONCANONICAL_PLAN_BLOCK_ORDER")
        if self.unscheduled_actions != sorted(self.unscheduled_actions, key=lambda u: (u.nba_rank, u.action_id)):
            raise ValueError("NONCANONICAL_UNSCHEDULED_ORDER")
        return self


class WeeklyPlanAssessmentV1(StrictPlanModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str
    revision_id: str
    revision_number: int
    checked_at: datetime
    status: Literal["FRESH", "STALE", "INVALID"]
    factors: list[Literal["NBA_CONTEXT_CHANGED", "CALENDAR_CONTEXT_CHANGED", "SOURCE_ARTIFACT_INVALID",
        "WEEK_ELAPSED", "BLOCK_IN_PAST", "ACTION_NO_LONGER_VALID", "BLOCK_CONFLICT"]]
    nba_match: bool | None
    calendar_match: bool | None


def canonical_weekly_plan_revision_json(value: WeeklyPlanRevisionV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")
