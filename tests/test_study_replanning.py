"""C2 acceptance using existing synthetic B1/B2/C1 fixtures."""
from datetime import datetime, time, timezone
import hashlib
import inspect
import os
import pytest
from sqlalchemy import text
from test_study_blocks import data, window, preview, apply, NOW, MON, ZONE
from test_study_block_changes import block, command, change
from test_study_block_completion import complete
from test_planning_calendar import entry
from uniflow.application.study_replanning import project_study_plan_health


def health(data, at=NOW, course=None):
    return project_study_plan_health(data[0], 'S', at, course)


def kinds(p, kind):
    return [a for a in p.advice if a.advice_type == kind]


def test_urgent_determinism_readonly_and_missing(data):
    before=hashlib.sha256(data[0].database_path.read_bytes()).hexdigest()
    p=health(data)
    assert p==health(data, NOW.astimezone(timezone.utc))
    assert kinds(p,'TIME_CRITICAL_LEARNING_NEED')[0].priority=='CRITICAL'
    assert kinds(p,'UNCOVERED_LEARNING_NEED')
    assert all(a.target_period is None for a in p.advice)
    assert any('Zeitbedarf' in a.missing_information for a in p.advice)
    assert any('Bestätigte Lernverfügbarkeit' in a.missing_information for a in p.advice)
    assert all(a.sources for a in kinds(p,'TIME_CRITICAL_LEARNING_NEED'))
    assert before==hashlib.sha256(data[0].database_path.read_bytes()).hexdigest()
    assert all('Seit der' not in str(a.reasons) for a in p.advice)
    with pytest.raises(ValueError):health(data,datetime(2026,11,5))
    with pytest.raises(ValueError):health(data,course='absent')
    with pytest.raises(ValueError):project_study_plan_health(data[0],'absent',NOW)


def test_coverage_current_revision_cancel_and_past(data,block):
    p=health(data); assert len(p.covered_need_ids)==1 and p.confirmed_blocks==1
    assert not kinds(p,'CONFIRMED_BLOCK_CONFLICT')
    change(data,command(data,block,begin=time(12)))
    entry(data[1],data[2][0],start_time='09:00',end_time='10:00')
    assert not kinds(health(data),'CONFIRMED_BLOCK_CONFLICT')
    p=health(data,datetime(2026,11,10,12,tzinfo=ZONE))
    assert not p.covered_need_ids and p.confirmed_blocks==1
    assert any('Vergangener' in str(a.reasons) and a.block_revision==2 for a in p.advice)
    change(data,command(data,block,cancel=True),request='cancel')
    p=health(data); assert p.cancelled_blocks==1 and p.confirmed_blocks==0 and not p.covered_need_ids


def test_conflict_sources_and_course_scope(data,block):
    entry(data[1],data[2][1],start_time='09:00',end_time='10:00')
    p=health(data)
    conflicts=kinds(p,'CONFIRMED_BLOCK_CONFLICT'); assert len(conflicts)==1
    assert any(s.startswith('schedule_entries/') for s in conflicts[0].sources)
    assert conflicts[0].block_id==block and conflicts[0].block_revision==1
    assert not kinds(health(data,course=data[2][1]),'CONFIRMED_BLOCK_CONFLICT')


def test_completed_user_report_keeps_priority(data,block):
    before=kinds(health(data),'TIME_CRITICAL_LEARNING_NEED')
    complete(data,block)
    p=health(data)
    assert p.completed_blocks==1 and p.confirmed_blocks==0 and not p.covered_need_ids
    a=kinds(p,'COMPLETED_ACTIVITY_REVIEW')[0]
    assert a.evidence_quality=='USER_REPORTED' and 'kein nachgewiesener Skillgewinn' in a.reasons[0]
    assert kinds(p,'TIME_CRITICAL_LEARNING_NEED')==before
    assert not kinds(p,'CONFIRMED_BLOCK_CONFLICT')


def test_no_name_matching_legacy_provenance(data,block,monkeypatch):
    import uniflow.application.study_replanning as module
    original=module.effective_blocks
    def legacy(*args, **kwargs):
        result=original(*args, **kwargs)
        for b in result: b.update(need_id=None,need_policy=None)
        return result
    monkeypatch.setattr(module,'effective_blocks',legacy)
    p=health(data)
    assert not p.covered_need_ids
    assert len({a.advice_id for a in p.advice}) == len(p.advice)
    assert not [a for a in kinds(p,'UNCOVERED_LEARNING_NEED') if a.course_id==data[2][0]]
    assert any('Zuordnung' in str(a.reasons) for a in kinds(p,'NEEDS_CLARIFICATION'))


