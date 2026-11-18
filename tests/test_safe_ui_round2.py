"""Round 2 presentation contract; no academic writes or priority changes."""
from types import SimpleNamespace
from uniflow.gui.safe_presentation import PRIORITY_COLORS, material_expectation, needs_html


def test_priority_palette_and_standard_hierarchy():
    n=SimpleNamespace(course_id='c',course_name='Mathematik',priority='HIGH',action='Frist prüfen: Abgabe',
        reasons=('Frist: Abgabe; Status OPEN; Information CONFIRMED.',),evidence_quality='Bestätigte Fristdaten')
    text=needs_html([n],{'c':('uniflow:course-0','#4edfed')})
    assert all(p in PRIORITY_COLORS for p in ['CRITICAL','HIGH','NORMAL','OPTIONAL','NEEDS_CLARIFICATION'])
    assert len(set(PRIORITY_COLORS.values()))==5
    assert PRIORITY_COLORS['CRITICAL']=='#ff334a'
    assert 'uniflow:course-0' in text and PRIORITY_COLORS['HIGH'] in text
    assert 'Nächste Aktion' in text and 'Mathematik' in text
    assert all(raw not in text for raw in ['Status OPEN','Information CONFIRMED','Begründung:'])


def test_material_certainty_does_not_claim_publication():
    confirmed=material_expectation('SLIDES','CONFIRMED')
    inferred=material_expectation('SLIDES','INFERRED')
    assert confirmed[0]=='Folie erwartet' and inferred[0]=='Mögliche Folie'
    assert confirmed[1]!=inferred[1] and confirmed[2]!=inferred[2]
    assert 'Rhythmus' in confirmed[3] and 'abgeleitet' in inferred[3]
    assert 'verknüpft' in material_expectation('EXERCISE','CONFIRMED',linked=True)[0]


def test_browser_icons_and_clean_event_border(qtbot):
    from PySide6.QtWidgets import QTextBrowser
    from PySide6.QtGui import QTextDocument
    from PySide6.QtCore import QUrl
    from uniflow.gui.components import AcademicEventButton
    from uniflow.gui.screen_chrome import browser_visuals,clean_event_button
    from uniflow.gui.visual_identity import resolve_course_visual
    view=QTextBrowser();qtbot.addWidget(view)
    visuals=browser_visuals(view,[('c','Mathematik')])
    assert visuals['c'][1]=='#4edfed'
    assert not view.document().resource(QTextDocument.ResourceType.ImageResource,QUrl(visuals['c'][0])).isNull()
    button=AcademicEventButton('Mathematik',accent_family='CYAN',location='Demoraum',activity_type='LECTURE')
    qtbot.addWidget(button);clean_event_button(button,resolve_course_visual('c','Mathematik'))
    assert 'border-left' not in button.styleSheet()
    assert 'QLabel { border: none;' in button.styleSheet()


def test_dashboard_confirmation_rows_are_secondary(qtbot):
    from PySide6.QtWidgets import QLabel
    from PySide6.QtGui import QIcon
    from uniflow.gui.components import DeadlineButton
    from uniflow.gui.screen_chrome import compact_deadline_button, _CHEVRON
    from uniflow.gui.visual_identity import resolve_course_visual
    button=DeadlineButton('Abgabe',identity=resolve_course_visual('c','Mathematik'),course_name='Mathematik',
        due_text='12.11.2026',status='OPEN',information='CONFIRMED',timing='Anstehend')
    qtbot.addWidget(button);compact_deadline_button(button)
    visible=' '.join(w.text() for w in button.findChildren(QLabel) if not w.isHidden())
    assert 'Offen' in visible and 'OPEN' not in visible and 'CONFIRMED' not in visible
    assert 'OPEN' in button.toolTip() and 'CONFIRMED' in button.toolTip()
    assert not QIcon(_CHEVRON).isNull()
