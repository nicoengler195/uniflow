"""C1: arithmetic/schema checks, NOT mathematical or provenance validators.

Review records are test-only data. Every case remains HUMAN_REVIEW_REQUIRED.
No project configuration, database connection, importer or mastery operation.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_scoring_contract_v1 import payload, task
from uniflow.application.scoring_contract_v1 import score_result
from uniflow.schemas.scoring_contract_v1 import ScoringResultV1

FIXTURES = Path(__file__).parent / 'fixtures' / 'scoring_c1'
REFERENCES = json.loads((FIXTURES / 'references.json').read_text(encoding='utf-8'))
P = 'SYNTHETIC_POWER_RULE'
C = 'SYNTHETIC_CHAIN_RULE'
D = 'SYNTHETIC_COMPOSITE_DERIVATIVE'
SCOPE = frozenset({P, C, D})
HISTORICAL_SHA = '1b545e372484c256c1ecaccc50c6118fa6408b79a9a7735d0935732ba27f70d0'
HANDOFF_SHA = '535c1e148ff4d4941e3e677e64f1cd2523ec396c6fc646934f503ec39e5a3ff7'


def score(value):
    model = ScoringResultV1.model_validate(value)
    return score_result(model, session_id=value['session_id'], handoff_id=value['handoff_id'],
                        handoff_artifact_sha256=value['handoff_artifact_sha256'],
                        allowed_skill_ids=SCOPE)


def ledger(awards):
    return '; '.join(f"{a['criterion']}={a['awarded']}/{a['max']} [{a['quote']}]" for a in awards)


def criteria(atoms):
    return '; '.join(f"{a['criterion']}:{a['max']} binary" for a in atoms)


def scenario(name):
    ref = REFERENCES['cases'][name]
    rubric = REFERENCES['rubrics'][ref['rubric_id']]
    marks = [(sid, value, None) for sid, value in ref['expected_marks'].items()]
    anchor = task('LEARNING-ANCHOR', 'LEARNING', marks=[(sid, 1.0, None) for sid, _, _ in marks])
    anchor.update(rubric='Synthetic calibration anchor; all declared marks 1.0. Not a mathematical example.',
                  evidence='Synthetic calibration input only, not observed learner work.',
                  rationale='Arithmetic anchor, no claim of independently reviewed mathematical evidence.')
    phase = 'LEARNING' if name == 'T5' else 'FINAL_EXAM'
    assessed = task(ref['task_id'], phase, ref['expected_fraction'], ref['difficulty'], marks)
    assessed['rubric'] = (ref['rubric_id'] + ' NEW_SYNTHETIC; TASK: ' + ref['task'] + '; ' +
        criteria(rubric['task']) + '; domain R; bracket/product/factored/expanded forms allowed. '
        'Final explicit answer else last unretracted line; contradictory candidates: final=0. '
        'Correct method after earlier error keeps method credit; no double penalty. '
        'A1 only; help before A1 is assessed, later correction is qualitative. '
        'RUBRIC_SOURCE: declared before synthetic answer, historical predeclaration MISSING. '
        'TASK_SOURCE: RECONSTRUCTED from design, not original chat. Difficulty: ' + ref['difficulty_reason'])
    assessed['evidence'] = ('ANSWER_SOURCE: RECONSTRUCTED new complete synthetic answer, not original chat. '
        'TIME_SOURCE: synthetic fixed test dates, not DIRECT_OBSERVATION. '
        'EVIDENCE_GAPS: original chat, external independence proof and historical predeclared rubric MISSING. '
        'Attempt A1; help before A1: none declared; independence externally unconfirmed. ANSWER: ' + ref['answer'])
    assessed['rationale'] = ledger(ref['task_awards']) + '; HUMAN_REVIEW_REQUIRED; ' + ref['provenance']
    for mark in assessed['skill_marks']:
        sid = mark['skill_id']
        mark['criterion'] = criteria(rubric['skills'][sid])
        mark['rationale'] = ledger(ref['skill_awards'][sid])
    if name == 'T5':
        # A1 in LEARNING; a separate synthetic exam anchor completes the session.
        anchor = task('EXAM-ANCHOR', 'FINAL_EXAM', marks=[(sid, 1.0, None) for sid, _, _ in marks])
    value = payload([assessed, anchor] if name == 'T5' else [anchor, assessed])
    for state in value['phase_states']:
        state['difficulty_rationale'] = ('Synthetic fixed times only; no observed event claims. '
            'Difficulty predeclared: ' + ref['difficulty_reason'])
    if name == 'T1':
        value['errors'] = [{'task_id':ref['task_id'], 'skill_id':None,
            'description':'Invalid algebra: 12x^3-6x^2+5 is not 108x^2+5.',
            'context':'A1 final contradiction; 80 process points + 0/20 final.', 'status':'OBSERVED'}]
    return value


@pytest.mark.parametrize('rid', ['R1', 'R2', 'R3', 'R4'])
def test_reference_rubrics_have_atomic_100_point_dimensions(rid):
    rubric = REFERENCES['rubrics'][rid]
    for dimension in [rubric['task'], *rubric['skills'].values()]:
        assert sum(a['max'] for a in dimension) == 100
        assert len({a['criterion'] for a in dimension}) == len(dimension)


@pytest.mark.parametrize('name,session,skills', [
    ('T1','90',{P:'100'}), ('T2','100',{P:'100',C:'100',D:'100'}),
    ('T3','97.5',{P:'100',C:'100',D:'95'}), ('T4','65',{P:'80',C:'50',D:'80'}),
    ('T5','75',{P:'75',C:'75',D:'80'}), ('T8','100',{P:'100'}),
])
def test_reference_arithmetic_and_quote_traceability(name, session, skills):
    ref = REFERENCES['cases'][name]
    assert ref['review_status'] == 'HUMAN_REVIEW_REQUIRED'
    rubric = REFERENCES['rubrics'][ref['rubric_id']]
    for awarded, declared in [(ref['task_awards'], rubric['task']),
                             *((ref['skill_awards'][s], a) for s, a in rubric['skills'].items())]:
        assert [(a['criterion'], a['max']) for a in awarded] == [
            (a['criterion'], a['max']) for a in declared]
    # Quotes and sums are mechanically checked, their mathematical relevance is reviewed by humans.
    for atoms, fraction in [(ref['task_awards'],ref['expected_fraction']),
                           *((ref['skill_awards'][s],f) for s,f in ref['expected_marks'].items())]:
        assert sum(a['awarded'] for a in atoms) == Decimal(str(fraction))*100
        for a in atoms:
            assert a['awarded'] in (0,a['max'])
            assert a['quote'] in ref['answer']
    value = scenario(name)
    result = score(value)
    assert result.session_score == Decimal(session)
    assert result.skill_session_scores == {s:Decimal(v) for s,v in skills.items()}
    phase = 'LEARNING' if name == 'T5' else 'FINAL_EXAM'
    assert result.phase_points[phase] == Decimal(str(ref['expected_fraction']))*50
    assert result.assessment_status == 'PENDING_REVIEW'
    assert result.completion_status == 'COMPLETE'
    assert all(mark['difficulty'] is None for t in value['tasks'] for mark in t['skill_marks'])


def test_t1_unresolved_candidates_and_documented_self_correction():
    value = scenario('T1')
    value['tasks'][1]['evidence'] += ' Two unretracted candidates: 12x^3-6x^2+5 and 108x^2+5.'
    assert score(value).session_score == 90  # human rubric: final block remains zero
    corrected = scenario('T8')
    corrected['tasks'][1]['evidence'] += (' Synthetic order: wrote 108x^2+5, explicitly retracted BEFORE '
        'submission; Final: x^2(12x-6)+5. Not evidence of historical chronology.')
    assert score(corrected).session_score == 100


def test_t4_predeclared_power_override_changes_only_skill_weighting():
    value = scenario('T4')
    other = task('EXAM-ANCHOR', 'FINAL_EXAM', 1.0, 'HARD',
                 [(sid, 1.0, None) for sid in (P, C, D)])
    other.update(rubric='Synthetic arithmetic anchor, predeclared unit inputs; no mathematical assessment.',
                 evidence='Fixed synthetic calibration input only, not learner evidence.',
                 rationale='Unit inputs isolate the effect of Skill difficulty weighting.')
    value['tasks'].append(other)
    baseline = score(value)
    value['tasks'][1]['skill_marks'][0].update(difficulty='MEDIUM',
        rationale='Pre-answer override: local power operations MEDIUM; nested chain/structure HARD. '
                  + value['tasks'][1]['skill_marks'][0]['rationale'])
    changed = score(value)
    assert changed.phase_points == baseline.phase_points
    assert changed.session_score == baseline.session_score
    assert baseline.skill_session_scores[P] == 90
    assert changed.skill_session_scores[P] == 92
    assert changed.skill_session_scores[C] == baseline.skill_session_scores[C]
    assert changed.skill_session_scores[D] == baseline.skill_session_scores[D]


def test_t5_later_guided_correction_does_not_change_first_score():
    value = scenario('T5')
    before = score(value)
    value['tasks'][0]['additional_skills'] = [{'provisional_skill_name':'Algebra',
        'observation':"AFTER_ASSESSMENT GUIDED: explanation, corrected f'=5(x^2-3x+1)^4(2x-3); "
                      'separate check 5(2x-3)=10x-15. Reconstructed synthetic order; no new independent evidence.'}]
    assert score(value) == before
    assert len(value['tasks']) == 2
    assert value['tasks'][0]['fraction'] == .5
    assert value['tasks'][0]['assistance'] == 'NONE'
    # Correct representation if an explanation actually preceded A1.
    value['tasks'][0].update(assistance='EXPLANATION',independent_solution=False)
    ScoringResultV1.model_validate(value)
    value['tasks'][0]['independent_solution'] = True
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(value)


def test_t6_bonus_is_unscored_and_cannot_contain_fraction_or_marks():
    value = scenario('T2')
    baseline = score(value)
    value['tasks'].append({'task_id':'BONUS','task_type':'PREVIEW_BONUS',
        'observation':'Qualitative only', 'additional_skills':[{'provisional_skill_name':'Algebra',
        'observation':'Future topic, no performance score.'}]})
    assert score(value) == baseline
    for key,val in [('fraction',1.0),('skill_marks',[])]:
        bad = deepcopy(value)
        bad['tasks'][-1][key] = val
        with pytest.raises(ValidationError):
            ScoringResultV1.model_validate(bad)


def historical():
    raw = (FIXTURES/'historical_result.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_SHA
    return json.loads(raw)


def test_t7_reconstructed_time_annotation_preserves_values_and_scores():
    value = historical()
    before = deepcopy(value)
    baseline = score(value)
    for t in value['tasks']:
        t['evidence'] += (' ANSWER_SOURCE: RECONSTRUCTED quoted excerpt; TASK_SOURCE: RECONSTRUCTED result declaration; '
            'RUBRIC_SOURCE: declaration, prior fixation MISSING; TIME_SOURCE: RECONSTRUCTED approximate chronology; '
            'EVIDENCE_GAPS: original transcript and exact time observation MISSING.')
    for state in value['phase_states']:
        state['difficulty_rationale'] += ' TIME_SOURCE: RECONSTRUCTED, approximate only.'
    assert score(value) == baseline
    assert value['final_exam_decision'] == before['final_exam_decision']
    assert [(t['issued_at'],t['answered_at']) for t in value['tasks']] == [
        (t['issued_at'],t['answered_at']) for t in before['tasks']]
    draft = scenario('T1')
    del draft['tasks'][1]['answered_at']
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(draft)  # stays outside final V1; no fabricated replacement


def test_t8_reordered_product_is_also_accepted_as_human_assessed_input():
    value = scenario('T2')
    assessed = value['tasks'][1]
    for record, field in [(assessed, 'evidence'), (assessed, 'rationale'),
                          *((mark, 'rationale') for mark in assessed['skill_marks'])]:
        record[field] = record[field].replace(
            '4(x^3-2x+4)^3(3x^2-2)', '4(3x^2-2)(x^3-2x+4)^3')
    assert score(value).session_score == 100  # not automatic equivalence recognition


def test_t9_qualitative_algebra_preserves_all_scores():
    value = scenario('T1')
    before = score(value)
    value['tasks'][1]['additional_skills'] = [{'provisional_skill_name':'Algebra',
        'observation':'Incorrect claimed equivalence to 108x^2+5; qualitative need only.'}]
    assert value['errors'][0]['skill_id'] is None
    assert score(value) == before


@pytest.mark.parametrize('kind',['mark','error','additional'])
def test_t9_unknown_skill_ids_rejected_by_engine(kind):
    value = scenario('T1')
    if kind == 'mark':
        value['tasks'][1]['skill_marks'][0]['skill_id'] = 'SYNTHETIC_ALGEBRA'
    elif kind == 'error':
        value['errors'][0]['skill_id'] = 'SYNTHETIC_ALGEBRA'
    else:
        value['tasks'][1]['additional_skills'] = [{'skill_id':'SYNTHETIC_ALGEBRA','observation':'Outside scope'}]
    with pytest.raises(ValueError,match='scope'):
        score(value)


def test_t10_byte_identical_historical_result_and_handoff():
    value = historical()
    raw = (FIXTURES/'historical_handoff.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HANDOFF_SHA == value['handoff_artifact_sha256']
    handoff = json.loads(raw)
    assert handoff['session_id'] == value['session_id']
    assert frozenset(handoff['return_contract']['allowed_skill_ids']) == SCOPE
    result = score(value)
    assert result == score(value)
    assert result.phase_points == {'LEARNING':Decimal('40.00'),
        'FINAL_EXAM':Decimal('29.583333333333333333333333333333333333333333333333')}
    assert result.session_score == Decimal('69.58333333333333333333333333')
    assert result.skill_session_scores == {P:Decimal('70.625'), C:Decimal('58.0'),
        D:Decimal('59.85714285714285714285714286')}
    by_id = {t['task_id']:t for t in value['tasks']}
    assert [by_id[s]['fraction'] for s in ('EXAM-001','EXAM-002','EXAM-003','LEARN-003')] == [1.0,.9,.25,.5]
    assert by_id['LEARN-003']['skill_marks'][1]['fraction'] == .3


@pytest.mark.parametrize('violation',['changed_rubric','unsupported_independence','false_final_full_credit','unsupported_direct_time'])
def test_semantic_negative_is_schema_valid_but_human_review_required(violation):
    value = scenario('T1')
    assessed = value['tasks'][1]
    if violation == 'changed_rubric':
        assessed['rubric'] = 'Changed AFTER answer: ignore final answer, process only.'
    elif violation == 'unsupported_independence':
        assessed['evidence'] = 'Independence guaranteed, no supporting source. ' + assessed['evidence']
    elif violation == 'false_final_full_credit':
        assessed['fraction'] = 1.0
    else:
        assessed['evidence'] = 'TIME_SOURCE: DIRECT_OBSERVATION; source MISSING. ' + assessed['evidence']
    result = score(value)  # expected V1 limitation, NEVER a semantic validation success
    assert result.assessment_status == 'PENDING_REVIEW'
    assert REFERENCES['cases']['T1']['review_status'] == 'HUMAN_REVIEW_REQUIRED'


@pytest.mark.parametrize('field,limit',[('rubric',4000),('rationale',4000),('evidence',4000),
                                      ('mark.criterion',2000),('mark.rationale',2000)])
def test_exact_text_limits_and_overflow_rejection(field,limit):
    value = scenario('T2')
    target = value['tasks'][1]
    if field.startswith('mark.'):
        target = target['skill_marks'][0]
        field = field.split('.')[1]
    target[field] = 'x'*limit
    ScoringResultV1.model_validate(value)
    target[field] += 'x'
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(value)


@pytest.mark.parametrize('help_kind,hints,independent',[
    ('NONE',['hidden hint'],True), ('HINTS',[],False), ('HINTS',['hint'],True),
    ('EXPLANATION',[],True), ('NOT_INDEPENDENT',[],True)])
def test_invalid_help_independence_combinations(help_kind,hints,independent):
    value = scenario('T5')
    value['tasks'][0].update(assistance=help_kind,hints=hints,independent_solution=independent)
    with pytest.raises(ValidationError):
        ScoringResultV1.model_validate(value)
