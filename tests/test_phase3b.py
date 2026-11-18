from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from typer.testing import CliRunner

from uniflow.application.drop_watcher import scan_drop, watch_drop, list_drop_jobs
from uniflow.application.material_manifest import generate_material_manifest
from uniflow.application.initializer import initialize_project
from uniflow.application.audit_import import import_audit
from uniflow.application.material_intake import import_material
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Course, MaterialReleaseRule, Semester, SourceArtifact
from uniflow.infrastructure.database.engine import initialize_database
from uniflow.infrastructure.write_lock import WriterLockTimeout, project_write_lock
from uniflow.cli.main import app


def setup(tmp_path):
    root = tmp_path / "project"
    drop = tmp_path / "external Drop"
    drop.mkdir()
    config = load_config(project_root=root)
    initialize_project(config)
    local = root / "config" / "local.yaml"
    local.parent.mkdir(exist_ok=True)
    local.write_text(f'external_drop_folder: "{drop.as_posix()}"\ndrop_settle_seconds: 0\ndrop_poll_seconds: 0.01\n', encoding="utf-8")
    return load_config(project_root=root), drop


def test_lock_cross_process_and_release(tmp_path):
    root = tmp_path / "project"
    script = "from pathlib import Path; from uniflow.infrastructure.write_lock import project_write_lock; import sys;\nwith project_write_lock(Path(sys.argv[1]), .1): print('ok')"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    with project_write_lock(root):
        with pytest.raises(WriterLockTimeout, match="Timed out"):
            with project_write_lock(root, .05):
                pass
        held = subprocess.run([sys.executable, "-c", script, str(root)], env=env, capture_output=True, text=True)
        assert held.returncode != 0 and "Timed out" in held.stderr
    free = subprocess.run([sys.executable, "-c", script, str(root)], env=env, capture_output=True, text=True)
    assert free.returncode == 0 and "ok" in free.stdout


def test_local_config_and_drop_lifecycle(tmp_path):
    config, drop = setup(tmp_path)
    assert config.external_drop_folder == drop.resolve()
    file = drop / "notes.md"
    file.write_bytes(b"one")
    assert scan_drop(config)[0].state == "WAITING_STABLE"
    assert scan_drop(config)[0].state == "IMPORTED"
    assert file.read_bytes() == b"one"
    assert scan_drop(config)[0].state == "ALREADY_PROCESSED"
    other = drop / "copy.md"
    other.write_bytes(b"one")
    scan_drop(config)
    assert any(row.state == "DUPLICATE" for row in scan_drop(config))
    file.write_bytes(b"different bytes")
    assert any(row.state == "WAITING_STABLE" for row in scan_drop(config))
    assert any(row.state == "IMPORTED" for row in scan_drop(config))
    (drop / "partial.crdownload").write_bytes(b"partial")
    (drop / "directory").mkdir()
    assert any(row.state == "SKIPPED_TEMPORARY" for row in scan_drop(config))
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SourceArtifact).count() == 2
    assert len(list_drop_jobs(config)) == 3


