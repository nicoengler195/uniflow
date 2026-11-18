from datetime import date
import json
import subprocess

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner
from alembic import command
from alembic.config import Config
from pathlib import Path

from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material
from uniflow.application.repository_snapshots import register_repository
from uniflow.application.academic_classification import (
    ClassificationConflict, accept_classification_proposal, create_manual_artifact_proposal,
    create_manual_expectation_proposal, create_manual_repository_proposal,
    get_classification_proposal, get_link_history, get_material_expectation_links,
    list_unclassified_artifacts, list_unlinked_repositories, reject_classification_proposal,
    correct_academic_link, rule_fingerprint,
    supersede_classification_proposal,
)
from uniflow.domain.enums import InformationStatus
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Assessment, Course, Material, MaterialArtifact, MaterialReleaseRule,
    RepositorySource, Semester, ClassificationProposal,
)
from uniflow.cli.main import app


def setup(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    Session = create_session_factory(get_engine(config.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Semester", starts_on=date(2026, 10, 12), ends_on=date(2026, 10, 31))
        course = Course(stable_id="C", name="Course", semester=semester)
        other = Course(stable_id="D", name="Other", semester=semester)
        session.add_all([semester, course, other])
        session.flush()
        session.add_all([
            Assessment(stable_id="A", name="Assignment", course_id=course.id),
            Assessment(stable_id="B", name="Other assignment", course_id=course.id),
            MaterialReleaseRule(stable_id="R", course_id=course.id,
                payload_json=json.dumps({"cadence": "WEEKLY", "weekday": "FRIDAY", "material_kind": "SHEET"}),
                information_status=InformationStatus.CONFIRMED),
            RepositorySource(stable_id="REPO", local_path=str(tmp_path / "repo")),
        ])
    file = tmp_path / "arbitrary.txt"
    file.write_text("bytes", encoding="utf-8")
    import_material(file, config)
    return config, Session


def test_artifact_proposal_acceptance_and_version_safety(tmp_path):
    config, Session = setup(tmp_path)
    artifact = list_unclassified_artifacts(config)[0]
    p = create_manual_artifact_proposal(config, artifact.id, course="C", new_material_stable_id="M",
        title="Sheet 1", material_type="EXERCISE_SHEET")
    assert create_manual_artifact_proposal(config, artifact.id, course="C", new_material_stable_id="M",
        title="Sheet 1", material_type="EXERCISE_SHEET") == p
    assert get_classification_proposal(config, p).confidence is None
    with Session() as session:
        assert session.scalar(select(Material).where(Material.stable_id == "M")) is None
    assert accept_classification_proposal(config, p, dry_run=True) == "CREATE_MATERIAL_AND_LINK_ARTIFACT"
    assert accept_classification_proposal(config, p) == "CREATE_MATERIAL_AND_LINK_ARTIFACT"
    assert len(list_unclassified_artifacts(config)) == 0
    with Session() as session:
        material = session.scalar(select(Material).where(Material.stable_id == "M"))
        assert material.material_type == "EXERCISE_SHEET"
        assert session.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material.id)).classification_proposal_id == p
    file2 = tmp_path / "different.txt"
    file2.write_text("different", encoding="utf-8")
    import_material(file2, config)
    a2 = list_unclassified_artifacts(config)[0]
    p2 = create_manual_artifact_proposal(config, a2.id, material="M")
    with pytest.raises(ClassificationConflict, match="CONFLICT_VERSION_REQUIRED"):
        accept_classification_proposal(config, p2)
    assert get_classification_proposal(config, p2).status == "PENDING"
    old = get_link_history(config, "artifact", artifact.id)[0]
    correction = correct_academic_link(config, "artifact", old.id, course="C",
        new_material_stable_id="M2", title="Correct sheet", material_type="SHEET")
    assert accept_classification_proposal(config, correction, reason="wrong Material") == "CREATE_MATERIAL_AND_LINK_ARTIFACT"
    history = get_link_history(config, "artifact", artifact.id)
    assert len(history) == 2 and not history[0].is_current and history[1].is_current
    assert history[0].replacement_link_id == history[1].id


def test_expectation_fingerprint_and_repository_correction(tmp_path):
    config, Session = setup(tmp_path)
    artifact = list_unclassified_artifacts(config)[0]
    p = create_manual_artifact_proposal(config, artifact.id, course="C", new_material_stable_id="M",
        title="Sheet", material_type="SHEET")
    accept_classification_proposal(config, p)
    with pytest.raises(ClassificationConflict, match="INVALID_EXPECTATION_OCCURRENCE"):
        create_manual_expectation_proposal(config, "M", "R@2026-10-17")
    e = create_manual_expectation_proposal(config, "M", "R@2026-10-16")
    assert accept_classification_proposal(config, e) == "LINK_EXPECTATION"
    links = get_material_expectation_links(config, "M")
    assert links[0][1] == "CURRENT"
    with Session.begin() as session:
        rule = session.scalar(select(MaterialReleaseRule).where(MaterialReleaseRule.stable_id == "R"))
        original = rule_fingerprint(rule)
        rule.payload_json = json.dumps({"cadence": "WEEKLY", "weekday": "FRIDAY", "material_kind": "SLIDES"})
    assert get_material_expectation_links(config, "M")[0][1] == "STALE_RULE"
    assert get_material_expectation_links(config, "M")[0][0].rule_fingerprint == original
    assert len(list_unlinked_repositories(config)) == 1
    r = create_manual_repository_proposal(config, "REPO", course="C", assessment="A")
    assert accept_classification_proposal(config, r) == "LINK_REPOSITORY"
    assert len(list_unlinked_repositories(config)) == 0
    old = get_link_history(config, "repository", "REPO")[0]
    correction = correct_academic_link(config, "repository", old.id, course="C", assessment="B")
    assert accept_classification_proposal(config, correction, reason="correct assignment") == "REPLACE_REPOSITORY_LINK"
    history = get_link_history(config, "repository", "REPO")
    assert len(history) == 2 and sum(x.is_active for x in history) == 1
    assert history[0].replacement_link_id == history[1].id
    with Session() as session:
        assert session.scalar(select(RepositorySource)).assessment_id == session.scalar(select(Assessment).where(Assessment.stable_id == "B")).id
    rejected = create_manual_repository_proposal(config, "REPO", course="D")
    assert reject_classification_proposal(config, rejected, reason="wrong course") == "REJECTED"
    assert len(get_link_history(config, "repository", "REPO")) == 2


def test_cli_manual_workflow(tmp_path):
    config, Session = setup(tmp_path)
    runner = CliRunner()
    root = str(tmp_path)
    artifact = list_unclassified_artifacts(config)[0]
    result = runner.invoke(app, ["classify-artifact", artifact.id, "--course", "C",
        "--new-material-stable-id", "M", "--title", "Sheet", "--material-type", "SHEET",
        "--project-root", root])
    assert result.exit_code == 0, result.output
    p = result.output.split("proposal=")[1].strip()
    assert "PENDING" in runner.invoke(app, ["classify-list", "--project-root", root]).output
    assert "MANUAL" in runner.invoke(app, ["classify-info", p, "--project-root", root]).output
    assert "DRY_RUN" in runner.invoke(app, ["classify-accept", p, "--dry-run", "--project-root", root]).output
    with Session() as session:
        assert session.scalar(select(Material).where(Material.stable_id == "M")) is None
    assert runner.invoke(app, ["classify-accept", p, "--project-root", root]).exit_code == 0
    assert artifact.id not in runner.invoke(app, ["artifact-unclassified", "--project-root", root]).output
    assert "proposal=" in runner.invoke(app, ["link-history", "artifact", artifact.id, "--project-root", root]).output
    result = runner.invoke(app, ["classify-expectation", "M", "--expectation", "R@2026-10-16", "--project-root", root])
    assert result.exit_code == 0, result.output
    e = result.output.split("proposal=")[1].strip()
    assert runner.invoke(app, ["classify-accept", e, "--project-root", root]).exit_code == 0
    assert "CURRENT" in runner.invoke(app, ["expectation-links", "M", "--project-root", root]).output
    result = runner.invoke(app, ["classify-repository", "REPO", "--course", "C", "--assessment", "A", "--project-root", root])
    assert result.exit_code == 0, result.output
    r = result.output.split("proposal=")[1].strip()
    assert runner.invoke(app, ["classify-accept", r, "--project-root", root]).exit_code == 0
    assert "REPO" not in runner.invoke(app, ["repository-unlinked", "--project-root", root]).output


def test_migration_preserves_legacy_rows(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "legacy.sqlite3"
    alembic = Config(str(root / "alembic.ini"))
    alembic.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    alembic.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(alembic, "0006_repository_snapshots")
    engine = get_engine(db)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,created_at,updated_at) VALUES ('s','S','Semester','ACTIVE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO courses (id,semester_id,stable_id,name,kind,participation,unknowns_json,information_status,created_at,updated_at) VALUES ('c','s','C','Course','MODULE','UNKNOWN','[]','CONFIRMED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO materials (id,course_id,stable_id,title,lifecycle_state,created_at,updated_at) VALUES ('m','c','M','Material','DOWNLOADED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO repository_sources (id,stable_id,local_path,course_id,created_at,last_seen_at) VALUES ('r','REPO','local','c',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(alembic, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0019_scoring_mastery"
        assert conn.execute(text("SELECT title FROM materials WHERE id='m'")).scalar_one() == "Material"
        assert conn.execute(text("SELECT count(*) FROM repository_academic_links WHERE repository_source_id='r' AND is_active=1")).scalar_one() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []


def test_proposal_constraints_supersession_and_existing_material(tmp_path):
    config, Session = setup(tmp_path)
    artifact = list_unclassified_artifacts(config)[0]
    new = create_manual_artifact_proposal(config, artifact.id, course="C", new_material_stable_id="M",
        title="Sheet", material_type="SHEET")
    other = create_manual_artifact_proposal(config, artifact.id, course="C", new_material_stable_id="M2",
        title="Other", material_type="SHEET")
    assert supersede_classification_proposal(config, new, other, reason="better target") == "SUPERSEDED"
    assert get_classification_proposal(config, new).superseded_by_proposal_id == other
    with pytest.raises(ClassificationConflict, match="PROPOSAL_SUPERSEDED"):
        accept_classification_proposal(config, new)
    accept_classification_proposal(config, other)
    existing = create_manual_artifact_proposal(config, artifact.id, material="M2")
    assert accept_classification_proposal(config, existing, dry_run=True) == "ALREADY_LINKED"
    assert accept_classification_proposal(config, existing) == "ALREADY_LINKED"
    with pytest.raises(IntegrityError):
        with Session.begin() as session:
            session.add(ClassificationProposal(proposal_kind="REPOSITORY_ACADEMIC_LINK", status="PENDING",
                identity_sha256="a" * 64, classifier_name="MANUAL", classifier_version="1",
                subject_source_artifact_id=artifact.id, subject_repository_source_id=session.scalar(select(RepositorySource)).id,
                confidence=1.1))
            session.flush()


def test_temporary_end_to_end_demonstration(tmp_path):
    config, Session = setup(tmp_path)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    assert register_repository(repo_path, config, stable_id="REPO")[0] == "ALREADY_REGISTERED"
    artifact = list_unclassified_artifacts(config)[0]
    print("unclassified", artifact.id)
    proposal = create_manual_artifact_proposal(config, artifact.id, course="C",
        new_material_stable_id="DEMO_M", title="Temporary sheet", material_type="SHEET")
    print("proposal", get_classification_proposal(config, proposal).status)
    with Session() as session:
        assert session.scalar(select(Material).where(Material.stable_id == "DEMO_M")) is None
    print("dry-run", accept_classification_proposal(config, proposal, dry_run=True))
    print("accept", accept_classification_proposal(config, proposal))
    assert artifact.id not in [a.id for a in list_unclassified_artifacts(config)]
    e = create_manual_expectation_proposal(config, "DEMO_M", "R@2026-10-16")
    accept_classification_proposal(config, e)
    print("expectation", get_material_expectation_links(config, "DEMO_M")[0][1])
    with Session.begin() as session:
        rule = session.scalar(select(MaterialReleaseRule).where(MaterialReleaseRule.stable_id == "R"))
        rule.payload_json = json.dumps({"cadence": "WEEKLY", "weekday": "FRIDAY", "material_kind": "EXTRA"})
    print("changed rule", get_material_expectation_links(config, "DEMO_M")[0][1])
    assert len(list_unlinked_repositories(config)) == 1
    r = create_manual_repository_proposal(config, "REPO", course="C", assessment="A")
    accept_classification_proposal(config, r)
    old = get_link_history(config, "repository", "REPO")[0]
    correction = correct_academic_link(config, "repository", old.id, course="C", assessment="B")
    accept_classification_proposal(config, correction, reason="demo correction")
    print("repository history", [(x.id, x.is_active) for x in get_link_history(config, "repository", "REPO")])
    rejected = create_manual_repository_proposal(config, "REPO", course="D")
    reject_classification_proposal(config, rejected, reason="demo rejection")
    print("rejected", get_classification_proposal(config, rejected).status)
