"""Small locally painted vector marks; no external artwork or icon package."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QIcon, QPixmap
from PySide6.QtWidgets import QWidget

from uniflow.gui.theme.tokens import ACCENT_BY_FAMILY


class VisualBadge(QWidget):
    def __init__(self, identity, *, compact=False):
        super().__init__()
        self.identity = identity
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(46 if compact else 76, 46 if compact else 68)
        self.setToolTip(f"{getattr(identity, 'domain_family', 'Skill')} visual: {identity.icon_key}")

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_badge(painter, self.rect(), self.identity)


def badge_icon(identity):
    """Static copy of the existing course badge; no QWidget or async lifetime."""
    pixmap = QPixmap(46, 46)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_badge(painter, pixmap.rect(), identity)
    painter.end()
    return QIcon(pixmap)


def paint_badge(painter, rect, identity):
    """Shared presentation paint path for badges and list-item icons."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(ACCENT_BY_FAMILY[identity.accent_family])
    faint = QColor(color)
    faint.setAlpha(38)
    painter.setPen(QPen(faint, 1.4))
    w, h = rect.width(), rect.height()
    artwork = getattr(identity, "artwork_family", "ACADEMIC_ABSTRACT")
    if artwork in {"MATH_CURVES", "PHYSICS_ORBITS", "NEURAL_NETWORK"}:
        painter.drawEllipse(QRectF(4, 5, w - 8, h - 10))
        painter.drawArc(QRectF(12, 13, w - 24, h - 26), 30 * 16, 230 * 16)
    elif artwork in {"DIGITAL_GRID", "CHEM_MOLECULAR"}:
        for x in range(7, w, 13):
            painter.drawLine(x, 4, x, h - 4)
        for y in range(7, h, 13):
            painter.drawLine(4, y, w - 4, y)
    else:
        painter.drawArc(QRectF(5, 8, w - 10, h - 12), 15 * 16, 150 * 16)
        painter.drawLine(8, h - 10, w - 8, h - 10)
    painter.setPen(QPen(color, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    key = identity.icon_key
    cx, cy = w / 2, h / 2
    if key in {"sigma", "code", "speech", "academic"}:
        glyph = {"sigma": "Σ", "code": "</>", "speech": "···", "academic": "✦"}[key]
        font = QFont("Segoe UI", 17 if not rect.width() < 50 else 13)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
    elif key == "atom":
        painter.drawEllipse(QRectF(cx - 18, cy - 9, 36, 18))
        painter.drawEllipse(QRectF(cx - 9, cy - 18, 18, 36))
        painter.drawEllipse(QPointF(cx, cy), 3, 3)
    elif key == "chip":
        painter.drawRoundedRect(QRectF(cx - 13, cy - 13, 26, 26), 3, 3)
        for offset in (-8, 0, 8):
            painter.drawLine(QPointF(cx - 18, cy + offset), QPointF(cx - 13, cy + offset))
            painter.drawLine(QPointF(cx + 13, cy + offset), QPointF(cx + 18, cy + offset))
    elif key in {"neural", "molecule"}:
        points = (QPointF(cx - 14, cy + 9), QPointF(cx, cy - 12), QPointF(cx + 15, cy + 8))
        painter.drawLine(points[0], points[1]); painter.drawLine(points[1], points[2]); painter.drawLine(points[0], points[2])
        for point in points:
            painter.drawEllipse(point, 3, 3)
    elif key == "leaf":
        painter.drawEllipse(QRectF(cx - 14, cy - 11, 28, 22))
        painter.drawLine(QPointF(cx - 12, cy + 12), QPointF(cx + 12, cy - 12))
    elif key == "chart":
        for offset, height in ((-13, 11), (-3, 19), (7, 26)):
            painter.drawLine(QPointF(cx + offset, cy + 15), QPointF(cx + offset, cy + 15 - height))
    elif key == "electrical_engineering":
        painter.drawRoundedRect(QRectF(cx - 10, cy - 10, 20, 20), 3, 3)
        for offset in (-5, 5):
            painter.drawLine(QPointF(cx - 15, cy + offset), QPointF(cx - 10, cy + offset))
            painter.drawLine(QPointF(cx + 10, cy + offset), QPointF(cx + 15, cy + offset))
            painter.drawLine(QPointF(cx + offset, cy - 15), QPointF(cx + offset, cy - 10))
            painter.drawLine(QPointF(cx + offset, cy + 10), QPointF(cx + offset, cy + 15))
        signal = QPainterPath(QPointF(cx - 6, cy))
        signal.cubicTo(cx - 3, cy, cx - 4, cy - 4, cx - 2, cy - 4)
        signal.cubicTo(cx, cy - 4, cx, cy + 4, cx + 2, cy + 4)
        signal.cubicTo(cx + 4, cy + 4, cx + 3, cy, cx + 6, cy)
        painter.drawPath(signal)
    elif key == "control_systems":
        painter.drawEllipse(QPointF(cx - 13, cy - 5), 3, 3)
        painter.drawRoundedRect(QRectF(cx - 4, cy - 10, 11, 10), 1.5, 1.5)
        painter.drawLine(QPointF(cx - 10, cy - 5), QPointF(cx - 4, cy - 5))
        painter.drawLine(QPointF(cx + 7, cy - 5), QPointF(cx + 17, cy - 5))
        feedback = QPainterPath(QPointF(cx + 13, cy - 5))
        feedback.lineTo(cx + 13, cy + 12)
        feedback.lineTo(cx - 13, cy + 12)
        feedback.lineTo(cx - 13, cy + 1)
        feedback.moveTo(cx - 17, cy + 5)
        feedback.lineTo(cx - 13, cy + 1)
        feedback.lineTo(cx - 9, cy + 5)
        painter.drawPath(feedback)
    elif key == "robotics":
        painter.drawEllipse(QPointF(cx - 9, cy + 6), 3, 3)
        painter.drawEllipse(QPointF(cx - 3, cy - 10), 3, 3)
        painter.drawLine(QPointF(cx - 8, cy + 3), QPointF(cx - 4, cy - 7))
        painter.drawLine(QPointF(cx, cy - 9), QPointF(cx + 11, cy - 5))
        painter.drawLine(QPointF(cx - 9, cy + 9), QPointF(cx - 9, cy + 15))
        painter.drawLine(QPointF(cx - 16, cy + 15), QPointF(cx - 2, cy + 15))
        gripper = QPainterPath(QPointF(cx + 11, cy - 5))
        gripper.lineTo(cx + 11, cy + 1)
        gripper.lineTo(cx + 6, cy + 5)
        gripper.lineTo(cx + 7, cy + 9)
        gripper.moveTo(cx + 11, cy + 1)
        gripper.lineTo(cx + 16, cy + 5)
        gripper.lineTo(cx + 15, cy + 9)
        painter.drawPath(gripper)
    else:
        painter.drawRect(QRectF(cx - 14, cy - 12, 13, 25))
        painter.drawRect(QRectF(cx + 1, cy - 12, 13, 25))
