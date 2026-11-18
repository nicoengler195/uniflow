"""Synthetic, isolated one-file export and read-only scoring preview tests."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from test_learning_results import setup
from uniflow.application.learning_chat_exchange import (
    export_learning_chat_package, preview_scoring_result,
)
from uniflow.application.learning_results import import_learning_result
from uniflow.application.learning_workflow import export_learning_chat
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.application.learning_handoffs import generate_handoff
from uniflow.infrastructure.database.models import (
    DerivedArtifact, Evidence, LearningHandoff, LearningResult, LearningSession, MasteryState, Skill,
)


def task(task_id, phase, fraction, skill_marks, difficulty="MEDIUM"):
    hour = "11" if phase == "FINAL_EXAM" else "10"
    return {"task_id": task_id, "task_type": "SCORED", "phase": phase,
        "issued_at": f"2026-10-20T{hour}:00:00+02:00",
        "answered_at": f"2026-10-20T{hour}:05:00+02:00",
        "difficulty": difficulty, "fraction": fraction, "rubric": "Overall criterion",
        "rationale": "Answer evaluated against rubric", "evidence": "Synthetic answer trace",
        "assistance": "NONE", "hints": [], "independent_solution": True,
        "skill_marks": [{"skill_id": sid, "criterion": "Skill criterion", "fraction": mark,
                         "difficulty": None, "rationale": "Synthetic observed work"}
                        for sid, mark in skill_marks], "additional_skills": []}


def result(package, *, exam=True):
    tasks = [task("learn", "LEARNING", 0.5, [("TARGET", 1.0), ("PREREQ", 0.0)])]
    if exam:
        tasks.append(task("exam", "FINAL_EXAM", 1.0, [("TARGET", 0.0)], "HARD"))
    return {"schema_version": "1.0", "contract_version": "uniflow-scoring-v1",
        "session_id": package["session_id"], "handoff_id": package["handoff_id"],
        "handoff_artifact_sha256": package["handoff_artifact_sha256"],
        "phase_states": [{"phase": "LEARNING", "status": "COMPLETED",
                          "difficulty_rationale": "Vary practice"},
                         {"phase": "FINAL_EXAM", "status": "COMPLETED" if exam else "NOT_STARTED",
                          "difficulty_rationale": "New independent task" if exam else None}],
        "final_exam_decision": {"offered_at": "2026-10-20T10:10:00+02:00",
                                "decided_at": "2026-10-20T10:11:00+02:00",
                                "decision": "START" if exam else "PAUSE"},
        "tasks": tasks, "errors": []}


def write(tmp_path, value):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_one_file_export_and_read_only_multiskill_preview(tmp_path):
    config, factory, sid, _, handoff = setup(tmp_path)
    exported = export_learning_chat(config, {"handoff_id": handoff.handoff_id,
        "destination_directory": tmp_path})
    assert exported.status == "EXPORTED"
    assert export_learning_chat_package(config, handoff.handoff_id, tmp_path).status == "ALREADY_EXPORTED"
    package = json.loads(exported.path.read_bytes())
    assert package["package_schema"] == "uniflow-learning-chat-package-v1"
    assert package["handoff_revision"] == handoff.revision
    instructions = " ".join(package["chat_instructions"])
    for required in ("Before issuing", "atomic task criteria", "Skill subcriteria",
                     "Eine korrekte Zwischenrechnung", "algebraically equivalent",
                     "error-propagation", "ANSWER_SOURCE", "TASK_SOURCE", "RUBRIC_SOURCE",
                     "TIME_SOURCE", "EVIDENCE_GAPS", "DIRECT_OBSERVATION", "RECONSTRUCTED",
                     "MISSING", "AFTER_ASSESSMENT", "HUMAN_REVIEW_REQUIRED",
                     "incomplete review draft", "4000", "2000", "50 points per phase"):
        assert required in instructions
    assert package["allowed_skill_ids"] == ["TARGET", "PREREQ"]
    assert package["handoff"]["session_id"] == sid
    assert hashlib.sha256(json.dumps(package["handoff"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest() == package["handoff_artifact_sha256"]
    assert package["handoff"]["source_data"] == []
    assert all(item["status"] == "UNKNOWN" for item in package["handoff"]["mastery_snapshot"])
    assert package["scoring_rules"]["phase_max_points"] == {"LEARNING": 50, "FINAL_EXAM": 50}
    assert package["scoring_rules"]["difficulty_weights"] == {
        "EASY": "0.5", "MEDIUM": "1.0", "HARD": "1.5", "VERY_HARD": "2.0"}
    assert "skill_marks" in package["result_json_schema"]["$defs"]["ScoredTaskV1"]["properties"]
    body = result(package)
    body["tasks"][0]["additional_skills"] = [{"provisional_skill_name": "Related ability",
        "observation": "Synthetic transfer", "recommended_next_session": "Check later"}]
    body["tasks"].append({"task_id": "bonus", "task_type": "PREVIEW_BONUS",
        "observation": "Unscored outlook", "additional_skills": []})
    body["errors"] = [{"task_id": "learn", "skill_id": "TARGET",
        "description": "Observed issue", "context": "First attempt", "status": "OBSERVED"},
        {"task_id": "exam", "skill_id": None, "description": "Possible issue",
         "context": "Later", "status": "SUSPECTED"}]
    preview = preview_scoring_result(config, write(tmp_path, body))
    assert preview.score.phase_points == {"LEARNING": Decimal(25), "FINAL_EXAM": Decimal(50)}
    assert preview.score.session_score == 75
    assert preview.score.skill_session_scores == {"PREREQ": None, "TARGET": Decimal(50)}
    assert "SKILL_INCOMPLETE:PREREQ" in preview.open_information
    assert preview.qualitative_observations[0]["provisional_skill_name"] == "Related ability"
    assert [item["status"] for item in preview.errors] == ["OBSERVED", "SUSPECTED"]
    assert preview.status == "PENDING_HUMAN_REVIEW"
    with factory() as db:
        assert db.get(LearningSession, sid).current_handoff_id == handoff.handoff_id
        assert list(db.scalars(select(LearningResult))) == []
        assert list(db.scalars(select(Evidence))) == []
        assert list(db.scalars(select(MasteryState))) == []
        assert len(list(db.scalars(select(Skill)))) == 3
    factory.kw["bind"].dispose()


def test_incomplete_and_zero_are_distinct(tmp_path):
    config, factory, _, _, handoff = setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    partial = result(package, exam=False)
    preview = preview_scoring_result(config, write(tmp_path, partial))
    assert preview.score.session_score is None
    assert preview.score.phase_points["FINAL_EXAM"] is None
    assert "FINAL_EXAM:NOT_STARTED" in preview.open_information
    empty = result(package, exam=False)
    empty["tasks"] = []
    empty["phase_states"][0]["status"] = "NOT_STARTED"
    empty["phase_states"][0]["difficulty_rationale"] = None
    empty["final_exam_decision"] = None
    unassessed = preview_scoring_result(config, write(tmp_path, empty))
    assert set(unassessed.open_information) >= {"SKILL_UNASSESSED:TARGET", "SKILL_UNASSESSED:PREREQ"}
    zero = result(package)
    for item in zero["tasks"]:
        item["fraction"] = 0.0
    assert preview_scoring_result(config, write(tmp_path, zero)).score.session_score == 0
    factory.kw["bind"].dispose()


@pytest.mark.parametrize("change,code", [
    (lambda p: p.update(session_id="wrong"), "SCORING_RESULT_SESSION_MISMATCH"),
    (lambda p: p.update(handoff_artifact_sha256="0" * 64), "SCORING_RESULT_HANDOFF_HASH_MISMATCH"),
    (lambda p: p["tasks"][0]["skill_marks"][0].update(skill_id="OUTSIDE"), "SCORING_RESULT_SKILL_NOT_ALLOWED"),
    (lambda p: p["tasks"][1].update(task_id="learn"), "SCORING_RESULT_SCHEMA_INVALID"),
    (lambda p: p["tasks"][0].update(difficulty="UNKNOWN"), "SCORING_RESULT_SCHEMA_INVALID"),
    (lambda p: p["tasks"][0].update(fraction=1.5), "SCORING_RESULT_SCHEMA_INVALID"),
    (lambda p: p["tasks"][0].update(evidence=""), "SCORING_RESULT_SCHEMA_INVALID"),
    (lambda p: p.update(final_exam_decision=None), "SCORING_RESULT_SCHEMA_INVALID"),
    (lambda p: p["final_exam_decision"].update(decision="PAUSE"), "SCORING_RESULT_SCHEMA_INVALID"),
])
def test_invalid_results_rejected_without_writes(tmp_path, change, code):
    config, factory, sid, _, handoff = setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package)
    change(body)
    with pytest.raises(ValueError, match=code):
        preview_scoring_result(config, write(tmp_path, body))
    with factory() as db:
        assert db.get(LearningSession, sid).state.value == "HANDOFF_CREATED"
        assert list(db.scalars(select(LearningResult))) == []
        assert list(db.scalars(select(MasteryState))) == []
    factory.kw["bind"].dispose()


def test_duplicate_json_key_old_import_boundary_and_tamper(tmp_path):
    config, factory, _, _, handoff = setup(tmp_path)
    export = export_learning_chat_package(config, handoff.handoff_id, tmp_path)
    package = json.loads(export.path.read_bytes())
    bad = tmp_path / "result.json"
    bad.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="SCORING_RESULT_DUPLICATE_JSON_KEY"):
        preview_scoring_result(config, bad)
    bad.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="SCORING_RESULT_INVALID_JSON"):
        preview_scoring_result(config, bad)
    body = result(package)
    with pytest.raises(ValueError, match="RESULT_SCORING_FORMAT_SELECTED"):
        import_learning_result(config, write(tmp_path, body))
    export.path.write_text("tampered", encoding="utf-8")
    with pytest.raises(WorkflowCommandError, match="EXCHANGE_DESTINATION_CONFLICT"):
        export_learning_chat_package(config, handoff.handoff_id, tmp_path)
    factory.kw["bind"].dispose()


def test_known_zero_historical_revision_and_stored_tamper(tmp_path):
    config, factory, sid, _, first = setup(tmp_path)
    with factory.begin() as db:
        db.add(MasteryState(skill_id="TARGET", value=0.0, calculation_method="synthetic"))
    second = generate_handoff(config, sid)
    assert second.revision == first.revision + 1
    with pytest.raises(WorkflowCommandError, match="EXCHANGE_HANDOFF_NOT_CURRENT"):
        export_learning_chat_package(config, first.handoff_id, tmp_path)
    exported = export_learning_chat_package(config, second.handoff_id, tmp_path)
    package = json.loads(exported.path.read_bytes())
    mastery = next(item for item in package["handoff"]["mastery_snapshot"] if item["skill_id"] == "TARGET")
    assert mastery["status"] == "KNOWN" and mastery["value"] == 0.0
    stale = result(package)
    stale["handoff_id"] = first.handoff_id
    stale["handoff_artifact_sha256"] = first.artifact_sha256
    with pytest.raises(ValueError, match="SCORING_RESULT_HANDOFF_NOT_CURRENT"):
        preview_scoring_result(config, write(tmp_path, stale))
    with factory() as db:
        artifact = db.get(DerivedArtifact, db.get(LearningHandoff, second.handoff_id).artifact_id)
        stored = artifact.stored_path
    with open(stored, "wb") as fh:
        fh.write(b"tampered")
    with pytest.raises(ValueError, match="SCORING_RESULT_HANDOFF_INVALID"):
        preview_scoring_result(config, write(tmp_path, result(package)))
    factory.kw["bind"].dispose()
