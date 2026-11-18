"""Synthetic material calendar acceptance on both supported schema revisions."""
from datetime import date, datetime, timezone
import hashlib
import pytest
from test_semester_calendar import calendar_data
from uniflow.application.semester_calendar import project_semester_calendar
from uniflow.application.academic_classification import rule_fingerprint
from uniflow.domain.enums import InformationStatus
from uniflow.infrastructure.database.models import (
    Material, MaterialArtifact, MaterialExpectationLink, MaterialReleaseRule, SourceArtifact,
)

@pytest.fixture
def drops(calendar_data):
    config, factory, cid, oid = calendar_data
    with factory.begin() as db:
        rules = [MaterialReleaseRule(id=rid, stable_id=rid, course_id=cid,
            payload_json='{"cadence":"WEEKLY","weekday":"'+weekday+'","material_kind":"'+kind+'"}',
            information_status=InformationStatus.CONFIRMED)
            for rid, weekday, kind in [('slides','MONDAY','SLIDES'), ('sheet','THURSDAY','EXERCISE')]]
        db.add_all(rules)
        db.add(MaterialReleaseRule(id='unknown', stable_id='unknown', course_id=oid,
            payload_json='{"cadence":"FORTNIGHTLY","material_kind":"NOTES"}',
            information_status=InformationStatus.CONFIRMED))
        for index in range(3):
            db.add(Material(id=f'm{index}', course_id=cid, title='Similar Slides.pdf', material_type='SLIDES'))
            db.add(SourceArtifact(id=f'a{index}', sha256=str(index)*64,
                original_filename=f'Slides{index}.pdf', original_source_path=f'synthetic/{index}',
                size_bytes=1, stored_path='synthetic', created_at=datetime(2026,10,15,23,30,tzinfo=timezone.utc)))
        db.flush()
        for index in range(3):
            db.add(MaterialArtifact(id=f'v{index}', material_id=f'm{index}', artifact_id=f'a{index}',
                version_number=1,is_current=True))
        db.add(Material(id='undated',course_id=cid,title='No imported artifact'))
        db.add(MaterialExpectationLink(id='explicit',material_id='m0',release_rule_id='slides',
            occurrence_date=date(2026,10,12),expectation_identity='slides@2026-10-12',
            rule_fingerprint=rule_fingerprint(rules[0]),is_active=True))
    return config, factory, cid, oid


def projection(drops, **kwargs):
    return project_semester_calendar(drops[0],'SYNTH',date(2026,10,12),date(2026,11,2),**kwargs)


def test_material_evidence_and_recurrence(drops):
    config, factory, cid, oid = drops
    before=hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    p=projection(drops)
    expected=[e for e in p.events if e.event_type=='MATERIAL_EXPECTED']
    assert [e.day for e in expected if e.material_kind=='SLIDES']==[date(2026,10,d) for d in (12,19,26)]+[date(2026,11,2)]
    assert [e.day for e in expected if e.material_kind=='EXERCISE']==[date(2026,10,d) for d in (15,22,29)]
    assert all(e.starts_at is None for e in expected)
    assert expected[0].status=='CONFIRMED_PRESENT' and expected[0].material_ids==('m0',)
    assert any(e.status=='UNCONFIRMED' for e in expected)
    actual=[e for e in p.events if e.event_type=='MATERIAL_OBSERVED']
    assert len(actual)==3 and len({e.event_id for e in actual})==3
    assert {e.artifact_id for e in actual}=={'a0','a1','a2'}
    assert all(e.day==date(2026,10,16) and e.date_basis=='ARTIFACT_RECORDED_AT' for e in actual)
    assert all('kein ursprüngliches Veröffentlichungsdatum' in e.notes and 'SHA-256:' in e.notes for e in actual)
    assert [e for e in actual if e.material_ids==('m0',)][0].expectation_ids==('slides@2026-10-12',)
    assert all(not e.expectation_ids for e in actual if e.material_ids!=('m0',))
    assert any('nicht datierbar' in d for d in p.diagnostics)
    assert any('undated' in d for d in p.diagnostics)
    assert all(e.course_id==cid for e in projection(drops,course_id=cid).events)
    assert not any(e.event_type.startswith('MATERIAL') for e in projection(drops,course_id=oid).events)
    assert before==hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    # The linked acquisition remains visible outside the expectation's displayed range.
    narrow=project_semester_calendar(config,'SYNTH',date(2026,10,16),date(2026,10,16))
    assert next(e for e in narrow.events if e.material_ids==('m0',)).expectation_ids==('slides@2026-10-12',)


