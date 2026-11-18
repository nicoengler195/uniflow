from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from uniflow.gui.presentation import PresentationError
from uniflow.gui.screens.topic_preview import TopicPreviewDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Runner:
    def submit(self, kind, token, fn, callback):
        self.pending = kind, token, fn, callback


def dialog():
    runner = Runner()
    controller = SimpleNamespace(runner=runner, config=None, workflow_identity=lambda: "same")
    return TopicPreviewDialog(controller, "link", "parse"), runner


def result():
    ref = dict(source_label="8", unit_ordinal=8, parsed_unit_id="private-id", quote="Wurfhöhe")
    return dict(topics=[dict(title="Wurfhöhe", evidence=ref)], conditions=[], warnings=[])


def test_candidates_read_only_and_no_technical_ids(app):
    view, runner = dialog()
    view._loaded("topic_preview", "same", result())
    assert view.topics.item(0).text() == "Wurfhöhe · Seite 8"
    assert view.evidence.isReadOnly()
    assert "nicht verifiziert" in view.evidence.toPlainText()
    assert "private-id" not in view.evidence.toPlainText()
    view.reject()


def test_source_failure(app):
    view, runner = dialog()
    view._loaded("topic_preview", "same", PresentationError("error", "error", "private path"))
    assert "beschädigt" in view.status.text()
    assert "private path" not in view.status.text()
    assert not view.source.isEnabled()
    view.reject()


def test_no_topics_and_repeated_open_close(app):
    for _ in range(3):
        view, runner = dialog()
        view._loaded("topic_preview", "same", dict(topics=[], conditions=[], warnings=[]))
        assert "Keine literalen Themenkandidaten" in view.evidence.toPlainText()
        assert not view.source.isEnabled()
        view.reject()


def test_preview_entry_does_not_require_course_assignment(app):
    from PySide6.QtWidgets import QPushButton
    from uniflow.gui.screens.material_detail import MaterialDetailScreen
    selected = []
    screen = SimpleNamespace(_show_topics=lambda link, parse: selected.append((link, parse)))
    material = SimpleNamespace(course_stable_id=None)
    current = SimpleNamespace(material_artifact_id="link", source_artifact=SimpleNamespace(
        parse_observation=SimpleNamespace(status="SUCCESS", parse_run_id="parse")))
    card = MaterialDetailScreen._learning_step(screen, material, current, [], "")
    buttons = card.findChildren(QPushButton)
    entry = next(button for button in buttons if button.text() == "Erkannte Themen anzeigen")
    entry.click()
    assert selected == [("link", "parse")]


def test_late_callbacks_after_close_and_page_change_are_ignored(app):
    view, runner = dialog()
    view._loaded("topic_preview", "same", result())
    view._source()
    kind, token, fn, callback = runner.pending
    view._select(0)
    callback(kind, token, "outdated page text")
    assert "outdated" not in view.evidence.toPlainText()
    view.reject()
    view._loaded("topic_preview", "same", result())
    assert view.topics.count() == 1
