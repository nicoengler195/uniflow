"""Pure text and safe error translation for detached DTOs."""
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.write_lock import WriterLockTimeout


def format_date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "Date unknown"


def format_time(value: datetime | None, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%d %b %Y · %H:%M") if value else "Time unknown"


def label(value: str) -> str:
    return value.replace("_", " ").title()


def mastery(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value:.0%} known mastery"


def information_status(value: str) -> str:
    return value.upper() if value.upper() in {"CONFIRMED", "OBSERVED", "INFERRED", "UNKNOWN"} else label(value)


@dataclass(frozen=True)
class PresentationError:
    code: str
    title: str
    message: str
    retryable: bool = False


def present_error(exc: Exception) -> PresentationError:
    if isinstance(exc, ValueError):
        code = str(exc)
        if code.startswith('STUDY_BLOCK_SCHEMA_REQUIRED'):
            return PresentationError('STUDY_BLOCK_SCHEMA_REQUIRED', 'Lernblöcke gesperrt',
                'Dieses Schema unterstützt keine B1-Schreibaktionen. Eine ausdrücklich durchgeführte Migration auf 0020 ist erforderlich.')
        if code in {'STALE_PREVIEW_RECHECK_REQUIRED', 'SELECTION_PREVIEW_RECHECK_REQUIRED'}:
            return PresentationError(code, 'Vorschau veraltet', 'Termindaten oder Lernbedarfe haben sich geändert. Vorschau erneut prüfen und ausdrücklich bestätigen.')
        if code == 'REQUEST_PAYLOAD_MISMATCH':
            return PresentationError(code, 'Request-Konflikt', 'Diese Request-ID gehört zu einer anderen Bestätigung. Persistenten Status prüfen.')
        if any(token in code for token in ('PERSONAL_LOCAL_TIME', 'INVALID_LOCAL_INTERVAL', 'OUTSIDE_SEMESTER_OR_INVALID_INTERVAL')):
            return PresentationError(code, 'Lernzeit prüfen', 'Ein gültiges, eindeutiges Zeitfenster innerhalb des Semesters wählen; Zeitumstellung beachten.')
        if code in {"SUPPORTED_MATERIAL_TYPE_REQUIRED", "INVALID_SOURCE"}:
            return PresentationError(code, "Material file unavailable",
                "Choose an existing PDF, TXT, or Markdown file.")
        if code == "MATERIAL_IMPORT_CONFLICT":
            return PresentationError(code, "Material import conflict",
                "This content is already linked with different material details. Review the existing Material before importing again.")
        if code in {"SKILL_INPUT_INVALID", "SKILL_QUOTE_MUST_MATCH_ONE_PARSED_UNIT"}:
            return PresentationError(code, "Skill-Auswahl prüfen",
                "Bitte einen Namen angeben und eine Passage direkt innerhalb einer geparsten Seite markieren.")
        if code == "SKILL_SELECTION_INVALID":
            return PresentationError(code, "Textauswahl ungültig",
                "Bitte eine nicht leere Passage innerhalb einer Seite wählen (höchstens 4096 UTF-8-Bytes).")
        if code == "SKILL_SELECTION_SOURCE_CHANGED":
            return PresentationError(code, "Material hat sich geändert",
                "Die ausgewählte Seite gehört nicht mehr zur aktuellen Materialversion. Detailansicht aktualisieren und erneut auswählen.")
        if code in {"MATERIAL_COURSE_REQUIRED", "MATERIAL_SOURCE_REQUIRED", "PARSED_MATERIAL_REQUIRED",
                "SKILL_COURSE_INVALID"}:
            return PresentationError(code, "Learning source unavailable",
                "Confirm the material course, parse its current source, and choose an active Skill.")
        if code in {"REVIEW_PROPOSAL_NOT_FOUND", "PROPOSAL_NOT_IN_SEMESTER"}:
            return PresentationError(code, "Proposal unavailable",
                "This proposal is not available in the selected Semester. Return to Materials and refresh.")
        if code in {"REVIEW_EVIDENCE_INVALID", "REVIEW_SOURCE_MISSING", "REVIEW_RUN_MISSING",
                "REVIEW_ATTEMPT_INVALID"}:
            return PresentationError(code, "Evidence unavailable",
                "Stored review evidence could not be verified. Review the project data before deciding.")
        if code in {"INVALID_SKILL_CHOICE_QUERY", "INVALID_SKILL_CHOICE_LIMIT"}:
            return PresentationError(code, "Check Skill search",
                "Search with at most 120 characters and try again.")
        if code in {"INVALID_MATERIAL_INBOX_RANGE", "MATERIAL_INBOX_RANGE_TOO_LARGE",
                "INVALID_MATERIAL_DETAIL_RANGE", "MATERIAL_DETAIL_RANGE_TOO_LARGE"}:
            return PresentationError(code, "Check Materials dates",
                "Choose an ordered From and Through range of at most 366 days.")
        if code in {"UNKNOWN_SEMESTER", "UNKNOWN_COURSE_IN_SEMESTER"}:
            return PresentationError(code, "Materials scope unavailable",
                "The selected Semester or Course is no longer available. Refresh Materials.")
        if code in {"UNKNOWN_MATERIAL", "MATERIAL_NOT_IN_SEMESTER"}:
            return PresentationError(code, "Material unavailable",
                "The selected Material is no longer in this Semester. Return to Materials and refresh.")
    if isinstance(exc, WriterLockTimeout):
        return PresentationError("PROJECT_BUSY", "Project busy", "Another UniFlow write is in progress. Retry when it finishes.", True)
    if isinstance(exc, WorkflowCommandError):
        code = exc.code
        if code == "RESULT_SCORING_FORMAT_SELECTED":
            return PresentationError(code, "Anderes Ergebnisformat ausgewählt",
                "Import Result erwartet LearningResultV2 (schema_version 2.0, subject-learning-v1). "
                "Für ScoringResultV1 den Einstieg Detailliertes Scoring-Review öffnen (V1) verwenden. "
                "Ein Prüfentwurf ist auch dort nicht importierbar; fehlende Pflichtangaben zuerst klären.")
        review_errors = {
            "REVIEW_INPUT_INVALID": ("Check review input", "Review the selected action and reason, then try again."),
            "REVIEW_PROPOSAL_NOT_FOUND": ("Proposal unavailable", "The proposal is no longer available. Refresh Materials."),
            "PROPOSAL_NOT_IN_SEMESTER": ("Proposal outside Semester", "This proposal is not in the selected Semester."),
            "REVIEW_PROPOSAL_NOT_PENDING": ("Proposal state changed", "The proposal was decided or superseded. Review its refreshed history."),
            "REVIEW_CONTEXT_CHANGED": ("Review context changed", "The proposal or its source changed. Review the refreshed proposal before deciding again."),
            "REVIEW_TARGET_CONFLICT": ("Decision target changed", "The recorded decision does not match the requested target. Review the refreshed history."),
            "REVIEW_EVIDENCE_INVALID": ("Evidence unavailable", "Stored review evidence could not be verified."),
            "REVIEW_SKILL_NOT_ACTIVE": ("Skill unavailable", "The selected Skill is no longer active. Search again and choose another target."),
            "REVIEW_REASON_CONFLICT": ("Rejection already recorded", "The recorded rejection has a different reason. Review its refreshed history."),
            "REVIEW_ACTION_UNSUPPORTED": ("Decision unavailable", "This proposal type can be viewed here but cannot be decided from this screen."),
        }
        if code in review_errors:
            title, message = review_errors[code]
            return PresentationError(code, title, message)
        if code in {"UNKNOWN_LEARNING_SESSION", "HANDOFF_NOT_FOUND", "RESULT_NOT_FOUND"}:
            return PresentationError(code, "Learning item not found", "The selected Session or Result is no longer available. Refresh Sessions.")
        if code in {"INVALID_SESSION_STATE", "HANDOFF_FROZEN", "RESULT_PROCESSING_STATE_INVALID",
                "EXCHANGE_HANDOFF_NOT_CURRENT", "RESULT_HANDOFF_MISSING", "RESULT_HANDOFF_MISMATCH",
                "RESULT_HANDOFF_ARTIFACT_INVALID"}:
            return PresentationError(code, "Session state changed", "Refresh this Session and review its current state before acting again.")
        if code in {"HANDOFF_CONTEXT_CHANGED", "RESULT_CONTEXT_CHANGED", "INVALID_SESSION_COURSE",
                "INVALID_SEMESTER_STATE", "INVALID_TARGET_SKILL", "TARGET_COURSESKILL_REQUIRED",
                "SKILL_GRAPH_INVALID", "DUPLICATE_TARGET_SKILL", "INVALID_SESSION_GOAL",
                "INVALID_PLANNED_TIME", "HANDOFF_TOO_LARGE", "HANDOFF_UNSAFE_PATH"}:
            return PresentationError(code, "Learning context changed", "Refresh the Session and review its Course and Skills before acting again.")
        if code in {"HANDOFF_ARTIFACT_CONFLICT", "RESULT_ARTIFACT_CONFLICT", "RESULT_EVIDENCE_INVALID",
                "MASTERY_PROVENANCE_CONFLICT", "MASTERY_PERSISTENCE_FAILED"}:
            return PresentationError(code, "Stored learning data needs review", "UniFlow found a conflict in stored learning data. Review the project before continuing.")
        if code in {"RESULT_FILE_NOT_FOUND", "RESULT_FILE_INVALID", "RESULT_FILE_UNREADABLE",
                "RESULT_TOO_LARGE", "RESULT_DUPLICATE_JSON_KEY", "RESULT_INVALID_JSON",
                "RESULT_FILENAME_MISMATCH"}:
            return PresentationError(code, "Result file invalid", "The selected Result file could not be imported. Choose another file or obtain a corrected Result.")
        if code in {"RESULT_SESSION_MISMATCH", "RESULT_COURSE_MISMATCH", "RESULT_HANDOFF_HASH_MISMATCH",
                "RESULT_PROTOCOL_MISMATCH", "RESULT_SKILL_NOT_ALLOWED", "RESULT_CONFLICT"}:
            return PresentationError(code, "Result does not match", "This Result does not match the current Session and learning package. Review the exported package and returned file.")
        if code in {"EXCHANGE_DESTINATION_INVALID", "EXCHANGE_DESTINATION_CONFLICT"}:
            return PresentationError(code, "Export destination unavailable", "Choose another existing parent directory. UniFlow will not overwrite conflicting files.")
        if code == "EXCHANGE_IO_FAILED":
            return PresentationError(code, "Export could not finish", "Check the selected directory or choose another one, then try again.", True)
        if code == "UNKNOWN_PLAN_BLOCK":
            return PresentationError(code, "Plan block unavailable", "The selected block is no longer available. Refresh Week and try again.")
        if code == "PLAN_ALREADY_EXISTS":
            return PresentationError(code, "Plan already exists", "The existing plan is being loaded.")
        if code == "REVISION_CHANGED":
            return PresentationError(code, "Plan changed", "The plan changed since this Week view was loaded. Week is refreshing; perform the action again if you still want it.")
        if code in {"ACCEPTED_REVISION_CHANGED", "REVISION_NOT_CURRENT"}:
            return PresentationError(code, "Plan changed", "The plan revision changed. Week is refreshing; review it before trying again.")
        if code in {"PLAN_ACCEPTANCE_STALE", "PLAN_ACCEPTANCE_INVALID"}:
            factors = exc.details.get("factors", [])
            reason = ", ".join(label(item) for item in factors[:4])
            return PresentationError(code, "Cannot accept draft", "The revision assessment is " +
                ("stale" if code.endswith("STALE") else "invalid") +
                (f" ({reason})." if reason else ".") + " Review the plan before trying again.")
        return PresentationError("APPLICATION_FAILURE", "Application error",
            "UniFlow could not complete the operation. Check the local log and retry.", True)
    return PresentationError("APPLICATION_FAILURE", "Application error", "UniFlow could not complete the operation. Check the local log and retry.", True)
