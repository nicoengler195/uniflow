"""Read-only dashboard projection of documented weekly material releases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from uniflow.application.academic_classification import rule_fingerprint
from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.material_manifest import _generate_material_manifest_in_transaction
from uniflow.infrastructure.database.models import (
    Course, Material, MaterialArtifact, MaterialExpectationLink, MaterialReleaseRule,
)


@dataclass(frozen=True)
class WeeklyDrop:
    course_id: str
    course_stable_id: str
    course_name: str
    parent_course_id: str | None
    material_kind: str
    release_date: date | None
    release_time: time | None
    timezone: str
    information_status: str
    rule_stable_id: str | None
    expectation_id: str | None
    release_state: str
    link_state: str
    material_id: str | None


@dataclass(frozen=True)
class WeeklyDrops:
    week_start: date
    timezone: str
    items: tuple[WeeklyDrop, ...]


def get_weekly_drops(config, semester_id: str, week_start: date, *, as_of: datetime) -> WeeklyDrops:
    if type(week_start) is not date or week_start.weekday() != 0:
        raise ValueError("WEEK_START_MUST_BE_MONDAY")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    with _read_transaction(config) as db:
        semester, _, _, _, _ = _context(db, semester_id, as_of)
        courses = list(db.scalars(select(Course).where(Course.semester_id == semester.id)))
        by_stable = {c.stable_id: c for c in courses}
        rules = {r.stable_id: r for r in db.scalars(select(MaterialReleaseRule).where(
            MaterialReleaseRule.course_id.in_([c.id for c in courses])))} if courses else {}
        manifest = _generate_material_manifest_in_transaction(db, semester_id, week_start,
            week_start + timedelta(days=6), as_of)
        links = list(db.scalars(select(MaterialExpectationLink).where(
            MaterialExpectationLink.release_rule_id.in_([r.id for r in rules.values()])))) if rules else []
        materials = {m.id: m for m in db.scalars(select(Material).where(
            Material.id.in_([link.material_id for link in links])))} if links else {}
        imported_ids = set(db.scalars(select(MaterialArtifact.material_id).where(
            MaterialArtifact.material_id.in_(list(materials))))) if materials else set()
        by_expectation = {}
        for link in links:
            if link.is_active:
                by_expectation.setdefault(link.expectation_identity, []).append(link)
        items = []
        for expectation in manifest.expectations:
            course = by_stable[expectation.course_id]
            rule = rules[expectation.rule_id]
            active = by_expectation.get(expectation.id, [])
            valid = [link for link in active if link.rule_fingerprint == rule_fingerprint(rule)
                and link.material_id in imported_ids
                and materials[link.material_id].course_id == course.id]
            valid_ids = {x.material_id for x in valid}
            material_id = next(iter(valid_ids)) if len(valid_ids) == 1 else None
            link_state = ("IMPORTED" if material_id else "AMBIGUOUS_LINK" if len(valid_ids) > 1
                else "LINK_STALE" if active and not any(
                    link.rule_fingerprint == rule_fingerprint(rule) for link in active)
                else "LINKED_NO_IMPORT" if active else "NO_EXPLICIT_LINK")
            items.append(WeeklyDrop(course.id, course.stable_id, course.name,
                course.parent_course_id, expectation.material_kind, expectation.release_date,
                expectation.release_time, semester.timezone, expectation.information_status,
                rule.stable_id, expectation.id, "EXPECTED", link_state, material_id))
        for unresolved in manifest.unresolved_rules:
            course = by_stable[unresolved.course_id]
            items.append(WeeklyDrop(course.id, course.stable_id, course.name,
                course.parent_course_id, unresolved.material_kind, None, None,
                semester.timezone, unresolved.information_status, unresolved.rule_id,
                None, "UNRESOLVED_RULE", "NO_EXPLICIT_LINK", None))
        covered = {item.course_id for item in items}
        for course in courses:
            if course.id not in covered and not any(r.course_id == course.id for r in rules.values()):
                items.append(WeeklyDrop(course.id, course.stable_id, course.name,
                    course.parent_course_id, "Unbekannt", None, None, semester.timezone,
                    "UNKNOWN", None, None, "NO_RULE", "NO_EXPLICIT_LINK", None))
        items.sort(key=lambda x: (x.release_date is None, x.release_date or date.max,
            x.release_time or time.max, x.course_name, x.rule_stable_id or ""))
        return WeeklyDrops(week_start, semester.timezone, tuple(items))
