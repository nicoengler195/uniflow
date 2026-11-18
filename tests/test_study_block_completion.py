"""C1 acceptance: synthetic persistence only."""
from dataclasses import replace
from datetime import date,datetime,time
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import pytest
from sqlalchemy import text
from test_study_blocks import data,window,preview,apply,MON,NOW,ZONE
from test_study_block_changes import block,command,change,events
from uniflow.application.study_block_completion import *
from uniflow.application.study_block_changes import study_block_history,preview_study_block_change


def complete(data,block,p=None,request='complete',actor='synthetic'):
    p=p or preview_study_block_completion(data[0],'S',block,study_block_history(data[0],'S',block)[-1].revision)
    return confirm_study_block_completion(data[0],p,request_id=request,confirmed=True,actor=actor,expected_fingerprint=p.fingerprint)


def snapshot(data):
    with data[1]() as db:
        tables=['confirmed_study_blocks','study_block_revisions','study_confirmation_requests','study_block_change_requests','study_block_audit_events','study_block_change_audit_events','learning_sessions','evidence','mastery_states','scoring_mastery_applications']
        return {t:rows(db,'SELECT * FROM '+t) for t in tables}


def test_explicit_completion_preserves_facts(data,block):
    change(data,command(data,block,title='Revised goal'))
    old=snapshot(data);before=events(data)[0]
    p=preview_study_block_completion(data[0],'S',block,2)
    assert project_study_block_progress(data[0],'S')[0].status=='CONFIRMED'
    e=complete(data,block,p)
    assert e.evidence_type=='USER_REPORTED' and e.session_id is None
    assert e.expected_revision==2 and json.loads(e.provenance_json)['block_snapshot']['title']=='Revised goal'
    assert snapshot(data)==old
    progress=project_study_block_progress(data[0],'S')[0]
    assert progress.status=='COMPLETED' and progress.completion==e and progress.need_id
    after=events(data)[0]
    assert after.status=='COMPLETED' and after.starts_at==before.starts_at and after.ends_at==before.ends_at
    assert complete(data,block,p)==e
    with pytest.raises(ValueError,match='PAYLOAD_MISMATCH'):complete(data,block,p,actor='different')
    with pytest.raises(ValueError,match='NOT_CONFIRMED'):complete(data,block,request='second')
    with pytest.raises(ValueError,match='NOT_CONFIRMED'):change(data,command(data,block,title='forbidden'))
    with data[1]() as db:
        assert db.scalar(text('SELECT count(*) FROM study_block_completions'))==1
        assert db.scalar(text('SELECT count(*) FROM study_block_completion_audit_events'))==1
        assert db.scalar(text('PRAGMA integrity_check'))=='ok'
        assert not db.execute(text('PRAGMA foreign_key_check')).all()


def test_stale_cancelled_and_session_links(data,block):
    p=preview_study_block_completion(data[0],'S',block,1)
    for identity in ('absent','looks-similar'):
        with pytest.raises(ValueError,match='SESSION_LINK_PROVENANCE'):preview_study_block_completion(data[0],'S',block,1,session_id=identity)
    change(data,command(data,block,title='new'))
    with pytest.raises(ValueError,match='STALE_REVISION'):complete(data,block,p)
    change(data,command(data,block,cancel=True),request='cancel')
    assert project_study_block_progress(data[0],'S')[0].status=='CANCELLED'
    with pytest.raises(ValueError,match='NOT_CONFIRMED'):complete(data,block)


def test_rollback_and_append_only(data,block):
    p=preview_study_block_completion(data[0],'S',block,1)
    with data[1].begin() as db:
        db.execute(text("CREATE TRIGGER synthetic_failure BEFORE INSERT ON study_block_completion_audit_events BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"))
    with pytest.raises(Exception,match='synthetic audit failure'):complete(data,block,p)
    with data[1].begin() as db:
        assert db.scalar(text('SELECT count(*) FROM study_block_completions'))==0
        db.execute(text('DROP TRIGGER synthetic_failure'))
    complete(data,block,p)
    for table in TABLES:
        for sql in (f'DELETE FROM {table}',f'UPDATE {table} SET id=id'):
            with data[1].begin() as db:
                with pytest.raises(Exception,match='immutable'):db.execute(text(sql))


