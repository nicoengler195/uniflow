"""Local, deterministic parsing of managed document artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from uniflow.infrastructure.config import AppConfig
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    DerivedArtifact, ParseRun, ParsedDocument, ParsedUnit, SourceArtifact,
)
from uniflow.infrastructure.write_lock import project_write_lock

PARSER_VERSION = "1"
PAGE_SEPARATOR = "\n\f\n"


class ParseFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UnitText:
    kind: str
    label: str | None
    text: str


@dataclass(frozen=True)
class ParsedText:
    format: str
    units: tuple[UnitText, ...]
    page_count: int | None = None


class DocumentParser(Protocol):
    name: str
    format: str
    dependency_version: str

    def parse(self, source: Path) -> ParsedText: ...


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class PlainTextDocumentParser:
    name = "plain_text"
    format = "TXT"
    dependency_version = "stdlib"

    def parse(self, source: Path) -> ParsedText:
        try:
            text = source.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseFailure("UNSUPPORTED_ENCODING", "Document is not valid UTF-8") from exc
        return ParsedText(self.format, (UnitText("DOCUMENT", None, _normalize(text)),))


class MarkdownDocumentParser(PlainTextDocumentParser):
    name = "markdown"
    format = "MARKDOWN"


class PdfDocumentParser:
    name = "pdf"
    format = "PDF"

    @property
    def dependency_version(self) -> str:
        return version("pypdf")

    def parse(self, source: Path) -> ParsedText:
        from pypdf import PdfReader
        with source.open("rb") as stream:
            header = stream.read(5)
        if header != b"%PDF-":
            raise ParseFailure("PDF_MALFORMED", "PDF header is missing")
        try:
            reader = PdfReader(str(source), strict=True)
            if reader.is_encrypted:
                raise ParseFailure("PDF_ENCRYPTED", "Encrypted PDF requires a password")
            units = tuple(UnitText("PAGE", str(number), _normalize(page.extract_text() or ""))
                          for number, page in enumerate(reader.pages, 1))
            if not units:
                raise ParseFailure("PDF_NO_PAGES", "PDF contains no pages")
            return ParsedText("PDF", units, len(units))
        except ParseFailure:
            raise
        except Exception as exc:
            raise ParseFailure("PDF_MALFORMED", f"PDF extraction failed: {type(exc).__name__}") from exc


PARSERS: dict[str, DocumentParser] = {
    ".pdf": PdfDocumentParser(),
    ".txt": PlainTextDocumentParser(),
    ".md": MarkdownDocumentParser(),
    ".markdown": MarkdownDocumentParser(),
}
MIME_PARSERS: dict[str, DocumentParser] = {
    "application/pdf": PARSERS[".pdf"],
    "text/plain": PARSERS[".txt"],
    "text/markdown": PARSERS[".md"],
}


def select_parser(artifact: SourceArtifact) -> DocumentParser:
    """A recognized recorded extension wins; MIME is fallback only."""
    suffix = (artifact.extension or "").lower()
    parser = PARSERS.get(suffix)
    if parser is not None:
        return parser
    if suffix:
        raise ValueError(f"UNSUPPORTED_FORMAT: {suffix}")
    parser = MIME_PARSERS.get((artifact.mime_type or "").lower())
    if parser is None:
        raise ValueError("UNSUPPORTED_FORMAT: no supported extension or MIME hint")
    return parser


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_file(path: Path, sha256: str, size: int) -> None:
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            actual_size = 0
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                actual_size += len(block)
    except OSError as exc:
        raise ValueError(f"SOURCE_INTEGRITY: stored artifact missing or unreadable: {path}") from exc
    if actual_size != size or digest.hexdigest() != sha256:
        raise ValueError(f"SOURCE_INTEGRITY: stored artifact hash/size mismatch: {path}")


def _identity_query(artifact_id: str, parser: DocumentParser, config_hash: str):
    return select(ParseRun).where(
        ParseRun.source_artifact_id == artifact_id,
        ParseRun.parser_name == parser.name,
        ParseRun.parser_version == PARSER_VERSION,
        ParseRun.dependency_version == parser.dependency_version,
        ParseRun.config_sha256 == config_hash,
    ).order_by(ParseRun.attempt_number.desc())


@dataclass(frozen=True)
class ParseOutcome:
    outcome: str
    source_artifact_id: str
    parser_name: str
    parser_version: str
    dependency_version: str
    config_sha256: str
    parse_run_id: str | None = None
    attempt_number: int | None = None
    status: str | None = None
    error_code: str | None = None


def _outcome(label: str, artifact_id: str, parser: DocumentParser, config_hash: str,
             run: ParseRun | None = None) -> ParseOutcome:
    return ParseOutcome(label, artifact_id, parser.name, PARSER_VERSION,
        parser.dependency_version, config_hash, run.id if run else None,
        run.attempt_number if run else None, run.status if run else None,
        run.error_code if run else None)


def _existing(run: ParseRun | None, retry_failed: bool) -> str | None:
    if run is None:
        return None
    if run.status in ("SUCCESS", "OCR_REQUIRED"):
        return "ALREADY_PARSED"
    if run.status == "FAILED" and not retry_failed:
        return "PREVIOUSLY_FAILED"
    return None


def _resolve(session, identifier: str) -> SourceArtifact:
    artifact = session.get(SourceArtifact, identifier)
    if artifact is None and len(identifier) == 64 and all(c in "0123456789abcdef" for c in identifier.lower()):
        artifact = session.scalar(select(SourceArtifact).where(SourceArtifact.sha256 == identifier.lower()))
    if artifact is None:
        raise ValueError(f"Unknown SourceArtifact UUID or full SHA-256: {identifier}")
    return artifact


def _document_text(parsed: ParsedText) -> tuple[str, list[tuple[UnitText, int, int]]]:
    separator = PAGE_SEPARATOR if parsed.format == "PDF" else ""
    chunks: list[str] = []
    units: list[tuple[UnitText, int, int]] = []
    offset = 0
    for ordinal, unit in enumerate(parsed.units):
        if ordinal:
            chunks.append(separator)
            offset += len(separator)
        start = offset
        chunks.append(unit.text)
        offset += len(unit.text)
        units.append((unit, start, offset))
    return "".join(chunks), units


def _derived_path(root: Path, sha256: str) -> Path:
    return root / "sha256" / sha256[:2] / sha256 / "content.txt"


def parse_source_artifact(identifier: str, config: AppConfig, *, retry_failed: bool = False,
                          dry_run: bool = False) -> ParseOutcome:
    if not config.database_path.is_file():
        raise ValueError("Database is not initialized; run uniflow init")
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        artifact = _resolve(session, identifier)
        artifact_id, source_path = artifact.id, Path(artifact.stored_path)
        source_hash, source_size = artifact.sha256, artifact.size_bytes
        parser = select_parser(artifact)
        config_json = json.dumps({"page_separator": PAGE_SEPARATOR if parser.format == "PDF" else "",
                                  "newline": "LF", "encoding": "utf-8-sig" if parser.format != "PDF" else None,
                                  "max_source_bytes_for_parse": config.max_source_bytes_for_parse,
                                  "max_extracted_text_bytes": config.max_extracted_text_bytes},
                                 sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        config_hash = _sha(config_json.encode("utf-8"))
        previous = session.scalar(_identity_query(artifact_id, parser, config_hash))
        previous_result = _existing(previous, retry_failed)
    _verify_file(source_path, source_hash, source_size)
    if previous_result:
        return _outcome(previous_result, artifact_id, parser, config_hash, previous)
    if dry_run:
        return _outcome("DRY_RUN_READY", artifact_id, parser, config_hash, previous)

    started = datetime.now(timezone.utc)
    parsed: ParsedText | None = None
    text_data: bytes | None = None
    text_units: list[tuple[UnitText, int, int]] = []
    error: ParseFailure | None = None
    try:
        if source_size > config.max_source_bytes_for_parse:
            raise ParseFailure("SOURCE_TOO_LARGE", "Source exceeds configured parse size limit")
        parsed = parser.parse(source_path)
        normalized, text_units = _document_text(parsed)
        text_data = normalized.encode("utf-8")
        if len(text_data) > config.max_extracted_text_bytes:
            raise ParseFailure("EXTRACTED_TEXT_TOO_LARGE", "Extracted text exceeds configured size limit")
    except ParseFailure as exc:
        error = exc
    except Exception as exc:
        error = ParseFailure("PARSE_ERROR", f"Parser failed: {type(exc).__name__}")
    completed = datetime.now(timezone.utc)
    _verify_file(source_path, source_hash, source_size)

    temp_path: Path | None = None
    if text_data is not None:
        config.derived_root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".parsed-", dir=config.derived_root)
        temp_path = Path(name)
        with os.fdopen(fd, "wb") as output:
            output.write(text_data)
    created_path: Path | None = None
    try:
        with project_write_lock(config.project_root, config.writer_lock_timeout_seconds):
            with Session.begin() as session:
                previous = session.scalar(_identity_query(artifact_id, parser, config_hash))
                previous_result = _existing(previous, retry_failed)
                if previous_result:
                    return _outcome(previous_result, artifact_id, parser, config_hash, previous)
                derived: DerivedArtifact | None = None
                if text_data is not None:
                    digest = _sha(text_data)
                    path = _derived_path(config.derived_root, digest)
                    derived = session.scalar(select(DerivedArtifact).where(DerivedArtifact.sha256 == digest))
                    if path.exists():
                        _verify_file(path, digest, len(text_data))
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        assert temp_path is not None
                        try:
                            os.link(temp_path, path)
                            created_path = path
                        except FileExistsError:
                            _verify_file(path, digest, len(text_data))
                    if derived is not None:
                        if derived.kind != "PARSED_TEXT" or Path(derived.stored_path) != path or derived.size_bytes != len(text_data):
                            raise ValueError("DERIVED_INTEGRITY: metadata mismatch")
                    else:
                        derived = DerivedArtifact(sha256=digest, kind="PARSED_TEXT",
                                                  stored_path=str(path), size_bytes=len(text_data))
                        session.add(derived)
                        session.flush()
                status = "FAILED" if error else ("OCR_REQUIRED" if parsed and parsed.format == "PDF" and
                    sum(not c.isspace() for unit in parsed.units for c in unit.text) == 0 else "SUCCESS")
                run = ParseRun(source_artifact_id=artifact_id, parser_name=parser.name,
                    parser_version=PARSER_VERSION, dependency_version=parser.dependency_version,
                    config_json=config_json, config_sha256=config_hash,
                    attempt_number=(previous.attempt_number + 1 if previous else 1),
                    status=status, started_at=started, completed_at=completed,
                    derived_artifact=derived, page_count=parsed.page_count if parsed else None,
                    unit_count=len(text_units) if parsed else None,
                    char_count=len(normalized) if parsed else None,
                    error_code=error.code if error else None,
                    error_message=str(error)[:500] if error else None)
                session.add(run)
                session.flush()
                if parsed and derived and text_data is not None:
                    session.add(ParsedDocument(parse_run_id=run.id, format=parsed.format,
                        derived_artifact_id=derived.id, normalized_text_sha256=derived.sha256,
                        char_count=len(normalized), non_whitespace_char_count=sum(not c.isspace() for c in normalized),
                        unit_count=len(text_units)))
                    for ordinal, (unit, start, end) in enumerate(text_units, 1):
                        session.add(ParsedUnit(parse_run_id=run.id, ordinal=ordinal, unit_kind=unit.kind,
                            source_label=unit.label, char_start=start, char_end=end,
                            text_sha256=_sha(unit.text.encode("utf-8")), char_count=len(unit.text),
                            non_whitespace_char_count=sum(not c.isspace() for c in unit.text)))
                outcome = _outcome(status, artifact_id, parser, config_hash, run)
            return outcome
    except Exception:
        if created_path is not None:
            created_path.unlink(missing_ok=True)
        raise
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def list_parse_runs(config: AppConfig, limit: int = 30) -> list[ParseRun]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return session.scalars(select(ParseRun).order_by(ParseRun.started_at.desc(), ParseRun.id).limit(limit)).all()


def get_parse_run(parse_run_id: str, config: AppConfig) -> tuple[ParseRun, list[ParsedUnit]]:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        run = session.get(ParseRun, parse_run_id)
        if run is None:
            raise ValueError(f"Unknown ParseRun: {parse_run_id}")
        units = session.scalars(select(ParsedUnit).where(ParsedUnit.parse_run_id == parse_run_id)
                                .order_by(ParsedUnit.ordinal)).all()
        session.expunge(run)
        for unit in units:
            session.expunge(unit)
        return run, units


def read_parsed_document(parse_run_id: str, config: AppConfig) -> str:
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        document = session.get(ParsedDocument, parse_run_id)
        if document is None:
            raise ValueError(f"ParseRun has no ParsedDocument: {parse_run_id}")
        derived = session.get(DerivedArtifact, document.derived_artifact_id)
        if derived is None:
            raise ValueError("DERIVED_INTEGRITY: DerivedArtifact row missing")
        path = Path(derived.stored_path)
        _verify_file(path, derived.sha256, derived.size_bytes)
        data = path.read_bytes()
        if _sha(data) != document.normalized_text_sha256:
            raise ValueError("DERIVED_INTEGRITY: document hash mismatch")
        text = data.decode("utf-8")
        if len(text) != document.char_count:
            raise ValueError("DERIVED_INTEGRITY: document character count mismatch")
        return text


def read_parsed_unit(parse_run_id: str, ordinal: int, config: AppConfig) -> str:
    text = read_parsed_document(parse_run_id, config)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        unit = session.scalar(select(ParsedUnit).where(ParsedUnit.parse_run_id == parse_run_id,
                                                       ParsedUnit.ordinal == ordinal))
        if unit is None:
            raise ValueError(f"Unknown ParsedUnit ordinal {ordinal} for {parse_run_id}")
        if not (0 <= unit.char_start <= unit.char_end <= len(text)):
            raise ValueError("DERIVED_INTEGRITY: unit offsets invalid")
        value = text[unit.char_start:unit.char_end]
        if _sha(value.encode("utf-8")) != unit.text_sha256 or len(value) != unit.char_count:
            raise ValueError("DERIVED_INTEGRITY: unit text mismatch")
        return value
