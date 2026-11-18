"""Presentation regressions for pilot navigation, local chronology and module identity."""
from datetime import date,datetime,time,timezone
from types import SimpleNamespace as N
from uniflow.gui.academic_presentation import academic_course_visual,course_caption,local_event_sort_key
from uniflow.gui.visual_identity import resolve_course_visual
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY


def test_module_yellow_and_subjects_preserved():
    for identity,name in [('TEST_SEMESTER_PDP','Prinzipien der Programmierung'),('uuid','Prinzipien der Programmierung')]:
        visual=academic_course_visual(identity,name)
        assert visual.icon_key=='code' and visual.accent_family=='YELLOW'
        assert ACCENT_BY_FAMILY[visual.accent_family]=='#ffe36b'
        assert course_caption(identity,name)=='PdP-Modul · Teilfach offen'
    for stable,name in [('TEST_SEMESTER_MATH1','Mathematik'),('TEST_SEMESTER_PHYSICS','Einführung in die Physik I'),('TEST_SEMESTER_PDP_FP','Funktionale Programmierung'),('TEST_SEMESTER_PDP_OOP','Objektorientierte Programmierung'),('TEST_SEMESTER_PDP_LP','Linux-Praktikum')]:
        assert academic_course_visual(stable,name)==resolve_course_visual(stable,name)
        assert course_caption(stable,name)==name


def test_mixed_events_sort_local_before_limit_and_unknown_last():
    day=date(2026,11,20)
    rows=[N(day=day,starts_at=None,expected_time=None,event_id='module'),
        N(day=day,starts_at=datetime(2026,11,20,11,tzinfo=timezone.utc),expected_time=None,event_id='oop'),
        N(day=day,starts_at=datetime(2026,11,20,9,tzinfo=timezone.utc),expected_time=None,event_id='deadline'),
        N(day=day,starts_at=None,expected_time=time(8),event_id='sheet')]
    ordered=sorted(rows,key=lambda e:local_event_sort_key(e,'Europe/Berlin'))
    assert [e.event_id for e in ordered]==['sheet','deadline','oop','module']
    assert [e.event_id for e in ordered[:2]]==['sheet','deadline']


def test_calendar_initial_and_today_use_controller_clock(qtbot):
    from uniflow.gui.screens.calendar import CalendarScreen
    semester=N(name='Pilot',stable_id='TEST_SEMESTER',timezone='Europe/Berlin')
    controller=N(semester=semester,clock=lambda:datetime(2026,11,15,23,tzinfo=timezone.utc))
    screen=CalendarScreen(controller);qtbot.addWidget(screen)
    assert screen.anchor==date(2026,11,16)
    assert screen.mode.currentText()=='Woche' and screen.kind.currentText()=='Alle Ereignisse'
    screen.kind.setCurrentIndex(2);screen.anchor=date(2026,12,7)
    screen.go_today()
    assert screen.anchor==date(2026,11,16) and screen.kind.currentText()=='Fristen'
