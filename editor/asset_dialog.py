"""Choosing an asset file from Fio's own filesystem.

Fio mounts every ``assets/*.fiopak`` at start-up: a model, sprite or sound
packed in one is as much an asset as a loose file, and the Asset Browser
lists both. The Browse buttons used the operating system's file dialog,
which only sees files on disk -- so nothing inside a pack could be chosen.

:func:`get_open_asset_file` is a drop-in for ``QFileDialog.getOpenFileName``
that browses the mounted asset tree (``engine.asset_archives``): loose and
packed files together, a packed one marked with the pack it comes from. It
returns ``(path, filter)`` like Qt's: an ``assets/...`` path for an asset
(relative to the working directory, as every caller already turns its pick
into), or an absolute path when the user chose *Other File...* and picked a
file outside the project in the system dialog.
"""

from __future__ import annotations

import os
import re

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QLabel,
                             QLineEdit, QPushButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QHBoxLayout)

from engine.asset_archives import (asset_archive_name, asset_isdir,
                                   asset_listdir)
from engine.material_maps import is_companion

ASSETS = 'assets'
_PATH_ROLE = Qt.UserRole
_DIR_ROLE = Qt.UserRole + 1
_LOADED_ROLE = Qt.UserRole + 2


def suffixes_from_filter(name_filter):
    """``('.obj', '.glb')`` from a Qt filter such as
    ``"Model Files (*.obj *.glb)"``; empty for "everything"."""
    found = re.findall(r'\*(\.[A-Za-z0-9_]+)', name_filter or '')
    return tuple(s.lower() for s in found)


def _norm(path):
    return os.path.normpath(path).replace('\\', '/')


class AssetFileDialog(QDialog):
    """A tree of the asset folders, loose and packed files alike."""

    def __init__(self, parent=None, title="Select File", start=ASSETS,
                 name_filter=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 560)
        self.name_filter = name_filter or ""
        self.suffixes = suffixes_from_filter(self.name_filter)
        self.selected_path = ""

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._load_children)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.currentItemChanged.connect(self._current_changed)
        layout.addWidget(self.tree, 1)

        self.where = QLabel("")
        self.where.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(self.where)

        row = QHBoxLayout()
        other = QPushButton("Other File...")
        other.setToolTip("Choose a file outside the project's assets")
        other.clicked.connect(self._other_file)
        row.addWidget(other)
        row.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept_current)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        layout.addLayout(row)

        root = self._folder_item(None, ASSETS, ASSETS)
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)
        self._reveal(start)
        self._current_changed(self.tree.currentItem(), None)

    # -- tree -------------------------------------------------------------
    def _folder_item(self, parent, label, path):
        item = QTreeWidgetItem([label + "/"])
        item.setData(0, _PATH_ROLE, path)
        item.setData(0, _DIR_ROLE, True)
        item.setData(0, _LOADED_ROLE, False)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        if parent is not None:
            parent.addChild(item)
        return item

    def _load_children(self, item):
        if not item.data(0, _DIR_ROLE) or item.data(0, _LOADED_ROLE):
            return
        item.setData(0, _LOADED_ROLE, True)
        folder = item.data(0, _PATH_ROLE)
        dirs, files = [], []
        for name in asset_listdir(folder):
            path = folder + '/' + name
            if asset_isdir(path):
                dirs.append((name, path))
            elif ((not self.suffixes or name.lower().endswith(self.suffixes))
                  and not is_companion(name)):
                # A texture's material maps (<name>_n, <name>_m, .mat.json:
                # engine.material_maps) are not textures to pick.
                files.append((name, path))
        for name, path in sorted(dirs, key=lambda e: e[0].lower()):
            self._folder_item(item, name, path)
        for name, path in sorted(files, key=lambda e: e[0].lower()):
            pack = asset_archive_name(path)
            child = QTreeWidgetItem([name if not pack else "%s   [%s]" % (name, pack)])
            child.setData(0, _PATH_ROLE, path)
            child.setData(0, _DIR_ROLE, False)
            if pack:
                child.setToolTip(0, "%s\nPacked in %s" % (path, pack))
            else:
                child.setToolTip(0, path)
            item.addChild(child)
        if not dirs and not files:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)
        self._apply_filter(self.search.text())

    def _reveal(self, start):
        """Open the tree down to *start* (a folder under assets) and make
        it current."""
        start = _norm(start or ASSETS)
        if os.path.isabs(start):
            start = _norm(os.path.relpath(start))
        item = self.tree.topLevelItem(0)
        self._load_children(item)
        if start.startswith(ASSETS + '/'):
            for part in start[len(ASSETS) + 1:].split('/'):
                found = None
                for i in range(item.childCount()):
                    child = item.child(i)
                    if child.data(0, _DIR_ROLE) and \
                            child.data(0, _PATH_ROLE).rsplit('/', 1)[-1] == part:
                        found = child
                        break
                if found is None:
                    break
                found.setExpanded(True)
                self._load_children(found)
                item = found
        self.tree.setCurrentItem(item)

    def _apply_filter(self, text):
        """Hide loaded files whose name does not contain *text*."""
        needle = (text or '').strip().lower()

        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.data(0, _DIR_ROLE):
                    walk(child)
                else:
                    name = child.data(0, _PATH_ROLE).rsplit('/', 1)[-1].lower()
                    child.setHidden(bool(needle) and needle not in name)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    # -- choosing ---------------------------------------------------------
    def _current_changed(self, current, _previous):
        is_file = current is not None and not current.data(0, _DIR_ROLE)
        self.buttons.button(QDialogButtonBox.Open).setEnabled(is_file)
        self.where.setText(current.data(0, _PATH_ROLE) if current is not None else "")

    def _double_clicked(self, item, _column):
        if not item.data(0, _DIR_ROLE):
            self.select_path(item.data(0, _PATH_ROLE))

    def _accept_current(self):
        item = self.tree.currentItem()
        if item is not None and not item.data(0, _DIR_ROLE):
            self.select_path(item.data(0, _PATH_ROLE))

    def select_path(self, path):
        self.selected_path = path
        self.accept()

    def _other_file(self):
        item = self.tree.currentItem()
        folder = ASSETS
        if item is not None:
            path = item.data(0, _PATH_ROLE)
            folder = path if item.data(0, _DIR_ROLE) else path.rsplit('/', 1)[0]
        if not os.path.isdir(folder):
            folder = ASSETS
        fp, _ = QFileDialog.getOpenFileName(self, self.windowTitle(), folder,
                                            self.name_filter)
        if fp:
            self.select_path(fp)


def get_open_asset_file(parent=None, title="Select File", start=ASSETS,
                        name_filter=""):
    """``(path, name_filter)`` of the asset chosen, or ``("", "")`` when the
    dialog was cancelled. See the module docstring."""
    dialog = AssetFileDialog(parent, title, start, name_filter)
    if dialog.exec_() == QDialog.Accepted and dialog.selected_path:
        return dialog.selected_path, name_filter
    return "", ""
