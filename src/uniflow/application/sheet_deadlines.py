"""Bounded audit-to-deadline compiler; never extrapolates missing sheet instances."""
from datetime import date, time, timedelta
from typing import Literal

from pydantic import Field, model_validator

from uniflow.domain.enums import InformationStatus
from uniflow.schemas.common import StrictModel
from uniflow.schemas.semester_audit import AuditDeadlineV1, SemesterAuditV1


class SheetInstanceV1(StrictModel):
    number: int = Field(ge=1)
    release_date: date
    basis: Literal["EVIDENCED", "BOUNDED_PLANNING"]
    evidence: str = Field(min_length=1)
    existing_deadline_id: str | None = None
    cancelled: bool = False


class SheetSeriesV1(StrictModel):
    release_weekday: int = Field(ge=0, le=6)
    release_time: time | None = None
    due_weekday: int = Field(ge=0, le=6)
    due_time: time
    instances: list[SheetInstanceV1] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_instances(self):
        if self.due_time.tzinfo is not None or (self.release_time and self.release_time.tzinfo is not None):
            raise ValueError("sheet times must be local wall times in the audit timezone")
        if len({i.number for i in self.instances}) != len(self.instances):
            raise ValueError("duplicate sheet number")
        if len({i.release_date for i in self.instances}) != len(self.instances):
            raise ValueError("duplicate sheet release date")
        if any(i.release_date.weekday() != self.release_weekday for i in self.instances):
            raise ValueError("sheet release weekday mismatch")
        return self


def compile_sheet_deadlines(audit: SemesterAuditV1):
    """Explicit opt-in via requirement_type SHEET_SUBMISSION_SERIES_V1.

    Existing deadlines require an exact same-course reference. Derived dates are
    always INFERRED, irrespective of evidence for the sheet or the policy.
    Completion is deliberately absent from this contract.
    """
    used_ids = {audit.semester}
    for course in audit.courses:
        used_ids.add(course.stable_id)
        for rows in (course.assessments, course.deadlines, course.requirements,
                     course.schedules, course.schedule_exceptions, course.material_release_rules):
            used_ids.update(row.stable_id for row in rows)
    result = []
    for course in audit.courses:
        deadlines = {d.stable_id: d for d in course.deadlines}
        occupied = {(d.due_date, d.due_time) for d in course.deadlines if d.due_date}
        claimed = set()
        for requirement in course.requirements:
            if requirement.requirement_type != "SHEET_SUBMISSION_SERIES_V1":
                continue
            if requirement.information_status == InformationStatus.UNKNOWN:
                raise ValueError("sheet series requires known policy status")
            series = SheetSeriesV1.model_validate(requirement.parameters)
            for instance in sorted(series.instances, key=lambda i: i.number):
                offset = (series.due_weekday - series.release_weekday) % 7 or 7
                due = instance.release_date + timedelta(days=offset)
                if instance.cancelled:
                    if instance.existing_deadline_id:
                        raise ValueError("cancelled sheet cannot reference an active deadline")
                    continue
                if instance.existing_deadline_id:
                    existing = deadlines.get(instance.existing_deadline_id)
                    if (existing is None or existing.due_date != due or existing.due_time != series.due_time
                            or existing.stable_id in claimed):
                        raise ValueError("sheet deadline reference mismatch or duplicate")
                    claimed.add(existing.stable_id)
                    continue
                stable_id = f"{requirement.stable_id}@sheet-{instance.number}"
                if stable_id in used_ids or (due, series.due_time) in occupied:
                    raise ValueError("sheet deadline collision; reference existing deadline explicitly")
                used_ids.add(stable_id)
                occupied.add((due, series.due_time))
                deadline = AuditDeadlineV1(
                    stable_id=stable_id, title=f"{course.name}: Blatt {instance.number}",
                    due_date=due, due_time=series.due_time,
                    assessment_stable_id=requirement.assessment_stable_id,
                    information_status=InformationStatus.INFERRED,
                    provenance=requirement.provenance,
                    description=f"Series {requirement.stable_id}; sheet {instance.number}; "
                        f"release {instance.release_date}; {instance.basis}; {instance.evidence}")
                result.append((course.stable_id, deadline))
    return tuple(result)
