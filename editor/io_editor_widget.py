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
    QMessageBox, QMenu, QAction
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


class IOConnectionDialog(QDialog):
    """Dialog for adding/editing a single I/O connection."""
    
    def __init__(self, parent=None, entity=None, editor_state=None, 
                 existing_connection=None):
        super().__init__(parent)
        self.entity = entity
        self.editor_state = editor_state
        self.existing_connection = existing_connection
        
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
        self.target_edit.setPlaceholderText("Target entity name...")
        
        all_names = self._get_all_entity_names()
        completer = QCompleter(all_names)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.target_edit.setCompleter(completer)
        
        target_row = QHBoxLayout()
        target_row.addWidget(self.target_edit)
        pick_btn = QPushButton("Pick")
        pick_btn.setFixedWidth(50)
        pick_btn.setToolTip("Click an entity in the viewport")
        pick_btn.clicked.connect(self._start_pick_mode)
        #target_row.addWidget(pick_btn)
        
        form_layout.addRow("Target Entity:", target_row)
        
        # Input selector
        self.input_combo = QComboBox()
        self.input_combo.setEditable(True)
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
            "on the entity named <b>Target Entity</b>.</i>"
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
    
    def _update_input_options(self, target_name):
        self.input_combo.clear()
        
        if not self.editor_state or not target_name:
            return
        
        target_type = None
        
        for brush in self.editor_state.brushes:
            if brush.get('name') == target_name:
                target_type = get_entity_type_for_io(brush)
                break
        
        if not target_type:
            for thing in self.editor_state.things:
                if thing.properties.get('name') == target_name:
                    target_type = get_entity_type_for_io(thing)
                    break
        
        if target_type:
            inputs = get_input_names(target_type)
            self.input_combo.addItems(inputs)
    
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
    
    def get_connection(self):
        return OutputConnection(
            output_name=self.output_combo.currentText().strip(),
            target_name=self.target_edit.text().strip(),
            input_name=self.input_combo.currentText().strip(),
            parameter=self.param_edit.text(),
            delay=self.delay_spin.value(),
            fire_once=self.fire_once_check.isChecked()
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

        # Spacer to push console button to the right
        btn_layout.addStretch()

        # OPEN CONSOLE Button
        self.console_btn = QPushButton("Debug Console")
        self.console_btn.setToolTip("Open the Debug Console")
        self.console_btn.setStyleSheet("font-weight: bold;")
        self.console_btn.clicked.connect(self._open_console)
        btn_layout.addWidget(self.console_btn)
        
        layout.addLayout(btn_layout)
        
        self.table.itemSelectionChanged.connect(self._update_button_states)
        self._update_button_states()
    
    def set_entity(self, entity):
        self.current_entity = entity
        self._refresh_table()
    
    def _refresh_table(self):
        self.table.setRowCount(0)
        
        if not self.current_entity:
            return
        
        connections = get_connections(self.current_entity)
        
        for conn in connections:
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            self.table.setItem(row, 0, QTableWidgetItem(conn.output_name))
            
            target_item = QTableWidgetItem(conn.target_name)
            if not self._target_exists(conn.target_name):
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
            fire_once=original.fire_once
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
        """Open the debug console if available on the editor."""
        if self.editor and hasattr(self.editor, 'debug_console'):
            # Ensure it is visible
            self.editor.debug_console.show()
            self.editor.debug_console.raise_()
            self.editor.debug_console.activateWindow()


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