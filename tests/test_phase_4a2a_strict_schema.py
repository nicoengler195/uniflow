"""Direct strict-shape checks for the Phase 4A2A DTOs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from uniflow.schemas.application_read_v1 import SemesterSummaryV1
from uniflow.schemas.change_feed_read_v1 import ChangeEventV1, ChangeFeedV1
from uniflow.schemas.week_read_v1 import WeekRevisionReadV1
from uniflow.schemas.weekly_plan_v1 import WeeklyPlanAssessmentV1


AT = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
SEMESTER = SemesterSummaryV1(id="sem", stable_id="SYNTH", name="Synthetic",
    state="ACTIVE", timezone="Europe/Berlin")
CALENDAR_IDENTITY = {"artifact_id": "calendar", "payload_sha256": "a" * 64,
    "context_fingerprint": "b" * 64}


def _revision(status: str, **overrides) -> WeekRevisionReadV1:
    values = dict(revision_id="rev", revision_number=1, revision_kind="GENERATED",
        parent_revision_id=None, created_at=AT,
        source_calendar_identity=CALENDAR_IDENTITY, blocks=[], unscheduled_actions=[],
        revision_assessment=WeeklyPlanAssessmentV1(plan_id="plan", revision_id="rev",
            revision_number=1, checked_at=AT, status=status,
            factors=["SOURCE_ARTIFACT_INVALID"] if status == "INVALID" else [],
            nba_match=None if status == "INVALID" else True,
            calendar_match=None if status == "INVALID" else True))
    values.update(overrides)
    return WeekRevisionReadV1.model_validate(values)


def test_week_revision_valid_complete_shape():
    assert _revision("FRESH").blocks == []


@pytest.mark.parametrize("missing", ["blocks", "unscheduled_actions", "source_calendar_identity"])
def test_week_revision_noninvalid_requires_all_details(missing):
    with pytest.raises(ValidationError, match="INVALID_REVISION_DISPLAY"):
        _revision("FRESH", **{missing: None})


def test_week_revision_invalid_requires_all_details_absent():
    assert _revision("INVALID", source_calendar_identity=None, blocks=None,
        unscheduled_actions=None).blocks is None


@pytest.mark.parametrize("present,value", [
    ("blocks", []), ("unscheduled_actions", []),
    ("source_calendar_identity", CALENDAR_IDENTITY),
])
def test_week_revision_invalid_rejects_any_detail(present, value):
    values = dict(source_calendar_identity=None, blocks=None, unscheduled_actions=None)
    values[present] = value
    with pytest.raises(ValidationError, match="INVALID_REVISION_DISPLAY"):
        _revision("INVALID", **values)


def _event(kind="AUDIT_IMPORTED", entity_id="audit", at=AT, **overrides) -> ChangeEventV1:
    values = dict(event_id=f"{kind}:{entity_id}", kind=kind, occurred_at=at,
        semester_id="sem", semester_stable_id="SYNTH", course_ids=[],
        course_stable_ids=[], entity_type="SemesterAudit", entity_id=entity_id,
        status=None, refs=[])
    values.update(overrides)
    return ChangeEventV1.model_validate(values)


def _feed(events: list[ChangeEventV1]) -> ChangeFeedV1:
    return ChangeFeedV1(as_of=AT + timedelta(hours=1),
        since=AT - timedelta(hours=1), through=AT + timedelta(minutes=30),
        semester=SEMESTER, course_filter_stable_id=None,
        events=events, returned_count=len(events), limit=50)


def test_feed_rejects_event_before_since():
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(at=AT - timedelta(hours=1, microseconds=1))])


def test_feed_includes_event_exactly_since():
    assert len(_feed([_event(at=AT - timedelta(hours=1))]).events) == 1


def test_feed_rejects_event_exactly_through():
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(at=AT + timedelta(minutes=30))])


def test_feed_rejects_duplicate_event_id():
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(), _event()])


@pytest.mark.parametrize("foreign", [
    {"semester_id": "other"}, {"semester_stable_id": "OTHER"},
])
def test_feed_rejects_foreign_semester(foreign):
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(**foreign)])


def test_feed_rejects_wrong_instant_order():
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(at=AT-timedelta(minutes=1)), _event(kind="PLAN_ACCEPTED", at=AT)])


def test_feed_rejects_wrong_tie_order():
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(kind="PLAN_ACCEPTED"), _event(kind="AUDIT_IMPORTED")])
    with pytest.raises(ValidationError, match="INVALID_FEED_EVENTS"):
        _feed([_event(entity_id="z"), _event(entity_id="a")])


def test_feed_accepts_correct_ties_by_instant_kind_and_entity():
    same_instant_different_offset = AT.astimezone(timezone(timedelta(hours=2)))
    feed = _feed([_event(entity_id="a", at=AT),
        _event(entity_id="b", at=same_instant_different_offset),
        _event(kind="PLAN_ACCEPTED", entity_id="p", at=AT)])
    assert len(feed.events) == 3
