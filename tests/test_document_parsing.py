from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from sqlalchemy import select, text
from typer.testing import CliRunner

from uniflow.application.document_parsing import (
    PAGE_SEPARATOR, get_parse_run, parse_source_artifact, read_parsed_document,
    read_parsed_unit,
)
from uniflow.application.doctor import run_doctor
from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material
from uniflow.cli.main import app
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine, initialize_database
from uniflow.infrastructure.database.models import DerivedArtifact, ParseRun, ParsedDocument, ParsedUnit


def setup(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    return config, tmp_path / "source"


def ingest(config, folder, name, data):
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    result = import_material(path, config)
    return result, path


def session_for(config):
    return create_session_factory(get_engine(config.database_path))


def pdf_bytes(*pages):
    """Tiny self-contained PDF with Helvetica text, or blank pages."""
    import io
    writer = PdfWriter()
    for value in pages:
        page = writer.add_blank_page(width=300, height=300)
        if value:
            font = writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }))
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 40 250 Td ({value}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("page_count", [1, 10, 31, 101])
def test_pdf_preserves_every_page_including_blank_pages(tmp_path, page_count):
    config, folder = setup(tmp_path)
    pages = [f"Page {number}" for number in range(1, page_count + 1)]
    if page_count > 1:
        pages[9] = ""
    intake, _ = ingest(config, folder, f"pages-{page_count}.pdf", pdf_bytes(*pages))
    result = parse_source_artifact(intake.sha256, config)
    run, units = get_parse_run(result.parse_run_id, config)
    assert result.status == "SUCCESS"
    assert run.page_count == page_count and run.unit_count == page_count
    assert len(units) == page_count
    assert [unit.source_label for unit in units] == [str(number) for number in range(1, page_count + 1)]
    assert read_parsed_unit(run.id, page_count, config) == pages[-1]
    if page_count > 1:
        assert units[9].non_whitespace_char_count == 0
        assert read_parsed_unit(run.id, 10, config) == ""


def test_text_bom_markdown_and_dedup(tmp_path):
    config, folder = setup(tmp_path)
    intake, source = ingest(config, folder, "notes.txt", b"\xef\xbb\xbfAlpha\r\nBeta\rGamma")
    first = parse_source_artifact(intake.sha256, config)
    assert first.outcome == "SUCCESS" and first.parser_name == "plain_text"
    assert read_parsed_document(first.parse_run_id, config) == "Alpha\nBeta\nGamma"
    assert read_parsed_unit(first.parse_run_id, 1, config) == "Alpha\nBeta\nGamma"
    assert parse_source_artifact(intake.sha256, config).outcome == "ALREADY_PARSED"
    assert source.read_bytes() == b"\xef\xbb\xbfAlpha\r\nBeta\rGamma"
    md, _ = ingest(config, folder, "notes.md", b"# Alpha\r\n[link](x)\r\n```\r\ncode\r\n```")
    result = parse_source_artifact(md.sha256, config)
    assert result.outcome == "SUCCESS"
    assert read_parsed_document(result.parse_run_id, config).startswith("# Alpha\n[link](x)")
    run, units = get_parse_run(result.parse_run_id, config)
    assert run.status == "SUCCESS" and len(units) == 1 and units[0].unit_kind == "DOCUMENT"
    with session_for(config)() as session:
        artifacts = session.scalars(select(DerivedArtifact)).all()
        assert len(artifacts) == 2
        assert artifacts[0].stored_path.startswith(str(config.derived_root))
        assert Path(artifacts[0].stored_path).read_bytes() == b"Alpha\nBeta\nGamma"
        assert artifacts[0].sha256 == hashlib.sha256(b"Alpha\nBeta\nGamma").hexdigest()


def test_pdf_pages_offsets_and_ocr_required(tmp_path):
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "slides.pdf", pdf_bytes("First", "Second"))
    result = parse_source_artifact(intake.sha256, config)
    assert result.outcome == "SUCCESS" and result.dependency_version
    text_value = read_parsed_document(result.parse_run_id, config)
    assert PAGE_SEPARATOR in text_value
    assert read_parsed_unit(result.parse_run_id, 1, config).strip() == "First"
    assert read_parsed_unit(result.parse_run_id, 2, config).strip() == "Second"
    run, units = get_parse_run(result.parse_run_id, config)
    assert run.page_count == 2 and [u.source_label for u in units] == ["1", "2"]
    assert text_value[units[0].char_end:units[1].char_start] == PAGE_SEPARATOR
    blank, _ = ingest(config, folder, "blank.pdf", pdf_bytes(""))
    ocr = parse_source_artifact(blank.sha256, config)
    assert ocr.outcome == "OCR_REQUIRED"
    assert parse_source_artifact(blank.sha256, config).outcome == "ALREADY_PARSED"
    assert read_parsed_unit(ocr.parse_run_id, 1, config) == ""
    sparse, _ = ingest(config, folder, "sparse.pdf", pdf_bytes("x"))
    assert parse_source_artifact(sparse.sha256, config).outcome == "SUCCESS"


