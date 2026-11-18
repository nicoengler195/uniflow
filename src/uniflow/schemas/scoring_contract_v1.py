"""Standalone, strictly versioned assessment contract; not a V2 learning result."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, field_validator, model_validator

from uniflow.schemas.common import StrictModel

Fraction = StrictFloat
Difficulty = Literal["EASY", "MEDIUM", "HARD", "VERY_HARD"]


class SkillMarkV1(StrictModel):
    skill_id: str = Field(min_length=1, max_length=120)
    fraction: Fraction = Field(ge=0, le=1, allow_inf_nan=False)
    difficulty: Difficulty | None = None  # None inherits the task difficulty.
    criterion: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=2000)


class AdditionalSkillObservationV1(StrictModel):
    skill_id: str | None = Field(default=None, min_length=1, max_length=120)
    provisional_skill_name: str | None = Field(default=None, min_length=1, max_length=240)
    observation: str = Field(min_length=1, max_length=2000)
    recommended_next_session: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def one_identity(self):
        if (self.skill_id is None) == (self.provisional_skill_name is None):
            raise ValueError("qualitative observation needs exactly one skill reference")
        return self


class ErrorObservationV1(StrictModel):
    task_id: str = Field(min_length=1, max_length=120)
    skill_id: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    context: str = Field(min_length=1, max_length=2000)
    status: Literal["OBSERVED", "SUSPECTED"]


class ScoredTaskV1(StrictModel):
    task_id: str = Field(min_length=1, max_length=120)
    task_type: Literal["SCORED"]
    phase: Literal["LEARNING", "FINAL_EXAM"]
    issued_at: datetime
    answered_at: datetime
    difficulty: Difficulty
    fraction: Fraction = Field(ge=0, le=1, allow_inf_nan=False)
    rubric: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)
    assistance: Literal["NONE", "HINTS", "EXPLANATION", "NOT_INDEPENDENT"]
    hints: list[str] = Field(default_factory=list, max_length=20)
    independent_solution: StrictBool
    skill_marks: list[SkillMarkV1] = Field(min_length=1, max_length=40)
    additional_skills: list[AdditionalSkillObservationV1] = Field(default_factory=list, max_length=40)

    @field_validator("issued_at", "answered_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("task timestamps must be timezone aware")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.issued_at >= self.answered_at:
            raise ValueError("difficulty must be recorded before the answer")
        ids = [mark.skill_id for mark in self.skill_marks]
        extra = [item.skill_id for item in self.additional_skills if item.skill_id is not None]
        if len(ids) != len(set(ids)) or len(extra) != len(set(extra)) or set(ids) & set(extra):
            raise ValueError("duplicate or conflicting task skill")
        if self.assistance == "NONE" and self.hints:
            raise ValueError("unassisted task cannot contain hints")
        if self.assistance == "HINTS" and not self.hints:
            raise ValueError("hinted task requires recorded hints")
        if self.assistance == "NOT_INDEPENDENT" and self.independent_solution:
            raise ValueError("non-independent task cannot claim independence")
        if self.assistance != "NONE" and self.independent_solution:
            raise ValueError("assisted task cannot claim independent solution")
        return self


class PreviewBonusV1(StrictModel):
    task_id: str = Field(min_length=1, max_length=120)
    task_type: Literal["PREVIEW_BONUS"]
    observation: str = Field(min_length=1, max_length=4000)
    recommended_next_action: str | None = Field(default=None, max_length=2000)
    additional_skills: list[AdditionalSkillObservationV1] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def distinct_skills(self):
        ids = [item.skill_id for item in self.additional_skills if item.skill_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate bonus skill observation")
        return self


class PhaseStateV1(StrictModel):
    phase: Literal["LEARNING", "FINAL_EXAM"]
    status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED"]
    difficulty_rationale: str | None = Field(default=None, min_length=1, max_length=2000)


class FinalExamDecisionV1(StrictModel):
    offered_at: datetime
    decided_at: datetime
    decision: Literal["START", "CONTINUE_PRACTICE", "PAUSE"]

    @field_validator("offered_at", "decided_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("exam decision timestamps must be timezone aware")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.offered_at > self.decided_at:
            raise ValueError("exam decision precedes offer")
        return self


class ScoringResultV1(StrictModel):
    schema_version: Literal["1.0"]
    contract_version: Literal["uniflow-scoring-v1"]
    session_id: str = Field(min_length=1, max_length=80)
    handoff_id: str = Field(min_length=1, max_length=80)
    handoff_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase_states: list[PhaseStateV1] = Field(min_length=2, max_length=2)
    final_exam_decision: FinalExamDecisionV1 | None = None
    tasks: list[ScoredTaskV1 | PreviewBonusV1] = Field(max_length=200)
    errors: list[ErrorObservationV1] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def coherent(self):
        states = {item.phase: item for item in self.phase_states}
        if set(states) != {"LEARNING", "FINAL_EXAM"} or len(states) != 2:
            raise ValueError("both distinct phase states are required")
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task id")
        scored = [task for task in self.tasks if isinstance(task, ScoredTaskV1)]
        learning = [task for task in scored if task.phase == "LEARNING"]
        exam = [task for task in scored if task.phase == "FINAL_EXAM"]
        if self.final_exam_decision is not None:
            decision = self.final_exam_decision
            if not learning or states["LEARNING"].status != "COMPLETED":
                raise ValueError("exam may be offered only after completed learning")
            if any(task.answered_at > decision.offered_at for task in learning):
                raise ValueError("exam was offered before learning ended")
            if decision.decision != "START" and (exam or states["FINAL_EXAM"].status != "NOT_STARTED"):
                raise ValueError("non-start exam decision conflicts with exam activity")
        if exam:
            if self.final_exam_decision is None or self.final_exam_decision.decision != "START":
                raise ValueError("exam tasks require an explicit start decision")
            if any(task.issued_at < self.final_exam_decision.decided_at for task in exam):
                raise ValueError("exam task issued before start decision")
        if states["FINAL_EXAM"].status != "NOT_STARTED" and (
            self.final_exam_decision is None or self.final_exam_decision.decision != "START"
        ):
            raise ValueError("exam activity requires a start decision")
        for phase, state in states.items():
            count = sum(task.phase == phase for task in scored)
            if state.status == "NOT_STARTED" and count:
                raise ValueError("not-started phase contains scored tasks")
            if state.status == "COMPLETED" and not count:
                raise ValueError("completed phase requires scored tasks")
            if count and not state.difficulty_rationale:
                raise ValueError("scored phase requires difficulty rationale")
        if any(error.task_id not in ids for error in self.errors):
            raise ValueError("error references unknown task")
        return self
