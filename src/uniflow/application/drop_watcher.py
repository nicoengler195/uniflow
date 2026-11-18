"""Polling Drop scanner; source files are always copy-only."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from sqlalchemy import select

from uniflow.application.material_intake import import_material
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import DropJob, SourceArtifact
from uniflow.infrastructure.write_lock import project_write_lock


@dataclass(frozen=True)
class DropOutcome:
    path: Path
    state: str
    detail: str = ""


def _observation(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            stream.read(1)
        return stat.st_size, stat.st_mtime_ns
    except (OSError, PermissionError):
        return None


def drop_status(config: AppConfig) -> dict[str, object]:
    folder = config.external_drop_folder
    return {"folder": folder, "exists": bool(folder and folder.is_dir()),
            "readable": bool(folder and folder.is_dir() and _readable(folder)),
            "settle_seconds": config.drop_settle_seconds,
            "poll_seconds": config.drop_poll_seconds}


def _readable(folder: Path) -> bool:
    try:
        next(folder.iterdir(), None)
        return True
    except OSError:
        return False


def scan_drop(config: AppConfig, *, dry_run: bool = False, retry_failed: bool = False,
              sleep: callable = time.sleep) -> list[DropOutcome]:
    folder = config.external_drop_folder
    if folder is None:
        raise ValueError("Drop folder is not configured; use uniflow drop-config PATH")
    if not folder.is_dir():
        raise ValueError(f"Drop folder is missing or not a directory: {folder}")
    Session = create_session_factory(get_engine(config.database_path))
    results: list[DropOutcome] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_file():
            continue
        if any(path.name.lower().endswith(suffix.lower()) for suffix in config.drop_temporary_suffixes):
            results.append(DropOutcome(path, "SKIPPED_TEMPORARY"))
            continue
        first = _observation(path)
        if first is None:
            results.append(DropOutcome(path, "WAITING_STABLE", "unreadable"))
            continue
        if dry_run:
            with Session() as session:
                previous = session.scalar(select(DropJob).where(DropJob.source_path == str(path),
                    DropJob.size_bytes == first[0], DropJob.mtime_ns == first[1]))
                if previous and previous.state in ("IMPORTED", "DUPLICATE"):
                    results.append(DropOutcome(path, "ALREADY_PROCESSED"))
                    continue
                if previous and previous.state == "FAILED" and not retry_failed:
                    results.append(DropOutcome(path, "FAILED_PREVIOUSLY"))
                    continue
            sleep(config.drop_settle_seconds)
            second = _observation(path)
            results.append(DropOutcome(path, "DRY_RUN_READY" if first == second else "WAITING_STABLE"))
            continue
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            with Session.begin() as session:
                job = session.scalar(select(DropJob).where(DropJob.source_path == str(path),
                    DropJob.size_bytes == first[0], DropJob.mtime_ns == first[1]))
                if job is None:
                    job = DropJob(source_path=str(path), observed_filename=path.name,
                        size_bytes=first[0], mtime_ns=first[1], first_seen_at=now,
                        last_seen_at=now, state="WAITING_STABLE")
                    session.add(job)
                    state = "WAITING_STABLE"
                elif job.state in ("IMPORTED", "DUPLICATE"):
                    state = "ALREADY_PROCESSED"
                elif job.state == "IMPORTING" and not retry_failed:
                    state = "IMPORTING"
                elif job.state == "FAILED" and not retry_failed:
                    state = "FAILED_PREVIOUSLY"
                elif (now - job.first_seen_at).total_seconds() < config.drop_settle_seconds:
                    state = "WAITING_STABLE"
                    job.last_seen_at = now
                elif _observation(path) != first:
                    state = "WAITING_STABLE"
                    job.first_seen_at = now
                    job.last_seen_at = now
                else:
                    state = "READY"
                    job.state = "IMPORTING"
                    job.attempt_count += 1
                    job.last_attempt_at = now
                job_id = job.id
                session.flush()
                job_id = job.id
        if state != "READY":
            results.append(DropOutcome(path, state))
            continue
        try:
            intake = import_material(path, config)
            if _observation(path) != first:
                raise ValueError("Source changed during intake; new observation required")
            final_state = "DUPLICATE" if intake.status == "ALREADY_IMPORTED" else "IMPORTED"
            error = None
        except Exception as exc:
            intake = None
            final_state = "FAILED"
            error = exc
        with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            with Session.begin() as session:
                job = session.get(DropJob, job_id)
                job.state = final_state
                job.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
                job.completed_at = job.last_seen_at
                if intake:
                    job.outcome = intake.status
                    artifact = session.scalar(select(SourceArtifact).where(SourceArtifact.sha256 == intake.sha256))
                    job.source_artifact_id = artifact.id if artifact else None
                else:
                    job.error_code = type(error).__name__
                    job.error_message = str(error)
        results.append(DropOutcome(path, final_state, str(error) if error else intake.status))
    return results


def watch_drop(config: AppConfig, stop: Event, *, on_scan=None) -> None:
    """Run scans until stop is signalled. The caller owns lifecycle."""
    if config.drop_poll_seconds <= 0:
        raise ValueError("drop_poll_seconds must be positive for continuous watch")
    while not stop.is_set():
        outcomes = scan_drop(config)
        if on_scan:
            on_scan(outcomes)
        stop.wait(config.drop_poll_seconds)


def list_drop_jobs(config: AppConfig, limit: int = 30) -> list[DropJob]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return session.scalars(select(DropJob).order_by(DropJob.first_seen_at.desc(), DropJob.id).limit(limit)).all()
