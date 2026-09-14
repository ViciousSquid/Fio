"""Help > Keys: a window listing every shortcut the editor answers to.

A real top-level window rather than a message box -- it is long enough to
want resizing, scrolling and searching, and it stays open beside the editor
so a key can be looked up without losing what is on screen.

The list itself comes from :mod:`editor.shortcuts`, which reads the menus,
the toolbar and the config file live.  Nothing here is written down twice.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from editor import shortcuts as sc


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
        self.tree.setIndentation(12)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        outer.addWidget(self.tree, 1)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #888;")
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
            group = QTreeWidgetItem(self.tree, [category, ""])
            group.setFirstColumnSpanned(True)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setFlags(Qt.ItemIsEnabled)
            for shortcut in shortcuts:
                row = QTreeWidgetItem(group, [shortcut.keys, shortcut.description])
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
