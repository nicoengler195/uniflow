import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from test_m1_4_b3_persistent_review import case
from test_learning_results import setup
from test_learning_chat_exchange import result
from uniflow.application.learning_chat_exchange import export_learning_chat_package
from uniflow.application.scoring_review import save_review
from uniflow.application.controlled_mastery import preview_mastery, apply_mastery, POLICY
from uniflow.infrastructure.database.models import MasteryState, Evidence, ScoringMasteryApplication, LearningSession


def preview(config, req):
    return preview_mastery(config, req['result_sha256'], req['session_id'], req['handoff_id'])


def save(config, raw, req, body=None, decisions=None):
    if body is not None:
        raw = json.dumps(body).encode()
    req = dict(req, result_sha256=hashlib.sha256(raw).hexdigest(), request_id=str(uuid4()),
        decisions=decisions or req['decisions'])
    save_review(config, raw, **req)
    return req


@pytest.mark.parametrize('fraction, expected', [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)])
def test_normalization_and_provenance(case, fraction, expected):
    config, factory, _, raw, req = case
    body = json.loads(raw)
    for task in body['tasks']:
        for mark in task['skill_marks']:
            if mark['skill_id'] == 'TARGET': mark['fraction'] = fraction
    req = save(config, raw, req, body)
    before = config.database_path.read_bytes()
    p = preview(config, req)
    assert config.database_path.read_bytes() == before
    assert next(s for s in p.skills if s.skill_id == 'TARGET').proposed_value == expected
    with pytest.raises(ValueError, match='CONFIRMATION'):
        apply_mastery(config, p, ['TARGET'])
    ids = apply_mastery(config, p, ['TARGET'], confirmed=True)
    with factory() as db:
        entry = db.get(ScoringMasteryApplication, ids[0])
        state = db.get(MasteryState, entry.new_state_id)
        assert state.value == expected and state.calculation_method == POLICY
        assert entry.review_version == 1 and entry.review_event_id == p.review_event_id
        assert entry.result_sha256 == req['result_sha256'] and entry.origin == 'LOCAL_GUI_CONFIRMATION'
        assert db.get(Evidence, state.evidence_id).id == entry.evidence_id
        assert len(list(db.scalars(select(MasteryState)))) == 1
    with pytest.raises(ValueError, match='STALE'):
        apply_mastery(config, p, ['TARGET'], confirmed=True)
    req2 = dict(req, expected_version=1, request_id=str(uuid4()), reason='New review')
    save_review(config, json.dumps(body).encode(), **req2)
    assert next(s for s in preview(config, req).skills if s.skill_id == 'TARGET').proposed_value is None


@pytest.mark.parametrize('mode', ['missing', 'hint', 'explanation', 'not_independent', 'deferred', 'rejected', 'pending'])
def test_exclusions(case, mode):
    config, factory, _, raw, req = case
    body = json.loads(raw)
    decisions = {'TARGET': 'ACCEPTED'}
    if mode == 'missing':
        body['tasks'][1]['skill_marks'] = [dict(body['tasks'][1]['skill_marks'][0], skill_id='PREREQ')]
        decisions = {'PREREQ': 'ACCEPTED'}
    elif mode in ('hint', 'explanation', 'not_independent'):
        task = body['tasks'][1]
        task['independent_solution'] = False
        task['assistance'] = {'hint':'HINTS', 'explanation':'EXPLANATION', 'not_independent':'NOT_INDEPENDENT'}[mode]
        if mode == 'hint': task['hints'] = ['Synthetic help']
    else:
        decisions = {'TARGET': {'deferred':'DEFERRED','rejected':'REJECTED','pending':'PENDING_HUMAN_REVIEW'}[mode], 'PREREQ':'REJECTED'}
    req = save(config, raw, req, body, decisions)
    p = preview(config, req)
    target = next(s for s in p.skills if s.skill_id == 'TARGET')
    assert target.proposed_value is None and target.exclusion_reason
    with pytest.raises(ValueError, match='INELIGIBLE'):
        apply_mastery(config, p, ['TARGET'], confirmed=True)
    with factory() as db:
        assert not list(db.scalars(select(MasteryState)))


def test_independent_skills_and_atomic_rollback(case):
    config, factory, _, raw, req = case
    body = json.loads(raw)
    body['tasks'][1]['skill_marks'].append(dict(body['tasks'][1]['skill_marks'][0], skill_id='PREREQ'))
    bad = dict(body['tasks'][1], task_id='assisted', skill_marks=[dict(body['tasks'][1]['skill_marks'][0])],
        assistance='EXPLANATION', independent_solution=False)
    body['tasks'].append(bad)
    req = save(config, raw, req, body, {'TARGET':'ACCEPTED','PREREQ':'ACCEPTED'})
    p = preview(config, req)
    assert next(s for s in p.skills if s.skill_id == 'TARGET').proposed_value is None
    assert next(s for s in p.skills if s.skill_id == 'PREREQ').proposed_value is not None
    with factory.begin() as db:
        db.execute(text("CREATE TRIGGER fail_apply BEFORE INSERT ON scoring_mastery_applications BEGIN SELECT RAISE(ABORT, 'synthetic'); END"))
    with pytest.raises(ValueError, match='PERSISTENCE'):
        apply_mastery(config, p, ['PREREQ'], confirmed=True)
    with factory() as db:
        for model in (MasteryState, Evidence, ScoringMasteryApplication):
            assert not list(db.scalars(select(model)))


