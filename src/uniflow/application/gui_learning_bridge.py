"""Small GUI bridge over the existing material and learning services."""
from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
import hashlib

from sqlalchemy import select

from uniflow.application import document_parsing, learning_workflow, material_intake, skill_extraction_foundation
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.schemas.workflow_v1 import CreateSessionSuccessV1
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (Course, CourseSkill, Material,
    MaterialArtifact, ParseRun, ParsedUnit, Semester, Skill, SourceArtifact)


def _session(config):
    return create_session_factory(get_engine(config.database_path))()


def course_skills(config, course_stable_id):
    with _session(config) as db:
        rows = db.execute(select(Skill.id, Skill.name).join(CourseSkill, CourseSkill.skill_id == Skill.id)
            .join(Course, Course.id == CourseSkill.course_id)
            .where(Course.stable_id == course_stable_id, CourseSkill.is_active.is_(True),
                   Skill.status == "ACTIVE").order_by(Skill.name, Skill.id)).all()
        return [(row.id, row.name) for row in rows]


def semester_courses(config, semester_id):
    with _session(config) as db:
        return [(c.stable_id, c.name) for c in db.scalars(select(Course).join(Semester,
            Course.semester_id == Semester.id).where(Semester.stable_id == semester_id)
            .order_by(Course.name, Course.stable_id))]


def create_course_study_session(config, semester_stable_id: str, course_stable_id: str,
        goal: str, skill_ids: list[str]) -> CreateSessionSuccessV1:
    """Resolve GUI stable identities; retain the workflow's target and goal gates."""
    with _session(config) as db:
        course_id = db.scalar(select(Course.id).join(Semester,
            Course.semester_id == Semester.id).where(
                Course.stable_id == course_stable_id,
                Semester.stable_id == semester_stable_id))
        if course_id is None:
            raise WorkflowCommandError("INVALID_SESSION_COURSE")
    return learning_workflow.create_session(config, {"course_id": course_id,
        "goal": goal, "target_skill_ids": skill_ids})


def import_and_parse(config, path: Path, course_stable_id: str):
    if path.suffix.lower() not in {".pdf", ".txt", ".md"}:
        raise ValueError("SUPPORTED_MATERIAL_TYPE_REQUIRED")
    # Stable per course and content so a repeated click/import reuses the same Material.
    if not path.is_file():
        raise ValueError("INVALID_SOURCE")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    stable_id = str(uuid5(NAMESPACE_URL, f"uniflow:gui-material:{course_stable_id}:{digest}"))
    intake = material_intake.import_material(path, config, course_stable_id=course_stable_id,
        material_stable_id=stable_id, title=path.name)
    if intake.status == "CONFLICT":
        raise ValueError("MATERIAL_IMPORT_CONFLICT")
    with _session(config) as db:
        artifact = db.scalar(select(SourceArtifact).where(SourceArtifact.sha256 == intake.sha256))
        material = db.scalar(select(Material).where(Material.stable_id == stable_id))
        artifact_id, material_id = artifact.id, material.id
    parsed = document_parsing.parse_source_artifact(artifact_id, config)
    return material_id, parsed.status, parsed.error_code


def propose_document_skill(config, material_id: str, name: str, kind: str, quote: str):
    if (not isinstance(name, str) or not name.strip() or
            kind not in {"CONCEPT", "ABILITY"} or
            not isinstance(quote, str) or not quote.strip() or len(quote) > 4000):
        raise ValueError("SKILL_INPUT_INVALID")
    with _session(config) as db:
        material = db.get(Material, material_id)
        if material is None or material.course_id is None:
            raise ValueError("MATERIAL_COURSE_REQUIRED")
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id,
            MaterialArtifact.is_current.is_(True)))
        if link is None:
            raise ValueError("MATERIAL_SOURCE_REQUIRED")
        parse = db.scalar(select(ParseRun).where(ParseRun.source_artifact_id == link.artifact_id,
            ParseRun.status == "SUCCESS").order_by(ParseRun.completed_at.desc(), ParseRun.id.desc()))
        if parse is None:
            raise ValueError("PARSED_MATERIAL_REQUIRED")
        units = [(u.id, u.ordinal) for u in db.scalars(select(ParsedUnit).where(
            ParsedUnit.parse_run_id == parse.id).order_by(ParsedUnit.ordinal))]
        link_id, parse_id = link.id, parse.id
    match = _unique_quote_match(quote, ((unit_id,
        document_parsing.read_parsed_unit(parse_id, ordinal, config)) for unit_id, ordinal in units))
    run_id = skill_extraction_foundation.create_semantic_run(config, "DOCUMENT",
        material_artifact_id=link_id, parse_run_id=parse_id)
    evidence_id = skill_extraction_foundation.create_semantic_evidence(config, run_id, *match)
    return skill_extraction_foundation.create_skill_proposal(config, run_id, "NEW_SKILL",
        evidence_ids=[evidence_id], name=name.strip(), skill_kind=kind)


def _unique_quote_match(quote: str, units):
    """Find exactly one occurrence, retaining the original unit-local offsets."""
    match = None
    for unit_id, value in units:
        start = value.find(quote)
        if start < 0:
            continue
        if match is not None or value.find(quote, start + 1) >= 0:
            raise ValueError("SKILL_QUOTE_MUST_MATCH_ONE_PARSED_UNIT")
        match = (unit_id, start, start + len(quote))
    if match is None:
        raise ValueError("SKILL_QUOTE_MUST_MATCH_ONE_PARSED_UNIT")
    return match


def propose_selected_document_skill(config, material_id: str, parse_run_id: str,
        parsed_unit_id: str, start: int, end: int, name: str, kind: str):
    """Propose from an exact, unit-local text selection without quote lookup."""
    return skill_extraction_foundation.create_selected_document_skill(
        config, material_id, parse_run_id, parsed_unit_id, start, end, name, kind)


def associate_skill_with_course(config, material_id: str, skill_id: str):
    with _session(config) as db:
        material = db.get(Material, material_id)
        skill = db.get(Skill, skill_id)
        if material is None or material.course_id is None or skill is None or skill.status != "ACTIVE":
            raise ValueError("SKILL_COURSE_INVALID")
        course_id = material.course_id
    run_id = skill_extraction_foundation.create_semantic_run(config, "MANUAL")
    proposal_id = skill_extraction_foundation.create_skill_proposal(config, run_id,
        "SKILL_COURSE_ASSOCIATION", target_skill_id=skill_id, course_id=course_id)
    return skill_extraction_foundation.accept_skill_proposal(config, proposal_id)
