from __future__ import annotations

from pathlib import Path

import yaml

from uniflow.infrastructure.config import load_config


def test_config_loading_resolves_paths_against_project_root(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    external_drop = tmp_path.parent / "UniFlow Drop"
    config_path = config_dir / "default.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project_root": ".",
                "database_path": "data/test.sqlite3",
                "semester_root": "semesters",
                "archive_root": "archive",
                "imports_root": "imports",
                "handoffs_root": "handoffs",
                "handoff_outgoing_root": "handoffs/outgoing",
                "handoff_inbox_root": "handoffs/inbox",
                "handoff_processed_root": "handoffs/processed",
                "handoff_rejected_root": "handoffs/rejected",
                "logs_root": "logs",
                "external_drop_folder": str(external_drop),
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.project_root == tmp_path.resolve()
    assert config.database_path == tmp_path.resolve() / "data" / "test.sqlite3"
    assert config.external_drop_folder == external_drop.resolve()


def test_project_root_override_keeps_tests_out_of_real_data(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)

    assert config.project_root == tmp_path.resolve()
    assert config.database_path.parent == tmp_path.resolve() / "data"
