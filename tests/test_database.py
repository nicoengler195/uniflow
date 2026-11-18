from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from uniflow.application.initializer import initialize_project
from uniflow.application.mastery import DeterministicMasteryService
from uniflow.domain.enums import (
    AssessmentType,
    EvidenceType,
    InformationStatus,
    LearningSessionState,
    MaterialState,
    SemesterState,
)
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine, initialize_database
from uniflow.infrastructure.database.models import (
    Assessment,
    Course,
    Evidence,
    LearningSession,
    Material,
    OfficialResult,
    Semester,
    Skill,
    SkillDependency,
    SourceReference,
)


def test_init_is_idempotent_and_creates_schema(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path)

    initialize_project(config)
    initialize_project(config)

    assert config.database_path.exists()
    assert config.handoff_inbox_root.exists()


def test_alembic_migration_upgrades_temp_database(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "src" / "uniflow" / "infrastructure" / "database" / "migrations"),
    )
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(alembic_cfg, "head")

    engine = get_engine(database_path)
    with engine.connect() as connection:
        tables = set(
            connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars().all()
        )

    assert "semesters" in tables
    assert "skills" in tables
    assert "alembic_version" in tables


def test_enums_include_required_lifecycles() -> None:
    assert SemesterState.RESULTS_PENDING == "RESULTS_PENDING"
    assert LearningSessionState.MASTERY_UPDATED == "MASTERY_UPDATED"
    assert MaterialState.HANDOFF_READY == "HANDOFF_READY"
    assert InformationStatus.UNKNOWN == "UNKNOWN"
    assert InformationStatus.OBSERVED == "OBSERVED"
    assert InformationStatus.CONFIRMED == "CONFIRMED"


def test_core_entities_round_trip_and_relationships(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "uniflow.sqlite3"
    initialize_database(database_path)
    Session = create_session_factory(get_engine(database_path))

    with Session.begin() as session:
        source = SourceReference(
            source_type="manual_input",
            description="Phase 1 test provenance",
            information_status=InformationStatus.CONFIRMED,
        )
        semester = Semester(stable_id="TEST_SEMESTER", name="Winter Semester 2026/27", state=SemesterState.ACTIVE)
        course = Course(
            semester=semester,
            stable_id="TEST_SEMESTER_PHYSICS_I",
            code="PHYS-I",
            name="Physics I",
            information_status=InformationStatus.OBSERVED,
            source=source,
        )
        assessment = Assessment(
            course=course,
            name="Written exam",
            assessment_type=AssessmentType.EXAM,
            information_status=InformationStatus.UNKNOWN,
        )
        material = Material(
            course=course,
            stable_id="PHYS_VL01",
            title="Lecture 1",
            lifecycle_state=MaterialState.DOWNLOADED,
            content_hash="a" * 64,
            source=source,
        )
        skill_basic = Skill(id="MATH_CALC_DERIVATIVE_BASIC", name="Basic derivatives", domain="Mathematics")
        skill_physics = Skill(id="PHYS_KINEMATICS_ACCELERATION", name="Acceleration", domain="Physics")
        dependency = SkillDependency(prerequisite=skill_basic, dependent=skill_physics, source=source)
        learning_session = LearningSession(id="SESSION_001", course=course, goal="Practice acceleration")
        evidence = Evidence(
            skill=skill_physics,
            learning_session=learning_session,
            evidence_type=EvidenceType.LEARNING_RESULT,
            value_json='{"recall": 0.8}',
            source=source,
        )
        mastery = DeterministicMasteryService().record_mastery(
            session,
            skill_id=skill_physics.id,
            value=0.72,
            evidence=evidence,
            learning_session_id=learning_session.id,
        )
        official_result = OfficialResult(
            course=course,
            assessment=assessment,
            status="passed",
            grade="1.7",
            credits=10,
            source=source,
        )
        session.add_all(
            [
                source,
                semester,
                course,
                assessment,
                material,
                skill_basic,
                skill_physics,
                dependency,
                learning_session,
                evidence,
                mastery,
                official_result,
            ]
        )

    with Session() as session:
        stored_course = session.query(Course).filter_by(stable_id="TEST_SEMESTER_PHYSICS_I").one()
        stored_dependency = session.query(SkillDependency).one()
        stored_result = session.query(OfficialResult).one()

        assert stored_course.source.information_status == InformationStatus.CONFIRMED
        assert stored_dependency.prerequisite_skill_id == "MATH_CALC_DERIVATIVE_BASIC"
        assert stored_dependency.dependent_skill_id == "PHYS_KINEMATICS_ACCELERATION"
        assert stored_result.grade == "1.7"
        assert session.query(Skill).filter_by(id="PHYS_KINEMATICS_ACCELERATION").one().name == "Acceleration"


def test_material_sha256_uniqueness(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "uniflow.sqlite3"
    initialize_database(database_path)
    Session = create_session_factory(get_engine(database_path))

    with pytest.raises(IntegrityError):
        with Session.begin() as session:
            session.add_all(
                [
                    Material(title="First copy", content_hash="b" * 64),
                    Material(title="Second copy", content_hash="b" * 64),
                ]
            )


def test_official_result_is_separate_from_mastery(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "uniflow.sqlite3"
    initialize_database(database_path)
    Session = create_session_factory(get_engine(database_path))

    with Session.begin() as session:
        semester = Semester(stable_id="SS2027", name="Summer Semester 2027")
        course = Course(semester=semester, stable_id="SS2027_NEURO", name="Neurobiology")
        skill = Skill(id="NEURO_MEMBRANE_POTENTIAL", name="Membrane potential")
        session.add_all([semester, course, skill])
        session.flush()
        session.add(OfficialResult(course=course, status="passed", grade="2.0", credits=5))
        DeterministicMasteryService().record_mastery(session, skill_id=skill.id, value=0.64)

    with Session() as session:
        assert session.query(OfficialResult).count() == 1
        assert session.query(Skill).count() == 1
        assert session.query(Skill).one().id == "NEURO_MEMBRANE_POTENTIAL"
