"""Read-only isolated I4 prototype. Not admitted for untrusted production input.

No imports of I1/I2/I3, persistence, UI, worker launchers or scoring. SymPy is
loaded only after the entire bounded operation plan has been accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

from uniflow.application.polynomial_parser_i4 import Parser, Plan, PolynomialIssue, limit
from uniflow.application.polynomial_report_i4 import (
    LIMITS, Coefficient, InputIdentity, NormalForm, PolynomialReport, ResourceUse,
)

OPERATIONS = frozenset(('COMPARE', 'DERIVATIVE_COMPARE', 'COMPOSE_COMPARE'))
CONTEXT = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
REASONS = {
    'IDENTICAL_COEFFICIENTS': 'Exact QQ coefficients establish polynomial identity on R.',
    'DIFFERENT_COEFFICIENTS': 'Different exact QQ coefficients disprove polynomial identity on R.',
    'ISOLATED_SYNTHETIC_ONLY': 'Explicit synthetic use is required; production admission is closed.',
    'RESOURCE_LIMIT': 'A conservative structural, arithmetic or output budget was exceeded.',
    'AMBIGUOUS': 'An unambiguous expression, domain and selected answer are required.',
    'UNDEFINED_RATIONAL': 'A rational literal has zero denominator.',
    'DEPENDENCY_MISSING': 'The separately approved CAS dependencies are unavailable.',
    'RUNTIME_MISMATCH': 'The approved Python/CAS versions are required.',
    'BACKEND_FAILURE': 'A technical calculation failure prevented a mathematical finding.',
}


def _identity(role: str, raw: bytes, maximum: int) -> InputIdentity:
    return InputIdentity(role, hashlib.sha256(raw).hexdigest() if len(raw) <= maximum else None, len(raw))


def _normal(poly) -> NormalForm:
    """Bounded integer/rational export. No CAS objects escape the API."""
    values = poly.as_dict()
    limit(len(values) <= LIMITS.degree + 1)
    result = []
    bits = 0
    for (power,), value in sorted(values.items()):
        numerator, denominator = int(value.p), int(value.q)
        nb, db = abs(numerator).bit_length(), denominator.bit_length()
        bits += nb + db
        limit(0 <= power <= LIMITS.degree and nb <= LIMITS.coefficient_bits
              and db <= LIMITS.coefficient_bits and bits <= LIMITS.normal_form_bits)
        result.append(Coefficient(int(power), str(numerator), str(denominator)))
    return NormalForm(tuple(result))


def _calculate(plan: Plan):
    # Fixed, lazy imports; never derived from caller text.
    import sympy
    import mpmath
    if sys.version_info[:3] != (3, 14, 6) or (sympy.__version__, mpmath.__version__) != ('1.14.0', '1.3.0'):
        raise PolynomialIssue('ERROR', 'RUNTIME_MISMATCH')
    x = sympy.Symbol('x', real=True)
    def constant(value):
        return sympy.Poly.from_dict({(0,): sympy.QQ(value.numerator, value.denominator)}, x, domain=sympy.QQ)
    stack, bases, captured = [], [], []
    for kind, value in plan.steps:
        if kind == 'const':
            stack.append(constant(value))
        elif kind == 'x':
            stack.append(sympy.Poly.from_dict({(1,): sympy.QQ(1)}, x, domain=sympy.QQ))
        elif kind == 'one':
            stack.append(constant(1))
        elif kind == 'save_base':
            base = stack.pop()
            if value == 0 and base.is_zero:
                raise PolynomialIssue('UNKNOWN', 'AMBIGUOUS')
            bases.append(base)
        elif kind == 'load_base':
            stack.append(bases[-1])
        elif kind == 'drop_base':
            bases.pop()
        elif kind in ('add', 'sub', 'mul'):
            b, a = stack.pop(), stack.pop()
            stack.append(a.add(b) if kind == 'add' else a.sub(b) if kind == 'sub' else a.mul(b))
        elif kind == 'neg':
            stack[-1] = -stack[-1]
        elif kind == 'pos':
            pass
        elif kind == 'diff':
            stack[-1] = stack[-1].diff()
        elif kind == 'capture':
            captured.append(_normal(stack.pop()))
            if bases or stack:
                raise RuntimeError('plan invariant')
        else:
            raise RuntimeError('unknown fixed instruction')
        if stack:
            # Additional checks after each operation; prechecks already bounded
            # every intermediate operation, including each power multiplication.
            _normal(stack[-1])
    if len(captured) != 2 or stack or bases:
        raise RuntimeError('result invariant')
    return captured[0], captured[1], (('sympy', sympy.__version__), ('mpmath', mpmath.__version__))


def validate_polynomials(left: bytes, right: bytes, *, operation: str = 'COMPARE',
                         inner: bytes | None = None, domain: str = 'R',
                         context_id: str = 'SYNTHETIC', context_revision: str = '1',
                         synthetic: bool = False) -> PolynomialReport:
    """Compare fixed synthetic byte expressions, optionally differentiating or
    composing the left operand. A flag is an admission gate, not authentication.

    COMPARE: left == right. DERIVATIVE_COMPARE: d(left)/dx == right.
    COMPOSE_COMPARE: left(inner(x)) == right. No implicit answer selection.
    """
    identities: tuple[InputIdentity, ...] = ()
    resources = ResourceUse()
    safe_operation = operation if type(operation) is str and len(operation) <= 64 and operation in OPERATIONS else 'INVALID'
    safe_id = context_id if type(context_id) is str and len(context_id) <= 64 and CONTEXT.fullmatch(context_id) else 'INVALID'
    safe_revision = context_revision if type(context_revision) is str and len(context_revision) <= 64 and CONTEXT.fullmatch(context_revision) else 'INVALID'
    context_hash = hashlib.sha256(json.dumps([safe_operation, safe_id, safe_revision, 'R', 'I4-ISOLATED-1'], separators=(',', ':')).encode()).hexdigest()

    def report(status, code, left_normal=None, right_normal=None, dependencies=()):
        result = PolynomialReport(status, code, REASONS.get(code, 'Input is outside the strict declared contract: ' + code),
                                  safe_operation, identities, safe_id, safe_revision, context_hash,
                                  resources, left_normal, right_normal, dependencies)
        if len(result.to_json().encode('ascii')) > LIMITS.output_bytes:
            return PolynomialReport('UNKNOWN', 'RESOURCE_LIMIT', REASONS['RESOURCE_LIMIT'], safe_operation,
                                    identities, safe_id, safe_revision, context_hash, resources)
        return result

    try:
        operands = [('left', left), ('right', right)]
        if inner is not None:
            operands.append(('inner', inner))
        if any(type(raw) is not bytes for _, raw in operands):
            raise PolynomialIssue('ERROR', 'BYTES_REQUIRED')
        identities = tuple(_identity(role, raw, LIMITS.input_bytes) for role, raw in operands)
        context_hash = hashlib.sha256(json.dumps(
            [safe_operation, safe_id, safe_revision, 'R' if type(domain) is str and domain == 'R' else 'AMBIGUOUS_DOMAIN', 'I4-ISOLATED-1',
             [(i.role, i.sha256, i.byte_length) for i in identities]],
            separators=(',', ':')).encode('ascii')).hexdigest()
        if synthetic is not True:
            return report('UNKNOWN', 'ISOLATED_SYNTHETIC_ONLY')
        if safe_operation == 'INVALID':
            raise PolynomialIssue('UNSUPPORTED', 'OPERATION')
        if safe_id == 'INVALID' or safe_revision == 'INVALID':
            raise PolynomialIssue('ERROR', 'CONTEXT')
        if type(domain) is not str or domain != 'R':
            raise PolynomialIssue('UNKNOWN', 'AMBIGUOUS')
        if (operation == 'COMPOSE_COMPARE') != (inner is not None):
            raise PolynomialIssue('ERROR', 'OPERATION_OPERANDS')
        parsers, trees = [], []
        for _, raw in operands:
            limit(len(raw) <= LIMITS.input_bytes)
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                raise PolynomialIssue('ERROR', 'INPUT_ENCODING') from None
            parser = Parser(raw)
            tree = parser.parse()
            parsers.append(parser)
            trees.append(tree)
        resources = ResourceUse(sum(p.token_count for p in parsers), sum(p.nodes for p in parsers),
                                sum(p.operators for p in parsers), parsing_complete=True)
        limit(resources.tokens <= LIMITS.tokens and resources.nodes <= LIMITS.nodes
              and resources.operators <= LIMITS.operators)
        plan = Plan()
        a = plan.build(trees[0], trees[2] if inner is not None else None)
        if operation == 'DERIVATIVE_COMPARE':
            a = plan.derivative(a)
        plan.emit('capture', None, a)
        b = plan.build(trees[1])
        plan.emit('capture', None, b)
        resources = ResourceUse(resources.tokens, resources.nodes, resources.operators, plan.work, len(plan.steps), True, True)
        a_normal, b_normal, dependencies = _calculate(plan)
        same = a_normal == b_normal
        return report('VERIFIED_EQUIVALENT' if same else 'VERIFIED_NOT_EQUIVALENT',
                      'IDENTICAL_COEFFICIENTS' if same else 'DIFFERENT_COEFFICIENTS',
                      a_normal, b_normal, dependencies)
    except PolynomialIssue as exc:
        return report(exc.status, exc.code)
    except ImportError:
        return report('ERROR', 'DEPENDENCY_MISSING')
    except (MemoryError, TimeoutError):
        return report('UNKNOWN', 'RESOURCE_LIMIT')
    except Exception:
        return report('ERROR', 'BACKEND_FAILURE')


def validate_polynomial_request(payload: bytes, *, synthetic: bool = False) -> PolynomialReport:
    """Strict bounded JSON convenience entry. Never loads paths or evidence_refs.

    Fields: contract_version='I4-POLYNOMIAL-REQUEST-1', left/right strings;
    optional operation/inner/domain/context_id/context_revision. All strings.
    """
    def failure(status, code):
        identities = (_identity('request', payload, LIMITS.request_bytes),) if type(payload) is bytes else ()
        return PolynomialReport(status, code, 'Strict JSON request rejected: ' + code, 'INVALID', identities,
                                'INVALID', 'INVALID', '', ResourceUse())
    if synthetic is not True:
        return validate_polynomials(b'', b'', synthetic=False)
    if type(payload) is not bytes:
        return failure('ERROR', 'BYTES_REQUIRED')
    if len(payload) > LIMITS.request_bytes:
        return failure('UNKNOWN', 'RESOURCE_LIMIT')
    try:
        # Check nesting before the stdlib recursive JSON decoder. Quotes/escapes
        # are observed so brackets inside inert string data do not count.
        depth, quoted, escaped = 0, False, False
        for char in payload:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char in (91, 123):
                depth += 1
                limit(depth <= 8)
            elif char in (93, 125):
                depth -= 1
        def pairs(entries):
            result = {}
            for key, value in entries:
                if key in result:
                    raise PolynomialIssue('ERROR', 'DUPLICATE_JSON_KEY')
                result[key] = value
            return result
        def invalid_constant(_):
            raise PolynomialIssue('ERROR', 'NONFINITE_JSON')
        data = json.loads(payload.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid_constant)
        allowed = {'contract_version', 'left', 'right', 'operation', 'inner', 'domain', 'context_id', 'context_revision'}
        if (type(data) is not dict or set(data) - allowed or not {'contract_version', 'left', 'right'} <= set(data)
                or any(type(v) is not str for v in data.values())
                or data['contract_version'] != 'I4-POLYNOMIAL-REQUEST-1'):
            return failure('ERROR', 'REQUEST_SCHEMA')
        if any(len(value) > LIMITS.input_bytes for value in data.values()):
            return failure('UNKNOWN', 'RESOURCE_LIMIT')
        return validate_polynomials(data['left'].encode('utf-8'), data['right'].encode('utf-8'),
                                    inner=data['inner'].encode('utf-8') if 'inner' in data else None,
                                    operation=data.get('operation', 'COMPARE'), domain=data.get('domain', 'R'),
                                    context_id=data.get('context_id', 'SYNTHETIC'), context_revision=data.get('context_revision', '1'),
                                    synthetic=True)
    except PolynomialIssue as exc:
        return failure(exc.status, exc.code)
    except (ValueError, UnicodeError, RecursionError):
        return failure('ERROR', 'INVALID_JSON')
