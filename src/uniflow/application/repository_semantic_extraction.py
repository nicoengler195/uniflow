"""Offline semantic extraction over verified, immutable repository blobs."""
from __future__ import annotations

import os
import json
import tempfile
from dataclasses import dataclass
from pathlib import PurePosixPath, Path
from typing import Any

from sqlalchemy import func, select

from uniflow.application.repository_snapshots import read_repository_blob
from uniflow.application.skill_extraction_foundation import STATES, _digest, _input_identity, _scope, _verify_span
from uniflow.application.semantic_extraction_runtime import (
    AttemptOutcome, ExtractionFailure, FixtureAdapter, _fail, _json, _outcome,
    _publish, _sha, _validate_response,
)
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    RepositoryBlob, RepositoryFile, RepositorySnapshot, SemanticEvidenceSpan,
    SemanticExtractionAttempt, SemanticExtractionRun, SkillProposal,
    SkillProposalEvidence, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock

POLICY_VERSION = "repository-text-v1"
ROLES = frozenset({"INSTRUCTION", "EXERCISE", "SOLUTION", "STUDENT_CODE", "TEST", "UNKNOWN"})
EXCLUDED_DIRS = frozenset({".git", "node_modules", "vendor", "target", "build", "dist"})
SECRET_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"})


@dataclass(frozen=True)
class RepositoryInputItem:
    item_id: str
    repository_file_id: str
    relative_path: str
    language_hint: str
    repository_state: str
    repository_blob_id: str
    repository_blob_sha256: str
    chunk_byte_start: int
    chunk_byte_end: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class RepositoryExtractionRequest:
    schema_version: str
    run_id: str
    source_role: str
    repository_state: str
    input_identity_sha256: str
    academic_provenance: str
    items: tuple[RepositoryInputItem, ...]
    input_package_sha256: str
    input_manifest_json: str


@dataclass(frozen=True)
class RepositoryFileEligibility:
    file_id: str
    relative_path: str
    entry_kind: str
    tracked_state: str
    language_hint: str
    state_available: bool
    blob_size: int | None
    eligible: bool
    reason: str | None


@dataclass(frozen=True)
class RepositoryDisclosurePreview:
    run_id: str
    source_role: str
    repository_state: str
    provider: str
    model: str
    selected_files: tuple[tuple[str, str], ...]
    file_count: int
    item_count: int
    utf8_bytes: int
    input_package_sha256: str


def preview_external_repository_extraction(config: AppConfig, run_id: str,
    file_ids: list[str], *, model: str | None = None) -> RepositoryDisclosurePreview:
    resolved = model or config.semantic_openai_model
    if not resolved or not resolved.strip():
        raise ExtractionFailure("ADAPTER_CONFIG_INVALID")
    request = build_repository_extraction_request(config, run_id, file_ids)
    manifest = json.loads(request.input_manifest_json)
    selected = tuple((f["relative_path"], f["tracked_state"]) for f in manifest["selected_files"])
    return RepositoryDisclosurePreview(run_id, request.source_role, request.repository_state,
        "OPENAI", resolved, selected, len(selected), len(request.items),
        sum(len(i.text.encode("utf-8")) for i in request.items), request.input_package_sha256)


def _session(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))()


def _path_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    if any(part.lower() in EXCLUDED_DIRS for part in parts[:-1]):
        return "PATH_EXCLUDED"
    if name in SECRET_NAMES or name.startswith(".env.") or name.endswith((".pem", ".key")):
        return "PATH_EXCLUDED"
    return None


