from PyQt5.QtWidgets import QTreeWidget, QTreeWidgetItem, QMenu, QAction, QHeaderView
from PyQt5.QtGui import QIcon, QColor, QBrush, QFont, QPainter, QPixmap
from PyQt5 import QtCore
from PyQt5.QtCore import Qt

class SceneHierarchy(QTreeWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        
        # Remove the header by setting its height to 0 and set column count to 2
        self.header().setVisible(False) 
        self.setColumnCount(2) # Set two columns
        self.header().setStretchLastSection(False) # Don't stretch the last section
        self.header().setSectionResizeMode(0, QHeaderView.Stretch) # Stretch the first column
        self.header().setSectionResizeMode(1, QHeaderView.Fixed) # Fixed size for the second column
        self.setColumnWidth(1, 60)
        self.setIconSize(QtCore.QSize(44, 20))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_menu)
        self.lock_icon = QIcon("assets/lock.png")
        self.hidden_icon = QIcon("assets/hidden.png")
        self.itemSelectionChanged.connect(self.handle_selection_change)

        # Highlight colour
        self.setStyleSheet("""
            QTreeWidget::item:selected {
                background-color: #E4D00A;
                color: black;
            }
            QTreeWidget::item:selected:!active {
                background-color: #B1B97D; /* Keeps colour even when window loses focus */
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

    def refresh_list(self):
        self.blockSignals(True)
        self.clear()
        
        # Define font for headers
        header_font = QFont()
        header_font.setBold(True)
        
        # Header background colour (British spelling in comment)
        header_brush = QBrush(QColor("#425F5D"))

        # Add Brushes Header with full-width background
        brushes_header = QTreeWidgetItem(self, ["Brushes", ""])
        brushes_header.setFlags(brushes_header.flags() & ~Qt.ItemIsSelectable)
        brushes_header.setForeground(0, QBrush(QColor("white")))
        brushes_header.setBackground(0, header_brush)
        brushes_header.setBackground(1, header_brush)
        brushes_header.setFont(0, header_font)
        brushes_header.setExpanded(True)
        
        # Add Brushes
        for i, brush_dict in enumerate(self.main_window.state.brushes):
            item_text = brush_dict.get('name', f'Brush {i+1}')
            item = QTreeWidgetItem(brushes_header, [item_text, ""])
            item.setData(0, Qt.UserRole, ('brush', i))
            
            if brush_dict.get('hidden', False):
                # Set name color to #FFAC1C if hidden
                item.setForeground(0, QBrush(QColor("#FFAC1C")))

            
            # Get colour icon if present (internal data uses 'color', not 'colour')
            colour_icon = None
            if 'color' in brush_dict and brush_dict['color'] in self.colour_icons:
                colour_icon = self.colour_icons[brush_dict['color']]
            
            # Get status icon (hidden takes precedence over lock)
            status_icon = None
            if brush_dict.get('hidden', False):
                status_icon = self.hidden_icon
            elif brush_dict.get('lock', False):
                status_icon = self.lock_icon
            
            # Apply icons to column 1 (right side)
            # IMPORTANT: Show padlock first (left) then colour tag
            if status_icon and colour_icon:
                # Both icons present - padlock appears first (left), then colour tag
                item.setIcon(1, self._create_composite_icon(status_icon, colour_icon, padlock_on_left=True))
            elif status_icon:
                # Only status icon
                item.setIcon(1, status_icon)
            elif colour_icon:
                # Only colour icon
                item.setIcon(1, colour_icon)
            
            if self.main_window.state.selected_object is brush_dict:
                item.setSelected(True)

        # Add Things Header with full-width background
        things_header = QTreeWidgetItem(self, ["Things", ""])
        things_header.setFlags(things_header.flags() & ~Qt.ItemIsSelectable)
        things_header.setForeground(0, QBrush(QColor("white")))
        things_header.setBackground(0, header_brush)
        things_header.setBackground(1, header_brush)
        things_header.setFont(0, header_font)
        things_header.setExpanded(True)
        
        # Add Things
        for i, thing_obj in enumerate(self.main_window.state.things):
            item_text = thing_obj.name if thing_obj.name else f'Thing {i+1}'
            item = QTreeWidgetItem(things_header, [item_text, ""])
            item.setData(0, Qt.UserRole, ('thing', i))

            if self.main_window.state.selected_object is thing_obj:
                item.setSelected(True)
                
        self.blockSignals(False)

    def _create_composite_icon(self, padlock_icon, colour_icon, size=20, padlock_on_left=True):
        spacing = 4
        # Match these dimensions to the setIconSize call in __init__
        total_width = 44 
        total_height = size
        
        padlock_pixmap = padlock_icon.pixmap(size, size)
        colour_pixmap = colour_icon.pixmap(size, size)
        
        result_pixmap = QPixmap(total_width, total_height)
        result_pixmap.fill(Qt.transparent)
        
        painter = QPainter(result_pixmap)
        # Keep things crisp
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
        menu = QMenu()
        selected_items = self.selectedItems()

        if not selected_items:
            return

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
            
            action = menu.exec_(self.viewport().mapToGlobal(position))

            if action == lock_action:
                self.main_window.save_state()
                brush_dict['lock'] = not is_locked
                self.main_window.update_all_ui()
            elif action == hide_action:
                self.main_window.save_state()
                brush_dict['hidden'] = not is_hidden
                self.main_window.update_all_ui()

    def handle_selection_change(self):
        selected_items = self.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        data = item.data(0, Qt.UserRole)
        
        if data:
            obj_type, obj_index = data
            if obj_type == 'brush':
                self.main_window.set_selected_object(self.main_window.state.brushes[obj_index])
            elif obj_type == 'thing':
                self.main_window.set_selected_object(self.main_window.state.things[obj_index])
        else:
            self.main_window.set_selected_object(None)

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