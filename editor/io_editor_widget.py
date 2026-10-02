"""
I/O Connection Editor Widget

This widget provides a visual interface for managing entity I/O connections
in the property editor, similar to the Hammer Editor's Output tab.
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QComboBox, QLineEdit,
    QDoubleSpinBox, QCheckBox, QHeaderView, QAbstractItemView,
    QDialog, QDialogButtonBox, QFormLayout, QCompleter, QGroupBox,
    QMessageBox, QMenu, QAction, QSizePolicy
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont

try:
    from .io_system import (
        OutputConnection, get_outputs, get_inputs, get_output_names,
        get_input_names, get_connections, add_connection, remove_connection,
        get_entity_type_for_io, IO_REGISTRY
    )
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False


# ── Random name generator for unnamed entities ──
_ADJECTIVES = [
    "red", "blue", "green", "dark", "bright", "old", "new", "big", "small",
    "fast", "slow", "hot", "cold", "wild", "calm", "sharp", "soft", "hard",
    "deep", "high", "low", "near", "far", "left", "right", "front", "back",
    "inner", "outer", "upper", "lower", "main", "side", "alpha", "beta",
    "gamma", "delta", "prime", "mega", "super", "ultra", "hyper", "nano"
]

_NOUNS = [
    "door", "gate", "lift", "mover", "block", "panel", "plate", "switch",
    "button", "lever", "valve", "hatch", "cover", "shield", "barrier",
    "wall", "floor", "ceiling", "ramp", "stairs", "bridge", "tunnel",
    "box", "crate", "chest", "locker", "cabinet", "shelf", "rack",
    "light", "lamp", "beacon", "torch", "flare", "spark", "glow",
    "unit", "node", "hub", "core", "center", "point", "spot", "zone",
    "sector", "region", "area", "cell", "chunk", "piece", "part"
]


def _generate_random_name() -> str:
    """Generate a random entity name like 'red_door_42'."""
    import random
    return f"{random.choice(_ADJECTIVES)}_{random.choice(_NOUNS)}_{random.randint(1, 999)}"


def _ensure_entity_named(entity) -> str:
    """
    Ensure an entity has a non-empty name. If unnamed, assign a random name
    and return it. Works for both brushes (dicts) and things (objects).
    """
    if isinstance(entity, dict):
        name = entity.get('name', '')
        if not name:
            name = _generate_random_name()
            entity['name'] = name
        return name
    elif hasattr(entity, 'properties'):
        name = entity.properties.get('name', '')
        if not name:
            name = _generate_random_name()
            entity.properties['name'] = name
        return name
    elif hasattr(entity, 'name'):
        if not entity.name:
            entity.name = _generate_random_name()
        return entity.name
    return _generate_random_name()


def _entity_name(entity) -> str:
    """Read an entity's current name without mutating it."""
    if isinstance(entity, dict):
        return entity.get('name', '')
    if hasattr(entity, 'properties'):
        return entity.properties.get('name', '')
    if hasattr(entity, 'name'):
        return entity.name or ''
    return ''


def _ensure_entity_id(entity) -> str:
    """
    Ensure an entity carries a stable UUID and return it. Entities normally
    receive an 'id' when created (Thing constructor / editor_state), but this
    guards the rare case of an older entity that predates ID assignment so a
    connection can always be stored identity-addressed.
    """
    import uuid
    if isinstance(entity, dict):
        eid = entity.get('id', '')
        if not eid:
            eid = str(uuid.uuid4())
            entity['id'] = eid
        return eid
    elif hasattr(entity, 'properties'):
        eid = entity.properties.get('id', '')
        if not eid:
            eid = str(uuid.uuid4())
            entity.properties['id'] = eid
        return eid
    return ''



class ClickableComboBox(QComboBox):
    """QComboBox that toggles its dropdown on any click, not just the arrow."""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Toggle: if popup is visible, hide it; otherwise show it
            if self.view().isVisible():
                self.hidePopup()
            else:
                self.showPopup()
            event.accept()
            return
        super().mousePressEvent(event)


