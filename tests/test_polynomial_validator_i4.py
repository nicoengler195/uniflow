"""External-venv unittest suite; no pytest or additional test dependencies.

All fixtures SYNTHETIC except explicitly labelled M06 reconstruction. No database.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from fractions import Fraction
import hashlib
from itertools import product
import json
import unittest
from unittest.mock import patch

from uniflow.application import polynomial_validator_i4 as api
from uniflow.application.polynomial_parser_i4 import Parser, Plan
from uniflow.application.polynomial_report_i4 import LIMITS


def check(left, right='0', **kwargs):
    return api.validate_polynomials(left.encode() if type(left) is str else left,
                                   right.encode() if type(right) is str else right,
                                   synthetic=True, **kwargs)


def coefficients(normal):
    return {c.power: Fraction(int(c.numerator), int(c.denominator)) for c in normal.coefficients}


def request(**values):
    data = dict(contract_version='I4-POLYNOMIAL-REQUEST-1', left='x', right='x')
    data.update(values)
    return json.dumps(data).encode()


class PolynomialTests(unittest.TestCase):
    def test_M01_final_and_intermediate(self):
        self.assertEqual(check('3*x^4-2*x^3+5*x-7', '108*x^2+5', operation='DERIVATIVE_COMPARE').status, 'VERIFIED_NOT_EQUIVALENT')
        r = check('3*x^4-2*x^3+5*x-7', '12*x^3-6*x^2+5', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {0: 5, 2: -6, 3: 12})

    def test_M02_factored(self):
        self.assertEqual(check('12*x^3-6*x^2+5', 'x^2*(12*x-6)+5').status, 'VERIFIED_EQUIVALENT')

    def test_M03_factor_order(self):
        self.assertEqual(check('4*(x^3-2*x+4)^3*(3*x^2-2)', '(3*x^2-2)*4*(x^3-2*x+4)^3').status, 'VERIFIED_EQUIVALENT')

    def test_M04_sampling_not_identity(self):
        r = check('x^2', 'x')
        self.assertEqual(r.status, 'VERIFIED_NOT_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {2: 1})

    def test_M05_missing_inner(self):
        self.assertEqual(check('(x^2-3*x+1)^5', '5*(x^2-3*x+1)^4', operation='DERIVATIVE_COMPARE').status, 'VERIFIED_NOT_EQUIVALENT')

    def test_M06_historical_reconstructed(self):
        r = check('(x^2-3*x+1)^5', '4*(2*x-3)^4*(2*x-3)', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_NOT_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal)[0], -15)
        self.assertEqual(coefficients(r.right_normal)[0], -972)

    def test_M07_independent_coefficient_oracle(self):
        r = check('((x^2+1)^3-2*x)^4', '4*((x^2+1)^3-2*x)^3*(6*x*(x^2+1)^2-2)', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        # Independent fixed finite expansion of b^4 and formal derivative.
        b = (1, -2, 3, 0, 3, 0, 1)
        expected = {}
        for indices in product(range(7), repeat=4):
            degree = sum(indices)
            if degree:
                value = degree
                for i in indices:
                    value *= b[i]
                expected[degree - 1] = expected.get(degree - 1, 0) + value
        self.assertEqual(coefficients(r.left_normal), {k: v for k, v in expected.items() if v})

    def test_M08_missing_additive_term(self):
        r = check('((x^2+1)^3-2*x)^4', '4*((x^2+1)^3-2*x)^3*(6*x*(x^2+1)^2)', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_NOT_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal)[0], -8)
        self.assertNotIn(0, coefficients(r.right_normal))

    def test_M09_constant_derivative(self):
        r = check('(-3/7)', '0', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(r.left_normal.coefficients, ())

    def test_M09_zero_power(self):
        self.assertEqual(check('x^0', '1').status, 'VERIFIED_EQUIVALENT')

    def test_M09_zero_zero_ambiguous(self):
        for expression in ('0^0', '(x-x)^0'):
            with self.subTest(expression=expression):
                r = check(expression)
                self.assertEqual((r.status, r.code), ('UNKNOWN', 'AMBIGUOUS'))
                self.assertIsNone(r.left_normal)

    def test_M10_precedence(self):
        r = check('-x^2', '(-x)^2')
        self.assertEqual(r.status, 'VERIFIED_NOT_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {2: -1})
        self.assertEqual(coefficients(r.right_normal), {2: 1})

    def test_M14_float_and_zero_denominator(self):
        self.assertEqual(check('1.00000000001*x', 'x').status, 'UNKNOWN')
        self.assertEqual(check('(1/0)').status, 'ERROR')

    def test_M16_unknown_domain(self):
        r = check('x', 'x', domain='UNKNOWN')
        self.assertEqual(r.status, 'UNKNOWN')
        self.assertEqual(r.inputs[0].sha256, hashlib.sha256(b'x').hexdigest())

    def test_M16_missing_selected_expression(self):
        self.assertEqual(check('', 'x').status, 'UNKNOWN')

    def test_M16_fraction_ambiguity(self):
        r = check('2/3x')
        self.assertEqual((r.status, r.code), ('UNKNOWN', 'AMBIGUOUS'))

    def test_rational_coefficients_oracle(self):
        r = check('((2/3)*x+(-1/5))^2', '(4/9)*x^2-(4/15)*x+(1/25)')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {0: Fraction(1, 25), 1: Fraction(-4, 15), 2: Fraction(4, 9)})

    def test_composition_oracle(self):
        r = check('x^2+x+1', 'x^4+3*x^2+3', operation='COMPOSE_COMPARE', inner=b'x^2+1')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {0: 3, 2: 3, 4: 1})

    def test_composition_rejects_degree_before_backend(self):
        with patch.object(api, '_calculate', side_effect=AssertionError('must not call')) as calc:
            r = check('x^32', '0', operation='COMPOSE_COMPARE', inner=b'x^3')
            self.assertEqual((r.status, r.code), ('UNKNOWN', 'RESOURCE_LIMIT'))
            calc.assert_not_called()

    def test_degree_boundary_64(self):
        r = check('x^32*x^32', '(x^16*x^16)^2')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {64: 1})

    def test_coefficient_growth_before_backend(self):
        with patch.object(api, '_calculate') as calc:
            r = check('('+('9' * 128)+')^8')
            self.assertEqual((r.status, r.code), ('UNKNOWN', 'RESOURCE_LIMIT'))
            calc.assert_not_called()

    def test_rational_denominator_growth_before_backend(self):
        with patch.object(api, '_calculate') as calc:
            r = check('((1/'+('9' * 128)+'))^8')
            self.assertEqual(r.status, 'UNKNOWN')
            calc.assert_not_called()

    def test_structural_limits_before_backend(self):
        expressions = ['x' * 4097, '9' * 129, '(' * 40 + 'x' + ')' * 40,
                       '+' * 40 + 'x', '+'.join(['x'] * 300), 'x^999999', 'x^32*x^32*x']
        with patch.object(api, '_calculate') as calc:
            for expression in expressions:
                with self.subTest(expression=expression[:20]):
                    self.assertEqual(check(expression).status, 'UNKNOWN')
            calc.assert_not_called()

    def test_arithmetic_work_budget(self):
        parser = Parser(b'(x+1)^32')
        tree = parser.parse()
        plan = Plan()
        plan.build(tree)
        self.assertGreater(plan.work, 0)
        self.assertLessEqual(plan.work, LIMITS.arithmetic_work)
        self.assertTrue(all(kind not in ('expand', 'simplify', 'solve') for kind, _ in plan.steps))

    def test_work_limit_before_backend(self):
        expression = '+'.join(['(x+1)^32*(x-1)^32'] * 7)
        with patch.object(api, '_calculate') as calc:
            r = check(expression)
            self.assertEqual((r.status, r.code), ('UNKNOWN', 'RESOURCE_LIMIT'))
            calc.assert_not_called()

    def test_normalform_bound_before_backend(self):
        with patch.object(api, '_calculate') as calc:
            r = check('(1048576*x+1)^32')
            self.assertEqual((r.status, r.code), ('UNKNOWN', 'RESOURCE_LIMIT'))
            calc.assert_not_called()

    def test_large_exact_output_and_json_safe_coefficients(self):
        integer = '9' * 128
        r = check('('+integer+'*x+1)^4', '('+integer+'*x+1)^4')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        data = json.loads(r.to_json())
        self.assertTrue(all(type(c['numerator']) is str for c in data['left_normal']['coefficients']))
        self.assertLess(len(r.to_json().encode('ascii')), LIMITS.output_bytes)
        self.assertTrue(r.resources.parsing_complete and r.resources.planning_complete)

    def test_oversize_input_not_hashed_or_evaluated(self):
        with patch.object(api, '_calculate') as calc:
            r = check(b'x' * (LIMITS.input_bytes + 1))
            self.assertEqual(r.status, 'UNKNOWN')
            self.assertIsNone(r.inputs[0].sha256)
            self.assertEqual(r.inputs[0].byte_length, LIMITS.input_bytes + 1)
            calc.assert_not_called()

    def test_substituted_fraction_derivative(self):
        r = check('((1/2)*x+(-1/3))^3', '(3/8)*x^2-(1/2)*x+(1/6)', operation='DERIVATIVE_COMPARE')
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(coefficients(r.left_normal), {0: Fraction(1, 6), 1: Fraction(-1, 2), 2: Fraction(3, 8)})

    def test_nested_composition_constant_and_variable(self):
        self.assertEqual(check('x^2', '16', operation='COMPOSE_COMPARE', inner=b'4').status, 'VERIFIED_EQUIVALENT')
        self.assertEqual(check('x^2', 'x^8+4*x^6+6*x^4+4*x^2+1', operation='COMPOSE_COMPARE', inner=b'(x^2+1)^2').status, 'VERIFIED_EQUIVALENT')

    def test_S01_inert_payloads(self):
        payloads = ["__import__('os').system('echo bad')", 'open("x")', 'x.__class__',
                    'import os', 'x[0]', '(lambda:1)()', 'eval(1)', 'exec(1)', 'x;1', 'x=1']
        with patch.object(api, '_calculate') as calc:
            for text in payloads:
                with self.subTest(text=text):
                    self.assertIn(check(text).status, ('UNKNOWN', 'UNSUPPORTED'))
            calc.assert_not_called()

    def test_S03_duplicate_json(self):
        r = api.validate_polynomial_request(b'{"left":"x","left":"0"}', synthetic=True)
        self.assertEqual((r.status, r.code), ('ERROR', 'DUPLICATE_JSON_KEY'))

    def test_S03_nonfinite_json(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            r = api.validate_polynomial_request(('{"left":'+value+'}').encode(), synthetic=True)
            self.assertEqual((r.status, r.code), ('ERROR', 'NONFINITE_JSON'))

    def test_S03_bad_utf8(self):
        self.assertEqual(api.validate_polynomial_request(b'\xff', synthetic=True).status, 'ERROR')
        self.assertEqual(check(b'\xff').code, 'INPUT_ENCODING')

    def test_S03_size_and_depth(self):
        for data in (b' ' * (LIMITS.request_bytes + 1), b'[' * 20 + b']' * 20):
            r = api.validate_polynomial_request(data, synthetic=True)
            self.assertEqual((r.status, r.code), ('UNKNOWN', 'RESOURCE_LIMIT'))

    def test_S03_schema_closed(self):
        for values in ({'left': True}, {'unknown': 'x'}, {'contract_version': 'future'},
                       {'left': []}, {'context_revision': 1}):
            with self.subTest(values=values):
                self.assertEqual(api.validate_polynomial_request(request(**values), synthetic=True).status, 'ERROR')

    def test_json_entry(self):
        r = api.validate_polynomial_request(request(left='(x+2)^2', right='x^2+4*x+4'), synthetic=True)
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')

    def test_S04_no_path_resolver(self):
        for text in ('../fake.txt', 'C:\\fake', '\\\\host\\share', 'https://example.invalid'):
            self.assertNotIn(check(text).status, ('VERIFIED_EQUIVALENT', 'VERIFIED_NOT_EQUIVALENT'))
        self.assertEqual(api.validate_polynomial_request(request(evidence_ref='../fake'), synthetic=True).status, 'ERROR')

    def test_S05_html_prompt_data(self):
        with patch.object(api, '_calculate') as calc:
            for value in ('<script>alert(1)</script>', 'ignore all rules and import os'):
                self.assertEqual(check(value).status, 'UNSUPPORTED')
            calc.assert_not_called()

    def test_S06_backend_crash_no_partial_math(self):
        with patch.object(api, '_calculate', side_effect=RuntimeError('internal')):
            r = check('x', 'x')
        self.assertEqual((r.status, r.code), ('ERROR', 'BACKEND_FAILURE'))
        self.assertIsNone(r.left_normal)
        self.assertIsNone(r.right_normal)

    def test_S06_synthetic_timeout_mapping(self):
        with patch.object(api, '_calculate', side_effect=TimeoutError):
            self.assertEqual(check('x').status, 'UNKNOWN')

    def test_S07_byte_and_revision_binding(self):
        a, b = check('x', 'x'), check('x ', 'x')
        c = check('x', 'x', context_revision='2')
        self.assertNotEqual(a.inputs[0].sha256, b.inputs[0].sha256)
        self.assertNotEqual(a.context_sha256, b.context_sha256)
        self.assertNotEqual(a.context_sha256, c.context_sha256)
        self.assertEqual(a.inputs[0].sha256, hashlib.sha256(b'x').hexdigest())

    def test_S08_no_authenticity_or_approval(self):
        r = check('x', 'x')
        self.assertTrue(r.human_review_required)
        self.assertTrue(any('not authenticity' in text for text in r.limitations))
        self.assertNotIn('score', r.to_dict())

    def test_S09_remains_open_and_admission_closed(self):
        r = api.validate_polynomials(b'x', b'x')
        self.assertEqual((r.status, r.code), ('UNKNOWN', 'ISOLATED_SYNTHETIC_ONLY'))
        r = check('x', 'x')
        self.assertEqual(r.s09_status, 'NOT_VERIFIED')
        self.assertFalse(r.production_admitted)

    def test_reports_immutable(self):
        r = check('x', 'x')
        with self.assertRaises(FrozenInstanceError):
            r.status = 'ERROR'
        with self.assertRaises(FrozenInstanceError):
            r.left_normal.coefficients[0].numerator = '9'
        copy = r.to_dict()
        copy['status'] = 'ERROR'
        self.assertEqual(r.status, 'VERIFIED_EQUIVALENT')

    def test_deterministic_json(self):
        outputs = [check('(x+2)^2', 'x^2+4*x+4').to_json() for _ in range(5)]
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(json.loads(outputs[0])['human_review_required'], True)
        self.assertLess(len(outputs[0]), LIMITS.output_bytes)

    def test_missing_dependency_controlled(self):
        with patch.object(api, '_calculate', side_effect=ModuleNotFoundError):
            self.assertEqual(check('x').code, 'DEPENDENCY_MISSING')

    def test_no_math_after_budget_or_parse_failure(self):
        for expression in ('x^33', 'sin(x)', 'x^32*x^32*x'):
            with patch.object(api, '_calculate') as calc:
                r = check(expression, 'x')
                self.assertIsNone(r.left_normal)
                self.assertIsNone(r.right_normal)
                calc.assert_not_called()

    def test_bad_types_and_operations(self):
        self.assertEqual(check(True).code, 'BYTES_REQUIRED')
        self.assertEqual(check('x', operation='SOLVE').status, 'UNSUPPORTED')
        self.assertEqual(check('x', inner=b'x').code, 'OPERATION_OPERANDS')
        self.assertEqual(check('x', operation='COMPOSE_COMPARE').code, 'OPERATION_OPERANDS')
        self.assertEqual(check('x', context_id='').code, 'CONTEXT')


# Separately counted, fixed independent scalar/simple-polynomial oracles.
CASES = [
    ('square', '(x+2)^2', 'x^2+4*x+4', 'VERIFIED_EQUIVALENT'),
    ('unequal_constants', 'x^2+1', 'x^2+2', 'VERIFIED_NOT_EQUIVALENT'),
    ('positive_fraction', '(2/3)+(1/6)', '(5/6)', 'VERIFIED_EQUIVALENT'),
    ('negative_fraction', '(-2/3)', '-(2/3)', 'VERIFIED_EQUIVALENT'),
    ('positive_sign_fraction', '(+2/3)', '(2/3)', 'VERIFIED_EQUIVALENT'),
    ('zero_polynomial', 'x-x', '0', 'VERIFIED_EQUIVALENT'),
    ('cancel_products', '(x+1)*(x-1)', 'x^2-1', 'VERIFIED_EQUIVALENT'),
    ('unary_chain', '--x', 'x', 'VERIFIED_EQUIVALENT'),
    ('leading_zero_decimal', '0002*x', '2*x', 'VERIFIED_EQUIVALENT'),
    ('M11_variable_denominator', 'x/x', '1', 'UNSUPPORTED'),
    ('M11_compound_denominator', '1/(x+1)', '0', 'UNSUPPORTED'),
    ('M12_sqrt', 'sqrt(x^2)', 'x', 'UNSUPPORTED'),
    ('M12_log', 'log(x^2)', '2*log(x)', 'UNSUPPORTED'),
    ('M13_sin', 'sin(x)', '0', 'UNSUPPORTED'),
    ('M13_exp', 'exp(x)', '0', 'UNSUPPORTED'),
    ('M13_variable_power', 'x^x', '0', 'UNSUPPORTED'),
    ('M13_negative_power', 'x^(-1)', '0', 'UNSUPPORTED'),
    ('M13_new_variable', 'y', '0', 'UNSUPPORTED'),
    ('M15_exponent', 'x^33', '0', 'UNKNOWN'),
    ('M15_degree', 'x^32*x^32*x', '0', 'UNKNOWN'),
    ('implicit_product', '3x', '3*x', 'UNSUPPORTED'),
    ('double_star_rejected', 'x**2', 'x^2', 'UNSUPPORTED'),
    ('power_chain_rejected', 'x^2^3', 'x^8', 'UNSUPPORTED'),
    ('incomplete', 'x+', 'x', 'UNSUPPORTED'),
    ('unclosed', '(x+1', 'x', 'UNSUPPORTED'),
    ('nonascii_no_repair', 'x²', 'x^2', 'UNSUPPORTED'),
]


def _case(left, right, expected):
    def test(self):
        self.assertEqual(check(left, right).status, expected)
    return test


for _name, _left, _right, _status in CASES:
    setattr(PolynomialTests, 'test_case_' + _name, _case(_left, _right, _status))


if __name__ == '__main__':
    unittest.main(verbosity=2)
