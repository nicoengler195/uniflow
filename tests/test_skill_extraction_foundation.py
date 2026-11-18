from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from typer.testing import CliRunner

from uniflow.application.academic_classification import (
    accept_classification_proposal, create_manual_artifact_proposal,
    create_manual_repository_proposal,
)
from uniflow.application.document_parsing import parse_source_artifact
from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material
from uniflow.application.repository_snapshots import register_repository, snapshot_repository
from uniflow.application.skill_extraction_foundation import (
    accept_skill_proposal, build_semantic_input, create_semantic_evidence,
    create_semantic_run, create_skill_proposal, deactivate_skill_relation,
    get_skill_proposal, list_skill_aliases, list_skill_dependencies,
    list_skill_evidence, reject_skill_proposal,
)
from uniflow.cli.main import app
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine, initialize_database
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Evidence, MasteryState, MaterialArtifact, ParsedUnit,
    RepositoryAcademicLink, RepositoryFile, RepositorySnapshot, Semester, Skill,
    SkillDependency, SkillProposal, SemanticEvidenceSpan,
)


def setup_document(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    Session = create_session_factory(get_engine(cfg.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Temporary semester")
        course = Course(stable_id="C", name="Calculus", semester=semester)
        other = Course(stable_id="C2", name="Next course", semester=semester)
        session.add_all([semester, course, other])
    source = tmp_path / "notes.txt"
    source.write_text("The chain rule combines derivatives.\nSecond passage.", encoding="utf-8")
    artifact = import_material(source, cfg)
    parse = parse_source_artifact(artifact.sha256, cfg)
    proposal = create_manual_artifact_proposal(cfg, parse.source_artifact_id, course="C",
        new_material_stable_id="M", title="Notes", material_type="LECTURE")
    accept_classification_proposal(cfg, proposal)
    with Session() as session:
        link = session.scalar(select(MaterialArtifact))
        unit = session.scalar(select(ParsedUnit))
        course_id = session.scalar(select(Course).where(Course.stable_id == "C")).id
        other_id = session.scalar(select(Course).where(Course.stable_id == "C2")).id
        ids = link.id, unit.id, course_id, other_id
    return cfg, Session, parse.parse_run_id, ids


def test_manual_document_skill_workflow_and_history(tmp_path):
    cfg, Session, parse_id, (link_id, unit_id, course_id, other_id) = setup_document(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id, parse_run_id=parse_id,
        role="LECTURE_MATERIAL")
    package = build_semantic_input(cfg, run)
    assert package["academic_provenance"] == "ACCEPTED_PROPOSAL"
    assert package["units"][0]["text"].startswith("The chain rule")
    span = create_semantic_evidence(cfg, run, unit_id, 4, 14)
    with Session() as session:
        row = session.get(SemanticEvidenceSpan, span)
        assert row.span_sha256 == hashlib.sha256(b"chain rule").hexdigest()
        assert not hasattr(row, "text")
    for start, end in [(-1, 2), (1, 1000), (5, 4)]:
        with pytest.raises(ValueError):
            create_semantic_evidence(cfg, run, unit_id, start, end)
    new = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Chain rule", skill_kind="CONCEPT")
    assert create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Chain rule", skill_kind="CONCEPT") == new
    with Session() as session:
        assert session.scalar(select(Skill)) is None
    assert accept_skill_proposal(cfg, new, dry_run=True).action == "CREATE_SKILL"
    skill_a = accept_skill_proposal(cfg, new).target_id
    assert accept_skill_proposal(cfg, new).target_id == skill_a
    existing = create_skill_proposal(cfg, run, "EXISTING_SKILL_EVIDENCE", evidence_ids=[span], target_skill_id=skill_a)
    accept_skill_proposal(cfg, existing)
    alias_run = create_semantic_run(cfg, "MANUAL")
    alias_p = create_skill_proposal(cfg, alias_run, "SKILL_ALIAS", target_skill_id=skill_a, alias="  CHAIN   RULE ")
    assert accept_skill_proposal(cfg, alias_p).action == "CREATE_ALIAS"
    alias_p2 = create_skill_proposal(cfg, alias_run, "SKILL_ALIAS", target_skill_id=skill_a, alias="chain rule")
    assert accept_skill_proposal(cfg, alias_p2).action == "REUSE_ALIAS"
    course_p = create_skill_proposal(cfg, alias_run, "SKILL_COURSE_ASSOCIATION", target_skill_id=skill_a, course_id=course_id)
    accept_skill_proposal(cfg, course_p)
    course_p2 = create_skill_proposal(cfg, alias_run, "SKILL_COURSE_ASSOCIATION", target_skill_id=skill_a, course_id=other_id)
    accept_skill_proposal(cfg, course_p2)
    assert len(list_skill_aliases(cfg, skill_a)) == 1
    second = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Differentiate a composition", skill_kind="ABILITY")
    skill_b = accept_skill_proposal(cfg, second).target_id
    dep = create_skill_proposal(cfg, run, "SKILL_DEPENDENCY", evidence_ids=[span], prerequisite_skill_id=skill_a, dependent_skill_id=skill_b)
    edge = accept_skill_proposal(cfg, dep).target_id
    back = create_skill_proposal(cfg, run, "SKILL_DEPENDENCY", evidence_ids=[span], prerequisite_skill_id=skill_b, dependent_skill_id=skill_a)
    with pytest.raises(ValueError, match="CYCLE_CONFLICT"):
        accept_skill_proposal(cfg, back)
    assert get_skill_proposal(cfg, back)[0].status == "PENDING"
    deactivate_skill_relation(cfg, "DEPENDENCY", edge, "corrected")
    assert not list_skill_dependencies(cfg)[0].is_active
    assert len(list_skill_evidence(cfg, skill_a)) == 2
    with Session() as session:
        assert session.query(Evidence).count() == session.query(MasteryState).count() == 0
        assert session.query(CourseSkill).count() == 2
    # A later Material artifact/parse and label change cannot retarget history.
    with Session.begin() as session:
        session.get(Skill, skill_a).name = "Renamed chain rule"
    assert list_skill_evidence(cfg, skill_a)[0].parsed_unit_id == unit_id
    rejected = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Unneeded", skill_kind="CONCEPT")
    reject_skill_proposal(cfg, rejected, "not useful")
    assert get_skill_proposal(cfg, rejected)[0].status == "REJECTED"
    again = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Unneeded", skill_kind="CONCEPT")
    assert again != rejected
    later = tmp_path / "later.txt"
    later.write_text("Revised lecture notes", encoding="utf-8")
    next_artifact = import_material(later, cfg, material_stable_id="M", new_version=True)
    parse_source_artifact(next_artifact.sha256, cfg)
    assert list_skill_evidence(cfg, skill_a)[0].parsed_unit_id == unit_id


def test_scope_recheck_and_existing_resolution(tmp_path):
    cfg, Session, parse_id, (link_id, unit_id, _, _) = setup_document(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id, parse_run_id=parse_id)
    span = create_semantic_evidence(cfg, run, unit_id, 0, 3)
    first = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Concept A", skill_kind="CONCEPT")
    target = accept_skill_proposal(cfg, first).target_id
    second = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Alternate wording", skill_kind="CONCEPT")
    assert accept_skill_proposal(cfg, second, existing_skill_id=target).action == "RESOLVE_TO_EXISTING_SKILL"
    with Session() as session:
        assert session.query(Skill).count() == 1
        assert session.query(Evidence).count() == session.query(MasteryState).count() == 0
    pending = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Concept B", skill_kind="CONCEPT")
    with Session.begin() as session:
        session.get(MaterialArtifact, link_id).is_current = False
    with pytest.raises(ValueError, match="ACADEMIC_SCOPE_CHANGED"):
        accept_skill_proposal(cfg, pending)
    assert get_skill_proposal(cfg, pending)[0].status == "PENDING"


def test_migrate_legacy_skill_dependency(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    root = Path(__file__).resolve().parents[1]
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(alembic, "0007_academic_classification")
    # The committed 0001 migration reads the live ORM. Recreate the actual
    # committed 0007 Skill shape in this temporary fixture before adding rows.
    # This represents an installed 0007 database made by the Phase 3D1 code.
    with get_engine(db).begin() as conn:
        conn.execute(text("DROP TABLE skill_dependencies"))
        conn.execute(text("DROP TABLE skills"))
        conn.execute(text("CREATE TABLE skills (id VARCHAR(120) NOT NULL PRIMARY KEY, name VARCHAR(240) NOT NULL, description TEXT, domain VARCHAR(120), last_validated_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"))
        conn.execute(text("CREATE TABLE skill_dependencies (prerequisite_skill_id VARCHAR(120) NOT NULL REFERENCES skills(id), dependent_skill_id VARCHAR(120) NOT NULL REFERENCES skills(id), source_id VARCHAR(36) REFERENCES source_references(id), created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (prerequisite_skill_id, dependent_skill_id))"))
    with get_engine(db).begin() as conn:
        for sid in ("A", "B"):
            conn.execute(text("INSERT INTO skills (id,name,created_at,updated_at) VALUES (:id,:id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {"id": sid})
        conn.execute(text("INSERT INTO source_references (id,source_type,information_status,created_at,updated_at) VALUES ('src','manual','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO skill_dependencies (prerequisite_skill_id,dependent_skill_id,source_id,created_at,updated_at) VALUES ('A','B','src',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO evidence (id,skill_id,evidence_type,observed_at,created_at,updated_at) VALUES ('e','A','MANUAL_OBSERVATION',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO mastery_states (id,skill_id,value,calculated_at,calculation_method,evidence_id,created_at,updated_at) VALUES ('m','A',0.5,CURRENT_TIMESTAMP,'legacy','e',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(alembic, "head")
    with get_engine(db).connect() as conn:
        assert conn.execute(text("SELECT kind,status,canonical_skill_id FROM skills WHERE id='A'")).one() == ("UNKNOWN", "ACTIVE", None)
        row = conn.execute(text("SELECT id,source_id,is_active,skill_proposal_id FROM skill_dependencies")).one()
        assert row[0] and row[1:] == ("src", 1, None)
        assert conn.execute(text("SELECT skill_id FROM evidence WHERE id='e'")).scalar_one() == "A"
        assert conn.execute(text("SELECT skill_id,evidence_id FROM mastery_states WHERE id='m'")).one() == ("A", "e")
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
    fresh = tmp_path / "fresh.sqlite3"
    initialize_database(fresh)
    names = ["skills", "skill_dependencies", "semantic_extraction_runs", "semantic_evidence_spans",
        "skill_proposals", "skill_proposal_evidence", "skill_aliases", "course_skills"]
    def structure(path):
        with get_engine(path).connect() as conn:
            inspector = inspect(conn)
            result = {}
            for name in names:
                result[name] = {
                    "columns": [(c["name"], str(c["type"]), c["nullable"], c["default"], c.get("primary_key")) for c in inspector.get_columns(name)],
                    "pk": inspector.get_pk_constraint(name)["constrained_columns"],
                    "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(name)),
                    "checks": sorted(c["sqltext"] for c in inspector.get_check_constraints(name)),
                    "indexes": sorted((i["name"], tuple(i["column_names"]), i["unique"], str(i.get("dialect_options", {}).get("sqlite_where"))) for i in inspector.get_indexes(name)),
                }
            return result
    assert structure(db) == structure(fresh)


def test_document_evidence_rechecked_at_acceptance(tmp_path):
    cfg, Session, parse_id, (link_id, unit_id, _, _) = setup_document(tmp_path)
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id, parse_run_id=parse_id)
    span = create_semantic_evidence(cfg, run, unit_id, 0, 3)
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Read", skill_kind="ABILITY")
    with Session.begin() as session:
        session.get(SemanticEvidenceSpan, span).span_sha256 = "0" * 64
    with pytest.raises(ValueError, match="EVIDENCE_HASH_MISMATCH"):
        accept_skill_proposal(cfg, proposal)
    assert get_skill_proposal(cfg, proposal)[0].status == "PENDING"


def test_repository_scope_rechecked_at_acceptance(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    Session = create_session_factory(get_engine(cfg.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Temp")
        session.add(Course(stable_id="C", name="Code", semester=semester))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "file.txt").write_text("assignment", encoding="utf-8")
    register_repository(repo, cfg, stable_id="R")
    p = create_manual_repository_proposal(cfg, "R", course="C")
    accept_classification_proposal(cfg, p)
    snapshot_repository("R", cfg)
    with Session() as session:
        link = session.scalar(select(RepositoryAcademicLink))
        snap = session.scalar(select(RepositorySnapshot))
        entry = session.scalar(select(RepositoryFile))
        ids = link.id, snap.id, entry.id
    run = create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0],
        repository_snapshot_id=ids[1], repository_state="WORKTREE")
    span = create_semantic_evidence(cfg, run, ids[2], 0, 10)
    proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Read task", skill_kind="ABILITY")
    with Session.begin() as session:
        session.get(RepositoryAcademicLink, ids[0]).is_active = False
    with pytest.raises(ValueError, match="ACADEMIC_SCOPE_CHANGED"):
        accept_skill_proposal(cfg, proposal)
    assert get_skill_proposal(cfg, proposal)[0].status == "PENDING"


def test_repository_states_binary_and_cli(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    Session = create_session_factory(get_engine(cfg.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Temp")
        session.add(Course(stable_id="C", name="Code", semester=semester))
    repo = tmp_path / "repository"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    file = repo / "data.bin"
    file.write_bytes(b"\x00HEAD")
    git("add", "data.bin")
    git("commit", "-qm", "initial")
    file.write_bytes(b"\x00INDEX")
    git("add", "data.bin")
    file.write_bytes(b"\x00WORKTREE")
    register_repository(repo, cfg, stable_id="R")
    p = create_manual_repository_proposal(cfg, "R", course="C")
    accept_classification_proposal(cfg, p)
    snapshot_repository("R", cfg)
    with Session() as session:
        link = session.scalar(select(RepositoryAcademicLink))
        snap = session.scalar(select(RepositorySnapshot))
        entry = session.scalar(select(RepositoryFile))
        ids = link.id, snap.id, entry.id
    for state, expected in [("HEAD", b"\x00HEAD"), ("INDEX", b"\x00INDEX"), ("WORKTREE", b"\x00WORKTREE")]:
        run = create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0],
            repository_snapshot_id=ids[1], repository_state=state, role="STUDENT_CODE")
        package = build_semantic_input(cfg, run)
        assert package["files"][0]["bytes"] == expected
        assert str(repo) not in str({k: v for k, v in package.items() if k != "files"})
        span = create_semantic_evidence(cfg, run, ids[2], 0, len(expected))
        with Session() as session:
            assert session.get(SemanticEvidenceSpan, span).span_sha256 == hashlib.sha256(expected).hexdigest()
        if state == "WORKTREE":
            proposal = create_skill_proposal(cfg, run, "NEW_SKILL", evidence_ids=[span], name="Read binary", skill_kind="ABILITY")
            skill = accept_skill_proposal(cfg, proposal).target_id
            assert len(list_skill_evidence(cfg, skill)) == 1
    runner = CliRunner()
    result = runner.invoke(app, ["skill-list", "--project-root", str(cfg.project_root)])
    assert result.exit_code == 0 and "Read binary" in result.output and str(repo) not in result.output
    file.write_bytes(b"\x00LATER")
    snapshot_repository("R", cfg)
    assert list_skill_evidence(cfg, skill)[0].repository_file_id == ids[2]
    with pytest.raises(ValueError, match="REPOSITORY_STATE_REQUIRED"):
        create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0], repository_snapshot_id=ids[1])
