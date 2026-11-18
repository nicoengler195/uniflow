"""Explicit bounded series, synthetic data only."""
import copy
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from uniflow.application.audit_import import import_audit
from uniflow.application.initializer import initialize_project
from uniflow.application.sheet_deadlines import compile_sheet_deadlines
from uniflow.application.learning_needs import project_learning_needs
from uniflow.application.semester_calendar import project_semester_calendar
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory, get_engine
from uniflow.infrastructure.database.models import Deadline
from uniflow.schemas.semester_audit import SemesterAuditV1


def document():
    courses = []
    for cid, release, weekday, due_weekday, clock in [
        ('M', '2026-11-12', 3, 2, '23:59'), ('P', '2026-11-13', 4, 4, '10:00')]:
        start = date.fromisoformat(release)
        from datetime import timedelta
        instances = [dict(number=n, release_date=str(start+timedelta(weeks=n-5)),
                          basis='EVIDENCED' if n == 5 else 'BOUNDED_PLANNING', evidence='Synthetic evidence')
                     for n in (5, 6, 7)]
        courses.append(dict(stable_id=cid, name=cid, requirements=[dict(stable_id=cid+'-series',
            requirement_type='SHEET_SUBMISSION_SERIES_V1', information_status='CONFIRMED',
            human_summary='Bounded synthetic series', parameters=dict(release_weekday=weekday,
            release_time='08:00' if cid == 'P' else None, due_weekday=due_weekday,
            due_time=clock, instances=instances))]))
    return dict(schema_version='1.0', kind='semester_audit', audit_id='SHEET', semester='S',
                semester_name='Synthetic', timezone='Europe/Berlin', courses=courses)


def compile_doc(doc):
    return compile_sheet_deadlines(SemesterAuditV1.model_validate(doc))


def test_exact_dates_missing_and_cancelled_instances():
    doc = document()
    rows = compile_doc(doc)
    assert [(str(d.due_date), str(d.due_time)) for _, d in rows] == [
        ('2026-11-18','23:59:00'),('2026-11-25','23:59:00'),('2026-12-02','23:59:00'),
        ('2026-11-20','10:00:00'),('2026-11-27','10:00:00'),('2026-12-04','10:00:00')]
    instances = doc['courses'][0]['requirements'][0]['parameters']['instances']
    instances.pop(1)
    instances[-1]['cancelled'] = True
    assert len(compile_doc(doc)) == 4
    assert all(d.information_status.value == 'INFERRED' for _, d in rows)
    assert rows == compile_doc(document())


@pytest.mark.parametrize('change', ['duplicate','weekday','unknown','completion','reference','offset'])
def test_invalid_contract_fails_closed(change):
    doc = document(); req = doc['courses'][0]['requirements'][0]; p = req['parameters']
    if change == 'duplicate': p['instances'].append(copy.deepcopy(p['instances'][0]))
    if change == 'weekday': p['instances'][0]['release_date'] = '2026-11-13'
    if change == 'unknown': req['information_status'] = 'UNKNOWN'
    if change == 'completion': p['instances'][0]['completion'] = 'MET'
    if change == 'reference': p['instances'][0]['existing_deadline_id'] = 'other-course-or-missing'
    if change == 'offset': p['due_time'] = '23:59+01:00'
    with pytest.raises(ValueError): compile_doc(doc)


def test_explicit_existing_deadline_reference_and_collision():
    doc = document(); course = doc['courses'][0]
    course['deadlines'] = [dict(stable_id='M5', title='Existing sheet 5', due_date='2026-11-18',
                               due_time='23:59', information_status='CONFIRMED')]
    with pytest.raises(ValueError, match='collision'): compile_doc(doc)
    course['requirements'][0]['parameters']['instances'][0]['existing_deadline_id'] = 'M5'
    assert len(compile_doc(doc)) == 5
    assert course['deadlines'][0]['information_status'] == 'CONFIRMED'


def test_import_idempotence_calendar_priority_and_unknown_completion(tmp_path):
    config = load_config(project_root=tmp_path/'project'); initialize_project(config)
    source = tmp_path/'audit.json'; source.write_text(json.dumps(document()))
    assert import_audit(source,config).status == 'IMPORTED'
    assert import_audit(source,config).status == 'ALREADY_IMPORTED'
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:
        rows = db.scalars(select(Deadline)).all()
        assert len(rows) == 6
        assert all(r.status.value == 'UNKNOWN' and r.information_status.value == 'INFERRED' for r in rows)
        ids = {r.id for r in rows}
    now = datetime(2026,11,15,12,tzinfo=ZoneInfo('Europe/Berlin'))
    needs = project_learning_needs(config,'S',now)
    assert len(needs.needs) == 6
    assert all(n.priority == 'NEEDS_CLARIFICATION' for n in needs.needs)
    assert all(any('deadlines/'+i in n.sources for i in ids) for n in needs.needs)
    calendar = project_semester_calendar(config,'S',date(2026,11,15),date(2026,12,5),as_of=now)
    events = [e for e in calendar.events if e.source_table == 'deadlines']
    assert len(events) == 6
    assert {e.day for e in events} == {date(2026,11,18),date(2026,11,25),date(2026,12,2),
                                    date(2026,11,20),date(2026,11,27),date(2026,12,4)}


@pytest.mark.parametrize('change', ['cancel', 'remove', 'type'])
def test_persisted_series_withdrawal_fails_closed(tmp_path, change):
    config = load_config(project_root=tmp_path/'project'); initialize_project(config)
    source = tmp_path/'audit.json'; doc = document()
    source.write_text(json.dumps(doc)); assert import_audit(source,config).status == 'IMPORTED'
    req = doc['courses'][0]['requirements'][0]
    if change == 'cancel': req['parameters']['instances'][0]['cancelled'] = True
    if change == 'remove': req['parameters']['instances'].pop(0)
    if change == 'type': req['requirement_type'] = 'LEGACY_FREE_PARAMETERS'
    source.write_text(json.dumps(doc))
    result = import_audit(source,config,dry_run=True)
    assert result.status == 'CONFLICT'
    assert any('withdrawal contract' in a.detail for a in result.actions)


def test_persisted_single_deadline_collision_without_document_reference(tmp_path):
    config = load_config(project_root=tmp_path/'project'); initialize_project(config)
    source = tmp_path/'audit.json'; doc = document()
    first = copy.deepcopy(doc)
    for course in first['courses']: course['requirements'] = []
    first['courses'][0]['deadlines'] = [dict(stable_id='old-single',title='Old sheet',
        due_date='2026-11-18',due_time='23:59',information_status='CONFIRMED')]
    source.write_text(json.dumps(first)); assert import_audit(source,config).status == 'IMPORTED'
    source.write_text(json.dumps(doc))
    result = import_audit(source,config,dry_run=True)
    assert result.status == 'CONFLICT'
    assert any('persisted same-course deadline collision' in a.detail for a in result.actions)


def test_omitting_entire_requirement_is_additive_noop(tmp_path):
    config = load_config(project_root=tmp_path/'project'); initialize_project(config)
    source = tmp_path/'audit.json'; doc = document()
    source.write_text(json.dumps(doc)); assert import_audit(source,config).status == 'IMPORTED'
    for course in doc['courses']: course['requirements'] = []
    source.write_text(json.dumps(doc)); assert import_audit(source,config).status == 'IMPORTED'
    factory = create_session_factory(get_engine(config.database_path))
    with factory() as db:
        assert len(db.scalars(select(Deadline)).all()) == 6
