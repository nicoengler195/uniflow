"""M1.6-A acceptance on synthetic 0017/0019 databases, never production."""
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from test_semester_calendar import calendar_data
from test_m1_4_b3_persistent_review import case
from uniflow.application.learning_needs import project_learning_needs, POLICY
from uniflow.infrastructure.database.models import (Deadline, Skill, CourseSkill, SkillDependency,
    MasteryState, Assessment, Material, Semester, Course, Evidence)
from uniflow.domain.enums import DeadlineStatus, InformationStatus, SemesterState, EvidenceType


def instant(day=12, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=ZoneInfo('Europe/Berlin'))


def test_deadlines_determinism_filters_readonly(calendar_data):
    config, factory, cid, oid = calendar_data
    with factory.begin() as db:
        for identity, days, status, info in [
            ('high', 3, 'OPEN', 'CONFIRMED'), ('normal', 8, 'OPEN', 'OBSERVED'),
            ('unknown', 1, 'UNKNOWN', 'CONFIRMED'), ('inferred', 1, 'OPEN', 'INFERRED'),
            ('met', 1, 'MET', 'CONFIRMED'), ('cancel', 1, 'CANCELLED', 'CONFIRMED'),
            ('past', -1, 'OPEN', 'CONFIRMED'), ('beyond', 31, 'OPEN', 'CONFIRMED')]:
            db.add(Deadline(id=identity, course_id=cid, title=identity, due_at=instant()+timedelta(days=days),
                is_date_only=True, status=DeadlineStatus(status), information_status=InformationStatus(info)))
        db.add(Deadline(id='undated', course_id=cid, title='Undated'))
        assessment = Assessment(id='exam', course_id=cid, name='Exam')
        db.add(assessment); db.flush()
        db.add(Deadline(id='via-exam', assessment_id='exam', title='Exam deadline', due_at=instant(13),
            is_date_only=True, status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED))
        db.add(Deadline(id='conflict', course_id=oid, assessment_id='exam', title='Conflict', due_at=instant(13),
            is_date_only=True, status=DeadlineStatus.OPEN, information_status=InformationStatus.CONFIRMED))
    before = hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    p = project_learning_needs(config, 'SYNTH', instant(), cid)
    assert p == project_learning_needs(config, 'SYNTH', instant(), cid)
    by_title = {n.action.split(': ')[-1]: n for n in p.needs}
    assert by_title['Date'].priority == 'CRITICAL' and 'Fristuhrzeit' in by_title['Date'].missing_information
    assert by_title['Timed'].priority == 'CRITICAL'
    assert by_title['high'].priority == 'HIGH' and by_title['normal'].priority == 'NORMAL'
    assert all(by_title[t].priority == 'NEEDS_CLARIFICATION' for t in ['unknown','inferred','past','Undated','Conflict'])
    assert not {'met','cancel','beyond'}.intersection(by_title)
    assert 'assessments/exam' in by_title['Exam deadline'].sources
    assert all(n.known_minutes is None and n.sources and n.course_id == cid for n in p.needs)
    assert len({n.recommendation_id for n in p.needs}) == len(p.needs)
    assert project_learning_needs(config, 'SYNTH', instant(13,23), cid).needs
    with pytest.raises(ValueError, match='UNKNOWN_COURSE'): project_learning_needs(config,'SYNTH',instant(),'missing')
    with pytest.raises(ValueError, match='UNKNOWN_SEMESTER'): project_learning_needs(config,'OTHER',instant())
    with pytest.raises(ValueError, match='TIMEZONE'): project_learning_needs(config,'SYNTH',datetime(2026,10,12))
    with pytest.raises(ValueError, match='HORIZON'): project_learning_needs(config,'SYNTH',instant(),horizon_days=91)
    assert before == hashlib.sha256(config.database_path.read_bytes()).hexdigest()


