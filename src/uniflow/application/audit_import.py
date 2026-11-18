from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uniflow.domain.enums import InformationStatus
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, AssessmentRequirement, AuditFactRecord, AuditResolutionRecord, Course, Deadline,
    MaterialReleaseRule, ScheduleEntry, ScheduleException, Semester, SemesterAudit,
    SourceReference, utc_now,
)
from uniflow.schemas.semester_audit import SemesterAuditV1
from uniflow.schemas.semester_audit_resolution import SemesterAuditResolutionV1

REGISTRY = {("semester_audit", "1.0"): SemesterAuditV1}
PRIORITY = {"UNKNOWN": 0, "INFERRED": 1, "OBSERVED": 2, "CONFIRMED": 3}


@dataclass(frozen=True)
class LoadedAudit:
    audit: SemesterAuditV1
    original_file_sha256: str
    canonical_content_sha256: str
    raw: bytes


@dataclass(frozen=True)
class LoadedResolution:
    resolution: SemesterAuditResolutionV1
    original_file_sha256: str
    canonical_content_sha256: str
    raw: bytes


@dataclass(frozen=True)
class ImportAction:
    action: str
    entity_type: str
    stable_id: str
    detail: str = ""


@dataclass(frozen=True)
class ImportResult:
    status: str
    revision_number: int | None
    actions: tuple[ImportAction, ...]
    canonical_content_sha256: str
    stored_file_path: Path | None = None


def canonical_json(audit: SemesterAuditV1) -> str:
    data = audit.model_dump(mode="json")
    data["courses"].sort(key=lambda item: item["stable_id"])
    data["known_unknowns"].sort()
    for course in data["courses"]:
        course["unknowns"].sort()
        for key in ("assessments", "deadlines", "requirements", "schedules",
                    "schedule_exceptions", "material_release_rules"):
            course[key].sort(key=lambda item: item["stable_id"])
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_audit(path: Path) -> LoadedAudit:
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        document = yaml.safe_load(raw)
    elif suffix == ".json":
        document = json.loads(raw)
    else:
        raise ValueError("audit must be .yaml, .yml, or .json")
    if not isinstance(document, dict):
        raise ValueError("audit root must be an object")
    key = (document.get("kind"), document.get("schema_version"))
    model = REGISTRY.get(key)
    if model is None:
        raise ValueError(f"unsupported audit kind/version: {key}")
    audit = model.model_validate(document)
    from uniflow.application.sheet_deadlines import compile_sheet_deadlines
    compile_sheet_deadlines(audit)  # Fail before planning or writing on ambiguous series.
    canonical = canonical_json(audit).encode("utf-8")
    return LoadedAudit(audit, hashlib.sha256(raw).hexdigest(), hashlib.sha256(canonical).hexdigest(), raw)


def load_resolution(path: Path) -> LoadedResolution:
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix.lower() in (".yaml", ".yml"):
        document = yaml.safe_load(raw)
    elif path.suffix.lower() == ".json":
        document = json.loads(raw)
    else:
        raise ValueError("resolution must be .yaml, .yml, or .json")
    if not isinstance(document, dict):
        raise ValueError("resolution root must be an object")
    resolution = SemesterAuditResolutionV1.model_validate(document)
    data = resolution.model_dump(mode="json")
    data["resolutions"].sort(key=lambda item: (item["entity_type"], item["stable_id"]))
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return LoadedResolution(resolution, hashlib.sha256(raw).hexdigest(), hashlib.sha256(canonical).hexdigest(), raw)


