"""Strict read-only calendar foundation response."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictCalendarModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CalendarDiagnosticV1(StrictCalendarModel):
    code: str
    refs: list[str] = Field(default_factory=list)


class AcademicOccurrenceV1(StrictCalendarModel):
    occurrence_id: str
    course_id: str
    course_stable_id: str
    source_kind: Literal["RECURRING", "REPLACEMENT", "ADDITION"]
    schedule_entry_id: str | None = None
    schedule_entry_stable_id: str | None = None
    schedule_exception_id: str | None = None
    schedule_exception_stable_id: str | None = None
    local_date: date
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    activity_type: str | None = None
    title: str | None = None
    course_name: str | None = None
    location: str | None = None
    information_status: str
    participation: Literal["REQUIRED", "OPTIONAL", "UNKNOWN"]
    hard_block: bool
    diagnostics: list[CalendarDiagnosticV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def aware_interval(self):
        if self.starts_at is not None and self.starts_at.utcoffset() is None:
            raise ValueError("academic start must be aware")
        if self.ends_at is not None and (self.starts_at is None or self.ends_at.utcoffset() is None
                or self.ends_at.astimezone(timezone.utc) <= self.starts_at.astimezone(timezone.utc)):
            raise ValueError("invalid academic interval")
        if self.hard_block and (self.starts_at is None or self.ends_at is None):
            raise ValueError("hard block needs interval")
        return self


class AvailabilityOccurrenceV1(StrictCalendarModel):
    availability_window_id: str
    local_date: date
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def aware_interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None \
                or self.ends_at.astimezone(timezone.utc) <= self.starts_at.astimezone(timezone.utc):
            raise ValueError("invalid availability interval")
        return self


class ConstraintOccurrenceV1(StrictCalendarModel):
    calendar_constraint_id: str
    constraint_type: Literal["BLOCKED", "AVAILABLE_ADDITION"]
    starts_at: datetime
    ends_at: datetime
    all_day: bool

    @model_validator(mode="after")
    def aware_interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None \
                or self.ends_at.astimezone(timezone.utc) <= self.starts_at.astimezone(timezone.utc):
            raise ValueError("invalid constraint interval")
        return self


class CalendarWeekProjectionV1(StrictCalendarModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["calendar-foundation-v1"] = "calendar-foundation-v1"
    week_start: date
    week_end_exclusive: date
    timezone: str
    semester_id: str
    semester_stable_id: str
    planning_profile_id: str | None
    profile_revision: int | None
    default_action_duration_minutes: int | None
    max_daily_study_minutes: int | None
    availability_occurrences: list[AvailabilityOccurrenceV1]
    constraint_occurrences: list[ConstraintOccurrenceV1]
    academic_occurrences: list[AcademicOccurrenceV1]
    diagnostics: list[CalendarDiagnosticV1]
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("week_start")
    @classmethod
    def monday(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("INVALID_WEEK_START")
        return value


def canonical_calendar_json(value: CalendarWeekProjectionV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
