"""Synthetic acceptance and real Qt calendar checks; databases live in pytest temp dirs."""
from datetime import date, datetime
import hashlib
import pytest
from alembic import command
from alembic.config import Config
from pathlib import Path
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import get_engine, create_session_factory
from uniflow.infrastructure.database.models import Semester, Course, Deadline
from uniflow.domain.enums import SemesterState, InformationStatus, DeadlineStatus
from uniflow.application.semester_calendar import project_semester_calendar
from test_planning_calendar import entry, exception

@pytest.fixture(params=['0017_audit_resolution','0019_scoring_mastery'])
def calendar_data(tmp_path, request):
    config=load_config(project_root=tmp_path/'synthetic')
    for directory in config.required_directories: directory.mkdir(parents=True,exist_ok=True)
    migration=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    migration.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'src/uniflow/infrastructure/database/migrations'))
    migration.set_main_option('sqlalchemy.url',f'sqlite:///{config.database_path.as_posix()}')
    command.upgrade(migration,request.param)
    factory=create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        s=Semester(stable_id='SYNTH',name='Synthetic',state=SemesterState.ACTIVE,starts_on=date(2026,10,12),ends_on=date(2026,11,2),timezone='Europe/Berlin')
        c=Course(stable_id='C',name='Math',semester=s); other=Course(stable_id='OTHER',name='Other',semester=s)
        db.add_all([c,other]); db.flush(); cid=c.id; oid=other.id
        for title,stamp,dateonly in [('Date',datetime(2026,10,13),True),('Timed',datetime(2026,10,14,23,59),False)]:
            db.add(Deadline(course_id=cid,title=title,due_at=stamp,is_date_only=dateonly,status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
    entry(factory,cid); entry(factory,oid,stable_id='OTHER_MON')
    exception(factory,cid,'CANCEL','CANCELLATION')
    exception(factory,cid,'MOVE','REPLACEMENT',date(2026,10,19),start_time='14:00',end_time='16:00')
    return config,factory,cid,oid

def test_projection(calendar_data):
    config,factory,cid,oid=calendar_data
    before=hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    p=project_semester_calendar(config,'SYNTH',date(2026,10,1),date(2026,11,30),cid)
    assert all(e.course_id==cid and date(2026,10,12)<=e.day<=date(2026,11,2) for e in p.events)
    assert len([e for e in p.events if e.event_type=='RECURRING'])==2
    assert len([e for e in p.events if e.event_type=='CANCELLATION'])==1
    moved=[e for e in p.events if e.event_type=='REPLACEMENT']; assert len(moved)==1 and moved[0].starts_at.hour==14
    assert next(e for e in p.events if e.title=='Date').starts_at is None
    assert next(e for e in p.events if e.title=='Timed').starts_at.hour==23
    assert len({e.event_id for e in p.events})==len(p.events)
    assert p==project_semester_calendar(config,'SYNTH',date(2026,10,1),date(2026,11,30),cid)
    assert project_semester_calendar(config,'SYNTH',date(2027,1,1),date(2027,1,31)).events==()
    assert len(project_semester_calendar(config,'SYNTH',date(2026,10,1),date(2026,11,30)).events)>len(p.events)
    assert before==hashlib.sha256(config.database_path.read_bytes()).hexdigest()

def test_real_widgets(calendar_data,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.calendar import CalendarScreen
    config,_,cid,_=calendar_data
    controller=GuiController(config,list_semesters(config)[0]); screen=CalendarScreen(controller)
    qtbot.addWidget(screen); screen.resize(1100,800); screen.show()
    screen.anchor=date(2026,10,12); screen.activate(True)
    qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    assert screen.agenda.count()>=3
    screen.agenda.setCurrentRow(0); assert 'Quelle:' in screen.details.text()
    screen.mode.setCurrentIndex(1); qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    screen.month.setSelectedDate(__import__('PySide6.QtCore',fromlist=['QDate']).QDate(2026,10,14))
    assert any(e.title=='Timed' for e in screen.displayed)
    screen.mode.setCurrentIndex(0)
    screen.move(1); screen.move(-1)
    screen.course.setCurrentIndex(screen.course.findData(cid))
    qtbot.waitUntil(lambda: screen.projection is not None,timeout=10000)
    assert all(e.course_id==cid for e in screen.projection.events)
    generation=screen.generation
    screen.completed('semester_calendar',generation-1,('stale',None)); assert screen.projection is not None
    screen.activate(False); screen.completed('semester_calendar',generation,('stale',None)); assert not screen.active
    controller.runner.pool.waitForDone(10000)


def test_excluded_dst_and_bounds(calendar_data):
    config,factory,cid,oid=calendar_data
    entry(factory,oid,stable_id='DST',weekday='SUNDAY',start_time='02:30',end_time='03:30')
    assert project_semester_calendar(config,'SYNTH',date(2026,10,19),date(2026,10,19),cid).events
    assert project_semester_calendar(config,'SYNTH',date(2026,10,19),date(2026,10,19)).events
    with pytest.raises(ValueError,match='AMBIGUOUS_ACADEMIC'):
        project_semester_calendar(config,'SYNTH',date(2026,10,25),date(2026,10,25))

def test_main_navigation_and_close(calendar_data,qtbot):
    from uniflow.gui.app import MainWindow
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.routes import Route
    config,_,_,_=calendar_data
    controller=GuiController(config,list_semesters(config)[0]); controller.refresh_home=lambda: None
    window=MainWindow(controller); qtbot.addWidget(window); window.show()
    window.calendar.anchor=date(2026,10,12); window.buttons[Route.CALENDAR].click()
    qtbot.waitUntil(lambda: window.calendar.projection is not None,timeout=10000)
    assert window.calendar.isVisible() and not window.week.isVisible()
    window.calendar.agenda.setCurrentRow(0); assert 'Quelle:' in window.calendar.details.text()
    import os
    if os.environ.get('CALENDAR_QA_SCREENSHOT'):
        window.grab().save(os.environ['CALENDAR_QA_SCREENSHOT']+'_'+config.database_path.parent.parent.parent.name+'.png')
    window.calendar.reload(); generation=window.calendar.generation
    window.close(); assert not window.calendar.active and window.calendar.generation>generation
    controller.runner.pool.waitForDone(10000)

def test_parity_validity_and_year(calendar_data):
    config,factory,cid,_=calendar_data
    entry(factory,cid,stable_id='BOUNDED',weekday='TUESDAY',valid_from='2026-10-20',valid_until='2026-10-27',calendar_week_parity='ODD')
    p=project_semester_calendar(config,'SYNTH',date(2026,10,1),date(2026,11,30),cid)
    bounded=[e for e in p.events if e.event_type=='RECURRING' and e.day.weekday()==1]
    assert len(bounded)==1 and bounded[0].day==date(2026,10,20)
    with factory.begin() as db:
        s=db.query(Semester).filter_by(stable_id='SYNTH').one(); s.starts_on=date(2026,12,28); s.ends_on=date(2027,1,11)
    p=project_semester_calendar(config,'SYNTH',date(2026,12,28),date(2027,1,11),cid)
    assert [e.day for e in p.events]==[date(2026,12,28),date(2027,1,4),date(2027,1,11)]