@pytest.mark.parametrize('first',['completion','change','parallel'])
def test_completion_change_cas(data,block,first):
    p=preview_study_block_completion(data[0],'S',block,1)
    c=preview_study_block_change(data[0],'S',command(data,block,title='changed'))
    def run(kind):
        try:
            return complete(data,block,p) if kind=='completion' else change(data,c.command,p=c)
        except ValueError as e:return str(e)
    if first=='parallel':
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(run,['completion','change']))
    else:result=[run(first),run('change' if first=='completion' else 'completion')]
    assert sum(not isinstance(r,str) for r in result)==1


def test_confirmation_and_forged_preview(data,block):
    p=preview_study_block_completion(data[0],'S',block,1)
    with pytest.raises(ValueError,match='EXPLICIT'):confirm_study_block_completion(data[0],p,request_id='x',confirmed=False,actor='x',expected_fingerprint=p.fingerprint)
    with pytest.raises(ValueError,match='RECHECK'):complete(data,block,replace(p,state_json='{}'))


def test_completion_gui(data,block,qtbot,monkeypatch):
    from uniflow.gui.controller import GuiController
    from uniflow.gui.screens.calendar import CalendarScreen
    from uniflow.application.semester_read_models import list_semesters
    controller=GuiController(data[0],list_semesters(data[0])[0])
    screen=CalendarScreen(controller);qtbot.addWidget(screen);screen.anchor=MON;screen.show();screen.activate(True)
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=15000)
    screen.complete_study.click();d=screen.study_completion_dialog;qtbot.addWidget(d)
    qtbot.waitUntil(lambda:d.current is not None and not d.busy,timeout=15000)
    assert d.zone.key=='Europe/Berlin'
    d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=15000)
    assert not d.apply.isEnabled() and 'USER_REPORTED' in d.comparison.toPlainText()
    prefix=os.environ.get('C1_QA_SCREENSHOT')
    if prefix:d.grab().save(prefix+'_preview.png')
    d.ack.setChecked(True);d.apply.click()
    qtbot.waitUntil(lambda:not d.busy and d.current['status']=='COMPLETED' and screen.projection is not None,timeout=15000)
    assert not d.check.isEnabled() and not d.apply.isEnabled()
    assert 'Abschlussnachweis' in d.history.toPlainText()
    assert any(e.source_id==block and e.status=='COMPLETED' for e in screen.projection.events)
    if prefix:
        d.grab().save(prefix+'_completed.png')
        Path(prefix+'_synthetic.sqlite3').write_bytes(data[0].database_path.read_bytes())
    d.reject();screen.open_study_completion();d=screen.study_completion_dialog;qtbot.addWidget(d)
    qtbot.waitUntil(lambda:d.current is not None and not d.busy,timeout=15000)
    pending=[];monkeypatch.setattr(controller.runner,'submit',lambda *args:pending.append(args))
    d.run('delayed',lambda:None,lambda result:pytest.fail('stale callback'))
    screen.semester.addItem('Other','OTHER');screen.semester.setCurrentIndex(screen.semester.findData('OTHER'))
    screen.semester.setCurrentIndex(screen.semester.findData('S'))
    pending[0][3](pending[0][0],pending[0][1],None)
    assert not d.context_current() and not d.apply.isEnabled() and not d.busy
    screen.activate(False);controller.runner.pool.waitForDone(15000)


