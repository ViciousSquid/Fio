from engine.asset_qt import load_icon
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, 
                             QMenu, QAction, QHeaderView, QAbstractItemView, 
                             QPushButton, QHBoxLayout, QLabel, QFrame, QGridLayout,
                             QLineEdit, QStyle, QStyledItemDelegate)
from PyQt5.QtGui import QIcon, QColor, QBrush, QFont, QPainter, QPixmap, QPen, QPolygonF
from PyQt5 import QtCore
import re
from PyQt5.QtCore import Qt, QTimer, QEvent, QPointF

from engine.change_journal import touch
from engine.spatial import set_live_flag
from editor.selection import SelectionLookup
from editor import brush_groups

try:
    from editor.io_system import io_revision as _io_revision
except ImportError:                                  # pragma: no cover
    def _io_revision():
        return 0

#: Item data: a grouped brush row's group colour ('#rrggbb') and which edges
#: of its group's outline box it carries (1: first row, 2: last row).
GROUP_COLOUR_ROLE = Qt.UserRole + 1
GROUP_EDGE_ROLE = Qt.UserRole + 2
GROUP_FIRST, GROUP_LAST = 1, 2
#: How strongly a group's colour tints its rows' background.
GROUP_TINT_ALPHA = 46
#: The section titles' background (their text is white).
HEADER_BACKGROUND = "#000000"


def group_tint(colour):
    """The row background for a group of *colour* (transparent for none)."""
    if not colour:
        return QBrush(Qt.transparent)
    tint = QColor(colour)
    tint.setAlpha(GROUP_TINT_ALPHA)
    return QBrush(tint)


