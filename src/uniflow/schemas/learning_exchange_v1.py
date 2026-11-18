"""Deterministic transport metadata about an immutable learning Handoff."""
from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import Field, model_validator

from uniflow.schemas.workflow_v1 import WorkflowModelV1


def result_id_for_handoff(handoff_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"uniflow:learning-result-v1:{handoff_id}"))


class LearningExchangeEnvelopeV1(WorkflowModelV1):
    schema_version: Literal["1.0"] = "1.0"
    protocol_version: Literal["subject-learning-v1"]
    session_id: str = Field(min_length=1, max_length=80)
    handoff_id: str = Field(min_length=1, max_length=80)
    handoff_filename: Literal["handoff.json"] = "handoff.json"
    handoff_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_id: str = Field(min_length=36, max_length=36)
    result_filename: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def coherent(self):
        if (self.result_id != result_id_for_handoff(self.handoff_id) or
            str(uuid.UUID(self.result_id)) != self.result_id):
            raise ValueError("RESULT_ID_INVALID")
        if (self.result_filename != f"{self.session_id}--{self.handoff_id}.json" or
            "/" in self.result_filename or "\\" in self.result_filename):
            raise ValueError("RESULT_FILENAME_INVALID")
        return self


def canonical_learning_exchange_envelope_json(value: LearningExchangeEnvelopeV1) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
