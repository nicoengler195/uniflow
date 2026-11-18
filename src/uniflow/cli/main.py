from __future__ import annotations

from pathlib import Path
from dataclasses import asdict
from datetime import date, datetime, timedelta
from threading import Event

import yaml

import typer

from uniflow.application.doctor import run_doctor
from uniflow.application.audit_import import import_audit, load_audit
from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material, list_materials
from uniflow.application.drop_watcher import drop_status as get_drop_status, scan_drop, watch_drop, list_drop_jobs
from uniflow.application.material_manifest import generate_material_manifest
from uniflow.application.document_parsing import parse_source_artifact, list_parse_runs, get_parse_run
from uniflow.application.repository_snapshots import (register_repository, snapshot_repository,
    list_repositories, repository_info, list_snapshots, read_repository_snapshot)
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import database_info
from uniflow.application.academic_classification import (
    create_manual_artifact_proposal, create_manual_repository_proposal,
    create_manual_expectation_proposal, accept_classification_proposal,
    reject_classification_proposal, correct_academic_link,
    list_classification_proposals, get_classification_proposal,
    list_unclassified_artifacts, list_unlinked_repositories,
    get_material_expectation_links, get_link_history,
)

app = typer.Typer(help="UniFlow local-first semester data commands.")

from uniflow.application.skill_extraction_foundation import (
    create_semantic_run, build_semantic_input, create_semantic_evidence,
    create_skill_proposal, accept_skill_proposal, reject_skill_proposal,
    supersede_skill_proposal, deactivate_skill_relation, get_skill_proposal,
    list_skill_proposals, list_skills, get_skill, list_skill_evidence,
    list_skill_aliases, list_course_skills, list_skill_dependencies,
)
from uniflow.application.semantic_extraction_runtime import (
    execute_document_extraction, list_semantic_attempts, get_semantic_attempt,
)
from uniflow.application.repository_semantic_extraction import (
    execute_repository_extraction, list_repository_semantic_files, preview_external_repository_extraction,
)
import json
from uniflow.application.learning_handoffs import (
    create_learning_session, learning_session_info, generate_handoff,
    handoff_info, export_handoff,
)
from uniflow.application.learning_results import (
    start_learning_session, import_learning_result, learning_result_info,
)
from uniflow.application.learning_result_mastery import apply_learning_result_mastery
from uniflow.application.learning_chat_exchange import (
    SCORING_RESULT_MESSAGES, export_learning_chat_package, preview_scoring_result,
)
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.application.planning import plan_next_actions
from uniflow.schemas.planning_v1 import canonical_planning_json
from uniflow.application.planning_calendar import (
    create_planning_profile, planning_profile_info, update_planning_profile,
    add_availability_window, list_availability_windows, deactivate_availability_window,
    add_calendar_constraint, list_calendar_constraints, deactivate_calendar_constraint,
    project_calendar_week, UNSET,
)
from uniflow.schemas.calendar_v1 import canonical_calendar_json
from uniflow.application.weekly_scheduler import schedule_week
from uniflow.schemas.weekly_schedule_v1 import canonical_weekly_schedule_json
from uniflow.application.weekly_plans import (
    INHERIT, create_weekly_plan, regenerate_weekly_plan, weekly_plan_info,
    weekly_plan_history, assess_weekly_plan_revision, accept_weekly_plan_revision,
    move_weekly_plan_block, set_weekly_plan_block_duration,
    remove_weekly_plan_block, place_weekly_plan_action,
)
from uniflow.application.weekly_block_materialization import (
    materialize_weekly_plan_block, weekly_block_materialization_info,
)


