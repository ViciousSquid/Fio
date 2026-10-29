"""Explicit, platform-independent appearance for the editor's Qt controls.

Component styles may override these rules intentionally. No native platform
theme supplies the appearance of an otherwise unstyled editor control.
"""

CONTROL_STYLESHEET = """
    QLabel { border: none; background: transparent; padding: 0px; }
    QMessageBox QLabel { font-family: 'Fio UI'; font-weight: normal; }
    QWidget:disabled { color: #858585; }
    QPushButton, QToolButton {
        background: #3c3f41; color: #e0e0e0; border: 1px solid #555;
        border-radius: 3px; padding: 4px 10px; margin: 0px;
    }
    QToolButton { padding: 3px; }
    QPushButton:hover, QToolButton:hover { background: #4b4d4d; border-color: #777; }
    QPushButton:pressed, QToolButton:pressed { background: #303334; }
    QPushButton:checked, QToolButton:checked { background: #425f5d; border-color: #F08000; }
    QPushButton:focus, QToolButton:focus { border-color: #F08000; }
    QPushButton:disabled, QToolButton:disabled { background: #333; color: #858585; border-color: #444; }
    QPushButton:default { border-color: #F08000; }
    QToolButton::menu-indicator { image: url(assets/ui/controls/down.svg); width: 10px; height: 10px; }

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
        background: #3c3f41; color: #e0e0e0; border: 1px solid #555;
        border-radius: 2px; padding: 3px 5px; margin: 0px;
        selection-background-color: #425f5d; selection-color: #fff;
    }
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #F08000; }
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
        background: #333; color: #858585; border-color: #444;
    }
    QTextEdit, QPlainTextEdit, QTextBrowser {
        background: #303234; color: #e0e0e0; border: 1px solid #555;
        padding: 4px; margin: 0px; selection-background-color: #425f5d; selection-color: #fff;
    }
    QSpinBox, QDoubleSpinBox { padding-right: 20px; }
    QSpinBox::up-button, QDoubleSpinBox::up-button {
        subcontrol-origin: border; subcontrol-position: top right;
        width: 16px; height: 11px; background: #444; border-left: 1px solid #555;
    }
    QSpinBox::down-button, QDoubleSpinBox::down-button {
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: 16px; height: 11px; background: #444; border-left: 1px solid #555;
    }
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #5a5c5e; }
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
        image: url(assets/ui/controls/up.svg); width: 10px; height: 10px;
    }
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
        image: url(assets/ui/controls/down.svg); width: 10px; height: 10px;
    }
    QComboBox { padding-right: 24px; }
    QComboBox::drop-down {
        subcontrol-origin: border; subcontrol-position: top right;
        width: 20px; border-left: 1px solid #555; background: #444;
    }
    QComboBox::down-arrow { image: url(assets/ui/controls/down.svg); width: 12px; height: 12px; }
    QComboBox QAbstractItemView {
        background: #303234; color: #e0e0e0; border: 1px solid #666;
        selection-background-color: #425f5d; selection-color: #fff; padding: 2px;
    }

    QCheckBox, QRadioButton { spacing: 6px; padding: 0px; border: none; background: transparent; }
    QCheckBox::indicator, QRadioButton::indicator {
        width: 16px; height: 16px; border: 1px solid #666; background: #3c3f41;
    }
    QCheckBox::indicator { border-radius: 2px; }
    QRadioButton::indicator { border-radius: 8px; }
    QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #F08000; }
    QCheckBox::indicator:checked { background: #b56812; image: url(assets/ui/controls/check.svg); }
    QCheckBox::indicator:indeterminate { background: #425f5d; image: url(assets/ui/controls/minus.svg); }
    QRadioButton::indicator:checked { background: #b56812; image: url(assets/ui/controls/radio.svg); }
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { background-color: #333; border-color: #444; }

    QGroupBox {
        border: 1px solid #555; border-radius: 3px;
        margin-top: 12px; padding: 10px 8px 8px 8px;
    }
    QGroupBox::title {
        subcontrol-origin: margin; subcontrol-position: top left;
        left: 8px; padding: 0px 4px; color: #e0e0e0;
    }
    QTabWidget::pane { border: 1px solid #555; padding: 0px; margin: 0px; }
    QTabWidget::tab-bar { alignment: left; }
    QTabBar { qproperty-drawBase: 0; }
    QTabBar::tab {
        background: #3c3f41; color: #ccc; border: 1px solid #555;
        padding: 5px 10px; margin: 0px;
    }
    QTabBar::tab:selected { background: #425f5d; color: #fff; border-bottom: 2px solid #F08000; }
    QTabBar::tab:hover { background: #4b4d4d; }
    QTabBar::tab:disabled { color: #858585; background: #333; }
    QTabBar::scroller { width: 32px; }
    QTabBar QToolButton { background: #3c3f41; border: 1px solid #555; padding: 0px; }
    QTabBar QToolButton::left-arrow { image: url(assets/ui/controls/left.svg); width: 12px; height: 12px; }
    QTabBar QToolButton::right-arrow { image: url(assets/ui/controls/right.svg); width: 12px; height: 12px; }
    QTabBar::close-button { image: url(assets/ui/controls/close.svg); width: 12px; height: 12px; }

    QScrollBar:vertical { width: 12px; margin: 0px; border: none; background: #252729; }
    QScrollArea { border: none; padding: 0px; margin: 0px; }
    QAbstractScrollArea::corner { border: none; background: #2b2b2b; }
    QScrollBar:horizontal { height: 12px; margin: 0px; border: none; background: #252729; }
    QScrollBar::handle:vertical { min-height: 24px; background: #55595d; border-radius: 4px; }
    QScrollBar::handle:horizontal { min-width: 24px; background: #55595d; border-radius: 4px; }
    QScrollBar::handle:hover { background: #73777a; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
    QSlider { border: none; background: transparent; }
    QSlider::groove:horizontal { height: 4px; background: #444; border: 1px solid #555; border-radius: 2px; }
    QSlider::sub-page:horizontal { background: #425f5d; border-radius: 2px; }
    QSlider::handle:horizontal { width: 12px; margin: -5px 0px; background: #ddd; border: 1px solid #888; border-radius: 3px; }
    QSlider::groove:vertical { width: 4px; background: #444; border: 1px solid #555; border-radius: 2px; }
    QSlider::handle:vertical { height: 12px; margin: 0px -5px; background: #ddd; border: 1px solid #888; border-radius: 3px; }

    QAbstractItemView {
        background: #2b2b2b; alternate-background-color: #303234; color: #e0e0e0;
        border: 1px solid #555; selection-background-color: #425f5d; selection-color: #fff;
        outline: 0; gridline-color: #444;
    }
    QAbstractItemView::item { padding: 3px 5px; border: none; }
    QAbstractItemView::item:selected { background: #425f5d; color: #fff; }
    QAbstractItemView::item:hover { background: #383e40; }
    QHeaderView::section {
        background: #3c3f41; color: #ddd; border: none;
        border-right: 1px solid #555; border-bottom: 1px solid #555; padding: 4px 6px;
    }
    QHeaderView::up-arrow { image: url(assets/ui/controls/up.svg); width: 10px; height: 10px; }
    QHeaderView::down-arrow { image: url(assets/ui/controls/down.svg); width: 10px; height: 10px; }
    QTableCornerButton::section { background: #3c3f41; border: 1px solid #555; }
    QTreeView::branch:has-children:closed { image: url(assets/ui/controls/right.svg); }
    QTreeView::branch:has-children:open { image: url(assets/ui/controls/down.svg); }

    QMenuBar { background: #3c3f41; color: #e0e0e0; padding: 0px; border: none; spacing: 0px; }
    QMenuBar::item { background: transparent; padding: 5px 10px; margin: 0px; }
    QMenuBar::item:selected { background: #4b4d4d; }
    QMenu { background: #2b2b2b; color: #e0e0e0; border: 1px solid #666; padding: 2px; }
    QMenu::item { padding: 5px 26px 5px 24px; margin: 0px; border: none; }
    QMenu::item:selected { background: #425f5d; color: #fff; }
    QMenu::item:disabled { color: #858585; }
    QMenu::separator { height: 1px; background: #555; margin: 3px 6px; }
    QMenu::indicator { width: 14px; height: 14px; }
    QMenu::indicator:checked { image: url(assets/ui/controls/check.svg); }
    QMenu::right-arrow { image: url(assets/ui/controls/right.svg); width: 12px; height: 12px; }
    QToolBar { background: #2b2b2b; border: none; padding: 2px; spacing: 4px; }
    QToolBar::separator { width: 1px; height: 1px; background: #555; margin: 3px; }
    QDockWidget { border: 1px solid #555; titlebar-close-icon: url(assets/ui/controls/close.svg); }
    QDockWidget::title { background: #3c3f41; text-align: left; padding: 4px 8px; }
    QSplitter::handle { background: #3c3f41; }
    QSplitter::handle:horizontal { width: 4px; }
    QSplitter::handle:vertical { height: 4px; }
    QStatusBar { background: #3c3f41; color: #ddd; border-top: 1px solid #555; }
    QStatusBar::item { border: none; }
    QProgressBar { background: #252729; border: 1px solid #555; border-radius: 3px; text-align: center; }
    QProgressBar::chunk { background: #425f5d; margin: 0px; }
    QToolTip { color: #eee; background: #333; border: 1px solid #777; padding: 5px 7px; }
"""