class GroupOutlineDelegate(QStyledItemDelegate):
    """Draws one box, in the group's colour, round the rows of each brush
    group: sides on every member, the top on the first, the bottom on the
    last."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # The row background is painted once, by paint() below -- never a
        # second time by the style, which would double a translucent tint
        # wherever the style does honour item backgrounds.
        option.backgroundBrush = QBrush()

    def paint(self, painter, option, index):
        # The row's own background -- a section title's black, a group's
        # tint, a search match -- is painted here: the tree's style sheet
        # takes over item painting, and Qt's style-sheet style then ignores
        # per-item background brushes, so they never showed. A selected row
        # keeps the selection colour.
        background = index.data(Qt.BackgroundRole)
        if (isinstance(background, QBrush) and background.style() != Qt.NoBrush
                and background.color().alpha() > 0
                and not option.state & QStyle.State_Selected):
            painter.fillRect(option.rect, background)
        super().paint(painter, option, index)
        if not index.parent().isValid():
            self._paint_section_arrow(painter, option, index)
            return
        colour = index.data(GROUP_COLOUR_ROLE)
        if not colour:
            return
        edges = index.data(GROUP_EDGE_ROLE) or 0
        rect = option.rect.adjusted(1, 0, -1, 0)
        painter.save()
        painter.setPen(QPen(QColor(colour), 2))
        painter.drawLine(rect.topLeft(), rect.bottomLeft())
        painter.drawLine(rect.topRight(), rect.bottomRight())
        if edges & GROUP_FIRST:
            painter.drawLine(rect.left(), rect.top() + 1, rect.right(), rect.top() + 1)
        if edges & GROUP_LAST:
            painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()

    def _paint_section_arrow(self, painter, option, index):
        """A section title's expand/collapse arrow, at its right end (the
        tree has no branch column; a title still opens and closes on a
        double-click)."""
        model = index.model()
        if model is None or not model.hasChildren(index):
            return
        tree = self.parent()
        expanded = bool(tree is not None and tree.isExpanded(index))
        rect = option.rect
        size = 4.0
        cx = rect.right() - 12.0
        cy = rect.center().y() + 0.5
        if expanded:                    # pointing down
            points = (QPointF(cx - size, cy - size / 2), QPointF(cx + size, cy - size / 2),
                      QPointF(cx, cy + size / 2))
        else:                           # pointing right
            points = (QPointF(cx - size / 2, cy - size), QPointF(cx - size / 2, cy + size),
                      QPointF(cx + size / 2, cy))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('white'))
        painter.drawPolygon(QPolygonF(points))
        painter.restore()


try:
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False

class SceneHierarchy(QWidget):
    """Scene hierarchy widget with sort button and tree view."""
    
    # Sort modes
    SORT_DEFAULT = 0
    SORT_ALPHABETICAL = 1
    SORT_BY_TYPE = 2
    SORT_MODE_COUNT = 3
    
    SORT_MODE_NAMES = {
        SORT_DEFAULT: "Default",
        SORT_ALPHABETICAL: "A-Z",
        SORT_BY_TYPE: "By Type"
    }
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.sort_mode = self.SORT_DEFAULT
        self._highlighted_item = None
        self._highlight_backgrounds = None
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_highlight)
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Search container (QLineEdit + Match Whole Word button)
        search_container = QWidget()
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(0)

        # Search box, above everything: on a level with a few hundred objects
        # the hierarchy is the only place to find one by name, and scrolling is
        # not finding.
        self.search_box = QLineEdit()
        # Just "Search": the box is narrow, and a longer hint was elided to
        # "Search scene... (Ente..." — a placeholder that cannot be read whole
        # is decoration, not help. The Enter behaviour is in the tooltip.
        self.search_box.setPlaceholderText("Search")
        self.search_box.setToolTip(
            "Type part of a name or type, then press Enter to select every match")
        # Qt's built-in clear button lives on the right; this one is on the
        # left, next to where the eye already is, and only appears once there
        # are results to clear.
        self.search_box.setClearButtonEnabled(False)
        self.search_box.setFixedHeight(38)
        self.search_box.setStyleSheet("""
            QLineEdit {
                background-color: #1b1b1f;
                color: #e0e0e0;
                border: none;
                border-bottom: 1px solid #425F5D;
                padding: 6px 10px;
            }
            QLineEdit:focus { border-bottom: 1px solid #E4D00A; }
        """)
        self.search_box.returnPressed.connect(self.apply_search)
        self.search_box.textChanged.connect(self._on_search_text_changed)

        clear_icon = self.style().standardIcon(QStyle.SP_LineEditClearButton)
        if clear_icon.isNull():          # not every style ships that one
            clear_icon = self.style().standardIcon(QStyle.SP_DialogCloseButton)
        self.clear_search_action = self.search_box.addAction(
            clear_icon, QLineEdit.LeadingPosition)
        self.clear_search_action.setToolTip("Clear the search")
        self.clear_search_action.triggered.connect(self.clear_search)
        self.clear_search_action.setVisible(False)

        search_layout.addWidget(self.search_box, stretch=1)

        # Match Whole Word Button ("ab")
        self.match_word_button = QPushButton("ab")
        self.match_word_button.setCheckable(True)
        self.match_word_button.setFixedSize(38, 38)
        self.match_word_button.setToolTip("Match Whole Word")
        self.match_word_button.setStyleSheet("""
            QPushButton {
                min-width: 0px;
                background-color: #1b1b1f;
                color: #888888;
                border: none;
                border-bottom: 1px solid #425F5D;
                font-weight: bold;
                font-family: 'Fio Mono';
                font-size: 13px;
            }
            QPushButton:hover {
                color: #ffffff;
                background-color: #2a2a30;
            }
            QPushButton:checked {
                color: #ffffff;
                background-color: #007acc;
                border-bottom: 1px solid #007acc;
            }
        """)
        self.match_word_button.toggled.connect(self.apply_search)
        search_layout.addWidget(self.match_word_button)

        layout.addWidget(search_container)

        #: Names matched by the last search, so a rebuilt tree keeps showing them.
        self._search_matches = set()
        #: The last rebuild's rows -- ``(headers, [(item, kind, index)],
        #: terrain item)`` -- and the :meth:`_structure_key` they were built
        #: for. A refresh whose key is unchanged only reselects rows.
        self._rows = None
        self._rows_key = None
        #: ``(token, objects key)``: the per-object half of the structure key,
        #: kept until the world says something changed (:meth:`_world_token`).
        self._objects_key_cache = None
        #: ``(token, rows, {id(obj): (item, obj)}, exact)``: which row stands
        #: for which object, so a selection change touches only the rows whose
        #: selection changed (:meth:`_rows_selected_by_identity`).
        self._row_index = None
        #: A refresh was skipped while the panel was out of sight; the next
        #: show catches up (:meth:`showEvent`).
        self._stale = False

        # Create sort button
        self.sort_button = QPushButton(f"Sort: {self.SORT_MODE_NAMES[self.sort_mode]}")
        self.sort_button.setFixedHeight(38)
        self.sort_button.clicked.connect(self.cycle_sort_mode)
        self.sort_button.setStyleSheet("""
            QPushButton {
                background-color: #000000;
                color: white;
                border: none;
                padding: 4px 8px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #5a7a78;
            }
            QPushButton:pressed {
                background-color: #354b4a;
            }
        """)
        layout.addWidget(self.sort_button)
        
        # Create content container (tree + bottom overlay)
        self.content_container = QWidget()
        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        
        # Create tree widget
        self.tree = QTreeWidget()
        self.tree.setUniformRowHeights(True)
        # Rows sit flush under their section titles (Brushes, Things, ...):
        # no indentation and no branch column. The titles draw their own
        # expand/collapse arrow (GroupOutlineDelegate).
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setItemDelegate(GroupOutlineDelegate(self.tree))
        content_layout.addWidget(self.tree, stretch=2)
        
        # Configure tree widget
        self.tree.header().setVisible(False) 
        self.tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # One column: tag, hidden and lock indicators are the item's icon, so
        # they sit left of the name and stay visible at any width; a long
        # name is elided instead.
        self.tree.setColumnCount(1)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setIconSize(QtCore.QSize(44, 20))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_menu)
        self.lock_icon = load_icon("assets/lock.png")
        self.hidden_icon = load_icon("assets/hidden.png")
        self.tree.itemSelectionChanged.connect(self.handle_selection_change)
        #: True while a selection made in the tree is being applied (its
        #: rebuild keeps the list still: refresh_list).
        self._selecting_in_tree = False
        #: Section titles (Brushes, Things, ...) the user has closed.
        self._collapsed_sections = set()
        # Double-clicks are read off the viewport, not itemDoubleClicked: the
        # first click of a double-click changes the selection, which rebuilds
        # the tree, so Qt never pairs the two clicks on one item.
        self.tree.viewport().installEventFilter(self)

        # Enable extended selection (shift-click, ctrl-click)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)

        # Highlight colour
        self.tree.setStyleSheet("""
            QTreeWidget::item:selected {
                background-color: #E4D00A;
                color: black;
            }
            QTreeWidget::item:selected:!active {
                background-color: #c87c2a;
            }
        """)

        # Load colour icons in rainbow order (ROYGIBV) with white last
        self.colour_icons = {
            'red': load_icon("assets/circ_red.png"),
            'orange': load_icon("assets/circ_orange.png"),
            'yellow': load_icon("assets/circ_yellow.png"),
            'green': load_icon("assets/circ_green.png"),
            'blue': load_icon("assets/circ_blue.png"),
            'pink': load_icon("assets/circ_pink.png"),
            'white': load_icon("assets/circ_white.png"),
        }
        self.colour_names = {
            "circ_red.png": "red",
            "circ_orange.png": "orange",
            "circ_yellow.png": "yellow",
            "circ_green.png": "green",
            "circ_blue.png": "blue",
            "circ_pink.png": "pink",
            "circ_white.png": "white",
        }

        layout.addWidget(self.content_container)

    # =====================================================================
    # SEARCH
    # =====================================================================

    @staticmethod
    def _searchable_text(obj, display_name):
        """Everything a search should match on for one object.

        The display name, the authored name and the entity type — so "monster"
        finds every monster and "door" finds both a brush called ``door_main``
        and every door brush, which is what someone typing either actually
        wants.
        """
        parts = [display_name or ""]
        if isinstance(obj, dict):
            parts.append(obj.get("name", "") or "")
            for flag, label in (("is_trigger", "trigger"), ("is_door", "door"),
                                ("is_mover", "mover"), ("is_water", "water"),
                                ("is_fog", "fog")):
                if obj.get(flag):
                    parts.append(label)
            if not any(obj.get(f) for f, _ in (("is_trigger", 0), ("is_door", 0),
                                               ("is_mover", 0), ("is_water", 0),
                                               ("is_fog", 0))):
                parts.append("brush")
        else:
            props = obj.properties
            parts.append(props.get("name", "") or "")
            parts.append(props.get("type", "") or "")
            parts.append(type(obj).__name__)
        return " ".join(parts).lower()

    def find_matches(self, text):
        """Every object whose name or type matches *text*."""
        needle = (text or "").strip().lower()
        if not needle:
            return []

        match_whole = self.match_word_button.isChecked()
        if match_whole:
            pattern = re.compile(r'\b' + re.escape(needle) + r'\b', re.IGNORECASE)
            is_match = lambda text: bool(pattern.search(text))
        else:
            is_match = lambda text: needle in text

        matches = []
        state = self.main_window.state
        stream = state.editor_stream
        if stream is not None and not stream.suspended:
            # A camera-streamed Big World map: search every cell, loading
            # only the objects that match.
            stream.merge(state._serialize_brushes(), [t.to_dict() for t in state.things])
            for kind, records in stream.records.items():
                for identity, record in list(records.items()):
                    props = record if kind == 'brushes' else record.get('properties', {})
                    text = self._searchable_text(props, props.get('name', ''))
                    if kind == 'things':
                        text += ' ' + str(props.get('type', record.get('type', ''))).lower()
                    if is_match(text):
                        obj = state.find_entity_by_id(identity)
                        if obj is not None:
                            matches.append(obj)
            return matches
        for index, brush in enumerate(self.main_window.state.brushes):
            name = self._get_brush_display_name(brush, index)
            if is_match(self._searchable_text(brush, name)):
                matches.append(brush)
        for thing in self.main_window.state.things:
            name = thing.properties.get("name", "")
            if is_match(self._searchable_text(thing, name)):
                matches.append(thing)
        return matches

    def _on_search_text_changed(self, text):
        """Clearing the box drops the highlight; typing waits for Enter.

        Selecting on every keystroke would rebuild the tree and move the 2D
        views on each letter typed, which is unusable.
        """
        if not (text or "").strip():
            self._search_matches = set()
            self._apply_search_styling()
            self._update_clear_button()

    def _update_clear_button(self):
        """Show the clear button only while results are on screen.

        It clears a *result*, not the text — offering it the moment someone
        starts typing would put a button under the cursor that undoes work
        nobody has done yet.
        """
        action = getattr(self, 'clear_search_action', None)
        if action is not None:
            action.setVisible(bool(self._search_matches))

    def clear_search(self):
        """Empty the box and drop the highlight and the selection with it."""
        self._search_matches = set()
        self.search_box.clear()
        self._apply_search_styling()
        self._update_clear_button()
        self.main_window.set_selected_objects([])

    def apply_search(self):
        """Select every match and show how many there were.

        Selecting them is what makes the result useful rather than decorative:
        the selection is the editor's own, so the matches light up in the 2D and
        3D views too, and Focus On works on the lot.
        """
        text = self.search_box.text()
        matches = self.find_matches(text)
        self._search_matches = {id(obj) for obj in matches}

        if not matches:
            self._apply_search_styling()
            self._update_clear_button()
            if (text or "").strip():
                self.main_window.show_toast("No object matches '%s'" % text.strip(),
                                            is_error=True)
            return

        self.main_window.set_selected_objects(matches)
        self._apply_search_styling()
        self._update_clear_button()
        first = self._tree_item_for(matches[0])
        if first is not None:
            self.tree.scrollToItem(first)
        self.main_window.show_toast(
            "%d match%s for '%s'" % (len(matches), "" if len(matches) == 1 else "es",
                                     text.strip()))

    def _tree_item_for(self, obj):
        """The tree row standing for *obj*, or None."""
        state = self.main_window.state
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                data = item.data(0, Qt.UserRole)
                if not data:
                    continue
                kind, index = data
                try:
                    if kind == 'brush' and state.brushes[index] is obj:
                        return item
                    if kind == 'thing' and state.things[index] is obj:
                        return item
                except (IndexError, TypeError):
                    continue
        return None

    def _apply_search_styling(self):
        """Tint every matching row, so several matches are all visible at once.

        Selection alone is not enough when the matches are scattered: only the
        rows on screen show it, and the count in the toast does not say *which*.
        """
        state = self.main_window.state
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                data = item.data(0, Qt.UserRole)
                if not data:
                    continue
                kind, index = data
                obj = None
                try:
                    if kind == 'brush':
                        obj = state.brushes[index]
                    elif kind == 'thing':
                        obj = state.things[index]
                except (IndexError, TypeError):
                    obj = None
                matched = obj is not None and id(obj) in self._search_matches
                background = (QBrush(QColor("#4a4320")) if matched
                              else group_tint(item.data(0, GROUP_COLOUR_ROLE)))
                if item is self._highlighted_item:
                    # A search can change before the temporary highlight's
                    # timer expires. Its cleanup must keep the new styling.
                    self._highlight_backgrounds = (background, background)
                for column in (0, 1):
                    item.setBackground(column, background)

    def groups_of(self, objects):
        """``(id, name)`` of the groups any of *objects* is in: visgroups by
        their UUID, then brush groups (id '')."""
        filters = self.main_window.state.view_filters
        found = []
        for obj in objects:
            for group in filters.visgroups_of(obj):
                if (group.id, group.name) not in found:
                    found.append((group.id, group.name))
        for obj in objects:
            # Brushes and entities alike: a grouped model offers its group.
            name = brush_groups.group_of(obj)
            if name and ('', name) not in found:
                found.append(('', name))
        return found

    def select_group(self, name='', group_id=''):
        """Select every member of a group -- what an I/O connection targeting
        it reaches (engine.io_groups): the visgroup whose UUID is *group_id*,
        else the group called *name*."""
        from engine.io_groups import resolve_group
        main_window = self.main_window
        members = resolve_group(main_window.state, name, group_id) or []
        main_window.set_selected_objects(members)
        main_window.update_views()
        return members

    def _add_select_group_actions(self, menu, objects):
        """"Select Group 'name'" for each group the clicked objects are in."""
        actions = []
        for group_id, name in self.groups_of(objects):
            action = menu.addAction("Select Group '%s'" % name)
            action.setToolTip(
                "Select every member of this group. An I/O connection that "
                "targets the group sends its input to all of them; members "
                "without that input ignore it."
                + ("\nGroup ID: %s" % group_id if group_id else ""))
            action.triggered.connect(
                lambda _checked=False, n=name, g=group_id: self.select_group(n, g))
            actions.append(action)
        return actions

    def _add_visgroup_menu(self, menu, objects):
        """The Visgroup submenu: put the clicked objects in a visgroup, take
        them out, or manage visgroups in Visgroups & Cordon."""
        main_window = self.main_window
        filters = main_window.state.view_filters
        objects = list(objects)
        sub = menu.addMenu("Visgroup")
        new = sub.addAction("New Visgroup from Selection...")
        new.setToolTip("Make a visgroup of these objects and name it")
        new.triggered.connect(
            lambda _checked=False: main_window.add_to_visgroup(objects, None))
        member_of = {id(g) for o in objects for g in filters.visgroups_of(o)}
        if filters.visgroups:
            sub.addSeparator()
            for group in filters.visgroups:
                add = sub.addAction("Add to '%s'" % group.name)
                add.setEnabled(not all(group in filters.visgroups_of(o) for o in objects))
                add.triggered.connect(
                    lambda _checked=False, g=group: main_window.add_to_visgroup(objects, g))
            removable = [g for g in filters.visgroups if id(g) in member_of]
            if removable:
                sub.addSeparator()
                for group in removable:
                    remove = sub.addAction("Remove from '%s'" % group.name)
                    remove.triggered.connect(
                        lambda _checked=False, g=group:
                        main_window.remove_from_visgroup(objects, g))
        sub.addSeparator()
        groups = [g for g in filters.visgroups if id(g) in member_of]
        manage = sub.addAction("Manage Visgroups...")
        manage.triggered.connect(
            lambda _checked=False: main_window.show_visgroup(groups[0] if groups else None))
        return sub

    def _add_group_action(self, menu, brushes):
        """"Add to Group..." for objects (brushes and/or entities) that are in
        none, "Edit Group..." when one of them already is: both open the
        group dialog."""
        brushes = [b for b in brushes if brush_groups.is_groupable(b)]
        if not brushes:
            return None
        grouped = any(brush_groups.group_of(b) for b in brushes)
        action = menu.addAction("Edit Group..." if grouped else "Add to Group...")
        action.setToolTip("Grouped objects are attached: they always move together "
                          "in the editor (and brushes in Play)")
        action.triggered.connect(
            lambda _checked=False, chosen=list(brushes):
            self.main_window.open_brush_group_dialog(chosen))
        if grouped:
            ungroup = menu.addAction("Ungroup")
            ungroup.setToolTip("Take these objects out of their group")
            ungroup.triggered.connect(
                lambda _checked=False, chosen=list(brushes):
                self.main_window.ungroup_brushes(chosen))
            every = self.main_window.with_group_mates(
                [b for b in brushes if brush_groups.group_of(b)])
            hidden = all(brush_groups.is_hidden(b) for b in every)
            toggle = menu.addAction("Show Group" if hidden else "Hide Group")
            toggle.setToolTip("Show or hide every object in the group")
            toggle.triggered.connect(
                lambda _checked=False, chosen=every, hide=not hidden:
                self.main_window.set_brushes_hidden(chosen, hide))
        return action

    def cycle_sort_mode(self):
        """Cycle through sort modes and refresh."""
        self.sort_mode = (self.sort_mode + 1) % self.SORT_MODE_COUNT
        self.sort_button.setText(f"Sort: {self.SORT_MODE_NAMES[self.sort_mode]}")
        self.refresh_list()

    def _get_brush_display_name(self, brush_dict, index):
        """Get display name for a brush, using 'Trigger X' for triggers."""
        # If brush has a custom name, use it
        if brush_dict.get('name'):
            return brush_dict['name']
        # If it's a trigger, name it "Trigger X"
        if brush_dict.get('is_trigger', False):
            return f'Trigger {index + 1}'
        # Default brush name
        return f'Brush {index + 1}'

    @staticmethod
    def _get_thing_display_name(thing_obj, index):
        """Get display name for an entity: its name, else 'Thing N'."""
        return thing_obj.name if thing_obj.name else f'Thing {index + 1}'

    @staticmethod
    def _mark_group_row(item, rows, row, group_colours):
        """Tint and box *item* (``rows[row]``) in its group's colour: the
        box's top on a group's first row, its bottom on the last."""
        group = brush_groups.group_of(rows[row][1])
        if not group:
            return
        colour = group_colours[group]
        edges = 0
        if row == 0 or brush_groups.group_of(rows[row - 1][1]) != group:
            edges |= GROUP_FIRST
        if row + 1 == len(rows) or brush_groups.group_of(rows[row + 1][1]) != group:
            edges |= GROUP_LAST
        item.setData(0, GROUP_COLOUR_ROLE, colour)
        item.setData(0, GROUP_EDGE_ROLE, edges)
        item.setBackground(0, group_tint(colour))
        item.setToolTip(0, "Group: %s" % group)

    def _get_sorted_brushes(self):
        """Brushes in the current sort order, each brush group's members
        kept together (where the group's first member sorts)."""
        return brush_groups.ordered(self._sorted_brushes())

    def _sorted_brushes(self):
        """Return brushes in sorted order based on current sort mode."""
        brushes = list(enumerate(self.main_window.state.brushes))
        
        if self.sort_mode == self.SORT_DEFAULT:
            return brushes
        
        elif self.sort_mode == self.SORT_ALPHABETICAL:
            return sorted(brushes, key=lambda x: self._get_brush_display_name(x[1], x[0]).lower())
        
        elif self.sort_mode == self.SORT_BY_TYPE:
            # Sort order: regular brushes, triggers, hidden, tagged
            def sort_key(item):
                idx, brush = item
                name = self._get_brush_display_name(brush, idx).lower()
                is_trigger = brush.get('is_trigger', False)
                is_hidden = brush.get('hidden', False)
                has_tag = 'color' in brush
                
                # Priority: 0=regular, 1=trigger, 2=hidden, 3=tagged
                if has_tag:
                    priority = 3
                elif is_hidden:
                    priority = 2
                elif is_trigger:
                    priority = 1
                else:
                    priority = 0
                return (priority, name)
            
            return sorted(brushes, key=sort_key)
        
        return brushes

    def _get_sorted_things(self):
        """Things in the current sort order, each group's entities kept
        together (where the group's first entity sorts)."""
        return brush_groups.ordered(self._sorted_things())

    def _sorted_things(self):
        """Return things in sorted order based on current sort mode."""
        things = list(enumerate(self.main_window.state.things))
        
        if self.sort_mode == self.SORT_DEFAULT:
            return things
        
        elif self.sort_mode == self.SORT_ALPHABETICAL:
            return sorted(things, key=lambda x: (x[1].name or f'Thing {x[0]+1}').lower())
        
        elif self.sort_mode == self.SORT_BY_TYPE:
            # Sort by type name, then alphabetically
            def sort_key(item):
                idx, thing = item
                name = (thing.name or f'Thing {idx+1}').lower()
                type_name = type(thing).__name__.lower()
                is_hidden = thing.properties.get('hidden', False)
                has_tag = 'color' in thing.properties
                
                # Priority: 0=regular, 1=hidden, 2=tagged
                if has_tag:
                    priority = 2
                elif is_hidden:
                    priority = 1
                else:
                    priority = 0
                return (type_name, priority, name)
            
            return sorted(things, key=sort_key)
        
        return things

    def _get_terrain_display_name(self):
        """Get a display name for the terrain based on its data."""
        terrain_data = self.main_window.state.terrain_data
        if terrain_data:
            biome = terrain_data.get('biome', '')
            if biome:
                return f'Terrain ({biome})'
        return 'Terrain'

    def _has_terrain(self):
        """Check if terrain data exists in the current scene."""
        terrain_data = self.main_window.state.terrain_data
        return terrain_data is not None

    def refresh_list(self, selection_only=False):
        """Rebuild the tree (every selection change does, through the editor's
        update_all_ui) without the list moving under the cursor.

        *selection_only*: the caller changed only the selection
        (``MainWindow.set_selected_objects``), so the objects' names and flags
        are re-read only if an editor transaction has happened since they last
        were (:meth:`_objects_key`).

        The scroll position is kept across the rebuild. A selection made in
        the tree itself -- a click, a Ctrl-click adding a row, a Shift-click
        range -- does not scroll at all, and the row clicked stays the current
        row (the anchor the next Shift-click ranges from). A selection made
        elsewhere (the 2D or 3D view) scrolls only if its first row is out of
        sight. Scrolling to the first selected row after every rebuild made
        the list jump to the top of a Ctrl-click selection on each click.
        """
        if self._out_of_sight():
            # A closed, collapsed or tabbed-away dock: nobody sees the rows,
            # and every selection change used to rebuild or rescan all of
            # them. Brought up to date once, when the panel is shown again.
            self._stale = True
            return
        self._stale = False
        bar = self.tree.verticalScrollBar()
        scrolled = bar.value()
        current_obj = self._object_of(self.tree.currentItem())
        from_tree = self._selecting_in_tree
        # A failed rebuild must not leave selection notifications disabled,
        # and callers may already have blocked signals for a larger operation.
        was_blocked = self.tree.blockSignals(True)
        try:
            key = self._structure_key(selection_only)
            if not self._reselect_rows(key):
                self._rebuild_items(key)
                # Lay the new rows out now, so the scroll range is the rebuilt
                # tree's (rebuilding row by row clamped the position as it went).
                self.tree.doItemsLayout()
                bar.setValue(min(scrolled, bar.maximum()))
                if current_obj is not None:
                    item = self._find_item(current_obj)
                    if item is not None:
                        self.tree.setCurrentItem(item, 0, QtCore.QItemSelectionModel.NoUpdate)
        finally:
            self.tree.blockSignals(was_blocked)

        if from_tree:
            return
        first_selected = self.tree.selectedItems()
        if first_selected:
            self.tree.scrollToItem(first_selected[0], QAbstractItemView.EnsureVisible)

    def _out_of_sight(self):
        """Whether the panel is hidden inside its window (its dock closed,
        collapsed to the strip, or behind another tab). A window that is not
        shown yet does not count: the panel would be seen once it is."""
        window = self.window()
        return window is not self and not self.isVisibleTo(window)

    def showEvent(self, event):
        super().showEvent(event)
        if self._stale:
            self.refresh_list()

    def _object_of(self, item):
        """The brush or Thing a tree row stands for, or None."""
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if not data:
            return None
        kind, index = data
        state = self.main_window.state
        try:
            if kind == 'brush':
                return state.brushes[index]
            if kind == 'thing':
                return state.things[index]
        except (IndexError, TypeError):
            return None
        return None

    def _find_item(self, obj):
        """The row standing for *obj*, at any depth (groups nest), or None."""
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if self._object_of(item) is obj:
                return item
            stack.extend(item.child(j) for j in range(item.childCount()))
        return None

    def _structure_key(self, selection_only=False):
        """Everything a rebuild reads except the selection.

        Equal keys mean a rebuild would make the same rows, in the same
        order, with the same text, colours and icons: only which rows are
        selected can differ, and :meth:`_reselect_rows` updates just that.
        Read from the objects' current values (not their identities), so an
        edit, an undo or a replaced list is seen; rows are found by index,
        exactly as a rebuild stores them. Building it costs a few
        milliseconds on thousands of brushes, against tens for a rebuild.
        """
        terrain = None
        if self._has_terrain():
            terrain = (self._get_terrain_display_name(),
                       bool(self.main_window.terrain_locked()))
        return (self.sort_mode, terrain, frozenset(self._search_matches),
                self._objects_key(reuse=selection_only))

    def _world_token(self):
        """What says the scene's objects may have changed since a cached
        answer about them: the world epoch every editor transaction bumps
        (:meth:`EditorState.mark_world_changed`), the I/O revision a rename
        bumps, and the lists themselves (held, so a replaced list is seen and
        its id cannot be reused by another)."""
        state = self.main_window.state
        return (getattr(state, 'world_epoch', None), _io_revision(),
                state.brushes, len(state.brushes), state.things, len(state.things))

    @staticmethod
    def _same_token(a, b):
        return (a is not None and b is not None and a[0] == b[0] and a[1] == b[1]
                and a[2] is b[2] and a[3] == b[3] and a[4] is b[4] and a[5] == b[5])

    def _objects_key(self, reuse=False):
        """The per-object part of :meth:`_structure_key`.

        With *reuse* (a selection change) the last answer is kept while
        :meth:`_world_token` has not moved: a selection change does not move
        it, and reading every object's name and flags was most of the cost of
        one on a big map (40 of 90 ms at 20k brushes). Every editor operation
        that changes what a row shows checkpoints (or journals) first, which
        moves the token. Any other refresh reads the objects afresh, so a
        value written outside a transaction still shows on the next one.
        """
        token = self._world_token()
        cached = self._objects_key_cache
        if reuse and cached is not None and self._same_token(cached[0], token):
            return cached[1]
        state = self.main_window.state
        group_key = brush_groups.GROUP_KEY
        brushes = tuple(
            (b.get('name'), b.get('is_trigger', False), b.get('hidden', False),
             b.get('lock', False), 'color' in b, b.get('color'), b.get(group_key))
            for b in state.brushes)
        things = []
        for t in state.things:
            p = t.properties
            things.append((type(t), t.name, p.get('hidden', False), p.get('lock', False),
                           'color' in p, p.get('color'), p.get(group_key)))
        value = (brushes, tuple(things))
        self._objects_key_cache = (token, value)
        return value

    def _rows_selected_by_identity(self):
        """The rows the selection selects, found through an object -> row
        map, or None when only the full scan can say.

        The full scan asks :class:`SelectionLookup` about every row, which
        also matches by *equality* (a copied brush dict, an object with its
        own ``__eq__``). The map answers by identity, which is the same
        answer only when no row could be matched any other way: every
        selected object is a row's own object, a plain dict whose id no other
        row shares or an object compared by identity, and no row is of a type
        that compares by value. Anything else falls back.
        """
        rows = self._rows
        token = self._world_token()
        index = self._row_index
        if index is None or index[1] is not rows or not self._same_token(index[0], token):
            index = (token, rows) + self._build_row_index(rows)
            self._row_index = index
        by_obj, duplicate_ids, exact = index[2], index[3], index[4]
        if not exact:
            return None
        wanted = []
        for obj in self.main_window.state.selected_objects:
            entry = by_obj.get(id(obj))
            if entry is None or entry[1] is not obj:
                return None
            if type(obj) is dict:
                try:
                    if obj.get('id') in duplicate_ids:
                        return None
                except TypeError:
                    return None
            elif type(obj).__eq__ is not object.__eq__:
                return None
            wanted.append(entry[0])
        return wanted

    def _build_row_index(self, rows):
        """``(by_obj, duplicate brush ids, exact)`` for the rows *rows*."""
        state = self.main_window.state
        lists = {'brush': state.brushes, 'thing': state.things}
        by_obj, seen, duplicates, exact = {}, set(), set(), True
        for item, kind, index in rows[1]:
            obj = lists[kind][index]
            by_obj[id(obj)] = (item, obj)
            if kind == 'brush':
                if type(obj) is not dict:
                    exact = False           # a dict subclass compares by value
                    continue
                try:
                    brush_id = obj.get('id')
                    if brush_id in seen:
                        duplicates.add(brush_id)
                    seen.add(brush_id)
                except TypeError:
                    exact = False
            elif type(obj).__eq__ is not object.__eq__:
                exact = False
        return by_obj, duplicates, exact

    def _reselect_rows(self, key):
        """Bring the existing rows' selection up to date without rebuilding,
        when nothing else a rebuild reads has changed (*key*). Returns False,
        touching nothing, when a rebuild is needed."""
        rows = getattr(self, '_rows', None)
        if rows is None or key != self._rows_key:
            return False
        headers, items, terrain_item = rows
        try:
            if (self.tree.topLevelItemCount() != len(headers)
                    or any(self.tree.indexOfTopLevelItem(h) != i
                           for i, h in enumerate(headers))):
                return False
        except RuntimeError:          # the rows were deleted (tree.clear())
            return False
        # As a rebuild does: a temporary highlight ends with the refresh.
        self._clear_highlight()
        self._highlight_timer.stop()
        state = self.main_window.state
        wanted = self._rows_selected_by_identity()
        if wanted is not None:
            # Only the rows whose selection changed are touched: what Qt has
            # selected now against what the selection wants.
            # (Keyed by id: a QTreeWidgetItem is not hashable. Qt hands back
            # the same wrappers, which self._rows keeps alive.)
            wanted = {id(item): item for item in wanted}
            for item in self.tree.selectedItems():
                if item is not terrain_item and id(item) not in wanted:
                    item.setSelected(False)
            for item in wanted.values():
                if not item.isSelected():
                    item.setSelected(True)
        else:
            selected = SelectionLookup(state.selected_objects)
            lists = {'brush': state.brushes, 'thing': state.things}
            for item, kind, index in items:
                want = lists[kind][index] in selected
                if item.isSelected() != want:
                    item.setSelected(want)
        if terrain_item is not None:
            want = bool(getattr(self.main_window, 'terrain_selected', False))
            if terrain_item.isSelected() != want:
                terrain_item.setSelected(want)
        return True

    def _rebuild_items(self, key=None):
        # Invalid until this rebuild completes: a failed one must not leave a
        # key that lets the next refresh skip rebuilding a half-built tree.
        self._rows = None
        self._rows_key = None
        self._row_index = None
        self.tree.clear()
        headers, row_items, terrain_item = [], [], None
        
        # Clear any pending highlight since items are being rebuilt
        self._highlighted_item = None
        self._highlight_backgrounds = None
        self._highlight_timer.stop()
        
        # Get selected objects list for multi-selection support
        selected_objects = SelectionLookup(self.main_window.state.selected_objects)
        
        # Define font for headers
        header_font = QFont()
        header_font.setBold(True)
        
        # Section titles: white on black.
        header_brush = QBrush(QColor(HEADER_BACKGROUND))

        # =====================================================================
        # TERRAIN SECTION
        # =====================================================================
        if self._has_terrain():
            terrain_header = QTreeWidgetItem(self.tree, ["Terrain", ""])
            terrain_header.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
            terrain_header.setFlags(terrain_header.flags() & ~Qt.ItemIsSelectable)
            terrain_header.setForeground(0, QBrush(QColor("white")))
            terrain_header.setBackground(0, header_brush)
            terrain_header.setBackground(1, header_brush)
            terrain_header.setFont(0, header_font)
            self._expand_section(terrain_header)
            headers.append(terrain_header)

            terrain_name = self._get_terrain_display_name()
            terrain_item = QTreeWidgetItem(terrain_header, [terrain_name, ""])
            terrain_item.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
            terrain_item.setData(0, Qt.UserRole, ('terrain', 0))
            terrain_item.setForeground(0, QBrush(QColor("#8FBC8F")))  # Earthy green
            if self.main_window.terrain_locked():
                terrain_item.setIcon(0, self.lock_icon)
            if getattr(self.main_window, 'terrain_selected', False):
                terrain_item.setSelected(True)

        # =====================================================================
        # BRUSHES SECTION
        # =====================================================================

        # Add Brushes Header with full-width background
        brushes_header = QTreeWidgetItem(self.tree, ["Brushes", ""])
        brushes_header.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
        brushes_header.setFlags(brushes_header.flags() & ~Qt.ItemIsSelectable)
        brushes_header.setForeground(0, QBrush(QColor("white")))
        brushes_header.setBackground(0, header_brush)
        brushes_header.setBackground(1, header_brush)
        brushes_header.setFont(0, header_font)
        self._expand_section(brushes_header)
        headers.append(brushes_header)
        
        # Add Brushes (sorted; a group's members adjacent, tinted and boxed)
        sorted_brushes = self._get_sorted_brushes()
        # One colour per group across both sections: a group holding brushes
        # and entities is boxed in the same colour under each header.
        group_colours = brush_groups.colours(
            brush_groups.scene_objects(self.main_window.state))
        for row, (i, brush_dict) in enumerate(sorted_brushes):
            item_text = self._get_brush_display_name(brush_dict, i)
            item = QTreeWidgetItem(brushes_header, [item_text, ""])
            item.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
            item.setData(0, Qt.UserRole, ('brush', i))
            row_items.append((item, 'brush', i))
            self._mark_group_row(item, sorted_brushes, row, group_colours)
            
            if brush_dict.get('hidden', False):
                item.setForeground(0, QBrush(QColor("#FFAC1C")))

            # Get colour icon if present
            colour_icon = None
            color = brush_dict.get('color')
            if isinstance(color, str) and color in self.colour_icons:
                colour_icon = self.colour_icons[color]
            
            # Get status icon (hidden takes precedence over lock)
            status_icon = None
            if brush_dict.get('hidden', False):
                status_icon = self.hidden_icon
            elif brush_dict.get('lock', False):
                status_icon = self.lock_icon
            
            # Indicators go before the name (column 0's icon)
            if status_icon and colour_icon:
                item.setIcon(0, self._create_composite_icon(status_icon, colour_icon, padlock_on_left=True))
            elif status_icon:
                item.setIcon(0, status_icon)
            elif colour_icon:
                item.setIcon(0, colour_icon)
            
            # Check if this brush is in the selected_objects list
            if brush_dict in selected_objects:
                item.setSelected(True)

        # =====================================================================
        # THINGS SECTION
        # =====================================================================

        # Add Things Header with full-width background
        things_header = QTreeWidgetItem(self.tree, ["Things", ""])
        things_header.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
        things_header.setFlags(things_header.flags() & ~Qt.ItemIsSelectable)
        things_header.setForeground(0, QBrush(QColor("white")))
        things_header.setBackground(0, header_brush)
        things_header.setBackground(1, header_brush)
        things_header.setFont(0, header_font)
        self._expand_section(things_header)
        headers.append(things_header)
        
        # Add Things (sorted; a group's members adjacent, tinted and boxed)
        sorted_things = self._get_sorted_things()
        for row, (i, thing_obj) in enumerate(sorted_things):
            item_text = self._get_thing_display_name(thing_obj, i)
            item = QTreeWidgetItem(things_header, [item_text, ""])
            item.setTextAlignment(0, Qt.AlignLeft | Qt.AlignVCenter)
            item.setData(0, Qt.UserRole, ('thing', i))
            row_items.append((item, 'thing', i))
            self._mark_group_row(item, sorted_things, row, group_colours)
            
            # Check if hidden
            if thing_obj.properties.get('hidden', False):
                item.setForeground(0, QBrush(QColor("#FFAC1C")))
            
            # Get colour icon if present
            colour_icon = None
            color = thing_obj.properties.get('color')
            if isinstance(color, str) and color in self.colour_icons:
                colour_icon = self.colour_icons[color]
            
            # Get status icon (hidden takes precedence over lock)
            status_icon = None
            if thing_obj.properties.get('hidden', False):
                status_icon = self.hidden_icon
            elif thing_obj.properties.get('lock', False):
                status_icon = self.lock_icon
            
            # Indicators go before the name (column 0's icon)
            if status_icon and colour_icon:
                item.setIcon(0, self._create_composite_icon(status_icon, colour_icon, padlock_on_left=True))
            elif status_icon:
                item.setIcon(0, status_icon)
            elif colour_icon:
                item.setIcon(0, colour_icon)

            if thing_obj in selected_objects:
                item.setSelected(True)

        # The tree is rebuilt on nearly every edit, so the search highlight has
        # to be reapplied or it vanishes the moment anything else happens.
        if self._search_matches:
            self._apply_search_styling()

        self._rows = (headers, row_items, terrain_item)
        self._rows_key = key

    def highlight_item(self, obj):
        """Highlight an object in the hierarchy without selecting it.
        Used for locked objects when locked_not_selectable_2d is enabled."""
        # Find the item corresponding to this object
        target_item = None
        
        # Search through all items
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                data = item.data(0, Qt.UserRole)
                if data:
                    obj_type, obj_index = data
                    if obj_type == 'brush' and obj_index < len(self.main_window.state.brushes):
                        if self.main_window.state.brushes[obj_index] is obj:
                            target_item = item
                            break
                    elif obj_type == 'thing' and obj_index < len(self.main_window.state.things):
                        if self.main_window.state.things[obj_index] is obj:
                            target_item = item
                            break
            if target_item:
                break
        
        if target_item:
            # Clear previous highlight
            if self._highlighted_item:
                self._clear_highlight()
            
            # Scroll to and highlight the item
            self.tree.scrollToItem(target_item)
            self._highlighted_item = target_item
            self._highlight_backgrounds = (target_item.background(0),
                                          target_item.background(1))
            
            # Set highlight background (cyan/teal color)
            highlight_brush = QBrush(QColor("#00CED1"))
            target_item.setBackground(0, highlight_brush)
            target_item.setBackground(1, highlight_brush)
            
            # Clear highlight after 1.5 seconds
            self._highlight_timer.start(1500)

    def _clear_highlight(self):
        """Clear the temporary highlight from an item."""
        if self._highlighted_item:
            try:
                # Check if item still exists by accessing a property
                # This will raise RuntimeError if the C++ object was deleted
                _ = self._highlighted_item.text(0)
                # Keep search styling and any distinct column backgrounds.
                for column, background in enumerate(self._highlight_backgrounds):
                    self._highlighted_item.setBackground(column, background)
            except RuntimeError:
                # Item was deleted (tree was refreshed), just ignore
                pass
            self._highlighted_item = None
            self._highlight_backgrounds = None

    def scroll_to_item(self, obj):
        """Scroll to an object in the hierarchy."""
        self.highlight_item(obj)

    def _create_composite_icon(self, padlock_icon, colour_icon, size=20, padlock_on_left=True):
        spacing = 4
        total_width = 44 
        total_height = size
        
        padlock_pixmap = padlock_icon.pixmap(size, size)
        colour_pixmap = colour_icon.pixmap(size, size)
        
        result_pixmap = QPixmap(total_width, total_height)
        result_pixmap.fill(Qt.transparent)
        
        painter = QPainter(result_pixmap)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        
        if padlock_on_left:
            painter.drawPixmap(0, 0, padlock_pixmap)
            painter.drawPixmap(size + spacing, 0, colour_pixmap)
        else:
            painter.drawPixmap(0, 0, colour_pixmap)
            painter.drawPixmap(size + spacing, 0, padlock_pixmap)
        painter.end()
        
        return QIcon(result_pixmap)

    def open_menu(self, position):
        # Ensure the item under the cursor is selected (right-click doesn't
        # always do this automatically in all selection modes).
        # Block signals so this doesn't trigger handle_selection_change,
        # which would rebuild the tree and destroy the item mid-menu.
        item_at_pos = self.tree.itemAt(position)
        if item_at_pos and not item_at_pos.isSelected():
            self.tree.blockSignals(True)
            self.tree.clearSelection()
            item_at_pos.setSelected(True)
            self.tree.blockSignals(False)

        menu = QMenu(self)
        selected_items = self.tree.selectedItems()

        if not selected_items:
            return

        # Check if we have multiple brushes or things selected
        brush_items = []
        thing_items = []
        terrain_items = []
        for item in selected_items:
            data = item.data(0, Qt.UserRole)
            if data:
                if data[0] == 'brush':
                    brush_items.append((item, data[1]))
                elif data[0] == 'thing':
                    thing_items.append((item, data[1]))
                elif data[0] == 'terrain':
                    terrain_items.append((item, data[1]))

        # -----------------------------------------------------------------
        # Terrain context menu
        # -----------------------------------------------------------------
        if terrain_items and not brush_items and not thing_items:
            locked = self.main_window.terrain_locked()
            edit_action = menu.addAction("Edit Terrain...")
            edit_action.setEnabled(not locked)
            lock_action = menu.addAction("Unlock" if locked else "Lock")
            menu.addSeparator()
            delete_action = menu.addAction("Delete Terrain")

            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action == edit_action:
                self.main_window.open_terrain_editor()
            elif action == lock_action:
                self.main_window.set_terrain_locked(not locked)
            elif action == delete_action:
                self.main_window.save_state()
                self.main_window.state.terrain_data = None
                # Remove the live terrain object so the 3D view stops rendering it
                self.main_window.terrain = None
                if self.main_window.terrain_editor_window is not None:
                    self.main_window.close_terrain_editor()
                elif self.main_window._current_overlay is not None:
                    self.main_window._close_current_overlay()
                self.main_window.set_selected_objects([])
                self.main_window.update_all_ui()
            return

        # -----------------------------------------------------------------
        # Focus On — every brush/thing menu, never terrain
        # -----------------------------------------------------------------
        focus_targets = []
        state = self.main_window.state
        for _item, index in brush_items:
            try:
                focus_targets.append(state.brushes[index])
            except IndexError:
                pass
        for _item, index in thing_items:
            try:
                focus_targets.append(state.things[index])
            except IndexError:
                pass

        if focus_targets:
            label = ("Focus on" if len(focus_targets) == 1
                     else "Focus on %d objects" % len(focus_targets))
            focus_action = menu.addAction(label)
            focus_action.setToolTip("Centre the 2D and 3D views on this object")
            focus_action.triggered.connect(
                lambda _checked=False, targets=list(focus_targets):
                self.focus_on(targets))
            self._add_select_group_actions(menu, focus_targets)
            self._add_visgroup_menu(menu, focus_targets)
            self._add_group_action(menu, focus_targets)
            menu.addSeparator()

        # If multiple items of the same type selected, show bulk operations
        if len(brush_items) > 1 and len(thing_items) == 0:
            # Bulk operations for multiple brushes
            lock_action = menu.addAction("Lock All")
            unlock_action = menu.addAction("Unlock All")
            menu.addSeparator()
            hide_action = menu.addAction("Hide All")
            show_action = menu.addAction("Show All")
            
            action = menu.exec_(self.tree.viewport().mapToGlobal(position))
            
            if action == lock_action:
                self.main_window.save_state()
                for _, idx in brush_items:
                    self.main_window.state.brushes[idx]['lock'] = True
                self.main_window.update_all_ui()
            elif action == unlock_action:
                self.main_window.save_state()
                for _, idx in brush_items:
                    self.main_window.state.brushes[idx]['lock'] = False
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                for _, idx in brush_items:
                    self.main_window.state.brushes[idx]['hidden'] = True
                    touch(self.main_window.state.brushes[idx])
                self.main_window.update_all_ui()
            elif action == show_action:
                self.main_window.save_state()
                for _, idx in brush_items:
                    self.main_window.state.brushes[idx]['hidden'] = False
                    touch(self.main_window.state.brushes[idx])
                self.main_window.update_all_ui()
            return
        
        if len(thing_items) > 1 and len(brush_items) == 0:
            # Bulk operations for multiple things
            lock_action = menu.addAction("Lock All")
            unlock_action = menu.addAction("Unlock All")
            menu.addSeparator()
            hide_action = menu.addAction("Hide All")
            show_action = menu.addAction("Show All")
            
            action = menu.exec_(self.tree.viewport().mapToGlobal(position))
            
            if action == lock_action:
                self.main_window.save_state()
                for _, idx in thing_items:
                    self.main_window.state.things[idx].properties['lock'] = True
                self.main_window.update_all_ui()
            elif action == unlock_action:
                self.main_window.save_state()
                for _, idx in thing_items:
                    self.main_window.state.things[idx].properties['lock'] = False
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                for _, idx in thing_items:
                    set_live_flag(self.main_window.state.things[idx], 'hidden', True)
                self.main_window.update_all_ui()
            elif action == show_action:
                self.main_window.save_state()
                for _, idx in thing_items:
                    set_live_flag(self.main_window.state.things[idx], 'hidden', False)
                self.main_window.update_all_ui()
            return

        # Single item selected - original behavior
        item = selected_items[0]
        data = item.data(0, Qt.UserRole)
        
        if data and data[0] == 'brush':
            brush_dict = self.main_window.state.brushes[data[1]]
            
            # Lock/Unlock Action
            is_locked = brush_dict.get('lock', False)
            lock_action_text = "Unlock" if is_locked else "Lock"
            lock_action = menu.addAction(lock_action_text)
            
            # Hide/Show Action
            is_hidden = brush_dict.get('hidden', False)
            hide_action_text = "Show" if is_hidden else "Hide"
            hide_action = menu.addAction(hide_action_text)
            
            menu.addSeparator()

            # Tag Submenu - use British spelling
            colour_menu = menu.addMenu("Tag")

            # Add 'None' option
            none_action = colour_menu.addAction("None")
            none_action.setCheckable(True)
            if 'color' not in brush_dict:
                none_action.setChecked(True)
            none_action.triggered.connect(lambda checked: self.set_brush_colour(brush_dict, None, checked))
            
            colour_menu.addSeparator()

            # Add colour options in rainbow order with white last
            rainbow_order = ['red', 'orange', 'yellow', 'green', 'blue', 'pink', 'white']
            for colour_name in rainbow_order:
                icon = self.colour_icons[colour_name]
                action = colour_menu.addAction(icon, colour_name.capitalize())
                action.setCheckable(True)
                if brush_dict.get('color') == colour_name:
                    action.setChecked(True)
                action.triggered.connect(lambda checked, c=colour_name: self.set_brush_colour(brush_dict, c, checked))

            menu.addSeparator()
            properties_action = menu.addAction("Properties")
            properties_action.setToolTip("Show this object in the Properties panel")

            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action == lock_action:
                self.main_window.save_state()
                brush_dict['lock'] = not is_locked
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                brush_dict['hidden'] = not is_hidden
                touch(brush_dict)
                self.main_window.update_all_ui()
            elif action == properties_action:
                self.show_properties_for(brush_dict)
        
        elif data and data[0] == 'thing':
            thing_obj = self.main_window.state.things[data[1]]
            
            # Lock/Unlock Action
            is_locked = thing_obj.properties.get('lock', False)
            lock_action_text = "Unlock" if is_locked else "Lock"
            lock_action = menu.addAction(lock_action_text)
            
            # Hide/Show Action
            is_hidden = thing_obj.properties.get('hidden', False)
            hide_action_text = "Show" if is_hidden else "Hide"
            hide_action = menu.addAction(hide_action_text)
            
            menu.addSeparator()

            # Tag Submenu - use British spelling
            colour_menu = menu.addMenu("Tag")

            # Add 'None' option
            none_action = colour_menu.addAction("None")
            none_action.setCheckable(True)
            if 'color' not in thing_obj.properties:
                none_action.setChecked(True)
            none_action.triggered.connect(lambda checked: self.set_thing_colour(thing_obj, None, checked))
            
            colour_menu.addSeparator()

            # Add colour options in rainbow order with white last
            rainbow_order = ['red', 'orange', 'yellow', 'green', 'blue', 'pink', 'white']
            for colour_name in rainbow_order:
                icon = self.colour_icons[colour_name]
                action = colour_menu.addAction(icon, colour_name.capitalize())
                action.setCheckable(True)
                if thing_obj.properties.get('color') == colour_name:
                    action.setChecked(True)
                action.triggered.connect(lambda checked, c=colour_name: self.set_thing_colour(thing_obj, c, checked))

            menu.addSeparator()
            inspect_action = menu.addAction("Inspect")
            inspect_action.setToolTip("Open a live, read-only view of this entity")
            properties_action = menu.addAction("Properties")
            properties_action.setToolTip("Show this object in the Properties panel")

            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action is not None and action == inspect_action:
                self.main_window.show_entity_inspector(thing_obj)
            elif action == lock_action:
                self.main_window.save_state()
                thing_obj.properties['lock'] = not is_locked
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                set_live_flag(thing_obj, 'hidden', not is_hidden)
                self.main_window.update_all_ui()
            elif action == properties_action:
                self.show_properties_for(thing_obj)

    def show_properties_for(self, obj):
        """Select *obj* and bring the Properties panel to the front.

        The selection is made explicitly rather than relied upon: ``open_menu``
        selects the item under the cursor with signals blocked, so
        ``handle_selection_change`` never ran and the panel could still be
        showing whatever was selected before the right-click.
        """
        if obj is None:
            return
        self.main_window.set_selected_objects([obj])
        self.main_window.show_properties_panel()

    def handle_selection_change(self):
        # The tree made this selection: its rebuild must not scroll.
        self._selecting_in_tree = True
        try:
            self._apply_tree_selection()
        finally:
            self._selecting_in_tree = False

    def _apply_tree_selection(self):
        selected_items = self.tree.selectedItems()
        if not selected_items:
            self.main_window.set_selected_objects([])
            return

        selected_objects = []
        terrain_selected = False
        for item in selected_items:
            data = item.data(0, Qt.UserRole)
            if data:
                obj_type, obj_index = data
                if obj_type == 'brush':
                    selected_objects.append(self.main_window.state.brushes[obj_index])
                elif obj_type == 'thing':
                    selected_objects.append(self.main_window.state.things[obj_index])
                elif obj_type == 'terrain':
                    terrain_selected = True
        
        if terrain_selected and not selected_objects:
            # The terrain's row: selected as such, locked or not (the row is
            # how a locked terrain is reached to unlock it).
            self.main_window.select_terrain()
            return

        self.main_window.set_selected_objects(selected_objects)

    def focus_on(self, targets):
        """Centre the 2D and 3D views on one object, or on a group of them.

        For several objects the views centre on the middle of the whole set and
        pull back far enough to hold it, which is what "focus on these" means —
        framing only the first would hide the rest.
        """
        targets = [t for t in targets if t is not None]
        if not targets:
            return
        if len(targets) == 1:
            self.main_window.focus_on_object(targets[0])
            return

        centres, radius = [], 0.0
        for obj in targets:
            centre, r = self.main_window._object_focus_target(obj)
            centres.append(centre)
            radius = max(radius, r)
        mid = [sum(c[axis] for c in centres) / len(centres) for axis in range(3)]
        spread = max(
            (max(c[axis] for c in centres) - min(c[axis] for c in centres))
            for axis in range(3))
        self.main_window.focus_on_bounds(mid, max(radius, spread * 0.5))

    def _expand_section(self, header):
        """Open a section title unless the user closed it (kept across the
        rebuild every selection change makes)."""
        header.setExpanded(header.text(0) not in self._collapsed_sections)

    def toggle_section(self, header):
        """Open or close a section title (a click on it: the tree has no
        branch arrows to click)."""
        expanded = not header.isExpanded()
        header.setExpanded(expanded)
        if expanded:
            self._collapsed_sections.discard(header.text(0))
        else:
            self._collapsed_sections.add(header.text(0))

    def eventFilter(self, obj, event):
        if (event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick)
                and event.button() == Qt.LeftButton
                and obj is self.tree.viewport()):
            item = self.tree.itemAt(event.pos())
            if item is not None and item.parent() is None and item.childCount():
                # A double-click arrives as a press and then a double-click:
                # toggling on both closed and reopened the section. The
                # press toggles; the double-click is swallowed.
                if event.type() == QEvent.MouseButtonPress:
                    self.toggle_section(item)
                return True
        if (event.type() == QEvent.MouseButtonDblClick
                and event.button() == Qt.LeftButton
                and obj is self.tree.viewport()):
            item = self.tree.itemAt(event.pos())
            if item is not None and self.handle_double_click(item, 0):
                return True
        return super().eventFilter(obj, event)

    def handle_double_click(self, item, column):
        """Double-clicking the terrain item opens the terrain editor.

        Same as its context menu's Edit Terrain. Returns True when handled.
        """
        data = item.data(0, Qt.UserRole)
        if data and data[0] == 'terrain':
            self.main_window.open_terrain_editor()
            return True
        return False

    def set_brush_colour(self, brush_dict, colour_name, checked):
        """Set brush colour (method name uses British spelling, but internal dict key remains 'color')"""
        self.main_window.save_state()
        if checked:
            if colour_name is None:
                if 'color' in brush_dict:
                    del brush_dict['color']
            else:
                brush_dict['color'] = colour_name
        else:
            if 'color' in brush_dict and brush_dict['color'] == colour_name:
                del brush_dict['color']
            elif colour_name is None and 'color' not in brush_dict:
                pass
        self.main_window.update_all_ui()
    
    def set_thing_colour(self, thing_obj, colour_name, checked):
        """Set thing colour tag"""
        self.main_window.save_state()
        if checked:
            if colour_name is None:
                if 'color' in thing_obj.properties:
                    del thing_obj.properties['color']
            else:
                thing_obj.properties['color'] = colour_name
        else:
            if 'color' in thing_obj.properties and thing_obj.properties['color'] == colour_name:
                del thing_obj.properties['color']
            elif colour_name is None and 'color' not in thing_obj.properties:
                pass
        self.main_window.update_all_ui()
    
    # Delegate common QTreeWidget methods to the internal tree for compatibility
    def selectedItems(self):
        return self.tree.selectedItems()
    
    def clear(self):
        self._rows = None
        return self.tree.clear()
    
    def topLevelItemCount(self):
        return self.tree.topLevelItemCount()
    
    def topLevelItem(self, index):
        return self.tree.topLevelItem(index)