def test_failure_retry_and_invalid_encoding(tmp_path):
    config, folder = setup(tmp_path)
    bad_pdf, _ = ingest(config, folder, "bad.pdf", b"not a pdf")
    first = parse_source_artifact(bad_pdf.sha256, config)
    assert first.outcome == "FAILED" and first.error_code == "PDF_MALFORMED"
    assert parse_source_artifact(bad_pdf.sha256, config).outcome == "PREVIOUSLY_FAILED"
    second = parse_source_artifact(bad_pdf.sha256, config, retry_failed=True)
    assert second.outcome == "FAILED" and second.attempt_number == 2
    with session_for(config)() as session:
        assert session.query(ParseRun).filter(ParseRun.source_artifact_id == first.source_artifact_id).count() == 2
    bad_text, _ = ingest(config, folder, "bad.txt", b"\xff")
    assert parse_source_artifact(bad_text.sha256, config).error_code == "UNSUPPORTED_ENCODING"


def test_dry_run_limits_integrity_and_unsupported(tmp_path):
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "notes.txt", b"abc")
    assert parse_source_artifact(intake.sha256, config, dry_run=True).outcome == "DRY_RUN_READY"
    with session_for(config)() as session:
        assert session.query(ParseRun).count() == 0
        assert session.query(DerivedArtifact).count() == 0
    assert not list(config.derived_root.rglob("*.txt"))
    limited = config.model_copy(update={"max_source_bytes_for_parse": 2})
    assert parse_source_artifact(intake.sha256, limited).error_code == "SOURCE_TOO_LARGE"
    assert parse_source_artifact(intake.sha256, config).outcome == "SUCCESS"
    other, _ = ingest(config, folder, "other.txt", b"four")
    limited_text = config.model_copy(update={"max_extracted_text_bytes": 2})
    assert parse_source_artifact(other.sha256, limited_text).error_code == "EXTRACTED_TEXT_TOO_LARGE"
    unsupported, _ = ingest(config, folder, "unknown.bin", b"foo")
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        parse_source_artifact(unsupported.sha256, config)
    intake.stored_path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="SOURCE_INTEGRITY"):
        parse_source_artifact(intake.sha256, config)
    intake.stored_path.unlink()
    with pytest.raises(ValueError, match="SOURCE_INTEGRITY"):
        parse_source_artifact(intake.sha256, config)


def test_derived_reuse_and_corruption(tmp_path):
    config, folder = setup(tmp_path)
    a, _ = ingest(config, folder, "a.txt", b"same\r\ntext")
    b, _ = ingest(config, folder, "b.md", b"same\ntext")
    first = parse_source_artifact(a.sha256, config)
    second = parse_source_artifact(b.sha256, config)
    with session_for(config)() as session:
        assert session.query(DerivedArtifact).count() == 1
        derived = session.query(DerivedArtifact).one()
        path = Path(derived.stored_path)
    assert read_parsed_document(first.parse_run_id, config) == "same\ntext"
    assert read_parsed_document(second.parse_run_id, config) == "same\ntext"
    path.write_bytes(b"different")
    with pytest.raises(ValueError, match="mismatch"):
        read_parsed_document(first.parse_run_id, config)
    c, _ = ingest(config, folder, "c.txt", b"\xef\xbb\xbfsame\ntext")
    with pytest.raises(ValueError, match="mismatch"):
        parse_source_artifact(c.sha256, config)


def test_cli_and_version_change(tmp_path, monkeypatch):
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "notes.txt", b"hello")
    runner = CliRunner()
    root = str(config.project_root)
    dry = runner.invoke(app, ["parse-artifact", intake.sha256, "--dry-run", "--project-root", root])
    assert dry.exit_code == 0 and "DRY_RUN_READY" in dry.stdout
    parsed = runner.invoke(app, ["parse-artifact", intake.sha256, "--project-root", root])
    assert parsed.exit_code == 0 and "SUCCESS" in parsed.stdout
    listing = runner.invoke(app, ["parse-list", "--project-root", root])
    assert listing.exit_code == 0 and "plain_text" in listing.stdout
    with session_for(config)() as session:
        run_id = session.query(ParseRun).one().id
    info = runner.invoke(app, ["parse-info", run_id, "--project-root", root])
    assert info.exit_code == 0 and "unit=1 kind=DOCUMENT" in info.stdout and "hello" not in info.stdout
    import uniflow.application.document_parsing as module
    monkeypatch.setattr(module, "PARSER_VERSION", "2")
    next_version = parse_source_artifact(intake.sha256, config)
    assert next_version.outcome == "SUCCESS" and next_version.parse_run_id != run_id
    with session_for(config)() as session:
        assert session.query(DerivedArtifact).count() == 1


