"""Bounded additive projection of pre-existing synthetic I4 report claims.

No import of the I4 parser/validator, SymPy, scoring services or persistence.
This module never authenticates a report's execution origin.
"""
from dataclasses import asdict, fields, replace
import hashlib
import json
import math
import re

from pydantic import BaseModel, ValidationError
from uniflow.schemas import review_projection_i3 as i3
from uniflow.schemas.evidence_validation_i2 import Finding
from uniflow.schemas.evidence_sidecar_v1 import EvidenceRef
from uniflow.application.evidence_artifacts_i2 import load_json_bytes, select, ArtifactError
from uniflow.application.polynomial_report_i4 import PolynomialReport, LIMITS
from uniflow.application.math_review_report_i5 import (
    OperandLink, MathSubmission, CheckedSpan, MathReviewEntry, MathReviewOverlay,
)

MAX_SUBMISSIONS = 16
MAX_REPORT_BYTES = 65536
MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_PROJECTION_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_NODES = 100000
MAX_TEXT = 262144
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
STATES = frozenset(('VERIFIED_EQUIVALENT', 'VERIFIED_NOT_EQUIVALENT', 'UNKNOWN', 'UNSUPPORTED', 'ERROR'))
OPS = frozenset(('COMPARE', 'DERIVATIVE_COMPARE', 'COMPOSE_COMPARE'))
MODELS = frozenset(v for v in vars(i3).values() if isinstance(v, type) and issubclass(v, BaseModel)) | {Finding}
REPORT_FIELDS = frozenset(f.name for f in fields(PolynomialReport))


class _Reject(Exception):
    def __init__(self, code):
        self.code = code


def _require(ok, code):
    if not ok:
        raise _Reject(code)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _id(value):
    return type(value) is str and len(value) <= 120 and ID.fullmatch(value) is not None


def _digest(value):
    return type(value) is str and len(value) == 64 and SHA.fullmatch(value) is not None


def _capture_projection(projection):
    _require(type(projection) is i3.ReviewProjectionV1, 'I3_TYPE')
    count = charge = 0
    def walk(value, depth=0):
        nonlocal count, charge
        count += 1
        charge += 16
        _require(depth <= 48 and count <= MAX_NODES and charge <= MAX_PROJECTION_BYTES, 'RESOURCE_LIMIT')
        kind = type(value)
        if any(kind is model for model in MODELS):
            data = object.__getattribute__(value, '__dict__')
            _require(type(data) is dict and all(type(k) is str for k in data)
                     and set(data) == set(kind.model_fields), 'I3_INVALID_OBJECT')
            return walk(data, depth + 1)
        if value is None or kind is bool:
            return value
        if kind is int:
            _require(value.bit_length() <= 128, 'I3_INVALID_OBJECT')
            return value
        if kind is str:
            _require(len(value) <= MAX_TEXT, 'RESOURCE_LIMIT')
            charge += 6 * len(value)
            _require(charge <= MAX_PROJECTION_BYTES, 'RESOURCE_LIMIT')
            return value
        if kind is tuple or kind is list:
            _require(len(value) <= MAX_NODES, 'RESOURCE_LIMIT')
            return [walk(v, depth + 1) for v in value]
        if kind is dict:
            _require(len(value) <= MAX_NODES and all(type(k) is str for k in value), 'I3_INVALID_OBJECT')
            return {walk(k, depth + 1): walk(v, depth + 1) for k, v in value.items()}
        raise _Reject('I3_INVALID_OBJECT')
    raw = _json(walk(projection))
    _require(len(raw) <= MAX_PROJECTION_BYTES, 'RESOURCE_LIMIT')
    # Validate an owned reconstruction; do not trust unchecked model_copy/construct.
    owned = i3.ReviewProjectionV1.model_validate_json(raw)
    return owned, raw, _sha(raw.encode('ascii'))


def projection_identity(projection):
    """Bounded canonical identity used in an explicit caller mapping."""
    return _capture_projection(projection)[2]


