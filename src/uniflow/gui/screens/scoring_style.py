"""Explicit local Qt surfaces for scoring review and its child preview."""
from uniflow.gui.theme import tokens as t


def scoring_dialog_stylesheet():
    scope = 'QDialog#scoringPreviewDialog'
    return f'''
    {scope} {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; }}
    {scope} QLabel, {scope} QCheckBox {{ color: {t.TEXT_PRIMARY}; background: transparent; }}
    {scope} QTabWidget::pane {{ background: {t.SURFACE}; border: 1px solid {t.BORDER}; }}
    {scope} QTabBar::tab {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; padding: 7px; border: 1px solid {t.BORDER}; }}
    {scope} QTabBar::tab:selected {{ background: {t.SURFACE}; color: {t.ACCENT_CYAN}; }}
    {scope} QTabBar::tab:disabled {{ color: {t.TEXT_SECONDARY}; background: {t.SURFACE_SECONDARY}; }}
    {scope} QPlainTextEdit, {scope} QAbstractItemView, {scope} QLineEdit, {scope} QComboBox {{
        background: {t.SURFACE}; color: {t.TEXT_PRIMARY};
        alternate-background-color: {t.SURFACE_SECONDARY}; border: 1px solid {t.BORDER};
        padding: 4px; selection-background-color: {t.ACCENT_CYAN}; selection-color: {t.SURFACE};
    }}
    {scope} QLineEdit {{ placeholder-text-color: {t.TEXT_SECONDARY}; }}
    {scope} QHeaderView::section, {scope} QTableCornerButton::section {{
        background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; padding: 4px; border: 1px solid {t.BORDER};
    }}
    {scope} QPushButton {{ background: {t.SURFACE}; color: {t.TEXT_PRIMARY}; border: 1px solid {t.BORDER}; border-radius: 8px; padding: 8px; }}
    {scope} QPushButton:hover, {scope} QPushButton:focus {{ border-color: {t.ACCENT_CYAN}; }}
    {scope} QPushButton:pressed {{ background: {t.SURFACE_SECONDARY}; }}
    {scope} QPushButton:disabled, {scope} QComboBox:disabled, {scope} QLineEdit:disabled,
    {scope} QAbstractItemView:disabled, {scope} QCheckBox:disabled {{ color: {t.TEXT_SECONDARY}; background: {t.SURFACE}; }}
    {scope} QCheckBox::indicator {{ width: 14px; height: 14px; border: 2px solid {t.TEXT_SECONDARY}; background: {t.SURFACE}; }}
    {scope} QCheckBox::indicator:checked {{ background: {t.ACCENT_CYAN}; border-color: {t.ACCENT_CYAN}; }}
    {scope} QLineEdit:focus, {scope} QComboBox:focus, {scope} QPlainTextEdit:focus, {scope} QAbstractItemView:focus {{ border-color: {t.ACCENT_CYAN}; }}
    {scope} QScrollBar {{ background: {t.SURFACE}; }}
    {scope} QScrollBar::handle {{ background: {t.TEXT_SECONDARY}; min-height: 24px; min-width: 24px; }}
    {scope} QScrollBar::handle:hover {{ background: {t.ACCENT_CYAN}; }}
    {scope} QScrollBar::add-line, {scope} QScrollBar::sub-line {{ width: 0; height: 0; }}
    '''
