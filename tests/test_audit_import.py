from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from typer.testing import CliRunner

from uniflow.application.audit_import import import_audit, load_audit
from uniflow.application.doctor import run_doctor
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, AssessmentRequirement, Course, Deadline, MaterialReleaseRule,
    ScheduleEntry, ScheduleException, SemesterAudit,
)
from uniflow.schemas.semester_audit import SemesterAuditV1
from uniflow.cli.main import app

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fictional_semester_audit_v1.yaml"


@pytest.fixture
def setup(tmp_path: Path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    path = tmp_path / "audit.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config, raw, path


def write(path: Path, raw: dict, *, json_format: bool = False) -> Path:
    target = path.with_suffix(".json" if json_format else ".yaml")
    target.write_text(json.dumps(raw, default=str) if json_format else yaml.safe_dump(raw), encoding="utf-8")
    return target


def session_for(config):
    return create_session_factory(get_engine(config.database_path))


def test_phase1_database_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "0001_initial_foundation")
    engine = get_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id, stable_id, name, state, created_at, updated_at) VALUES "
                          "('old', 'OLD', 'Old', 'PLANNING', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        conn.execute(text("DELETE FROM alembic_version"))
    from uniflow.infrastructure.database.engine import initialize_database
    initialize_database(path)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM semesters WHERE stable_id='OLD'")).scalar_one() == "Old"
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0022_study_block_completion"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_init_upgrades_to_head(setup) -> None:
    config, _, _ = setup
    initialize_project(config)
    with get_engine(config.database_path).connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0022_study_block_completion"
    assert all(check.ok for check in run_doctor(config))


