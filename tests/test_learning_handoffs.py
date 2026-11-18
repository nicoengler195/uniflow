"""Synthetic, offline 3E1 contract and persistence tests."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from uniflow.application.initializer import initialize_project
from uniflow.application.material_intake import import_material
from uniflow.application.document_parsing import parse_source_artifact
from uniflow.application.academic_classification import create_manual_artifact_proposal, accept_classification_proposal
from uniflow.application.skill_extraction_foundation import (create_semantic_run,
    create_semantic_evidence, create_skill_proposal, accept_skill_proposal)
from uniflow.application.learning_handoffs import (
    create_learning_session, export_handoff, generate_handoff, handoff_info,
    learning_session_info,
)
from uniflow.domain.enums import InformationStatus, LearningSessionState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, Deadline, DerivedArtifact, Evidence, LearningHandoff,
    LearningSession, LearningSessionTargetSkill, MasteryState, Semester, Skill,
    SkillDependency,
    MaterialArtifact, ParsedUnit,
    RepositorySource, RepositorySnapshot, RepositoryFile, RepositoryBlob,
    RepositoryAcademicLink,
)
from uniflow.schemas.learning_handoff_v2 import LearningHandoffV2


def base(tmp_path):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    factory = create_session_factory(get_engine(cfg.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id="S", name="Synthetic", state=SemesterState.ACTIVE,
                            timezone="Europe/Berlin")
        course = Course(stable_id="MATH_SYNTH", name="Math", semester=semester,
                        information_status=InformationStatus.CONFIRMED)
        other = Course(stable_id="OTHER", name="Other", semester=semester)
        db.add_all([course, other])
        db.flush()
        for sid in ("DERIVATIVE", "CHAIN_RULE", "COMPOSITE"):
            db.add(Skill(id=sid, name=sid, kind="CONCEPT", status="ACTIVE"))
        db.flush()
        db.add_all([CourseSkill(course_id=course.id, skill_id="CHAIN_RULE"),
                    CourseSkill(course_id=course.id, skill_id="COMPOSITE"),
                    SkillDependency(prerequisite_skill_id="DERIVATIVE", dependent_skill_id="CHAIN_RULE"),
                    SkillDependency(prerequisite_skill_id="CHAIN_RULE", dependent_skill_id="COMPOSITE"),
                    MasteryState(skill_id="DERIVATIVE", value=0.4, calculation_method="synthetic"),
                    Deadline(course_id=course.id, title="Exercise", due_at=datetime(2026, 11, 1),
                             is_date_only=True, information_status=InformationStatus.CONFIRMED)])
        course_id = course.id
    return cfg, factory, course_id


def test_explicit_handoff_revision_and_unknown_mastery(tmp_path):
    cfg, factory, course_id = base(tmp_path)
    sid = create_learning_session(cfg, course_id, "Practice composition", ["COMPOSITE"])
    assert learning_session_info(cfg, sid)["state"] == "PLANNED"
    first = generate_handoff(cfg, sid)
    assert first.status == "GENERATED" and first.revision == 1
    again = generate_handoff(cfg, sid)
    assert again.status == "ALREADY_GENERATED" and again.handoff_id == first.handoff_id
    path = export_handoff(cfg, sid)
    data = LearningHandoffV2.model_validate_json(path.read_bytes())
    assert [s.skill_id for s in data.prerequisites] == ["DERIVATIVE", "CHAIN_RULE"]
    assert data.mastery_snapshot[2].status == "UNKNOWN"
    assert data.return_contract.expected_filename == f"{sid}--{first.handoff_id}.json"
    assert data.academic_context.deadlines[0].due_date == date(2026, 11, 1)
    assert data.academic_context.deadlines[0].due_at is None
    assert not data.source_data
    assert all(s.source_status == "NO_REVIEWED_SOURCE" for s in data.target_skills + data.prerequisites)
    assert handoff_info(cfg, sid)["artifact_sha256"] == first.artifact_sha256
    with factory.begin() as db:
        db.add(MasteryState(skill_id="CHAIN_RULE", value=0.2, calculation_method="synthetic"))
    second = generate_handoff(cfg, sid)
    assert second.status == "GENERATED" and second.revision == 2
    with factory() as db:
        assert db.get(LearningSession, sid).current_handoff_id == second.handoff_id
        assert len(list(db.scalars(select(LearningHandoff)))) == 2
        assert db.scalar(select(Evidence)) is None
        assert len(list(db.scalars(select(MasteryState)))) == 2
    with factory.begin() as db:
        db.get(LearningSession, sid).state = LearningSessionState.IN_PROGRESS
    with pytest.raises(ValueError, match="HANDOFF_FROZEN"):
        generate_handoff(cfg, sid)


def test_exact_course_and_active_target_authority(tmp_path):
    cfg, factory, course_id = base(tmp_path)
    with pytest.raises(ValueError, match="TARGET_COURSESKILL_REQUIRED"):
        create_learning_session(cfg, course_id, "Study", ["DERIVATIVE"])
    with pytest.raises(ValueError, match="DUPLICATE_TARGET_SKILL"):
        create_learning_session(cfg, course_id, "Study", ["COMPOSITE", "COMPOSITE"])
    with factory.begin() as db:
        db.get(Skill, "COMPOSITE").status = "MERGED"
        db.get(Skill, "COMPOSITE").canonical_skill_id = "CHAIN_RULE"
    with pytest.raises(ValueError, match="INVALID_TARGET_SKILL"):
        create_learning_session(cfg, course_id, "Study", ["COMPOSITE"])


def test_cycle_rejected_defensively(tmp_path):
    cfg, factory, course_id = base(tmp_path)
    sid = create_learning_session(cfg, course_id, "Study", ["COMPOSITE"])
    with factory.begin() as db:
        db.add(SkillDependency(prerequisite_skill_id="COMPOSITE", dependent_skill_id="DERIVATIVE"))
    with pytest.raises(ValueError, match="SKILL_GRAPH_INVALID"):
        generate_handoff(cfg, sid)


def test_reviewed_document_source_and_pending_excluded(tmp_path):
    cfg, factory, course_id = base(tmp_path)
    source = tmp_path / "lecture.txt"
    source.write_text("The chain rule combines derivatives.\n", encoding="utf-8")
    artifact = import_material(source, cfg)
    parse = parse_source_artifact(artifact.sha256, cfg)
    proposal = create_manual_artifact_proposal(cfg, parse.source_artifact_id,
        course="MATH_SYNTH", new_material_stable_id="LECTURE", title="Lecture", material_type="NOTES")
    accept_classification_proposal(cfg, proposal)
    with factory() as db:
        link = db.scalar(select(MaterialArtifact))
        unit = db.scalar(select(ParsedUnit))
        link_id, unit_id = link.id, unit.id
    run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=link_id,
                              parse_run_id=parse.parse_run_id, role="INSTRUCTION")
    span = create_semantic_evidence(cfg, run, unit_id, 4, 14)
    accepted = create_skill_proposal(cfg, run, "EXISTING_SKILL_EVIDENCE",
        evidence_ids=[span], target_skill_id="CHAIN_RULE")
    accept_skill_proposal(cfg, accepted)
    # The same source for COMPOSITE remains only a pending interpretation.
    create_skill_proposal(cfg, run, "EXISTING_SKILL_EVIDENCE",
        evidence_ids=[span], target_skill_id="COMPOSITE")
    sid = create_learning_session(cfg, course_id, "Study", ["COMPOSITE"])
    result = generate_handoff(cfg, sid)
    data = LearningHandoffV2.model_validate_json(export_handoff(cfg, sid).read_bytes())
    assert result.status == "GENERATED"
    assert len(data.source_data) == 1
    entry = data.source_data[0]
    assert entry.skill_id == "CHAIN_RULE" and entry.excerpt == "chain rule"
    assert entry.current_material_artifact
    assert data.target_skills[0].source_status == "NO_REVIEWED_SOURCE"
    assert "\\" not in json.dumps(entry.model_dump(mode="json"))


def test_reviewed_repository_exact_blob_excerpt(tmp_path):
    cfg, factory, course_id = base(tmp_path)
    content = "// café chain rule\n".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    blob_path = cfg.repository_root / "blobs" / "sha256" / digest[:2] / digest / "content"
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(content)
    with factory.begin() as db:
        repo = RepositorySource(stable_id="SYNTH_REPO", local_path=str(tmp_path / "private"),
                                course_id=course_id)
        db.add(repo)
        db.flush()
        link = RepositoryAcademicLink(repository_source_id=repo.id, course_id=course_id,
                                      is_active=True)
        snapshot = RepositorySnapshot(repository_source_id=repo.id,
            snapshot_identity="a" * 64, policy_version="1", include_untracked=False,
            is_detached=False, is_dirty=False, tracked_file_count=1,
            untracked_file_count=0, deleted_file_count=0, blob_count=1,
            build_system_hint="UNKNOWN", status="SUCCESS")
        blob = RepositoryBlob(sha256=digest, size_bytes=len(content), stored_path=str(blob_path))
        db.add_all([link, snapshot, blob])
        db.flush()
        file = RepositoryFile(snapshot_id=snapshot.id, path="src/math.py",
            entry_kind="REGULAR", tracked_state="TRACKED", head_blob_id=blob.id,
            language_hint="PYTHON")
        db.add(file)
        db.flush()
        link_id, snapshot_id, file_id = link.id, snapshot.id, file.id
    run = create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=link_id,
        repository_snapshot_id=snapshot_id, repository_state="HEAD", role="INSTRUCTION")
    start = content.index("café".encode("utf-8"))
    end = start + len("café".encode("utf-8"))
    span = create_semantic_evidence(cfg, run, file_id, start, end)
    proposal = create_skill_proposal(cfg, run, "EXISTING_SKILL_EVIDENCE",
        evidence_ids=[span], target_skill_id="CHAIN_RULE")
    accept_skill_proposal(cfg, proposal)
    document = tmp_path / "lecture.txt"
    document.write_text("Composite differentiation uses the chain rule.\n", encoding="utf-8")
    artifact = import_material(document, cfg)
    parse = parse_source_artifact(artifact.sha256, cfg)
    classification = create_manual_artifact_proposal(cfg, parse.source_artifact_id,
        course="MATH_SYNTH", new_material_stable_id="COMPOSITE_LECTURE",
        title="Composite lecture", material_type="NOTES")
    accept_classification_proposal(cfg, classification)
    with factory() as db:
        material_artifact_id = db.scalar(select(MaterialArtifact).where(
            MaterialArtifact.artifact_id == parse.source_artifact_id)).id
        parsed_unit_id = db.scalar(select(ParsedUnit).where(
            ParsedUnit.parse_run_id == parse.parse_run_id)).id
    doc_run = create_semantic_run(cfg, "DOCUMENT", material_artifact_id=material_artifact_id,
        parse_run_id=parse.parse_run_id, role="INSTRUCTION")
    doc_span = create_semantic_evidence(cfg, doc_run, parsed_unit_id, 0, 25)
    doc_proposal = create_skill_proposal(cfg, doc_run, "EXISTING_SKILL_EVIDENCE",
        evidence_ids=[doc_span], target_skill_id="COMPOSITE")
    accept_skill_proposal(cfg, doc_proposal)
    sid = create_learning_session(cfg, course_id, "Study", ["COMPOSITE"])
    generate_handoff(cfg, sid)
    data = LearningHandoffV2.model_validate_json(export_handoff(cfg, sid).read_bytes())
    assert {item.source_type for item in data.source_data} == {"DOCUMENT", "REPOSITORY"}
    assert [item.skill_id for item in data.prerequisites] == ["DERIVATIVE", "CHAIN_RULE"]
    assert data.mastery_snapshot[2].status == "UNKNOWN"
    assert data.academic_context.deadlines[0].is_date_only
    entry = next(item for item in data.source_data if item.source_type == "REPOSITORY")
    assert entry.source_type == "REPOSITORY" and entry.excerpt == "café"
    assert entry.byte_start == start and entry.byte_end == end
    assert entry.relative_path == "src/math.py"
    assert str(tmp_path) not in json.dumps(data.model_dump(mode="json"))
    with factory.begin() as db:
        db.add(MasteryState(skill_id="CHAIN_RULE", value=0.3, calculation_method="synthetic"))
    updated = generate_handoff(cfg, sid)
    assert updated.revision == 2
    with factory.begin() as db:
        db.get(LearningSession, sid).state = LearningSessionState.IN_PROGRESS
    with pytest.raises(ValueError, match="HANDOFF_FROZEN"):
        generate_handoff(cfg, sid)


def test_populated_0010_upgrade_preserves_legacy_session(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    ini = Config(str((__import__("pathlib").Path(__file__).resolve().parents[1] / "alembic.ini")))
    ini.set_main_option("script_location", str(__import__("pathlib").Path(__file__).resolve().parents[1]
        / "src" / "uniflow" / "infrastructure" / "database" / "migrations"))
    ini.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(ini, "0010_external_semantic_adapter")
    factory = create_session_factory(get_engine(db_path))
    with factory.begin() as db:
        semester = Semester(stable_id="LEGACY", name="Legacy")
        course = Course(stable_id="LEGACY_C", name="Legacy Course", semester=semester)
        row = LearningSession(id="legacy-session", course=course, goal="Old goal",
                              handoff_path="handoffs/old.json")
        db.add(row)
    command.upgrade(ini, "0011_learning_handoff_generation")
    with factory() as db:
        row = db.get(LearningSession, "legacy-session")
        assert row.goal == "Old goal" and row.handoff_path == "handoffs/old.json"
        assert row.current_handoff_id is None
        assert not list(db.scalars(select(LearningSessionTargetSkill)))
        assert not list(db.scalars(select(LearningHandoff)))
        assert db.execute(text("PRAGMA foreign_key_check")).fetchall() == []
