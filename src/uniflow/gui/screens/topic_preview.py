"""Read-only material topic dialog; all source reads use existing verification."""
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QVBoxLayout)

from uniflow.gui.presentation import PresentationError
from uniflow.gui.theme import tokens as t


def _dialog_stylesheet():
    """Local surfaces, matching the existing study-block dialog contrast fix."""
    return f"""
    QDialog#topicPreviewDialog {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; }}
    QDialog#topicPreviewDialog QLabel {{ color: {t.TEXT_PRIMARY}; background: transparent; }}
    QDialog#topicPreviewDialog QLabel#topicPreviewStatus {{ color: {t.TEXT_SECONDARY}; }}
    QDialog#topicPreviewDialog QListWidget, QDialog#topicPreviewDialog QPlainTextEdit {{
        background: {t.SURFACE}; color: {t.TEXT_PRIMARY};
        alternate-background-color: {t.SURFACE_SECONDARY};
        border: 1px solid {t.BORDER}; border-radius: 4px; padding: 6px;
        selection-background-color: {t.ACCENT_CYAN}; selection-color: {t.SURFACE};
    }}
    QDialog#topicPreviewDialog QListWidget::item {{ padding: 5px; }}
    QDialog#topicPreviewDialog QListWidget::item:selected {{ background: {t.ACCENT_CYAN}; color: {t.SURFACE}; }}
    QDialog#topicPreviewDialog QListWidget:focus, QDialog#topicPreviewDialog QPlainTextEdit:focus {{
        border-color: {t.ACCENT_CYAN};
    }}
    QDialog#topicPreviewDialog QPushButton {{
        background: {t.SURFACE}; color: {t.TEXT_PRIMARY};
        border: 1px solid {t.BORDER}; border-radius: 8px; padding: 8px 12px;
    }}
    QDialog#topicPreviewDialog QPushButton:hover {{ background: {t.SURFACE_ELEVATED}; border-color: {t.ACCENT_CYAN}; }}
    QDialog#topicPreviewDialog QPushButton:focus {{ border-color: {t.ACCENT_CYAN}; outline: none; }}
    QDialog#topicPreviewDialog QPushButton:pressed {{ background: {t.SURFACE_SECONDARY}; border-color: {t.ACCENT_CYAN}; }}
    QDialog#topicPreviewDialog QPushButton:disabled,
    QDialog#topicPreviewDialog QListWidget:disabled, QDialog#topicPreviewDialog QPlainTextEdit:disabled {{
        background: {t.SURFACE}; color: {t.TEXT_SECONDARY}; border-color: {t.BORDER};
    }}
    QDialog#topicPreviewDialog QScrollBar {{ background: {t.SURFACE}; }}
    QDialog#topicPreviewDialog QScrollBar:vertical {{ width: 12px; margin: 0; }}
    QDialog#topicPreviewDialog QScrollBar:horizontal {{ height: 12px; margin: 0; }}
    QDialog#topicPreviewDialog QScrollBar::handle {{ background: {t.TEXT_SECONDARY}; border-radius: 5px; }}
    QDialog#topicPreviewDialog QScrollBar::handle:vertical {{ min-height: 28px; }}
    QDialog#topicPreviewDialog QScrollBar::handle:horizontal {{ min-width: 28px; }}
    QDialog#topicPreviewDialog QScrollBar::handle:hover {{ background: {t.ACCENT_CYAN}; }}
    QDialog#topicPreviewDialog QScrollBar::handle:pressed {{ background: {t.ACCENT_CYAN}; }}
    QDialog#topicPreviewDialog QScrollBar::add-line, QDialog#topicPreviewDialog QScrollBar::sub-line {{
        width: 0; height: 0; background: transparent; border: none;
    }}
    QDialog#topicPreviewDialog QScrollBar::add-page, QDialog#topicPreviewDialog QScrollBar::sub-page {{ background: transparent; }}
    """


def load_material_topics(config, link_id, parse_run_id):
    from uniflow.application.semantic_topic_preview import preview_material_topics
    return preview_material_topics(config, link_id, parse_run_id)


