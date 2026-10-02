from PyQt5.QtWidgets import (
    QDialog, QCheckBox, QVBoxLayout, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QLabel, QSpinBox, QPushButton, QTabWidget, QWidget, QFormLayout, QSlider,
    QMessageBox, QKeySequenceEdit, QFrame, QGridLayout, QComboBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
import sys
import os

class SettingsWindow(QDialog):
    """
    A dialog window for editing application settings, built with PyQt5.
    """
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(600)
        self.config = config
        self.main_window = parent
        self.binding_in_progress = None

        self.layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.layout.addWidget(self.tabs)
        
        self._create_editor_tab()
        self._create_display_tab()
        self._create_play_modes_tab()
        self._create_controls_tab()
        self._create_keyboard_tab()
        self._create_split_screen_tab()   # new tab
        
        button_layout = QHBoxLayout()
        
        self.restart_button = QPushButton("Apply && Restart")
        self.restart_button.setToolTip("Save settings and restart the application")
        self.restart_button.clicked.connect(self._apply_and_restart)
        self.restart_button.setStyleSheet("""
            QPushButton {
                background-color: #425f5d;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border: 1px solid #333;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #4A6B73;
                border: 1px solid #555;
            }
            QPushButton:pressed {
                background-color: #3a5250;
            }
        """)
        button_layout.addWidget(self.restart_button)
        
        button_layout.addStretch()
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_layout.addWidget(button_box)
        
        self.layout.addLayout(button_layout)
        
        self.load_settings()
        self._apply_stylesheet()

    def _create_editor_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, "Editor")
        
        autosave_group = QGroupBox("Autosave")
        autosave_layout = QHBoxLayout()
        
        self.autosave_checkbox = QCheckBox("Enable Autosave")
        autosave_layout.addWidget(self.autosave_checkbox)
        
        autosave_layout.addWidget(QLabel("Interval (min):"))
        self.autosave_interval_spin = QSpinBox()
        self.autosave_interval_spin.setRange(5, 60)
        self.autosave_interval_spin.setValue(10)
        autosave_layout.addWidget(self.autosave_interval_spin)
        
        autosave_group.setLayout(autosave_layout)
        layout.addWidget(autosave_group)

        view_3d_group = QGroupBox("3D View")
        view_3d_layout = QVBoxLayout()
        
        self.show_caulk_checkbox = QCheckBox("Show Caulk textures")
        #view_3d_layout.addWidget(self.show_caulk_checkbox)
        
        self.sync_selection_checkbox = QCheckBox("Highlight selected brushes")
        view_3d_layout.addWidget(self.sync_selection_checkbox)
        
        self.click_select_3d_checkbox = QCheckBox("Click to select in 3D view")
        self.click_select_3d_checkbox.setToolTip("Allow selecting brushes/things by clicking in the 3D view (without Shift)")
        view_3d_layout.addWidget(self.click_select_3d_checkbox)

        self.place_camera_at_player_start_checkbox = QCheckBox("Focus Player_Start on load")
        self.place_camera_at_player_start_checkbox.setToolTip(
            "When enabled, loading a map moves the 3D editor camera to the Player Start location\n"
            "and centers the 2D views on it. This helps preview the map from the player's perspective."
        )
        view_3d_layout.addWidget(self.place_camera_at_player_start_checkbox)
        
        selection_trans_layout = QHBoxLayout()
        selection_trans_layout.addWidget(QLabel("Selection Transparency:"))
        self.selection_transparency_slider = QSlider(Qt.Horizontal)
        self.selection_transparency_slider.setRange(0, 100)
        self.selection_transparency_slider.setValue(50)
        self.selection_transparency_slider.setTickPosition(QSlider.TicksBelow)
        self.selection_transparency_slider.setTickInterval(10)
        selection_trans_layout.addWidget(self.selection_transparency_slider)
        self.selection_transparency_label = QLabel("50%")
        self.selection_transparency_slider.valueChanged.connect(
            lambda v: self.selection_transparency_label.setText(f"{v}%"))
        selection_trans_layout.addWidget(self.selection_transparency_label)
        view_3d_layout.addLayout(selection_trans_layout)
        
        view_3d_group.setLayout(view_3d_layout)
        layout.addWidget(view_3d_group)
        
        view_2d_group = QGroupBox("2D Views")
        view_2d_layout = QVBoxLayout()
        
        self.show_connections_checkbox = QCheckBox("Show connection lines")
        view_2d_layout.addWidget(self.show_connections_checkbox)
        
        self.locked_not_selectable_checkbox = QCheckBox("Locked items not selectable")
        view_2d_layout.addWidget(self.locked_not_selectable_checkbox)
        
        glow_arrow_layout = QHBoxLayout()
        glow_arrow_layout.addWidget(QLabel("Glow Arrow Scale:"))
        self.glow_arrow_scale_slider = QSlider(Qt.Horizontal)
        self.glow_arrow_scale_slider.setRange(50, 300)
        self.glow_arrow_scale_slider.setValue(100)
        self.glow_arrow_scale_slider.setTickPosition(QSlider.TicksBelow)
        self.glow_arrow_scale_slider.setTickInterval(25)
        glow_arrow_layout.addWidget(self.glow_arrow_scale_slider)
        self.glow_arrow_scale_label = QLabel("100%")
        self.glow_arrow_scale_slider.valueChanged.connect(
            lambda v: self.glow_arrow_scale_label.setText(f"{v}%"))
        glow_arrow_layout.addWidget(self.glow_arrow_scale_label)
        view_2d_layout.addLayout(glow_arrow_layout)
        
        view_2d_group.setLayout(view_2d_layout)
        layout.addWidget(view_2d_group)
        
        layout.addStretch()

    def _create_display_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        self.show_fps_checkbox = QCheckBox("Show FPS Counter")
        layout.addWidget(self.show_fps_checkbox)
        
        self.always_show_sysmon_checkbox = QCheckBox("Always Show System Monitor (F3)")
        layout.addWidget(self.always_show_sysmon_checkbox)
        
        self.always_show_io_debug_checkbox = QCheckBox("Always Show IO Debug Console")
        self.always_show_io_debug_checkbox.setToolTip("If enabled, the debug console will open automatically when the app starts.")
        layout.addWidget(self.always_show_io_debug_checkbox)
        
        self.disable_toasts_checkbox = QCheckBox("Disable Toast Notifications")
        layout.addWidget(self.disable_toasts_checkbox)

        font_layout = QHBoxLayout()
        font_layout.addWidget(QLabel("UI Font Size:"))
        self.font_size_spinbox = QSpinBox()
        self.font_size_spinbox.setRange(8, 24)
        font_layout.addWidget(self.font_size_spinbox)
        layout.addLayout(font_layout)
        
        self.vsync_checkbox = QCheckBox("Enable V-Sync")
        layout.addWidget(self.vsync_checkbox)
        
        self.dpi_scaling_checkbox = QCheckBox("Enable High DPI Scaling")
        layout.addWidget(self.dpi_scaling_checkbox)

        self.big_toolbar_buttons_checkbox = QCheckBox("Large Toolbar Buttons")
        layout.addWidget(self.big_toolbar_buttons_checkbox)
        
        conn_group = QGroupBox("Connection Visualization")
        conn_layout = QVBoxLayout()
        
        self.animate_connections_checkbox = QCheckBox("Animate Connection Lines (Moving Arrows)")
        conn_layout.addWidget(self.animate_connections_checkbox)
        
        conn_group.setLayout(conn_layout)
        layout.addWidget(conn_group)
        
        renderer_group = QGroupBox("Renderer Performance")
        renderer_layout = QVBoxLayout()
        
        self.arm_detected_label = QLabel()
        self._update_arm_detection_label()
        renderer_layout.addWidget(self.arm_detected_label)
        
        self.arm_mode_checkbox = QCheckBox("ARM Optimized Shaders")
        self.arm_mode_checkbox.setToolTip(
            "Use optimized shaders that pre-compute normal matrices on CPU.\n"
            "Recommended for ARM devices (Surface Pro X/9, Apple Silicon) and\n"
            "x64 emulation. Safe to enable on all devices - no quality loss."
        )
        renderer_layout.addWidget(self.arm_mode_checkbox)
        
        self.shadows_enabled_checkbox = QCheckBox("Enable Dynamic Shadows")
        self.shadows_enabled_checkbox.setToolTip(
            "Enable depth cube-map shadows from lights with 'casts_shadows' enabled.\n"
            "Disable for better performance on slower devices."
        )
        renderer_layout.addWidget(self.shadows_enabled_checkbox)
        
        auto_detect_btn = QPushButton("Auto-Detect Best Settings")
        auto_detect_btn.clicked.connect(self._auto_detect_renderer_settings)
        renderer_layout.addWidget(auto_detect_btn)
        
        renderer_group.setLayout(renderer_layout)
        layout.addWidget(renderer_group)

        layout.addStretch()
        self.tabs.addTab(tab, "Display")
    
    def _detect_arm_platform(self):
        import platform
        machine = platform.machine().lower()
        
        if 'arm' in machine or 'aarch' in machine:
            return True, "ARM processor detected"
        
        if sys.platform == 'win32':
            if os.environ.get('PROCESSOR_ARCHITECTURE', '').upper() == 'ARM64':
                return True, "Windows ARM64 detected"
            if os.environ.get('PROCESSOR_ARCHITEW6432', '').upper() == 'ARM64':
                return True, "Running under x64 emulation on ARM64"
            
            proc_id = os.environ.get('PROCESSOR_IDENTIFIER', '').lower()
            if 'qualcomm' in proc_id or 'snapdragon' in proc_id or 'arm' in proc_id:
                return True, "Qualcomm/ARM processor detected"
        
        return False, "x64/x86 processor detected"
    
    def _update_arm_detection_label(self):
        is_arm, reason = self._detect_arm_platform()
        if is_arm:
            self.arm_detected_label.setText(f"⚠️ {reason} - optimizations recommended")
            self.arm_detected_label.setStyleSheet("color: #FFA500;")
        else:
            self.arm_detected_label.setText(f"✓ {reason}")
            self.arm_detected_label.setStyleSheet("color: #90EE90;")
    
    def _auto_detect_renderer_settings(self):
        is_arm, reason = self._detect_arm_platform()
        
        if is_arm:
            self.arm_mode_checkbox.setChecked(True)
            self.shadows_enabled_checkbox.setChecked(False)
            QMessageBox.information(
                self,
                "Auto-Detect Complete",
                f"Detected: {reason}\n\n"
                "Applied ARM-optimized settings:\n"
                "• ARM Optimized Shaders: ON\n"
                "• Dynamic Shadows: OFF\n\n"
                "These settings improve performance on ARM devices."
            )
        else:
            self.arm_mode_checkbox.setChecked(True)
            self.shadows_enabled_checkbox.setChecked(True)
            QMessageBox.information(
                self,
                "Auto-Detect Complete", 
                f"Detected: {reason}\n\n"
                "Applied standard settings:\n"
                "• ARM Optimized Shaders: ON (no quality loss)\n"
                "• Dynamic Shadows: ON\n\n"
                "Full quality rendering enabled."
            )

    def _create_play_modes_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, "Play Modes")
        
        gameplay_group = QGroupBox("Gameplay")
        gameplay_layout = QVBoxLayout()
        
        self.physics_checkbox = QCheckBox("Enable physics")
        gameplay_layout.addWidget(self.physics_checkbox)
        
        self.show_hud_checkbox = QCheckBox("Show HUD (health, etc.)")
        gameplay_layout.addWidget(self.show_hud_checkbox)

        gameplay_group.setLayout(gameplay_layout)
        layout.addWidget(gameplay_group)

        save_group = QGroupBox("Play-session Save Mode")
        save_form = QFormLayout()
        self.save_mode_combo = QComboBox()
        # userData carries the value persisted to settings.ini.
        self.save_mode_combo.addItem("Full", "full")
        self.save_mode_combo.addItem("Delta", "delta")
        self.save_mode_combo.addItem("Both", "both")
        self.save_mode_combo.setToolTip(
            "How save / quicksave writes a play session:\n"
            "• Full — Complete, self-contained save. Largest file size.\n"
            "• Delta — Saves only changes from the original level. Smallest file\n"
            "   size but requires the base map to load.\n"
            "• Both — Saves a compact delta plus a complete fallback snapshot."
        )
        save_form.addRow("Default save mode:", self.save_mode_combo)
        self.save_mode_desc = QLabel()
        self.save_mode_desc.setWordWrap(True)
        self.save_mode_desc.setStyleSheet("color: #9fb7b5;")
        save_form.addRow(self.save_mode_desc)
        self.save_mode_combo.currentIndexChanged.connect(self._update_save_mode_desc)
        save_group.setLayout(save_form)
        layout.addWidget(save_group)
        
        mode_group = QGroupBox("Window Mode (Fullscreen Mode F12)")
        mode_layout = QFormLayout()
        self.kiosk_mode_combo = QComboBox()
        self.kiosk_mode_combo.addItems(["Fullscreen", "Borderless", "Windowed"])
        mode_layout.addRow("Display Mode:", self.kiosk_mode_combo)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        self.res_group = QGroupBox("Resolution (Windowed Only)")
        res_layout = QFormLayout()
        self.kiosk_res_w = QSpinBox()
        self.kiosk_res_w.setRange(640, 7680)
        self.kiosk_res_h = QSpinBox()
        self.kiosk_res_h.setRange(480, 4320)
        res_layout.addRow("Width:", self.kiosk_res_w)
        res_layout.addRow("Height:", self.kiosk_res_h)
        self.res_group.setLayout(res_layout)
        layout.addWidget(self.res_group)

        self.launch_in_editor_checkbox = QCheckBox("Launch packages in editor mode")
        self.launch_in_editor_checkbox.setToolTip(
            "When enabled, opening a .fiopak loads the map in the editor\n"
            "instead of launching kiosk mode."
        )
        layout.addWidget(self.launch_in_editor_checkbox)

        self.kiosk_mode_combo.currentTextChanged.connect(self._toggle_resolution_visibility)
        self._toggle_resolution_visibility()

        layout.addStretch()

    _SAVE_MODE_DESCS = {
        "full": "Complete, self-contained save. Largest file size.",
        "delta": ("Saves only changes from the original level. Smallest file "
                  "size but requires the base map."),
        "both": "Saves a compact delta plus a complete fallback snapshot.",
    }

    def _update_save_mode_desc(self):
        mode = self.save_mode_combo.currentData() or "full"
        self.save_mode_desc.setText(self._SAVE_MODE_DESCS.get(mode, ""))

    def _toggle_resolution_visibility(self):
        mode = self.kiosk_mode_combo.currentText()
        self.res_group.setVisible(mode == "Windowed")

    def _create_controls_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, "Mouse")
        
        mouse_group = QGroupBox("Mouse")
        mouse_layout = QVBoxLayout()
        
        self.invert_mouse_checkbox = QCheckBox("Invert Mouse Look")
        mouse_layout.addWidget(self.invert_mouse_checkbox)
        
        self.middle_click_drag_checkbox = QCheckBox("Middle Click to Drag in 2D Views")
        mouse_layout.addWidget(self.middle_click_drag_checkbox)
        
        # Player 2 sensitivity has been moved to the Split Screen tab.
        
        mouse_group.setLayout(mouse_layout)
        layout.addWidget(mouse_group)
        
        layout.addStretch()

    def _create_keyboard_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, "Keyboard")

        columns_layout = QHBoxLayout()
        left_form = QFormLayout()
        right_form = QFormLayout()
        
        left_form.setContentsMargins(0, 0, 10, 0)
        right_form.setContentsMargins(10, 0, 0, 0)

        asset_browser_label = QLabel("T")
        left_form.addRow("Asset Browser:", asset_browser_label)

        shortcut_definitions = {
            "Clone Selected": "SPACE",
            "Delete Selected": "DEL",
            "reset_layout": "Ctrl+Shift+R",
            "save_layout": "Ctrl+Shift+S",
            "Logic Graph Editor": "Ctrl+L",
            "Logic Wizard": "Ctrl+Shift+W",
            "Hide Brush": "H",
            "Unhide All Brushes": "Shift+H",
            "Decrease Grid Size": "[",
            "Increase Grid Size": "]",
            "Use (play mode)": "E",
            "Show connections": "F1",
            "Show sprites": "F3",
            "Light Radius": "Shift+Wheel",
            "Light Intensity": "Ctrl+Wheel",
            "Free Camera": "R-Click+WASD",
        }
        
        self.shortcut_labels = {}
        
        items = list(shortcut_definitions.items())
        mid_point = (len(items) // 2) + 1
        
        for i, (action_name, shortcut_text) in enumerate(items):
            label_text = action_name.replace('_', ' ').title() + ":"
            shortcut_label = QLabel(shortcut_text)
            self.shortcut_labels[action_name] = shortcut_label
            
            if i < mid_point:
                left_form.addRow(label_text, shortcut_label)
            else:
                right_form.addRow(label_text, shortcut_label)

        switch_2d_views_label = QLabel("Ctrl+Tab")
        right_form.addRow("Switch 2D Views:", switch_2d_views_label)

        columns_layout.addLayout(left_form)
        columns_layout.addLayout(right_form)
        layout.addLayout(columns_layout)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addSpacing(10)
        layout.addWidget(line)
        layout.addSpacing(10)
        
        header_label = QLabel("Function Keys (Rebindable)")
        header_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(header_label)

        rebind_grid = QGridLayout()
        rebind_grid.setSpacing(10)
        
        self.key_f1_edit = QKeySequenceEdit()
        rebind_grid.addWidget(QLabel("Show Logic Links:"), 0, 0)
        rebind_grid.addWidget(self.key_f1_edit, 0, 1)
        
        self.key_f2_edit = QKeySequenceEdit()
        rebind_grid.addWidget(QLabel("Toggle Wireframe:"), 0, 2)
        rebind_grid.addWidget(self.key_f2_edit, 0, 3)

        self.key_f3_edit = QKeySequenceEdit()
        rebind_grid.addWidget(QLabel("System Monitor:"), 1, 0)
        rebind_grid.addWidget(self.key_f3_edit, 1, 1)
        
        self.key_f5_edit = QKeySequenceEdit()
        rebind_grid.addWidget(QLabel("Toggle Play Mode:"), 1, 2)
        rebind_grid.addWidget(self.key_f5_edit, 1, 3)
        
        layout.addLayout(rebind_grid)
        layout.addStretch()

    def _create_split_screen_tab(self):
        """Split Screen settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, "Split Screen")
        
        p2_group = QGroupBox("Player 2 (Split‑Screen)")
        p2_layout = QVBoxLayout()
        
        # Turn sensitivity
        sens_layout = QHBoxLayout()
        sens_layout.addWidget(QLabel("Turn Sensitivity:"))
        self.p2_turn_sensitivity_spin = QSpinBox()
        self.p2_turn_sensitivity_spin.setRange(30, 500)
        self.p2_turn_sensitivity_spin.setSuffix(" °/s")
        self.p2_turn_sensitivity_spin.setValue(10)
        self.p2_turn_sensitivity_spin.setToolTip(
            "How many degrees per second Player 2 turns when pressing Left/Right or using the gamepad right stick."
        )
        sens_layout.addWidget(self.p2_turn_sensitivity_spin)
        p2_layout.addLayout(sens_layout)
        
        p2_group.setLayout(p2_layout)
        layout.addWidget(p2_group)
        layout.addStretch()

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QCheckBox::indicator:checked {
                background-color: #F08000;
                border: 1px solid #333;
                image: none;
            }
            QCheckBox::indicator:unchecked {
                background-color: #425f5d;
                border: 1px solid #333;
            }
            QCheckBox::indicator {
                width: 25px;
                height: 25px;
            }
            QCheckBox::indicator:hover {
                border: 1px solid #555;
            }
            QCheckBox::indicator:checked:hover {
                background-color: #FF8C00;
                image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16'><path fill='white' d='M6 12.5l-4-4 1.4-1.4L6 9.7l6.6-6.6L14 4.5z'/></svg>");
                image-position: center;
            }
            QCheckBox::indicator:unchecked:hover {
                background-color: #4A6B73;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #555;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)

    def load_settings(self):
        self.show_caulk_checkbox.setChecked(self.config.getboolean('Display', 'show_caulk', fallback=True))
        self.sync_selection_checkbox.setChecked(self.config.getboolean('Display', 'sync_selection', fallback=True))
        self.click_select_3d_checkbox.setChecked(self.config.getboolean('Display', 'click_select_3d', fallback=False))
        selection_trans = self.config.getint('Display', 'selection_transparency', fallback=50)
        self.selection_transparency_slider.setValue(selection_trans)
        self.selection_transparency_label.setText(f"{selection_trans}%")
        self.show_connections_checkbox.setChecked(self.config.getboolean('Display', 'show_connections', fallback=True))
        self.locked_not_selectable_checkbox.setChecked(self.config.getboolean('Display', 'locked_not_selectable_2d', fallback=True))
        glow_arrow_scale = self.config.getint('Display', 'glow_arrow_scale', fallback=100)
        self.glow_arrow_scale_slider.setValue(glow_arrow_scale)
        self.glow_arrow_scale_label.setText(f"{glow_arrow_scale}%")
        
        self.show_fps_checkbox.setChecked(self.config.getboolean('Display', 'show_fps', fallback=True))
        self.always_show_sysmon_checkbox.setChecked(self.config.getboolean('Display', 'always_show_sysmon', fallback=False))
        self.always_show_io_debug_checkbox.setChecked(self.config.getboolean('Display', 'always_show_io_debug', fallback=True))
        self.disable_toasts_checkbox.setChecked(self.config.getboolean('Display', 'disable_toasts', fallback=False))
        self.font_size_spinbox.setValue(self.config.getint('Display', 'font_size', fallback=10))
        self.vsync_checkbox.setChecked(self.config.getboolean('Display', 'vsync', fallback=False))
        self.dpi_scaling_checkbox.setChecked(self.config.getboolean('Display', 'high_dpi_scaling', fallback=False))
        self.big_toolbar_buttons_checkbox.setChecked(self.config.getboolean('Display', 'big_toolbar_buttons', fallback=False))
        self.animate_connections_checkbox.setChecked(self.config.getboolean('Display', 'animate_connections', fallback=False))
        
        is_arm, _ = self._detect_arm_platform()
        default_arm_mode = True
        default_shadows = not is_arm
        self.arm_mode_checkbox.setChecked(self.config.getboolean('Renderer', 'arm_mode', fallback=default_arm_mode))
        self.shadows_enabled_checkbox.setChecked(self.config.getboolean('Renderer', 'shadows_enabled', fallback=default_shadows))

        self.physics_checkbox.setChecked(self.config.getboolean('Settings', 'physics', fallback=True))
        self.show_hud_checkbox.setChecked(self.config.getboolean('Display', 'show_hud', fallback=True))

        save_mode = str(self.config.get('Settings', 'save_mode', fallback='full')).strip().lower()
        idx = self.save_mode_combo.findData(save_mode)
        self.save_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._update_save_mode_desc()

        self.invert_mouse_checkbox.setChecked(self.config.getboolean('Controls', 'invert_mouse', fallback=False))
        self.middle_click_drag_checkbox.setChecked(self.config.getboolean('Controls', 'middle_click_drag', fallback=False))

        self.place_camera_at_player_start_checkbox.setChecked(
        self.config.getboolean('Display', 'place_camera_at_player_start', fallback=True)
        )
        
        self.p2_turn_sensitivity_spin.setValue(
            self.config.getint('Controls', 'p2_turn_sensitivity', fallback=10)
        )
        
        self.key_f1_edit.setKeySequence(QKeySequence(self.config.get('Shortcuts', 'key_show_connections', fallback='F1')))
        self.key_f2_edit.setKeySequence(QKeySequence(self.config.get('Shortcuts', 'key_toggle_wireframe', fallback='F2')))
        self.key_f3_edit.setKeySequence(QKeySequence(self.config.get('Shortcuts', 'key_sysmon', fallback='F3')))
        self.key_f5_edit.setKeySequence(QKeySequence(self.config.get('Shortcuts', 'key_play_mode', fallback='F5')))

        k_mode = self.config.get('Kiosk', 'window_mode', fallback='Fullscreen')
        idx = self.kiosk_mode_combo.findText(k_mode)
        if idx >= 0:
            self.kiosk_mode_combo.setCurrentIndex(idx)
        self.kiosk_res_w.setValue(self.config.getint('Kiosk', 'res_width', fallback=1280))
        self.kiosk_res_h.setValue(self.config.getint('Kiosk', 'res_height', fallback=720))
        self.launch_in_editor_checkbox.setChecked(
            self.config.getboolean('Kiosk', 'launch_in_editor', fallback=False)
        )

    def accept(self):
        self._save_settings()
        super().accept()

    def change_key(self, control_name):
        pass

    def keyPressEvent(self, event):
        super().keyPressEvent(event)

    def _has_unsaved_work(self):
        if not self.main_window:
            return False
        
        has_content = False
        if hasattr(self.main_window, 'state'):
            state = self.main_window.state
            has_content = (len(getattr(state, 'brushes', [])) > 0 or 
                          len(getattr(state, 'things', [])) > 0)
        
        no_file = not getattr(self.main_window, 'file_path', None)
        
        has_undo_history = False
        if hasattr(self.main_window, 'undo_stack'):
            has_undo_history = len(self.main_window.undo_stack) > 0
        
        return (has_content and no_file) or has_undo_history

    def _apply_and_restart(self):
        if self._has_unsaved_work():
            reply = QMessageBox.warning(
                self,
                "Unsaved Work",
                "Unsaved work will be lost!\n\nAre you sure you want to restart?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        self._save_settings()
        
        if self.main_window and hasattr(self.main_window, 'save_config'):
            self.main_window.save_config()
        
        self._restart_application()

    def _save_settings(self):
        if not self.config.has_section('Display'): 
            self.config.add_section('Display')
        
        self.config.set('Display', 'show_caulk', str(self.show_caulk_checkbox.isChecked()))
        self.config.set('Display', 'sync_selection', str(self.sync_selection_checkbox.isChecked()))
        self.config.set('Display', 'click_select_3d', str(self.click_select_3d_checkbox.isChecked()))
        self.config.set('Display', 'selection_transparency', str(self.selection_transparency_slider.value()))
        self.config.set('Display', 'show_connections', str(self.show_connections_checkbox.isChecked()))
        self.config.set('Display', 'locked_not_selectable_2d', str(self.locked_not_selectable_checkbox.isChecked()))
        self.config.set('Display', 'glow_arrow_scale', str(self.glow_arrow_scale_slider.value()))
        
        self.config.set('Display', 'show_fps', str(self.show_fps_checkbox.isChecked()))
        self.config.set('Display', 'always_show_sysmon', str(self.always_show_sysmon_checkbox.isChecked()))
        self.config.set('Display', 'always_show_io_debug', str(self.always_show_io_debug_checkbox.isChecked()))
        self.config.set('Display', 'disable_toasts', str(self.disable_toasts_checkbox.isChecked()))
        self.config.set('Display', 'font_size', str(self.font_size_spinbox.value()))
        self.config.set('Display', 'vsync', str(self.vsync_checkbox.isChecked()))
        self.config.set('Display', 'high_dpi_scaling', str(self.dpi_scaling_checkbox.isChecked()))
        self.config.set('Display', 'big_toolbar_buttons', str(self.big_toolbar_buttons_checkbox.isChecked()))
        self.config.set('Display', 'animate_connections', str(self.animate_connections_checkbox.isChecked()))
        
        if not self.config.has_section('Renderer'): 
            self.config.add_section('Renderer')
        self.config.set('Renderer', 'arm_mode', str(self.arm_mode_checkbox.isChecked()))
        self.config.set('Renderer', 'shadows_enabled', str(self.shadows_enabled_checkbox.isChecked()))
        
        self.config.set('Display', 'show_hud', str(self.show_hud_checkbox.isChecked()))
        
        if not self.config.has_section('Settings'):
            self.config.add_section('Settings')
        self.config.set('Settings', 'physics', str(self.physics_checkbox.isChecked()))
        self.config.set('Settings', 'save_mode',
                        self.save_mode_combo.currentData() or 'full')

        if not self.config.has_section('Controls'): 
            self.config.add_section('Controls')
        self.config.set('Controls', 'invert_mouse', str(self.invert_mouse_checkbox.isChecked()))
        self.config.set('Controls', 'middle_click_drag', str(self.middle_click_drag_checkbox.isChecked()))
        self.config.set('Controls', 'p2_turn_sensitivity', str(self.p2_turn_sensitivity_spin.value()))
        
        if not self.config.has_section('Shortcuts'):
            self.config.add_section('Shortcuts')
        self.config.set('Shortcuts', 'key_show_connections', self.key_f1_edit.keySequence().toString())
        self.config.set('Shortcuts', 'key_toggle_wireframe', self.key_f2_edit.keySequence().toString())
        self.config.set('Shortcuts', 'key_sysmon', self.key_f3_edit.keySequence().toString())
        self.config.set('Shortcuts', 'key_play_mode', self.key_f5_edit.keySequence().toString())

        if not self.config.has_section('Kiosk'):
            self.config.add_section('Kiosk')
        self.config.set('Kiosk', 'window_mode', self.kiosk_mode_combo.currentText())
        self.config.set('Kiosk', 'res_width', str(self.kiosk_res_w.value()))
        self.config.set('Kiosk', 'res_height', str(self.kiosk_res_h.value()))
        self.config.set('Kiosk', 'launch_in_editor',
                        str(self.launch_in_editor_checkbox.isChecked()))

        self.config.set('Display', 'place_camera_at_player_start',
                str(self.place_camera_at_player_start_checkbox.isChecked()))

    def _restart_application(self):
        from PyQt5.QtWidgets import QApplication
        
        python = sys.executable
        script = sys.argv[0]
        args = sys.argv[1:]
        
        if self.main_window:
            try:
                self.main_window.closeEvent = lambda e: e.accept()
                self.main_window.close()
            except Exception:
                pass
        
        os.execl(python, python, script, *args)