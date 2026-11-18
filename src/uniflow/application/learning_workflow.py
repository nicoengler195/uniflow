"""GUI-independent, thin command facade for the explicit learning lifecycle."""
from __future__ import annotations

from uniflow.application import learning_handoffs, learning_results, learning_result_mastery
from uniflow.application.workflow_errors import WorkflowCommandError, normalize_known, parse_request
from uniflow.schemas.workflow_v1 import (
    ApplyMasteryRequestV1, ApplyMasterySuccessV1, CreateSessionRequestV1,
    CreateSessionSuccessV1, ExportExchangeRequestV1, GenerateHandoffRequestV1,
    GenerateHandoffSuccessV1, ImportResultFileRequestV1, ImportResultFileSuccessV1,
    StartSessionRequestV1, StartSessionSuccessV1,
)

CREATE_ERRORS = frozenset({"INVALID_SESSION_GOAL", "INVALID_TARGET_SKILL", "HANDOFF_TOO_LARGE",
    "DUPLICATE_TARGET_SKILL", "INVALID_PLANNED_TIME", "INVALID_SESSION_COURSE",
    "INVALID_SEMESTER_STATE", "TARGET_COURSESKILL_REQUIRED"})
GENERATE_ERRORS = frozenset({"UNKNOWN_LEARNING_SESSION", "HANDOFF_FROZEN", "INVALID_SESSION_STATE",
    "INVALID_SESSION_COURSE", "INVALID_SEMESTER_STATE", "INVALID_TARGET_SKILL",
    "TARGET_COURSESKILL_REQUIRED", "SKILL_GRAPH_INVALID", "HANDOFF_TOO_LARGE",
    "HANDOFF_UNSAFE_PATH", "HANDOFF_ARTIFACT_CONFLICT", "HANDOFF_CONTEXT_CHANGED"})
START_ERRORS = frozenset({"UNKNOWN_LEARNING_SESSION", "INVALID_SESSION_STATE",
    "RESULT_HANDOFF_MISSING", "RESULT_HANDOFF_MISMATCH", "RESULT_HANDOFF_ARTIFACT_INVALID"})
IMPORT_ERRORS = frozenset({"RESULT_SCORING_FORMAT_SELECTED", "RESULT_TOO_LARGE", "RESULT_DUPLICATE_JSON_KEY", "RESULT_INVALID_JSON",
    "RESULT_FILENAME_MISMATCH", "RESULT_HANDOFF_MISSING", "RESULT_HANDOFF_MISMATCH",
    "RESULT_HANDOFF_ARTIFACT_INVALID", "RESULT_SESSION_MISMATCH", "RESULT_COURSE_MISMATCH",
    "RESULT_HANDOFF_HASH_MISMATCH", "RESULT_PROTOCOL_MISMATCH", "RESULT_SKILL_NOT_ALLOWED",
    "RESULT_CONFLICT", "RESULT_ARTIFACT_CONFLICT", "RESULT_CONTEXT_CHANGED", "INVALID_SESSION_STATE"})
APPLY_ERRORS = frozenset({"RESULT_NOT_FOUND", "RESULT_HANDOFF_MISMATCH", "RESULT_ARTIFACT_CONFLICT",
    "RESULT_EVIDENCE_INVALID", "MASTERY_PROVENANCE_CONFLICT", "RESULT_PROCESSING_STATE_INVALID",
    "INVALID_SESSION_STATE", "MASTERY_PERSISTENCE_FAILED"})


def create_session(config, request: CreateSessionRequestV1 | dict) -> CreateSessionSuccessV1:
    request = parse_request(CreateSessionRequestV1, request)
    try:
        session_id = learning_handoffs.create_learning_session(config, request.course_id, request.goal,
            request.target_skill_ids, request.planned_for)
    except ValueError as exc:
        normalize_known(exc, CREATE_ERRORS)
    return CreateSessionSuccessV1(session_id=session_id)


def generate_handoff(config, request: GenerateHandoffRequestV1 | dict) -> GenerateHandoffSuccessV1:
    request = parse_request(GenerateHandoffRequestV1, request)
    try:
        outcome = learning_handoffs.generate_handoff(config, request.session_id)
    except ValueError as exc:
        normalize_known(exc, GENERATE_ERRORS)
    return GenerateHandoffSuccessV1(outcome=outcome.status, handoff_id=outcome.handoff_id,
        session_id=outcome.session_id, revision=outcome.revision, artifact_sha256=outcome.artifact_sha256)


def export_exchange(config, request: ExportExchangeRequestV1 | dict):
    from uniflow.application.learning_exchange import export_learning_exchange
    request = parse_request(ExportExchangeRequestV1, request)
    return export_learning_exchange(config, request)


def export_learning_chat(config, request: ExportExchangeRequestV1 | dict):
    """Export one self-describing scoring handoff without changing the V2 export."""
    from uniflow.application.learning_chat_exchange import export_learning_chat_package
    request = parse_request(ExportExchangeRequestV1, request)
    return export_learning_chat_package(config, request.handoff_id, request.destination_directory)


def preview_scoring_result(config, path):
    """Validate and calculate an uncommitted ScoringResultV1 preview."""
    from uniflow.application.learning_chat_exchange import preview_scoring_result as preview
    return preview(config, path)


def start_session(config, request: StartSessionRequestV1 | dict) -> StartSessionSuccessV1:
    request = parse_request(StartSessionRequestV1, request)
    try:
        outcome = learning_results.start_learning_session(config, request.session_id)
    except ValueError as exc:
        normalize_known(exc, START_ERRORS)
    return StartSessionSuccessV1(outcome=outcome, session_id=request.session_id)


def import_result_file(config, request: ImportResultFileRequestV1 | dict) -> ImportResultFileSuccessV1:
    request = parse_request(ImportResultFileRequestV1, request)
    if request.path.is_dir():
        raise WorkflowCommandError("RESULT_FILE_INVALID")
    try:
        outcome = learning_results.import_learning_result(config, request.path)
    except FileNotFoundError as exc:
        raise WorkflowCommandError("RESULT_FILE_NOT_FOUND") from exc
    except IsADirectoryError as exc:
        raise WorkflowCommandError("RESULT_FILE_INVALID") from exc
    except PermissionError as exc:
        raise WorkflowCommandError("RESULT_FILE_UNREADABLE") from exc
    except ValueError as exc:
        normalize_known(exc, IMPORT_ERRORS)
    return ImportResultFileSuccessV1(outcome=outcome.status, result_id=outcome.result_id,
        artifact_sha256=outcome.artifact_sha256, evidence_count=outcome.evidence_count)


def apply_mastery(config, request: ApplyMasteryRequestV1 | dict) -> ApplyMasterySuccessV1:
    request = parse_request(ApplyMasteryRequestV1, request)
    try:
        outcome = learning_result_mastery.apply_learning_result_mastery(config, request.result_id)
    except ValueError as exc:
        normalize_known(exc, APPLY_ERRORS)
    return ApplyMasterySuccessV1(outcome=outcome.status, result_id=outcome.result_id,
        session_id=outcome.session_id, created_mastery_state_ids=outcome.created_mastery_state_ids,
        skipped_skill_ids=outcome.skipped_skill_ids)
