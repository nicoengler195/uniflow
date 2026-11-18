import hashlib
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from test_skill_extraction_foundation import setup_document
from uniflow.application.semantic_extraction_runtime import (
    FixtureAdapter, build_document_extraction_request, execute_document_extraction,
    get_semantic_attempt, list_semantic_attempts,
)
from uniflow.application.skill_extraction_foundation import accept_skill_proposal, create_semantic_run
from uniflow.infrastructure.database.engine import get_engine
from uniflow.infrastructure.database.models import (
    CourseSkill, DerivedArtifact, Evidence, LearningSession, MasteryState,
    Material, MaterialArtifact, SemanticEvidenceSpan, SemanticExtractionAttempt,
    SemanticExtractionRun, Skill, SkillAlias, SkillDependency, SkillProposal,
)


def prepared(tmp_path):
    config, Session, parse_id, (link_id, unit_id, _, _) = setup_document(tmp_path)
    run_id = create_semantic_run(config, "DOCUMENT", material_artifact_id=link_id,
        parse_run_id=parse_id, role="LECTURE_MATERIAL")
    request = build_document_extraction_request(config, run_id)
    return config, Session, run_id, request, link_id


def result(request, *, two=False):
    item = request.items[0]
    skills = [{"candidate_id": "a", "name": "Chain rule", "kind": "CONCEPT",
        "confidence": 0.6, "evidence": [{"item_id": item.item_id, "start": 4, "end": 14}]}]
    if two:
        skills.append({"candidate_id": "b", "name": "Differentiate a composition", "kind": "ABILITY",
            "evidence": [{"item_id": item.item_id, "start": 4, "end": 14}]})
    return {"schema_version": "1.0", "skills": skills}


