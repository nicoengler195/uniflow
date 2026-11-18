from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text
from typer.testing import CliRunner

from uniflow.application.doctor import run_doctor
from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material
from uniflow.cli.main import app
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine, initialize_database
from uniflow.infrastructure.database.models import Course, Material, MaterialArtifact, Semester, SourceArtifact

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    source = tmp_path / "outside" / "lecture.pdf"
    source.parent.mkdir()
    source.write_bytes(b"lecture version one")
    return config, source


def session_for(config):
    return create_session_factory(get_engine(config.database_path))


def test_migration_from_phase2_preserves_rows_and_hash(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "0002_semester_audit")
    engine = get_engine(db)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES "
                          "('s','TEST_SEMESTER','Real','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO semester_audits (id,semester_id,schema_version,audit_id,revision_number,canonical_content_sha256,created_at,updated_at) VALUES "
                          "('a','s','1.0','TEST_SEMESTER_AUDIT',1,:hash,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
                     {"hash": "ff2f7134a3f8c77ae5fe41e274dd48b9a207c2ba6652f1c11f006e9f9d701d17"})
        conn.execute(text("INSERT INTO materials (id,title,lifecycle_state,content_hash,created_at,updated_at) VALUES "
                          "('m','Legacy','DOWNLOADED',:hash,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {"hash": "a" * 64})
    initialize_database(db)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0022_study_block_completion"
        assert conn.execute(text("SELECT canonical_content_sha256 FROM semester_audits WHERE id='a'")).scalar_one().startswith("ff2f")
        assert conn.execute(text("SELECT content_hash FROM materials WHERE id='m'")).scalar_one() == "a" * 64
        assert conn.execute(text("SELECT COUNT(*) FROM source_artifacts")).scalar_one() == 0
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_copy_dedup_versions_and_unclassified(setup):
    config, source = setup
    original = source.read_bytes()
    first = import_material(source, config, material_stable_id="M1", title="Lecture")
    assert first.status == "IMPORTED"
    assert source.read_bytes() == original
    assert first.stored_path.read_bytes() == original
    assert first.stored_path == config.artifact_root / "sha256" / first.sha256[:2] / first.sha256 / "content"
    assert import_material(source, config, material_stable_id="M1").status == "ALREADY_IMPORTED"
    assert import_material(source, config, material_stable_id="M2").status == "LINKED_EXISTING_ARTIFACT"
    assert import_material(source, config).status == "ALREADY_IMPORTED"

    renamed = source.with_name("different-name.bin")
    renamed.write_bytes(original)
    assert import_material(renamed, config).sha256 == first.sha256
    assert import_material(renamed, config).status == "ALREADY_IMPORTED"

    changed = source.parent / "same-name" / source.name
    changed.parent.mkdir()
    changed.write_bytes(b"lecture version two")
    assert import_material(changed, config, material_stable_id="M1").status == "CONFLICT"
    assert not import_material(changed, config, material_stable_id="M1", dry_run=True).stored_path.exists()
    second = import_material(changed, config, material_stable_id="M1", new_version=True)
    assert second.status == "NEW_MATERIAL_VERSION" and second.version_number == 2
    assert second.sha256 != first.sha256
    assert first.stored_path.read_bytes() == original
    assert second.stored_path.read_bytes() == changed.read_bytes()
    with session_for(config)() as session:
        assert session.query(SourceArtifact).count() == 2
        assert session.query(Material).count() == 2
        assert session.query(MaterialArtifact).count() == 3
        links = session.scalars(select(MaterialArtifact).join(Material).where(Material.stable_id == "M1")).all()
        assert sorted((link.version_number, link.is_current) for link in links) == [(1, False), (2, True)]
    assert all(check.ok for check in run_doctor(config))


def test_dry_run_validation_and_cli(setup):
    config, source = setup
    result = import_material(source, config, material_stable_id="M1", dry_run=True)
    assert result.status == "DRY_RUN_IMPORTED" and not result.stored_path.exists()
    with session_for(config)() as session:
        assert session.query(SourceArtifact).count() == 0
    with pytest.raises(ValueError, match="Unknown course"):
        import_material(source, config, course_stable_id="MISSING", material_stable_id="M1")
    with pytest.raises(ValueError, match="INVALID_SOURCE"):
        import_material(source.parent / "missing.pdf", config)
    with pytest.raises(ValueError, match="INVALID_SOURCE"):
        import_material(source.parent, config)
    runner = CliRunner()
    args = ["--project-root", str(config.project_root)]
    assert runner.invoke(app, ["material-import", str(source), "--stable-id", "M1", *args]).exit_code == 0
    listed = runner.invoke(app, ["material-list", *args])
    assert listed.exit_code == 0 and "M1" in listed.stdout


def test_course_linked_material_and_legacy_hash_conflict(setup):
    config, source = setup
    with session_for(config).begin() as session:
        semester = Semester(stable_id="TEST_TERM", name="Temporary term")
        session.add(Course(semester=semester, stable_id="TEST_COURSE", name="Temporary course"))
        session.add(Material(stable_id="LEGACY", title="Old material", content_hash="a" * 64))
    linked = import_material(source, config, course_stable_id="TEST_COURSE",
                             material_stable_id="COURSE_MATERIAL")
    assert linked.status == "IMPORTED"
    with session_for(config)() as session:
        assert session.query(Material).filter_by(stable_id="COURSE_MATERIAL").one().course.stable_id == "TEST_COURSE"
    assert import_material(source, config, material_stable_id="LEGACY").status == "CONFLICT"
    assert import_material(source, config, material_stable_id="LEGACY", new_version=True).status == "LINKED_EXISTING_ARTIFACT"
    with session_for(config)() as session:
        assert session.query(Material).filter_by(stable_id="LEGACY").one().content_hash == "a" * 64


def test_db_failure_cleans_new_artifact(setup, monkeypatch):
    config, source = setup
    import uniflow.application.material_intake as intake
    real_factory = intake.create_session_factory

    def failing_factory(engine):
        factory = real_factory(engine)
        @event.listens_for(factory, "before_flush")
        def fail(_session, _context, _instances):
            raise RuntimeError("forced database failure")
        return factory

    monkeypatch.setattr(intake, "create_session_factory", failing_factory)
    with pytest.raises(RuntimeError, match="forced database failure"):
        import_material(source, config, material_stable_id="M1")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert not (config.artifact_root / "sha256" / sha[:2] / sha / "content").exists()
    with session_for(config)() as session:
        assert session.query(SourceArtifact).count() == 0
