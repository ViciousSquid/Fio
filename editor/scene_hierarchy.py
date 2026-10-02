from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, 
                             QMenu, QAction, QHeaderView, QAbstractItemView, 
                             QPushButton, QHBoxLayout, QLabel, QFrame, QGridLayout)
from PyQt5.QtGui import QIcon, QColor, QBrush, QFont, QPainter, QPixmap
from PyQt5 import QtCore
import os
from PyQt5.QtCore import Qt, QTimer

from editor.things import Light, Model, Monster

try:
    from editor.io_system import get_connections
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
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_highlight)
        
        # Create layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
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
                text-align: center;
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
        content_layout.addWidget(self.tree, stretch=2)
        
        # Configure tree widget
        self.tree.header().setVisible(False) 
        self.tree.setColumnCount(2)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tree.setColumnWidth(1, 60)
        self.tree.setIconSize(QtCore.QSize(44, 20))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_menu)
        self.lock_icon = QIcon("assets/lock.png")
        self.hidden_icon = QIcon("assets/hidden.png")
        self.tree.itemSelectionChanged.connect(self.handle_selection_change)
        self.tree.itemDoubleClicked.connect(self.handle_double_click)

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
            'red': QIcon("assets/circ_red.png"),
            'orange': QIcon("assets/circ_orange.png"),
            'yellow': QIcon("assets/circ_yellow.png"),
            'green': QIcon("assets/circ_green.png"),
            'blue': QIcon("assets/circ_blue.png"),
            'pink': QIcon("assets/circ_pink.png"),
            'white': QIcon("assets/circ_white.png"),
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

        # Create overview overlay (bottom third)
        self.overview_overlay = QFrame()
        self.overview_overlay.setFrameShape(QFrame.StyledPanel)
        self.overview_overlay.setStyleSheet("""
            QFrame {
                background-color: #2b2b2b;
                color: #f0f0f0;
                border: 1px solid #555;
                border-bottom: none;
            }
            QLabel {
                color: #f0f0f0;
                background-color: transparent;
                border: none;
            }
        """)
        content_layout.addWidget(self.overview_overlay, stretch=1)
        self.overview_overlay.hide()

        # Build overlay layout
        overlay_layout = QVBoxLayout(self.overview_overlay)
        overlay_layout.setContentsMargins(12, 12, 12, 12)
        overlay_layout.setSpacing(8)

        # Header row with arrow indicator
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        
        self.arrow_label = QLabel("▲")
        self.arrow_label.setStyleSheet("color: #FF8C00; font-size: 14px;")
        header_row.addWidget(self.arrow_label)
        
        header_label = QLabel("Overview")
        header_font = QFont()
        header_font.setBold(True)
        header_font.setPointSize(14)
        header_label.setFont(header_font)
        header_label.setStyleSheet("color: #FF8C00;")
        header_row.addWidget(header_label)
        header_row.addStretch()
        
        overlay_layout.addLayout(header_row)

        # Map name label (populated by _update_overview_metrics)
        self.map_name_label = QLabel("Untitled")
        map_name_font = QFont()
        map_name_font.setBold(True)
        map_name_font.setPointSize(10)
        self.map_name_label.setFont(map_name_font)
        self.map_name_label.setStyleSheet("color: #585F2A;")
        overlay_layout.addWidget(self.map_name_label)

        metrics_grid = QGridLayout()
        metrics_grid.setSpacing(8)
        self.overview_labels = {}

        metrics = [
            ("Lights", "lights"),
            ("Brushes", "brushes"),
            ("Things", "things"),
            ("Models", "models"),
            ("Movers", "movers"),
            ("Monsters", "monsters"),
            ("Triggers", "triggers"),
            ("Links", "connections"),
        ]

        for i, (name, key) in enumerate(metrics):
            name_label = QLabel(f"{name}:")
            name_label.setStyleSheet("color: #aaaaaa;")
            value_label = QLabel("0")
            value_label.setStyleSheet("color: #ffffff; font-weight: bold;")
            metrics_grid.addWidget(name_label, i, 0)
            metrics_grid.addWidget(value_label, i, 1)
            self.overview_labels[key] = value_label

        overlay_layout.addLayout(metrics_grid)
        overlay_layout.addStretch()

        layout.addWidget(self.content_container)

        # Create metrics banner at bottom (taller)
        self.metrics_banner = QPushButton("Overview ▲")
        self.metrics_banner.setFixedHeight(38)
        self.metrics_banner.setStyleSheet("""
            QPushButton {
                background-color: #000000;
                color: white;
                border: none;
                border-top: 1px solid #333;
                padding: 4px 8px;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #1a1a1a;
            }
            QPushButton:pressed {
                background-color: #333333;
            }
        """)
        self.metrics_banner.clicked.connect(self._toggle_overview)
        layout.addWidget(self.metrics_banner)

    def _toggle_overview(self):
        """Toggle the overview overlay on/off."""
        if self.overview_overlay.isVisible():
            self.overview_overlay.hide()
            self.metrics_banner.setText("Overview ▲")
        else:
            self._update_overview_metrics()
            self.overview_overlay.show()
            self.metrics_banner.setText("Overview ▼")

    def _update_overview_metrics(self):
        """Calculate and display scene metrics in the overview overlay."""
        # Update map name display
        if hasattr(self.main_window, 'file_path') and self.main_window.file_path:
            map_name = os.path.basename(self.main_window.file_path)
            self.map_name_label.setText(f"{map_name}")
            self.map_name_label.setStyleSheet("color: #A7B454;")
        else:
            self.map_name_label.setText("Untitled")
            self.map_name_label.setStyleSheet("color: #A7B454;")

        state = self.main_window.state
        
        lights = sum(1 for t in state.things if isinstance(t, Light))
        brushes = len(state.brushes)
        things = len(state.things)
        models = sum(1 for t in state.things if isinstance(t, Model))
        movers = sum(1 for b in state.brushes if b.get('is_mover', False))
        monsters = sum(1 for t in state.things if isinstance(t, Monster))
        triggers = sum(1 for b in state.brushes if b.get('is_trigger', False))
        
        connections = 0
        if IO_AVAILABLE:
            for brush in state.brushes:
                connections += len(get_connections(brush))
            for thing in state.things:
                connections += len(get_connections(thing))
        
        self.overview_labels['lights'].setText(str(lights))
        self.overview_labels['brushes'].setText(str(brushes))
        self.overview_labels['things'].setText(str(things))
        self.overview_labels['models'].setText(str(models))
        self.overview_labels['movers'].setText(str(movers))
        self.overview_labels['monsters'].setText(str(monsters))
        self.overview_labels['triggers'].setText(str(triggers))
        self.overview_labels['connections'].setText(str(connections))

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

    def _get_sorted_brushes(self):
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
        terrain_data = getattr(self.main_window.state, 'terrain_data', None)
        if terrain_data:
            biome = terrain_data.get('biome', '')
            if biome:
                return f'Terrain ({biome})'
        return 'Terrain'

    def _has_terrain(self):
        """Check if terrain data exists in the current scene."""
        terrain_data = getattr(self.main_window.state, 'terrain_data', None)
        return terrain_data is not None

    def refresh_list(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        
        # Clear any pending highlight since items are being rebuilt
        self._highlighted_item = None
        self._highlight_timer.stop()
        
        # Get selected objects list for multi-selection support
        selected_objects = getattr(self.main_window.state, 'selected_objects', [])
        
        # Define font for headers
        header_font = QFont()
        header_font.setBold(True)
        
        # Header background colour
        header_brush = QBrush(QColor("#425F5D"))

        # =====================================================================
        # TERRAIN SECTION
        # =====================================================================
        if self._has_terrain():
            terrain_header = QTreeWidgetItem(self.tree, ["Terrain", ""])
            terrain_header.setFlags(terrain_header.flags() & ~Qt.ItemIsSelectable)
            terrain_header.setForeground(0, QBrush(QColor("white")))
            terrain_header.setBackground(0, header_brush)
            terrain_header.setBackground(1, header_brush)
            terrain_header.setFont(0, header_font)
            terrain_header.setExpanded(True)

            terrain_name = self._get_terrain_display_name()
            terrain_item = QTreeWidgetItem(terrain_header, [terrain_name, ""])
            terrain_item.setData(0, Qt.UserRole, ('terrain', 0))
            terrain_item.setForeground(0, QBrush(QColor("#8FBC8F")))  # Earthy green

            # Show if terrain is selected
            if 'terrain' in [getattr(obj, '_terrain_marker', None) for obj in selected_objects]:
                terrain_item.setSelected(True)

        # =====================================================================
        # BRUSHES SECTION
        # =====================================================================

        # Add Brushes Header with full-width background
        brushes_header = QTreeWidgetItem(self.tree, ["Brushes", ""])
        brushes_header.setFlags(brushes_header.flags() & ~Qt.ItemIsSelectable)
        brushes_header.setForeground(0, QBrush(QColor("white")))
        brushes_header.setBackground(0, header_brush)
        brushes_header.setBackground(1, header_brush)
        brushes_header.setFont(0, header_font)
        brushes_header.setExpanded(True)
        
        # Add Brushes (sorted)
        for i, brush_dict in self._get_sorted_brushes():
            item_text = self._get_brush_display_name(brush_dict, i)
            item = QTreeWidgetItem(brushes_header, [item_text, ""])
            item.setData(0, Qt.UserRole, ('brush', i))
            
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
            
            # Apply icons to column 1 (right side)
            if status_icon and colour_icon:
                item.setIcon(1, self._create_composite_icon(status_icon, colour_icon, padlock_on_left=True))
            elif status_icon:
                item.setIcon(1, status_icon)
            elif colour_icon:
                item.setIcon(1, colour_icon)
            
            # Check if this brush is in the selected_objects list
            if brush_dict in selected_objects:
                item.setSelected(True)

        # =====================================================================
        # THINGS SECTION
        # =====================================================================

        # Add Things Header with full-width background
        things_header = QTreeWidgetItem(self.tree, ["Things", ""])
        things_header.setFlags(things_header.flags() & ~Qt.ItemIsSelectable)
        things_header.setForeground(0, QBrush(QColor("white")))
        things_header.setBackground(0, header_brush)
        things_header.setBackground(1, header_brush)
        things_header.setFont(0, header_font)
        things_header.setExpanded(True)
        
        # Add Things (sorted)
        for i, thing_obj in self._get_sorted_things():
            item_text = thing_obj.name if thing_obj.name else f'Thing {i+1}'
            item = QTreeWidgetItem(things_header, [item_text, ""])
            item.setData(0, Qt.UserRole, ('thing', i))
            
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
            
            # Apply icons to column 1 (right side)
            if status_icon and colour_icon:
                item.setIcon(1, self._create_composite_icon(status_icon, colour_icon, padlock_on_left=True))
            elif status_icon:
                item.setIcon(1, status_icon)
            elif colour_icon:
                item.setIcon(1, colour_icon)

            if thing_obj in selected_objects:
                item.setSelected(True)
                
        self.tree.blockSignals(False)

        # Scroll to the first selected item
        first_selected = self.tree.selectedItems()
        if first_selected:
            self.tree.scrollToItem(first_selected[0], QAbstractItemView.EnsureVisible)

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
            self._original_background = target_item.background(0)
            
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
                # Reset to transparent/default
                self._highlighted_item.setBackground(0, QBrush())
                self._highlighted_item.setBackground(1, QBrush())
            except RuntimeError:
                # Item was deleted (tree was refreshed), just ignore
                pass
            self._highlighted_item = None

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
            edit_action = menu.addAction("Edit Terrain...")
            menu.addSeparator()
            delete_action = menu.addAction("Delete Terrain")

            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action == edit_action:
                self.main_window.open_terrain_editor()
            elif action == delete_action:
                self.main_window.save_state()
                self.main_window.state.terrain_data = None
                # Remove the live terrain object so the 3D view stops rendering it
                if hasattr(self.main_window, 'terrain'):
                    self.main_window.terrain = None
                panel = getattr(self.main_window, 'terrain_editor_window', None)
                if panel is not None:
                    if getattr(self.main_window, '_current_overlay', None) is panel:
                        self.main_window._close_current_overlay()
                    self.main_window.terrain_editor_window = None
                self.main_window.set_selected_objects([])
                self.main_window.update_all_ui()
            return

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
                self.main_window.update_all_ui()
            elif action == show_action:
                self.main_window.save_state()
                for _, idx in brush_items:
                    self.main_window.state.brushes[idx]['hidden'] = False
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
                    self.main_window.state.things[idx].properties['hidden'] = True
                self.main_window.update_all_ui()
            elif action == show_action:
                self.main_window.save_state()
                for _, idx in thing_items:
                    self.main_window.state.things[idx].properties['hidden'] = False
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
            
            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action == lock_action:
                self.main_window.save_state()
                brush_dict['lock'] = not is_locked
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                brush_dict['hidden'] = not is_hidden
                self.main_window.update_all_ui()
        
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
            
            action = menu.exec_(self.tree.viewport().mapToGlobal(position))

            if action == lock_action:
                self.main_window.save_state()
                thing_obj.properties['lock'] = not is_locked
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                thing_obj.properties['hidden'] = not is_hidden
                self.main_window.update_all_ui()

    def handle_selection_change(self):
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
            # Terrain is selected in the hierarchy but it's not a brush/thing —
            # nothing to pass to set_selected_objects.  Just leave it visually
            # highlighted; the right-click menu handles Edit / Delete.
            self.main_window.set_selected_objects([])
            return

        self.main_window.set_selected_objects(selected_objects)

    def handle_double_click(self, item, column):
        """Double-clicking the terrain item opens the terrain editor."""
        data = item.data(0, Qt.UserRole)
        if data and data[0] == 'terrain':
            self.main_window.open_terrain_editor()

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
        return self.tree.clear()
    
    def topLevelItemCount(self):
        return self.tree.topLevelItemCount()
    
    def topLevelItem(self, index):
        return self.tree.topLevelItem(index)