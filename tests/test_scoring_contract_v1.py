"""Pure synthetic contract cases; these tests never configure a project or database."""
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from uniflow.application.scoring_contract_v1 import (
    display_points, mastery_display_score, score_result,
)
from uniflow.schemas.learning_result_v2 import LearningResultV2
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1

HASH = "a" * 64
SCOPE = frozenset({"chain", "power", "algebra"})


def task(task_id, phase, fraction=1.0, difficulty="MEDIUM", marks=None):
    marks = marks if marks is not None else [("chain", fraction, None)]
    issued = "2026-10-20T11:00:00+02:00" if phase == "FINAL_EXAM" else "2026-10-20T10:00:00+02:00"
    answered = "2026-10-20T11:05:00+02:00" if phase == "FINAL_EXAM" else "2026-10-20T10:05:00+02:00"
    return {"task_id": task_id, "task_type": "SCORED", "phase": phase,
        "issued_at": issued, "answered_at": answered, "difficulty": difficulty,
        "fraction": fraction, "rubric": "Explicit overall criterion", "rationale": "Checked work",
        "evidence": "Learner answer and solution trace", "assistance": "NONE", "hints": [],
        "independent_solution": True,
        "skill_marks": [{"skill_id": sid, "fraction": value, "difficulty": level,
            "criterion": "Explicit skill criterion", "rationale": "Observed answer"}
            for sid, value, level in marks], "additional_skills": []}


def payload(tasks=None, learning="COMPLETED", exam="COMPLETED"):
    tasks = tasks if tasks is not None else [task("a", "LEARNING"), task("b", "FINAL_EXAM")]
    offer = ({"offered_at": "2026-10-20T10:10:00+02:00",
              "decided_at": "2026-10-20T10:11:00+02:00", "decision": "START"}
             if any(item.get("phase") == "FINAL_EXAM" for item in tasks) else None)
    return {"schema_version": "1.0", "contract_version": "uniflow-scoring-v1",
        "session_id": "session", "handoff_id": "handoff", "handoff_artifact_sha256": HASH,
        "final_exam_decision": offer,
        "phase_states": [{"phase": "LEARNING", "status": learning,
                          "difficulty_rationale": "Adaptive practice"},
                         {"phase": "FINAL_EXAM", "status": exam,
                          "difficulty_rationale": "New transfer questions"}],
        "tasks": tasks,
        "errors": []}


def scored(value):
    return score_result(ScoringResultV1.model_validate(value), session_id="session",
        handoff_id="handoff", handoff_artifact_sha256=HASH, allowed_skill_ids=SCOPE)


def test_all_correct_zero_and_mastery_scale():
    assert scored(payload()).session_score == Decimal(100)
    assert scored(payload([task("a", "LEARNING", 0.0), task("b", "FINAL_EXAM", 0.0)])).session_score == 0
    assert mastery_display_score(None) is None
    assert mastery_display_score(Decimal(0)) == 0
    assert mastery_display_score(Decimal("0.725")) == Decimal("72.500")
    assert display_points(Decimal("12.345")) == Decimal("12.35")


def test_weighted_phases_partial_credit_and_different_question_counts():
    tasks = [task("easy", "LEARNING", 0.0, "EASY"),
        task("medium", "LEARNING", 0.5, "MEDIUM"),
        task("hard", "LEARNING", 1.0, "HARD"),
        task("very", "LEARNING", 1.0, "VERY_HARD"),
        task("exam", "FINAL_EXAM", 0.5, "HARD")]
    result = scored(payload(tasks))
    assert result.phase_points["LEARNING"] == Decimal(40)
    assert result.phase_points["FINAL_EXAM"] == Decimal(25)
    assert result.session_score == Decimal(65)
    assert result.skill_session_scores["chain"] == Decimal(65)
    assert scored(payload([task("a", "LEARNING", 0, "VERY_HARD"),
        task("b", "LEARNING", 1, "EASY"), task("c", "FINAL_EXAM")])).session_score == Decimal(60)


def test_multi_skill_task_counts_once_but_skills_differ():
    learning = task("a", "LEARNING", 0.5, marks=[("chain", 1.0, "EASY"),
        ("power", 0.0, "HARD"), ("algebra", 0.5, "VERY_HARD")])
    exam = task("b", "FINAL_EXAM", 1.0, marks=[("chain", 0.0, None),
        ("power", 1.0, None), ("algebra", 1.0, None)])
    result = scored(payload([learning, exam]))
    assert result.session_score == Decimal(75)
    assert result.skill_session_scores == {"algebra": Decimal(75), "chain": Decimal(50),
                                           "power": Decimal(50)}
    assert result.skill_phase_points["power"]["LEARNING"] == 0


