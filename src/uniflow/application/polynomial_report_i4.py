"""Immutable dependency-free I4 information, isolated from schema package imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Literal

Status = Literal['VERIFIED_EQUIVALENT', 'VERIFIED_NOT_EQUIVALENT', 'UNKNOWN', 'UNSUPPORTED', 'ERROR']


@dataclass(frozen=True, slots=True)
class PolynomialLimits:
    input_bytes: int = 4096
    tokens: int = 512
    nodes: int = 512
    operators: int = 256
    depth: int = 32
    literal_digits: int = 128
    exponent: int = 32
    degree: int = 64
    coefficient_bits: int = 2048
    normal_form_bits: int = 16384
    arithmetic_work: int = 20000
    instructions: int = 4096
    output_bytes: int = 65536
    request_bytes: int = 16384


LIMITS = PolynomialLimits()


@dataclass(frozen=True, slots=True)
class InputIdentity:
    role: str
    sha256: str | None
    byte_length: int


@dataclass(frozen=True, slots=True)
class Coefficient:
    power: int
    numerator: str
    denominator: str


@dataclass(frozen=True, slots=True)
class NormalForm:
    coefficients: tuple[Coefficient, ...]


@dataclass(frozen=True, slots=True)
class ResourceUse:
    """Completed-phase counts; work is a conservative plan estimate, not timing.

    Zero in an incomplete phase means unavailable, not zero CPU/memory use.
    """
    tokens: int = 0
    nodes: int = 0
    operators: int = 0
    arithmetic_work: int = 0
    instructions: int = 0
    parsing_complete: bool = False
    planning_complete: bool = False


@dataclass(frozen=True, slots=True)
class PolynomialReport:
    status: Status
    code: str
    reason: str
    operation: str
    inputs: tuple[InputIdentity, ...]
    context_id: str
    context_revision: str
    context_sha256: str
    resources: ResourceUse
    left_normal: NormalForm | None = None
    right_normal: NormalForm | None = None
    dependencies: tuple[tuple[str, str], ...] = ()
    contract_version: str = 'I4-POLYNOMIAL-REPORT-1'
    validator_version: str = 'I4-ISOLATED-1'
    grammar_version: str = 'I4-POLY-ASCII-1'
    method: str = 'EXACT_QQ_CANONICAL_COEFFICIENTS_V1'
    mathematical_scope: str = 'QQ[x]; polynomial functions R -> R'
    variable: str = 'x'
    domain: str = 'R'
    limits: PolynomialLimits = LIMITS
    human_review_required: bool = True
    production_admitted: bool = False
    s09_status: str = 'NOT_VERIFIED'
    limitations: tuple[str, ...] = (
        'Synthetic isolated use only; no OS sandbox or hard time/memory guarantee.',
        'Input hashes bind bytes, not authenticity or an earlier human approval.',
        'Polynomial identity only; no scoring, mastery, provenance or review decision.',
        'Conservative budgets may abstain even when cancellation yields a small polynomial.',
    )

    def to_dict(self) -> dict:
        """Owned mutable copy; the report contains only immutable values."""
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True,
                          allow_nan=False, separators=(',', ':'))
