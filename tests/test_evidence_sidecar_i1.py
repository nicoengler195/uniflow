"""I1 only: structure and local declared arithmetic, never authenticity or truth."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from uniflow.schemas.evidence_sidecar_v1 import (
    EvidenceSidecarV1, Provenance, TimeClaim, exact_sum,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'evidence_sidecar_i1'


def load(name='f1_bound.json'):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


@pytest.mark.parametrize('name', ['f1_bound.json', 'f2_missing_draft.json',
                                  'f3_multiple_attempts.json', 'f4_separate_dimensions.json'])
def test_t01_synthetic_models_and_json_roundtrip(name):
    model = EvidenceSidecarV1.model_validate(load(name))
    assert model.sidecar_version == 'uniflow-evidence-sidecar-i1.0'
    assert model.data_classification == 'SYNTHETIC'
    assert EvidenceSidecarV1.model_validate_json(model.model_dump_json()) == model
    assert all(a.review_status == 'HUMAN_REVIEW_REQUIRED' for a in model.assessments)


def test_t01_bound_is_declaration_not_external_verification():
    value = load()
    value['result_binding']['result_sha256'] = 'c'*64
    # No file is supplied or read: any syntactically valid hash remains a declaration.
    assert EvidenceSidecarV1.model_validate(value).result_binding.status == 'BOUND'


def test_t02_missing_times_and_pending_are_not_fabricated():
    model = EvidenceSidecarV1.model_validate(load('f2_missing_draft.json'))
    assert model.result_binding.status == 'UNBOUND_DRAFT'
    assert model.result_binding.result_sha256 is None
    for time in (model.tasks[0].issued_time_claim, model.attempts[0].answer_time_claim):
        assert time.time_status == 'MISSING'
        assert time.asserted_at is time.observed_at is time.received_at is None
        assert time.provenance.gaps[0].reason
    assert all(a.awarded_points is None and a.status == 'PENDING'
               for a in model.assessments[0].criterion_awards)


INVALID = load('f5_invalid_cases.json')['cases']


@pytest.mark.parametrize('case', INVALID, ids=[c['case_id'] for c in INVALID])
def test_t03_t04_t12_invalid_fixtures(case):
    value = load(case['base'])
    target = value
    for key in case['path'][:-1]:
        target = target[key]
    key = case['path'][-1]
    if case['operation'] == 'replace':
        target[key] = case['value']
    elif case['operation'] == 'delete':
        del target[key]
    elif case['operation'] == 'duplicate_first':
        target[key].append(deepcopy(target[key][0]))
    elif case['operation'] == 'remove_first':
        target[key].pop(0)
    else:
        pytest.fail('Unknown negative fixture operation')
    with pytest.raises(ValidationError):
        EvidenceSidecarV1.model_validate(value)
    with pytest.raises(ValidationError):
        EvidenceSidecarV1.model_validate_json(json.dumps(value))


def direct_provenance():
    p = deepcopy(load()['attempts'][0]['provenance'])
    p.update(provenance_status='DIRECT_OBSERVATION', observer_id='SYNTHETIC-COLLECTOR',
             observation_method='SYNTHETIC boundary observation declaration')
    p['evidence_refs'][0]['provenance_status'] = 'DIRECT_OBSERVATION'
    return p


@pytest.mark.parametrize('field', ['observer_id', 'observation_method', 'evidence_refs'])
def test_t03_direct_requires_all_support_fields(field):
    p = direct_provenance()
    p[field] = [] if field == 'evidence_refs' else None
    with pytest.raises(ValidationError):
        Provenance.model_validate(p)


def test_direct_declaration_is_representable_without_authenticating_it():
    assert Provenance.model_validate(direct_provenance()).observer_id == 'SYNTHETIC-COLLECTOR'


@pytest.mark.parametrize('status,field', [('RECONSTRUCTED','reconstruction_reason'),
                                         ('RECONSTRUCTED','evidence_refs'), ('MISSING','gaps'),
                                         ('UNVERIFIED','asserted_by')])
def test_provenance_requirements(status,field):
    p = deepcopy(load()['attempts'][0]['provenance'])
    p.update(provenance_status=status, reconstruction_reason='SYNTHETIC reconstruction',
             gaps=[dict(gap_id='G',missing_information='time',reason='not captured')])
    p[field] = [] if field in {'evidence_refs','gaps'} else None
    with pytest.raises(ValidationError):
        Provenance.model_validate(p)


def test_reconstructed_is_not_observed_time():
    time = load()['attempts'][0]['answer_time_claim']
    time['time_status']='RECONSTRUCTED'
    time['provenance'].update(provenance_status='RECONSTRUCTED', reconstruction_reason='Synthetic secondary source')
    assert TimeClaim.model_validate(time).observed_at is None
    time['observed_at']=time['asserted_at']
    with pytest.raises(ValidationError):
        TimeClaim.model_validate(time)


@pytest.mark.parametrize('update', [dict(asserted_at='2026-10-25T02:30:00'),
    dict(interval_start='2026-10-25T02:30:00+02:00'),
    dict(interval_start='2026-10-25T02:30:00+01:00',interval_end='2026-10-25T02:30:00+02:00')])
def test_time_shape_rejects_ambiguity_or_reversed_interval(update):
    time = load()['attempts'][0]['answer_time_claim']
    time.update(update)
    with pytest.raises(ValidationError):
        TimeClaim.model_validate(time)


def test_dst_uncertainty_interval_retains_offsets():
    time=load()['attempts'][0]['answer_time_claim']
    time.update(interval_start='2026-10-25T02:30:00+02:00',interval_end='2026-10-25T02:30:00+01:00')
    assert TimeClaim.model_validate(time).interval_start.endswith('+02:00')


def test_f3_keeps_original_assessment_and_separate_guided_correction():
    model=EvidenceSidecarV1.model_validate(load('f3_multiple_attempts.json'))
    a1,a2=model.attempts
    assert a1.attempt_order == 1 and a2.attempt_order == 2
    assert a1.feedback_after_attempt == a2.assistance_before_attempt.event_refs
    assert a2.correction_attempt.corrects_attempt_id == a1.attempt_id
    assert a2.independence_status == 'GUIDED'
    assert model.assessments[0].criterion_awards[1].awarded_points == '0'
    assert model.assessments[1].criterion_awards[1].awarded_points == '20'
    assert model.assessments[0].attempt_id == 'A1'


def test_f4_task_and_skill_dimensions_are_separate():
    model=EvidenceSidecarV1.model_validate(load('f4_separate_dimensions.json'))
    awards={a.criterion_id: Decimal(a.awarded_points) for a in model.assessments[0].criterion_awards}
    assert awards['TASK-PROCESS']+awards['TASK-FINAL'] == 80
    assert awards['POWER'] == 100
    assert model.assessments[0].qualitative_observations[0].provisional_skill_name == 'SYNTHETIC Algebra'
    assert model.rubrics[0].skill_difficulty_overrides[0].rationale


def test_rubric_and_assessment_revisions_keep_history():
    value=load()
    rubric=deepcopy(value['rubrics'][0])
    rubric.update(revision=2,revision_reason='SYNTHETIC changed policy')
    value['rubrics'].append(rubric)
    assessment=deepcopy(value['assessments'][0])
    assessment.update(assessment_id='ASSESSMENT-A1-R2',revision=2,revision_reason='SYNTHETIC new assessment',
                      supersedes_assessment_id='ASSESSMENT-A1',rubric_revision_ref=dict(rubric_id='R1',revision=2))
    value['assessments'].append(assessment)
    value['attempts'][0]['assessment'].append('ASSESSMENT-A1-R2')
    model=EvidenceSidecarV1.model_validate(value)
    assert [r.revision for r in model.rubrics] == [1,2]
    assert model.assessments[0].rubric_revision_ref.revision == 1


def test_exact_decimal_sums_do_not_use_caller_context():
    value=load()
    rubric=value['rubrics'][0]
    rubric['task_criteria'][0]['max_points']='0.1'
    rubric['task_criteria'][1]['max_points']='0.2'
    rubric['dimension_totals'][0]['max_points']='0.3'
    value['assessments'][0]['criterion_awards'][0]['awarded_points']='0.1'
    value['assessments'][0]['criterion_awards'][1]['awarded_points']='0.2'
    with localcontext() as ctx:
        ctx.prec=2
        assert exact_sum(['0.10000000000000000000000000000000000001','0.2']) == Decimal('0.30000000000000000000000000000000000001')
        EvidenceSidecarV1.model_validate(value)


@pytest.mark.parametrize('path', [ ['result_binding'], ['tasks',0], ['rubrics',0],
    ['rubrics',0,'task_criteria',0], ['assessments',0], ['assessments',0,'criterion_awards',0],
    ['attempts',0], ['attempts',0,'assistance_before_attempt'], ['events',0],
    ['events',0,'source_ref'], ['events',0,'source_ref','selector'], ['events',0,'provenance'],
    ['events',0,'time_claim'], ['source_artifacts',0] ])
def test_t04_unknown_fields_at_each_nested_boundary(path):
    value=load()
    target=value
    for key in path:
        target=target[key]
    target['unknown']=True
    with pytest.raises(ValidationError):
        EvidenceSidecarV1.model_validate(value)


def test_references_are_inert_unresolved_data_in_i1():
    value=load()
    value['events'][0]['source_ref']['artifact_id']='https://invalid.example/../source'
    value['events'][0]['parent_event_ids']=['UNRESOLVED-EVENT']
    value['attempts'][0]['answer_evidence'][0]['quote']="__import__('os').system('never execute')"
    assert EvidenceSidecarV1.model_validate(value).events[0].parent_event_ids == ['UNRESOLVED-EVENT']


def test_json_schema_is_exportable_and_strict():
    schema=EvidenceSidecarV1.model_json_schema()
    assert schema['additionalProperties'] is False
    assert all(d.get('additionalProperties') is False for d in schema['$defs'].values() if d.get('type')=='object')


def test_duplicate_assessment_revision_for_same_attempt_is_ambiguous():
    value=load()
    another=deepcopy(value['assessments'][0])
    another['assessment_id']='DISTINCT-ID-SAME-REVISION'
    value['assessments'].append(another)
    with pytest.raises(ValidationError):
        EvidenceSidecarV1.model_validate(value)


def test_scoped_independence_requires_explicit_direct_capture_declaration():
    value=load()
    attempt=value['attempts'][0]
    attempt.update(independence_status='VERIFIED_WITHIN_CAPTURE_SCOPE',
                   provenance_status='DIRECT_OBSERVATION',provenance=direct_provenance())
    attempt['assistance_before_attempt'].update(coverage='COMPLETE_IN_CAPTURED_CHANNEL',
        scope='SYNTHETIC collector channel only',classification='NONE_OBSERVED',availability='NOT_OBSERVED')
    model=EvidenceSidecarV1.model_validate(value)
    assert model.attempts[0].independence_status == 'VERIFIED_WITHIN_CAPTURE_SCOPE'
    assert model.attempts[0].provenance.observer_id


@pytest.mark.parametrize('field,value', [('result_sha256','a'*64),('contract_version','uniflow-scoring-v1'),('reason',None)])
def test_draft_binding_rejects_fabricated_result_or_missing_reason(field,value):
    payload=load('f2_missing_draft.json')
    payload['result_binding'][field]=value
    with pytest.raises(ValidationError):
        EvidenceSidecarV1.model_validate(payload)


@pytest.mark.parametrize('field,value', [('asserted_at','2026-10-25T12:00:00+01:00'),('provenance',None)])
def test_missing_time_requires_null_values_and_explicit_provenance(field,value):
    time=load('f2_missing_draft.json')['tasks'][0]['issued_time_claim']
    time[field]=value
    with pytest.raises(ValidationError):
        TimeClaim.model_validate(time)


def test_partial_pending_award_retains_other_recorded_values():
    value=load()
    value['assessments'][0]['criterion_awards'][1].update(awarded_points=None,status='PENDING')
    model=EvidenceSidecarV1.model_validate(value)
    assert model.assessments[0].criterion_awards[0].awarded_points == '80'
    assert model.assessments[0].criterion_awards[1].awarded_points is None