def test_mastery_dependencies_and_unknown(calendar_data):
    config, factory, cid, oid = calendar_data
    with factory.begin() as db:
        db.add_all([Skill(id=sid,name=sid) for sid in ['low','unknown','good','pre','inactive','unrelated']]); db.flush()
        for sid in ['low','unknown','good','pre']:
            db.add(CourseSkill(course_id=cid,skill_id=sid))
        db.add_all([SkillDependency(id='edge',prerequisite_skill_id='pre',dependent_skill_id='low'),
                    SkillDependency(id='off',prerequisite_skill_id='inactive',dependent_skill_id='low',is_active=False)])
        db.add(Evidence(id='e',skill_id='low',evidence_type=EvidenceType.ASSESSMENT_PERFORMANCE,
                        value_json='{"policy":"synthetic-ai-policy-v1"}'))
        db.flush()
        for identity,sid,value,stamp in [('old','low',.9,instant(10)),('new','low',.2,instant(11)),
                                         ('good','good',.8,instant(11)),('future','low',.95,instant(14))]:
            db.add(MasteryState(id=identity,skill_id=sid,value=value,calculated_at=stamp,
                calculation_method='synthetic-ai-policy-v1',evidence_id='e' if sid=='low' else None))
    p = project_learning_needs(config,'SYNTH',instant(),cid)
    by_skill = {n.skill_ids[0]:n for n in p.needs if n.skill_ids}
    assert set(by_skill) == {'low','unknown','pre'}
    assert by_skill['low'].priority == 'NORMAL' and 'mastery_states/new' in by_skill['low'].sources
    assert 'evidence/e' in by_skill['low'].sources and 'synthetic-ai-policy-v1' in by_skill['low'].reasons[0]
    assert by_skill['unknown'].priority == 'NEEDS_CLARIFICATION'
    assert 'skill_dependencies/edge' in by_skill['pre'].sources
    assert len([n for n in p.needs if n.skill_ids == ('pre',)]) == 1
    assert all(n.known_minutes is None for n in p.needs)
    assert not any(n.skill_ids for n in project_learning_needs(config,'SYNTH',instant(),oid).needs)
    with factory.begin() as db:
        db.add(SkillDependency(id='cycle',prerequisite_skill_id='low',dependent_skill_id='pre'))
    p=project_learning_needs(config,'SYNTH',instant(),cid)
    assert any('Zyklische' in d for d in p.diagnostics)
    assert not any('skill_dependencies/edge' in n.sources for n in p.needs)


@pytest.mark.parametrize('days,expected', [(0,'CRITICAL'),(2,'CRITICAL'),(3,'HIGH'),(7,'HIGH'),(8,'NORMAL'),(-1,'NEEDS_CLARIFICATION')])
def test_policy_boundaries(days,expected):
    assert POLICY.deadline(days,known_open=True)==expected


