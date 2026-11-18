"""Exact-Session, one-snapshot read using the verified Inbox projector."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.session_inbox_read_models import _session
from uniflow.infrastructure.database.models import Course, LearningSession, Skill
from uniflow.schemas.session_detail_read_v1 import SessionDetailV1, SessionTargetLabelV1


def get_session_detail(config, semester_id: str, session_id: str, *, as_of: datetime) -> SessionDetailV1:
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    with _read_transaction(config) as db:
        semester, _, local, _, summary = _context(db, semester_id, as_of)
        row = db.get(LearningSession, session_id)
        if row is None:
            raise ValueError("UNKNOWN_LEARNING_SESSION")
        course = db.get(Course, row.course_id)
        if course is None or course.semester_id != semester.id:
            raise ValueError("SESSION_NOT_IN_SEMESTER")
        detail = _session(db, config, row, course)
        skill_ids = [target.skill_id for target in detail.targets]
        skills = {skill.id: skill for skill in db.scalars(select(Skill).where(Skill.id.in_(skill_ids)))}
        if len(skills) != len(skill_ids):
            raise ValueError("SESSION_TARGET_SKILL_MISSING")
        return SessionDetailV1(as_of=local, semester=summary, course_name=course.name,
            session=detail, target_labels=[SessionTargetLabelV1(skill_id=target.skill_id,
                name=skills[target.skill_id].name, ordinal=target.ordinal) for target in detail.targets])
