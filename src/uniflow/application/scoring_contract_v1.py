"""Pure UniFlow scoring contract V1 calculation; no persistence or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext

from uniflow.schemas.scoring_contract_v1 import ScoredTaskV1, ScoringResultV1

WEIGHTS = {"EASY": Decimal("0.5"), "MEDIUM": Decimal("1.0"),
           "HARD": Decimal("1.5"), "VERY_HARD": Decimal("2.0")}
PHASES = ("LEARNING", "FINAL_EXAM")


def display_points(value: Decimal) -> Decimal:
    """Round only at the display boundary to two points decimals, half up."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mastery_display_score(mastery_value: Decimal | None) -> Decimal | None:
    """Convert an existing 0..1 mastery value; None remains unassessed."""
    if mastery_value is None:
        return None
    if not mastery_value.is_finite() or not Decimal(0) <= mastery_value <= Decimal(1):
        raise ValueError("invalid mastery value")
    return mastery_value * 100


@dataclass(frozen=True)
class ScoreSummaryV1:
    assessment_status: str
    completion_status: str
    phase_points: dict[str, Decimal | None]
    session_score: Decimal | None
    skill_phase_points: dict[str, dict[str, Decimal | None]]
    skill_session_scores: dict[str, Decimal | None]


def _points(values: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    if not values:
        return None
    denominator = sum((weight for weight, _ in values), Decimal(0))
    if denominator <= 0:
        raise ValueError("nonpositive score weight")
    numerator = sum((weight * fraction for weight, fraction in values), Decimal(0))
    with localcontext() as context:
        context.prec = 50
        return Decimal(50) * numerator / denominator


def score_result(result: ScoringResultV1, *, session_id: str, handoff_id: str,
                 handoff_artifact_sha256: str, allowed_skill_ids: frozenset[str]) -> ScoreSummaryV1:
    """Validate a result against an explicit Handoff scope and calculate exact scores.

    This API deliberately cannot accept LearningResultV2 and never invokes mastery apply.
    """
    if not isinstance(result, ScoringResultV1):
        raise TypeError("ScoringResultV1 required")
    if (result.session_id != session_id or result.handoff_id != handoff_id or
            result.handoff_artifact_sha256 != handoff_artifact_sha256):
        raise ValueError("scoring result handoff identity mismatch")
    if not allowed_skill_ids:
        raise ValueError("empty handoff skill scope")
    scored = [task for task in result.tasks if isinstance(task, ScoredTaskV1)]
    for task in result.tasks:
        for item in task.additional_skills:
            if item.skill_id is not None and item.skill_id not in allowed_skill_ids:
                raise ValueError("additional skill outside handoff scope")
    for task in scored:
        for mark in task.skill_marks:
            if mark.skill_id not in allowed_skill_ids:
                raise ValueError("scored skill outside handoff scope")
    for error in result.errors:
        if error.skill_id is not None and error.skill_id not in allowed_skill_ids:
            raise ValueError("error skill outside handoff scope")

    states = {item.phase: item.status for item in result.phase_states}
    phase_points = {phase: _points([(WEIGHTS[task.difficulty], Decimal(str(task.fraction)))
        for task in scored if task.phase == phase]) for phase in PHASES}
    complete = all(states[phase] == "COMPLETED" and phase_points[phase] is not None
                   for phase in PHASES)
    session_score = sum((phase_points[phase] for phase in PHASES), Decimal(0)) if complete else None

    observed_skills = sorted({mark.skill_id for task in scored for mark in task.skill_marks})
    skill_phase_points = {}
    skill_session_scores = {}
    for skill_id in observed_skills:
        by_phase = {}
        for phase in PHASES:
            by_phase[phase] = _points([(WEIGHTS[mark.difficulty or task.difficulty],
                Decimal(str(mark.fraction))) for task in scored if task.phase == phase
                for mark in task.skill_marks if mark.skill_id == skill_id])
        skill_phase_points[skill_id] = by_phase
        skill_session_scores[skill_id] = (sum(by_phase.values(), Decimal(0))
            if complete and all(value is not None for value in by_phase.values()) else None)

    return ScoreSummaryV1(
        assessment_status="PENDING_REVIEW" if scored else "UNASSESSED",
        completion_status="COMPLETE" if complete else "INCOMPLETE",
        phase_points=phase_points, session_score=session_score,
        skill_phase_points=skill_phase_points, skill_session_scores=skill_session_scores)
