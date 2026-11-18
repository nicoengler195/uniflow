"""Detached presentation acceptance: no database, runner, or academic writes."""
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from uniflow.application.semester_calendar import CalendarEventV1, CalendarProjectionV1
from uniflow.gui.screens.calendar import CalendarScreen


def test_day_groups_icons_types_and_source_details(qtbot):
    controller = SimpleNamespace(semester=SimpleNamespace(timezone='Europe/Berlin', name='Synthetic', stable_id='S'))
    screen = CalendarScreen(controller)
    qtbot.addWidget(screen)
    event = CalendarEventV1('private-event','RECURRING','private-source','schedule_entries','S','private-course',
        'Mathematik','LECTURE',date(2026,11,10),datetime(2026,11,10,9,tzinfo=ZoneInfo('UTC')),
        datetime(2026,11,10,11,tzinfo=ZoneInfo('UTC')),'RECURRING','CONFIRMED',location='Demoraum')
    events = (event, replace(event,event_id='exercise',title='EXERCISE'),
        replace(event,event_id='deadline',event_type='DEADLINE',day=date(2026,11,11),title='Abgabe'),
        replace(event,event_id='block',event_type='STUDY_BLOCK_CONFIRMED',day=date(2026,11,11),title='Lernen',status='CONFIRMED'))
    projection = CalendarProjectionV1('S','Synthetic','Europe/Berlin',(),events,('private-diagnostic',))
    screen.projection = projection
    screen.render()
    assert screen.displayed == list(events)
    assert screen.agenda.count() == len(events)
    assert 'Dienstag · 10.11.2026' in screen.agenda.item(0).text()
    assert 'Dienstag' not in screen.agenda.item(1).text()
    assert 'Mittwoch · 11.11.2026' in screen.agenda.item(2).text()
    assert '10:00–12:00' in screen.agenda.item(0).text()
    assert all(not screen.agenda.item(i).icon().isNull() for i in range(len(events)))
    assert 'Vorlesung' in screen.agenda.item(0).text()
    assert 'Übung' in screen.agenda.item(1).text()
    assert 'Frist' in screen.agenda.item(2).text()
    assert 'Bestätigter Lernblock' in screen.agenda.item(3).text()
    assert 'private-diagnostic' not in screen.message.text()
    screen.agenda.setCurrentRow(0)
    assert 'private-source' not in screen.details.text()
    screen.technical.click()
    assert 'schedule_entries/private-source' in screen.details.text()
    assert 'private-diagnostic' in screen.details.text()
    screen.technical.click()
    assert 'private-source' not in screen.details.text()
    assert screen.projection is projection and projection.events == events
    screen.projection = replace(projection, events=())
    screen.render()
    assert screen.agenda.count() == 0
    screen.technical.click()
    assert 'private-diagnostic' in screen.details.text()
    screen.technical.click()
    assert not screen.details.text()


def test_priorities_summary_preserves_unknown_selfreport_and_history(qtbot):
    from uniflow.application.learning_needs import LearningNeedV1, LearningNeedsProjectionV1
    from uniflow.gui.screens.learning_priorities import LearningPrioritiesScreen
    controller = SimpleNamespace(semester=SimpleNamespace(timezone='Europe/Berlin', name='Synthetic', stable_id='S'))
    screen = LearningPrioritiesScreen(controller)
    qtbot.addWidget(screen)
    stamp = datetime(2026,11,10,12,tzinfo=ZoneInfo('Europe/Berlin'))
    unknown = LearningNeedV1('private-unknown','S','C','Mathematik',('unknown',),'Kenntnisstand prüfen: Beweise',
        'NEEDS_CLARIFICATION',('Mastery unbekannt oder ungültig; fehlende Bewertung ist nicht 0.',),
        ('skills/private-unknown-source',),'Keine gültige Bewertung',('Gültige Mastery-Bewertung',))
    reported = replace(unknown,recommendation_id='private-reported',skill_ids=('reported',),priority='NORMAL',
        action='Wissenslücke bearbeiten: Rechnen',reasons=('Dokumentierter Mastery-Wert 0.3 unter Policygrenze 0.6; Methode USER_REPORTED; private-evidence.',),
        sources=('evidence/private-evidence',),evidence_quality='Lokale Bewertung (USER_REPORTED)')
    past = replace(unknown,recommendation_id='private-past',skill_ids=(),action='Frist prüfen: Vergangene Abgabe',
        reasons=('Frist: Vergangene Abgabe; Status OPEN; Information CONFIRMED.',
            'Frist bereits vergangen; Ausgang prüfen, keine neue Dringlichkeit ableiten.'))
    needs = (unknown,reported,past)
    projection = LearningNeedsProjectionV1('S',stamp,30,(('C','Mathematik'),),needs,('private-diagnostic',))
    advice = SimpleNamespace(advice_type='COMPLETED_ACTIVITY_REVIEW',priority='OPTIONAL',course_id='C',
        reasons=('Aktivität abgeschlossen; USER_REPORTED ist kein nachgewiesener Skillgewinn.',),
        user_action='Lernprioritäten öffnen',evidence_quality='USER_REPORTED',block_id='private-block',
        block_revision=1,sources=('study_block_completions/private-completion',),missing_information=())
    screen.projection=projection
    screen.health=SimpleNamespace(as_of=stamp,diagnostics=('private-health-diagnostic',),advice=(advice,))
    before=repr((projection,screen.health))
    screen.render()
    text=screen.details.toPlainText()
    assert 'UNKNOWN' in text and 'fehlende Bewertung ist nicht 0' in text
    assert 'USER_REPORTED' in text
    assert 'Frist bereits vergangen' in text and 'keine neue zukünftige Dringlichkeit' in text
    assert 'Nächste Aktion: Wissenslücke bearbeiten: Rechnen' in text
    assert 'private-' not in text + screen.health_details.toPlainText() + screen.message.text()
    assert 'USER_REPORTED' in screen.health_details.toPlainText()
    screen.technical.click()
    assert 'private-unknown-source' in screen.details.toPlainText()
    assert 'private-evidence' in screen.details.toPlainText()
    assert 'private-completion' in screen.health_details.toPlainText()
    screen.technical.click()
    assert repr((projection,screen.health))==before
    assert projection.needs == needs
    assert [n.priority for n in projection.needs]==['NEEDS_CLARIFICATION','NORMAL','NEEDS_CLARIFICATION']
