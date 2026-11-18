from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_root: Path
    database_path: Path
    artifact_root: Path
    derived_root: Path
    repository_root: Path
    max_repository_files: int = 10000
    max_repository_file_bytes: int = 52428800
    max_repository_snapshot_bytes: int = 524288000
    max_source_bytes_for_parse: int = 209715200
    max_extracted_text_bytes: int = 52428800
    semantic_max_input_utf8_bytes: int = 1048576
    semantic_max_chunk_chars: int = 12000
    semantic_max_input_items: int = 256
    semantic_max_output_bytes: int = 65536
    semantic_max_candidates: int = 50
    semantic_max_evidence_references: int = 200
    semantic_external_policy: str = "DENY"
    semantic_openai_model: str | None = None
    semantic_openai_api_key_env: str = "OPENAI_API_KEY"
    semantic_openai_timeout_seconds: float = 30.0
    semantic_external_max_input_utf8_bytes: int = 131072
    semantic_external_max_input_items: int = 32
    semantic_openai_max_output_tokens: int = 4096
    semantic_openai_max_response_bytes: int = 262144
    repository_semantic_max_files: int = 8
    repository_semantic_max_items: int = 32
    repository_semantic_max_total_bytes: int = 131072
    repository_semantic_max_file_bytes: int = 65536
    repository_semantic_max_item_chars: int = 12000
    repository_semantic_max_item_bytes: int = 49152
    learning_handoff_max_targets: int = 8
    learning_handoff_max_prerequisites: int = 32
    learning_handoff_max_sources: int = 16
    learning_handoff_max_excerpt_utf8_bytes: int = 4096
    learning_handoff_max_total_excerpt_utf8_bytes: int = 65536
    learning_handoff_max_deadlines: int = 16
    learning_handoff_max_assessments: int = 8
    learning_handoff_max_json_bytes: int = 262144
    learning_result_max_raw_bytes: int = 262144
    learning_result_max_json_bytes: int = 131072
    semester_root: Path
    archive_root: Path
    imports_root: Path
    audit_accepted_root: Path
    audit_rejected_root: Path
    handoffs_root: Path
    handoff_outgoing_root: Path
    handoff_inbox_root: Path
    handoff_processed_root: Path
    handoff_rejected_root: Path
    logs_root: Path
    external_drop_folder: Path | None = None
    drop_settle_seconds: float = 2.0
    drop_poll_seconds: float = 2.0
    writer_lock_timeout_seconds: float = 10.0
    drop_temporary_suffixes: list[str] = [".crdownload", ".part", ".tmp"]

    @field_validator("drop_settle_seconds", "drop_poll_seconds", "writer_lock_timeout_seconds")
    @classmethod
    def valid_duration(cls, value: float) -> float:
        if value < 0:
            raise ValueError("duration must be nonnegative")
        return value

    @field_validator("semantic_external_policy")
    @classmethod
    def valid_external_policy(cls, value: str) -> str:
        if value not in {"DENY", "ALLOW_WITH_EXPLICIT_CONFIRMATION"}:
            raise ValueError("invalid external semantic policy")
        return value

    @field_validator("semantic_openai_timeout_seconds")
    @classmethod
    def valid_openai_timeout(cls, value: float) -> float:
        if not 0 < value <= 300:
            raise ValueError("OpenAI timeout must be finite and between 0 and 300 seconds")
        return value

    @field_validator("max_source_bytes_for_parse", "max_extracted_text_bytes", "max_repository_files", "max_repository_file_bytes", "max_repository_snapshot_bytes", "semantic_max_input_utf8_bytes", "semantic_max_chunk_chars", "semantic_max_input_items", "semantic_max_output_bytes", "semantic_max_candidates", "semantic_max_evidence_references", "semantic_external_max_input_utf8_bytes", "semantic_external_max_input_items", "semantic_openai_max_output_tokens", "semantic_openai_max_response_bytes", "repository_semantic_max_files", "repository_semantic_max_items", "repository_semantic_max_total_bytes", "repository_semantic_max_file_bytes", "repository_semantic_max_item_chars", "repository_semantic_max_item_bytes", "learning_handoff_max_targets", "learning_handoff_max_prerequisites", "learning_handoff_max_sources", "learning_handoff_max_excerpt_utf8_bytes", "learning_handoff_max_total_excerpt_utf8_bytes", "learning_handoff_max_deadlines", "learning_handoff_max_assessments", "learning_handoff_max_json_bytes", "learning_result_max_raw_bytes", "learning_result_max_json_bytes")
    @classmethod
    def positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("parse limits must be positive")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def valid_handoff_limits(self):
        fixed_contract_caps = {
            "learning_handoff_max_targets": 8,
            "learning_handoff_max_prerequisites": 32,
            "learning_handoff_max_sources": 16,
            "learning_handoff_max_excerpt_utf8_bytes": 4096,
            "learning_handoff_max_deadlines": 16,
            "learning_handoff_max_assessments": 8,
        }
        if any(getattr(self, key) > cap for key, cap in fixed_contract_caps.items()):
            raise ValueError("handoff limit exceeds V2 contract cap")
        return self

    @property
    def required_directories(self) -> list[Path]:
        return [
            self.project_root,
            self.database_path.parent,
            self.artifact_root,
            self.derived_root,
            self.repository_root,
            self.semester_root,
            self.archive_root,
            self.imports_root,
            self.audit_accepted_root,
            self.audit_rejected_root,
            self.handoffs_root,
            self.handoff_outgoing_root,
            self.handoff_inbox_root,
            self.handoff_processed_root,
            self.handoff_rejected_root,
            self.logs_root,
        ]


