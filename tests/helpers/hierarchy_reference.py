"""Frozen hierarchy refresh before the October 2026 optimization pass.

Used for tree-equivalence checks and interleaved whole-refresh measurements.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import QAbstractItemView, QTreeWidgetItem

def reference_refresh_list(self):
    self.tree.blockSignals(True)
    self.tree.clear()

    # Clear any pending highlight since items are being rebuilt
    self._highlighted_item = None
    self._highlight_timer.stop()

    # Get selected objects list for multi-selection support
    selected_objects = self.main_window.state.selected_objects

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

    # The tree is rebuilt on nearly every edit, so the search highlight has
    # to be reapplied or it vanishes the moment anything else happens.
    if self._search_matches:
        self._apply_search_styling()

    self.tree.blockSignals(False)

    # Scroll to the first selected item
    first_selected = self.tree.selectedItems()
    if first_selected:
        self.tree.scrollToItem(first_selected[0], QAbstractItemView.EnsureVisible)