def _normal(value):
    _require(type(value) is dict and set(value) == {'coefficients'} and type(value['coefficients']) is list, 'REPORT_SCHEMA')
    _require(len(value['coefficients']) <= 65, 'REPORT_SCHEMA')
    previous = -1
    bits = 0
    for c in value['coefficients']:
        _require(type(c) is dict and set(c) == {'power', 'numerator', 'denominator'}, 'REPORT_SCHEMA')
        power, n, d = c['power'], c['numerator'], c['denominator']
        _require(type(power) is int and previous < power <= 64, 'REPORT_SCHEMA')
        _require(type(n) is str and type(d) is str and 0 < len(n) <= 618 and 0 < len(d) <= 617, 'REPORT_SCHEMA')
        _require(re.fullmatch(r'-?[1-9][0-9]*', n) is not None and re.fullmatch(r'[1-9][0-9]*', d) is not None, 'REPORT_SCHEMA')
        ni, di = int(n), int(d)
        bits += abs(ni).bit_length() + di.bit_length()
        _require(abs(ni).bit_length() <= 2048 and di.bit_length() <= 2048 and bits <= 16384 and math.gcd(ni, di) == 1, 'REPORT_SCHEMA')
        previous = power


def _report(data):
    _require(type(data) is dict, 'REPORT_SCHEMA')
    _require(data.get('domain') == 'R', 'DOMAIN_MISSING_OR_UNSUPPORTED')
    _require(data.get('contract_version') == 'I4-POLYNOMIAL-REPORT-1'
             and data.get('validator_version') == 'I4-ISOLATED-1'
             and data.get('grammar_version') == 'I4-POLY-ASCII-1', 'REPORT_VERSION')
    _require(set(data) == REPORT_FIELDS, 'REPORT_SCHEMA')
    _require(data['human_review_required'] is True and data['production_admitted'] is False
             and data['s09_status'] == 'NOT_VERIFIED', 'REPORT_SAFETY_FLAGS')
    _require(data['status'] in STATES and data['operation'] in OPS and data['variable'] == 'x'
             and data['mathematical_scope'] == 'QQ[x]; polynomial functions R -> R'
             and data['method'] == 'EXACT_QQ_CANONICAL_COEFFICIENTS_V1', 'REPORT_SCOPE')
    _require(all(type(data[k]) is str and 0 < len(data[k]) <= 4096 for k in ('code', 'reason', 'context_id', 'context_revision')), 'REPORT_SCHEMA')
    _require(_digest(data['context_sha256']), 'REPORT_SCHEMA')
    _require(type(data['limitations']) is list and 0 < len(data['limitations']) <= 16 and all(type(s) is str and len(s) <= 4096 for s in data['limitations']), 'REPORT_SCHEMA')
    _require(data['limits'] == asdict(LIMITS), 'REPORT_BUDGET_PROFILE')
    resources = data['resources']
    expected = {'tokens', 'nodes', 'operators', 'arithmetic_work', 'instructions', 'parsing_complete', 'planning_complete'}
    _require(type(resources) is dict and set(resources) == expected, 'REPORT_SCHEMA')
    for key in ('tokens', 'nodes', 'operators', 'arithmetic_work', 'instructions'):
        bound = {'arithmetic_work': LIMITS.arithmetic_work, 'instructions': LIMITS.instructions}.get(key, getattr(LIMITS, key))
        _require(type(resources[key]) is int and 0 <= resources[key] <= bound, 'REPORT_SCHEMA')
    _require(type(resources['parsing_complete']) is bool and type(resources['planning_complete']) is bool, 'REPORT_SCHEMA')
    inputs = data['inputs']
    roles = ['left', 'right', 'inner'] if data['operation'] == 'COMPOSE_COMPARE' else ['left', 'right']
    _require(type(inputs) is list and len(inputs) == len(roles), 'REPORT_INPUT_IDENTITIES')
    for role, value in zip(roles, inputs):
        _require(type(value) is dict and set(value) == {'role', 'sha256', 'byte_length'} and value['role'] == role, 'REPORT_INPUT_IDENTITIES')
        _require(_digest(value['sha256']) and type(value['byte_length']) is int and 0 <= value['byte_length'] <= 4096, 'REPORT_INPUT_IDENTITIES')
    context = [data['operation'], data['context_id'], data['context_revision'], 'R', 'I4-ISOLATED-1',
               [(v['role'], v['sha256'], v['byte_length']) for v in inputs]]
    _require(_sha(json.dumps(context, separators=(',', ':')).encode('ascii')) == data['context_sha256'], 'REPORT_CONTEXT_HASH')
    verified = data['status'].startswith('VERIFIED_')
    if verified:
        _require(resources['parsing_complete'] and resources['planning_complete']
                 and data['dependencies'] == [['sympy', '1.14.0'], ['mpmath', '1.3.0']], 'REPORT_SCHEMA')
        _normal(data['left_normal']); _normal(data['right_normal'])
        _require((data['left_normal'] == data['right_normal']) == (data['status'] == 'VERIFIED_EQUIVALENT'), 'REPORT_INTERNAL_CONFLICT')
    else:
        _require(data['left_normal'] is None and data['right_normal'] is None
                 and data['dependencies'] in ([], [['sympy', '1.14.0'], ['mpmath', '1.3.0']]), 'REPORT_SCHEMA')
    return data