def _file_reason(file: RepositoryFile, snapshot: RepositorySnapshot, state: str,
                 blob: RepositoryBlob | None, config: AppConfig, *, inspect_text: bool) -> str | None:
    if file.entry_kind != "REGULAR":
        return "UNSUPPORTED_ENTRY_KIND"
    reason = _path_reason(file.path)
    if reason:
        return reason
    if file.tracked_state == "UNTRACKED" and (state != "WORKTREE" or not snapshot.include_untracked):
        return "FILE_STATE_MISSING"
    if blob is None:
        return "FILE_STATE_MISSING"
    if blob.size_bytes > config.repository_semantic_max_file_bytes:
        return "INPUT_TOO_LARGE"
    if not inspect_text:
        return None
    data = read_repository_blob(blob.id, config)
    if not data:
        return "EMPTY_SOURCE"
    if b"\x00" in data:
        return "BINARY_FILE_UNSUPPORTED"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "UNSUPPORTED_TEXT_ENCODING"
    return None


def list_repository_semantic_files(config: AppConfig, run_id: str) -> list[RepositoryFileEligibility]:
    with _session(config) as session:
        run = session.get(SemanticExtractionRun, run_id)
        if run is None:
            raise ExtractionFailure("UNKNOWN_RUN")
        if run.source_family != "REPOSITORY":
            raise ExtractionFailure("UNSUPPORTED_EXTRACTION_SOURCE")
        _scope(session, run)
        snap = session.get(RepositorySnapshot, run.repository_snapshot_id)
        files = session.scalars(select(RepositoryFile).where(
            RepositoryFile.snapshot_id == snap.id).order_by(RepositoryFile.path)).all()
        rows = []
        for file in files:
            blob_id = getattr(file, STATES[run.repository_state])
            blob = session.get(RepositoryBlob, blob_id) if blob_id else None
            reason = _file_reason(file, snap, run.repository_state, blob, config, inspect_text=True)
            rows.append(RepositoryFileEligibility(file.id, file.path, file.entry_kind,
                file.tracked_state, file.language_hint, blob is not None,
                blob.size_bytes if blob else None, reason is None, reason))
        return rows


def _offsets(value: str) -> list[int]:
    result = [0]
    for character in value:
        result.append(result[-1] + len(character.encode("utf-8")))
    return result


def _chunks(value: str, offsets: list[int], max_chars: int, max_bytes: int):
    start = 0
    while start < len(value):
        limit = min(len(value), start + max_chars)
        end = start
        while end < limit and offsets[end + 1] - offsets[start] <= max_bytes:
            end += 1
        if end == start:
            raise ExtractionFailure("INPUT_TOO_LARGE")
        if end < len(value):
            # Keep a complete line when one ends inside the bounded window.
            boundary = value.rfind("\n", start, end)
            if boundary >= start:
                end = boundary + 1
            elif value[end - 1] == "\r":
                end -= 1  # keep CRLF together, unless the line exceeds the cap
                if end == start:
                    end = limit
        yield start, end, offsets[start], offsets[end]
        start = end


