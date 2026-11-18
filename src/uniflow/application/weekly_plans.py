"""Persist exact generated weekly proposals and explicitly accept immutable revisions."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application.planning import plan_next_actions, _plan_next_actions_in_transaction
from uniflow.application.planning_calendar import project_calendar_week, _project_calendar_week_in_transaction
from uniflow.application.weekly_scheduler import _local_midnight, schedule_captured_week
from uniflow.application.weekly_plan_editing import (
    transform_move_block, transform_set_block_duration, transform_remove_block,
    transform_place_action, validate_weekly_plan_layout, verify_single_edit,
)
from uniflow.domain.enums import LearningSessionState
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, DerivedArtifact, LearningSession, LearningSessionTargetSkill,
    Semester, Skill, WeeklyPlan, WeeklyPlanAcceptance, WeeklyPlanRevision, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock
from uniflow.schemas.calendar_v1 import CalendarWeekProjectionV1, canonical_calendar_json
from uniflow.schemas.planning_v1 import PlanningRecommendationV1, canonical_planning_json
from uniflow.schemas.weekly_plan_v1 import (
    PlanBlockV1, PlanSourceV1, PlanUnscheduledActionV1, WeeklyPlanAssessmentV1,
    WeeklyPlanRevisionV1, canonical_weekly_plan_revision_json,
)
from uniflow.schemas.weekly_schedule_v1 import WeeklyScheduleV1, canonical_weekly_schedule_json

KINDS = ("PLANNING_RECOMMENDATION_JSON", "CALENDAR_WEEK_PROJECTION_JSON",
         "WEEKLY_SCHEDULE_JSON", "WEEKLY_PLAN_REVISION_JSON")
POLICY = "weekly-plan-revision-v1"
INHERIT = object()


def _db(config):
    return create_session_factory(get_engine(config.database_path))()


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _path(config, digest: str, kind: str) -> Path:
    return config.derived_root / "sha256" / digest[:2] / digest / f"{kind.lower()}.json"


def _verified(config, row: DerivedArtifact | None, kind: str, expected_sha: str | None = None) -> bytes:
    if row is None or row.kind != kind or (expected_sha and row.sha256 != expected_sha):
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    path = _path(config, row.sha256, kind)
    if Path(row.stored_path) != path or not path.is_file():
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ValueError("SOURCE_ARTIFACT_INVALID") from exc
    if len(body) != row.size_bytes or _sha(body) != row.sha256:
        raise ValueError("SOURCE_ARTIFACT_INVALID")
    return body


def _publish(config, db, body: bytes, kind: str, created_files: list[Path]) -> DerivedArtifact:
    digest = _sha(body)
    existing = db.scalar(select(DerivedArtifact).where(DerivedArtifact.sha256 == digest))
    if existing is not None:
        if existing.kind != kind:
            raise ValueError("ARTIFACT_KIND_CONFLICT")
        if _verified(config, existing, kind, digest) != body:
            raise ValueError("SOURCE_ARTIFACT_INVALID")
        return existing
    path = _path(config, digest, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        if path.exists():
            if path.read_bytes() != body:
                raise ValueError("SOURCE_ARTIFACT_INVALID")
        else:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fh:
                fh.write(body)
                temp = Path(fh.name)
            try:
                os.link(temp, path)
                created_files.append(path)
            except FileExistsError:
                if path.read_bytes() != body:
                    raise ValueError("SOURCE_ARTIFACT_INVALID")
        row = DerivedArtifact(sha256=digest, kind=kind, stored_path=str(path), size_bytes=len(body))
        db.add(row)
        db.flush()
        return row
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _clean_created(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _valid_overrides(durations) -> dict[str, int]:
    result = dict(durations or {})
    if any(not isinstance(k, str) or not k or type(v) is not int or v <= 0 for k, v in result.items()):
        raise ValueError("INVALID_ACTION_DURATION")
    return result


def _captured(config, semester_id: str, week_start: date, as_of: datetime | None,
              course_id: str | None, limit: int, durations: dict[str, int]):
    if week_start.weekday() != 0:
        raise ValueError("INVALID_WEEK_START")
    captured_at = as_of if as_of is not None else datetime.now(timezone.utc)
    if captured_at.utcoffset() is None:
        raise ValueError("AS_OF_MUST_BE_AWARE")
    nba = plan_next_actions(config, semester_id, as_of=captured_at, course_id=course_id, limit=limit)
    calendar = project_calendar_week(config, semester_id, week_start)
    schedule = schedule_captured_week(nba, calendar, durations)
    return nba, calendar, schedule


def _recheck(config, semester_id, week_start, course_id, limit, nba, calendar) -> None:
    try:
        nba_now = plan_next_actions(config, semester_id, as_of=nba.as_of, course_id=course_id, limit=limit)
        calendar_now = project_calendar_week(config, semester_id, week_start)
    except ValueError as exc:
        raise ValueError("SOURCE_CHANGED_DURING_CAPTURE") from exc
    if (_sha(canonical_planning_json(nba_now)) != _sha(canonical_planning_json(nba))
            or nba_now.context_fingerprint != nba.context_fingerprint
            or _sha(canonical_calendar_json(calendar_now)) != _sha(canonical_calendar_json(calendar))
            or calendar_now.context_fingerprint != calendar.context_fingerprint):
        raise ValueError("SOURCE_CHANGED_DURING_CAPTURE")


def _block(revision_id: str, value) -> PlanBlockV1:
    block_id = "plan-block:" + _sha(_json({"policy_version": POLICY,
        "revision_id": revision_id, "action_id": value.action_id}).encode("utf-8"))
    return PlanBlockV1(block_id=block_id, source_scheduler_block_id=value.block_id,
        action_id=value.action_id, nba_rank=value.nba_rank, action_type=value.action_type,
        course_id=value.course_id, course_stable_id=value.course_stable_id,
        target_skill_ids=value.target_skill_ids, existing_session_id=value.existing_session_id,
        starts_at=value.starts_at, ends_at=value.ends_at, duration_minutes=value.duration_minutes,
        duration_source="GENERATED_EXPLICIT_OVERRIDE" if value.duration_source == "EXPLICIT_OVERRIDE" else "GENERATED_PROFILE_DEFAULT",
        deadline_constraints=value.deadline_constraints, placement_source="GENERATED")


def _unscheduled(value) -> PlanUnscheduledActionV1:
    return PlanUnscheduledActionV1(**{**value.model_dump(mode="python"),
        "duration_source": "GENERATED_EXPLICIT_OVERRIDE" if value.duration_source == "EXPLICIT_OVERRIDE" else
            "GENERATED_PROFILE_DEFAULT" if value.duration_source == "PROFILE_DEFAULT" else "MISSING"})


def _expected_body(plan_id: str, revision_id: str, number: int, parent_id: str | None,
                   created_at: datetime, nba, calendar, schedule, source: PlanSourceV1):
    return WeeklyPlanRevisionV1(plan_id=plan_id, revision_id=revision_id,
        revision_number=number, revision_kind="GENERATED", parent_revision_id=parent_id,
        created_at=created_at, semester=schedule.semester, week_start=schedule.week_start,
        week_end_exclusive=schedule.week_end_exclusive, timezone=schedule.timezone,
        source=source, blocks=[_block(revision_id, b) for b in schedule.scheduled_blocks],
        unscheduled_actions=[_unscheduled(u) for u in schedule.unscheduled_actions])


def _current(db, plan_id: str) -> WeeklyPlanRevision | None:
    return db.scalar(select(WeeklyPlanRevision).where(WeeklyPlanRevision.weekly_plan_id == plan_id)
        .order_by(WeeklyPlanRevision.revision_number.desc()).limit(1))


def _persist(config, db, root: WeeklyPlan, previous: WeeklyPlanRevision | None,
             nba, calendar, schedule, course_id, limit, durations, created_files):
    nba_body, calendar_body, schedule_body = (canonical_planning_json(nba),
        canonical_calendar_json(calendar), canonical_weekly_schedule_json(schedule))
    nba_art = _publish(config, db, nba_body, KINDS[0], created_files)
    cal_art = _publish(config, db, calendar_body, KINDS[1], created_files)
    schedule_art = _publish(config, db, schedule_body, KINDS[2], created_files)
    source = PlanSourceV1(nba_artifact_id=nba_art.id, nba_payload_sha256=nba_art.sha256,
        nba_context_fingerprint=nba.context_fingerprint,
        calendar_artifact_id=cal_art.id, calendar_payload_sha256=cal_art.sha256,
        calendar_context_fingerprint=calendar.context_fingerprint,
        weekly_schedule_artifact_id=schedule_art.id,
        weekly_schedule_payload_sha256=schedule_art.sha256,
        scheduler_context_fingerprint=schedule.context_fingerprint,
        source_as_of=nba.as_of, source_course_filter_id=course_id, source_limit=limit,
        duration_overrides=durations)
    revision_id = str(uuid.uuid4())
    created_at = utc_now()
    number = previous.revision_number + 1 if previous else 1
    body = _expected_body(root.id, revision_id, number, previous.id if previous else None,
                          created_at, nba, calendar, schedule, source)
    revision_bytes = canonical_weekly_plan_revision_json(body)
    revision_art = _publish(config, db, revision_bytes, KINDS[3], created_files)
    row = WeeklyPlanRevision(id=revision_id, weekly_plan_id=root.id, revision_number=number,
        parent_revision_id=previous.id if previous else None, revision_kind="GENERATED",
        schema_version="1.0", policy_version=POLICY,
        nba_artifact_id=nba_art.id, calendar_artifact_id=cal_art.id,
        weekly_schedule_artifact_id=schedule_art.id, revision_artifact_id=revision_art.id,
        nba_payload_sha256=nba_art.sha256, calendar_payload_sha256=cal_art.sha256,
        weekly_schedule_payload_sha256=schedule_art.sha256, revision_payload_sha256=revision_art.sha256,
        nba_context_fingerprint=nba.context_fingerprint,
        calendar_context_fingerprint=calendar.context_fingerprint,
        scheduler_context_fingerprint=schedule.context_fingerprint,
        source_as_of=nba.as_of, source_course_filter_id=course_id, source_limit=limit,
        duration_overrides_json=_json(durations), created_at=created_at)
    db.add(row)
    db.flush()
    return {"plan_id": root.id, "revision_id": row.id, "revision_number": number,
            "revision_kind": row.revision_kind, "revision_payload_sha256": row.revision_payload_sha256,
            "accepted_revision_id": root.accepted_revision_id}


def create_weekly_plan(config, semester_id: str, week_start: date, *, as_of: datetime | None = None,
                       course_id: str | None = None, limit: int = 10, durations=None) -> dict:
    durations = _valid_overrides(durations)
    nba, calendar, schedule = _captured(config, semester_id, week_start, as_of, course_id, limit, durations)
    created_files: list[Path] = []
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        try:
            _recheck(config, semester_id, week_start, course_id, limit, nba, calendar)
            with _db(config) as db, db.begin():
                if db.scalar(select(WeeklyPlan).where(WeeklyPlan.semester_id == nba.semester.id,
                                                    WeeklyPlan.week_start == week_start)):
                    raise ValueError("PLAN_ALREADY_EXISTS")
                root = WeeklyPlan(id=str(uuid.uuid4()), semester_id=nba.semester.id,
                                  week_start=week_start)
                db.add(root)
                db.flush()
                return _persist(config, db, root, None, nba, calendar, schedule,
                                course_id, limit, durations, created_files)
        except Exception:
            _clean_created(created_files)
            raise


def regenerate_weekly_plan(config, plan_id: str, *, as_of: datetime | None = None,
                           course_id=INHERIT, limit: int | None = None, durations=INHERIT) -> dict:
    with _db(config) as db:
        root = db.get(WeeklyPlan, plan_id)
        if root is None:
            raise ValueError("UNKNOWN_WEEKLY_PLAN")
        previous = _current(db, plan_id)
        if previous is None:
            raise ValueError("PLAN_WITHOUT_REVISION")
        _load_verified(config, db, root, previous)
        semester = db.get(Semester, root.semester_id)
        semester_id, week_start = semester.stable_id, root.week_start
        chosen_course = previous.source_course_filter_id if course_id is INHERIT else course_id
        chosen_limit = previous.source_limit if limit is None else limit
        chosen_durations = (json.loads(previous.duration_overrides_json) if durations is INHERIT else durations)
        chosen_durations = _valid_overrides(chosen_durations)
    try:
        nba, calendar, schedule = _captured(config, semester_id, week_start, as_of,
                                            chosen_course, chosen_limit, chosen_durations)
    except ValueError as exc:
        if durations is INHERIT and str(exc) == "UNKNOWN_DURATION_ACTION":
            raise ValueError("STALE_DURATION_OVERRIDE") from exc
        raise
    created_files: list[Path] = []
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        try:
            _recheck(config, semester_id, week_start, chosen_course, chosen_limit, nba, calendar)
            with _db(config) as db, db.begin():
                root = db.get(WeeklyPlan, plan_id)
                if root is None or root.semester_id != nba.semester.id or root.week_start != week_start:
                    raise ValueError("PLAN_CHANGED_DURING_CAPTURE")
                current = _current(db, plan_id)
                if current.id != previous.id:
                    raise ValueError("PLAN_CHANGED_DURING_CAPTURE")
                result = _persist(config, db, root, current, nba, calendar, schedule,
                                  chosen_course, chosen_limit, chosen_durations, created_files)
                root.updated_at = utc_now()
                return result
        except Exception:
            _clean_created(created_files)
            raise


def _load_verified(config, db, root: WeeklyPlan, row: WeeklyPlanRevision):
    try:
        blobs = [_verified(config, db.get(DerivedArtifact, artifact_id), kind, digest)
            for artifact_id, kind, digest in (
                (row.nba_artifact_id, KINDS[0], row.nba_payload_sha256),
                (row.calendar_artifact_id, KINDS[1], row.calendar_payload_sha256),
                (row.weekly_schedule_artifact_id, KINDS[2], row.weekly_schedule_payload_sha256),
                (row.revision_artifact_id, KINDS[3], row.revision_payload_sha256))]
        nba = PlanningRecommendationV1.model_validate_json(blobs[0])
        calendar = CalendarWeekProjectionV1.model_validate_json(blobs[1])
        schedule = WeeklyScheduleV1.model_validate_json(blobs[2])
        body = WeeklyPlanRevisionV1.model_validate_json(blobs[3])
        if (canonical_planning_json(nba) != blobs[0] or canonical_calendar_json(calendar) != blobs[1]
                or canonical_weekly_schedule_json(schedule) != blobs[2]
                or canonical_weekly_plan_revision_json(body) != blobs[3]):
            raise ValueError("SOURCE_ARTIFACT_INVALID")
        source = body.source
        checks = (
            body.plan_id == root.id, body.revision_id == row.id,
            body.revision_number == row.revision_number, body.parent_revision_id == row.parent_revision_id,
            body.revision_kind == row.revision_kind,
            body.schema_version == row.schema_version, body.policy_version == row.policy_version,
            body.week_start == root.week_start == schedule.week_start == calendar.week_start,
            body.week_end_exclusive == schedule.week_end_exclusive == calendar.week_end_exclusive,
            body.semester.id == root.semester_id == nba.semester.id == schedule.semester.id == calendar.semester_id,
            body.semester.stable_id == nba.semester.stable_id == schedule.semester.stable_id == calendar.semester_stable_id,
            body.timezone == nba.semester.timezone == schedule.timezone == calendar.timezone,
            source.nba_artifact_id == row.nba_artifact_id,
            source.calendar_artifact_id == row.calendar_artifact_id,
            source.weekly_schedule_artifact_id == row.weekly_schedule_artifact_id,
            source.nba_payload_sha256 == row.nba_payload_sha256 == schedule.nba_payload_sha256,
            source.calendar_payload_sha256 == row.calendar_payload_sha256 == schedule.calendar_payload_sha256,
            source.weekly_schedule_payload_sha256 == row.weekly_schedule_payload_sha256,
            source.nba_context_fingerprint == row.nba_context_fingerprint == nba.context_fingerprint == schedule.nba_context_fingerprint,
            source.calendar_context_fingerprint == row.calendar_context_fingerprint == calendar.context_fingerprint == schedule.calendar_context_fingerprint,
            source.scheduler_context_fingerprint == row.scheduler_context_fingerprint == schedule.context_fingerprint,
            source.source_as_of == nba.as_of == schedule.as_of,
            source.source_course_filter_id == row.source_course_filter_id == nba.course_filter_id,
            source.source_limit == row.source_limit,
            _json(source.duration_overrides) == row.duration_overrides_json,
            row.source_as_of.replace(tzinfo=None) == source.source_as_of.replace(tzinfo=None),
            row.created_at.replace(tzinfo=None) == body.created_at.replace(tzinfo=None),
        )
        if not all(checks):
            raise ValueError("SOURCE_ARTIFACT_INVALID")
        if row.revision_kind == "GENERATED":
            expected = _expected_body(root.id, row.id, row.revision_number, row.parent_revision_id,
                                      body.created_at, nba, calendar, schedule, source)
            if (canonical_weekly_plan_revision_json(expected) != canonical_weekly_plan_revision_json(body)
                    or canonical_weekly_schedule_json(schedule_captured_week(
                        nba, calendar, source.duration_overrides)) != canonical_weekly_schedule_json(schedule)):
                raise ValueError("SOURCE_ARTIFACT_INVALID")
        elif row.revision_kind == "MANUAL_EDIT":
            parent_row = db.get(WeeklyPlanRevision, row.parent_revision_id) if row.parent_revision_id else None
            if (parent_row is None or parent_row.weekly_plan_id != root.id
                    or parent_row.revision_number != row.revision_number - 1):
                raise ValueError("SOURCE_ARTIFACT_INVALID")
            parent = _load_verified(config, db, root, parent_row)[3]
            if (source != parent.source or body.semester != parent.semester
                    or body.week_start != parent.week_start or body.timezone != parent.timezone):
                raise ValueError("SOURCE_ARTIFACT_INVALID")
            validate_weekly_plan_layout(body, calendar, schedule)
            verify_single_edit(parent, body, schedule)
        else:
            raise ValueError("SOURCE_ARTIFACT_INVALID")
        return nba, calendar, schedule, body
    except (OSError, TypeError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("SOURCE_ARTIFACT_INVALID") from exc


def read_weekly_plan_revision(config, plan_id: str, revision_number: int | None = None) -> WeeklyPlanRevisionV1:
    with _db(config) as db:
        root = db.get(WeeklyPlan, plan_id)
        if root is None:
            raise ValueError("UNKNOWN_WEEKLY_PLAN")
        row = _revision(db, plan_id, revision_number)
        return _load_verified(config, db, root, row)[3]


def _edit_weekly_plan(config, plan_id: str, expected_revision_id: str, transform) -> dict:
    created_files: list[Path] = []
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        try:
            with _db(config) as db, db.begin():
                root = db.get(WeeklyPlan, plan_id)
                if root is None:
                    raise ValueError("UNKNOWN_WEEKLY_PLAN")
                expected = db.get(WeeklyPlanRevision, expected_revision_id)
                if expected is None or expected.weekly_plan_id != plan_id:
                    raise ValueError("UNKNOWN_WEEKLY_PLAN_REVISION")
                current = _current(db, plan_id)
                if current is None or current.id != expected.id:
                    raise ValueError("REVISION_CHANGED")
                _, calendar, schedule, parent = _load_verified(config, db, root, current)
                revision_id, created_at = str(uuid.uuid4()), utc_now()
                body = transform(parent, schedule, revision_id, current.revision_number + 1, created_at)
                try:
                    body = WeeklyPlanRevisionV1.model_validate_json(canonical_weekly_plan_revision_json(body))
                except (ValidationError, TypeError, ValueError) as exc:
                    raise ValueError("SOURCE_ARTIFACT_INVALID") from exc
                validate_weekly_plan_layout(body, calendar, schedule)
                verify_single_edit(parent, body, schedule)
                artifact = _publish(config, db, canonical_weekly_plan_revision_json(body), KINDS[3], created_files)
                row = WeeklyPlanRevision(id=revision_id, weekly_plan_id=root.id,
                    revision_number=body.revision_number, parent_revision_id=current.id,
                    revision_kind="MANUAL_EDIT", schema_version="1.0", policy_version=POLICY,
                    nba_artifact_id=current.nba_artifact_id,
                    calendar_artifact_id=current.calendar_artifact_id,
                    weekly_schedule_artifact_id=current.weekly_schedule_artifact_id,
                    revision_artifact_id=artifact.id,
                    nba_payload_sha256=current.nba_payload_sha256,
                    calendar_payload_sha256=current.calendar_payload_sha256,
                    weekly_schedule_payload_sha256=current.weekly_schedule_payload_sha256,
                    revision_payload_sha256=artifact.sha256,
                    nba_context_fingerprint=current.nba_context_fingerprint,
                    calendar_context_fingerprint=current.calendar_context_fingerprint,
                    scheduler_context_fingerprint=current.scheduler_context_fingerprint,
                    source_as_of=current.source_as_of,
                    source_course_filter_id=current.source_course_filter_id,
                    source_limit=current.source_limit,
                    duration_overrides_json=current.duration_overrides_json,
                    created_at=created_at)
                db.add(row)
                db.flush()
                root.updated_at = created_at
                return {"plan_id": root.id, "revision_id": row.id,
                    "revision_number": row.revision_number, "parent_revision_id": current.id,
                    "revision_kind": row.revision_kind,
                    "revision_payload_sha256": row.revision_payload_sha256,
                    "accepted_revision_id": root.accepted_revision_id}
        except Exception:
            _clean_created(created_files)
            raise


def move_weekly_plan_block(config, plan_id: str, block_id: str, new_start: datetime,
                           *, expected_revision_id: str) -> dict:
    return _edit_weekly_plan(config, plan_id, expected_revision_id,
        lambda parent, schedule, rid, number, at:
            transform_move_block(parent, rid, number, at, block_id, new_start))


def set_weekly_plan_block_duration(config, plan_id: str, block_id: str, minutes: int,
                                   *, expected_revision_id: str) -> dict:
    return _edit_weekly_plan(config, plan_id, expected_revision_id,
        lambda parent, schedule, rid, number, at:
            transform_set_block_duration(parent, rid, number, at, block_id, minutes))


def remove_weekly_plan_block(config, plan_id: str, block_id: str,
                             *, expected_revision_id: str) -> dict:
    return _edit_weekly_plan(config, plan_id, expected_revision_id,
        lambda parent, schedule, rid, number, at:
            transform_remove_block(parent, rid, number, at, block_id))


def place_weekly_plan_action(config, plan_id: str, action_id: str, start: datetime,
                             *, duration_minutes: int | None = None,
                             expected_revision_id: str) -> dict:
    return _edit_weekly_plan(config, plan_id, expected_revision_id,
        lambda parent, schedule, rid, number, at:
            transform_place_action(parent, schedule, rid, number, at, action_id, start, duration_minutes))


def _revision(db, plan_id: str, number: int | None) -> WeeklyPlanRevision:
    row = (_current(db, plan_id) if number is None else
        db.scalar(select(WeeklyPlanRevision).where(WeeklyPlanRevision.weekly_plan_id == plan_id,
                                                   WeeklyPlanRevision.revision_number == number)))
    if row is None:
        raise ValueError("UNKNOWN_WEEKLY_PLAN_REVISION")
    return row


def _action_valid(db, block, semester_id: str) -> bool:
    course = db.get(Course, block.course_id)
    if course is None or course.semester_id != semester_id or course.stable_id != block.course_stable_id:
        return False
    if block.action_type == "START_SKILL_SESSION":
        if block.existing_session_id is not None or len(block.target_skill_ids) != 1:
            return False
        skill_id = block.target_skill_ids[0]
        skill = db.get(Skill, skill_id)
        relation = db.scalar(select(CourseSkill).where(CourseSkill.course_id == course.id,
            CourseSkill.skill_id == skill_id, CourseSkill.is_active.is_(True)))
        return skill is not None and skill.status == "ACTIVE" and relation is not None
    session = db.get(LearningSession, block.existing_session_id) if block.existing_session_id else None
    if session is None or session.course_id != course.id or session.state not in {
            LearningSessionState.PLANNED, LearningSessionState.HANDOFF_CREATED,
            LearningSessionState.IN_PROGRESS}:
        return False
    targets = list(db.scalars(select(LearningSessionTargetSkill).where(
        LearningSessionTargetSkill.learning_session_id == session.id).order_by(LearningSessionTargetSkill.ordinal)))
    return [x.skill_id for x in targets] == block.target_skill_ids


def _assess(config, db, root, row, checked_at: datetime) -> WeeklyPlanAssessmentV1:
    factors: list[str] = []
    nba_match = calendar_match = None
    try:
        nba, calendar, schedule, body = _load_verified(config, db, root, row)
    except ValueError:
        factors.append("SOURCE_ARTIFACT_INVALID")
    else:
        try:
            replay = _plan_next_actions_in_transaction(db, config, nba.semester.stable_id,
                as_of=body.source.source_as_of, course_id=body.source.source_course_filter_id,
                limit=body.source.source_limit)
            nba_match = (_sha(canonical_planning_json(replay)) == row.nba_payload_sha256
                         and replay.context_fingerprint == row.nba_context_fingerprint)
        except ValueError:
            nba_match = False
        if not nba_match:
            factors.append("NBA_CONTEXT_CHANGED")
        try:
            replay_cal = _project_calendar_week_in_transaction(db, nba.semester.stable_id, root.week_start)
            calendar_match = (_sha(canonical_calendar_json(replay_cal)) == row.calendar_payload_sha256
                              and replay_cal.context_fingerprint == row.calendar_context_fingerprint)
        except ValueError:
            calendar_match = False
        if not calendar_match:
            factors.append("CALENDAR_CONTEXT_CHANGED")
        week_end = _local_midnight(schedule.week_end_exclusive, ZoneInfo(schedule.timezone))
        if checked_at.astimezone(timezone.utc) >= week_end.astimezone(timezone.utc):
            factors.append("WEEK_ELAPSED")
        if any(block.starts_at.astimezone(timezone.utc) < checked_at.astimezone(timezone.utc)
               for block in body.blocks):
            factors.append("BLOCK_IN_PAST")
        if any(not _action_valid(db, action, root.semester_id)
               for action in [*body.blocks, *body.unscheduled_actions]):
            factors.append("ACTION_NO_LONGER_VALID")
    return WeeklyPlanAssessmentV1(plan_id=root.id, revision_id=row.id,
        revision_number=row.revision_number, checked_at=checked_at,
        status="INVALID" if "SOURCE_ARTIFACT_INVALID" in factors else "STALE" if factors else "FRESH",
        factors=factors, nba_match=nba_match, calendar_match=calendar_match)


def assess_weekly_plan_revision(config, plan_id: str, revision_number: int | None = None,
                                *, checked_at: datetime | None = None) -> WeeklyPlanAssessmentV1:
    checked_at = checked_at if checked_at is not None else datetime.now(timezone.utc)
    if checked_at.utcoffset() is None:
        raise ValueError("CHECKED_AT_MUST_BE_AWARE")
    with _db(config) as db:
        root = db.get(WeeklyPlan, plan_id)
        if root is None:
            raise ValueError("UNKNOWN_WEEKLY_PLAN")
        return _assess(config, db, root, _revision(db, plan_id, revision_number), checked_at)


def accept_weekly_plan_revision(config, plan_id: str, revision_number: int | None = None,
                                *, accepted_at: datetime | None = None) -> dict:
    accepted_at = accepted_at if accepted_at is not None else datetime.now(timezone.utc)
    if accepted_at.utcoffset() is None:
        raise ValueError("ACCEPTED_AT_MUST_BE_AWARE")
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            root = db.get(WeeklyPlan, plan_id)
            if root is None:
                raise ValueError("UNKNOWN_WEEKLY_PLAN")
            row = _revision(db, plan_id, revision_number)
            if root.accepted_revision_id == row.id:
                _load_verified(config, db, root, row)
                event = db.scalar(select(WeeklyPlanAcceptance).where(
                    WeeklyPlanAcceptance.weekly_plan_revision_id == row.id))
                if event is None or event.weekly_plan_id != root.id:
                    raise ValueError("ACCEPTANCE_HISTORY_INVALID")
                return {"status": "ALREADY_ACCEPTED", "plan_id": root.id,
                        "revision_id": row.id, "acceptance_id": event.id,
                        "accepted_at": event.accepted_at.isoformat()}
            if row.id != _current(db, plan_id).id:
                raise ValueError("REVISION_NOT_CURRENT")
            assessment = _assess(config, db, root, row, accepted_at)
            if assessment.status != "FRESH":
                return {"status": assessment.status, "assessment": assessment.model_dump(mode="json")}
            event = WeeklyPlanAcceptance(id=str(uuid.uuid4()), weekly_plan_id=root.id,
                weekly_plan_revision_id=row.id, accepted_at=accepted_at, actor="MANUAL_USER")
            db.add(event)
            db.flush()
            root.accepted_revision_id = row.id
            root.updated_at = utc_now()
            return {"status": "ACCEPTED", "plan_id": root.id, "revision_id": row.id,
                    "acceptance_id": event.id, "accepted_at": accepted_at.isoformat()}


def weekly_plan_info(config, plan_id: str) -> dict:
    with _db(config) as db:
        root = db.get(WeeklyPlan, plan_id)
        if root is None:
            raise ValueError("UNKNOWN_WEEKLY_PLAN")
        current = _revision(db, plan_id, None)
        body = _load_verified(config, db, root, current)[3]
        return {"plan_id": root.id, "semester_id": root.semester_id,
            "week_start": root.week_start.isoformat(),
            "current_revision_id": current.id, "current_revision_number": current.revision_number,
            "accepted_revision_id": root.accepted_revision_id,
            "revision_kind": current.revision_kind,
            "revision_payload_sha256": current.revision_payload_sha256,
            "nba_payload_sha256": current.nba_payload_sha256,
            "calendar_payload_sha256": current.calendar_payload_sha256,
            "weekly_schedule_payload_sha256": current.weekly_schedule_payload_sha256,
            "source_as_of": body.source.source_as_of.isoformat(),
            "source_course_filter_id": current.source_course_filter_id,
            "source_limit": current.source_limit}


def weekly_plan_history(config, plan_id: str) -> dict:
    with _db(config) as db:
        root = db.get(WeeklyPlan, plan_id)
        if root is None:
            raise ValueError("UNKNOWN_WEEKLY_PLAN")
        rows = list(db.scalars(select(WeeklyPlanRevision).where(
            WeeklyPlanRevision.weekly_plan_id == plan_id).order_by(WeeklyPlanRevision.revision_number)))
        for row in rows:
            _load_verified(config, db, root, row)
        events = list(db.scalars(select(WeeklyPlanAcceptance).where(
            WeeklyPlanAcceptance.weekly_plan_id == plan_id).order_by(WeeklyPlanAcceptance.accepted_at)))
        return {"plan_id": plan_id, "current_revision_id": rows[-1].id if rows else None,
            "accepted_revision_id": root.accepted_revision_id,
            "revisions": [{"revision_id": r.id, "revision_number": r.revision_number,
                "parent_revision_id": r.parent_revision_id, "revision_kind": r.revision_kind,
                "revision_payload_sha256": r.revision_payload_sha256} for r in rows],
            "acceptances": [{"acceptance_id": e.id, "revision_id": e.weekly_plan_revision_id,
                "accepted_at": e.accepted_at.isoformat(), "actor": e.actor} for e in events]}
