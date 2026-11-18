from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QPainter
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.theme import tokens as theme_tokens
from uniflow.gui.components.visual_badge import VisualBadge
from uniflow.gui.components.artwork import LayeredCanvas, paint_artwork
from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY


class BaseCard(QFrame):
    def __init__(self, title=None, *, hero=False, variant=None, artwork_family=None,
                 accent_family="CYAN", icon_kind=None):
        super().__init__()
        self.setObjectName("hero" if hero else variant or "card")
        self.artwork_family = artwork_family
        self.accent_family = accent_family
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(24, 21, 24, 21)
        self.body.setSpacing(10)
        if title:
            if icon_kind:
                heading = QHBoxLayout()
                heading.setSpacing(9)
                symbol = QLabel()
                symbol.setPixmap(content_icon(icon_kind).pixmap(20, 20))
                symbol.setFixedSize(20, 20)
                heading.addWidget(symbol)
                heading.addWidget(SectionHeader(title), 1)
                self.body.addLayout(heading)
            else:
                self.body.addWidget(SectionHeader(title))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.artwork_family:
            painter = QPainter(self)
            color = QColor(ACCENT_BY_FAMILY[self.accent_family])
            paint_artwork(painter, QRectF(self.width() * .54, 2, self.width() * .44, self.height() - 4),
                self.artwork_family, color)


class SectionHeader(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.setObjectName("heading")


class StatusChip(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.setObjectName("chip")


class EmptyState(BaseCard):
    def __init__(self, title, detail):
        super().__init__(title)
        value = QLabel(detail)
        value.setWordWrap(True)
        value.setObjectName("muted")
        self.body.addWidget(value)


class FeedbackBanner(BaseCard):
    def __init__(self):
        super().__init__()
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.body.addWidget(self.message)
        self.hide()

    def show_feedback(self, value):
        self.message.setText(value.message if hasattr(value, "message") else str(value))
        self.show()


class PrimaryButton(QPushButton):
    def __init__(self, text):
        super().__init__(text)
        self.setObjectName("primary")


class SecondaryButton(QPushButton):
    pass


class WrapButton(QPushButton):
    """An accessible button whose label can wrap without imposing its text width."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.caption = QLabel(text)
        self.caption.setWordWrap(True)
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.caption)

    def text(self):
        return self.caption.text()

    def sizeHint(self):
        native = super().sizeHint()
        content = self.layout().sizeHint() if self.layout() else native
        return native.expandedTo(content)

    def minimumSizeHint(self):
        native = super().sizeHint()
        if not self.layout():
            return native
        # Keep the native minimum width so long labels can wrap in narrow cards.
        return QSize(native.width(), max(native.height(), self.layout().minimumSize().height()))

    def heightForWidth(self, width):
        if not self.layout():
            return super().heightForWidth(width)
        return max(super().sizeHint().height(), self.layout().heightForWidth(width))


class AcademicEventButton(WrapButton):
    """Course-led event row with restrained factual type and location metadata."""

    _TYPES = {
        "LECTURE": ("Vorlesung", "lecture"),
        "EXERCISE": ("Übung", "exercise"),
        "TUTORIAL": ("Tutorium", "tutorial"),
        "PRACTICAL": ("Praktikum", "practical"),
        "LAB": ("Praktikum", "practical"),
    }

    def __init__(self, text, *, accent_family, activity_type=None, location=None):
        super().__init__(text)
        accent = theme_tokens.ACCENT_BY_FAMILY[accent_family]
        self.setStyleSheet(f"QPushButton {{ border-left: 3px solid {accent}; }}")
        self.caption.setStyleSheet(f"color: {accent}; font-weight: 600;")
        metadata = QHBoxLayout()
        metadata.setSpacing(6)
        kind = self._TYPES.get((activity_type or "").upper())
        if kind:
            self._add_meta(metadata, kind[0], kind[1])
        if location:
            self._add_meta(metadata, location, "location")
        if kind or location:
            metadata.addStretch()
            self.layout().addLayout(metadata)

    def _add_meta(self, layout, value, icon_kind):
        symbol = QLabel()
        symbol.setPixmap(content_icon(icon_kind, color=theme_tokens.TEXT_SECONDARY).pixmap(16, 16))
        symbol.setFixedSize(16, 16)
        symbol.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label = QLabel(value)
        label.setObjectName("muted")
        label.setWordWrap(True)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(symbol)
        layout.addWidget(label)


class DeadlineButton(WrapButton):
    """A deadline row with separate course, due instant, and work status."""

    def __init__(self, title, *, identity, course_name, due_text, status,
                 information, timing):
        super().__init__(title)
        self.setProperty("deadlineTiming", timing)
        if timing == "Vergangen" and status == "OPEN":
            self.setStyleSheet(f"QPushButton {{ border-left: 3px solid {theme_tokens.DANGER}; }}")
        elif timing == "Heute fällig":
            self.setStyleSheet(f"QPushButton {{ border-left: 3px solid {theme_tokens.WARNING}; }}")
        self.caption.setStyleSheet("font-weight: 700;")
        heading = QHBoxLayout()
        heading.setSpacing(8)
        badge = VisualBadge(identity, compact=True)
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        heading.addWidget(badge)
        course = QLabel(course_name)
        course.setWordWrap(True)
        course.setStyleSheet(f"color: {theme_tokens.ACCENT_BY_FAMILY[identity.accent_family]}; font-weight: 600;")
        course.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        heading.addWidget(course, 1)
        self.layout().insertLayout(0, heading)
        self._meta("deadline", f"{timing} · {due_text}", emphasis=True)
        self._meta("proposal", f"Bearbeitung: {status}")
        self._meta("document", f"Informationsstatus: {information}")
        self.setAccessibleName(self.text())

    def _meta(self, icon_kind, value, *, emphasis=False):
        row = QHBoxLayout()
        row.setSpacing(7)
        symbol = QLabel()
        symbol.setPixmap(content_icon(icon_kind, color=theme_tokens.TEXT_SECONDARY).pixmap(16, 16))
        symbol.setFixedSize(16, 16)
        symbol.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(symbol)
        label = QLabel(value)
        label.setObjectName("muted")
        if emphasis:
            label.setStyleSheet("font-weight: 700;")
        label.setWordWrap(True)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(label, 1)
        self.layout().addLayout(row)

    def text(self):
        parts = [label.text() for label in self.findChildren(QLabel) if label.text()]
        return " · ".join(parts)
