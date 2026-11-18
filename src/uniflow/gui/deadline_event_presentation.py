"""Read-only deadline summary shared by the agenda and dashboard preview."""
from zoneinfo import ZoneInfo
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy
from uniflow.gui.components import WrapButton, VisualBadge
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY

# Existing calendar glyph, deliberately distinct from the forecast clock and subject colors.
DEADLINE_ICON_KIND = 'calendar'
DEADLINE_ICON_COLOR = '#ff8585'
NEUTRAL_CARD_STYLE = ('QPushButton { text-align: left; background: #172441; border: 1px solid #294566; border-radius: 12px; } '
    'QPushButton:hover { background: #20324c; } QLabel { border: none; background: transparent; }')


def deadline_when(event, timezone):
    clock = event.starts_at.astimezone(ZoneInfo(timezone)).strftime('%H:%M') if event.starts_at else 'Uhrzeit unbekannt'
    return f'{event.day:%d.%m.%Y} · {clock}'


class DeadlineSummary(QWidget):
    def __init__(self, event, identity, course_name, timezone, *, framed=True):
        super().__init__()
        self.setObjectName('deadlineSummary')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        when = deadline_when(event, timezone)
        accessible = f'Frist · {event.title} · {course_name} · {when}'
        self.setAccessibleName(accessible); self.setToolTip(accessible)
        self.setStyleSheet(('QWidget#deadlineSummary { background: #172441; border: 1px solid #294566; border-radius: 10px; } '
            if framed else 'QWidget#deadlineSummary { background: transparent; border: none; } ')
            + 'QLabel { background: transparent; border: none; }')
        body=QHBoxLayout(self); body.setContentsMargins(12,8,12,8); body.setSpacing(12)
        self.badge=VisualBadge(identity,compact=True); body.addWidget(self.badge)
        column=QVBoxLayout(); column.setSpacing(4); body.addLayout(column,1)
        self.title=QLabel(event.title); self.title.setObjectName('deadlineTaskTitle')
        self.title.setWordWrap(True); self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.title.setStyleSheet(f'color: {ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 700;')
        column.addWidget(self.title)
        timing=QHBoxLayout(); timing.setSpacing(7)
        self.deadline_icon=QLabel(); self.deadline_icon.setObjectName('deadlineRoleIcon')
        self.deadline_icon.setPixmap(content_icon(DEADLINE_ICON_KIND,color=DEADLINE_ICON_COLOR).pixmap(20,20))
        self.deadline_icon.setProperty('iconKind',DEADLINE_ICON_KIND);self.deadline_icon.setProperty('iconColor',DEADLINE_ICON_COLOR)
        self.deadline_icon.setAccessibleName('Frist'); self.deadline_icon.setToolTip('Frist')
        timing.addWidget(self.deadline_icon)
        self.when=QLabel(when);self.when.setStyleSheet('color: #edf6ff;'); timing.addWidget(self.when,1)
        column.addLayout(timing)
        self.subject=QLabel(course_name);self.subject.setWordWrap(True);self.subject.setTextFormat(Qt.TextFormat.PlainText)
        self.subject.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.subject.setStyleSheet('color: #a8bdd2;');column.addWidget(self.subject)


def deadline_event_button(event, identity, course_name, timezone):
    summary=DeadlineSummary(event,identity,course_name,timezone,framed=False)
    button=WrapButton(summary.accessibleName())
    button.layout().removeWidget(button.caption);button.caption.deleteLater()
    button.caption=summary.title;button.layout().setContentsMargins(0,0,0,0)
    button.layout().addWidget(summary)
    button.setStyleSheet(NEUTRAL_CARD_STYLE);button.setToolTip(summary.accessibleName())
    button.setAccessibleName(summary.accessibleName())
    return button
