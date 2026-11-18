"""Explicit reviewed scoring mastery policy; separate from LearningResultV2."""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from uniflow.application.scoring_review import _state
from uniflow.application.scoring_contract_v1 import score_result
from uniflow.application.learning_results import _handoff
from uniflow.infrastructure.database.engine import get_engine, create_session_factory
from uniflow.infrastructure.database.models import (ScoringResultSnapshot, MasteryState,
    ScoringMasteryApplication, LearningSession, Skill, Evidence, utc_now)
from uniflow.domain.enums import EvidenceType
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1, ScoredTaskV1

POLICY = "scoring-result-reviewed-mastery-v1"


@dataclass(frozen=True)
class MasteryCandidate:
    skill_id: str
    decision: str
    source_points: str | None
    previous_value: float | None
    previous_state_id: str | None
    proposed_value: float | None
    exclusion_reason: str


@dataclass(frozen=True)
class ControlledMasteryPreview:
    result_sha256: str
    session_id: str
    handoff_id: str
    handoff_revision: int
    handoff_sha256: str
    review_version: int
    review_event_id: str | None
    skills: tuple[MasteryCandidate, ...]
    policy: str = POLICY


def _preview(db, config, sha, session_id, handoff_id):
    snapshot = db.get(ScoringResultSnapshot, sha)
    if snapshot is None or (snapshot.session_id, snapshot.handoff_id) != (session_id, handoff_id):
        raise ValueError("REVIEW_IDENTITY_MISMATCH")
    if hashlib.sha256(snapshot.raw_bytes).hexdigest() != sha:
        raise ValueError("REVIEW_SNAPSHOT_CORRUPT")
    session = db.get(LearningSession, session_id)
    if session is None or session.current_handoff_id != handoff_id:
        raise ValueError("MASTERY_HANDOFF_CHANGED")
    handoff, artifact, body = _handoff(db, config, session)
    scope = json.loads(snapshot.allowed_skill_ids_json)
    if (handoff.id != handoff_id or handoff.revision != snapshot.handoff_revision
            or artifact.sha256 != snapshot.handoff_sha256
            or set(scope) != set(body.return_contract.allowed_skill_ids)):
        raise ValueError("MASTERY_HANDOFF_CHANGED")
    result = ScoringResultV1.model_validate_json(snapshot.raw_bytes)
    score = score_result(result, session_id=session_id, handoff_id=handoff_id,
        handoff_artifact_sha256=artifact.sha256, allowed_skill_ids=frozenset(scope))
    state = _state(db, sha)
    skills = []
    for sid in sorted(scope):
        skill = db.get(Skill, sid)
        if skill is None or skill.status != "ACTIVE" or skill.canonical_skill_id is not None:
            raise ValueError("MASTERY_SKILL_IDENTITY_CHANGED")
        previous = db.scalar(select(MasteryState).where(MasteryState.skill_id == sid)
            .order_by(MasteryState.calculated_at.desc(), MasteryState.id.desc()).limit(1))
        points = score.skill_session_scores.get(sid)
        decision = state.decisions.get(sid, "PENDING_HUMAN_REVIEW")
        exams = [t for t in result.tasks if isinstance(t, ScoredTaskV1)
            and t.phase == "FINAL_EXAM" and any(m.skill_id == sid for m in t.skill_marks)]
        reason = ""
        if decision != "ACCEPTED":
            reason = "Nicht ausdrücklich akzeptiert: " + decision
        elif any(p.status != "COMPLETED" for p in result.phase_states):
            reason = "Beide Phasen müssen abgeschlossen sein"
        elif points is None or not points.is_finite() or not Decimal(0) <= points <= Decimal(100):
            reason = "Vollständige Skillbewertung fehlt"
        elif not exams:
            reason = "Bewertete Abschlussaufgabe fehlt"
        elif any(not t.independent_solution or t.assistance != "NONE" or t.hints for t in exams):
            reason = "Abschlussaufgabe ist assistiert oder nicht unabhängig"
        elif db.scalar(select(ScoringMasteryApplication.id).where(
                ScoringMasteryApplication.result_sha256 == sha, ScoringMasteryApplication.skill_id == sid)):
            reason = "Dieser Ergebnissnapshot wurde für den Skill bereits angewendet"
        skills.append(MasteryCandidate(sid, decision, str(points) if points is not None else None,
            previous.value if previous else None, previous.id if previous else None,
            float(points / Decimal(100)) if not reason else None, reason))
    return ControlledMasteryPreview(sha, session_id, handoff_id, handoff.revision, artifact.sha256,
        state.version, state.history[-1]['id'] if state.history else None, tuple(skills))


