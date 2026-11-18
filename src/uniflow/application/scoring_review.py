"""Bounded scoring-only review persistence. No evidence or mastery writers.

The origin records a local GUI assertion, not authentication or independent proof.
All mutation occurs under a SQLite writer lock and a single transaction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timezone
from uuid import UUID

from sqlalchemy import select, text
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1
from uniflow.application.scoring_contract_v1 import score_result

from uniflow.application.learning_chat_exchange import preview_scoring_bytes, build_scoring_preview
from uniflow.infrastructure.database.engine import get_engine, create_session_factory
from uniflow.infrastructure.database.models import (
    LearningHandoff, LearningSession, DerivedArtifact, ScoringResultSnapshot, ScoringReviewEvent,
)

STATUSES = ("PENDING_HUMAN_REVIEW", "ACCEPTED", "DEFERRED", "REJECTED")
ORIGIN = "LOCAL_GUI_CONFIRMATION"


@dataclass(frozen=True)
class ReviewState:
    result_sha256: str
    version: int
    decisions: dict[str, str]
    reason: str
    history: tuple[dict, ...]


def _state(db, sha):
    events = list(db.scalars(select(ScoringReviewEvent).where(
        ScoringReviewEvent.result_sha256 == sha).order_by(ScoringReviewEvent.version)))
    last = events[-1] if events else None
    return ReviewState(sha, last.version if last else 0,
        json.loads(last.decisions_json) if last else {}, last.reason if last else "",
        tuple(dict(id=e.id, version=e.version, decisions=json.loads(e.decisions_json),
                   reason=e.reason, origin=e.origin, decided_at=e.decided_at.replace(tzinfo=timezone.utc).isoformat()) for e in events))


def read_review(config, sha, session_id, handoff_id):
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            snapshot = db.get(ScoringResultSnapshot, sha)
            if snapshot is not None and (snapshot.session_id, snapshot.handoff_id) != (session_id, handoff_id):
                raise ValueError("REVIEW_IDENTITY_MISMATCH")
            return _state(db, sha)
    finally:
        engine.dispose()


def list_reviews(config, session_id, handoff_id=None):
    """No writes on opening, including no pending snapshot inserts."""
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            query = select(ScoringResultSnapshot.sha256, ScoringResultSnapshot.handoff_id).where(ScoringResultSnapshot.session_id == session_id)
            if handoff_id is not None:
                query = query.where(ScoringResultSnapshot.handoff_id == handoff_id)
            snapshots = list(db.execute(query.order_by(ScoringResultSnapshot.created_at)))
            return tuple(s.sha256 for s in snapshots) if handoff_id else tuple((s.sha256, s.handoff_id) for s in snapshots)
    finally:
        engine.dispose()


def reopen_review(config, sha, session_id, handoff_id):
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            snapshot = db.get(ScoringResultSnapshot, sha)
            if snapshot is None or (snapshot.session_id, snapshot.handoff_id) != (session_id, handoff_id):
                raise ValueError("REVIEW_IDENTITY_MISMATCH")
            raw = snapshot.raw_bytes
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("REVIEW_SNAPSHOT_CORRUPT")
            # The immutable snapshot retains the scope validated at save time.
            # Historical reopening never authorizes a write to a superseded Handoff.
            if len(raw) > config.learning_result_max_raw_bytes:
                raise ValueError("SCORING_RESULT_TOO_LARGE")
            result = ScoringResultV1.model_validate_json(raw)
            score = score_result(result, session_id=snapshot.session_id, handoff_id=snapshot.handoff_id,
                handoff_artifact_sha256=snapshot.handoff_sha256,
                allowed_skill_ids=frozenset(json.loads(snapshot.allowed_skill_ids_json)))
            return build_scoring_preview(result, score, snapshot.handoff_revision,
                json.loads(snapshot.allowed_skill_ids_json), raw)
    finally:
        engine.dispose()


def save_review(config, raw: bytes, *, session_id: str, handoff_id: str,
                handoff_revision: int, result_sha256: str, decisions: dict[str, str],
                reason: str, expected_version: int, request_id: str, confirmed: bool):
    if confirmed is not True:
        raise ValueError("REVIEW_EXPLICIT_CONFIRMATION_REQUIRED")
    UUID(request_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("REVIEW_VERSION_INVALID")
    if not isinstance(reason, str) or len(reason) > 4000:
        raise ValueError("REVIEW_REASON_INVALID")
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != result_sha256:
        raise ValueError("REVIEW_RESULT_MISMATCH")
    preview = preview_scoring_bytes(config, raw)
    result = preview.result
    if (result.session_id, result.handoff_id, preview.handoff_revision) != (session_id, handoff_id, handoff_revision):
        raise ValueError("REVIEW_IDENTITY_MISMATCH")
    # Include the full validated Handoff scope; missing observations stay pending.
    scores = preview.score.skill_session_scores
    scope = set(preview.allowed_skill_ids)
    if not isinstance(decisions, dict) or set(decisions) - scope:
        raise ValueError("REVIEW_SKILL_INVALID")
    normalized = {sid: decisions.get(sid, STATUSES[0]) for sid in sorted(scope)}
    if any(status not in STATUSES for status in normalized.values()):
        raise ValueError("REVIEW_STATUS_INVALID")
    if any(status == "ACCEPTED" and scores.get(sid) is None for sid, status in normalized.items()):
        raise ValueError("REVIEW_SKILL_UNASSESSED")
    if not normalized or all(s == STATUSES[0] for s in normalized.values()):
        raise ValueError("REVIEW_NO_EXPLICIT_DECISION")
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            # Serializes even independent processes; no read-then-write race.
            db.execute(text("BEGIN IMMEDIATE"))
            session = db.get(LearningSession, session_id)
            handoff = db.get(LearningHandoff, handoff_id)
            artifact = db.get(DerivedArtifact, handoff.artifact_id) if handoff else None
            if (session is None or session.current_handoff_id != handoff_id or handoff is None
                    or handoff.learning_session_id != session_id or handoff.revision != handoff_revision
                    or artifact is None or artifact.sha256 != result.handoff_artifact_sha256):
                raise ValueError("REVIEW_IDENTITY_MISMATCH")
            current = _state(db, result_sha256)
            replay = db.get(ScoringReviewEvent, request_id)
            if replay:
                if (replay.result_sha256, replay.decisions_json, replay.reason) != (result_sha256, payload, reason):
                    raise ValueError("REVIEW_REQUEST_CONFLICT")
                return current
            # Semantic replay prevents double-clicks even with different request IDs.
            if current.version and current.decisions == normalized and current.reason == reason:
                return current
            if current.version != expected_version:
                raise ValueError("REVIEW_VERSION_CONFLICT")
            snapshot = db.get(ScoringResultSnapshot, result_sha256)
            if snapshot is None:
                db.add(ScoringResultSnapshot(sha256=result_sha256, session_id=session_id,
                    handoff_id=handoff_id, handoff_revision=handoff_revision,
                    handoff_sha256=result.handoff_artifact_sha256,
                    contract_version=result.contract_version, raw_bytes=raw,
                    allowed_skill_ids_json=json.dumps(preview.allowed_skill_ids)))
                db.flush()
            elif snapshot.raw_bytes != raw:
                raise ValueError("REVIEW_SNAPSHOT_CORRUPT")
            db.add(ScoringReviewEvent(id=request_id, result_sha256=result_sha256,
                version=current.version + 1, decisions_json=payload, reason=reason, origin=ORIGIN))
            db.flush()
            state = _state(db, result_sha256)
            db.commit()
            return state
    finally:
        engine.dispose()
