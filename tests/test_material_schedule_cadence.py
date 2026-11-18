"""Synthetic regressions for material rules following authoritative schedule occurrences."""
from datetime import date, datetime, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from test_planning_calendar import entry, exception
from uniflow.application.audit_import import import_audit
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import get_engine, create_session_factory
from uniflow.domain.enums import SemesterState
import shutil
from uniflow.application.material_manifest import generate_material_manifest
from uniflow.application.semester_calendar import project_semester_calendar
from uniflow.domain.enums import InformationStatus
from uniflow.infrastructure.database.models import MaterialReleaseRule, Course, Semester

START = date(2026, 10, 12)
END = date(2026, 11, 2)
NOW = datetime(2026, 10, 20, 12, tzinfo=ZoneInfo('Europe/Berlin'))


@pytest.fixture(scope='module')
def seed_database(tmp_path_factory):
    config = load_config(project_root=tmp_path_factory.mktemp('cadence-seed'))
    initialize_project(config)
    factory = create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        semester = Semester(stable_id='SYNTH', name='Synthetic', state=SemesterState.ACTIVE,
                            timezone='Europe/Berlin')
        course = Course(stable_id='MATH_SYNTH', name='Math', semester=semester,
                        information_status=InformationStatus.CONFIRMED)
        db.add(course)
    return config.database_path


@pytest.fixture
def setup(seed_database, tmp_path):
    config = load_config(project_root=tmp_path/'project')
    for directory in config.required_directories:
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_database, config.database_path)
    engine = get_engine(config.database_path)
    factory = create_session_factory(engine)
    with factory() as db:
        cid = db.query(Course).filter_by(stable_id='MATH_SYNTH').one().id
    yield config, factory, cid
    engine.dispose()


def rule(factory, cid, stable_id='R', status='INFERRED', **changes):
    payload = dict(cadence='EACH_SCHEDULE_OCCURRENCE', schedule_stable_id='MON',
                   material_kind='LECTURE_SLIDES', time=None)
    payload.update(changes)
    with factory.begin() as db:
        db.add(MaterialReleaseRule(stable_id=stable_id, course_id=cid,
            information_status=InformationStatus(status), payload_json=json.dumps(payload)))


def manifest(config, start=START, end=END, now=NOW):
    return generate_material_manifest(config, 'SYNTH', start, end, now)


def dates(config):
    return [e.release_date for e in manifest(config).expectations]


def test_normal_schedule_bounds_and_occurrence_start_time(setup):
    config, factory, cid = setup
    entry(factory, cid, valid_from='2026-10-19', valid_until='2026-10-26')
    # A stray rule weekday cannot override its linked schedule's dates.
    rule(factory, cid, weekday='FRIDAY')
    m = manifest(config)
    assert [e.release_date for e in m.expectations] == [date(2026,10,19), date(2026,10,26)]
    assert all(e.release_time.hour == 10 and e.information_status == 'INFERRED' for e in m.expectations)
    assert [e.state for e in m.expectations] == ['NOT_LINKED_IN_UNIFLOW', 'UPCOMING']
    assert not m.unresolved_rules
    assert not manifest(config, date(2026,10,13), date(2026,10,18)).expectations


@pytest.mark.parametrize('parity,expected', [('ODD',[19]), ('EVEN',[12,26])])
def test_iso_week_parity(setup, parity, expected):
    config, factory, cid = setup
    entry(factory, cid, calendar_week_parity=parity)
    rule(factory, cid)
    assert [e.release_date.day for e in manifest(config, START, date(2026,10,31)).expectations] == expected


def test_cancellation_and_testate_replacement_are_not_lectures(setup):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid)
    exception(factory, cid, 'CANCEL', 'CANCELLATION')
    exception(factory, cid, 'TEST', 'CANCELLATION', date(2026,10,19),
              title='Lecture replaced by exercise testate')
    # The separately documented test has no reference to the cancelled lecture.
    exception(factory, cid, 'TEST_EVENT', 'ADDITION', date(2026,10,19),
              replaces_schedule_id=None, start_time='16:00', end_time='17:00', title='Testate')
    assert dates(config) == [date(2026,10,26), date(2026,11,2)]


@pytest.mark.parametrize('start,end', [('10:00','12:00'), ('14:00','16:00')])
def test_room_change_and_time_replacement_keep_expectation(setup, start, end):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid, time='18:00')
    exception(factory, cid, 'MOVE', 'REPLACEMENT', start_time=start, end_time=end,
              title='Lecture in another room')
    m = manifest(config)
    assert len(m.expectations) == 4
    assert m.expectations[0].release_date == START
    assert m.expectations[0].release_time.hour == int(start[:2])
    assert len({e.id for e in m.expectations}) == 4


def test_cross_day_move_uses_existing_cancellation_and_addition_semantics(setup):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid)
    exception(factory, cid, 'CANCEL', 'CANCELLATION')
    exception(factory, cid, 'NEW', 'ADDITION', date(2026,10,13),
              replaces_schedule_id=None, start_time='10:00', end_time='12:00')
    # ADDITION has no structured link to MON. Do not guess a material association.
    assert START not in dates(config)
    assert date(2026,10,13) not in dates(config)