def test_yaml_json_and_canonical_hash(setup) -> None:
    _, raw, path = setup
    yaml_loaded = load_audit(path)
    assert yaml_loaded.audit.courses[1].parent_stable_id == "FICTIONAL_PDP"
    changed_format = path.with_name("format.yaml")
    changed_format.write_text("# comment\n" + path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert load_audit(changed_format).canonical_content_sha256 == yaml_loaded.canonical_content_sha256
    assert load_audit(changed_format).original_file_sha256 != yaml_loaded.original_file_sha256
    json_path = write(path, raw, json_format=True)
    assert load_audit(json_path).canonical_content_sha256 == yaml_loaded.canonical_content_sha256


@pytest.mark.parametrize("mutation", ["duplicate", "bad_parent", "cycle", "time_without_date"])
def test_invalid_audits_rejected(setup, mutation: str) -> None:
    _, raw, _ = setup
    if mutation == "duplicate":
        raw["courses"][1]["stable_id"] = raw["courses"][0]["stable_id"]
    elif mutation == "bad_parent":
        raw["courses"][1]["parent_stable_id"] = "MISSING"
    elif mutation == "cycle":
        raw["courses"][0]["parent_stable_id"] = raw["courses"][1]["stable_id"]
    else:
        raw["courses"][0]["deadlines"][0]["due_date"] = None
        raw["courses"][0]["deadlines"][0]["due_time"] = "12:00"
    with pytest.raises(ValueError):
        SemesterAuditV1.model_validate(raw)


def test_import_idempotency_archive_and_derived_entities(setup) -> None:
    config, _, path = setup
    original = path.read_bytes()
    planned = import_audit(path, config, dry_run=True)
    assert planned.status == "READY"
    assert any(a.action == "CREATE" for a in planned.actions)
    with session_for(config)() as session:
        assert session.query(SemesterAudit).count() == 0
    result = import_audit(path, config)
    assert result.status == "IMPORTED" and result.revision_number == 1
    assert path.read_bytes() == original
    assert result.stored_file_path.read_bytes() == original
    assert import_audit(path, config).status == "ALREADY_IMPORTED"
    with session_for(config)() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(Assessment).count() == 1
        assert session.query(Deadline).count() == 1
        assert session.query(Course).count() == 2
        assert session.query(Course).filter_by(stable_id="FICTIONAL_PDP_LINUX").one().parent_course_id is not None
        assert session.query(ScheduleEntry).count() == 2
        assert session.query(ScheduleException).count() == 1
        assert session.query(MaterialReleaseRule).count() == 3
        assert session.query(AssessmentRequirement).count() == 1


def test_changed_audit_revision_and_unknown_to_confirmed(setup) -> None:
    config, raw, path = setup
    raw["courses"][0]["deadlines"][0]["due_date"] = None
    raw["courses"][0]["deadlines"][0]["information_status"] = "UNKNOWN"
    write(path, raw)
    import_audit(path, config)
    raw["audit_id"] = "FICTIONAL_WS2026_27_R2"
    raw["courses"][0]["deadlines"][0]["due_date"] = "2027-01-15"
    raw["courses"][0]["deadlines"][0]["information_status"] = "CONFIRMED"
    write(path, raw)
    result = import_audit(path, config)
    assert result.status == "IMPORTED" and result.revision_number == 2
    with session_for(config)() as session:
        assert session.query(SemesterAudit).count() == 2
        assert session.query(Deadline).one().due_at is not None
        assert session.query(Assessment).count() == 1


def test_conflict_blocks_whole_revision_and_preserves_source(setup) -> None:
    config, raw, path = setup
    import_audit(path, config)
    raw["audit_id"] = "FICTIONAL_WS2026_27_R2"
    raw["courses"][0]["assessments"][0]["name"] = "Different final exam"
    raw["courses"][0]["deadlines"][0]["information_status"] = "UNKNOWN"
    raw["courses"][0]["deadlines"][0]["due_date"] = None
    raw["courses"][1]["schedules"].append({
        "stable_id": "NEW_SCHEDULE", "activity_type": "LECTURE",
        "weekday": "FRIDAY", "start_time": "09:00",
    })
    write(path, raw)
    dry = import_audit(path, config, dry_run=True)
    assert dry.status == "CONFLICT"
    assert any(a.action == "CONFLICT" for a in dry.actions)
    assert any(a.detail == "unknown cannot replace known" for a in dry.actions)
    assert import_audit(path, config).status == "CONFLICT"
    assert path.exists()
    with session_for(config)() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(ScheduleEntry).count() == 2
        assert session.query(Assessment).one().name == "Final exam"
        assert session.query(Deadline).one().due_at is not None


def test_lower_confidence_conflict(setup) -> None:
    config, raw, path = setup
    import_audit(path, config)
    raw["audit_id"] = "FICTIONAL_WS2026_27_R2"
    raw["courses"][0]["assessments"][0]["name"] = "Tentative exam"
    raw["courses"][0]["assessments"][0]["information_status"] = "OBSERVED"
    write(path, raw)
    result = import_audit(path, config, dry_run=True)
    assert any(a.detail == "lower-confidence changed fact" for a in result.actions)


def test_invalid_import_archives_rejected_without_db_changes(setup) -> None:
    config, raw, path = setup
    raw["courses"][0]["deadlines"][0]["due_time"] = "12:00"
    raw["courses"][0]["deadlines"][0]["due_date"] = None
    write(path, raw)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        import_audit(path, config)
    assert path.read_bytes() == original
    assert any(p.read_bytes() == original for p in config.audit_rejected_root.glob("*.yaml"))
    with session_for(config)() as session:
        assert session.query(SemesterAudit).count() == 0


def test_unmanaged_stable_id_is_explicit_conflict(setup) -> None:
    config, _, path = setup
    from uniflow.infrastructure.database.models import Semester
    with session_for(config).begin() as session:
        session.add(Semester(stable_id="FICTIONAL_WS2026_27", name="Existing manual semester"))
    result = import_audit(path, config, dry_run=True)
    assert result.status == "CONFLICT"
    assert any(a.detail == "existing item has no audit provenance" for a in result.actions)


def test_cli_validation_and_dry_run_exit_codes(setup) -> None:
    config, raw, path = setup
    runner = CliRunner()
    assert runner.invoke(app, ["audit-validate", str(path)]).exit_code == 0
    result = runner.invoke(app, ["audit-import", str(path), "--dry-run", "--project-root", str(config.project_root)])
    assert result.exit_code == 0 and "READY" in result.stdout
    raw["courses"][0]["deadlines"][0]["due_date"] = None
    raw["courses"][0]["deadlines"][0]["due_time"] = "12:00"
    write(path, raw)
    assert runner.invoke(app, ["audit-validate", str(path)]).exit_code == 1
