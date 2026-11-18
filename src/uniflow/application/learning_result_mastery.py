"""Apply the local, versioned observed-dimensions policy to a validated result."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from uniflow.application.learning_results import KIND, _handoff, _result_path
from uniflow.domain.enums import EvidenceType, LearningSessionState
from uniflow.infrastructure.config.settings import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    DerivedArtifact, Evidence, LearningResult, LearningSession, MasteryState, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock
from uniflow.schemas.common import StrictModel
from uniflow.schemas.learning_result_v2 import LearningResultV2

POLICY = "learning-result-observed-dimensions-v1"
DIMENSIONS = ("recall", "application", "transfer")


class ResultEvidenceV1(StrictModel):
    schema_version: Literal["learning-result-evidence-v1"]
    recall: StrictFloat | None = Field(ge=0, le=1, allow_inf_nan=False)
    application: StrictFloat | None = Field(ge=0, le=1, allow_inf_nan=False)
    transfer: StrictFloat | None = Field(ge=0, le=1, allow_inf_nan=False)
    independent_solution: StrictBool | None
    suggested_mastery: StrictFloat | None = Field(ge=0, le=1, allow_inf_nan=False)


@dataclass(frozen=True)
class ApplyOutcome:
    status: str
    result_id: str
    session_id: str
    created_mastery_state_ids: tuple[str, ...]
    skipped_skill_ids: tuple[str, ...]


def _fail(code: str):
    raise ValueError(code)


def _same_timestamp(stored: datetime, expected: datetime) -> bool:
    # SQLite drops the offset, retaining the original wall-clock components.
    if stored.tzinfo is None:
        return stored == expected.replace(tzinfo=None)
    return stored.astimezone(timezone.utc) == expected.astimezone(timezone.utc)


def _strict_json(raw: str) -> ResultEvidenceV1:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("RESULT_EVIDENCE_INVALID")
            result[key] = value
        return result
    try:
        data = json.loads(raw, object_pairs_hook=unique_pairs,
            parse_constant=lambda _: _fail("RESULT_EVIDENCE_INVALID"))
        return ResultEvidenceV1.model_validate(data)
    except (TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("RESULT_EVIDENCE_INVALID") from exc


def _score(payload: ResultEvidenceV1) -> float | None:
    values = [Decimal(str(getattr(payload, name))) for name in DIMENSIONS
              if getattr(payload, name) is not None]
    if not values:
        return None
    return float(sum(values, Decimal(0)) / Decimal(len(values)))


def _validated(db, config: AppConfig, result_id: str):
    row = db.get(LearningResult, result_id)
    if row is None:
        _fail("RESULT_NOT_FOUND")
    session = db.get(LearningSession, row.learning_session_id)
    if session is None:
        _fail("RESULT_HANDOFF_MISMATCH")
    if session.current_handoff_id != row.learning_handoff_id:
        _fail("RESULT_HANDOFF_MISMATCH")
    try:
        handoff, handoff_artifact, handoff_body = _handoff(db, config, session)
    except (ValueError, OSError) as exc:
        raise ValueError("RESULT_HANDOFF_MISMATCH") from exc
    if handoff.id != row.learning_handoff_id:
        _fail("RESULT_HANDOFF_MISMATCH")
    artifact = db.get(DerivedArtifact, row.artifact_id)
    if artifact is None or artifact.kind != KIND or Path(artifact.stored_path) != _result_path(config, artifact.sha256):
        _fail("RESULT_ARTIFACT_CONFLICT")
    path = Path(artifact.stored_path)
    try:
        if artifact.size_bytes > config.learning_result_max_json_bytes or path.stat().st_size != artifact.size_bytes:
            _fail("RESULT_ARTIFACT_CONFLICT")
        body = path.read_bytes()
    except OSError as exc:
        raise ValueError("RESULT_ARTIFACT_CONFLICT") from exc
    if len(body) != artifact.size_bytes or hashlib.sha256(body).hexdigest() != artifact.sha256:
        _fail("RESULT_ARTIFACT_CONFLICT")
    try:
        parsed = LearningResultV2.model_validate_json(body)
    except ValidationError as exc:
        raise ValueError("RESULT_ARTIFACT_CONFLICT") from exc
    if (parsed.result_id != row.id or parsed.session_id != session.id or
        parsed.handoff_id != handoff.id or parsed.course_id != session.course_id or
        parsed.handoff_artifact_sha256 != handoff_artifact.sha256 or
        parsed.schema_version != row.schema_version or parsed.protocol_version != row.protocol_version or
        parsed.protocol_version != handoff.protocol_version or parsed.session_outcome != row.session_outcome or
        not _same_timestamp(row.completed_at, parsed.completed_at)):
        _fail("RESULT_ARTIFACT_CONFLICT")
    if any(item.skill_id not in handoff_body.return_contract.allowed_skill_ids
           for item in parsed.skill_evidence):
        _fail("RESULT_HANDOFF_MISMATCH")
    evidence = list(db.scalars(select(Evidence).where(Evidence.learning_result_id == row.id)
        .order_by(Evidence.skill_id, Evidence.id)))
    expected = {item.skill_id: item for item in parsed.skill_evidence}
    if len(evidence) != len(expected):
        _fail("RESULT_EVIDENCE_INVALID")
    candidates = []
    for entry in evidence:
        item = expected.get(entry.skill_id)
        if (item is None or entry.learning_result_id != row.id or
            entry.learning_session_id != session.id or entry.evidence_type != EvidenceType.LEARNING_RESULT or
            not _same_timestamp(entry.observed_at, parsed.completed_at) or entry.notes != item.notes):
            _fail("RESULT_EVIDENCE_INVALID")
        payload = _strict_json(entry.value_json)
        if payload.model_dump() != {"schema_version": "learning-result-evidence-v1",
            "recall": item.recall, "application": item.application, "transfer": item.transfer,
            "independent_solution": item.independent_solution,
            "suggested_mastery": item.suggested_mastery}:
            _fail("RESULT_EVIDENCE_INVALID")
        candidates.append((entry, _score(payload)))
    return row, session, candidates


def _existing_states(db, row: LearningResult, candidates) -> tuple[str, ...]:
    states = list(db.scalars(select(MasteryState).join(Evidence, MasteryState.evidence_id == Evidence.id)
        .where(Evidence.learning_result_id == row.id, MasteryState.calculation_method == POLICY)
        .order_by(MasteryState.skill_id, MasteryState.id)))
    by_evidence = {state.evidence_id: state for state in states}
    numeric = {entry.id for entry, value in candidates if value is not None}
    if len(states) != len(numeric) or set(by_evidence) != numeric or any(
        state.skill_id != entry.skill_id or state.learning_session_id != row.learning_session_id or
        state.value != value for entry, value in candidates for state in states if state.evidence_id == entry.id
    ):
        _fail("MASTERY_PROVENANCE_CONFLICT")
    for state in states:
        if state.previous_mastery_state_id:
            previous = db.get(MasteryState, state.previous_mastery_state_id)
            if previous is None or previous.id == state.id or previous.skill_id != state.skill_id:
                _fail("MASTERY_PROVENANCE_CONFLICT")
    return tuple(state.id for state in states)


def preview_learning_result_mastery(config: AppConfig, result_id: str):
    """Read-only proposal using the exact checks and policy of the final application."""
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:
        row, session, candidates = _validated(db, config, result_id)
        if row.processing_status != "VALIDATED" or session.state != LearningSessionState.VALIDATED:
            _fail("INVALID_SESSION_STATE")
        return {entry.skill_id: value for entry, value in candidates}


def apply_learning_result_mastery(config: AppConfig, result_id: str) -> ApplyOutcome:
    factory = create_session_factory(get_engine(config.database_path))
    # Preflight catches most malformed input without occupying the writer lock.
    with factory() as db:
        _validated(db, config, result_id)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        try:
            with factory.begin() as db:
                row, session, candidates = _validated(db, config, result_id)
                skipped = tuple(entry.skill_id for entry, value in candidates if value is None)
                if row.processing_status == "MASTERY_APPLIED":
                    if session.state != LearningSessionState.COMPLETED:
                        _fail("MASTERY_PROVENANCE_CONFLICT")
                    ids = _existing_states(db, row, candidates)
                    return ApplyOutcome("ALREADY_APPLIED", row.id, session.id, ids, skipped)
                if row.processing_status != "VALIDATED":
                    _fail("RESULT_PROCESSING_STATE_INVALID")
                if session.state != LearningSessionState.VALIDATED:
                    _fail("INVALID_SESSION_STATE")
                _existing_states(db, row, [])
                calculated_at = utc_now()
                created = []
                for entry, value in candidates:
                    if value is None:
                        continue
                    previous = db.scalar(select(MasteryState).where(MasteryState.skill_id == entry.skill_id)
                        .order_by(MasteryState.calculated_at.desc(), MasteryState.id.desc()).limit(1))
                    if previous is not None and previous.skill_id != entry.skill_id:
                        _fail("MASTERY_PROVENANCE_CONFLICT")
                    state = MasteryState(skill_id=entry.skill_id, value=value,
                        calculated_at=calculated_at, calculation_method=POLICY, evidence_id=entry.id,
                        learning_session_id=session.id,
                        previous_mastery_state_id=previous.id if previous else None)
                    db.add(state)
                    db.flush()
                    created.append(state.id)
                row.processing_status = "MASTERY_APPLIED"
                session.state = LearningSessionState.COMPLETED
                return ApplyOutcome("APPLIED", row.id, session.id, tuple(created), skipped)
        except IntegrityError as exc:
            raise ValueError("MASTERY_PROVENANCE_CONFLICT") from exc
        except SQLAlchemyError as exc:
            raise ValueError("MASTERY_PERSISTENCE_FAILED") from exc
