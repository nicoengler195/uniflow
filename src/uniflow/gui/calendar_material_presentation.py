"""Compose existing subject, material-type and forecast glyphs for the agenda."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap,QPainter,QIcon
from uniflow.gui.components.visual_badge import badge_icon
from uniflow.gui.components.nav_icons import content_icon
from uniflow.gui.dashboard_materials import material_kind, forecast_status


def calendar_material_kind(kind):
    return ('Artemis-Aufgabe','exercise') if kind=='ARTEMIS_TASK' else material_kind(kind)


def calendar_material_icon(identity, event):
    kind_label,kind_icon=calendar_material_kind(event.material_kind)
    _,status_icon,status_color,_=forecast_status(event.material_kind,event.information_status,
        linked=event.status=='CONFIRMED_PRESENT',ambiguous=event.status=='AMBIGUOUS')
    pixmap=QPixmap(58,48);pixmap.fill(Qt.GlobalColor.transparent)
    painter=QPainter(pixmap)
    painter.drawPixmap(0,4,badge_icon(identity).pixmap(40,40))
    painter.drawPixmap(42,2,content_icon(kind_icon,color='#c7cfd9').pixmap(16,16))
    painter.drawPixmap(42,30,content_icon(status_icon,color=status_color).pixmap(16,16))
    painter.end()
    return QIcon(pixmap)
