from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from typer.testing import CliRunner
from sqlalchemy import select, text

from uniflow.application.initializer import initialize_project
from uniflow.application.repository_snapshots import (
    RepositoryError, read_repository_blob, read_repository_snapshot,
    register_repository, repository_info, sanitize_remote, snapshot_repository,
)
import uniflow.application.repository_snapshots as repository_service
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import RepositoryBlob
from uniflow.cli.main import app


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return result.stdout.decode().strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = load_config(project_root=root)
    initialize_project(config)
    repo = tmp_path / "student"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "tester@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("target/\n", encoding="utf-8")
    (repo / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (repo / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return config, repo


def files(config, snapshot_id):
    return {f.path: f for f in read_repository_snapshot(snapshot_id, config)[1]}


def test_registration_security_and_clean_snapshot(project):
    config, repo = project
    git(repo, "remote", "add", "origin", "https://user:secret@example.com/class/repo.git?token=x#frag")
    (repo / "sub").mkdir()
    outcome, source_id = register_repository(repo / "sub", config, stable_id="HOMEWORK")
    assert outcome == "REGISTERED"
    assert register_repository(repo, config)[0] == "ALREADY_REGISTERED"
    source, remotes = repository_info(source_id, config)
    assert source.local_path == str(repo)
    assert remotes[0].sanitized_identity == "https://example.com/class/repo.git"
    assert "secret" not in remotes[0].sanitized_identity
    result = snapshot_repository(source_id, config)
    assert result.outcome == "SNAPSHOTTED" and result.build_system_hint == "MAVEN"
    assert snapshot_repository(source_id, config).outcome == "ALREADY_SNAPSHOTTED"
    by_path = files(config, result.snapshot_id)
    main = by_path["Main.java"]
    assert main.head_blob_id == main.index_blob_id == main.working_blob_id
    assert main.language_hint == "JAVA" and main.head_git_oid
    assert read_repository_blob(main.working_blob_id, config) == (repo / "Main.java").read_bytes()
    assert all(not path.startswith(".git/") for path in by_path)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        blob = session.scalar(select(RepositoryBlob).where(RepositoryBlob.id == main.working_blob_id))
        assert blob.sha256 == hashlib.sha256((repo / "Main.java").read_bytes()).hexdigest()


def test_staged_unstaged_untracked_ignored_binary_and_dry_run(project):
    config, repo = project
    _, source_id = register_repository(repo, config)
    first = snapshot_repository(source_id, config)
    (repo / "Main.java").write_text("class Main { int x; }\n", encoding="utf-8")
    git(repo, "add", "Main.java")
    (repo / "Main.java").write_text("class Main { int y; }\n", encoding="utf-8")
    (repo / "notes.hs").write_text("main = pure ()\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00\xff\x80")
    (repo / "target").mkdir()
    (repo / "target" / "generated.bin").write_bytes(b"generated")
    planned = snapshot_repository(source_id, config, dry_run=True)
    assert planned.outcome == "DRY_RUN_READY"
    second = snapshot_repository(source_id, config)
    assert second.identity == planned.identity != first.identity
    by_path = files(config, second.snapshot_id)
    main = by_path["Main.java"]
    assert len({main.head_blob_id, main.index_blob_id, main.working_blob_id}) == 3
    assert by_path["notes.hs"].tracked_state == "UNTRACKED"
    assert by_path["notes.hs"].language_hint == "HASKELL"
    assert read_repository_blob(by_path["binary.bin"].working_blob_id, config) == b"\x00\xff\x80"
    assert "target/generated.bin" not in by_path
    assert snapshot_repository(source_id, config).outcome == "ALREADY_SNAPSHOTTED"
    tracked = snapshot_repository(source_id, config, tracked_only=True)
    assert "notes.hs" not in files(config, tracked.snapshot_id)
    (repo / "target" / "generated.bin").write_bytes(b"different")
    assert snapshot_repository(source_id, config).outcome == "ALREADY_SNAPSHOTTED"


def test_deletion_rename_limits_and_remote_parser(project):
    config, repo = project
    _, source_id = register_repository(repo, config)
    (repo / "Main.java").unlink()
    git(repo, "mv", "pom.xml", "renamed.xml")
    result = snapshot_repository(source_id, config)
    by_path = files(config, result.snapshot_id)
    assert by_path["Main.java"].working_blob_id is None
    assert by_path["renamed.xml"].original_path == "pom.xml"
    assert sanitize_remote("git@example.com:path/repo.git") == "example.com:path/repo.git"
    with pytest.raises(RepositoryError, match="UNSAFE_REMOTE"):
        sanitize_remote("not a remote secret")
    config.max_repository_files = 1
    with pytest.raises(RepositoryError, match="REPOSITORY_FILE_COUNT_LIMIT"):
        snapshot_repository(source_id, config, dry_run=True)


def test_non_git_fails_and_untracked_nested_does_not_recurse(project):
    config, repo = project
    with pytest.raises(RepositoryError, match="NOT_A_GIT_WORKTREE"):
        register_repository(config.project_root, config)
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "private.txt").write_text("inside", encoding="utf-8")
    _, source_id = register_repository(repo, config)
    result = snapshot_repository(source_id, config)
    by_path = files(config, result.snapshot_id)
    assert all("private.txt" not in name for name in by_path)


def test_changed_during_scan_has_no_snapshot_and_retry_succeeds(project, monkeypatch):
    config, repo = project
    _, source_id = register_repository(repo, config)
    original = repository_service._fingerprint
    calls = 0

    def moving(root, include):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "Main.java").write_text("changed while scanning", encoding="utf-8")
        return original(root, include)

    monkeypatch.setattr(repository_service, "_fingerprint", moving)
    with pytest.raises(RepositoryError, match="REPOSITORY_CHANGED"):
        snapshot_repository(source_id, config)
    assert read_snapshot_count(config) == 0
    monkeypatch.setattr(repository_service, "_fingerprint", original)
    assert snapshot_repository(source_id, config).outcome == "SNAPSHOTTED"


