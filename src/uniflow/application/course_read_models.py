"""Exact-Course, one-snapshot read projection."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _plan, _read_transaction
from uniflow.application.change_feed_read_models import _events_in_transaction
from uniflow.application.material_inbox_read_models import _material_dtos, _source_dtos, _strict_json, _utc
from uniflow.application.planning import _source
from uniflow.application.session_inbox_read_models import _session
from uniflow.application.week_read_models import _revision
from uniflow.application.planning_calendar import _project_calendar_week_in_transaction
from uniflow.domain.enums import DeadlineStatus, InformationStatus
from uniflow.infrastructure.database.models import (
    Assessment, AssessmentAttempt, AssessmentRequirement, Course, CourseSkill,
    Deadline, MasteryState, Material, MaterialArtifact, OfficialResult,
    RepositoryAcademicLink, SemanticEvidenceSpan, Skill, SkillDependency,
    SkillProposal, SkillProposalEvidence, SourceArtifact, LearningSession,
)
from uniflow.schemas.course_read_v1 import (
    AcceptedCoursePlanReadV1, AssessmentReadV1, AttemptReadV1, CourseDeadlineReadV1, CourseIdentityV1,
    CourseMasteryReadV1, CourseOverviewV1, CourseSkillReadV1,
    DirectPrerequisiteV1, OfficialResultReadV1, RequirementReadV1,
)
from uniflow.schemas.semester_audit import AuditRequirementV1


def _reviewed_repositories(db, course_id: str, skill_id: str) -> list[str]:
    # Planning's _source is the authority. This only projects repository IDs
    # for provenance already accepted by that policy.
    ids = set()
    for proposal in db.scalars(select(SkillProposal).where(
            SkillProposal.accepted_skill_id == skill_id, SkillProposal.status == "ACCEPTED")):
        for link in db.scalars(select(SkillProposalEvidence).where(SkillProposalEvidence.proposal_id == proposal.id)):
            span = db.get(SemanticEvidenceSpan, link.evidence_span_id)
            if span and span.repository_academic_link_id:
                academic = db.get(RepositoryAcademicLink, span.repository_academic_link_id)
                if academic and academic.is_active and academic.course_id == course_id:
                    ids.add(academic.repository_source_id)
    return sorted(ids)


def _skills(db, course):
    result = []
    for link in db.scalars(select(CourseSkill).where(CourseSkill.course_id == course.id,
            CourseSkill.is_active.is_(True))):
        skill = db.get(Skill, link.skill_id)
        if skill is None or skill.status != "ACTIVE":
            raise ValueError("COURSE_SKILL_INTEGRITY_INVALID")
        row = db.scalar(select(MasteryState).where(MasteryState.skill_id == skill.id)
            .order_by(MasteryState.calculated_at.desc(), MasteryState.id.desc()).limit(1))
        if row is not None and (not math.isfinite(row.value) or not 0 <= row.value <= 1):
            raise ValueError("INVALID_MASTERY_STATE")
        mastery = CourseMasteryReadV1(mastery_status="KNOWN", mastery_state_id=row.id,
            value=row.value, calculated_at=_utc(row.calculated_at),
            calculation_method=row.calculation_method, evidence_id=row.evidence_id,
            learning_session_id=row.learning_session_id) if row else CourseMasteryReadV1(mastery_status="UNKNOWN")
        prereqs = []
        for edge in db.scalars(select(SkillDependency).where(
                SkillDependency.dependent_skill_id == skill.id, SkillDependency.is_active.is_(True))):
            prerequisite = db.get(Skill, edge.prerequisite_skill_id)
            if prerequisite is None or prerequisite.status != "ACTIVE":
                raise ValueError("SKILL_DEPENDENCY_INTEGRITY_INVALID")
            prereqs.append(DirectPrerequisiteV1(dependency_id=edge.id,
                prerequisite_skill_id=edge.prerequisite_skill_id))
        prereqs.sort(key=lambda p: (p.prerequisite_skill_id, p.dependency_id))
        source = _source(db, course.id, skill.id)
        repositories = _reviewed_repositories(db, course.id, skill.id)
        if (source.status == "AVAILABLE") != bool(source.material_ids or repositories):
            raise ValueError("REVIEWED_SOURCE_PROVENANCE_INVALID")
        result.append(CourseSkillReadV1(course_skill_id=link.id, skill_id=skill.id,
            name=skill.name, kind=skill.kind, status=skill.status,
            reviewed_source_status="AVAILABLE" if source.status == "AVAILABLE" else "NONE",
            reviewed_source_material_ids=source.material_ids,
            reviewed_source_repository_ids=repositories, mastery=mastery,
            direct_prerequisites=prereqs))
    return sorted(result, key=lambda x: (x.name, x.skill_id))


def _materials(db, course):
    rows = list(db.scalars(select(Material).where(Material.course_id == course.id)))
    ids = [m.id for m in rows]
    links = list(db.scalars(select(MaterialArtifact).where(MaterialArtifact.material_id.in_(ids)))) if ids else []
    artifact_ids = {link.artifact_id for link in links}
    artifacts = list(db.scalars(select(SourceArtifact).where(SourceArtifact.id.in_(artifact_ids)))) if artifact_ids else []
    details = _material_dtos(rows, links, _source_dtos(db, artifacts), {course.id: course})
    return sorted(details.values(), key=lambda x: (x.stable_id or x.title, x.material_id)), set(ids)


def _deadlines(db, course, assessment_ids, material_ids, zone, local):
    result = []
    for row in db.scalars(select(Deadline).where(
            (Deadline.course_id == course.id) | Deadline.assessment_id.in_(assessment_ids) |
            Deadline.material_id.in_(material_ids))):
        kinds = [kind for kind, present in (("COURSE", row.course_id == course.id),
            ("ASSESSMENT", row.assessment_id in assessment_ids),
            ("MATERIAL", row.material_id in material_ids)) if present]
        due_date = row.due_at.date() if row.is_date_only and row.due_at else None
        due_at = (row.due_at.replace(tzinfo=zone) if row.due_at.tzinfo is None else
            row.due_at.astimezone(zone)) if row.due_at and not row.is_date_only else None
        authoritative = row.information_status in {InformationStatus.CONFIRMED, InformationStatus.OBSERVED}
        if row.due_at is None:
            timing = "UNDATED"
        elif not authoritative:
            timing = "NONAUTHORITATIVE"
        elif row.status != DeadlineStatus.OPEN:
            timing = "KNOWN_DUE_OTHER_STATUS"
        elif due_date >= local.date() if row.is_date_only else due_at >= local:
            timing = "UPCOMING_OPEN"
        else:
            timing = "OVERDUE_OPEN"
        result.append(CourseDeadlineReadV1(deadline_id=row.id,
            deadline_stable_id=row.stable_id, course_id=course.id, title=row.title,
            status=row.status.value, information_status=row.information_status.value,
            is_date_only=row.is_date_only, due_date=due_date, due_at=due_at,
            timing=timing, relevance_kinds=kinds,
            related_course_id=row.course_id if row.course_id == course.id else None,
            related_assessment_id=row.assessment_id if row.assessment_id in assessment_ids else None,
            related_material_id=row.material_id if row.material_id in material_ids else None,
            source_reference_id=row.source_id, audit_revision_id=row.audit_revision_id))
    return sorted(result, key=lambda x: (x.due_date or (x.due_at.date() if x.due_at else date.max),
        x.due_at.timetz().replace(tzinfo=None) if x.due_at else time.min,
        x.deadline_stable_id or "", x.deadline_id))


def _assessments(db, course):
    result = []
    ids = set()
    for row in db.scalars(select(Assessment).where(Assessment.course_id == course.id)):
        ids.add(row.id)
        attempts = [AttemptReadV1(attempt_id=a.id, assessment_id=row.id,
            attempt_number=a.attempt_number, status=a.status.value,
            scheduled_at=_utc(a.scheduled_at), score=a.score, grade=a.grade,
            passed=a.passed, source_reference_id=a.source_id)
            for a in db.scalars(select(AssessmentAttempt).where(AssessmentAttempt.assessment_id == row.id))]
        attempts.sort(key=lambda a: (a.attempt_number, a.scheduled_at or datetime.min.replace(tzinfo=ZoneInfo("UTC")), a.attempt_id))
        result.append(AssessmentReadV1(assessment_id=row.id, stable_id=row.stable_id,
            course_id=course.id, name=row.name, assessment_type=row.assessment_type.value,
            information_status=row.information_status.value, rules_summary=row.rules_summary,
            source_reference_id=row.source_id, audit_revision_id=row.audit_revision_id,
            attempts=attempts))
    return sorted(result, key=lambda a: (a.stable_id or a.name, a.assessment_id)), ids


def _requirements(db, course, assessments):
    result = []
    assessment_by_stable = {a.stable_id: a.assessment_id for a in assessments if a.stable_id}
    for row in db.scalars(select(AssessmentRequirement).where(AssessmentRequirement.course_id == course.id)):
        payload = _strict_json(row.payload_json, "REQUIREMENT_PAYLOAD", shape=dict)
        if (not isinstance(payload.get("requirement_type"), str) or
                not payload["requirement_type"] or
                not isinstance(payload.get("human_summary"), str) or
                not isinstance(payload.get("parameters"), dict)):
            raise ValueError("REQUIREMENT_PAYLOAD_INVALID")
        try:
            validated = AuditRequirementV1.model_validate(payload)
        except ValueError as exc:
            raise ValueError("REQUIREMENT_PAYLOAD_INVALID") from exc
        if validated.stable_id != row.stable_id:
            raise ValueError("REQUIREMENT_PAYLOAD_INVALID")
        parameters = validated.parameters
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(canonical.encode("utf-8")) > 16384:
            raise ValueError("REQUIREMENT_PARAMETERS_TOO_LARGE")
        assessment_id = assessment_by_stable.get(validated.assessment_stable_id)
        if validated.assessment_stable_id is not None and assessment_id is None:
            raise ValueError("REQUIREMENT_ASSESSMENT_INVALID")
        result.append(RequirementReadV1(requirement_id=row.id, stable_id=row.stable_id,
            course_id=course.id, assessment_id=assessment_id,
            requirement_type=validated.requirement_type,
            parameters_json=canonical, human_summary=validated.human_summary,
            information_status=row.information_status.value,
            source_reference_id=row.source_id, audit_revision_id=row.audit_revision_id))
    return sorted(result, key=lambda x: (x.stable_id, x.requirement_id))


def _results(db, course, assessment_ids):
    result = []
    for row in db.scalars(select(OfficialResult).where(OfficialResult.course_id == course.id)):
        if row.assessment_id is not None and row.assessment_id not in assessment_ids:
            raise ValueError("OFFICIAL_RESULT_ASSESSMENT_INVALID")
        result.append(OfficialResultReadV1(official_result_id=row.id, course_id=course.id,
            assessment_id=row.assessment_id, status=row.status, grade=row.grade,
            credits=row.credits, issued_on=row.issued_on, source_reference_id=row.source_id))
    return sorted(result, key=lambda x: (x.issued_on or date.max, x.official_result_id))


def get_course_overview(config, semester_id: str, course_id: str, *, as_of: datetime,
                        activity_since: datetime, activity_through: datetime,
                        activity_limit: int = 50, completed_limit: int = 20) -> CourseOverviewV1:
    for label, value in (("AS_OF", as_of), ("ACTIVITY_SINCE", activity_since),
            ("ACTIVITY_THROUGH", activity_through)):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError(f"{label}_MUST_BE_AWARE")
    if not activity_since < activity_through <= as_of:
        raise ValueError("INVALID_COURSE_ACTIVITY_RANGE")
    if type(activity_limit) is not int or not 1 <= activity_limit <= 200:
        raise ValueError("INVALID_ACTIVITY_LIMIT")
    if type(completed_limit) is not int or not 1 <= completed_limit <= 100:
        raise ValueError("INVALID_COMPLETED_LIMIT")
    with _read_transaction(config) as db:
        semester, zone, local, monday, summary = _context(db, semester_id, as_of)
        course = db.scalar(select(Course).where(Course.semester_id == semester.id,
            Course.stable_id == course_id))
        if course is None:
            raise ValueError("UNKNOWN_COURSE_IN_SEMESTER")
        identity = CourseIdentityV1(course_id=course.id, course_stable_id=course.stable_id,
            semester_id=semester.id, semester_stable_id=semester.stable_id,
            name=course.name, code=course.code, kind=course.kind,
            parent_course_id=course.parent_course_id, participation=course.participation,
            information_status=course.information_status.value,
            source_reference_id=course.source_id, audit_revision_id=course.audit_revision_id)
        skills = _skills(db, course)
        materials, material_ids = _materials(db, course)
        assessments, assessment_ids = _assessments(db, course)
        deadlines = _deadlines(db, course, assessment_ids, material_ids, zone, local)
        requirements = _requirements(db, course, assessments)
        results = _results(db, course, assessment_ids)
        groups = {"resumable": [], "processing": [], "completed": []}
        for row in db.scalars(select(LearningSession).where(LearningSession.course_id == course.id)):
            detail = _session(db, config, row, course)
            group = ("resumable" if detail.state in {"PLANNED", "HANDOFF_CREATED", "IN_PROGRESS"}
                else "processing" if detail.state in {"RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED"}
                else "completed")
            groups[group].append(detail)
        for group in groups.values():
            group.sort(key=lambda s: (-s.activity_at.timestamp(), s.session_id))
        plan, _, accepted = _plan(db, config, semester, monday)
        blocks = []
        accepted_plan = None
        if accepted:
            root, revision = accepted
            calendar = _project_calendar_week_in_transaction(db, semester.stable_id, monday)
            accepted_detail = _revision(db, config, root, revision, as_of, calendar,
                accepted=True, course_filter_id=course.id)
            blocks = accepted_detail.blocks or []
            accepted_plan = AcceptedCoursePlanReadV1(revision_id=revision.id,
                revision_number=revision.revision_number,
                acceptance_id=accepted_detail.acceptance_id,
                accepted_at=accepted_detail.accepted_at,
                acceptance_actor=accepted_detail.acceptance_actor)
        courses = list(db.scalars(select(Course).where(Course.semester_id == semester.id)))
        activity = _events_in_transaction(db, config, semester, courses,
            activity_since, activity_through, course.id)
        activity.sort(key=lambda e: e.entity_id)
        activity.sort(key=lambda e: e.kind)
        activity.sort(key=lambda e: e.occurred_at, reverse=True)
        return CourseOverviewV1(as_of=local, semester=summary, course=identity,
            skills=skills, materials=materials, deadlines=deadlines, assessments=assessments,
            requirements=requirements, official_results=results,
            resumable_count=len(groups["resumable"]), processing_count=len(groups["processing"]),
            completed_count=len(groups["completed"]), resumable_sessions=groups["resumable"],
            processing_sessions=groups["processing"],
            completed_sessions=groups["completed"][:completed_limit], completed_limit=completed_limit,
            week_start=monday, plan=plan, accepted_plan=accepted_plan,
            accepted_course_blocks=blocks,
            activity_since=activity_since, activity_through=activity_through,
            activity_limit=activity_limit, activity=activity[:activity_limit])
