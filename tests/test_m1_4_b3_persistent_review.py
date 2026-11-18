"""B3 acceptance on fresh synthetic projects only."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from test_learning_results import setup
from test_learning_chat_exchange import result, write
from uniflow.application.learning_chat_exchange import export_learning_chat_package, preview_scoring_result
from uniflow.application.scoring_review import save_review, read_review, reopen_review, list_reviews
from uniflow.infrastructure.database.models import ScoringResultSnapshot, ScoringReviewEvent, MasteryState, Evidence, LearningResult


@pytest.fixture
def case(tmp_path):
    config, factory, sid, _, handoff = setup(tmp_path)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    path = write(tmp_path, result(package))
    raw = path.read_bytes()
    request = dict(session_id=sid, handoff_id=handoff.handoff_id, handoff_revision=handoff.revision,
        result_sha256=hashlib.sha256(raw).hexdigest(), decisions={'TARGET': 'ACCEPTED'}, reason='Explicit local review',
        expected_version=0, request_id=str(uuid4()), confirmed=True)
    yield config, factory, path, raw, request
    factory.kw['bind'].dispose()


def test_roundtrip_history_original_and_no_mastery(case):
    config, factory, path, raw, req = case
    before = config.database_path.read_bytes()
    assert read_review(config, req['result_sha256'], req['session_id'], req['handoff_id']).version == 0
    assert config.database_path.read_bytes() == before
    preview = preview_scoring_result(config, path)
    assert preview.raw_bytes == raw
    first = save_review(config, raw, **req)
    assert first.version == 1 and first.decisions == {'TARGET':'ACCEPTED', 'PREREQ':'PENDING_HUMAN_REVIEW'}
    assert first.history[0]['origin'] == 'LOCAL_GUI_CONFIRMATION'
    assert first.history[0]['decided_at']
    assert path.read_bytes() == raw
    path.unlink()
    reopened = reopen_review(config, req['result_sha256'], req['session_id'], req['handoff_id'])
    assert reopened.raw_bytes == raw
    assert list_reviews(config, req['session_id'], req['handoff_id']) == (req['result_sha256'],)
    second = save_review(config, raw, **dict(req, request_id=str(uuid4()), expected_version=1,
        decisions={'TARGET':'DEFERRED', 'PREREQ':'REJECTED'}, reason='Correction required'))
    assert second.version == 2 and len(second.history) == 2
    assert second.history[0] == first.history[0]
    assert read_review(config, req['result_sha256'], req['session_id'], req['handoff_id']) == second
    with factory() as db:
        assert db.get(ScoringResultSnapshot, req['result_sha256']).raw_bytes == raw
        for model in (MasteryState, Evidence, LearningResult):
            assert not list(db.scalars(select(model)))


@pytest.mark.parametrize('change', ['session','handoff','revision','sha','bytes','confirmation','unknown_skill','unassessed','status','pending'])
def test_reject_invalid_boundaries(case, change):
    config, factory, path, raw, req = case
    if change == 'session': req['session_id'] = 'foreign'
    if change == 'handoff': req['handoff_id'] = 'foreign'
    if change == 'revision': req['handoff_revision'] += 1
    if change == 'sha': req['result_sha256'] = '0'*64
    if change == 'bytes': raw += b' '
    if change == 'confirmation': req['confirmed'] = False
    if change == 'unknown_skill': req['decisions'] = {'OTHER':'ACCEPTED'}
    if change == 'unassessed': req['decisions'] = {'PREREQ':'ACCEPTED'}
    if change == 'status': req['decisions'] = {'TARGET':'VERIFIED'}
    if change == 'pending': req['decisions'] = {}
    with pytest.raises(ValueError): save_review(config, raw, **req)
    with factory() as db:
        assert not list(db.scalars(select(ScoringResultSnapshot)))
        assert not list(db.scalars(select(ScoringReviewEvent)))


def test_replay_concurrency_and_stale_conflict(case):
    config, factory, _, raw, req = case
    def save(_): return save_review(config, raw, **dict(req, request_id=str(uuid4())))
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(save, range(4)))
    assert all(s.version == 1 for s in states)
    assert save_review(config, raw, **req).version == 1
    with pytest.raises(ValueError, match='VERSION_CONFLICT'):
        save_review(config, raw, **dict(req, decisions={'TARGET':'REJECTED'}, request_id=str(uuid4())))
    with factory() as db:
        assert len(list(db.scalars(select(ScoringReviewEvent)))) == 1
    latest = save_review(config, raw, **dict(req, expected_version=1, decisions={'TARGET':'DEFERRED'}, request_id=str(uuid4())))
    assert latest.version == 2
    with pytest.raises(ValueError, match='REQUEST_CONFLICT'):
        save_review(config, raw, **dict(req, request_id=latest.history[-1]['id']))


def test_injected_database_failure_rolls_back_snapshot(case):
    config, factory, _, raw, req = case
    with factory.begin() as db:
        db.execute(text("CREATE TRIGGER fail_test BEFORE INSERT ON scoring_review_events BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"))
    with pytest.raises(IntegrityError): save_review(config, raw, **req)
    with factory() as db:
        assert not list(db.scalars(select(ScoringResultSnapshot)))
        assert not list(db.scalars(select(ScoringReviewEvent)))


@pytest.mark.parametrize('table', ['scoring_result_snapshots','scoring_review_events'])
@pytest.mark.parametrize('action', ['UPDATE','DELETE'])
def test_database_immutability(case, table, action):
    config, factory, _, raw, req = case
    save_review(config, raw, **req)
    query = f'DELETE FROM {table}' if action == 'DELETE' else f'UPDATE {table} SET ' + ('sha256=sha256' if table.endswith('snapshots') else 'reason=reason')
    with pytest.raises(IntegrityError):
        with factory.begin() as db: db.execute(text(query))


def test_reject_foreign_reopen(case):
    config, _, _, raw, req = case
    save_review(config, raw, **req)
    for reader in (read_review, reopen_review):
        with pytest.raises(ValueError, match='IDENTITY_MISMATCH'):
            reader(config, req['result_sha256'], 'foreign', req['handoff_id'])

def test_superseded_handoff_history_remains_readable(case):
    from uniflow.application.learning_handoffs import generate_handoff
    from uniflow.infrastructure.database.models import LearningSession
    config, factory, _, raw, req = case
    save_review(config, raw, **req)
    with factory.begin() as db:
        db.get(LearningSession, req['session_id']).goal = 'Changed synthetic goal'
    generate_handoff(config, req['session_id'])
    preview = reopen_review(config, req['result_sha256'], req['session_id'], req['handoff_id'])
    assert preview.raw_bytes == raw and preview.handoff_revision == req['handoff_revision']
    assert list_reviews(config, req['session_id']) == ((req['result_sha256'], req['handoff_id']),)
    with pytest.raises(ValueError):
        save_review(config, raw, **dict(req, expected_version=1, decisions={'TARGET':'REJECTED'}, request_id=str(uuid4())))


def test_different_concurrent_decisions_have_one_winner(case):
    config, factory, _, raw, req = case
    def save(status):
        try:
            return save_review(config, raw, **dict(req, decisions={'TARGET':status}, request_id=str(uuid4())))
        except ValueError as exc:
            assert str(exc) == 'REVIEW_VERSION_CONFLICT'
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(save, ['ACCEPTED','REJECTED']))
    assert sum(s is not None for s in states) == 1
    with factory() as db:
        assert len(list(db.scalars(select(ScoringReviewEvent)))) == 1


def test_migration_from_synthetic_0017(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from uniflow.infrastructure.database.engine import get_engine
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path/'migration.sqlite3'
    cfg = Config(str(root/'alembic.ini'))
    cfg.set_main_option('script_location', str(root/'src/uniflow/infrastructure/database/migrations'))
    cfg.set_main_option('sqlalchemy.url', 'sqlite:///' + db_path.as_posix())
    command.upgrade(cfg, '0017_audit_resolution')
    engine = get_engine(db_path)
    with engine.begin() as conn:
        before = dict(conn.execute(text("SELECT name,sql FROM sqlite_master WHERE type='table'")).all())
        conn.execute(text("INSERT INTO semesters (id,stable_id,name,state,timezone,created_at,updated_at) VALUES ('synthetic','SYN','Synthetic','ACTIVE','UTC',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(cfg, '0018_scoring_review')
    command.upgrade(cfg, '0018_scoring_review')
    with engine.connect() as conn:
        after = dict(conn.execute(text("SELECT name,sql FROM sqlite_master WHERE type='table'")).all())
        assert set(after)-set(before) == {'scoring_result_snapshots','scoring_review_events'}
        assert all(after[name] == sql for name, sql in before.items())
        assert conn.execute(text("SELECT name FROM semesters WHERE id='synthetic'")).scalar_one() == 'Synthetic'
        assert conn.execute(text('PRAGMA integrity_check')).scalar_one() == 'ok'
        assert not conn.execute(text('PRAGMA foreign_key_check')).all()
    engine.dispose()

def test_missing_skill_is_explicitly_pending_and_cannot_be_accepted(case):
    config, _, _, raw, req = case
    body = json.loads(raw)
    for task in body['tasks']:
        task['skill_marks'] = [mark for mark in task['skill_marks'] if mark['skill_id'] != 'PREREQ']
    raw = json.dumps(body).encode()
    req['result_sha256'] = hashlib.sha256(raw).hexdigest()
    state = save_review(config, raw, **req)
    assert state.decisions['PREREQ'] == 'PENDING_HUMAN_REVIEW'
    with pytest.raises(ValueError, match='SKILL_UNASSESSED'):
        save_review(config, raw, **dict(req, request_id=str(uuid4()), expected_version=1,
            decisions={'PREREQ':'ACCEPTED'}))