def test_ambiguity_stale_inactive_and_inconsistent(drops):
    _,factory,_,_=drops
    with factory.begin() as db:
        rule=db.get(MaterialReleaseRule,'slides')
        db.add(MaterialExpectationLink(id='second',material_id='m1',release_rule_id='slides',
            occurrence_date=date(2026,10,12),expectation_identity='slides@2026-10-12',
            rule_fingerprint=rule_fingerprint(rule),is_active=True))
    e=next(e for e in projection(drops).events if e.event_id=='material-expected:slides@2026-10-12')
    assert e.status=='AMBIGUOUS' and e.material_ids==('m0','m1')
    with factory.begin() as db:
        db.get(MaterialExpectationLink,'explicit').rule_fingerprint='stale'
        db.get(MaterialExpectationLink,'second').occurrence_date=date(2026,10,19)
    e=next(e for e in projection(drops).events if e.event_id=='material-expected:slides@2026-10-12')
    assert e.status=='UNCONFIRMED' and not e.material_ids
    with factory.begin() as db:
        db.get(MaterialExpectationLink,'explicit').rule_fingerprint=rule_fingerprint(db.get(MaterialReleaseRule,'slides'))
        db.get(MaterialArtifact,'v0').deactivated_at=datetime(2026,10,20)
    e=next(e for e in projection(drops).events if e.event_id=='material-expected:slides@2026-10-12')
    assert e.status=='UNCONFIRMED'


