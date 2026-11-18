from types import SimpleNamespace as N
from uniflow.gui.safe_presentation import needs_html
from uniflow.gui.academic_presentation import academic_course_visual, course_caption

def test_unassessed_assigned_skill_explains_actual_course():
    n=N(skill_ids=('accel',),action='Lernen: Momentanbeschleunigung',course_id='phys',course_name='Physik I',priority='NEEDS_CLARIFICATION',reasons=('Mastery unbekannt oder ungültig; Test',))
    html=needs_html([n])
    assert 'Momentanbeschleunigung' in html
    assert 'Noch nicht bewertet' in html
    assert 'Dieser Skill ist Physik I zugeordnet. Bisher liegt keine bestätigte Bewertung vor.' in html
    n.reasons=('Dokumentierter Mastery-Wert 0.2; Methode Test',);n.priority='HIGH'
    assert 'Dringend' in needs_html([n]) and 'Noch nicht bewertet' not in needs_html([n])

def test_shared_pdp_identity_keeps_group_and_programming_icon():
    v=academic_course_visual('TEST_SEMESTER_PDP','Prinzipien der Programmierung')
    assert (v.icon_key,v.accent_family)==('code','YELLOW')
    assert 'Teilfach offen' in course_caption('TEST_SEMESTER_PDP','Prinzipien der Programmierung')

def test_recorded_material_card_keeps_filename_subject_and_local_time(qtbot):
    from datetime import datetime,date,timezone
    from PySide6.QtWidgets import QWidget,QVBoxLayout,QLabel
    from uniflow.gui.screens.dashboard_summary import DashboardSummary
    from uniflow.gui.components import BaseCard
    from uniflow.gui.components.nav_icons import content_icon
    owner=QWidget();qtbot.addWidget(owner)
    c=N(home=N(courses=[N(course_id='phys',course_stable_id='TEST_SEMESTER_PHYSIK',name='Physik I')]))
    screen=N(controller=c,projection=N(timezone='Europe/Berlin'))
    card=BaseCard('Test');qtbot.addWidget(card)
    event=N(event_type='MATERIAL_OBSERVED',course_id='phys',course_name='Physik I',material_kind='SLIDES',information_status='OBSERVED',status='RECORDED',title='Physikfolie',notes='Datei: Physik_01.pdf; Herkunft: test',starts_at=datetime(2026,11,16,10,15,tzinfo=timezone.utc),day=date(2026,11,16))
    DashboardSummary.add_material_event(screen,card,event)
    labels=card.findChildren(QLabel)
    assert any('Physik_01.pdf' in w.text() for w in labels)
    assert any('11:15' in w.text() for w in labels)
    assert any(w.accessibleName()=='Material erfasst' and w.toolTip()=='Material erfasst' for w in labels)
    assert all('Material erfasst/importiert' not in w.text() for w in labels)
    assert not content_icon('slides_upload').isNull()
