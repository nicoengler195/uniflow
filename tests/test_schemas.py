from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from uniflow.domain.enums import InformationStatus
from uniflow.schemas import LearningHandoffV1, LearningResultV1, SemesterAuditV1


ROOT = Path(__file__).resolve().parents[1]


def test_semester_audit_example_validates() -> None:
    raw = yaml.safe_load((ROOT / "examples" / "semester_audit_v1.yaml").read_text(encoding="utf-8"))
    audit = SemesterAuditV1.model_validate(raw)

    assert audit.schema_version == "1.0"
    assert audit.courses[0].deadlines[0].information_status == InformationStatus.UNKNOWN


def test_learning_handoff_example_validates() -> None:
    raw = yaml.safe_load((ROOT / "examples" / "learning_handoff_v1.yaml").read_text(encoding="utf-8"))
    handoff = LearningHandoffV1.model_validate(raw)

    assert handoff.schema_version == "1.0"
    assert handoff.skills[0].skill_id == "PHYS_KINEMATICS_ACCELERATION"


def test_learning_result_example_validates() -> None:
    raw = json.loads((ROOT / "examples" / "learning_result_v1.json").read_text(encoding="utf-8"))
    result = LearningResultV1.model_validate(raw)

    assert result.schema_version == "1.0"
    assert result.skill_evidence[0].suggested_mastery == 0.78


def test_unknown_schema_fields_are_rejected() -> None:
    raw = {
        "schema_version": "1.0",
        "session_id": "SESSION_BAD",
        "course": {"stable_id": "COURSE", "name": "Course"},
        "completed_at": "2027-05-12T15:30:00+02:00",
        "skill_evidence": [],
        "unexpected": "not allowed",
    }

    try:
        LearningResultV1.model_validate(raw)
    except ValidationError as exc:
        assert "unexpected" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("schema accepted an unknown field")