class IOConnectionDialog(QDialog):
    """Dialog for adding/editing a single I/O connection."""
    
    def __init__(self, parent=None, entity=None, editor_state=None, 
                 existing_connection=None):
        super().__init__(parent)
        self.entity = entity
        self.editor_state = editor_state
        self.existing_connection = existing_connection

        # Identity addressing: the entity explicitly picked as the target (if
        # any) so we can capture its stable UUID even if it is later renamed.
        self._picked_entity = None

        self.setWindowTitle(
            "Edit Output Connection" if existing_connection else "Add Output Connection"
        )
        self.setMinimumWidth(450)
        
        self._setup_ui()
        
        if existing_connection:
            self._populate_from_connection(existing_connection)
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        form_layout = QFormLayout()
        form_layout.setSpacing(8)
        
        # Output selector
        self.output_combo = QComboBox()
        entity_type = get_entity_type_for_io(self.entity)
        outputs = get_output_names(entity_type)
        self.output_combo.addItems(outputs)
        self.output_combo.setEditable(True)
        form_layout.addRow("My Output:", self.output_combo)
        
        # Target entity name
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("Target entity name or UUID...")

        all_names = self._get_all_entity_names()
        completer = QCompleter(all_names)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.target_edit.setCompleter(completer)

        target_row = QHBoxLayout()
        target_row.addWidget(self.target_edit)

        # Dropdown button to show all valid entities
        dropdown_btn = QPushButton("▼")
        dropdown_btn.setFixedWidth(28)
        dropdown_btn.setToolTip("Click to show all entities in the level")
        dropdown_btn.clicked.connect(self._show_target_dropdown)
        target_row.addWidget(dropdown_btn)

        form_layout.addRow("Target Entity:", target_row)
        
        # Input selector
        self.input_combo = ClickableComboBox()
        self.input_combo.setEditable(True)
        self.input_combo.setStyleSheet("""
            QComboBox {
                padding-right: 30px;
            }
            QComboBox::drop-down {
                width: 30px;
                border: none;
                background-color: #F08000;
            }
            QComboBox::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
                border-left: 8px solid transparent;
                border-right: 8px solid transparent;
                border-top: 12px solid white;
            }
            QComboBox::down-arrow:hover {
                border-top: 12px solid #eeeeee;
            }
        """)
        self.target_edit.textChanged.connect(self._update_input_options)
        form_layout.addRow("Target Input:", self.input_combo)
        
        # Parameter
        self.param_edit = QLineEdit()
        self.param_edit.setPlaceholderText("Optional parameter...")
        form_layout.addRow("Parameter:", self.param_edit)
        
        # Delay
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 999.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setDecimals(2)
        self.delay_spin.setSuffix(" sec")
        form_layout.addRow("Delay:", self.delay_spin)
        
        # Fire once
        self.fire_once_check = QCheckBox("Only fire once per play session")
        form_layout.addRow("", self.fire_once_check)
        
        layout.addLayout(form_layout)
        
        help_label = QLabel(
            "<i>When <b>My Output</b> fires, it will call <b>Target Input</b> "
            "on the <b>Target Entity</b> (its name or UUID). If a UUID is used "
            "the link survives renaming the target.</i>"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #888; margin-top: 10px;")
        layout.addWidget(help_label)
        
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _get_all_entity_names(self):
        names = []
        if self.editor_state:
            for brush in self.editor_state.brushes:
                name = brush.get('name', '')
                if name:
                    names.append(name)
            for thing in self.editor_state.things:
                name = thing.properties.get('name', '')
                if name:
                    names.append(name)
        return names
    
    def _find_target_entity(self, text):
        """
        Resolve the Target Entity field to a scene entity.

        The field accepts either a name or a stable UUID, so a designer can
        paste an entity's ID (shown in the property editor) and have it
        addressed identity-first. Name is tried before ID.
        """
        if not self.editor_state or not text:
            return None
        ent = self.editor_state.find_entity_by_name(text)
        if ent is not None:
            return ent
        if hasattr(self.editor_state, 'find_entity_by_id'):
            return self.editor_state.find_entity_by_id(text)
        return None

    def _update_input_options(self, target_name):
        # If the user has hand-edited the name away from the picked entity,
        # drop the picked reference so we fall back to name-based ID resolution.
        if self._picked_entity is not None and \
                _entity_name(self._picked_entity) != target_name:
            self._picked_entity = None

        self.input_combo.clear()

        if not self.editor_state or not target_name:
            return

        # Accept a name OR a UUID in the target field.
        target_entity = self._find_target_entity(target_name)
        target_type = get_entity_type_for_io(target_entity) if target_entity is not None else None

        if target_type:
            inputs = get_input_names(target_type)
            self.input_combo.addItems(inputs)
    
    def _show_target_dropdown(self):
        """Show a menu with all door/mover brushes and all entities (things) in the level.

        Unnamed entities will be auto-named when selected so they can be targeted.
        """
        self._target_menu = QMenu(self)
        menu = self._target_menu

        door_movers = []
        entities = []

        if self.editor_state:
            # Collect door and mover brushes (with or without names)
            for brush in self.editor_state.brushes:
                if brush.get('is_door') or brush.get('is_mover'):
                    name = brush.get('name', '')
                    btype = 'door' if brush.get('is_door') else 'mover'
                    display = name if name else f"<unnamed {btype}>"
                    door_movers.append((display, brush, name, btype))

            # Collect all thing entities (with or without names)
            for thing in self.editor_state.things:
                name = thing.properties.get('name', '')
                ttype = thing.properties.get('type', 'thing')
                display = name if name else f"<unnamed {ttype}>"
                entities.append((display, thing, name, ttype))

        # Sort each group by display name
        door_movers.sort(key=lambda x: x[0].lower())
        entities.sort(key=lambda x: x[0].lower())

        has_items = False

        # Door/Mover brushes section
        if door_movers:
            has_items = True
            menu.addSection("DOOR / MOVER BRUSHES")
            for display, entity, current_name, btype in door_movers:
                action = QAction(f"{display}  [{btype}]", self)
                # Pass the entity reference so we can auto-name it if unnamed
                from functools import partial
                action.triggered.connect(partial(self._set_target_from_entity, entity))
                menu.addAction(action)

        # Separator between sections
        if door_movers and entities:
            menu.addSeparator()

        # Entities (things) section
        if entities:
            has_items = True
            menu.addSection("ENTITIES (THINGS)")
            for display, entity, current_name, ttype in entities:
                action = QAction(f"{display}  [{ttype}]", self)
                from functools import partial
                action.triggered.connect(partial(self._set_target_from_entity, entity))
                menu.addAction(action)

        if not has_items:
            no_action = QAction("No door/mover brushes or entities in level", self)
            no_action.setEnabled(False)
            menu.addAction(no_action)

        # Position menu below the button
        sender = self.sender()
        if sender:
            menu.exec_(sender.mapToGlobal(sender.rect().bottomLeft()))

    def _set_target_from_dropdown(self, name):
        """Set the target entity name from the dropdown selection (legacy)."""
        self.target_edit.setText(name)
        self.target_edit.editingFinished.emit()
        # Trigger input options update
        self._update_input_options(name)

    def _set_target_from_entity(self, entity):
        """Set the target from an entity reference, auto-naming if unnamed.

        Records the entity so its stable UUID can be captured for
        identity-addressed targeting (see get_connection).
        """
        name = _ensure_entity_named(entity)
        _ensure_entity_id(entity)
        self._picked_entity = entity
        self.target_edit.setText(name)
        self.target_edit.editingFinished.emit()
        # Trigger input options update (do not clear the freshly-picked entity)
        self._update_input_options(name)

    def _start_pick_mode(self):
        QMessageBox.information(
            self, "Pick Mode",
            "Click on an entity in the 2D or 3D view to select it as the target."
        )
    
    def _populate_from_connection(self, conn):
        self.output_combo.setCurrentText(conn.output_name)
        self.target_edit.setText(conn.target_name)
        self.input_combo.setCurrentText(conn.input_name)
        self.param_edit.setText(conn.parameter)
        self.delay_spin.setValue(conn.delay)
        self.fire_once_check.setChecked(conn.fire_once)
    
    def _validate_and_accept(self):
        output = self.output_combo.currentText().strip()
        target = self.target_edit.text().strip()
        input_name = self.input_combo.currentText().strip()
        
        if not output:
            QMessageBox.warning(self, "Validation Error", "Please select an output.")
            return
        
        if not target:
            QMessageBox.warning(self, "Validation Error", "Please enter a target entity name.")
            return
        
        if not input_name:
            QMessageBox.warning(self, "Validation Error", "Please select a target input.")
            return
        
        self.accept()
    
    def _resolve_target(self, typed: str):
        """
        Resolve the Target Entity field into a (target_name, target_id) pair.

        The field accepts either a name or a UUID. Identity addressing is
        preferred: when the target resolves to a scene entity its stable UUID
        is stored, and a human-readable name is kept for display / legacy
        fallback. When a bare UUID is pasted for an unnamed entity the UUID is
        retained as the name so the connection still shows something.

        Order of preference:
          1. The entity explicitly picked from the dropdown.
          2. A by-name lookup in the current scene.
          3. A by-ID lookup (the field itself is a pasted UUID).
          4. The name/ID already on the connection being edited, kept when the
             typed text is unchanged (so IDs survive an edit even offline).
        """
        # 1. Explicitly picked entity (name must still match the field)
        if self._picked_entity is not None and _entity_name(self._picked_entity) == typed:
            return typed, _ensure_entity_id(self._picked_entity)

        if self.editor_state and typed:
            # 2. Resolve by name
            ent = self.editor_state.find_entity_by_name(typed)
            if ent is not None:
                return typed, _ensure_entity_id(ent)

            # 3. The typed text is itself a UUID
            if hasattr(self.editor_state, 'find_entity_by_id'):
                ent = self.editor_state.find_entity_by_id(typed)
                if ent is not None:
                    real_name = _entity_name(ent)
                    # Keep the entity's real name for display when it has one;
                    # otherwise fall back to the UUID so the row isn't blank.
                    return (real_name or typed), typed

        # 4. Preserve existing target when editing and the field is unchanged
        if self.existing_connection is not None and \
                getattr(self.existing_connection, 'target_name', '') == typed:
            return typed, getattr(self.existing_connection, 'target_id', '') or ''

        return typed, ''

    def get_connection(self):
        typed = self.target_edit.text().strip()
        target_name, target_id = self._resolve_target(typed)
        return OutputConnection(
            output_name=self.output_combo.currentText().strip(),
            target_name=target_name,
            input_name=self.input_combo.currentText().strip(),
            parameter=self.param_edit.text(),
            delay=self.delay_spin.value(),
            fire_once=self.fire_once_check.isChecked(),
            target_id=target_id
        )


class IOEditorWidget(QWidget):
    """
    Widget for editing I/O connections on an entity.
    Embeds in the property editor.
    """
    
    connections_changed = pyqtSignal()
    
    def __init__(self, parent=None, editor=None, entity=None, entity_type=None, editor_state=None):
        super().__init__(parent)
        self.editor = editor
        self.current_entity = None
        self.entity_type = entity_type
        self.editor_state = editor_state
        
        self._setup_ui()
        
        if entity:
            self.set_entity(entity)
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        header = QLabel("Output Connections")
        header.setStyleSheet("""
            QLabel {
                background-color: #2D5A6B;
                color: white;
                font-weight: bold;
                padding: 6px 8px;
                border-radius: 3px;
            }
        """)
        layout.addWidget(header)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)
        
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self._add_connection)
        btn_layout.addWidget(self.add_btn)
        
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(self._edit_selected)
        btn_layout.addWidget(self.edit_btn)
        
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        btn_layout.addWidget(self.remove_btn)
        
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._copy_selected)
        btn_layout.addWidget(self.copy_btn)
        
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Output", "Target", "Input", "Param", "Delay"
        ])
        
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # Compact, non-expanding table: fixed row height, height recalculated
        # after every refresh so it hugs exactly the visible connections.
        # Height is derived from the table's actual font metrics (plus cell
        # padding) rather than a guessed constant, so rows aren't clipped.
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._row_height = self.table.fontMetrics().height() + 12
        self.table.verticalHeader().setDefaultSectionSize(self._row_height)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        
        # === FIX: enforce readable header contrast ===
        self.table.setStyleSheet("""
            QHeaderView::section {
                background-color: #3A3A3A;
                color: #E6E6E6;
                padding: 4px;
                border: 1px solid #2A2A2A;
                font-weight: bold;
            }
            QTableWidget::item {
                background-color: #2A2A2A;
                color: #E6E6E6;
                padding: 4px 6px;
            }
            QTableWidget::item:alternate {
                background-color: #252525;
                color: #E6E6E6;
            }
            QTableWidget::item:selected {
                background-color: #F08000;
                color: #000000;
            }
        """)
        
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header_view.setHighlightSections(False)
        header_view.setStretchLastSection(True)
        
        self.table.doubleClicked.connect(self._edit_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        
        layout.addWidget(self.table)

        # Spacer to push console button to the right
        btn_layout.addStretch()

        # OPEN CONSOLE Button
        self.console_btn = QPushButton("Debug Console")
        self.console_btn.setToolTip("Open the Debug Console")
        self.console_btn.setStyleSheet("font-weight: bold;")
        self.console_btn.clicked.connect(self._open_console)
        btn_layout.addWidget(self.console_btn)
        
        layout.addLayout(btn_layout)
        layout.addStretch()
        
        self.table.itemSelectionChanged.connect(self._update_button_states)
        self._update_button_states()
        self._update_table_height()
    
    def set_entity(self, entity):
        self.current_entity = entity
        self._refresh_table()
    
    def _refresh_table(self):
        self.table.setRowCount(0)
        
        if not self.current_entity:
            self._update_table_height()
            return
        
        connections = get_connections(self.current_entity)
        
        for conn in connections:
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            self.table.setItem(row, 0, QTableWidgetItem(conn.output_name))

            # Identity-addressed display: prefer resolving the target by its
            # stable UUID so a renamed target still shows its *current* name.
            resolved = self._resolve_connection_target(conn)
            if resolved is not None:
                current_name = self._entity_display_name(resolved)
                target_item = QTableWidgetItem(current_name or conn.target_name)
                if getattr(conn, 'target_id', '') and current_name and \
                        current_name != conn.target_name:
                    # Name drifted but the ID still points at the entity.
                    target_item.setToolTip(
                        f"Resolved by ID (was '{conn.target_name}')"
                    )
            else:
                target_item = QTableWidgetItem(conn.target_name)
                target_item.setForeground(QColor(255, 100, 100))
                target_item.setToolTip("Target entity not found!")
            self.table.setItem(row, 1, target_item)
            
            self.table.setItem(row, 2, QTableWidgetItem(conn.input_name))
            
            param_text = conn.parameter if conn.parameter else "-"
            self.table.setItem(row, 3, QTableWidgetItem(param_text))
            
            delay_text = f"{conn.delay:.2f}s" if conn.delay > 0 else "-"
            if conn.fire_once:
                delay_text += " (once)"
            self.table.setItem(row, 4, QTableWidgetItem(delay_text))
        
        self._update_button_states()
        self._update_table_height()

    def _update_table_height(self):
        """
        Size the table to exactly fit its current rows (plus header/frame)
        so it never expands beyond its content, leaving the action row
        directly beneath it instead of pushed to the bottom of the tab.
        """
        row_count = self.table.rowCount()
        header_height = self.table.horizontalHeader().height()
        frame = 2 * self.table.frameWidth()
        total_height = header_height + (row_count * self._row_height) + frame
        self.table.setFixedHeight(total_height)
    
    @staticmethod
    def _entity_display_name(entity):
        """Current name of a resolved brush/thing (may be empty)."""
        if isinstance(entity, dict):
            return entity.get('name', '')
        if hasattr(entity, 'properties'):
            return entity.properties.get('name', '')
        return getattr(entity, 'name', '') or ''

    def _resolve_connection_target(self, conn):
        """
        Resolve the entity a connection targets, preferring its stable UUID and
        falling back to the target name (identity-addressed with name fallback).
        Returns the brush/thing, or None if it can't be found.
        """
        if not self.editor:
            return None
        state = self.editor.state
        target_id = getattr(conn, 'target_id', '')
        if target_id and hasattr(state, 'find_entity_by_id'):
            ent = state.find_entity_by_id(target_id)
            if ent is not None:
                return ent
        target_name = getattr(conn, 'target_name', '')
        if target_name and hasattr(state, 'find_entity_by_name'):
            return state.find_entity_by_name(target_name)
        return None

    def _target_exists(self, target_name):
        if not self.editor or not target_name:
            return False

        for brush in self.editor.state.brushes:
            if brush.get('name') == target_name:
                return True

        for thing in self.editor.state.things:
            if thing.properties.get('name') == target_name:
                return True

        return False
    
    def _update_button_states(self):
        has_selection = len(self.table.selectedItems()) > 0
        self.edit_btn.setEnabled(has_selection)
        self.remove_btn.setEnabled(has_selection)
        self.copy_btn.setEnabled(has_selection)
    
    def _add_connection(self):
        if not self.current_entity:
            return
        
        dialog = IOConnectionDialog(
            self,
            entity=self.current_entity,
            editor_state=self.editor.state if self.editor else None
        )
        
        if dialog.exec_() == QDialog.Accepted:
            conn = dialog.get_connection()
            add_connection(self.current_entity, conn)
            self._refresh_table()
            self.connections_changed.emit()
    
    def _edit_selected(self):
        if not self.current_entity:
            return
        
        row = self.table.currentRow()
        if row < 0:
            return
        
        connections = get_connections(self.current_entity)
        if row >= len(connections):
            return
        
        existing_conn = connections[row]
        
        dialog = IOConnectionDialog(
            self,
            entity=self.current_entity,
            editor_state=self.editor.state if self.editor else None,
            existing_connection=existing_conn
        )
        
        if dialog.exec_() == QDialog.Accepted:
            connections[row] = dialog.get_connection()
            self._refresh_table()
            self.connections_changed.emit()
    
    def _remove_selected(self):
        if not self.current_entity:
            return
        
        row = self.table.currentRow()
        if row < 0:
            return
        
        connections = get_connections(self.current_entity)
        if row >= len(connections):
            return
        
        conn = connections[row]
        
        reply = QMessageBox.question(
            self, "Remove Connection",
            f"Remove connection: {conn.output_name} -> {conn.target_name}.{conn.input_name}?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            remove_connection(self.current_entity, conn)
            self._refresh_table()
            self.connections_changed.emit()
    
    def _copy_selected(self):
        if not self.current_entity:
            return
        
        row = self.table.currentRow()
        if row < 0:
            return
        
        connections = get_connections(self.current_entity)
        if row >= len(connections):
            return
        
        original = connections[row]
        
        copy = OutputConnection(
            output_name=original.output_name,
            target_name=original.target_name,
            input_name=original.input_name,
            parameter=original.parameter,
            delay=original.delay,
            fire_once=original.fire_once,
            target_id=getattr(original, 'target_id', '')
        )
        
        add_connection(self.current_entity, copy)
        self._refresh_table()
        self.connections_changed.emit()
    
    def _show_context_menu(self, pos):
        menu = QMenu(self)
        
        add_action = QAction("Add Connection...", self)
        add_action.triggered.connect(self._add_connection)
        menu.addAction(add_action)
        
        if self.table.currentRow() >= 0:
            edit_action = QAction("Edit...", self)
            edit_action.triggered.connect(self._edit_selected)
            menu.addAction(edit_action)
            
            copy_action = QAction("Duplicate", self)
            copy_action.triggered.connect(self._copy_selected)
            menu.addAction(copy_action)
            
            menu.addSeparator()
            
            remove_action = QAction("Remove", self)
            remove_action.triggered.connect(self._remove_selected)
            menu.addAction(remove_action)
        
        menu.exec_(self.table.mapToGlobal(pos))
    
    def _open_console(self):
        """Switch to the Debug Console tab in the properties pane."""
        if self.editor and hasattr(self.editor, 'properties_tab_widget'):
            tab = self.editor.properties_tab_widget
            console_idx = tab.indexOf(self.editor.debug_console)
            if console_idx >= 0:
                tab.setCurrentIndex(console_idx)
                self.editor.properties_dock.setVisible(True)


class IOInputsWidget(QWidget):
    """
    Widget showing available inputs for an entity (read-only reference).
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        header = QLabel("Available Inputs (for targeting)")
        header.setStyleSheet("""
            QLabel {
                background-color: #4A6B2D;
                color: white;
                font-weight: bold;
                padding: 6px 8px;
                border-radius: 3px;
            }
        """)
        layout.addWidget(header)
        
        self.inputs_list = QLabel()
        self.inputs_list.setWordWrap(True)
        self.inputs_list.setStyleSheet("""
            QLabel {
                padding: 8px;
                background-color: #2A2A2A;
                border-radius: 3px;
                color: #f0f0f0;
            }
        """)
        layout.addWidget(self.inputs_list)
        layout.addStretch()
    
    def set_entity(self, entity):
        try:
            from .io_system import get_input_names, get_entity_type_for_io
        except ImportError:
            self.inputs_list.setText("<i>I/O system not available</i>")
            return
        
        entity_type = get_entity_type_for_io(entity)
        inputs = get_input_names(entity_type)
        
        if inputs:
            text = ", ".join(f"<b>{i}</b>" for i in inputs)
        else:
            text = "<i>No inputs defined</i>"
        
        self.inputs_list.setText(text)