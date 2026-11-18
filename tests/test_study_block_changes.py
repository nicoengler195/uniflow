"""B2 acceptance, always isolated synthetic persistence."""
from dataclasses import replace
from datetime import date,datetime,time
import hashlib
import json
import os
from pathlib import Path
import pytest
from sqlalchemy import text
from test_study_blocks import data,window,preview,apply,MON,NOW,ZONE
from test_planning_calendar import entry,exception
from uniflow.application.study_block_changes import *
from uniflow.application.semester_calendar import project_semester_calendar


@pytest.fixture
def block(data):
    window(data[0],MON,time(9),time(14))
    p=preview(data);identity=apply(data,p,selected=[p.proposals[0].need_id])[0]
    return identity


def command(data,block,**changes):
    b=study_block_history(data[0],'S',block)[-1].state
    start=datetime.fromisoformat(b['starts_at']).astimezone(ZONE)
    return replace(StudyBlockChangeCommandV1(block,b['revision'],start.date(),start.time(),b['minutes'],b['title']),**changes)


def change(data,c,request='change',p=None):
    p=p or preview_study_block_change(data[0],'S',c)
    return confirm_study_block_change(data[0],p,request_id=request,confirmed=True,actor='synthetic',expected_fingerprint=p.fingerprint)


def events(data):
    return [e for e in project_semester_calendar(data[0],'S',MON,MON).events if e.event_type=='STUDY_BLOCK_CONFIRMED']


def test_revision_lifecycle_and_b1_projection(data,block):
    original=study_block_history(data[0],'S',block)[0]
    before=data[0].database_path.read_bytes()
    p=preview_study_block_change(data[0],'S',command(data,block,begin=time(11)))
    assert data[0].database_path.read_bytes()==before
    r=change(data,p.command,p=p);assert r.revision==2 and r.block_id==block
    assert len(events(data))==1 and events(data)[0].starts_at.hour==11
    assert 'Revision 2' in events(data)[0].notes
    b1=preview(data);assert b1.proposals[0].starts_at==original.state['starts_at']
    apply(data,b1,request='reuse',selected=[b1.proposals[0].need_id])
    assert len(events(data))==2
    change(data,command(data,block,minutes=90),request='duration')
    change(data,command(data,block,title='Präzisiertes Lernziel'),request='title')
    current=study_block_history(data[0],'S',block)[-1].state
    for key in ('skill_ids_json','course_id','need_id','need_policy'):assert current[key]==original.state[key]
    change(data,command(data,block,cancel=True),request='cancel')
    history=study_block_history(data[0],'S',block)
    assert [r.revision for r in history]==[1,2,3,4,5]
    assert history[0]==original and history[-1].state['status']=='CANCELLED'
    assert all(e.source_id!=block for e in events(data))
    with pytest.raises(ValueError,match='NOT_CONFIRMED'):change(data,command(data,block,title='No resurrection'))
    with data[1]() as db:
        assert db.scalar(text('SELECT count(*) FROM study_block_audit_events'))==2
        assert db.scalar(text('SELECT count(*) FROM study_block_change_audit_events'))==4
        assert db.scalar(text('PRAGMA integrity_check'))=='ok'
        assert not db.execute(text('PRAGMA foreign_key_check')).all()


def test_shift_reserves_new_time_and_cancellation_frees_old(data,block):
    change(data,command(data,block,begin=time(9,30)))
    assert datetime.fromisoformat(preview(data).proposals[0].starts_at).astimezone(ZONE).time()==time(10,30)
    change(data,command(data,block,cancel=True),request='cancel')
    assert datetime.fromisoformat(preview(data).proposals[0].starts_at).astimezone(ZONE).time()==time(9)
    apply(data,preview(data),request='fresh')


def test_idempotence_staleness_and_payload(data,block):
    c=command(data,block,begin=time(11));p=preview_study_block_change(data[0],'S',c)
    r=change(data,c,p=p);assert change(data,c,p=p)==r
    with pytest.raises(ValueError,match='PAYLOAD_MISMATCH'):change(data,c,p=replace(p,context_hash='forged'))
    with pytest.raises(ValueError,match='STALE_REVISION'):change(data,c,request='stale',p=p)
    assert len(study_block_history(data[0],'S',block))==2