def load_config(config_path: Path | str | None = None, *, project_root: Path | str | None = None) -> AppConfig:
    root_hint = Path(project_root).expanduser() if project_root is not None else Path.cwd()
    path = Path(config_path).expanduser() if config_path is not None else root_hint / "config" / "default.yaml"

    raw: dict[str, Any]
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"Configuration must be a YAML mapping: {path}")
    local_path = path.parent / "local.yaml"
    if local_path.exists():
        try:
            local = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed local configuration {local_path}: {exc}") from exc
        if not isinstance(local, dict):
            raise ValueError(f"Local configuration must be a YAML mapping: {local_path}")
        unknown = set(local) - {"external_drop_folder", "drop_settle_seconds", "drop_poll_seconds", "writer_lock_timeout_seconds", "drop_temporary_suffixes", "max_source_bytes_for_parse", "max_extracted_text_bytes", "derived_root", "repository_root", "max_repository_files", "max_repository_file_bytes", "max_repository_snapshot_bytes", "semantic_max_input_utf8_bytes", "semantic_max_chunk_chars", "semantic_max_input_items", "semantic_max_output_bytes", "semantic_max_candidates", "semantic_max_evidence_references", "semantic_external_policy", "semantic_openai_model", "semantic_openai_api_key_env", "semantic_openai_timeout_seconds", "semantic_external_max_input_utf8_bytes", "semantic_external_max_input_items", "semantic_openai_max_output_tokens", "semantic_openai_max_response_bytes", "repository_semantic_max_files", "repository_semantic_max_items", "repository_semantic_max_total_bytes", "repository_semantic_max_file_bytes", "repository_semantic_max_item_chars", "repository_semantic_max_item_bytes", "learning_handoff_max_targets", "learning_handoff_max_prerequisites", "learning_handoff_max_sources", "learning_handoff_max_excerpt_utf8_bytes", "learning_handoff_max_total_excerpt_utf8_bytes", "learning_handoff_max_deadlines", "learning_handoff_max_assessments", "learning_handoff_max_json_bytes", "learning_result_max_raw_bytes", "learning_result_max_json_bytes"}
        if unknown:
            raise ValueError(f"Unknown local configuration keys: {sorted(unknown)}")
        raw.update(local)

    config_base = path.parent.parent if path.name else root_hint
    raw_root = Path(raw.get("project_root") or root_hint).expanduser()
    resolved_root = _resolve_path(raw_root, config_base)
    if project_root is not None:
        resolved_root = Path(project_root).expanduser().resolve()

    imports_root = _resolve_path(raw.get("imports_root", "imports"), resolved_root)
    values = {
        "project_root": resolved_root,
        "database_path": _resolve_path(raw.get("database_path", "data/uniflow.sqlite3"), resolved_root),
        "artifact_root": _resolve_path(raw.get("artifact_root", "data/artifacts"), resolved_root),
        "derived_root": _resolve_path(raw.get("derived_root", "data/derived"), resolved_root),
        "repository_root": _resolve_path(raw.get("repository_root", "data/repositories"), resolved_root),
        "max_repository_files": raw.get("max_repository_files", 10000),
        "max_repository_file_bytes": raw.get("max_repository_file_bytes", 52428800),
        "max_repository_snapshot_bytes": raw.get("max_repository_snapshot_bytes", 524288000),
        "max_source_bytes_for_parse": raw.get("max_source_bytes_for_parse", 209715200),
        "max_extracted_text_bytes": raw.get("max_extracted_text_bytes", 52428800),
        "semantic_max_input_utf8_bytes": raw.get("semantic_max_input_utf8_bytes", 1048576),
        "semantic_max_chunk_chars": raw.get("semantic_max_chunk_chars", 12000),
        "semantic_max_input_items": raw.get("semantic_max_input_items", 256),
        "semantic_max_output_bytes": raw.get("semantic_max_output_bytes", 65536),
        "semantic_max_candidates": raw.get("semantic_max_candidates", 50),
        "semantic_max_evidence_references": raw.get("semantic_max_evidence_references", 200),
        "semantic_external_policy": raw.get("semantic_external_policy", "DENY"),
        "semantic_openai_model": raw.get("semantic_openai_model"),
        "semantic_openai_api_key_env": raw.get("semantic_openai_api_key_env", "OPENAI_API_KEY"),
        "semantic_openai_timeout_seconds": raw.get("semantic_openai_timeout_seconds", 30.0),
        "semantic_external_max_input_utf8_bytes": raw.get("semantic_external_max_input_utf8_bytes", 131072),
        "semantic_external_max_input_items": raw.get("semantic_external_max_input_items", 32),
        "semantic_openai_max_output_tokens": raw.get("semantic_openai_max_output_tokens", 4096),
        "semantic_openai_max_response_bytes": raw.get("semantic_openai_max_response_bytes", 262144),
        "repository_semantic_max_files": raw.get("repository_semantic_max_files", 8),
        "repository_semantic_max_items": raw.get("repository_semantic_max_items", 32),
        "repository_semantic_max_total_bytes": raw.get("repository_semantic_max_total_bytes", 131072),
        "repository_semantic_max_file_bytes": raw.get("repository_semantic_max_file_bytes", 65536),
        "repository_semantic_max_item_chars": raw.get("repository_semantic_max_item_chars", 12000),
        "repository_semantic_max_item_bytes": raw.get("repository_semantic_max_item_bytes", 49152),
        **{key: raw.get(key, default) for key, default in {
            "learning_handoff_max_targets": 8,
            "learning_handoff_max_prerequisites": 32,
            "learning_handoff_max_sources": 16,
            "learning_handoff_max_excerpt_utf8_bytes": 4096,
            "learning_handoff_max_total_excerpt_utf8_bytes": 65536,
            "learning_handoff_max_deadlines": 16,
            "learning_handoff_max_assessments": 8,
            "learning_handoff_max_json_bytes": 262144,
            "learning_result_max_raw_bytes": 262144,
            "learning_result_max_json_bytes": 131072,
        }.items()},
        "semester_root": _resolve_path(raw.get("semester_root", "semesters"), resolved_root),
        "archive_root": _resolve_path(raw.get("archive_root", "archive"), resolved_root),
        "imports_root": imports_root,
        "audit_accepted_root": _resolve_path(raw.get("audit_accepted_root", imports_root / "semester_audits" / "accepted"), resolved_root),
        "audit_rejected_root": _resolve_path(raw.get("audit_rejected_root", imports_root / "semester_audits" / "rejected"), resolved_root),
        "handoffs_root": _resolve_path(raw.get("handoffs_root", "handoffs"), resolved_root),
        "handoff_outgoing_root": _resolve_path(raw.get("handoff_outgoing_root", "handoffs/outgoing"), resolved_root),
        "handoff_inbox_root": _resolve_path(raw.get("handoff_inbox_root", "handoffs/inbox"), resolved_root),
        "handoff_processed_root": _resolve_path(raw.get("handoff_processed_root", "handoffs/processed"), resolved_root),
        "handoff_rejected_root": _resolve_path(raw.get("handoff_rejected_root", "handoffs/rejected"), resolved_root),
        "logs_root": _resolve_path(raw.get("logs_root", "logs"), resolved_root),
        "external_drop_folder": _resolve_optional_path(raw.get("external_drop_folder"), resolved_root),
        "drop_settle_seconds": raw.get("drop_settle_seconds", 2.0),
        "drop_poll_seconds": raw.get("drop_poll_seconds", 2.0),
        "writer_lock_timeout_seconds": raw.get("writer_lock_timeout_seconds", 10.0),
        "drop_temporary_suffixes": raw.get("drop_temporary_suffixes", [".crdownload", ".part", ".tmp"]),
    }
    return AppConfig(**values)


def _resolve_optional_path(value: str | Path | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    return _resolve_path(value, base)


def _resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
