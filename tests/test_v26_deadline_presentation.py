"""Deadline role/subject separation, safe text, and forecast type distinction."""
from datetime import date,datetime,timezone
from types import SimpleNamespace as N
from PySide6.QtCore import Qt
from uniflow.gui.academic_presentation import academic_course_visual
from uniflow.gui.deadline_event_presentation import DeadlineSummary,deadline_event_button,deadline_when,DEADLINE_ICON_COLOR
from uniflow.gui.calendar_material_presentation import calendar_material_kind,calendar_material_icon
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY


def event(title='Mathematik Übungsblatt 5 Abgabe'):
    return N(title=title,day=date(2026,11,18),starts_at=datetime(2026,11,18,22,59,tzinfo=timezone.utc))


def test_deadline_subject_colors_and_shared_role(qtbot):
    assert DEADLINE_ICON_COLOR not in ACCENT_BY_FAMILY.values()
    for name,stable,color in [('Mathematik','TEST_SEMESTER_MATH1','#4edfed'),('Einführung in die Physik I','TEST_SEMESTER_PHYS1','#69aef4'),('Objektorientierte Programmierung','TEST_SEMESTER_PDP_OOP','#efbe78')]:
        identity=academic_course_visual(stable,name)
        summary=DeadlineSummary(event(),identity,name,'Europe/Berlin');qtbot.addWidget(summary)
        assert color in summary.title.styleSheet()
        assert summary.badge.identity==identity
        assert summary.when.text()=='18.11.2026 · 23:59'
        assert summary.deadline_icon.property('iconColor')==DEADLINE_ICON_COLOR
        assert summary.deadline_icon.property('iconKind')=='calendar'
        assert summary.deadline_icon.accessibleName()=='Frist'
        assert 'Frist' in summary.accessibleName()
        button=deadline_event_button(event(),identity,name,'Europe/Berlin');qtbot.addWidget(button)
        assert 'border: 1px solid #294566' in button.styleSheet()
        assert '#efb45d' not in button.styleSheet()
        assert 'Frist' in button.accessibleName() and '23:59' in button.accessibleName()
        clicked=[];button.clicked.connect(lambda:clicked.append(True));button.show();qtbot.mouseClick(button,Qt.MouseButton.LeftButton)
        assert clicked


def test_unknown_time_and_task_title_are_not_interpreted(qtbot):
    e=event('<b>Keine HTML-Aufgabe</b>');e.starts_at=None
    assert deadline_when(e,'Europe/Berlin')=='18.11.2026 · Uhrzeit unbekannt'
    summary=DeadlineSummary(e,academic_course_visual('TEST_SEMESTER_MATH1','Mathematik'),'Mathematik','Europe/Berlin');qtbot.addWidget(summary)
    assert summary.title.textFormat()==Qt.TextFormat.PlainText
    assert summary.title.text()==e.title


def test_material_task_slide_and_deadline_have_different_existing_symbols(qtbot):
    assert calendar_material_kind('ARTEMIS_TASK')==('Artemis-Aufgabe','exercise')
    assert calendar_material_kind('LECTURE_SLIDES')==('Folie','lecture')
    assert calendar_material_kind('EXERCISE_SHEET')==('Übungsblatt','exercise')
    icons=[]
    for kind in ['ARTEMIS_TASK','LECTURE_SLIDES']:
        e=N(material_kind=kind,information_status='CONFIRMED',status='NOT_LINKED_IN_UNIFLOW')
        identity=academic_course_visual('TEST_SEMESTER_PDP_OOP','Objektorientierte Programmierung')
        icons.append(calendar_material_icon(identity,e).pixmap(58,48).toImage())
        assert e.information_status=='CONFIRMED' and identity.accent_family=='AMBER'
    assert icons[0]!=icons[1]
    # A title containing Artemis never changes the real shared-module identity.
    identity=academic_course_visual('TEST_SEMESTER_PDP','Prinzipien der Programmierung')
    assert identity.accent_family=='YELLOW'
