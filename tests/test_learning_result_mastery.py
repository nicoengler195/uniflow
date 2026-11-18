"""Offline policy, provenance, lifecycle and migration checks for 3F2."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from test_learning_results import setup, payload, write
from uniflow.application.learning_handoffs import create_learning_session, generate_handoff
from uniflow.application.learning_result_mastery import POLICY, ResultEvidenceV1, _score, apply_learning_result_mastery
from uniflow.application.learning_results import import_learning_result, learning_result_info, start_learning_session
from uniflow.domain.enums import LearningSessionState
from uniflow.infrastructure.database.engine import get_engine
from uniflow.infrastructure.database.models import (
    DerivedArtifact, Evidence, LearningResult, LearningSession, MasteryState,
)
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
from uniflow.application.learning_handoffs import export_handoff


@pytest.mark.parametrize("scores,expected", [
    ({"recall": .8}, .8), ({"application": .4}, .4), ({"transfer": .2}, .2),
    ({"recall": .9, "application": .6, "transfer": .3}, .6),
    ({"recall": .8, "transfer": .4}, .6),
    ({"recall": 0.0, "application": .5}, .25),
    ({}, None),
])
def test_policy_only_observed_numeric_dimensions(scores, expected):
    base = {"schema_version": "learning-result-evidence-v1", "recall": None,
        "application": None, "transfer": None, "independent_solution": True,
        "suggested_mastery": .99}
    base.update(scores)
    assert _score(ResultEvidenceV1.model_validate(base)) == expected
    base["suggested_mastery"] = .01
    base["independent_solution"] = False
    assert _score(ResultEvidenceV1.model_validate(base)) == expected


def imported(tmp_path, observations=None, outcome="COMPLETED"):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    start_learning_session(cfg, sid)
    result = payload(sid, course_id, handoff)
    if observations is not None:
        result["skill_evidence"] = observations
    result["session_outcome"] = outcome
    import_learning_result(cfg, write(tmp_path, result))
    return cfg, factory, sid, course_id, handoff, result


def test_closed_loop_lineage_and_idempotency(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    with factory.begin() as db:
        prior = MasteryState(skill_id="TARGET", value=.3, calculation_method="manual_phase1_placeholder")
        db.add(prior)
        db.flush()
        prior_id = prior.id
    start_learning_session(cfg, sid)
    p = payload(sid, course_id, handoff)
    p["skill_evidence"][0].update(recall=.8, application=.6, transfer=None,
                                   suggested_mastery=.95, independent_solution=False)
    p["skill_evidence"][1] = {"skill_id": "PREREQ", "independent_solution": True}
    import_learning_result(cfg, write(tmp_path, p))
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED
    first = apply_learning_result_mastery(cfg, "result-one")
    assert first.status == "APPLIED" and first.skipped_skill_ids == ("PREREQ",)
    assert len(first.created_mastery_state_ids) == 1
    with factory() as db:
        state = db.get(MasteryState, first.created_mastery_state_ids[0])
        assert state.skill_id == "TARGET" and state.value == .7
        assert state.previous_mastery_state_id == prior_id
        assert state.calculation_method == POLICY and state.learning_session_id == sid
        evidence = db.get(Evidence, state.evidence_id)
        assert evidence.learning_result_id == "result-one"
        assert db.get(LearningResult, "result-one").processing_status == "MASTERY_APPLIED"
        assert db.get(LearningSession, sid).state == LearningSessionState.COMPLETED
        assert db.get(MasteryState, prior_id).value == .3
    info = learning_result_info(cfg, "result-one")
    assert info["mastery_state_ids"] == list(first.created_mastery_state_ids)
    assert info["skipped_skill_ids"] == ["PREREQ"]
    second = apply_learning_result_mastery(cfg, "result-one")
    assert second.status == "ALREADY_APPLIED"
    with factory() as db:
        assert len(list(db.scalars(select(MasteryState)))) == 2
    new_sid = create_learning_session(cfg, course_id, "Next session", ["TARGET"])
    generate_handoff(cfg, new_sid)
    new_body = LearningHandoffV2.model_validate_json(export_handoff(cfg, new_sid).read_bytes())
    snapshot = next(item for item in new_body.mastery_snapshot if item.skill_id == "TARGET")
    assert snapshot.mastery_state_id == first.created_mastery_state_ids[0]
    assert snapshot.value == .7 and snapshot.calculation_method == POLICY
    assert snapshot.evidence_id == state.evidence_id


@pytest.mark.parametrize("observations,outcome,skipped", [
    ([], "PARTIAL", ()),
    ([{"skill_id": "TARGET", "independent_solution": False}], "COMPLETED", ("TARGET",)),
])
def test_zero_numeric_still_completes(tmp_path, observations, outcome, skipped):
    cfg, factory, sid, _, _, _ = imported(tmp_path, observations, outcome)
    result = apply_learning_result_mastery(cfg, "result-one")
    assert result.status == "APPLIED" and result.created_mastery_state_ids == ()
    assert result.skipped_skill_ids == skipped
    with factory() as db:
        assert db.scalar(select(MasteryState)) is None
        assert db.get(LearningSession, sid).state == LearningSessionState.COMPLETED
        assert db.get(LearningResult, "result-one").processing_status == "MASTERY_APPLIED"


@pytest.mark.parametrize("change", [
    lambda e: setattr(e, "notes", "changed"),
    lambda e: setattr(e, "value_json", e.value_json.replace('0.8', '0.7')),
    lambda e: setattr(e, "evidence_type", "OTHER"),
    lambda e: setattr(e, "learning_session_id", None),
    lambda e: setattr(e, "value_json", '{"recall":0.8}'),
    lambda e: setattr(e, "value_json", e.value_json[:-1] + ',"unknown":1}'),
])
def test_evidence_mutation_rejected_atomically(tmp_path, change):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    with factory.begin() as db:
        e = db.scalar(select(Evidence).where(Evidence.skill_id == "TARGET"))
        change(e)
    with pytest.raises(ValueError, match="RESULT_EVIDENCE_INVALID"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory() as db:
        assert db.scalar(select(MasteryState)) is None
        assert db.get(LearningResult, "result-one").processing_status == "VALIDATED"
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED


@pytest.mark.parametrize("change", [
    lambda a: setattr(a, "kind", "OTHER"),
    lambda a: setattr(a, "size_bytes", a.size_bytes + 1),
    lambda a: Path(a.stored_path).write_bytes(b"corrupted"),
])
def test_result_artifact_mutation_rejected(tmp_path, change):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    with factory.begin() as db:
        a = db.get(DerivedArtifact, db.get(LearningResult, "result-one").artifact_id)
        change(a)
    with pytest.raises(ValueError, match="RESULT_ARTIFACT_CONFLICT"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory() as db:
        assert db.scalar(select(MasteryState)) is None
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED


def test_concurrent_apply_and_unique_index(tmp_path):
    cfg, factory, _, _, _, _ = imported(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: apply_learning_result_mastery(cfg, "result-one").status, range(2)))
    assert sorted(outcomes) == ["ALREADY_APPLIED", "APPLIED"]
    with factory() as db:
        state = db.scalar(select(MasteryState))
        assert state is not None
        evidence_id = state.evidence_id
    with pytest.raises(IntegrityError):
        with factory.begin() as db:
            db.add(MasteryState(skill_id="TARGET", value=.5, calculation_method=POLICY,
                evidence_id=evidence_id))
    with factory.begin() as db:
        db.add(MasteryState(skill_id="TARGET", value=.5, calculation_method="future-policy-v2",
            evidence_id=evidence_id))


def test_populated_0012_upgrade(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "upgrade.sqlite3"
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(alembic, "0012_learning_result_import")
    with get_engine(path).begin() as conn:
        conn.execute(text("INSERT INTO skills (id,name,kind,status,created_at,updated_at) VALUES ('k','Skill','CONCEPT','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO learning_sessions (id,goal,state,created_at,updated_at) VALUES ('s','Goal','VALIDATED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO derived_artifacts (id,sha256,kind,stored_path,size_bytes,created_at) VALUES ('a',:hash,'LEARNING_RESULT_JSON','synthetic',1,CURRENT_TIMESTAMP)"), {"hash": "0"*64})
        conn.execute(text("INSERT INTO learning_handoffs (id,learning_session_id,revision,artifact_id,schema_version,protocol_version,context_fingerprint_sha256,generated_at) VALUES ('h','s',1,'a','2.0','subject-learning-v1',:hash,CURRENT_TIMESTAMP)"), {"hash": "0"*64})
        conn.execute(text("INSERT INTO learning_results (id,learning_session_id,learning_handoff_id,artifact_id,schema_version,protocol_version,raw_input_sha256,completed_at,received_at,session_outcome,processing_status) VALUES ('r','s','h','a','2.0','subject-learning-v1',:hash,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'PARTIAL','VALIDATED')"), {"hash": "0"*64})
        conn.execute(text("INSERT INTO evidence (id,skill_id,learning_session_id,learning_result_id,evidence_type,observed_at,created_at,updated_at) VALUES ('e','k','s','r','LEARNING_RESULT',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO mastery_states (id,skill_id,value,calculated_at,calculation_method,evidence_id,created_at,updated_at) VALUES ('m','k',0.3,CURRENT_TIMESTAMP,'legacy','e',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(alembic, "head")
    with get_engine(path).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert conn.execute(text("SELECT previous_mastery_state_id FROM mastery_states WHERE id='m'")).scalar_one_or_none() is None
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


@pytest.mark.parametrize("prior_value", [.1, .9])
def test_prior_value_not_blended(tmp_path, prior_value):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    with factory.begin() as db:
        db.add(MasteryState(id="prior", skill_id="TARGET", value=prior_value,
            calculation_method="manual_phase1_placeholder"))
    start_learning_session(cfg, sid)
    p = payload(sid, course_id, handoff)
    p["skill_evidence"] = [{"skill_id": "TARGET", "recall": .8, "application": .6}]
    import_learning_result(cfg, write(tmp_path, p))
    result = apply_learning_result_mastery(cfg, "result-one")
    with factory() as db:
        state = db.get(MasteryState, result.created_mastery_state_ids[0])
        assert state.value == .7 and state.previous_mastery_state_id == "prior"


def test_latest_prior_uses_timestamp_then_id(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory.begin() as db:
        db.add_all([
            MasteryState(id="old", skill_id="TARGET", value=.1,
                calculated_at=t - timedelta(days=1), calculation_method="legacy"),
            MasteryState(id="a", skill_id="TARGET", value=.2,
                calculated_at=t, calculation_method="legacy"),
            MasteryState(id="z", skill_id="TARGET", value=.9,
                calculated_at=t, calculation_method="legacy"),
            MasteryState(id="other", skill_id="PREREQ", value=.5,
                calculated_at=t + timedelta(days=1), calculation_method="legacy"),
        ])
    start_learning_session(cfg, sid)
    p = payload(sid, course_id, handoff)
    p["skill_evidence"] = [{"skill_id": "TARGET", "recall": .8}]
    import_learning_result(cfg, write(tmp_path, p))
    result = apply_learning_result_mastery(cfg, "result-one")
    with factory() as db:
        state = db.get(MasteryState, result.created_mastery_state_ids[0])
        assert state.previous_mastery_state_id == "z"
        assert state.value == .8


def test_missing_evidence_and_wrong_state(tmp_path):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    with factory.begin() as db:
        db.delete(db.scalar(select(Evidence).where(Evidence.skill_id == "TARGET")))
    with pytest.raises(ValueError, match="RESULT_EVIDENCE_INVALID"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory.begin() as db:
        db.get(LearningResult, "result-one").processing_status = "OTHER"
    with pytest.raises(ValueError, match="RESULT_EVIDENCE_INVALID"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED


def test_metadata_mismatch_and_retry(tmp_path):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    with factory.begin() as db:
        db.get(LearningResult, "result-one").session_outcome = "PARTIAL"
    with pytest.raises(ValueError, match="RESULT_ARTIFACT_CONFLICT"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory.begin() as db:
        db.get(LearningResult, "result-one").session_outcome = "COMPLETED"
    assert apply_learning_result_mastery(cfg, "result-one").status == "APPLIED"
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.COMPLETED


def test_invalid_lifecycle_states(tmp_path):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    with factory.begin() as db:
        db.get(LearningResult, "result-one").processing_status = "OTHER"
    with pytest.raises(ValueError, match="RESULT_PROCESSING_STATE_INVALID"):
        apply_learning_result_mastery(cfg, "result-one")
    with factory.begin() as db:
        db.get(LearningResult, "result-one").processing_status = "VALIDATED"
        db.get(LearningSession, sid).state = LearningSessionState.IN_PROGRESS
    with pytest.raises(ValueError, match="INVALID_SESSION_STATE"):
        apply_learning_result_mastery(cfg, "result-one")


def test_persistence_failure_rolls_back_and_retries(tmp_path):
    cfg, factory, sid, _, _, _ = imported(tmp_path)
    def fail_insert(_mapper, _connection, _target):
        raise ValueError("synthetic persistence failure")
    event.listen(MasteryState, "before_insert", fail_insert)
    try:
        with pytest.raises(ValueError, match="synthetic persistence failure"):
            apply_learning_result_mastery(cfg, "result-one")
    finally:
        event.remove(MasteryState, "before_insert", fail_insert)
    with factory() as db:
        assert db.scalar(select(MasteryState)) is None
        assert db.get(LearningResult, "result-one").processing_status == "VALIDATED"
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED
    assert apply_learning_result_mastery(cfg, "result-one").status == "APPLIED"