def _submission(s):
    _require(type(s) is MathSubmission, 'SUBMISSION_TYPE')
    _require(_id(s.report_id) and type(s.report_bytes) is bytes, 'SUBMISSION_TYPE')
    _require(len(s.report_bytes) <= MAX_REPORT_BYTES, 'RESOURCE_LIMIT')
    for name in ('report_sha256', 'projection_sha256', 'i3_context_sha256', 'i4_context_sha256'):
        _require(_digest(getattr(s, name)), 'SUBMISSION_SCHEMA')
    _require(_id(s.i4_context_id) and _id(s.i4_context_revision), 'SUBMISSION_SCHEMA')
    _require(all(value is None or _id(value) for value in (s.task_id, s.attempt_id, s.assessment_id)), 'SUBMISSION_SCHEMA')
    _require(type(s.synthetic_test_report) is bool and type(s.declared_origin) is str and len(s.declared_origin) <= 256, 'SUBMISSION_SCHEMA')
    try:
        s.declared_origin.encode('utf-8', errors='strict')
    except UnicodeError:
        raise _Reject('SUBMISSION_SCHEMA') from None
    _require(type(s.operands) is tuple and len(s.operands) <= 3, 'SUBMISSION_SCHEMA')
    for v in s.operands:
        _require(type(v) is OperandLink and type(v.role) is str and v.role in ('left', 'right', 'inner'), 'SUBMISSION_SCHEMA')
        _require(type(v.evidence_index) is int and type(v.start) is int and type(v.end) is int
                 and 0 <= v.evidence_index <= 20000 and 0 <= v.start < v.end <= MAX_ARTIFACT_BYTES and _digest(v.sha256), 'SUBMISSION_SCHEMA')


def _snapshot_in_root(root, snapshot, digest):
    """Check an I3 navigation snapshot against its retained original sidecar root.

    This checks semantic consistency, not historic authenticity or reserialized
    byte identity. No I/O or external reference resolution is involved.
    """
    source = snapshot.source
    _require(source.origin == 'SIDECAR' and source.source_sha256 == digest, 'I3_DECLARATION_CONFLICT')
    pointer = source.pointer
    _require(type(pointer) is str and len(pointer) <= 2048 and pointer.startswith('/')
             and re.search(r'~(?![01])', pointer) is None, 'I3_DECLARATION_CONFLICT')
    value = root
    for encoded in pointer[1:].split('/'):
        token = encoded.replace('~1', '/').replace('~0', '~')
        if type(value) is dict:
            _require(token in value, 'I3_DECLARATION_CONFLICT')
            value = value[token]
        elif type(value) is list:
            _require(len(token) <= 8 and re.fullmatch(r'0|[1-9][0-9]*', token) is not None, 'I3_DECLARATION_CONFLICT')
            index = int(token)
            _require(index < len(value), 'I3_DECLARATION_CONFLICT')
            value = value[index]
        else:
            raise _Reject('I3_DECLARATION_CONFLICT')
    _require(value == load_json_bytes(snapshot.payload_json.encode('utf-8')), 'I3_DECLARATION_CONFLICT')