def test_migration_from_0004_preserves_existing_data(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "legacy.sqlite3"
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(alembic, "0004_drop_watcher")
    engine = get_engine(db)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES ('s','S','Test','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO semester_audits (id,semester_id,schema_version,audit_id,revision_number,canonical_content_sha256,created_at,updated_at) VALUES ('a','s','1.0','A',1,'abc',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO materials (id,title,lifecycle_state,created_at,updated_at) VALUES ('m','Test','DOWNLOADED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO source_artifacts (id,sha256,original_filename,original_source_path,size_bytes,stored_path,created_at) VALUES ('f',:hash,'x.txt','C:/x.txt',3,'C:/stored',CURRENT_TIMESTAMP)"), {"hash": "a" * 64})
        conn.execute(text("INSERT INTO material_artifacts (id,material_id,artifact_id,version_number,is_current,linked_at) VALUES ('l','m','f',1,1,CURRENT_TIMESTAMP)"))
    initialize_database(db)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert conn.execute(text("SELECT canonical_content_sha256 FROM semester_audits")).scalar_one() == "abc"
        assert conn.execute(text("SELECT count(*) FROM material_artifacts")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM parse_runs")).scalar_one() == 0
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    config = load_config(project_root=tmp_path)
    assert all(check.ok for check in run_doctor(config))


def test_failed_persistence_removes_new_derived_blob(tmp_path, monkeypatch):
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "notes.txt", b"rollback")
    import uniflow.application.document_parsing as module
    original = module.ParsedDocument

    def fail_document(**kwargs):
        raise RuntimeError("forced database persistence failure")

    monkeypatch.setattr(module, "ParsedDocument", fail_document)
    with pytest.raises(RuntimeError, match="forced"):
        parse_source_artifact(intake.sha256, config)
    monkeypatch.setattr(module, "ParsedDocument", original)
    with session_for(config)() as session:
        assert session.query(DerivedArtifact).count() == 0
        assert session.query(ParseRun).count() == 0
    assert not list(config.derived_root.rglob("content.txt"))


def test_extraction_outside_lock_and_persistence_under_lock(tmp_path, monkeypatch):
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "notes.txt", b"lock")
    import uniflow.application.document_parsing as module
    from uniflow.infrastructure.write_lock import project_write_lock
    from contextlib import contextmanager
    original_parse = module.PlainTextDocumentParser.parse
    checked = {"extraction": False, "persistence": False}

    def parse_without_lock(self, source):
        with project_write_lock(config.project_root, .05):
            checked["extraction"] = True
        return original_parse(self, source)

    @contextmanager
    def tracked_lock(root, timeout):
        with project_write_lock(root, timeout):
            checked["persistence"] = True
            yield

    monkeypatch.setattr(module.PlainTextDocumentParser, "parse", parse_without_lock)
    monkeypatch.setattr(module, "project_write_lock", tracked_lock)
    assert parse_source_artifact(intake.sha256, config).outcome == "SUCCESS"
    assert all(checked.values())


def test_concurrent_equivalent_parses_persist_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "notes.txt", b"concurrent")
    import uniflow.application.document_parsing as module
    barrier = Barrier(2)
    original_parse = module.PlainTextDocumentParser.parse

    def synchronized_parse(self, source):
        barrier.wait(timeout=5)
        return original_parse(self, source)

    monkeypatch.setattr(module.PlainTextDocumentParser, "parse", synchronized_parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(parse_source_artifact, intake.sha256, config) for _ in range(2)]
        outcomes = [future.result() for future in futures]
    assert sorted(outcome.outcome for outcome in outcomes) == ["ALREADY_PARSED", "SUCCESS"]
    with session_for(config)() as session:
        assert session.query(ParseRun).count() == 1
        assert session.query(DerivedArtifact).count() == 1


def test_encrypted_pdf_fails_explicitly(tmp_path):
    import io
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    config, folder = setup(tmp_path)
    intake, _ = ingest(config, folder, "protected.pdf", output.getvalue())
    result = parse_source_artifact(intake.sha256, config)
    assert result.outcome == "FAILED" and result.error_code == "PDF_ENCRYPTED"
