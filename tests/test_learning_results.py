"""Offline 3F1 result and Evidence boundary tests."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from uniflow.application.initializer import initialize_project
from uniflow.application.learning_handoffs import create_learning_session, generate_handoff
from uniflow.application.learning_results import import_learning_result, learning_result_info, start_learning_session
from uniflow.domain.enums import InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, DerivedArtifact, Evidence, LearningHandoff, LearningResult,
    LearningSession, MasteryState, Semester, Skill, SkillDependency,
)
from uniflow.schemas.learning_result_v2 import LearningResultV2


def setup(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="SYN", name="Synthetic", state=SemesterState.ACTIVE, timezone="UTC")
        course = Course(stable_id="C", name="Course", semester=semester,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
        db.flush()
        db.add_all([Skill(id="TARGET", name="Target", kind="CONCEPT"),
                    Skill(id="PREREQ", name="Prereq", kind="CONCEPT"),
                    Skill(id="OTHER", name="Other", kind="CONCEPT")])
        db.flush()
        db.add_all([CourseSkill(course_id=course.id, skill_id="TARGET"),
                    SkillDependency(prerequisite_skill_id="PREREQ", dependent_skill_id="TARGET")])
        course_id = course.id
    sid = create_learning_session(cfg, course_id, "Learn", ["TARGET"])
    handoff = generate_handoff(cfg, sid)
    return cfg, factory, sid, course_id, handoff


def payload(sid, course_id, handoff):
    return {"schema_version": "2.0", "protocol_version": "subject-learning-v1",
        "result_id": "result-one", "session_id": sid, "handoff_id": handoff.handoff_id,
        "course_id": course_id, "handoff_artifact_sha256": handoff.artifact_sha256,
        "completed_at": "2026-10-13T10:00:00+02:00", "session_outcome": "COMPLETED",
        "skill_evidence": [
            {"skill_id": "TARGET", "recall": 0.0, "application": 0.8, "transfer": None,
             "independent_solution": True, "suggested_mastery": 0.9, "notes": "Observed work"},
            {"skill_id": "PREREQ", "independent_solution": False}],
        "new_gaps": ["Unknown topic in text"], "errors": ["Mistake"],
        "recommended_next_actions": ["https://example.invalid/do-not-fetch"]}


def write(tmp_path, value, name="result.json", pretty=False):
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2 if pretty else None), encoding="utf-8")
    return path


def test_schema_bounds_and_observations(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    p = payload(sid, course_id, handoff)
    assert LearningResultV2.model_validate(p).skill_evidence[0].recall == 0.0
    p["session_outcome"] = "PARTIAL"
    p["skill_evidence"] = []
    assert LearningResultV2.model_validate(p).skill_evidence == []
    p["session_outcome"] = "COMPLETED"
    with pytest.raises(ValueError, match="RESULT_COMPLETED_WITHOUT_EVIDENCE"):
        LearningResultV2.model_validate(p)
    p = payload(sid, course_id, handoff)
    p["skill_evidence"] = [{"skill_id": "TARGET", "suggested_mastery": 0.8}]
    with pytest.raises(ValueError, match="RESULT_UNOBSERVED_SKILL"):
        LearningResultV2.model_validate(p)
    p["skill_evidence"] = [{"skill_id": "TARGET", "recall": 0.4}] * 2
    with pytest.raises(ValueError, match="RESULT_DUPLICATE_SKILL"):
        LearningResultV2.model_validate(p)
    for bad in (float("nan"), float("inf"), -0.1, 1.1, "0.5"):
        p["skill_evidence"] = [{"skill_id": "TARGET", "recall": bad}]
        with pytest.raises(ValueError):
            LearningResultV2.model_validate(p)
    p = payload(sid, course_id, handoff)
    p["completed_at"] = "2026-10-13T10:00:00"
    with pytest.raises(ValueError):
        LearningResultV2.model_validate(p)
    p = payload(sid, course_id, handoff)
    p["unexpected"] = True
    with pytest.raises(ValueError):
        LearningResultV2.model_validate(p)


def test_import_evidence_idempotency_and_conflict(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    assert start_learning_session(cfg, sid) == "STARTED"
    assert start_learning_session(cfg, sid) == "ALREADY_STARTED"
    p = payload(sid, course_id, handoff)
    first_file = write(tmp_path, p)
    first = import_learning_result(cfg, first_file)
    assert first.status == "IMPORTED" and first.evidence_count == 2
    assert import_learning_result(cfg, write(tmp_path, p, "pretty.json", pretty=True)).status == "ALREADY_IMPORTED"
    info = learning_result_info(cfg, "result-one")
    assert info["raw_input_sha256"] == hashlib.sha256(first_file.read_bytes()).hexdigest()
    assert info["artifact_sha256"] == first.artifact_sha256
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED
        result = db.get(LearningResult, "result-one")
        artifact = db.get(DerivedArtifact, result.artifact_id)
        assert artifact.kind == "LEARNING_RESULT_JSON"
        assert hashlib.sha256(Path(artifact.stored_path).read_bytes()).hexdigest() == first.artifact_sha256
        evidence = list(db.scalars(select(Evidence).where(Evidence.learning_result_id == result.id)))
        assert len(evidence) == 2
        assert all(e.learning_session_id == sid and e.evidence_type.value == "LEARNING_RESULT" for e in evidence)
        target = next(e for e in evidence if e.skill_id == "TARGET")
        values = json.loads(target.value_json)
        assert values["recall"] == 0.0 and values["transfer"] is None
        assert values["suggested_mastery"] == 0.9
        assert db.scalar(select(MasteryState)) is None
        assert len(list(db.scalars(select(Skill)))) == 3
    p["errors"] = ["Different"]
    with pytest.raises(ValueError, match="RESULT_CONFLICT"):
        import_learning_result(cfg, write(tmp_path, p, "conflict.json"))


def test_identity_invalid_state_and_partial(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    p = payload(sid, course_id, handoff)
    path = write(tmp_path, p)
    with pytest.raises(ValueError, match="INVALID_SESSION_STATE"):
        import_learning_result(cfg, path)
    start_learning_session(cfg, sid)
    for field, value, code in (
        ("handoff_id", "wrong", "RESULT_HANDOFF_MISMATCH"),
        ("course_id", "wrong", "RESULT_COURSE_MISMATCH"),
        ("handoff_artifact_sha256", "0" * 64, "RESULT_HANDOFF_HASH_MISMATCH"),
    ):
        bad = payload(sid, course_id, handoff)
        bad[field] = value
        with pytest.raises(ValueError, match=code):
            import_learning_result(cfg, write(tmp_path, bad))
    bad = payload(sid, course_id, handoff)
    bad["skill_evidence"] = [{"skill_id": "OTHER", "recall": 0.3}]
    with pytest.raises(ValueError, match="RESULT_SKILL_NOT_ALLOWED"):
        import_learning_result(cfg, write(tmp_path, bad))
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.IN_PROGRESS
        assert db.scalar(select(LearningResult)) is None
    partial = payload(sid, course_id, handoff)
    partial["session_outcome"] = "PARTIAL"
    partial["skill_evidence"] = []
    assert import_learning_result(cfg, write(tmp_path, partial)).status == "IMPORTED"
    with factory() as db:
        assert db.get(LearningSession, sid).state == LearningSessionState.VALIDATED
        assert db.scalar(select(Evidence)) is None
        assert db.scalar(select(MasteryState)) is None


def test_populated_0011_upgrade_preserves_learning_history(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "old.sqlite3"
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(alembic, "0011_learning_handoff_generation")
    engine = get_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES ('s','S','Synthetic','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO courses (id,semester_id,stable_id,name,kind,participation,unknowns_json,information_status,created_at,updated_at) VALUES ('c','s','C','Course','MODULE','UNKNOWN','[]','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO skills (id,name,kind,status,created_at,updated_at) VALUES ('k','Skill','CONCEPT','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO learning_sessions (id,course_id,goal,state,created_at,updated_at) VALUES ('ls','c','Goal','HANDOFF_CREATED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO derived_artifacts (id,sha256,kind,stored_path,size_bytes,created_at) VALUES ('a',:digest,'LEARNING_HANDOFF_JSON','synthetic',1,CURRENT_TIMESTAMP)"), {"digest": "0" * 64})
        conn.execute(text("INSERT INTO learning_handoffs (id,learning_session_id,revision,artifact_id,schema_version,protocol_version,context_fingerprint_sha256,generated_at) VALUES ('h','ls',1,'a','2.0','subject-learning-v1',:digest,CURRENT_TIMESTAMP)"), {"digest": "0" * 64})
        conn.execute(text("UPDATE learning_sessions SET current_handoff_id='h' WHERE id='ls'"))
        conn.execute(text("INSERT INTO evidence (id,skill_id,learning_session_id,evidence_type,observed_at,created_at,updated_at) VALUES ('e','k','ls','LEARNING_RESULT',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO mastery_states (id,skill_id,value,calculated_at,calculation_method,evidence_id,learning_session_id,created_at,updated_at) VALUES ('m','k',0.25,CURRENT_TIMESTAMP,'synthetic','e','ls',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    engine.dispose()
    command.upgrade(alembic, "0012_learning_result_import")
    with get_engine(path).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0012_learning_result_import"
        assert conn.execute(text("SELECT COUNT(*) FROM learning_results")).scalar_one() == 0
        assert conn.execute(text("SELECT current_handoff_id FROM learning_sessions WHERE id='ls'")).scalar_one() == "h"
        assert conn.execute(text("SELECT learning_result_id FROM evidence WHERE id='e'")).scalar_one_or_none() is None
        assert conn.execute(text("SELECT value FROM mastery_states WHERE id='m'")).scalar_one() == 0.25
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []


def test_stale_handoff_filename_and_file_bounds(tmp_path):
    cfg, factory, sid, course_id, first = setup(tmp_path)
    with factory.begin() as db:
        db.add(MasteryState(skill_id="PREREQ", value=0.4, calculation_method="synthetic"))
    second = generate_handoff(cfg, sid)
    assert second.handoff_id != first.handoff_id
    start_learning_session(cfg, sid)
    stale = payload(sid, course_id, first)
    with pytest.raises(ValueError, match="RESULT_HANDOFF_MISMATCH"):
        import_learning_result(cfg, write(tmp_path, stale))
    current = payload(sid, course_id, second)
    bad_name = f"{sid}--{first.handoff_id}.json"
    with pytest.raises(ValueError, match="RESULT_FILENAME_MISMATCH"):
        import_learning_result(cfg, write(tmp_path, current, bad_name))
    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="RESULT_INVALID_JSON"):
        import_learning_result(cfg, invalid_utf8)
    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (cfg.learning_result_max_raw_bytes + 1))
    with pytest.raises(ValueError, match="RESULT_TOO_LARGE"):
        import_learning_result(cfg, huge)
    duplicate_key = tmp_path / "duplicates.json"
    duplicate_key.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="RESULT_DUPLICATE_JSON_KEY"):
        import_learning_result(cfg, duplicate_key)
    assert import_learning_result(cfg, write(tmp_path, current, "renamed.json")).status == "IMPORTED"


def test_concurrent_duplicate_import_is_single_result(tmp_path):
    cfg, factory, sid, course_id, handoff = setup(tmp_path)
    start_learning_session(cfg, sid)
    result_file = write(tmp_path, payload(sid, course_id, handoff))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: import_learning_result(cfg, result_file).status, range(2)))
    assert sorted(outcomes) == ["ALREADY_IMPORTED", "IMPORTED"]
    with factory() as db:
        assert len(list(db.scalars(select(LearningResult)))) == 1
        assert len(list(db.scalars(select(Evidence)))) == 2
        assert db.scalar(select(MasteryState)) is None
