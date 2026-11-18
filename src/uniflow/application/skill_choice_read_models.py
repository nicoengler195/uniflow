"""Bounded, detached choices for explicit existing-Skill resolution."""
from __future__ import annotations

from sqlalchemy import func, or_, select

from uniflow.application.application_read_models import _read_transaction
from uniflow.infrastructure.database.models import Skill
from uniflow.schemas.review_read_v1 import SkillChoiceV1, SkillChoicesV1

MAX_SKILL_QUERY_CHARS = 120
MAX_SKILL_CHOICES = 50


def search_active_skills(config, *, query: str = "", limit: int = 20) -> SkillChoicesV1:
    if not isinstance(query, str) or len(query) > MAX_SKILL_QUERY_CHARS or query != query.strip():
        raise ValueError("INVALID_SKILL_CHOICE_QUERY")
    if type(limit) is not int or not 1 <= limit <= MAX_SKILL_CHOICES:
        raise ValueError("INVALID_SKILL_CHOICE_LIMIT")
    needle = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _read_transaction(config) as db:
        statement = select(Skill).where(Skill.status == "ACTIVE")
        if needle:
            pattern = f"%{needle}%"
            statement = statement.where(or_(func.lower(Skill.name).like(pattern, escape="\\"),
                func.lower(Skill.domain).like(pattern, escape="\\")))
        rows = db.scalars(statement.order_by(Skill.name, Skill.id).limit(limit)).all()
        choices = [SkillChoiceV1(skill_id=skill.id, name=skill.name, kind=skill.kind,
                domain=skill.domain, description_excerpt=skill.description[:160] if skill.description else None)
            for skill in rows]
        return SkillChoicesV1(query=query, limit=limit, choices=choices)
