"""Repository semantic extraction uses synthetic Git snapshots and fixture output only."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from uniflow.application.academic_classification import (
    accept_classification_proposal, create_manual_repository_proposal,
)
from uniflow.application.initializer import initialize_project
from uniflow.application.repository_semantic_extraction import (
    build_repository_extraction_request, execute_repository_extraction,
    list_repository_semantic_files,
)
from uniflow.application.semantic_extraction_runtime import FixtureAdapter
from uniflow.cli.main import app
from uniflow.application.repository_snapshots import register_repository, snapshot_repository
from uniflow.application.skill_extraction_foundation import accept_skill_proposal, create_semantic_run
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, DerivedArtifact, Evidence, LearningSession, MasteryState,
    RepositoryAcademicLink, RepositoryFile, RepositorySnapshot, SemanticEvidenceSpan,
    SemanticExtractionAttempt, Semester, Skill, SkillAlias, SkillDependency,
    SkillProposal, CourseSkill,
)


def prepared(tmp_path, *, tracked=True):
    cfg = load_config(project_root=tmp_path / "project")
    initialize_project(cfg)
    Session = create_session_factory(get_engine(cfg.database_path))
    with Session.begin() as session:
        semester = Semester(stable_id="S", name="Synthetic")
        session.add(Course(stable_id="C", name="Programming", semester=semester))
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL)
    git("init", "-q")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "synthetic@example.invalid")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "README.md").write_bytes(b"Assignment: explain caf\xc3\xa9\r\n")
    (repo / "src" / "Main.java").write_bytes(b"class Main { // caf\xc3\xa9\n}\n")
    (repo / "tests" / "MainTest.java").write_bytes(b"test Main\n")
    (repo / ".env").write_bytes(b"SYNTHETIC=1\n")
    (repo / "invalid.txt").write_bytes(b"\xff")
    (repo / "binary.txt").write_bytes(b"a\x00b")
    (repo / "empty.txt").write_bytes(b"")
    if tracked:
        git("add", ".")
        git("commit", "-qm", "synthetic")
    register_repository(repo, cfg, stable_id="R")
    proposal = create_manual_repository_proposal(cfg, "R", course="C")
    accept_classification_proposal(cfg, proposal)
    snapshot_repository("R", cfg)
    with Session() as session:
        link = session.scalar(select(RepositoryAcademicLink))
        snap = session.scalar(select(RepositorySnapshot))
        files = {f.path: f.id for f in session.scalars(select(RepositoryFile))}
        ids = link.id, snap.id
    return cfg, Session, repo, files, ids


def run(cfg, ids, state="WORKTREE", role="EXERCISE"):
    return create_semantic_run(cfg, "REPOSITORY", repository_academic_link_id=ids[0],
        repository_snapshot_id=ids[1], repository_state=state, role=role)


def candidate(item, phrase, name="Concept", candidate_id="a"):
    start = item.text.index(phrase)
    return {"candidate_id": candidate_id, "name": name, "kind": "CONCEPT",
        "evidence": [{"item_id": item.item_id, "start": start, "end": start + len(phrase)}]}


def test_synthetic_end_to_end_review_and_retry(tmp_path):
    cfg, Session, repo, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    listings = list_repository_semantic_files(cfg, rid)
    assert {x.relative_path for x in listings if x.eligible} == {"README.md", "src/Main.java", "tests/MainTest.java"}
    assert {x.relative_path: x.reason for x in listings if not x.eligible}[".env"] == "PATH_EXCLUDED"
    request = build_repository_extraction_request(cfg, rid, [files["src/Main.java"], files["README.md"], files["README.md"]])
    assert [x.relative_path for x in request.items] == ["README.md", "src/Main.java"]
    assert request == build_repository_extraction_request(cfg, rid, [files["README.md"], files["src/Main.java"]])
    assert str(repo) not in request.input_manifest_json
    assert "Assignment:" not in request.input_manifest_json
    assert "tests/MainTest.java" not in request.input_manifest_json
    assert hashlib.sha256(request.input_manifest_json.encode()).hexdigest() == request.input_package_sha256
    readme, source = request.items
    assert (readme.chunk_byte_start, readme.chunk_byte_end) == (0, len(readme.text.encode()))
    result = {"schema_version": "1.0", "skills": [candidate(readme, "café", "Unicode", "a"),
        candidate(source, "café", "Code", "b")]}
    first = execute_repository_extraction(cfg, rid, [files["README.md"], files["src/Main.java"]],
        fixture_result={"schema_version": "1.0", "skills": [{**result["skills"][0], "evidence": [{"item_id": "bad", "start": 0, "end": 1}]}]})
    assert (first.status, first.error_code) == ("FAILED", "EVIDENCE_INVALID")
    with Session() as session:
        assert session.query(SkillProposal).count() == session.query(SemanticEvidenceSpan).count() == 0
    second = execute_repository_extraction(cfg, rid, [files["src/Main.java"], files["README.md"]],
        fixture_result=result, retry_of_attempt_id=first.attempt_id)
    assert (second.status, second.attempt_number, second.candidate_count, second.proposal_count) == ("SUCCEEDED", 2, 2, 2)
    with Session() as session:
        attempt = session.get(SemanticExtractionAttempt, second.attempt_id)
        assert session.get(SemanticExtractionAttempt, first.attempt_id).status == "FAILED"
        artifact = session.get(DerivedArtifact, attempt.output_artifact_id)
        assert artifact.kind == "SEMANTIC_EXTRACTION_OUTPUT_JSON"
        assert hashlib.sha256(Path(artifact.stored_path).read_bytes()).hexdigest() == attempt.output_sha256
        spans = session.scalars(select(SemanticEvidenceSpan).order_by(SemanticEvidenceSpan.byte_start)).all()
        assert len(spans) == 2 and all(s.extraction_attempt_id == second.attempt_id for s in spans)
        assert [(s.byte_start, s.byte_end) for s in spans] == [(len(b"class Main { // "), len(b"class Main { // caf\xc3\xa9")),
            (len(b"Assignment: explain "), len(b"Assignment: explain caf\xc3\xa9"))]
        assert all(s.span_sha256 == hashlib.sha256("café".encode()).hexdigest() for s in spans)
        proposals = session.scalars(select(SkillProposal).order_by(SkillProposal.proposed_name)).all()
        assert len(proposals) == 2 and all(p.status == "PENDING" and p.extraction_attempt_id == second.attempt_id for p in proposals)
        assert all(session.query(model).count() == 0 for model in
            (Skill, SkillAlias, SkillDependency, CourseSkill, Evidence, MasteryState, LearningSession))
        proposal_ids = [p.id for p in proposals]
    skill = accept_skill_proposal(cfg, proposal_ids[0]).target_id
    assert accept_skill_proposal(cfg, proposal_ids[1], existing_skill_id=skill).action == "RESOLVE_TO_EXISTING_SKILL"
    with Session() as session:
        assert session.query(Skill).count() == 1
        assert all(session.query(model).count() == 0 for model in (SkillAlias, Evidence, MasteryState, LearningSession))
    changed = execute_repository_extraction(cfg, rid, [files["README.md"]], fixture_result={"schema_version": "1.0", "skills": []}, retry_of_attempt_id=first.attempt_id)
    assert changed.error_code == "RETRY_INPUT_MISMATCH"
    new = execute_repository_extraction(cfg, rid, [files["README.md"]], fixture_result={"schema_version": "1.0", "skills": []})
    assert new.status == "SUCCEEDED" and new.attempt_number == 4


@pytest.mark.parametrize("path,code", [(".env", "PATH_EXCLUDED"), ("invalid.txt", "UNSUPPORTED_TEXT_ENCODING"),
    ("binary.txt", "BINARY_FILE_UNSUPPORTED"), ("empty.txt", "EMPTY_SOURCE")])
def test_selected_file_failures_are_atomic(tmp_path, path, code):
    cfg, Session, _, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    result = {"schema_version": "1.0", "skills": []}
    outcome = execute_repository_extraction(cfg, rid, [files["README.md"], files[path]], fixture_result=result)
    assert (outcome.status, outcome.error_code) == ("FAILED", code)
    with Session() as session:
        assert session.query(SkillProposal).count() == session.query(SemanticEvidenceSpan).count() == 0


def test_state_selection_and_untracked(tmp_path):
    cfg, _, _, files, ids = prepared(tmp_path, tracked=False)
    work = run(cfg, ids, "WORKTREE")
    request = build_repository_extraction_request(cfg, work, [files["README.md"]])
    assert request.items[0].repository_state == "WORKTREE"
    head = run(cfg, ids, "HEAD")
    outcome = execute_repository_extraction(cfg, head, [files["README.md"]], fixture_result={"schema_version": "1.0", "skills": []})
    assert outcome.error_code == "FILE_STATE_MISSING"
    assert build_repository_extraction_request(cfg, work, [files["src/Main.java"]]).input_package_sha256 != request.input_package_sha256


def test_chunking_bom_and_limits(tmp_path):
    cfg, _, _, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    cfg.repository_semantic_max_item_chars = 8
    request = build_repository_extraction_request(cfg, rid, [files["README.md"]])
    assert len(request.items) > 1
    assert "".join(i.text for i in request.items) == "Assignment: explain café\r\n"
    assert all(len(i.text) <= 8 for i in request.items)
    assert all(i.chunk_byte_end - i.chunk_byte_start == len(i.text.encode()) for i in request.items)
    cfg.repository_semantic_max_items = 1
    with pytest.raises(ValueError, match="INPUT_TOO_LARGE"):
        build_repository_extraction_request(cfg, rid, [files["README.md"]])


def test_head_index_worktree_are_exact_and_no_fallback(tmp_path):
    cfg, Session, repo, _, ids = prepared(tmp_path)
    source = repo / "src" / "Main.java"
    source.write_bytes(b"class Index {}\n")
    subprocess.run(["git", "-C", str(repo), "add", "src/Main.java"], check=True)
    source.write_bytes(b"class Worktree {}\n")
    snapshot_repository("R", cfg)
    with Session() as session:
        snap = session.scalars(select(RepositorySnapshot).order_by(RepositorySnapshot.captured_at.desc())).first()
        fid = session.scalar(select(RepositoryFile).where(RepositoryFile.snapshot_id == snap.id,
            RepositoryFile.path == "src/Main.java")).id
        link = session.scalar(select(RepositoryAcademicLink)).id
    texts = {}
    hashes = {}
    for state in ("HEAD", "INDEX", "WORKTREE"):
        rid = run(cfg, (link, snap.id), state)
        request = build_repository_extraction_request(cfg, rid, [fid])
        texts[state] = request.items[0].text
        hashes[state] = request.input_package_sha256
    assert texts == {"HEAD": "class Main { // café\n}\n", "INDEX": "class Index {}\n", "WORKTREE": "class Worktree {}\n"}
    assert len(set(hashes.values())) == 3


def test_bom_crlf_multibyte_exact_bytes_and_policy(tmp_path):
    cfg, Session, repo, _, ids = prepared(tmp_path)
    data = b"\xef\xbb\xbfalpha\r\nbeta caf\xc3\xa9\r\n"
    (repo / "bom.txt").write_bytes(data)
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.txt").write_text("synthetic", encoding="utf-8")
    snapshot_repository("R", cfg)
    with Session() as session:
        snap = session.scalars(select(RepositorySnapshot).order_by(RepositorySnapshot.captured_at.desc())).first()
        fs = {f.path: f.id for f in session.scalars(select(RepositoryFile).where(RepositoryFile.snapshot_id == snap.id))}
        link = session.scalar(select(RepositoryAcademicLink)).id
    rid = run(cfg, (link, snap.id))
    item = build_repository_extraction_request(cfg, rid, [fs["bom.txt"]]).items[0]
    assert item.text == "\ufeffalpha\r\nbeta café\r\n"
    outcome = execute_repository_extraction(cfg, rid, [fs["bom.txt"]],
        fixture_result={"schema_version": "1.0", "skills": [candidate(item, "café")]})
    assert outcome.status == "SUCCEEDED"
    with Session() as session:
        span = session.scalar(select(SemanticEvidenceSpan))
        assert (span.byte_start, span.byte_end) == (data.index("café".encode()), data.index("café".encode()) + len("café".encode()))
    assert execute_repository_extraction(cfg, rid, [fs["vendor/lib.txt"]],
        fixture_result={"schema_version": "1.0", "skills": []}).error_code == "PATH_EXCLUDED"


def test_missing_selection_wrong_snapshot_scope_and_caps(tmp_path):
    cfg, Session, repo, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    with pytest.raises(ValueError, match="FILE_SELECTION_REQUIRED"):
        build_repository_extraction_request(cfg, rid, [])
    with pytest.raises(ValueError, match="UNKNOWN_REPOSITORY_FILE"):
        build_repository_extraction_request(cfg, rid, ["missing"])
    cfg.repository_semantic_max_files = 1
    with pytest.raises(ValueError, match="INPUT_TOO_LARGE"):
        build_repository_extraction_request(cfg, rid, [files["README.md"], files["src/Main.java"]])
    cfg.repository_semantic_max_files = 8
    cfg.repository_semantic_max_file_bytes = 1
    with pytest.raises(ValueError, match="INPUT_TOO_LARGE"):
        build_repository_extraction_request(cfg, rid, [files["README.md"]])
    cfg.repository_semantic_max_file_bytes = 65536
    with Session.begin() as session:
        session.get(RepositoryAcademicLink, ids[0]).is_active = False
    outcome = execute_repository_extraction(cfg, rid, [files["README.md"]],
        fixture_result={"schema_version": "1.0", "skills": []})
    assert outcome.error_code == "ACADEMIC_SCOPE_CHANGED"


def test_cli_metadata_and_fixture_path(tmp_path):
    cfg, _, repo, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    cli = CliRunner()
    listed = cli.invoke(app, ["repository-semantic-files", rid, "--project-root", str(cfg.project_root)])
    assert listed.exit_code == 0 and "README.md" in listed.stdout and str(repo) not in listed.stdout
    missing = cli.invoke(app, ["semantic-extract-repository", rid, "--result-json", str(tmp_path / "missing.json"),
        "--project-root", str(cfg.project_root)])
    assert missing.exit_code != 0 and "FILE_SELECTION_REQUIRED" in missing.stderr
    fixture = tmp_path / "result.json"
    fixture.write_text(json.dumps({"schema_version": "1.0", "skills": []}), encoding="utf-8")
    extracted = cli.invoke(app, ["semantic-extract-repository", rid, "--file", files["README.md"],
        "--result-json", str(fixture), "--project-root", str(cfg.project_root)])
    assert extracted.exit_code == 0 and "status=SUCCEEDED" in extracted.stdout


def test_final_scope_change_fails_atomically(tmp_path):
    cfg, Session, _, files, ids = prepared(tmp_path)
    rid = run(cfg, ids)
    request = build_repository_extraction_request(cfg, rid, [files["README.md"]])
    response = {"schema_version": "1.0", "skills": [candidate(request.items[0], "café")]}
    class DeactivatingFixture(FixtureAdapter):
        def extract(self, request):
            with Session.begin() as session:
                session.get(RepositoryAcademicLink, ids[0]).is_active = False
            return response
    outcome = execute_repository_extraction(cfg, rid, [files["README.md"]],
        fixture_result=response, adapter=DeactivatingFixture(response))
    assert outcome.error_code == "ACADEMIC_SCOPE_CHANGED"
    with Session() as session:
        assert session.query(SemanticEvidenceSpan).count() == session.query(SkillProposal).count() == 0
