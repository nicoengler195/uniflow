"""Internal I1 evidence data contract. No I/O, external verification or scoring.

All references and provenance are declarations, including BOUND and review records.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
# Bounded canonical fixed decimal: no exponents, signs, leading/trailing zeros.
Points = Annotated[str, StringConstraints(
    strict=True, max_length=40, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$"
)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
Status = Literal["DIRECT_OBSERVATION", "RECONSTRUCTED", "MISSING", "UNVERIFIED"]
Phase = Literal["LEARNING", "FINAL_EXAM"]
Difficulty = Literal["EASY", "MEDIUM", "HARD", "VERY_HARD"]
ReviewStatus = Literal["PENDING", "HUMAN_REVIEW_REQUIRED"]


def distinct(values, label):
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


def exact_sum(values):
    # Avoid dependence on caller's Decimal context; each input has <=40 chars.
    values = list(values)
    with localcontext() as ctx:
        ctx.prec = 90 + len(str(len(values)))
        return sum((Decimal(v) for v in values), Decimal(0))


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Selector(Model):
    kind: Literal["JSON_POINTER", "UTF8_BYTE_RANGE"]
    pointer: Text | None
    start: NonnegativeInt | None
    end: NonnegativeInt | None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "JSON_POINTER":
            if self.pointer is None or not self.pointer.startswith("/") or self.start is not None or self.end is not None:
                raise ValueError("JSON pointer needs /pointer only; root selected by byte range")
            # RFC 6901 escapes only; actual resolution belongs to I2.
            import re
            if re.search(r"~(?![01])", self.pointer):
                raise ValueError("invalid JSON pointer escape")
        elif self.pointer is not None or self.start is None or self.end is None or self.end <= self.start:
            raise ValueError("byte selector needs nonempty [start,end) only")
        return self


class EvidenceRef(Model):
    artifact_id: Text
    sha256: Digest
    selector: Selector
    quote: Text | None
    provenance_status: Status
    source_message_id: Text | None


class Gap(Model):
    gap_id: Text
    missing_information: Text
    reason: Text


class Provenance(Model):
    provenance_status: Status
    evidence_refs: list[EvidenceRef]
    asserted_by: Text | None
    observer_id: Text | None
    observation_method: Text | None
    reconstruction_reason: Text | None
    gaps: list[Gap]

    @model_validator(mode="after")
    def supported_declaration(self):
        distinct([g.gap_id for g in self.gaps], "gap id")
        if self.provenance_status == "DIRECT_OBSERVATION":
            if not self.observer_id or not self.observation_method or not self.evidence_refs:
                raise ValueError("direct observation requires observer, method and evidence")
            if not any(e.provenance_status == "DIRECT_OBSERVATION" for e in self.evidence_refs):
                raise ValueError("direct observation requires a declared direct evidence reference")
        elif self.observer_id is not None or self.observation_method is not None:
            raise ValueError("non-direct claim must not suggest an independent observer")
        if self.provenance_status == "RECONSTRUCTED" and (not self.evidence_refs or not self.reconstruction_reason):
            raise ValueError("reconstruction requires sources and reason")
        if self.provenance_status == "MISSING" and not self.gaps:
            raise ValueError("missing information requires an explicit gap")
        if self.provenance_status == "UNVERIFIED" and not self.asserted_by:
            raise ValueError("unverified claim requires an asserting source")
        return self


class TimeClaim(Model):
    asserted_at: Text | None
    observed_at: Text | None
    received_at: Text | None
    source_clock: Text | None
    timezone_or_offset: Text | None
    precision: Text | None
    interval_start: Text | None
    interval_end: Text | None
    time_status: Status
    provenance: Provenance

    @model_validator(mode="after")
    def time_shape(self):
        if self.time_status != self.provenance.provenance_status:
            raise ValueError("time and provenance status differ")
        fields = (self.asserted_at, self.observed_at, self.received_at, self.interval_start, self.interval_end)
        for value in fields:
            if value is not None:
                parsed = datetime.fromisoformat(value)
                if parsed.utcoffset() is None or "T" not in value:
                    raise ValueError("timestamps require ISO datetime with explicit offset")
        if (self.interval_start is None) != (self.interval_end is None):
            raise ValueError("uncertainty interval requires both endpoints")
        if self.interval_start is not None and datetime.fromisoformat(self.interval_start) > datetime.fromisoformat(self.interval_end):
            raise ValueError("reversed uncertainty interval")
        if self.time_status == "MISSING":
            if any(v is not None for v in fields):
                raise ValueError("missing time must remain null")
        else:
            if not any(v is not None for v in fields) or not self.source_clock or not self.timezone_or_offset or not self.precision:
                raise ValueError("known time requires value, clock, offset/zone and precision")
        if self.observed_at is not None and self.time_status != "DIRECT_OBSERVATION":
            raise ValueError("observed time requires declared direct observation")
        if self.time_status == "DIRECT_OBSERVATION" and self.observed_at is None:
            raise ValueError("direct time requires observed_at")
        return self


class SourceArtifact(Model):
    artifact_id: Text
    byte_length: NonnegativeInt
    media_type: Text
    sha256: Digest
    source_kind: Text
    capture_method: Text


class ResultBinding(Model):
    status: Literal["BOUND", "UNBOUND_DRAFT"]
    result_sha256: Digest | None
    contract_version: Literal["uniflow-scoring-v1"] | None
    reason: Text | None

    @model_validator(mode="after")
    def binding_shape(self):
        if self.status == "BOUND":
            if self.result_sha256 is None or self.contract_version != "uniflow-scoring-v1" or self.reason is not None:
                raise ValueError("BOUND requires declared V1 hash and contract, no draft reason")
        elif self.result_sha256 is not None or self.contract_version is not None or not self.reason:
            raise ValueError("draft requires null result hash/contract and explicit reason")
        return self


class RubricRef(Model):
    rubric_id: Text
    revision: PositiveInt


class Task(Model):
    task_id: Text
    phase: Phase
    task_statement_ref: EvidenceRef
    issued_time_claim: TimeClaim
    rubric_ref: RubricRef
    difficulty: Difficulty
    difficulty_rationale: Text
    transfer_of_task_id: Text | None


class Event(Model):
    event_id: Text
    event_type: Literal["TASK_ISSUED", "RUBRIC_DECLARED", "ANSWER_SUBMITTED", "HINT_AVAILABLE",
                        "EXPLANATION_AVAILABLE", "ASSESSMENT_RECORDED", "FEEDBACK_DELIVERED",
                        "CORRECTION_SUBMITTED", "RETRACTION", "REVIEW_RECORDED"]
    task_id: Text | None
    attempt_id: Text | None
    source_ref: EvidenceRef
    content_sha256: Digest
    time_claim: TimeClaim
    parent_event_ids: list[Text]
    collector_sequence: NonnegativeInt | None
    provenance: Provenance

    @model_validator(mode="after")
    def event_shape(self):
        distinct(self.parent_event_ids, "parent event")
        if self.event_id in self.parent_event_ids:
            raise ValueError("event cannot parent itself")
        if self.event_type in {"ANSWER_SUBMITTED", "CORRECTION_SUBMITTED", "ASSESSMENT_RECORDED", "FEEDBACK_DELIVERED"} and (self.task_id is None or self.attempt_id is None):
            raise ValueError("attempt event requires task and attempt IDs")
        if self.event_type in {"TASK_ISSUED", "RUBRIC_DECLARED"} and self.task_id is None:
            raise ValueError("task event requires task ID")
        return self


class Assistance(Model):
    event_refs: list[Text]
    coverage: Literal["COMPLETE_IN_CAPTURED_CHANNEL", "PARTIAL", "UNKNOWN"]
    scope: Text | None
    classification: Literal["NONE_OBSERVED", "HINTS", "EXPLANATION", "NOT_INDEPENDENT", "UNKNOWN"]
    availability: Literal["AVAILABLE", "NOT_OBSERVED", "UNKNOWN"]
    usage: Literal["DECLARED_USED", "DECLARED_NOT_USED", "UNKNOWN"]
    access: Literal["DECLARED_ACCESSED", "DECLARED_NOT_ACCESSED", "UNKNOWN"]

    @model_validator(mode="after")
    def assistance_shape(self):
        distinct(self.event_refs, "assistance event")
        if self.coverage == "COMPLETE_IN_CAPTURED_CHANNEL" and not self.scope:
            raise ValueError("complete capture needs documented scope")
        if self.classification == "NONE_OBSERVED" and (self.coverage != "COMPLETE_IN_CAPTURED_CHANNEL" or self.event_refs or self.availability != "NOT_OBSERVED"):
            raise ValueError("empty/partial history does not prove no assistance")
        if self.classification in {"HINTS", "EXPLANATION", "NOT_INDEPENDENT"} and (not self.event_refs or self.availability != "AVAILABLE"):
            raise ValueError("assistance requires declared available event")
        return self


class Correction(Model):
    corrects_attempt_id: Text
    kind: Literal["GUIDED_CORRECTION", "SELF_CORRECTION"]


class Attempt(Model):
    task_id: Text
    attempt_id: Text
    phase: Phase
    attempt_order: PositiveInt
    answer_evidence: list[EvidenceRef] = Field(min_length=1)
    answer_time_claim: TimeClaim
    assistance_before_attempt: Assistance
    assessment: list[Text]
    feedback_after_attempt: list[Text]
    correction_attempt: Correction | None
    independence_status: Literal["GUIDED", "VERIFIED_WITHIN_CAPTURE_SCOPE", "UNVERIFIED"]
    provenance_status: Status
    provenance: Provenance

    @model_validator(mode="after")
    def attempt_shape(self):
        distinct(self.assessment, "assessment reference")
        distinct(self.feedback_after_attempt, "feedback reference")
        if self.provenance_status != self.provenance.provenance_status:
            raise ValueError("attempt provenance differs")
        help_ = self.assistance_before_attempt
        if self.independence_status == "VERIFIED_WITHIN_CAPTURE_SCOPE" and (
            help_.coverage != "COMPLETE_IN_CAPTURED_CHANNEL" or not help_.scope
            or help_.classification != "NONE_OBSERVED" or self.provenance_status != "DIRECT_OBSERVATION"
        ):
            raise ValueError("scoped independence requires direct, complete, scoped capture declaration")
        if help_.classification in {"HINTS", "EXPLANATION", "NOT_INDEPENDENT"} and self.independence_status != "GUIDED":
            raise ValueError("assisted attempt must be guided")
        if self.correction_attempt is not None:
            if self.correction_attempt.corrects_attempt_id == self.attempt_id or self.attempt_order == 1:
                raise ValueError("correction must reference a different earlier attempt")
            if self.correction_attempt.kind == "GUIDED_CORRECTION" and self.independence_status != "GUIDED":
                raise ValueError("guided correction cannot claim independence")
        return self


class CriterionDefinition(Model):
    criterion_id: Text
    dimension: Literal["TASK", "SKILL"]
    skill_id: Text | None
    description: Text
    max_points: Points
    award_rule: Literal["BINARY"]
    evidence_requirement: Text
    prerequisite_criteria: list[Text]
    error_family: Text | None
    final_answer_flag: bool

    @model_validator(mode="after")
    def criterion_shape(self):
        if (self.dimension == "SKILL") != (self.skill_id is not None):
            raise ValueError("only skill criterion requires skill id")
        if Decimal(self.max_points) <= 0:
            raise ValueError("criterion maximum must be positive")
        distinct(self.prerequisite_criteria, "prerequisite")
        if self.criterion_id in self.prerequisite_criteria:
            raise ValueError("criterion cannot require itself")
        return self


class SkillDifficultyOverride(Model):
    skill_id: Text
    difficulty: Difficulty
    rationale: Text


class DimensionTotal(Model):
    dimension: Literal["TASK", "SKILL"]
    skill_id: Text | None
    max_points: Points

    @model_validator(mode="after")
    def total_shape(self):
        if (self.dimension == "SKILL") != (self.skill_id is not None) or Decimal(self.max_points) <= 0:
            raise ValueError("invalid dimension total")
        return self


class RubricDefinition(Model):
    rubric_id: Text
    revision: PositiveInt
    revision_reason: Text
    rule_version: Literal["i1-binary-1"]
    task_id: Text
    phase: Phase
    task_statement_ref: EvidenceRef
    domain: Text
    accepted_forms: list[Text] = Field(min_length=1)
    difficulty: Difficulty
    difficulty_rationale: Text
    skill_difficulty_overrides: list[SkillDifficultyOverride]
    task_criteria: list[CriterionDefinition] = Field(min_length=1)
    skill_criteria: list[CriterionDefinition]
    dimension_totals: list[DimensionTotal] = Field(min_length=1)
    final_answer_policy: Text
    error_propagation_policy: Text
    rubric_provenance: Provenance
    declaration_event_ref: Text | None
    content_sha256: Digest

    @model_validator(mode="after")
    def rubric_shape(self):
        criteria = self.task_criteria + self.skill_criteria
        distinct([c.criterion_id for c in criteria], "criterion id within revision")
        if any(c.dimension != "TASK" for c in self.task_criteria) or any(c.dimension != "SKILL" for c in self.skill_criteria):
            raise ValueError("task and skill dimensions must remain separate")
        keys = [(t.dimension, t.skill_id) for t in self.dimension_totals]
        distinct(keys, "dimension total")
        groups = {(c.dimension, c.skill_id) for c in criteria}
        if set(keys) != groups:
            raise ValueError("dimension totals must cover exactly the criterion dimensions")
        for total in self.dimension_totals:
            if exact_sum(c.max_points for c in criteria if (c.dimension, c.skill_id) == (total.dimension, total.skill_id)) != Decimal(total.max_points):
                raise ValueError("criterion maxima do not equal declared dimension total")
        distinct([o.skill_id for o in self.skill_difficulty_overrides], "skill override")
        if any(("SKILL", o.skill_id) not in groups for o in self.skill_difficulty_overrides):
            raise ValueError("override requires a rubric skill dimension")
        return self


class AssessedComponent(Model):
    kind: Literal["INTERMEDIATE", "FINAL", "NOTATION", "STRUCTURE"]
    evidence_ref: EvidenceRef


class CriterionAward(Model):
    criterion_id: Text
    awarded_points: Points | None
    status: Literal["PENDING", "RECORDED"]
    evidence_refs: list[EvidenceRef]
    assessed_components: list[AssessedComponent]
    correctness: Literal["CORRECT", "INCORRECT", "UNKNOWN"]
    deduction_reason: Text | None
    error_refs: list[Text]
    propagation_from: list[Text]
    validator_report_refs: list[Text]
    review_status: ReviewStatus

    @model_validator(mode="after")
    def award_shape(self):
        if (self.status == "PENDING") != (self.awarded_points is None):
            raise ValueError("pending award must be null; recorded award requires points")
        if self.status == "RECORDED" and (not self.evidence_refs or not self.assessed_components):
            raise ValueError("recorded award requires declared concrete evidence")
        return self


class QualitativeObservation(Model):
    provisional_skill_name: Text
    observation: Text
    evidence_refs: list[EvidenceRef]


class Assessment(Model):
    assessment_id: Text
    revision: PositiveInt
    revision_reason: Text
    attempt_id: Text
    rubric_revision_ref: RubricRef
    criterion_awards: list[CriterionAward]
    qualitative_observations: list[QualitativeObservation]
    validator_reports: list[Text]
    review_status: ReviewStatus
    supersedes_assessment_id: Text | None
    provenance: Provenance

    @model_validator(mode="after")
    def assessment_shape(self):
        distinct([a.criterion_id for a in self.criterion_awards], "criterion award")
        if self.supersedes_assessment_id == self.assessment_id:
            raise ValueError("assessment cannot supersede itself")
        return self


class Review(Model):
    review_id: Text
    reviewer_ref: Text
    reviewed_object_hashes: list[Digest] = Field(min_length=1)
    decision: Literal["APPROVE", "REJECT", "REQUEST_CHANGES", "PENDING"]
    reasons: list[Text] = Field(min_length=1)
    recorded_time_claim: TimeClaim
    provenance: Provenance


class EvidenceSidecarV1(Model):
    sidecar_version: Literal["uniflow-evidence-sidecar-i1.0"]
    sidecar_id: Text
    session_id: Text
    handoff_id: Text
    handoff_revision: PositiveInt
    handoff_artifact_sha256: Digest
    result_binding: ResultBinding
    data_classification: Literal["SYNTHETIC", "DECLARED_EVIDENCE"]
    source_artifacts: list[SourceArtifact]
    tasks: list[Task]
    rubrics: list[RubricDefinition]
    events: list[Event]
    attempts: list[Attempt]
    assessments: list[Assessment]
    reviews: list[Review]
    gaps: list[Gap]

    @model_validator(mode="after")
    def internal_structure(self):
        for items, key in ((self.source_artifacts, "artifact_id"), (self.tasks, "task_id"),
                           (self.events, "event_id"), (self.attempts, "attempt_id"),
                           (self.assessments, "assessment_id"), (self.reviews, "review_id"), (self.gaps, "gap_id")):
            distinct([getattr(item, key) for item in items], key)
        distinct([(r.rubric_id, r.revision) for r in self.rubrics], "rubric revision")
        distinct([(a.task_id, a.attempt_order) for a in self.attempts], "attempt order within task")
        distinct([(a.attempt_id, a.revision) for a in self.assessments], "assessment revision within attempt")
        rubrics = {(r.rubric_id, r.revision): r for r in self.rubrics}
        # Only local declared rubric arithmetic; no evidence, V1, task/scope or event resolution.
        for assessment in self.assessments:
            ref = assessment.rubric_revision_ref
            rubric = rubrics.get((ref.rubric_id, ref.revision))
            if rubric is None:
                raise ValueError("assessment requires a locally declared rubric revision for point checks")
            definitions = {c.criterion_id: c for c in rubric.task_criteria + rubric.skill_criteria}
            if {a.criterion_id for a in assessment.criterion_awards} != set(definitions):
                raise ValueError("each criterion needs exactly one award, including explicit PENDING")
            for award in assessment.criterion_awards:
                if award.awarded_points is not None:
                    definition = definitions[award.criterion_id]
                    points, maximum = Decimal(award.awarded_points), Decimal(definition.max_points)
                    if points > maximum or points not in (Decimal(0), maximum):
                        raise ValueError("binary award must equal zero or declared maximum")
                    if points < maximum and not award.deduction_reason:
                        raise ValueError("deduction requires reason")
        return self