def canonical_fact_json(entity_type: str, stable_id: str, payload_json: str, status: InformationStatus) -> str:
    """One identity representation for both stored and proposed audit facts."""
    return json.dumps({"entity_type": entity_type, "stable_id": stable_id,
                       "payload": json.loads(payload_json), "information_status": status.value},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fact_sha256(entity_type: str, stable_id: str, payload_json: str, status: InformationStatus) -> str:
    return hashlib.sha256(canonical_fact_json(entity_type, stable_id, payload_json, status).encode("utf-8")).hexdigest()


def _write_verified_archive(destination: Path, raw: bytes, expected_sha256: str) -> None:
    destination.write_bytes(raw)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError(f"accepted archive SHA-256 mismatch: {destination.name}")


def _checked_current_fact(existing: AuditFactRecord) -> str:
    try:
        payload = json.loads(existing.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if existing.entity_type != "semester":
            if payload.get("stable_id") != existing.stable_id or payload.get("information_status") != existing.information_status.value:
                raise ValueError("record status or stable ID differs from its payload")
            if "provenance" not in payload:
                raise ValueError("record provenance missing from payload")
        return fact_sha256(existing.entity_type, existing.stable_id, existing.payload_json, existing.information_status)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"inconsistent current audit fact: {existing.stable_id}") from exc


def _facts(audit: SemesterAuditV1):
    yield "semester", audit.semester, {
        "name": audit.semester_name, "state": audit.state.value,
        "starts_on": audit.starts_on.isoformat() if audit.starts_on else None,
        "ends_on": audit.ends_on.isoformat() if audit.ends_on else None,
        "timezone": audit.timezone,
    }, InformationStatus.CONFIRMED, audit.provenance, None
    for course in audit.courses:
        yield "course", course.stable_id, course.model_dump(mode="json", exclude={
            "assessments", "deadlines", "requirements", "schedules", "schedule_exceptions", "material_release_rules",
        }), course.information_status, course.provenance, course.stable_id
        for key, entity in (
            ("assessments", "assessment"), ("deadlines", "deadline"), ("requirements", "requirement"),
            ("schedules", "schedule"), ("schedule_exceptions", "exception"),
            ("material_release_rules", "material_release"),
        ):
            for item in getattr(course, key):
                yield entity, item.stable_id, item.model_dump(mode="json"), item.information_status, item.provenance, course.stable_id
    from uniflow.application.sheet_deadlines import compile_sheet_deadlines
    for course_id, item in compile_sheet_deadlines(audit):
        yield "deadline", item.stable_id, item.model_dump(mode="json"), item.information_status, item.provenance, course_id


def _plan(session: Session, loaded: LoadedAudit, resolution: LoadedResolution | None = None) -> ImportResult:
    existing_hash = session.scalar(select(SemesterAudit).where(
        SemesterAudit.canonical_content_sha256 == loaded.canonical_content_sha256))
    if existing_hash:
        return ImportResult("ALREADY_IMPORTED", existing_hash.revision_number,
                            (ImportAction("ALREADY_IMPORTED", "audit", loaded.audit.audit_id),),
                            loaded.canonical_content_sha256, Path(existing_hash.stored_file_path))
    decisions = {}
    if resolution is not None:
        manifest = resolution.resolution
        if (manifest.semester != loaded.audit.semester or manifest.audit_id != loaded.audit.audit_id
                or manifest.candidate_canonical_sha256 != loaded.canonical_content_sha256):
            raise ValueError("resolution candidate identity mismatch")
        decisions = {(item.entity_type, item.stable_id): item for item in manifest.resolutions}
    consumed: set[tuple[str, str]] = set()
    actions: list[ImportAction] = []
    from uniflow.application.sheet_deadlines import compile_sheet_deadlines
    generated = {item.stable_id: (cid, item) for cid, item in compile_sheet_deadlines(loaded.audit)}
    for course in loaded.audit.courses:
        current_course = session.scalar(select(Course).where(Course.stable_id == course.stable_id))
        if current_course is None:
            continue
        stored_deadlines = session.scalars(select(Deadline).where(Deadline.course_id == current_course.id)).all()
        for requirement in course.requirements:
            previous = session.get(AuditFactRecord, requirement.stable_id)
            previous_series = (previous is not None and previous.entity_type == "requirement" and
                json.loads(previous.payload_json).get("requirement_type") == "SHEET_SUBMISSION_SERIES_V1")
            if requirement.requirement_type != "SHEET_SUBMISSION_SERIES_V1":
                if previous_series:
                    actions.append(ImportAction("CONFLICT", "requirement", requirement.stable_id,
                        "changing a series contract type requires a separate withdrawal contract"))
                continue
            retained = set(generated) | {i.get("existing_deadline_id")
                for i in requirement.parameters["instances"] if not i.get("cancelled", False)}
            for stored in stored_deadlines:
                if (stored.stable_id and stored.stable_id.startswith(requirement.stable_id + "@sheet-")
                        and stored.stable_id not in retained):
                    actions.append(ImportAction("CONFLICT", "deadline", stored.stable_id,
                        "removing or cancelling a persisted series deadline requires a separate withdrawal contract"))
        for stable_id, (cid, item) in generated.items():
            if cid != course.stable_id:
                continue
            for stored in stored_deadlines:
                local_due = stored.due_at
                if local_due is not None and local_due.tzinfo is not None:
                    local_due = local_due.astimezone(ZoneInfo(loaded.audit.timezone))
                if (stored.stable_id != stable_id and local_due is not None
                        and local_due.date() == item.due_date and local_due.time() == item.due_time):
                    actions.append(ImportAction("CONFLICT", "deadline", stable_id,
                        "persisted same-course deadline collision; include and reference existing deadline"))
    existing_models = {
        "semester": Semester, "course": Course, "assessment": Assessment, "deadline": Deadline,
        "requirement": AssessmentRequirement, "schedule": ScheduleEntry,
        "exception": ScheduleException, "material_release": MaterialReleaseRule,
    }
    for entity, stable_id, payload, status, _, _ in _facts(loaded.audit):
        existing = session.get(AuditFactRecord, stable_id)
        if existing is None:
            unmanaged = session.scalar(select(existing_models[entity]).where(
                existing_models[entity].stable_id == stable_id))
            if unmanaged is not None:
                actions.append(ImportAction("CONFLICT", entity, stable_id, "existing item has no audit provenance"))
            else:
                actions.append(ImportAction("CREATE", entity, stable_id))
            continue
        incoming = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if existing.entity_type != entity:
            actions.append(ImportAction("CONFLICT", entity, stable_id, "stable ID belongs to another entity type"))
        elif existing.payload_json == incoming:
            _checked_current_fact(existing)
            actions.append(ImportAction("NO_CHANGE", entity, stable_id))
        elif status == InformationStatus.UNKNOWN and existing.information_status != InformationStatus.UNKNOWN:
            _checked_current_fact(existing)
            actions.append(ImportAction("NO_CHANGE", entity, stable_id, "unknown cannot replace known"))
        elif PRIORITY[status.value] < PRIORITY[existing.information_status.value]:
            detail = "lower-confidence changed fact"
            actions.append(_resolved_action(session, loaded.audit.semester, existing, entity, stable_id,
                                            incoming, status, decisions, consumed, detail))
        elif existing.information_status == InformationStatus.UNKNOWN and status != InformationStatus.UNKNOWN:
            _checked_current_fact(existing)
            actions.append(ImportAction("UPDATE", entity, stable_id, "unknown to known"))
        else:
            detail = "changed fact needs explicit resolution"
            actions.append(_resolved_action(session, loaded.audit.semester, existing, entity, stable_id,
                                            incoming, status, decisions, consumed, detail))
    if decisions.keys() != consumed:
        raise ValueError("extraneous or stale resolution decision")
    semester = session.scalar(select(Semester).where(Semester.stable_id == loaded.audit.semester))
    revision = (session.scalar(select(func.max(SemesterAudit.revision_number)).where(
        SemesterAudit.semester_id == semester.id)) or 0) + 1 if semester else 1
    state = "CONFLICT" if any(a.action == "CONFLICT" for a in actions) else "READY"
    return ImportResult(state, revision, tuple(actions), loaded.canonical_content_sha256)


def _resolved_action(session: Session, semester_id: str, existing: AuditFactRecord, entity: str, stable_id: str, incoming: str,
                     status: InformationStatus, decisions: dict, consumed: set, detail: str) -> ImportAction:
    old_hash = _checked_current_fact(existing)
    key = (entity, stable_id)
    decision = decisions.get(key)
    if decision is None:
        return ImportAction("CONFLICT", entity, stable_id, detail)
    previous_revision = session.get(SemesterAudit, existing.audit_revision_id) if existing.audit_revision_id else None
    previous_semester = session.get(Semester, previous_revision.semester_id) if previous_revision else None
    if (previous_semester is None or previous_semester.stable_id != semester_id
            or decision.expected_current_revision_id != existing.audit_revision_id
            or decision.expected_current_fact_sha256 != old_hash
            or decision.proposed_fact_sha256 != fact_sha256(entity, stable_id, incoming, status)):
        raise ValueError(f"stale resolution decision: {entity} {stable_id}")
    consumed.add(key)
    return ImportAction("RESOLVED_UPDATE", entity, stable_id, "explicit ACCEPT_REPLACEMENT")


def _source(session: Session, provenance, fallback: str, revision: SemesterAudit) -> str:
    data = provenance.model_dump(mode="json") if provenance else {}
    source = SourceReference(
        source_type=data.get("source_type", "semester_audit"),
        uri=data.get("uri"), location=data.get("location"),
        description=data.get("description", fallback),
        information_status=InformationStatus(data.get("information_status", "UNKNOWN")),
    )
    session.add(source)
    session.flush()
    return source.id


def _persist(session: Session, loaded: LoadedAudit, result: ImportResult, destination: Path,
             resolution: LoadedResolution | None = None, resolution_destination: Path | None = None) -> None:
    audit = loaded.audit
    semester = session.scalar(select(Semester).where(Semester.stable_id == audit.semester))
    if semester is None:
        semester = Semester(stable_id=audit.semester, name=audit.semester_name)
        session.add(semester)
        session.flush()
    revision = SemesterAudit(
        semester_id=semester.id, schema_version=audit.schema_version, audit_id=audit.audit_id,
        revision_number=result.revision_number, original_file_sha256=loaded.original_file_sha256,
        canonical_content_sha256=loaded.canonical_content_sha256, stored_file_path=str(destination),
        file_path=str(destination), imported_at=utc_now(),
    )
    session.add(revision)
    session.flush()
    revision.source_id = _source(session, audit.provenance, audit.audit_id, revision)
    action_by_id = {a.stable_id: a for a in result.actions}
    course_ids: dict[str, str] = {}
    for course in audit.courses:
        row = session.scalar(select(Course).where(Course.stable_id == course.stable_id))
        if row is None:
            row = Course(semester_id=semester.id, stable_id=course.stable_id, name=course.name)
            session.add(row)
            session.flush()
        course_ids[course.stable_id] = row.id
    for entity, stable_id, payload, status, provenance, course_stable_id in _facts(audit):
        action = action_by_id[stable_id]
        if action.action not in ("CREATE", "UPDATE", "RESOLVED_UPDATE"):
            continue
        source_id = _source(session, provenance, stable_id, revision)
        record = session.get(AuditFactRecord, stable_id)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if action.action == "RESOLVED_UPDATE":
            assert record is not None and resolution is not None and resolution_destination is not None
            decision = next(item for item in resolution.resolution.resolutions
                            if item.entity_type == entity and item.stable_id == stable_id)
            session.add(AuditResolutionRecord(
                audit_revision_id=revision.id, entity_type=entity, stable_id=stable_id,
                old_fact_sha256=decision.expected_current_fact_sha256,
                new_fact_sha256=decision.proposed_fact_sha256,
                old_payload_json=record.payload_json, new_payload_json=serialized,
                old_information_status=record.information_status, new_information_status=status,
                previous_audit_revision_id=record.audit_revision_id,
                action=decision.action, reason=decision.reason, actor=decision.actor,
                decided_at=decision.decided_at,
                resolution_raw_sha256=resolution.original_file_sha256,
                resolution_canonical_sha256=resolution.canonical_content_sha256,
                resolution_stored_file_path=str(resolution_destination),
            ))
        if record is None:
            record = AuditFactRecord(stable_id=stable_id, entity_type=entity, payload_json=serialized,
                                     information_status=status, source_id=source_id, audit_revision_id=revision.id)
            session.add(record)
        else:
            record.payload_json = serialized
            record.information_status = status
            record.source_id = source_id
            record.audit_revision_id = revision.id
        if entity == "semester":
            semester.name = payload["name"]
            semester.state = payload["state"]
            semester.starts_on = audit.starts_on
            semester.ends_on = audit.ends_on
            semester.timezone = audit.timezone
            semester.source_id = source_id
        elif entity == "course":
            row = session.scalar(select(Course).where(Course.stable_id == stable_id))
            row.name = payload["name"]
            row.code = payload["code"]
            row.kind = payload["kind"]
            row.parent_course_id = course_ids.get(payload["parent_stable_id"])
            row.participation = payload["participation"]
            row.participation_reason = payload["participation_reason"]
            row.unknowns_json = json.dumps(payload["unknowns"])
            row.information_status = status
            row.source_id = source_id
            row.audit_revision_id = revision.id
        elif entity == "assessment":
            row = session.scalar(select(Assessment).where(Assessment.stable_id == stable_id))
            if row is None:
                row = Assessment(stable_id=stable_id, course_id=course_ids[course_stable_id])
                session.add(row)
            row.name = payload["name"]
            row.assessment_type = payload["assessment_type"]
            row.rules_summary = payload["rules_summary"]
            row.information_status = status
            row.source_id = source_id
            row.audit_revision_id = revision.id
        elif entity == "deadline":
            row = session.scalar(select(Deadline).where(Deadline.stable_id == stable_id))
            if row is None:
                row = Deadline(stable_id=stable_id, course_id=course_ids[course_stable_id], title=payload["title"])
                session.add(row)
            row.title = payload["title"]
            row.description = payload["description"]
            row.is_date_only = payload["due_date"] is not None and payload["due_time"] is None
            if payload["due_date"]:
                clock = time.fromisoformat(payload["due_time"]) if payload["due_time"] else time.min
                row.due_at = datetime.combine(datetime.fromisoformat(payload["due_date"]).date(), clock,
                                              tzinfo=ZoneInfo(audit.timezone))
            else:
                row.due_at = None
            row.information_status = status
            row.source_id = source_id
            row.audit_revision_id = revision.id
        else:
            model = {
                "requirement": AssessmentRequirement, "schedule": ScheduleEntry,
                "exception": ScheduleException, "material_release": MaterialReleaseRule,
            }[entity]
            row = session.scalar(select(model).where(model.stable_id == stable_id))
            if row is None:
                row = model(stable_id=stable_id, course_id=course_ids[course_stable_id],
                            payload_json=serialized, information_status=status)
                session.add(row)
            row.payload_json = serialized
            row.information_status = status
            row.source_id = source_id
            row.audit_revision_id = revision.id


from uniflow.infrastructure.write_lock import project_write_lock


def import_audit(path: Path, config: AppConfig, *, dry_run: bool = False,
                 resolution_path: Path | None = None) -> ImportResult:
    if dry_run:
        return _import_audit_unlocked(path, config, dry_run=True, resolution_path=resolution_path)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        return _import_audit_unlocked(path, config, resolution_path=resolution_path)


def _import_audit_unlocked(path: Path, config: AppConfig, *, dry_run: bool = False,
                           resolution_path: Path | None = None) -> ImportResult:
    path = Path(path)
    try:
        loaded = load_audit(path)
    except (ValueError, ValidationError, yaml.YAMLError, json.JSONDecodeError) as exc:
        if not dry_run and path.is_file():
            config.audit_rejected_root.mkdir(parents=True, exist_ok=True)
            rejected = config.audit_rejected_root / f"{uuid4().hex}_{path.name}"
            shutil.copyfile(path, rejected)
            rejected.with_suffix(rejected.suffix + ".txt").write_text(str(exc), encoding="utf-8")
        raise
    resolution = load_resolution(resolution_path) if resolution_path is not None else None
    Session = create_session_factory(get_engine(config.database_path))
    if dry_run:
        with Session() as session:
            return _plan(session, loaded, resolution)
    destination: Path | None = None
    resolution_destination: Path | None = None
    archive_directory_created = False
    try:
        with Session.begin() as session:
            result = _plan(session, loaded, resolution)
            if result.status != "READY":
                return result
            destination = config.audit_accepted_root / loaded.audit.semester / (
                f"rev{result.revision_number}_{loaded.canonical_content_sha256[:16]}_{uuid4().hex[:8]}{path.suffix.lower()}")
            archive_directory_created = not destination.parent.exists()
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolution_destination = (destination.with_name(destination.stem + ".resolution" + resolution_path.suffix.lower())
                                      if resolution is not None and resolution_path is not None else None)
            _write_verified_archive(destination, loaded.raw, loaded.original_file_sha256)
            if resolution_destination is not None:
                _write_verified_archive(resolution_destination, resolution.raw,
                                        resolution.original_file_sha256)
            _persist(session, loaded, result, destination, resolution, resolution_destination)
            session.flush()
    except Exception:
        if destination is not None:
            destination.unlink(missing_ok=True)
        if resolution_destination is not None:
            resolution_destination.unlink(missing_ok=True)
        if archive_directory_created and destination is not None:
            try:
                destination.parent.rmdir()
            except OSError:
                pass
        raise
    return ImportResult("IMPORTED", result.revision_number, result.actions,
                        result.canonical_content_sha256, destination)