def _bind(p, digest, s, data, artifacts):
    _require(p.mode == 'SIDECAR_EVIDENCE', 'I3_MODE_NOT_ASSOCIABLE')
    _require(s.synthetic_test_report, 'SYNTHETIC_DECLARATION_REQUIRED')
    _require(s.projection_sha256 == digest and s.i3_context_sha256 == p.input_context.context_sha256, 'I3_CONTEXT_CONFLICT')
    root = load_json_bytes(p.session.sidecar_declaration.payload_json.encode('utf-8')) if p.session.sidecar_declaration else {}
    _require(type(root) is dict and root.get('data_classification') == 'SYNTHETIC', 'SYNTHETIC_CONTEXT_REQUIRED')
    _require(p.session.sidecar_declaration.source.origin == 'SIDECAR'
             and p.session.sidecar_declaration.source.pointer == ''
             and p.session.sidecar_declaration.source.source_sha256 == p.input_context.sidecar_sha256, 'I3_DECLARATION_CONFLICT')
    _require((s.i4_context_id, s.i4_context_revision, s.i4_context_sha256) ==
             (data['context_id'], data['context_revision'], data['context_sha256']), 'DECLARED_I4_CONTEXT_CONFLICT')
    tasks = [t for t in p.tasks if t.task_id == s.task_id]
    _require(len(tasks) == 1, 'TASK_MISSING_OR_AMBIGUOUS')
    task = tasks[0]
    attempts = [(t, a) for t in p.tasks for a in t.attempts if a.attempt_id == s.attempt_id]
    _require(len(attempts) == 1, 'ATTEMPT_MISSING_OR_AMBIGUOUS')
    owner, attempt = attempts[0]
    _require(owner.task_id == task.task_id == attempt.task_id and task.phase == attempt.phase, 'ATTEMPT_TASK_CONFLICT')
    _snapshot_in_root(root, task.declaration, p.input_context.sidecar_sha256)
    _snapshot_in_root(root, attempt.declaration, p.input_context.sidecar_sha256)
    _require(load_json_bytes(task.declaration.payload_json.encode())['task_id'] == task.task_id
             and load_json_bytes(attempt.declaration.payload_json.encode())['attempt_id'] == attempt.attempt_id
             and load_json_bytes(attempt.declaration.payload_json.encode())['task_id'] == task.task_id, 'I3_DECLARATION_CONFLICT')
    if s.assessment_id is not None:
        matches = [(t, a, v) for t in p.tasks for a in t.attempts for v in a.assessments if v.assessment_id == s.assessment_id]
        _require(len(matches) == 1, 'ASSESSMENT_MISSING_OR_AMBIGUOUS')
        t, a, v = matches[0]
        selected = [x for x in p.input_context.assessment_selection if x[0] == task.task_id]
        _require(t.task_id == task.task_id and a.attempt_id == attempt.attempt_id and v.attempt_id == attempt.attempt_id
                 and v.selected_for_v1 and task.selected_assessment_id == v.assessment_id
                 and v.rubric_key == task.rubric_key
                 and p.input_context.assessment_selection_supplied and selected == [(task.task_id, v.assessment_id)], 'ASSESSMENT_NOT_SELECTED')
        declaration = load_json_bytes(v.declaration.payload_json.encode())
        _snapshot_in_root(root, v.declaration, p.input_context.sidecar_sha256)
        _require(type(declaration) is dict and declaration.get('assessment_id') == v.assessment_id
                 and declaration.get('attempt_id') == attempt.attempt_id and declaration.get('revision') == v.revision, 'I3_DECLARATION_CONFLICT')
    roles = [v['role'] for v in data['inputs']]
    _require([v.role for v in s.operands] == roles, 'OPERAND_MAPPING_INCOMPLETE')
    relevant = set(task.finding_indices + attempt.finding_indices + p.session.binding_finding_indices)
    spans = []
    for link, identity in zip(s.operands, data['inputs']):
        _require(link.evidence_index < len(p.evidence), 'EVIDENCE_INDEX')
        ev = p.evidence[link.evidence_index]
        if link.role == 'right':
            _require(attempt.answer_evidence_indices.count(link.evidence_index) == 1, 'ANSWER_EVIDENCE_MEMBERSHIP')
            position = attempt.answer_evidence_indices.index(link.evidence_index)
            expected_path = attempt.declaration.source.pointer + '/answer_evidence/' + str(position)
        else:
            _require(link.evidence_index == task.statement_evidence_index, 'TASK_EVIDENCE_MEMBERSHIP')
            expected_path = task.declaration.source.pointer + '/task_statement_ref'
        _require(ev.ref_source == ev.declaration.source and ev.ref_source.origin == 'SIDECAR'
                 and ev.ref_source.pointer == expected_path
                 and ev.ref_source.source_sha256 == p.input_context.sidecar_sha256, 'EVIDENCE_LOCATOR_CONFLICT')
        _snapshot_in_root(root, ev.declaration, p.input_context.sidecar_sha256)
        indices = ev.selection_finding_indices + ev.message_finding_indices
        _require(all(type(i) is int and 0 <= i < len(p.findings) for i in indices), 'I3_FINDING_INDEX')
        _require(not any(p.findings[i].finding.status in ('CONFLICT', 'ERROR') for i in indices), 'I3_CONFLICT_REMAINS')
        _require(any(p.findings[i].finding.code == 'REF_RESOLVED' and p.findings[i].finding.status == 'PASS' for i in ev.selection_finding_indices), 'REFERENCE_UNRESOLVED')
        relevant.update(indices)
        ref = EvidenceRef.model_validate(load_json_bytes(ev.declaration.payload_json.encode('utf-8')))
        _require(ref.artifact_id == ev.artifact_id and ref.selector.kind == 'UTF8_BYTE_RANGE', 'EXACT_BYTE_SOURCE_REQUIRED')
        actual = [v for v in p.input_context.original_artifacts if v.artifact_id == ref.artifact_id]
        _require(len(actual) == 1 and ref.artifact_id in artifacts, 'SOURCE_MISSING_OR_AMBIGUOUS')
        views = [v for v in p.source_artifacts if v.artifact_id == ref.artifact_id]
        _require(len(views) == 1 and views[0].actual == actual[0], 'I3_SOURCE_IDENTITY_CONFLICT')
        if views[0].declaration is not None:
            _snapshot_in_root(root, views[0].declaration, p.input_context.sidecar_sha256)
        raw = artifacts[ref.artifact_id]
        _require(_sha(raw) == ref.sha256 == actual[0].sha256 and len(raw) == actual[0].byte_length, 'SOURCE_HASH_CONFLICT')
        chosen = select(raw, ref.selector)
        _require(chosen.text == ev.selected_text and ev.selected_text_source == ev.ref_source
                 and (ref.quote is None or ref.quote == chosen.text), 'EVIDENCE_TEXT_CONFLICT')
        _require(link.end <= len(chosen.raw), 'INPUT_SPAN_RANGE')
        expression = chosen.raw[link.start:link.end]
        _require(len(expression) <= 4096 and _sha(expression) == link.sha256 == identity['sha256']
                 and len(expression) == identity['byte_length'], 'INPUT_HASH_CONFLICT')
        spans.append(CheckedSpan(link.role, link.evidence_index, ref.artifact_id, ref.sha256,
                                 ref.selector.start + link.start, ref.selector.start + link.end,
                                 _sha(expression), expression.decode('utf-8')))
    _require(all(type(i) is int and 0 <= i < len(p.findings) for i in relevant), 'I3_FINDING_INDEX')
    _require(not any(p.findings[i].finding.status in ('CONFLICT', 'ERROR') for i in relevant), 'I3_CONFLICT_REMAINS')
    return tuple(spans), tuple(sorted(relevant))