def test_concurrent_revision_cas(data,block):
    from concurrent.futures import ThreadPoolExecutor
    p=preview_study_block_change(data[0],'S',command(data,block,begin=time(11)))
    def run(r):
        try:return change(data,p.command,request=r,p=p)
        except ValueError as e:return str(e)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['one','two']))
    assert sum(isinstance(r,StudyBlockChangeReceiptV1) for r in results)==1
    assert len(study_block_history(data[0],'S',block))==2


def test_context_change_and_confirmation(data,block):
    c=command(data,block,begin=time(11));p=preview_study_block_change(data[0],'S',c)
    with pytest.raises(ValueError,match='EXPLICIT'):
        confirm_study_block_change(data[0],p,request_id='x',confirmed=False,actor='x',expected_fingerprint=p.fingerprint)
    with pytest.raises(ValueError,match='RECHECK'):
        confirm_study_block_change(data[0],p,request_id='x',confirmed=True,actor='x',expected_fingerprint='forged')
    window(data[0],date(2026,11,10))
    with pytest.raises(ValueError,match='STALE_PREVIEW'):change(data,c,p=p)
    assert len(study_block_history(data[0],'S',block))==1


def test_conflicts_availability_and_self(data,block):
    with pytest.raises(ValueError,match='AVAILABILITY'):change(data,command(data,block,day=date(2026,11,10)))
    p=preview(data);apply(data,p,request='second',selected=[p.proposals[0].need_id])
    with pytest.raises(ValueError,match='HARD_CONFLICT'):change(data,command(data,block,begin=time(10)))
    entry(data[1],data[2][0],start_time='12:00',end_time='13:00')
    with pytest.raises(ValueError,match='HARD_CONFLICT'):change(data,command(data,block,begin=time(12)))
    # Same starting time and shorter duration excludes self.
    change(data,command(data,block,minutes=30))


def test_late_conflict_and_rollback(data,block):
    c=command(data,block,begin=time(11));p=preview_study_block_change(data[0],'S',c)
    entry(data[1],data[2][0],start_time='11:00',end_time='12:00')
    with pytest.raises(ValueError,match='HARD_CONFLICT'):change(data,c,p=p)
    with data[1].begin() as db:
        db.execute(text("CREATE TRIGGER synthetic_failure BEFORE INSERT ON study_block_change_audit_events BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"))
    with pytest.raises(Exception,match='synthetic audit failure'):change(data,command(data,block,begin=time(13)))
    with data[1]() as db:
        assert all(db.scalar(text('SELECT count(*) FROM '+t))==0 for t in TABLES)
    assert len(study_block_history(data[0],'S',block))==1


@pytest.mark.parametrize('table',['study_availability_windows','confirmed_study_blocks','study_confirmation_requests','study_block_audit_events',*TABLES])
@pytest.mark.parametrize('action',['UPDATE','DELETE'])
def test_all_append_only_triggers(data,block,table,action):
    change(data,command(data,block,begin=time(11)))
    with data[1].begin() as db:
        column=db.execute(text('PRAGMA table_info('+table+')')).first()[1]
        sql=f'UPDATE {table} SET {column}={column}' if action=='UPDATE' else 'DELETE FROM '+table
        with pytest.raises(Exception,match='immutable'):db.execute(text(sql))


@pytest.mark.parametrize('day,error',[(date(2026,10,25),'AMBIGUOUS'),(date(2027,3,28),'NONEXISTENT'),(date(2027,4,1),'OUTSIDE')])
def test_dst_semester(data,block,day,error):
    with pytest.raises(ValueError,match=error):change(data,command(data,block,day=day,begin=time(2,30)))


