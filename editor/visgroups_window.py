"""Visgroups & Cordon: Hammer's Visgroups and Cordon tool in one window.

Opened from the eye on the toolbar. Four tabs:

* **User** -- named groups of brushes and entities, each shown or hidden by
  its checkbox: make one from the selection, add or remove the selection,
  select a group's members, rename (double-click) or delete it.
* **Groups** -- brush groups (brushes and entities attached to one another,
  which always move together) and the members of each: rename
  (double-click), select or ungroup one. Made from Scene Hierarchy > Add to Group...
* **Auto** -- every kind of object (the Filter menu), shown or hidden.
* **Cordon** -- a box; while it is on, only what reaches into it is shown in
  the editor's views. Set it from the selection or type its corners.

All of it is :class:`engine.view_filters.ViewFilters` state: it hides objects
from every editor view, never from Play, and visgroups and the cordon are
saved with the map. The padlock keeps the window on top of everything.
"""

import os

from engine.asset_archives import asset_exists
from engine.asset_qt import load_icon

from PyQt5.QtCore import Qt, QSize, QTimer
from PyQt5.QtGui import QBrush, QColor, QIcon
from PyQt5.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from engine.view_filters import FILTERS, brush_bounds
from editor import brush_groups

_BUTTON_STYLE = """
    QPushButton { background-color: #333; color: #ddd; border: 1px solid #555;
                  border-radius: 4px; padding: 5px 8px; }
    QPushButton:hover { background-color: #444; }
    QPushButton:disabled { color: #777; }
"""
_LOCK_STYLE = """
    QPushButton { background-color: #333; border: 1px solid #555; border-radius: 4px; }
    QPushButton:hover { background-color: #444; }
    QPushButton:checked { background-color: #F08000; border: 1px solid #F08000; }
"""
# Column headers are drawn natively (white on Windows) unless styled here.
_HEADER_STYLE = """
    QHeaderView { background-color: #333; border: none; }
    QHeaderView::section { background-color: #333; color: white;
                           padding: 4px 6px; border: none;
                           border-right: 1px solid #555;
                           border-bottom: 1px solid #555; }
"""


