"""Local, validated learning observations. Canonical mastery belongs to 3F2."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from uniflow.application.learning_handoffs import _verified_artifact as verified_handoff_artifact
from uniflow.domain.enums import EvidenceType, LearningSessionState
from uniflow.infrastructure.config.settings import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    DerivedArtifact, Evidence, LearningHandoff, LearningResult, LearningSession, MasteryState, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
from uniflow.schemas.learning_result_v2 import LearningResultV2

KIND = "LEARNING_RESULT_JSON"
RESERVED_NAME = re.compile(r"^([0-9a-fA-F-]{36})--([0-9a-fA-F-]{36})\.json$")


def _fail(code: str):
    raise ValueError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _db(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))()


def _handoff(db, config: AppConfig, row: LearningSession):
    if not row.current_handoff_id:
        _fail("RESULT_HANDOFF_MISSING")
    handoff = db.get(LearningHandoff, row.current_handoff_id)
    if handoff is None or handoff.learning_session_id != row.id:
        _fail("RESULT_HANDOFF_MISMATCH")
    artifact = db.get(DerivedArtifact, handoff.artifact_id)
    if artifact is None:
        _fail("RESULT_HANDOFF_ARTIFACT_INVALID")
    try:
        body = verified_handoff_artifact(config, artifact)
        parsed = LearningHandoffV2.model_validate_json(body)
    except (ValueError, OSError, ValidationError) as exc:
        raise ValueError("RESULT_HANDOFF_ARTIFACT_INVALID") from exc
    contract = parsed.return_contract
    if (parsed.session_id != row.id or parsed.handoff_id != handoff.id or
        contract.session_id != row.id or contract.handoff_id != handoff.id or
        parsed.course.id != row.course_id or contract.course_id != row.course_id or
        parsed.protocol_version != handoff.protocol_version or
        contract.expected_result_schema_version != "2.0"):
        _fail("RESULT_HANDOFF_ARTIFACT_INVALID")
    return handoff, artifact, parsed


def _validate_context(result: LearningResultV2, row: LearningSession,
                      handoff: LearningHandoff, artifact: DerivedArtifact,
                      parsed: LearningHandoffV2) -> None:
    contract = parsed.return_contract
    if result.session_id != row.id or result.session_id != handoff.learning_session_id:
        _fail("RESULT_SESSION_MISMATCH")
    if result.handoff_id != row.current_handoff_id or result.handoff_id != handoff.id:
        _fail("RESULT_HANDOFF_MISMATCH")
    if result.course_id != row.course_id or result.course_id != contract.course_id:
        _fail("RESULT_COURSE_MISMATCH")
    if result.handoff_artifact_sha256 != artifact.sha256:
        _fail("RESULT_HANDOFF_HASH_MISMATCH")
    if result.schema_version != contract.expected_result_schema_version or result.protocol_version != parsed.protocol_version:
        _fail("RESULT_PROTOCOL_MISMATCH")
    allowed = set(contract.allowed_skill_ids)
    if any(item.skill_id not in allowed for item in result.skill_evidence):
        _fail("RESULT_SKILL_NOT_ALLOWED")


def start_learning_session(config: AppConfig, session_id: str) -> str:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db, db.begin():
            row = db.get(LearningSession, session_id)
            if row is None:
                _fail("UNKNOWN_LEARNING_SESSION")
            if row.state not in {LearningSessionState.HANDOFF_CREATED, LearningSessionState.IN_PROGRESS}:
                _fail("INVALID_SESSION_STATE")
            _handoff(db, config, row)
            if row.state == LearningSessionState.IN_PROGRESS:
                return "ALREADY_STARTED"
            row.state = LearningSessionState.IN_PROGRESS
            return "STARTED"


def _read_result(config: AppConfig, path: Path):
    with path.open("rb") as fh:
        raw = fh.read(config.learning_result_max_raw_bytes + 1)
    if len(raw) > config.learning_result_max_raw_bytes:
        _fail("RESULT_TOO_LARGE")
    try:
        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    _fail("RESULT_DUPLICATE_JSON_KEY")
                value[key] = item
            return value
        source = json.loads(raw.decode("utf-8"), parse_constant=lambda _: _fail("RESULT_INVALID_JSON"),
                            object_pairs_hook=unique_pairs)
        if not isinstance(source, dict):
            _fail("RESULT_INVALID_JSON")
        if source.get("status") == "REVIEW_DRAFT_NOT_IMPORTABLE" or source.get("contract_version") == "uniflow-scoring-v1":
            _fail("RESULT_SCORING_FORMAT_SELECTED")
        result = LearningResultV2.model_validate(source)
    except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("RESULT_INVALID_JSON") from exc
    body = _canonical(result.model_dump(mode="json"))
    if len(body) > config.learning_result_max_json_bytes:
        _fail("RESULT_TOO_LARGE")
    match = RESERVED_NAME.fullmatch(path.name)
    if match and (match.group(1) != result.session_id or match.group(2) != result.handoff_id):
        _fail("RESULT_FILENAME_MISMATCH")
    return result, raw, body


def _result_path(config: AppConfig, digest: str) -> Path:
    return config.derived_root / "sha256" / digest[:2] / digest / "learning-result.json"


def _existing_result(db, config: AppConfig, result: LearningResultV2, digest: str, handoff_id: str):
    by_id = db.get(LearningResult, result.result_id)
    by_handoff = db.scalar(select(LearningResult).where(LearningResult.learning_handoff_id == handoff_id))
    if by_id is not None or by_handoff is not None:
        if by_id is None or by_handoff is None or by_id.id != by_handoff.id:
            _fail("RESULT_CONFLICT")
        artifact = db.get(DerivedArtifact, by_id.artifact_id)
        if artifact is None or artifact.sha256 != digest or artifact.kind != KIND:
            _fail("RESULT_CONFLICT")
        stored = Path(artifact.stored_path)
        if stored != _result_path(config, digest):
            _fail("RESULT_ARTIFACT_CONFLICT")
        if (not stored.is_file() or artifact.size_bytes > config.learning_result_max_json_bytes or
            stored.stat().st_size != artifact.size_bytes or _sha(stored.read_bytes()) != digest):
            _fail("RESULT_ARTIFACT_CONFLICT")
        return by_id
    return None


@dataclass(frozen=True)
class ImportOutcome:
    status: str
    result_id: str
    artifact_sha256: str
    evidence_count: int


def import_learning_result(config: AppConfig, path: Path) -> ImportOutcome:
    result, raw, body = _read_result(config, Path(path))
    digest = _sha(body)
    raw_digest = _sha(raw)
    with _db(config) as db:
        reused_id = db.get(LearningResult, result.result_id)
        if reused_id is not None and (reused_id.learning_session_id != result.session_id or
                                       reused_id.learning_handoff_id != result.handoff_id):
            _fail("RESULT_CONFLICT")
        row = db.get(LearningSession, result.session_id)
        if row is None:
            _fail("RESULT_SESSION_MISMATCH")
        handoff, artifact, parsed = _handoff(db, config, row)
        _validate_context(result, row, handoff, artifact, parsed)
        expected = (row.current_handoff_id, artifact.sha256, row.state)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _db(config) as db:
            reused_id = db.get(LearningResult, result.result_id)
            if reused_id is not None and (reused_id.learning_session_id != result.session_id or
                                           reused_id.learning_handoff_id != result.handoff_id):
                _fail("RESULT_CONFLICT")
            row = db.get(LearningSession, result.session_id)
            if row is None or row.current_handoff_id != expected[0]:
                _fail("RESULT_CONTEXT_CHANGED")
            handoff, handoff_artifact, parsed = _handoff(db, config, row)
            if handoff_artifact.sha256 != expected[1]:
                _fail("RESULT_CONTEXT_CHANGED")
            _validate_context(result, row, handoff, handoff_artifact, parsed)
            existing = _existing_result(db, config, result, digest, handoff.id)
            if existing is not None:
                return ImportOutcome("ALREADY_IMPORTED", existing.id, digest, len(result.skill_evidence))
            if row.state != LearningSessionState.IN_PROGRESS:
                _fail("INVALID_SESSION_STATE")
            if expected[2] != LearningSessionState.IN_PROGRESS:
                _fail("RESULT_CONTEXT_CHANGED")
            db.commit()
            target = _result_path(config, digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            created = False
            temp = None
            try:
                if target.exists():
                    if _sha(target.read_bytes()) != digest:
                        _fail("RESULT_ARTIFACT_CONFLICT")
                else:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as fh:
                        fh.write(body)
                        temp = Path(fh.name)
                    try:
                        os.link(temp, target)
                        created = True
                    except FileExistsError:
                        if _sha(target.read_bytes()) != digest:
                            _fail("RESULT_ARTIFACT_CONFLICT")
                with db.begin():
                    stored_artifact = db.scalar(select(DerivedArtifact).where(DerivedArtifact.sha256 == digest))
                    if stored_artifact is None:
                        stored_artifact = DerivedArtifact(sha256=digest, kind=KIND,
                            stored_path=str(target), size_bytes=len(body))
                        db.add(stored_artifact)
                        db.flush()
                    elif (stored_artifact.kind != KIND or stored_artifact.stored_path != str(target)
                          or stored_artifact.size_bytes != len(body)):
                        _fail("RESULT_ARTIFACT_CONFLICT")
                    metadata = LearningResult(id=result.result_id, learning_session_id=row.id,
                        learning_handoff_id=handoff.id, artifact_id=stored_artifact.id,
                        schema_version=result.schema_version, protocol_version=result.protocol_version,
                        raw_input_sha256=raw_digest, completed_at=result.completed_at,
                        received_at=utc_now(), session_outcome=result.session_outcome,
                        processing_status="VALIDATED")
                    db.add(metadata)
                    db.flush()
                    for item in result.skill_evidence:
                        values = {"schema_version": "learning-result-evidence-v1",
                            "recall": item.recall, "application": item.application,
                            "transfer": item.transfer, "independent_solution": item.independent_solution,
                            "suggested_mastery": item.suggested_mastery}
                        db.add(Evidence(skill_id=item.skill_id, learning_session_id=row.id,
                            learning_result_id=metadata.id, evidence_type=EvidenceType.LEARNING_RESULT,
                            value_json=_canonical(values).decode("utf-8"), notes=item.notes,
                            observed_at=result.completed_at))
                    row.state = LearningSessionState.VALIDATED
                return ImportOutcome("IMPORTED", result.result_id, digest, len(result.skill_evidence))
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)


def learning_result_info(config: AppConfig, result_id: str) -> dict:
    with _db(config) as db:
        row = db.get(LearningResult, result_id)
        if row is None:
            _fail("RESULT_NOT_FOUND")
        artifact = db.get(DerivedArtifact, row.artifact_id)
        if artifact is None or artifact.kind != KIND or Path(artifact.stored_path) != _result_path(config, artifact.sha256):
            _fail("RESULT_ARTIFACT_CONFLICT")
        result_path = Path(artifact.stored_path)
        if (artifact.size_bytes > config.learning_result_max_json_bytes or not result_path.is_file() or
            result_path.stat().st_size != artifact.size_bytes):
            _fail("RESULT_ARTIFACT_CONFLICT")
        body = result_path.read_bytes()
        if len(body) != artifact.size_bytes or _sha(body) != artifact.sha256:
            _fail("RESULT_ARTIFACT_CONFLICT")
        parsed = LearningResultV2.model_validate_json(body)
        evidence = list(db.scalars(select(Evidence).where(Evidence.learning_result_id == row.id).order_by(Evidence.skill_id)))
        mastery = list(db.scalars(select(MasteryState).join(Evidence, MasteryState.evidence_id == Evidence.id)
            .where(Evidence.learning_result_id == row.id,
                   MasteryState.calculation_method == "learning-result-observed-dimensions-v1")
            .order_by(MasteryState.skill_id, MasteryState.id)))
        numeric_skills = {state.skill_id for state in mastery}
        return {"result_id": row.id, "session_id": row.learning_session_id,
            "handoff_id": row.learning_handoff_id,
            "course_id": db.get(LearningSession, row.learning_session_id).course_id,
            "schema_version": row.schema_version, "protocol_version": row.protocol_version,
            "completed_at": parsed.completed_at.isoformat(),
            "received_at": row.received_at.replace(tzinfo=timezone.utc).isoformat(),
            "session_outcome": row.session_outcome, "processing_status": row.processing_status,
            "artifact_sha256": artifact.sha256, "raw_input_sha256": row.raw_input_sha256,
            "observed_skill_ids": [item.skill_id for item in evidence],
            "evidence_ids": [item.id for item in evidence],
            "mastery_state_ids": [item.id for item in mastery],
            "skipped_skill_ids": [item.skill_id for item in evidence if row.processing_status == "MASTERY_APPLIED"
                                  and item.skill_id not in numeric_skills]}
