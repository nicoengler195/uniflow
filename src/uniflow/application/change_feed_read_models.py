"""One source row yields at most one bounded What Changed event."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from uniflow.application.application_read_models import _context, _read_transaction
from uniflow.application.weekly_plans import _load_verified
from uniflow.infrastructure.database.models import (
    ClassificationProposal, Course, Evidence, LearningHandoff, LearningResult,
    LearningSession, MasteryState, Material, MaterialArtifact, ParseRun,
    RepositoryAcademicLink, SemanticExtractionRun, SemesterAudit, SkillProposal,
    WeeklyPlan, WeeklyPlanAcceptance, WeeklyPlanBlockExecution, WeeklyPlanRevision,
)
from uniflow.schemas.change_feed_read_v1 import ChangeEventV1, ChangeFeedV1


def _utc(value: datetime) -> datetime:
    # Application event timestamps are written in UTC; SQLite drops tzinfo.
    return (value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _events_in_transaction(db, config, semester, courses, since, through, course_filter_id=None):
    by_id = {c.id: c for c in courses}
    material_course = {m.id: m.course_id for m in db.scalars(select(Material).where(
        Material.course_id.in_(by_id)))} if by_id else {}
    links = list(db.scalars(select(MaterialArtifact)))
    artifact_courses = defaultdict(set)
    for link in links:
        cid = material_course.get(link.material_id)
        if cid:
            artifact_courses[link.artifact_id].add(cid)
    repo_courses = defaultdict(set)
    for link in db.scalars(select(RepositoryAcademicLink)):
        if link.course_id in by_id:
            repo_courses[link.repository_source_id].add(link.course_id)
    plans = {p.id: p for p in db.scalars(select(WeeklyPlan).where(
        WeeklyPlan.semester_id == semester.id))}
    revisions = {r.id: r for r in db.scalars(select(WeeklyPlanRevision).where(
        WeeklyPlanRevision.weekly_plan_id.in_(plans)))} if plans else {}
    sessions = {s.id: s for s in db.scalars(select(LearningSession).where(
        LearningSession.course_id.in_(by_id)))} if by_id else {}
    handoffs = {h.id: h for h in db.scalars(select(LearningHandoff).where(
        LearningHandoff.learning_session_id.in_(sessions)))} if sessions else {}
    results = {r.id: r for r in db.scalars(select(LearningResult).where(
        LearningResult.learning_session_id.in_(sessions)))} if sessions else {}
    evidence_session = {e.id: e.learning_session_id for e in db.scalars(select(Evidence).where(
        Evidence.learning_session_id.in_(sessions)))} if sessions else {}
    runs = {r.id: r for r in db.scalars(select(SemanticExtractionRun))}
    material_link_by_id = {x.id: x for x in links}
    events = []

    def add(kind, row, at, cids=(), *, status=None, refs=(), entity_type=None):
        if at is None:
            return
        instant = _utc(at)
        if not (_utc(since) <= instant < _utc(through)):
            return
        scoped = sorted(set(cids) & by_id.keys())
        if course_filter_id and course_filter_id not in scoped:
            return
        # These rows have a direct plan -> Semester path even with no action.
        if not scoped and kind not in {"AUDIT_IMPORTED", "PLAN_GENERATED", "PLAN_EDITED", "PLAN_ACCEPTED"}:
            return
        events.append(ChangeEventV1(event_id=f"{kind}:{row.id}", kind=kind,
            occurred_at=instant, semester_id=semester.id,
            semester_stable_id=semester.stable_id, course_ids=scoped,
            course_stable_ids=[by_id[cid].stable_id for cid in scoped],
            entity_type=entity_type or type(row).__name__, entity_id=row.id,
            status=status, refs=sorted(set(x for x in refs if x))))

    for row in db.scalars(select(SemesterAudit).where(SemesterAudit.semester_id == semester.id)):
        add("AUDIT_IMPORTED", row, row.imported_at)
    for row in links:
        cid = material_course.get(row.material_id)
        add("MATERIAL_VERSION_LINKED", row, row.linked_at, [cid] if cid else [],
            refs=(row.material_id, row.artifact_id))
    for row in db.scalars(select(ParseRun)):
        if row.status in {"SUCCESS", "FAILED", "OCR_REQUIRED"}:
            add("PARSE_RUN_FINISHED", row, row.completed_at,
                artifact_courses[row.source_artifact_id], status=row.status,
                refs=(row.source_artifact_id,))
    for row in db.scalars(select(ClassificationProposal).where(
        ClassificationProposal.decided_at.is_not(None),
        ClassificationProposal.status.in_(("ACCEPTED", "REJECTED", "SUPERSEDED")))):
        cids = set()
        if row.proposed_course_id in by_id:
            cids.add(row.proposed_course_id)
        if not cids:
            if row.subject_material_id in material_course:
                cids.add(material_course[row.subject_material_id])
            if row.proposed_material_id in material_course:
                cids.add(material_course[row.proposed_material_id])
            if not cids:
                candidate = set()
                if row.subject_source_artifact_id:
                    candidate.update(artifact_courses[row.subject_source_artifact_id])
                if row.subject_repository_source_id:
                    candidate.update(repo_courses[row.subject_repository_source_id])
                if len(candidate) == 1:
                    cids.update(candidate)
        add("CLASSIFICATION_DECIDED", row, row.decided_at, cids,
            status=row.status, refs=(row.proposal_kind,))
    for row in db.scalars(select(SkillProposal).where(SkillProposal.decided_at.is_not(None),
        SkillProposal.status.in_(("ACCEPTED", "REJECTED", "SUPERSEDED")))):
        cids = set()
        if row.course_id in by_id:
            cids.add(row.course_id)
        run = runs.get(row.extraction_run_id)
        if run:
            if run.material_artifact_id in material_link_by_id:
                cid = material_course.get(material_link_by_id[run.material_artifact_id].material_id)
                if cid:
                    cids.add(cid)
            if run.repository_academic_link_id:
                link = db.get(RepositoryAcademicLink, run.repository_academic_link_id)
                if link and link.course_id in by_id:
                    cids.add(link.course_id)
        add("SKILL_PROPOSAL_DECIDED", row, row.decided_at, cids,
            status=row.status, refs=(row.proposal_kind,))
    revision_courses = {}
    for row in revisions.values():
        root = plans[row.weekly_plan_id]
        try:
            body = _load_verified(config, db, root, row)[3]
        except ValueError as exc:
            if str(exc) != "SOURCE_ARTIFACT_INVALID":
                raise
            cids = set()  # The relational Semester path remains authoritative.
        else:
            cids = {item.course_id for item in [*body.blocks, *body.unscheduled_actions]}
        revision_courses[row.id] = cids
        add("PLAN_GENERATED" if row.revision_kind == "GENERATED" else "PLAN_EDITED",
            row, row.created_at, cids, refs=(row.weekly_plan_id,))
    for row in db.scalars(select(WeeklyPlanAcceptance).where(
        WeeklyPlanAcceptance.weekly_plan_id.in_(plans))):
        rev = revisions.get(row.weekly_plan_revision_id)
        cids = revision_courses.get(rev.id, ()) if rev else ()
        add("PLAN_ACCEPTED", row, row.accepted_at, cids,
            refs=(row.weekly_plan_id, row.weekly_plan_revision_id))
    for row in db.scalars(select(WeeklyPlanBlockExecution).where(
        WeeklyPlanBlockExecution.weekly_plan_revision_id.in_(revisions))):
        session = sessions.get(row.learning_session_id)
        add("BLOCK_MATERIALIZED", row, row.materialized_at,
            [session.course_id] if session else [], status=row.execution_kind,
            refs=(row.weekly_plan_revision_id, row.block_id, row.learning_session_id))
    for row in handoffs.values():
        session = sessions[row.learning_session_id]
        add("HANDOFF_GENERATED", row, row.generated_at, [session.course_id],
            refs=(row.learning_session_id,))
    for row in results.values():
        session = sessions[row.learning_session_id]
        add("RESULT_IMPORTED", row, row.received_at, [session.course_id],
            status=row.processing_status, refs=(row.learning_session_id, row.learning_handoff_id))
    for row in db.scalars(select(MasteryState)):
        session_id = row.learning_session_id or evidence_session.get(row.evidence_id)
        session = sessions.get(session_id)
        add("MASTERY_RECORDED", row, row.calculated_at,
            [session.course_id] if session else [], refs=(row.skill_id, row.evidence_id))
    return events


def get_change_feed(config, semester_id: str, *, as_of: datetime, since: datetime,
                    through: datetime, course_id: str | None = None,
                    limit: int = 50) -> ChangeFeedV1:
    for label, value in (("AS_OF", as_of), ("SINCE", since), ("THROUGH", through)):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError(f"{label}_MUST_BE_AWARE")
    if not since < through <= as_of:
        raise ValueError("INVALID_CHANGE_FEED_RANGE")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("INVALID_LIMIT")
    with _read_transaction(config) as db:
        semester, _, local, _, summary = _context(db, semester_id, as_of)
        courses = list(db.scalars(select(Course).where(Course.semester_id == semester.id)
            .order_by(Course.stable_id, Course.id)))
        selected = next((c for c in courses if c.stable_id == course_id), None) if course_id else None
        if course_id is not None and selected is None:
            raise ValueError("UNKNOWN_COURSE_IN_SEMESTER")
        events = _events_in_transaction(db, config, semester, courses, since, through,
            selected.id if selected else None)
        events.sort(key=lambda e: e.entity_id)
        events.sort(key=lambda e: e.kind)
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        chosen = events[:limit]
        return ChangeFeedV1(as_of=local, since=since, through=through,
            semester=summary, course_filter_stable_id=course_id,
            events=chosen, returned_count=len(chosen), limit=limit)