def test_incomplete_and_per_skill_non_assessable():
    partial = scored(payload([task("a", "LEARNING")], exam="NOT_STARTED"))
    assert partial.completion_status == "INCOMPLETE"
    assert partial.session_score is None
    assert partial.phase_points == {"LEARNING": Decimal(50), "FINAL_EXAM": None}
    assert partial.skill_session_scores["chain"] is None
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(payload([task("b", "FINAL_EXAM")], learning="NOT_STARTED"))
    split = scored(payload([task("a", "LEARNING", marks=[("chain", 1.0, None)]),
        task("b", "FINAL_EXAM", marks=[("power", 1.0, None)])]))
    assert split.session_score == 100
    assert split.skill_session_scores == {"chain": None, "power": None}
    empty = scored(payload([], learning="NOT_STARTED", exam="NOT_STARTED"))
    assert empty.assessment_status == "UNASSESSED" and empty.session_score is None


def test_bonus_and_later_observation_have_zero_score_effect():
    baseline = payload()
    extra = deepcopy(baseline)
    extra["tasks"][0]["additional_skills"] = [{"skill_id": "power", "observation": "Possible transfer"}]
    extra["tasks"].append({"task_id": "bonus", "task_type": "PREVIEW_BONUS",
        "observation": "Correct or incorrect is qualitative only",
        "additional_skills": [{"skill_id": "algebra", "observation": "Future topic"}]})
    assert scored(extra) == scored(baseline)


@pytest.mark.parametrize("change", [
    lambda p: p["tasks"][0].update(skill_marks=[]),
    lambda p: p["tasks"][0].update(task_type="UNKNOWN"),
    lambda p: p["tasks"][1].update(task_id="a"),
    lambda p: p["tasks"][0]["skill_marks"].append(deepcopy(p["tasks"][0]["skill_marks"][0])),
    lambda p: p["tasks"][0].update(difficulty="IMPOSSIBLE"),
    lambda p: p["tasks"][0].update(fraction=-0.1),
    lambda p: p["tasks"][0].update(fraction=1.1),
    lambda p: p["tasks"][0].update(fraction=float("nan")),
    lambda p: p["tasks"][0].update(fraction=float("inf")),
    lambda p: p["tasks"][0].update(fraction=True),
    lambda p: p.update(schema_version="2.0"),
    lambda p: p.update(phase_states=[p["phase_states"][0], p["phase_states"][0]]),
    lambda p: p["tasks"][0].update(rubric=""),
    lambda p: p["tasks"][0].update(answered_at="2026-10-20T09:00:00+02:00"),
    lambda p: p["tasks"][0].update(assistance="HINTS"),
    lambda p: p["tasks"][0]["skill_marks"][0].update(fraction=True),
    lambda p: p["tasks"][0]["skill_marks"][0].update(difficulty="X"),
    lambda p: p.update(final_exam_decision=None),
    lambda p: p["final_exam_decision"].update(decision="PAUSE"),
    lambda p: p["final_exam_decision"].update(decided_at="2026-10-20T11:01:00+02:00"),
])
def test_invalid_contract_rejected(change):
    value = payload()
    change(value)
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(value)


def test_scope_identity_and_error_evidence():
    value = payload()
    value["tasks"][0]["skill_marks"][0]["skill_id"] = "unknown"
    with pytest.raises(ValueError, match="scope"):
        scored(value)
    value = payload()
    value["tasks"][0]["additional_skills"] = [{"skill_id": "unknown", "observation": "Observed"}]
    with pytest.raises(ValueError, match="scope"):
        scored(value)
    value = payload()
    value["errors"] = [{"task_id": "a", "skill_id": "chain", "description": "Rule confusion",
                        "context": "Initial answer", "status": "OBSERVED"}]
    assert scored(value).session_score == 100
    value["errors"][0]["task_id"] = "missing"
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(value)
    with pytest.raises(ValueError, match="identity"):
        score_result(ScoringResultV1.model_validate(payload()), session_id="other",
            handoff_id="handoff", handoff_artifact_sha256=HASH, allowed_skill_ids=SCOPE)


def test_legacy_v2_is_separate_and_not_accepted_by_pure_scoring():
    legacy = LearningResultV2.model_validate({"schema_version": "2.0",
        "protocol_version": "subject-learning-v1", "result_id": "old", "session_id": "session",
        "handoff_id": "handoff", "course_id": "course", "handoff_artifact_sha256": HASH,
        "completed_at": "2026-10-20T12:00:00+02:00", "session_outcome": "COMPLETED",
        "skill_evidence": [{"skill_id": "chain", "recall": 0.0}]})
    with pytest.raises(TypeError, match="ScoringResultV1"):
        score_result(legacy, session_id="session", handoff_id="handoff",
            handoff_artifact_sha256=HASH, allowed_skill_ids=SCOPE)