def test_material_gui(drops,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.calendar import CalendarScreen
    from PySide6.QtCore import QDate
    config,factory,cid,_=drops
    before_db=hashlib.sha256(config.database_path.read_bytes()).hexdigest()

    def material_state():
        with factory() as db:
            return tuple(tuple(db.execute(model.__table__.select().order_by(model.id)))
                for model in (Material, MaterialArtifact, SourceArtifact,
                              MaterialExpectationLink, MaterialReleaseRule))

    before_materials=material_state()
    controller=GuiController(config,list_semesters(config)[0])
    screen=CalendarScreen(controller); qtbot.addWidget(screen); screen.resize(1200,900); screen.show()
    screen.anchor=date(2026,10,12); screen.activate(True)
    qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    screen.kind.setCurrentIndex(3)
    assert len(screen.displayed)==5 and all(e.event_type.startswith('MATERIAL_') for e in screen.displayed)
    original_projection=screen.projection
    original_events=screen.projection.events
    screen.agenda.setCurrentRow(0)
    linked=screen.displayed[0]
    assert linked.event_id=='material-expected:slides@2026-10-12'
    assert linked.material_ids==('m0',) and linked.expectation_ids==('slides@2026-10-12',)
    assert linked.source_table=='material_release_rules' and linked.source_id=='slides'
    assert linked.status=='CONFIRMED_PRESENT' and linked.information_status=='CONFIRMED'
    assert not screen.technical.isChecked()
    summary=screen.details.text()
    assert 'Math · Folie verknüpft' in summary and '12.10.2026' in summary
    assert 'Informationsstand: CONFIRMED' in summary
    assert all(value not in summary for value in
        ('Materialdatensätze:', 'm0', 'material_release_rules/slides', 'Links: explicit', cid))
    screen.technical.click()
    technical=screen.details.text()
    assert 'Materialdatensätze: m0' in technical and 'Quelle:' in technical
    assert 'Quelle: material_release_rules/slides' in technical
    assert 'Typ: MATERIAL_EXPECTED · Status: CONFIRMED_PRESENT · Informationsstand: CONFIRMED' in technical
    assert 'Datumsbasis: EXPECTED_RELEASE · Artefakt: keines' in technical
    assert 'Math ('+cid+')' in technical and 'Regel: slides;' in technical
    assert 'Links: explicit' in technical and linked.notes in technical
    screen.technical.click()
    assert not screen.technical.isChecked() and screen.details.text()==summary

    # The three identically titled materials must retain distinct full identities.
    for index in range(3):
        event=next(e for e in screen.displayed if e.event_id==f'material-observed:v{index}')
        assert event.material_ids==(f'm{index}',) and event.artifact_id==f'a{index}'
        assert event.source_id==f'v{index}' and event.source_table=='material_artifacts'
        assert event.status=='RECORDED' and event.information_status=='OBSERVED'
        assert event.expectation_ids==(('slides@2026-10-12',) if index==0 else ())
        screen.agenda.setCurrentRow(screen.displayed.index(event))
        summary=screen.details.text()
        assert 'Material erfasst/importiert' in summary and 'Similar Slides.pdf' in summary
        assert '16.10.2026' in summary
        assert all(value not in summary for value in
            (f'm{index}', f'a{index}', f'material_artifacts/v{index}', str(index)*64))
        screen.technical.click()
        lines=screen.details.text().splitlines()
        assert f'Materialtyp: SLIDES · Materialdatensätze: m{index}' in lines
        assert f'Datumsbasis: ARTIFACT_RECORDED_AT · Artefakt: a{index}' in lines
        assert f'Quelle: material_artifacts/v{index}' in lines
        assert 'Typ: MATERIAL_OBSERVED · Status: RECORDED · Informationsstand: OBSERVED' in lines
        assert event.notes in screen.details.text()
        assert f'SHA-256: {str(index)*64};' in screen.details.text()
        assert f'Datei: Slides{index}.pdf; Herkunft: synthetic/{index};' in screen.details.text()
        assert ('Zuordnung: slides@2026-10-12.' if index==0 else
                'Zuordnung: ungeklärt (kein belastbarer Erwartungsbezug).') in screen.details.text()
        screen.technical.click()
        assert screen.details.text()==summary
    assert screen.projection is original_projection and screen.projection.events==original_events
    assert material_state()==before_materials
    assert hashlib.sha256(config.database_path.read_bytes()).hexdigest()==before_db
    screen.kind.setCurrentIndex(1); assert all(not e.event_type.startswith('MATERIAL_') for e in screen.displayed)
    screen.kind.setCurrentIndex(2); assert all(e.event_type=='DEADLINE' for e in screen.displayed)
    screen.kind.setCurrentIndex(3); screen.mode.setCurrentIndex(1)
    qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    screen.month.setSelectedDate(QDate(2026,10,16))
    assert len(screen.displayed)==3
    screen.agenda.setCurrentRow(1)
    assert screen.displayed[1].event_id=='material-observed:v1'
    assert not screen.technical.isChecked()
    screen.technical.click()
    assert 'ungeklärt' in screen.details.text()
    assert 'Quelle: material_artifacts/v1' in screen.details.text()
    assert 'Materialdatensätze: m1' in screen.details.text()
    screen.technical.click()
    assert 'material_artifacts/v1' not in screen.details.text()
    qtbot.wait(150)
    import os
    if os.environ.get('MATERIAL_QA_SCREENSHOT'):
        screen.grab().save(os.environ['MATERIAL_QA_SCREENSHOT']+'.png')
    screen.move(1); screen.move(-1); screen.course.setCurrentIndex(screen.course.findData(cid))
    qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    current=screen.projection
    screen.completed('semester_calendar',screen.generation-1,('stale',None)); assert screen.projection is current
    screen.activate(False); controller.runner.pool.waitForDone(10000)
    assert material_state()==before_materials
    assert hashlib.sha256(config.database_path.read_bytes()).hexdigest()==before_db


def test_known_release_time_and_date_only(drops):
    from zoneinfo import ZoneInfo
    from uniflow.application.material_calendar import project_material_events
    from uniflow.infrastructure.database.models import Semester, Course
    _, factory, cid, _ = drops
    with factory.begin() as db:
        db.get(MaterialReleaseRule, 'slides').payload_json = '{"cadence":"WEEKLY","weekday":"MONDAY","time":"09:00","material_kind":"SLIDES"}'
    with factory() as db:
        semester = db.query(Semester).filter_by(stable_id='SYNTH').one()
        courses = [db.get(Course, cid)]
        def state(day, hour, rule):
            events, _ = project_material_events(db, semester, courses, day, day,
                datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo('Europe/Berlin')))
            return next(e.status for e in events if e.event_id=='material-expected:'+rule+'@'+day.isoformat())
        assert state(date(2026,10,19),8,'slides')=='EXPECTED'
        assert state(date(2026,10,19),18,'slides')=='UNCONFIRMED'
        assert state(date(2026,10,22),23,'sheet')=='EXPECTED'
