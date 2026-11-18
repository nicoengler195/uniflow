from uniflow.gui.theme import tokens as t


def stylesheet():
    return f"""
    QWidget {{ color: {t.TEXT_PRIMARY}; font-family: 'Segoe UI'; font-size: 13px; }}
    QMainWindow {{ background: {t.APP_BACKGROUND}; }}
    QDialog#planningDialog {{ background: {t.SURFACE_ELEVATED}; border: 1px solid {t.ACCENT_VIOLET}; }}
    QDialog#planningDialog QLabel#sectionTitle {{ color: {t.ACCENT_CYAN}; }}
    /* B1/B2/C1 share one scope; C1 inherits the B2 dialog identity.
       Explicit surfaces prevent native light palettes mixing with global light text. */
    QDialog#studyBlockDialog {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; }}
    QDialog#studyBlockDialog QLabel, QDialog#studyBlockDialog QCheckBox {{ color: {t.TEXT_PRIMARY}; }}
    QDialog#studyBlockDialog QLineEdit, QDialog#studyBlockDialog QAbstractSpinBox,
    QDialog#studyBlockDialog QComboBox, QDialog#studyBlockDialog QPlainTextEdit,
    QDialog#studyBlockDialog QAbstractItemView {{
        color: {t.TEXT_PRIMARY}; background: {t.SURFACE};
        alternate-background-color: {t.SURFACE_SECONDARY};
        border: 1px solid {t.BORDER}; border-radius: 4px; padding: 4px;
        selection-background-color: {t.ACCENT_VIOLET}; selection-color: {t.APP_BACKGROUND};
    }}
    QDialog#studyBlockDialog QHeaderView::section,
    QDialog#studyBlockDialog QTableCornerButton::section {{
        color: {t.TEXT_PRIMARY}; background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER}; padding: 4px;
    }}
    QDialog#studyBlockDialog QAbstractSpinBox:focus, QDialog#studyBlockDialog QLineEdit:focus,
    QDialog#studyBlockDialog QComboBox:focus, QDialog#studyBlockDialog QPlainTextEdit:focus {{ border-color: {t.ACCENT_CYAN}; }}
    QDialog#studyBlockDialog QAbstractSpinBox:disabled, QDialog#studyBlockDialog QLineEdit:disabled,
    QDialog#studyBlockDialog QComboBox:disabled, QDialog#studyBlockDialog QPlainTextEdit:disabled,
    QDialog#studyBlockDialog QAbstractItemView:disabled {{ color: {t.TEXT_SECONDARY}; background: {t.SURFACE_SECONDARY}; }}
    QDialog#studyBlockDialog QCheckBox:disabled {{ color: {t.TEXT_SECONDARY}; }}
    QDialog#studyBlockDialog QCheckBox::indicator,
    QDialog#studyBlockDialog QAbstractItemView::indicator {{
        width: 14px; height: 14px; border: 2px solid {t.TEXT_SECONDARY}; background: {t.SURFACE};
    }}
    QDialog#studyBlockDialog QCheckBox::indicator:checked,
    QDialog#studyBlockDialog QAbstractItemView::indicator:checked {{ background: {t.ACCENT_CYAN}; border-color: {t.ACCENT_CYAN}; }}
    QDialog#studyBlockDialog QCheckBox::indicator:disabled {{ border-color: {t.BORDER}; }}
    QDialog#studyBlockDialog QCheckBox:focus {{ outline: 1px solid {t.ACCENT_CYAN}; }}
    QInputDialog {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; }}
    QInputDialog QLabel {{ color: {t.TEXT_PRIMARY}; }}
    QInputDialog QLineEdit, QInputDialog QPlainTextEdit, QInputDialog QTextEdit,
    QInputDialog QComboBox {{ color: {t.TEXT_PRIMARY}; background: {t.SURFACE};
        border: 1px solid {t.BORDER}; border-radius: 8px; padding: 7px;
        selection-background-color: {t.ACCENT_VIOLET}; }}
    QInputDialog QComboBox QAbstractItemView {{ color: {t.TEXT_PRIMARY};
        background: {t.SURFACE_ELEVATED}; selection-background-color: {t.SURFACE_SECONDARY}; }}
    QDialog#skillSelectionDialog, QMessageBox {{ background: {t.SURFACE_ELEVATED}; color: {t.TEXT_PRIMARY}; }}
    QDialog#skillSelectionDialog QLabel, QMessageBox QLabel {{ color: {t.TEXT_PRIMARY}; }}
    QDialog#skillSelectionDialog QPlainTextEdit, QDialog#skillSelectionDialog QLineEdit,
    QDialog#skillSelectionDialog QComboBox, QDialog#skillSelectionDialog QSpinBox {{ color: {t.TEXT_PRIMARY}; background: {t.SURFACE};
        border: 1px solid {t.BORDER}; border-radius: 8px; padding: 7px;
        selection-background-color: {t.ACCENT_VIOLET}; selection-color: {t.TEXT_PRIMARY}; }}
    QDialog#skillSelectionDialog QComboBox QAbstractItemView {{ color: {t.TEXT_PRIMARY};
        background: {t.SURFACE_ELEVATED}; selection-background-color: {t.SURFACE_SECONDARY}; }}
    QMessageBox QPushButton, QDialog#skillSelectionDialog QPushButton {{ color: {t.TEXT_PRIMARY};
        background: {t.SURFACE}; border: 1px solid {t.BORDER}; border-radius: 8px; padding: 8px 12px; }}
    QDialog#planningDialog QLineEdit, QDialog#planningDialog QComboBox,
    QDialog#planningDialog QSpinBox, QDialog#planningDialog QDateTimeEdit,
    QDialog#planningDialog QPlainTextEdit {{
        background: {t.SURFACE}; border: 1px solid {t.BORDER}; border-radius: 8px;
        padding: 7px; selection-background-color: {t.ACCENT_VIOLET};
    }}
    QWidget#materialsScreen QComboBox, QWidget#materialsScreen QDateEdit {{
        color: {t.TEXT_PRIMARY}; background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER};
        border-radius: 8px; padding: 6px 9px; selection-background-color: {t.ACCENT_VIOLET};
    }}
    QWidget#materialsScreen QComboBox QAbstractItemView {{
        color: {t.TEXT_PRIMARY}; background: {t.SURFACE_ELEVATED}; selection-background-color: {t.SURFACE_SECONDARY};
    }}
    QWidget#proposalReviewScreen QLineEdit {{
        background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER}; border-radius: 8px;
        padding: 8px; selection-background-color: {t.ACCENT_VIOLET};
    }}
    QWidget#materialsScreen QToolButton {{ color: {t.ACCENT_CYAN}; border: none; padding: 5px; text-align: left; }}
    QMenu {{ background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER}; padding: 5px; }}
    QMenu::item {{ padding: 7px 20px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {t.SURFACE_SECONDARY}; color: {t.ACCENT_CYAN}; }}
    QLabel {{ background: transparent; }}
    QFrame#card, QFrame#course {{ background: {t.SURFACE}; border: 1px solid {t.BORDER}; border-radius: 18px; }}
    QFrame#secondary {{ background: {t.SURFACE_SECONDARY}; border: 1px solid {t.BORDER}; border-radius: 16px; }}
    QFrame#diagnostic {{ background: {t.SURFACE_SECONDARY}; border: 1px solid {t.WARNING}; border-radius: 16px; }}
    QFrame#accepted {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #102b48,stop:.38 {t.SURFACE},stop:1 #111a31); border: 1px solid {t.ACCENT_CYAN}; border-radius: 18px; }}
    QFrame#draft {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #29204a,stop:.38 {t.SURFACE},stop:1 #111a31); border: 1px solid {t.ACCENT_VIOLET}; border-radius: 18px; }}
    QFrame#hero {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {t.SURFACE_ELEVATED},stop:0.55 {t.SURFACE},stop:1 {t.HERO_END}); border: 1px solid {t.ACCENT_CYAN}; border-radius: 22px; }}
    QFrame#day {{ background: {t.SURFACE_SECONDARY}; border: 1px solid {t.BORDER}; border-radius: 12px; }}
    QFrame#day[current="true"] {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #12365a,stop:1 #221e4f); border: 1px solid {t.ACCENT_CYAN}; }}
    QFrame#day[selected="true"] {{ background: {t.SURFACE_ELEVATED}; border: 2px solid {t.ACCENT_MAGENTA}; }}
    QWidget#sidebar {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {t.SURFACE_SECONDARY},stop:1 {t.SURFACE}); border-right: 1px solid {t.BORDER}; }}
    QLabel#title {{ font-size: 26px; font-weight: 700; }}
    QLabel#heading {{ font-size: 17px; font-weight: 600; }}
    QLabel#heroTitle {{ font-size: 26px; font-weight: 700; }}
    QLabel#eyebrow {{ color: {t.ACCENT_CYAN}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
    QLabel#sectionTitle {{ font-size: 20px; font-weight: 700; }}
    QLabel#muted {{ color: {t.TEXT_SECONDARY}; }}
    QLabel#dayMeta {{ color: {t.TEXT_SECONDARY}; font-size: 11px; }}
    QLabel#chip {{ color: {t.ACCENT_CYAN}; background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER}; border-radius: 8px; padding: 4px 9px; }}
    QProgressBar {{ background: {t.SURFACE_SECONDARY}; border: 1px solid {t.BORDER}; border-radius: 5px; height: 9px; text-align: center; }}
    QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t.ACCENT_CYAN},stop:1 {t.ACCENT_VIOLET}); border-radius: 5px; }}
    QPushButton {{ background: {t.SURFACE_ELEVATED}; border: 1px solid {t.BORDER}; border-radius: 11px; padding: 10px 16px; text-align: left; }}
    QPushButton:hover {{ background: {t.SURFACE_SECONDARY}; border-color: {t.ACCENT_CYAN}; }}
    QPushButton:focus {{ border-color: {t.ACCENT_CYAN}; outline: none; }}
    QPushButton:pressed {{ background: {t.SURFACE}; border-color: {t.ACCENT_MAGENTA}; }}
    QPushButton#nav {{ background: transparent; border: 1px solid transparent; border-left: 3px solid transparent; padding: 13px 15px; font-weight: 600; }}
    QPushButton#nav:hover {{ background: {t.SURFACE_ELEVATED}; border-color: {t.BORDER}; }}
    QPushButton#nav[selected="true"] {{ background: {t.SURFACE_ELEVATED}; border-color: {t.ACCENT_CYAN}; color: {t.ACCENT_CYAN}; }}
    QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t.ACCENT_CYAN},stop:1 {t.ACCENT_BLUE}); color: {t.APP_BACKGROUND}; font-weight: 700; }}
    QPushButton:disabled {{ color: {t.TEXT_SECONDARY}; background: {t.SURFACE}; }}
    QPushButton#primary:disabled {{ color: {t.TEXT_SECONDARY}; background: {t.SURFACE_ELEVATED}; border-color: {t.BORDER}; }}
    QPushButton#sessionLink {{ background: {t.SURFACE_ELEVATED}; color: {t.ACCENT_CYAN}; border: 1px solid {t.BORDER}; border-radius: 9px; padding: 5px 11px; font-weight: 600; }}
    QPushButton#sessionLink:hover, QPushButton#sessionLink:focus {{ background: {t.SURFACE_SECONDARY}; border-color: {t.ACCENT_CYAN}; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: {t.SURFACE_SECONDARY}; border: none; border-radius: 6px; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t.ACCENT_BLUE},stop:1 {t.ACCENT_VIOLET}); border: 2px solid {t.SURFACE_SECONDARY}; border-radius: 6px; min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: {t.ACCENT_CYAN}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; border: none; background: transparent; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    """
