"""Offline document extraction runtime. Source text is data, never adapter instructions."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select

from uniflow.application.document_parsing import read_parsed_unit
from uniflow.application.skill_extraction_foundation import (
    _digest, _input_identity, _scope, _verify_span, build_semantic_input,
)
from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    DerivedArtifact, ParsedUnit, SemanticEvidenceSpan, SemanticExtractionAttempt,
    SemanticExtractionRun, SkillProposal, SkillProposalEvidence, utc_now,
)
from uniflow.infrastructure.write_lock import project_write_lock


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ExtractionFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: str = Field(min_length=1, max_length=160)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SkillCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    kind: str
    description: str | None = Field(default=None, max_length=2000)
    domain: str | None = Field(default=None, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    evidence: list[EvidenceReference] = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"CONCEPT", "ABILITY"}:
            raise ValueError("invalid kind")
        return value

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank name")
        return value


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str
    skills: list[SkillCandidate]

    @field_validator("schema_version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("unsupported schema")
        return value


@dataclass(frozen=True)
class InputItem:
    item_id: str
    parsed_unit_id: str
    unit_ordinal: int
    source_label: str | None
    unit_char_start: int
    unit_char_end: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class ExtractionRequest:
    schema_version: str
    run_id: str
    source_role: str
    input_identity_sha256: str
    academic_provenance: str
    items: tuple[InputItem, ...]
    input_package_sha256: str
    input_manifest_json: str
    # Future adapters must keep source content in items, separate from trusted instructions.


class SemanticExtractorAdapter(Protocol):
    name: str
    version: str
    is_external: bool
    capabilities: frozenset[str]

    def extract(self, request: ExtractionRequest) -> Any: ...


class FixtureAdapter:
    name = "FIXTURE"
    version = "1"
    is_external = False
    capabilities = frozenset({"DOCUMENT"})

    def __init__(self, result: Any):
        self.result = result

    def extract(self, request: ExtractionRequest) -> Any:
        return self.result


def get_adapter(name: str, *, fixture_result: Any = None) -> SemanticExtractorAdapter:
    if name != "FIXTURE":
        raise ExtractionFailure("UNKNOWN_ADAPTER")
    if fixture_result is None:
        raise ExtractionFailure("FIXTURE_RESULT_REQUIRED")
    return FixtureAdapter(fixture_result)


def _session(config: AppConfig):
    return create_session_factory(get_engine(config.database_path))()


def build_document_extraction_request(config: AppConfig, run_id: str) -> ExtractionRequest:
    source = build_semantic_input(config, run_id)
    if source["source_family"] != "DOCUMENT":
        raise ExtractionFailure("UNSUPPORTED_EXTRACTION_SOURCE")
    with _session(config) as session:
        units = {u.id: u for u in session.scalars(select(ParsedUnit).where(ParsedUnit.parse_run_id == source["parse_run_id"])).all()}
    return _document_request(config, source, units)


def _document_request(config: AppConfig, source: dict, units: dict) -> ExtractionRequest:
    """Shared chunking/package construction for verified document text."""
    items: list[InputItem] = []
    total = 0
    for unit in source["units"]:
        metadata = units.get(unit["id"])
        if metadata is None:
            raise ExtractionFailure("EVIDENCE_INVALID")
        value = unit["text"]
        for start in range(0, len(value), config.semantic_max_chunk_chars):
            end = min(start + config.semantic_max_chunk_chars, len(value))
            chunk = value[start:end]
            total += len(chunk.encode("utf-8"))
            if total > config.semantic_max_input_utf8_bytes or len(items) >= config.semantic_max_input_items:
                raise ExtractionFailure("INPUT_TOO_LARGE")
            items.append(InputItem(f"unit:{metadata.id}:{start}:{end}", metadata.id, metadata.ordinal,
                metadata.source_label, start, end, chunk, _sha(chunk.encode("utf-8"))))
    manifest = {"schema_version": "1.0", "chunking_policy": "FIXED_UNICODE_V1",
        "max_chunk_chars": config.semantic_max_chunk_chars, "run_id": source["run_id"],
        "source_role": source["source_role"], "input_identity_sha256": source["input_identity_sha256"],
        "academic_provenance": source["academic_provenance"],
        "items": [{"item_id": i.item_id, "parsed_unit_id": i.parsed_unit_id,
            "unit_ordinal": i.unit_ordinal, "source_label": i.source_label,
            "unit_char_start": i.unit_char_start, "unit_char_end": i.unit_char_end,
            "text_sha256": i.text_sha256} for i in items]}
    body = _json(manifest)
    return ExtractionRequest("1.0", source["run_id"], source["source_role"],
        source["input_identity_sha256"], source["academic_provenance"], tuple(items),
        _sha(body), body.decode("utf-8"))


@dataclass(frozen=True)
class AttemptOutcome:
    attempt_id: str
    attempt_number: int
    status: str
    error_code: str | None
    candidate_count: int
    proposal_count: int


@dataclass(frozen=True)
class DisclosurePreview:
    run_id: str
    source_family: str
    source_role: str
    material_artifact_id: str | None
    provider: str
    model: str
    item_count: int
    utf8_bytes: int
    input_package_sha256: str


def preview_external_document_extraction(config: AppConfig, run_id: str, *, model: str | None = None) -> DisclosurePreview:
    resolved = model or config.semantic_openai_model
    if not resolved or not resolved.strip():
        raise ExtractionFailure("ADAPTER_CONFIG_INVALID")
    request = build_document_extraction_request(config, run_id)
    with _session(config) as session:
        run = session.get(SemanticExtractionRun, run_id)
        material_artifact_id = run.material_artifact_id if run else None
    return DisclosurePreview(run_id, "DOCUMENT", request.source_role, material_artifact_id,
        "OPENAI", resolved, len(request.items), sum(len(i.text.encode("utf-8")) for i in request.items),
        request.input_package_sha256)


def _outcome(attempt: SemanticExtractionAttempt) -> AttemptOutcome:
    return AttemptOutcome(attempt.id, attempt.attempt_number, attempt.status,
        attempt.error_code, attempt.candidate_count, attempt.proposal_count)


def _fail(config: AppConfig, attempt_id: str, code: str) -> AttemptOutcome:
    with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
        with _session(config) as session, session.begin():
            attempt = session.get(SemanticExtractionAttempt, attempt_id)
            if attempt is None or attempt.status != "RUNNING":
                raise ExtractionFailure("ATTEMPT_STATE_CHANGED")
            attempt.status = "FAILED"
            attempt.error_code = code
            attempt.completed_at = utc_now()
            session.flush()
            return _outcome(attempt)


def _validate_response(raw: Any, request: ExtractionRequest, config: AppConfig) -> tuple[ExtractionResponse, bytes]:
    try:
        response = ExtractionResponse.model_validate(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ExtractionFailure("INVALID_RESPONSE") from None
    if len(response.skills) > config.semantic_max_candidates:
        raise ExtractionFailure("INVALID_RESPONSE")
    if sum(len(c.evidence) for c in response.skills) > config.semantic_max_evidence_references:
        raise ExtractionFailure("INVALID_RESPONSE")
    ids = [c.candidate_id for c in response.skills]
    if len(ids) != len(set(ids)):
        raise ExtractionFailure("INVALID_RESPONSE")
    item_map = {i.item_id: i for i in request.items}
    for candidate in response.skills:
        for reference in candidate.evidence:
            item = item_map.get(reference.item_id)
            if item is None or not 0 <= reference.start < reference.end <= len(item.text):
                raise ExtractionFailure("EVIDENCE_INVALID")
    data = _json(response.model_dump(mode="json"))
    if len(data) > config.semantic_max_output_bytes:
        raise ExtractionFailure("OUTPUT_TOO_LARGE")
    return response, data


def _publish(config: AppConfig, session, data: bytes, temp_path: Path) -> tuple[DerivedArtifact, Path | None]:
    digest = _sha(data)
    path = config.derived_root / "sha256" / digest[:2] / digest / "semantic-output.json"
    existing = session.scalar(select(DerivedArtifact).where(DerivedArtifact.sha256 == digest))
    if existing is not None and (existing.kind != "SEMANTIC_EXTRACTION_OUTPUT_JSON" or
        Path(existing.stored_path) != path or existing.size_bytes != len(data)):
        raise ExtractionFailure("OUTPUT_ARTIFACT_CONFLICT")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = None
    if path.exists():
        if path.read_bytes() != data:
            raise ExtractionFailure("OUTPUT_ARTIFACT_CONFLICT")
    else:
        try:
            os.link(temp_path, path)
            created = path
        except FileExistsError:
            if path.read_bytes() != data:
                raise ExtractionFailure("OUTPUT_ARTIFACT_CONFLICT")
    if existing is None:
        existing = DerivedArtifact(sha256=digest, kind="SEMANTIC_EXTRACTION_OUTPUT_JSON",
            stored_path=str(path), size_bytes=len(data))
        session.add(existing)
        session.flush()
    return existing, created


def _finalize(config: AppConfig, attempt_id: str, request: ExtractionRequest,
              response: ExtractionResponse, output: bytes, temp_path: Path) -> AttemptOutcome:
    created_path: Path | None = None
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
                candidate_spans: list[list[tuple[ParsedUnit, int, int, str]]] = []
                for candidate in response.skills:
                    mapped = []
                    for reference in candidate.evidence:
                        item = item_map[reference.item_id]
                        unit = session.get(ParsedUnit, item.parsed_unit_id)
                        if unit is None or unit.parse_run_id != run.parse_run_id:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        original = read_parsed_unit(run.parse_run_id, unit.ordinal, config)
                        if original[item.unit_char_start:item.unit_char_end] != item.text or _sha(item.text.encode("utf-8")) != item.text_sha256:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        start, end = item.unit_char_start + reference.start, item.unit_char_start + reference.end
                        selected = original[start:end]
                        if selected != item.text[reference.start:reference.end]:
                            raise ExtractionFailure("EVIDENCE_INVALID")
                        mapped.append((unit, start, end, _sha(selected.encode("utf-8"))))
                    candidate_spans.append(mapped)
                artifact, created_path = _publish(config, session, output, temp_path)
                proposal_ids = set()
                for candidate, mapped in zip(response.skills, candidate_spans):
                    spans = []
                    for unit, start, end, digest in mapped:
                        span = session.scalar(select(SemanticEvidenceSpan).where(
                            SemanticEvidenceSpan.extraction_run_id == run.id,
                            SemanticEvidenceSpan.extraction_attempt_id.is_not(None),
                            SemanticEvidenceSpan.parsed_unit_id == unit.id,
                            SemanticEvidenceSpan.char_start == start,
                            SemanticEvidenceSpan.char_end == end,
                            SemanticEvidenceSpan.span_sha256 == digest))
                        if span is None:
                            span = SemanticEvidenceSpan(extraction_run_id=run.id, extraction_attempt_id=attempt.id,
                                material_artifact_id=run.material_artifact_id, parsed_unit_id=unit.id,
                                char_start=start, char_end=end, span_sha256=digest)
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


def execute_document_extraction(config: AppConfig, run_id: str, *, adapter_name: str = "FIXTURE",
    fixture_result: Any = None, adapter: SemanticExtractorAdapter | None = None,
    retry_of_attempt_id: str | None = None, allow_external: bool = False,
    model: str | None = None, transport: Any = None) -> AttemptOutcome:
    selected = adapter if adapter is not None else (get_adapter(adapter_name, fixture_result=fixture_result)
        if adapter_name != "OPENAI" else None)
    if selected is None and adapter_name != "OPENAI":
        raise ExtractionFailure("UNKNOWN_ADAPTER")
    selected_name = selected.name if selected is not None else "OPENAI"
    selected_version = selected.version if selected is not None else "1"
    is_external = selected.is_external if selected is not None else True
    if selected is not None and (not selected.name or not selected.version):
        raise ExtractionFailure("UNKNOWN_ADAPTER")
    effective = {"schema_version": "1.0", "max_chunk_chars": config.semantic_max_chunk_chars,
        "max_input_utf8_bytes": config.semantic_max_input_utf8_bytes,
        "max_input_items": config.semantic_max_input_items,
        "max_output_bytes": config.semantic_max_output_bytes,
        "max_candidates": config.semantic_max_candidates,
        "max_evidence_references": config.semantic_max_evidence_references}
    if is_external:
        from uniflow.application.openai_semantic_adapter import PROMPT_SHA256, PROMPT_VERSION
        effective.update({"provider": selected_name, "model": model or config.semantic_openai_model,
            "adapter_version": selected_version, "prompt_version": PROMPT_VERSION,
            "prompt_template_sha256": PROMPT_SHA256, "timeout_seconds": config.semantic_openai_timeout_seconds,
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
            if retry_of_attempt_id:
                prior = session.get(SemanticExtractionAttempt, retry_of_attempt_id)
                if prior is None or prior.run_id != run_id or prior.status != "FAILED":
                    raise ExtractionFailure("RETRY_REQUIRES_FAILED_ATTEMPT")
            number = (session.scalar(select(func.max(SemanticExtractionAttempt.attempt_number)).where(
                SemanticExtractionAttempt.run_id == run_id)) or 0) + 1
            attempt = SemanticExtractionAttempt(run_id=run_id, attempt_number=number,
                adapter_name=selected_name, adapter_version=selected_version, status="RUNNING",
                effective_config_json=effective_json.decode("utf-8"), effective_config_sha256=_sha(effective_json),
                prompt_schema_version=PROMPT_VERSION if is_external else "1.0", external_disclosure_allowed=False,
                provider_name="OPENAI" if selected_name == "OPENAI" else None,
                model_name=(model or config.semantic_openai_model) if selected_name == "OPENAI" else None,
                started_at=utc_now(), candidate_count=0, proposal_count=0)
            session.add(attempt)
            session.flush()
            attempt_id = attempt.id
    if is_external and (config.semantic_external_policy != "ALLOW_WITH_EXPLICIT_CONFIRMATION" or not allow_external):
        return _fail(config, attempt_id, "DISCLOSURE_DENIED")
    if run.source_family != "DOCUMENT" or (selected is not None and "DOCUMENT" not in selected.capabilities):
        return _fail(config, attempt_id, "UNSUPPORTED_EXTRACTION_SOURCE")
    if is_external and (selected_name != "OPENAI" or not (model or config.semantic_openai_model) or
        not (model or config.semantic_openai_model).strip() or
        not config.semantic_openai_api_key_env or not config.semantic_openai_api_key_env.isidentifier()):
        return _fail(config, attempt_id, "ADAPTER_CONFIG_INVALID")
    try:
        request = build_document_extraction_request(config, run_id)
        if is_external and (len(request.items) > config.semantic_external_max_input_items or
            sum(len(i.text.encode("utf-8")) for i in request.items) > config.semantic_external_max_input_utf8_bytes):
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
                    row.authorized_utf8_bytes = sum(len(i.text.encode("utf-8")) for i in request.items)
        if is_external:
            key = os.environ.get(config.semantic_openai_api_key_env, "").strip()
            if not key:
                raise ExtractionFailure("CREDENTIAL_MISSING")
            if selected is None:
                from uniflow.application.openai_semantic_adapter import OpenAISemanticExtractorAdapter
                selected = OpenAISemanticExtractorAdapter(model=model or config.semantic_openai_model,
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
        if str(exc) in {"ACADEMIC_SCOPE_CHANGED", "SOURCE_CHANGED"}:
            return _fail(config, attempt_id, "ACADEMIC_SCOPE_CHANGED")
        return _fail(config, attempt_id, "EVIDENCE_INVALID")
    except Exception:
        return _fail(config, attempt_id, "EVIDENCE_INVALID")


def list_semantic_attempts(config: AppConfig, run_id: str) -> list[SemanticExtractionAttempt]:
    with _session(config) as session:
        return list(session.scalars(select(SemanticExtractionAttempt).where(
            SemanticExtractionAttempt.run_id == run_id).order_by(SemanticExtractionAttempt.attempt_number)).all())


def get_semantic_attempt(config: AppConfig, attempt_id: str) -> SemanticExtractionAttempt:
    with _session(config) as session:
        row = session.get(SemanticExtractionAttempt, attempt_id)
        if row is None:
            raise ExtractionFailure("UNKNOWN_ATTEMPT")
        return row
