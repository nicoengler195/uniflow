from datetime import date, datetime, timezone

from uniflow.application.academic_classification import rule_fingerprint
from uniflow.application.initializer import initialize_project
from uniflow.application.weekly_drops_read_models import get_weekly_drops
from uniflow.domain.enums import InformationStatus, MaterialState, SemesterState
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import (Course, Material, MaterialArtifact,
    MaterialExpectationLink, MaterialReleaseRule, Semester, SourceArtifact)


def test_weekly_drops_keep_provenance_and_import_separate(tmp_path):
    config = load_config(project_root=tmp_path)
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    with factory.begin() as db:
        db.add(Semester(id="s", stable_id="S", name="Semester", state=SemesterState.ACTIVE,
            starts_on=date(2026, 10, 19), ends_on=date(2026, 11, 1), timezone="Europe/Berlin"))
        db.add_all([Course(id="parent", semester_id="s", stable_id="P", name="Parent"),
            Course(id="child", semester_id="s", stable_id="C", name="Child", parent_course_id="parent")])
        db.flush()
        monday = MaterialReleaseRule(id="r1", stable_id="R1", course_id="child",
            payload_json='{"cadence":"WEEKLY","weekday":"MONDAY","time":"09:00","material_kind":"SLIDES"}',
            information_status=InformationStatus.CONFIRMED)
        thursday = MaterialReleaseRule(id="r2", stable_id="R2", course_id="child",
            payload_json='{"cadence":"WEEKLY","weekday":"THURSDAY","material_kind":"EXERCISE"}',
            information_status=InformationStatus.INFERRED)
        unknown = MaterialReleaseRule(id="r3", stable_id="R3", course_id="child",
            payload_json='{"cadence":"WEEKLY","material_kind":"NOTES"}',
            information_status=InformationStatus.UNKNOWN)
        db.add_all([monday, thursday, unknown])
        db.add(Material(id="m", course_id="child", title="Slides", lifecycle_state=MaterialState.DOWNLOADED))
        db.add(SourceArtifact(id="a", sha256="a" * 64, original_filename="R1.pdf",
            original_source_path="synthetic", extension=".pdf", mime_type="application/pdf",
            size_bytes=1, stored_path="synthetic", created_at=now))
        db.flush()
        db.add(MaterialArtifact(id="v", material_id="m", artifact_id="a", version_number=1,
            is_current=True, linked_at=now))
        db.add(MaterialExpectationLink(id="link", material_id="m", release_rule_id="r1",
            occurrence_date=date(2026, 10, 19), expectation_identity="R1@2026-10-19",
            rule_fingerprint=rule_fingerprint(monday), is_active=True, created_at=now))
    week = get_weekly_drops(config, "S", date(2026, 10, 19), as_of=now)
    by_rule = {item.rule_stable_id: item for item in week.items if item.rule_stable_id}
    assert by_rule["R1"].release_time.hour == 9
    assert by_rule["R1"].material_id == "m"
    assert by_rule["R1"].parent_course_id == "parent"
    assert by_rule["R2"].release_date == date(2026, 10, 22)
    assert by_rule["R2"].release_time is None
    assert by_rule["R2"].material_id is None
    assert by_rule["R3"].release_date is None
    assert by_rule["R3"].release_state == "UNRESOLVED_RULE"
    assert any(x.release_state == "NO_RULE" and x.course_id == "parent" for x in week.items)
    with factory.begin() as db:
        db.query(MaterialExpectationLink).delete()
    unlinked = get_weekly_drops(config, "S", date(2026, 10, 19), as_of=now)
    assert next(x for x in unlinked.items if x.rule_stable_id == "R1").material_id is None
    next_week = get_weekly_drops(config, "S", date(2026, 10, 26), as_of=now)
    assert next(x for x in next_week.items if x.rule_stable_id == "R1").release_date == date(2026, 10, 26)
    assert next_week.timezone == "Europe/Berlin"  # DST changed on 25 October.
    beyond = get_weekly_drops(config, "S", date(2026, 11, 2), as_of=now)
    assert not any(x.release_state == "EXPECTED" for x in beyond.items)
    factory.kw["bind"].dispose()
