"""Synthetic evidence and presentation boundaries; no pilot writes."""
from dataclasses import replace
from datetime import time
from test_semester_calendar import calendar_data
from test_learning_needs import instant
from uniflow.application.learning_needs import project_learning_needs
from uniflow.infrastructure.database.models import Skill, CourseSkill, MasteryState
from uniflow.gui.safe_presentation import needs_html, health_html
from uniflow.gui.dashboard_materials import material_sort_key


def test_evidenced_skill_first_and_unmapped_deadlines_absent(calendar_data):
    config, factory, cid, _ = calendar_data
    with factory.begin() as db:
        db.add(Skill(id='synth-linear', name='Lineare Gleichungssysteme')); db.flush()
        db.add(CourseSkill(course_id=cid, skill_id='synth-linear'))
        db.add(MasteryState(skill_id='synth-linear', value=.2, calculated_at=instant(11), calculation_method='synthetic'))
    projection=project_learning_needs(config,'SYNTH',instant(),cid)
    skill=next(n for n in projection.needs if n.skill_ids)
    html=needs_html(projection.needs)
    assert html.index('Lineare Gleichungssysteme') < html.index(skill.course_name)
    assert 'Kenntnisstand 0.2' in html
    assert 'Frist prüfen' not in html and 'Nächste Aktion' not in html
    assert 'skills/synth-linear' in skill.sources
    context=replace(skill,reasons=skill.reasons+('Frist: Synthetische Abgabe; Status UNKNOWN; Information INFERRED.',))
    html=needs_html([context])
    assert 'Synthetische Abgabe' in html and 'Abgabestatus unbekannt' in html
    assert 'Planungsannahme: abgeleitete Frist' in html
    assert 'UNKNOWN' not in html and 'INFERRED' not in html
    assert 'Noch keine belegten Lernprioritäten' in needs_html([n for n in projection.needs if not n.skill_ids])


def test_unknown_clock_sorts_last_and_generic_health_hidden():
    from types import SimpleNamespace as N
    from datetime import date
    base=dict(day=date(2026,11,16),information_status='INFERRED',material_kind='SLIDES')
    rows=[N(**base,expected_time=t) for t in (None,time(16),time(12),time(14,15))]
    assert [r.expected_time for r in sorted(rows,key=material_sort_key)] == [time(12),time(14,15),time(16),None]
    assert 'Klärung offen' not in health_html([N(advice_type='NEEDS_CLARIFICATION',user_action='Lernprioritäten öffnen')],{})
