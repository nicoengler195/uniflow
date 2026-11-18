"""The only GUI-facing mutation boundary for first-scope proposal review.

Owning Phase 3 services retain their writer locks and transactions.
"""
from __future__ import annotations

from uniflow.application import academic_classification, skill_extraction_foundation
from uniflow.application.review_read_models import get_classification_review, get_skill_review
from uniflow.application.workflow_errors import WorkflowCommandError, parse_request
from uniflow.schemas.review_workflow_v1 import (
    ClassificationAcceptRequestV1, ClassificationRejectRequestV1, ReviewSuccessV1,
    SkillAcceptNewRequestV1, SkillRejectRequestV1, SkillResolveExistingRequestV1,
)

CLASSIFICATION_TARGET_ERRORS = frozenset({"MISSING_ARTIFACT", "MISSING_COURSE",
    "CONFLICT_REQUIRES_CORRECTION", "MATERIAL_STABLE_ID_EXISTS_REQUIRES_EXPLICIT_TRANSITION",
    "REPLACEMENT_LINK_NOT_ACTIVE", "ARTIFACT_ALREADY_IN_HISTORY", "CONFLICT_VERSION_REQUIRED",
    "REPOSITORY_TARGET_CONFLICT", "EXPECTATION_TARGET_CONFLICT", "INVALID_EXPECTATION_IDENTITY",
    "INVALID_EXPECTATION_OCCURRENCE", "PROPOSAL_REJECTED", "PROPOSAL_SUPERSEDED"})
SKILL_CONTEXT_ERRORS = frozenset({"ACADEMIC_SCOPE_CHANGED", "SOURCE_CHANGED",
    "EVIDENCE_RUN_MISMATCH", "EVIDENCE_SOURCE_MISMATCH", "EVIDENCE_HASH_MISMATCH",
    "INVALID_DOCUMENT_RANGE", "INVALID_REPOSITORY_RANGE", "UNKNOWN_UNIT",
    "UNKNOWN_REPOSITORY_FILE", "PROPOSAL_NOT_PENDING"})
SKILL_TARGET_ERRORS = frozenset({"UNKNOWN_SKILL", "SKILL_NOT_ACTIVE", "SKILL_ID_CONFLICT",
    "INVALID_SKILL_KIND"})
READ_ERRORS = frozenset({"REVIEW_PROPOSAL_NOT_FOUND", "PROPOSAL_NOT_IN_SEMESTER",
    "UNKNOWN_SEMESTER",
    "REVIEW_SOURCE_MISSING", "REVIEW_RUN_MISSING", "REVIEW_ATTEMPT_INVALID",
    "REVIEW_EVIDENCE_INVALID"})
COMMAND_ERRORS = {
    "review.classification.accept": READ_ERRORS | CLASSIFICATION_TARGET_ERRORS |
        {"REVIEW_CONTEXT_CHANGED"},
    "review.classification.reject": READ_ERRORS | {"REVIEW_CONTEXT_CHANGED",
        "REJECTION_REASON_CONFLICT", "PROPOSAL_ACCEPTED", "PROPOSAL_SUPERSEDED"},
    "review.skill.accept_new": READ_ERRORS | SKILL_CONTEXT_ERRORS |
        {"REVIEW_CONTEXT_CHANGED", "UNKNOWN_PROPOSAL", "INVALID_SKILL_KIND", "SKILL_ID_CONFLICT"},
    "review.skill.resolve_existing": READ_ERRORS | SKILL_CONTEXT_ERRORS | SKILL_TARGET_ERRORS |
        {"REVIEW_CONTEXT_CHANGED", "UNKNOWN_PROPOSAL"},
    "review.skill.reject": READ_ERRORS | {"REVIEW_CONTEXT_CHANGED", "UNKNOWN_PROPOSAL",
        "PROPOSAL_NOT_PENDING"},
}


def _request(model, request):
    try:
        return parse_request(model, request)
    except WorkflowCommandError as exc:
        if exc.code != "WORKFLOW_INPUT_INVALID":
            raise
        raise WorkflowCommandError("REVIEW_INPUT_INVALID", details=exc.details) from exc


def _known(exc: ValueError, command: str):
    value = str(exc)
    if value not in COMMAND_ERRORS[command]:
        raise exc
    if value == "REVIEW_CONTEXT_CHANGED":
        raise WorkflowCommandError("REVIEW_CONTEXT_CHANGED") from exc
    if value == "REJECTION_REASON_CONFLICT":
        raise WorkflowCommandError("REVIEW_REASON_CONFLICT") from exc
    if value in {"REVIEW_PROPOSAL_NOT_FOUND", "PROPOSAL_NOT_IN_SEMESTER"}:
        raise WorkflowCommandError(value) from exc
    if value == "UNKNOWN_SEMESTER":
        raise WorkflowCommandError("REVIEW_TARGET_CONFLICT") from exc
    if value == "UNKNOWN_PROPOSAL":
        raise WorkflowCommandError("REVIEW_PROPOSAL_NOT_FOUND") from exc
    if value in {"PROPOSAL_ACCEPTED", "PROPOSAL_REJECTED", "PROPOSAL_SUPERSEDED"}:
        raise WorkflowCommandError("REVIEW_PROPOSAL_NOT_PENDING") from exc
    if value in {"REVIEW_SOURCE_MISSING", "REVIEW_RUN_MISSING", "REVIEW_ATTEMPT_INVALID"}:
        raise WorkflowCommandError("REVIEW_EVIDENCE_INVALID") from exc
    if value == "REVIEW_EVIDENCE_INVALID" or value in SKILL_CONTEXT_ERRORS:
        raise WorkflowCommandError("REVIEW_EVIDENCE_INVALID" if value != "PROPOSAL_NOT_PENDING"
            else "REVIEW_PROPOSAL_NOT_PENDING") from exc
    if value in CLASSIFICATION_TARGET_ERRORS and command.startswith("review.classification"):
        raise WorkflowCommandError("REVIEW_PROPOSAL_NOT_PENDING" if value.startswith("PROPOSAL_")
            else "REVIEW_TARGET_CONFLICT") from exc
    if value in SKILL_TARGET_ERRORS and command.startswith("review.skill"):
        raise WorkflowCommandError("REVIEW_SKILL_NOT_ACTIVE" if value in {"UNKNOWN_SKILL", "SKILL_NOT_ACTIVE"}
            else "REVIEW_TARGET_CONFLICT") from exc
    raise exc


