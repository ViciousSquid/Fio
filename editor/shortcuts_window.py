"""Help > Keys: a window listing every shortcut the editor answers to.

A real top-level window rather than a message box -- it is long enough to
want resizing, scrolling and searching, and it stays open beside the editor
so a key can be looked up without losing what is on screen.

The list itself comes from :mod:`editor.shortcuts`, which reads the menus,
the toolbar and the config file live.  Nothing here is written down twice.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from editor import shortcuts as sc

# Fio's palette, as main.dark_stylesheet uses it.  The global sheet styles
# QWidget but never item views, so a QTreeWidget left to itself paints with
# Qt's light defaults -- white header, near-white alternating rows, grey text.
_BG = '#2b2b2b'
_ALT = '#313131'
_PANEL = '#3c3f41'
_BORDER = '#555'
_TEXT = '#e0e0e0'
_DIM = '#8a8a8a'
_ACCENT = '#F08000'

_SHEET = """
    QTreeWidget {
        background-color: %(bg)s;
        alternate-background-color: %(alt)s;
        color: %(text)s;
        border: 1px solid %(border)s;
        outline: none;
    }
    QTreeWidget::item { padding: 4px 6px; border: none; }
    QTreeWidget::item:selected { background-color: %(accent)s; color: white; }
    QHeaderView::section {
        background-color: %(panel)s;
        color: %(dim)s;
        padding: 5px 6px;
        border: none;
        border-bottom: 1px solid %(border)s;
        border-right: 1px solid %(border)s;
        font-weight: bold;
    }
    QTreeWidget QScrollBar:vertical { background: %(bg)s; width: 12px; border: none; }
    QTreeWidget QScrollBar::handle:vertical {
        background: #4b4d4d; min-height: 20px; border-radius: 6px;
    }
    QTreeWidget QScrollBar::add-line:vertical,
    QTreeWidget QScrollBar::sub-line:vertical { height: 0px; }
""" % {'bg': _BG, 'alt': _ALT, 'panel': _PANEL, 'border': _BORDER,
       'text': _TEXT, 'dim': _DIM, 'accent': _ACCENT}


class ShortcutsWindow(QDialog):
    """Every shortcut, by category, searchable."""

    def __init__(self, editor=None, parent=None):
        super().__init__(parent)
        self.editor = editor

        self.setWindowTitle("Keyboard Shortcuts")
        # A plain window: it gets a title bar, a close box, and can be
        # minimised and resized like any other, rather than floating on top.
        self.setWindowFlags(Qt.Window)
        self.resize(640, 680)

        self._grouped = {}
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------ #
    # UI                                                                  #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        outer = QVBoxLayout(self)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by key or description...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        search_row.addWidget(self.search, 1)
        outer.addLayout(search_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Shortcut", "Action"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setStyleSheet(_SHEET)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setHighlightSections(False)
        outer.addWidget(self.tree, 1)

        # Keys read far better set apart from their descriptions: a fixed
        # pitch lines the modifiers up down the column.
        self._key_font = QFont('Consolas')
        self._key_font.setStyleHint(QFont.Monospace)
        self._key_font.setPointSize(QFont().pointSize())

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: %s;" % _DIM)
        outer.addWidget(self.count_label)

        buttons = QHBoxLayout()
        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.setToolTip("Copy the whole list as plain text")
        copy_btn.clicked.connect(self._copy)
        buttons.addWidget(copy_btn)
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        outer.addLayout(buttons)

    # ------------------------------------------------------------------ #
    # Contents                                                            #
    # ------------------------------------------------------------------ #
    def reload(self):
        """Re-read the shortcuts and rebuild the list.

        Called when the window is opened, so a binding changed in Settings
        since last time shows its new key rather than the one it had.
        """
        self._grouped = sc.collect(self.editor)
        self.tree.clear()
        for category, shortcuts in self._grouped.items():
            group = QTreeWidgetItem(self.tree, [category.upper(), ""])
            group.setFirstColumnSpanned(True)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setForeground(0, QColor(_ACCENT))
            group.setBackground(0, QColor(_PANEL))
            group.setBackground(1, QColor(_PANEL))
            # A heading, not a row: it cannot be picked or selected.
            group.setFlags(Qt.ItemIsEnabled)
            for shortcut in shortcuts:
                row = QTreeWidgetItem(group, [shortcut.keys, shortcut.description])
                row.setFont(0, self._key_font)
                row.setForeground(0, QColor(_TEXT))
                # The Action column elides when the window is narrow, so the
                # full text stays reachable by hovering.
                row.setToolTip(1, shortcut.description)
        self.tree.expandAll()
        self._filter(self.search.text())

    def _filter(self, text):
        """Show only rows matching ``text``, hiding categories left empty."""
        needle = (text or '').strip().lower()
        shown = 0
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            matches = 0
            for j in range(group.childCount()):
                row = group.child(j)
                hit = (not needle
                       or needle in row.text(0).lower()
                       or needle in row.text(1).lower())
                row.setHidden(not hit)
                matches += bool(hit)
            group.setHidden(matches == 0)
            shown += matches

        total = sum(len(v) for v in self._grouped.values())
        self.count_label.setText(
            "%d shortcuts" % total if shown == total
            else "%d of %d shortcuts" % (shown, total))

    def _copy(self):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(sc.as_text(self._grouped))

    def showEvent(self, event):
        """Pick up anything rebound since the window was last opened."""
        self.reload()
        super().showEvent(event)