def test_0021_migration_and_startup_preserves_rows(tmp_path):
    from test_m1_4_b3_startup_schema import schema_project
    from uniflow.infrastructure.database.engine import get_engine,create_session_factory
    from uniflow.gui.app import open_project
    from alembic import command as migrate
    from alembic.config import Config
    from test_study_blocks import MON,NOW
    from uniflow.application.study_blocks import confirm_availability
    from uniflow.application.study_block_changes import StudyBlockChangeCommandV1
    from datetime import time
    config=schema_project(tmp_path,'0021_study_block_changes')
    factory=create_session_factory(get_engine(config.database_path))
    confirm_availability(config,'SYN',MON,time(9),time(12),confirmed=True,actor='synthetic',as_of=NOW)
    with factory.begin() as db:
        from uniflow.infrastructure.database.models import Course
        sid=db.scalar(text('SELECT id FROM semesters LIMIT 1'))
        c=Course(stable_id='M',name='Migration',semester_id=sid);db.add(c);db.flush()
        w=db.scalar(text('SELECT id FROM study_availability_windows LIMIT 1'))
        db.execute(text("INSERT INTO study_confirmation_requests VALUES ('b1','hash','fingerprint','now','synthetic','[\"b\"]')"))
        db.execute(text("INSERT INTO confirmed_study_blocks VALUES ('b',:s,:c,'[]','need','policy','goal','2026-11-09T09:00:00+00:00','2026-11-09T10:00:00+00:00',60,'UTC',:w,'CONFIRMED',1,'b1')"),dict(s=sid,c=c.id,w=w))
        db.execute(text("INSERT INTO study_block_audit_events VALUES ('a','b','CREATED','{}','now','b1')"))
    # Existing B2 operations remain usable on 0021.
    from uniflow.application.study_block_changes import confirm_study_block_change
    c=StudyBlockChangeCommandV1('b',1,MON,time(10),60,'goal')
    p=preview_study_block_change(config,'SYN',c)
    confirm_study_block_change(config,p,request_id='b2',confirmed=True,actor='synthetic',expected_fingerprint=p.fingerprint)
    with pytest.raises(ValueError,match='0022'):preview_study_block_completion(config,'SYN','b',2)
    with factory() as db:
        names=[r[0] for r in db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' ORDER BY name"))]
        before={t:rows(db,'SELECT * FROM '+t) for t in names}
    cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    cfg.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'src/uniflow/infrastructure/database/migrations'))
    cfg.set_main_option('sqlalchemy.url','sqlite:///'+config.database_path.as_posix())
    migrate.upgrade(cfg,'0022_study_block_completion')
    with factory() as db:
        assert before=={t:rows(db,'SELECT * FROM '+t) for t in names}
    raw=config.database_path.read_bytes();open_project(config.project_root)
    assert config.database_path.read_bytes()==raw
    p=preview_study_block_completion(config,'SYN','b',2)
    confirm_study_block_completion(config,p,request_id='c1',confirmed=True,actor='synthetic',expected_fingerprint=p.fingerprint)
    assert project_study_block_progress(config,'SYN')[0].status=='COMPLETED'


def test_past_time_is_not_completion(data,block):
    from datetime import datetime,timezone
    from uniflow.application.semester_calendar import project_semester_calendar
    # Viewing a historical date with a later as_of never writes completion.
    p=project_semester_calendar(data[0],'S',MON,MON,as_of=datetime(2027,1,1,tzinfo=timezone.utc))
    assert next(e for e in p.events if e.source_id==block).status=='CONFIRMED'
    assert project_study_block_progress(data[0],'S')[0].completion is None


def test_completion_retains_reservation(data,block):
    before=preview(data)
    complete(data,block)
    after=preview(data)
    assert [(p.starts_at,p.ends_at) for p in before.proposals]==[(p.starts_at,p.ends_at) for p in after.proposals]


def test_c1_selected_semester_timezone(data,qtbot,monkeypatch):
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
    screen.complete_study.click();d=screen.study_completion_dialog;qtbot.addWidget(d)
    qtbot.waitUntil(lambda:d.current is not None,timeout=10000)
    assert d.zone.key=='UTC'
    d.check.click();qtbot.waitUntil(lambda:d.preview is not None,timeout=10000)
    assert json.loads(d.preview.state_json)['starts_at']=='2026-11-09T09:00:00+00:00'
    assert '09.11.2026 09:00' in d.comparison.toPlainText()
    # Invalidate even an already checked preview immediately on semester switch.
    d.ack.setChecked(True)
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
