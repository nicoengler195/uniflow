"""Strict, detached Course overview read contract."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.application_read_v1 import DeadlineItemV1, PlanIdentityV1, ReadDiagnosticV1, SemesterSummaryV1, StrictReadModel
from uniflow.schemas.application_read_detail_v1 import MaterialReadV1, SessionReadV1
from uniflow.schemas.change_feed_read_v1 import ChangeEventV1
from uniflow.schemas.week_read_v1 import PlanBlockReadV1


class CourseIdentityV1(StrictReadModel):
    course_id: str
    course_stable_id: str
    semester_id: str
    semester_stable_id: str
    name: str
    code: str | None
    kind: str
    parent_course_id: str | None
    participation: str
    information_status: str
    source_reference_id: str | None
    audit_revision_id: str | None


class CourseMasteryReadV1(StrictReadModel):
    mastery_status: Literal["UNKNOWN", "KNOWN"]
    mastery_state_id: str | None = None
    value: float | None = Field(default=None, ge=0, le=1)
    calculated_at: datetime | None = None
    calculation_method: str | None = None
    evidence_id: str | None = None
    learning_session_id: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.mastery_status == "UNKNOWN":
            if any(getattr(self, key) is not None for key in ("mastery_state_id", "value", "calculated_at", "calculation_method", "evidence_id", "learning_session_id")):
                raise ValueError("UNKNOWN_MASTERY_HAS_VALUE")
        elif self.mastery_state_id is None or self.value is None or self.calculated_at is None or self.calculation_method is None:
            raise ValueError("KNOWN_MASTERY_MISSING_VALUE")
        if self.calculated_at is not None and self.calculated_at.utcoffset() is None:
            raise ValueError("MASTERY_TIME_MUST_BE_AWARE")
        return self


class DirectPrerequisiteV1(StrictReadModel):
    dependency_id: str
    prerequisite_skill_id: str


class CourseSkillReadV1(StrictReadModel):
    course_skill_id: str
    skill_id: str
    name: str
    kind: str
    status: str
    reviewed_source_status: Literal["AVAILABLE", "NONE"]
    reviewed_source_material_ids: list[str]
    reviewed_source_repository_ids: list[str]
    mastery: CourseMasteryReadV1
    direct_prerequisites: list[DirectPrerequisiteV1]

    @model_validator(mode="after")
    def coherent(self):
        if (self.reviewed_source_status == "AVAILABLE") != bool(self.reviewed_source_material_ids or self.reviewed_source_repository_ids):
            raise ValueError("REVIEWED_SOURCE_STATUS_INVALID")
        if self.reviewed_source_material_ids != sorted(set(self.reviewed_source_material_ids)) or self.reviewed_source_repository_ids != sorted(set(self.reviewed_source_repository_ids)):
            raise ValueError("REVIEWED_SOURCE_ORDER_INVALID")
        if self.direct_prerequisites != sorted(self.direct_prerequisites, key=lambda p: (p.prerequisite_skill_id, p.dependency_id)):
            raise ValueError("PREREQUISITE_ORDER_INVALID")
        if (len({p.dependency_id for p in self.direct_prerequisites}) != len(self.direct_prerequisites) or
                len({p.prerequisite_skill_id for p in self.direct_prerequisites}) != len(self.direct_prerequisites)):
            raise ValueError("PREREQUISITE_DUPLICATE")
        return self


class CourseDeadlineReadV1(DeadlineItemV1):
    relevance_kinds: list[Literal["COURSE", "ASSESSMENT", "MATERIAL"]]
    related_course_id: str | None
    related_assessment_id: str | None
    related_material_id: str | None
    source_reference_id: str | None
    audit_revision_id: str | None

    @model_validator(mode="after")
    def coherent_relevance(self):
        expected = [kind for kind, present in (("COURSE", self.related_course_id is not None),
            ("ASSESSMENT", self.related_assessment_id is not None),
            ("MATERIAL", self.related_material_id is not None)) if present]
        if not expected or self.relevance_kinds != expected:
            raise ValueError("DEADLINE_RELEVANCE_INVALID")
        return self


class AttemptReadV1(StrictReadModel):
    attempt_id: str
    assessment_id: str
    attempt_number: int = Field(ge=1)
    status: str
    scheduled_at: datetime | None
    score: float | None
    grade: str | None
    passed: bool | None
    source_reference_id: str | None

    @model_validator(mode="after")
    def aware(self):
        if self.scheduled_at is not None and self.scheduled_at.utcoffset() is None:
            raise ValueError("ATTEMPT_TIME_MUST_BE_AWARE")
        return self


class AssessmentReadV1(StrictReadModel):
    assessment_id: str
    stable_id: str | None
    course_id: str
    name: str
    assessment_type: str
    information_status: str
    rules_summary: str | None
    source_reference_id: str | None
    audit_revision_id: str | None
    attempts: list[AttemptReadV1]

    @model_validator(mode="after")
    def coherent(self):
        numbers = [a.attempt_number for a in self.attempts]
        if len(numbers) != len(set(numbers)) or any(a.assessment_id != self.assessment_id for a in self.attempts):
            raise ValueError("ATTEMPT_RELATION_INVALID")
        if self.attempts != sorted(self.attempts, key=lambda a: (a.attempt_number, a.scheduled_at or datetime.min.replace(tzinfo=timezone.utc), a.attempt_id)):
            raise ValueError("ATTEMPT_ORDER_INVALID")
        return self


class RequirementReadV1(StrictReadModel):
    requirement_id: str
    stable_id: str
    course_id: str
    assessment_id: str | None
    requirement_type: str
    parameters_json: str
    human_summary: str
    information_status: str
    source_reference_id: str | None
    audit_revision_id: str | None
    progress_status: Literal["UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def canonical_parameters(self):
        if not self.requirement_type or not isinstance(self.human_summary, str):
            raise ValueError("REQUIREMENT_PAYLOAD_INVALID")
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("DUPLICATE_KEY")
                    result[key] = value
                return result
            parsed = json.loads(self.parameters_json, object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE")))
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("REQUIREMENT_PARAMETERS_INVALID") from exc
        if not isinstance(parsed, dict) or canonical != self.parameters_json or len(canonical.encode("utf-8")) > 16384:
            raise ValueError("REQUIREMENT_PARAMETERS_INVALID")
        return self


class OfficialResultReadV1(StrictReadModel):
    official_result_id: str
    course_id: str
    assessment_id: str | None
    status: str
    grade: str | None
    credits: float | None
    issued_on: date | None
    source_reference_id: str | None


class AcceptedCoursePlanReadV1(StrictReadModel):
    revision_id: str
    revision_number: int = Field(ge=1)
    acceptance_id: str
    accepted_at: datetime
    acceptance_actor: str

    @model_validator(mode="after")
    def aware(self):
        if self.accepted_at.utcoffset() is None:
            raise ValueError("ACCEPTANCE_TIME_MUST_BE_AWARE")
        return self


class CourseOverviewV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["course-overview-v1"] = "course-overview-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    course: CourseIdentityV1
    skills: list[CourseSkillReadV1]
    materials: list[MaterialReadV1]
    deadlines: list[CourseDeadlineReadV1]
    assessments: list[AssessmentReadV1]
    requirements: list[RequirementReadV1]
    official_results: list[OfficialResultReadV1]
    resumable_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    resumable_sessions: list[SessionReadV1]
    processing_sessions: list[SessionReadV1]
    completed_sessions: list[SessionReadV1]
    completed_limit: int = Field(ge=1, le=100, strict=True)
    week_start: date
    plan: PlanIdentityV1
    accepted_plan: AcceptedCoursePlanReadV1 | None
    accepted_course_blocks: list[PlanBlockReadV1]
    activity_since: datetime
    activity_through: datetime
    activity_limit: int = Field(ge=1, le=200, strict=True)
    activity: list[ChangeEventV1]
    diagnostics: list[ReadDiagnosticV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if any(t.utcoffset() is None for t in (self.as_of, self.activity_since, self.activity_through)) or not self.activity_since < self.activity_through <= self.as_of:
            raise ValueError("INVALID_COURSE_ACTIVITY_RANGE")
        cid = self.course.course_id
        if (self.course.semester_id, self.course.semester_stable_id) != (self.semester.id, self.semester.stable_id):
            raise ValueError("COURSE_SEMESTER_MISMATCH")
        if any(x.course_id != cid for group in (self.materials, self.assessments, self.requirements, self.official_results, self.resumable_sessions, self.processing_sessions, self.completed_sessions, self.accepted_course_blocks) for x in group):
            raise ValueError("COURSE_SCOPE_INVALID")
        if (len(self.resumable_sessions), len(self.processing_sessions), len(self.completed_sessions)) != (self.resumable_count, self.processing_count, min(self.completed_count, self.completed_limit)):
            raise ValueError("COURSE_SESSION_COUNT_INVALID")
        if len(self.activity) > self.activity_limit or any(cid not in e.course_ids for e in self.activity):
            raise ValueError("COURSE_ACTIVITY_SCOPE_INVALID")
        if any((e.semester_id, e.semester_stable_id) != (self.semester.id, self.semester.stable_id)
                for e in self.activity):
            raise ValueError("COURSE_ACTIVITY_SEMESTER_INVALID")
        if len({e.event_id for e in self.activity}) != len(self.activity):
            raise ValueError("COURSE_ACTIVITY_DUPLICATE")
        # The three stable sorts below mirror ChangeFeed ordering.
        ordered = sorted(self.activity, key=lambda e: e.entity_id)
        ordered.sort(key=lambda e: e.kind)
        ordered.sort(key=lambda e: e.occurred_at, reverse=True)
        if self.activity != ordered:
            raise ValueError("COURSE_ACTIVITY_ORDER_INVALID")
        for e in self.activity:
            if not self.activity_since <= e.occurred_at < self.activity_through:
                raise ValueError("COURSE_ACTIVITY_RANGE_INVALID")
        if self.plan.accepted_revision_id is None and (self.accepted_plan is not None or self.accepted_course_blocks):
            raise ValueError("UNACCEPTED_BLOCKS_INVALID")
        if self.plan.accepted_revision_id is not None and (self.accepted_plan is None or
                self.accepted_plan.revision_id != self.plan.accepted_revision_id or
                self.accepted_plan.revision_number != self.plan.accepted_revision_number):
            raise ValueError("ACCEPTED_PLAN_IDENTITY_INVALID")
        if self.week_start.weekday() != 0:
            raise ValueError("COURSE_WEEK_INVALID")
        if len({x.block_id for x in self.accepted_course_blocks}) != len(self.accepted_course_blocks):
            raise ValueError("COURSE_BLOCK_DUPLICATE")
        for rows, states in ((self.resumable_sessions, {"PLANNED", "HANDOFF_CREATED", "IN_PROGRESS"}),
                (self.processing_sessions, {"RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED"}),
                (self.completed_sessions, {"COMPLETED"})):
            if any(row.state not in states for row in rows) or rows != sorted(rows, key=lambda s: (-s.activity_at.timestamp(), s.session_id)):
                raise ValueError("COURSE_SESSION_GROUP_INVALID")
        for group, key in ((self.skills, lambda x: (x.name, x.skill_id)), (self.materials, lambda x: (x.stable_id or x.title, x.material_id)), (self.assessments, lambda x: (x.stable_id or x.name, x.assessment_id)), (self.requirements, lambda x: (x.stable_id, x.requirement_id)), (self.official_results, lambda x: (x.issued_on or date.max, x.official_result_id))):
            if group != sorted(group, key=key):
                raise ValueError("COURSE_SECTION_ORDER_INVALID")
        if self.deadlines != sorted(self.deadlines, key=lambda x: (
                x.due_date or (x.due_at.date() if x.due_at else date.max),
                x.due_at.timetz().replace(tzinfo=None) if x.due_at else time.min,
                x.deadline_stable_id or "", x.deadline_id)):
            raise ValueError("COURSE_DEADLINE_ORDER_INVALID")
        for group, field in ((self.skills, "skill_id"), (self.materials, "material_id"),
                (self.deadlines, "deadline_id"), (self.assessments, "assessment_id"),
                (self.requirements, "requirement_id"), (self.official_results, "official_result_id")):
            if len({getattr(x, field) for x in group}) != len(group):
                raise ValueError("COURSE_SECTION_DUPLICATE")
        return self


def canonical_course_overview_json(value: CourseOverviewV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
