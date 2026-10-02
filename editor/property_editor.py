import os
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QSpinBox,
                             QFormLayout, QCheckBox, QComboBox, QPushButton,
                             QHBoxLayout, QColorDialog, QFileDialog, QGridLayout,
                             QToolButton, QSlider, QTabWidget, QGroupBox, QScrollArea,
                             QFrame, QDoubleSpinBox, QSizePolicy)
from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QFont
from editor.things import (Thing, Light, Pickup, Monster, Model, Speaker,
                           LogicGate, PathNode, LogicCamera, LogicSpawner, Portal,
                           LogicKeyValueStore)
from engine.monster_constants import MONSTER_VARIANTS

# I/O System imports
try:
    from editor.io_editor_widget import IOEditorWidget, IOInputsWidget
    from editor.io_system import get_entity_type_for_io, IO_REGISTRY
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False


# ────────────────────────────
# Centralised styles
# ────────────────────────────
class _Style:
    TAB_BAR = """
        QTabBar::tab:selected { background: #F08000; color: white; }
        QTabBar::tab { background: #425f5d; color: #ccc; padding: 8px 16px; border: 1px solid #333; }
        QTabBar::tab:hover { background: #5a7a82; }
    """
    SCROLL_V = """
        QScrollBar:vertical { width: 18px; background: #2b2b2b; border: none; margin: 0px; }
        QScrollBar::handle:vertical { background: #4b4d4d; min-height: 20px; border-radius: 4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    """
    CHECKBOX = """
        QCheckBox::indicator:checked { background-color: #F08000; border: 1px solid #333; }
        QCheckBox::indicator:unchecked { background-color: #425f5d; border: 1px solid #333; }
        QCheckBox::indicator { width: 22px; height: 22px; }
    """
    HEADER = "QLabel {{ background-color: {color}; color: white; font-weight: bold; padding: 8px 12px; border-radius: 4px; font-size: 12px; }}"
    SECTION = "QLabel { color: #F08000; font-weight: bold; padding: 4px 0px; border-bottom: 1px solid #F08000; margin-top: 8px; }"
    NAME_LBL = "QLabel { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px 8px; border-radius: 3px; }"
    NAME_INP = ("QLineEdit { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px; "
                "border: 2px solid #8B5AC2; border-radius: 3px; } "
                "QLineEdit:focus { border: 2px solid #A875D6; background-color: #7B4AB9; }")
    TARGETED = ("QLabel { color: #00FF00; font-weight: bold; padding: 2px; "
                "background-color: #1a3d1a; border: 1px solid #00AA00; border-radius: 3px; }")
    ID_BTN = ("QPushButton { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px 8px; "
              "border: 2px solid #8B5AC2; border-radius: 3px; } "
              "QPushButton:hover { background-color: #7B4AB9; border: 2px solid #A875D6; } "
              "QPushButton:pressed { background-color: #5A2E96; }")
    ID_FIELD = ("QLineEdit { background-color: #6C3BAA; color: white; font-weight: bold; padding: 6px; "
                "border: 2px solid #8B5AC2; border-radius: 3px; font-family: monospace; } "
                "QLineEdit:focus { border: 2px solid #A875D6; background-color: #7B4AB9; }")

    @staticmethod
    def group_box(color: str, title_bg: str = "#2b3d3b") -> str:
        return (f"QGroupBox {{ font-weight: bold; color: {color}; border: 1px solid {color}; "
                f"border-radius: 4px; margin-top: 12px; padding-top: 8px; }}"
                f"QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 8px; "
                f"padding: 0 4px; background-color: {title_bg}; }}")


# ────────────────────────────
# Widget factories
# ────────────────────────────
def _hbox(*widgets, stretch=True, margins=(0, 0, 0, 0), spacing=4) -> QHBoxLayout:
    """Helper to build a horizontal layout with common defaults."""
    lay = QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        lay.addWidget(w)
    if stretch:
        lay.addStretch()
    return lay


def _make_slider(parent, value: float, range0: int, range1: int, fmt: str = "{:.2f}",
                 callback=None, tooltip: str = "") -> tuple[QSlider, QLabel]:
    """Return a slider + value-label pair wired to a callback."""
    slider = QSlider(Qt.Horizontal)
    slider.setRange(range0, range1)
    slider.setValue(int(value * (100 if range1 <= 100 else 1)))
    label = QLabel(fmt.format(value))
    if tooltip:
        slider.setToolTip(tooltip)

    def _on_change(v):
        real = v / (100 if range1 <= 100 else 1)
        label.setText(fmt.format(real))
        if callback:
            callback(real)

    slider.valueChanged.connect(_on_change)
    return slider, label


def _make_checkbox(label: str, checked: bool, callback, style=None) -> QCheckBox:
    cb = QCheckBox(label)
    cb.setChecked(checked)
    if style:
        cb.setStyleSheet(style)
    if callback is not None:
        cb.toggled.connect(callback)
    return cb


class ClickableComboBox(QComboBox):
    """QComboBox that toggles its dropdown on any click, not just the arrow."""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Toggle popup: if already open, hide it; otherwise show it
            if self.view().isVisible():
                self.hidePopup()
            else:
                self.showPopup()
        else:
            super().mousePressEvent(event)


def _make_combo(items, current, callback=None, editable=False, tooltip="") -> "ClickableComboBox":
    c = ClickableComboBox()
    c.setEditable(editable)
    c.addItems(items)
    if current in items:
        c.setCurrentText(current)
    if callback:
        c.currentTextChanged.connect(callback)
    if tooltip:
        c.setToolTip(tooltip)
    return c


