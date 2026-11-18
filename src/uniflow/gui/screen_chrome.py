from pathlib import Path
"""Screen-local paint/text resources. No workers, signals, or data access."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QHBoxLayout
from uniflow.gui.components import VisualBadge
from uniflow.gui.components.visual_badge import badge_icon
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY
from uniflow.gui.visual_identity import resolve_course_visual

SCREEN_STYLE = """
QComboBox, QDateEdit { background: #172441; color: #edf6ff; border: 1px solid #294566;
    border-radius: 10px; padding: 8px 10px; min-height: 22px; }
QComboBox QAbstractItemView { background: #172441; color: #edf6ff;
    border: 1px solid #294566; border-radius: 10px; selection-background-color: #294566; }
QListWidget, QTextBrowser { background: #101a31; color: #edf6ff; border: 1px solid #294566;
    border-radius: 14px; padding: 10px; selection-background-color: #294566; }
QListWidget::item { border: none; border-radius: 8px; padding: 7px; margin: 2px; }
QListWidget::item:selected { background: #263a54; color: #edf6ff; }
QCalendarWidget QWidget { background: #172441; color: #edf6ff; }
QCalendarWidget QAbstractItemView { background: #172441; color: #edf6ff; border-radius: 10px; }
"""

_CHEVRON = (Path(__file__).parent/'assets/controls/control-chevron.svg').as_posix()
SCREEN_STYLE += ('QComboBox::drop-down, QDateEdit::drop-down { border: none; background: transparent; width: 24px; '
    'subcontrol-origin: padding; subcontrol-position: top right; } '
    'QComboBox::down-arrow, QDateEdit::down-arrow { image: url("' + _CHEVRON + '"); width: 12px; height: 10px; }')


def compact_deadline_button(button):
    from PySide6.QtWidgets import QLabel
    original = button.text()
    for i in range(button.layout().count()):
        row = button.layout().itemAt(i).layout()
        if row is None: continue
        labels = [row.itemAt(j).widget() for j in range(row.count())]
        labels = [w for w in labels if isinstance(w, QLabel)]
        if any(w.text().startswith('Informationsstatus:') and any(x in w.text() for x in ('CONFIRMED','OBSERVED')) for w in labels):
            for w in labels: w.hide()
        for w in labels:
            if w.text()=='Bearbeitung: OPEN': w.setText('Offen')
    button.setToolTip(original)


def clean_event_button(button, identity):
    # Scope borders to QPushButton: a bare declaration also paints child labels.
    button.setStyleSheet('QPushButton { text-align: left; background: #172441; '
        'border: 1px solid #294566; border-radius: 12px; } '
        'QPushButton:hover { background: #20324c; } QLabel { border: none; background: transparent; }')
    button.caption.setStyleSheet(f'color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 600;')
    layout = button.layout()
    layout.removeWidget(button.caption)
    heading = QHBoxLayout()
    heading.addWidget(VisualBadge(identity, compact=True))
    heading.addWidget(button.caption, 1)
    layout.insertLayout(0, heading)


def browser_visuals(browser, courses, hints=()):
    names = dict(courses)
    stable = {c.course_id: c.course_stable_id for c in hints}
    visuals = {}
    document = browser.document()
    for index, (cid, name) in enumerate(names.items()):
        identity = resolve_course_visual(stable.get(cid, cid), name)
        key = f'uniflow:course-{index}'
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(key), badge_icon(identity).pixmap(32,32).toImage())
        visuals[cid] = (key, ACCENT_BY_FAMILY[identity.accent_family])
    from uniflow.gui.safe_presentation import PRIORITY_COLORS, PRIORITY_ICONS
    for priority, color in PRIORITY_COLORS.items():
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl('uniflow:'+priority),
            content_icon(PRIORITY_ICONS[priority], color=color).pixmap(20,20).toImage())
    for key, kind in [('need','document'),('action','action'),('due','deadline')]:
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl('uniflow:'+key), content_icon(kind).pixmap(16,16).toImage())
    return visuals
