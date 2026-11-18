"""Shared calendar/dashboard identity and local ordering; no academic reassignment."""
from datetime import time
from zoneinfo import ZoneInfo
from uniflow.gui.visual_identity import CourseVisualIdentity, resolve_course_visual


def is_group_module(stable_id, name):
    return name.strip().casefold() == 'prinzipien der programmierung'


def course_caption(stable_id, name):
    return 'PdP-Modul · Teilfach offen' if is_group_module(stable_id, name) else name


def academic_course_visual(stable_id, name):
    if is_group_module(stable_id, name):
        return CourseVisualIdentity('COMPUTER_SCIENCE', 'code', 'YELLOW', 'DIGITAL_GRID')
    return resolve_course_visual(stable_id, name)


def local_event_sort_key(event, timezone):
    clock = event.starts_at.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None) if event.starts_at else event.expected_time
    return (event.day, clock is None, clock or time.min, event.event_id)