class VisgroupsWindow(QWidget):
    def __init__(self, main_window):
        # A window of its own (not a Tool window), so it only floats above
        # everything when its padlock says so.
        super().__init__(main_window, Qt.Window)
        self.main_window = main_window
        self.setWindowTitle("Visgroups & Cordon")
        self.resize(320, 560)
        self.setStyleSheet("QWidget { background-color: #2b2b2b; color: #ddd; }")
        self._refreshing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        header = QHBoxLayout()
        title = QLabel("Visgroups & Cordon")
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        self.lock_btn = QPushButton()
        lock_icon = os.path.join(main_window.root_dir, 'assets', 'lock.png')
        if asset_exists(lock_icon):
            self.lock_btn.setIcon(load_icon(lock_icon))
            self.lock_btn.setIconSize(QSize(18, 18))
        else:
            self.lock_btn.setText("\U0001F512")
        self.lock_btn.setFixedSize(30, 30)
        self.lock_btn.setCheckable(True)
        self.lock_btn.setStyleSheet(_LOCK_STYLE)
        self.lock_btn.setToolTip("Keep this window on top of everything")
        self.lock_btn.toggled.connect(self.set_always_on_top)
        header.addWidget(self.lock_btn)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._user_tab(), "User")
        self.tabs.addTab(self._groups_tab(), "Groups")
        self.tabs.addTab(self._auto_tab(), "Auto")
        self.tabs.addTab(self._cordon_tab(), "Cordon")
        layout.addWidget(self.tabs)

        config = main_window.config
        on_top = config.getboolean('Visgroups', 'always_on_top', fallback=False)
        self.lock_btn.setChecked(on_top)
        self.refresh()

    # -- helpers ---------------------------------------------------------------------

    @property
    def filters(self):
        return self.main_window.state.view_filters

    def _selection(self):
        return [o for o in (self.main_window.state.selected_objects or []) if o is not None]

    def _changed(self, saved=True, refresh=True):
        """Redraw the editor's views; visgroups and the cordon are map data."""
        if saved:
            self.main_window.mark_as_modified()
        self.main_window.update_views()
        if refresh:
            self.refresh()

    @staticmethod
    def _button(text, tip, slot):
        button = QPushButton(text)
        button.setStyleSheet(_BUTTON_STYLE)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    # -- always on top -----------------------------------------------------------------

    def set_always_on_top(self, on):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if visible:
            self.show()                       # a flag change hides a window
        config = self.main_window.config
        if not config.has_section('Visgroups'):
            config.add_section('Visgroups')
        config.set('Visgroups', 'always_on_top', str(bool(on)))
        try:
            self.main_window.save_config()
        except Exception:
            pass

    @property
    def always_on_top(self):
        return bool(self.windowFlags() & Qt.WindowStaysOnTopHint)

    # -- User tab ----------------------------------------------------------------------

    def _user_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.group_tree = QTreeWidget()
        self.group_tree.setHeaderLabels(["Visgroup", "Objects"])
        self.group_tree.header().setStyleSheet(_HEADER_STYLE)
        self.group_tree.setRootIsDecorated(False)
        self.group_tree.setColumnWidth(0, 200)
        self.group_tree.itemChanged.connect(self._on_group_item_changed)
        self.group_tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.group_tree)

        grid = QGridLayout()
        self.new_btn = self._button("New from Selection",
                                    "Make a visgroup of the selected objects", self.new_group)
        self.add_btn = self._button("Add Selection",
                                    "Put the selected objects in this visgroup", self.add_selection)
        self.remove_btn = self._button("Remove Selection",
                                       "Take the selected objects out of this visgroup",
                                       self.remove_selection)
        self.select_btn = self._button("Select Members",
                                       "Select every object in this visgroup", self.select_members)
        self.delete_btn = self._button("Delete", "Delete this visgroup (its objects stay)",
                                       self.delete_group)
        grid.addWidget(self.new_btn, 0, 0, 1, 2)
        grid.addWidget(self.add_btn, 1, 0)
        grid.addWidget(self.remove_btn, 1, 1)
        grid.addWidget(self.select_btn, 2, 0)
        grid.addWidget(self.delete_btn, 2, 1)
        layout.addLayout(grid)
        return tab

    def current_group(self):
        item = self.group_tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _member_count(self, group):
        state = self.main_window.state
        ids = {str(b.get('id')) for b in state.brushes}
        ids |= {str(t.properties.get('id')) for t in state.things}
        return len(group.ids & ids)

    def new_group(self):
        selection = self._selection()
        name = f"Visgroup {len(self.filters.visgroups) + 1}"
        group = self.filters.add_visgroup(name, selection)
        self._changed()
        self._select_group(group)
        return group

    def add_selection(self):
        group = self.current_group()
        if group is not None and self._selection():
            self.filters.add_to_visgroup(group, self._selection())
            self._changed()

    def remove_selection(self):
        group = self.current_group()
        if group is not None and self._selection():
            self.filters.remove_from_visgroup(group, self._selection())
            self._changed()

    def select_members(self):
        group = self.current_group()
        if group is None:
            return
        state = self.main_window.state
        members = [b for b in state.brushes if str(b.get('id')) in group.ids]
        members += [t for t in state.things if str(t.properties.get('id')) in group.ids]
        self.main_window.set_selected_objects(members)
        self.main_window.update_views()

    def delete_group(self):
        group = self.current_group()
        if group is not None:
            self.filters.remove_visgroup(group)
            self._changed()

    def show_group(self, group=None, rename=False):
        """Show the User tab with *group* selected (its name being edited)."""
        self.refresh()
        self.tabs.setCurrentIndex(0)
        if group is None:
            return
        self._select_group(group)
        item = self.group_tree.currentItem()
        if rename and item is not None:
            self.group_tree.editItem(item, 0)

    def _select_group(self, group):
        for i in range(self.group_tree.topLevelItemCount()):
            item = self.group_tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) is group:
                self.group_tree.setCurrentItem(item)

    def _on_group_item_changed(self, item, column):
        if self._refreshing:
            return
        group = item.data(0, Qt.UserRole)
        if column == 0:
            visible = item.checkState(0) == Qt.Checked
            if visible != group.visible:
                self.filters.set_visgroup_visible(group, visible)
            if item.text(0).strip() and item.text(0) != group.name:
                self.filters.rename_visgroup(group, item.text(0))
            # Not rebuilt from inside the item's own change signal.
            self._changed(refresh=False)

    def _update_buttons(self):
        has_group = self.current_group() is not None
        has_selection = bool(self._selection())
        self.new_btn.setEnabled(has_selection)
        self.add_btn.setEnabled(has_group and has_selection)
        self.remove_btn.setEnabled(has_group and has_selection)
        self.select_btn.setEnabled(has_group)
        self.delete_btn.setEnabled(has_group)
        has_brush_group = bool(self.current_brush_group())
        self.group_from_selection_btn.setEnabled(
            any(brush_groups.is_groupable(o) for o in self._selection()))
        self.select_brush_group_btn.setEnabled(has_brush_group)
        self.edit_brush_group_btn.setEnabled(has_brush_group)
        self.ungroup_btn.setEnabled(has_brush_group)

    # -- Groups tab --------------------------------------------------------------------

    def _groups_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.brush_group_tree = QTreeWidget()
        self.brush_group_tree.setHeaderLabels(["Group / Member", "Members"])
        self.brush_group_tree.header().setStyleSheet(_HEADER_STYLE)
        self.brush_group_tree.setColumnWidth(0, 200)
        self.brush_group_tree.itemChanged.connect(self._on_brush_group_renamed)
        self.brush_group_tree.itemSelectionChanged.connect(self._update_buttons)
        self.brush_group_tree.itemDoubleClicked.connect(self._on_brush_group_double_click)
        layout.addWidget(self.brush_group_tree)

        grid = QGridLayout()
        self.group_from_selection_btn = self._button(
            "Group Selection...", "Attach the selected brushes and entities to "
            "one another: they always move together in the editor (and brushes "
            "in Play)",
            self.group_selection)
        self.select_brush_group_btn = self._button(
            "Select Members", "Select every brush and entity in this group",
            self.select_brush_group)
        self.edit_brush_group_btn = self._button(
            "Edit...", "Rename this group, or add and remove members",
            self.edit_brush_group)
        self.ungroup_btn = self._button(
            "Ungroup", "Dissolve this group (its members stay, detached)",
            self.ungroup_brush_group)
        grid.addWidget(self.group_from_selection_btn, 0, 0, 1, 2)
        grid.addWidget(self.select_brush_group_btn, 1, 0)
        grid.addWidget(self.edit_brush_group_btn, 1, 1)
        grid.addWidget(self.ungroup_btn, 2, 0, 1, 2)
        layout.addLayout(grid)
        return tab

    def current_brush_group(self):
        """The name of the group the selected row is, or is a brush of."""
        item = self.brush_group_tree.currentItem()
        if item is None:
            return ''
        if item.parent() is not None:
            item = item.parent()
        return item.data(0, Qt.UserRole) or ''

    def _brush_group_members(self, name=None):
        name = self.current_brush_group() if name is None else name
        return brush_groups.members(
            brush_groups.scene_objects(self.main_window.state), name)

    def group_selection(self):
        brushes = [o for o in self._selection() if brush_groups.is_groupable(o)]
        if brushes:
            self.main_window.open_brush_group_dialog(brushes)

    def select_brush_group(self):
        chosen = self._brush_group_members()
        if chosen:
            self.main_window.set_selected_objects(chosen)
            self.main_window.update_views()

    def edit_brush_group(self):
        chosen = self._brush_group_members()
        if chosen:
            self.main_window.open_brush_group_dialog(chosen)

    def ungroup_brush_group(self):
        chosen = self._brush_group_members()
        if chosen:
            self.main_window.set_brush_group(chosen, '')

    def _on_brush_group_double_click(self, item, _column):
        # ('brush' | 'thing', index): Qt would copy a dict stored as item data.
        data = item.data(0, Qt.UserRole + 1)
        if not data:
            return
        kind, index = data
        state = self.main_window.state
        objects = state.brushes if kind == 'brush' else state.things
        if 0 <= index < len(objects):
            self.main_window.set_selected_objects([objects[index]])
            self.main_window.update_views()

    def _on_brush_group_renamed(self, item, column):
        if self._refreshing or column != 0 or item.parent() is not None:
            return
        old, new = item.data(0, Qt.UserRole), item.text(0)
        # Not rebuilt from inside the item's own change signal.
        QTimer.singleShot(0, lambda: self._rename_brush_group(old, new))

    def _rename_brush_group(self, old, new):
        if not self.main_window.rename_brush_group(old, new):
            self.refresh()

    def _refresh_brush_groups(self):
        tree = self.brush_group_tree
        current = self.current_brush_group()
        tree.clear()
        state = self.main_window.state
        index_of = {id(b): ('brush', i) for i, b in enumerate(state.brushes)}
        index_of.update((id(t), ('thing', i)) for i, t in enumerate(state.things))
        every = brush_groups.scene_objects(state)
        colours = brush_groups.colours(every)
        label = brush_groups.labeller(
            state, getattr(self.main_window, 'scene_hierarchy', None))
        for name, chosen in brush_groups.groups(every).items():
            top = QTreeWidgetItem([name, str(len(chosen))])
            top.setData(0, Qt.UserRole, name)
            top.setFlags(top.flags() | Qt.ItemIsEditable)
            top.setForeground(0, QBrush(QColor(colours[name])))
            tree.addTopLevelItem(top)
            for brush in chosen:
                child = QTreeWidgetItem(top, [label(brush), ""])
                child.setData(0, Qt.UserRole + 1, index_of[id(brush)])
            top.setExpanded(True)
            if name == current:
                tree.setCurrentItem(top)

    # -- Auto tab ----------------------------------------------------------------------

    def _auto_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.auto_list = QListWidget()
        for key, label in FILTERS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.auto_list.addItem(item)
        self.auto_list.itemChanged.connect(self._on_auto_item_changed)
        layout.addWidget(self.auto_list)
        layout.addWidget(self._button("Show All", "Show every kind of object",
                                      self._show_all_auto))
        return tab

    def _on_auto_item_changed(self, item):
        if self._refreshing:
            return
        # The Filter menu's own setter, so the menu ticks follow.
        self.main_window.set_view_filter(item.data(Qt.UserRole),
                                         item.checkState() == Qt.Checked)

    def _show_all_auto(self):
        self.main_window.show_all_view_filters()
        self.refresh()

    # -- Cordon tab --------------------------------------------------------------------

    def _cordon_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.cordon_check = QCheckBox("Cordon on: show only what is inside the box")
        self.cordon_check.toggled.connect(self._on_cordon_edited)
        layout.addWidget(self.cordon_check)

        grid = QGridLayout()
        self.cordon_min, self.cordon_max = [], []
        for col, axis in enumerate("XYZ", start=1):
            grid.addWidget(QLabel(axis), 0, col, alignment=Qt.AlignCenter)
        for row, (label, spins) in enumerate((("Min", self.cordon_min),
                                              ("Max", self.cordon_max)), start=1):
            grid.addWidget(QLabel(label), row, 0)
            for col in range(3):
                spin = QDoubleSpinBox()
                spin.setRange(-1e6, 1e6)
                spin.setDecimals(0)
                spin.setSingleStep(16)
                spin.valueChanged.connect(self._on_cordon_edited)
                grid.addWidget(spin, row, col + 1)
                spins.append(spin)
        layout.addLayout(grid)
        layout.addWidget(self._button(
            "Set from Selection", "Fit the cordon round the selected objects",
            self.cordon_from_selection))
        layout.addStretch()
        return tab

    def _on_cordon_edited(self, *_args):
        if self._refreshing:
            return
        self.filters.set_cordon(enabled=self.cordon_check.isChecked(),
                                lo=[s.value() for s in self.cordon_min],
                                hi=[s.value() for s in self.cordon_max])
        self._changed()

    def cordon_from_selection(self):
        """Fit the cordon round the selection and turn it on."""
        selection = self._selection()
        if not selection:
            self.main_window.show_toast("Select something to cordon off")
            return
        los, his = [], []
        for obj in selection:
            if isinstance(obj, dict):
                lo, hi = brush_bounds(obj)
            else:
                lo = hi = [float(v) for v in obj.pos]
            los.append(lo)
            his.append(hi)
        lo = [min(p[i] for p in los) for i in range(3)]
        hi = [max(p[i] for p in his) for i in range(3)]
        self.filters.set_cordon(enabled=True, lo=lo, hi=hi)
        self._changed()

    # -- refresh -----------------------------------------------------------------------

    def refresh(self):
        """Show the current visgroups, filter groups and cordon."""
        self._refreshing = True
        try:
            current = self.current_group()
            self.group_tree.clear()
            for group in self.filters.visgroups:
                item = QTreeWidgetItem([group.name, str(self._member_count(group))])
                item.setData(0, Qt.UserRole, group)
                # The UUID an I/O connection targeting the group stores.
                item.setToolTip(0, "Group ID: %s\nI/O can target this group: every "
                                   "member receives the input." % group.id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                item.setCheckState(0, Qt.Checked if group.visible else Qt.Unchecked)
                self.group_tree.addTopLevelItem(item)
                if group is current:
                    self.group_tree.setCurrentItem(item)
            self._refresh_brush_groups()
            for i in range(self.auto_list.count()):
                item = self.auto_list.item(i)
                shown = self.filters.shows(item.data(Qt.UserRole))
                item.setCheckState(Qt.Checked if shown else Qt.Unchecked)
            cordon = self.filters.cordon
            self.cordon_check.setChecked(cordon.enabled)
            for spin, value in zip(self.cordon_min + self.cordon_max, cordon.lo + cordon.hi):
                spin.setValue(value)
        finally:
            self._refreshing = False
        self._update_buttons()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)
        self.main_window.sync_visgroups_button()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.main_window.sync_visgroups_button()