def test_migration_b1_preserved_startup_and_older_schema(tmp_path):
    from test_m1_4_b3_startup_schema import schema_project
    from uniflow.infrastructure.database.engine import get_engine,create_session_factory
    from uniflow.application.study_blocks import confirm_availability,preview_study_blocks,confirm_study_blocks,selection_fingerprint
    from uniflow.gui.app import open_project
    from alembic import command as migrate
    from alembic.config import Config
    config=schema_project(tmp_path,'0020_study_blocks');factory=create_session_factory(get_engine(config.database_path))
    confirm_availability(config,'SYN',MON,time(9),time(12),confirmed=True,actor='synthetic',as_of=NOW)
    # Fixture semester contains no learning needs: create B1 provenance by its exact SQL contract.
    with factory.begin() as db:
        semester=db.scalar(text('SELECT id FROM semesters LIMIT 1'))
        from uniflow.infrastructure.database.models import Course
        c=Course(stable_id='M',name='Migration',semester_id=semester);db.add(c);db.flush()
        w=db.scalar(text('SELECT id FROM study_availability_windows LIMIT 1'))
        db.execute(text("INSERT INTO study_confirmation_requests VALUES ('b1','hash','fingerprint','now','synthetic','[\"b\"]')"))
        db.execute(text("INSERT INTO confirmed_study_blocks VALUES ('b',:s,:c,'[]','need','policy','goal','2026-11-09T09:00:00+00:00','2026-11-09T10:00:00+00:00',60,'UTC',:w,'CONFIRMED',1,'b1')"),dict(s=semester,c=c.id,w=w))
        db.execute(text("INSERT INTO study_block_audit_events VALUES ('a','b','CREATED','{}','now','b1')"))
    with factory() as db:before={t:rows(db,'SELECT * FROM '+t) for t in ('study_availability_windows','study_confirmation_requests','confirmed_study_blocks','study_block_audit_events')}
    bytes_before=config.database_path.read_bytes();open_project(config.project_root);assert config.database_path.read_bytes()==bytes_before
    with pytest.raises(ValueError,match='0021'):preview_study_block_change(config,'SYN',StudyBlockChangeCommandV1('b',1,MON,time(10),60,'goal'))
    assert config.database_path.read_bytes()==bytes_before
    cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'));cfg.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'src/uniflow/infrastructure/database/migrations'));cfg.set_main_option('sqlalchemy.url','sqlite:///'+config.database_path.as_posix())
    migrate.upgrade(cfg,'0021_study_block_changes')
    with factory() as db:
        assert before=={t:rows(db,'SELECT * FROM '+t) for t in before}
        assert db.scalar(text('PRAGMA integrity_check'))=='ok'
        assert not db.execute(text('PRAGMA foreign_key_check')).all()
    bytes_before=config.database_path.read_bytes();open_project(config.project_root);assert config.database_path.read_bytes()==bytes_before
    p=preview_study_block_change(config,'SYN',StudyBlockChangeCommandV1('b',1,MON,time(10),60,'goal'))
    r=confirm_study_block_change(config,p,request_id='after-migration',confirmed=True,actor='synthetic',expected_fingerprint=p.fingerprint)
    assert r.revision==2
    with factory() as db:assert before=={t:rows(db,'SELECT * FROM '+t) for t in before}
    if os.environ.get('B2_QA_SCREENSHOT'):
        prefix=os.environ['B2_QA_SCREENSHOT']
        Path(prefix+'_migration.sqlite3').write_bytes(config.database_path.read_bytes())
        Path(prefix+'_migration.json').write_text(json.dumps(dict(before=before,revision=asdict(r),integrity='ok',foreign_keys=[]),indent=2),encoding='utf-8')