def build_repository_extraction_request(config: AppConfig, run_id: str,
                                        file_ids: list[str] | tuple[str, ...]) -> RepositoryExtractionRequest:
    if not file_ids:
        raise ExtractionFailure("FILE_SELECTION_REQUIRED")
    selected_ids = set(file_ids)
    if len(selected_ids) > config.repository_semantic_max_files:
        raise ExtractionFailure("INPUT_TOO_LARGE")
    with _session(config) as session:
        run = session.get(SemanticExtractionRun, run_id)
        if run is None:
            raise ExtractionFailure("UNKNOWN_RUN")
        if run.source_family != "REPOSITORY":
            raise ExtractionFailure("UNSUPPORTED_EXTRACTION_SOURCE")
        if run.source_role not in ROLES:
            raise ExtractionFailure("UNSUPPORTED_SOURCE_ROLE")
        provenance = _scope(session, run)
        if _input_identity(session, run) != run.input_identity_sha256:
            raise ExtractionFailure("ACADEMIC_SCOPE_CHANGED")
        snap = session.get(RepositorySnapshot, run.repository_snapshot_id)
        files = [session.get(RepositoryFile, fid) for fid in selected_ids]
        if any(f is None or f.snapshot_id != snap.id for f in files):
            raise ExtractionFailure("UNKNOWN_REPOSITORY_FILE")
        files.sort(key=lambda f: f.path)
        items = []
        selected = []
        total = 0
        for file in files:
            blob_id = getattr(file, STATES[run.repository_state])
            blob = session.get(RepositoryBlob, blob_id) if blob_id else None
            reason = _file_reason(file, snap, run.repository_state, blob, config, inspect_text=False)
            if reason:
                raise ExtractionFailure(reason)
            data = read_repository_blob(blob.id, config)
            if not data:
                raise ExtractionFailure("EMPTY_SOURCE")
            if b"\x00" in data:
                raise ExtractionFailure("BINARY_FILE_UNSUPPORTED")
            try:
                value = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ExtractionFailure("UNSUPPORTED_TEXT_ENCODING") from None
            total += len(data)
            if total > config.repository_semantic_max_total_bytes:
                raise ExtractionFailure("INPUT_TOO_LARGE")
            offsets = _offsets(value)
            selected.append({"repository_file_id": file.id, "relative_path": file.path,
                "tracked_state": file.tracked_state, "repository_blob_id": blob.id,
                "repository_blob_sha256": blob.sha256, "language_hint": file.language_hint})
            for _, _, byte_start, byte_end in _chunks(value, offsets,
                    config.repository_semantic_max_item_chars, config.repository_semantic_max_item_bytes):
                if len(items) >= config.repository_semantic_max_items:
                    raise ExtractionFailure("INPUT_TOO_LARGE")
                text = data[byte_start:byte_end].decode("utf-8")
                items.append(RepositoryInputItem(
                    f"repo:{file.id}:{run.repository_state}:{byte_start}:{byte_end}",
                    file.id, file.path, file.language_hint, run.repository_state,
                    blob.id, blob.sha256, byte_start, byte_end, text, _sha(data[byte_start:byte_end])))
        manifest = {"schema_version": "1.0", "policy_version": POLICY_VERSION,
            "run_id": run.id, "source_role": run.source_role, "repository_state": run.repository_state,
            "input_identity_sha256": run.input_identity_sha256, "academic_provenance": provenance,
            "selected_files": selected,
            "limits": {"files": config.repository_semantic_max_files, "items": config.repository_semantic_max_items,
                "total_bytes": config.repository_semantic_max_total_bytes,
                "file_bytes": config.repository_semantic_max_file_bytes,
                "item_chars": config.repository_semantic_max_item_chars,
                "item_bytes": config.repository_semantic_max_item_bytes},
            "items": [{"item_id": i.item_id, "repository_file_id": i.repository_file_id,
                "repository_blob_id": i.repository_blob_id, "chunk_byte_start": i.chunk_byte_start,
                "chunk_byte_end": i.chunk_byte_end, "text_sha256": i.text_sha256}
                for i in items]}
        encoded = _json(manifest)
        return RepositoryExtractionRequest("1.0", run.id, run.source_role, run.repository_state,
            run.input_identity_sha256, provenance, tuple(items), _sha(encoded), encoded.decode("utf-8"))