def _make_spin(value, range0, range1, suffix="", decimals=0, step=1, callback=None, tooltip=""):
    if decimals:
        s = QDoubleSpinBox()
        s.setDecimals(decimals)
        s.setSingleStep(step)
    else:
        s = QSpinBox()
        s.setSingleStep(step)
    s.setRange(range0, range1)
    # Qt spin boxes are backed by a C int/double; a property that somehow holds a
    # value outside the widget's range (e.g. a hand-edited or imported int beyond
    # 2**31) would raise OverflowError from setValue. Clamp defensively so a stray
    # value can never crash the property panel — the stored data is untouched
    # unless the user actually commits an edit.
    try:
        if value < range0:
            value = range0
        elif value > range1:
            value = range1
    except TypeError:
        value = range0
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    if tooltip:
        s.setToolTip(tooltip)
    if callback:
        s.valueChanged.connect(callback)
    return s


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
        self._populating = False
        self._widgets: dict[str, QWidget] = {}
        self._linked_key_pickup = None
        self._linked_door_brush = None
        self.tab_widget = None

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(2)
        self.setLayout(self.main_layout)
        self.set_object(None)

    # ────────────────────────────
    # Internal helpers
    # ────────────────────────────
    def _find_targeting_sources(self, target_name: str, target_id: str = ""):
        """
        Find entities whose I/O connections point at this entity.

        A connection matches if its stored target_id equals target_id
        (identity-addressed) OR its target_name equals target_name (legacy /
        name-addressed). This keeps the "Targeted by" list correct even when
        the target has been renamed.
        """
        if not target_name and not target_id:
            return []
        sources = []

        def _conn_matches(conn):
            cid = getattr(conn, 'target_id', None)
            if cid is None and isinstance(conn, dict):
                cid = conn.get('target_id')
            if target_id and cid and cid == target_id:
                return True
            t = getattr(conn, 'target_name', None) or (conn.get('target') if isinstance(conn, dict) else None)
            return bool(target_name) and t == target_name

        for brush in self.editor.state.brushes:
            if target_name and brush.get('target') == target_name:
                src_type = 'trigger' if brush.get('is_trigger') else 'mover' if brush.get('is_mover') else None
                if src_type:
                    sources.append((brush.get('name', 'unnamed'), src_type))
            for conn in brush.get('_io_connections', []):
                o = getattr(conn, 'output_name', None) or (conn.get('output', '?') if isinstance(conn, dict) else '?')
                if _conn_matches(conn):
                    sources.append((brush.get('name', 'unnamed'), f"I/O: {o}"))
        for thing in self.editor.state.things:
            for conn in thing.properties.get('_io_connections', []):
                o = getattr(conn, 'output_name', None) or (conn.get('output', '?') if isinstance(conn, dict) else '?')
                if _conn_matches(conn):
                    sources.append((thing.properties.get('name', 'unnamed'), f"I/O: {o}"))
        return sources

    def _check_target_exists(self, target_name: str) -> bool:
        if not target_name:
            return False
        for brush in self.editor.state.brushes:
            if brush.get('name') == target_name:
                return True
        for thing in self.editor.state.things:
            if (getattr(thing, 'name', '') or thing.properties.get('name', '')) == target_name:
                return True
        return False

    def clear_layout(self):
        while self.main_layout.count():
            child = self.main_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                while child.layout().count():
                    item = child.layout().takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
        self._widgets.clear()
        self.tab_widget = None

    def set_object(self, obj):
        saved_tab_index = None
        saved_scroll_pos = 0
        if self.current_object is obj and self.tab_widget is not None:
            saved_tab_index = self.tab_widget.currentIndex()
            for i in range(self.main_layout.count()):
                w = self.main_layout.itemAt(i).widget()
                if isinstance(w, QScrollArea):
                    saved_scroll_pos = w.verticalScrollBar().value()
                    break

        if obj is None and self.current_object is not None:
            if hasattr(self.editor, 'properties_tab_widget'):
                prev = getattr(self.editor, '_previous_tab_index', None)
                if prev is not None and self.editor.properties_tab_widget.currentIndex() == 0:
                    self.editor.properties_tab_widget.setCurrentIndex(prev)

        self._populating = True
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

        if saved_tab_index is not None and self.tab_widget is not None:
            if saved_tab_index < self.tab_widget.count():
                self.tab_widget.setCurrentIndex(saved_tab_index)

        if saved_scroll_pos > 0:
            for i in range(self.main_layout.count()):
                w = self.main_layout.itemAt(i).widget()
                if isinstance(w, QScrollArea):
                    QTimer.singleShot(0, lambda w=w, p=saved_scroll_pos: w.verticalScrollBar().setValue(p))
                    break

        self._populating = False

    # ────────────────────────────
    # Brush population
    # ────────────────────────────
    def populate_for_brush(self, brush):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.verticalScrollBar().setStyleSheet(_Style.SCROLL_V)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Name row
        form = QFormLayout()
        form.setSpacing(4)
        name_lbl = QLabel("Name:")
        name_lbl.setStyleSheet(_Style.NAME_LBL)
        name_inp = QLineEdit(brush.get('name', ''))
        name_inp.setStyleSheet(_Style.NAME_INP)
        name_inp.setPlaceholderText("Enter name...")
        name_inp.editingFinished.connect(lambda: self.update_object_prop('name', name_inp.text()))

        id_btn = QPushButton("ID")
        id_btn.setStyleSheet(_Style.ID_BTN)
        id_btn.setFixedWidth(32)
        id_btn.setToolTip("Show/hide UUID")

        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(name_inp, 1)
        name_row.addWidget(id_btn)

        form.addRow(name_lbl, name_row)
        self._widgets['name_input'] = name_inp

        uuid_field = QLineEdit(brush.get('id', ''))
        uuid_field.setStyleSheet(_Style.ID_FIELD)
        uuid_field.setReadOnly(True)
        uuid_field.setVisible(False)
        form.addRow("", uuid_field)
        self._widgets['uuid_field'] = uuid_field

        id_btn.clicked.connect(lambda: uuid_field.setVisible(not uuid_field.isVisible()))

        brush_name = brush.get('name', '')
        if brush_name or brush.get('id'):
            sources = self._find_targeting_sources(brush_name, brush.get('id', ''))
            if sources:
                txt = ", ".join(f"{n} ({t})" for n, t in sources)
                lbl = QLabel(txt)
                lbl.setStyleSheet(_Style.TARGETED)
                lbl.setWordWrap(True)
                form.addRow("Targeted by:", lbl)

        layout.addLayout(form)

        # Tabs
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(_Style.TAB_BAR)

        is_trigger = brush.get('is_trigger', False)
        is_mover = brush.get('is_mover', False)
        is_door = brush.get('is_door', False)
        shader_type = brush.get('shader', '<None>')

        self.tab_widget.addTab(self._create_general_tab(brush), "General")

        self.trigger_tab = self._create_trigger_tab(brush)
        self.trigger_tab_index = self.tab_widget.addTab(self.trigger_tab, "🎯 Trigger")
        self.tab_widget.setTabVisible(self.trigger_tab_index, is_trigger)

        self.mover_tab = self._create_mover_tab(brush)
        self.mover_tab_index = self.tab_widget.addTab(self.mover_tab, "⚡ Mover")
        self.tab_widget.setTabVisible(self.mover_tab_index, is_mover)

        self.door_tab = self._create_door_tab(brush)
        self.door_tab_index = self.tab_widget.addTab(self.door_tab, "🚪 Door")
        self.tab_widget.setTabVisible(self.door_tab_index, is_door)

        self.shader_tab = self._create_shader_tab(brush)
        self.shader_tab_index = self.tab_widget.addTab(self.shader_tab, "✨ Shader")
        self.tab_widget.setTabVisible(self.shader_tab_index, shader_type not in ('<<None>', None, ''))

        self.tab_widget.addTab(self._create_appearance_tab(brush), "Appearance")

        self.io_tab_index = None
        self.io_tab = None
        if IO_AVAILABLE and (is_trigger or is_mover or is_door):
            self.io_tab = self._create_io_tab_for_brush(brush)
            self.io_tab_index = self.tab_widget.addTab(self.io_tab, "⚡ I/O")

        layout.addWidget(self.tab_widget)
        layout.addStretch()
        scroll.setWidget(content)
        self.main_layout.addWidget(scroll)

    def _create_io_tab_for_brush(self, brush):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)

        entity_type = 'trigger' if brush.get('is_trigger') else 'door' if brush.get('is_door') else 'mover' if brush.get('is_mover') else 'brush'
        io_editor = IOEditorWidget(entity=brush, entity_type=entity_type,
                                   editor_state=self.editor.state, editor=self.editor)
        io_editor.connections_changed.connect(self._on_io_connections_changed)
        layout.addWidget(io_editor)
        self._widgets['io_editor'] = io_editor

        inputs = IOInputsWidget()
        inputs.set_entity(entity_type)
        layout.addWidget(inputs)
        return tab

    def _ensure_io_tab(self):
        if not IO_AVAILABLE or self.io_tab_index is not None or self.current_object is None:
            return
        if not any(self.current_object.get(k) for k in ('is_trigger', 'is_mover', 'is_door')):
            return
        self.io_tab = self._create_io_tab_for_brush(self.current_object)
        self.io_tab_index = self.tab_widget.addTab(self.io_tab, "⚡ I/O")

    def _remove_io_tab(self):
        if hasattr(self, 'io_tab_index') and self.io_tab_index is not None:
            self.tab_widget.removeTab(self.io_tab_index)
            self.io_tab_index = None
            if hasattr(self, 'io_tab'):
                self.io_tab.deleteLater()
                self.io_tab = None

    def _update_io_tab_presence(self):
        if not IO_AVAILABLE:
            return
        needs = any(self.current_object.get(k) for k in ('is_trigger', 'is_mover', 'is_door'))
        has = self.io_tab_index is not None
        if needs and not has:
            self._ensure_io_tab()
        elif not needs and has:
            self._remove_io_tab()

    def _create_io_tab_for_thing(self, thing):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        etype = get_entity_type_for_io(thing)
        io_editor = IOEditorWidget(entity=thing, entity_type=etype,
                                   editor_state=self.editor.state, editor=self.editor)
        io_editor.connections_changed.connect(self._on_io_connections_changed)
        layout.addWidget(io_editor)
        self._widgets['io_editor'] = io_editor
        inputs = IOInputsWidget()
        inputs.set_entity(etype)
        layout.addWidget(inputs)
        return tab

    def _on_io_connections_changed(self):
        if hasattr(self.editor.state, 'save_state'):
            self.editor.state.save_state()
        if hasattr(self.editor, 'mark_dirty'):
            self.editor.mark_dirty()
        if not self._populating:
            self.editor.update_all_ui()

    # ────────────────────────────
    # Brush tabs
    # ────────────────────────────
    def _create_general_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)

        type_group = QGroupBox("Brush Type")
        type_group.setStyleSheet(_Style.group_box("#F08000"))
        type_layout = QVBoxLayout(type_group)

        # Shader + Lock row
        shader_row = _hbox(QLabel("Shader:"), stretch=False)
        shader_combo = _make_combo(['<None>', 'Glass', 'Glow', 'Water', 'Fog'],
                                   brush.get('shader', '<None>'),
                                   self.on_shader_changed)
        shader_row.addWidget(shader_combo)
        shader_row.addStretch()
        lock_cb = _make_checkbox("Prevent selection/editing", brush.get('lock', False),
                                 lambda c: self.update_object_prop('lock', c), _Style.CHECKBOX)
        shader_row.addWidget(QLabel("Lock:"))
        shader_row.addWidget(lock_cb)
        type_layout.addLayout(shader_row)
        self._widgets['shader_combo'] = shader_combo
        self._widgets['lock_cb'] = lock_cb

        type_layout.addWidget(self._section("Behaviors"))
        for key, label, tip in (
            ('is_trigger', "Is Trigger (activates other objects)", self.on_trigger_changed),
            ('is_mover', "Is Mover (moves back and forth)", self.on_mover_changed),
            ('is_door', "Is Door (opens when triggered)", self.on_door_changed),
        ):
            cb = _make_checkbox(label, brush.get(key, False), tip, _Style.CHECKBOX)
            type_layout.addWidget(cb)
            self._widgets[f'{key}_cb'] = cb

        type_layout.addWidget(self._section("Grouping"))
        group_inp = QLineEdit(brush.get('brush_group', ''))
        group_inp.setPlaceholderText("e.g. front_door")
        group_inp.setToolTip("Assign the same group name to a door/mover and its companion brushes.")
        group_inp.editingFinished.connect(lambda: self.update_object_prop('brush_group', group_inp.text().strip()))
        type_layout.addLayout(_hbox(QLabel("Brush Group:"), group_inp, stretch=False))
        self._widgets['brush_group_input'] = group_inp

        layout.addWidget(type_group)
        layout.addStretch()
        return w

    def _section(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_Style.SECTION)
        return lbl

    def _create_trigger_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        form.setSpacing(8)

        type_combo = _make_combo(['Once', 'Multiple'], brush.get('trigger_type', 'Once'),
                                 lambda t: self.update_object_prop('trigger_type', t))
        form.addRow("Trigger Type:", type_combo)
        self._widgets['trigger_type_combo'] = type_combo

        # Activation mode: touch fires on entry; use requires E press while inside
        activation_combo = _make_combo(
            ['touch', 'use'],
            brush.get('trigger_activation', 'touch'),
            tooltip="touch — fires when player walks inside\nuse — fires when player presses E while inside"
        )
        form.addRow("Activation:", activation_combo)
        self._widgets['trigger_activation_combo'] = activation_combo

        # Use Label: custom HUD prompt shown when activation == 'use'
        use_label_lbl = QLabel("Use Label:")
        use_label_input = QLineEdit(brush.get('use_label', ''))
        use_label_input.setPlaceholderText("Activate")
        use_label_input.editingFinished.connect(
            lambda: self.update_object_prop('use_label', use_label_input.text().strip()))
        is_use_mode = brush.get('trigger_activation', 'touch') == 'use'
        use_label_lbl.setVisible(is_use_mode)
        use_label_input.setVisible(is_use_mode)
        form.addRow(use_label_lbl, use_label_input)
        self._widgets['trigger_use_label_lbl'] = use_label_lbl
        self._widgets['trigger_use_label_input'] = use_label_input

        def _on_activation_changed(val):
            self.update_object_prop('trigger_activation', val)
            show = (val == 'use')
            use_label_lbl.setVisible(show)
            use_label_input.setVisible(show)

        activation_combo.currentTextChanged.connect(_on_activation_changed)

        action_combo = _make_combo(['target', 'hurt', 'teleport'],
                                   brush.get('trigger_action', 'target'),
                                   tooltip="target — fire I/O outputs\nhurt — damage player\nteleport — move player to PathNode")
        form.addRow("Action:", action_combo)
        self._widgets['trigger_action_combo'] = action_combo

        # Target node (teleport only)
        node_lbl = QLabel("Target Node:")
        node_combo = self._pathnode_combo(brush.get('target_node', ''))
        node_combo.currentTextChanged.connect(
            lambda t: self.update_object_prop('target_node', '' if t.strip() == '(none)' else t.strip()))
        is_teleport = brush.get('trigger_action', 'target') == 'teleport'
        node_lbl.setVisible(is_teleport)
        node_combo.setVisible(is_teleport)
        form.addRow(node_lbl, node_combo)
        self._widgets['trigger_target_node_label'] = node_lbl
        self._widgets['trigger_target_node_combo'] = node_combo

        def _on_action_changed(txt):
            self.update_object_prop('trigger_action', txt)
            show = txt == 'teleport'
            node_lbl.setVisible(show)
            node_combo.setVisible(show)
            if txt == 'hurt':
                self.update_object_prop('hurt', True)
            elif brush.get('trigger_action') == 'hurt':
                self.update_object_prop('hurt', False)

        action_combo.currentTextChanged.connect(_on_action_changed)

        layout.addLayout(form)

        # Damage group
        dmg_group = QGroupBox("Damage")
        dmg_group.setStyleSheet(_Style.group_box("#F08000"))
        dmg_layout = QVBoxLayout(dmg_group)
        hurt_cb = _make_checkbox("Hurts player on contact", brush.get('hurt', False),
                                 self.on_hurt_changed, _Style.CHECKBOX)
        dmg_layout.addWidget(hurt_cb)
        self._widgets['hurt_cb'] = hurt_cb

        dmg_spin = _make_spin(brush.get('hurt_amount', 10), 1, 1000)
        dmg_spin.setEnabled(brush.get('hurt', False))
        dmg_spin.editingFinished.connect(lambda: self.update_object_prop('hurt_amount', dmg_spin.value()))
        dmg_layout.addLayout(_hbox(QLabel("Damage Amount:"), dmg_spin, stretch=False))
        self._widgets['damage_spin'] = dmg_spin

        layout.addWidget(dmg_group)
        layout.addStretch()
        return w

    def _create_mover_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)

        preview_btn = self._preview_button("▶ Preview Movement", self.toggle_mover_preview)
        layout.addWidget(preview_btn)
        self._widgets['mover_preview_btn'] = preview_btn

        form = QFormLayout()
        form.setSpacing(8)

        for key, label, default in (('speed', "Speed:", 64.0), ('distance', "Distance:", 128.0)):
            inp = QLineEdit(str(brush.get(key, default)))
            inp.editingFinished.connect(lambda k=key, le=inp, d=default:
                                      self.update_object_prop(k, float(le.text()) if le.text() else d))
            form.addRow(label, inp)
            self._widgets[f'{key}_input'] = inp

        # Direction vector
        dir_vec = brush.get('direction', [0, 1, 0])
        dir_widget, dir_inputs = self._vec3_row(dir_vec, lambda v: self.update_object_prop('direction', v))
        form.addRow("Direction:", dir_widget)
        self._widgets['dir_x'], self._widgets['dir_y'], self._widgets['dir_z'] = dir_inputs

        layout.addLayout(form)

        # PathNode waypoint
        path_group = QGroupBox("PathNode Waypoint")
        path_group.setStyleSheet(_Style.group_box("#26A69A", "#1a2f2d"))
        path_form = QFormLayout(path_group)
        path_target = self._pathnode_combo(brush.get('path_target', ''))
        path_target.currentTextChanged.connect(
            lambda t: self.update_object_prop('path_target', '' if t.strip() == '(none)' else t.strip()))
        path_form.addRow("Path Target:", path_target)
        self._widgets['mover_path_target_combo'] = path_target
        layout.addWidget(path_group)

        # Options
        opt_group = QGroupBox("Options")
        opt_group.setStyleSheet(_Style.group_box("#F08000"))
        opt_layout = QVBoxLayout(opt_group)

        start_cb = _make_checkbox("Start moving immediately", brush.get('start_on', False),
                                  lambda c: self.update_object_prop('start_on', c), _Style.CHECKBOX)
        opt_layout.addWidget(start_cb)
        self._widgets['start_on_cb'] = start_cb

        rotate_cb = _make_checkbox("Rotate continuously (func_rotating)", brush.get('rotate', False),
                                   None, _Style.CHECKBOX)
        opt_layout.addWidget(rotate_cb)
        self._widgets['rotate_cb'] = rotate_cb

        rot_axis = brush.get('rot_axis', [0, 1, 0])
        rot_widget, rot_inputs = self._vec3_row(rot_axis, lambda v: self.update_object_prop('rot_axis', v),
                                                indent=16)
        opt_layout.addWidget(rot_widget)
        self._widgets['rot_ax'], self._widgets['rot_ay'], self._widgets['rot_az'] = rot_inputs

        def on_rotate(checked):
            self.update_object_prop('rotate', checked)
            rot_widget.setVisible(checked)
            if checked:
                self.update_object_prop('direction', [0, 0, 0])
                for k, v in (('dir_x', '0'), ('dir_y', '0'), ('dir_z', '0')):
                    if k in self._widgets:
                        self._widgets[k].setText(v)
            else:
                self.update_object_prop('direction', [0, 1.0, 0])
                for k, v in (('dir_x', '0'), ('dir_y', '1.0'), ('dir_z', '0')):
                    if k in self._widgets:
                        self._widgets[k].setText(v)
            if 'distance_input' in self._widgets:
                self._widgets['distance_input'].setEnabled(not checked)
            for k in ('dir_x', 'dir_y', 'dir_z'):
                if k in self._widgets:
                    self._widgets[k].setEnabled(not checked)

        rotate_cb.toggled.connect(on_rotate)
        rot_widget.setVisible(brush.get('rotate', False))

        layout.addWidget(opt_group)
        layout.addStretch()
        return w

    def _create_door_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)

        preview_btn = self._preview_button("▶ Preview Door", self.toggle_door_preview)
        layout.addWidget(preview_btn)
        self._widgets['door_preview_btn'] = preview_btn

        form = QFormLayout()
        form.setSpacing(8)

        dir_combo = _make_combo(['up', 'down', 'north', 'south', 'east', 'west'],
                                brush.get('door_direction', 'up'),
                                lambda t: self.update_object_prop('door_direction', t))
        form.addRow("Open Direction:", dir_combo)
        self._widgets['door_dir_combo'] = dir_combo

        for key, label, default in (('door_distance', "Open Distance:", 128.0),
                                    ('door_lip', "Lip (stay closed):", 8.0),
                                    ('door_speed', "Speed:", 64.0)):
            inp = QLineEdit(str(brush.get(key, default)))
            inp.editingFinished.connect(lambda k=key, le=inp, d=default:
                                      self.update_object_prop(k, float(le.text()) if le.text() else d))
            form.addRow(label, inp)
            self._widgets[f'{key}_input'] = inp

        layout.addLayout(form)

        opt_group = QGroupBox("Door Options")
        opt_group.setStyleSheet(_Style.group_box("#F08000"))
        opt_layout = QVBoxLayout(opt_group)

        for key, label in (('door_auto_open', "Auto-open when player is near"),
                           ('door_locked', "Locked (requires trigger to open)"),
                           ('door_needs_key', "Requires key to open")):
            cb = _make_checkbox(label, brush.get(key, False),
                                getattr(self, f'on_{key}_changed', None) or (lambda c, k=key: self.update_object_prop(k, c)),
                                _Style.CHECKBOX)
            opt_layout.addWidget(cb)
            self._widgets[f'{key}_cb'] = cb

        # Key dropdown
        key_lbl = QLabel("Key Name:")
        key_combo = _make_combo(['red_key', 'blue_key', 'yellow_key', 'custom'],
                                brush.get('door_key_name', 'red_key'),
                                lambda t: self.update_object_prop('door_key_name', t))
        key_lbl.setVisible(brush.get('door_needs_key', False))
        key_combo.setVisible(brush.get('door_needs_key', False))
        opt_layout.addLayout(_hbox(key_lbl, key_combo, stretch=False))
        self._widgets['door_key_input'] = key_combo
        self._widgets['door_key_label'] = key_lbl

        # Link label + select button
        link_lbl = QLabel("")
        link_lbl.setWordWrap(True)
        link_lbl.setStyleSheet("QLabel { padding: 4px; }")
        opt_layout.addWidget(link_lbl)
        self._widgets['door_key_link_label'] = link_lbl

        sel_btn = QPushButton("Select Key Pickup ▸")
        sel_btn.setStyleSheet("""
            QPushButton { background-color: #2a5a2a; color: #88FF88; border: 1px solid #44AA44;
                          border-radius: 3px; padding: 4px 8px; font-size: 11px; }
            QPushButton:hover { background-color: #3a6a3a; }
        """)
        sel_btn.setVisible(False)
        sel_btn.clicked.connect(self._select_linked_key_pickup)
        opt_layout.addWidget(sel_btn)
        self._widgets['door_key_select_btn'] = sel_btn

        key_combo.currentTextChanged.connect(lambda _: self._update_door_key_link(brush))
        self._update_door_key_link(brush)

        layout.addWidget(opt_group)
        layout.addStretch()
        return w

    def _create_shader_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        stype = brush.get('shader', '<None>')
        factory = {
            'Glass': self._create_glass_properties,
            'Glow': self._create_glow_properties,
            'Water': self._create_water_properties,
            'Fog': self._create_fog_properties,
        }.get(stype)
        if factory:
            layout.addWidget(factory(brush))
        else:
            layout.addWidget(QLabel("No shader-specific properties."))
        layout.addStretch()
        return w

    def _create_glass_properties(self, brush):
        group = QGroupBox("Glass Properties")
        group.setStyleSheet(_Style.group_box("#00BFFF"))
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setSpacing(6)

        color_btn = self._color_button(brush.get('glass_color', [0.9, 0.95, 1.0]),
                                       lambda: self._pick_color('glass_color', color_btn, [0.9, 0.95, 1.0]))
        form.addRow("Tint Color:", color_btn)
        self._widgets['glass_color_btn'] = color_btn

        slider, label = _make_slider(self, brush.get('glass_opacity', 0.3), 0, 100,
                                     callback=lambda v: self.update_object_prop('glass_opacity', v))
        form.addRow("Opacity:", _hbox(slider, label, stretch=False))

        layout.addLayout(form)

        # Distortion
        dist_group = QGroupBox("Distortion Effects")
        dist_group.setStyleSheet(_Style.group_box("#87CEEB", "#2b3d3b"))
        dist_form = QFormLayout(dist_group)
        for key, label_txt, default, tip in (
            ('glass_distortion', "Warp Strength:", 0.5, "How much the view through glass is warped"),
            ('glass_refraction', "Refraction:", 1.5, "Index of refraction (1.0=air, 1.5=glass, 2.4=diamond)"),
            ('glass_roughness', "Roughness:", 0.0, "Surface roughness (0=clear, 1=frosted)"),
        ):
            slider, label = _make_slider(self, brush.get(key, default), 0, 100 if 'refraction' not in key else 250,
                                         fmt="{:.2f}", callback=lambda v, k=key: self.update_object_prop(k, v),
                                         tooltip=tip)
            dist_form.addRow(label_txt, _hbox(slider, label, stretch=False))
            self._widgets[f'{key}_slider'] = slider
        layout.addWidget(dist_group)

        # Fresnel
        fres_group = QGroupBox("Fresnel Effect")
        fres_group.setStyleSheet(_Style.group_box("#98FB98", "#2b3d3b"))
        fres_form = QFormLayout(fres_group)
        slider, label = _make_slider(self, brush.get('glass_fresnel', 0.5), 0, 100,
                                     callback=lambda v: self.update_object_prop('glass_fresnel', v),
                                     tooltip="Edge reflection intensity")
        fres_form.addRow("Intensity:", _hbox(slider, label, stretch=False))
        self._widgets['glass_fresnel_slider'] = slider
        layout.addWidget(fres_group)

        return group

    def _create_glow_properties(self, brush):
        group = QGroupBox("Glow Properties")
        group.setStyleSheet(_Style.group_box("#FFD700"))
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        form = QFormLayout()

        color_btn = self._color_button(brush.get('glow_color', [1.0, 0.9, 0.7]),
                                       lambda: self._pick_color('glow_color', color_btn, [1.0, 0.9, 0.7]))
        form.addRow("Glow Color:", color_btn)
        self._widgets['glow_color_btn'] = color_btn

        slider, label = _make_slider(self, brush.get('glow_intensity', 3.0), 300, 1000,
                                     fmt="{:.2f}", callback=lambda v: self.update_object_prop('glow_intensity', v))
        form.addRow("Intensity:", _hbox(slider, label, stretch=False))
        self._widgets['glow_intensity_slider'] = slider

        layout.addLayout(form)
        return group

    def _create_water_properties(self, brush):
        group = QGroupBox("Water Properties")
        group.setStyleSheet(_Style.group_box("#00CED1"))
        layout = QFormLayout(group)
        layout.setSpacing(8)

        color_btn = self._color_button(brush.get('water_tint', [0.0, 0.4, 0.6]),
                                       lambda: self._pick_color('water_tint', color_btn, [0.0, 0.4, 0.6]))
        layout.addRow("Water Tint:", color_btn)
        self._widgets['water_color_btn'] = color_btn

        for key, label_txt, default in (('water_opacity', "Opacity:", 0.5),
                                        ('water_reflectivity', "Reflectivity:", 0.5)):
            slider, label = _make_slider(self, brush.get(key, default), 0, 100,
                                         callback=lambda v, k=key: self.update_object_prop(k, v))
            layout.addRow(label_txt, _hbox(slider, label, stretch=False))
            self._widgets[f'{key}_slider'] = slider

        wave_cb = _make_checkbox("Enable Rolling Waves", brush.get('water_wave_enabled', True),
                                 lambda c: self.update_object_prop('water_wave_enabled', c), _Style.CHECKBOX)
        layout.addRow("", wave_cb)
        self._widgets['water_wave_cb'] = wave_cb

        # 0..1 fraction; the renderer maps this to world-space wave amplitude
        wave_h = min(float(brush.get('water_wave_height', 0.5)), 1.0)
        slider, label = _make_slider(self, wave_h, 0, 100,
                                     callback=lambda v: self.update_object_prop('water_wave_height', v),
                                     tooltip="Amplitude of the waves")
        layout.addRow("Wave Height:", _hbox(slider, label, stretch=False))
        self._widgets['water_wave_h_slider'] = slider

        plane_cb = _make_checkbox("Draw top surface only", brush.get('water_plane', False),
                                  lambda c: self.update_object_prop('water_plane', c), _Style.CHECKBOX)
        layout.addRow("", plane_cb)
        self._widgets['water_plane_cb'] = plane_cb

        return group

    def _create_fog_properties(self, brush):
        group = QGroupBox("Fog Properties")
        group.setStyleSheet(_Style.group_box("#B0C4DE"))
        layout = QFormLayout(group)
        layout.setSpacing(8)

        color_btn = self._color_button(brush.get('fog_color', [0.5, 0.6, 0.7]),
                                       lambda: self._pick_color('fog_color', color_btn, [0.5, 0.6, 0.7]))
        layout.addRow("Fog Color:", color_btn)
        self._widgets['fog_color_btn'] = color_btn

        density_inp = QLineEdit(str(brush.get('fog_density', 2.0)))
        density_inp.editingFinished.connect(
            lambda: self.update_object_prop('fog_density', float(density_inp.text()) if density_inp.text() else 2.0))
        layout.addRow("Density:", density_inp)
        self._widgets['fog_density_input'] = density_inp

        return group

    def _create_appearance_tab(self, brush):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        form.setSpacing(8)

        current = brush.get('colour', [0.8, 0.8, 0.8])
        btn = self._color_button(current, lambda: self._pick_color('colour', btn, [0.8, 0.8, 0.8]), size=(140, 32))
        reset = QPushButton("Reset")
        reset.setFixedSize(60, 32)
        reset.setToolTip("Reset to default grey")
        reset.clicked.connect(lambda: self._reset_brush_colour(btn))
        form.addRow("Brush Colour:", _hbox(btn, reset, stretch=False))
        self._widgets['brush_colour_btn'] = btn

        layout.addLayout(form)
        layout.addStretch()
        return w

    # ────────────────────────────
    # Thing population
    # ────────────────────────────
    def populate_for_thing(self, thing):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.verticalScrollBar().setStyleSheet(_Style.SCROLL_V)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Name row (shared with brush layout)
        name_layout = QFormLayout()
        name_layout.setSpacing(4)
        name_lbl = QLabel("Name:")
        name_lbl.setStyleSheet(_Style.NAME_LBL)
        name_inp = QLineEdit(str(thing.properties.get('name', '')))
        name_inp.setStyleSheet(_Style.NAME_INP)
        name_inp.setPlaceholderText("Enter name...")
        name_inp.editingFinished.connect(lambda: self.update_object_prop('name', name_inp.text()))

        id_btn = QPushButton("ID")
        id_btn.setStyleSheet(_Style.ID_BTN)
        id_btn.setFixedWidth(32)
        id_btn.setToolTip("Show/hide UUID")

        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(name_inp, 1)
        name_row.addWidget(id_btn)

        name_layout.addRow(name_lbl, name_row)

        uuid_field = QLineEdit(thing.properties.get('id', ''))
        uuid_field.setStyleSheet(_Style.ID_FIELD)
        uuid_field.setReadOnly(True)
        uuid_field.setVisible(False)
        name_layout.addRow("", uuid_field)
        self._widgets['uuid_field'] = uuid_field

        id_btn.clicked.connect(lambda: uuid_field.setVisible(not uuid_field.isVisible()))

        tname = getattr(thing, 'name', '') or thing.properties.get('name', '')
        tid = thing.properties.get('id', '')
        sources = self._find_targeting_sources(tname, tid) if (tname or tid) else []
        if sources:
            txt = ", ".join(f"{n} ({t})" for n, t in sources)
            lbl = QLabel(txt)
            lbl.setStyleSheet(_Style.TARGETED)
            lbl.setWordWrap(True)
            name_layout.addRow("Targeted by:", lbl)

        layout.addLayout(name_layout)

        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(_Style.TAB_BAR)

        props_tab = self._create_thing_properties_tab(thing)
        self.tab_widget.addTab(props_tab, "Properties")

        if IO_AVAILABLE:
            etype = get_entity_type_for_io(thing)
            if etype and etype in IO_REGISTRY:
                self.tab_widget.addTab(self._create_io_tab_for_thing(thing), "⚡ I/O")

        layout.addWidget(self.tab_widget)
        layout.addStretch()
        scroll.setWidget(content)
        self.main_layout.addWidget(scroll)

    def _create_thing_properties_tab(self, thing) -> QWidget:
        w = QWidget()
        tab_layout = QVBoxLayout(w)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(4)
        form = QFormLayout()

        if isinstance(thing, Model):
            self.add_model_path_widget(form, thing)
            self.add_vector3_widget(form, thing, 'scale')
            self.add_vector3_widget(form, thing, 'rotation')

            # Collision toggle for this model entity
            no_collision = thing.properties.get('no_collision', False)
            collision_cb = _make_checkbox("Disable collision for this model", no_collision,
                                           lambda c: self.update_object_prop('no_collision', c),
                                           _Style.CHECKBOX)
            collision_cb.setToolTip("If checked, player and monsters will pass through this model")
            form.addRow("", collision_cb)
            self._widgets['model_no_collision_cb'] = collision_cb

            # Collision size override
            collision_size = thing.properties.get('collision_size')
            cs_widget, cs_inputs = self._vec3_row(
                collision_size if collision_size else [0, 0, 0],
                lambda v: self._on_collision_size_changed(v, thing)
            )
            cs_label = QLabel("Collision Size:")
            cs_label.setToolTip("Custom collision box size (0,0,0 = auto from scale)")
            form.addRow(cs_label, cs_widget)
            self._widgets['model_collision_size_inputs'] = cs_inputs

            if IO_AVAILABLE:
                note = QLabel("💡 Use the I/O tab for advanced targeting")
                note.setStyleSheet("QLabel { color: #88AAFF; font-style: italic; padding: 4px; }")
                form.addRow("", note)

        if isinstance(thing, Light):
            self.add_color_picker_widget(form, thing, 'colour')
            self._build_attach_to_mover(form, thing)

        if isinstance(thing, Portal):
            self._build_attach_to_mover(form, thing, prefix='portal_')
            form.addRow(QLabel(""))  # spacer
            self._build_portal_target(form, thing)

        # Pickup
        if isinstance(thing, Pickup):
            self._build_pickup_ui(form, thing)

        # Dynamic property iteration (excludes keys handled above)
        self._iterate_thing_properties(form, thing)

        tab_layout.addLayout(form)

        if isinstance(thing, PathNode):
            self._build_pathnode_group(tab_layout, thing)
        if isinstance(thing, LogicCamera):
            self._build_logic_camera_group(tab_layout, thing)
        if isinstance(thing, LogicSpawner):
            self._build_spawner_group(tab_layout, thing)
        if isinstance(thing, LogicKeyValueStore):
            self._build_keyvalue_group(tab_layout, thing)
        if isinstance(thing, Monster):
            self._build_monster_groups(tab_layout, thing)

        tab_layout.addStretch()
        return w

    def _build_attach_to_mover(self, form, thing, prefix=''):
        """Shared attach-to-mover logic for Light and Portal."""
        current = thing.properties.get('parent_mover', '')
        is_attached = bool(current)

        cb = _make_checkbox("Attach to Mover", is_attached, None)
        form.addRow("", cb)

        combo = ClickableComboBox()
        combo.addItem("(none)")
        for brush in self.editor.state.brushes:
            if brush.get('is_mover'):
                mname = brush.get('name', '')
                if mname:
                    combo.addItem(mname)
        if current:
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.addItem(current + " (missing)")
                combo.setCurrentIndex(combo.count() - 1)

        lbl = QLabel("Parent Mover:")
        lbl.setVisible(is_attached)
        combo.setVisible(is_attached)

        def on_toggle(checked):
            lbl.setVisible(checked)
            combo.setVisible(checked)
            if not checked:
                thing.properties['parent_mover'] = ''
                thing.properties['parent_offset'] = [0.0, 0.0, 0.0]
                combo.setCurrentIndex(0)
                self.editor.update_all_ui()

        def on_changed(text):
            clean = text.replace(" (missing)", "")
            if clean == "(none)":
                thing.properties['parent_mover'] = ''
                thing.properties['parent_offset'] = [0.0, 0.0, 0.0]
            else:
                thing.properties['parent_mover'] = clean
                for b in self.editor.state.brushes:
                    if b.get('is_mover') and b.get('name') == clean:
                        thing.properties['parent_offset'] = [
                            thing.pos[0] - b['pos'][0],
                            thing.pos[1] - b['pos'][1],
                            thing.pos[2] - b['pos'][2],
                        ]
                        break
            self.editor.update_all_ui()

        cb.toggled.connect(on_toggle)
        combo.currentTextChanged.connect(on_changed)
        form.addRow(lbl, combo)

    def _build_portal_target(self, form, thing):
        current = thing.properties.get('portal_target', '')
        others = [t for t in self.editor.state.things if isinstance(t, Portal) and t is not thing]
        combo = ClickableComboBox()
        combo.addItem("(none)")
        for p in others:
            combo.addItem(p.properties.get('name', ''))
        if current:
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.addItem(current + " (missing)")
                combo.setCurrentIndex(combo.count() - 1)

        def on_changed(text):
            clean = text.replace(" (missing)", "")
            thing.properties['portal_target'] = '' if clean == '(none)' else clean
            self.editor.update_all_ui()

        combo.currentTextChanged.connect(on_changed)

        sel_btn = QPushButton("Select →")
        sel_btn.setMaximumWidth(70)
        sel_btn.setToolTip("Select the linked portal in the viewport")

        def on_select():
            tname = thing.properties.get('portal_target', '')
            for t in self.editor.state.things:
                if isinstance(t, Portal) and t.properties.get('name') == tname:
                    if hasattr(self.editor, 'select_object'):
                        self.editor.select_object(t)
                    else:
                        self.editor.state.selected_object = t
                        self.editor.update_all_ui()
                    break

        sel_btn.clicked.connect(on_select)
        form.addRow("Portal Target:", _hbox(combo, sel_btn, stretch=False))

    def _build_pickup_ui(self, form, thing):
        self._pickup_value_widgets = []
        self._pickup_key_widgets = []
        self._pickup_sprite_widgets = []

    def _iterate_thing_properties(self, form, thing):
        is_pickup = isinstance(thing, Pickup)
        current_item = thing.properties.get('item_type', 'health') if is_pickup else None

        _MONSTER_ONLY = {'awake', 'damage', 'health', 'monster_type', 'variant',
                         'triggered', 'wake_on_sight', 'can_hear', 'dead', 'non_hostile', 'sight',
                         'patrol', 'patrol_target', 'patrol_mode'}

        for key, value in sorted(thing.properties.items()):
            if key in ('name', 'id', '_io_connections', 'type'):
                continue
            if isinstance(thing, Light) and key in ('colour', 'parent_mover', 'parent_offset'):
                continue
            if isinstance(thing, Model) and key in ('model_path', 'scale', 'rotation'):
                continue
            if isinstance(thing, Portal) and key in ('rotation', 'portal_target', 'parent_mover', 'parent_offset', 'parent_local_pos', 'parent_local_yaw'):
                continue
            if not isinstance(thing, Monster) and key in _MONSTER_ONLY:
                continue
            if isinstance(thing, Monster) and key in ('triggered', 'wake_on_sight', 'can_hear', 'dead', 'non_hostile', 'sight', 'patrol', 'patrol_target', 'patrol_mode', 'variant', 'team'):
                continue
            if isinstance(thing, PathNode) and key in ('radius', 'show_radius', 'affects_type', 'next_node', 'wait_time', 'speed', 'patrol_speed'):
                continue
            if isinstance(thing, LogicCamera) and key in ('path_target', 'speed', 'fov_override', 'look_ahead'):
                continue
            if isinstance(thing, LogicSpawner) and key in ('spawn_type', 'target_node', 'max_spawn', 'spawn_properties'):
                continue
            if isinstance(thing, LogicKeyValueStore) and key in ('store_name', 'initial_data', '_runtime_data'):
                continue
            if is_pickup and key in ('key_name', 'custom_sprite', 'respawns', 'respawn_time'):
                continue
            if isinstance(thing, Light) and key == 'show_radius':
                # Force boolean checkbox, convert string "True"/"False" to bool
                bool_val = value
                if isinstance(value, str):
                    bool_val = value.lower() == 'true'
                cb = _make_checkbox("Show Radius", bool_val,
                                    lambda c, k=key: self.update_object_prop(k, c),
                                    _Style.CHECKBOX)
                form.addRow(label_text, cb)
                self._widgets['light_show_radius_cb'] = cb
                continue

            label_text = "Visible:" if key == 'show_rim' else key.replace('_', ' ').title() + ":"

            if key == 'angle' and isinstance(thing, Portal):
                spin = _make_spin(int(float(value)) % 360, 0, 359, suffix="°", step=45)
                spin.setWrapping(True)
                spin.setToolTip("Portal facing direction in degrees")
                spin.valueChanged.connect(lambda v: self.update_object_prop('angle', v))
                form.addRow(label_text, spin)
            elif key == 'angle':
                combo = _make_combo(['0°', '90°', '180°', '270°'],
                                    f"{int(float(value)) % 360}°",
                                    lambda t: self.update_object_prop('angle', int(t.replace('°', ''))))
                form.addRow(label_text, combo)
            elif isinstance(thing, Monster) and key == 'monster_type':
                self._build_monster_type_row(form, thing)
            elif isinstance(thing, Light) and key == 'state':
                combo = _make_combo(['on', 'off'], value, lambda t: self.update_object_prop(key, t))
                form.addRow(label_text, combo)
            elif isinstance(thing, Light) and key == 'shadow_map_size':
                cur = str(thing.get_shadow_map_size())
                combo = _make_combo(
                    ['256', '512', '1024', '2048'], cur,
                    lambda t: self.update_object_prop('shadow_map_size', int(t)),
                    tooltip=("Per-face shadow cube-map resolution for this light.\n"
                             "Higher = sharper shadow edges, but ~4x the VRAM and\n"
                             "fill cost per step. Only used when 'Casts Shadows' is on."))
                form.addRow("Shadow Map Size:", combo)
            elif isinstance(thing, Speaker) and key == 'sound_file':
                self.add_sound_file_widget(form, thing, key, value)
            elif isinstance(thing, LogicGate) and key == 'logic_type':
                combo = _make_combo(['AND', 'OR', 'XOR', 'NAND', 'NOR'], value,
                                    lambda t: self.update_object_prop(key, t))
                form.addRow("Logic Type:", combo)
            elif is_pickup and key == 'item_type':
                self._build_pickup_item_type_row(form, thing)
            elif is_pickup and key == 'activation':
                self._build_pickup_activation_row(form, thing, value)
            elif is_pickup and key == 'value':
                self._build_pickup_value_row(form, thing, value)
            elif isinstance(value, bool):
                # _make_checkbox wires the `toggled(bool)` signal, so the callback
                # already receives the new checked state as a bool.  (Comparing it
                # to Qt.Checked — an int enum == 2 — is always False, which is why
                # generic bool props like 'casts_shadows' never stayed enabled.)
                cb = _make_checkbox("", value, lambda c, k=key: self.update_object_prop(k, c), _Style.CHECKBOX)
                form.addRow(label_text, cb)
            elif isinstance(value, int):
                # Full 32-bit range so large-but-valid ints (gold, radii, health
                # caps) show their real value instead of being pinned at 99999;
                # _make_spin still clamps anything beyond it so nothing overflows.
                spin = _make_spin(value, -2147483648, 2147483647)
                spin.editingFinished.connect(lambda w=spin, k=key: self.update_object_prop(k, w.value()))
                form.addRow(label_text, spin)
            elif isinstance(value, float):
                inp = QLineEdit(str(value))
                inp.editingFinished.connect(
                    lambda le=inp, k=key: self.update_object_prop(
                        k, float(le.text()) if le.text() and le.text().replace('.', '', 1).replace('-', '', 1).isdigit() else 0.0))
                form.addRow(label_text, inp)
            else:
                inp = QLineEdit(str(value))
                inp.editingFinished.connect(lambda le=inp, k=key: self.update_object_prop(k, le.text()))
                form.addRow(label_text, inp)

        # Sprite + respawn controls (pickup only)
        if is_pickup:
            self._build_pickup_sprite_row(form, thing)
            self._build_pickup_respawn_row(form, thing)

    def _build_monster_type_row(self, form, thing):
        combo = _make_combo(['human', 'flying'], thing.properties.get('monster_type', 'human'))
        form.addRow("Monster Type:", combo)

        variant_combo = ClickableComboBox()
        variant_combo.setToolTip("Sprite variant — selects an alternate sprite subfolder.")
        self._widgets['monster_variant_combo'] = variant_combo

        def populate(mtype=None):
            if mtype is None:
                mtype = thing.properties.get('monster_type', 'human')
            variant_combo.blockSignals(True)
            variant_combo.clear()
            variant_combo.addItem('<None>')
            for v in MONSTER_VARIANTS.get(mtype, []):
                variant_combo.addItem(v)
            cur = thing.properties.get('variant', '<None>')
            idx = variant_combo.findText(cur)
            variant_combo.setCurrentIndex(idx if idx >= 0 else 0)
            variant_combo.blockSignals(False)

        populate()

        def on_variant(text):
            thing.properties['variant'] = text
            try:
                Monster.clear_sprite_cache()
            except Exception:
                pass
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()
            try:
                self.editor.view_3d.update()
            except Exception:
                pass

        def on_type_changed(new_type):
            self.update_object_prop('monster_type', new_type)
            from engine.monster_constants import MONSTER_SPRITE_SIZES
            default_w, default_h = MONSTER_SPRITE_SIZES.get(new_type, (128, 128))
            self.update_object_prop('sprite_width', default_w)
            self.update_object_prop('sprite_height', default_h)
            thing.properties['variant'] = '<None>'
            populate(new_type)
            is_flying = new_type == 'flying'
            for k in ('projectile_sprite_label', 'projectile_sprite_path'):
                if k in self._widgets:
                    self._widgets[k].setVisible(is_flying)
            self.set_object(thing)

        combo.currentTextChanged.connect(on_type_changed)
        variant_combo.currentTextChanged.connect(on_variant)
        form.addRow("Variant:", variant_combo)

    def _build_pickup_item_type_row(self, form, thing):
        combo = _make_combo(['health', 'key', 'gun1', 'gun2', 'cig'],
                            thing.properties.get('item_type', 'health'),
                            self.on_pickup_item_type_changed)
        form.addRow("Item Type:", combo)

        lbl = QLabel("Key Name:")
        key_combo = _make_combo(['blue_key', 'red_key', 'yellow_key', 'green_key'],
                                thing.properties.get('key_name', 'blue_key'),
                                self.on_pickup_key_name_changed)
        key_combo.setEditable(True)
        form.addRow(lbl, key_combo)
        self._pickup_key_widgets.append((lbl, key_combo))

        is_key = thing.properties.get('item_type') == 'key'
        lbl.setVisible(is_key)
        key_combo.setVisible(is_key)

        # Door link
        door_lbl = QLabel("")
        door_lbl.setWordWrap(True)
        door_lbl.setVisible(False)
        form.addRow("", door_lbl)
        self._widgets['pickup_door_link_label'] = door_lbl
        self._pickup_key_widgets.append((QLabel(""), door_lbl))

        door_btn = QPushButton("Select Door ▸")
        door_btn.setVisible(False)
        door_btn.clicked.connect(self._select_linked_door)
        form.addRow("", door_btn)
        self._widgets['pickup_door_select_btn'] = door_btn
        self._pickup_key_widgets.append((QLabel(""), door_btn))

        if is_key:
            self._update_pickup_door_link(thing)
        key_combo.currentTextChanged.connect(lambda _: self._update_pickup_door_link(self.current_object))

    def _build_pickup_activation_row(self, form, thing, value):
        combo = _make_combo(['walk_over', 'use'], value, lambda t: self.update_object_prop('activation', t))
        form.addRow("Activation:", combo)
        self._pickup_activation_widget = combo
        if thing.properties.get('item_type') == 'health':
            combo.setCurrentText('walk_over')
            combo.setEnabled(False)
            self.update_object_prop('activation', 'walk_over')

    def _build_pickup_value_row(self, form, thing, value):
        lbl = QLabel("Value:")
        spin = _make_spin(value, -99999, 99999)
        spin.editingFinished.connect(lambda w=spin: self.update_object_prop('value', w.value()))
        form.addRow(lbl, spin)
        self._pickup_value_widgets.append((lbl, spin))
        if thing.properties.get('item_type') == 'key':
            lbl.setVisible(False)
            spin.setVisible(False)

    def _build_pickup_sprite_row(self, form, thing):
        lbl = QLabel("Sprite:")
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        path = QLineEdit(thing.properties.get('custom_sprite', ''))
        path.setReadOnly(True)
        path.setPlaceholderText("Default sprite")
        btn = QPushButton("Sprite...")
        btn.setFixedWidth(80)
        btn.clicked.connect(self.on_pickup_sprite_select)
        clear = QPushButton("Clear")
        clear.setFixedWidth(60)
        clear.setToolTip("Clear custom sprite")
        clear.clicked.connect(self.on_pickup_sprite_clear)
        h.addWidget(path)
        h.addWidget(btn)
        h.addWidget(clear)
        form.addRow(lbl, widget)
        self._pickup_sprite_widgets.append((lbl, widget))
        self.pickup_sprite_path = path

        is_key = thing.properties.get('item_type') == 'key'
        lbl.setVisible(not is_key)
        widget.setVisible(not is_key)

    def _build_pickup_respawn_row(self, form, thing):
        form.addRow(self._section("Respawn"))
        respawns = thing.properties.get('respawns', False)
        rtime = thing.properties.get('respawn_time', 20.0)

        rw = QWidget()
        rl = QHBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        cb = _make_checkbox("Respawns", respawns, self.on_respawn_toggled, _Style.CHECKBOX)
        lbl = QLabel("after")
        spin = _make_spin(rtime, 0.1, 9999.0, suffix=" sec", decimals=1)
        spin.editingFinished.connect(lambda: self.update_object_prop('respawn_time', spin.value()))
        lbl.setVisible(respawns)
        spin.setVisible(respawns)
        rl.addWidget(cb)
        rl.addWidget(lbl)
        rl.addWidget(spin)
        rl.addStretch()
        form.addRow("", rw)
        self.respawn_checkbox = cb
        self.respawn_time_label = lbl
        self.respawn_time_spin = spin

    def _build_pathnode_group(self, tab_layout, thing):
        for k, v in (('radius', 256.0), ('show_radius', False), ('affects_type', 'both'),
                     ('next_node', ''), ('wait_time', 0.0), ('speed', 1.0)):
            thing.properties.setdefault(k, v)

        group = QGroupBox("Path Node")
        group.setStyleSheet(_Style.group_box("#26A69A", "#1a2f2d"))
        form = QFormLayout(group)
        form.setSpacing(6)
        form.setContentsMargins(8, 8, 8, 8)

        # Radius + show button
        radius_spin = _make_spin(thing.properties.get('radius', 256.0), 1.0, 99999.0,
                                 suffix=" u", decimals=1, step=16.0)
        show_btn = QToolButton()
        show_btn.setText("⊙")
        show_btn.setCheckable(True)
        show_btn.setChecked(bool(thing.properties.get('show_radius', False)))
        show_btn.setStyleSheet("""
            QToolButton { background-color: #425f5d; color: white; border-radius: 4px; padding: 3px 8px; font-size: 14px; border: 1px solid #555; }
            QToolButton:checked { background-color: #26A69A; border-color: #26A69A; }
            QToolButton:hover { background-color: #5a7a82; }
        """)
        form.addRow("Radius:", _hbox(radius_spin, show_btn, stretch=False))

        def _on_radius(v):
            thing.properties['radius'] = float(v)
            if getattr(self.editor, '_sight_preview_thing', None) is thing:
                self._repaint_viewport()

        def _on_show(checked):
            thing.properties['show_radius'] = bool(checked)
            self._repaint_viewport()

        radius_spin.valueChanged.connect(_on_radius)
        show_btn.toggled.connect(_on_show)

        # Affects type
        affects = _make_combo(PathNode.AFFECTS_TYPES,
                              str(thing.properties.get('affects_type', 'both')).lower(),
                              tooltip="Which monster types may use this node")
        form.addRow("Affects Type:", affects)

        # Next node
        my_name = thing.properties.get('name', '') or ''
        next_combo = ClickableComboBox()
        next_combo.addItem("(none)")
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '') or ''
                if n and n != my_name:
                    next_combo.addItem(n)
        current = thing.properties.get('next_node', '') or ''
        if current:
            idx = next_combo.findText(current)
            if idx >= 0:
                next_combo.setCurrentIndex(idx)
            else:
                next_combo.addItem(current + "  (missing)")
                next_combo.setCurrentIndex(next_combo.count() - 1)

        def _on_next(text):
            clean = (text or '').replace("  (missing)", "").strip()
            thing.properties['next_node'] = '' if clean == '(none)' else clean
            self._repaint_viewport()

        next_combo.currentTextChanged.connect(_on_next)
        form.addRow("Next Node:", next_combo)

        # Wait time
        wait = _make_spin(thing.properties.get('wait_time', 0.0), 0.0, 9999.0,
                          suffix=" sec", decimals=1, step=0.5,
                          tooltip="How long a monster pauses at this node")
        wait.valueChanged.connect(lambda v: thing.properties.update({'wait_time': float(v)}))
        form.addRow("Wait Time:", wait)

        # Speed
        speed = _make_spin(thing.properties.get('speed', 1.0), 0.01, 10.0,
                           suffix="×", decimals=2, step=0.25,
                           tooltip="Speed multiplier for entities heading toward this node")
        speed.valueChanged.connect(lambda v: thing.properties.update({'speed': float(v)}))
        form.addRow("Speed:", speed)

        tab_layout.addWidget(group)

    def _build_logic_camera_group(self, tab_layout, thing):
        group = QGroupBox("Cinematic Camera")
        group.setStyleSheet(_Style.group_box("#42A5F5", "#1a2a3d"))
        form = QFormLayout(group)
        form.setSpacing(6)
        form.setContentsMargins(8, 8, 8, 8)

        # Path target
        combo = ClickableComboBox()
        combo.setEditable(True)
        combo.addItem("(none)")
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '')
                if n:
                    combo.addItem(n)
        current = thing.properties.get('path_target', '')
        if current:
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(current)

        def _on_path(text):
            clean = text.strip()
            thing.properties['path_target'] = '' if clean == '(none)' else clean

        combo.currentTextChanged.connect(_on_path)
        form.addRow("Path Target:", combo)

        # Speed
        speed = _make_spin(thing.properties.get('speed', 200.0), 1.0, 9999.0,
                           suffix=" u/s", decimals=1, step=10.0)
        speed.valueChanged.connect(lambda v: thing.properties.update({'speed': float(v)}))
        form.addRow("Speed:", speed)

        # FOV
        fov = _make_spin(thing.properties.get('fov_override', 0.0), 0.0, 179.0,
                         suffix="°", decimals=1, step=5.0,
                         tooltip="Override FOV during sequence. 0 = use default.")
        fov.valueChanged.connect(lambda v: thing.properties.update({'fov_override': float(v)}))
        form.addRow("FOV Override:", fov)

        # Look ahead
        look = _make_checkbox("Look at next node", thing.properties.get('look_ahead', True),
                              lambda c: thing.properties.update({'look_ahead': bool(c)}), _Style.CHECKBOX)
        look.setToolTip("Camera faces the next PathNode instead of forward")
        form.addRow("", look)

        tab_layout.addWidget(group)

    def _build_spawner_group(self, tab_layout, thing):
        from editor.things import ENTITY_TYPES
        group = QGroupBox("Spawner")
        group.setStyleSheet(_Style.group_box("#AB47BC", "#2a1a3d"))
        form = QFormLayout(group)
        form.setSpacing(6)
        form.setContentsMargins(8, 8, 8, 8)

        spawn_combo = _make_combo(sorted(ENTITY_TYPES.keys()),
                                  thing.properties.get('spawn_type', 'Monster'),
                                  lambda t: thing.properties.update({'spawn_type': t}),
                                  tooltip="Entity class to instantiate when Spawn is fired")
        form.addRow("Spawn Type:", spawn_combo)

        # Target node
        node_combo = ClickableComboBox()
        node_combo.setEditable(True)
        node_combo.addItem("(none)")
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '')
                if n:
                    node_combo.addItem(n)
        current = thing.properties.get('target_node', '')
        if current:
            idx = node_combo.findText(current)
            if idx >= 0:
                node_combo.setCurrentIndex(idx)
            else:
                node_combo.setEditText(current)

        def _on_node(text):
            clean = text.strip()
            thing.properties['target_node'] = '' if clean == '(none)' else clean

        node_combo.currentTextChanged.connect(_on_node)
        form.addRow("Target Node:", node_combo)

        max_spin = _make_spin(thing.properties.get('max_spawn', 0), 0, 9999,
                              tooltip="Maximum entities this spawner will create. 0 = unlimited.")
        max_spin.valueChanged.connect(lambda v: thing.properties.update({'max_spawn': int(v)}))
        form.addRow("Max Spawn:", max_spin)

        tab_layout.addWidget(group)

        # Monster spawn settings (conditional)
        monster_group = QGroupBox("Monster Spawn Settings")
        monster_group.setStyleSheet(_Style.group_box("#F08000"))
        mform = QFormLayout(monster_group)
        mform.setSpacing(6)
        mform.setContentsMargins(8, 8, 8, 8)

        spawn_props = thing.properties.setdefault('spawn_properties', {})

        mtype_combo = _make_combo(['human', 'flying'],
                                  spawn_props.get('monster_type', 'human'),
                                  lambda t: spawn_props.update({'monster_type': t}))
        mform.addRow("Monster Type:", mtype_combo)

        variant_combo = ClickableComboBox()
        variant_combo.setToolTip("Sprite variant")
        mform.addRow("Variant:", variant_combo)

        random_cb = _make_checkbox("Spawn random type & variant each time",
                                   spawn_props.get('random', False), None, _Style.CHECKBOX)
        mform.addRow(random_cb)

        def populate_variants(mtype=None):
            if mtype is None:
                mtype = spawn_props.get('monster_type', 'human')
            variant_combo.blockSignals(True)
            variant_combo.clear()
            variant_combo.addItem('<None>')
            for v in MONSTER_VARIANTS.get(mtype, []):
                variant_combo.addItem(v)
            cur = spawn_props.get('variant', '<None>')
            idx = variant_combo.findText(cur)
            variant_combo.setCurrentIndex(idx if idx >= 0 else 0)
            variant_combo.blockSignals(False)

        def on_mtype(text):
            if not random_cb.isChecked():
                spawn_props['monster_type'] = text
            populate_variants(text)

        def on_variant(text):
            if not random_cb.isChecked():
                spawn_props['variant'] = text

        def on_random(checked):
            spawn_props['random'] = checked
            mtype_combo.setEnabled(not checked)
            variant_combo.setEnabled(not checked)
            if checked:
                spawn_props.pop('monster_type', None)
                spawn_props.pop('variant', None)
            else:
                spawn_props['monster_type'] = mtype_combo.currentText()
                spawn_props['variant'] = variant_combo.currentText()

        mtype_combo.currentTextChanged.connect(on_mtype)
        variant_combo.currentTextChanged.connect(on_variant)
        random_cb.toggled.connect(on_random)
        on_random(random_cb.isChecked())
        populate_variants()

        def update_visibility():
            is_monster = spawn_combo.currentText() == 'Monster'
            monster_group.setVisible(is_monster)
            if not is_monster:
                for k in ('monster_type', 'variant', 'random'):
                    spawn_props.pop(k, None)
            thing.properties['spawn_properties'] = spawn_props
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()

        spawn_combo.currentTextChanged.connect(lambda _: update_visibility())
        update_visibility()
        tab_layout.addWidget(monster_group)


    def _build_keyvalue_group(self, tab_layout, thing):
        """Display LogicKeyValueStore runtime data and persistent registry info."""
        group = QGroupBox("Key/Value Store")
        group.setStyleSheet(_Style.group_box("#26A69A", "#1a2f2d"))
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # Store name (read-only display)
        store_name = thing.properties.get('store_name', thing.properties.get('name', ''))
        name_lbl = QLabel(f"<b>Store Name:</b> {store_name}")
        name_lbl.setStyleSheet("QLabel { color: #88FF88; }")
        layout.addWidget(name_lbl)

        # Refresh button
        refresh_btn = QPushButton("🔄 Refresh Values")
        refresh_btn.setStyleSheet("""
            QPushButton { background-color: #2a5a5a; color: white; border: 1px solid #26A69A;
                          border-radius: 3px; padding: 4px 8px; }
            QPushButton:hover { background-color: #3a7a7a; }
        """)
        refresh_btn.setToolTip("Reload values from runtime storage")
        refresh_btn.clicked.connect(lambda: self._refresh_keyvalue_group(thing))
        layout.addWidget(refresh_btn)


        # Initial data (designer defaults)
        initial_data = thing.properties.get('initial_data', {})
        if initial_data:
            layout.addWidget(QLabel("<b>Initial Data (Designer Defaults):</b>"))
            for k, v in sorted(initial_data.items()):
                row = QHBoxLayout()
                row.setSpacing(4)
                key_lbl = QLabel(f"  {k}:")
                key_lbl.setStyleSheet("QLabel { color: #AAAAAA; min-width: 100px; }")
                val_lbl = QLabel(str(v))
                val_lbl.setStyleSheet("QLabel { color: #FFFFFF; }")
                row.addWidget(key_lbl)
                row.addWidget(val_lbl)
                row.addStretch()
                layout.addLayout(row)

        # Separator
        if initial_data:
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("QFrame { color: #555; }")
            layout.addWidget(line)

        # Runtime data (live values)
        runtime_data = getattr(thing, '_runtime_data', {})
        persistent = getattr(thing.__class__, '_persistent_registry', {})

        # Check persistent registry for this store
        persistent_data = persistent.get(store_name, {})

        if runtime_data or persistent_data:
            layout.addWidget(QLabel("<b>Runtime Values (Live):</b>"))

            # Show runtime data (authoritative for this instance)
            all_keys = set(runtime_data.keys()) | set(persistent_data.keys())
            for k in sorted(all_keys):
                row = QHBoxLayout()
                row.setSpacing(4)

                # Key label
                key_lbl = QLabel(f"  {k}:")
                key_lbl.setStyleSheet("QLabel { color: #F08000; min-width: 100px; }")

                # Value with source indicator
                if k in runtime_data:
                    val_str = str(runtime_data[k])
                    source = "runtime"
                else:
                    val_str = str(persistent_data[k])
                    source = "persistent"

                val_lbl = QLabel(val_str)
                if source == "runtime":
                    val_lbl.setStyleSheet("QLabel { color: #00FF00; font-weight: bold; }")
                else:
                    val_lbl.setStyleSheet("QLabel { color: #88AAFF; }")

                row.addWidget(key_lbl)
                row.addWidget(val_lbl)
                row.addStretch()
                layout.addLayout(row)

            # Count
            count_lbl = QLabel(f"<i>{len(all_keys)} / {thing.MAX_PAIRS} pairs stored</i>")
            count_lbl.setStyleSheet("QLabel { color: #888; font-size: 10px; }")
            layout.addWidget(count_lbl)
        else:
            empty_lbl = QLabel("<i>No values stored yet.</i>")
            empty_lbl.setStyleSheet("QLabel { color: #888; font-style: italic; }")
            layout.addWidget(empty_lbl)


        tab_layout.addWidget(group)
        self._widgets['keyvalue_group'] = group

    def _refresh_keyvalue_group(self, thing):
        """Refresh the keyvalue display by rebuilding the property editor."""
        self.set_object(thing)

    def _build_monster_groups(self, tab_layout, thing):
        for k, v in (('sight', 512), ('triggered', False), ('wake_on_sight', True),
                     ('can_hear', False), ('dead', False), ('non_hostile', False)):
            thing.properties.setdefault(k, v)

        # AI group
        ai_group = QGroupBox("AI")
        ai_group.setStyleSheet(_Style.group_box("#F08000"))
        aform = QFormLayout(ai_group)
        aform.setSpacing(6)
        aform.setContentsMargins(8, 8, 8, 8)

        sight_spin = _make_spin(thing.properties.get('sight', 512), 0, 9999, suffix=" u")
        sight_spin.setToolTip("Distance at which this monster detects the player")
        sight_btn = QToolButton()
        sight_btn.setText("👁")
        sight_btn.setCheckable(True)
        sight_btn.setChecked(getattr(self.editor, '_sight_preview_thing', None) is thing)
        sight_btn.setStyleSheet("""
            QToolButton { background-color: #425f5d; color: white; border-radius: 4px; padding: 3px 8px; font-size: 14px; border: 1px solid #555; }
            QToolButton:checked { background-color: #F08000; border-color: #F08000; }
            QToolButton:hover { background-color: #5a7a82; }
        """)
        aform.addRow("Range:", _hbox(sight_spin, sight_btn, stretch=False))

        def _on_sight(v):
            thing.properties['sight'] = v
            if getattr(self.editor, '_sight_preview_thing', None) is thing:
                self._repaint_viewport()

        def _on_preview(checked):
            self.editor._sight_preview_thing = thing if checked else None
            self._repaint_viewport()

        sight_spin.valueChanged.connect(_on_sight)
        sight_btn.toggled.connect(_on_preview)

        hear_cb = _make_checkbox("Can hear gunfire", thing.properties.get('can_hear', False),
                                 lambda c: self.update_object_prop('can_hear', c), _Style.CHECKBOX)
        aform.addRow("", hear_cb)

        # Team
        thing.properties.setdefault('team', '')
        team_combo = ClickableComboBox()
        team_combo.setEditable(True)
        team_combo.addItems(['(none)', '1', '2', '3', 'player'])
        cur = str(thing.properties.get('team', ''))
        if cur:
            idx = team_combo.findText(cur)
            if idx >= 0:
                team_combo.setCurrentIndex(idx)
            else:
                team_combo.addItem(cur)
                team_combo.setCurrentIndex(team_combo.count() - 1)
        team_combo.setToolTip("Monsters on DIFFERENT teams are enemies")
        team_combo.currentTextChanged.connect(
            lambda t: thing.properties.update({'team': '' if t.strip() == '(none)' else t.strip()}))
        aform.addRow("Team:", team_combo)

        # Projectile sprite (flying only)
        proj_lbl = QLabel("Projectile Sprite:")
        proj_widget = QWidget()
        proj_h = QHBoxLayout(proj_widget)
        proj_h.setContentsMargins(0, 0, 0, 0)
        proj_path = QLineEdit()
        proj_path.setReadOnly(True)
        proj_path.setPlaceholderText("Default: assets/sprites/monsters/projectile.png")
        cur_proj = thing.properties.get('projectile_sprite', '')
        if cur_proj:
            proj_path.setText(cur_proj)

        def pick_proj():
            start = os.path.join(os.getcwd(), 'assets', 'sprites', 'monsters')
            os.makedirs(start, exist_ok=True)
            fp, _ = QFileDialog.getOpenFileName(self, "Select Projectile Sprite", start,
                                                "Image Files (*.png *.jpg *.jpeg *.bmp *.tga)")
            if fp:
                rel = os.path.relpath(fp, os.getcwd()).replace('\\', '/')
                thing.properties['projectile_sprite'] = rel
                proj_path.setText(rel)
                if hasattr(self.editor, 'mark_dirty'):
                    self.editor.mark_dirty()

        def clear_proj():
            thing.properties.pop('projectile_sprite', None)
            proj_path.setText('')
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()

        proj_btn = QPushButton("...")
        proj_btn.setFixedWidth(30)
        proj_btn.clicked.connect(pick_proj)
        proj_clear = QPushButton("✕")
        proj_clear.setFixedWidth(30)
        proj_clear.setToolTip("Clear custom projectile sprite")
        proj_clear.clicked.connect(clear_proj)
        proj_h.addWidget(proj_path)
        proj_h.addWidget(proj_btn)
        proj_h.addWidget(proj_clear)
        aform.addRow(proj_lbl, proj_widget)
        self._widgets['projectile_sprite_label'] = proj_lbl
        self._widgets['projectile_sprite_path'] = proj_path

        is_flying = thing.properties.get('monster_type', 'human') == 'flying'
        proj_lbl.setVisible(is_flying)
        proj_widget.setVisible(is_flying)

        # Patrol
        thing.properties.setdefault('patrol', False)
        thing.properties.setdefault('patrol_target', '')
        thing.properties.setdefault('patrol_mode', 'loop')

        patrol_cb = _make_checkbox("Patrol", thing.properties.get('patrol', False), None, _Style.CHECKBOX)
        patrol_cb.setToolTip("Monster walks toward selected PathNode when player is not in sight")
        aform.addRow("", patrol_cb)

        patrol_combo = ClickableComboBox()
        patrol_combo.addItem("(none)")
        mtype = str(thing.properties.get('monster_type', 'human')).lower()
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '') or ''
                if n and t.accepts_monster_type(mtype):
                    patrol_combo.addItem(n)
                elif n:
                    patrol_combo.addItem(f"{n}  (wants {t.get_affects_type()})")
                    item = patrol_combo.model().item(patrol_combo.count() - 1)
                    if item is not None:
                        item.setFlags(item.flags() & ~Qt.ItemIsEnabled)

        cur_patrol = thing.properties.get('patrol_target', '') or ''
        if cur_patrol:
            idx = patrol_combo.findText(cur_patrol)
            if idx >= 0:
                patrol_combo.setCurrentIndex(idx)
            else:
                patrol_combo.addItem(cur_patrol + "  (missing)")
                patrol_combo.setCurrentIndex(patrol_combo.count() - 1)

        patrol_lbl = QLabel("Target Node:")
        is_patrolling = bool(thing.properties.get('patrol', False))
        patrol_lbl.setVisible(is_patrolling)
        patrol_combo.setVisible(is_patrolling)

        def _on_patrol(checked):
            thing.properties['patrol'] = bool(checked)
            patrol_lbl.setVisible(checked)
            patrol_combo.setVisible(checked)
            if not checked:
                thing.properties['patrol_target'] = ''
                patrol_combo.setCurrentIndex(0)
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()

        def _on_patrol_target(text):
            clean = (text or '').replace("  (missing)", "")
            if "  (wants " in clean:
                clean = clean.split("  (wants ")[0]
            thing.properties['patrol_target'] = '' if clean == '(none)' else clean
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()

        patrol_cb.toggled.connect(_on_patrol)
        patrol_combo.currentTextChanged.connect(_on_patrol_target)

        mode_combo = _make_combo(['loop', 'ping_pong', 'once'],
                                 str(thing.properties.get('patrol_mode', 'loop')).lower(),
                                 lambda t: thing.properties.update({'patrol_mode': t}))
        mode_lbl = QLabel("Patrol Mode:")
        mode_lbl.setVisible(is_patrolling)
        mode_combo.setVisible(is_patrolling)

        def _on_mode(text):
            thing.properties['patrol_mode'] = text
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()

        mode_combo.currentTextChanged.connect(_on_mode)

        # Rebind patrol toggle to also show mode
        patrol_cb.toggled.disconnect()
        patrol_cb.toggled.connect(lambda c: (_on_patrol(c), mode_lbl.setVisible(c), mode_combo.setVisible(c)))

        aform.addRow(patrol_lbl, patrol_combo)
        aform.addRow(mode_lbl, mode_combo)

        tab_layout.addWidget(ai_group)

        # Flags
        flags_group = QGroupBox("Behaviour Flags")
        flags_group.setStyleSheet(_Style.group_box("#F08000"))
        flay = QVBoxLayout(flags_group)
        flay.setSpacing(6)
        for prop_key, label_text, tooltip in (
            ('triggered', 'Trigger', 'Monster starts dormant — must be woken via I/O'),
            ('wake_on_sight', 'Wake when sees player', 'Auto-wakes when player enters sight range'),
            ('dead', 'Dead', 'Placed in dead/inactive state at level start'),
            ('non_hostile', 'Non-hostile', 'Will not attack the player'),
        ):
            cb = _make_checkbox(label_text, thing.properties.get(prop_key, False),
                                lambda c, k=prop_key: self.update_object_prop(k, c), _Style.CHECKBOX)
            cb.setToolTip(tooltip)
            flay.addWidget(cb)
            self._widgets[f'monster_flag_{prop_key}'] = cb

        tab_layout.addWidget(flags_group)

        # Customise button
        cust_btn = QPushButton("🎨  Customise Sprites…")
        cust_btn.setFixedWidth(350)
        cust_btn.setToolTip("Assign custom idle / shoot / dead PNGs and billboard size")
        cust_btn.setStyleSheet("""
            QPushButton { background-color: #3c3f41; border: 1px solid #F08000; color: #F08000; padding: 6px; font-weight: bold; margin-top: 4px; }
            QPushButton:hover { background-color: #4b4d4d; }
            QPushButton:pressed { background-color: #2b2b2b; }
        """)

        def _open_customise():
            from editor.monster_customise_dialog import MonsterCustomiseDialog
            dlg = MonsterCustomiseDialog(thing, self)
            if dlg.exec_() == MonsterCustomiseDialog.Accepted:
                self.set_object(thing)
                try:
                    self.editor.view_3d.update()
                except Exception:
                    pass
                for attr in ('view_top', 'view_front', 'view_side', 'view_2d'):
                    widget = getattr(self.editor, attr, None)
                    if widget is not None:
                        try:
                            widget.update()
                        except Exception:
                            pass

        cust_btn.clicked.connect(_open_customise)
        tab_layout.addLayout(_hbox(cust_btn, stretch=False))

    # ────────────────────────────
    # Shared small helpers
    # ────────────────────────────
    def _pathnode_combo(self, current_value):
        combo = ClickableComboBox()
        combo.setEditable(True)
        combo.addItem("(none)")
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '')
                if n:
                    combo.addItem(n)
        if current_value:
            idx = combo.findText(current_value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(current_value)
        return combo

    def _vec3_row(self, values, callback, indent=0):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(indent, 0, 0, 4)
        h.setSpacing(4)
        inputs = []
        for i, v in enumerate(values):
            lbl = QLabel(("X:", "Y:", "Z:")[i])
            inp = QLineEdit(str(v))
            inp.setFixedWidth(50)
            h.addWidget(lbl)
            h.addWidget(inp)
            inputs.append(inp)

        def _update():
            try:
                callback([float(inp.text()) for inp in inputs])
            except ValueError:
                pass

        for inp in inputs:
            inp.editingFinished.connect(_update)
        h.addStretch()
        return w, inputs

    def _color_button(self, color_rgb, callback, size=(100, 28)):
        btn = QPushButton()
        btn.setFixedSize(*size)
        self._update_color_button(btn, color_rgb)
        btn.clicked.connect(callback)
        return btn

    def _update_color_button(self, btn, color_rgb):
        if isinstance(color_rgb, (list, tuple)) and len(color_rgb) >= 3:
            if any(c > 1.0 for c in color_rgb):
                r, g, b = int(color_rgb[0]), int(color_rgb[1]), int(color_rgb[2])
            else:
                r, g, b = int(color_rgb[0] * 255), int(color_rgb[1] * 255), int(color_rgb[2] * 255)
            btn.setStyleSheet(f"background-color: rgb({r}, {g}, {b}); border: 2px solid #555; border-radius: 4px;")

    def _preview_button(self, text, callback):
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setStyleSheet("""
            QPushButton { background-color: #425F5D; color: white; border-radius: 4px; padding: 8px; font-weight: bold; }
            QPushButton:checked { background-color: #0056b3; }
            QPushButton:hover { background-color: #5a7a82; }
        """)
        btn.toggled.connect(callback)
        return btn

    def _pick_color(self, prop_name, button, default_color):
        if self.current_object is None:
            return
        current = self.current_object.get(prop_name, default_color)
        if any(c > 1.0 for c in current):
            qc = QColor(int(current[0]), int(current[1]), int(current[2]))
        else:
            qc = QColor(int(current[0] * 255), int(current[1] * 255), int(current[2] * 255))
        color = QColorDialog.getColor(qc, self, f"Choose {prop_name.replace('_', ' ').title()}")
        if color.isValid():
            new_color = [color.redF(), color.greenF(), color.blueF()]
            self.current_object[prop_name] = new_color
            self._update_color_button(button, new_color)
            self.editor.update_all_ui()

    def _reset_brush_colour(self, button):
        if self.current_object is None:
            return
        default = [0.8, 0.8, 0.8]
        self.current_object['colour'] = default
        self._update_color_button(button, default)
        self.editor.update_all_ui()

    def _repaint_viewport(self):
        for attr in ('gl_widget', 'viewport', 'canvas', 'render_widget', 'opengl_widget', 'view_3d'):
            widget = getattr(self.editor, attr, None)
            if widget is not None:
                widget.update()
                break
        for attr in ('view_top', 'view_front', 'view_side', 'view_2d'):
            widget = getattr(self.editor, attr, None)
            if widget is not None:
                widget.update()

    # ────────────────────────────
    # Event handlers
    # ────────────────────────────
    def on_shader_changed(self, shader_type):
        if self.current_object is None or self._populating:
            return
        
        # Normalize "none" variants to '<None>'
        if shader_type and shader_type.lower() in ('none', '<none>'):
            shader_type = '<None>'
        
        self.current_object['shader'] = shader_type

        if shader_type == '<None>':
            # Convert back to a solid normal brush: clear all shader/fog state
            self.current_object['is_fog'] = False
            shader_keys = ('glass_color', 'glass_opacity', 'glass_distortion', 'glass_refraction',
                           'glass_roughness', 'glass_fresnel', 'glow_color', 'glow_intensity',
                           'water_tint', 'water_opacity', 'water_reflectivity', 'water_wave_enabled',
                           'water_wave_height', 'water_plane', 'fog_color', 'fog_density')
            for key in shader_keys:
                self.current_object.pop(key, None)
        elif shader_type != 'Fog':
            self.current_object['is_fog'] = False

        # Initialize shader-specific defaults (existing code)
        if shader_type == 'Glass':
            if 'glass_color' not in self.current_object:
                self.current_object['glass_color'] = [0.9, 0.95, 1.0]
            if 'glass_opacity' not in self.current_object:
                self.current_object['glass_opacity'] = 0.3
            if 'glass_distortion' not in self.current_object:
                self.current_object['glass_distortion'] = 0.5
            if 'glass_refraction' not in self.current_object:
                self.current_object['glass_refraction'] = 1.5
            if 'glass_roughness' not in self.current_object:
                self.current_object['glass_roughness'] = 0.0
            if 'glass_fresnel' not in self.current_object:
                self.current_object['glass_fresnel'] = 0.5
        elif shader_type == 'Glow':
            self.current_object['glow_intensity'] = 10.0
        elif shader_type == 'Water':
            if 'water_opacity' not in self.current_object:
                self.current_object['water_opacity'] = 0.5
            if 'water_reflectivity' not in self.current_object:
                self.current_object['water_reflectivity'] = 0.5
            if 'water_tint' not in self.current_object:
                self.current_object['water_tint'] = [0.0, 0.4, 0.6]
            if 'water_wave_enabled' not in self.current_object:
                self.current_object['water_wave_enabled'] = True
            if 'water_wave_height' not in self.current_object:
                self.current_object['water_wave_height'] = 0.5
        elif shader_type == 'Fog':
            self.current_object['is_fog'] = True
            if 'fog_density' not in self.current_object:
                self.current_object['fog_density'] = 2.0
            if 'fog_color' not in self.current_object:
                self.current_object['fog_color'] = [0.5, 0.6, 0.7]

        if shader_type != '<None>':
            self.current_object['is_trigger'] = False

        # Defer refresh to avoid interrupting shader combo's own update cycle
        QTimer.singleShot(0, self._deferred_shader_refresh)

    def _deferred_shader_refresh(self):
        """Refresh property editor and jump to shader tab after shader change."""
        if self.current_object is None:
            return
        self.set_object(self.current_object)
        if hasattr(self, 'shader_tab_index') and self.shader_tab_index is not None:
            shader = self.current_object.get('shader', '<None>')
            if shader not in ('<<None>', None, ''):
                self.tab_widget.setCurrentIndex(self.shader_tab_index)

    def on_trigger_changed(self, is_trigger):
        if self.current_object is None:
            return
        self.current_object['is_trigger'] = is_trigger
        if is_trigger:
            self.current_object.setdefault('trigger_type', 'Once')
            self.current_object.setdefault('textures', {})
            for face in ['top', 'bottom', 'north', 'south', 'east', 'west']:
                self.current_object['textures'][face] = 'trigger.jpg'
        if hasattr(self, 'trigger_tab_index'):
            self.tab_widget.setTabVisible(self.trigger_tab_index, is_trigger)
            if is_trigger:
                self.tab_widget.setCurrentIndex(self.trigger_tab_index)
        self._update_io_tab_presence()
        self.editor.update_views()
        self.editor.scene_hierarchy.refresh_list()

    def on_mover_changed(self, is_mover):
        if self.current_object is None:
            return
        if is_mover and self.current_object.get('is_door', False):
            self.current_object['is_door'] = False
            door_cb = self._widgets.get('door_cb')
            if door_cb:
                door_cb.blockSignals(True)
                door_cb.setChecked(False)
                door_cb.blockSignals(False)
            if hasattr(self, 'door_tab_index'):
                self.tab_widget.setTabVisible(self.door_tab_index, False)
        self.current_object['is_mover'] = is_mover
        if is_mover:
            self.current_object.setdefault('speed', 64.0)
            self.current_object.setdefault('distance', 128.0)
            self.current_object.setdefault('direction', [0, 1, 0])
        if hasattr(self, 'mover_tab_index'):
            self.tab_widget.setTabVisible(self.mover_tab_index, is_mover)
            if is_mover:
                self.tab_widget.setCurrentIndex(self.mover_tab_index)
        self._update_io_tab_presence()
        self.editor.update_views()
        self.editor.scene_hierarchy.refresh_list()

    def on_door_changed(self, is_door):
        if self.current_object is None:
            return
        if is_door and self.current_object.get('is_mover', False):
            self.current_object['is_mover'] = False
            mover_cb = self._widgets.get('mover_cb')
            if mover_cb:
                mover_cb.blockSignals(True)
                mover_cb.setChecked(False)
                mover_cb.blockSignals(False)
            if hasattr(self, 'mover_tab_index'):
                self.tab_widget.setTabVisible(self.mover_tab_index, False)
        self.current_object['is_door'] = is_door
        if is_door:
            self.current_object.setdefault('door_direction', 'up')
            self.current_object.setdefault('door_distance', 128.0)
            self.current_object.setdefault('door_lip', 8.0)
            self.current_object.setdefault('door_speed', 64.0)
        if hasattr(self, 'door_tab_index'):
            self.tab_widget.setTabVisible(self.door_tab_index, is_door)
            if is_door:
                self.tab_widget.setCurrentIndex(self.door_tab_index)
        self._update_io_tab_presence()
        self.editor.update_views()
        self.editor.scene_hierarchy.refresh_list()

    def on_hurt_changed(self, is_hurt):
        if self.current_object is None:
            return
        self.current_object['hurt'] = is_hurt
        if is_hurt and 'hurt_amount' not in self.current_object:
            self.current_object['hurt_amount'] = 10
        if 'damage_spin' in self._widgets:
            self._widgets['damage_spin'].setEnabled(is_hurt)
        self.editor.update_all_ui()

    def on_door_needs_key_changed(self, needs_key):
        if self.current_object is None:
            return
        self.current_object['door_needs_key'] = needs_key
        if needs_key and 'door_key_name' not in self.current_object:
            self.current_object['door_key_name'] = ''
        for k in ('door_key_input', 'door_key_label'):
            if k in self._widgets:
                self._widgets[k].setVisible(needs_key)
        self._update_door_key_link(self.current_object)
        self.editor.update_all_ui()

    def _update_door_key_link(self, brush):
        link_lbl = self._widgets.get('door_key_link_label')
        sel_btn = self._widgets.get('door_key_select_btn')
        if not link_lbl:
            return
        if not brush or not brush.get('door_needs_key', False):
            link_lbl.setText("")
            link_lbl.setVisible(False)
            if sel_btn:
                sel_btn.setVisible(False)
            return

        key_name = brush.get('door_key_name', '')
        if not key_name:
            link_lbl.setText("⚠ No key name set")
            link_lbl.setStyleSheet("QLabel { color: #FF8800; padding: 4px; }")
            link_lbl.setVisible(True)
            if sel_btn:
                sel_btn.setVisible(False)
            return

        self._linked_key_pickup = None
        for thing in self.editor.state.things:
            if isinstance(thing, Pickup):
                if thing.properties.get('item_type') == 'key' and thing.properties.get('key_name') == key_name:
                    self._linked_key_pickup = thing
                    break

        if self._linked_key_pickup:
            name = self._linked_key_pickup.properties.get('name', 'unnamed')
            pos = self._linked_key_pickup.pos
            pos_str = f"({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})" if pos else ""
            link_lbl.setText(f"🔑 Linked to: {name} {pos_str}")
            link_lbl.setStyleSheet("QLabel { color: #88FF88; padding: 4px; }")
            link_lbl.setVisible(True)
            if sel_btn:
                sel_btn.setVisible(True)
        else:
            link_lbl.setText(f"⚠ No key pickup named '{key_name}' found in map")
            link_lbl.setStyleSheet("QLabel { color: #FF4444; padding: 4px; }")
            link_lbl.setVisible(True)
            if sel_btn:
                sel_btn.setVisible(False)

    def _select_linked_key_pickup(self):
        pickup = getattr(self, '_linked_key_pickup', None)
        if pickup:
            self.editor.select_object(pickup)

    def _update_pickup_door_link(self, thing):
        link_lbl = self._widgets.get('pickup_door_link_label')
        sel_btn = self._widgets.get('pickup_door_select_btn')
        if not link_lbl or not isinstance(thing, Pickup) or thing.properties.get('item_type') != 'key':
            link_lbl.setVisible(False) if link_lbl else None
            if sel_btn:
                sel_btn.setVisible(False)
            return

        key_name = thing.properties.get('key_name', '')
        if not key_name:
            link_lbl.setText("⚠ No key name set")
            link_lbl.setStyleSheet("QLabel { color: #FF8800; padding: 4px; }")
            link_lbl.setVisible(True)
            if sel_btn:
                sel_btn.setVisible(False)
            return

        matching = [b for b in self.editor.state.brushes
                    if b.get('is_door') and b.get('door_needs_key') and b.get('door_key_name') == key_name]
        if matching:
            self._linked_door_brush = matching[0]
            door_name = matching[0].get('name', 'unnamed door')
            pos = matching[0].get('pos', [0, 0, 0])
            pos_str = f"({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})"
            extra = f" (+{len(matching) - 1} more)" if len(matching) > 1 else ""
            link_lbl.setText(f"🚪 Unlocks: {door_name} {pos_str}{extra}")
            link_lbl.setStyleSheet("QLabel { color: #88AAFF; padding: 4px; }")
            link_lbl.setVisible(True)
            if sel_btn:
                sel_btn.setVisible(True)
        else:
            link_lbl.setText(f"⚠ No door requires key '{key_name}'")
            link_lbl.setStyleSheet("QLabel { color: #FF4444; padding: 4px; }")
            link_lbl.setVisible(True)
            self._linked_door_brush = None
            if sel_btn:
                sel_btn.setVisible(False)

    def _select_linked_door(self):
        brush = getattr(self, '_linked_door_brush', None)
        if brush:
            self.editor.select_object(brush)

    def toggle_mover_preview(self, checked):
        if self.editor:
            btn = self._widgets.get('mover_preview_btn')
            if checked:
                if btn:
                    btn.setText("■ Stop Preview")
                self.editor.start_mover_preview(self.current_object)
            else:
                if btn:
                    btn.setText("▶ Preview Movement")
                self.editor.stop_mover_preview()

    def toggle_door_preview(self, checked):
        if self.editor:
            btn = self._widgets.get('door_preview_btn')
            if checked:
                if btn:
                    btn.setText("■ Stop Preview")
                if hasattr(self.editor, 'start_mover_preview'):
                    self.editor.start_mover_preview(self.current_object)
            else:
                if btn:
                    btn.setText("▶ Preview Door")
                if hasattr(self.editor, 'stop_mover_preview'):
                    self.editor.stop_mover_preview()

    def on_respawn_toggled(self, state):
        # Connected via _make_checkbox -> toggled(bool), so `state` is already
        # the boolean checked state (not a Qt.CheckState int).
        respawns = bool(state)
        self.update_object_prop('respawns', respawns)
        if hasattr(self, 'respawn_time_label'):
            self.respawn_time_label.setVisible(respawns)
        if hasattr(self, 'respawn_time_spin'):
            self.respawn_time_spin.setVisible(respawns)

    def on_pickup_key_name_changed(self, key_name):
        self.update_object_prop('key_name', key_name)
        is_custom = key_name == 'custom'
        if hasattr(self, '_pickup_sprite_widgets'):
            for lbl, widget in self._pickup_sprite_widgets:
                lbl.setVisible(is_custom)
                widget.setVisible(is_custom)

    def on_pickup_sprite_select(self):
        if self.current_object is None or not isinstance(self.current_object, Pickup):
            return
        start = os.path.join(os.getcwd(), 'assets', 'sprites')
        os.makedirs(start, exist_ok=True)
        fp, _ = QFileDialog.getOpenFileName(self, "Select Sprite Image", start,
                                            "Image Files (*.png *.jpg *.jpeg *.bmp *.tga)")
        if fp:
            rel = os.path.relpath(fp, os.getcwd()).replace('\\', '/')
            self.update_object_prop('custom_sprite', rel)
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText(rel)
            if hasattr(Pickup, 'clear_sprite_cache'):
                Pickup.clear_sprite_cache()
            self.editor.update_all_ui()

    def on_pickup_sprite_clear(self):
        if self.current_object is None or not isinstance(self.current_object, Pickup):
            return
        self.update_object_prop('custom_sprite', '')
        if hasattr(self, 'pickup_sprite_path'):
            self.pickup_sprite_path.setText('')
        if hasattr(Pickup, 'clear_sprite_cache'):
            Pickup.clear_sprite_cache()
        self.editor.update_all_ui()

    def on_pickup_item_type_changed(self, item_type):
        if self.current_object is None:
            return
        self.update_object_prop('item_type', item_type)
        is_key = item_type == 'key'
        is_health = item_type == 'health'
        is_gun = item_type in ('gun1', 'gun2', 'cig')

        current_key = self.current_object.properties.get('key_name', 'red_key')

        if hasattr(self, '_pickup_key_widgets'):
            for lbl, widget in self._pickup_key_widgets:
                lbl.setVisible(is_key)
                widget.setVisible(is_key)

        if is_key:
            self._update_pickup_door_link(self.current_object)

        if hasattr(self, '_pickup_value_widgets'):
            for lbl, widget in self._pickup_value_widgets:
                lbl.setVisible(not is_key)
                widget.setVisible(not is_key)

        show_sprite = (not is_key) or (is_key and current_key == 'custom')
        if hasattr(self, '_pickup_sprite_widgets'):
            for lbl, widget in self._pickup_sprite_widgets:
                lbl.setVisible(show_sprite)
                widget.setVisible(show_sprite)

        if is_health:
            self.update_object_prop('custom_sprite', 'assets/sprites/health.png')
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText('assets/sprites/health.png')
            self.update_object_prop('activation', 'walk_over')
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setCurrentText('walk_over')
                self._pickup_activation_widget.setEnabled(False)
        elif is_gun:
            sprite = f'assets/sprites/{item_type}.png'
            self.update_object_prop('custom_sprite', sprite)
            if hasattr(self, 'pickup_sprite_path'):
                self.pickup_sprite_path.setText(sprite)
            self.update_object_prop('activation', 'walk_over')
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setCurrentText('walk_over')
                self._pickup_activation_widget.setEnabled(False)
        else:
            if hasattr(self, '_pickup_activation_widget'):
                self._pickup_activation_widget.setEnabled(True)

        if hasattr(Pickup, 'clear_sprite_cache'):
            Pickup.clear_sprite_cache()
        self.editor.update_all_ui()

    def _on_collision_size_changed(self, value, thing):
        """Handle collision size vector update."""
        # If all zeros, remove the property (use auto)
        if all(v == 0 for v in value):
            thing.properties.pop('collision_size', None)
        else:
            thing.properties['collision_size'] = list(value)
        if hasattr(self.editor, 'mark_dirty'):
            self.editor.mark_dirty()

    def add_model_path_widget(self, layout, thing):
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit(thing.properties.get('model_path', ''))
        path_edit.setReadOnly(True)
        btn = QPushButton("...")
        btn.setFixedWidth(30)

        def pick():
            fp, _ = QFileDialog.getOpenFileName(self, "Select OBJ Model", "assets/models", "OBJ Files (*.obj)")
            if fp:
                try:
                    rel = os.path.relpath(fp, "assets").replace("\\", "/")
                except Exception:
                    rel = fp
                if not rel.startswith(".."):
                    rel = os.path.join("assets", rel) if not rel.startswith("assets") else rel
                self.update_object_prop('model_path', rel)
                path_edit.setText(rel)

        btn.clicked.connect(pick)
        h.addWidget(path_edit)
        h.addWidget(btn)
        layout.addRow("Model Path:", widget)

    def add_vector3_widget(self, layout, thing, key):
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        val = thing.properties.get(key, [0, 0, 0])
        if not isinstance(val, list) or len(val) != 3:
            val = [0, 0, 0]
        inputs = []
        for i in range(3):
            le = QLineEdit(str(val[i]))
            le.setFixedWidth(50)

            def update_vec(text, idx=i):
                try:
                    vec = thing.properties.get(key, [0, 0, 0])
                    vec[idx] = float(text)
                    self.update_object_prop(key, vec)
                except ValueError:
                    pass

            le.editingFinished.connect(lambda l=le, idx=i: update_vec(l.text(), idx))
            h.addWidget(le)
            inputs.append(le)
        layout.addRow(key.title() + ":", widget)

    def add_sound_file_widget(self, form_layout, thing, key, value):
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        line_edit = QLineEdit(str(value))
        line_edit.setReadOnly(True)
        button = QPushButton("...")
        button.setFixedWidth(30)

        def open_dialog():
            start = os.path.join('assets', 'sounds')
            if not os.path.exists(start):
                os.makedirs(start)
            fp, _ = QFileDialog.getOpenFileName(self, "Select Sound File", start, "Sound Files (*.wav *.mp3)")
            if fp:
                try:
                    rel = os.path.relpath(fp, ".").replace('\\', '/')
                except ValueError:
                    rel = os.path.basename(fp)
                self.update_object_prop(key, rel)
                line_edit.setText(rel)

        button.clicked.connect(open_dialog)
        h.addWidget(line_edit)
        h.addWidget(button)
        form_layout.addRow(key.replace('_', ' ').title() + ":", widget)

    def add_color_picker_widget(self, form_layout, thing, key):
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 0, 0, 0)
        rgb = thing.properties.get(key, [255, 255, 255])
        swatch = QPushButton()
        swatch.setFixedSize(200, 32)

        def update_swatch():
            rgb = thing.properties.get(key, [255, 255, 255])
            swatch.setStyleSheet(f"background-color: rgb({rgb[0]}, {rgb[1]}, {rgb[2]});")

        def open_dialog():
            rgb = thing.properties.get(key, [255, 255, 255])
            color = QColorDialog.getColor(QColor(*rgb), self, "Choose Light Colour")
            if color.isValid():
                self.update_object_prop(key, [color.red(), color.green(), color.blue()])
                update_swatch()

        swatch.clicked.connect(open_dialog)
        update_swatch()
        h.addWidget(swatch)
        form_layout.addRow("Colour:", widget)

    def update_object_prop(self, key, value):
        if self.current_object is None:
            return
        if isinstance(self.current_object, dict):
            self.current_object[key] = value
        elif isinstance(self.current_object, Thing):
            if key in self.current_object.properties:
                prop_type = type(self.current_object.properties.get(key))
                if prop_type == float:
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        value = 0.0
            self.current_object.properties[key] = value

        if isinstance(self.current_object, Portal) and key == 'angle':
            rot = self.current_object.properties.get('rotation', [0.0, 0.0, 0.0])
            if isinstance(rot, list) and len(rot) > 0:
                rot[0] = float(value)

        # Force redraw of 2D views when show_radius changes ==========
        if key == 'show_radius' and isinstance(self.current_object, Light):
            # The 2D views need a hard refresh to redraw the radius circle
            for v in ('view_top', 'view_front', 'view_side'):
                if hasattr(self.editor, v):
                    getattr(self.editor, v).update()

        if not self._populating:
            # Big World: a change to the settings entity (esp. "Fill world with
            # terrain") drives the editor terrain preview. Do it here, on the
            # main thread, so a terrain can be *generated* if the map has none —
            # the 2D paint path only re-applies an existing fill, never creates
            # GL state. Runs before the repaint below so the new terrain shows.
            if key in ('terrain_fill', 'terrain_infinite', 'enabled',
                       'terrain_stream_radius', 'activation_radius') \
                    and getattr(self.current_object, 'TYPE', None) == 'bigworldsettings' \
                    and hasattr(self.editor, 'sync_bigworld_terrain'):
                self.editor.sync_bigworld_terrain(allow_create=True)
                # When fill (or infinite) is switched on, surface the Terrain
                # Editor so the generated ground can be sculpted / re-biomed.
                if key in ('terrain_fill', 'terrain_infinite') and bool(value) \
                        and getattr(self.editor, 'terrain', None) is not None \
                        and hasattr(self.editor, '_show_terrain_editor_panel'):
                    self.editor._show_terrain_editor_panel()

            # Only repaint viewports — do NOT call update_all_ui() here.
            # update_all_ui() rebuilds the entire property editor via set_object(),
            # which destroys and recreates every widget (including whichever combo/spin
            # just fired the signal).  The deferred deleteLater() window causes Qt to
            # briefly re-show the combo dropdown before the widget is actually gone,
            # producing the flickering popup.
            self.editor.view_3d.update()
            for v in ('view_top', 'view_front', 'view_side'):
                if hasattr(self.editor, v):
                    getattr(self.editor, v).update()
            # Hierarchy only needs a refresh when the display name changes.
            if key == 'name' and hasattr(self.editor, 'scene_hierarchy'):
                self.editor.scene_hierarchy.refresh_list()
            # Mark the scene dirty so Ctrl+S knows there are unsaved changes.
            if hasattr(self.editor, 'mark_dirty'):
                self.editor.mark_dirty()