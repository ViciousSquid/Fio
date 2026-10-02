import os
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QFormLayout, QCheckBox, QComboBox, QPushButton,
                             QHBoxLayout, QColorDialog, QFileDialog, QGridLayout, 
                             QToolButton, QSlider, QTabWidget, QGroupBox, QScrollArea,
                             QFrame, QDoubleSpinBox, QSizePolicy)
from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QFont
from editor.things import Thing, Light, Pickup, Monster, Model, Speaker, LogicGate

# I/O System imports
try:
    from editor.io_editor_widget import IOEditorWidget, IOInputsWidget
    from editor.io_system import get_entity_type_for_io, IO_REGISTRY
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False


class ClickableLineEdit(QLineEdit):
    clicked_while_empty = pyqtSignal()
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.text().strip():
            self.clicked_while_empty.emit()
        super().mousePressEvent(event)


class PropertyEditor(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.current_object = None
        self._populating = False  # Flag to prevent recursion during population
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(2)
        self.setLayout(self.main_layout)
        
        # Tab widget for organized properties
        self.tab_widget = None
        
        # Store widget references for visibility updates
        self._widgets = {}
        
        self.set_object(None)
    
    def _find_targeting_sources(self, target_name):
        """Find all entities that target the given entity name (legacy + I/O)."""
        if not target_name: return []
        sources = []
        
        # Check legacy 'target' property on brushes
        for brush in self.editor.state.brushes:
            brush_target = brush.get('target', '')
            if brush_target == target_name:
                is_trigger = brush.get('is_trigger', False)
                is_mover = brush.get('is_mover', False)
                if is_trigger or is_mover:
                    source_name = brush.get('name', 'unnamed')
                    source_type = 'trigger' if is_trigger else 'mover'
                    sources.append((source_name, source_type))
            
            # Also check I/O connections
            io_connections = brush.get('_io_connections', [])
            for conn in io_connections:
                if isinstance(conn, dict) and conn.get('target') == target_name:
                    source_name = brush.get('name', 'unnamed')
                    sources.append((source_name, f"I/O: {conn.get('output', '?')}"))
        
        # Check I/O connections on Things
        for thing in self.editor.state.things:
            io_connections = thing.properties.get('_io_connections', [])
            for conn in io_connections:
                if isinstance(conn, dict) and conn.get('target') == target_name:
                    source_name = thing.properties.get('name', 'unnamed')
                    sources.append((source_name, f"I/O: {conn.get('output', '?')}"))
        
        return sources
    
    def _check_target_exists(self, target_name):
        if not target_name: return False
        for brush in self.editor.state.brushes:
            if brush.get('name') == target_name: return True
        for thing in self.editor.state.things:
            thing_name = getattr(thing, 'name', '') or thing.properties.get('name', '')
            if thing_name == target_name: return True
        return False
    

    def clear_layout(self):
        while self.main_layout.count():
            child = self.main_layout.takeAt(0)
            if child.widget(): child.widget().deleteLater()
            elif child.layout():
                layout = child.layout()
                while layout.count():
                    item = layout.takeAt(0)
                    if item.widget(): item.widget().deleteLater()
        self._widgets = {}
        self.tab_widget = None

    def set_object(self, obj):
        # Preserve tab index when refreshing the same object
        saved_tab_index = None
        if self.tab_widget is not None and self.current_object is obj:
            saved_tab_index = self.tab_widget.currentIndex()
        
        self._populating = True  # Set flag to prevent recursion
        self.current_object = obj
        self.clear_layout()
        if obj is None:
            self.main_layout.addWidget(QLabel("Nothing selected."))
            self._populating = False
            return
        if isinstance(obj, dict): 
            self.populate_for_brush(obj)
        elif isinstance(obj, Thing): 
            self.populate_for_thing(obj)
        
        # Restore tab index if we saved one
        if saved_tab_index is not None and self.tab_widget is not None:
            if saved_tab_index < self.tab_widget.count():
                self.tab_widget.setCurrentIndex(saved_tab_index)
        
        self._populating = False  # Reset flag after population complete

    def _create_styled_header(self, text, color="#6C3BAA"):
        """Create a styled header label."""
        label = QLabel(text)
        label.setStyleSheet(f"""
            QLabel {{
                background-color: {color};
                color: white;
                font-weight: bold;
                padding: 8px 12px;
                border-radius: 4px;
                font-size: 12px;
            }}
        """)
        return label

    def _create_section_header(self, text):
        """Create a section header for grouping properties."""
        label = QLabel(text)
        label.setStyleSheet("""
            QLabel {
                color: #F08000;
                font-weight: bold;
                padding: 4px 0px;
                border-bottom: 1px solid #F08000;
                margin-top: 8px;
            }
        """)
        return label

    def _create_color_button(self, color_rgb, size=(100, 28)):
        """Create a color picker button with current color display."""
        btn = QPushButton()
        btn.setFixedSize(*size)
        self._update_color_button(btn, color_rgb)
        return btn

    def _update_color_button(self, btn, color_rgb):
        """Update a color button's background."""
        if isinstance(color_rgb, (list, tuple)) and len(color_rgb) >= 3:
            if any(c > 1.0 for c in color_rgb):
                r, g, b = int(color_rgb[0]), int(color_rgb[1]), int(color_rgb[2])
            else:
                r, g, b = int(color_rgb[0] * 255), int(color_rgb[1] * 255), int(color_rgb[2] * 255)
            btn.setStyleSheet(f"background-color: rgb({r}, {g}, {b}); border: 2px solid #555; border-radius: 4px;")

    def _checkbox_style(self):
        return """
            QCheckBox::indicator:checked { background-color: #F08000; border: 1px solid #333; }
            QCheckBox::indicator:unchecked { background-color: #425f5d; border: 1px solid #333; }
            QCheckBox::indicator { width: 22px; height: 22px; }
        """

    def populate_for_brush(self, brush):
        """Populate property editor for a brush with tabbed interface."""
        
        # Create scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)
        
        # === BASIC PROPERTIES (Always visible) ===
        basic_layout = QFormLayout()
        basic_layout.setSpacing(4)
        
        # Name field with styled header
        name_label = QLabel("Name:")
        name_label.setStyleSheet("QLabel { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px 8px; border-radius: 3px; }")
        name_input = QLineEdit(brush.get('name', ''))
        name_input.setStyleSheet("QLineEdit { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px; border: 2px solid #8B5AC2; border-radius: 3px; } QLineEdit:focus { border: 2px solid #A875D6; background-color: #7B4AB9; }")
        name_input.setPlaceholderText("Enter name...")
        name_input.editingFinished.connect(lambda: self.update_object_prop('name', name_input.text()))
        basic_layout.addRow(name_label, name_input)
        self._widgets['name_input'] = name_input
        
        # Targeted by indicator
        brush_name = brush.get('name', '')
        if brush_name:
            targeting_sources = self._find_targeting_sources(brush_name)
            if targeting_sources:
                source_texts = [f"{name} ({stype})" for name, stype in targeting_sources]
                targeted_label = QLabel(", ".join(source_texts))
                targeted_label.setStyleSheet("QLabel { color: #00FF00; font-weight: bold; padding: 2px; background-color: #1a3d1a; border: 1px solid #00AA00; border-radius: 3px; }")
                targeted_label.setWordWrap(True)
                basic_layout.addRow("Targeted by:", targeted_label)
        
        content_layout.addLayout(basic_layout)
        
        # === TAB WIDGET FOR ADVANCED PROPERTIES ===
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet("""
            QTabBar::tab:selected { background: #F08000; color: white; }
            QTabBar::tab { background: #425f5d; color: #ccc; padding: 8px 16px; border: 1px solid #333; }
            QTabBar::tab:hover { background: #5a7a82; }
        """)
        
        # Determine which tabs to show
        is_trigger = brush.get('is_trigger', False)
        is_mover = brush.get('is_mover', False)
        is_door = brush.get('is_door', False)
        shader_type = brush.get('shader', 'Default')
        
        # === GENERAL TAB (Type selection) ===
        general_tab = self._create_general_tab(brush)
        self.tab_widget.addTab(general_tab, "General")
        
        # === TRIGGER TAB (conditional) ===
        self.trigger_tab = self._create_trigger_tab(brush)
        self.trigger_tab_index = self.tab_widget.addTab(self.trigger_tab, "🎯 Trigger")
        self.tab_widget.setTabVisible(self.trigger_tab_index, is_trigger)
        
        # === MOVER TAB (conditional) ===
        self.mover_tab = self._create_mover_tab(brush)
        self.mover_tab_index = self.tab_widget.addTab(self.mover_tab, "⚡ Mover")
        self.tab_widget.setTabVisible(self.mover_tab_index, is_mover)
        
        # === DOOR TAB (conditional) ===
        self.door_tab = self._create_door_tab(brush)
        self.door_tab_index = self.tab_widget.addTab(self.door_tab, "🚪 Door")
        self.tab_widget.setTabVisible(self.door_tab_index, is_door)
        
        # === SHADER TAB (conditional) ===
        self.shader_tab = self._create_shader_tab(brush)
        self.shader_tab_index = self.tab_widget.addTab(self.shader_tab, "✨ Shader")
        self.tab_widget.setTabVisible(self.shader_tab_index, shader_type not in ['Default', None, ''])
        
        # === APPEARANCE TAB ===
        appearance_tab = self._create_appearance_tab(brush)
        self.tab_widget.addTab(appearance_tab, "Appearance")
        
        # === I/O TAB (for triggers, movers, doors) ===
        if IO_AVAILABLE and (is_trigger or is_mover or is_door):
            io_tab = self._create_io_tab_for_brush(brush)
            self.io_tab_index = self.tab_widget.addTab(io_tab, "⚡ I/O")
        
        content_layout.addWidget(self.tab_widget)
        content_layout.addStretch()
        
        scroll.setWidget(content_widget)
        self.main_layout.addWidget(scroll)

    def _create_io_tab_for_brush(self, brush):
        """Create I/O editor tab for brushes (triggers, movers, doors)."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Determine entity type for I/O registry lookup
        if brush.get('is_trigger'):
            entity_type = 'trigger'
        elif brush.get('is_door'):
            entity_type = 'door'
        elif brush.get('is_mover'):
            entity_type = 'mover'
        else:
            entity_type = 'brush'
        
        # Create I/O editor widget with the ACTUAL brush entity
        io_editor = IOEditorWidget(
            entity=brush,  # FIX: Pass the actual brush dict, not the Thing class
            entity_type=entity_type,
            editor_state=self.editor.state,
            editor=self.editor
        )
        io_editor.connections_changed.connect(self._on_io_connections_changed)
        layout.addWidget(io_editor)
        self._widgets['io_editor'] = io_editor
        
        # Add inputs reference section
        inputs_widget = IOInputsWidget()
        inputs_widget.set_entity(entity_type)
        layout.addWidget(inputs_widget)
        
        return tab

    def _create_io_tab_for_thing(self, thing):
        """Create I/O editor tab for Things."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        
        entity_type = get_entity_type_for_io(thing)
        
        io_editor = IOEditorWidget(
            entity=thing,
            entity_type=entity_type,
            editor_state=self.editor.state,
            editor=self.editor
        )
        io_editor.connections_changed.connect(self._on_io_connections_changed)
        layout.addWidget(io_editor)
        self._widgets['io_editor'] = io_editor
        
        # Add inputs reference section
        inputs_widget = IOInputsWidget()
        inputs_widget.set_entity(entity_type)
        layout.addWidget(inputs_widget)
        
        return tab

    def _on_io_connections_changed(self):
        """Called when I/O connections are modified."""
        # Save state for undo/redo
        if hasattr(self.editor.state, 'save_state'):
            self.editor.state.save_state()
        
        # Mark document as dirty
        if hasattr(self.editor, 'mark_dirty'):
            self.editor.mark_dirty()
        
        # Refresh targeting indicators
        if not self._populating:
            self.editor.update_all_ui()

    def _create_general_tab(self, brush):
        """Create the General tab with type toggles."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        # Type Selection Group
        type_group = QGroupBox("Brush Type")
        type_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #F08000; 
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        type_layout = QVBoxLayout(type_group)
        
        # Shader dropdown and Lock checkbox on same row
        shader_lock_layout = QHBoxLayout()
        shader_label = QLabel("Shader:")
        shader_combo = QComboBox()
        shader_combo.addItems(['Default', 'Glass', 'Glow', 'Water', 'Fog'])
        shader_combo.setCurrentText(brush.get('shader', 'Default'))
        shader_combo.currentTextChanged.connect(self.on_shader_changed)
        shader_lock_layout.addWidget(shader_label)
        shader_lock_layout.addWidget(shader_combo)
        shader_lock_layout.addStretch()
        
        # Lock checkbox on the right
        lock_label = QLabel("Lock:")
        lock_cb = QCheckBox("Prevent selection/editing")
        lock_cb.setStyleSheet(self._checkbox_style())
        lock_cb.setChecked(brush.get('lock', False))
        lock_cb.toggled.connect(lambda checked: self.update_object_prop('lock', checked))
        shader_lock_layout.addWidget(lock_label)
        shader_lock_layout.addWidget(lock_cb)
        
        type_layout.addLayout(shader_lock_layout)
        self._widgets['shader_combo'] = shader_combo
        self._widgets['lock_cb'] = lock_cb
        
        type_layout.addWidget(self._create_section_header("Behaviors"))
        
        # Trigger checkbox
        trigger_cb = QCheckBox("Is Trigger (activates other objects)")
        trigger_cb.setStyleSheet(self._checkbox_style())
        trigger_cb.setChecked(brush.get('is_trigger', False))
        trigger_cb.toggled.connect(self.on_trigger_changed)
        type_layout.addWidget(trigger_cb)
        self._widgets['trigger_cb'] = trigger_cb
        
        # Mover checkbox
        mover_cb = QCheckBox("Is Mover (moves back and forth)")
        mover_cb.setStyleSheet(self._checkbox_style())
        mover_cb.setChecked(brush.get('is_mover', False))
        mover_cb.toggled.connect(self.on_mover_changed)
        type_layout.addWidget(mover_cb)
        self._widgets['mover_cb'] = mover_cb
        
        # Door checkbox
        door_cb = QCheckBox("Is Door (opens when triggered)")
        door_cb.setStyleSheet(self._checkbox_style())
        door_cb.setChecked(brush.get('is_door', False))
        door_cb.toggled.connect(self.on_door_changed)
        type_layout.addWidget(door_cb)
        self._widgets['door_cb'] = door_cb
        
        layout.addWidget(type_group)
        layout.addStretch()
        return widget

    def _create_trigger_tab(self, brush):
        """Create the Trigger properties tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        form = QFormLayout()
        form.setSpacing(8)
        
        # Trigger type
        type_combo = QComboBox()
        type_combo.addItems(['Once', 'Multiple'])
        type_combo.setCurrentText(brush.get('trigger_type', 'Once'))
        type_combo.currentTextChanged.connect(lambda t: self.update_object_prop('trigger_type', t))
        form.addRow("Trigger Type:", type_combo)
        self._widgets['trigger_type_combo'] = type_combo
        
        # Damage section
        damage_group = QGroupBox("Damage")
        damage_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #F08000; 
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        damage_layout = QVBoxLayout(damage_group)
        
        hurt_cb = QCheckBox("Hurts player on contact")
        hurt_cb.setStyleSheet(self._checkbox_style())
        hurt_cb.setChecked(brush.get('hurt', False))
        hurt_cb.toggled.connect(self.on_hurt_changed)
        damage_layout.addWidget(hurt_cb)
        self._widgets['hurt_cb'] = hurt_cb
        
        damage_amount_layout = QHBoxLayout()
        damage_amount_layout.addWidget(QLabel("Damage Amount:"))
        damage_spin = QSpinBox()
        damage_spin.setRange(1, 1000)
        damage_spin.setValue(brush.get('hurt_amount', 10))
        damage_spin.valueChanged.connect(lambda v: self.update_object_prop('hurt_amount', v))
        damage_amount_layout.addWidget(damage_spin)
        damage_amount_layout.addStretch()
        damage_layout.addLayout(damage_amount_layout)
        self._widgets['damage_spin'] = damage_spin
        
        # Show/hide damage amount based on hurt checkbox
        damage_spin.setEnabled(brush.get('hurt', False))
        
        layout.addLayout(form)
        layout.addWidget(damage_group)
        layout.addStretch()
        return widget

    def _create_mover_tab(self, brush):
        """Create the Mover properties tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        form = QFormLayout()
        form.setSpacing(8)
        
        # Speed
        speed_input = QLineEdit(str(brush.get('speed', 64.0)))
        speed_input.editingFinished.connect(lambda: self.update_object_prop('speed', float(speed_input.text()) if speed_input.text() else 64.0))
        form.addRow("Speed:", speed_input)
        self._widgets['speed_input'] = speed_input
        
        # Distance
        distance_input = QLineEdit(str(brush.get('distance', 128.0)))
        distance_input.editingFinished.connect(lambda: self.update_object_prop('distance', float(distance_input.text()) if distance_input.text() else 128.0))
        form.addRow("Distance:", distance_input)
        self._widgets['distance_input'] = distance_input
        
        # Direction
        dir_widget = QWidget()
        dir_layout = QHBoxLayout(dir_widget)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(4)
        
        direction = brush.get('direction', [0, 1, 0])
        dir_x = QLineEdit(str(direction[0]))
        dir_y = QLineEdit(str(direction[1]))
        dir_z = QLineEdit(str(direction[2]))
        for inp in [dir_x, dir_y, dir_z]:
            inp.setFixedWidth(50)
        
        def update_direction():
            try:
                d = [float(dir_x.text()), float(dir_y.text()), float(dir_z.text())]
                self.update_object_prop('direction', d)
            except ValueError: pass
        
        dir_x.editingFinished.connect(update_direction)
        dir_y.editingFinished.connect(update_direction)
        dir_z.editingFinished.connect(update_direction)
        
        dir_layout.addWidget(QLabel("X:")); dir_layout.addWidget(dir_x)
        dir_layout.addWidget(QLabel("Y:")); dir_layout.addWidget(dir_y)
        dir_layout.addWidget(QLabel("Z:")); dir_layout.addWidget(dir_z)
        dir_layout.addStretch()
        form.addRow("Direction:", dir_widget)
        self._widgets['dir_x'] = dir_x
        self._widgets['dir_y'] = dir_y
        self._widgets['dir_z'] = dir_z
        
        layout.addLayout(form)
        
        # Options
        options_group = QGroupBox("Options")
        options_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #F08000; 
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        options_layout = QVBoxLayout(options_group)
        
        start_on_cb = QCheckBox("Start moving immediately")
        start_on_cb.setStyleSheet(self._checkbox_style())
        start_on_cb.setChecked(brush.get('start_on', False))
        start_on_cb.toggled.connect(lambda checked: self.update_object_prop('start_on', checked))
        options_layout.addWidget(start_on_cb)
        self._widgets['start_on_cb'] = start_on_cb
        
        layout.addWidget(options_group)
        
        # Preview button
        preview_btn = QPushButton("▶ Preview Movement")
        preview_btn.setCheckable(True)
        preview_btn.setStyleSheet("""
            QPushButton { background-color: #425F5D; color: white; border-radius: 4px; padding: 8px; font-weight: bold; }
            QPushButton:checked { background-color: #0056b3; }
            QPushButton:hover { background-color: #5a7a82; }
        """)
        preview_btn.toggled.connect(self.toggle_mover_preview)
        layout.addWidget(preview_btn)
        self._widgets['mover_preview_btn'] = preview_btn
        
        layout.addStretch()
        return widget
        
        def update_direction():
            try:
                d = [float(dir_x.text()), float(dir_y.text()), float(dir_z.text())]
                self.update_object_prop('direction', d)
            except ValueError: pass
        
        dir_x.editingFinished.connect(update_direction)
        dir_y.editingFinished.connect(update_direction)
        dir_z.editingFinished.connect(update_direction)
        
        dir_layout.addWidget(QLabel("X:")); dir_layout.addWidget(dir_x)
        dir_layout.addWidget(QLabel("Y:")); dir_layout.addWidget(dir_y)
        dir_layout.addWidget(QLabel("Z:")); dir_layout.addWidget(dir_z)
        dir_layout.addStretch()
        form.addRow("Direction:", dir_widget)
        self._widgets['dir_x'] = dir_x
        self._widgets['dir_y'] = dir_y
        self._widgets['dir_z'] = dir_z
        
        layout.addLayout(form)
        
        # Options
        options_group = QGroupBox("Options")
        options_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #F08000; 
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        options_layout = QVBoxLayout(options_group)
        
        start_on_cb = QCheckBox("Start moving immediately")
        start_on_cb.setStyleSheet(self._checkbox_style())
        start_on_cb.setChecked(brush.get('start_on', False))
        start_on_cb.toggled.connect(lambda checked: self.update_object_prop('start_on', checked))
        options_layout.addWidget(start_on_cb)
        self._widgets['start_on_cb'] = start_on_cb
        
        layout.addWidget(options_group)
        
        # Preview button
        preview_btn = QPushButton("▶ Preview Movement")
        preview_btn.setCheckable(True)
        preview_btn.setStyleSheet("""
            QPushButton { background-color: #425F5D; color: white; border-radius: 4px; padding: 8px; font-weight: bold; }
            QPushButton:checked { background-color: #0056b3; }
            QPushButton:hover { background-color: #5a7a82; }
        """)
        preview_btn.toggled.connect(self.toggle_mover_preview)
        layout.addWidget(preview_btn)
        self._widgets['mover_preview_btn'] = preview_btn
        
        layout.addStretch()
        return widget

    def _create_door_tab(self, brush):
        """Create the Door properties tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        form = QFormLayout()
        form.setSpacing(8)
        
        # Open direction
        dir_combo = QComboBox()
        dir_combo.addItems(['up', 'down', 'north', 'south', 'east', 'west'])
        dir_combo.setCurrentText(brush.get('door_direction', 'up'))
        dir_combo.currentTextChanged.connect(lambda t: self.update_object_prop('door_direction', t))
        form.addRow("Open Direction:", dir_combo)
        self._widgets['door_dir_combo'] = dir_combo
        
        # Open distance
        dist_input = QLineEdit(str(brush.get('door_distance', 128.0)))
        dist_input.editingFinished.connect(lambda: self.update_object_prop('door_distance', float(dist_input.text()) if dist_input.text() else 128.0))
        form.addRow("Open Distance:", dist_input)
        self._widgets['door_dist_input'] = dist_input
        
        # Lip
        lip_input = QLineEdit(str(brush.get('door_lip', 8.0)))
        lip_input.editingFinished.connect(lambda: self.update_object_prop('door_lip', float(lip_input.text()) if lip_input.text() else 8.0))
        form.addRow("Lip (stay closed):", lip_input)
        self._widgets['door_lip_input'] = lip_input
        
        # Speed
        speed_input = QLineEdit(str(brush.get('door_speed', 64.0)))
        speed_input.editingFinished.connect(lambda: self.update_object_prop('door_speed', float(speed_input.text()) if speed_input.text() else 64.0))
        form.addRow("Speed:", speed_input)
        self._widgets['door_speed_input'] = speed_input
        
        layout.addLayout(form)
        
        # Options
        options_group = QGroupBox("Door Options")
        options_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #F08000; 
                border: 1px solid #F08000;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        options_layout = QVBoxLayout(options_group)
        
        auto_open_cb = QCheckBox("Auto-open when player is near")
        auto_open_cb.setStyleSheet(self._checkbox_style())
        auto_open_cb.setChecked(brush.get('door_auto_open', False))
        auto_open_cb.toggled.connect(lambda checked: self.update_object_prop('door_auto_open', checked))
        options_layout.addWidget(auto_open_cb)
        self._widgets['door_auto_open_cb'] = auto_open_cb
        
        locked_cb = QCheckBox("Locked (requires trigger to open)")
        locked_cb.setStyleSheet(self._checkbox_style())
        locked_cb.setChecked(brush.get('door_locked', False))
        locked_cb.toggled.connect(lambda checked: self.update_object_prop('door_locked', checked))
        options_layout.addWidget(locked_cb)
        self._widgets['door_locked_cb'] = locked_cb
        
        needs_key_cb = QCheckBox("Requires key to open")
        needs_key_cb.setStyleSheet(self._checkbox_style())
        needs_key_cb.setChecked(brush.get('door_needs_key', False))
        needs_key_cb.toggled.connect(self.on_door_needs_key_changed)
        options_layout.addWidget(needs_key_cb)
        self._widgets['door_needs_key_cb'] = needs_key_cb
        
        # Key name field
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel("Key Name:"))
        key_input = QLineEdit(brush.get('door_key_name', ''))
        key_input.setPlaceholderText("e.g. blue_key")
        key_input.editingFinished.connect(lambda: self.update_object_prop('door_key_name', key_input.text()))
        key_layout.addWidget(key_input)
        options_layout.addLayout(key_layout)
        self._widgets['door_key_input'] = key_input
        key_input.setEnabled(brush.get('door_needs_key', False))
        
        layout.addWidget(options_group)
        
        # Preview button
        preview_btn = QPushButton("▶ Preview Door")
        preview_btn.setCheckable(True)
        preview_btn.setStyleSheet("""
            QPushButton { background-color: #425F5D; color: white; border-radius: 4px; padding: 8px; font-weight: bold; }
            QPushButton:checked { background-color: #0056b3; }
            QPushButton:hover { background-color: #5a7a82; }
        """)
        preview_btn.toggled.connect(self.toggle_door_preview)
        layout.addWidget(preview_btn)
        self._widgets['door_preview_btn'] = preview_btn
        
        layout.addStretch()
        return widget

    def _create_shader_tab(self, brush):
        """Create the Shader properties tab with dynamic content based on shader type."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        shader_type = brush.get('shader', 'Default')
        
        if shader_type == 'Glass':
            layout.addWidget(self._create_glass_properties(brush))
        elif shader_type == 'Glow':
            layout.addWidget(self._create_glow_properties(brush))
        elif shader_type == 'Water':
            layout.addWidget(self._create_water_properties(brush))
        elif shader_type == 'Fog':
            layout.addWidget(self._create_fog_properties(brush))
        else:
            layout.addWidget(QLabel("No shader-specific properties."))
        
        layout.addStretch()
        return widget

    def _create_glass_properties(self, brush):
        """Create Glass shader properties for realistic glass rendering."""
        group = QGroupBox("Glass Properties")
        group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #00BFFF; 
                border: 1px solid #00BFFF;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        
        form = QFormLayout()
        form.setSpacing(6)
        
        # Glass color/tint
        color_btn = self._create_color_button(brush.get('glass_color', [0.9, 0.95, 1.0]))
        color_btn.clicked.connect(lambda: self._pick_color('glass_color', color_btn, [0.9, 0.95, 1.0]))
        form.addRow("Tint Color:", color_btn)
        self._widgets['glass_color_btn'] = color_btn
        
        # Opacity slider (0 = fully transparent, 1 = opaque)
        opacity_layout = QHBoxLayout()
        opacity_slider = QSlider(Qt.Horizontal)
        opacity_slider.setRange(0, 100)
        opacity_slider.setValue(int(brush.get('glass_opacity', 0.3) * 100))
        opacity_label = QLabel(f"{brush.get('glass_opacity', 0.3):.2f}")
        opacity_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glass_opacity', v / 100.0),
            opacity_label.setText(f"{v / 100.0:.2f}")
        ))
        opacity_layout.addWidget(opacity_slider)
        opacity_layout.addWidget(opacity_label)
        form.addRow("Opacity:", opacity_layout)
        self._widgets['glass_opacity_slider'] = opacity_slider
        
        layout.addLayout(form)
        
        # Distortion section
        distort_group = QGroupBox("Distortion Effects")
        distort_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #87CEEB; 
                border: 1px solid #4a6a8a;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        distort_layout = QFormLayout(distort_group)
        distort_layout.setSpacing(6)
        
        # Distortion strength (warping effect)
        warp_layout = QHBoxLayout()
        warp_slider = QSlider(Qt.Horizontal)
        warp_slider.setRange(0, 100)
        warp_slider.setValue(int(brush.get('glass_distortion', 0.5) * 100))
        warp_label = QLabel(f"{brush.get('glass_distortion', 0.5):.2f}")
        warp_slider.setToolTip("How much the view through glass is warped/distorted")
        warp_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glass_distortion', v / 100.0),
            warp_label.setText(f"{v / 100.0:.2f}")
        ))
        warp_layout.addWidget(warp_slider)
        warp_layout.addWidget(warp_label)
        distort_layout.addRow("Warp Strength:", warp_layout)
        self._widgets['glass_distort_slider'] = warp_slider
        
        # Refraction index (how much light bends)
        refract_layout = QHBoxLayout()
        refract_slider = QSlider(Qt.Horizontal)
        refract_slider.setRange(100, 250)  # 1.0 to 2.5 (1.5 is typical glass)
        refract_slider.setValue(int(brush.get('glass_refraction', 1.5) * 100))
        refract_label = QLabel(f"{brush.get('glass_refraction', 1.5):.2f}")
        refract_slider.setToolTip("Index of refraction (1.0=air, 1.5=glass, 2.4=diamond)")
        refract_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glass_refraction', v / 100.0),
            refract_label.setText(f"{v / 100.0:.2f}")
        ))
        refract_layout.addWidget(refract_slider)
        refract_layout.addWidget(refract_label)
        distort_layout.addRow("Refraction:", refract_layout)
        self._widgets['glass_refraction_slider'] = refract_slider
        
        # Roughness (frosted glass effect)
        rough_layout = QHBoxLayout()
        rough_slider = QSlider(Qt.Horizontal)
        rough_slider.setRange(0, 100)
        rough_slider.setValue(int(brush.get('glass_roughness', 0.0) * 100))
        rough_label = QLabel(f"{brush.get('glass_roughness', 0.0):.2f}")
        rough_slider.setToolTip("Surface roughness (0=clear, 1=frosted)")
        rough_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glass_roughness', v / 100.0),
            rough_label.setText(f"{v / 100.0:.2f}")
        ))
        rough_layout.addWidget(rough_slider)
        rough_layout.addWidget(rough_label)
        distort_layout.addRow("Roughness:", rough_layout)
        self._widgets['glass_roughness_slider'] = rough_slider
        
        layout.addWidget(distort_group)
        
        # Fresnel section (edge reflection)
        fresnel_group = QGroupBox("Fresnel Effect")
        fresnel_group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #98FB98; 
                border: 1px solid #4a8a6a;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        fresnel_layout = QFormLayout(fresnel_group)
        fresnel_layout.setSpacing(6)
        
        # Fresnel strength
        fres_layout = QHBoxLayout()
        fres_slider = QSlider(Qt.Horizontal)
        fres_slider.setRange(0, 100)
        fres_slider.setValue(int(brush.get('glass_fresnel', 0.5) * 100))
        fres_label = QLabel(f"{brush.get('glass_fresnel', 0.5):.2f}")
        fres_slider.setToolTip("Edge reflection intensity (more reflective at grazing angles)")
        fres_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glass_fresnel', v / 100.0),
            fres_label.setText(f"{v / 100.0:.2f}")
        ))
        fres_layout.addWidget(fres_slider)
        fres_layout.addWidget(fres_label)
        fresnel_layout.addRow("Intensity:", fres_layout)
        self._widgets['glass_fresnel_slider'] = fres_slider
        
        layout.addWidget(fresnel_group)
        
        return group

    def _create_glow_properties(self, brush):
        """Create Glow shader properties."""
        group = QGroupBox("Glow Properties")
        group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #FFD700; 
                border: 1px solid #FFD700;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        
        form = QFormLayout()
        
        # Glow color
        color_btn = self._create_color_button(brush.get('glow_color', [1.0, 0.9, 0.7]))
        color_btn.clicked.connect(lambda: self._pick_color('glow_color', color_btn, [1.0, 0.9, 0.7]))
        form.addRow("Glow Color:", color_btn)
        self._widgets['glow_color_btn'] = color_btn
        
        # Intensity slider
        intensity_layout = QHBoxLayout()
        intensity_slider = QSlider(Qt.Horizontal)
        intensity_slider.setRange(10, 300)
        intensity_slider.setValue(int(brush.get('glow_intensity', 1.5) * 100))
        intensity_label = QLabel(f"{brush.get('glow_intensity', 1.5):.2f}")
        intensity_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('glow_intensity', v / 100.0),
            intensity_label.setText(f"{v / 100.0:.2f}")
        ))
        intensity_layout.addWidget(intensity_slider)
        intensity_layout.addWidget(intensity_label)
        form.addRow("Intensity:", intensity_layout)
        self._widgets['glow_intensity_slider'] = intensity_slider
        
        layout.addLayout(form)
        
        # Light direction selection
        layout.addWidget(QLabel("Emitting Face:"))
        
        face_grid = QGridLayout()
        face_grid.setSpacing(4)
        
        current_face = brush.get('light_direction', 'top')
        
        def create_face_btn(text, face_name, tooltip):
            btn = QPushButton(text)
            btn.setFixedSize(50, 35)
            btn.setToolTip(tooltip)
            btn.setCheckable(True)
            btn.setChecked(face_name == current_face)
            btn.setStyleSheet("""
                QPushButton { background-color: #425F5D; color: white; border-radius: 4px; font-weight: bold; }
                QPushButton:checked { background-color: #F08000; }
                QPushButton:hover { background-color: #5a7a82; }
            """)
            btn.clicked.connect(lambda: self._set_glow_direction(face_name))
            return btn
        
        face_grid.addWidget(create_face_btn("TOP", "top", "Emit from top"), 0, 1)
        face_grid.addWidget(create_face_btn("N", "north", "Emit from north (+Z)"), 1, 2)
        face_grid.addWidget(create_face_btn("W", "west", "Emit from west (-X)"), 1, 0)
        face_grid.addWidget(create_face_btn("ALL", "all", "Emit from all faces"), 1, 1)
        face_grid.addWidget(create_face_btn("E", "east", "Emit from east (+X)"), 1, 3)
        face_grid.addWidget(create_face_btn("S", "south", "Emit from south (-Z)"), 2, 2)
        face_grid.addWidget(create_face_btn("BTM", "bottom", "Emit from bottom"), 2, 1)
        
        layout.addLayout(face_grid)
        
        return group

    def _create_water_properties(self, brush):
        """Create Water shader properties."""
        group = QGroupBox("Water Properties")
        group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #00CED1; 
                border: 1px solid #00CED1;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        layout = QFormLayout(group)
        layout.setSpacing(8)
        
        # Water tint color
        color_btn = self._create_color_button(brush.get('water_tint', [0.0, 0.4, 0.6]))
        color_btn.clicked.connect(lambda: self._pick_color('water_tint', color_btn, [0.0, 0.4, 0.6]))
        layout.addRow("Water Tint:", color_btn)
        self._widgets['water_color_btn'] = color_btn
        
        # Opacity slider
        opacity_layout = QHBoxLayout()
        opacity_slider = QSlider(Qt.Horizontal)
        opacity_slider.setRange(0, 100)
        opacity_slider.setValue(int(brush.get('water_opacity', 0.5) * 100))
        opacity_label = QLabel(f"{brush.get('water_opacity', 0.5):.2f}")
        opacity_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('water_opacity', v / 100.0),
            opacity_label.setText(f"{v / 100.0:.2f}")
        ))
        opacity_layout.addWidget(opacity_slider)
        opacity_layout.addWidget(opacity_label)
        layout.addRow("Opacity:", opacity_layout)
        self._widgets['water_opacity_slider'] = opacity_slider
        
        # Reflectivity slider
        reflect_layout = QHBoxLayout()
        reflect_slider = QSlider(Qt.Horizontal)
        reflect_slider.setRange(0, 100)
        reflect_slider.setValue(int(brush.get('water_reflectivity', 0.5) * 100))
        reflect_label = QLabel(f"{brush.get('water_reflectivity', 0.5):.2f}")
        reflect_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('water_reflectivity', v / 100.0),
            reflect_label.setText(f"{v / 100.0:.2f}")
        ))
        reflect_layout.addWidget(reflect_slider)
        reflect_layout.addWidget(reflect_label)
        layout.addRow("Reflectivity:", reflect_layout)
        self._widgets['water_reflect_slider'] = reflect_slider

        # Wave Displacement Checkbox
        wave_cb = QCheckBox("Enable Vertex Waves")
        wave_cb.setStyleSheet(self._checkbox_style())
        wave_cb.setChecked(brush.get('water_wave_enabled', False))
        wave_cb.toggled.connect(lambda checked: self.update_object_prop('water_wave_enabled', checked))
        layout.addRow("", wave_cb)
        self._widgets['water_wave_cb'] = wave_cb

        # Wave Height Slider
        wave_h_layout = QHBoxLayout()
        wave_h_slider = QSlider(Qt.Horizontal)
        wave_h_slider.setRange(0, 200) # 0.0 to 2.0
        wave_h_slider.setValue(int(brush.get('water_wave_height', 0.5) * 100))
        wave_h_label = QLabel(f"{brush.get('water_wave_height', 0.5):.2f}")
        wave_h_slider.setToolTip("Amplitude of the waves. Keep low for realism.")
        wave_h_slider.valueChanged.connect(lambda v: (
            self.update_object_prop('water_wave_height', v / 100.0),
            wave_h_label.setText(f"{v / 100.0:.2f}")
        ))
        wave_h_layout.addWidget(wave_h_slider)
        wave_h_layout.addWidget(wave_h_label)
        layout.addRow("Wave Height:", wave_h_layout)
        self._widgets['water_wave_h_slider'] = wave_h_slider
        
        # Plane only checkbox
        plane_cb = QCheckBox("Draw top surface only")
        plane_cb.setStyleSheet(self._checkbox_style())
        plane_cb.setChecked(brush.get('water_plane', False))
        plane_cb.toggled.connect(lambda checked: self.update_object_prop('water_plane', checked))
        layout.addRow("", plane_cb)
        self._widgets['water_plane_cb'] = plane_cb
        
        return group

    def _create_fog_properties(self, brush):
        """Create Fog shader properties."""
        group = QGroupBox("Fog Properties")
        group.setStyleSheet("""
            QGroupBox { 
                font-weight: bold; 
                color: #B0C4DE; 
                border: 1px solid #B0C4DE;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 8px;
                padding: 0 4px;
                background-color: #2b3d3b;
            }
        """)
        layout = QFormLayout(group)
        layout.setSpacing(8)
        
        # Fog color
        color_btn = self._create_color_button(brush.get('fog_color', [0.5, 0.6, 0.7]))
        color_btn.clicked.connect(lambda: self._pick_color('fog_color', color_btn, [0.5, 0.6, 0.7]))
        layout.addRow("Fog Color:", color_btn)
        self._widgets['fog_color_btn'] = color_btn
        
        # Density
        density_input = QLineEdit(str(brush.get('fog_density', 2.0)))
        density_input.editingFinished.connect(lambda: self.update_object_prop('fog_density', float(density_input.text()) if density_input.text() else 2.0))
        layout.addRow("Density:", density_input)
        self._widgets['fog_density_input'] = density_input
        
        return group

    def _create_appearance_tab(self, brush):
        """Create the Appearance tab with color controls."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        
        form = QFormLayout()
        form.setSpacing(8)
        
        # Brush colour
        colour_widget = QWidget()
        colour_layout = QHBoxLayout(colour_widget)
        colour_layout.setContentsMargins(0, 0, 0, 0)
        colour_layout.setSpacing(8)
        
        current_colour = brush.get('colour', [0.8, 0.8, 0.8])
        colour_btn = self._create_color_button(current_colour, (140, 32))
        colour_btn.clicked.connect(lambda: self._pick_color('colour', colour_btn, [0.8, 0.8, 0.8]))
        
        reset_btn = QPushButton("Reset")
        reset_btn.setFixedSize(60, 32)
        reset_btn.setToolTip("Reset to default grey")
        reset_btn.clicked.connect(lambda: self._reset_brush_colour(colour_btn))
        
        colour_layout.addWidget(colour_btn)
        colour_layout.addWidget(reset_btn)
        colour_layout.addStretch()
        
        form.addRow("Brush Colour:", colour_widget)
        self._widgets['brush_colour_btn'] = colour_btn
        
        layout.addLayout(form)
        layout.addStretch()
        return widget

    def _pick_color(self, prop_name, button, default_color):
        """Open color picker and update property."""
        if self.current_object is None: return
        
        current = self.current_object.get(prop_name, default_color)
        if any(c > 1.0 for c in current):
            current_qcolor = QColor(int(current[0]), int(current[1]), int(current[2]))
        else:
            current_qcolor = QColor(int(current[0] * 255), int(current[1] * 255), int(current[2] * 255))
        
        color = QColorDialog.getColor(current_qcolor, self, f"Choose {prop_name.replace('_', ' ').title()}")
        if color.isValid():
            new_color = [color.redF(), color.greenF(), color.blueF()]
            self.current_object[prop_name] = new_color
            self._update_color_button(button, new_color)
            self.editor.update_all_ui()

    def _reset_brush_colour(self, button):
        """Reset brush colour to default."""
        if self.current_object is None: return
        default = [0.8, 0.8, 0.8]
        self.current_object['colour'] = default
        self._update_color_button(button, default)
        self.editor.update_all_ui()

    def _set_glow_direction(self, face_name):
        """Set the glow emission direction."""
        if self.current_object is None: return
        self.current_object['light_direction'] = face_name
        # Rebuild shader tab to update button states
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    # === Event Handlers ===

    def on_shader_changed(self, shader_type):
        if self.current_object is None: return
        self.current_object['shader'] = shader_type
        
        # Initialize shader-specific defaults
        if shader_type == 'Glass':
            if 'glass_color' not in self.current_object: self.current_object['glass_color'] = [0.9, 0.95, 1.0]
            if 'glass_opacity' not in self.current_object: self.current_object['glass_opacity'] = 0.3
            if 'glass_distortion' not in self.current_object: self.current_object['glass_distortion'] = 0.5
            if 'glass_refraction' not in self.current_object: self.current_object['glass_refraction'] = 1.5
            if 'glass_roughness' not in self.current_object: self.current_object['glass_roughness'] = 0.0
            if 'glass_fresnel' not in self.current_object: self.current_object['glass_fresnel'] = 0.5
        elif shader_type == 'Glow':
            if 'glow_color' not in self.current_object: self.current_object['glow_color'] = [1.0, 0.9, 0.7]
            if 'glow_intensity' not in self.current_object: self.current_object['glow_intensity'] = 1.5
            if 'light_direction' not in self.current_object: self.current_object['light_direction'] = 'top'
        elif shader_type == 'Water':
            self.current_object['is_fog'] = False
            if 'water_opacity' not in self.current_object: self.current_object['water_opacity'] = 0.5
            if 'water_reflectivity' not in self.current_object: self.current_object['water_reflectivity'] = 0.5
            if 'water_tint' not in self.current_object: self.current_object['water_tint'] = [0.0, 0.4, 0.6]
            if 'water_wave_enabled' not in self.current_object: self.current_object['water_wave_enabled'] = False
            if 'water_wave_height' not in self.current_object: self.current_object['water_wave_height'] = 0.5
        elif shader_type == 'Fog':
            self.current_object['is_fog'] = True
            if 'fog_density' not in self.current_object: self.current_object['fog_density'] = 2.0
            if 'fog_color' not in self.current_object: self.current_object['fog_color'] = [0.5, 0.6, 0.7]
        
        # Disable trigger for shader brushes
        if shader_type != 'Default':
            self.current_object['is_trigger'] = False
        
        # Rebuild UI
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_trigger_changed(self, is_trigger):
        if self.current_object is None: return
        self.current_object['is_trigger'] = is_trigger
        
        if is_trigger:
            if 'trigger_type' not in self.current_object: self.current_object['trigger_type'] = 'Once'
            # Set trigger texture
            for face in ['top', 'bottom', 'north', 'south', 'east', 'west']:
                if 'textures' not in self.current_object: self.current_object['textures'] = {}
                self.current_object['textures'][face] = 'trigger.jpg'
        
        # Update tab visibility
        if hasattr(self, 'trigger_tab_index') and self.tab_widget:
            self.tab_widget.setTabVisible(self.trigger_tab_index, is_trigger)
            if is_trigger:
                self.tab_widget.setCurrentIndex(self.trigger_tab_index)
        
        # Rebuild to add/remove I/O tab
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_mover_changed(self, is_mover):
        if self.current_object is None: return
        self.current_object['is_mover'] = is_mover
        
        if is_mover:
            if 'speed' not in self.current_object: self.current_object['speed'] = 64.0
            if 'distance' not in self.current_object: self.current_object['distance'] = 128.0
            if 'direction' not in self.current_object: self.current_object['direction'] = [0, 1, 0]
        
        # Update tab visibility
        if hasattr(self, 'mover_tab_index') and self.tab_widget:
            self.tab_widget.setTabVisible(self.mover_tab_index, is_mover)
            if is_mover:
                self.tab_widget.setCurrentIndex(self.mover_tab_index)
        
        # Rebuild to add/remove I/O tab
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_door_changed(self, is_door):
        if self.current_object is None: return
        self.current_object['is_door'] = is_door
        
        if is_door:
            if 'door_direction' not in self.current_object: self.current_object['door_direction'] = 'up'
            if 'door_distance' not in self.current_object: self.current_object['door_distance'] = 128.0
            if 'door_lip' not in self.current_object: self.current_object['door_lip'] = 8.0
            if 'door_speed' not in self.current_object: self.current_object['door_speed'] = 64.0
        
        # Update tab visibility
        if hasattr(self, 'door_tab_index') and self.tab_widget:
            self.tab_widget.setTabVisible(self.door_tab_index, is_door)
            if is_door:
                self.tab_widget.setCurrentIndex(self.door_tab_index)
        
        # Rebuild to add/remove I/O tab
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_hurt_changed(self, is_hurt):
        if self.current_object is None: return
        self.current_object['hurt'] = is_hurt
        if is_hurt:
            if 'hurt_amount' not in self.current_object: self.current_object['hurt_amount'] = 10
        
        # Enable/disable damage spin
        if 'damage_spin' in self._widgets:
            self._widgets['damage_spin'].setEnabled(is_hurt)
        
        self.editor.update_all_ui()

    def on_door_needs_key_changed(self, needs_key):
        if self.current_object is None: return
        self.current_object['door_needs_key'] = needs_key
        if needs_key:
            if 'door_key_name' not in self.current_object: self.current_object['door_key_name'] = ''
        
        # Enable/disable key input
        if 'door_key_input' in self._widgets:
            self._widgets['door_key_input'].setEnabled(needs_key)
        
        self.editor.update_all_ui()

    def toggle_mover_preview(self, checked):
        if self.editor:
            btn = self._widgets.get('mover_preview_btn')
            if checked:
                if btn: btn.setText("■ Stop Preview")
                self.editor.start_mover_preview(self.current_object)
            else:
                if btn: btn.setText("▶ Preview Movement")
                self.editor.stop_mover_preview()

    def toggle_door_preview(self, checked):
        if self.editor:
            # Correctly retrieve the button from the internal dictionary
            btn = self._widgets.get('door_preview_btn')
            if checked:
                if btn: btn.setText("■ Stop Preview")
                # Doors use the same mover logic for the preview
                if hasattr(self.editor, 'start_mover_preview'):
                    self.editor.start_mover_preview(self.current_object)
            else:
                if btn: btn.setText("▶ Preview Door")
                if hasattr(self.editor, 'stop_mover_preview'):
                    self.editor.stop_mover_preview()

    def populate_for_thing(self, thing):
        """Populate property editor for a Thing."""
        
        # Create scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)
        
        # Basic properties layout
        layout = QFormLayout()
        
        name_lbl = QLabel("Name:")
        name_lbl.setStyleSheet("QLabel { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px 8px; border-radius: 3px; }")
        cur_name = thing.properties.get('name', '')
        name_inp = QLineEdit(str(cur_name))
        name_inp.setStyleSheet("QLineEdit { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px; border: 2px solid #8B5AC2; border-radius: 3px; } QLineEdit:focus { border: 2px solid #A875D6; background-color: #7B4AB9; }")
        name_inp.setPlaceholderText("Enter name...")
        name_inp.editingFinished.connect(lambda: self.update_object_prop('name', name_inp.text()))
        layout.addRow(name_lbl, name_inp)
        
        thing_name = getattr(thing, 'name', '') or thing.properties.get('name', '')
        targeting_sources = self._find_targeting_sources(thing_name) if thing_name else []
        if targeting_sources:
            source_texts = [f"{name} ({stype})" for name, stype in targeting_sources]
            targeted_label = QLabel(", ".join(source_texts))
            targeted_label.setStyleSheet("QLabel { color: #00FF00; font-weight: bold; padding: 2px; background-color: #1a3d1a; border: 1px solid #00AA00; border-radius: 3px; }")
            targeted_label.setWordWrap(True)
            layout.addRow("Targeted by:", targeted_label)
        
        if isinstance(thing, Model):
            self.add_model_path_widget(layout, thing)
            self.add_vector3_widget(layout, thing, 'scale')
            self.add_vector3_widget(layout, thing, 'rotation')
            
            if IO_AVAILABLE:
                io_note = QLabel("💡 Use the I/O tab for advanced targeting")
                io_note.setStyleSheet("QLabel { color: #88AAFF; font-style: italic; padding: 4px; }")
                layout.addRow("", io_note)

        if isinstance(thing, Light):
            self.add_color_picker_widget(layout, thing, 'colour')

        is_pickup = isinstance(thing, Pickup)
        current_item_type = thing.properties.get('item_type', 'health') if is_pickup else None
        
        # Track widgets to hide/show for pickup type changes
        self._pickup_value_widgets = []
        self._pickup_key_widgets = []
        self._pickup_sprite_widgets = []
        
        for key, value in sorted(thing.properties.items()):
            if key == 'name': continue
            if key == '_io_connections': continue  # Skip I/O connections - handled in I/O tab
            if isinstance(thing, Light) and key in ['colour', 'type']: continue
            if isinstance(thing, Model) and key in ['model_path', 'scale', 'rotation', 'type']: continue
            # Skip these - we handle them specially for Pickup
            if is_pickup and key in ['key_name', 'custom_sprite', 'respawns', 'respawn_time']: continue

            label_text = key.replace('_', ' ').title() + ":"
            
            if isinstance(thing, Light) and key == 'state':
                widget = QComboBox()
                widget.addItems(['on', 'off'])
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow(label_text, widget)
            elif isinstance(thing, Speaker) and key == 'sound_file':
                self.add_sound_file_widget(layout, thing, key, value)
            # === NEW: Specific handling for Logic Gate Type ===
            elif isinstance(thing, LogicGate) and key == 'logic_type':
                widget = QComboBox()
                widget.addItems(['AND', 'OR', 'XOR', 'NAND', 'NOR'])
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow("Logic Type:", widget)
            elif isinstance(thing, Pickup) and key == 'item_type':
                widget = QComboBox()
                item_types = ['health', 'key', 'gun1']  #add 'gun2' in future update
                widget.addItems(item_types)
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(self.on_pickup_item_type_changed)
                layout.addRow(label_text, widget)
                
                # Key Name dropdown (only visible for key type)
                self.pickup_key_name_label = QLabel("Key Name:")
                self.pickup_key_name_combo = QComboBox()
                key_names = ['blue_key', 'red_key', 'yellow_key', 'green_key']
                self.pickup_key_name_combo.addItems(key_names)
                # Allow custom key names too
                self.pickup_key_name_combo.setEditable(True)
                current_key = thing.properties.get('key_name', 'blue_key')
                if current_key in key_names:
                    self.pickup_key_name_combo.setCurrentText(current_key)
                else:
                    self.pickup_key_name_combo.setCurrentText(current_key)
                self.pickup_key_name_combo.currentTextChanged.connect(self.on_pickup_key_name_changed)
                layout.addRow(self.pickup_key_name_label, self.pickup_key_name_combo)
                
                self._pickup_key_widgets.append((self.pickup_key_name_label, self.pickup_key_name_combo))
                
                is_key_type = (value == 'key')
                self.pickup_key_name_label.setVisible(is_key_type)
                self.pickup_key_name_combo.setVisible(is_key_type)
                
            elif isinstance(thing, Pickup) and key == 'activation':
                widget = QComboBox()
                activation_types = ['walk_over', 'use']
                widget.addItems(activation_types)
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow(label_text, widget)
                # Store reference to restrict activation for health pickups
                self._pickup_activation_widget = widget
                # If health pickup, force walk_over and disable
                if current_item_type == 'health':
                    widget.setCurrentText('walk_over')
                    widget.setEnabled(False)
                    self.update_object_prop('activation', 'walk_over')
            elif isinstance(thing, Pickup) and key == 'value':
                # Store reference to hide when item_type is 'key'
                label = QLabel(label_text)
                widget = QSpinBox()
                widget.setRange(-99999, 99999)
                widget.setValue(value)
                widget.valueChanged.connect(lambda v, k=key: self.update_object_prop(k, v))
                layout.addRow(label, widget)
                self._pickup_value_widgets.append((label, widget))
                # Hide if currently a key
                if current_item_type == 'key':
                    label.setVisible(False)
                    widget.setVisible(False)
            elif isinstance(value, bool):
                widget = QCheckBox()
                widget.setStyleSheet(self._checkbox_style())
                widget.setChecked(value)
                widget.stateChanged.connect(lambda state, k=key: self.update_object_prop(k, state == Qt.Checked))
                layout.addRow(label_text, widget)
            elif isinstance(value, int):
                widget = QSpinBox()
                widget.setRange(-99999, 99999)
                widget.setValue(value)
                widget.valueChanged.connect(lambda v, k=key: self.update_object_prop(k, v))
                layout.addRow(label_text, widget)
            elif isinstance(value, float):
                widget = QLineEdit(str(value))
                widget.editingFinished.connect(lambda le=widget, k=key: self.update_object_prop(k, float(le.text()) if le.text() and le.text().replace('.', '', 1).replace('-', '', 1).isdigit() else 0.0))
                layout.addRow(label_text, widget)
            else:
                widget = QLineEdit(str(value))
                widget.editingFinished.connect(lambda le=widget, k=key: self.update_object_prop(k, le.text()))
                layout.addRow(label_text, widget)
        
        # Add Pickup-specific controls
        if is_pickup:
            # Sprite selection button (only for non-key types)
            self.pickup_sprite_label = QLabel("Sprite:")
            sprite_widget = QWidget()
            sprite_layout = QHBoxLayout(sprite_widget)
            sprite_layout.setContentsMargins(0, 0, 0, 0)
            
            custom_sprite = thing.properties.get('custom_sprite', '')
            self.pickup_sprite_path = QLineEdit(custom_sprite)
            self.pickup_sprite_path.setReadOnly(True)
            self.pickup_sprite_path.setPlaceholderText("Default sprite")
            
            sprite_btn = QPushButton("Sprite...")
            sprite_btn.setFixedWidth(120)
            sprite_btn.clicked.connect(self.on_pickup_sprite_select)
            
            clear_btn = QPushButton("clear")
            clear_btn.setFixedWidth(120)
            clear_btn.setToolTip("Clear custom sprite")
            clear_btn.clicked.connect(self.on_pickup_sprite_clear)
            
            sprite_layout.addWidget(self.pickup_sprite_path)
            sprite_layout.addWidget(sprite_btn)
            sprite_layout.addWidget(clear_btn)
            
            layout.addRow(self.pickup_sprite_label, sprite_widget)
            self._pickup_sprite_widgets.append((self.pickup_sprite_label, sprite_widget))
            
            # Hide sprite controls for keys (keys have fixed sprites)
            if current_item_type == 'key':
                self.pickup_sprite_label.setVisible(False)
                sprite_widget.setVisible(False)
        
        # Add respawn controls ONLY for Pickups (PlayerStart, Speaker, etc. do not respawn)
        if isinstance(thing, Pickup):
            layout.addRow(self._create_section_header("Respawn"))
            
            respawns = thing.properties.get('respawns', False)
            respawn_time = thing.properties.get('respawn_time', 20.0)
            
            respawn_widget = QWidget()
            respawn_layout = QHBoxLayout(respawn_widget)
            respawn_layout.setContentsMargins(0, 0, 0, 0)
            
            self.respawn_checkbox = QCheckBox("Respawns")
            self.respawn_checkbox.setStyleSheet(self._checkbox_style())
            self.respawn_checkbox.setChecked(respawns)
            self.respawn_checkbox.stateChanged.connect(self.on_respawn_toggled)
            
            self.respawn_time_label = QLabel("after")
            self.respawn_time_spin = QDoubleSpinBox()
            self.respawn_time_spin.setRange(0.1, 9999.0)
            self.respawn_time_spin.setValue(respawn_time)
            self.respawn_time_spin.setSuffix(" sec")
            self.respawn_time_spin.valueChanged.connect(lambda v: self.update_object_prop('respawn_time', v))
            
            # Show/hide respawn time based on checkbox
            self.respawn_time_label.setVisible(respawns)
            self.respawn_time_spin.setVisible(respawns)
            
            respawn_layout.addWidget(self.respawn_checkbox)
            respawn_layout.addWidget(self.respawn_time_label)
            respawn_layout.addWidget(self.respawn_time_spin)
            respawn_layout.addStretch()
            
            layout.addRow("", respawn_widget)
        
        content_layout.addLayout(layout)
        
        # === TAB WIDGET FOR I/O (if available) ===
        if IO_AVAILABLE:
            entity_type = get_entity_type_for_io(thing)
            if entity_type and entity_type in IO_REGISTRY:
                # Create tab widget for I/O
                self.tab_widget = QTabWidget()
                self.tab_widget.setStyleSheet("""
                    QTabBar::tab:selected { background: #F08000; color: white; }
                    QTabBar::tab { background: #425f5d; color: #ccc; padding: 8px 16px; border: 1px solid #333; }
                    QTabBar::tab:hover { background: #5a7a82; }
                """)
                
                # Add I/O tab
                io_tab = self._create_io_tab_for_thing(thing)
                self.tab_widget.addTab(io_tab, "⚡ I/O")
                
                content_layout.addWidget(self.tab_widget)
        
        content_layout.addStretch()
        scroll.setWidget(content_widget)
        self.main_layout.addWidget(scroll)

    def on_respawn_toggled(self, state):
        """Handle respawn checkbox toggle."""
        respawns = state == Qt.Checked
        self.update_object_prop('respawns', respawns)
        if hasattr(self, 'respawn_time_label'):
            self.respawn_time_label.setVisible(respawns)
        if hasattr(self, 'respawn_time_spin'):
            self.respawn_time_spin.setVisible(respawns)

    def on_pickup_key_name_changed(self, key_name):
        """Handle key name dropdown change - updates sprite immediately."""
        if self.current_object is None: return
        self.update_object_prop('key_name', key_name)
        # Clear sprite cache to force reload
        if hasattr(Pickup, 'clear_sprite_cache'):
            Pickup.clear_sprite_cache()
        # Refresh views to show new sprite
        self.editor.update_all_ui()

    def on_pickup_sprite_select(self):
        """Open file dialog to select custom sprite for pickup."""
        if self.current_object is None or not isinstance(self.current_object, Pickup):
            return
        
        start_path = os.path.join('assets', 'sprites')
        if not os.path.exists(start_path):
            start_path = 'assets'
        
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Select Sprite Image", start_path,
            "Image Files (*.png *.jpg *.jpeg *.bmp)"
        )
        
        if filepath:
            try:
                rel_path = os.path.relpath(filepath, '.').replace('\\', '/')
            except ValueError:
                rel_path = filepath
            
            self.update_object_prop('custom_sprite', rel_path)
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText(rel_path)
            
            # Clear sprite cache to force reload
            if hasattr(Pickup, 'clear_sprite_cache'):
                Pickup.clear_sprite_cache()
            
            self.editor.update_all_ui()

    def on_pickup_sprite_clear(self):
        """Clear custom sprite, revert to default."""
        if self.current_object is None or not isinstance(self.current_object, Pickup):
            return
        
        self.update_object_prop('custom_sprite', '')
        if hasattr(self, 'pickup_sprite_path'):
            self.pickup_sprite_path.setText('')
        
        # Clear sprite cache to force reload
        if hasattr(Pickup, 'clear_sprite_cache'):
            Pickup.clear_sprite_cache()
        
        self.editor.update_all_ui()

    def on_pickup_item_type_changed(self, item_type):
        if self.current_object is None: return
        self.update_object_prop('item_type', item_type)
        
        is_key = (item_type == 'key')
        is_health = (item_type == 'health')
        is_gun = (item_type in ['gun1', 'gun2'])
        
        # Show/hide key name widgets
        if hasattr(self, '_pickup_key_widgets'):
            for label, widget in self._pickup_key_widgets:
                label.setVisible(is_key)
                widget.setVisible(is_key)
        
        # Show/hide value widgets (hide for keys)
        if hasattr(self, '_pickup_value_widgets'):
            for label, widget in self._pickup_value_widgets:
                label.setVisible(not is_key)
                widget.setVisible(not is_key)
        
        # Show/hide sprite widgets (hide for keys - they use predefined sprites)
        if hasattr(self, '_pickup_sprite_widgets'):
            for label, widget in self._pickup_sprite_widgets:
                label.setVisible(not is_key)
                widget.setVisible(not is_key)
        
        # Handle specific item type logic
        if is_health:
            # Set default sprite for health pickups
            self.update_object_prop('custom_sprite', 'assets/sprites/health.png')
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText('assets/sprites/health.png')
            # Force walk_over activation
            self.update_object_prop('activation', 'walk_over')
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setCurrentText('walk_over')
                self._pickup_activation_widget.setEnabled(False)
        
        elif is_gun:
            # Set default sprite for guns (e.g. assets/sprites/gun1.png)
            sprite_path = f'assets/sprites/{item_type}.png'
            self.update_object_prop('custom_sprite', sprite_path)
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText(sprite_path)
            # Force walk_over activation for guns
            self.update_object_prop('activation', 'walk_over')
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setCurrentText('walk_over')
                self._pickup_activation_widget.setEnabled(False)
                
        else:
            # Re-enable activation dropdown for generic pickups
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setEnabled(True)
        
        # Clear sprite cache and refresh views
        if hasattr(Pickup, 'clear_sprite_cache'):
            Pickup.clear_sprite_cache()
        
        self.editor.update_all_ui()

    def add_model_path_widget(self, layout, thing):
        widget = QWidget()
        h_box = QHBoxLayout(widget)
        h_box.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit(thing.properties.get('model_path', ''))
        path_edit.setReadOnly(True)
        btn = QPushButton("...")
        btn.setFixedWidth(30)
        def pick_model():
            filepath, _ = QFileDialog.getOpenFileName(self, "Select OBJ Model", "assets/models", "OBJ Files (*.obj)")
            if filepath:
                try: rel_path = os.path.relpath(filepath, "assets").replace("\\", "/")
                except: rel_path = filepath
                if not rel_path.startswith(".."): rel_path = os.path.join("assets", rel_path) if not rel_path.startswith("assets") else rel_path
                self.update_object_prop('model_path', rel_path)
                path_edit.setText(rel_path)
        btn.clicked.connect(pick_model)
        h_box.addWidget(path_edit)
        h_box.addWidget(btn)
        layout.addRow("Model Path:", widget)

    def add_vector3_widget(self, layout, thing, key):
        widget = QWidget()
        h_box = QHBoxLayout(widget)
        h_box.setContentsMargins(0, 0, 0, 0)
        val = thing.properties.get(key, [0, 0, 0])
        if not isinstance(val, list) or len(val) != 3: val = [0, 0, 0]
        for i in range(3):
            le = QLineEdit(str(val[i]))
            le.setFixedWidth(50)
            def update_vec(text, idx=i):
                try:
                    current_vec = thing.properties.get(key, [0,0,0])
                    current_vec[idx] = float(text)
                    self.update_object_prop(key, current_vec)
                except ValueError: pass
            le.editingFinished.connect(lambda l=le, idx=i: update_vec(l.text(), idx))
            h_box.addWidget(le)
        layout.addRow(key.title() + ":", widget)

    def add_sound_file_widget(self, form_layout, thing, key, value):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        line_edit = QLineEdit(str(value))
        line_edit.setReadOnly(True)
        button = QPushButton("...")
        button.setFixedWidth(30)
        def open_dialog():
            start_path = os.path.join('assets', 'sounds')
            if not os.path.exists(start_path): os.makedirs(start_path)
            filepath, _ = QFileDialog.getOpenFileName(self, "Select Sound File", start_path, "Sound Files (*.wav *.mp3)")
            if filepath:
                try: relative_path = os.path.relpath(filepath, ".").replace('\\', '/')
                except ValueError: relative_path = os.path.basename(filepath)
                self.update_object_prop(key, relative_path)
                line_edit.setText(relative_path)
        button.clicked.connect(open_dialog)
        layout.addWidget(line_edit)
        layout.addWidget(button)
        form_layout.addRow(key.replace('_', ' ').title() + ":", widget)

    def add_color_picker_widget(self, form_layout, thing, key):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        current_color_rgb = thing.properties.get(key, [255, 255, 255])
        color_swatch = QPushButton()
        color_swatch.setFixedSize(200, 32)
        def update_swatch():
            rgb = thing.properties.get(key, [255, 255, 255])
            color_swatch.setStyleSheet(f"background-color: rgb({rgb[0]}, {rgb[1]}, {rgb[2]});")
        def open_color_dialog():
            current_color_rgb = thing.properties.get(key, [255, 255, 255])
            color = QColorDialog.getColor(QColor(*current_color_rgb), self, "Choose Light Colour")
            if color.isValid():
                self.update_object_prop(key, [color.red(), color.green(), color.blue()])
                update_swatch()
        color_swatch.clicked.connect(open_color_dialog)
        update_swatch()
        layout.addWidget(color_swatch)
        form_layout.addRow("Colour:", widget)

    def update_object_prop(self, key, value):
        if self.current_object is None: return
        if isinstance(self.current_object, dict):
            self.current_object[key] = value
        elif isinstance(self.current_object, Thing):
            if key in self.current_object.properties:
                prop_type = type(self.current_object.properties.get(key))
                if prop_type == float:
                    try: value = float(value)
                    except (ValueError, TypeError): value = 0.0
            self.current_object.properties[key] = value
        
        # Only update UI if we're not in the middle of populating
        # This prevents infinite recursion when populate calls update_object_prop
        if not self._populating:
            self.editor.update_all_ui()