class TopicPreviewDialog(QDialog):
    def __init__(self, controller, link_id, parse_run_id, parent=None):
        super().__init__(parent)
        self.setObjectName("topicPreviewDialog")
        self.setStyleSheet(_dialog_stylesheet())
        self.controller, self.parse_run_id = controller, parse_run_id
        self.link_id = link_id
        self.identity = controller.workflow_identity()
        self.closed = False
        self.generation = 0
        self.result = None
        self.setWindowTitle("Erkannte Themen · Vorschau")
        self.resize(850, 680)
        body = QVBoxLayout(self)
        intro = QLabel("Literale Themenkandidaten aus dem Quelltext, keine fachlich validierten Skills.\n"
            "Ohne lokalen Modellprovider ist noch keine automatische Skillableitung verfügbar.\n"
            "Nur Ansicht: Es werden keine Skills gespeichert oder freigegeben.")
        intro.setWordWrap(True)
        body.addWidget(intro)
        self.status = QLabel("Themen werden aus den geprüften Quellen geladen …")
        self.status.setObjectName("topicPreviewStatus")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        self.topics = QListWidget()
        body.addWidget(self.topics, 1)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        body.addWidget(self.evidence, 2)
        self.source = QPushButton("Quellseite im Material ansehen")
        self.source.setEnabled(False)
        self.source.clicked.connect(self._source)
        body.addWidget(self.source)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Schließen")
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)
        self.finished.connect(self._finished)
        self.topics.currentRowChanged.connect(self._select)
        controller.runner.submit("topic_preview", self.identity,
            lambda: load_material_topics(controller.config, link_id, parse_run_id), self._loaded)

    def _finished(self, result):
        self.closed = True
        self.generation += 1

    def _current(self):
        return not self.closed and self.identity == self.controller.workflow_identity()

    def _loaded(self, kind, token, result):
        if not self._current():
            return
        if isinstance(result, PresentationError):
            self.status.setText("Die Quelle ist nicht verfügbar, beschädigt oder nicht mehr aktuell. "
                "Bitte Material aktualisieren und die Quelle prüfen.")
            return
        self.result = result  # Full technical evidence remains available internally.
        self.status.setText(f"{len(result['topics'])} Themenkandidaten · Überschriften können auch "
            "Deckblatttitel oder Fragen enthalten. Formeln und Abbildungen sind nicht verifiziert.")
        for topic in result['topics']:
            ref = topic['evidence']
            self.topics.addItem(f"{topic['title']} · Seite {ref['source_label'] or ref['unit_ordinal']}")
        if result['topics']:
            self.topics.setCurrentRow(0)
        else:
            self.evidence.setPlainText("Keine literalen Themenkandidaten erkannt. "
                "Dies bedeutet nicht, dass das Material keine Themen enthält.")

    def _select(self, index):
        self.generation += 1
        self.source.setEnabled(False)
        if not self._current() or self.result is None or index < 0:
            return
        ref = self.result['topics'][index]['evidence']
        parts = [f"Seite {ref['source_label'] or ref['unit_ordinal']} · Quellenzitat", ref['quote'][:400],
            "", "Formeln: Struktur und Vollständigkeit nicht verifiziert.",
            "Abbildungen: Inhalte und Beschriftungen nicht verifiziert."]
        conditions = [c['evidence']['quote'][:300] for c in self.result['conditions']
            if c['evidence']['parsed_unit_id'] == ref['parsed_unit_id']]
        if conditions:
            parts += ["", "Weitere Quellausschnitte · Bedingungen nicht fachlich geprüft", *conditions]
        self.evidence.setPlainText("\n".join(parts))
        self.source.setEnabled(True)

    def _source(self):
        from uniflow.application.semantic_topic_preview import read_material_topic_page
        if not self._current() or self.result is None or self.topics.currentRow() < 0:
            return
        ref = self.result['topics'][self.topics.currentRow()]['evidence']
        self.generation += 1
        generation = self.generation
        self.source.setEnabled(False)
        def loaded(kind, token, result):
            if not self._current() or token != self.generation:
                return
            self.source.setEnabled(True)
            if isinstance(result, PresentationError):
                self.evidence.setPlainText("Quellseite nicht verfügbar oder beschädigt. Bitte Material aktualisieren.")
                return
            self.evidence.setPlainText(f"Quellseite {ref['source_label'] or ref['unit_ordinal']}\n"
                "Extrahierter Text · Formeln und Abbildungen nicht verifiziert\n\n" + result)
        self.controller.runner.submit("topic_source", generation,
            lambda: read_material_topic_page(self.controller.config, self.link_id,
                self.parse_run_id, ref['unit_ordinal']), loaded)
