"""Exact Semester bounds and Course labels in one query-only snapshot."""
from datetime import datetime

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.infrastructure.database.models import Course
from uniflow.schemas.materials_context_read_v1 import MaterialsContextV1, MaterialsCourseChoiceV1


def get_materials_context(config, semester_id: str, *, as_of: datetime) -> MaterialsContextV1:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    with _read_transaction(config) as db:
        semester, _, local, _, _ = _context(db, semester_id, as_of)
        courses = db.scalars(select(Course).where(Course.semester_id == semester.id)
            .order_by(Course.stable_id, Course.name, Course.id)).all()
        return MaterialsContextV1(as_of=local, semester_stable_id=semester.stable_id,
            semester_name=semester.name, timezone=semester.timezone,
            starts_on=semester.starts_on, ends_on=semester.ends_on,
            courses=[MaterialsCourseChoiceV1(course_id=c.id, stable_id=c.stable_id,
                name=c.name, parent_course_id=c.parent_course_id) for c in courses])
