"""Read-only V1 topic preview over the existing verified semantic request.

Heuristics identify literal source text, never abilities or mathematical truth.
No adapter is invoked and no proposals/mastery are persisted.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import select

from uniflow.application.semantic_extraction_runtime import (
    ExtractionRequest, _document_request, build_document_extraction_request,
)
from uniflow.application.skill_extraction_foundation import (
    _digest, _document_scope, _require, _selected_unit_text_in_session, _session,
)
from uniflow.infrastructure.database.models import (
    Material, MaterialArtifact, ParseRun, ParsedDocument, ParsedUnit,
    SemanticExtractionRun, SourceArtifact,
)

POLICY_VERSION = "literal-topic-preview-v1"


def _reference(item, start: int, end: int) -> dict:
    text = item.text[start:end]
    return {"item_id": item.item_id, "parsed_unit_id": item.parsed_unit_id,
            "source_label": item.source_label, "unit_ordinal": item.unit_ordinal,
            "start": start, "end": end,
            "unit_char_start": item.unit_char_start + start,
            "unit_char_end": item.unit_char_start + end,
            "quote": text, "span_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def preview_topics(request: ExtractionRequest) -> dict:
    """Extract first-line heading candidates and bullet labels, retaining exact spans.

    Only a unit's first chunk can supply a heading. Heading candidacy is a
    layout heuristic, not semantic classification. Detected mathematical lines are
    conservatively unverified; detection completeness is not guaranteed.
    """
    topics, conditions, warnings = [], [], []
    for item in request.items:
        if hashlib.sha256(item.text.encode("utf-8")).hexdigest() != item.text_sha256:
            raise ValueError("INPUT_TEXT_HASH_MISMATCH")
        if item.unit_char_end - item.unit_char_start != len(item.text):
            raise ValueError("INPUT_RANGE_MISMATCH")
        offset = 0
        first_nonblank = True
        for line in item.text.splitlines(keepends=True):
            stripped = line.strip()
            left = len(line) - len(line.lstrip())
            start, end = offset + left, offset + left + len(stripped)
            if stripped:
                ref = _reference(item, start, end)
                if first_nonblank and item.unit_char_start == 0 and len(stripped) <= 160:
                    topics.append({"title": stripped, "basis": "EXPLICIT_TEXT",
                                   "method": "FIRST_LINE_HEADING_CANDIDATE", "evidence": ref})
                # A colon after a bullet label marks an explicit subtopic.
                match = re.match(r"[•*\-]\s*([^:?!]{2,90}):", stripped)
                if match:
                    a, b = start + match.start(1), start + match.end(1)
                    topics.append({"title": item.text[a:b], "basis": "EXPLICIT_TEXT",
                                   "method": "BULLET_LABEL", "evidence": _reference(item, a, b)})
                if re.search(r"\b(bei|wenn|ohne|konstant\w*|Spezialfall)\b", stripped, re.I):
                    conditions.append({"basis": "EXPLICIT_TEXT", "interpretation": "UNVERIFIED_CONDITION_EXCERPT",
                                       "evidence": ref})
                if re.search(r"[=Δθ²³∫]|\b(?:sin|cos|lim)\b|[A-Za-z]\d", stripped):
                    warnings.append({"code": "FORMULA_STRUCTURE_UNVERIFIED", "evidence": ref,
                                     "reason": "Text extraction does not verify fractions, powers, indices or layout."})
                first_nonblank = False
            offset += len(line)
        warnings.append({"code": "GRAPHIC_CONTENT_UNVERIFIED", "item_id": item.item_id,
                         "parsed_unit_id": item.parsed_unit_id, "source_label": item.source_label,
                         "reason": "Text-only input cannot establish diagram content or completeness."})
    return {"schema_version": "1.0", "policy_version": POLICY_VERSION,
            "input_package_sha256": request.input_package_sha256,
            "status": "V1_SEMANTIC_EXTRACTION_MVP_PARTIAL",
            "provider_status": "NO_LOCAL_SEMANTIC_PROVIDER_CONFIGURED",
            "topics": topics, "conditions": conditions, "warnings": warnings,
            "skill_candidates": [], "skill_status": "NOT_EXECUTED_NO_LOCAL_PROVIDER",
            "human_review_required": True, "mastery_changed": False,
            "limitations": ["Heading candidates can include cover titles and questions.",
                            "Only first-line headings and colon-delimited bullet labels are recognized.",
                            "No semantic understanding, formula repair, OCR or graphic inference.",
                            "Condition excerpts can be incomplete across lines; inspect original page."]}


def preview_document_topics(config, run_id: str) -> dict:
    """Reuse verified Parsed Units and foundation scope; do not create a run."""
    return preview_topics(build_document_extraction_request(config, run_id))


def _material_request(config, link_id: str, parse_run_id: str):
    """Bind a current material/parse to verified units, without creating a run."""
    with _session(config) as session:
        provenance = _document_scope(session, link_id, parse_run_id)
        link = session.get(MaterialArtifact, link_id)
        _require(session.get(Material, link.material_id), "MATERIAL_NOT_FOUND")
        latest = session.scalar(select(ParseRun.id).where(
            ParseRun.source_artifact_id == link.artifact_id).order_by(
                ParseRun.completed_at.desc(), ParseRun.started_at.desc(),
                ParseRun.attempt_number.desc(), ParseRun.id).limit(1))
        if latest != parse_run_id:
            raise ValueError("SOURCE_CHANGED")
        artifact = _require(session.get(SourceArtifact, link.artifact_id), "SOURCE_CHANGED")
        document = session.get(ParsedDocument, parse_run_id)
        units = {u.id: u for u in session.scalars(select(ParsedUnit).where(
            ParsedUnit.parse_run_id == parse_run_id).order_by(ParsedUnit.ordinal)).all()}
        if not units:
            raise ValueError("EVIDENCE_INVALID")
        source = {"run_id": "", "source_role": "UNKNOWN", "academic_provenance": provenance,
            "input_identity_sha256": _digest(["DOCUMENT", link.id, artifact.id, artifact.sha256,
                parse_run_id, document.normalized_text_sha256, "UNKNOWN"]),
            "units": [{"id": u.id, "text": _selected_unit_text_in_session(session, parse_run_id, u)}
                for u in units.values()]}
        run_id = session.scalar(select(SemanticExtractionRun.id).where(
            SemanticExtractionRun.source_family == "DOCUMENT",
            SemanticExtractionRun.material_artifact_id == link_id,
            SemanticExtractionRun.parse_run_id == parse_run_id).order_by(
                SemanticExtractionRun.created_at.desc(), SemanticExtractionRun.id).limit(1))
        return _document_request(config, source, units), run_id


def preview_material_topics(config, link_id: str, parse_run_id: str) -> dict:
    """Read-only preview for imported materials, retaining existing run validation."""
    request, run_id = _material_request(config, link_id, parse_run_id)
    return preview_document_topics(config, run_id) if run_id else preview_topics(request)


def read_material_topic_page(config, link_id: str, parse_run_id: str, ordinal: int) -> str:
    """Revalidate the material binding and source hashes when opening its page."""
    request, run_id = _material_request(config, link_id, parse_run_id)
    if run_id:
        request = build_document_extraction_request(config, run_id)
    items = [item for item in request.items if item.unit_ordinal == ordinal]
    if not items:
        raise ValueError("EVIDENCE_INVALID")
    return "".join(item.text for item in items)
