import os
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QFormLayout, QCheckBox, QComboBox, QPushButton,
                             QHBoxLayout, QColorDialog, QFileDialog, QGridLayout, QToolButton)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QColor, QIcon
from editor.things import Thing, Light, Pickup, Monster, Model, Speaker

class PropertyEditor(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.current_object = None
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.setLayout(self.main_layout)
        
        self.locked_checkbox = None
        self.trigger_checkbox = None
        self.target_label = None
        self.target_input = None
        self.type_label = None
        self.type_combo = None
        self.fog_checkbox = None
        self.fog_density_label = None
        self.fog_density_input = None
        self.fog_emit_light_label = None
        self.fog_emit_light_checkbox = None
        self.fog_color_label = None
        self.fog_color_button = None

        # Brush name (for targeting)
        self.brush_name_label = None
        self.brush_name_input = None

        # Mover properties
        self.mover_checkbox = None
        self.mover_speed_label = None
        self.mover_speed_input = None
        self.mover_distance_label = None
        self.mover_distance_input = None
        self.mover_direction_label = None 
        self.mover_dir_widget = None
        self.mover_start_on_label = None
        self.mover_start_on_checkbox = None
        self.preview_btn = None

        self.set_object(None)

    def clear_layout(self):
        """Removes all widgets from the main layout."""
        while self.main_layout.count():
            child = self.main_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                # Also clear widgets from child layouts before deleting the layout
                layout = child.layout()
                while layout.count():
                    item = layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()

    def set_object(self, obj):
        """Sets the object for the property editor and rebuilds the UI."""
        self.current_object = obj
        self.clear_layout()

        if obj is None:
            self.main_layout.addWidget(QLabel("Nothing selected."))
            return

        if isinstance(obj, dict):
            self.populate_for_brush(obj)
        elif isinstance(obj, Thing):
            self.populate_for_thing(obj)

    def populate_for_brush(self, brush):
        """Populates the UI with properties for a brush object."""
        layout = QFormLayout()
        is_locked = brush.get('lock', False)
        is_trigger = brush.get('is_trigger', False)
        is_fog = brush.get('is_fog', False)

        # Create Widgets and store them as instance variables
        self.locked_checkbox = QCheckBox()
        self.locked_checkbox.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")
        self.trigger_checkbox = QCheckBox()
        self.trigger_checkbox.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")
        self.target_label = QLabel("Target:")
        self.target_input = QLineEdit(brush.get('target', ''))
        self.type_label = QLabel("Trigger Type:")
        self.type_combo = QComboBox()
        self.type_combo.addItems(['Once', 'Multiple'])
        self.fog_checkbox = QCheckBox()
        self.fog_checkbox.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")
        self.fog_density_label = QLabel("Density:")
        self.fog_density_input = QLineEdit(str(brush.get('fog_density', 2.0)))
        self.fog_color_label = QLabel("Colour:")
        self.fog_color_button = QPushButton()
        self.fog_color_button.setFixedSize(200, 32)
        self.update_fog_color_button(brush.get('fog_color', [0.5, 0.6, 0.7]))

        # Mover widgets
        self.mover_checkbox = QCheckBox()
        self.mover_checkbox.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")
        
        self.mover_speed_label = QLabel("Speed:")
        self.mover_speed_input = QLineEdit(str(brush.get('speed', 64.0)))
        self.mover_distance_label = QLabel("Distance:")
        self.mover_distance_input = QLineEdit(str(brush.get('distance', 128.0)))
        
        self.mover_direction_label = QLabel("Direction:")
        
        # Directional Arrow Controls
        self.mover_dir_widget = QWidget()
        dir_layout = QGridLayout(self.mover_dir_widget)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(2)

        def create_dir_btn(text, direction):
            btn = QPushButton(text)
            btn.setFixedSize(30, 30)
            btn.setToolTip(f"Set direction to {direction}")
            btn.clicked.connect(lambda: self.update_object_prop('direction', direction))
            return btn

        # Layout: Vertical movement on the left, Cardinal on the right
        # UP/DOWN (Y-Axis)
        dir_layout.addWidget(create_dir_btn("UP", [0, 1, 0]), 0, 0)
        dir_layout.addWidget(create_dir_btn("DN", [0, -1, 0]), 2, 0)

        # COMPASS (X/Z Axes)
        # N=(0,0,1), S=(0,0,-1), E=(1,0,0), W=(-1,0,0) based on typical editors
        dir_layout.addWidget(create_dir_btn("N", [0, 0, 1]), 0, 3) 
        dir_layout.addWidget(create_dir_btn("W", [-1, 0, 0]), 1, 2)
        dir_layout.addWidget(create_dir_btn("E", [1, 0, 0]), 1, 4)
        dir_layout.addWidget(create_dir_btn("S", [0, 0, -1]), 2, 3)

        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setCheckable(True)
        self.preview_btn.setStyleSheet("""
            QPushButton {
                background-color: #425F5D; 
                color: white; 
                border-radius: 4px; 
                padding: 6px;
                font-weight: bold;
            }
            QPushButton:checked {
                background-color: #0056b3;
            }
        """)
        self.preview_btn.toggled.connect(self.toggle_preview)

        self.mover_start_on_label = QLabel("Start On:")
        self.mover_start_on_checkbox = QCheckBox()
        self.mover_start_on_checkbox.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")

        # Brush name (for triggering movers)
        self.brush_name_label = QLabel("Name:")
        self.brush_name_input = QLineEdit(brush.get('name', ''))

        # Set initial state
        self.locked_checkbox.setChecked(is_locked)
        self.trigger_checkbox.setChecked(is_trigger)
        self.type_combo.setCurrentText(brush.get('trigger_type', 'Once'))
        self.fog_checkbox.setChecked(is_fog)
        self.mover_checkbox.setChecked(brush.get('is_mover', False))
        self.mover_start_on_checkbox.setChecked(brush.get('start_on', False))



        # Add to Layout
        layout.addRow("Lock:", self.locked_checkbox)
        layout.addRow("Is Trigger:", self.trigger_checkbox)
        layout.addRow(self.target_label, self.target_input)
        layout.addRow(self.type_label, self.type_combo)
        layout.addRow("Fog Volume:", self.fog_checkbox)
        layout.addRow(self.fog_density_label, self.fog_density_input)
        layout.addRow(self.fog_color_label, self.fog_color_button)

        # Mover Section
        layout.addRow("Is Mover:", self.mover_checkbox)
        layout.addRow(self.brush_name_label, self.brush_name_input)
        layout.addRow(self.mover_speed_label, self.mover_speed_input)
        layout.addRow(self.mover_distance_label, self.mover_distance_input)
        layout.addRow(self.mover_direction_label, self.mover_dir_widget)
        layout.addRow(self.mover_start_on_label, self.mover_start_on_checkbox)
        layout.addRow("", self.preview_btn) # Add Preview Button at bottom of mover section



        # Connect Signals
        self.locked_checkbox.toggled.connect(self.on_lock_changed)
        self.brush_name_input.editingFinished.connect(lambda: self.update_object_prop('name', self.brush_name_input.text()))
        self.trigger_checkbox.toggled.connect(self.on_trigger_changed)
        self.target_input.editingFinished.connect(lambda: self.update_object_prop('target', self.target_input.text()))
        self.type_combo.currentTextChanged.connect(lambda t: self.update_object_prop('trigger_type', t))
        self.fog_checkbox.toggled.connect(self.on_fog_changed)
        self.fog_density_input.editingFinished.connect(lambda: self.update_object_prop('fog_density', float(self.fog_density_input.text()) if self.fog_density_input.text() else 0.1))
        self.fog_color_button.clicked.connect(self.on_fog_color_changed)
        
        self.mover_checkbox.toggled.connect(self.on_mover_changed)
        self.mover_speed_input.editingFinished.connect(
            lambda: self.update_object_prop('speed', float(self.mover_speed_input.text()) if self.mover_speed_input.text() else 64.0))
        self.mover_distance_input.editingFinished.connect(
            lambda: self.update_object_prop('distance', float(self.mover_distance_input.text()) if self.mover_distance_input.text() else 128.0))
        
        self.mover_start_on_checkbox.toggled.connect(
            lambda checked: self.update_object_prop('start_on', checked))

        self.main_layout.addLayout(layout)
        self.update_brush_ui_state()

    def toggle_preview(self, checked):
        if self.editor:
            if checked:
                self.preview_btn.setText("Stop Preview")
                self.editor.start_mover_preview(self.current_object)
            else:
                self.preview_btn.setText("Preview Movement")
                self.editor.stop_mover_preview()

    def on_fog_color_changed(self):
        if self.current_object is None:
            return
        color = QColorDialog.getColor()
        if color.isValid():
            self.current_object['fog_color'] = [color.redF(), color.greenF(), color.blueF()]
            self.update_fog_color_button(self.current_object['fog_color'])
            self.editor.update_all_ui()

    def on_mover_changed(self, is_mover):
        if self.current_object is None: return
        self.current_object['is_mover'] = is_mover
        if is_mover:
            if 'speed' not in self.current_object: self.current_object['speed'] = 64.0
            if 'distance' not in self.current_object: self.current_object['distance'] = 128.0
            if 'direction' not in self.current_object: self.current_object['direction'] = [0, 1, 0]
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def update_fog_color_button(self, color_rgb):
        qcolor = QColor.fromRgbF(*color_rgb)
        self.fog_color_button.setStyleSheet(f"background-color: {qcolor.name()}")

    def update_brush_ui_state(self):
        """Updates the enabled/visible state of brush property widgets."""
        if self.current_object is None:
            return

        is_locked = self.current_object.get('lock', False)
        is_trigger = self.current_object.get('is_trigger', False)
        is_fog = self.current_object.get('is_fog', False)

        self.trigger_checkbox.setEnabled(not is_locked)

        show_trigger_fields = is_trigger and not is_locked
        self.target_label.setVisible(show_trigger_fields)
        self.target_input.setVisible(show_trigger_fields)
        self.type_label.setVisible(show_trigger_fields)
        self.type_combo.setVisible(show_trigger_fields)

        show_fog_fields = is_fog and not is_locked
        self.fog_density_label.setVisible(show_fog_fields)
        self.fog_density_input.setVisible(show_fog_fields)
        self.fog_color_label.setVisible(show_fog_fields)
        self.fog_color_button.setVisible(show_fog_fields)

        is_mover = self.current_object.get('is_mover', False)
        show_mover_fields = is_mover and not is_locked
        self.brush_name_label.setVisible(show_mover_fields)
        self.brush_name_input.setVisible(show_mover_fields)
        self.mover_speed_label.setVisible(show_mover_fields)
        self.mover_speed_input.setVisible(show_mover_fields)
        self.mover_distance_label.setVisible(show_mover_fields)
        self.mover_direction_label.setVisible(show_mover_fields)
        self.mover_dir_widget.setVisible(show_mover_fields)
        self.mover_start_on_label.setVisible(show_mover_fields)
        self.mover_start_on_checkbox.setVisible(show_mover_fields)
        self.preview_btn.setVisible(show_mover_fields)


    def on_lock_changed(self, is_locked):
        """Handler for when the lock checkbox is toggled."""
        if self.current_object is None: return
        
        self.current_object['lock'] = is_locked
        self.update_brush_ui_state()
        self.editor.update_all_ui()

    def on_trigger_changed(self, is_trigger):
        """Handler for when the trigger checkbox is toggled."""
        if self.current_object is None: return
        
        self.current_object['is_trigger'] = is_trigger
        # Automatically assign trigger texture when a brush is set as a trigger
        if is_trigger:
            self.current_object['is_fog'] = False
            if 'textures' not in self.current_object:
                self.current_object['textures'] = {}
            for face in ['north','south','east','west','top','down']:
                self.current_object['textures'][face] = 'trigger.jpg'
        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_fog_changed(self, is_fog):
        """Handler for when the fog checkbox is toggled."""
        if self.current_object is None: return
        
        self.current_object['is_fog'] = is_fog
        if is_fog:
            self.current_object['is_trigger'] = False
            
            # Initialize defaults immediately so the renderer sees them
            if 'fog_density' not in self.current_object:
                self.current_object['fog_density'] = 2.0
            if 'fog_color' not in self.current_object:
                self.current_object['fog_color'] = [0.5, 0.6, 0.7]

        self.set_object(self.current_object)
        self.editor.update_all_ui()

    def on_fog_emit_light_changed(self, emit_light):
        """Handler for when the fog emit light checkbox is toggled."""
        if self.current_object is None: return
        
        self.current_object['fog_emit_light'] = emit_light
        self.editor.update_all_ui()

    def populate_for_thing(self, thing):
        """Populates the UI with properties for a Thing object."""
        layout = QFormLayout()
        
        # Special handling for colour picker at the top
        if isinstance(thing, Light): #
            self.add_color_picker_widget(layout, thing, 'colour')

        for key, value in sorted(thing.properties.items()):
            if isinstance(thing, Light) and key == 'colour':
                continue

            label_text = key.replace('_', ' ').title() + ":"
            
            if isinstance(thing, Light) and key == 'state':
                widget = QComboBox()
                widget.addItems(['on', 'off'])
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow(label_text, widget)
            elif isinstance(thing, Speaker) and key == 'sound_file':
                self.add_sound_file_widget(layout, thing, key, value)
            elif isinstance(thing, Pickup) and key == 'item_type':
                widget = QComboBox()
                # PICKUP TYPES
                item_types = ['health', 'ammo', 'armour', 'powerup', 'key', 'message', 'weapon']
                widget.addItems(item_types)
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow(label_text, widget)
            elif isinstance(thing, Pickup) and key == 'activation':
                widget = QComboBox()
                activation_types = ['walk_over', 'use']
                widget.addItems(activation_types)
                widget.setCurrentText(value)
                widget.currentTextChanged.connect(lambda t, k=key: self.update_object_prop(k, t))
                layout.addRow(label_text, widget)
            elif isinstance(value, bool):
                widget = QCheckBox()
                widget.setStyleSheet("QCheckBox::indicator { width: 25px; height: 25px; }")
                widget.setChecked(value)
                widget.stateChanged.connect(lambda state, k=key: self.update_object_prop(k, state == Qt.Checked))
                layout.addRow(label_text, widget)
            elif isinstance(value, int):
                widget = QSpinBox(); widget.setRange(-99999, 99999); widget.setValue(value)
                widget.valueChanged.connect(lambda v, k=key: self.update_object_prop(k, v))
                layout.addRow(label_text, widget)
            elif isinstance(value, float):
                widget = QLineEdit(str(value))
                widget.editingFinished.connect(
                    lambda le=widget, k=key: self.update_object_prop(k, float(le.text()) if le.text() and le.text().replace('.', '', 1).isdigit() else 0.0)
                )
                layout.addRow(label_text, widget)
            else:
                widget = QLineEdit(str(value))
                widget.editingFinished.connect(lambda le=widget, k=key: self.update_object_prop(k, le.text()))
                layout.addRow(label_text, widget)
                
        self.main_layout.addLayout(layout)

    def add_sound_file_widget(self, form_layout, thing, key, value):
        """Creates a sound file selector widget."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        line_edit = QLineEdit(str(value))
        line_edit.setReadOnly(True)
        button = QPushButton("...")
        button.setFixedWidth(30)

        def open_dialog():
            start_path = os.path.join('assets', 'sounds')
            if not os.path.exists(start_path):
                os.makedirs(start_path)
            
            filepath, _ = QFileDialog.getOpenFileName(self, "Select Sound File", start_path, "Sound Files (*.wav *.mp3)")
            if filepath:
                try:
                    relative_path = os.path.relpath(filepath, 'assets').replace('\\', '/')
                except ValueError:
                    relative_path = os.path.basename(filepath)
                
                self.update_object_prop(key, relative_path)
                line_edit.setText(relative_path)

        button.clicked.connect(open_dialog)
        layout.addWidget(line_edit)
        layout.addWidget(button)
        form_layout.addRow(key.replace('_', ' ').title() + ":", widget)

    def add_color_picker_widget(self, form_layout, thing, key):
        """Creates a color picker widget for light color properties."""
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
        """
        Updates a property on the current object and tells the main editor 
        to refresh the entire UI to ensure consistency.
        """
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

        self.editor.update_all_ui()