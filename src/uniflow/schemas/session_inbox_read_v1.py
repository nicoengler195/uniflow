"""Strict Session Inbox V1 contract."""
from __future__ import annotations
import json
from datetime import datetime
from typing import Literal
from pydantic import Field, model_validator
from uniflow.schemas.application_read_v1 import StrictReadModel, SemesterSummaryV1, ReadDiagnosticV1
from uniflow.schemas.application_read_detail_v1 import SessionReadV1

RESUMABLE = {"PLANNED", "HANDOFF_CREATED", "IN_PROGRESS"}
PROCESSING = {"RESULT_RECEIVED", "VALIDATED", "MASTERY_UPDATED"}

class SessionInboxV1(StrictReadModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["session-inbox-v1"] = "session-inbox-v1"
    as_of: datetime
    semester: SemesterSummaryV1
    course_filter_stable_id: str | None
    resumable_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    resumable_sessions: list[SessionReadV1]
    processing_sessions: list[SessionReadV1]
    completed_sessions: list[SessionReadV1]
    completed_limit: int = Field(ge=1, le=100, strict=True)
    diagnostics: list[ReadDiagnosticV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.as_of.utcoffset() is None:
            raise ValueError("AS_OF_MUST_BE_AWARE")
        if self.resumable_count != len(self.resumable_sessions) or self.processing_count != len(self.processing_sessions):
            raise ValueError("SESSION_COUNT_INVALID")
        if len(self.completed_sessions) != min(self.completed_count, self.completed_limit):
            raise ValueError("COMPLETED_COUNT_INVALID")
        all_sessions = self.resumable_sessions + self.processing_sessions + self.completed_sessions
        if len({s.session_id for s in all_sessions}) != len(all_sessions):
            raise ValueError("SESSION_DUPLICATE")
        for group, states in ((self.resumable_sessions, RESUMABLE), (self.processing_sessions, PROCESSING), (self.completed_sessions, {"COMPLETED"})):
            if any(s.state not in states or (self.course_filter_stable_id is not None and s.course_stable_id != self.course_filter_stable_id) for s in group):
                raise ValueError("SESSION_GROUP_INVALID")
            if group != sorted(group, key=lambda s: (-s.activity_at.timestamp(), s.session_id)):
                raise ValueError("SESSION_ORDER_INVALID")
        return self

def canonical_session_inbox_json(value: SessionInboxV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
