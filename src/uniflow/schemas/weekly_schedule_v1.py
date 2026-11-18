"""Strict, derived Phase 3E2B2 weekly schedule response."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uniflow.schemas.calendar_v1 import CalendarDiagnosticV1
from uniflow.schemas.planning_v1 import PlanningFactorV1


class StrictWeeklyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WeeklySemesterV1(StrictWeeklyModel):
    id: str
    stable_id: str


class WeeklyDeadlineConstraintV1(StrictWeeklyModel):
    deadline_id: str
    deadline_stable_id: str | None
    kind: Literal["TIMED", "DATE_ONLY"]
    due_at: datetime | None
    due_date: date | None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "TIMED":
            if self.due_at is None or self.due_at.utcoffset() is None or self.due_date is not None:
                raise ValueError("INVALID_TIMED_DEADLINE_CONSTRAINT")
        elif self.due_date is None or self.due_at is not None:
            raise ValueError("INVALID_DATE_ONLY_DEADLINE_CONSTRAINT")
        return self


class WeeklyBlockV1(StrictWeeklyModel):
    block_id: str
    action_id: str
    nba_rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str] = Field(min_length=1)
    existing_session_id: str | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(gt=0)
    duration_source: Literal["EXPLICIT_OVERRIDE", "PROFILE_DEFAULT"]
    deadline_constraints: list[WeeklyDeadlineConstraintV1]
    placement_reason: Literal["EARLIEST_FEASIBLE"] = "EARLIEST_FEASIBLE"

    @model_validator(mode="after")
    def interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("BLOCK_TIMES_MUST_BE_AWARE")
        elapsed = self.ends_at.astimezone(timezone.utc) - self.starts_at.astimezone(timezone.utc)
        if elapsed.total_seconds() != self.duration_minutes * 60:
            raise ValueError("BLOCK_DURATION_MISMATCH")
        return self


class WeeklyUnscheduledActionV1(StrictWeeklyModel):
    action_id: str
    nba_rank: int = Field(ge=1)
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str] = Field(min_length=1)
    existing_session_id: str | None
    resolved_duration_minutes: int | None
    duration_source: Literal["EXPLICIT_OVERRIDE", "PROFILE_DEFAULT", "MISSING"]
    primary_reason: Literal[
        "NO_PLANNING_PROFILE", "MISSING_DURATION", "NO_AVAILABLE_WINDOW",
        "NO_PRE_DEADLINE_SLOT", "DURATION_EXCEEDS_MAX_LEGAL_INTERVAL",
        "DAILY_LIMIT_REACHED", "NO_FREE_INTERVAL",
    ]
    deadline_constraints: list[WeeklyDeadlineConstraintV1]
    references: list[str]


class WeeklyDiagnosticV1(StrictWeeklyModel):
    code: str
    refs: list[str] = Field(default_factory=list)


class WeeklyScheduleV1(StrictWeeklyModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["weekly-scheduler-v1"] = "weekly-scheduler-v1"
    week_start: date
    week_end_exclusive: date
    timezone: str
    as_of: datetime
    semester: WeeklySemesterV1
    nba_policy_version: Literal["next-best-action-v1"]
    nba_context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    nba_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_policy_version: Literal["calendar-foundation-v1"]
    calendar_context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planning_profile_id: str | None
    profile_revision: int | None
    default_action_duration_minutes: int | None
    max_daily_study_minutes: int | None
    status: Literal["NO_ELIGIBLE_ACTIONS", "OK", "PARTIAL", "NO_SCHEDULED_ACTIONS"]
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduled_blocks: list[WeeklyBlockV1]
    unscheduled_actions: list[WeeklyUnscheduledActionV1]
    nba_diagnostics: list[PlanningFactorV1]
    calendar_diagnostics: list[CalendarDiagnosticV1]
    scheduler_diagnostics: list[WeeklyDiagnosticV1]


def canonical_weekly_schedule_json(value: WeeklyScheduleV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
