"""Real startup gate on external synthetic Alembic schemas only."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from uniflow.application.semester_read_models import validate_project_database
from uniflow.gui.app import open_project, MainWindow
from uniflow.gui.controller import GuiController
from uniflow.gui.presentation import PresentationError
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import get_engine

ROOT = Path(__file__).resolve().parents[1]


def configured(root):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/default.yaml").write_bytes((ROOT / "config/default.yaml").read_bytes())
    return load_config(project_root=root)


def schema_project(tmp_path, revision):
    config = configured(tmp_path / "project")
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + config.database_path.as_posix())
    command.upgrade(cfg, revision)
    engine = get_engine(config.database_path)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO semesters (id,stable_id,name,state,timezone,created_at,updated_at) "
            "VALUES ('synthetic','SYN','Synthetic','ACTIVE','UTC',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    engine.dispose()
    return config


def schema_state(config):
    engine = get_engine(config.database_path)
    try:
        with engine.connect() as connection:
            return (connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                connection.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all())
    finally:
        engine.dispose()


@pytest.mark.parametrize("revision", ["0016_weekly_plan_block_execution", "0017_audit_resolution", "0018_scoring_review", "0019_scoring_mastery", "0020_study_blocks"])
def test_older_startup_is_read_only(tmp_path, revision):
    config = schema_project(tmp_path, revision)
    before, raw = schema_state(config), config.database_path.read_bytes()
    opened, semesters = open_project(config.project_root)
    assert opened.database_path == config.database_path
    assert [(s.stable_id, s.name, s.state) for s in semesters] == [("SYN", "Synthetic", "ACTIVE")]
    assert schema_state(config) == before
    assert config.database_path.read_bytes() == raw
    if revision in {"0016_weekly_plan_block_execution", "0017_audit_resolution"}:
        assert not any(row[1].startswith("scoring_") for row in before[1])


def test_current_startup_and_persistent_review(tmp_path):
    from test_learning_results import setup
    from test_learning_chat_exchange import result, write
    from uniflow.application.learning_chat_exchange import export_learning_chat_package
    from uniflow.application.scoring_review import save_review, reopen_review, read_review
    config, factory, sid, _, handoff = setup(tmp_path)
    configured(config.project_root)
    before, raw_db = schema_state(config), config.database_path.read_bytes()
    assert before[0] == "0022_study_block_completion"
    validate_project_database(config)
    opened, semesters = open_project(config.project_root)
    assert semesters[0].name == "Synthetic"
    assert schema_state(config) == before
    assert config.database_path.read_bytes() == raw_db
    package = json.loads(export_learning_chat_package(opened, handoff.handoff_id, tmp_path).path.read_bytes())
    path = write(tmp_path, result(package))
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    state = save_review(opened, raw, session_id=sid, handoff_id=handoff.handoff_id,
        handoff_revision=handoff.revision, result_sha256=sha, decisions={"TARGET": "ACCEPTED"},
        reason="Synthetic startup review", expected_version=0, request_id=str(uuid4()), confirmed=True)
    assert state.version == 1
    assert state.decisions == {"TARGET": "ACCEPTED", "PREREQ": "PENDING_HUMAN_REVIEW"}
    path.unlink()
    reopened_config, _ = open_project(config.project_root)
    assert reopen_review(reopened_config, sha, sid, handoff.handoff_id).raw_bytes == raw
    assert read_review(reopened_config, sha, sid, handoff.handoff_id) == state
    factory.kw["bind"].dispose()


@pytest.mark.parametrize("revision", ["0019_future", "0021_future", "invalid"])
def test_unknown_revision_rejected_without_writes(tmp_path, revision):
    config = schema_project(tmp_path, "0017_audit_resolution")
    engine = get_engine(config.database_path)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision})
    engine.dispose()
    before = config.database_path.read_bytes()
    with pytest.raises(ValueError, match="incompatible"):
        open_project(config.project_root)
    assert config.database_path.read_bytes() == before


def test_0017_b3_failure_is_contained_by_real_window(tmp_path, qtbot):
    from uniflow.application.scoring_review import list_reviews
    config = schema_project(tmp_path, "0017_audit_resolution")
    opened, semesters = open_project(config.project_root)
    before, raw = schema_state(config), config.database_path.read_bytes()
    controller = GuiController(opened, semesters[0])
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    results = []
    controller.runner.submit("synthetic_b3_on_0017", None,
        lambda: list_reviews(opened, "synthetic"), lambda op, token, value: results.append(value))
    qtbot.waitUntil(lambda: bool(results), timeout=15000)
    assert isinstance(results[0], PresentationError)
    assert window.isVisible() and window.isEnabled()
    window.buttons[next(iter(window.buttons))].click()
    assert controller.runner.pool.waitForDone(15000)
    qtbot.waitUntil(lambda: not controller.runner._active, timeout=15000)
    assert schema_state(config) == before
    assert config.database_path.read_bytes() == raw
    assert not any(row[1].startswith("scoring_") for row in before[1])
