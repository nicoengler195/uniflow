"""Small, locally painted icons for active navigation and content."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from uniflow.gui.theme import tokens as t


class BrandMark(QWidget):
    """Scalable two-stroke UniFlow mark drawn locally in Qt."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(32, 34)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for color, start, end in (
            (t.ACCENT_MAGENTA, (8, 26), (16, 8)),
            (t.ACCENT_CYAN, (19, 26), (27, 8)),
        ):
            painter.setPen(QPen(QColor(color), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(*start, *end)
        painter.end()


def brand_icon():
    """Render the existing UniFlow mark for the native window and taskbar."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for color, start, end in (
        (t.ACCENT_MAGENTA, (14, 48), (30, 14)),
        (t.ACCENT_CYAN, (36, 48), (52, 14)),
    ):
        painter.setPen(QPen(QColor(color), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(*start, *end)
    painter.end()
    return QIcon(pixmap)


def route_icon(route, *, active=False):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(t.ACCENT_CYAN if active else t.TEXT_SECONDARY), 1.7))
    if route.value == "home":
        for x, y in ((2, 2), (13, 2), (2, 13), (13, 13)):
            painter.drawRoundedRect(QRectF(x, y, 8, 8), 2, 2)
    elif route.value in {"week", "calendar"}:
        painter.drawRoundedRect(QRectF(2, 3, 20, 18), 3, 3)
        painter.drawLine(2, 9, 22, 9)
        painter.drawEllipse(QRectF(6, 12, 3, 3))
        painter.drawEllipse(QRectF(14, 12, 3, 3))
    elif route.value == "materials":
        painter.drawRoundedRect(QRectF(4, 5, 15, 16), 2, 2)
        painter.drawLine(8, 9, 16, 9)
        painter.drawLine(8, 13, 16, 13)
        painter.drawLine(8, 17, 14, 17)
        painter.drawLine(7, 3, 20, 3)
    else:
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        painter.drawLine(12, 1, 12, 4)
        painter.drawLine(12, 12, 17, 9)
        painter.drawLine(12, 12, 17, 15)
    painter.end()
    return QIcon(pixmap)


def content_icon(kind, *, color=None):
    """Small presentation-only vector mark; format never determines academic role."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color or t.ACCENT_CYAN), 1.5))
    if kind in {"today", "calendar"}:
        painter.drawRoundedRect(QRectF(2, 4, 16, 14), 2, 2)
        painter.drawLine(2, 8, 18, 8)
        painter.drawLine(6, 2, 6, 6)
        painter.drawLine(14, 2, 14, 6)
        painter.drawEllipse(QRectF(6, 11, 2, 2))
        painter.drawEllipse(QRectF(12, 11, 2, 2))
    elif kind == "location":
        painter.drawEllipse(QRectF(5, 2, 10, 10))
        painter.drawLine(5, 9, 10, 18)
        painter.drawLine(15, 9, 10, 18)
        painter.drawEllipse(QRectF(9, 6, 2, 2))
    elif kind == "slides_upload":
        painter.drawRoundedRect(QRectF(1, 3, 12, 11), 1, 1)
        painter.drawLine(3, 7, 10, 7)
        painter.drawLine(7, 14, 7, 18)
        painter.drawLine(3, 18, 11, 18)
        painter.drawLine(16, 16, 16, 5)
        painter.drawLine(13, 8, 16, 5)
        painter.drawLine(19, 8, 16, 5)
    elif kind == "lecture":
        painter.drawLine(2, 4, 10, 7)
        painter.drawLine(10, 7, 18, 4)
        painter.drawLine(2, 4, 2, 16)
        painter.drawLine(18, 4, 18, 16)
        painter.drawLine(2, 16, 10, 19)
        painter.drawLine(10, 19, 18, 16)
        painter.drawLine(10, 7, 10, 19)
    elif kind in {"exercise", "tutorial"}:
        painter.drawRoundedRect(QRectF(3, 3, 14, 14), 2, 2)
        painter.drawLine(6, 8, 14, 8)
        painter.drawLine(6, 12, 12 if kind == "tutorial" else 14, 12)
    elif kind == "practical":
        painter.drawRoundedRect(QRectF(3, 5, 14, 12), 2, 2)
        painter.drawLine(6, 5, 6, 3)
        painter.drawLine(14, 5, 14, 3)
        painter.drawLine(6, 11, 14, 11)
    elif kind in {"deadline", "expectation"}:
        painter.drawEllipse(QRectF(3, 3, 14, 14))
        painter.drawLine(10, 5, 10, 10)
        painter.drawLine(10, 10, 14, 12)
    elif kind == "plan":
        painter.drawRoundedRect(QRectF(2, 3, 16, 14), 2, 2)
        painter.drawLine(2, 8, 18, 8)
        painter.drawLine(6, 12, 9, 15)
        painter.drawLine(9, 15, 15, 10)
    elif kind == "progress":
        for x, top in ((3, 10), (8, 6), (13, 3)):
            painter.drawRoundedRect(QRectF(x, top, 3, 16-top), 1, 1)
    elif kind in {"session", "action"}:
        painter.drawEllipse(QRectF(3, 3, 14, 14))
        painter.drawLine(8, 6, 14, 10)
        painter.drawLine(14, 10, 8, 14)
    elif kind == "repository":
        painter.drawRoundedRect(QRectF(2, 5, 16, 11), 2, 2)
        painter.drawLine(7, 8, 5, 10)
        painter.drawLine(5, 10, 7, 12)
        painter.drawLine(13, 8, 15, 10)
        painter.drawLine(15, 10, 13, 12)
    elif kind == "warning":
        painter.drawLine(10, 2, 18, 17)
        painter.drawLine(18, 17, 2, 17)
        painter.drawLine(2, 17, 10, 2)
        painter.drawLine(10, 7, 10, 12)
    elif kind == "proposal":
        painter.drawRoundedRect(QRectF(3, 3, 14, 14), 3, 3)
        painter.drawLine(6, 8, 14, 8)
        painter.drawLine(6, 12, 11, 12)
    else:
        painter.drawRoundedRect(QRectF(4, 2, 12, 16), 2, 2)
        if kind == "pdf":
            painter.drawLine(6, 11, 14, 11)
            painter.drawLine(6, 14, 12, 14)
        elif kind in {"text", "markdown"}:
            for y in (7, 10, 13):
                painter.drawLine(6, y, 14, y)
        else:
            painter.drawLine(7, 9, 13, 9)
    painter.end()
    return QIcon(pixmap)
