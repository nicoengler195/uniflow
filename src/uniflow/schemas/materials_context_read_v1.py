"""Small detached presentation context for Materials."""
from datetime import date, datetime
from typing import Literal

from pydantic import model_validator

from uniflow.schemas.application_read_v1 import StrictReadModel


class MaterialsCourseChoiceV1(StrictReadModel):
    course_id: str
    stable_id: str
    name: str
    parent_course_id: str | None = None


class MaterialsContextV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    as_of: datetime
    semester_stable_id: str
    semester_name: str
    timezone: str
    starts_on: date | None
    ends_on: date | None
    courses: list[MaterialsCourseChoiceV1]

    @model_validator(mode="after")
    def coherent(self):
        if self.as_of.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        if self.starts_on and self.ends_on and self.starts_on > self.ends_on:
            raise ValueError("SEMESTER_DATES_INVALID")
        if self.courses != sorted(self.courses, key=lambda c: (c.stable_id, c.name)):
            raise ValueError("MATERIALS_COURSE_ORDER_INVALID")
        if len({c.stable_id for c in self.courses}) != len(self.courses):
            raise ValueError("MATERIALS_COURSE_DUPLICATE")
        return self