def preview_mastery(config, sha, session_id, handoff_id):
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            db.execute(text("BEGIN"))
            return _preview(db, config, sha, session_id, handoff_id)
    finally:
        engine.dispose()


def apply_mastery(config, preview, selected_skill_ids, *, confirmed=False):
    if confirmed is not True:
        raise ValueError("MASTERY_EXPLICIT_CONFIRMATION_REQUIRED")
    selected = tuple(selected_skill_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("MASTERY_SELECTION_INVALID")
    engine = get_engine(config.database_path)
    try:
        with create_session_factory(engine)() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            current = _preview(db, config, preview.result_sha256, preview.session_id, preview.handoff_id)
            if current != preview:
                raise ValueError("MASTERY_PREVIEW_STALE")
            candidates = {s.skill_id: s for s in current.skills}
            if any(sid not in candidates or candidates[sid].proposed_value is None for sid in selected):
                raise ValueError("MASTERY_SELECTION_INELIGIBLE")
            now = utc_now()
            ids = []
            for sid in selected:
                candidate = candidates[sid]
                evidence = Evidence(skill_id=sid, learning_session_id=current.session_id,
                    evidence_type=EvidenceType.ASSESSMENT_PERFORMANCE, observed_at=now,
                    value_json=json.dumps(dict(policy=POLICY, result_sha256=current.result_sha256,
                        review_event_id=current.review_event_id, score_points=candidate.source_points)),
                    notes="Explicit local GUI approval; S09 = NOT_VERIFIED")
                db.add(evidence)
                db.flush()
                mastery = MasteryState(skill_id=sid, value=candidate.proposed_value,
                    calculated_at=now, calculation_method=POLICY, evidence_id=evidence.id,
                    learning_session_id=current.session_id, previous_mastery_state_id=candidate.previous_state_id)
                db.add(mastery)
                db.flush()
                entry = ScoringMasteryApplication(result_sha256=current.result_sha256,
                    review_event_id=current.review_event_id, review_version=current.review_version,
                    session_id=current.session_id, handoff_id=current.handoff_id, skill_id=sid,
                    policy_id=POLICY, policy_version=1, score_points=candidate.source_points,
                    evidence_id=evidence.id, previous_state_id=candidate.previous_state_id,
                    new_state_id=mastery.id, applied_at=now, origin="LOCAL_GUI_CONFIRMATION")
                db.add(entry)
                db.flush()
                ids.append(entry.id)
            db.commit()
            return tuple(ids)
    except SQLAlchemyError as exc:
        raise ValueError("MASTERY_PERSISTENCE_FAILED") from exc
    finally:
        engine.dispose()


def preview_text(preview):
    lines = [f"Policy: {preview.policy} · Version 1", f"Session: {preview.session_id}",
        f"Handoff: {preview.handoff_id} · Revision: {preview.handoff_revision}",
        f"Ergebnis-SHA-256: {preview.result_sha256}", f"B3-Reviewversion: {preview.review_version}",
        "Quelle: unverändertes ScoringResultV1 / uniflow-scoring-v1",
        "Berechnung: neuer Mastery-Wert = vollständige Skillpunkte / 100.",
        "Der neue Wert ersetzt die aktuelle Messung und darf höher oder niedriger liegen.",
        "Lokale Leistungsmessregel; keine wissenschaftlich validierte Beherrschungswahrscheinlichkeit.",
        "Lokale Akzeptanz ist kein unabhängiger Fachnachweis. S09 = NOT_VERIFIED", ""]
    for skill in preview.skills:
        before = "UNKNOWN" if skill.previous_value is None else str(skill.previous_value)
        after = "nicht anwendbar" if skill.proposed_value is None else str(skill.proposed_value)
        lines.append(f"{skill.skill_id}: {skill.decision} · Punkte: {skill.source_points if skill.source_points is not None else 'Nicht bewertet'} / 100"
            f" · Mastery vorher: {before} → {after} · {skill.exclusion_reason or 'Anwendbar'}")
    return "\n".join(lines)
