"""Pure text presentation; no reads, writes, ranking, or academic inference."""
from html import escape
import re

PRIORITY_LABELS = {'CRITICAL': 'Sehr dringend', 'HIGH': 'Dringend', 'NORMAL': 'Regulär',
                   'OPTIONAL': 'Optional', 'NEEDS_CLARIFICATION': 'Klärung nötig'}


def short_reason(text):
    # Complete original evidence remains available in the existing technical view.
    text = re.sub(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{64}\b', '…', text)
    text = re.sub(r'\b[a-z_]+/[^\s;,]+', '…', text)
    return text if len(text) <= 220 else text[:217] + '…'


def need_reason(need):
    if any('Frist bereits vergangen;' in r for r in need.reasons):
        return 'Frist bereits vergangen; Ausgang prüfen, keine neue zukünftige Dringlichkeit ableiten.'
    first = need.reasons[0] if need.reasons else 'Begründung in den technischen Details prüfen.'
    if first.startswith('Mastery unbekannt oder ungültig;'):
        return f'Dieser Skill ist {need.course_name} zugeordnet. Bisher liegt keine bestätigte Bewertung vor.'
    # A mastery reason appends calculation/evidence/provenance after the first clause.
    if first.startswith('Dokumentierter Mastery-Wert '):
        value = first.split()[2]
        return f'Dokumentierter Kenntnisstand {value}; weiterer Lernbedarf aufgrund des niedrigen Werts.'
    first = first.split('; Methode', 1)[0].split(' Gespeicherte Voraussetzung', 1)[0]
    return short_reason(first)


PRIORITY_COLORS = {'CRITICAL': '#ff334a', 'HIGH': '#bd5272', 'NORMAL': '#ffa348',
                   'OPTIONAL': '#ffe36b', 'NEEDS_CLARIFICATION': '#f4f7fc'}
PRIORITY_ICONS = {'CRITICAL': 'warning', 'HIGH': 'deadline', 'NORMAL': 'session',
                  'OPTIONAL': 'expectation', 'NEEDS_CLARIFICATION': 'proposal'}


def course_heading(cid, name, priority, visuals, *, unassessed=False):
    src, color = (visuals or {}).get(cid, ('', '#4edfed'))
    icon = f'<img src="{src}" width="32" height="32"> ' if src else ''
    return (f'<p>{icon}<font color="{color}" size="5"><b>{escape(name)}</b></font><br>'
        f'<img src="uniflow:{escape(priority)}" width="20" height="20"> '
        f'<font color="{PRIORITY_COLORS.get(priority, "#f4f7fc")}"><b>{escape('Noch nicht bewertet' if unassessed else PRIORITY_LABELS.get(priority,priority))}</b></font></p>')


def active_skill_needs(needs):
    return [n for n in needs if n.skill_ids and not any('Frist bereits vergangen;' in r for r in n.reasons)]


def needs_html(needs, visuals=None):
    sections = []
    for n in active_skill_needs(needs):
        topic = n.action.partition(': ')[2] or 'Belegtes Lernthema'
        notices = []
        # Deadline context is shown only on an explicitly skill-linked need.
        for reason in n.reasons:
            if reason.startswith(('Frist:', 'Lokales Fristdatum:', 'Gespeicherte Fristuhrzeit')):
                notices.append(reason.split('; Status', 1)[0])
        if any('Status UNKNOWN' in r for r in n.reasons): notices.append('Abgabestatus unbekannt')
        if any('Information INFERRED' in r for r in n.reasons): notices.append('Planungsannahme: abgeleitete Frist')
        sections.append(f'<p><font size="5"><b>{escape(topic)}</b></font></p>'
            + course_heading(n.course_id, n.course_name, n.priority, visuals, unassessed=any(r.startswith('Mastery unbekannt oder ungültig;') for r in n.reasons))
            + f'<p>{escape(need_reason(n))}</p>'
            + ''.join(f'<p>{escape(x)}</p>' for x in notices) + '<hr>')
    return ''.join(sections) or ('Noch keine belegten Lernprioritäten für diese Auswahl. '
        'Es liegen keine zugeordneten Skills oder Themen mit Lernbedarf vor. '
        'Fristen bleiben im Dashboard und Semesterkalender sichtbar. '
        'Dies ist kein Nachweis vollständiger Beherrschung.')


def health_html(advice, courses, visuals=None):
    captions = {'TIME_CRITICAL_LEARNING_NEED': 'Zeitkritischer Lernbedarf',
        'UNCOVERED_LEARNING_NEED': 'Lernzeit noch nicht zugeordnet',
        'CONFIRMED_BLOCK_CONFLICT': 'Terminkonflikt prüfen',
        'COMPLETED_ACTIVITY_REVIEW': 'Abgeschlossene Lernaktivität'}
    sections = []
    for a in advice:
        if a.user_action == 'Lernprioritäten öffnen': continue
        sections.append(course_heading(a.course_id, courses.get(a.course_id, 'Lernplanung'), a.priority, visuals)
            + f'<p><b>{escape(captions.get(a.advice_type, "Planungshinweis"))}</b></p>'
            + f'<p>{escape(short_reason(a.reasons[0]).replace("USER_REPORTED", "Selbstbericht"))}</p>'
            + ('<p>Über „Lernblöcke planen und bestätigen“ Verfügbarkeit und Lernzeiten prüfen.</p>' if a.user_action == 'Bestehende B1-Planung öffnen' else '<p>Den betroffenen Lernblock im Semesterkalender prüfen.</p>')
            + '<hr>')
    return ''.join(sections) or 'Keine konkreten Hinweise zur bestehenden Planung für diese Auswahl.'


def material_expectation(kind, information, *, linked=False):
    singular = {'SLIDES':'Folie', 'LECTURE':'Vorlesungsmaterial', 'EXERCISE':'Übungsblatt',
                'NOTES':'Notizen', 'SCRIPT':'Skript'}.get(kind, 'Material')
    if linked: return singular + ' verknüpft', 'document', '#69d9b7', 'Mit Material verknüpft'
    if information in {'CONFIRMED','OBSERVED'}:
        return singular + ' erwartet', 'expectation', '#4edfed', 'Rhythmus bestätigt' if information=='CONFIRMED' else 'Rhythmus beobachtet'
    possible = {'Folie':'Mögliche Folie', 'Übungsblatt':'Mögliches Übungsblatt', 'Notizen':'Mögliche Notizen'}.get(singular, 'Mögliches ' + singular)
    return possible, 'proposal', '#a18af4', 'Aus dem Rhythmus abgeleitet' if information=='INFERRED' else 'Rhythmus ungeklärt'
