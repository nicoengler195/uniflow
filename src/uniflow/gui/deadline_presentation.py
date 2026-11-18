"""Presentation-only formatting for an existing deadline read item."""
import re


def due_day(item, zone):
    return item.due_date or (item.due_at.astimezone(zone).date() if item.due_at else None)


def is_past(item, as_of, zone):
    if item.due_date is not None:
        return item.due_date < as_of.astimezone(zone).date()
    return item.due_at.astimezone(zone) < as_of.astimezone(zone) if item.due_at else False


def timing_label(item, as_of, zone):
    day = due_day(item, zone)
    return ("Vergangen" if is_past(item, as_of, zone) else
        "Heute fällig" if day == as_of.astimezone(zone).date() else
        "Anstehend" if day else "Ohne Datum")


def display_title(title, day):
    """Remove only an exact, explicitly marked due-date suffix from display text."""
    if day is None:
        return title
    dates = (day.strftime("%d.%m.%Y"), day.isoformat())
    for date_text in dates:
        match = re.fullmatch(rf"(.+?)(\s+[·–—-]\s+(?:Abgabe|Fällig(?:keit)?))\s*:?[ ]*{re.escape(date_text)}", title, re.I)
        if match and match.group(1).strip():
            return (match.group(1) + match.group(2)).strip()
    return title
