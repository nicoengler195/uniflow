"""Separate immutable I5 overlay: claims, byte association and trust stay distinct."""
from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True, slots=True)
class OperandLink:
    role: str
    evidence_index: int
    start: int  # UTF-8 offsets relative to the explicitly selected I3 evidence
    end: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MathSubmission:
    report_id: str
    report_bytes: bytes
    report_sha256: str
    projection_sha256: str
    i3_context_sha256: str
    i4_context_id: str
    i4_context_revision: str
    i4_context_sha256: str
    task_id: str | None
    attempt_id: str | None
    assessment_id: str | None
    operands: tuple[OperandLink, ...]
    synthetic_test_report: bool = False
    declared_origin: str = 'UNVERIFIED'


@dataclass(frozen=True, slots=True)
class CheckedSpan:
    role: str
    evidence_index: int
    artifact_id: str
    artifact_sha256: str
    original_byte_start: int
    original_byte_end: int
    input_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class MathReviewEntry:
    report_id: str
    report_sha256: str
    raw_report_hex: str
    report_json: str | None
    declared_task_id: str | None
    declared_attempt_id: str | None
    declared_assessment_id: str | None
    association: str
    diagnostics: tuple[str, ...]
    claimed_math_status: str | None
    claimed_operation: str | None
    claimed_domain: str | None
    claimed_normal_forms_json: str | None
    checked_spans: tuple[CheckedSpan, ...] = ()
    associated_task_id: str | None = None
    associated_attempt_id: str | None = None
    associated_assessment_id: str | None = None
    i3_finding_indices: tuple[int, ...] = ()
    i3_finding_statuses: tuple[str, ...] = ()
    declared_origin: str = 'UNVERIFIED'
    execution_origin: str = 'UNVERIFIED'
    independent_execution_proven: bool = False
    human_review_required: bool = True
    score_effect: str = 'NONE'


@dataclass(frozen=True, slots=True)
class MathReviewOverlay:
    status: str
    original_i3_json: str | None
    original_i3_sha256: str | None
    i3_mode: str | None
    entries: tuple[MathReviewEntry, ...]
    diagnostics: tuple[str, ...]
    complete: bool
    overlay_version: str = 'uniflow-math-review-overlay-i5.0'
    policy_version: str = 'i5-claim-association-trust-1'
    human_review_required: bool = True
    production_admitted: bool = False
    s09_status: str = 'NOT_VERIFIED'
    execution_origin: str = 'UNVERIFIED'
    display_format: str = 'PLAIN_TEXT_JSON'
    review_questions: tuple[str, ...] = (
        'Which explicitly selected answer/subspan belongs to the intended task?',
        'Is the supplied mathematical claim authentic? Execution origin is not proven here.',
        'Does the limited operation/domain address the whole task or only an intermediate step?',
        'Which I3/I2 conflicts, unknowns and provenance limitations still need human review?',
        'How does the rubric evaluate the answer? No mathematical claim changes a score.',
    )
    notices: tuple[str, ...] = (
        'BOUND_BYTES means local byte consistency, never authenticated mathematical proof.',
        'All original I3 values, findings and order remain in original_i3_json without replacement.',
        'No CAS call, answer selection, scoring, mastery, persistence or network/file lookup.',
    )

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        text = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True,
                          allow_nan=False, separators=(',', ':'))
        return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
