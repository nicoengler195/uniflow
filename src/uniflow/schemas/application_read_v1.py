"""Detached, GUI-independent Phase 4A1 application read contracts."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uniflow.schemas.calendar_v1 import (
    AcademicOccurrenceV1, AvailabilityOccurrenceV1, CalendarDiagnosticV1,
    ConstraintOccurrenceV1,
)
from uniflow.schemas.planning_v1 import PlanningActionV1, PlanningFactorV1
from uniflow.schemas.weekly_schedule_v1 import WeeklyDeadlineConstraintV1


class StrictReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ReadDiagnosticV1(StrictReadModel):
    code: str
    refs: list[str] = Field(default_factory=list)


class SemesterSummaryV1(StrictReadModel):
    id: str
    stable_id: str
    name: str
    state: str
    timezone: str


class SetupStatusV1(StrictReadModel):
    semester_loaded: bool
    audit_present: bool
    planning_profile_present: bool
    active_availability_window_count: int = Field(ge=0)
    active_course_skill_count: int = Field(ge=0)
    reviewed_skill_available: bool
    weekly_plan_present: bool
    accepted_weekly_plan_present: bool
    diagnostics: list[ReadDiagnosticV1]


class MasteryCoverageV1(StrictReadModel):
    active_skill_count: int = Field(ge=0)
    known_mastery_count: int = Field(ge=0)
    unknown_mastery_count: int = Field(ge=0)
    known_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    mean_known_mastery: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def coherent(self):
        if self.known_mastery_count + self.unknown_mastery_count != self.active_skill_count:
            raise ValueError("INVALID_MASTERY_COVERAGE")
        if self.active_skill_count == 0:
            if self.known_coverage_ratio is not None or self.mean_known_mastery is not None:
                raise ValueError("INVALID_MASTERY_COVERAGE")
        elif self.known_coverage_ratio != self.known_mastery_count / self.active_skill_count:
            raise ValueError("INVALID_MASTERY_COVERAGE")
        if (self.mean_known_mastery is None) != (self.known_mastery_count == 0):
            raise ValueError("INVALID_MASTERY_COVERAGE")
        return self


class MaterialFactsV1(StrictReadModel):
    logical_material_count: int = Field(ge=0)
    current_artifact_count: int = Field(ge=0)
    successfully_parsed_current_artifact_count: int = Field(ge=0)


class DeadlineItemV1(StrictReadModel):
    deadline_id: str
    deadline_stable_id: str | None
    course_id: str | None
    title: str
    status: str
    information_status: str
    is_date_only: bool
    due_date: date | None
    due_at: datetime | None
    timing: Literal["UPCOMING_OPEN", "OVERDUE_OPEN", "KNOWN_DUE_OTHER_STATUS", "NONAUTHORITATIVE", "UNDATED"]

    @model_validator(mode="after")
    def date_shape(self):
        if self.is_date_only:
            if self.due_at is not None or (self.due_date is None and self.timing != "UNDATED"):
                raise ValueError("INVALID_DEADLINE_DATE_SHAPE")
        elif self.due_date is not None or (self.due_at is not None and self.due_at.utcoffset() is None):
            raise ValueError("INVALID_DEADLINE_DATE_SHAPE")
        return self


class DeadlineSummaryV1(StrictReadModel):
    authoritative_upcoming_count: int = Field(ge=0)
    authoritative_overdue_count: int = Field(ge=0)
    known_due_other_status_count: int = Field(ge=0)
    nonauthoritative_or_undated_count: int = Field(ge=0)
    authoritative_upcoming: list[DeadlineItemV1]
    authoritative_overdue: list[DeadlineItemV1]
    due_fact_diagnostics: list[DeadlineItemV1]


class SessionItemV1(StrictReadModel):
    session_id: str
    course_id: str
    course_stable_id: str
    goal: str
    state: str
    # SQLite stores this as a local wall time without an offset or DST fold.
    planned_local_wall_time: str | None
    target_skill_ids: list[str]
    current_handoff_id: str | None


class SessionInboxSummaryV1(StrictReadModel):
    resumable_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    resumable_and_processing: list[SessionItemV1]


class PlanIdentityV1(StrictReadModel):
    plan_id: str | None
    current_revision_id: str | None
    current_revision_number: int | None
    accepted_revision_id: str | None
    accepted_revision_number: int | None
    accepted_is_current: bool | None


class PlannedBlockSummaryV1(StrictReadModel):
    block_id: str
    action_id: str
    course_id: str
    course_stable_id: str
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def aware_interval(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("BLOCK_TIMES_MUST_BE_AWARE")
        if self.ends_at.astimezone(timezone.utc) <= self.starts_at.astimezone(timezone.utc):
            raise ValueError("INVALID_BLOCK_INTERVAL")
        return self


class CourseCardV1(StrictReadModel):
    course_id: str
    course_stable_id: str
    parent_course_id: str | None = None
    release_rule_count: int = Field(default=0, ge=0)
    name: str
    mastery_coverage: MasteryCoverageV1
    materials: MaterialFactsV1
    upcoming_authoritative_deadline_count: int = Field(ge=0)
    resumable_session_count: int = Field(ge=0)
    processing_session_count: int = Field(ge=0)
    accepted_planned_block_count: int = Field(ge=0)


class HomeDashboardV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["home-dashboard-v1"] = "home-dashboard-v1"
    as_of: datetime
    local_date: date
    week_start: date
    semester: SemesterSummaryV1
    setup_status: SetupStatusV1
    plan: PlanIdentityV1
    next_accepted_block: PlannedBlockSummaryV1 | None
    next_planner_recommendation: PlanningActionV1 | None
    planner_status: Literal["OK", "NO_ELIGIBLE_ACTIONS", "UNSUPPORTED_SEMESTER_STATE", "PLANNER_COMPATIBILITY_DIAGNOSTIC"]
    planner_diagnostics: list[PlanningFactorV1 | ReadDiagnosticV1]
    deadlines: DeadlineSummaryV1
    sessions: SessionInboxSummaryV1
    courses: list[CourseCardV1]
    today_academic_occurrences: list[AcademicOccurrenceV1] = Field(default_factory=list)
    upcoming_academic_occurrences: list[AcademicOccurrenceV1] = Field(default_factory=list)
    pending_review_count: int = Field(default=0, ge=0)

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        return value


class MaterializationV1(StrictReadModel):
    status: Literal["NOT_MATERIALIZED", "MATERIALIZED"]
    execution_kind: Literal["CREATED_SESSION", "RESUMED_SESSION"] | None = None
    execution_id: str | None = None
    materialized_at: datetime | None = None

    @model_validator(mode="after")
    def coherent(self):
        present = self.status == "MATERIALIZED"
        if any((self.execution_kind is not None, self.execution_id is not None,
                self.materialized_at is not None)) != present:
            raise ValueError("INVALID_MATERIALIZATION_SHAPE")
        if present and (self.execution_kind is None or self.execution_id is None or
                self.materialized_at is None or self.materialized_at.utcoffset() is None):
            raise ValueError("INVALID_MATERIALIZATION_SHAPE")
        return self


class TodayBlockV1(StrictReadModel):
    block_id: str
    action_id: str
    action_type: Literal["RESUME_SESSION", "START_SKILL_SESSION"]
    nba_rank: int = Field(ge=1)
    course_id: str
    course_stable_id: str
    target_skill_ids: list[str]
    existing_session_id: str | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(gt=0)
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


class TodayViewV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["today-view-v1"] = "today-view-v1"
    as_of: datetime
    local_date: date
    week_start: date
    semester: SemesterSummaryV1
    plan: PlanIdentityV1
    blocks: list[TodayBlockV1]
    academic_occurrences: list[AcademicOccurrenceV1]
    availability_occurrences: list[AvailabilityOccurrenceV1]
    constraint_occurrences: list[ConstraintOccurrenceV1]
    calendar_diagnostics: list[CalendarDiagnosticV1]
    diagnostics: list[ReadDiagnosticV1]

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        return value


def _canonical(value: StrictReadModel) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_home_json(value: HomeDashboardV1) -> bytes:
    return _canonical(value)


def canonical_today_json(value: TodayViewV1) -> bytes:
    return _canonical(value)
