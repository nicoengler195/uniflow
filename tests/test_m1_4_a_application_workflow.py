"""Synthetic M1.4-A application path; no scoring return or mastery application."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from threading import Event, Thread

import pytest
from sqlalchemy import select

from uniflow.application import (document_parsing, gui_learning_bridge as bridge,
    learning_workflow, material_intake, skill_extraction_foundation as foundation)
from uniflow.application.initializer import initialize_project
from uniflow.application.review_read_models import get_skill_review
from uniflow.application.review_workflow import accept_new_skill, reject_skill, resolve_existing_skill
from uniflow.application.workflow_errors import WorkflowCommandError
from uniflow.domain.enums import SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (
    Course, CourseSkill, DerivedArtifact, Evidence, LearningHandoff, LearningResult,
    LearningSession, MasteryState, MaterialArtifact, ParseRun, ParsedUnit, Semester,
    Skill, SkillProposal, Material, SemanticExtractionRun, SemanticEvidenceSpan,
    SkillProposalEvidence,
)
from uniflow.infrastructure.write_lock import project_write_lock, WriterLockTimeout

TEXT = "Einleitung 🧭\n𝑥² + α = 0; Größe e\u0301.\n𝑥² + α = 0; Größe e\u0301.\n"
QUOTE = "𝑥² + α = 0; Größe e\u0301."


@pytest.fixture
def project(tmp_path):
    config = load_config(project_root=tmp_path / "project")
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add_all([Semester(id="semester-db", stable_id="S", name="Synthetic",
            state=SemesterState.ACTIVE, starts_on=date(2026, 10, 1),
            ends_on=date(2027, 3, 31), timezone="UTC"),
            Semester(id="other-semester-db", stable_id="T", name="Other",
            state=SemesterState.ACTIVE, starts_on=date(2026, 10, 1),
            ends_on=date(2027, 3, 31), timezone="UTC")])
        db.flush()
        db.add_all([Course(id="course-db", stable_id="C", semester_id="semester-db", name="Math"),
            Course(id="other-course-db", stable_id="O", semester_id="semester-db", name="Other"),
            Course(id="foreign-course-db", stable_id="F", semester_id="other-semester-db", name="Foreign")])
    source = tmp_path / "synthetic.txt"
    source.write_text(TEXT, encoding="utf-8")
    material_id, status, error = bridge.import_and_parse(config, source, "C")
    assert (status, error) == ("SUCCESS", None)
    with factory() as db:
        link = db.scalar(select(MaterialArtifact).where(MaterialArtifact.material_id == material_id))
        parse = db.scalar(select(ParseRun).where(ParseRun.source_artifact_id == link.artifact_id))
        unit = db.scalar(select(ParsedUnit).where(ParsedUnit.parse_run_id == parse.id))
        identities = material_id, link.id, parse.id, unit.id
    yield config, factory, identities
    # Every test, including failure paths, must leave these write domains untouched.
    with factory() as db:
        for model in (LearningResult, Evidence, MasteryState):
            assert list(db.scalars(select(model))) == []
    factory.kw["bind"].dispose()


def proposal(project, name="Unicode concept"):
    config, _, (material, _, parse, unit) = project
    text = document_parsing.read_parsed_unit(parse, 1, config)
    assert text == TEXT
    start = text.rindex(QUOTE)  # Repeated passage: selection, never quote lookup.
    return bridge.propose_selected_document_skill(config, material, parse, unit,
        start, start + len(QUOTE), name, "CONCEPT")


def review_request(config, pid):
    review = get_skill_review(config, "S", pid)
    return {"semester_id": "S", "proposal_id": pid,
        "expected_review_context_token": review.review_context_token}


def accepted(project):
    config, _, _ = project
    pid = proposal(project)
    request = review_request(config, pid)
    result = accept_new_skill(config, request)
    assert result.outcome == "ACCEPTED"
    assert accept_new_skill(config, request).outcome == "ALREADY_ACCEPTED"
    return result.accepted_skill_id


def session_handoff(project):
    config, _, (material, _, _, _) = project
    skill = accepted(project)
    bridge.associate_skill_with_course(config, material, skill)
    session = bridge.create_course_study_session(config, "S", "C", "Understand α and 𝑥²", [skill])
    handoff = learning_workflow.generate_handoff(config, {"session_id": session.session_id})
    return skill, session, handoff


def assert_code(code, fn):
    with pytest.raises(WorkflowCommandError) as caught:
        fn()
    assert caught.value.code == code


def test_unicode_review_association_session_revision_and_single_file_export(project, tmp_path):
    config, factory, (material, _, parse, unit) = project
    assert bridge.course_skills(config, "C") == []
    skill = accepted(project)
    assert bridge.course_skills(config, "C") == []  # Review does not associate.
    assert_code("TARGET_COURSESKILL_REQUIRED", lambda: bridge.create_course_study_session(
        config, "S", "C", "Learn", [skill]))
    bridge.associate_skill_with_course(config, material, skill)
    assert bridge.course_skills(config, "C") == [(skill, "Unicode concept")]
    session = bridge.create_course_study_session(config, "S", "C", "Understand α and 𝑥²", [skill])
    with factory() as db:
        assert db.get(LearningSession, session.session_id).course_id == "course-db"
    handoff = learning_workflow.generate_handoff(config, {"session_id": session.session_id})
    repeated = learning_workflow.generate_handoff(config, {"session_id": session.session_id})
    assert repeated.outcome == "ALREADY_GENERATED" and repeated.handoff_id == handoff.handoff_id
    destination = tmp_path / "exports"
    destination.mkdir()
    exported = learning_workflow.export_learning_chat(config, {"handoff_id": handoff.handoff_id,
        "destination_directory": destination})
    assert exported.status == "EXPORTED"
    original = exported.path.read_bytes()
    package = json.loads(original)
    assert list(destination.iterdir()) == [exported.path]
    assert package["session_id"] == session.session_id
    assert package["handoff_id"] == handoff.handoff_id
    assert package["handoff_revision"] == handoff.revision
    assert package["handoff_artifact_sha256"] == handoff.artifact_sha256
    assert package["allowed_skill_ids"] == [skill]
    assert exported.result_filename == package["result_filename"]
    body = package["handoff"]
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == handoff.artifact_sha256
    assert body["goal"] == "Understand α and 𝑥²"
    source = body["source_data"][0]
    assert source["material_id"] == material and source["parsed_unit_id"] == unit
    assert source["parse_run_id"] == parse
    assert (source["char_start"], source["char_end"]) == (TEXT.rindex(QUOTE), TEXT.rindex(QUOTE) + len(QUOTE))
    assert source["span_sha256"] == hashlib.sha256(QUOTE.encode("utf-8")).hexdigest()
    assert source["excerpt"] == QUOTE
    assert all(item["status"] == "UNKNOWN" for item in body["mastery_snapshot"])
    again = learning_workflow.export_learning_chat(config, {"handoff_id": handoff.handoff_id,
        "destination_directory": destination})
    assert again.status == "ALREADY_EXPORTED" and again.path.read_bytes() == original
    with factory.begin() as db:
        db.get(Skill, skill).name = "Explicit revised concept label"
    revised = learning_workflow.generate_handoff(config, {"session_id": session.session_id})
    assert revised.handoff_id != handoff.handoff_id and revised.revision == handoff.revision + 1
    assert_code("EXCHANGE_HANDOFF_NOT_CURRENT", lambda: learning_workflow.export_learning_chat(
        config, {"handoff_id": handoff.handoff_id, "destination_directory": destination}))
    assert learning_workflow.export_learning_chat(config, {"handoff_id": revised.handoff_id,
        "destination_directory": destination}).status == "EXPORTED"
    assert exported.path.read_bytes() == original


def test_explicit_resolve_reject_and_stale_review(project):
    config, factory, (_, link, _, _) = project
    skill = accepted(project)
    alias = proposal(project, "Alternative name")
    request = review_request(config, alias) | {"existing_skill_id": skill}
    assert resolve_existing_skill(config, request).accepted_skill_id == skill
    assert resolve_existing_skill(config, request).outcome == "ALREADY_ACCEPTED"
    rejected = proposal(project, "Rejected concept")
    request = review_request(config, rejected)
    assert reject_skill(config, request | {"reason": "Not useful"}).outcome == "REJECTED"
    assert_code("REVIEW_PROPOSAL_NOT_PENDING", lambda: accept_new_skill(config, request))
    assert_code("INVALID_TARGET_SKILL", lambda: bridge.create_course_study_session(
        config, "S", "C", "Learn", [rejected]))
    pending = proposal(project, "Pending concept")
    stale = review_request(config, pending)
    with factory.begin() as db:
        db.get(MaterialArtifact, link).is_current = False
    assert_code("REVIEW_CONTEXT_CHANGED", lambda: accept_new_skill(config, stale))
    with factory() as db:
        assert len(list(db.scalars(select(Skill)))) == 1
        assert db.get(SkillProposal, pending).status == "PENDING"
        assert db.get(SkillProposal, rejected).accepted_skill_id is None
        assert list(db.scalars(select(CourseSkill))) == []


@pytest.mark.parametrize("semester,course", [("S", "F"), ("T", "C"), ("missing", "C"),
    ("S", "missing"), ("semester-db", "C"), ("S", "course-db")])
def test_session_stable_identity_membership(project, semester, course):
    config, factory, _ = project
    assert_code("INVALID_SESSION_COURSE", lambda: bridge.create_course_study_session(
        config, semester, course, "Learn", ["missing"]))
    with factory() as db:
        assert list(db.scalars(select(LearningSession))) == []


def test_session_target_and_goal_gates(project):
    config, factory, (material, _, _, _) = project
    skill = accepted(project)
    bridge.associate_skill_with_course(config, material, skill)
    assert_code("TARGET_COURSESKILL_REQUIRED", lambda: bridge.create_course_study_session(
        config, "S", "O", "Wrong course", [skill]))
    assert_code("INVALID_SESSION_GOAL", lambda: bridge.create_course_study_session(
        config, "S", "C", " ", [skill]))
    with factory.begin() as db:
        db.scalar(select(CourseSkill).where(CourseSkill.skill_id == skill)).is_active = False
    assert_code("TARGET_COURSESKILL_REQUIRED", lambda: bridge.create_course_study_session(
        config, "S", "C", "Inactive association", [skill]))
    with factory.begin() as db:
        db.scalar(select(CourseSkill).where(CourseSkill.skill_id == skill)).is_active = True
        db.add(Skill(id="canonical-target", name="Canonical concept", kind="CONCEPT"))
        db.flush()
        db.get(Skill, skill).status = "MERGED"
        db.get(Skill, skill).canonical_skill_id = "canonical-target"
    assert_code("INVALID_TARGET_SKILL", lambda: bridge.create_course_study_session(
        config, "S", "C", "Inactive skill", [skill]))


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED", "OCR_REQUIRED"])
def test_explicit_old_parse_never_substituted_even_on_same_source(project, status):
    config, factory, (material, _, parse_id, unit) = project
    with factory.begin() as db:
        old = db.get(ParseRun, parse_id)
        db.add(ParseRun(id=str(uuid4()), source_artifact_id=old.source_artifact_id,
            parser_name=old.parser_name, parser_version=old.parser_version,
            dependency_version=old.dependency_version, config_json=old.config_json,
            config_sha256=old.config_sha256, attempt_number=old.attempt_number + 1,
            status=status, started_at=old.started_at + timedelta(seconds=1),
            completed_at=old.completed_at + timedelta(seconds=1)))
    with pytest.raises(ValueError, match="^SKILL_SELECTION_SOURCE_CHANGED$"):
        bridge.propose_selected_document_skill(config, material, parse_id, unit, 0, 4, "Stale", "CONCEPT")
    with factory() as db:
        assert list(db.scalars(select(SkillProposal))) == []


@pytest.mark.parametrize("change", ["material", "unit", "parse", "inactive-link"])
def test_selection_source_identity_mismatch(project, change):
    config, factory, (material, link, parse_id, unit) = project
    if change == "material":
        source = config.project_root / "other.txt"
        source.write_text("Different source", encoding="utf-8")
        material, _, _ = bridge.import_and_parse(config, source, "O")
    elif change == "unit":
        unit = "missing"
    elif change == "parse":
        parse_id = "missing"
    else:
        with factory.begin() as db:
            db.get(MaterialArtifact, link).is_current = False
    with pytest.raises(ValueError, match="^SKILL_SELECTION_SOURCE_CHANGED$"):
        bridge.propose_selected_document_skill(config, material, parse_id, unit, 0, 4, "Wrong", "CONCEPT")


@pytest.mark.parametrize("start,end", [(0, 0), (-1, 3), (0, len(TEXT) + 1), (True, 4),
    (TEXT.index("\n"), TEXT.index("\n") + 1)])
def test_absent_or_invalid_selection(project, start, end):
    config, factory, (material, _, parse, unit) = project
    with pytest.raises(ValueError, match="^SKILL_SELECTION_INVALID$"):
        bridge.propose_selected_document_skill(config, material, parse, unit, start, end, "Invalid", "CONCEPT")
    assert chain_counts(factory) == (0, 0, 0, 0)


def test_export_missing_inconsistent_artifact_and_recovery(project, tmp_path):
    config, factory, _ = project
    _, session, handoff = session_handoff(project)
    export = lambda hid, dest: learning_workflow.export_learning_chat(config,
        {"handoff_id": hid, "destination_directory": dest})
    assert_code("HANDOFF_NOT_FOUND", lambda: export(str(uuid4()), tmp_path))
    assert_code("EXCHANGE_DESTINATION_INVALID", lambda: export(handoff.handoff_id, tmp_path / "absent"))
    file = tmp_path / "not-directory"
    file.write_text("synthetic", encoding="utf-8")
    assert_code("EXCHANGE_DESTINATION_INVALID", lambda: export(handoff.handoff_id, file))
    with factory() as db:
        artifact = db.get(DerivedArtifact, db.get(LearningHandoff, handoff.handoff_id).artifact_id)
        path = Path(artifact.stored_path)
    original = path.read_bytes()
    path.write_bytes(b"inconsistent")
    assert_code("HANDOFF_ARTIFACT_CONFLICT", lambda: export(handoff.handoff_id, tmp_path))
    path.unlink()
    assert_code("HANDOFF_ARTIFACT_CONFLICT", lambda: export(handoff.handoff_id, tmp_path))
    path.write_bytes(original)
    with factory.begin() as db:
        db.get(LearningSession, session.session_id).current_handoff_id = None
    assert_code("EXCHANGE_HANDOFF_NOT_CURRENT", lambda: export(handoff.handoff_id, tmp_path))
    with factory.begin() as db:
        db.get(LearningSession, session.session_id).current_handoff_id = handoff.handoff_id
    assert export(handoff.handoff_id, tmp_path).status == "EXPORTED"

CHAIN = (SemanticExtractionRun, SemanticEvidenceSpan, SkillProposal, SkillProposalEvidence)


def chain_counts(factory):
    with factory() as db:
        return tuple(len(list(db.scalars(select(model)))) for model in CHAIN)


def assert_selection_chain(project, pid):
    config, factory, (_, link, parse, unit) = project
    with factory() as db:
        p = db.get(SkillProposal, pid)
        run = db.get(SemanticExtractionRun, p.extraction_run_id)
        span = db.scalar(select(SemanticEvidenceSpan).where(
            SemanticEvidenceSpan.extraction_run_id == run.id))
        assert (run.material_artifact_id, run.parse_run_id) == (link, parse)
        assert (span.parsed_unit_id, span.char_start, span.char_end) == (
            unit, TEXT.rindex(QUOTE), TEXT.rindex(QUOTE) + len(QUOTE))
        assert span.span_sha256 == hashlib.sha256(QUOTE.encode('utf-8')).hexdigest()
        assert foundation._scope(db, run) == 'LEGACY_MANUAL'
        assert run.input_identity_sha256 == foundation._input_identity(db, run)
        assert db.scalar(select(SkillProposalEvidence).where(
            SkillProposalEvidence.proposal_id == pid)).evidence_span_id == span.id


@pytest.mark.parametrize('status', ['SUCCESS', 'FAILED', 'OCR_REQUIRED', 'RELINK'])
@pytest.mark.parametrize('first', ['proposal', 'writer'])
def test_atomic_selection_real_competing_writers(project, monkeypatch, status, first):
    config, factory, (material, _, parse, _) = project
    with factory() as db:
        artifact = db.get(ParseRun, parse).source_artifact_id
        stable = db.get(Material, material).stable_id
    replacement = config.project_root / 'replacement.txt'
    replacement.write_text('A different material version', encoding='utf-8')

    # New parser identity ensures the real parse writer creates an observation,
    # rather than taking the existing-success fast path. No fixture ParseRun writes.
    class ControlledParser:
        name = 'atomic-repair-parser'
        dependency_version = 'controlled-1'
        format = 'PDF' if status == 'OCR_REQUIRED' else 'TXT'

        def parse(self, source):
            if status == 'FAILED':
                raise document_parsing.ParseFailure('CONTROLLED_FAILURE', 'Synthetic failure')
            return document_parsing.ParsedText(self.format, (
                document_parsing.UnitText('DOCUMENT', None,
                    '   ' if status == 'OCR_REQUIRED' else TEXT),))

    monkeypatch.setattr(document_parsing, 'select_parser', lambda artifact: ControlledParser())
    held, release, attempted = Event(), Event(), Event()
    acquired = {'proposal': Event(), 'writer': Event()}
    order, results, errors = [], {}, {}

    def lock_for(role):
        @contextmanager
        def instrumented(root, timeout):
            if role != first:
                # Prove an actual OS lock attempt is refused while the other
                # product path holds it; signal only after this deterministic probe.
                with pytest.raises(WriterLockTimeout):
                    with project_write_lock(root, 0):
                        pass
                attempted.set()
            with project_write_lock(root, timeout):
                acquired[role].set()
                order.append(role)
                if role == 'writer' and first == 'writer':
                    held.set()
                    assert release.wait(8), 'writer release timed out'
                yield
        return instrumented

    monkeypatch.setattr(foundation, 'project_write_lock', lock_for('proposal'))
    monkeypatch.setattr(document_parsing, 'project_write_lock', lock_for('writer'))
    monkeypatch.setattr(material_intake, 'project_write_lock', lock_for('writer'))
    original = foundation._create_skill_proposal_in_session

    def proposal_before_commit(db, *args, **kwargs):
        pid = original(db, *args, **kwargs)
        db.flush()  # All four chain tables exist in this still-uncommitted transaction.
        p = db.get(SkillProposal, pid)
        run = db.get(SemanticExtractionRun, p.extraction_run_id)
        span = db.scalar(select(SemanticEvidenceSpan).where(
            SemanticEvidenceSpan.extraction_run_id == run.id))
        assert run.parse_run_id == parse
        assert span.char_start == TEXT.rindex(QUOTE)
        assert span.span_sha256 == hashlib.sha256(QUOTE.encode('utf-8')).hexdigest()
        assert foundation._scope(db, run) == 'LEGACY_MANUAL'
        assert foundation._input_identity(db, run) == run.input_identity_sha256
        if first == 'proposal':
            held.set()
            assert release.wait(8), 'proposal release timed out'
        return pid

    monkeypatch.setattr(foundation, '_create_skill_proposal_in_session', proposal_before_commit)

    def writer():
        if status == 'RELINK':
            return material_intake.import_material(replacement, config,
                course_stable_id='C', material_stable_id=stable, new_version=True)
        return document_parsing.parse_source_artifact(artifact, config, retry_failed=True)

    def execute(role, fn):
        try:
            results[role] = fn()
        except BaseException as exc:
            errors[role] = exc

    actions = {'proposal': lambda: proposal(project), 'writer': writer}
    second = 'writer' if first == 'proposal' else 'proposal'
    threads = [Thread(target=execute, args=(role, actions[role]), daemon=True)
               for role in (first, second)]
    threads[0].start()
    try:
        assert held.wait(8), f'first writer did not reach seam: {errors}'
        threads[1].start()
        assert attempted.wait(8), 'competing writer never attempted actual lock'
        assert not acquired[second].is_set()
        # Even flushed proposal rows must be invisible until the single commit.
        assert chain_counts(factory) == (0, 0, 0, 0)
        with factory() as db:
            assert len(list(db.scalars(select(ParseRun)))) == 1
    finally:
        release.set()
        # Never join the blocked writer while holding the lock.
        for thread in threads:
            if thread.ident is not None:
                thread.join(12)
        assert all(not thread.is_alive() for thread in threads)
    assert order == [first, second]
    assert 'writer' not in errors, errors
    if status == 'RELINK':
        assert results['writer'].status == 'NEW_MATERIAL_VERSION'
        with factory() as db:
            assert db.scalar(select(MaterialArtifact).where(
                MaterialArtifact.material_id == material,
                MaterialArtifact.is_current.is_(True))).version_number == 2
    else:
        assert results['writer'].status == status
        assert results['writer'].parse_run_id != parse
        with factory() as db:
            newest = db.scalar(select(ParseRun).order_by(ParseRun.completed_at.desc()))
            assert newest.id == results['writer'].parse_run_id
            assert newest.status == status
    if first == 'proposal':
        assert errors == {}
        assert chain_counts(factory) == (1, 1, 1, 1)
        # The proposal was valid at its commit; a later writer may invalidate scope.
        if status != 'RELINK':
            assert_selection_chain(project, results['proposal'])
        else:
            with factory() as db:
                assert db.get(SkillProposal, results['proposal']).status == 'PENDING'
    else:
        assert type(errors.get('proposal')) is ValueError, errors
        assert str(errors['proposal']) == 'SKILL_SELECTION_SOURCE_CHANGED'
        assert chain_counts(factory) == (0, 0, 0, 0)


def test_repeat_action_keeps_existing_evidence_identity_semantics(project):
    _, factory, _ = project
    first, second = proposal(project), proposal(project)
    assert first != second  # Fresh span IDs enter the existing identity digest.
    assert chain_counts(factory) == (2, 2, 2, 2)
    assert_selection_chain(project, first)
    assert_selection_chain(project, second)
    config, _, _ = project
    with factory() as db:
        p = db.get(SkillProposal, first)
        eid = db.scalar(select(SkillProposalEvidence.evidence_span_id).where(
            SkillProposalEvidence.proposal_id == first))
        run = p.extraction_run_id
    assert foundation.create_skill_proposal(config, run, 'NEW_SKILL', evidence_ids=[eid],
        name='Unicode concept', skill_kind='CONCEPT') == first
    assert chain_counts(factory) == (2, 2, 2, 2)


def test_atomic_chain_rolls_back_after_all_rows_flushed(project, monkeypatch):
    _, factory, _ = project
    original = foundation._create_skill_proposal_in_session

    def fail(db, *args, **kwargs):
        original(db, *args, **kwargs)
        db.flush()
        for model in CHAIN:
            assert len(list(db.scalars(select(model)))) == 1
        raise RuntimeError('controlled rollback')

    with monkeypatch.context() as patch:
        patch.setattr(foundation, '_create_skill_proposal_in_session', fail)
        def separate_reader_forbidden(*args):
            pytest.fail('Atomic path must read source metadata in its own transaction')
        patch.setattr(foundation, 'read_parsed_unit', separate_reader_forbidden)
        with pytest.raises(RuntimeError, match='^controlled rollback$'):
            proposal(project)
    assert chain_counts(factory) == (0, 0, 0, 0)
    pid = proposal(project)
    assert chain_counts(factory) == (1, 1, 1, 1)
    assert_selection_chain(project, pid)


@pytest.mark.parametrize('name,kind', [('', 'CONCEPT'), ('   ', 'ABILITY'),
    (None, 'CONCEPT'), ('Valid', 'INVALID')])
def test_atomic_name_kind_errors_leave_no_chain(project, name, kind):
    config, factory, (material, _, parse, unit) = project
    with pytest.raises(ValueError, match='^SKILL_INPUT_INVALID$'):
        bridge.propose_selected_document_skill(config, material, parse, unit, 0, 4, name, kind)
    assert chain_counts(factory) == (0, 0, 0, 0)


def test_atomic_excerpt_byte_limit_leaves_no_chain(project):
    config, factory, (material, _, parse, unit) = project
    limited = config.model_copy(update={'learning_handoff_max_excerpt_utf8_bytes': 1})
    with pytest.raises(ValueError, match='^SKILL_SELECTION_INVALID$'):
        bridge.propose_selected_document_skill(limited, material, parse, unit,
            TEXT.rindex(QUOTE), TEXT.rindex(QUOTE) + len(QUOTE), 'Concept', 'CONCEPT')
    assert chain_counts(factory) == (0, 0, 0, 0)