def test_gui_persist_move_history_cancel(data,block,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.calendar import CalendarScreen
    from PySide6.QtCore import QTime
    controller=GuiController(data[0],list_semesters(data[0])[0])
    screen=CalendarScreen(controller);qtbot.addWidget(screen);screen.anchor=MON;screen.show();screen.activate(True)
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    screen.agenda.setCurrentRow(next(i for i,e in enumerate(screen.displayed) if e.source_id==block))
    screen.edit_study.click();d=screen.study_change_dialog;qtbot.addWidget(d)
    qtbot.waitUntil(lambda:d.current is not None,timeout=10000)
    def evidence(stage):
        prefix=os.environ.get('B2_QA_SCREENSHOT')
        if not prefix:return
        target=Path(prefix+'_'+stage+'.sqlite3')
        target.write_bytes(data[0].database_path.read_bytes())
        payload=dict(stage=stage,history=[asdict(r) for r in study_block_history(data[0],'S',block)],
            calendar=[dict(block_id=e.source_id,starts_at=e.starts_at.isoformat()) for e in events(data)],
            database_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        Path(prefix+'_'+stage+'.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    assert d.current['revision']==1
    evidence('opened')
    d.begin.setTime(QTime(11,0));d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000)
    assert not d.apply.isEnabled();assert len(study_block_history(data[0],'S',block))==1
    if os.environ.get('B2_QA_SCREENSHOT'):d.grab().save(os.environ['B2_QA_SCREENSHOT']+'_preview.png')
    evidence('preview')
    d.ack.setChecked(True);d.apply.click()
    qtbot.waitUntil(lambda:not d.busy and d.current['revision']==2 and screen.projection is not None,timeout=10000)
    assert len(events(data))==1 and events(data)[0].starts_at.hour==11
    assert 'Revision 2' in events(data)[0].notes
    assert [e.starts_at.hour for e in screen.projection.events if e.source_id==block]==[11]
    evidence('moved')
    d.refresh.click();qtbot.waitUntil(lambda:not d.busy,timeout=10000);assert 'Revision 2' in d.history.toPlainText()
    evidence('history')
    d.cancel.setChecked(True);d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000)
    d.ack.setChecked(True);assert not d.apply.isEnabled();d.cancel_ack.setChecked(True);d.apply.click()
    qtbot.waitUntil(lambda:not d.busy and d.current['status']=='CANCELLED' and screen.projection is not None,timeout=10000)
    assert not events(data) and all(e.source_id!=block for e in screen.projection.events)
    assert len(study_block_history(data[0],'S',block))==3 and 'Revision 3' in d.history.toPlainText()
    evidence('cancelled')
    if os.environ.get('B2_QA_SCREENSHOT'):d.grab().save(os.environ['B2_QA_SCREENSHOT']+'_cancelled.png')
    # Callback from a previous semester must not overwrite this context.
    screen.semester.addItem('Other','OTHER');screen.semester.setCurrentIndex(screen.semester.findData('OTHER'))
    assert not d.context_current();d.show_preview(replace(d.preview,semester_id='OTHER')) if d.preview else None
    assert not d.apply.isEnabled()
    screen.activate(False);controller.runner.pool.waitForDone(10000)


def test_personal_constraint_and_replacement(data,block):
    from uniflow.application.planning_calendar import create_planning_profile,add_calendar_constraint
    create_planning_profile(data[0],'S')
    add_calendar_constraint(data[0],'S',constraint_type='BLOCKED',starts_at=datetime(2026,11,9,11,tzinfo=ZONE),ends_at=datetime(2026,11,9,12,tzinfo=ZONE),label='private')
    with pytest.raises(ValueError,match='HARD_CONFLICT'):change(data,command(data,block,begin=time(11)))
    entry(data[1],data[2][0],start_time='15:00',end_time='16:00')
    exception(data[1],data[2][0],'REPLACE','REPLACEMENT',MON,start_time='12:00',end_time='13:00')
    with pytest.raises(ValueError,match='HARD_CONFLICT'):change(data,command(data,block,begin=time(12)))


def test_b2_accepted_legacy_plan(tmp_path):
    from test_weekly_plans import setup,create,MON as day,BEFORE,CHECK
    from uniflow.application.weekly_plans import accept_weekly_plan_revision
    from uniflow.application.study_blocks import confirm_availability,preview_study_blocks,confirm_study_blocks,selection_fingerprint
    from uniflow.application.learning_needs import project_learning_needs
    fixture=setup.__wrapped__(tmp_path);config,factory=next(fixture)
    try:
        result=create(config)
        confirm_availability(config,'SYNTH',day,time(9),time(12),confirmed=True,actor='synthetic',as_of=BEFORE)
        needs=project_learning_needs(config,'SYNTH',BEFORE).needs
        p=preview_study_blocks(config,'SYNTH',BEFORE,{n.recommendation_id:60 for n in needs})
        selected=[p.proposals[0].need_id]
        identity=confirm_study_blocks(config,p,selected,request_id='b1',confirmed=True,actor='synthetic',expected_selection_fingerprint=selection_fingerprint(p,selected))[0]
        assert accept_weekly_plan_revision(config,result['plan_id'],accepted_at=CHECK)['status']=='ACCEPTED'
        c=StudyBlockChangeCommandV1(identity,1,day,time(9,30),60,'goal')
        with pytest.raises(ValueError,match='HARD_CONFLICT'):preview_study_block_change(config,'SYNTH',c)
    finally:
        try:next(fixture)
        except StopIteration:pass


def test_b2_selected_semester_timezone(data,qtbot,monkeypatch):
    from uniflow.infrastructure.database.models import Semester,Course,Deadline
    from uniflow.domain.enums import InformationStatus,DeadlineStatus
    from uniflow.application.study_blocks import confirm_availability,preview_study_blocks,confirm_study_blocks,selection_fingerprint
    from uniflow.application.learning_needs import project_learning_needs
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.controller import GuiController
    from uniflow.gui.screens.calendar import CalendarScreen
    from PySide6.QtCore import QTime
    with data[1].begin() as db:
        semester=Semester(stable_id='UTC',name='UTC semester',timezone='UTC',starts_on=date(2026,10,1),ends_on=date(2027,3,31))
        c=Course(stable_id='U',name='UTC course',semester=semester);db.add(c);db.flush()
        db.add(Deadline(course_id=c.id,title='UTC goal',due_at=datetime(2026,11,12),is_date_only=True,status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
    confirm_availability(data[0],'UTC',MON,time(9),time(12),confirmed=True,actor='synthetic',as_of=NOW)
    durations={n.recommendation_id:60 for n in project_learning_needs(data[0],'UTC',NOW).needs}
    p=preview_study_blocks(data[0],'UTC',NOW,durations);selected=[p.proposals[0].need_id]
    identity=confirm_study_blocks(data[0],p,selected,request_id='utc',confirmed=True,actor='synthetic',expected_selection_fingerprint=selection_fingerprint(p,selected))[0]
    controller=GuiController(data[0],next(s for s in list_semesters(data[0]) if s.stable_id=='S'))
    screen=CalendarScreen(controller);qtbot.addWidget(screen);screen.anchor=MON;screen.show();screen.activate(True)
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    screen.semester.setCurrentIndex(screen.semester.findData('UTC'))
    qtbot.waitUntil(lambda:screen.projection is not None and screen.projection.semester_id=='UTC',timeout=10000)
    assert controller.semester.timezone=='Europe/Berlin'
    screen.edit_study.click();d=screen.study_change_dialog;qtbot.addWidget(d)
    qtbot.waitUntil(lambda:d.current is not None,timeout=10000)
    assert d.zone.key=='UTC' and d.begin.time().hour()==9
    d.begin.setTime(QTime(11,0));d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000)
    assert json.loads(d.preview.after_json)['starts_at']=='2026-11-09T11:00:00+00:00'
    d.ack.setChecked(True);d.apply.click();qtbot.waitUntil(lambda:not d.busy and d.current['revision']==2,timeout=10000)
    assert study_block_history(data[0],'UTC',identity)[-1].state['starts_at']=='2026-11-09T11:00:00+00:00'
    # Invalidate even an already checked preview immediately on semester switch.
    d.begin.setTime(QTime(10,0));d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000);d.ack.setChecked(True)
    stale=d.preview
    pending=[]
    monkeypatch.setattr(controller.runner,'submit',lambda *args:pending.append(args))
    d.run('delayed-preview',lambda:stale,d.show_preview)
    screen.semester.setCurrentIndex(screen.semester.findData('S'))
    assert d.preview is None and not d.apply.isEnabled()
    screen.semester.setCurrentIndex(screen.semester.findData('UTC'))
    pending[0][3](pending[0][0],pending[0][1],stale)
    d.show_preview(stale);assert d.preview is None and not d.context_current() and not d.busy
    screen.activate(False);controller.runner.pool.waitForDone(10000)


@pytest.mark.parametrize('day',[date(2026,10,25),date(2027,3,28)])
def test_dst_crossing_unambiguous_endpoints_rejected(data,block,day):
    window(data[0],day,time(0),time(5))
    before=data[0].database_path.read_bytes()
    with pytest.raises(ValueError,match='DST_DURATION_MISMATCH'):
        change(data,command(data,block,day=day,begin=time(1,30),minutes=120))
    assert data[0].database_path.read_bytes()==before