def test_config_missing_malformed_and_missing_folder(tmp_path):
    root = tmp_path / "project"
    config = load_config(project_root=root)
    assert config.external_drop_folder is None
    local = root / "config" / "local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text("[broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed local configuration"):
        load_config(project_root=root)
    local.write_text(f'external_drop_folder: "{(tmp_path / "missing").as_posix()}"\n', encoding="utf-8")
    config = load_config(project_root=root)
    with pytest.raises(ValueError, match="missing"):
        scan_drop(config)


def test_existing_writers_obey_lock(tmp_path):
    config, drop = setup(tmp_path)
    config = config.model_copy(update={"writer_lock_timeout_seconds": 0.05})
    source = drop / "notes.md"
    source.write_bytes(b"notes")
    audit = Path(__file__).resolve().parents[1] / "examples" / "fictional_semester_audit_v1.yaml"
    with project_write_lock(config.project_root):
        with pytest.raises(WriterLockTimeout):
            import_material(source, config)
        with pytest.raises(WriterLockTimeout):
            import_audit(audit, config)
    assert import_material(source, config).status == "IMPORTED"
    assert import_audit(audit, config).status == "IMPORTED"


def test_dry_run_failure_retry_and_stop(tmp_path, monkeypatch):
    config, drop = setup(tmp_path)
    file = drop / "failed.pdf"
    file.write_bytes(b"content")
    assert scan_drop(config, dry_run=True, sleep=lambda _: None)[0].state == "DRY_RUN_READY"
    assert list_drop_jobs(config) == []
    import uniflow.application.drop_watcher as module
    original = module.import_material
    monkeypatch.setattr(module, "import_material", lambda *_: (_ for _ in ()).throw(ValueError("forced")))
    scan_drop(config)
    assert scan_drop(config)[0].state == "FAILED"
    assert scan_drop(config)[0].state == "FAILED_PREVIOUSLY"
    assert list_drop_jobs(config)[0].attempt_count == 1
    monkeypatch.setattr(module, "import_material", original)
    assert scan_drop(config, retry_failed=True)[0].state == "IMPORTED"
    stop = Event()
    watch_drop(config, stop, on_scan=lambda _: stop.set())
    assert stop.is_set()


def test_manifest_weekly_unresolved_and_inventory(tmp_path):
    config, drop = setup(tmp_path)
    Session = create_session_factory(get_engine(config.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Test", starts_on=date(2026, 10, 12),
                            ends_on=date(2026, 10, 31), timezone="Europe/Berlin")
        course = Course(stable_id="C", name="Course", semester=semester)
        session.add_all([semester, course])
        session.flush()
        from uniflow.domain.enums import InformationStatus
        for stable, status, day, clock in [("R1", InformationStatus.CONFIRMED, "FRIDAY", "08:00:00"),
                                           ("R2", InformationStatus.OBSERVED, "MONDAY", None),
                                           ("R3", InformationStatus.UNKNOWN, None, None)]:
            session.add(MaterialReleaseRule(stable_id=stable, course_id=course.id,
                payload_json=json.dumps({"cadence": "WEEKLY", "weekday": day, "time": clock,
                                         "material_kind": "SHEET"}), information_status=status))
    file = drop / "sheet.pdf"
    file.write_bytes(b"unclassified")
    scan_drop(config)
    scan_drop(config)
    manifest = generate_material_manifest(config, "S", date(2026, 10, 1), date(2026, 11, 10),
        datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc))
    assert [e.id for e in manifest.expectations if e.rule_id == "R1"] == ["R1@2026-10-16", "R1@2026-10-23", "R1@2026-10-30"]
    assert manifest.expectations[0].release_date >= date(2026, 10, 12)
    assert all(e.release_date <= date(2026, 10, 31) for e in manifest.expectations)
    assert {e.information_status for e in manifest.expectations} == {"CONFIRMED", "OBSERVED"}
    assert any(e.release_time is None for e in manifest.expectations)
    assert [r.rule_id for r in manifest.unresolved_rules] == ["R3"]
    assert len(manifest.unclassified_artifacts) == 1
    assert any(e.state == "NOT_LINKED_IN_UNIFLOW" for e in manifest.expectations)
    assert any(e.state == "UPCOMING" for e in manifest.expectations)
    assert any(e.release_time is None and e.information_status == "OBSERVED" for e in manifest.expectations)
    assert any(e.release_time.hour == 8 for e in manifest.expectations if e.release_time)


def test_migration_from_phase3a_preserves_audit_and_material(tmp_path):
    db = tmp_path / "old.sqlite3"
    root = Path(__file__).resolve().parents[1]
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(alembic, "0003_material_intake")
    engine = get_engine(db)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES ('s','S','Semester','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO semester_audits (id,semester_id,schema_version,audit_id,revision_number,canonical_content_sha256,created_at,updated_at) VALUES ('a','s','1.0','A',1,'abc',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO materials (id,title,lifecycle_state,created_at,updated_at) VALUES ('m','Material','DOWNLOADED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO source_artifacts (id,sha256,original_filename,original_source_path,size_bytes,stored_path,created_at) VALUES ('f',:hash,'x.pdf','C:/x.pdf',3,'C:/stored',CURRENT_TIMESTAMP)"), {"hash": "a" * 64})
        connection.execute(text("INSERT INTO material_artifacts (id,material_id,artifact_id,version_number,is_current,linked_at) VALUES ('l','m','f',1,1,CURRENT_TIMESTAMP)"))
    initialize_database(db)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert connection.execute(text("SELECT canonical_content_sha256 FROM semester_audits")).scalar_one() == "abc"
        assert connection.execute(text("SELECT sha256 FROM source_artifacts")).scalar_one() == "a" * 64
        assert connection.execute(text("SELECT COUNT(*) FROM material_artifacts")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_drop_and_manifest_cli(tmp_path):
    config, drop = setup(tmp_path)
    runner = CliRunner()
    root = str(config.project_root)
    assert runner.invoke(app, ["drop-status", "--project-root", root]).exit_code == 0
    assert runner.invoke(app, ["drop-scan", "--project-root", root]).exit_code == 0
    assert runner.invoke(app, ["drop-jobs", "--project-root", root]).exit_code == 0
    audit = Path(__file__).resolve().parents[1] / "examples" / "fictional_semester_audit_v1.yaml"
    import_audit(audit, config)
    result = runner.invoke(app, ["manifest", "--semester", "FICTIONAL_WS2026_27", "--from", "2026-10-12",
        "--through", "2026-10-23", "--as-of", "2026-10-16T06:00:00+00:00", "--project-root", root])
    assert result.exit_code == 0
    assert "FICTIONAL_LINUX_SHEETS@2026-10-16" in result.output
    assert "UNRESOLVED_RULE FICTIONAL_LINUX_NOTES" in result.output
    # The fictional audit cancels a lecture on Nov 2; release facts remain independent.
    before = generate_material_manifest(config, "FICTIONAL_WS2026_27", date(2026, 11, 2),
        date(2026, 11, 2), datetime(2026, 11, 1, tzinfo=timezone.utc))
    after = generate_material_manifest(config, "FICTIONAL_WS2026_27", date(2026, 11, 2),
        date(2026, 11, 2), datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert [e.id for e in before.expectations] == ["FICTIONAL_LINUX_SLIDES@2026-11-02"]
    assert before.expectations[0].state == "UPCOMING"
    assert after.expectations[0].state == "NOT_LINKED_IN_UNIFLOW"
