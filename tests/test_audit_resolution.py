"""Exact reconciliation against disposable semester projects only."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from uniflow.application.audit_import import _facts, fact_sha256, import_audit, load_audit, load_resolution
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import AuditFactRecord, AuditResolutionRecord, SemesterAudit

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional_semester_audit_v1.yaml"


def dump(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def scenario(tmp_path: Path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    audit = tmp_path / "audit.yaml"
    dump(audit, raw)
    first = import_audit(audit, config)
    raw["audit_id"] = "FICTIONAL_REVISION_2"
    raw["courses"][0]["assessments"][0]["name"] = "Corrected examination"
    dump(audit, raw)
    loaded = load_audit(audit)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        old = session.scalar(select(AuditFactRecord).where(AuditFactRecord.stable_id == raw["courses"][0]["assessments"][0]["stable_id"]))
        entity, stable_id, payload, status, _, _ = next(item for item in _facts(loaded.audit) if item[1] == old.stable_id)
        current_hash = fact_sha256(old.entity_type, old.stable_id, old.payload_json, old.information_status)
        proposed = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        proposed_hash = fact_sha256(entity, stable_id, proposed, status)
        old_revision = old.audit_revision_id
    manifest = {
        "schema_version": "1.0", "kind": "semester_audit_resolution",
        "semester": loaded.audit.semester, "audit_id": loaded.audit.audit_id,
        "candidate_canonical_sha256": loaded.canonical_content_sha256,
        "resolutions": [{
            "entity_type": entity, "stable_id": stable_id,
            "expected_current_fact_sha256": current_hash, "proposed_fact_sha256": proposed_hash,
            "expected_current_revision_id": old_revision, "action": "ACCEPT_REPLACEMENT",
            "reason": "Reviewed correction", "actor": "MANUAL_USER",
            "decided_at": "2026-10-17T12:00:00+02:00",
        }],
    }
    resolution = tmp_path / "resolution.yaml"
    dump(resolution, manifest)
    return config, audit, resolution, manifest, first


def test_exact_resolution_dry_run_import_history_and_replay(scenario):
    config, audit, resolution, manifest, first = scenario
    audit_loaded = load_audit(audit)
    raw_hash = hashlib.sha256(resolution.read_bytes()).hexdigest()
    loaded = load_resolution(resolution)
    reordered = copy.deepcopy(manifest)
    reordered["resolutions"].reverse()
    dump(resolution.with_name("other.yaml"), reordered)
    assert load_resolution(resolution.with_name("other.yaml")).canonical_content_sha256 == loaded.canonical_content_sha256
    assert import_audit(audit, config, dry_run=True).status == "CONFLICT"
    dry = import_audit(audit, config, dry_run=True, resolution_path=resolution)
    assert dry.status == "READY"
    assert sum(a.action == "RESOLVED_UPDATE" for a in dry.actions) == 1
    assert len(list(config.audit_accepted_root.rglob("*resolution*"))) == 0
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    result = import_audit(audit, config, resolution_path=resolution)
    assert result.status == "IMPORTED" and result.revision_number == 2
    assert hashlib.sha256(result.stored_file_path.read_bytes()).hexdigest() == audit_loaded.original_file_sha256
    with Session() as session:
        assert session.query(SemesterAudit).count() == 2
        assert session.query(AuditResolutionRecord).count() == 1
        record = session.query(AuditResolutionRecord).one()
        current = session.get(AuditFactRecord, record.stable_id)
        assert record.old_fact_sha256 == manifest["resolutions"][0]["expected_current_fact_sha256"]
        assert record.new_fact_sha256 == manifest["resolutions"][0]["proposed_fact_sha256"]
        assert record.previous_audit_revision_id != current.audit_revision_id
        assert current.audit_revision_id == record.audit_revision_id
        assert json.loads(record.old_payload_json)["name"] != json.loads(record.new_payload_json)["name"]
        assert record.resolution_raw_sha256 == raw_hash
        assert record.resolution_canonical_sha256 == loaded.canonical_content_sha256
        assert hashlib.sha256(Path(record.resolution_stored_file_path).read_bytes()).hexdigest() == raw_hash
        revision = session.get(SemesterAudit, record.audit_revision_id)
        assert revision.original_file_sha256 == audit_loaded.original_file_sha256
        assert revision.canonical_content_sha256 == audit_loaded.canonical_content_sha256
    assert first.stored_file_path.exists() and result.stored_file_path.exists()
    assert import_audit(audit, config, resolution_path=resolution).status == "ALREADY_IMPORTED"
    with Session() as session:
        assert session.query(AuditResolutionRecord).count() == 1


@pytest.mark.parametrize("field,value", [
    ("expected_current_fact_sha256", "0" * 64), ("proposed_fact_sha256", "0" * 64),
    ("expected_current_revision_id", "wrong"), ("stable_id", "WRONG"),
    ("entity_type", "course"),
])
def test_stale_or_extraneous_decision_fails_closed(scenario, field, value):
    config, audit, resolution, manifest, _ = scenario
    manifest["resolutions"][0][field] = value
    dump(resolution, manifest)
    with pytest.raises(ValueError):
        import_audit(audit, config, resolution_path=resolution)
    assert not list(config.audit_accepted_root.rglob("*resolution*"))


@pytest.mark.parametrize("field,value", [
    ("candidate_canonical_sha256", "0" * 64), ("semester", "WRONG"), ("audit_id", "WRONG"),
])
def test_wrong_candidate_fails_closed(scenario, field, value):
    config, audit, resolution, manifest, _ = scenario
    manifest[field] = value
    dump(resolution, manifest)
    with pytest.raises(ValueError, match="candidate identity mismatch"):
        import_audit(audit, config, resolution_path=resolution)


def test_duplicate_and_invalid_manifest_rejected(scenario):
    config, audit, resolution, manifest, _ = scenario
    manifest["resolutions"].append(copy.deepcopy(manifest["resolutions"][0]))
    dump(resolution, manifest)
    with pytest.raises(ValueError):
        import_audit(audit, config, resolution_path=resolution)
    manifest["resolutions"].pop()
    manifest["resolutions"][0]["decided_at"] = "2026-10-17T12:00:00"
    dump(resolution, manifest)
    with pytest.raises(ValueError):
        import_audit(audit, config, resolution_path=resolution)


def test_partial_manifest_blocks_all_writes(scenario):
    config, audit, resolution, manifest, _ = scenario
    raw = yaml.safe_load(audit.read_text(encoding="utf-8"))
    raw["courses"][0]["name"] = "Second independent correction"
    dump(audit, raw)
    manifest["candidate_canonical_sha256"] = load_audit(audit).canonical_content_sha256
    dump(resolution, manifest)
    plan = import_audit(audit, config, dry_run=True, resolution_path=resolution)
    assert plan.status == "CONFLICT"
    assert sum(a.action == "RESOLVED_UPDATE" for a in plan.actions) == 1
    assert sum(a.action == "CONFLICT" for a in plan.actions) == 1
    assert import_audit(audit, config, resolution_path=resolution).status == "CONFLICT"
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    assert not list(config.audit_accepted_root.rglob("*resolution*"))


def test_lower_confidence_requires_exact_approval(scenario):
    config, audit, resolution, manifest, _ = scenario
    raw = yaml.safe_load(audit.read_text(encoding="utf-8"))
    assessment = raw["courses"][0]["assessments"][0]
    assessment["information_status"] = "OBSERVED"
    dump(audit, raw)
    loaded = load_audit(audit)
    manifest["candidate_canonical_sha256"] = loaded.canonical_content_sha256
    entity, stable_id, payload, status, _, _ = next(item for item in _facts(loaded.audit) if item[1] == assessment["stable_id"])
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["resolutions"][0]["proposed_fact_sha256"] = fact_sha256(entity, stable_id, serialized, status)
    dump(resolution, manifest)
    assert import_audit(audit, config, dry_run=True).status == "CONFLICT"
    assert import_audit(audit, config, dry_run=True, resolution_path=resolution).status == "READY"
    assert import_audit(audit, config, resolution_path=resolution).status == "IMPORTED"
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.get(AuditFactRecord, stable_id).information_status.value == "OBSERVED"


def test_resolution_for_no_change_create_or_ordinary_update_is_rejected(scenario):
    config, audit, resolution, manifest, _ = scenario
    raw = yaml.safe_load(audit.read_text(encoding="utf-8"))
    for variant in ("NO_CHANGE", "CREATE", "UPDATE"):
        changed = copy.deepcopy(raw)
        if variant == "NO_CHANGE":
            changed["courses"][0]["assessments"][0]["name"] = "Final exam"
        elif variant == "CREATE":
            changed["courses"][0]["assessments"][0]["name"] = "Final exam"
            new_assessment = copy.deepcopy(changed["courses"][0]["assessments"][0])
            new_assessment["stable_id"] = "NEW_ASSESSMENT"
            changed["courses"][0]["assessments"].append(new_assessment)
        else:
            # UNKNOWN -> known on a different existing fact, while decision still targets assessment.
            changed["courses"][0]["assessments"][0]["name"] = "Final exam"
            changed["courses"][0]["deadlines"][0]["due_date"] = "2027-01-16"
        dump(audit, changed)
        manifest["candidate_canonical_sha256"] = load_audit(audit).canonical_content_sha256
        dump(resolution, manifest)
        with pytest.raises(ValueError, match="extraneous"):
            import_audit(audit, config, resolution_path=resolution)
    dump(audit, raw)


def test_cli_resolution_and_safe_default(scenario):
    from typer.testing import CliRunner
    from uniflow.cli.main import app
    config, audit, resolution, _, _ = scenario
    runner = CliRunner()
    args = ["audit-import", str(audit), "--project-root", str(config.project_root)]
    assert runner.invoke(app, args + ["--dry-run"]).exit_code == 2
    dry = runner.invoke(app, args + ["--dry-run", "--resolution", str(resolution)])
    assert dry.exit_code == 0 and "RESOLVED_UPDATE" in dry.stdout
    assert runner.invoke(app, args + ["--resolution", str(resolution)]).exit_code == 0


def test_current_fact_change_after_approval_is_stale(scenario):
    config, audit, resolution, manifest, _ = scenario
    Session = create_session_factory(get_engine(config.database_path))
    with Session.begin() as session:
        old = session.get(AuditFactRecord, manifest["resolutions"][0]["stable_id"])
        payload = json.loads(old.payload_json)
        payload["name"] = "Intervening audited change"
        old.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with pytest.raises(ValueError, match="stale resolution"):
        import_audit(audit, config, resolution_path=resolution)
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    assert not list(config.audit_accepted_root.rglob("*resolution*"))


@pytest.mark.parametrize("change", ["unmanaged", "entity_collision", "status_mismatch"])
def test_hard_conflicts_cannot_be_resolved(scenario, change):
    config, audit, resolution, manifest, _ = scenario
    stable_id = manifest["resolutions"][0]["stable_id"]
    Session = create_session_factory(get_engine(config.database_path))
    with Session.begin() as session:
        old = session.get(AuditFactRecord, stable_id)
        if change == "unmanaged":
            session.delete(old)
        elif change == "entity_collision":
            old.entity_type = "course"
        else:
            old.information_status = "UNKNOWN"
    with pytest.raises(ValueError):
        import_audit(audit, config, resolution_path=resolution)
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0


def test_unknown_cannot_replace_known_and_omitted_fact_remains(scenario):
    config, audit, resolution, _, _ = scenario
    raw = yaml.safe_load(audit.read_text(encoding="utf-8"))
    omitted = raw["courses"][1]["schedules"][1]["stable_id"]
    raw["courses"][1]["schedules"] = [item for item in raw["courses"][1]["schedules"]
                                       if item["stable_id"] != omitted]
    changed = raw["courses"][0]["assessments"][0]
    changed["name"] = "Final exam"
    changed["information_status"] = "UNKNOWN"
    dump(audit, raw)
    plan = import_audit(audit, config, dry_run=True)
    assert plan.status == "READY"
    assert any(a.action == "NO_CHANGE" and a.detail == "unknown cannot replace known" for a in plan.actions)
    assert not any(a.stable_id == omitted for a in plan.actions)
    assert import_audit(audit, config).status == "IMPORTED"
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.get(AuditFactRecord, omitted) is not None


def test_v2_shaped_counts_in_temporary_project(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    base = {
        "schema_version": "1.0", "kind": "semester_audit", "audit_id": "SHAPE_R1",
        "semester": "SHAPE_SEMESTER", "semester_name": "Shape test", "courses": [],
    }
    def course(index, status="CONFIRMED", name=None):
        return {"stable_id": f"SHAPE_C{index:02}", "name": name or f"Course {index}",
                "information_status": status}
    base["courses"] = [course(i, "UNKNOWN" if 12 <= i < 15 else "CONFIRMED") for i in range(59)]
    audit = tmp_path / "audit.yaml"
    dump(audit, base)
    assert import_audit(audit, config).status == "IMPORTED"
    revised = copy.deepcopy(base)
    revised["audit_id"] = "SHAPE_R2"
    for i in range(12):
        revised["courses"][i]["name"] += " corrected"
    for i in range(12, 15):
        revised["courses"][i]["information_status"] = "CONFIRMED"
        revised["courses"][i]["name"] += " known"
    revised["courses"].extend(course(i) for i in range(59, 72))
    dump(audit, revised)
    plan = import_audit(audit, config, dry_run=True)
    assert {kind: sum(a.action == kind for a in plan.actions)
            for kind in ("CREATE", "UPDATE", "NO_CHANGE", "CONFLICT")} == {
                "CREATE": 13, "UPDATE": 3, "NO_CHANGE": 45, "CONFLICT": 12}
    loaded = load_audit(audit)
    Session = create_session_factory(get_engine(config.database_path))
    decisions = []
    with Session() as session:
        for entity, stable_id, payload, status, _, _ in _facts(loaded.audit):
            if not any(a.action == "CONFLICT" and a.stable_id == stable_id for a in plan.actions):
                continue
            old = session.get(AuditFactRecord, stable_id)
            incoming = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            decisions.append({
                "entity_type": entity, "stable_id": stable_id,
                "expected_current_fact_sha256": fact_sha256(entity, stable_id, old.payload_json, old.information_status),
                "proposed_fact_sha256": fact_sha256(entity, stable_id, incoming, status),
                "expected_current_revision_id": old.audit_revision_id,
                "action": "ACCEPT_REPLACEMENT", "reason": "Reviewed test correction",
                "actor": "MANUAL_USER", "decided_at": "2026-10-17T12:00:00+02:00",
            })
    manifest = {"schema_version": "1.0", "kind": "semester_audit_resolution",
                "semester": loaded.audit.semester, "audit_id": loaded.audit.audit_id,
                "candidate_canonical_sha256": loaded.canonical_content_sha256, "resolutions": decisions}
    resolution = tmp_path / "resolution.yaml"
    dump(resolution, manifest)
    reversed_manifest = copy.deepcopy(manifest)
    reversed_manifest["resolutions"].reverse()
    reversed_path = tmp_path / "reversed.yaml"
    dump(reversed_path, reversed_manifest)
    assert load_resolution(reversed_path).canonical_content_sha256 == load_resolution(resolution).canonical_content_sha256
    approved = import_audit(audit, config, dry_run=True, resolution_path=resolution)
    assert {kind: sum(a.action == kind for a in approved.actions)
            for kind in ("CREATE", "UPDATE", "NO_CHANGE", "RESOLVED_UPDATE", "CONFLICT")} == {
                "CREATE": 13, "UPDATE": 3, "NO_CHANGE": 45, "RESOLVED_UPDATE": 12, "CONFLICT": 0}


def test_upgrade_existing_0016_shape_is_non_destructive(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    engine = get_engine(config.database_path)
    with engine.begin() as connection:
        # Reconstruct the old synthetic schema, including absence of B3 tables.
        connection.execute(text("DROP TABLE scoring_mastery_applications"))
        connection.execute(text("DROP TABLE scoring_review_events"))
        connection.execute(text("DROP TABLE scoring_result_snapshots"))
        connection.execute(text("DROP TABLE audit_resolution_records"))
        connection.execute(text("UPDATE alembic_version SET version_num='0016_weekly_plan_block_execution'"))
        connection.execute(text("INSERT INTO semesters (id, stable_id, name, state, created_at, updated_at) "
                                "VALUES ('survivor', 'SURVIVOR', 'Untouched', 'PLANNING', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] /
                                               "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert connection.execute(text("SELECT name FROM semesters WHERE id='survivor'")).scalar_one() == "Untouched"
        assert connection.execute(text("SELECT count(*) FROM sqlite_master WHERE type='table'")).scalar_one() == 56


def test_failed_persistence_cleans_both_archives_and_rolls_back(scenario, monkeypatch):
    from uniflow.application import audit_import
    config, audit, resolution, _, _ = scenario
    original = audit_import._persist
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure")
    monkeypatch.setattr(audit_import, "_persist", broken)
    with pytest.raises(RuntimeError, match="injected failure"):
        import_audit(audit, config, resolution_path=resolution)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    assert not list(config.audit_accepted_root.rglob("*resolution*"))
    assert len(list(config.audit_accepted_root.rglob("*.yaml"))) == 1


def test_failed_resolution_copy_removes_new_audit_archive(scenario, monkeypatch):
    from uniflow.application import audit_import
    config, audit, resolution, _, _ = scenario
    original = audit_import._write_verified_archive
    def broken(destination, raw, expected_sha256):
        if ".resolution." in destination.name:
            raise OSError("injected resolution copy failure")
        return original(destination, raw, expected_sha256)
    monkeypatch.setattr(audit_import, "_write_verified_archive", broken)
    with pytest.raises(OSError, match="injected resolution copy failure"):
        import_audit(audit, config, resolution_path=resolution)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    assert not list(config.audit_accepted_root.rglob("*resolution*"))
    assert len(list(config.audit_accepted_root.rglob("*.yaml"))) == 1


@pytest.mark.parametrize("changed_source", ["audit", "resolution"])
def test_source_changes_after_load_archive_captured_bytes(scenario, monkeypatch, changed_source):
    from uniflow.application import audit_import
    config, audit, resolution, _, _ = scenario
    audit_loaded = load_audit(audit)
    resolution_loaded = load_resolution(resolution)
    original_plan = audit_import._plan
    def change_after_load(session, loaded, supplied=None):
        source = audit if changed_source == "audit" else resolution
        source.write_bytes(b"changed after validation")
        return original_plan(session, loaded, supplied)
    monkeypatch.setattr(audit_import, "_plan", change_after_load)
    result = import_audit(audit, config, resolution_path=resolution)
    assert result.status == "IMPORTED"
    assert result.stored_file_path.read_bytes() == audit_loaded.raw
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        revision = session.query(SemesterAudit).filter_by(revision_number=2).one()
        decision = session.query(AuditResolutionRecord).one()
        assert revision.original_file_sha256 == hashlib.sha256(result.stored_file_path.read_bytes()).hexdigest()
        archived_resolution = Path(decision.resolution_stored_file_path)
        assert archived_resolution.read_bytes() == resolution_loaded.raw
        assert decision.resolution_raw_sha256 == hashlib.sha256(archived_resolution.read_bytes()).hexdigest()


@pytest.mark.parametrize("corrupt_archive", ["audit", "resolution"])
def test_archive_hash_mismatch_rolls_back_and_cleans_both_files(scenario, monkeypatch, corrupt_archive):
    config, audit, resolution, _, first = scenario
    original_write = Path.write_bytes
    def corrupt(self, data):
        if self.parent == first.stored_file_path.parent and self.name.startswith("rev2_"):
            is_resolution = ".resolution." in self.name
            if is_resolution == (corrupt_archive == "resolution"):
                data = b"corrupted accepted bytes"
        return original_write(self, data)
    monkeypatch.setattr(Path, "write_bytes", corrupt)
    with pytest.raises(ValueError, match="accepted archive SHA-256 mismatch"):
        import_audit(audit, config, resolution_path=resolution)
    Session = create_session_factory(get_engine(config.database_path))
    with Session() as session:
        assert session.query(SemesterAudit).count() == 1
        assert session.query(AuditResolutionRecord).count() == 0
    assert first.stored_file_path.exists()
    assert not list(first.stored_file_path.parent.glob("rev2_*"))