@pytest.mark.parametrize('status', ['CONFIRMED','INFERRED'])
def test_expected_unconfirmed_and_no_automatic_evidence_or_progress(setup, status):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid, status=status)
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    with factory() as db:
        tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")).scalars().all()
        counts_before = {t:db.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() for t in tables}
    p = project_semester_calendar(config, 'SYNTH', START, END, as_of=NOW)
    events = [e for e in p.events if e.event_type == 'MATERIAL_EXPECTED']
    assert [e.status for e in events] == ['UNCONFIRMED','UNCONFIRMED','EXPECTED','EXPECTED']
    assert all(e.information_status == status and not e.material_ids for e in events)
    assert all(e.expected_time.hour == 10 for e in events)
    assert not any(e.event_type == 'MATERIAL_OBSERVED' for e in p.events)
    assert all('Keine Aussage \u00fcber Nichtver\u00f6ffentlichung' in e.notes for e in events)
    with factory() as db:
        assert counts_before == {t:db.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() for t in tables}
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()


def test_known_time_changes_forecast_state_only(setup):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid, status='CONFIRMED', time='18:00')
    for hour, state in [(9,'UPCOMING'),(11,'NOT_LINKED_IN_UNIFLOW')]:
        m = manifest(config, START, START, datetime(2026,10,12,hour,tzinfo=ZoneInfo('Europe/Berlin')))
        assert m.expectations[0].state == state


@pytest.mark.parametrize('change', [dict(status='UNKNOWN'),dict(schedule_stable_id=None),
                                  dict(schedule_stable_id='MISSING')])
def test_unresolved_rules_stay_unresolved(setup, change):
    config, factory, cid = setup
    entry(factory, cid)
    rule(factory, cid, **change)
    m = manifest(config)
    assert not m.expectations and len(m.unresolved_rules) == 1


def test_same_course_link_and_calendar_diagnostics_fail_closed(setup):
    config, factory, cid = setup
    entry(factory, cid)
    with factory.begin() as db:
        other = Course(stable_id='OTHER', name='Other', semester_id=db.get(Course,cid).semester_id)
        db.add(other); db.flush(); oid=other.id
    rule(factory, oid)
    assert not manifest(config).expectations
    rule(factory, cid, stable_id='GOOD')
    exception(factory, cid, 'UNSUPPORTED', 'SOMETHING_ELSE')
    m = manifest(config)
    assert [e.release_date for e in m.expectations] == [date(2026,10,19), date(2026,10,26), date(2026,11,2)]
    assert any('UNSUPPORTED_SCHEDULE_EXCEPTION_TYPE' in r.reason for r in m.unresolved_rules)


def test_weekly_remains_independent_of_cancellations(setup):
    config, factory, cid = setup
    entry(factory, cid)
    exception(factory, cid, 'CANCEL', 'CANCELLATION')
    rule(factory, cid, cadence='WEEKLY', weekday='MONDAY')
    assert dates(config) == [date(2026,10,12), date(2026,10,19),date(2026,10,26),date(2026,11,2)]


def test_import_idempotency_for_schedule_rule(setup, tmp_path):
    config, factory, _ = setup
    source = tmp_path/'synthetic.yaml'
    document = dict(schema_version='1.0',kind='semester_audit',audit_id='TEST',semester='IMPORT',
        semester_name='Synthetic import',timezone='Europe/Berlin',courses=[dict(stable_id='IC',name='Synthetic',
        schedules=[dict(stable_id='IS',activity_type='LECTURE',weekday='MONDAY',start_time='10:00',
                        end_time='12:00',information_status='CONFIRMED')],
        material_release_rules=[dict(stable_id='IR',material_kind='LECTURE_SLIDES',
            cadence='EACH_SCHEDULE_OCCURRENCE',schedule_stable_id='IS',information_status='INFERRED')])])
    source.write_text(json.dumps(document),encoding='utf-8')
    original=source.read_bytes()
    assert import_audit(source,config).status == 'IMPORTED'
    first=generate_material_manifest(config,'IMPORT',START,END,NOW)
    assert len(first.expectations)==4
    assert import_audit(source,config).status == 'ALREADY_IMPORTED'
    assert first==generate_material_manifest(config,'IMPORT',START,END,NOW)
    assert source.read_bytes()==original


def test_unrelated_diagnostic_preserves_range_and_day_identities(setup):
    config, factory, cid = setup
    entry(factory, cid)
    entry(factory, cid, stable_id='THU', weekday='THURSDAY')
    rule(factory, cid)
    exception(factory, cid, 'OTHER_ISSUE', 'UNSUPPORTED', date(2026,10,22), replaces_schedule_id='THU')
    broad = manifest(config)
    assert len(broad.expectations) == 4 and not broad.unresolved_rules
    assert broad.expectations == tuple(e for day in (START,date(2026,10,19),date(2026,10,26),END)
                                      for e in manifest(config,day,day).expectations)
    # Relevant uncertainty suppresses only its affected occurrence, even in a broad range.
    exception(factory, cid, 'LINKED_ISSUE', 'UNSUPPORTED', date(2026,10,19))
    broad = manifest(config)
    assert [e.release_date for e in broad.expectations] == [START,date(2026,10,26),END]
    assert len(broad.unresolved_rules) == 1
    assert broad.expectations == tuple(e for day in (START,date(2026,10,19),date(2026,10,26),END)
                                      for e in manifest(config,day,day).expectations)