def project_math_review(projection, submissions=(), *, artifacts=None):
    """Pure adapter. No mathematical execution; all origin statuses UNVERIFIED.

    Only UTF8_BYTE_RANGE evidence and exact UTF-8 subspans can be byte-bound.
    JSON-pointer reserialization is deliberately not represented as source bytes.
    """
    base = digest = mode = None
    def abort(code):
        # Whole overlay abstention: no partial reports or silently dropped entries.
        return MathReviewOverlay('DIAGNOSTIC_ONLY', None, digest, mode, (), (code,), False)
    try:
        p, base, digest = _capture_projection(projection)
        mode = p.mode
        _require(type(submissions) is tuple, 'SUBMISSIONS_TYPE')
        _require(len(submissions) <= MAX_SUBMISSIONS, 'RESOURCE_LIMIT')
        supplied = {} if artifacts is None else artifacts
        _require(type(supplied) is dict and len(supplied) <= MAX_ARTIFACTS, 'RESOURCE_LIMIT')
        copied = {}
        total = 0
        for key, raw in supplied.items():
            _require(_id(key) and '..' not in key and '/' not in key and ':' not in key
                     and type(raw) is bytes, 'ARTIFACT_TYPE')
            total += len(raw)
            _require(len(raw) <= MAX_ARTIFACT_BYTES and total <= MAX_TOTAL_ARTIFACT_BYTES, 'RESOURCE_LIMIT')
            copied[key] = raw
        for s in submissions:
            _submission(s)
        entries = []
        groups = {}
        for s in submissions:
            rawhex = s.report_bytes.hex()
            data = None
            normalized = None
            codes = ['EXECUTION_ORIGIN_UNVERIFIED']
            spans, indices = (), ()
            associated = False
            try:
                _require(_sha(s.report_bytes) == s.report_sha256, 'REPORT_HASH_CONFLICT')
                data = load_json_bytes(s.report_bytes)
                normalized = _json(data)
                _report(data)
                spans, indices = _bind(p, digest, s, data, copied)
                associated = True
                if data['status'] in ('UNKNOWN', 'UNSUPPORTED', 'ERROR'):
                    codes.append('I4_' + data['status'])
            except ArtifactError as exc:
                if exc.code.endswith('_LIMIT'):
                    raise _Reject('RESOURCE_LIMIT') from None
                codes.append(exc.code)
            except _Reject as exc:
                if exc.code == 'RESOURCE_LIMIT':
                    raise
                codes.append(exc.code)
            except (ValidationError, ValueError, KeyError, TypeError, UnicodeError):
                codes.append('REPORT_OR_REFERENCE_INVALID')
            status = data.get('status') if type(data) is dict and type(data.get('status')) is str and data['status'] in STATES else None
            operation = data.get('operation') if type(data) is dict and type(data.get('operation')) is str and len(data['operation']) <= 64 else None
            domain = data.get('domain') if type(data) is dict and type(data.get('domain')) is str and len(data['domain']) <= 64 else None
            normal = _json({k: data[k] for k in ('left_normal', 'right_normal')}) if associated else None
            entry = MathReviewEntry(s.report_id, _sha(s.report_bytes), rawhex, normalized,
                                    s.task_id, s.attempt_id, s.assessment_id,
                                    'BOUND_BYTES' if associated else 'UNASSIGNED', tuple(codes), status, operation, domain, normal,
                                    spans if associated else (), s.task_id if associated else None,
                                    s.attempt_id if associated else None, s.assessment_id if associated else None,
                                    indices if associated else (), tuple(p.findings[i].finding.status for i in indices) if associated else (),
                                    s.declared_origin)
            entries.append(entry)
            if associated:
                # Different declared I4 context/revision cannot hide contradictory
                # claims about the very same operation and exact source subspans.
                key = (s.task_id, s.attempt_id, s.assessment_id, operation, 'R',
                       tuple((v.role, v.evidence_index, v.start, v.end) for v in s.operands))
                groups.setdefault(key, []).append(len(entries) - 1)
        # Never choose the newest/best report. Competing claims stay detached.
        duplicates = {s.report_id for s in submissions if sum(x.report_id == s.report_id for x in submissions) > 1}
        for indexes in groups.values():
            if len(indexes) <= 1:
                continue
            verified = [entries[i] for i in indexes if entries[i].claimed_math_status.startswith('VERIFIED_')]
            claims = {(v.claimed_math_status, v.claimed_normal_forms_json) for v in verified}
            code = 'CONFLICTING_REPORTS' if len(claims) > 1 else 'MULTIPLE_REPORTS'
            for i in indexes:
                entries[i] = replace(entries[i], association='UNASSIGNED', associated_task_id=None,
                                     associated_attempt_id=None, associated_assessment_id=None, checked_spans=(),
                                     diagnostics=entries[i].diagnostics + (code,))
        for i, entry in enumerate(entries):
            if entry.report_id in duplicates:
                entries[i] = replace(entry, association='UNASSIGNED', associated_task_id=None,
                                     associated_attempt_id=None, associated_assessment_id=None, checked_spans=(),
                                     diagnostics=entry.diagnostics + ('DUPLICATE_REPORT_ID',))
        overlay = MathReviewOverlay('REPORTS_PRESENT' if entries else 'NO_REPORTS', base, digest, mode,
                                    tuple(entries), () if entries else ('NO_MATH_REPORTS',), True)
        _require(len(overlay.to_json().encode('ascii')) <= MAX_OUTPUT_BYTES, 'RESOURCE_LIMIT')
        return overlay
    except _Reject as exc:
        return abort(exc.code)
    except MemoryError:
        return abort('RESOURCE_LIMIT')
    except (ValidationError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        return abort('INVALID_OR_UNSTABLE_INPUT')
