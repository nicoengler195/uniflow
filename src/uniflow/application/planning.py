"""Deterministic, read-only Skill learning recommendations (3E2A)."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from uniflow.domain.enums import DeadlineStatus, InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config.settings import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, Course, CourseSkill, Deadline, LearningSession, LearningSessionTargetSkill,
    MasteryState, Material, MaterialArtifact, RepositoryAcademicLink, SemanticEvidenceSpan,
    Skill, SkillDependency, SkillProposal, SkillProposalEvidence, Semester,
)
from uniflow.schemas.planning_v1 import (
    PlanningActionV1, PlanningDeadlineV1, PlanningFactorV1, PlanningMasteryV1,
    PlanningPrerequisiteV1, PlanningRecommendationV1, PlanningSemesterV1, PlanningSourceV1,
)

POLICY = "next-best-action-v1"
RESUME_ORDER = {LearningSessionState.IN_PROGRESS: 0,
                LearningSessionState.HANDOFF_CREATED: 1,
                LearningSessionState.PLANNED: 2}
PROCESSING_STATES = {
    LearningSessionState.VALIDATED: "VALIDATED_SESSION_AWAITING_MASTERY",
    LearningSessionState.RESULT_RECEIVED: "UNEXPECTED_RESULT_RECEIVED_STATE",
    LearningSessionState.MASTERY_UPDATED: "UNEXPECTED_MASTERY_UPDATED_STATE",
}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _factor(code: str, *refs: str) -> PlanningFactorV1:
    return PlanningFactorV1(code=code, refs=sorted(set(refs)))


def _aware(value: datetime, zone: ZoneInfo) -> datetime:
    # SQLite drops offsets; audit due_at is stored as semester-local wall time.
    return value if value.tzinfo else value.replace(tzinfo=zone)


def _mastery(db, skill_id: str) -> PlanningMasteryV1:
    row = db.scalar(select(MasteryState).where(MasteryState.skill_id == skill_id)
                    .order_by(MasteryState.calculated_at.desc(), MasteryState.id.desc()).limit(1))
    if row is None:
        return PlanningMasteryV1(skill_id=skill_id, status="UNKNOWN")
    if not math.isfinite(row.value) or not 0 <= row.value <= 1:
        raise ValueError("INVALID_MASTERY_STATE")
    return PlanningMasteryV1(skill_id=skill_id, status="KNOWN", mastery_state_id=row.id,
        value=row.value, calculation_method=row.calculation_method,
        calculated_at=_aware(row.calculated_at, ZoneInfo("UTC")), evidence_id=row.evidence_id)


def _prerequisites(db, target_id: str, edges: list[SkillDependency],
                   course_skill_by_skill: dict[str, CourseSkill]) -> list[PlanningPrerequisiteV1]:
    incoming: dict[str, list[SkillDependency]] = {}
    for edge in edges:
        incoming.setdefault(edge.dependent_skill_id, []).append(edge)
    for rows in incoming.values():
        rows.sort(key=lambda e: (e.prerequisite_skill_id, e.id))
    visiting: set[str] = set()
    visited: set[str] = set()
    relevant: dict[str, set[str]] = {}

    def walk(sid: str) -> None:
        if sid in visiting:
            raise ValueError("SKILL_GRAPH_INVALID")
        if sid in visited:
            return
        skill = db.get(Skill, sid)
        if skill is None or skill.status != "ACTIVE":
            raise ValueError("SKILL_GRAPH_INVALID")
        visiting.add(sid)
        for edge in incoming.get(sid, []):
            relevant.setdefault(edge.prerequisite_skill_id, set()).add(edge.id)
            walk(edge.prerequisite_skill_id)
        visiting.remove(sid)
        visited.add(sid)

    walk(target_id)
    relevant.pop(target_id, None)
    return [PlanningPrerequisiteV1(skill_id=sid, dependency_ids=sorted(ids),
        course_skill_id=course_skill_by_skill[sid].id if sid in course_skill_by_skill else None,
        mastery=_mastery(db, sid)) for sid, ids in sorted(relevant.items())]


def _source(db, course_id: str, skill_id: str) -> PlanningSourceV1:
    """Read IDs only; never load excerpts or managed source bytes."""
    proposals = db.scalars(select(SkillProposal).where(
        SkillProposal.accepted_skill_id == skill_id, SkillProposal.status == "ACCEPTED").order_by(SkillProposal.id)).all()
    provenance: set[str] = set()
    material_ids: set[str] = set()
    current = historical = False
    for proposal in proposals:
        links = db.scalars(select(SkillProposalEvidence).where(
            SkillProposalEvidence.proposal_id == proposal.id).order_by(SkillProposalEvidence.evidence_span_id)).all()
        for link in links:
            span = db.get(SemanticEvidenceSpan, link.evidence_span_id)
            if span is None:
                continue
            if span.material_artifact_id:
                ma = db.get(MaterialArtifact, span.material_artifact_id)
                material = db.get(Material, ma.material_id) if ma else None
                if material is None or material.course_id != course_id:
                    continue
                material_ids.add(material.id)
                if ma.is_current and ma.deactivated_at is None:
                    current = True
                else:
                    historical = True
            elif span.repository_academic_link_id:
                academic = db.get(RepositoryAcademicLink, span.repository_academic_link_id)
                if academic is None or not academic.is_active or academic.course_id != course_id:
                    continue
                current = True  # Active academic link; snapshot age remains provenance.
            else:
                continue
            provenance.add(span.id)
    return PlanningSourceV1(status="AVAILABLE" if provenance else "NO_REVIEWED_SOURCE",
        current_reviewed_source_available=current,
        historical_reviewed_source_available=historical,
        provenance_ids=sorted(provenance), material_ids=sorted(material_ids))


def _deadlines(db, course_id: str, material_ids: set[str], zone: ZoneInfo,
               local_as_of: datetime) -> list[PlanningDeadlineV1]:
    assessments = set(db.scalars(select(Assessment.id).where(Assessment.course_id == course_id)))
    rows = db.scalars(select(Deadline)).all()
    result: list[PlanningDeadlineV1] = []
    for row in rows:
        if row.course_id == course_id:
            relevance = "COURSE"
        elif row.assessment_id in assessments:
            relevance = "ASSESSMENT"
        elif row.material_id in material_ids:
            relevance = "SKILL_MATERIAL"
        else:
            continue
        date_only = bool(row.is_date_only)
        due_date = row.due_at.date() if date_only and row.due_at else None
        due_at = _aware(row.due_at, zone).astimezone(zone) if row.due_at and not date_only else None
        eligible = row.status == DeadlineStatus.OPEN and row.information_status in {
            InformationStatus.CONFIRMED, InformationStatus.OBSERVED}
        if not eligible:
            timing = "EXCLUDED"
        elif row.due_at is None:
            timing = "UNRESOLVED"
        elif date_only:
            timing = "UPCOMING" if due_date >= local_as_of.date() else "OVERDUE"
        else:
            timing = "UPCOMING" if due_at >= local_as_of else "OVERDUE"
        result.append(PlanningDeadlineV1(id=row.id, stable_id=row.stable_id,
            relevance=relevance, course_id=row.course_id, assessment_id=row.assessment_id,
            material_id=row.material_id, information_status=row.information_status.value,
            status=row.status.value, is_date_only=date_only, due_date=due_date,
            due_at=due_at, timing=timing, actionable=timing == "UPCOMING"))
    return sorted(result, key=lambda d: (d.due_date or (d.due_at.date() if d.due_at else datetime.max.date()),
        d.stable_id or "", d.id))


def _deadline_reasons(items: list[PlanningDeadlineV1]) -> tuple[list[PlanningFactorV1], list[PlanningFactorV1]]:
    reasons: list[PlanningFactorV1] = []
    risks: list[PlanningFactorV1] = []
    by_relevance = {"COURSE": "UPCOMING_COURSE_DEADLINE",
                    "ASSESSMENT": "UPCOMING_ASSESSMENT_DEADLINE",
                    "SKILL_MATERIAL": "UPCOMING_SKILL_MATERIAL_DEADLINE"}
    for item in items:
        if item.actionable:
            reasons.append(_factor(by_relevance[item.relevance], item.id))
        elif item.timing == "OVERDUE":
            risks.append(_factor("OVERDUE_OPEN_DEADLINE", item.id))
        elif item.information_status == "INFERRED":
            risks.append(_factor("INFERRED_DEADLINE_EXCLUDED", item.id))
        elif item.information_status == "UNKNOWN":
            risks.append(_factor("UNKNOWN_DEADLINE_EXCLUDED", item.id))
    return reasons, risks


def plan_next_actions(config: AppConfig, semester_id: str, *, as_of: datetime,
                      course_id: str | None = None, limit: int = 10) -> PlanningRecommendationV1:
    """Return advice from one read transaction. IDs accepted here are stable IDs."""
    if as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if not 1 <= limit <= 100:
        raise ValueError("INVALID_LIMIT")
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:
        return _plan_next_actions_in_transaction(db, config, semester_id,
            as_of=as_of, course_id=course_id, limit=limit)


def _plan_next_actions_in_transaction(db, config: AppConfig, semester_id: str, *,
                                      as_of: datetime, course_id: str | None = None,
                                      limit: int = 10) -> PlanningRecommendationV1:
    """Compose the canonical planner inside a caller-owned read transaction."""
    if as_of.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    if not 1 <= limit <= 100:
        raise ValueError("INVALID_LIMIT")
    semester = db.scalar(select(Semester).where(Semester.stable_id == semester_id))
    if semester is None:
        raise ValueError("UNKNOWN_SEMESTER")
    if semester.state != SemesterState.ACTIVE:
        raise ValueError("UNSUPPORTED_SEMESTER_STATE")
    zone = ZoneInfo(semester.timezone)
    local_as_of = as_of.astimezone(zone)
    courses = db.scalars(select(Course).where(Course.semester_id == semester.id).order_by(Course.stable_id)).all()
    if course_id is not None:
        courses = [c for c in courses if c.stable_id == course_id]
        if not courses:
            raise ValueError("UNKNOWN_COURSE_IN_SEMESTER")
    course_ids = {c.id for c in courses}
    semester_data = PlanningSemesterV1(id=semester.id, stable_id=semester.stable_id,
        name=semester.name, timezone=semester.timezone, state="ACTIVE")
    edges = db.scalars(select(SkillDependency).where(SkillDependency.is_active.is_(True))).all()
    sessions = db.scalars(select(LearningSession).where(LearningSession.course_id.in_(course_ids))).all()
    session_targets: dict[str, list[str]] = {}
    for session in sessions:
        rows = db.scalars(select(LearningSessionTargetSkill).where(
            LearningSessionTargetSkill.learning_session_id == session.id).order_by(
            LearningSessionTargetSkill.ordinal, LearningSessionTargetSkill.id)).all()
        session_targets[session.id] = [r.skill_id for r in rows]
    candidate_rows: list[tuple[tuple, PlanningActionV1]] = []
    diagnostics: list[PlanningFactorV1] = []
    suppressed: set[tuple[str, str]] = set()
    context_sessions = []
    for session in sorted(sessions, key=lambda s: s.id):
        targets = session_targets[session.id]
        if session.state == LearningSessionState.COMPLETED:
            continue
        context_sessions.append({"id": session.id, "course_id": session.course_id,
            "state": session.state.value, "targets": targets})
        if session.state in PROCESSING_STATES:
            diagnostics.append(_factor(PROCESSING_STATES[session.state], session.id))
            suppressed.update((session.course_id, sid) for sid in targets)
            continue
        if session.state not in RESUME_ORDER:
            raise ValueError("INVALID_SESSION_STATE")
        if not targets:
            diagnostics.append(_factor("SESSION_WITHOUT_TARGETS", session.id))
            continue
        course = next(c for c in courses if c.id == session.course_id)
        suppressed.update((course.id, sid) for sid in targets)
        action = PlanningActionV1(action_id="nba:" + _digest({"policy_version": POLICY,
            "action_type": "RESUME_SESSION", "session_id": session.id}), rank=1,
            action_type="RESUME_SESSION", priority_class=0, course_id=course.id,
            course_stable_id=course.stable_id, target_skill_ids=targets,
            existing_session_id=session.id, existing_session_state=session.state.value,
            target_mastery=None, prerequisite_context=[], deadline_context=[], source_status=None,
            reasons=[_factor("ACTIVE_SESSION", session.id)], risk_factors=[])
        candidate_rows.append(((0, RESUME_ORDER[session.state], course.stable_id, session.id), action))
    context_skills = []
    context_dependencies: dict[str, dict] = {}
    context_mastery: dict[str, dict] = {}
    context_sources = []
    context_deadlines: dict[str, dict] = {}
    active_course_skills = []
    for course in courses:
        links = db.scalars(select(CourseSkill).where(CourseSkill.course_id == course.id,
            CourseSkill.is_active.is_(True)).order_by(CourseSkill.skill_id, CourseSkill.id)).all()
        exact = {link.skill_id: link for link in links}
        for link in links:
            skill = db.get(Skill, link.skill_id)
            active_course_skills.append({"id": link.id, "course_id": course.id,
                "skill_id": link.skill_id, "skill_status": skill.status if skill else None})
            if skill is None or skill.status != "ACTIVE":
                continue
            context_skills.append({"id": skill.id, "name": skill.name, "kind": skill.kind,
                "status": skill.status, "course_id": course.id})
            prerequisites = _prerequisites(db, skill.id, edges, exact)
            mastery = _mastery(db, skill.id)
            context_mastery[skill.id] = mastery.model_dump(mode="json")
            for pre in prerequisites:
                context_mastery[pre.skill_id] = pre.mastery.model_dump(mode="json")
                for edge in edges:
                    if edge.id in pre.dependency_ids:
                        context_dependencies[edge.id] = {"id": edge.id,
                            "prerequisite": edge.prerequisite_skill_id,
                            "dependent": edge.dependent_skill_id}
            source = _source(db, course.id, skill.id)
            context_sources.append({"course_id": course.id, "skill_id": skill.id,
                **source.model_dump(mode="json")})
            deadlines = _deadlines(db, course.id, set(source.material_ids), zone, local_as_of)
            for d in deadlines:
                context_deadlines[d.id] = d.model_dump(mode="json")
            if (course.id, skill.id) in suppressed:
                continue
            upcoming = [d for d in deadlines if d.actionable]
            earliest = min((d.due_date or d.due_at.date() for d in upcoming), default=None)
            priority_class = 1 if earliest else 2
            check = any(p.mastery.status == "UNKNOWN" for p in prerequisites)
            reasons, risks = _deadline_reasons(deadlines)
            reasons.append(_factor("MASTERY_KNOWN" if mastery.status == "KNOWN" else "MASTERY_UNKNOWN", skill.id))
            if check:
                reasons.append(_factor("PREREQUISITE_CHECK_NEEDED", *(p.skill_id for p in prerequisites if p.mastery.status == "UNKNOWN")))
                risks.append(_factor("PREREQUISITE_MASTERY_UNKNOWN", *(p.skill_id for p in prerequisites if p.mastery.status == "UNKNOWN")))
            if source.status == "AVAILABLE":
                reasons.append(_factor("SOURCE_AVAILABLE", *source.provenance_ids))
                if source.historical_reviewed_source_available and not source.current_reviewed_source_available:
                    risks.append(_factor("HISTORICAL_SOURCE_ONLY", *source.provenance_ids))
            else:
                reasons.append(_factor("NO_REVIEWED_SOURCE", skill.id))
                risks.append(_factor("NO_REVIEWED_SOURCE", skill.id))
            action = PlanningActionV1(action_id="nba:" + _digest({"policy_version": POLICY,
                "action_type": "START_SKILL_SESSION", "course_id": course.id,
                "skill_id": skill.id}), rank=1, action_type="START_SKILL_SESSION",
                priority_class=priority_class, course_id=course.id,
                course_stable_id=course.stable_id, target_skill_ids=[skill.id],
                existing_session_id=None, existing_session_state=None,
                target_mastery=mastery, prerequisite_context=prerequisites,
                deadline_context=deadlines, source_status=source,
                reasons=sorted(reasons, key=lambda f: (f.code, f.refs)),
                risk_factors=sorted(risks, key=lambda f: (f.code, f.refs)))
            key = (priority_class, earliest.isoformat() if earliest else "",
                0 if check else 1, 0 if mastery.status == "KNOWN" else 1,
                mastery.value if mastery.status == "KNOWN" else 0,
                course.stable_id, skill.id)
            candidate_rows.append((key, action))
    if not context_skills:
        diagnostics.append(_factor("NO_REVIEWED_SKILLS"))
    candidate_rows.sort(key=lambda item: item[0])
    if not candidate_rows:
        diagnostics.append(_factor("NO_ELIGIBLE_ACTIONS"))
    input_context = {"schema_version": "1.0", "policy_version": POLICY,
        "as_of": local_as_of.isoformat(), "semester": semester_data.model_dump(mode="json"),
        "course_filter_id": course_id, "limit": limit,
        "courses": [{"id": c.id, "stable_id": c.stable_id, "name": c.name,
            "information_status": c.information_status.value} for c in courses],
        "course_skills": sorted(active_course_skills, key=lambda x: x["id"]),
        "skills": sorted(context_skills, key=lambda x: (x["course_id"], x["id"])),
        "dependencies": sorted(context_dependencies.values(), key=lambda x: x["id"]),
        "mastery": [context_mastery[k] for k in sorted(context_mastery)],
        "sessions": context_sessions,
        "sources": sorted(context_sources, key=lambda x: (x["course_id"], x["skill_id"])),
        "deadlines": [context_deadlines[k] for k in sorted(context_deadlines)]}
    fingerprint = _digest(input_context)
    selected = []
    for rank, (_, action) in enumerate(candidate_rows[:limit], 1):
        selected.append(action.model_copy(update={"rank": rank}))
    return PlanningRecommendationV1(as_of=local_as_of, semester=semester_data,
        course_filter_id=course_id, status="OK" if selected else "NO_ELIGIBLE_ACTIONS",
        context_fingerprint=fingerprint, recommendations=selected,
        diagnostics=sorted(diagnostics, key=lambda f: (f.code, f.refs)))
