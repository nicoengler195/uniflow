"""Independent SYNTHETIC claim/link oracles; no CAS execution or production DB."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import sys

import pytest

from uniflow.application.review_projection_i3 import project_review_bytes, project_legacy_scoring_bytes
from uniflow.application import math_review_overlay_i5 as api
from uniflow.application.math_review_report_i5 import MathSubmission, OperandLink

FIX = Path(__file__).parent / 'fixtures' / 'review_projection_i3'
SIDECAR = (FIX / 'sidecar.json').read_bytes()
HANDOFF = (FIX / 'handoff.json').read_bytes()
RESULT = (FIX / 'result.json').read_bytes()
REPORT = (Path(__file__).parent / 'fixtures' / 'math_review_i5' / 'synthetic_report.json').read_bytes()
LEFT, RIGHT = b'(x+2)^2', b'x^2+4*x+4'
SOURCE = LEFT + b'\n' + RIGHT + b'\n'
sha = lambda value: hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def context(source=SOURCE):
    side = json.loads(SIDECAR)
    def rewrite(value):
        if type(value) is dict:
            if value.get('artifact_id') == 'SYNTHETIC-SOURCE':
                value['sha256'] = sha(source)
                if 'byte_length' in value:
                    value['byte_length'] = len(source)
                if 'selector' in value:
                    value['selector'] = dict(kind='UTF8_BYTE_RANGE', pointer=None, start=0, end=len(source))
                    value['quote'] = source.decode()
            for v in value.values():
                rewrite(v)
        elif type(value) is list:
            for v in value:
                rewrite(v)
    rewrite(side)
    artifacts = {'handoff': HANDOFF, 'result': RESULT, 'SYNTHETIC-SOURCE': source}
    p = project_review_bytes(encoded(side), artifacts=artifacts, handoff_artifact_id='handoff',
                             result_artifact_id='result', handoff_revision=1,
                             score_assessment_ids={'TASK-1': 'ASSESSMENT-A1'})
    return p, artifacts


def submission(p, raw=REPORT, **updates):
    d = json.loads(raw)
    task = p.tasks[0]
    attempt = task.attempts[0]
    s = MathSubmission('R1', raw, sha(raw), api.projection_identity(p), p.input_context.context_sha256,
                       d['context_id'], d['context_revision'], d['context_sha256'], task.task_id,
                       attempt.attempt_id, 'ASSESSMENT-A1',
                       (OperandLink('left', task.statement_evidence_index, 0, len(LEFT), sha(LEFT)),
                        OperandLink('right', attempt.answer_evidence_indices[0], len(LEFT) + 1,
                                    len(LEFT) + 1 + len(RIGHT), sha(RIGHT))), True)
    return replace(s, **updates)


def run(p=None, a=None, s=None):
    if p is None:
        p, a = context()
    if s is None:
        s = submission(p)
    return api.project_math_review(p, (s,), artifacts=a)


def changed_report(**updates):
    data = json.loads(REPORT)
    data.update(updates)
    return encoded(data)


def test_i5_p01_exact_binding_and_four_separate_axes():
    p, a = context()
    out = run(p, a)
    assert out.complete and len(out.entries) == 1
    entry = out.entries[0]
    assert entry.association == 'BOUND_BYTES', entry.diagnostics
    assert entry.associated_task_id == 'TASK-1' and entry.associated_attempt_id == 'A1'
    assert entry.claimed_math_status == 'VERIFIED_EQUIVALENT'
    assert entry.execution_origin == 'UNVERIFIED' and not entry.independent_execution_proven
    assert entry.human_review_required and entry.score_effect == 'NONE'
    assert entry.checked_spans[0].text == LEFT.decode()
    assert entry.checked_spans[1].text == RIGHT.decode()
    assert entry.checked_spans[1].original_byte_start == len(LEFT) + 1
    assert json.loads(entry.claimed_normal_forms_json)['left_normal']['coefficients'] == [
        {'power': 0, 'numerator': '4', 'denominator': '1'},
        {'power': 1, 'numerator': '4', 'denominator': '1'},
        {'power': 2, 'numerator': '1', 'denominator': '1'}]


def test_i5_p02_original_scores_and_findings_preserved_exactly():
    p, a = context()
    before = p.model_dump_json()
    out = run(p, a)
    assert p.model_dump_json() == before
    assert json.loads(out.original_i3_json) == p.model_dump(mode='json')
    assert [f['finding']['status'] for f in json.loads(out.original_i3_json)['findings']] == [f.finding.status for f in p.findings]
    awards = p.tasks[0].attempts[0].assessments[0].awards
    assert {(v.dimension, v.awarded_points) for v in awards} == {('TASK', '80'), ('TASK', '0'), ('SKILL', '100')}
    assert json.loads(p.tasks[0].v1_task.payload_json)['fraction'] == 0.8
    assert any(q for q in out.review_questions if 'whole task' in q)


def test_i5_p03_non_equivalence_is_untrusted_claim_not_score():
    # Independent genuine nonidentity: only the constant differs by one.
    wrong_answer = b'x^2+4*x+5'
    p, a = context(LEFT + b'\n' + wrong_answer + b'\n')
    d = json.loads(REPORT)
    d['right_normal']['coefficients'][0]['numerator'] = '5'
    d.update(status='VERIFIED_NOT_EQUIVALENT', code='DIFFERENT_COEFFICIENTS')
    d['inputs'][1]['sha256'] = sha(wrong_answer)
    d['context_sha256'] = sha(json.dumps([d['operation'], d['context_id'], '1', 'R', 'I4-ISOLATED-1',
        [(v['role'], v['sha256'], v['byte_length']) for v in d['inputs']]], separators=(',', ':')).encode())
    s = submission(p, encoded(d))
    s = replace(s, operands=(s.operands[0], replace(s.operands[1], sha256=sha(wrong_answer))))
    out = run(p, a, s)
    assert out.entries[0].association == 'BOUND_BYTES'
    assert out.entries[0].claimed_math_status == 'VERIFIED_NOT_EQUIVALENT'
    assert not out.entries[0].independent_execution_proven
    assert out.entries[0].score_effect == 'NONE'
    assert json.loads(out.original_i3_json) == p.model_dump(mode='json')


@pytest.mark.parametrize('field,value,code', [
    ('task_id', 'OTHER', 'TASK_MISSING_OR_AMBIGUOUS'),
    ('task_id', None, 'TASK_MISSING_OR_AMBIGUOUS'),
    ('attempt_id', 'OTHER', 'ATTEMPT_MISSING_OR_AMBIGUOUS'),
    ('attempt_id', None, 'ATTEMPT_MISSING_OR_AMBIGUOUS'),
    ('assessment_id', 'OTHER', 'ASSESSMENT_MISSING_OR_AMBIGUOUS'),
    ('assessment_id', 'ASSESSMENT-A2', 'ASSESSMENT_NOT_SELECTED'),
    ('projection_sha256', '0' * 64, 'I3_CONTEXT_CONFLICT'),
    ('i3_context_sha256', '0' * 64, 'I3_CONTEXT_CONFLICT'),
    ('i4_context_sha256', '0' * 64, 'DECLARED_I4_CONTEXT_CONFLICT'),
    ('i4_context_revision', '2', 'DECLARED_I4_CONTEXT_CONFLICT'),
    ('i4_context_id', 'OTHER', 'DECLARED_I4_CONTEXT_CONFLICT'),
    ('report_sha256', '0' * 64, 'REPORT_HASH_CONFLICT'),
    ('synthetic_test_report', False, 'SYNTHETIC_DECLARATION_REQUIRED'),
])
def test_i5_p04_bad_declared_reference(field, value, code):
    p, a = context()
    out = run(p, a, replace(submission(p), **{field: value}))
    assert out.entries[0].association == 'UNASSIGNED'
    assert code in out.entries[0].diagnostics
    assert out.entries[0].associated_task_id is None


def test_i5_p05_optional_assessment_not_automatically_chosen():
    p, a = context()
    out = run(p, a, submission(p, assessment_id=None))
    assert out.entries[0].association == 'BOUND_BYTES'
    assert out.entries[0].associated_assessment_id is None


@pytest.mark.parametrize('which', ['task', 'attempt', 'assessment'])
def test_i5_p06_duplicate_i3_identity_never_last_wins(which):
    p, a = context()
    t = p.tasks[0]
    if which == 'task':
        p = p.model_copy(update={'tasks': (t, t)})
    elif which == 'attempt':
        p = p.model_copy(update={'tasks': (t.model_copy(update={'attempts': t.attempts + (t.attempts[0],)}),)})
    else:
        at = t.attempts[0]
        at = at.model_copy(update={'assessments': at.assessments + at.assessments})
        p = p.model_copy(update={'tasks': (t.model_copy(update={'attempts': (at,) + t.attempts[1:]}),)})
    out = run(p, a)
    assert out.entries[0].association == 'UNASSIGNED'
    assert any('AMBIGUOUS' in code for code in out.entries[0].diagnostics)


def test_i5_p07_input_span_hash_conflict():
    p, a = context()
    s = submission(p)
    s = replace(s, operands=(s.operands[0], replace(s.operands[1], sha256='0' * 64)))
    out = run(p, a, s)
    assert 'INPUT_HASH_CONFLICT' in out.entries[0].diagnostics
    assert out.entries[0].association == 'UNASSIGNED'


def test_i5_p08_source_hash_conflict():
    p, a = context()
    a['SYNTHETIC-SOURCE'] = SOURCE.replace(b'4', b'5')
    assert 'SOURCE_HASH_CONFLICT' in run(p, a).entries[0].diagnostics


def test_i5_p09_wrong_evidence_membership():
    p, a = context()
    s = submission(p)
    wrong = p.tasks[0].statement_evidence_index
    out = run(p, a, replace(s, operands=(s.operands[0], replace(s.operands[1], evidence_index=wrong))))
    assert 'ANSWER_EVIDENCE_MEMBERSHIP' in out.entries[0].diagnostics


@pytest.mark.parametrize('kind', ['duplicate', 'competing', 'duplicate_id'])
def test_i5_p10_multiple_reports_not_selected(kind):
    p, a = context()
    first = submission(p)
    if kind == 'competing':
        normal = json.loads(REPORT)['right_normal']
        normal['coefficients'][0]['numerator'] = '5'
        second = submission(p, changed_report(status='VERIFIED_NOT_EQUIVALENT', right_normal=normal), report_id='R2')
    else:
        second = replace(first, report_id='R2' if kind == 'duplicate' else 'R1')
    out = api.project_math_review(p, (first, second), artifacts=a)
    assert len(out.entries) == 2 and all(v.association == 'UNASSIGNED' for v in out.entries)
    code = 'CONFLICTING_REPORTS' if kind == 'competing' else 'MULTIPLE_REPORTS'
    assert all(code in v.diagnostics for v in out.entries)


@pytest.mark.parametrize('status', ['UNKNOWN', 'UNSUPPORTED', 'ERROR'])
def test_i5_p11_i4_abstention_is_not_wrong_answer(status):
    p, a = context()
    raw = changed_report(status=status, code='SYNTHETIC_ABSTENTION', left_normal=None, right_normal=None, dependencies=[])
    out = run(p, a, submission(p, raw))
    entry = out.entries[0]
    assert entry.claimed_math_status == status and entry.association == 'BOUND_BYTES'
    assert 'I4_' + status in entry.diagnostics and entry.score_effect == 'NONE'


@pytest.mark.parametrize('field,value,code', [
    ('contract_version', 'FUTURE', 'REPORT_VERSION'),
    ('validator_version', 'FUTURE', 'REPORT_VERSION'),
    ('grammar_version', 'FUTURE', 'REPORT_VERSION'),
    ('domain', '', 'DOMAIN_MISSING_OR_UNSUPPORTED'),
    ('domain', 'C', 'DOMAIN_MISSING_OR_UNSUPPORTED'),
    ('human_review_required', False, 'REPORT_SAFETY_FLAGS'),
    ('production_admitted', True, 'REPORT_SAFETY_FLAGS'),
    ('s09_status', 'VERIFIED', 'REPORT_SAFETY_FLAGS'),
    ('context_sha256', '0' * 64, 'REPORT_CONTEXT_HASH'),
    ('method', 'NUMERIC_SAMPLING', 'REPORT_SCOPE'),
])
def test_i5_p12_report_version_domain_and_flags(field, value, code):
    p, a = context()
    raw = changed_report(**{field: value})
    out = run(p, a, submission(p, raw))
    assert out.entries[0].association == 'UNASSIGNED'
    assert code in out.entries[0].diagnostics
    assert bytes.fromhex(out.entries[0].raw_report_hex) == raw


def test_i5_p13_forged_external_equivalence_never_authenticates():
    p, a = context()
    s = submission(p, changed_report(reason='Attacker claims trusted execution'), declared_origin='VERIFIED_BY_CALLER')
    out = run(p, a, s)
    e = out.entries[0]
    assert e.association == 'BOUND_BYTES'  # bytes really match; origin still does not
    assert e.execution_origin == 'UNVERIFIED' and not e.independent_execution_proven
    assert e.declared_origin == 'VERIFIED_BY_CALLER' and e.human_review_required
    assert 'EXECUTION_ORIGIN_UNVERIFIED' in e.diagnostics


@pytest.mark.parametrize('mode', ['LEGACY_V1', 'DIAGNOSTIC_ONLY'])
def test_i5_p14_legacy_diagnostic_separate(mode):
    original, a = context()
    p = project_legacy_scoring_bytes(RESULT, handoff_bytes=HANDOFF) if mode == 'LEGACY_V1' else project_review_bytes(b'{}', artifacts={})
    assert p.mode == mode
    s = replace(submission(original), projection_sha256=api.projection_identity(p), i3_context_sha256=p.input_context.context_sha256)
    out = run(p, a, s)
    assert out.i3_mode == mode and out.entries[0].association == 'UNASSIGNED'
    assert 'I3_MODE_NOT_ASSOCIABLE' in out.entries[0].diagnostics
    assert json.loads(out.original_i3_json) == p.model_dump(mode='json')


def test_i5_p15_no_reports_keeps_whole_original():
    p, a = context()
    out = api.project_math_review(p, artifacts=a)
    assert out.status == 'NO_REPORTS' and out.complete and out.entries == ()
    assert json.loads(out.original_i3_json) == p.model_dump(mode='json')


def test_i5_p16_no_validator_sympy_or_io(monkeypatch):
    import builtins
    p, a = context()
    s = submission(p)
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not any(word in name for word in ('sympy', 'mpmath', 'polynomial_validator_i4', 'polynomial_parser_i4', 'sqlalchemy'))
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('No I/O permitted')
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    assert run(p, a, s).entries[0].association == 'BOUND_BYTES'
    assert 'sympy' not in sys.modules and 'mpmath' not in sys.modules


def test_i5_p17_immutable_deterministic_json():
    p, a = context()
    s = submission(p, declared_origin='<script>&data</script>')
    before = (p.model_dump_json(), s.report_bytes, dict(a))
    one, two = run(p, a, s), run(p, a, s)
    assert one.to_json() == two.to_json()
    assert '<script>' not in one.to_json() and '\\u003c' in one.to_json()
    assert json.loads(one.to_json())['entries'][0]['declared_origin'] == s.declared_origin
    with pytest.raises(FrozenInstanceError):
        one.status = 'APPROVED'
    with pytest.raises(FrozenInstanceError):
        one.entries[0].association = 'APPROVED'
    copied = one.to_dict()
    copied['entries'][0]['claimed_math_status'] = 'ERROR'
    assert one.entries[0].claimed_math_status == 'VERIFIED_EQUIVALENT'
    assert (p.model_dump_json(), s.report_bytes, a) == before


@pytest.mark.parametrize('kind', ['count', 'report_bytes', 'artifact_bytes', 'output', 'projection'])
def test_i5_p18_budget_aborts_whole_overlay(monkeypatch, kind):
    p, a = context()
    s = submission(p)
    submissions = (s,)
    if kind == 'count':
        submissions *= api.MAX_SUBMISSIONS + 1
    elif kind == 'report_bytes':
        submissions += (replace(s, report_bytes=b'x' * (api.MAX_REPORT_BYTES + 1)),)
    elif kind == 'artifact_bytes':
        a['SYNTHETIC-SOURCE'] = b'x' * (api.MAX_ARTIFACT_BYTES + 1)
    elif kind == 'output':
        monkeypatch.setattr(api, 'MAX_OUTPUT_BYTES', 4096)
    else:
        p = p.model_copy(update={'not_checked': ('x' * (api.MAX_TEXT + 1),)})
    out = api.project_math_review(p, submissions, artifacts=a)
    assert not out.complete and out.entries == () and out.original_i3_json is None
    assert out.diagnostics == ('RESOURCE_LIMIT',)


@pytest.mark.parametrize('raw', [b'{', b'\xff', b'{"status":NaN}', b'{"status":"x","status":"y"}'])
def test_i5_p19_malformed_report_preserved_detached(raw):
    p, a = context()
    s = replace(submission(p), report_bytes=raw, report_sha256=sha(raw))
    out = run(p, a, s)
    assert out.complete and out.entries[0].association == 'UNASSIGNED'
    assert bytes.fromhex(out.entries[0].raw_report_hex) == raw


def test_i5_p20_arbitrary_report_object_not_serialized():
    p, a = context()
    class Trap:
        def to_json(self):
            raise AssertionError('must not call')
    out = run(p, a, replace(submission(p), report_bytes=Trap()))
    assert not out.complete and out.entries == ()


def test_i5_p21_i3_conflicts_never_upgraded():
    p, a = context()
    index = p.tasks[0].finding_indices[0]
    original = p.findings[index]
    changed = original.model_copy(update={'finding': original.finding.model_copy(update={'status': 'CONFLICT'})})
    p = p.model_copy(update={'findings': p.findings[:index] + (changed,) + p.findings[index + 1:]})
    out = run(p, a)
    assert out.entries[0].association == 'UNASSIGNED'
    assert 'I3_CONFLICT_REMAINS' in out.entries[0].diagnostics
    assert json.loads(out.original_i3_json)['findings'][index]['finding']['status'] == 'CONFLICT'


def test_i5_p22_missing_source_is_not_searched():
    p, a = context()
    del a['SYNTHETIC-SOURCE']
    assert 'SOURCE_MISSING_OR_AMBIGUOUS' in run(p, a).entries[0].diagnostics


def test_i5_p23_wrong_attempt_parent():
    p, a = context()
    t = p.tasks[0]
    attempt = t.attempts[0].model_copy(update={'task_id': 'OTHER'})
    p = p.model_copy(update={'tasks': (t.model_copy(update={'attempts': (attempt,) + t.attempts[1:]}),)})
    out = run(p, a)
    assert 'ATTEMPT_TASK_CONFLICT' in out.entries[0].diagnostics


def test_i5_p24_context_revision_cannot_hide_conflicting_claims():
    p, a = context()
    first = submission(p)
    d = json.loads(REPORT)
    d['context_revision'] = '2'
    d['status'] = 'VERIFIED_NOT_EQUIVALENT'
    d['right_normal']['coefficients'][0]['numerator'] = '5'
    d['context_sha256'] = sha(json.dumps([d['operation'], d['context_id'], '2', 'R', 'I4-ISOLATED-1',
        [(v['role'], v['sha256'], v['byte_length']) for v in d['inputs']]], separators=(',', ':')).encode())
    second = submission(p, encoded(d), report_id='R2')
    out = api.project_math_review(p, (first, second), artifacts=a)
    assert all(v.association == 'UNASSIGNED' and 'CONFLICTING_REPORTS' in v.diagnostics for v in out.entries)


def test_i5_p25_abstention_and_claim_are_competing_not_logical_opposites():
    p, a = context()
    second = submission(p, changed_report(status='UNKNOWN', left_normal=None, right_normal=None, dependencies=[]), report_id='R2')
    out = api.project_math_review(p, (submission(p), second), artifacts=a)
    assert all('MULTIPLE_REPORTS' in v.diagnostics for v in out.entries)


@pytest.mark.parametrize('what', ['normal', 'input', 'resource', 'dependency', 'extra'])
def test_i5_p26_manipulated_report_contract(what):
    p, a = context()
    d = json.loads(REPORT)
    if what == 'normal':
        d['left_normal']['coefficients'][0]['numerator'] = '5'
    elif what == 'input':
        d['inputs'][1]['byte_length'] = True
    elif what == 'resource':
        d['resources']['tokens'] = 999999
    elif what == 'dependency':
        d['dependencies'] = [['sympy', 'NEXT']]
    else:
        d['score'] = 100
    out = run(p, a, submission(p, encoded(d)))
    assert out.complete and out.entries[0].association == 'UNASSIGNED'


@pytest.mark.parametrize('what', ['quote', 'locator', 'selection', 'span'])
def test_i5_p27_original_evidence_contradictions(what):
    p, a = context()
    s = submission(p)
    index = s.operands[1].evidence_index
    ev = p.evidence[index]
    if what == 'quote':
        ev = ev.model_copy(update={'selected_text': 'not the original bytes'})
    elif what == 'locator':
        ev = ev.model_copy(update={'ref_source': ev.ref_source.model_copy(update={'pointer': '/attempts/999/answer_evidence/0'})})
    elif what == 'selection':
        ev = ev.model_copy(update={'selection_finding_indices': ()})
    else:
        s = replace(s, operands=(s.operands[0], replace(s.operands[1], end=99999)))
    p = p.model_copy(update={'evidence': p.evidence[:index] + (ev,) + p.evidence[index + 1:]})
    s = replace(s, projection_sha256=api.projection_identity(p))
    assert run(p, a, s).entries[0].association == 'UNASSIGNED'


def test_i5_p28_resource_abort_has_no_partial_entries():
    p, a = context()
    bad = b'[' * 49 + b']' * 49
    second = replace(submission(p), report_id='R2', report_bytes=bad, report_sha256=sha(bad))
    out = api.project_math_review(p, (submission(p), second), artifacts=a)
    assert not out.complete and out.entries == () and out.diagnostics == ('RESOURCE_LIMIT',)


def test_i5_p29_malformed_i3_object_has_no_arbitrary_serialization():
    p, a = context()
    class Trap:
        def __str__(self):
            raise AssertionError('not allowed')
        def model_dump(self):
            raise AssertionError('not allowed')
    broken = p.model_copy(update={'not_checked': (Trap(),)})
    out = api.project_math_review(broken, (), artifacts=a)
    assert not out.complete and out.entries == ()


@pytest.mark.parametrize('status', ['UNKNOWN', 'UNVERIFIED'])
def test_i5_p30_i3_nonfinal_status_preserved(status):
    p, a = context()
    related = p.tasks[0].finding_indices
    index = next(i for i in related if p.findings[i].finding.code != 'REF_RESOLVED')
    finding = p.findings[index]
    changed = finding.model_copy(update={'finding': finding.finding.model_copy(update={'status': status})})
    p = p.model_copy(update={'findings': p.findings[:index] + (changed,) + p.findings[index + 1:]})
    out = run(p, a)
    assert json.loads(out.original_i3_json)['findings'][index]['finding']['status'] == status
    assert out.entries[0].execution_origin == 'UNVERIFIED'
    assert out.entries[0].association == 'BOUND_BYTES'
    assert status in out.entries[0].i3_finding_statuses


def test_i5_p31_non_synthetic_context_is_not_promoted():
    p, a = context()
    snapshot = p.session.sidecar_declaration
    data = json.loads(snapshot.payload_json)
    data['data_classification'] = 'REAL_WORLD'
    p = p.model_copy(update={'session': p.session.model_copy(update={'sidecar_declaration': snapshot.model_copy(update={'payload_json': json.dumps(data)})})})
    out = run(p, a)
    assert out.entries[0].association == 'UNASSIGNED'
    assert 'SYNTHETIC_CONTEXT_REQUIRED' in out.entries[0].diagnostics


@pytest.mark.parametrize('operation', ['DERIVATIVE_COMPARE', 'COMPOSE_COMPARE'])
def test_i5_p32_operation_and_all_operands_preserved(operation):
    d = json.loads(REPORT)
    if operation == 'DERIVATIVE_COMPARE':
        right = b'2*x+4'
        source = LEFT + b'\n' + right + b'\n'
        right_start = len(LEFT) + 1
        expected = [(0, '4'), (1, '2')]
        operands = [('left', LEFT), ('right', right)]
    else:
        right, inner = b'(x+3)^2', b'x+1'
        source = LEFT + b'\n' + inner + b'\n' + right + b'\n'
        right_start = len(LEFT) + len(inner) + 2
        expected = [(0, '9'), (1, '6'), (2, '1')]
        operands = [('left', LEFT), ('right', right), ('inner', inner)]
    d['operation'] = operation
    d['inputs'] = [dict(role=role, sha256=sha(raw), byte_length=len(raw)) for role, raw in operands]
    d['context_sha256'] = sha(json.dumps([operation, d['context_id'], '1', 'R', 'I4-ISOLATED-1',
        [(v['role'], v['sha256'], v['byte_length']) for v in d['inputs']]], separators=(',', ':')).encode())
    d['left_normal'] = d['right_normal'] = {'coefficients': [dict(power=i, numerator=n, denominator='1') for i, n in expected]}
    p, a = context(source)
    s = submission(p, encoded(d))
    links = (s.operands[0], replace(s.operands[1], start=right_start, end=right_start + len(right), sha256=sha(right)))
    if operation == 'COMPOSE_COMPARE':
        links += (OperandLink('inner', p.tasks[0].statement_evidence_index, len(LEFT) + 1, len(LEFT) + 1 + len(inner), sha(inner)),)
    out = run(p, a, replace(s, operands=links))
    assert out.entries[0].association == 'BOUND_BYTES', out.entries[0].diagnostics
    assert out.entries[0].claimed_operation == operation
    assert [v.role for v in out.entries[0].checked_spans] == [v[0] for v in operands]


def test_i5_p33_invalid_unicode_origin_is_diagnostic():
    p, a = context()
    out = run(p, a, submission(p, declared_origin='\ud800'))
    assert not out.complete and out.entries == ()


def test_i5_p34_missing_domain_does_not_infer_R():
    p, a = context()
    d = json.loads(REPORT)
    del d['domain']
    out = run(p, a, submission(p, encoded(d)))
    assert out.entries[0].claimed_domain is None and out.entries[0].association == 'UNASSIGNED'
    assert 'DOMAIN_MISSING_OR_UNSUPPORTED' in out.entries[0].diagnostics


def test_i5_p35_forged_navigation_task_not_in_original_root():
    p, a = context()
    task = p.tasks[0]
    payload = json.loads(task.declaration.payload_json)
    payload['task_id'] = 'FORGED'
    fake = task.model_copy(update={'task_id': 'FORGED', 'declaration': task.declaration.model_copy(update={'payload_json': json.dumps(payload)})})
    p = p.model_copy(update={'tasks': (fake,)})
    s = submission(p, assessment_id=None)
    # Let the attempted navigation join appear internally consistent while the
    # retained sidecar root still contains TASK-1. It must not be attached.
    attempt = fake.attempts[0]
    attempt_payload = json.loads(attempt.declaration.payload_json)
    attempt_payload['task_id'] = 'FORGED'
    altered = attempt.model_copy(update={'task_id': 'FORGED', 'declaration': attempt.declaration.model_copy(update={'payload_json': json.dumps(attempt_payload)})})
    fake = fake.model_copy(update={'attempts': (altered,) + fake.attempts[1:]})
    p = p.model_copy(update={'tasks': (fake,)})
    s = replace(s, projection_sha256=api.projection_identity(p))
    out = run(p, a, s)
    assert out.entries[0].association == 'UNASSIGNED'
    assert 'I3_DECLARATION_CONFLICT' in out.entries[0].diagnostics


def test_i5_p36_conflicting_i3_source_identity_is_not_ignored():
    p, a = context()
    index = next(i for i, v in enumerate(p.source_artifacts) if v.artifact_id == 'SYNTHETIC-SOURCE')
    view = p.source_artifacts[index]
    view = view.model_copy(update={'actual': view.actual.model_copy(update={'sha256': '0' * 64})})
    p = p.model_copy(update={'source_artifacts': p.source_artifacts[:index] + (view,) + p.source_artifacts[index + 1:]})
    out = run(p, a)
    assert out.entries[0].association == 'UNASSIGNED'
    assert 'I3_SOURCE_IDENTITY_CONFLICT' in out.entries[0].diagnostics