def test_fixture_success_review_and_no_side_effects(tmp_path):
    config, Session, run_id, request, _ = prepared(tmp_path)
    assert request == build_document_extraction_request(config, run_id)
    assert len(request.items) == 1
    assert request.items[0].text_sha256 == hashlib.sha256(request.items[0].text.encode()).hexdigest()
    assert request.items[0].item_id.startswith("unit:")
    assert "chain rule" not in request.input_manifest_json
    outcome = execute_document_extraction(config, run_id, fixture_result=result(request, two=True))
    assert (outcome.status, outcome.attempt_number, outcome.candidate_count, outcome.proposal_count) == ("SUCCEEDED", 1, 2, 2)
    with Session() as session:
        attempt = session.get(SemanticExtractionAttempt, outcome.attempt_id)
        artifact = session.get(DerivedArtifact, attempt.output_artifact_id)
        assert artifact.kind == "SEMANTIC_EXTRACTION_OUTPUT_JSON"
        assert Path(artifact.stored_path).read_bytes() == json.dumps(
            {"schema_version": "1.0", "skills": [
                {"candidate_id": c["candidate_id"], "name": c["name"], "kind": c["kind"],
                 "description": None, "domain": None, "confidence": c.get("confidence"),
                 "evidence": c["evidence"]} for c in result(request, two=True)["skills"]]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        assert attempt.output_sha256 == hashlib.sha256(Path(artifact.stored_path).read_bytes()).hexdigest()
        proposals = session.scalars(select(SkillProposal).order_by(SkillProposal.proposed_name)).all()
        assert len(proposals) == 2
        assert all(p.status == "PENDING" and p.proposal_kind == "NEW_SKILL" and p.extraction_attempt_id == attempt.id for p in proposals)
        spans = session.scalars(select(SemanticEvidenceSpan)).all()
        assert len(spans) == 1
        assert (spans[0].char_start, spans[0].char_end, spans[0].span_sha256) == (4, 14, hashlib.sha256(b"chain rule").hexdigest())
        assert spans[0].extraction_attempt_id == attempt.id
        assert session.query(Skill).count() == session.query(SkillAlias).count() == session.query(SkillDependency).count() == session.query(CourseSkill).count() == 0
        assert session.query(Evidence).count() == session.query(MasteryState).count() == session.query(LearningSession).count() == 0
        assert session.query(Material).first().lifecycle_state.name != "MAPPED_TO_SKILLS"
        ids = [p.id for p in proposals]
    canonical = accept_skill_proposal(config, ids[0]).target_id
    assert accept_skill_proposal(config, ids[1], existing_skill_id=canonical).action == "RESOLVE_TO_EXISTING_SKILL"
    with Session() as session:
        assert session.query(Skill).count() == 1
        assert session.query(SkillAlias).count() == 0
        assert session.query(Evidence).count() == session.query(MasteryState).count() == 0


def test_invalid_batch_retry_and_idempotency(tmp_path):
    config, Session, run_id, request, _ = prepared(tmp_path)
    bad = result(request, two=True)
    bad["skills"][1]["evidence"][0]["item_id"] = "page 5"
    first = execute_document_extraction(config, run_id, fixture_result=bad)
    assert (first.status, first.error_code) == ("FAILED", "EVIDENCE_INVALID")
    with Session() as session:
        assert session.query(SkillProposal).count() == session.query(SemanticEvidenceSpan).count() == 0
    second = execute_document_extraction(config, run_id, fixture_result=result(request, two=True), retry_of_attempt_id=first.attempt_id)
    assert (second.status, second.attempt_number) == ("SUCCEEDED", 2)
    third = execute_document_extraction(config, run_id, fixture_result=result(request, two=True))
    assert (third.status, third.attempt_number, third.proposal_count) == ("SUCCEEDED", 3, 2)
    with Session() as session:
        assert session.query(SkillProposal).count() == 2
        assert session.query(SemanticEvidenceSpan).count() == 1
        assert session.query(DerivedArtifact).filter_by(kind="SEMANTIC_EXTRACTION_OUTPUT_JSON").count() == 1
        assert session.get(SemanticExtractionAttempt, first.attempt_id).status == "FAILED"
        assert session.get(SemanticExtractionRun, run_id).output_sha256 is None


def test_strict_validation_chunking_and_limits(tmp_path):
    config, Session, run_id, request, _ = prepared(tmp_path)
    invalid = []
    for change in ({"extra": 1}, {"schema_version": "2.0"}):
        value = result(request)
        value.update(change)
        invalid.append(value)
    for name, kind, confidence in (("", "CONCEPT", None), ("x"*241, "CONCEPT", None),
        ("X", "OTHER", None), ("X", "CONCEPT", 1.1), ("X", "CONCEPT", float("nan"))):
        value = result(request)
        value["skills"][0].update(name=name, kind=kind, confidence=confidence)
        invalid.append(value)
    for value in invalid:
        assert execute_document_extraction(config, run_id, fixture_result=value).error_code == "INVALID_RESPONSE"
    config.semantic_max_chunk_chars = 10
    chunks = build_document_extraction_request(config, run_id)
    assert [i.unit_char_start for i in chunks.items] == list(range(0, len(request.items[0].text), 10))
    assert all(i.unit_char_end - i.unit_char_start <= 10 for i in chunks.items)
    assert chunks.input_package_sha256 != request.input_package_sha256
    config.semantic_max_input_items = 1
    assert execute_document_extraction(config, run_id, fixture_result=result(request)).error_code == "INPUT_TOO_LARGE"


def test_external_denied_and_scope_change(tmp_path):
    config, Session, run_id, request, link_id = prepared(tmp_path)
    class External:
        name, version, is_external, capabilities = "FAKE", "1", True, frozenset({"DOCUMENT"})
        called = False
        def extract(self, request):
            self.called = True
            raise AssertionError("must not call")
    external = External()
    denied = execute_document_extraction(config, run_id, adapter=external)
    assert (denied.error_code, external.called) == ("DISCLOSURE_DENIED", False)
    class Deactivate(FixtureAdapter):
        def extract(self, request):
            with Session.begin() as session:
                session.get(MaterialArtifact, link_id).is_current = False
            return result(request)
    changed = execute_document_extraction(config, run_id, adapter=Deactivate(result(request)))
    assert (changed.status, changed.error_code) == ("FAILED", "ACADEMIC_SCOPE_CHANGED")
    with Session() as session:
        assert session.query(SkillProposal).count() == session.query(SemanticEvidenceSpan).count() == 0


def test_output_limit_and_non_document_runtime(tmp_path):
    config, Session, run_id, request, _ = prepared(tmp_path)
    config.semantic_max_output_bytes = 20
    assert execute_document_extraction(config, run_id, fixture_result=result(request)).error_code == "OUTPUT_TOO_LARGE"
    manual = create_semantic_run(config, "MANUAL")
    assert execute_document_extraction(config, manual, fixture_result={"schema_version": "1.0", "skills": []}).error_code == "UNSUPPORTED_EXTRACTION_SOURCE"
    with Session() as session:
        assert session.query(SkillProposal).count() == session.query(SemanticEvidenceSpan).count() == 0


def test_migration_0008_preserves_manual_rows(tmp_path):
    db = tmp_path / "old.sqlite3"
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src/uniflow/infrastructure/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "0008_skill_extraction_foundation")
    # Historical 0008 uses live ORM metadata on fresh installs; its temporary
    # fixture has a forward FK before 0009 creates the referenced table.
    with get_engine(db).connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.execute(text("INSERT INTO semantic_extraction_runs (id,created_at,source_family,source_role,extractor_name,extractor_version,input_identity_sha256) VALUES ('r',CURRENT_TIMESTAMP,'MANUAL','UNKNOWN','MANUAL','1','x')"))
        conn.execute(text("INSERT INTO skill_proposals (id,created_at,extraction_run_id,proposal_kind,status,identity_sha256) VALUES ('p',CURRENT_TIMESTAMP,'r','NEW_SKILL','PENDING','y')"))
        conn.commit()
    command.upgrade(cfg, "head")
    with get_engine(db).connect() as conn:
        assert conn.execute(text("SELECT extraction_attempt_id FROM skill_proposals WHERE id='p'")).scalar_one() is None
        assert conn.execute(text("SELECT id FROM semantic_extraction_runs WHERE id='r'")).scalar_one() == "r"
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
