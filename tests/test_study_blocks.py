"""B1 acceptance: only synthetic SQLite files, real Qt/TaskRunner."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import pytest
from sqlalchemy import text, event
from uniflow.application.study_blocks import *
from uniflow.application.learning_needs import project_learning_needs
from uniflow.application.semester_calendar import project_semester_calendar
from uniflow.application.initializer import initialize_project
from uniflow.infrastructure.config import load_config
from uniflow.infrastructure.database.engine import create_session_factory,get_engine
from uniflow.infrastructure.database.models import Semester,Course,Deadline
from uniflow.domain.enums import InformationStatus,DeadlineStatus
from test_planning_calendar import entry,exception

ZONE=ZoneInfo('Europe/Berlin')
NOW=datetime(2026,11,5,12,tzinfo=ZONE)
MON=date(2026,11,9)

@pytest.fixture
def data(tmp_path):
    config=load_config(project_root=tmp_path/'synthetic-b1');initialize_project(config)
    factory=create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        s=Semester(stable_id='S',name='Synthetic B1',starts_on=date(2026,10,1),ends_on=date(2027,3,31))
        courses=[Course(stable_id='P',name='Physik I',semester=s),Course(stable_id='J',name='Java/OOP',semester=s)]
        db.add_all(courses);db.flush();ids=[c.id for c in courses]
        for c in courses:db.add(Deadline(course_id=c.id,title=c.name,due_at=datetime(2026,11,7 if c.stable_id=='P' else 12),is_date_only=True,status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
    needs=project_learning_needs(config,'S',NOW).needs
    durations={n.recommendation_id:60 for n in needs}
    return config,factory,ids,durations


def window(config,day=MON,begin=time(9),end=time(11)):
    return confirm_availability(config,'S',day,begin,end,confirmed=True,actor='synthetic-user',as_of=NOW)


def preview(data):
    return preview_study_blocks(data[0],'S',NOW,data[3])


def apply(data,p,request='r',selected=None):
    selected=selected or [x.need_id for x in p.proposals]
    return confirm_study_blocks(data[0],p,selected,request_id=request,confirmed=True,actor='synthetic-user',expected_selection_fingerprint=selection_fingerprint(p,selected))


def counts(data):
    with data[1]() as db:return tuple(db.scalar(text('SELECT count(*) FROM '+t)) for t in TABLES)


def test_no_availability_or_duration_and_no_preview_write(data):
    assert not preview(data).proposals
    window(data[0]);before=hashlib.sha256(data[0].database_path.read_bytes()).hexdigest()
    assert not preview_study_blocks(data[0],'S',NOW).proposals
    p=preview(data);assert len(p.proposals)==2 and p==preview(data)
    assert p.proposals[0].ends_at==p.proposals[1].starts_at
    assert before==hashlib.sha256(data[0].database_path.read_bytes()).hexdigest()
    assert counts(data)==(1,0,0,0)


def test_selective_confirmation_and_calendar(data):
    window(data[0]);p=preview(data);ids=apply(data,p,selected=[p.proposals[0].need_id]);assert len(ids)==1
    assert counts(data)==(1,1,1,1)
    events=project_semester_calendar(data[0],'S',MON,MON).events
    fixed=[e for e in events if e.event_type=='STUDY_BLOCK_CONFIRMED'];assert len(fixed)==1 and fixed[0].status=='CONFIRMED'
    assert preview(data).proposals[0].starts_at==p.proposals[0].ends_at


def test_two_audits_replay_and_payload_mismatch(data):
    window(data[0]);p=preview(data);ids=apply(data,p);assert len(ids)==2
    assert counts(data)==(1,2,1,2);assert apply(data,p)==ids
    assert json.loads(request_status(data[0],'r')['result_json'])==ids
    with pytest.raises(ValueError,match='PAYLOAD_MISMATCH'):apply(data,p,selected=[p.proposals[0].need_id])
    assert request_status(data[0],'unknown') is None
    with data[1].begin() as db:
        with pytest.raises(Exception,match='immutable'):db.execute(text('UPDATE study_block_audit_events SET event_type=event_type'))


def test_stale_source_and_precommit_conflict(data):
    window(data[0]);p=preview(data)
    entry(data[1],data[2][0],start_time='09:00',end_time='10:00')
    with pytest.raises(ValueError,match='STALE'):apply(data,p)
    assert counts(data)==(1,0,0,0)
    p=preview(data);assert len(p.proposals)==1 and datetime.fromisoformat(p.proposals[0].starts_at).astimezone(ZONE).hour==10


def test_academic_cancellation_and_replacement(data):
    window(data[0]);entry(data[1],data[2][0],start_time='09:00',end_time='11:00')
    assert not preview(data).proposals
    exception(data[1],data[2][0],'CANCEL','CANCELLATION',MON)
    assert len(preview(data).proposals)==2


def test_replacement_blocks_and_unknown_blocks(data):
    window(data[0]);entry(data[1],data[2][0],start_time='12:00',end_time='13:00')
    exception(data[1],data[2][0],'MOVE','REPLACEMENT',MON,start_time='09:00',end_time='11:00')
    assert not preview(data).proposals


def test_incomplete_academic_data_fail_closed(data):
    window(data[0]);entry(data[1],data[2][0],start_time='09:00',end_time=None)
    assert not preview(data).proposals


def test_atomic_rollback_second_insert(data):
    window(data[0]);p=preview(data)
    with data[1].begin() as db:
        db.execute(text("CREATE TRIGGER synthetic_second_insert BEFORE INSERT ON confirmed_study_blocks WHEN (SELECT count(*) FROM confirmed_study_blocks)=1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"))
    with pytest.raises(Exception,match='synthetic failure'):apply(data,p)
    assert counts(data)==(1,0,0,0);assert request_status(data[0],'r') is None


def test_concurrent_double_click(data):
    window(data[0]);p=preview(data)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:apply(data,p),range(2)))
    assert results[0]==results[1];assert counts(data)==(1,2,1,2)


def test_concurrent_distinct_requests(data):
    window(data[0]);p=preview(data)
    def attempt(r):
        try:return apply(data,p,r)
        except ValueError as e:return str(e)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,['a','b']))
    assert sum(isinstance(r,list) for r in results)==1
    assert counts(data)==(1,2,1,2)


@pytest.mark.parametrize('day,begin,end,error',[(date(2026,10,25),time(2,30),time(3,30),'AMBIGUOUS'),(date(2027,3,28),time(2,30),time(3,30),'NONEXISTENT'),(date(2027,4,1),time(9),time(10),'OUTSIDE')])
def test_dst_and_semester(data,day,begin,end,error):
    with pytest.raises(ValueError,match=error):window(data[0],day,begin,end)
    assert counts(data)==(0,0,0,0)


def test_unconfirmed_commands_rejected(data):
    with pytest.raises(ValueError,match='EXPLICIT'):confirm_availability(data[0],'S',MON,time(9),time(10),confirmed=False,actor='x',as_of=NOW)
    window(data[0]);p=preview(data)
    with pytest.raises(ValueError,match='EXPLICIT'):confirm_study_blocks(data[0],p,[p.proposals[0].need_id],request_id='r',confirmed=False,actor='x')
    assert counts(data)==(1,0,0,0)


def test_real_qt_confirmation_and_reread(data,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.study_block_confirmation import StudyBlockConfirmationDialog
    from PySide6.QtCore import Qt,QDate,QTime,QPoint
    # Required reference example, only in the synthetic fixture.
    from dataclasses import replace
    controller=GuiController(data[0],list_semesters(data[0])[0])
    projection=replace(project_learning_needs(data[0],'S',NOW),as_of=NOW.astimezone(ZoneInfo('UTC')))
    dialog=StudyBlockConfirmationDialog(controller,projection);qtbot.addWidget(dialog);dialog.show()
    for day,begin,end in ((MON,time(9),time(10)),(date(2026,11,10),time(15),time(16))):
        dialog.day.setDate(QDate(day));dialog.begin.setTime(QTime(begin));dialog.end.setTime(QTime(end))
        dialog.availability_ack.setChecked(True);dialog.save_window.click()
        qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    assert counts(data)==(2,0,0,0)
    for spin in dialog.spins.values():spin.setValue(60)
    qtbot.mouseClick(dialog.check,Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda:dialog.preview is not None,timeout=10000)
    assert dialog.table.rowCount()==2 and not dialog.apply.isEnabled()
    assert [(dialog.table.item(r,1).text(),dialog.table.item(r,2).text()) for r in range(2)]==[('09.11.2026 09:00','09.11.2026 10:00'),('10.11.2026 15:00','10.11.2026 16:00')]
    assert '120 Minuten' in dialog.summary.text();assert counts(data)==(2,0,0,0)
    if os.environ.get('B1_QA_SCREENSHOT'):dialog.grab().save(os.environ['B1_QA_SCREENSHOT']+'_preview.png')
    qtbot.mouseClick(dialog.ack,Qt.MouseButton.LeftButton,pos=QPoint(7,dialog.ack.height()//2));assert dialog.apply.isEnabled()
    qtbot.mouseClick(dialog.apply,Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    assert '2 Lernblöcke bestätigt' in dialog.status.text()
    qtbot.mouseClick(dialog.recover,Qt.MouseButton.LeftButton);qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    assert 'Request bestätigt' in dialog.status.text();assert counts(data)==(2,2,1,2)
    assert len([e for e in project_semester_calendar(data[0],'S',MON,date(2026,11,10)).events if e.event_type=='STUDY_BLOCK_CONFIRMED'])==2
    if os.environ.get('B1_QA_SCREENSHOT'):dialog.grab().save(os.environ['B1_QA_SCREENSHOT']+'_confirmed.png')
    controller.runner.pool.waitForDone(10000)

@pytest.mark.parametrize('revision',['0017_audit_resolution','0019_scoring_mastery'])
def test_legacy_readable_apply_gated_and_migration(tmp_path,revision):
    from test_m1_4_b3_startup_schema import schema_project,schema_state
    from uniflow.gui.app import open_project
    from alembic import command
    from alembic.config import Config
    config=schema_project(tmp_path,revision)
    before=config.database_path.read_bytes()
    open_project(config.project_root)
    project_learning_needs(config,'SYN',NOW)
    project_semester_calendar(config,'SYN',MON,MON)
    with pytest.raises(ValueError,match='SCHEMA_REQUIRED'):
        confirm_availability(config,'SYN',MON,time(9),time(10),confirmed=True,actor='x',as_of=NOW)
    assert config.database_path.read_bytes()==before
    engine=get_engine(config.database_path)
    with engine.connect() as c:
        old=c.execute(text('SELECT * FROM semesters')).all()
    cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    cfg.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'src/uniflow/infrastructure/database/migrations'))
    cfg.set_main_option('sqlalchemy.url','sqlite:///'+config.database_path.as_posix())
    command.upgrade(cfg,'head')
    with engine.connect() as c:
        assert c.execute(text('SELECT * FROM semesters')).all()==old
        assert c.execute(text('PRAGMA integrity_check')).scalar()=='ok'
        assert not c.execute(text('PRAGMA foreign_key_check')).all()
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar()=='0022_study_block_completion'
    before=config.database_path.read_bytes();open_project(config.project_root)
    assert config.database_path.read_bytes()==before


def test_altered_preview_rejected_and_availability_stales(data):
    from dataclasses import replace
    window(data[0]);p=preview(data)
    forged=replace(p,proposals=(replace(p.proposals[0],minutes=30),*p.proposals[1:]))
    with pytest.raises(ValueError,match='STALE'):apply(data,forged)
    window(data[0],date(2026,11,10))
    with pytest.raises(ValueError,match='STALE'):apply(data,p)
    assert counts(data)==(2,0,0,0)


def test_reference_slots_and_selective_qt(data,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.study_block_confirmation import StudyBlockConfirmationDialog
    from PySide6.QtCore import Qt
    window(data[0],MON,time(9),time(10));window(data[0],date(2026,11,10),time(15),time(16))
    controller=GuiController(data[0],list_semesters(data[0])[0])
    d=StudyBlockConfirmationDialog(controller,project_learning_needs(data[0],'S',NOW));qtbot.addWidget(d);d.show()
    for spin in d.spins.values():spin.setValue(60)
    d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000)
    assert [(datetime.fromisoformat(p.starts_at).astimezone(ZONE).day,datetime.fromisoformat(p.starts_at).astimezone(ZONE).hour) for p in d.preview.proposals]==[(9,9),(10,15)]
    d.ack.setChecked(True)
    d.table.item(1,0).setCheckState(Qt.CheckState.Unchecked);assert not d.ack.isChecked() and not d.apply.isEnabled()
    assert '60 Minuten' in d.summary.text()
    d.ack.setChecked(True);d.apply.click();qtbot.waitUntil(lambda:not d.busy,timeout=10000)
    assert counts(data)==(2,1,1,1);controller.runner.pool.waitForDone(10000)


def test_same_day_visible_minutes_and_seconds_after_conflict(data):
    window(data[0],MON,time(9),time(12))
    instant=datetime(2026,11,9,9,0,53,714,tzinfo=ZONE)
    p=preview_study_blocks(data[0],'S',instant,data[3])
    assert datetime.fromisoformat(p.proposals[0].starts_at).astimezone(ZONE).time()==time(9,1)
    assert all(datetime.fromisoformat(x.starts_at).second==0 and datetime.fromisoformat(x.ends_at).microsecond==0 for x in p.proposals)
    entry(data[1],data[2][0],start_time='09:00',end_time='10:00:42')
    p=preview_study_blocks(data[0],'S',instant,data[3])
    assert datetime.fromisoformat(p.proposals[0].starts_at).astimezone(ZONE).time()==time(10,1)


def test_selection_fingerprint_and_explicit_binding(data):
    window(data[0]);p=preview(data);one=[p.proposals[0].need_id];both=[x.need_id for x in p.proposals]
    assert selection_fingerprint(p,one)!=selection_fingerprint(p,both)
    with pytest.raises(ValueError,match='SELECTION_PREVIEW'):
        confirm_study_blocks(data[0],p,one,request_id='r',confirmed=True,actor='synthetic-user',expected_selection_fingerprint=selection_fingerprint(p,both))
    apply(data,p,selected=one)
    assert request_status(data[0],'r')['preview_fingerprint']==selection_fingerprint(p,one)


def test_existing_weekly_proposal_is_not_a_block_but_accepted_slots_are(tmp_path):
    from test_weekly_plans import setup,create,MON as old_day,BEFORE,CHECK
    from uniflow.application.weekly_plans import accept_weekly_plan_revision,read_weekly_plan_revision
    fixture=setup.__wrapped__(tmp_path)
    config,factory=next(fixture)
    try:
        result=create(config)
        old=read_weekly_plan_revision(config,result['plan_id']);assert old.blocks
        confirm_availability(config,'SYNTH',old_day,time(9),time(12),confirmed=True,actor='synthetic-user',as_of=BEFORE)
        durations={n.recommendation_id:60 for n in project_learning_needs(config,'SYNTH',BEFORE).needs}
        p=preview_study_blocks(config,'SYNTH',BEFORE,durations)
        assert p.proposals and datetime.fromisoformat(p.proposals[0].starts_at).astimezone(ZONE).hour==9
        assert accept_weekly_plan_revision(config,result['plan_id'],accepted_at=CHECK)['status']=='ACCEPTED'
        p=preview_study_blocks(config,'SYNTH',BEFORE,durations)
        assert p.proposals
        assert not any(overlap((datetime.fromisoformat(p.proposals[0].starts_at),datetime.fromisoformat(p.proposals[0].ends_at)),(utc(b.starts_at),utc(b.ends_at))) for b in old.blocks)
        assert datetime.fromisoformat(p.proposals[0].starts_at).astimezone(ZONE).hour==10
    finally:
        try:next(fixture)
        except StopIteration:pass


@pytest.mark.parametrize('day,as_of,utc_hour', [
    (date(2026,11,9), datetime.fromisoformat('2026-11-08T23:30:00+00:00'), 8),
    (date(2026,10,19), datetime.fromisoformat('2026-10-18T22:30:00+00:00'), 7),
])
def test_qt_semester_timezone_with_utc_projection(data, qtbot, day, as_of, utc_hour):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.study_block_confirmation import StudyBlockConfirmationDialog
    from dataclasses import replace
    controller = GuiController(data[0], list_semesters(data[0])[0])
    projection = replace(project_learning_needs(data[0], 'S', as_of), as_of=as_of)
    assert projection.as_of.utcoffset().total_seconds() == 0
    dialog = StudyBlockConfirmationDialog(controller, projection)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.day.date().toPython() == day
    assert counts(data) == (0,0,0,0)
    dialog.save_window.click()
    assert counts(data) == (0,0,0,0)
    dialog.availability_ack.setChecked(True)
    dialog.save_window.click()
    qtbot.waitUntil(lambda: not dialog.busy, timeout=10000)
    assert counts(data) == (1,0,0,0)
    for spin in dialog.spins.values(): spin.setValue(60)
    dialog.check.click()
    qtbot.waitUntil(lambda: dialog.preview is not None, timeout=10000)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0,1).text() == day.strftime('%d.%m.%Y') + ' 09:00'
    assert dialog.table.item(0,2).text() == day.strftime('%d.%m.%Y') + ' 10:00'
    proposal = dialog.preview.proposals[0]
    assert proposal.starts_at == day.isoformat() + f'T{utc_hour:02d}:00:00+00:00'
    assert proposal.ends_at == day.isoformat() + f'T{utc_hour+1:02d}:00:00+00:00'
    assert not dialog.apply.isEnabled()
    dialog.ack.setChecked(True)
    dialog.apply.click()
    qtbot.waitUntil(lambda: not dialog.busy, timeout=10000)
    assert counts(data) == (1,1,1,1)
    with data[1]() as db:
        stored = db.execute(text('SELECT starts_at,ends_at FROM confirmed_study_blocks')).one()
    assert tuple(stored) == (proposal.starts_at, proposal.ends_at)
    events = [e for e in project_semester_calendar(data[0], 'S', day, day).events if e.event_type == 'STUDY_BLOCK_CONFIRMED']
    assert len(events) == 1
    controller.runner.pool.waitForDone(10000)


@pytest.mark.parametrize('revision',['0017_audit_resolution','0019_scoring_mastery','0020_study_blocks'])
@pytest.mark.parametrize('instant,local_a,slot_day',[
    ('2026-11-08T23:30:00+00:00',date(2026,11,9),date(2026,11,9)),
    ('2026-10-18T22:30:00+00:00',date(2026,10,19),date(2026,10,19)),
])
def test_qt_selected_semester_timezone_and_stale_callbacks(tmp_path,qtbot,revision,instant,local_a,slot_day):
    from dataclasses import replace
    from test_m1_4_b3_startup_schema import schema_project
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.learning_priorities import LearningPrioritiesScreen
    from uniflow.gui.study_block_confirmation import StudyBlockConfirmationDialog
    from PySide6.QtCore import QDate,QTime,Qt
    from alembic import command
    from alembic.config import Config
    config=schema_project(tmp_path,revision)
    factory=create_session_factory(get_engine(config.database_path))
    with factory.begin() as db:
        db.add(Semester(stable_id='A',name='Berlin A',timezone='Europe/Berlin',starts_on=date(2026,10,1),ends_on=date(2027,3,31)))
        b=db.query(Semester).filter_by(stable_id='SYN').one()
        b.starts_on=date(2026,10,1);b.ends_on=date(2027,3,31)
        for i in range(2):
            course=Course(stable_id='UTC'+str(i),name='UTC course '+str(i),semester=b)
            db.add(course);db.flush()
            db.add(Deadline(course_id=course.id,title='UTC target '+str(i),due_at=datetime(2026,11,12),is_date_only=True,status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
    now=datetime.fromisoformat(instant)
    choices=list_semesters(config)
    controller=GuiController(config,next(s for s in choices if s.stable_id=='A'),clock=lambda:now)
    screen=LearningPrioritiesScreen(controller);qtbot.addWidget(screen);screen.show();screen.activate(True)
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    screen.semester.setFocus();qtbot.keyClick(screen.semester,Qt.Key.Key_Down)
    qtbot.waitUntil(lambda:screen.projection is not None and screen.projection.semester_id=='SYN',timeout=10000)
    assert controller.semester.stable_id=='A' and controller.semester.timezone=='Europe/Berlin'
    b_projection=replace(screen.projection,as_of=now)
    screen.projection=b_projection
    dialog=StudyBlockConfirmationDialog(controller,b_projection,screen);qtbot.addWidget(dialog);dialog.show()
    assert dialog.semester_id=='SYN' and dialog.semester_zone.key=='UTC'
    assert dialog.day.date().toPython()==now.date()
    if revision!='0020_study_blocks':
        before=config.database_path.read_bytes()
        dialog.availability_ack.setChecked(True);dialog.save_window.click()
        qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
        assert '0020' in dialog.status.text()
        assert config.database_path.read_bytes()==before
        dialog.close()
        cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
        cfg.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'src/uniflow/infrastructure/database/migrations'))
        cfg.set_main_option('sqlalchemy.url','sqlite:///'+config.database_path.as_posix())
        command.upgrade(cfg,'0020_study_blocks')
        dialog=StudyBlockConfirmationDialog(controller,b_projection,screen);qtbot.addWidget(dialog);dialog.show()
        assert dialog.day.date().toPython()==now.date() and dialog.semester_zone.key=='UTC'
    dialog.day.setDate(QDate(slot_day));dialog.begin.setTime(QTime(9,0));dialog.end.setTime(QTime(11,0))
    dialog.availability_ack.setChecked(True);dialog.save_window.click()
    qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    with factory() as db:
        availability=db.execute(text('SELECT timezone,local_day,starts_at,ends_at FROM study_availability_windows')).one()
    assert tuple(availability)==('UTC',slot_day.isoformat(),slot_day.isoformat()+'T09:00:00+00:00',slot_day.isoformat()+'T11:00:00+00:00')
    for spin in dialog.spins.values():spin.setValue(60)
    qtbot.mouseClick(dialog.check,Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda:dialog.preview is not None,timeout=10000)
    assert [(dialog.table.item(r,1).text(),dialog.table.item(r,2).text()) for r in range(2)]==[
        (slot_day.strftime('%d.%m.%Y')+' 09:00',slot_day.strftime('%d.%m.%Y')+' 10:00'),
        (slot_day.strftime('%d.%m.%Y')+' 10:00',slot_day.strftime('%d.%m.%Y')+' 11:00')]
    p=dialog.preview
    assert p.semester_id=='SYN'
    expected_starts=[slot_day.isoformat()+'T09:00:00+00:00',slot_day.isoformat()+'T10:00:00+00:00']
    assert [x.starts_at for x in p.proposals]==expected_starts
    if os.environ.get('B1_QA_SCREENSHOT'):dialog.grab().save(os.environ['B1_QA_SCREENSHOT']+'_'+revision+'_'+slot_day.isoformat()+'_switch_preview.png')
    fingerprint=selection_fingerprint(p,dialog.selected())
    dialog.show_preview(replace(p,semester_id='A'))
    assert dialog.preview is None and not dialog.apply.isEnabled()
    dialog.show_preview(p)
    dialog.ack.setChecked(True);qtbot.mouseClick(dialog.apply,Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    assert '2 Lernblöcke bestätigt' in dialog.status.text()
    with factory() as db:
        stored=db.execute(text('SELECT semester_id,timezone,starts_at FROM confirmed_study_blocks ORDER BY starts_at')).all()
        assert [r[2] for r in stored]==expected_starts
        assert all(r[0]=='synthetic' and r[1]=='UTC' for r in stored)
        assert db.scalar(text('SELECT count(*) FROM study_block_audit_events'))==2
    assert request_status(config,dialog.request_id)['preview_fingerprint']==fingerprint
    dialog.recover.click();qtbot.waitUntil(lambda:not dialog.busy,timeout=10000)
    assert 'Request bestätigt' in dialog.status.text()
    if os.environ.get('B1_QA_SCREENSHOT'):dialog.grab().save(os.environ['B1_QA_SCREENSHOT']+'_semester_switch.png')
    # Rapid B -> A -> B -> A: queued callbacks must not replace final A.
    stale_token=screen.generation
    screen.semester.setCurrentIndex(screen.semester.findData('A'))
    screen.semester.setCurrentIndex(screen.semester.findData('SYN'))
    screen.semester.setCurrentIndex(screen.semester.findData('A'))
    screen.completed('learning_priorities',stale_token,(choices,b_projection))
    qtbot.waitUntil(lambda:screen.projection is not None and screen.projection.semester_id=='A',timeout=10000)
    current=screen.projection
    screen.completed('learning_priorities',screen.generation,(choices,b_projection))
    assert screen.projection is current
    assert not dialog.context_current() and dialog.preview is None
    back=StudyBlockConfirmationDialog(controller,current,screen);qtbot.addWidget(back)
    assert back.semester_zone.key=='Europe/Berlin' and back.day.date().toPython()==local_a
    # Explicit DST ambiguity/nonexistence is rejected under authoritative A timezone.
    for day in (date(2026,10,25),date(2027,3,28)):
        with pytest.raises(ValueError,match='AMBIGUOUS|NONEXISTENT'):
            confirm_availability(config,'A',day,time(2,30),time(3,30),confirmed=True,actor='qa',as_of=now)
    screen.activate(False);controller.runner.pool.waitForDone(10000)