def test_time_precision_semesters_and_invalid_mastery(calendar_data):
    config, factory, cid, _ = calendar_data
    with factory.begin() as db:
        other = Semester(stable_id='SECOND',name='Second',state=SemesterState.ACTIVE,timezone='Europe/Berlin')
        course = Course(stable_id='SECOND-C',name='Second course',semester=other)
        db.add(course); db.flush(); other_id=course.id
        db.add(Deadline(id='other-semester',course_id=other_id,title='Second deadline',due_at=instant(13),
            is_date_only=True,status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
        for identity, stamp, dateonly in [('today-date',instant(),True),('today-past-time',instant(12,10),False),
                                           ('dst',datetime(2026,10,25,2,30),False)]:
            db.add(Deadline(id=identity,course_id=cid,title=identity,due_at=stamp,is_date_only=dateonly,
                status=DeadlineStatus.OPEN,information_status=InformationStatus.CONFIRMED))
        db.add(Skill(id='invalid',name='Invalid')); db.flush()
        db.add(CourseSkill(course_id=cid,skill_id='invalid'))
        db.add(MasteryState(id='invalid',skill_id='invalid',value=1.5,calculated_at=instant(11),calculation_method='invalid-synthetic'))
    p=project_learning_needs(config,'SYNTH',instant(12,23))
    assert not any(n.course_id==other_id for n in p.needs)
    assert all(n.course_id==other_id for n in project_learning_needs(config,'SECOND',instant()).needs)
    by_id={n.recommendation_id:n for n in p.needs}
    assert by_id[f'v1:deadline:today-date:{cid}'].priority=='CRITICAL'
    assert by_id[f'v1:deadline:today-past-time:{cid}'].priority=='NEEDS_CLARIFICATION'
    assert by_id[f'v1:deadline:dst:{cid}'].priority=='NEEDS_CLARIFICATION'
    assert by_id[f'v1:skill:{cid}:invalid'].priority=='NEEDS_CLARIFICATION'


def test_actual_reviewed_mastery_origin(case):
    from uniflow.application.scoring_review import save_review
    from uniflow.application.controlled_mastery import preview_mastery, apply_mastery
    from uniflow.infrastructure.database.models import ScoringMasteryApplication
    from sqlalchemy import select
    config, factory, _, raw, request = case
    save_review(config,raw,**request)
    preview=preview_mastery(config,request['result_sha256'],request['session_id'],request['handoff_id'])
    ids=apply_mastery(config,preview,['TARGET'],confirmed=True)
    with factory() as db:
        state=db.scalar(select(MasteryState).where(MasteryState.skill_id=='TARGET'))
        as_of=state.calculated_at.replace(tzinfo=ZoneInfo('UTC'))+timedelta(seconds=1)
    before=hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    p=project_learning_needs(config,'SYN',as_of)
    need=next(n for n in p.needs if n.skill_ids==('TARGET',))
    assert f'scoring_mastery_applications/{ids[0]}' in need.sources
    assert 'scoring-result-reviewed-mastery-v1 Version 1' in need.reasons[0]
    assert request['result_sha256'] in need.reasons[0]
    assert 'S09 = NOT_VERIFIED' in need.evidence_quality
    assert before==hashlib.sha256(config.database_path.read_bytes()).hexdigest()


def test_gui_lifecycle(calendar_data, qtbot, request):
    from uniflow.gui.controller import GuiController
    from uniflow.gui.app import MainWindow
    from uniflow.gui.routes import Route
    from uniflow.gui.presentation import PresentationError
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.theme.stylesheet import stylesheet
    from PySide6.QtGui import QFontDatabase
    # The Windows offscreen plugin needs explicit application font registration.
    if os.environ.get('QT_QPA_PLATFORM')=='offscreen':
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    config, _, cid, _ = calendar_data
    before=hashlib.sha256(config.database_path.read_bytes()).hexdigest()
    controller=GuiController(config,list_semesters(config)[0],clock=instant)
    controller.refresh_home=lambda:None
    window=MainWindow(controller); qtbot.addWidget(window); window.setStyleSheet(stylesheet())
    window.resize(1100,850); window.show(); controller.navigate(Route.LEARNING_PRIORITIES)
    screen=window.learning_priorities
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    assert 'Noch keine belegten Lernprioritäten' in screen.details.toPlainText()
    assert not screen.health_details.isVisible()
    old=screen.generation
    screen.course.setCurrentIndex(screen.course.findData(cid))
    screen.reload(); screen.reload()
    screen.completed('learning_priorities',old,('stale',None))
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    assert all(n.course_id==cid for n in screen.projection.needs)
    screen.priority.setCurrentIndex(screen.priority.findData('OPTIONAL'))
    assert 'Noch keine belegten Lernprioritäten' in screen.details.toPlainText()
    screen.priority.setCurrentIndex(0)
    screen.completed('learning_priorities',screen.generation,PresentationError('TEST','Test','Synthetic failure'))
    assert 'Synthetic failure' in screen.message.text()
    screen.reload(); controller.navigate(Route.CALENDAR)
    screen.completed('learning_priorities',screen.generation-1,('stale',None))
    assert not screen.active
    controller.navigate(Route.LEARNING_PRIORITIES)
    qtbot.waitUntil(lambda:screen.projection is not None,timeout=10000)
    if os.environ.get('LEARNING_QA_DIR'):
        target=Path(os.environ['LEARNING_QA_DIR']); target.mkdir(parents=True,exist_ok=True)
        backend=os.environ.get('QT_QPA_PLATFORM','windows')
        window.grab().save(str(target/f'learning-{backend}-{request.node.callspec.params["calendar_data"]}.png'))
    if os.environ.get('LEARNING_HOLD_MS'): qtbot.wait(int(os.environ['LEARNING_HOLD_MS']))
    window.resize(760,640)
    qtbot.waitUntil(lambda:window.scroll.verticalScrollBar().maximum()>0)
    assert window.scroll.horizontalScrollBar().maximum()==0
    screen.reload(); window.close()
    assert not screen.active
    screen.completed('learning_priorities',screen.generation,('stale',None))
    controller.runner.pool.waitForDone(10000)
    assert before==hashlib.sha256(config.database_path.read_bytes()).hexdigest()
