"""Shared presentation roles and stable material-only ordering. No reads/writes."""
from uniflow.gui.safe_presentation import material_expectation
MATERIAL_STATUS_COLORS = {"INFERRED": "#adb7c5", "RECORDED": "#eef1f5"}
from uniflow.gui.academic_presentation import is_group_module, course_caption

MATERIAL_ICON_COLOR = '#c7cfd9'  # neutral, never a reserved subject accent


def material_kind(kind):
    return {'SLIDES': ('Folie', 'lecture'), 'LECTURE_SLIDES': ('Folie', 'lecture'),
        'EXERCISE': ('Übungsblatt', 'exercise'), 'EXERCISE_SHEET': ('Übungsblatt', 'exercise'),
        'LECTURE': ('Vorlesungsmaterial', 'lecture'), 'NOTES': ('Notizen', 'text'),
        'SCRIPT': ('Skript', 'document')}.get(kind, ('Material', 'document'))


def forecast_status(kind, information, *, linked=False, ambiguous=False, recorded=False):
    _, icon, _, detail = material_expectation(kind, information, linked=linked)
    color = '#eef1f5' if linked else '#d8e0e9' if information in {'CONFIRMED', 'OBSERVED'} else '#adb7c5'
    if ambiguous:
        return 'Zuordnung offen', 'proposal', MATERIAL_STATUS_COLORS['INFERRED'], 'Mehrere Materialzuordnungen; keine Veröffentlichung bestätigt.'
    if recorded:
        return 'Erfasst', 'document', MATERIAL_STATUS_COLORS['RECORDED'], 'In UniFlow erfasst/importiert; kein Veröffentlichungsdatum belegt.'
    return ('Verknüpft' if linked else 'Material erwartet' if information in {'CONFIRMED', 'OBSERVED'}
        else 'Mögliches Material' if information == 'INFERRED' else 'Ungeklärt', icon, color, detail)


def planning_time(item):
    release = getattr(item, 'release_time', None) or getattr(item, 'expected_time', None)
    return release if release is not None else getattr(item, 'assumed_upload_time', None)


def compact_clock(item):
    clock = planning_time(item)
    if clock is None: return 'Zeit ungeklärt'
    explicit = getattr(item, 'release_time', None) or getattr(item, 'expected_time', None)
    return clock.strftime('%H:%M') + (' · geplant' if explicit is not None and item.information_status in {'CONFIRMED', 'OBSERVED'} else ' · vermutet')


def time_explanation(item):
    if getattr(item, 'assumed_upload_time', None) is not None:
        return 'Benutzerseitige Planungsannahme: möglicher Upload zum Beginn der eindeutig referenzierten Vorlesung; keine bestätigte Veröffentlichung.'
    if planning_time(item) is not None:
        return 'Planzeit aus der zugehörigen Materialregel; kein Nachweis einer tatsächlichen Veröffentlichung.'
    return 'Keine eindeutig zugeordnete Uploadzeit; es wird keine andere Vorlesung ersatzweise gewählt.'


def material_rule_id(item):
    explicit = getattr(item, 'rule_stable_id', None)
    identities = getattr(item, 'expectation_ids', ())
    return explicit or (identities[0].split('@',1)[0] if len(identities)==1 else '')


def provenance_explanation(stable_id, name, *, rule_id=''):
    if not is_group_module(stable_id, name): return ''
    if (rule_id or '').endswith('_PDP_EXERCISE_SHEETS'):
        return (f'Quellregel {rule_id}: gemeinsame PdP-Übung am Elternmodul. '
            'Konkrete Zuordnung zu LP/OOP/FP nicht belegt. Keine Umklassifizierung.')
    return 'Dem Elternmodul zugeordnet; eine konkrete Teilfachzuordnung zu LP/OOP/FP ist nicht belegt.'


def material_sort_key(item):
    from datetime import time
    day = getattr(item, 'release_date', None) or item.day
    clock = planning_time(item)
    # Production DTOs carry stable rule@date identities, never use DB UUIDs.
    identity = getattr(item, 'expectation_id', '') or '|'.join(getattr(item, 'expectation_ids', ()))
    return (day, clock is None, clock or time.min, identity,
        getattr(item, 'course_stable_id', '') or getattr(item, 'course_name', '').casefold(),
        item.material_kind or '', item.information_status)