def _calendar_call(operation):
    try:
        return operation()
    except (ValueError, TypeError) as exc:
        typer.echo(f"CALENDAR FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("planning-profile-init")
def planning_profile_init(semester: str = typer.Option(..., "--semester"),
    default_duration: int | None = typer.Option(None, "--default-duration"),
    max_daily: int | None = typer.Option(None, "--max-daily"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_calendar_call(lambda: create_planning_profile(load_config(project_root=project_root),
        semester, default_action_duration_minutes=default_duration, max_daily_study_minutes=max_daily)), sort_keys=True))


@app.command("planning-profile-info")
def planning_profile_readback(semester: str = typer.Option(..., "--semester"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_calendar_call(lambda: planning_profile_info(load_config(project_root=project_root), semester)), sort_keys=True))


@app.command("planning-profile-set")
def planning_profile_set(semester: str = typer.Option(..., "--semester"),
    default_duration: int | None = typer.Option(None, "--default-duration"),
    clear_default_duration: bool = typer.Option(False, "--clear-default-duration"),
    max_daily: int | None = typer.Option(None, "--max-daily"),
    clear_max_daily: bool = typer.Option(False, "--clear-max-daily"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    if (default_duration is not None and clear_default_duration) or (max_daily is not None and clear_max_daily):
        raise typer.BadParameter("set and clear options conflict")
    duration = None if clear_default_duration else default_duration if default_duration is not None else UNSET
    daily = None if clear_max_daily else max_daily if max_daily is not None else UNSET
    typer.echo(json.dumps(_calendar_call(lambda: update_planning_profile(load_config(project_root=project_root),
        semester, default_action_duration_minutes=duration, max_daily_study_minutes=daily)), sort_keys=True))


@app.command("availability-add")
def availability_add(semester: str = typer.Option(..., "--semester"),
    weekday: str = typer.Option(..., "--weekday"), start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"), valid_from: str | None = typer.Option(None, "--valid-from"),
    valid_until: str | None = typer.Option(None, "--valid-until"),
    start_fold: int | None = typer.Option(None, "--start-fold"),
    end_fold: int | None = typer.Option(None, "--end-fold"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def action():
        return add_availability_window(load_config(project_root=project_root), semester, weekday=weekday,
            start_local_time=datetime.strptime(start, "%H:%M").time(),
            end_local_time=datetime.strptime(end, "%H:%M").time(),
            valid_from=date.fromisoformat(valid_from) if valid_from else None,
            valid_until=date.fromisoformat(valid_until) if valid_until else None,
            start_fold=start_fold, end_fold=end_fold)
    typer.echo(json.dumps(_calendar_call(action), sort_keys=True))


@app.command("availability-list")
def availability_list(semester: str = typer.Option(..., "--semester"),
    include_inactive: bool = typer.Option(False, "--include-inactive"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_calendar_call(lambda: list_availability_windows(load_config(project_root=project_root),
        semester, include_inactive=include_inactive)), sort_keys=True))


@app.command("availability-deactivate")
def availability_deactivate(window_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_calendar_call(lambda: deactivate_availability_window(load_config(project_root=project_root), window_id)))


@app.command("calendar-constraint-add")
def calendar_constraint_add(semester: str = typer.Option(..., "--semester"),
    kind: str = typer.Option(..., "--kind"), start: str | None = typer.Option(None, "--start"),
    end: str | None = typer.Option(None, "--end"), local_date: str | None = typer.Option(None, "--date"),
    label: str | None = typer.Option(None, "--label"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def action():
        return add_calendar_constraint(load_config(project_root=project_root), semester, constraint_type=kind,
            starts_at=datetime.fromisoformat(start) if start else None,
            ends_at=datetime.fromisoformat(end) if end else None,
            local_date=date.fromisoformat(local_date) if local_date else None, label=label)
    typer.echo(json.dumps(_calendar_call(action), sort_keys=True))


@app.command("calendar-constraint-list")
def calendar_constraint_list(semester: str = typer.Option(..., "--semester"),
    include_inactive: bool = typer.Option(False, "--include-inactive"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_calendar_call(lambda: list_calendar_constraints(load_config(project_root=project_root),
        semester, include_inactive=include_inactive)), sort_keys=True))


@app.command("calendar-constraint-deactivate")
def calendar_constraint_deactivate(constraint_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_calendar_call(lambda: deactivate_calendar_constraint(load_config(project_root=project_root), constraint_id)))


@app.command("planning-calendar-week")
def planning_calendar_week(semester: str = typer.Option(..., "--semester"),
    week_start: str = typer.Option(..., "--week-start"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _calendar_call(lambda: project_calendar_week(load_config(project_root=project_root),
        semester, date.fromisoformat(week_start)))
    typer.echo(canonical_calendar_json(result).decode("utf-8"))


@app.command("plan-next")
def plan_next(semester: str = typer.Option(..., "--semester"),
    course: str | None = typer.Option(None, "--course"),
    limit: int = typer.Option(10, "--limit"),
    as_of: str | None = typer.Option(None, "--as-of"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    """Print read-only, canonical JSON learning recommendations."""
    try:
        captured = datetime.fromisoformat(as_of) if as_of is not None else datetime.now().astimezone()
        result = plan_next_actions(load_config(project_root=project_root), semester,
            as_of=captured, course_id=course, limit=limit)
        typer.echo(canonical_planning_json(result).decode("utf-8"))
    except ValueError as exc:
        typer.echo(f"PLAN NEXT FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


def _parse_duration_overrides(values: list[str]) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("INVALID_ACTION_DURATION")
        action_id, minutes_text = value.rsplit("=", 1)
        if not action_id or not minutes_text.isascii() or not minutes_text.isdecimal():
            raise ValueError("INVALID_ACTION_DURATION")
        if action_id in parsed:
            raise ValueError("DUPLICATE_DURATION_ACTION")
        minutes = int(minutes_text)
        if minutes <= 0:
            raise ValueError("INVALID_ACTION_DURATION")
        parsed[action_id] = minutes
    return parsed


@app.command("schedule-week")
def schedule_week_command(semester: str = typer.Option(..., "--semester"),
    week_start: str = typer.Option(..., "--week-start"),
    course: str | None = typer.Option(None, "--course"),
    limit: int = typer.Option(10, "--limit"),
    as_of: str | None = typer.Option(None, "--as-of"),
    duration: list[str] | None = typer.Option(None, "--duration"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    """Print a read-only deterministic weekly schedule as canonical JSON."""
    try:
        captured = datetime.fromisoformat(as_of) if as_of is not None else datetime.now().astimezone()
        result = schedule_week(load_config(project_root=project_root), semester,
            date.fromisoformat(week_start), as_of=captured, course_id=course,
            limit=limit, durations=_parse_duration_overrides(duration or []))
        typer.echo(canonical_weekly_schedule_json(result).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        typer.echo(f"SCHEDULE WEEK FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


def _weekly_plan_call(operation):
    try:
        return operation()
    except (ValueError, TypeError) as exc:
        typer.echo(f"WEEKLY PLAN FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


def _parse_weekly_plan_start(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("INVALID_BLOCK_START") from exc


@app.command("weekly-plan-create")
def weekly_plan_create_command(semester: str = typer.Option(..., "--semester"),
    week_start: str = typer.Option(..., "--week-start"),
    course: str | None = typer.Option(None, "--course"),
    limit: int = typer.Option(10, "--limit"),
    as_of: str | None = typer.Option(None, "--as-of"),
    duration: list[str] | None = typer.Option(None, "--duration"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: create_weekly_plan(load_config(project_root=project_root),
        semester, date.fromisoformat(week_start), course_id=course, limit=limit,
        as_of=datetime.fromisoformat(as_of) if as_of else None,
        durations=_parse_duration_overrides(duration or [])))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-regenerate")
def weekly_plan_regenerate_command(plan_id: str,
    course: str | None = typer.Option(None, "--course"),
    all_courses: bool = typer.Option(False, "--all-courses"),
    limit: int | None = typer.Option(None, "--limit"),
    as_of: str | None = typer.Option(None, "--as-of"),
    duration: list[str] | None = typer.Option(None, "--duration"),
    clear_durations: bool = typer.Option(False, "--clear-durations"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    if (course is not None and all_courses) or (duration and clear_durations):
        typer.echo("WEEKLY PLAN FAILED: CONFLICTING_REGENERATION_OPTIONS", err=True)
        raise typer.Exit(1)
    chosen_course = None if all_courses else course if course is not None else INHERIT
    chosen_durations = ({} if clear_durations else
        _parse_duration_overrides(duration) if duration else INHERIT)
    result = _weekly_plan_call(lambda: regenerate_weekly_plan(load_config(project_root=project_root),
        plan_id, course_id=chosen_course, limit=limit,
        as_of=datetime.fromisoformat(as_of) if as_of else None, durations=chosen_durations))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-info")
def weekly_plan_info_command(plan_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_weekly_plan_call(lambda: weekly_plan_info(
        load_config(project_root=project_root), plan_id)), sort_keys=True))


@app.command("weekly-plan-history")
def weekly_plan_history_command(plan_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_weekly_plan_call(lambda: weekly_plan_history(
        load_config(project_root=project_root), plan_id)), sort_keys=True))


@app.command("weekly-plan-check")
def weekly_plan_check_command(plan_id: str, revision: int | None = typer.Option(None, "--revision"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: assess_weekly_plan_revision(
        load_config(project_root=project_root), plan_id, revision))
    typer.echo(json.dumps(result.model_dump(mode="json"), sort_keys=True))


@app.command("weekly-plan-accept")
def weekly_plan_accept_command(plan_id: str, revision: int | None = typer.Option(None, "--revision"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: accept_weekly_plan_revision(
        load_config(project_root=project_root), plan_id, revision))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-move-block")
def weekly_plan_move_block_command(plan_id: str, block_id: str,
    revision_id: str = typer.Option(..., "--revision-id"),
    start: str = typer.Option(..., "--start"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: move_weekly_plan_block(load_config(project_root=project_root),
        plan_id, block_id, _parse_weekly_plan_start(start), expected_revision_id=revision_id))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-set-duration")
def weekly_plan_set_duration_command(plan_id: str, block_id: str,
    revision_id: str = typer.Option(..., "--revision-id"),
    minutes: int = typer.Option(..., "--minutes"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: set_weekly_plan_block_duration(load_config(project_root=project_root),
        plan_id, block_id, minutes, expected_revision_id=revision_id))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-remove-block")
def weekly_plan_remove_block_command(plan_id: str, block_id: str,
    revision_id: str = typer.Option(..., "--revision-id"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: remove_weekly_plan_block(load_config(project_root=project_root),
        plan_id, block_id, expected_revision_id=revision_id))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-plan-place-action")
def weekly_plan_place_action_command(plan_id: str, action_id: str,
    revision_id: str = typer.Option(..., "--revision-id"),
    start: str = typer.Option(..., "--start"),
    minutes: int | None = typer.Option(None, "--minutes"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: place_weekly_plan_action(load_config(project_root=project_root),
        plan_id, action_id, _parse_weekly_plan_start(start), duration_minutes=minutes,
        expected_revision_id=revision_id))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-block-materialize")
def weekly_block_materialize_command(plan_id: str, block_id: str,
    revision_id: str = typer.Option(..., "--revision-id"),
    goal: str | None = typer.Option(None, "--goal"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: materialize_weekly_plan_block(
        load_config(project_root=project_root), plan_id, block_id,
        expected_accepted_revision_id=revision_id, goal=goal))
    typer.echo(json.dumps(result, sort_keys=True))


@app.command("weekly-block-materialization-info")
def weekly_block_materialization_info_command(execution_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _weekly_plan_call(lambda: weekly_block_materialization_info(
        load_config(project_root=project_root), execution_id))
    typer.echo(json.dumps(result, sort_keys=True))


def _handoff_call(operation):
    try:
        return operation()
    except ValueError as exc:
        typer.echo(f"HANDOFF FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("learning-session-create")
def learning_session_create(course: str = typer.Option(..., "--course"),
    goal: str = typer.Option(..., "--goal"), skills: list[str] = typer.Option(..., "--skill"),
    planned_for: datetime | None = typer.Option(None, "--planned-for"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    result = _handoff_call(lambda: create_learning_session(load_config(project_root=project_root),
        course, goal, skills, planned_for))
    typer.echo(result)


@app.command("learning-session-info")
def learning_session_readback(session_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_handoff_call(lambda: learning_session_info(
        load_config(project_root=project_root), session_id)), indent=2))


@app.command("learning-session-start")
def learning_session_start_command(session_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_handoff_call(lambda: start_learning_session(
        load_config(project_root=project_root), session_id)))


@app.command("learning-result-import")
def learning_result_import_command(path: Path,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    outcome = _handoff_call(lambda: import_learning_result(load_config(project_root=project_root), path))
    typer.echo(f"{outcome.status} {outcome.result_id} sha256={outcome.artifact_sha256} evidence={outcome.evidence_count}")


@app.command("learning-result-info")
def learning_result_info_command(result_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_handoff_call(lambda: learning_result_info(
        load_config(project_root=project_root), result_id)), indent=2))


@app.command("learning-result-apply")
def learning_result_apply_command(result_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    outcome = _handoff_call(lambda: apply_learning_result_mastery(
        load_config(project_root=project_root), result_id))
    typer.echo(json.dumps({"status": outcome.status, "result_id": outcome.result_id,
        "session_id": outcome.session_id,
        "created_mastery_count": len(outcome.created_mastery_state_ids),
        "mastery_state_ids": outcome.created_mastery_state_ids,
        "skipped_skill_ids": outcome.skipped_skill_ids}, indent=2))


@app.command("handoff-generate")
def handoff_generate_command(session_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    outcome = _handoff_call(lambda: generate_handoff(load_config(project_root=project_root), session_id))
    typer.echo(f"{outcome.status} {outcome.handoff_id} revision={outcome.revision} sha256={outcome.artifact_sha256}")


@app.command("handoff-info")
def handoff_info_command(identifier: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(json.dumps(_handoff_call(lambda: handoff_info(
        load_config(project_root=project_root), identifier)), indent=2))


@app.command("handoff-export")
def handoff_export_command(identifier: str, path: Path | None = None,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_handoff_call(lambda: export_handoff(load_config(project_root=project_root), identifier, path)))


@app.command("learning-chat-export")
def learning_chat_export_command(handoff_id: str,
    destination_directory: Path = typer.Option(..., "--destination-directory"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    """Export the current verified Handoff as one chat upload file."""
    try:
        outcome = export_learning_chat_package(load_config(project_root=project_root),
                                               handoff_id, destination_directory)
    except WorkflowCommandError as exc:
        typer.echo(f"LEARNING CHAT EXPORT FAILED: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"status": outcome.status, "path": str(outcome.path),
        "result_filename": outcome.result_filename, "handoff_artifact_sha256":
        outcome.handoff_artifact_sha256}, indent=2))


@app.command("learning-scoring-preview")
def learning_scoring_preview_command(path: Path,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    """Validate a returned scoring file and print a non-authoritative preview."""
    try:
        preview = preview_scoring_result(load_config(project_root=project_root), path)
    except ValueError as exc:
        code = str(exc)
        typer.echo(f"SCORING PREVIEW FAILED: {code} – {SCORING_RESULT_MESSAGES.get(code, 'Ergebnis kann nicht geprüft werden.')}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"status": preview.status, "score": asdict(preview.score),
        "handoff_revision": preview.handoff_revision, "assistance": preview.assistance,
        "errors": preview.errors, "qualitative_observations": preview.qualitative_observations,
        "open_information": preview.open_information}, indent=2, default=str))


def _skill_call(operation):
    try:
        return operation()
    except Exception as exc:
        typer.echo(f"SKILL FOUNDATION FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("semantic-run-document")
def semantic_run_document(material_artifact_id: str, parse_run_id: str,
    role: str = typer.Option("UNKNOWN", "--role"), project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_semantic_run(load_config(project_root=project_root), "DOCUMENT",
        material_artifact_id=material_artifact_id, parse_run_id=parse_run_id, role=role)))


@app.command("semantic-run-repository")
def semantic_run_repository(repository_academic_link_id: str, snapshot_id: str,
    state: str = typer.Option(..., "--state"), role: str = typer.Option("UNKNOWN", "--role"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_semantic_run(load_config(project_root=project_root), "REPOSITORY",
        repository_academic_link_id=repository_academic_link_id, repository_snapshot_id=snapshot_id,
        repository_state=state, role=role)))


@app.command("semantic-run-manual")
def semantic_run_manual(role: str = typer.Option("UNKNOWN", "--role"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_semantic_run(load_config(project_root=project_root), "MANUAL", role=role)))


@app.command("semantic-run-info")
def semantic_run_info(run_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    from uniflow.infrastructure.database.engine import get_engine, create_session_factory
    from uniflow.infrastructure.database.models import SemanticExtractionRun
    def operation():
        config = load_config(project_root=project_root)
        with create_session_factory(get_engine(config.database_path))() as session:
            row = session.get(SemanticExtractionRun, run_id)
            if row is None: raise ValueError("UNKNOWN_RUN")
            return f"{row.id} family={row.source_family} role={row.source_role} input={row.input_identity_sha256} state={row.repository_state}"
    typer.echo(_skill_call(operation))


@app.command("semantic-evidence-document")
def semantic_evidence_document(run_id: str, parsed_unit_id: str, start: int, end: int,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_semantic_evidence(load_config(project_root=project_root), run_id, parsed_unit_id, start, end)))


@app.command("semantic-evidence-repository")
def semantic_evidence_repository(run_id: str, repository_file_id: str, start: int, end: int,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_semantic_evidence(load_config(project_root=project_root), run_id, repository_file_id, start, end)))


@app.command("skill-propose-new")
def skill_propose_new(run_id: str, name: str = typer.Option(..., "--name"), kind: str = typer.Option(..., "--kind"),
    evidence: list[str] = typer.Option([], "--evidence"), description: str | None = typer.Option(None, "--description"),
    domain: str | None = typer.Option(None, "--domain"), skill_id: str | None = typer.Option(None, "--skill-id"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_skill_proposal(load_config(project_root=project_root), run_id, "NEW_SKILL",
        evidence_ids=evidence, name=name, skill_kind=kind, description=description, domain=domain, proposed_skill_id=skill_id)))


@app.command("skill-propose-existing")
def skill_propose_existing(run_id: str, skill: str = typer.Option(..., "--skill"), evidence: list[str] = typer.Option([], "--evidence"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_skill_proposal(load_config(project_root=project_root), run_id, "EXISTING_SKILL_EVIDENCE",
        evidence_ids=evidence, target_skill_id=skill)))


@app.command("skill-propose-dependency")
def skill_propose_dependency(run_id: str, prerequisite: str = typer.Option(..., "--prerequisite"),
    dependent: str = typer.Option(..., "--dependent"), evidence: list[str] = typer.Option([], "--evidence"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    typer.echo(_skill_call(lambda: create_skill_proposal(load_config(project_root=project_root), run_id, "SKILL_DEPENDENCY",
        evidence_ids=evidence, prerequisite_skill_id=prerequisite, dependent_skill_id=dependent)))


@app.command("skill-propose-alias")
def skill_propose_alias(skill: str = typer.Option(..., "--skill"), alias: str = typer.Option(..., "--alias"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def operation():
        config = load_config(project_root=project_root)
        run = create_semantic_run(config, "MANUAL")
        return create_skill_proposal(config, run, "SKILL_ALIAS", target_skill_id=skill, alias=alias)
    typer.echo(_skill_call(operation))


@app.command("skill-propose-course")
def skill_propose_course(skill: str = typer.Option(..., "--skill"), course: str = typer.Option(..., "--course"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def operation():
        from uniflow.infrastructure.database.engine import get_engine, create_session_factory
        from uniflow.infrastructure.database.models import Course
        from sqlalchemy import select
        config = load_config(project_root=project_root)
        with create_session_factory(get_engine(config.database_path))() as session:
            row = session.scalar(select(Course).where(Course.stable_id == course))
            if row is None: raise ValueError("UNKNOWN_COURSE")
            course_id = row.id
        run = create_semantic_run(config, "MANUAL")
        return create_skill_proposal(config, run, "SKILL_COURSE_ASSOCIATION", target_skill_id=skill, course_id=course_id)
    typer.echo(_skill_call(operation))


@app.command("skill-proposal-list")
def skill_proposal_list(project_root: Path | None = typer.Option(None, "--project-root"),
    run_id: str | None = typer.Option(None, "--run"), attempt_id: str | None = typer.Option(None, "--attempt")) -> None:
    for p in _skill_call(lambda: list_skill_proposals(load_config(project_root=project_root))):
        if run_id and p.extraction_run_id != run_id: continue
        if attempt_id and p.extraction_attempt_id != attempt_id: continue
        typer.echo(f"{p.id} {p.proposal_kind} {p.status} name={p.proposed_name or ''} attempt={p.extraction_attempt_id or ''}")


@app.command("semantic-extract-document")
def semantic_extract_document(run_id: str, result_json: Path | None = typer.Option(None, "--result-json"),
    adapter: str = typer.Option("FIXTURE", "--adapter"),
    allow_external: bool = typer.Option(False, "--allow-external"),
    model: str | None = typer.Option(None, "--model"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def operation():
        config = load_config(project_root=project_root)
        if adapter == "FIXTURE":
            if result_json is None: raise ValueError("FIXTURE_RESULT_REQUIRED")
            if result_json.stat().st_size > 1048576: raise ValueError("FIXTURE_FILE_TOO_LARGE")
            result = json.loads(result_json.read_text(encoding="utf-8"))
            return execute_document_extraction(config, run_id, adapter_name=adapter, fixture_result=result)
        if adapter != "OPENAI": raise ValueError("UNKNOWN_ADAPTER")
        if result_json is not None: raise ValueError("RESULT_JSON_FIXTURE_ONLY")
        if config.semantic_external_policy == "ALLOW_WITH_EXPLICIT_CONFIRMATION" and allow_external:
            from uniflow.application.semantic_extraction_runtime import preview_external_document_extraction
            p = preview_external_document_extraction(config, run_id, model=model)
            typer.echo(f"External disclosure: run={p.run_id} family={p.source_family} role={p.source_role} material_artifact={p.material_artifact_id or ''} provider={p.provider} model={p.model} items={p.item_count} utf8_bytes={p.utf8_bytes} input={p.input_package_sha256}; selected source text will be sent to OpenAI")
        return execute_document_extraction(config, run_id, adapter_name=adapter,
            allow_external=allow_external, model=model)
    row = _skill_call(operation)
    typer.echo(f"{row.attempt_id} attempt={row.attempt_number} status={row.status} error={row.error_code or ''} candidates={row.candidate_count} proposals={row.proposal_count}")


@app.command("semantic-attempt-retry")
def semantic_attempt_retry(attempt_id: str, result_json: Path | None = typer.Option(None, "--result-json"),
    allow_external: bool = typer.Option(False, "--allow-external"),
    model: str | None = typer.Option(None, "--model"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def operation():
        config = load_config(project_root=project_root)
        old = get_semantic_attempt(config, attempt_id)
        from uniflow.infrastructure.database.engine import get_engine, create_session_factory
        from uniflow.infrastructure.database.models import SemanticExtractionRun
        with create_session_factory(get_engine(config.database_path))() as session:
            source_family = session.get(SemanticExtractionRun, old.run_id).source_family
        if source_family == "REPOSITORY":
            if not old.input_manifest_json: raise ValueError("RETRY_REQUIRES_INPUT_PACKAGE")
            ids = [f["repository_file_id"] for f in json.loads(old.input_manifest_json)["selected_files"]]
            if old.adapter_name == "OPENAI":
                if result_json is not None: raise ValueError("RESULT_JSON_FIXTURE_ONLY")
                if config.semantic_external_policy == "ALLOW_WITH_EXPLICIT_CONFIRMATION" and allow_external:
                    _show_repository_preview(preview_external_repository_extraction(config, old.run_id, ids, model=model))
                return execute_repository_extraction(config, old.run_id, ids, adapter_name="OPENAI",
                    retry_of_attempt_id=attempt_id, allow_external=allow_external, model=model)
            if old.adapter_name != "FIXTURE": raise ValueError("UNKNOWN_ADAPTER")
            if result_json is None: raise ValueError("FIXTURE_RESULT_REQUIRED")
            if result_json.stat().st_size > 1048576: raise ValueError("FIXTURE_FILE_TOO_LARGE")
            result = json.loads(result_json.read_text(encoding="utf-8"))
            return execute_repository_extraction(config, old.run_id, ids, fixture_result=result,
                retry_of_attempt_id=attempt_id)
        if old.adapter_name == "OPENAI":
            if config.semantic_external_policy == "ALLOW_WITH_EXPLICIT_CONFIRMATION" and allow_external:
                from uniflow.application.semantic_extraction_runtime import preview_external_document_extraction
                p = preview_external_document_extraction(config, old.run_id, model=model)
                typer.echo(f"External disclosure: run={p.run_id} family={p.source_family} role={p.source_role} material_artifact={p.material_artifact_id or ''} provider={p.provider} model={p.model} items={p.item_count} utf8_bytes={p.utf8_bytes} input={p.input_package_sha256}; selected source text will be sent to OpenAI")
            return execute_document_extraction(config, old.run_id, adapter_name="OPENAI",
                retry_of_attempt_id=attempt_id, allow_external=allow_external, model=model)
        if result_json is None: raise ValueError("FIXTURE_RESULT_REQUIRED")
        if result_json.stat().st_size > 1048576: raise ValueError("FIXTURE_FILE_TOO_LARGE")
        result = json.loads(result_json.read_text(encoding="utf-8"))
        return execute_document_extraction(config, old.run_id, fixture_result=result, retry_of_attempt_id=attempt_id)
    row = _skill_call(operation)
    typer.echo(f"{row.attempt_id} attempt={row.attempt_number} status={row.status} error={row.error_code or ''}")


@app.command("repository-semantic-files")
def repository_semantic_files(run_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    rows = _skill_call(lambda: list_repository_semantic_files(load_config(project_root=project_root), run_id))
    for row in rows:
        typer.echo(f"{row.file_id} path={row.relative_path} entry={row.entry_kind} tracked={row.tracked_state} language={row.language_hint} state_available={row.state_available} bytes={row.blob_size if row.blob_size is not None else ''} eligible={row.eligible} reason={row.reason or ''}")


@app.command("semantic-extract-repository")
def semantic_extract_repository(run_id: str, files: list[str] = typer.Option([], "--file"),
    result_json: Path | None = typer.Option(None, "--result-json"),
    adapter: str = typer.Option("FIXTURE", "--adapter"),
    allow_external: bool = typer.Option(False, "--allow-external"),
    model: str | None = typer.Option(None, "--model"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    def operation():
        if not files: raise ValueError("FILE_SELECTION_REQUIRED")
        config = load_config(project_root=project_root)
        if adapter == "OPENAI":
            if result_json is not None: raise ValueError("RESULT_JSON_FIXTURE_ONLY")
            if config.semantic_external_policy == "ALLOW_WITH_EXPLICIT_CONFIRMATION" and allow_external:
                _show_repository_preview(preview_external_repository_extraction(config, run_id, files, model=model))
            return execute_repository_extraction(config, run_id, files, adapter_name="OPENAI",
                allow_external=allow_external, model=model)
        if adapter != "FIXTURE": raise ValueError("UNKNOWN_ADAPTER")
        if result_json is None: raise ValueError("FIXTURE_RESULT_REQUIRED")
        if allow_external or model is not None: raise ValueError("FIXTURE_EXTERNAL_OPTIONS_UNSUPPORTED")
        if result_json.stat().st_size > 1048576: raise ValueError("FIXTURE_FILE_TOO_LARGE")
        result = json.loads(result_json.read_text(encoding="utf-8"))
        return execute_repository_extraction(config, run_id, files,
            fixture_result=result)
    row = _skill_call(operation)
    typer.echo(f"{row.attempt_id} attempt={row.attempt_number} status={row.status} error={row.error_code or ''} candidates={row.candidate_count} proposals={row.proposal_count}")


def _show_repository_preview(preview) -> None:
    typer.echo(f"External repository disclosure: run={preview.run_id} family=REPOSITORY role={preview.source_role} state={preview.repository_state} provider={preview.provider} model={preview.model} files={preview.file_count} items={preview.item_count} utf8_bytes={preview.utf8_bytes} input={preview.input_package_sha256}")
    for path, tracked in preview.selected_files:
        typer.echo(f"  selected path={path} tracked={tracked}")
    if preview.source_role == "STUDENT_CODE":
        typer.echo("SELECTED SOURCE CODE WILL BE TRANSMITTED TO THE CONFIGURED EXTERNAL PROVIDER.")
    else:
        typer.echo("Selected repository text and relative paths will be transmitted to the configured external provider.")


@app.command("semantic-attempt-list")
def semantic_attempt_list(run_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for row in _skill_call(lambda: list_semantic_attempts(load_config(project_root=project_root), run_id)):
        typer.echo(f"{row.id} attempt={row.attempt_number} status={row.status} adapter={row.adapter_name} error={row.error_code or ''}")


@app.command("semantic-attempt-info")
def semantic_attempt_info(attempt_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    row = _skill_call(lambda: get_semantic_attempt(load_config(project_root=project_root), attempt_id))
    typer.echo(f"{row.id} run={row.run_id} attempt={row.attempt_number} status={row.status} adapter={row.adapter_name}/{row.adapter_version} input={row.input_package_sha256 or ''} output={row.output_sha256 or ''} candidates={row.candidate_count} proposals={row.proposal_count} error={row.error_code or ''}")


@app.command("skill-proposal-info")
def skill_proposal_info(proposal_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    p, evidence = _skill_call(lambda: get_skill_proposal(load_config(project_root=project_root), proposal_id))
    adapter_name = ""
    paths = {}
    if p.extraction_attempt_id or any(span.repository_file_id for span in evidence):
        from uniflow.infrastructure.database.engine import get_engine, create_session_factory
        from uniflow.infrastructure.database.models import RepositoryFile, SemanticExtractionAttempt
        config = load_config(project_root=project_root)
        with create_session_factory(get_engine(config.database_path))() as session:
            if p.extraction_attempt_id:
                attempt = session.get(SemanticExtractionAttempt, p.extraction_attempt_id)
                adapter_name = attempt.adapter_name if attempt else ""
            for span in evidence:
                if span.repository_file_id:
                    file = session.get(RepositoryFile, span.repository_file_id)
                    paths[span.repository_file_id] = file.path if file else ""
    typer.echo(f"{p.id} {p.proposal_kind} {p.status} action={p.decision_action} accepted_skill={p.accepted_skill_id} adapter={adapter_name} confidence={p.confidence}")
    for span in evidence:
        typer.echo(f"evidence={span.id} sha256={span.span_sha256} path={paths.get(span.repository_file_id, '')} state={span.repository_state or ''} byte_start={span.byte_start if span.byte_start is not None else ''} byte_end={span.byte_end if span.byte_end is not None else ''}")


@app.command("skill-proposal-accept")
def skill_proposal_accept(proposal_id: str, existing_skill: str | None = typer.Option(None, "--existing-skill"),
    dry_run: bool = typer.Option(False, "--dry-run"), project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    d = _skill_call(lambda: accept_skill_proposal(load_config(project_root=project_root), proposal_id,
        existing_skill_id=existing_skill, dry_run=dry_run))
    typer.echo(f"{'DRY_RUN ' if dry_run else ''}{d.action} target={d.target_id or 'GENERATED_ON_ACCEPT'} academic={d.academic_provenance} evidence={','.join(d.evidence_ids)}")


@app.command("skill-proposal-reject")
def skill_proposal_reject(proposal_id: str, reason: str | None = typer.Option(None, "--reason"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    _skill_call(lambda: reject_skill_proposal(load_config(project_root=project_root), proposal_id, reason))
    typer.echo("REJECTED")


@app.command("skill-proposal-supersede")
def skill_proposal_supersede(proposal_id: str, replacement_id: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    _skill_call(lambda: supersede_skill_proposal(load_config(project_root=project_root), proposal_id, replacement_id))
    typer.echo("SUPERSEDED")


@app.command("skill-relation-deactivate")
def skill_relation_deactivate(kind: str, relation_id: str, reason: str = typer.Option(..., "--reason"),
    replacement_id: str | None = typer.Option(None, "--replacement-id"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    _skill_call(lambda: deactivate_skill_relation(load_config(project_root=project_root), kind, relation_id, reason, replacement_id))
    typer.echo("DEACTIVATED")


@app.command("skill-list")
def skill_list(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for s in _skill_call(lambda: list_skills(load_config(project_root=project_root))):
        typer.echo(f"{s.id} {s.kind} {s.status} {s.name}")


@app.command("skill-info")
def skill_info(skill_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    s = _skill_call(lambda: get_skill(load_config(project_root=project_root), skill_id))
    typer.echo(f"{s.id} {s.kind} {s.status} {s.name} domain={s.domain}")


@app.command("skill-evidence")
def skill_evidence(skill_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for span in _skill_call(lambda: list_skill_evidence(load_config(project_root=project_root), skill_id)):
        typer.echo(f"{span.id} sha256={span.span_sha256} unit={span.parsed_unit_id} file={span.repository_file_id}")


@app.command("skill-aliases")
def skill_aliases(skill_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for a in _skill_call(lambda: list_skill_aliases(load_config(project_root=project_root), skill_id)):
        typer.echo(f"{a.id} active={a.is_active} alias={a.alias}")


@app.command("course-skills")
def course_skills(course_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for relation in _skill_call(lambda: list_course_skills(load_config(project_root=project_root), course_id)):
        typer.echo(f"{relation.id} skill={relation.skill_id} active={relation.is_active}")


@app.command("skill-dependencies")
def skill_dependencies(skill_id: str | None = None, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for relation in _skill_call(lambda: list_skill_dependencies(load_config(project_root=project_root), skill_id)):
        typer.echo(f"{relation.id} {relation.prerequisite_skill_id}->{relation.dependent_skill_id} active={relation.is_active}")


@app.command("artifact-unclassified")
def artifact_unclassified(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for artifact in list_unclassified_artifacts(load_config(project_root=project_root)):
        typer.echo(f"{artifact.id} sha256={artifact.sha256} filename={artifact.original_filename}")


@app.command("repository-unlinked")
def repository_unlinked(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for repo in list_unlinked_repositories(load_config(project_root=project_root)):
        typer.echo(f"{repo.id} stable_id={repo.stable_id}")


@app.command("classify-artifact")
def classify_artifact(artifact: str, course: str | None = typer.Option(None, "--course"),
    material: str | None = typer.Option(None, "--material"),
    new_material_stable_id: str | None = typer.Option(None, "--new-material-stable-id"),
    title: str | None = typer.Option(None, "--title"),
    material_type: str | None = typer.Option(None, "--material-type"),
    replaces_link: str | None = typer.Option(None, "--replaces-link"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        identifier = create_manual_artifact_proposal(load_config(project_root=project_root), artifact,
            course=course, material=material, new_material_stable_id=new_material_stable_id,
            title=title, material_type=material_type, replaces_link_id=replaces_link)
        typer.echo(f"PENDING proposal={identifier}")
    except Exception as exc:
        typer.echo(f"CLASSIFICATION FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("classify-repository")
def classify_repository(repository: str, course: str | None = typer.Option(None, "--course"),
    assessment: str | None = typer.Option(None, "--assessment"),
    replaces_link: str | None = typer.Option(None, "--replaces-link"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        identifier = create_manual_repository_proposal(load_config(project_root=project_root), repository,
            course=course, assessment=assessment, replaces_link_id=replaces_link)
        typer.echo(f"PENDING proposal={identifier}")
    except Exception as exc:
        typer.echo(f"CLASSIFICATION FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("classify-expectation")
def classify_expectation(material: str, expectation: str = typer.Option(..., "--expectation"),
    replaces_link: str | None = typer.Option(None, "--replaces-link"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        identifier = create_manual_expectation_proposal(load_config(project_root=project_root), material,
            expectation, replaces_link_id=replaces_link)
        typer.echo(f"PENDING proposal={identifier}")
    except Exception as exc:
        typer.echo(f"CLASSIFICATION FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("classify-list")
def classify_list(status: str | None = typer.Option(None, "--status"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for p in list_classification_proposals(load_config(project_root=project_root), status):
        typer.echo(f"{p.id} {p.status} {p.proposal_kind} classifier={p.classifier_name}/{p.classifier_version}")


@app.command("classify-info")
def classify_info(proposal_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    p = get_classification_proposal(load_config(project_root=project_root), proposal_id)
    for name in ("id", "status", "proposal_kind", "subject_source_artifact_id",
                 "subject_repository_source_id", "subject_material_id", "proposed_course_id",
                 "proposed_assessment_id", "proposed_material_id", "proposed_material_stable_id",
                 "proposed_material_title", "proposed_material_type", "proposed_expectation_identity",
                 "classifier_name", "classifier_version", "confidence", "decided_at", "decision_actor",
                 "decision_reason", "replaces_link_id", "superseded_by_proposal_id"):
        typer.echo(f"{name}={getattr(p, name)}")


@app.command("classify-accept")
def classify_accept(proposal_id: str, dry_run: bool = typer.Option(False, "--dry-run"),
    reason: str | None = typer.Option(None, "--reason"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        action = accept_classification_proposal(load_config(project_root=project_root), proposal_id,
            dry_run=dry_run, reason=reason)
        typer.echo(f"{'DRY_RUN' if dry_run else 'ACCEPTED'} action={action} proposal={proposal_id}")
        if dry_run:
            p = get_classification_proposal(load_config(project_root=project_root), proposal_id)
            typer.echo(f"kind={p.proposal_kind} artifact={p.subject_source_artifact_id or '-'} "
                       f"repository={p.subject_repository_source_id or '-'} material={p.subject_material_id or p.proposed_material_id or p.proposed_material_stable_id or '-'} "
                       f"course={p.proposed_course_id or '-'} assessment={p.proposed_assessment_id or '-'} "
                       f"expectation={p.proposed_expectation_identity or '-'} replaces={p.replaces_link_id or '-'}")
    except Exception as exc:
        typer.echo(f"CLASSIFICATION CONFLICT: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("classify-reject")
def classify_reject(proposal_id: str, reason: str | None = typer.Option(None, "--reason"),
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        typer.echo(reject_classification_proposal(load_config(project_root=project_root), proposal_id, reason=reason))
    except Exception as exc:
        typer.echo(f"CLASSIFICATION FAILED: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("link-history")
def link_history(link_type: str, identifier: str,
    project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for link in get_link_history(load_config(project_root=project_root), link_type, identifier):
        typer.echo(f"{link.id} active={getattr(link, 'is_active', getattr(link, 'is_current', False))} proposal={link.classification_proposal_id or '-'} "
                   f"replacement={getattr(link, 'replacement_link_id', None) or '-'}")


@app.command("expectation-links")
def expectation_links(material: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for link, state in get_material_expectation_links(load_config(project_root=project_root), material):
        typer.echo(f"{link.id} {link.expectation_identity} {state} active={link.is_active}")


@app.command("repo-register")
def repo_register(path: Path, stable_id: str | None = typer.Option(None, "--stable-id"),
                  course: str | None = typer.Option(None, "--course"),
                  assessment: str | None = typer.Option(None, "--assessment"),
                  project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        outcome, identifier = register_repository(path, load_config(project_root=project_root),
            stable_id=stable_id, course_stable_id=course, assessment_stable_id=assessment)
        typer.echo(f"{outcome} repository={identifier}")
    except Exception as exc:
        typer.echo(f"REPOSITORY FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("repo-snapshot")
def repo_snapshot(identifier: str, dry_run: bool = typer.Option(False, "--dry-run"),
                  tracked_only: bool = typer.Option(False, "--tracked-only"),
                  project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        result = snapshot_repository(identifier, load_config(project_root=project_root),
            dry_run=dry_run, tracked_only=tracked_only)
        typer.echo(f"{result.outcome} snapshot={result.snapshot_id or '-'} identity={result.identity} "
                   f"files={result.file_count} blobs={result.blob_count} build={result.build_system_hint}")
    except Exception as exc:
        typer.echo(f"SNAPSHOT FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("repo-list")
def repo_list(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    for source in list_repositories(load_config(project_root=project_root)):
        typer.echo(f"{source.stable_id} id={source.id}")


@app.command("repo-info")
def repo_info(identifier: str, show_path: bool = typer.Option(False, "--show-path"),
              project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        source, remotes = repository_info(identifier, load_config(project_root=project_root))
        typer.echo(f"{source.stable_id} id={source.id} course={source.course_id or '-'} assessment={source.assessment_id or '-'}")
        if show_path:
            typer.echo(f"local_path={source.local_path}")
        for remote in remotes:
            typer.echo(f"remote {remote.name} {remote.sanitized_identity}")
    except Exception as exc:
        typer.echo(f"REPOSITORY FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("repo-snapshots")
def repo_snapshots(identifier: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        for snap in list_snapshots(identifier, load_config(project_root=project_root)):
            typer.echo(f"{snap.id} {snap.snapshot_identity} head={snap.head_commit_oid or '-'} branch={snap.branch_name or '-'}")
    except Exception as exc:
        typer.echo(f"REPOSITORY FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("repo-snapshot-info")
def repo_snapshot_info(identifier: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        snap, files = read_repository_snapshot(identifier, load_config(project_root=project_root))
        typer.echo(f"{snap.id} {snap.snapshot_identity} status={snap.status} build={snap.build_system_hint} "
                   f"tracked={snap.tracked_file_count} untracked={snap.untracked_file_count} deleted={snap.deleted_file_count}")
        for file in files:
            typer.echo(f"{file.tracked_state} {file.entry_kind} {file.path}")
    except Exception as exc:
        typer.echo(f"SNAPSHOT FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("parse-artifact")
def parse_artifact(identifier: str, retry_failed: bool = typer.Option(False, "--retry-failed"),
                   dry_run: bool = typer.Option(False, "--dry-run"),
                   project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        result = parse_source_artifact(identifier, load_config(project_root=project_root),
                                       retry_failed=retry_failed, dry_run=dry_run)
    except Exception as exc:
        typer.echo(f"PARSE FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{result.outcome} run={result.parse_run_id or '-'} attempt={result.attempt_number or '-'} "
               f"parser={result.parser_name}/{result.parser_version} dependency={result.dependency_version} "
               f"config_sha256={result.config_sha256} error={result.error_code or '-'}")


@app.command("parse-list")
def parse_list(project_root: Path | None = typer.Option(None, "--project-root"),
               limit: int = typer.Option(30, "--limit", min=1)) -> None:
    for run in list_parse_runs(load_config(project_root=project_root), limit):
        typer.echo(f"{run.id} {run.status} artifact={run.source_artifact_id} "
                   f"parser={run.parser_name}/{run.parser_version} attempt={run.attempt_number}")


@app.command("parse-info")
def parse_info(parse_run_id: str, project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        run, units = get_parse_run(parse_run_id, load_config(project_root=project_root))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"run={run.id} status={run.status} artifact={run.source_artifact_id} "
               f"parser={run.parser_name}/{run.parser_version} dependency={run.dependency_version} "
               f"config_sha256={run.config_sha256} attempt={run.attempt_number} "
               f"pages={run.page_count or 0} units={run.unit_count or 0} chars={run.char_count or 0} "
               f"error={run.error_code or '-'}")
    for unit in units:
        typer.echo(f"unit={unit.ordinal} kind={unit.unit_kind} label={unit.source_label or '-'} "
                   f"chars={unit.char_start}:{unit.char_end} sha256={unit.text_sha256}")


@app.command("drop-config")
def drop_config(path: Path, create: bool = typer.Option(False, "--create"),
                project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    config = load_config(project_root=project_root)
    folder = path.expanduser().resolve()
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise typer.BadParameter(f"Drop folder is not a directory: {folder}")
    local = config.project_root / "config" / "local.yaml"
    existing = yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {}
    if not isinstance(existing, dict):
        raise typer.BadParameter(f"Malformed local configuration: {local}")
    existing["external_drop_folder"] = str(folder)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(yaml.safe_dump(existing, sort_keys=True), encoding="utf-8")
    typer.echo(f"Drop folder: {folder}")
    typer.echo(f"Local configuration: {local}")


@app.command("drop-status")
def drop_status(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    status = get_drop_status(load_config(project_root=project_root))
    for key, value in status.items():
        typer.echo(f"{key}: {value}")
    typer.echo("watcher: unknown (this command does not own a watcher)")


@app.command("drop-scan")
def drop_scan(dry_run: bool = typer.Option(False, "--dry-run"),
              retry_failed: bool = typer.Option(False, "--retry-failed"),
              project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        for outcome in scan_drop(load_config(project_root=project_root), dry_run=dry_run,
                                 retry_failed=retry_failed):
            typer.echo(f"{outcome.state} {outcome.path} {outcome.detail}")
    except Exception as exc:
        typer.echo(f"DROP SCAN FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("drop-watch")
def drop_watch(project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    stop = Event()
    try:
        watch_drop(load_config(project_root=project_root), stop,
                   on_scan=lambda rows: [typer.echo(f"{row.state} {row.path} {row.detail}")
                                         for row in rows if row.state not in ("ALREADY_PROCESSED",)])
    except KeyboardInterrupt:
        stop.set()
        typer.echo("Drop watcher stopped")


@app.command("drop-jobs")
def drop_jobs(project_root: Path | None = typer.Option(None, "--project-root"),
              limit: int = typer.Option(30, "--limit", min=1)) -> None:
    for job in list_drop_jobs(load_config(project_root=project_root), limit):
        typer.echo(f"{job.state} attempts={job.attempt_count} {job.source_path} "
                   f"outcome={job.outcome or '-'} error={job.error_message or '-'}")


@app.command("manifest")
def manifest(semester: str = typer.Option(..., "--semester"),
             from_date: str = typer.Option(..., "--from"),
             through_date: str = typer.Option(..., "--through"),
             as_of: str | None = typer.Option(None, "--as-of"),
             project_root: Path | None = typer.Option(None, "--project-root")) -> None:
    try:
        result = generate_material_manifest(load_config(project_root=project_root), semester,
            date.fromisoformat(from_date), date.fromisoformat(through_date),
            datetime.fromisoformat(as_of) if as_of else datetime.now().astimezone())
    except Exception as exc:
        typer.echo(f"MANIFEST FAILED: {exc}", err=True)
        raise typer.Exit(1) from exc
    for item in result.expectations:
        typer.echo(f"{item.id} {item.course_id} {item.material_kind} "
                   f"{item.release_date} {item.release_time or 'date-only'} "
                   f"{item.information_status} {item.state}")
    for item in result.unresolved_rules:
        typer.echo(f"UNRESOLVED_RULE {item.rule_id} {item.course_id} {item.information_status} {item.reason}")
    for sha256, name in result.unclassified_artifacts:
        typer.echo(f"UNCLASSIFIED {sha256} {name}")


@app.command("material-import")
def material_import(
    path: Path,
    course: str | None = typer.Option(None, "--course"),
    stable_id: str | None = typer.Option(None, "--stable-id"),
    title: str | None = typer.Option(None, "--title"),
    material_type: str | None = typer.Option(None, "--material-type"),
    new_version: bool = typer.Option(False, "--new-version"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Path | None = typer.Option(None, "--config", "-c"),
    project_root: Path | None = typer.Option(None, "--project-root"),
) -> None:
    """Copy one local file into content-addressed storage."""
    try:
        result = import_material(path, load_config(config, project_root=project_root),
                                 course_stable_id=course, material_stable_id=stable_id,
                                 title=title, material_type=material_type,
                                 new_version=new_version, dry_run=dry_run)
    except Exception as exc:
        typer.echo(f"IMPORT FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{result.status} sha256={result.sha256} material={result.material_stable_id or '-'} "
               f"version={result.version_number or '-'}")
    typer.echo(f"Artifact: {result.stored_path}")
    if result.detail:
        typer.echo(result.detail)
    if result.status == "CONFLICT":
        raise typer.Exit(code=2)


@app.command("material-list")
def material_list(
    config: Path | None = typer.Option(None, "--config", "-c"),
    project_root: Path | None = typer.Option(None, "--project-root"),
) -> None:
    """List logical Materials and their current artifact versions."""
    for stable_id, title, course, version, sha256 in list_materials(load_config(config, project_root=project_root)):
        typer.echo(f"{stable_id} | {title} | course={course or '-'} | version={version or '-'} | sha256={sha256 or '-'}")


@app.command("audit-validate")
def audit_validate(path: Path) -> None:
    """Validate a YAML or JSON semester audit."""
    try:
        loaded = load_audit(path)
    except Exception as exc:
        typer.echo(f"INVALID: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"VALID {loaded.audit.audit_id} canonical_sha256={loaded.canonical_content_sha256}")


@app.command("audit-import")
def audit_import(
    path: Path,
    dry_run: bool = typer.Option(False, "--dry-run"),
    resolution: Path | None = typer.Option(None, "--resolution"),
    config: Path | None = typer.Option(None, "--config", "-c"),
    project_root: Path | None = typer.Option(None, "--project-root"),
) -> None:
    """Plan or import a validated semester audit."""
    try:
        result = import_audit(path, load_config(config, project_root=project_root), dry_run=dry_run,
                              resolution_path=resolution)
    except Exception as exc:
        typer.echo(f"IMPORT FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{result.status} revision={result.revision_number} canonical_sha256={result.canonical_content_sha256}")
    for action in result.actions:
        typer.echo(f"  {action.action} {action.entity_type} {action.stable_id} {action.detail}")
    if result.stored_file_path:
        typer.echo(f"Archived: {result.stored_file_path}")
    if result.status == "CONFLICT":
        raise typer.Exit(code=2)


@app.command()
def init(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a UniFlow YAML config."),
    project_root: Path | None = typer.Option(None, "--project-root", help="Override project root."),
) -> None:
    """Create required folders and initialize the SQLite schema safely."""
    app_config = load_config(config, project_root=project_root)
    initialize_project(app_config)
    typer.echo(f"Initialized UniFlow at {app_config.project_root}")
    typer.echo(f"Database: {app_config.database_path}")


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a UniFlow YAML config."),
    project_root: Path | None = typer.Option(None, "--project-root", help="Override project root."),
) -> None:
    """Check basic project health."""
    app_config = load_config(config, project_root=project_root)
    checks = run_doctor(app_config)
    all_ok = True
    for check in checks:
        marker = "OK" if check.ok else "FAIL"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")
        all_ok = all_ok and check.ok
    if not all_ok:
        raise typer.Exit(code=1)


@app.command("db-info")
def db_info(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to a UniFlow YAML config."),
    project_root: Path | None = typer.Option(None, "--project-root", help="Override project root."),
) -> None:
    """Print SQLite schema information."""
    app_config = load_config(config, project_root=project_root)
    info = database_info(app_config.database_path)
    typer.echo(f"Database: {info['database_path']}")
    typer.echo(f"Foreign keys: {info['foreign_keys']}")
    typer.echo("Tables:")
    for table in info["tables"]:
        typer.echo(f"  - {table}")


if __name__ == "__main__":
    app()
