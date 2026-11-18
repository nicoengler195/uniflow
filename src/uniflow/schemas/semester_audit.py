from __future__ import annotations

from datetime import date, time as clock_time
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from uniflow.domain.enums import AssessmentType, InformationStatus, SemesterState
from uniflow.schemas.common import ProvenanceModel, StrictModel


class AuditFact(StrictModel):
    stable_id: str = Field(min_length=1)
    information_status: InformationStatus = InformationStatus.UNKNOWN
    provenance: ProvenanceModel | None = None


class AuditDeadlineV1(AuditFact):
    title: str
    due_date: date | None = None
    due_time: clock_time | None = None
    assessment_stable_id: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def valid_time(self) -> AuditDeadlineV1:
        if self.due_time is not None and self.due_date is None:
            raise ValueError("due_time requires due_date")
        return self


class AuditRequirementV1(AuditFact):
    requirement_type: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    human_summary: str
    assessment_stable_id: str | None = None

    @field_validator("parameters")
    @classmethod
    def json_compatible(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        json.dumps(value, allow_nan=False)
        return value


class AuditAssessmentV1(AuditFact):
    name: str
    assessment_type: AssessmentType = AssessmentType.UNKNOWN
    rules_summary: str | None = None


class ScheduleEntryV1(AuditFact):
    activity_type: str
    weekday: Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    start_time: clock_time
    end_time: clock_time | None = None
    location: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    calendar_week_parity: Literal["ALL", "ODD", "EVEN"] = "ALL"
    participation: Literal["REQUIRED", "OPTIONAL", "UNKNOWN"] = "UNKNOWN"
    participation_reason: str | None = None
    content_lag_weeks: int | None = Field(default=None, ge=0)
    relation_notes: str | None = None


class ScheduleExceptionV1(AuditFact):
    date: date
    start_time: clock_time | None = None
    end_time: clock_time | None = None
    event_type: str
    title: str
    replaces_schedule_id: str | None = None
    description: str | None = None


class MaterialReleaseRuleV1(AuditFact):
    material_kind: str
    weekday: Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"] | None = None
    time: clock_time | None = None
    schedule_stable_id: str | None = None
    cadence: str | None = None
    notes: str | None = None


class AuditCourseV1(AuditFact):
    name: str
    code: str | None = None
    kind: Literal["MODULE", "COMPONENT"] = "MODULE"
    parent_stable_id: str | None = None
    participation: Literal["REQUIRED", "OPTIONAL", "UNKNOWN"] = "UNKNOWN"
    participation_reason: str | None = None
    assessments: list[AuditAssessmentV1] = Field(default_factory=list)
    deadlines: list[AuditDeadlineV1] = Field(default_factory=list)
    requirements: list[AuditRequirementV1] = Field(default_factory=list)
    schedules: list[ScheduleEntryV1] = Field(default_factory=list)
    schedule_exceptions: list[ScheduleExceptionV1] = Field(default_factory=list)
    material_release_rules: list[MaterialReleaseRuleV1] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class SemesterAuditV1(StrictModel):
    schema_version: Literal["1.0"]
    kind: Literal["semester_audit"]
    audit_id: str = Field(min_length=1)
    semester: str = Field(min_length=1)
    semester_name: str
    state: SemesterState = SemesterState.PLANNING
    starts_on: date | None = None
    ends_on: date | None = None
    timezone: str = "Europe/Berlin"
    courses: list[AuditCourseV1] = Field(default_factory=list)
    known_unknowns: list[str] = Field(default_factory=list)
    provenance: ProvenanceModel | None = None

    @model_validator(mode="after")
    def validate_references(self) -> SemesterAuditV1:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        courses = {course.stable_id: course for course in self.courses}
        if len(courses) != len(self.courses):
            raise ValueError("duplicate course stable_id")
        seen: set[str] = set(courses)
        for course in self.courses:
            if course.parent_stable_id and course.parent_stable_id not in courses:
                raise ValueError(f"unknown parent: {course.parent_stable_id}")
            parent = course.parent_stable_id
            chain = {course.stable_id}
            while parent:
                if parent in chain:
                    raise ValueError("course hierarchy cycle")
                chain.add(parent)
                parent = courses[parent].parent_stable_id
            local_schedules = {item.stable_id for item in course.schedules}
            local_assessments = {item.stable_id for item in course.assessments}
            for collection in (course.assessments, course.deadlines, course.requirements, course.schedules,
                               course.schedule_exceptions, course.material_release_rules):
                for item in collection:
                    if item.stable_id in seen:
                        raise ValueError(f"duplicate stable_id: {item.stable_id}")
                    seen.add(item.stable_id)
            for item in course.schedule_exceptions:
                if item.replaces_schedule_id and item.replaces_schedule_id not in local_schedules:
                    raise ValueError(f"unknown schedule: {item.replaces_schedule_id}")
            for item in course.material_release_rules:
                if item.schedule_stable_id and item.schedule_stable_id not in local_schedules:
                    raise ValueError(f"unknown schedule: {item.schedule_stable_id}")
            for item in [*course.deadlines, *course.requirements]:
                if item.assessment_stable_id and item.assessment_stable_id not in local_assessments:
                    raise ValueError(f"unknown assessment: {item.assessment_stable_id}")
        return self