def _classification(config, request, model, command):
    value = _request(model, request)
    try:
        review = get_classification_review(config, value.semester_id, value.proposal_id)
    except ValueError as exc:
        _known(exc, command)
    if review.proposal.status == "PENDING":
        if review.review_context_token != value.expected_review_context_token:
            raise WorkflowCommandError("REVIEW_CONTEXT_CHANGED")
    elif review.proposal.status not in {"ACCEPTED" if command.endswith("accept") else "REJECTED"}:
        raise WorkflowCommandError("REVIEW_PROPOSAL_NOT_PENDING")
    try:
        if command.endswith("accept"):
            action = academic_classification.accept_classification_proposal(config, value.proposal_id,
                reason=value.reason, expected_review_context_token=value.expected_review_context_token)
            replay = review.proposal.status == "ACCEPTED" or action == "ACCEPTED"
            return ReviewSuccessV1(command=command, outcome="ALREADY_ACCEPTED" if replay else "ACCEPTED",
                proposal_id=value.proposal_id, status="ACCEPTED", decision_action=action)
        action = academic_classification.reject_classification_proposal(config, value.proposal_id,
            reason=value.reason, expected_review_context_token=value.expected_review_context_token)
        return ReviewSuccessV1(command=command, outcome="ALREADY_REJECTED" if action == "ALREADY_REJECTED" else "REJECTED",
            proposal_id=value.proposal_id, status="REJECTED", decision_action=action)
    except ValueError as exc:
        _known(exc, command)


def accept_classification(config, request) -> ReviewSuccessV1:
    return _classification(config, request, ClassificationAcceptRequestV1, "review.classification.accept")


def reject_classification(config, request) -> ReviewSuccessV1:
    return _classification(config, request, ClassificationRejectRequestV1, "review.classification.reject")


def _skill(config, request, model, command):
    value = _request(model, request)
    try:
        review = get_skill_review(config, value.semester_id, value.proposal_id)
    except ValueError as exc:
        _known(exc, command)
    p = review.proposal
    if p.proposal_kind != "NEW_SKILL":
        raise WorkflowCommandError("REVIEW_ACTION_UNSUPPORTED")
    if p.status == "PENDING":
        if review.review_context_token != value.expected_review_context_token:
            raise WorkflowCommandError("REVIEW_CONTEXT_CHANGED")
    elif p.status not in {"REJECTED" if command.endswith("reject") else "ACCEPTED"}:
        raise WorkflowCommandError("REVIEW_PROPOSAL_NOT_PENDING")
    if command.endswith("reject"):
        if p.status == "REJECTED":
            if p.decision_reason != value.reason:
                raise WorkflowCommandError("REVIEW_REASON_CONFLICT")
            return ReviewSuccessV1(command=command, outcome="ALREADY_REJECTED",
                proposal_id=p.proposal_id, status="REJECTED")
        try:
            outcome = skill_extraction_foundation.reject_skill_proposal(config, p.proposal_id, value.reason,
                expected_review_context_token=value.expected_review_context_token)
            return ReviewSuccessV1(command=command, outcome=outcome, proposal_id=p.proposal_id,
                status="REJECTED")
        except ValueError as exc:
            _known(exc, command)
    wanted = "CREATE_SKILL" if command.endswith("accept_new") else "RESOLVE_TO_EXISTING_SKILL"
    target = value.existing_skill_id if wanted == "RESOLVE_TO_EXISTING_SKILL" else None
    if p.status == "ACCEPTED" and (p.decision_action != wanted or
            (target is not None and p.accepted_skill_id != target)):
        raise WorkflowCommandError("REVIEW_TARGET_CONFLICT")
    try:
        decision = skill_extraction_foundation.accept_skill_proposal(config, p.proposal_id,
            existing_skill_id=target, expected_review_context_token=value.expected_review_context_token)
    except ValueError as exc:
        _known(exc, command)
    if decision.action == "HISTORICAL":
        if p.decision_action != wanted or (target is not None and decision.target_id != target):
            raise WorkflowCommandError("REVIEW_TARGET_CONFLICT")
    elif decision.action != wanted:
        raise WorkflowCommandError("REVIEW_TARGET_CONFLICT")
    return ReviewSuccessV1(command=command,
        outcome="ALREADY_ACCEPTED" if p.status == "ACCEPTED" or decision.action == "HISTORICAL" else "ACCEPTED",
        proposal_id=p.proposal_id, status="ACCEPTED", decision_action=wanted,
        accepted_skill_id=decision.target_id)


def accept_new_skill(config, request) -> ReviewSuccessV1:
    return _skill(config, request, SkillAcceptNewRequestV1, "review.skill.accept_new")


def resolve_existing_skill(config, request) -> ReviewSuccessV1:
    return _skill(config, request, SkillResolveExistingRequestV1, "review.skill.resolve_existing")


def reject_skill(config, request) -> ReviewSuccessV1:
    return _skill(config, request, SkillRejectRequestV1, "review.skill.reject")