def _finalize(config: AppConfig, attempt_id: str, request: RepositoryExtractionRequest,
              response, output: bytes, temp_path: Path) -> AttemptOutcome:
    created_path = None
    try:
        with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            with _session(config) as session, session.begin():
                attempt = session.get(SemanticExtractionAttempt, attempt_id)
                if attempt is None or attempt.status != "RUNNING":
                    raise ExtractionFailure("ATTEMPT_STATE_CHANGED")
                run = session.get(SemanticExtractionRun, attempt.run_id)
                try:
                    _scope(session, run)
                    if _input_identity(session, run) != request.input_identity_sha256:
                        raise ValueError("SOURCE_CHANGED")
                except ValueError:
                    raise ExtractionFailure("ACADEMIC_SCOPE_CHANGED") from None
                if attempt.input_package_sha256 != request.input_package_sha256:
                    raise ExtractionFailure("INPUT_IDENTITY_CHANGED")
                item_map = {i.item_id: i for i in request.items}
                mapped_candidates = []
                for candidate in response.skills:
                    mapped = []
                    for ref in candidate.evidence:
                        item = item_map[ref.item_id]
                        file = session.get(RepositoryFile, item.repository_file_id)
                        blob = session.get(RepositoryBlob, item.repository_blob_id)
                        snap = session.get(RepositorySnapshot, run.repository_snapshot_id)
                        if file is None or file.snapshot_id != snap.id or blob is None or blob.sha256 != item.repository_blob_sha256 or getattr(file, STATES[run.repository_state]) != blob.id:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        if _file_reason(file, snap, run.repository_state, blob, config, inspect_text=False):
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        data = read_repository_blob(blob.id, config)
                        chunk = data[item.chunk_byte_start:item.chunk_byte_end]
                        if chunk != item.text.encode("utf-8") or _sha(chunk) != item.text_sha256:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        offsets = _offsets(item.text)
                        start = item.chunk_byte_start + offsets[ref.start]
                        end = item.chunk_byte_start + offsets[ref.end]
                        chosen = data[start:end]
                        if chosen != item.text[ref.start:ref.end].encode("utf-8") or not chosen:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        mapped.append((file, blob, start, end, _sha(chosen)))
                    mapped_candidates.append(mapped)
                artifact, created_path = _publish(config, session, output, temp_path)
                proposal_ids = set()
                for candidate, mapped in zip(response.skills, mapped_candidates):
                    spans = []
                    for file, blob, start, end, digest in mapped:
                        span = session.scalar(select(SemanticEvidenceSpan).where(
                            SemanticEvidenceSpan.extraction_run_id == run.id,
                            SemanticEvidenceSpan.extraction_attempt_id.is_not(None),
                            SemanticEvidenceSpan.repository_file_id == file.id,
                            SemanticEvidenceSpan.repository_blob_id == blob.id,
                            SemanticEvidenceSpan.repository_state == run.repository_state,
                            SemanticEvidenceSpan.byte_start == start,
                            SemanticEvidenceSpan.byte_end == end,
                            SemanticEvidenceSpan.span_sha256 == digest))
                        if span is None:
                            span = SemanticEvidenceSpan(extraction_run_id=run.id, extraction_attempt_id=attempt.id,
                                repository_academic_link_id=run.repository_academic_link_id,
                                repository_file_id=file.id, repository_blob_id=blob.id,
                                repository_state=run.repository_state, byte_start=start, byte_end=end,
                                span_sha256=digest)
                            session.add(span)
                            session.flush()
                        _verify_span(session, config, span, run)
                        spans.append(span)
                    unique_spans = sorted({s.id: s for s in spans}.values(), key=lambda s: s.id)
                    identity = _digest(["NEW_SKILL", candidate.name, candidate.kind, candidate.description,
                        candidate.domain, None, None, None, None, None, None,
                        sorted((s.id, s.span_sha256, s.char_start, s.char_end, s.byte_start, s.byte_end) for s in unique_spans),
                        run.input_identity_sha256, attempt.adapter_name, attempt.adapter_version,
                        attempt.effective_config_sha256])
                    proposal = session.scalar(select(SkillProposal).where(SkillProposal.identity_sha256 == identity,
                        SkillProposal.status == "PENDING"))
                    if proposal is None:
                        proposal = SkillProposal(extraction_run_id=run.id, extraction_attempt_id=attempt.id,
                            proposal_kind="NEW_SKILL", status="PENDING", identity_sha256=identity,
                            proposed_name=candidate.name, proposed_kind=candidate.kind,
                            proposed_description=candidate.description, proposed_domain=candidate.domain,
                            confidence=candidate.confidence)
                        session.add(proposal)
                        session.flush()
                        for span in unique_spans:
                            session.add(SkillProposalEvidence(proposal_id=proposal.id, evidence_span_id=span.id))
                    proposal_ids.add(proposal.id)
                attempt.status = "SUCCEEDED"
                attempt.completed_at = utc_now()
                attempt.output_sha256 = artifact.sha256
                attempt.output_artifact_id = artifact.id
                attempt.candidate_count = len(response.skills)
                attempt.proposal_count = len(proposal_ids)
                session.flush()
                return _outcome(attempt)
    except Exception:
        if created_path is not None:
            created_path.unlink(missing_ok=True)
        raise