@pytest.mark.parametrize('revision',['0017_audit_resolution','0019_scoring_mastery','0020_study_blocks','0021_study_block_changes'])
def test_legacy_readonly(tmp_path,revision):
    from test_m1_4_b3_startup_schema import schema_project
    config=schema_project(tmp_path,revision)
    before=config.database_path.read_bytes()
    p=project_study_plan_health(config,'SYN',NOW)
    assert p.confirmed_blocks==0 and p.completed_blocks==0
    assert before==config.database_path.read_bytes()


def test_gui_generation_and_navigation(data,qtbot):
    from uniflow.gui.controller import GuiController
    from uniflow.application.semester_read_models import list_semesters
    from uniflow.gui.screens.learning_priorities import LearningPrioritiesScreen
    c=GuiController(data[0],list_semesters(data[0])[0]);c.clock=lambda:NOW
    screen=LearningPrioritiesScreen(c);qtbot.addWidget(screen);screen.resize(1050,1000);screen.show();screen.activate(True)
    qtbot.waitUntil(lambda:screen.health is not None,timeout=15000)
    assert 'TIME_CRITICAL_LEARNING_NEED' not in screen.health_details.toPlainText()
    screen.technical.click()
    assert 'TIME_CRITICAL_LEARNING_NEED' in screen.health_details.toPlainText()
    screen.technical.click()
    for _ in range(5):screen.reload()
    screen.course.setCurrentIndex(1)
    qtbot.waitUntil(lambda:screen.health is not None,timeout=15000)
    assert all(a.course_id in (None,screen.course.currentData()) for a in screen.health.advice)
    old=screen.health;screen.completed('learning_priorities',screen.generation-1,('invalid',))
    assert screen.health==old
    if os.environ.get('C2_SCREENSHOT'):screen.grab().save(os.environ['C2_SCREENSHOT'])
    screen.activate(False);c.runner.pool.waitForDone(10000)


def test_no_c2_write_api():
    import uniflow.application.study_replanning as module
    source=inspect.getsource(module)
    assert not any(word in source for word in ('INSERT ', 'UPDATE ', 'DELETE ', '.commit(', 'confirm_study', 'project_write_lock'))


def test_exception_cancellation_and_personal_constraint(data,block):
    from test_planning_calendar import exception
    from uniflow.application.planning_calendar import create_planning_profile, add_calendar_constraint
    entry(data[1],data[2][0],start_time='09:00',end_time='10:00')
    assert kinds(health(data),'CONFIRMED_BLOCK_CONFLICT')
    exception(data[1],data[2][0],'cancel','CANCELLATION',day=MON)
    assert not kinds(health(data),'CONFIRMED_BLOCK_CONFLICT')
    create_planning_profile(data[0],'S')
    c=add_calendar_constraint(data[0],'S',constraint_type='BLOCKED',starts_at=datetime(2026,11,9,9,tzinfo=ZONE),ends_at=datetime(2026,11,9,10,tzinfo=ZONE),label='synthetic')
    assert any('calendar_constraints/'+c['id'] in a.sources for a in kinds(health(data),'CONFIRMED_BLOCK_CONFLICT'))


def test_dst_calendar_is_clarification(data,monkeypatch,block):
    import uniflow.application.study_replanning as module
    def ambiguous(*args,**kwargs): raise ValueError('AMBIGUOUS_ACADEMIC_LOCAL_TIME')
    monkeypatch.setattr(module,'_project_calendar_week_in_transaction',ambiguous)
    p=health(data)
    assert not kinds(p,'CONFIRMED_BLOCK_CONFLICT')
    assert any('AMBIGUOUS_ACADEMIC_LOCAL_TIME' in a.missing_information for a in kinds(p,'NEEDS_CLARIFICATION'))


def test_occurrence_uncertainty_with_source(data,block):
    identity=entry(data[1],data[2][0],start_time='11:00',end_time='12:00',participation='UNKNOWN')
    p=health(data)
    assert any('schedule_entries/'+identity in a.sources and any('Ungeklärte Veranstaltung' in m for m in a.missing_information)
               for a in kinds(p,'NEEDS_CLARIFICATION'))
