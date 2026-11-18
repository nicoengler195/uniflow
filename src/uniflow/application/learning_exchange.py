"""Read-only Handoff verification and deterministic two-file external export."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from uniflow.application.learning_handoffs import _verified_artifact
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import DerivedArtifact, LearningHandoff, LearningSession
from uniflow.schemas.learning_exchange_v1 import (
    LearningExchangeEnvelopeV1, canonical_learning_exchange_envelope_json, result_id_for_handoff,
)
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2
from uniflow.schemas.workflow_v1 import ExportExchangeRequestV1, ExportExchangeSuccessV1


def _identity(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT") from exc
    return value


def _verified_current_details(config, handoff_id: str) -> tuple[bytes, LearningExchangeEnvelopeV1, int]:
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:  # type: Session
        handoff = db.get(LearningHandoff, handoff_id)
        if handoff is None:
            raise WorkflowCommandError("HANDOFF_NOT_FOUND")
        session = db.get(LearningSession, handoff.learning_session_id)
        if session is None or session.id != handoff.learning_session_id:
            raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT")
        if session.current_handoff_id != handoff.id:
            raise WorkflowCommandError("EXCHANGE_HANDOFF_NOT_CURRENT")
        artifact = db.get(DerivedArtifact, handoff.artifact_id)
        if artifact is None:
            raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT")
        try:
            data = _verified_artifact(config, artifact)
            body = LearningHandoffV2.model_validate_json(data)
        except (ValueError, OSError) as exc:
            raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT") from exc
        contract = body.return_contract
        if (body.handoff_id != handoff.id or body.session_id != session.id or
            body.course.id != session.course_id or body.protocol_version != handoff.protocol_version or
            body.schema_version != handoff.schema_version or
            contract.session_id != session.id or contract.handoff_id != handoff.id or
            contract.course_id != session.course_id or
            contract.expected_result_schema_version != "2.0"):
            raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT")
        _identity(session.id)
        _identity(handoff.id)
        try:
            envelope = LearningExchangeEnvelopeV1(protocol_version=body.protocol_version,
                session_id=session.id, handoff_id=handoff.id,
                handoff_artifact_sha256=artifact.sha256,
                result_id=result_id_for_handoff(handoff.id),
                result_filename=contract.expected_filename)
        except ValueError as exc:
            raise WorkflowCommandError("HANDOFF_ARTIFACT_CONFLICT") from exc
        return data, envelope, handoff.revision


def _verified_current(config, handoff_id: str) -> tuple[bytes, LearningExchangeEnvelopeV1]:
    data, envelope, _ = _verified_current_details(config, handoff_id)
    return data, envelope


def _exact_existing(final: Path, handoff_bytes: bytes, envelope_bytes: bytes) -> bool:
    mode = final.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        return False
    children = {item.name for item in final.iterdir()}
    handoff_file = final / "handoff.json"
    envelope_file = final / "handoff-envelope.json"
    return (children == {"handoff.json", "handoff-envelope.json"} and
        not handoff_file.is_symlink() and not envelope_file.is_symlink() and
        handoff_file.stat().st_size == len(handoff_bytes) and
        envelope_file.stat().st_size == len(envelope_bytes) and
        handoff_file.read_bytes() == handoff_bytes and
        envelope_file.read_bytes() == envelope_bytes)


def _existing_outcome(final: Path, handoff_bytes: bytes, envelope_bytes: bytes) -> str:
    try:
        exact = _exact_existing(final, handoff_bytes, envelope_bytes)
    except OSError as exc:
        raise WorkflowCommandError("EXCHANGE_IO_FAILED") from exc
    if not exact:
        raise WorkflowCommandError("EXCHANGE_DESTINATION_CONFLICT")
    return "ALREADY_EXPORTED"


def _cleanup_stage_best_effort(stage: Path) -> None:
    try:
        shutil.rmtree(stage)
    except OSError:
        # A failed cleanup leaves an external orphan; it must not replace the
        # publication error or the result of inspecting a concurrent winner.
        pass


def _write(stage: Path, name: str, body: bytes) -> None:
    with (stage / name).open("xb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())


def export_learning_exchange(config, request: ExportExchangeRequestV1) -> ExportExchangeSuccessV1:
    handoff_bytes, envelope = _verified_current(config, request.handoff_id)
    envelope_bytes = canonical_learning_exchange_envelope_json(envelope)
    try:
        parent = request.destination_directory.resolve(strict=True)
        if not parent.is_dir():
            raise NotADirectoryError
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        raise WorkflowCommandError("EXCHANGE_DESTINATION_INVALID") from exc
    directory_name = f"{envelope.session_id}--{envelope.handoff_id}"
    final = parent / directory_name
    if final.exists() or final.is_symlink():
        outcome = _existing_outcome(final, handoff_bytes, envelope_bytes)
    else:
        stage = None
        try:
            stage = Path(tempfile.mkdtemp(prefix=f".{directory_name}.tmp-", dir=parent))
            _write(stage, "handoff.json", handoff_bytes)
            _write(stage, "handoff-envelope.json", envelope_bytes)
            # On Windows os.rename does not replace an existing destination directory.
            os.rename(stage, final)
            stage = None
            outcome = "EXPORTED"
        except OSError as exc:
            if final.exists() or final.is_symlink():
                outcome = _existing_outcome(final, handoff_bytes, envelope_bytes)
            else:
                raise WorkflowCommandError("EXCHANGE_IO_FAILED") from exc
        finally:
            if stage is not None:
                _cleanup_stage_best_effort(stage)
    return ExportExchangeSuccessV1(outcome=outcome, session_id=envelope.session_id,
        handoff_id=envelope.handoff_id, handoff_artifact_sha256=envelope.handoff_artifact_sha256,
        result_id=envelope.result_id, directory_name=directory_name,
        result_filename=envelope.result_filename)