def execute_repository_extraction(config: AppConfig, run_id: str, file_ids: list[str], *,
    fixture_result: Any = None, retry_of_attempt_id: str | None = None,
    adapter: FixtureAdapter | None = None, adapter_name: str = "FIXTURE",
    allow_external: bool = False, model: str | None = None, transport: Any = None) -> AttemptOutcome:
    if adapter_name not in {"FIXTURE", "OPENAI"}:
        raise ExtractionFailure("UNKNOWN_ADAPTER")
    is_external = adapter_name == "OPENAI"
    selected = adapter or (None if is_external else FixtureAdapter(fixture_result))
    if selected is not None and (selected.name != "FIXTURE" or selected.is_external or is_external):
        raise ExtractionFailure("UNKNOWN_ADAPTER")
    selected_version = "1" if is_external else selected.version
    effective = {"schema_version": "1.0", "source_family": "REPOSITORY",
        "policy_version": POLICY_VERSION, "adapter_version": selected_version,
        "max_candidates": config.semantic_max_candidates,
        "max_evidence_references": config.semantic_max_evidence_references,
        "max_output_bytes": config.semantic_max_output_bytes,
        "max_files": config.repository_semantic_max_files,
        "max_items": config.repository_semantic_max_items,
        "max_total_bytes": config.repository_semantic_max_total_bytes,
        "max_file_bytes": config.repository_semantic_max_file_bytes,
        "max_item_chars": config.repository_semantic_max_item_chars,
        "max_item_bytes": config.repository_semantic_max_item_bytes}
    if is_external:
        from uniflow.application.openai_repository_semantic_adapter import PROMPT_SHA256, PROMPT_VERSION, ROLE_GUIDANCE_SHA256
        effective.update({"provider": "OPENAI", "model": model or config.semantic_openai_model,
            "prompt_version": PROMPT_VERSION, "prompt_template_sha256": PROMPT_SHA256,
            "role_guidance_sha256": ROLE_GUIDANCE_SHA256,
            "timeout_seconds": config.semantic_openai_timeout_seconds,
            "external_max_input_utf8_bytes": config.semantic_external_max_input_utf8_bytes,
            "external_max_input_items": config.semantic_external_max_input_items,
            "max_output_tokens": config.semantic_openai_max_output_tokens,
            "max_response_bytes": config.semantic_openai_max_response_bytes,
            "structured_output": "json_schema_strict"})
    effective_json = _json(effective)
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            run = session.get(SemanticExtractionRun, run_id)
            if run is None:
                raise ExtractionFailure("UNKNOWN_RUN")
            if run.source_family != "REPOSITORY":
                raise ExtractionFailure("UNSUPPORTED_EXTRACTION_SOURCE")
            prior_hash = None
            if retry_of_attempt_id:
                prior = session.get(SemanticExtractionAttempt, retry_of_attempt_id)
                if prior is None or prior.run_id != run_id or prior.status != "FAILED" or prior.adapter_name != adapter_name or not prior.input_package_sha256:
                    raise ExtractionFailure("RETRY_REQUIRES_FAILED_ATTEMPT")
                prior_hash = prior.input_package_sha256
            number = (session.scalar(select(func.max(SemanticExtractionAttempt.attempt_number)).where(
                SemanticExtractionAttempt.run_id == run_id)) or 0) + 1
            attempt = SemanticExtractionAttempt(run_id=run_id, attempt_number=number,
                adapter_name=adapter_name, adapter_version=selected_version, status="RUNNING",
                effective_config_json=effective_json.decode("utf-8"),
                effective_config_sha256=_sha(effective_json),
                prompt_schema_version=PROMPT_VERSION if is_external else "repository-input-v1",
                external_disclosure_allowed=False, started_at=utc_now(),
                provider_name="OPENAI" if is_external else None,
                model_name=(model or config.semantic_openai_model) if is_external else None,
                candidate_count=0, proposal_count=0)
            session.add(attempt)
            session.flush()
            attempt_id = attempt.id
    if is_external and (config.semantic_external_policy != "ALLOW_WITH_EXPLICIT_CONFIRMATION" or not allow_external):
        return _fail(config, attempt_id, "DISCLOSURE_DENIED")
    if is_external and (not (model or config.semantic_openai_model) or
        not (model or config.semantic_openai_model).strip() or
        not config.semantic_openai_api_key_env or not config.semantic_openai_api_key_env.isidentifier()):
        return _fail(config, attempt_id, "ADAPTER_CONFIG_INVALID")
    try:
        request = build_repository_extraction_request(config, run_id, file_ids)
        if prior_hash is not None and request.input_package_sha256 != prior_hash:
            raise ExtractionFailure("RETRY_INPUT_MISMATCH")
        authorized_bytes = sum(len(i.text.encode("utf-8")) for i in request.items)
        if is_external and (len(request.items) > config.semantic_external_max_input_items or
            authorized_bytes > config.semantic_external_max_input_utf8_bytes):
            raise ExtractionFailure("INPUT_TOO_LARGE")
        with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            with _session(config) as session, session.begin():
                row = session.get(SemanticExtractionAttempt, attempt_id)
                row.input_package_sha256 = request.input_package_sha256
                row.input_manifest_json = request.input_manifest_json
                if is_external:
                    row.external_disclosure_allowed = True
                    row.authorization_actor = "MANUAL_USER"
                    row.authorization_at = utc_now()
                    row.authorization_policy_version = "external-v1"
                    row.authorized_item_count = len(request.items)
                    row.authorized_utf8_bytes = authorized_bytes
        if is_external:
            key = os.environ.get(config.semantic_openai_api_key_env, "").strip()
            if not key:
                raise ExtractionFailure("CREDENTIAL_MISSING")
            from uniflow.application.openai_repository_semantic_adapter import OpenAIRepositorySemanticAdapter
            selected = OpenAIRepositorySemanticAdapter(model=model or config.semantic_openai_model,
                api_key=key, timeout_seconds=config.semantic_openai_timeout_seconds,
                max_output_tokens=config.semantic_openai_max_output_tokens,
                max_response_bytes=config.semantic_openai_max_response_bytes,
                max_candidates=config.semantic_max_candidates,
                max_evidence_references=config.semantic_max_evidence_references, transport=transport)
            with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
                with _session(config) as session, session.begin():
                    session.get(SemanticExtractionAttempt, attempt_id).external_request_started_at = utc_now()
        try:
            raw = selected.extract(request)
        except ExtractionFailure:
            raise
        except TimeoutError:
            raise ExtractionFailure("TIMEOUT") from None
        except Exception:
            raise ExtractionFailure("ADAPTER_FAILED") from None
        response, output = _validate_response(raw, request, config)
        config.derived_root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".semantic-", dir=config.derived_root)
        temp_path = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(output)
            return _finalize(config, attempt_id, request, response, output, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
    except ExtractionFailure as exc:
        return _fail(config, attempt_id, exc.code)
    except ValueError as exc:
        return _fail(config, attempt_id, "ACADEMIC_SCOPE_CHANGED" if str(exc) in {"ACADEMIC_SCOPE_CHANGED", "SOURCE_CHANGED"} else "EVIDENCE_INVALID")
    except Exception:
        return _fail(config, attempt_id, "EVIDENCE_INVALID")
