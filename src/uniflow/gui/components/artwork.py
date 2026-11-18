"""Low-contrast local artwork motifs for presentation cards."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from uniflow.gui.theme import tokens as t
from uniflow.gui.assets.loader import scaled_pixmap


class PackagedArtwork(QWidget):
    """Rounded, cropped production artwork with graceful local fallback."""

    FOCUS = {
        "courses/mathematics.png": (.29, .46),
        "courses/physics.png": (.55, .45),
        "courses/neuroscience.png": (.65, .43),
        "courses/programming.png": (.40, .43),
    }

    def __init__(self, asset_key: str, *, mode: str = "course", fallback_family: str = "ACADEMIC_ABSTRACT"):
        super().__init__()
        self.asset_key = asset_key
        self.mode = mode
        self.fallback_family = fallback_family
        self.setMinimumHeight(0 if mode == "thumbnail" else 98 if mode == "course" else 180)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(area, 14, 14)
        p.setClipPath(clip)
        dpr = self.devicePixelRatioF()
        image = scaled_pixmap(self.asset_key, max(1, round(self.width() * dpr)),
            max(1, round(self.height() * dpr)))
        if image.isNull() and self.asset_key != "courses/generic_academic.png":
            image = scaled_pixmap("courses/generic_academic.png", max(1, round(self.width() * dpr)),
                max(1, round(self.height() * dpr)))
        if image.isNull():
            p.fillRect(area, QColor(t.SURFACE_ELEVATED))
            paint_artwork(p, area, self.fallback_family, QColor(t.ACCENT_CYAN))
        else:
            logical_w = image.width() / dpr
            logical_h = image.height() / dpr
            focus_x, focus_y = self.FOCUS.get(self.asset_key, (.5, .5))
            offset_x = min(0, max(self.width() - logical_w, self.width() / 2 - logical_w * focus_x))
            offset_y = min(0, max(self.height() - logical_h, self.height() / 2 - logical_h * focus_y))
            if self.mode == "sidebar":
                offset_x = 0
            p.drawPixmap(QRectF(offset_x, offset_y, logical_w, logical_h),
                image, QRectF(image.rect()))
        overlay = QLinearGradient(0, 0, self.width() if self.mode == "hero" else 0,
            self.height() if self.mode != "hero" else 0)
        if self.mode == "hero":
            overlay.setColorAt(0, QColor(8, 13, 33, 165))
            overlay.setColorAt(.34, QColor(8, 13, 33, 40))
            overlay.setColorAt(1, QColor(8, 13, 33, 0))
        else:
            overlay.setColorAt(0, QColor(8, 13, 33, 0))
            overlay.setColorAt(.55, QColor(8, 13, 33, 0))
            overlay.setColorAt(1, QColor(8, 13, 33, 190))
        p.fillRect(area, overlay)
        p.end()


class SunsetScene(QWidget):
    """Decorative study scene; it conveys no schedule or progress facts."""

    def __init__(self, *, compact=False):
        super().__init__()
        self.compact = compact
        self.setMinimumHeight(100 if compact else 180)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        sky = QLinearGradient(0, 0, w, h)
        for at, color in ((0, '#101b4a'), (.38, '#4b286d'), (.7, '#e46b9c'), (1, '#f7aa78')):
            sky.setColorAt(at, QColor(color))
        p.fillRect(self.rect(), sky)
        sun = QRadialGradient(w * .72, h * .51, h * .34)
        sun.setColorAt(0, QColor(255, 224, 174, 235))
        sun.setColorAt(.34, QColor(255, 156, 170, 100))
        sun.setColorAt(1, QColor(255, 156, 170, 0))
        p.fillRect(self.rect(), sun)
        p.setBrush(QColor('#ffc5a0'))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(w * .72, h * .52), h * .075, h * .075)
        # A quiet skyline and a desk silhouette provide the sunset/study cue.
        p.setBrush(QColor('#172144'))
        for a, height in ((.47,.20),(.53,.29),(.59,.16),(.64,.37),(.8,.24),(.87,.31),(.93,.18)):
            p.drawRect(QRectF(w * a, h * (.78-height), w * .035, h * height))
        p.fillRect(QRectF(0, h * .77, w, h * .23), QColor('#0a1530'))
        p.setPen(QPen(QColor('#4b72a2'), 1.5))
        p.drawLine(QPointF(0, h * .78), QPointF(w, h * .78))
        p.setBrush(QColor('#081229'))
        p.setPen(QPen(QColor('#6287c8'), 1.5))
        laptop = QPainterPath(QPointF(w * .39, h * .82))
        laptop.lineTo(w * .34, h * .50)
        laptop.lineTo(w * .59, h * .50)
        laptop.lineTo(w * .63, h * .82)
        laptop.closeSubpath()
        p.drawPath(laptop)
        p.setPen(QPen(QColor('#28d8f4'), 2))
        p.drawLine(QPointF(w * .35, h * .51), QPointF(w * .58, h * .51))
        p.setBrush(QColor('#111735'))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(w * .27, h * .83, w * .45, h * .035), 4, 4)
        for offset in range(3):
            p.setBrush(QColor('#1b244b'))
            p.drawRoundedRect(QRectF(w * (.67 + offset * .02), h * (.82 - offset * .035), w * .17, h * .033), 2, 2)
        p.end()


class CourseScene(QWidget):
    """Small local illustrations keyed only by cosmetic identity."""

    def __init__(self, family: str, accent: QColor):
        super().__init__()
        self.family, self.accent = family, accent
        self.setFixedHeight(82)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base = QLinearGradient(0, 0, w, h)
        base.setColorAt(0, QColor('#102243'))
        base.setColorAt(.65, QColor('#1c1d54'))
        base.setColorAt(1, QColor(self.accent).darker(230))
        p.fillRect(self.rect(), base)
        glow = QRadialGradient(w * .78, h * .43, h * .9)
        center = QColor(self.accent); center.setAlpha(100)
        edge = QColor(self.accent); edge.setAlpha(0)
        glow.setColorAt(0, center); glow.setColorAt(1, edge)
        p.fillRect(self.rect(), glow)
        if self.family == 'MATH_CURVES':
            p.setBrush(QColor('#102d52')); p.setPen(QPen(self.accent, 1.2))
            mountain = QPainterPath(QPointF(0, h))
            for x, y in ((w*.21,h*.57),(w*.33,h*.74),(w*.58,h*.22),(w*.79,h*.7),(w,h*.42),(w,h)):
                mountain.lineTo(x, y)
            p.drawPath(mountain)
        elif self.family == 'PHYSICS_ORBITS':
            p.setBrush(QColor('#29346d')); p.setPen(QPen(QColor(self.accent), 1.5))
            p.drawEllipse(QPointF(w*.77,h*.56),h*.43,h*.43)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(w*.48,h*.36,w*.58,h*.28))
        elif self.family in {'NEURAL_NETWORK', 'CHEM_MOLECULAR'}:
            paint_artwork(p, QRectF(w*.33, 0, w*.67, h), self.family, self.accent)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(self.accent)
            p.drawEllipse(QPointF(w*.72,h*.45),5,5)
        elif self.family == 'DATA_CONSTELLATION':
            p.setPen(QPen(self.accent, 1.5))
            points = [QPointF(w*a, h*b) for a, b in ((.39,.68),(.49,.43),(.61,.54),(.72,.26),(.83,.38),(.94,.17))]
            for first, second in zip(points, points[1:]):
                p.drawLine(first, second)
            p.setBrush(self.accent)
            for point in points:
                p.drawEllipse(point, 3, 3)
        elif self.family == 'DIGITAL_GRID':
            paint_artwork(p, QRectF(w*.32, 0, w*.68, h), self.family, self.accent)
            p.setPen(QPen(self.accent, 2))
            path = QPainterPath(QPointF(w*.43,h*.72))
            path.lineTo(w*.55,h*.35); path.lineTo(w*.65,h*.62); path.lineTo(w*.84,h*.2)
            p.drawPath(path)
        else:
            paint_artwork(p, QRectF(w*.32, 0, w*.68, h), self.family, self.accent)
        p.end()


def paint_artwork(painter: QPainter, area: QRectF, family: str, color: QColor):
    """Paint a sparse motif into an existing Qt painter."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(area)
    ink = QColor(color)
    ink.setAlpha(38)
    painter.setPen(QPen(ink, 1.4))
    x, y, w, h = area.x(), area.y(), area.width(), area.height()
    if family == "MATH_CURVES":
        for shift in (0, 24, 48):
            path = QPainterPath(QPointF(x, y + h * .65 + shift))
            path.cubicTo(x + w * .3, y - 30 + shift, x + w * .55, y + h + 20 + shift, x + w, y + h * .25 + shift)
            painter.drawPath(path)
    elif family == "PHYSICS_ORBITS":
        for inset in (10, 37, 65):
            painter.drawEllipse(QRectF(x + inset, y + inset * .4, w - 2 * inset, h - inset))
        painter.drawEllipse(QPointF(x + w * .74, y + h * .34), 3, 3)
    elif family in {"NEURAL_NETWORK", "CHEM_MOLECULAR"}:
        points = [QPointF(x + w * a, y + h * b) for a, b in ((.09,.35),(.28,.12),(.45,.61),(.67,.22),(.85,.72),(.93,.35))]
        for a, b in ((0,1),(0,2),(1,3),(2,3),(2,4),(3,5),(4,5)):
            painter.drawLine(points[a], points[b])
        for point in points:
            painter.drawEllipse(point, 3.5, 3.5)
    elif family == "DIGITAL_GRID":
        for offset in range(0, int(w), 23):
            painter.drawLine(QPointF(x + offset, y), QPointF(x + offset, y + h))
        for offset in range(0, int(h), 23):
            painter.drawLine(QPointF(x, y + offset), QPointF(x + w, y + offset))
    elif family == "DATA_CONSTELLATION":
        points = [QPointF(x + w * a, y + h * b) for a, b in ((.05,.76),(.25,.43),(.43,.59),(.64,.24),(.82,.38),(.96,.12))]
        for first, second in zip(points, points[1:]):
            painter.drawLine(first, second)
        for point in points:
            painter.drawEllipse(point, 3, 3)
    elif family == "BIO_ORGANIC":
        for offset in (0, 32, 65):
            path = QPainterPath(QPointF(x + offset, y + h))
            path.cubicTo(x + offset + 40, y + h * .6, x + offset - 22, y + h * .3, x + offset + 46, y)
            painter.drawPath(path)
    elif family == "COASTAL_RETRO":
        painter.drawArc(QRectF(x + w * .55, y + h * .1, h * .75, h * .75), 0, 180 * 16)
        for offset in (0, 16, 32):
            painter.drawLine(QPointF(x, y + h * .65 + offset), QPointF(x + w, y + h * .65 + offset))
    else:
        for offset in (0, 27, 54):
            painter.drawRoundedRect(QRectF(x + offset, y + offset * .4, w * .35, h * .54), 12, 12)
    painter.restore()


class LayeredCanvas(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(t.APP_BACKGROUND))
        for cx, cy, radius, hue in (
            (self.width() * .68, -35, 440, t.ACCENT_CYAN),
            (self.width() * .94, self.height() * .75, 510, t.ACCENT_VIOLET),
            (self.width() * .24, self.height() * .95, 420, t.SUNSET_GLOW),
        ):
            from PySide6.QtGui import QRadialGradient
            gradient = QRadialGradient(cx, cy, radius)
            center = QColor(hue)
            center.setAlpha(23)
            edge = QColor(hue)
            edge.setAlpha(0)
            gradient.setColorAt(0, center)
            gradient.setColorAt(1, edge)
            painter.fillRect(self.rect(), gradient)
