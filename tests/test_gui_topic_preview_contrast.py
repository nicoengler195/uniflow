"""Qt palette/contrast checks for the local topic dialog, without database access."""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QPushButton, QStyle, QStyleOptionSlider
import pytest

from test_gui_topic_preview import dialog, result
from uniflow.gui.theme.stylesheet import stylesheet


def contrast(foreground, background):
    def luminance(color):
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
    a, b = sorted((luminance(foreground), luminance(background)))
    return (b + .05) / (a + .05)


@pytest.fixture
def themed_app():
    app = QApplication.instance() or QApplication([])
    previous = app.styleSheet()
    app.setStyleSheet(stylesheet())
    yield app
    app.setStyleSheet(previous)


def measured_colors(view):
    """Read polished widget palettes, including actual viewport surfaces."""
    for widget in [view, *view.findChildren(QLabel), view.topics, view.topics.viewport(),
                   view.evidence, view.evidence.viewport(), *view.findChildren(QPushButton),
                   view.topics.verticalScrollBar(), view.evidence.verticalScrollBar(),
                   view.evidence.horizontalScrollBar()]:
        widget.ensurePolished()
    role = QPalette.ColorRole
    pairs = {}
    def record(name, widget, foreground, background, group=QPalette.ColorGroup.Active):
        palette = widget.palette()
        fg, bg = palette.color(group, foreground).name(), palette.color(group, background).name()
        pairs[name] = dict(foreground=fg, background=bg, contrast=contrast(fg, bg))
    record('dialog', view, role.WindowText, role.Window)
    record('description', next(label for label in view.findChildren(QLabel) if label != view.status), role.WindowText, role.Window)
    record('status', view.status, role.WindowText, role.Window)
    for name, widget in [('list', view.topics), ('list_viewport', view.topics.viewport()),
                         ('source', view.evidence), ('source_viewport', view.evidence.viewport())]:
        record(name, widget, role.Text, role.Base)
        record(name + '_selection', widget, role.HighlightedText, role.Highlight)
        record(name + '_inactive_selection', widget, role.HighlightedText, role.Highlight,
               QPalette.ColorGroup.Inactive)
        record(name + '_disabled', widget, role.Text, role.Base, QPalette.ColorGroup.Disabled)
    for index, widget in enumerate(view.findChildren(QPushButton)):
        record(f'button_{index}', widget, role.ButtonText, role.Button)
        record(f'button_{index}_disabled', widget, role.ButtonText, role.Button, QPalette.ColorGroup.Disabled)
    return pairs


def test_actual_palettes_have_readable_surfaces_and_selection(themed_app):
    view, _ = dialog()
    view._loaded('topic_preview', 'same', result())
    view.show()
    themed_app.processEvents()
    colors = measured_colors(view)
    assert colors['dialog']['background'] == '#172441'
    assert colors['status']['foreground'] == '#a8bdd2'
    for name in ('list', 'list_viewport', 'source', 'source_viewport'):
        assert colors[name]['foreground'] == '#edf6ff'
        assert colors[name]['background'] == '#101a31'
        assert colors[name + '_selection']['background'] == '#4edfed'
        assert colors[name + '_selection']['foreground'] == '#101a31'
    assert all(pair['contrast'] >= 4.5 for pair in colors.values()), colors
    view.reject()


def test_long_sources_scroll_and_focus_with_no_global_theme_change(themed_app):
    unrelated = QListWidget()
    unrelated.ensurePolished()
    before = unrelated.palette().color(QPalette.ColorRole.Base)
    app_css = themed_app.styleSheet()
    view, _ = dialog()
    view._loaded('topic_preview', 'same', result())
    view.evidence.setPlainText(('Längeres Quellenzitat mit Umlauten und Seitenbezug. ' * 20 + '\n') * 100)
    view.show()
    view.evidence.setFocus()
    themed_app.processEvents()
    assert view.evidence.isReadOnly()
    assert view.evidence.verticalScrollBar().maximum() > 0
    assert view.evidence.hasFocus()
    assert 'border-color: #4edfed' in view.styleSheet()
    view.source.setEnabled(False)
    themed_app.processEvents()
    assert measured_colors(view)['button_0_disabled']['contrast'] >= 4.5
    assert themed_app.styleSheet() == app_css
    assert unrelated.palette().color(QPalette.ColorRole.Base) == before
    view.reject()


def scrollbar_style_colors(bar, hovered=False):
    """Exercise Qt's scrollbar painter, not a desktop capture or screenshot."""
    option = QStyleOptionSlider()
    option.initFrom(bar)
    option.orientation = bar.orientation()
    option.rect = QRect(0, 0, 12, 160) if bar.orientation() == Qt.Orientation.Vertical else QRect(0, 0, 160, 12)
    option.minimum, option.maximum, option.pageStep, option.sliderPosition = 0, 100, 20, 30
    option.state |= QStyle.StateFlag.State_Enabled
    if hovered:
        option.state |= QStyle.StateFlag.State_MouseOver
        option.activeSubControls = QStyle.SubControl.SC_ScrollBarSlider
    image = QImage(option.rect.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    bar.style().drawComplexControl(QStyle.ComplexControl.CC_ScrollBar, option, painter, bar)
    painter.end()
    return {image.pixelColor(x, y).name() for x in range(image.width())
            for y in range(image.height()) if image.pixelColor(x, y).alpha() == 255}


def test_scrollbar_track_and_handle_use_local_dark_colors(themed_app):
    view, _ = dialog()
    view.ensurePolished()
    for bar in (view.topics.verticalScrollBar(), view.evidence.verticalScrollBar(), view.evidence.horizontalScrollBar()):
        bar.ensurePolished()
        colors = scrollbar_style_colors(bar)
        assert '#101a31' in colors
        assert '#a8bdd2' in colors
        assert '#4edfed' in scrollbar_style_colors(bar, hovered=True)
    assert contrast('#a8bdd2', '#101a31') >= 4.5
    view.reject()
