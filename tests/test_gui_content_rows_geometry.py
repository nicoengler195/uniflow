"""Real Qt layout checks for composed clickable content rows."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

from uniflow.gui.components import AcademicEventButton, DeadlineButton
from uniflow.gui.visual_identity import resolve_course_visual


def assert_content_visible(button):
    button_rect = button.rect()
    labels = button.findChildren(QLabel)
    assert labels and button.layout().geometry() == button_rect
    text_rects = []
    for child in labels:
        assert child.isVisible()
        rect = child.rect().translated(child.mapTo(button, QPoint(0, 0)))
        assert button_rect.contains(rect), (button.text(), child.text(), rect, button_rect)
        if child.text():
            assert child.height() >= child.heightForWidth(child.width()), child.text()
            text_rects.append(rect)
        elif child.pixmap() and not child.pixmap().isNull():
            assert child.width() >= child.pixmap().width()
            assert child.height() >= child.pixmap().height()
    for index, first in enumerate(text_rects):
        for second in text_rects[index + 1:]:
            assert not first.intersects(second), (first, second)
    assert button.height() >= button.heightForWidth(button.width())


def test_event_and_deadline_rows_follow_wrapped_content_and_keep_input(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    layout = QVBoxLayout(parent)
    event = AcademicEventButton(
        "09:00 · Neuro- und Verhaltensbiologie · Einführung in die Neurophysiologie und Grundlagen der Verhaltensbiologie",
        accent_family="MAGENTA", activity_type="LECTURE",
        location="Campus Nord, Seminarraum 123")
    deadline = DeadlineButton("Übungsblatt 1 – Abgabe mit längerem Titel",
        identity=resolve_course_visual("MATH", "Mathematik"),
        course_name="Mathematik I: Analysis und Lineare Algebra",
        due_text="19.10.2026 · 12:00", status="UNKNOWN",
        information="CONFIRMED", timing="Heute fällig")
    assert isinstance(event, QPushButton) and isinstance(deadline, QPushButton)
    assert event.accessibleName() and deadline.accessibleName()
    clicked = []
    event.clicked.connect(lambda: clicked.append("event"))
    deadline.clicked.connect(lambda: clicked.append("deadline"))
    layout.addWidget(event)
    layout.addWidget(deadline)
    layout.addStretch()
    for width in (300, 720, 300, 1360, 1920):
        parent.resize(width, 800)
        parent.show()
        QApplication.processEvents()
        assert_content_visible(event)
        assert_content_visible(deadline)
        assert event.geometry().bottom() < deadline.geometry().top()
        assert event.height() < deadline.height() + 20
    qtbot.mouseClick(event, Qt.MouseButton.LeftButton)
    deadline.setFocus()
    assert deadline.hasFocus()
    qtbot.keyClick(deadline, Qt.Key.Key_Space)
    assert clicked == ["event", "deadline"]
