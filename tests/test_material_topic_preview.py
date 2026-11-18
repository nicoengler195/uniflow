"""Integration contracts over ordinary synthetic PDF imports, never production."""
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from test_document_parsing import pdf_bytes, setup, session_for
from uniflow.application.document_parsing import parse_source_artifact
from uniflow.application.material_intake import import_material
from uniflow.application.semantic_topic_preview import (
    preview_document_topics, preview_material_topics, read_material_topic_page,
)
from uniflow.application.skill_extraction_foundation import create_semantic_run
from uniflow.infrastructure.database.models import (
    DerivedArtifact, MaterialArtifact, ParseRun, ParsedDocument, ParsedUnit, SemanticExtractionRun,
)


def prepared(tmp_path, pages=("Beschleunigung", "Wurf")):
    cfg, _ = setup(tmp_path)
    source = tmp_path / "lecture.pdf"
    source.write_bytes(pdf_bytes(*pages))
    intake = import_material(source, cfg, material_stable_id="TEST_PDF", title="Testvorlesung")
    parse = parse_source_artifact(intake.sha256, cfg)
    with session_for(cfg)() as db:
        link_id = db.scalar(select(MaterialArtifact.id))
        doc = db.get(ParsedDocument, parse.parse_run_id)
        path = Path(db.get(DerivedArtifact, doc.derived_artifact_id).stored_path)
        assert db.scalar(select(SemanticExtractionRun.id)) is None
    return cfg, link_id, parse.parse_run_id, path


def test_normal_pdf_preview_is_source_backed_and_never_writes(tmp_path):
    cfg, link, parse, _ = prepared(tmp_path)
    before = hashlib.sha256(cfg.database_path.read_bytes()).hexdigest()
    statements = []
    def observed(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(Engine, "before_cursor_execute", observed)
    try:
        result = preview_material_topics(cfg, link, parse)
        assert read_material_topic_page(cfg, link, parse, 2) == "Wurf"
    finally:
        event.remove(Engine, "before_cursor_execute", observed)
    assert statements and set(statements) <= {"SELECT", "PRAGMA"}
    assert hashlib.sha256(cfg.database_path.read_bytes()).hexdigest() == before
    assert [(t['title'], t['evidence']['source_label']) for t in result['topics']] == [
        ("Beschleunigung", "1"), ("Wurf", "2")]
    assert all(t['evidence']['quote'] == t['title'] for t in result['topics'])
    assert result['skill_candidates'] == [] and not result['mastery_changed']


def test_existing_run_matches_and_invalid_identity_never_falls_back(tmp_path):
    cfg, link, parse, _ = prepared(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link, parse_run_id=parse)
    assert preview_material_topics(cfg, link, parse) == preview_document_topics(cfg, run)
    with session_for(cfg).begin() as db:
        db.get(SemanticExtractionRun, run).input_identity_sha256 = "0" * 64
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        preview_material_topics(cfg, link, parse)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unit_hash"])
def test_bad_sources_are_rejected(tmp_path, damage):
    cfg, link, parse, path = prepared(tmp_path)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        with session_for(cfg).begin() as db:
            db.scalar(select(ParsedUnit)).text_sha256 = "0" * 64
    with pytest.raises((ValueError, FileNotFoundError)):
        preview_material_topics(cfg, link, parse)
    with pytest.raises((ValueError, FileNotFoundError)):
        read_material_topic_page(cfg, link, parse, 1)


@pytest.mark.parametrize("change", ["inactive", "wrong_parse", "newer_failed_parse"])
def test_stale_material_or_parse_is_rejected(tmp_path, change):
    cfg, link, parse, _ = prepared(tmp_path)
    if change == "wrong_parse":
        parse = "missing"
    else:
        with session_for(cfg).begin() as db:
            if change == "inactive":
                db.get(MaterialArtifact, link).is_current = False
            else:
                original = db.get(ParseRun, parse)
                db.add(ParseRun(source_artifact_id=original.source_artifact_id,
                    status="FAILED", parser_name=original.parser_name,
                    parser_version=original.parser_version, attempt_number=2,
                    config_json=original.config_json, config_sha256=original.config_sha256,
                    started_at=original.started_at + timedelta(seconds=1),
                    completed_at=original.completed_at + timedelta(seconds=1)))
    with pytest.raises(ValueError):
        preview_material_topics(cfg, link, parse)
    with pytest.raises(ValueError):
        read_material_topic_page(cfg, link, parse, 1)


def test_no_usable_heading_is_an_empty_preview(tmp_path):
    cfg, link, parse, _ = prepared(tmp_path, pages=("x" * 161,))
    assert preview_material_topics(cfg, link, parse)['topics'] == []
