"""Controlled workflow rejections; unexpected service failures remain exceptions."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from pydantic import ValidationError

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _safe_detail(value, depth: int = 0) -> bool:
    if depth > 3:
        return False
    if isinstance(value, str):
        return bool(_REF.fullmatch(value))
    if isinstance(value, list):
        return len(value) <= 12 and all(_safe_detail(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 8 and all(isinstance(key, str) and _REF.fullmatch(key) and
            _safe_detail(item, depth + 1) for key, item in value.items())
    return isinstance(value, (bool, int)) or value is None


class WorkflowCommandError(Exception):
    def __init__(self, code: str, *, refs: tuple[str, ...] = (), details: Mapping | None = None):
        if not _CODE.fullmatch(code):
            raise ValueError("invalid workflow error code")
        if len(refs) > 8 or any(not _REF.fullmatch(ref) for ref in refs):
            raise ValueError("invalid workflow error refs")
        value = dict(details or {})
        if not _safe_detail(value):
            raise ValueError("invalid workflow error details")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 2048:
            raise ValueError("workflow error details too large")
        self.code = code
        self.message_key = "workflow." + code.lower()
        self.refs = tuple(sorted(set(refs)))
        self.details = json.loads(encoded)
        super().__init__(code)

    def public_dict(self) -> dict:
        return {"code": self.code, "message_key": self.message_key,
                "refs": list(self.refs), "details": self.details}


def normalize_known(exc: ValueError, codes: frozenset[str]) -> None:
    code = str(exc)
    if code in codes:
        raise WorkflowCommandError(code) from exc
    raise exc


def parse_request(model, request):
    """Only validation performed at this explicit facade boundary is normalized."""
    try:
        return model.model_validate(request)
    except ValidationError as exc:
        fields = set(model.model_fields)
        items = sorted({(str(err["loc"][0]) if err["loc"] and err["loc"][0] in fields else "request",
                         err["type"])
                        for err in exc.errors(include_input=False, include_context=False)})[:12]
        details = {"errors": [{"field": field[:80], "type": kind[:80]} for field, kind in items]}
        raise WorkflowCommandError("WORKFLOW_INPUT_INVALID", details=details) from exc
