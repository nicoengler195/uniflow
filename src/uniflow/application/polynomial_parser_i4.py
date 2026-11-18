"""Small explicit grammar and conservative pre-CAS operation planning."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from uniflow.application.polynomial_report_i4 import LIMITS


class PolynomialIssue(Exception):
    def __init__(self, status: str, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def limit(ok: bool) -> None:
    if not ok:
        raise PolynomialIssue('UNKNOWN', 'RESOURCE_LIMIT')


@dataclass(frozen=True, slots=True)
class Node:
    kind: str
    value: Fraction | int | None = None
    children: tuple[Node, ...] = ()
    height: int = 1


class Parser:
    def __init__(self, raw: bytes):
        limit(len(raw) <= LIMITS.input_bytes)
        try:
            text = raw.decode('ascii')
        except UnicodeDecodeError:
            raise PolynomialIssue('UNSUPPORTED', 'ASCII_SYNTAX_REQUIRED') from None
        self.tokens: list[tuple[str, str]] = []
        self.nodes = self.operators = 0
        i = 0
        while i < len(text):
            char = text[i]
            if char in ' \t\r\n':
                i += 1
                continue
            if char in '0123456789':
                end = i + 1
                while end < len(text) and text[end] in '0123456789':
                    end += 1
                    limit(end - i <= LIMITS.literal_digits)
                self.tokens.append(('int', text[i:end]))
                i = end
            elif char in 'x+-*^()/':
                self.tokens.append((char, char))
                i += 1
            elif char == '.':
                raise PolynomialIssue('UNKNOWN', 'NUMERIC_OR_ATTRIBUTE_SYNTAX')
            else:
                raise PolynomialIssue('UNSUPPORTED', 'UNKNOWN_TOKEN')
            limit(len(self.tokens) <= LIMITS.tokens)
        self.token_count = len(self.tokens)
        self.tokens.append(('end', ''))
        self.pos = 0

    def peek(self, offset: int = 0) -> str:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)][0]

    def take(self, kind: str) -> str:
        if self.peek() != kind:
            raise PolynomialIssue('UNSUPPORTED', 'SYNTAX')
        value = self.tokens[self.pos][1]
        self.pos += 1
        return value

    def node(self, kind: str, value=None, children=()) -> Node:
        self.nodes += 1
        self.operators += int(kind not in ('const', 'x'))
        height = 1 + max((n.height for n in children), default=0)
        limit(self.nodes <= LIMITS.nodes and self.operators <= LIMITS.operators
              and height <= LIMITS.depth)
        return Node(kind, value, children, height)

    def parse(self) -> Node:
        if self.peek() == 'end':
            raise PolynomialIssue('UNKNOWN', 'AMBIGUOUS')
        # This exact unparenthesized fraction-with-implicit-product has two readings.
        if [t[0] for t in self.tokens[:-1]] == ['int', '/', 'int', 'x']:
            raise PolynomialIssue('UNKNOWN', 'AMBIGUOUS')
        result = self.expr(0)
        if self.peek() == '/':
            raise PolynomialIssue('UNSUPPORTED', 'DIVISION_OUTSIDE_RATIONAL_LITERAL')
        self.take('end')
        return result

    def expr(self, depth: int) -> Node:
        limit(depth <= LIMITS.depth)
        result = self.term(depth)
        while self.peek() in ('+', '-'):
            op = self.peek()
            self.take(op)
            result = self.node('add' if op == '+' else 'sub', children=(result, self.term(depth)))
        return result

    def term(self, depth: int) -> Node:
        result = self.unary(depth)
        while self.peek() == '*':
            self.take('*')
            result = self.node('mul', children=(result, self.unary(depth)))
        return result

    def unary(self, depth: int) -> Node:
        limit(depth <= LIMITS.depth)
        if self.peek() in ('+', '-'):
            sign = self.peek()
            self.take(sign)
            return self.node('neg' if sign == '-' else 'pos', children=(self.unary(depth + 1),))
        result = self.atom(depth)
        if self.peek() == '^':
            self.take('^')
            if self.peek() != 'int':
                raise PolynomialIssue('UNSUPPORTED', 'NON_LITERAL_EXPONENT')
            exponent = int(self.take('int'))
            limit(exponent <= LIMITS.exponent)
            result = self.node('pow', exponent, (result,))
        return result

    def atom(self, depth: int) -> Node:
        if self.peek() == 'int':
            return self.node('const', Fraction(int(self.take('int'))))
        if self.peek() == 'x':
            self.take('x')
            return self.node('x')
        if self.peek() == '(':
            self.take('(')
            sign_offset = int(self.peek() in ('+', '-'))
            if (self.peek(sign_offset) == 'int' and self.peek(sign_offset + 1) == '/'
                    and self.peek(sign_offset + 2) == 'int' and self.peek(sign_offset + 3) == ')'):
                sign = 1
                if sign_offset:
                    s = self.peek()
                    self.take(s)
                    sign = -1 if s == '-' else 1
                numerator = sign * int(self.take('int'))
                self.take('/')
                denominator = int(self.take('int'))
                self.take(')')
                if not denominator:
                    raise PolynomialIssue('ERROR', 'UNDEFINED_RATIONAL')
                return self.node('const', Fraction(numerator, denominator))
            result = self.expr(depth + 1)
            if self.peek() == '/':
                raise PolynomialIssue('UNSUPPORTED', 'VARIABLE_OR_COMPOUND_DENOMINATOR')
            self.take(')')
            return result
        raise PolynomialIssue('UNSUPPORTED', 'SYNTAX')


@dataclass(frozen=True, slots=True)
class Bound:
    degree: int
    numerator_bits: int
    denominator_bits: int  # 0 denotes common denominator exactly 1


class Plan:
    """Straight-line QQ operations, completely budgeted before importing SymPy.

    Every polynomial has a hypothetical COMMON denominator. This avoids assuming
    unrelated reduced coefficient denominators can be added for free.
    """
    def __init__(self):
        self.steps: list[tuple[str, object]] = []
        self.work = 0

    def emit(self, kind: str, value: object, bound: Bound, work: int = 0) -> Bound:
        limit(bound.degree <= LIMITS.degree
              and bound.numerator_bits <= LIMITS.coefficient_bits
              and bound.denominator_bits <= LIMITS.coefficient_bits
              and (bound.degree + 1) * (bound.numerator_bits + bound.denominator_bits)
              <= LIMITS.normal_form_bits)
        self.work += work
        limit(self.work <= LIMITS.arithmetic_work and len(self.steps) < LIMITS.instructions)
        self.steps.append((kind, value))
        return bound

    def binary(self, kind: str, a: Bound, b: Bound) -> Bound:
        if kind in ('add', 'sub'):
            bound = Bound(max(a.degree, b.degree),
                          max(a.numerator_bits + b.denominator_bits,
                              b.numerator_bits + a.denominator_bits) + 1,
                          a.denominator_bits + b.denominator_bits)
            cost = bound.degree + 1
        else:
            # Each convolution coefficient sums at most min(deg)+1 products.
            terms = min(a.degree, b.degree) + 1
            bound = Bound(a.degree + b.degree,
                          a.numerator_bits + b.numerator_bits + (terms - 1).bit_length(),
                          a.denominator_bits + b.denominator_bits)
            cost = (a.degree + 1) * (b.degree + 1)
        return self.emit(kind, None, bound, cost)

    def build(self, node: Node, variable: Node | None = None) -> Bound:
        if node.kind == 'x':
            if variable is not None:
                return self.build(variable)
            return self.emit('x', None, Bound(1, 1, 0))
        if node.kind == 'const':
            f = node.value
            return self.emit('const', f, Bound(0, abs(f.numerator).bit_length(),
                             0 if f.denominator == 1 else f.denominator.bit_length()))
        a = self.build(node.children[0], variable)
        if node.kind in ('neg', 'pos'):
            return self.emit(node.kind, None, a, a.degree + 1)
        if node.kind == 'pow':
            # Bounded repeated multiplication, not an unbounded Poly.pow/expand.
            # Save a bounded base on the evaluator stack, then one checked step
            # for each multiplication. exponent 0 still checks zero polynomial.
            exponent = node.value
            self.emit('save_base', exponent, a)
            result = Bound(0, 1, 0)
            self.emit('one', None, result)
            for _ in range(exponent):
                # load_base adds only an already budgeted bounded stack value.
                self.emit('load_base', None, a)
                result = self.binary('mul', result, a)
            return self.emit('drop_base', None, result)
        return self.binary(node.kind, a, self.build(node.children[1], variable))

    def derivative(self, a: Bound) -> Bound:
        result = Bound(max(0, a.degree - 1), a.numerator_bits + a.degree.bit_length(), a.denominator_bits)
        return self.emit('diff', None, result, a.degree + 1)