def test_concurrency_review_and_mastery_staleness(case):
    config, factory, _, raw, req = case
    save_review(config, raw, **req)
    p = preview(config, req)
    def attempt(_):
        try: return apply_mastery(config, p, ['TARGET'], confirmed=True)
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(v is not None for v in pool.map(attempt, range(4))) == 1
    with factory() as db:
        assert len(list(db.scalars(select(ScoringMasteryApplication)))) == 1
    # A new result cannot use a preview based on an older review version.
    body = json.loads(raw)
    body['tasks'][0]['evidence'] = 'Distinct snapshot'
    req2 = save(config, raw, dict(req, expected_version=0), body)
    p2 = preview(config, req2)
    save_review(config, json.dumps(body).encode(), **dict(req2, expected_version=1,
        request_id=str(uuid4()), reason='Changed review'))
    with pytest.raises(ValueError, match='STALE'):
        apply_mastery(config, p2, ['TARGET'], confirmed=True)
    p3 = preview(config, req2)
    with factory.begin() as db:
        db.add(MasteryState(skill_id='TARGET', value=0.9, calculation_method='synthetic'))
    with pytest.raises(ValueError, match='STALE'):
        apply_mastery(config, p3, ['TARGET'], confirmed=True)


def test_new_real_session_lineage(case, tmp_path):
    from uniflow.application.learning_handoffs import create_learning_session, generate_handoff
    config, factory, _, raw, req = case
    save_review(config, raw, **req)
    apply_mastery(config, preview(config, req), ['TARGET'], confirmed=True)
    with factory() as db:
        course_id = db.get(LearningSession, req['session_id']).course_id
        first = db.scalar(select(MasteryState))
        first_id = first.id
    sid = create_learning_session(config, course_id, 'New genuine session', ['TARGET'])
    handoff = generate_handoff(config, sid)
    package = json.loads(export_learning_chat_package(config, handoff.handoff_id, tmp_path).path.read_bytes())
    body = result(package)
    body['tasks'][0]['skill_marks'][0]['fraction'] = 0.0
    new_raw = json.dumps(body).encode()
    new_req = dict(req, session_id=sid, handoff_id=handoff.handoff_id, handoff_revision=handoff.revision,
        result_sha256=hashlib.sha256(new_raw).hexdigest(), request_id=str(uuid4()))
    save_review(config, new_raw, **new_req)
    apply_mastery(config, preview(config, new_req), ['TARGET'], confirmed=True)
    with factory() as db:
        states = list(db.scalars(select(MasteryState).order_by(MasteryState.calculated_at)))
        assert len(states) == 2 and states[1].previous_mastery_state_id == first_id
        assert states[0].value == 0.5 and states[1].value == 0.0

@pytest.mark.parametrize('change', ['skill', 'handoff', 'snapshot'])
def test_identity_changes_block_apply(case, change):
    from uniflow.infrastructure.database.models import Skill
    config, factory, _, raw, req = case
    save_review(config, raw, **req)
    p = preview(config, req)
    if change == 'snapshot':
        from dataclasses import replace
        p = replace(p, result_sha256='0' * 64)
    else:
        with factory.begin() as db:
            if change == 'skill':
                skill = db.get(Skill, 'TARGET')
                skill.status = 'MERGED'
                skill.canonical_skill_id = 'OTHER'
            else:
                db.get(LearningSession, req['session_id']).current_handoff_id = None
    with pytest.raises(ValueError):
        apply_mastery(config, p, ['TARGET'], confirmed=True)
    with factory() as db:
        for model in (MasteryState, Evidence, ScoringMasteryApplication):
            assert not list(db.scalars(select(model)))


def test_multiple_skills_roll_back_together(case):
    config, factory, _, raw, req = case
    body = json.loads(raw)
    body['tasks'][1]['skill_marks'].append(dict(body['tasks'][1]['skill_marks'][0], skill_id='PREREQ'))
    req = save(config, raw, req, body, {'TARGET':'ACCEPTED','PREREQ':'ACCEPTED'})
    p = preview(config, req)
    with factory.begin() as db:
        db.execute(text("CREATE TRIGGER fail_second BEFORE INSERT ON scoring_mastery_applications WHEN NEW.skill_id='TARGET' BEGIN SELECT RAISE(ABORT, 'synthetic second skill failure'); END"))
    with pytest.raises(ValueError, match='PERSISTENCE'):
        apply_mastery(config, p, ['PREREQ', 'TARGET'], confirmed=True)
    with factory() as db:
        for model in (MasteryState, Evidence, ScoringMasteryApplication):
            assert not list(db.scalars(select(model)))


def test_incomplete_phases_never_turn_missing_into_zero(case):
    config, factory, _, raw, req = case
    body = json.loads(raw)
    body['tasks'] = body['tasks'][:1]
    body['phase_states'][1]['status'] = 'NOT_STARTED'
    body['final_exam_decision']['decision'] = 'PAUSE'
    req = save(config, raw, req, body, {'TARGET':'DEFERRED'})
    p = preview(config, req)
    assert all(s.source_points is None and s.proposed_value is None for s in p.skills)
    with pytest.raises(ValueError, match='INELIGIBLE'):
        apply_mastery(config, p, ['TARGET'], confirmed=True)
