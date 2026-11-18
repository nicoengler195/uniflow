"""Bounded in-memory artifact access. No filesystem, URL or database authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Mapping

MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ARTIFACTS = 64
MAX_DEPTH = 48
MAX_NODES = 50000
MAX_STRING = 65536
MAX_NUMBER = 128
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


class ArtifactError(ValueError):
    """Stable error code, deliberately without untrusted input in the message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_utf8(raw: bytes) -> str:
    if type(raw) is not bytes:
        raise ArtifactError("BYTES_REQUIRED")
    if len(raw) > MAX_BYTES:
        raise ArtifactError("BYTE_LIMIT")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ArtifactError("INVALID_UTF8") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _number(token, convert):
    if len(token) > MAX_NUMBER:
        raise ArtifactError("NUMBER_LIMIT")
    value = convert(token)
    if isinstance(value, float) and not math.isfinite(value):
        raise ArtifactError("NONFINITE_JSON_NUMBER")
    return value


def _constant(_):
    raise ArtifactError("NONFINITE_JSON_NUMBER")


def load_json_bytes(raw: bytes):
    """Strict UTF-8 JSON preserving input bytes; bounded before and after parsing."""
    text = decode_utf8(raw)
    # Bound parser recursion before json.loads, respecting quoted delimiters.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ArtifactError("DEPTH_LIMIT")
        elif char in "]}":
            depth -= 1
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_int=lambda s: _number(s, int),
                           parse_float=lambda s: _number(s, float),
                           parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError("INVALID_JSON") from exc
    stack = [value]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ArtifactError("NODE_LIMIT")
        if isinstance(item, str):
            if len(item) > MAX_STRING:
                raise ArtifactError("STRING_LIMIT")
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ArtifactError("INVALID_UNICODE_SCALAR") from exc
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return value


class ArtifactStore:
    """Explicit opaque IDs -> immutable raw bytes; IDs can never be paths."""

    def __init__(self, artifacts: Mapping[str, bytes]):
        if type(artifacts) is not dict or len(artifacts) > MAX_ARTIFACTS:
            raise ArtifactError("ARTIFACT_COUNT_OR_MAPPING")
        copied = {}
        total = 0
        for name, raw in artifacts.items():
            if type(name) is not str or not ID_PATTERN.fullmatch(name) or ".." in name:
                raise ArtifactError("INVALID_ARTIFACT_ID")
            decode_utf8(raw)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise ArtifactError("TOTAL_BYTE_LIMIT")
            copied[name] = raw
        self._artifacts = MappingProxyType(copied)

    def get(self, artifact_id: str) -> bytes:
        try:
            return self._artifacts[artifact_id]
        except KeyError as exc:
            raise ArtifactError("ARTIFACT_MISSING") from exc

    def inventory(self) -> tuple[tuple[str, str, int], ...]:
        return tuple((name, sha256(raw), len(raw))
                     for name, raw in sorted(self._artifacts.items()))


@dataclass(frozen=True)
class Selection:
    value: object
    text: str | None
    raw: bytes | None


def select(raw: bytes, selector) -> Selection:
    """RFC 6901 resolution; no serialization is passed off as original JSON bytes."""
    decode_utf8(raw)
    if selector.kind == "UTF8_BYTE_RANGE":
        start, end = selector.start, selector.end
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
            raise ArtifactError("BYTE_RANGE_INVALID")
        piece = raw[start:end]
        try:
            text = piece.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ArtifactError("UTF8_BOUNDARY_INVALID") from exc
        return Selection(text, text, piece)
    if selector.kind != "JSON_POINTER":
        raise ArtifactError("SELECTOR_INVALID")
    pointer = selector.pointer
    if not isinstance(pointer, str) or not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ArtifactError("POINTER_SYNTAX")
    value = load_json_bytes(raw)
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise ArtifactError("POINTER_TARGET_MISSING")
            value = value[token]
        elif isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or len(token) > 10:
                raise ArtifactError("POINTER_INDEX_INVALID")
            index = int(token)
            if index >= len(value):
                raise ArtifactError("POINTER_TARGET_MISSING")
            value = value[index]
        else:
            raise ArtifactError("POINTER_TARGET_MISSING")
    return Selection(value, value if isinstance(value, str) else None, None)