def read_snapshot_count(config):
    from sqlalchemy import func
    from uniflow.infrastructure.database.models import RepositorySnapshot
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        return session.scalar(select(func.count()).select_from(RepositorySnapshot))


def test_byte_limits_symlink_and_no_dereference(project):
    config, repo = project
    _, source_id = register_repository(repo, config)
    config.max_repository_file_bytes = 2
    with pytest.raises(RepositoryError, match="REPOSITORY_FILE_LIMIT"):
        snapshot_repository(source_id, config, dry_run=True)
    config.max_repository_file_bytes = 52428800
    config.max_repository_snapshot_bytes = 2
    with pytest.raises(RepositoryError, match="REPOSITORY_SNAPSHOT_LIMIT"):
        snapshot_repository(source_id, config, dry_run=True)
    config.max_repository_snapshot_bytes = 524288000
    target = repo.parent / "outside-secret.txt"
    target.write_text("outside content", encoding="utf-8")
    link = repo / "outside-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    result = snapshot_repository(source_id, config)
    linked = files(config, result.snapshot_id)["outside-link"]
    assert linked.entry_kind == "SYMLINK"
    assert read_repository_blob(linked.working_blob_id, config) != target.read_bytes()


def test_cli_and_no_raw_remote_output(project):
    config, repo = project
    git(repo, "remote", "add", "origin", "https://user:private-token@example.com/repo.git")
    runner = CliRunner()
    root_option = ["--project-root", str(config.project_root)]
    registered = runner.invoke(app, ["repo-register", str(repo), "--stable-id", "CLI_REPO", *root_option])
    assert registered.exit_code == 0 and "private-token" not in registered.output
    for args in (["repo-list"], ["repo-info", "CLI_REPO"]):
        response = runner.invoke(app, [*args, *root_option])
        assert response.exit_code == 0 and "private-token" not in response.output
        assert str(repo) not in response.output
    dry = runner.invoke(app, ["repo-snapshot", "CLI_REPO", "--dry-run", *root_option])
    assert dry.exit_code == 0 and "DRY_RUN_READY" in dry.output
    assert read_snapshot_count(config) == 0
    persisted = runner.invoke(app, ["repo-snapshot", "CLI_REPO", *root_option])
    assert persisted.exit_code == 0 and "SNAPSHOTTED" in persisted.output
    assert runner.invoke(app, ["repo-snapshots", "CLI_REPO", *root_option]).exit_code == 0
    snapshot_id = persisted.output.split("snapshot=")[1].split()[0]
    assert runner.invoke(app, ["repo-snapshot-info", snapshot_id, *root_option]).exit_code == 0


def test_git_unavailable_is_controlled(project, monkeypatch):
    config, repo = project
    monkeypatch.setattr(repository_service.shutil, "which", lambda executable: None)
    with pytest.raises(RepositoryError, match="GIT_NOT_AVAILABLE"):
        register_repository(repo, config)


def test_registration_and_snapshot_leave_git_and_worktree_untouched(project):
    config, repo = project
    tracked = repo / "Main.java"
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain=v2"),
              (repo / ".git" / "index").read_bytes(), (repo / ".git" / "config").read_bytes(), tracked.read_bytes())
    _, source_id = register_repository(repo, config)
    snapshot_repository(source_id, config)
    after = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain=v2"),
             (repo / ".git" / "index").read_bytes(), (repo / ".git" / "config").read_bytes(), tracked.read_bytes())
    assert after == before


def test_0005_to_0006_preserves_audit_and_parse_rows(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = load_config(project_root=root)
    repository_root = Path(__file__).resolve().parents[1]
    alembic = Config(str(repository_root / "alembic.ini"))
    alembic.set_main_option("script_location", str(repository_root / "src/uniflow/infrastructure/database/migrations"))
    config.database_path.parent.mkdir(parents=True)
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{config.database_path}")
    command.upgrade(alembic, "0005_document_parsing")
    engine = get_engine(config.database_path)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES ('s','S','Semester','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO semester_audits (id,semester_id,schema_version,audit_id,revision_number,canonical_content_sha256,created_at,updated_at) VALUES ('a','s','1.0','A',1,'abc',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO source_artifacts (id,sha256,original_filename,original_source_path,size_bytes,stored_path,created_at) VALUES ('f',:hash,'x.txt','local',1,'stored',CURRENT_TIMESTAMP)"), {"hash": "a" * 64})
        connection.execute(text("INSERT INTO derived_artifacts (id,sha256,kind,stored_path,size_bytes,created_at) VALUES ('d',:hash,'PARSED_TEXT','derived',1,CURRENT_TIMESTAMP)"), {"hash": "b" * 64})
        connection.execute(text("INSERT INTO parse_runs (id,source_artifact_id,parser_name,parser_version,dependency_version,config_json,config_sha256,attempt_number,status,started_at,completed_at,derived_artifact_id) VALUES ('p','f','plain_text','1','stdlib','{}',:hash,1,'SUCCESS',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'d')"), {"hash": "c" * 64})
    initialize_project(config)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert connection.execute(text("SELECT audit_id FROM semester_audits")).scalar_one() == "A"
        assert connection.execute(text("SELECT status FROM parse_runs")).scalar_one() == "SUCCESS"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
