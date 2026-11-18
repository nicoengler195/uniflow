"""Strict, detached GUI boundary contracts for planning mutations."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from uniflow.schemas.workflow_v1 import RequestV1, SuccessV1


class PlanningRequestV1(RequestV1):
    @model_validator(mode="after")
    def no_blank_ids(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name.endswith("_id") and isinstance(value, str) and value.strip() != value:
                raise ValueError("invalid ID whitespace")
        return self


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("aware datetime required")
    return value


class CreatePlanRequestV1(PlanningRequestV1):
    semester_id: str = Field(min_length=1, max_length=80)
    week_start: date
    as_of: datetime | None = None
    course_id: str | None = Field(default=None, min_length=1, max_length=80)
    limit: StrictInt = Field(default=10, ge=1, le=100)
    duration_overrides: dict[str, StrictInt] = Field(default_factory=dict, max_length=100)

    _validate_as_of = field_validator("as_of")(_aware)

    @field_validator("duration_overrides")
    @classmethod
    def overrides(cls, value):
        if any(not key or len(key) > 120 or minutes <= 0 for key, minutes in value.items()):
            raise ValueError("invalid duration override")
        return value


Mode = Literal["INHERIT", "CLEAR", "REPLACE"]


class RegeneratePlanRequestV1(PlanningRequestV1):
    plan_id: str = Field(min_length=1, max_length=80)
    as_of: datetime | None = None
    course_filter_mode: Mode = "INHERIT"
    course_id: str | None = Field(default=None, min_length=1, max_length=80)
    duration_overrides_mode: Mode = "INHERIT"
    duration_overrides: dict[str, StrictInt] | None = Field(default=None, max_length=100)
    limit: StrictInt | None = Field(default=None, ge=1, le=100)

    _validate_as_of = field_validator("as_of")(_aware)

    @model_validator(mode="after")
    def modes(self):
        if (self.course_filter_mode == "REPLACE") != (self.course_id is not None):
            raise ValueError("course mode and ID disagree")
        if (self.duration_overrides_mode == "REPLACE") != (self.duration_overrides is not None):
            raise ValueError("duration mode and overrides disagree")
        if self.duration_overrides is not None and any(
            not key or len(key) > 120 or minutes <= 0 for key, minutes in self.duration_overrides.items()
        ):
            raise ValueError("invalid duration override")
        return self


class EditRequestV1(PlanningRequestV1):
    plan_id: str = Field(min_length=1, max_length=80)
    expected_revision_id: str = Field(min_length=1, max_length=80)


class MoveBlockRequestV1(EditRequestV1):
    block_id: str = Field(min_length=1, max_length=120)
    new_start: datetime
    _validate_start = field_validator("new_start")(_aware)


class SetBlockDurationRequestV1(EditRequestV1):
    block_id: str = Field(min_length=1, max_length=120)
    minutes: StrictInt = Field(gt=0)


class RemoveBlockRequestV1(EditRequestV1):
    block_id: str = Field(min_length=1, max_length=120)


class PlaceUnscheduledRequestV1(EditRequestV1):
    action_id: str = Field(min_length=1, max_length=120)
    start: datetime
    duration_minutes: StrictInt | None = Field(default=None, gt=0)
    _validate_start = field_validator("start")(_aware)


class AcceptPlanRequestV1(PlanningRequestV1):
    plan_id: str = Field(min_length=1, max_length=80)
    revision_id: str = Field(min_length=1, max_length=80)
    accepted_at: datetime | None = None
    _validate_at = field_validator("accepted_at")(_aware)


class MaterializeBlockRequestV1(PlanningRequestV1):
    plan_id: str = Field(min_length=1, max_length=80)
    block_id: str = Field(min_length=1, max_length=120)
    expected_accepted_revision_id: str = Field(min_length=1, max_length=80)
    goal: str | None = Field(default=None, max_length=4000)
    materialized_at: datetime | None = None
    _validate_at = field_validator("materialized_at")(_aware)


class _RevisionFactsV1(SuccessV1):
    plan_id: str
    revision_id: str
    revision_number: StrictInt = Field(ge=1)
    revision_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_revision_id: str | None


class GeneratedRevisionSuccessV1(_RevisionFactsV1):
    command: Literal["planning.plan.create", "planning.plan.regenerate"]
    outcome: Literal["CREATED", "REGENERATED"]
    revision_kind: Literal["GENERATED"]

    @model_validator(mode="after")
    def command_outcome(self):
        expected = "CREATED" if self.command == "planning.plan.create" else "REGENERATED"
        if self.outcome != expected:
            raise ValueError("invalid revision success")
        return self


class EditRevisionSuccessV1(_RevisionFactsV1):
    command: Literal["planning.block.move", "planning.block.set_duration",
        "planning.block.remove", "planning.unscheduled.place"]
    outcome: Literal["EDITED"] = "EDITED"
    parent_revision_id: str = Field(min_length=1, max_length=80)
    revision_kind: Literal["MANUAL_EDIT"]


class AcceptPlanSuccessV1(SuccessV1):
    command: Literal["planning.plan.accept"] = "planning.plan.accept"
    outcome: Literal["ACCEPTED", "ALREADY_ACCEPTED"]
    plan_id: str
    revision_id: str
    acceptance_id: str
    accepted_at: datetime
    _validate_at = field_validator("accepted_at")(_aware)


class MaterializeBlockSuccessV1(SuccessV1):
    command: Literal["planning.block.materialize"] = "planning.block.materialize"
    outcome: Literal["MATERIALIZED", "ALREADY_MATERIALIZED"]
    execution_id: str
    plan_id: str
    revision_id: str
    block_id: str
    action_id: str
    execution_kind: Literal["CREATED_SESSION", "RESUMED_SESSION"]
    learning_session_id: str
    materialized_at: datetime
    session_state: Literal["PLANNED", "HANDOFF_CREATED", "IN_PROGRESS",
        "RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED", "COMPLETED"]
    _validate_at = field_validator("materialized_at")(_aware)
