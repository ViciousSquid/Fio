import sys
import json
import os
import subprocess
import random
import numpy as np
import configparser
import math
import copy
import uuid
import glm
import time


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QFileDialog, QDialog, QWidget, QLabel, QVBoxLayout,
    QGraphicsOpacityEffect, QInputDialog, QColorDialog, QProgressDialog, QAction, QToolBar, QDockWidget,
    QPushButton, QDialogButtonBox, QHBoxLayout
)
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtCore import Qt, QByteArray, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt5.QtGui import QKeySequence, QPixmap, QCursor, QColor, QIcon

from editor.things import Light, PlayerStart, Prop, update_all_counters_from_entities
from editor.SettingsWindow import SettingsWindow
from editor.ui import LAYOUT_VERSION, Ui_MainWindow
from editor.tooltips import set_tooltips_enabled
from engine.constants import TILE_SIZE
from engine.glasses import DEFAULT_GLASSES, normalize_glasses
from engine import brush_geometry
from engine.change_journal import moved, touch
from engine.fileio import write_json_atomic
from editor.view_2d import View2D
from editor.editor_state import EditorState
from editor import component_edit
from editor import face_texture
from editor.component_edit import (
    ComponentController, COMPONENT_MODES, MODE_LABELS,
    MODE_OBJECT, MODE_FACE, MODE_EDGE, MODE_VERTEX,
)
from editor.terrain_editor import TerrainEditorPanel
from editor.debug_console import DebugConsole, CommandInput, debug_log
from editor.console_commands import ConsoleCommandHandler


class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        # CRITICAL: Remove Qt.SubWindow to use parent coordinates
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAlignment(Qt.AlignCenter)
        self.hide()
        
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        
        self.anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        # Toasts fade in and out over exactly 0.5 seconds.  The opacity effect
        # covers the complete QLabel, so the coloured background fades with
        # the text rather than popping in/out separately.
        self.anim.setDuration(500)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fade_out)
        
        self.current_toast_id = None

    def update_position(self):
        if not self.isVisible() or not self.parentWidget():
            return
            
        parent = self.parentWidget()
        # Calculate horizontal center
        x = max(0, (parent.width() - self.width()) // 2)
        
        # FIX: Remove the -60 offset to align with the bottom status bar area.
        # parent.height() represents the absolute bottom of the MainWindow.
        y = parent.height() - self.height()
        
        self.move(x, y)
        self.raise_()  # Ensures it stays above the Status Bar widgets

    def show_message(self, text, parent_widget=None, is_error=False, duration=None, 
                     is_tooltip=False, toast_id=None):
        """Show toast notification with STRICT bottom-middle positioning."""
        if is_tooltip:
            bg_color = "#2b2b2b"
        elif is_error:
            bg_color = "#8B0000"
        else:
            bg_color = "#2E6F40"
        
        self.setTextFormat(Qt.PlainText)
        self.setStyleSheet(f"""
            QLabel {{
                background-color: {bg_color};
                color: white;
                padding: 10px 20px;
                border-radius: 5px;
                font-weight: bold;
                font-size: 14px;
            }}
        """)
        
        self.setText(text)
        self.adjustSize()
        self.update_position()  # Force immediate positioning
        
        self.show()
        self.raise_()
        
        self.opacity_effect.setOpacity(0)
        self.anim.setDirection(QPropertyAnimation.Forward)
        self.anim.setStartValue(0)
        self.anim.setEndValue(1)
        self.anim.start()
        
        self.current_toast_id = toast_id
        
        if duration == 0:
            self.timer.stop()
        else:
            final_duration = duration if duration is not None else (4000 if is_error else 2500)
            self.timer.start(final_duration)

    def fade_out(self):
        self.anim.setDirection(QPropertyAnimation.Backward)
        self.anim.setEndValue(0)
        self.anim.start()
        

class MainWindow(QMainWindow):
    load_level_signal = pyqtSignal(str)
    def __init__(self, root_dir):
        super().__init__()
        self.root_dir = root_dir
        self.root_dir = os.path.abspath(root_dir) 
        self.assets_root = os.path.join(self.root_dir, 'assets')
        self.debug_console = None
        self.key_bindings = {}

        self.config = configparser.ConfigParser()
        self.config.optionxform = str          # preserve case of option names
        self.config_path = 'settings.ini'
        self.load_config()
        self.load_key_bindings()

        self.unsaved_changes = False
        #: The world as Play started, when Stop is set to restore it.
        self._pre_play_world = None
        self.file_path = None
        self.recent_files = []
        self.load_level_signal.connect(self.load_level_file)

        self.setWindowTitle("Fio")
        self.setWindowIcon(QIcon(os.path.join(self.root_dir, 'assets', 'icon.ico')))
        self.setGeometry(100, 100, 1600, 900)
        self.setMinimumSize(1280, 800)
        self.state = EditorState()
        # Checkpoints re-journal their objects once the editing event is done.
        self.state.post_event = lambda fn: QTimer.singleShot(0, fn)
        self.load_recent_files()
        
        # Initialize selected_objects list for multi-selection support
        if not hasattr(self.state, 'selected_objects'):
            self.state.selected_objects = []
        if not hasattr(self.state, 'selected_object'):
            self.state.selected_object = None
            
        self.keys_pressed = set()
        self._brush_clipboard = None  # For Ctrl+C / Ctrl+V brush copy-paste
        self.grid_visible = True
        self.clip_mode = False  # Radiant-style clip/slice tool (toggled with X)
        self.rotate_mode = False  # Free-rotate tool: drag in a 2D view to spin
        # Base 2D interaction tool (Hammer-style): 'select' drags a rubber-band
        # marquee, 'brush' drags out new box geometry.  Clip/rotate are separate
        # drag tools layered on top and take precedence while active.
        self.tool_mode = 'brush'
        # Shared component-selection model (object / face / edge / vertex).
        # Both the 2D views and the 3D viewport drive this one controller, so
        # "click geometry, drag geometry" means the same thing in either view.
        self.components = ComponentController()
        # Clone-and-place: after Shift+Space the duplicate follows the cursor
        # until a click drops it (Radiant-style).  Holds the objects being
        # placed and the view-plane point they were grabbed at.
        self.clone_placement = None
        # Wall thickness the Hollow tool offers next time, so repeated hollows
        # are a dialog keypress apart rather than a re-typed number.
        self.last_hollow_thickness = 16
        self.preview_timer = QTimer(self)  # OPTIMIZATION: Added parent=self for proper cleanup
        self.preview_timer.timeout.connect(self.update_mover_preview)
        self.preview_data = {} 
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.update_recent_files_menu()
        self.setup_package_actions() 
        self.update_title()
        
        # The object name is the label Help > Keys lists these under; an
        # unnamed QShortcut cannot describe itself.
        self.ctrl_tab_shortcut = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.ctrl_tab_shortcut.setObjectName("Cycle the 2D view")
        self.ctrl_tab_shortcut.activated.connect(self.cycle_2d_view)

        # Page Up / Page Down rotate brush-face textures 90 degrees. Window-
        # level shortcuts so they fire no matter which panel has focus.
        self.tex_rot_cw_shortcut = QShortcut(QKeySequence(Qt.Key_PageUp), self)
        self.tex_rot_cw_shortcut.setObjectName("Rotate the face texture 90 clockwise")
        self.tex_rot_cw_shortcut.activated.connect(lambda: self.rotate_textures(1))
        self.tex_rot_ccw_shortcut = QShortcut(QKeySequence(Qt.Key_PageDown), self)
        self.tex_rot_ccw_shortcut.setObjectName(
            "Rotate the face texture 90 anticlockwise")
        self.tex_rot_ccw_shortcut.activated.connect(lambda: self.rotate_textures(-1))
        self.setFocus()
        self.update_global_font()
        self.apply_tooltip_settings()
        self.load_layout()
        
        self.terrain = None
        self.terrain_editor_window = None
        self.surface_inspector = None  # lazily created Face-mode Surface Inspector
        self._entity_inspectors = {}   # id(entity) -> open EntityInspector (API 1.5.0)
        self.shortcuts_window = None   # lazily created Help > Keys window

        # debug_console is embedded in the properties tab widget (created in setupUi)
        self.debug_console = DebugConsole.get_instance(self)
        # --- Connect the command_issued signal to the command handler ---
        self.console_handler = ConsoleCommandHandler(self)
        self.debug_console.command_issued.connect(self.console_handler.handle_command)

        # --- Play-mode console overlay (Quake-style drop-down input) ---
        self._create_play_console_overlay()

        # If configured, switch to the Debug Console tab on startup
        if self.config.getboolean('Display', 'always_show_io_debug', fallback=False):
            if hasattr(self, 'properties_tab_widget'):
                idx = self.properties_tab_widget.indexOf(self.debug_console)
                self.properties_tab_widget.setCurrentIndex(idx)

        self.ui.action_asset_browser.triggered.connect(self.toggle_asset_browser)

        # Enable sysmon at launch if configured
        if self.config.getboolean('Display', 'always_show_sysmon', fallback=False):
            self.view_3d.sysmon.set_active(True)
            self.view_3d.sysmon.set_expanded(True)
            if hasattr(self, 'system_monitor_action'):
                self.system_monitor_action.setChecked(True)

        self.show_logic_links = True
        
        # Tooltips
        self.camera_movement_learned = self.config.getboolean('Tooltips', 'camera_movement_learned', fallback=False)
        self.startup_tooltip_shown = False
        self.tooltip_tips = [
            "Right-click + WASD: Move camera",
            "Mouse wheel: Zoom in/out",
            "Ctrl+Tab: Cycle 2D views",
            "Shift+Space: Clone, then click to place it",
            "H: Hide selected, Shift+H: Unhide all",
            "Delete: Remove selected brush/object",
            "Add Player Start before Play Mode",
            "Shift+Wheel on Light: Adjust radius",
            "Ctrl+Wheel on Light: Adjust intensity",
            "Ctrl+Drag from Trigger to Connect",
            "Triggers activate movers, doors, etc.",
            "F5: Enter/Exit Play Mode",
            "F3: Toggle System Monitor",
            "F4: Toggle sprite visibility",
            "F1: Toggle connection lines",
            "Ctrl+Click: Multi-select",
            "Ctrl+C/V: Copy & Paste brushes",
            "T: Toggle Asset Browser",
        ]
        self.last_tooltip_time = 0
        self.tooltip_interval = 30  # Seconds between occasional tooltips
        
        # Timer for occasional tooltips
        self.tooltip_timer = QTimer(self)
        self.tooltip_timer.timeout.connect(self._check_occasional_tooltip)
        self.tooltip_timer.start(10000)  # Check every 10 seconds (tooltip_interval throttles display)
        
        # Track right-click state for camera movement detection
        self.right_mouse_held = False
        self.view_3d.installEventFilter(self)
        # Install event filter on self to catch arrow keys globally for nudging
        self.installEventFilter(self)
        
        # Show startup tooltip after window is shown
        QTimer.singleShot(1500, self._show_startup_tooltip)
        
        # Autosave Timer
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.setup_autosave()

        # REMOVED: Redundant 200ms play button sync timer.
        # All code paths that change play_mode already call update_play_button_color() directly:
        #   - enter_play_mode() → update_play_button_color()
        #   - _exit_play_mode() → update_play_button_color()
        #   - load_level_file() → enter_play_mode() → update_play_button_color()

        # Overlay management for Properties dock
        self._original_properties_widget = None   # the widget that was replaced
        self._current_overlay = None              # currently active overlay widget
        self._overlay_close_callback = None       # optional cleanup when overlay is closed


    def _close_current_overlay(self):
        """Close any active overlay and restore the original Properties dock content."""
        if self._current_overlay is not None:
            # Call custom close callback if provided
            if self._overlay_close_callback:
                self._overlay_close_callback()
                self._overlay_close_callback = None

            # Remove the overlay widget
            self._current_overlay.setParent(None)
            self._current_overlay.deleteLater()
            self._current_overlay = None

            # Restore original widget
            if self._original_properties_widget:
                self.properties_dock.setWidget(self._original_properties_widget)
                self._original_properties_widget = None

    def _cleanup_export_overlay(self):
        """Clean up after the export overlay is closed, however it closes.

        An unsaved level is exported from a temporary copy written into
        ``maps/``; it is removed here so Cancel or the close button do not
        leave an ``export_temp_*.json`` behind in the project.
        """
        if hasattr(self, '_export_dialog'):
            self._export_dialog = None
        self._discard_export_temp_file()
        # The overlay itself will be destroyed by _close_current_overlay

    def _discard_export_temp_file(self):
        temp_file = getattr(self, '_export_temp_file', None)
        self._export_temp_file = None
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except OSError as e:
                print(f"Warning: could not delete temp file {temp_file}: {e}")

    def _show_overlay(self, overlay_widget, close_callback=None):
        """
        Replace the Properties dock content with overlay_widget.
        Any existing overlay is closed first.
        close_callback is called when the overlay is later closed.
        """
        self._close_current_overlay()
        self._original_properties_widget = self.properties_dock.widget()
        self.properties_dock.setWidget(overlay_widget)
        self._current_overlay = overlay_widget
        self._overlay_close_callback = close_callback

    def update_title(self):
        """Updates window title with filename and dirty status."""
        fname = os.path.basename(self.file_path) if self.file_path else "Untitled"
        dirty_marker = "*" if self.unsaved_changes else ""
        self.setWindowTitle(f"Fio - {fname} {dirty_marker}")

    def load_key_bindings(self):
        if self.config.has_section('KeyBindings'):
            for key, command in self.config.items('KeyBindings'):
                self.key_bindings[key] = command

    def save_key_bindings(self):
        if not self.config.has_section('KeyBindings'):
            self.config.add_section('KeyBindings')
        else:
            self.config.remove_section('KeyBindings')
            self.config.add_section('KeyBindings')
        for key, command in self.key_bindings.items():
            self.config.set('KeyBindings', key, command)
        self.save_config()

    def set_key_binding(self, key_str, command):
        """Bind a key to a console command. Warn if key already bound and ask to overwrite."""
        from PyQt5.QtWidgets import QMessageBox

        if key_str in self.key_bindings:
            old_cmd = self.key_bindings[key_str]
            reply = QMessageBox.question(
                self,
                "Key Binding Conflict",
                f"Key '{key_str}' is already bound to:\n\n  {old_cmd}\n\nOverwrite?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return False

        self.key_bindings[key_str] = command
        self.save_key_bindings()
        return True

    def mark_as_modified(self):
        """Mark the project as having unsaved changes."""
        if not self.unsaved_changes:
            self.unsaved_changes = True
            self.update_title()
    
    def mark_dirty(self):
        """Alias for mark_as_modified — called by property_editor and other subsystems."""
        self.mark_as_modified()

    def check_unsaved_changes(self):
        """
        Checks for unsaved changes. Returns True if it's safe to proceed 
        (changes saved, discarded, or no changes), False if canceled.
        """
        if not self.unsaved_changes:
            return True
            
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Question)
        msg.setWindowTitle("Unsaved Changes")
        msg.setText("You have unsaved changes.")
        msg.setInformativeText("Do you want to save your changes?")
        msg.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        msg.setDefaultButton(QMessageBox.Save)
        
        ret = msg.exec_()
        
        if ret == QMessageBox.Save:
            self.save_level()
            # If save failed (user cancelled file dialog), unsaved is still True
            return not self.unsaved_changes 
        elif ret == QMessageBox.Discard:
            self.unsaved_changes = False
            return True
        else: # Cancel
            return False

    def load_recent_files(self):
        # Ensure the list exists by default (fixes AttributeError on first run)
        self.recent_files = [] 

        if self.config.has_section('History') and self.config.has_option('History', 'recent_files'):
            try:
                raw_data = self.config.get('History', 'recent_files')
                if raw_data:
                    self.recent_files = json.loads(raw_data)
            except Exception:
                # Fallback to empty list on JSON error
                self.recent_files = []

    def save_recent_files(self):
        if not self.config.has_section('History'):
            self.config.add_section('History')
        self.config.set('History', 'recent_files', json.dumps(self.recent_files))
        self.save_config()

    def add_recent_file(self, file_path):
        # Normalize path
        file_path = os.path.abspath(file_path)
        
        if file_path in self.recent_files:
            self.recent_files.remove(file_path)
        
        self.recent_files.insert(0, file_path)
        
        # Keep only last 5
        if len(self.recent_files) > 5:
            self.recent_files = self.recent_files[:5]
            
        self.save_recent_files()
        self.update_recent_files_menu()

    def update_recent_files_menu(self):
        if not hasattr(self, 'recent_menu'):
            return
        
        # Actions are parented to the menu: clear() deletes only the actions
        # it owns, so window-owned ones piled up on every map load.
        self.recent_menu.clear()
        
        if not self.recent_files:
            dummy = QAction("No recent files", self.recent_menu)
            dummy.setEnabled(False)
            self.recent_menu.addAction(dummy)
            return
            
        for path in self.recent_files:
            # Check if file still exists
            if not os.path.exists(path):
                continue
                
            fname = os.path.basename(path)
            action = QAction(fname, self.recent_menu)
            action.setToolTip(path)
            # Use lambda with default arg to capture variable in loop
            action.triggered.connect(lambda checked, p=path: self.load_level_file(p))
            self.recent_menu.addAction(action)

    def setup_autosave(self):
        enabled = self.config.getboolean('Editor', 'autosave_enabled', fallback=True)
        interval_min = self.config.getint('Editor', 'autosave_interval', fallback=10)
        
        if enabled:
            # Convert minutes to milliseconds
            self.autosave_timer.start(interval_min * 60 * 1000)
        else:
            self.autosave_timer.stop()

    def autosave(self):
        """Background autosave to a specific autosave file."""
        if not self.unsaved_changes:
            return # Nothing to save
            
        try:
            # Ensure maps directory exists
            autosave_dir = os.path.join(self.root_dir, "maps")
            if not os.path.exists(autosave_dir):
                os.makedirs(autosave_dir)
                
            # Use a generic autosave name or derived from current file
            if self.file_path:
                base = os.path.splitext(os.path.basename(self.file_path))[0]
                save_name = f"{base}_autosave.json"
            else:
                save_name = "untitled_autosave.json"
                
            save_path = os.path.join(autosave_dir, save_name)
            
            write_json_atomic(save_path, self.state.get_level_data(), indent=4)

            print(f"[Autosave] Saved to {save_path}")
            # Do NOT clear unsaved_changes flag on autosave
            
        except Exception as e:
            print(f"Autosave failed: {e}")

    def show_procedural_map_generator(self):
        """Open the procedural map generator as an overlay in the Properties dock."""
        from editor.procedural_generator import ProceduralMapWidget

        # Create the generator widget (it will emit map_generated when closed)
        generator = ProceduralMapWidget(self.properties_dock)
        generator.map_generated.connect(self._on_procedural_map_generated)

        # Show it in the overlay system
        self._show_overlay(generator)

    def _on_procedural_map_generated(self, map_data):
        """
        Handle the signal from the generator:
        - if map_data is not None → load the generated map (keep generator open)
        - if map_data is None → user clicked X → close the overlay
        """
        if map_data is not None:
            # Loaded straight from memory: the level has no file until the
            # user saves it, so it opens untitled and unsaved.
            if self._load_level(map_data, None):
                self.show_toast("Generated map loaded – use Save As to keep it")
        else:
            # User closed the generator – close the overlay
            self._close_current_overlay()

    def center_2d_views_on(self, world_pos):
        """Center all 2D views on the given world position (list/tuple of [x, y, z])."""
        from PyQt5.QtCore import QPointF
        self.view_top.pan_offset = QPointF(world_pos[0], world_pos[2])
        self.view_side.pan_offset = QPointF(world_pos[2], world_pos[1])
        self.view_front.pan_offset = QPointF(world_pos[0], world_pos[1])
        self.view_top.update()
        self.view_side.update()
        self.view_front.update()


    @staticmethod
    def _object_focus_target(obj):
        """``(centre, radius)`` of a brush or thing, in world units.

        The radius is what decides how far back to stand: a 2048-unit floor and
        a light entity both want to fill the view, and a fixed distance would
        bury one and lose the other.
        """
        if isinstance(obj, dict):
            pos = obj.get('pos') or [0.0, 0.0, 0.0]
            size = obj.get('size') or [64.0, 64.0, 64.0]
            centre = [float(pos[0]), float(pos[1]), float(pos[2])]
            radius = max(float(size[0]), float(size[1]), float(size[2])) * 0.5
        else:
            pos = getattr(obj, 'pos', None) or [0.0, 0.0, 0.0]
            centre = [float(pos[0]), float(pos[1]), float(pos[2])]
            radius = 48.0
            getter = getattr(obj, 'get_radius', None)
            if callable(getter):
                try:
                    radius = max(radius, float(getter()))
                except Exception:
                    pass
        return centre, max(16.0, radius)

    def focus_on_object(self, obj):
        """Centre every view on one object, in 2D and in 3D."""
        if obj is None:
            return
        centre, radius = self._object_focus_target(obj)
        self.focus_on_bounds(centre, radius)
        name = (obj.get('name') if isinstance(obj, dict)
                else obj.properties.get('name', '')) or 'object'
        self.show_toast("Focused on %s" % name)

    def focus_on_bounds(self, centre, radius):
        """Centre every view on a point, framed for something *radius* across.

        The 3D camera keeps its current yaw and pitch and simply moves so the
        target is in front of it. Snapping to a canned angle would be easier and
        would throw away the orientation the user had chosen, which is usually
        the thing they were reasoning about.
        """
        radius = max(16.0, float(radius))
        self.center_2d_views_on(centre)
        # Zoom so the object spans a comfortable fraction of the viewport rather
        # than whatever zoom happened to be set.
        for view in (self.view_top, self.view_side, self.view_front):
            try:
                extent = min(view.width(), view.height())
                if extent > 0:
                    view.zoom_factor = max(0.05, min(8.0, extent / (radius * 6.0)))
                view.update()
            except Exception:
                pass

        camera = getattr(getattr(self, 'view_3d', None), 'camera', None)
        if camera is not None:
            try:
                import glm
                front = camera.get_front_vector()
                distance = max(radius * 3.0, 128.0)
                camera.pos = glm.vec3(centre[0], centre[1], centre[2]) - front * distance
                self.view_3d.update()
            except Exception:
                pass

    def moveEvent(self, event):
        """Handle window move."""
        super().moveEvent(event)

    def toggle_debug_console(self):
        # --- Play mode: use the overlay instead of switching tabs ---
        if self.view_3d.play_mode:
            if self._is_play_console_visible():
                self._hide_play_console_overlay()
            else:
                self._show_play_console_overlay()
            return

        # --- Editor mode: switch tabs as before ---
        tab = self.properties_tab_widget
        console_idx = tab.indexOf(self.debug_console)
        # Ensure the properties dock is visible
        self.properties_dock.setVisible(True)
        if tab.currentIndex() == console_idx:
            # Already on the console tab — switch back to Properties
            tab.setCurrentIndex(0)
        else:
            tab.setCurrentIndex(console_idx)

    def show_properties_panel(self):
        """Bring the Properties tab to the front and make sure it is visible.

        The dock is tabbed with the Debug Console and can be closed outright,
        so showing the panel means three things, not one: the dock visible, the
        dock raised above anything docked over it, and the Properties tab
        selected rather than the console.
        """
        dock = getattr(self, 'properties_dock', None)
        tab = getattr(self, 'properties_tab_widget', None)
        if dock is not None:
            dock.setVisible(True)
            dock.raise_()
        if tab is not None:
            index = tab.indexOf(self.property_editor)
            if index >= 0:
                tab.setCurrentIndex(index)

    def _clear_terrain(self):
        """Remove the terrain object and clear all references."""
        # Destroy the live terrain object
        if self.terrain is not None:
            self.terrain.cleanup()
            self.terrain = None

        # Clear terrain data from editor state
        if hasattr(self.state, 'terrain_data'):
            self.state.terrain_data = None

        # Notify the 3D view's logic thread (if any) that terrain is gone
        if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
            self.view_3d.logic_thread.set_terrain(None)

        # Close the terrain editor panel if it is open in the Properties dock
        if self.terrain_editor_window is not None:
            if self._current_overlay is self.terrain_editor_window:
                self._close_current_overlay()
            self.terrain_editor_window = None

        # Force a UI refresh
        self.update_all_ui()

    # ------------------------------------------------------------------
    #  Play-mode console overlay helpers
    # ------------------------------------------------------------------

    def _create_play_console_overlay(self):
        """Create a translucent command overlay for use during play mode."""
        from PyQt5.QtWidgets import QFrame, QVBoxLayout
        from PyQt5.QtGui import QFont

        # Container frame — parented to view_3d so it draws on top of the 3D view
        self._play_console_frame = QFrame(self.view_3d)
        self._play_console_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(0, 0, 0, 200);
                border-bottom: 2px solid #4CAF50;
            }
        """)
        self._play_console_frame.setFixedHeight(50)
        self._play_console_frame.hide()

        layout = QVBoxLayout(self._play_console_frame)
        layout.setContentsMargins(8, 4, 8, 4)

        self._play_console_input = CommandInput(self._play_console_frame)
        self._play_console_input.setPlaceholderText("Enter command...")
        self._play_console_input.setFont(QFont("Consolas", 12))
        self._play_console_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(30, 30, 30, 220);
                color: #00FF00;
                border: 1px solid #555;
                padding: 4px 8px;
                selection-background-color: #4CAF50;
            }
        """)
        self._play_console_input.returnPressed.connect(self._on_play_console_submit)
        layout.addWidget(self._play_console_input)

    def _show_play_console_overlay(self):
        """Show the overlay and release the mouse cursor."""
        frame = self._play_console_frame
        # Stretch to full width of the 3D view
        frame.setFixedWidth(self.view_3d.width())
        frame.move(0, 0)
        frame.show()
        frame.raise_()

        # Temporarily restore cursor so the user can see what they type
        QApplication.restoreOverrideCursor()
        self.view_3d.setCursor(Qt.ArrowCursor)

        self._play_console_input.clear()
        self._play_console_input.setFocus()

    def _hide_play_console_overlay(self):
        """Hide the overlay and re-grab the mouse."""
        self._play_console_frame.hide()

        # Re-hide cursor for FPS control
        QApplication.setOverrideCursor(Qt.BlankCursor)
        self.view_3d.setFocus()

    def _is_play_console_visible(self):
        return self._play_console_frame.isVisible()

    def _on_play_console_submit(self):
        """Submit the typed command, echo it in the debug console, then hide."""
        cmd = self._play_console_input.text().strip()
        if cmd:
            self._play_console_input.add_history(cmd)
            self.console_handler.handle_command(cmd)
        self._hide_play_console_overlay()


    def cycle_2d_view(self):
        """Cycles through the 2D view tabs (Top, Side, Front) unless in play mode."""
        if self.view_3d.play_mode:
            return
        
        # Access the tab widget created in ui.py
        if hasattr(self, 'right_tabs'):
            count = self.right_tabs.count()
            if count > 0:
                next_index = (self.right_tabs.currentIndex() + 1) % count
                self.right_tabs.setCurrentIndex(next_index)

    def eventFilter(self, obj, event):
        """Track right-click state on view_3d for camera movement detection."""
        from PyQt5.QtCore import QEvent

        if obj == self.view_3d:
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.RightButton:
                    self.right_mouse_held = True
            elif event.type() == QEvent.MouseButtonRelease:
                if event.button() == Qt.RightButton:
                    self.right_mouse_held = False

        # --- Arrow key nudging: works from any widget focus ---
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right):
                # Only nudge if we have a selected object and not in play mode
                selected = self.state.selected_object
                if selected and not getattr(self.view_3d, 'play_mode', False):
                    # Determine which 2D view to use for nudging
                    current_view = self.right_tabs.currentWidget()
                    if isinstance(current_view, View2D):
                        # Let the 2D view handle the nudge (it has all the logic)
                        current_view.keyPressEvent(event)
                        return True  # Event consumed, don't propagate further

        return super().eventFilter(obj, event)

    def toggle_asset_browser(self):
        """Toggles the visibility of the Asset Browser dock."""
        if hasattr(self, 'asset_browser_dock'):
            is_visible = self.asset_browser_dock.isVisible()
            if is_visible:
                self.asset_browser_dock.hide()
            else:
                self.asset_browser_dock.show()
                # Ensure it is raised if tabbed or floating
                self.asset_browser_dock.raise_()


    def show_toast(self, message, is_error=False, duration=None):
        """Displays a notification"""
        if self.config.getboolean('Display', 'disable_toasts', fallback=False):
            return
        
        # Set the style based on the message type
        if is_error:
            bg = "#8B0000" # Dark Red
            fg = "white"
        else:
            bg = "#2b2b2b"
            fg = "white"

        self.ui.notification_label.setStyleSheet(f"""
            background-color: {bg};
            color: {fg};
            font-weight: bold;
            padding: 2px 10px;
            border-radius: 3px;
        """)
        
        self.ui.notification_label.setTextFormat(Qt.PlainText)
        self.ui.notification_label.setText(message.upper())
        
        # Auto-clear timer
        final_duration = duration if duration is not None else (4000 if is_error else 2500)
        if final_duration > 0:
            QTimer.singleShot(final_duration, lambda: self.ui.notification_label.setText(""))

    def show_tooltip(self, message, duration=4000, toast_id=None):
        """Displays teal-styled tooltips in the same area."""
        # Re-use the toast logic with teal styling
        self.ui.notification_label.setStyleSheet("""
            background-color: #2b2b2b;
            color: white;
            font-weight: bold;
            padding: 2px 10px;
            border-radius: 3px;
        """)
        self.ui.notification_label.setText(message.upper())
        
        if duration > 0:
            QTimer.singleShot(duration, lambda: self.ui.notification_label.setText(""))

    def _show_startup_tooltip(self):
        """Show the camera movement tooltip on startup if not yet learned."""
        if self.camera_movement_learned:
            return
        if self.startup_tooltip_shown:
            return
        self.startup_tooltip_shown = True
        # Duration 0 = persistent until dismissed
        self.show_tooltip("Hold right mouse to move camera with WASD", duration=0, toast_id="camera_tip")

    def _check_occasional_tooltip(self):
        """Periodically show helpful tooltips."""
        # Don't show tooltips in play mode
        if hasattr(self, 'view_3d') and self.view_3d.play_mode:
            return
        
        # Don't interrupt the startup tooltip
        if not self.camera_movement_learned and self.startup_tooltip_shown:
            return
        
        current_time = time.time()
        if current_time - self.last_tooltip_time < self.tooltip_interval:
            return
        
        # Pick a random tip
        if self.tooltip_tips:
            tip = random.choice(self.tooltip_tips)
            self.show_tooltip(tip, duration=5000)
            self.last_tooltip_time = current_time

    def on_camera_moved_with_wasd(self):
        """Called when user holds right-click and moves camera with WASD."""
        if self.camera_movement_learned:
            return
        
        self.camera_movement_learned = True
        
        # Save to config
        if not self.config.has_section('Tooltips'):
            self.config.add_section('Tooltips')
        self.config.set('Tooltips', 'camera_movement_learned', 'True')
        self.save_config()
        
        # FIX: Clear the notification label directly instead of using self.toast
        self.ui.notification_label.setText("")

    
    def open_terrain_editor(self):
        """Open the terrain editor floating window."""
        from PyQt5.QtCore import Qt
        
       # Create terrain if it doesn't exist
        if self.terrain is None:
            # Show progress dialog BEFORE creating terrain
            progress = QProgressDialog("Doing the thing...", None, 0, 0, self)
            
            # REVISION: Set window flags to force the dialog to the top of the Z-order
            progress.setWindowFlags(progress.windowFlags() | Qt.WindowStaysOnTopHint | Qt.Dialog)
            
            progress.setWindowTitle("Please Wait")
            
            # REVISION: ApplicationModal is more aggressive than WindowModal for staying on top
            progress.setWindowModality(Qt.ApplicationModal)
            
            progress.setMinimumDuration(0)
            progress.setMinimumWidth(300)
            progress.setMinimumHeight(100)
            progress.setStyleSheet("""
                QProgressDialog {
                    font-size: 14px;
                }
                QLabel {
                    font-size: 14px;
                    padding: 15px;
                }
            """)
            progress.show()
            QApplication.processEvents()  # Force the dialog to appear immediately
            
            try:
                # Now create the terrain (this is the slow part)
                from engine.terrain import Terrain
                self.terrain = Terrain(seed=42)
                
                # Load from state if available
                if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                    self.terrain.from_dict(self.state.terrain_data)
                
                # Setup shader in renderer
                if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                    self.view_3d.renderer.setup_terrain_shader(self.terrain)
                
                # Wire up terrain to logic thread for collision
                if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                    self.view_3d.logic_thread.set_terrain(self.terrain)
            finally:
                # Always close the progress dialog
                progress.close()

            # Store terrain data in state so the scene hierarchy can see it
            self.state.terrain_data = self.terrain.to_dict()
            self.scene_hierarchy.refresh_list()
        
        # Show the terrain editor as an overlay in the Properties dock (bottom
        # left pane), the same way as the procedural map generator — not a
        # floating window.
        self._show_terrain_editor_panel()

    def _show_terrain_editor_panel(self):
        """Open (or re-raise) the Terrain Editor overlay for the current terrain.

        The single place the biome/sculpt/size panel is created, shared by the
        Terrain menu action and by the Big World fill (which surfaces it so the
        generated ground can be customised). No-op without a terrain.
        """
        if getattr(self, 'terrain', None) is None:
            return
        # Already open → just make sure it's visible and on top.
        if getattr(self, 'terrain_editor_window', None) is not None:
            self.properties_dock.setVisible(True)
            self.properties_dock.raise_()
            return
        panel = TerrainEditorPanel(self.terrain, self)
        panel.terrain_changed.connect(self.on_terrain_changed)
        # _show_overlay closes any existing overlay first (whose close callback
        # may null terrain_editor_window), so store the reference afterwards.
        self._show_overlay(panel, close_callback=self._on_terrain_editor_closed)
        self.terrain_editor_window = panel
        self.properties_dock.setVisible(True)
        self.properties_dock.raise_()

    def _on_terrain_editor_closed(self):
        """Clear the reference when the terrain editor overlay is closed."""
        self.terrain_editor_window = None

    def on_terrain_changed(self):
        """Handle terrain changes."""
        if self.terrain:
            if hasattr(self.state, 'terrain_data'):
                self.state.terrain_data = self.terrain.to_dict()
        self.update_all_ui()

    def clone_selected_object(self):
        """Clone the selection and hand it to the cursor to place.

        Radiant's clone workflow is ``select -> Shift+Space -> move -> click``:
        the
        duplicate appears immediately and follows the cursor until a click drops
        it, so a row of pillars is a sequence of taps rather than a clone
        followed by a separate drag.  The initial grid offset is kept so an
        immediate click still leaves the copy beside the original instead of
        exactly on top of it, and clipboard copy/paste is untouched.
        """
        sources = list(getattr(self.state, 'selected_objects', []) or [])
        if self.state.selected_object is not None and \
                self.state.selected_object not in sources:
            sources.append(self.state.selected_object)
        if not sources:
            return
        sources, skipped = self._drop_singleton_copies(sources)
        if not sources:
            return

        # A clone while one is still being placed drops the pending one first,
        # so repeated Shift+Space never strands half-placed duplicates.
        self.finish_clone_placement()
        self.save_state()

        # Offset based on the current 2D view, in grid-size steps
        current_view = self.right_tabs.currentWidget()
        axis_map = {'top': ('x', 'z'), 'side': ('y', 'z'), 'front': ('x', 'y')}
        pos_map = {'x': 0, 'y': 1, 'z': 2}
        offset = self.grid_size_spinbox.value()
        delta = [0.0, 0.0, 0.0]
        if isinstance(current_view, View2D):
            ax1_name, ax2_name = axis_map.get(current_view.view_type, ('x', 'z'))
            delta[pos_map[ax1_name]] = offset
            delta[pos_map[ax2_name]] = offset

        # Names already in the scene, so each copy can be given one of its own
        # as it is created (two copies sharing a name would make every
        # name-addressed I/O connection ambiguous between them).
        taken_names = set(self.state.get_all_entity_names())

        clones = []
        for source in sources:
            if isinstance(source, dict):
                # Drop the runtime-only geometry caches before copying: the
                # clone derives its own, and deep-copying them is pure waste.
                new_obj = copy.deepcopy({
                    k: v for k, v in source.items()
                    if k not in brush_geometry.GEO_RUNTIME_KEYS})
                new_obj['id'] = str(uuid.uuid4())    # a clone is a new entity
                name = source.get('name', '')
                if name:
                    new_obj['name'] = self._copy_name(name, taken_names)
                self.state.brushes.append(new_obj)
            else:
                # Entities carry their whole property set across, with a fresh
                # UUID and their own name.  (A plain copy.copy would leave the
                # clone sharing the original's properties dict.)
                new_obj = source.duplicate(existing_names=taken_names)
                taken_names.add(new_obj.properties.get('name', ''))
                self.state.things.append(new_obj)
            self._translate_object(new_obj, delta)
            clones.append(new_obj)

        self.set_selected_objects(clones)
        self.show_toast("Cloned — move the cursor and click to place (Esc cancels)")

        # Hand the copies to the cursor; the 2D views drive the placement.
        self.clone_placement = {'objects': clones, 'anchor': None}

        for obj in clones:
            if isinstance(obj, dict):
                obj['_flash_until'] = time.time() + 0.5  # Flash for 0.5s
                QTimer.singleShot(500, lambda o=obj: self._clear_flash(o))
        self.update_all_ui()

    def _drop_singleton_copies(self, sources):
        """*sources* minus entities a copy of which would break a per-map
        singleton (one BigWorldSettings per map, say), and how many went.

        A copy is refused while the scene already holds an instance of its
        type; the toast says so. Brushes and ordinary entities pass through.
        """
        try:
            from plugins.integration import singleton_instance
        except Exception:
            return list(sources), 0
        kept, skipped = [], 0
        for source in sources:
            props = getattr(source, 'properties', None)
            if (not isinstance(source, dict) and isinstance(props, dict)
                    and singleton_instance(self.state.things, props.get('type'))
                    is not None):
                skipped += 1
                continue
            kept.append(source)
        if skipped:
            self.show_toast(
                "Only one of this entity is allowed per map - not copied.",
                is_error=True)
        return kept, skipped

    @staticmethod
    def _copy_name(base, taken):
        """``base`` with ``(copy)`` appended, numbered until it is unused.

        ``taken`` is updated in place so a run of clones in one operation each
        get a distinct name.
        """
        name = '%s (copy)' % base
        counter = 2
        while name in taken:
            name = '%s (copy %d)' % (base, counter)
            counter += 1
        taken.add(name)
        return name

    @staticmethod
    def _translate_object(obj, delta):
        """Move a brush (plane set included) or entity by a world delta."""
        if isinstance(obj, dict):
            if brush_geometry.brush_has_geometry(obj):
                # An angled brush carries world-space planes: move those too,
                # or the geometry stays behind while 'pos' walks off.
                brush_geometry.translate_brush(obj, delta)
                return
            pos = obj['pos']
            pos[0] += delta[0]
            pos[1] += delta[1]
            pos[2] += delta[2]
        else:
            # Assign, don't mutate in place: the assignment journals the move
            # so the 3D view's entity table picks it up this frame.
            obj.pos = [float(obj.pos[0]) + delta[0],
                       float(obj.pos[1]) + delta[1],
                       float(obj.pos[2]) + delta[2]]

    def clone_placement_active(self):
        return self.clone_placement is not None

    def move_clone_placement(self, delta):
        """Slide the objects being placed by a world-space delta."""
        if not self.clone_placement:
            return
        for obj in self.clone_placement['objects']:
            self._translate_object(obj, delta)

    def finish_clone_placement(self):
        """Drop the copies where they are.  Returns ``True`` if one was pending."""
        if not self.clone_placement:
            return False
        objects = list(self.clone_placement['objects'])
        count = len(objects)
        self.clone_placement = None
        self.unsaved_changes = True
        self.state.mark_lighting_dirty(objects)
        self.show_toast("Placed %d copy(s)" % count)
        self.update_all_ui()
        return True

    def cancel_clone_placement(self):
        """Throw the pending copies away (Esc / right-click)."""
        if not self.clone_placement:
            return False
        for obj in self.clone_placement['objects']:
            if isinstance(obj, dict):
                if obj in self.state.brushes:
                    self.state.brushes.remove(obj)
            elif obj in self.state.things:
                self.state.things.remove(obj)
        self.clone_placement = None
        self.set_selected_object(None)
        # The clone pushed an undo checkpoint it no longer needs.
        self.state.discard_last_checkpoint()
        self.show_toast("Clone cancelled")
        self.update_all_ui()
        return True

    def _clear_flash(self, obj):
        """Clear the flash flag from an object and refresh views."""
        if isinstance(obj, dict) and '_flash_until' in obj:
            del obj['_flash_until']
            self.update_all_ui()


    def tint_selected_brush(self):
        """Open colour picker dialog to tint the selected brush - unified with property editor."""
        if not isinstance(self.state.selected_object, dict):
            self.show_toast("Select a brush first", is_error=True)
            return
        
        self.save_state()
        brush = self.state.selected_object
        
        # Get current colour (0.0-1.0 range) and convert to 0-255
        current = brush.get('colour', [0.8, 0.8, 0.8])
        current_qcolor = QColor(int(current[0] * 255), int(current[1] * 255), int(current[2] * 255))
        
        color = QColorDialog.getColor(current_qcolor, self, "Choose Brush Colour")
        if color.isValid():
            # Store as 0.0-1.0 range
            brush['colour'] = [color.redF(), color.greenF(), color.blueF()]
            self.update_all_ui()


    def add_model_to_scene(self, filepath, rotation, scale):
        self.save_state()
        
        # Optional: Try to make path relative to project root for portability
        try:
            # Assuming self.root_dir is set, otherwise just use filepath
            if hasattr(self, 'root_dir'):
                assets_dir = os.path.join(self.root_dir, "assets")
                rel_path = os.path.relpath(filepath, assets_dir)
                if not rel_path.startswith(".."):
                    filepath = os.path.join("assets", rel_path)
        except Exception:
            pass

        # Every model is a Prop, with a Prop's defaults: not solid and not
        # carryable until the author turns either on.
        new_model = Prop.for_model(filepath, pos=[0, 0, 0],
                                   properties={'rotation': rotation})

        # Downloaded OBJs are commonly authored in real-world units and can be
        # only a few Fio units across. Fio's world is much larger (TILE_SIZE is
        # 50), so an otherwise valid imported mesh can become effectively
        # invisible in the editor at the default camera distance. When the Asset
        # Browser supplies the neutral [1,1,1] scale, give unusually small OBJs a
        # sensible initial scene scale. Existing authored maps and explicit
        # non-unit scales are left untouched.
        initial_scale = list(scale) if isinstance(scale, (list, tuple)) else scale
        if (
            str(filepath).lower().endswith('.obj')
            and isinstance(initial_scale, (list, tuple))
            and len(initial_scale) == 3
            and all(float(v) == 1.0 for v in initial_scale)
        ):
            try:
                from engine.obj_loader import OBJLoader
                loader = OBJLoader()
                if loader.load(filepath) and loader.vertices:
                    verts = np.asarray(loader.vertices, dtype=np.float32)
                    extent = float(np.max(verts.max(axis=0) - verts.min(axis=0)))
                    if 0.0 < extent < TILE_SIZE * 0.2:
                        fit_target = TILE_SIZE * 0.5
                        fit = min(fit_target / extent, 25.0)
                        initial_scale = [fit, fit, fit]
            except Exception:
                pass

        new_model.properties['scale'] = initial_scale
        
        # Set a default name based on filename
        model_name = os.path.splitext(os.path.basename(filepath))[0]
        new_model.properties['name'] = model_name
        
        self.state.things.append(new_model)
        self.set_selected_object(new_model)
        self.show_toast(f"Added {model_name}")

    def set_selected_object(self, obj):
        """Set a single selected object (backwards compatibility)."""
        # Component handles belong to a selection: drop them and mark the
        # overlay stale, since the brushes it was drawing handles for changed.
        self.components.clear()
        self.components.invalidate()
        if obj is None:
            self.state.selected_objects = []
            self.state.selected_object = None
        else:
            self.state.selected_objects = [obj]
            self.state.selected_object = obj
        
        if self.config.getboolean('Display', 'sync_selection', fallback=True):
            self.view_3d.selected_object = self.state.selected_object
        else:
            self.view_3d.selected_object = None
        self.update_all_ui()

    def set_selected_objects(self, objects):
        """Set multiple selected objects."""
        # Component handles belong to a selection: drop them and mark the
        # overlay stale, since the brushes it was drawing handles for changed.
        self.components.clear()
        self.components.invalidate()
        self.state.selected_objects = objects if objects else []
        # For backwards compatibility, selected_object is the first one (or None)
        self.state.selected_object = objects[0] if objects else None
        
        if self.config.getboolean('Display', 'sync_selection', fallback=True):
            self.view_3d.selected_object = self.state.selected_object
        else:
            self.view_3d.selected_object = None
        self.update_all_ui()

    def update_all_ui(self):
        self.property_editor.set_object(self.state.selected_object)
        self.scene_hierarchy.refresh_list()
        self.sync_surface_inspector()
        self.update_views()

    def update_views(self):
        self.sync_bigworld_terrain(allow_create=True)
        self.view_3d.update()
        self.view_top.reset_state()
        self.view_front.reset_state()
        self.view_side.reset_state()

    # ------------------------------------------------------------------
    # Big World: "fill world with terrain" — editor preview
    # ------------------------------------------------------------------
    @staticmethod
    def _bigworld_truthy(val, default=False):
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _find_bigworld_settings(self):
        """The map's BigWorldSettings entity, or None."""
        for thing in getattr(self.state, 'things', None) or []:
            props = getattr(thing, 'properties', None) or {}
            if getattr(thing, 'TYPE', None) == 'bigworldsettings' \
                    or props.get('type') == 'bigworldsettings':
                return thing
        return None

    def _bigworld_world_extent(self, pad):
        """World-space (min_x, min_z, max_x, max_z) AABB of all placed content.

        Mirrors the runtime session's notion of "the whole world" (the bounding
        box of everything the map contains), padded so terrain extends a little
        past the outermost object. Returns None when the map is empty.
        """
        min_x = min_z = float('inf')
        max_x = max_z = float('-inf')
        found = False
        for b in getattr(self.state, 'brushes', None) or []:
            pos = b.get('pos'); size = b.get('size') or [0, 0, 0]
            if not pos:
                continue
            hx = abs(size[0]) / 2.0; hz = abs(size[2]) / 2.0
            min_x = min(min_x, pos[0] - hx); max_x = max(max_x, pos[0] + hx)
            min_z = min(min_z, pos[2] - hz); max_z = max(max_z, pos[2] + hz)
            found = True
        for t in getattr(self.state, 'things', None) or []:
            pos = getattr(t, 'pos', None)
            if not pos:
                continue
            min_x = min(min_x, pos[0]); max_x = max(max_x, pos[0])
            min_z = min(min_z, pos[2]); max_z = max(max_z, pos[2])
            found = True
        if not found:
            return None
        return (min_x - pad, min_z - pad, max_x + pad, max_z + pad)

    def _ensure_terrain(self):
        """Create a procedural Terrain if the map has none, and return it.

        Mirrors the terrain-creation path in :meth:`open_terrain_editor` (minus
        the modal progress dialog) so "Fill world with terrain" can generate a
        terrain to fill even on a map that never opened the terrain editor.
        Must be called on the main thread (GL setup), never from a paint event.
        """
        if getattr(self, 'terrain', None) is not None:
            return self.terrain
        try:
            from engine.terrain import Terrain
            self.terrain = Terrain(seed=42)
            if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                self.terrain.from_dict(self.state.terrain_data)
            if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                self.view_3d.renderer.setup_terrain_shader(self.terrain)
            if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                self.view_3d.logic_thread.set_terrain(self.terrain)
            if hasattr(self.state, 'terrain_data'):
                self.state.terrain_data = self.terrain.to_dict()
            if hasattr(self, 'scene_hierarchy'):
                try:
                    self.scene_hierarchy.refresh_list()
                except Exception:
                    pass
        except Exception as exc:
            print(f"[bigworld] could not create terrain for fill: {exc}")
            return None
        return self.terrain

    def sync_bigworld_terrain(self, allow_create=False):
        """Reflect the BigWorldSettings ``terrain_fill`` option in the editor.

        When the map opts in, expand the procedural terrain to cover the whole
        world and switch it to streaming so the world is visible in the editor
        straight away while only the chunks around the editor camera are meshed
        (as the camera moves). Turning the option off — or removing the entity —
        restores the authored terrain. Cheap and idempotent; called on any edit
        and on every top-view repaint. Never persists the expansion (see
        ``Terrain.to_dict``).

        ``allow_create`` lets the fill *generate* a terrain when the map has
        none yet (the common case when the user has never opened the terrain
        editor). It does GL setup, so it is only passed from main-thread callers
        (edits / the property toggle), never from the 2D paint path.
        """
        settings = self._find_bigworld_settings()
        fill = bool(
            settings is not None
            and self._bigworld_truthy(settings.properties.get('enabled', True), True)
            and self._bigworld_truthy(settings.properties.get('terrain_fill', False))
        )
        terrain = getattr(self, 'terrain', None)
        if terrain is None and fill and allow_create:
            terrain = self._ensure_terrain()
        if terrain is None or not hasattr(terrain, 'editor_fill_world'):
            return
        if not fill:
            terrain.editor_unfill_world()
            self._refresh_terrain_editor_size_lock()
            return
        try:
            radius = float(settings.properties.get('terrain_stream_radius', 0.0) or 0.0)
        except (TypeError, ValueError):
            radius = 0.0
        if radius <= 0.0:
            try:
                radius = float(settings.properties.get('activation_radius', 2048.0) or 2048.0)
            except (TypeError, ValueError):
                radius = 2048.0
        if self._bigworld_truthy(settings.properties.get('terrain_infinite', False)):
            # Stream the terrain forever around the camera — no edge to walk off.
            # Only the ring of chunks near the camera is ever resident, so the
            # huge extent costs nothing. (Matches BigWorldSession.INFINITE_HALF_EXTENT.)
            h = 1.0e7
            extent = (-h, -h, h, h)
        else:
            extent = self._bigworld_world_extent(pad=max(512.0, radius))
        if extent is None:
            terrain.editor_unfill_world()
            return
        min_wx, min_wz, max_wx, max_wz = extent
        terrain.editor_fill_world(min_wx, min_wz, max_wx, max_wz, radius)
        self._refresh_terrain_editor_size_lock()

    def _refresh_terrain_editor_size_lock(self):
        """If the Terrain Editor is open, lock/unlock its Size tab to match
        whether Big World currently owns the world size."""
        panel = getattr(self, 'terrain_editor_window', None)
        terrain = getattr(self, 'terrain', None)
        if panel is not None and hasattr(panel, 'set_bigworld_managed') and terrain is not None:
            try:
                panel.set_bigworld_managed(
                    getattr(terrain, '_authored_bounds', None) is not None)
            except Exception:
                pass

    def select_object(self, obj):
        self.set_selected_object(obj)

    def highlight_in_hierarchy(self, obj):
        """Highlight an object in the scene hierarchy without selecting it.
        Used for locked objects when locked_not_selectable_2d is enabled."""
        if hasattr(self.scene_hierarchy, 'highlight_item'):
            self.scene_hierarchy.highlight_item(obj)
        elif hasattr(self.scene_hierarchy, 'scroll_to_item'):
            self.scene_hierarchy.scroll_to_item(obj)


    def update_play_button_color(self):
        """Update the Play button color based on current mode."""
        if hasattr(self, 'play_button'):
            if self.view_3d.play_mode:
                # Red for play mode
                self.play_button.setStyleSheet("""
                    QPushButton {
                        background-color: #C62828;
                        color: white;
                        border: 1px solid #B71C1C;
                        border-radius: 3px;
                        padding: 5px 15px;
                        font-weight: bold;
                        min-width: 250px;
                        max-width: 250px;
                    }
                    QPushButton:hover {
                        background-color: #D32F2F;
                    }
                    QPushButton:pressed {
                        background-color: #B71C1C;
                    }
                """)
                self.play_button.setText("Stop")
            else:
                # Green for editor mode
                self.play_button.setStyleSheet("""
                    QPushButton {
                        background-color: #2E7D32;
                        color: white;
                        border: 1px solid #1B5E20;
                        border-radius: 3px;
                        padding: 5px 15px;
                        font-weight: bold;
                        min-width: 250px;
                        max-width: 250px;
                    }
                    QPushButton:hover {
                        background-color: #388E3C;
                    }
                    QPushButton:pressed {
                        background-color: #1B5E20;
                    }
                """)
                self.play_button.setText("Play")

    @staticmethod
    def _snap_to_power_of_two(n):
        if n <= 0: return 1
        power = round(math.log2(n))
        return int(2**power)

    def start_mover_preview(self, brush):
        if not brush or not isinstance(brush, dict):
            return
        
        is_mover = brush.get('is_mover', False)
        is_door = brush.get('is_door', False)
        if not is_mover and not is_door:
            return

        # ── Rotate preview ───────────────────────────────────────────────
        if is_mover and brush.get('rotate', False):
            self.preview_data = {
                'obj': brush,
                'is_rotate': True,
                'speed': brush.get('speed', 45.0),
                'angle': brush.get('_rot_angle', 0.0),
            }
            self.preview_timer.start(16)
            return

        # Check for path-following preview
        path_target = brush.get('path_target', '')
        if path_target:
            # Build chain of PathNodes
            chain = []
            visited = set()
            current = path_target
            while current and current not in visited:
                node = self._find_path_node_by_name(current)
                if not node:
                    break
                visited.add(current)
                chain.append(node)
                current = node.properties.get('next_node', '')
            if not chain:
                # No valid chain – fall back to oscillation preview
                self._start_oscillation_preview(brush)
                return

            original_pos = list(brush['pos'])

            self.preview_data = {
                'obj': brush,
                'is_path': True,
                'chain': chain,
                'current_idx': 0,
                'lerp_t': 0.0,
                'speed': brush.get('speed', 64.0),
                'origin': np.array(chain[0].pos, dtype=float),
                'target': np.array(chain[0].pos, dtype=float),
                'waiting': False,
                'wait_remaining': 0.0,
                'time': 0.0,
                'original_pos': original_pos,
            }
            # Position the brush at the first node to start
            brush['pos'] = list(chain[0].pos)

        # No path – use oscillation preview (original behaviour)
        self._start_oscillation_preview(brush)

    # FIX: Map door_direction strings to vectors for preview
    _DOOR_DIR_MAP = {
        'up': [0, 1, 0], 'down': [0, -1, 0],
        'north': [0, 0, 1], 'south': [0, 0, -1],
        'east': [1, 0, 0], 'west': [-1, 0, 0],
    }

    def _start_oscillation_preview(self, brush):
        """Sine-wave oscillation preview.  Reads door_* properties and
        translates them so the preview matches what _update_doors uses."""
        # For doors, the editor stores door_speed/door_distance/door_direction.
        # Translate to the engine-expected keys for the preview.
        if brush.get('is_door'):
            speed = brush.get('door_speed', brush.get('speed', 64.0))
            distance = brush.get('door_distance', brush.get('distance', 128.0))
            lip = float(brush.get('door_lip', 0.0))
            distance = max(1.0, distance - lip)
            dir_val = brush.get('door_direction', brush.get('direction', [0, 1, 0]))
            if isinstance(dir_val, str):
                direction = self._DOOR_DIR_MAP.get(dir_val, [0, 1, 0])
            else:
                direction = dir_val
        else:
            speed = brush.get('speed', 64.0)
            distance = brush.get('distance', 128.0)
            direction = brush.get('direction', [0, 1, 0])

        self.preview_data = {
            'obj': brush,
            'is_path': False,
            'original_pos': list(brush['pos']),
            'direction': np.array(direction, dtype=float),
            'distance': distance,
            'speed': speed,
            'time': 0.0,
            'is_door': brush.get('is_door', False)
        }
        norm = np.linalg.norm(self.preview_data['direction'])
        if norm > 0:
            self.preview_data['direction'] /= norm
        self.preview_timer.start(16)

    def _find_path_node_by_name(self, name):
        """Helper to locate a PathNode by name."""
        for t in self.state.things:
            from editor.things import PathNode
            if isinstance(t, PathNode) and t.properties.get('name') == name:
                return t
        return None

    def stop_mover_preview(self):
        if self.preview_timer.isActive():
            self.preview_timer.stop()
            if self.preview_data and self.preview_data.get('obj'):
                if self.preview_data.get('is_rotate'):
                    self.preview_data['obj'].pop('_rot_angle', None)
                elif self.preview_data.get('is_path'):
                    # Restore the original position that was saved before preview started
                    original_pos = self.preview_data.get('original_pos')
                    if original_pos is not None:
                        self.preview_data['obj']['pos'] = original_pos
                    else:
                        # Fallback (should not happen) – use first node or origin
                        chain = self.preview_data.get('chain', [])
                        if chain:
                            self.preview_data['obj']['pos'] = list(chain[0].pos)
                        else:
                            self.preview_data['obj']['pos'] = [0, 0, 0]
                else:
                    self.preview_data['obj']['pos'] = self.preview_data['original_pos']
                moved(self.preview_data['obj'])
                self.preview_data = {}
                self.update_views()

                # Reset buttons
                m_btn = self.property_editor._widgets.get('mover_preview_btn')
                if m_btn:
                    m_btn.blockSignals(True)
                    m_btn.setChecked(False)
                    m_btn.setText("▶ Preview Movement")
                    m_btn.blockSignals(False)
                d_btn = self.property_editor._widgets.get('door_preview_btn')
                if d_btn:
                    d_btn.blockSignals(True)
                    d_btn.setChecked(False)
                    d_btn.setText("▶ Preview Door")
                    d_btn.blockSignals(False)

    def update_mover_preview(self):
        """One preview step. The brush is written in place, so it is journalled
        for the render tables, which no longer poll movers every frame."""
        brush = self.preview_data.get('obj') if self.preview_data else None
        try:
            self._advance_mover_preview()
        finally:
            if brush is not None:
                moved(brush)

    def _advance_mover_preview(self):
        if not self.preview_data:
            return

        dt = 0.016  # ~60 FPS
        data = self.preview_data
        brush = data['obj']

        if data.get('is_rotate'):
            data['angle'] = (data['angle'] + data['speed'] * dt) % 360.0
            brush['_rot_angle'] = data['angle']
            self.update_views()
            return

        # ------------------------------------------------------------------
        #  Path‑following preview (when is_path is True)
        # ------------------------------------------------------------------
        if data.get('is_path'):
            chain = data['chain']
            idx = data['current_idx']
            if idx >= len(chain):
                self.stop_mover_preview()
                return

            current_node = chain[idx]
            target_pos = np.array(current_node.pos, dtype=float)

            # If waiting at a node, count down and then advance
            if data['waiting']:
                data['wait_remaining'] -= dt
                if data['wait_remaining'] <= 0.0:
                    data['waiting'] = False
                    idx += 1
                    data['current_idx'] = idx
                    if idx < len(chain):
                        data['origin'] = target_pos.copy()
                        data['target'] = np.array(chain[idx].pos, dtype=float)
                        data['lerp_t'] = 0.0
                    else:
                        # End of chain reached
                        brush['pos'] = target_pos.tolist()
                        self.update_views()
                        self.stop_mover_preview()
                        return
                else:
                    # Still waiting, no movement
                    return

            # Move toward the current target node
            origin = data['origin']
            target = data['target']
            segment_vec = target - origin
            segment_len = np.linalg.norm(segment_vec)

            if segment_len < 1.0:
                # Already at the node – snap and start waiting (or advance immediately)
                data['lerp_t'] = 1.0
                brush['pos'] = target.tolist()
                wait_time = current_node.properties.get('wait_time', 0.0)
                if wait_time > 0.0:
                    data['waiting'] = True
                    data['wait_remaining'] = wait_time
                else:
                    idx += 1
                    data['current_idx'] = idx
                    if idx < len(chain):
                        data['origin'] = target.copy()
                        data['target'] = np.array(chain[idx].pos, dtype=float)
                        data['lerp_t'] = 0.0
                    else:
                        brush['pos'] = target.tolist()
                        self.update_views()
                        self.stop_mover_preview()
                        return
            else:
                # Linear interpolation with speed multiplier
                speed = data['speed'] * current_node.properties.get('speed', 1.0)
                data['lerp_t'] += (speed * dt) / segment_len
                t = min(data['lerp_t'], 1.0)
                new_pos = origin + segment_vec * t
                brush['pos'] = new_pos.tolist()

                if t >= 1.0:
                    # Arrived at the node
                    wait_time = current_node.properties.get('wait_time', 0.0)
                    if wait_time > 0.0:
                        data['waiting'] = True
                        data['wait_remaining'] = wait_time
                    else:
                        idx += 1
                        data['current_idx'] = idx
                        if idx < len(chain):
                            data['origin'] = target.copy()
                            data['target'] = np.array(chain[idx].pos, dtype=float)
                            data['lerp_t'] = 0.0
                        else:
                            brush['pos'] = target.tolist()
                            self.update_views()
                            self.stop_mover_preview()
                            return

            self.update_views()

        # ------------------------------------------------------------------
        #  Original oscillation preview (direction‑based)
        # ------------------------------------------------------------------
        else:
            data['time'] += dt
            speed = data['speed']
            distance = data['distance']
            if distance == 0:
                return

            # Sine wave between 0 and distance
            progress = (math.sin(data['time'] * (speed / distance) * math.pi - (math.pi / 2)) + 1) / 2
            current_offset = progress * distance
            movement_vector = data['direction'] * current_offset
            original_pos = np.array(data['original_pos'])
            new_pos = original_pos + movement_vector
            brush['pos'] = new_pos.tolist()
            self.update_views()

    def load_config(self):
        self.config.read(self.config_path)

    def save_config(self):
        with open(self.config_path, 'w') as configfile:
            self.config.write(configfile)

    def update_global_font(self):
        font_size = self.config.getint('Display', 'font_size', fallback=11)
        font = QApplication.font()
        font.setPointSize(font_size)
        QApplication.setFont(font)

    def show_settings_dialog(self):
        # Store old values to check for changes
        old_dpi_setting = self.config.getboolean('Display', 'high_dpi_scaling', fallback=False)
        old_font_size = self.config.getint('Display', 'font_size', fallback=10)
        old_show_caulk = self.config.getboolean('Display', 'show_caulk', fallback=True)
        old_big_toolbar_buttons = self.config.getboolean('Display', 'big_toolbar_buttons', fallback=False)
        
        # New: Autosave setting check
        old_autosave = self.config.getboolean('Editor', 'autosave_enabled', fallback=True)
        old_autosave_interval = self.config.getint('Editor', 'autosave_interval', fallback=10)

        dialog = SettingsWindow(self.config, self)
        if dialog.exec_():
            self.save_config()
            self.update_shortcuts()
            self.apply_tooltip_settings()
            
            # Update Autosave if changed
            new_autosave = self.config.getboolean('Editor', 'autosave_enabled', fallback=True)
            new_autosave_interval = self.config.getint('Editor', 'autosave_interval', fallback=10)
            
            if new_autosave != old_autosave or new_autosave_interval != old_autosave_interval:
                self.setup_autosave()
            
            # Track which settings require restart
            restart_required = []
            
            new_font_size = self.config.getint('Display', 'font_size', fallback=10)
            if old_font_size != new_font_size:
                self.update_global_font()
                
            new_show_caulk = self.config.getboolean('Display', 'show_caulk', fallback=True)
            if old_show_caulk != new_show_caulk:
                self.update_views()

            # Player glasses visibility is live; no restart is required.
            self.view_3d.show_glasses = self.config.getboolean(
                'Display', 'show_glasses', fallback=True
            )
            # So is player 1's choice of glasses (Settings > Appearance).
            self.view_3d.player1_glasses = normalize_glasses(self.config.get(
                'Appearance', 'glasses', fallback=DEFAULT_GLASSES
            ))
            self.view_3d.update()
                
            new_dpi_setting = self.config.getboolean('Display', 'high_dpi_scaling', fallback=False)
            if old_dpi_setting != new_dpi_setting:
                restart_required.append("High DPI scaling")
                
            new_big_toolbar_buttons = self.config.getboolean('Display', 'big_toolbar_buttons', fallback=False)
            if old_big_toolbar_buttons != new_big_toolbar_buttons:
                restart_required.append("Toolbar button size")
            
            # Show restart message if any settings require it
            if restart_required:
                QMessageBox.information(self, "Restart Required",
                    f"The following settings have been changed:\n\n" +
                    "\n".join(f"• {setting}" for setting in restart_required) +
                    "\n\nPlease restart the application for the changes to take effect.")

    def apply_tooltip_settings(self):
        """Settings > Editor > Tooltips: show or hide each area's tooltips.

        Split by area because they are read differently -- toolbar tooltips
        are how the icons are learned and stop being wanted long before the
        Property Editor's do.
        """
        panel = getattr(self, 'property_editor', None)
        if panel is not None and hasattr(panel, 'set_tooltips_enabled'):
            panel.set_tooltips_enabled(
                self.config.getboolean('Editor', 'property_editor_tooltips',
                                       fallback=True))

        toolbar = getattr(self, 'tool_toolbar', None)
        if toolbar is not None:
            set_tooltips_enabled(
                toolbar,
                self.config.getboolean('Editor', 'toolbar_tooltips',
                                       fallback=True))

    def copy_selection(self):
        """Copy the current object/multi-selection into the editor clipboard."""
        sources = list(getattr(self.state, 'selected_objects', []) or [])
        if self.state.selected_object is not None and self.state.selected_object not in sources:
            sources.append(self.state.selected_object)

        if sources:
            clipboard = []
            for source in sources:
                if isinstance(source, dict):
                    source = {
                        k: v for k, v in source.items()
                        if k not in brush_geometry.GEO_RUNTIME_KEYS
                    }
                clipboard.append(copy.deepcopy(source))
            self._brush_clipboard = clipboard

            names = []
            for source in clipboard:
                if isinstance(source, dict):
                    names.append(source.get('name', 'Brush'))
                else:
                    names.append(source.properties.get('name', 'Entity'))
            if len(names) == 1:
                self.show_toast(f"Copied: {names[0]}")
            else:
                self.show_toast(f"Copied {len(names)} objects")
        else:
            self._brush_clipboard = None
            self.show_toast("Nothing to copy", is_error=True)

    def paste_selection(self):
        """Paste the editor clipboard with fresh UUIDs and a grid offset."""
        if not self._brush_clipboard:
            self.show_toast("Nothing to paste", is_error=True)
            return
        sources, _skipped = self._drop_singleton_copies(self._brush_clipboard)
        if not sources:
            return

        self.save_state()
        offset = self.grid_size_spinbox.value()
        delta = [offset, 0.0, offset]
        pasted_objects = []
        taken_names = set(self.state.get_all_entity_names())

        for source in sources:
            pasted = copy.deepcopy(source)

            if isinstance(pasted, dict):
                pasted['id'] = str(uuid.uuid4())
                base_name = pasted.get('name', 'Brush')
                if base_name:
                    pasted['name'] = self._copy_name(base_name, taken_names)

                if brush_geometry.brush_has_geometry(pasted):
                    brush_geometry.translate_brush(pasted, delta)
                else:
                    pasted['pos'] = [
                        pasted['pos'][0] + delta[0],
                        pasted['pos'][1] + delta[1],
                        pasted['pos'][2] + delta[2],
                    ]

                pasted.pop('_io_connections', None)
                pasted.pop('io_connections', None)
                self.state.brushes.append(pasted)
            else:
                pasted.properties['id'] = str(uuid.uuid4())
                base_name = pasted.properties.get('name', 'Entity')
                pasted.properties['name'] = self._copy_name(base_name, taken_names)
                pasted.pos = [
                    pasted.pos[0] + delta[0],
                    pasted.pos[1] + delta[1],
                    pasted.pos[2] + delta[2],
                ]
                pasted.properties.pop('_io_connections', None)
                pasted.properties.pop('io_connections', None)
                self.state.things.append(pasted)

            pasted_objects.append(pasted)

        self.set_selected_objects(pasted_objects)
        self.show_toast(
            f"Pasted {len(pasted_objects)} object(s)"
            if len(pasted_objects) != 1
            else f"Pasted: {pasted_objects[0].get('name', 'Brush') if isinstance(pasted_objects[0], dict) else pasted_objects[0].properties.get('name', 'Entity')}"
        )

        for pasted in pasted_objects:
            if isinstance(pasted, dict):
                pasted['_flash_until'] = time.time() + 0.5
                QTimer.singleShot(500, lambda o=pasted: self._clear_flash(o))

    def handle_escape(self):
        """Back out of whatever is in progress, innermost first.

        Shared so that panels which would otherwise swallow Escape behave the
        same as the viewports.  A QDialog closes itself on Escape, which meant
        pressing it to leave Face Mode shut the Surface Inspector instead of
        leaving the mode the panel had put the user in.

        Returns True when something was backed out of, so a caller can tell an
        Escape that did something from one that had nothing to do.
        """
        if self.cancel_clone_placement():
            return True
        if (hasattr(self, 'view_3d') and
                getattr(self.view_3d, 'terrain_sculpt_active', False)):
            self.view_3d.set_terrain_sculpt_active(False)
            return True
        if self.components.cancel_drag():
            self.refresh_views()
            return True
        if self.components.is_component_mode():
            self.set_component_mode(MODE_OBJECT)
            return True
        if getattr(self.view_3d, 'face_mode_active', False):
            self.toggle_face_mode(False)
            return True
        if self.state.selected_object:
            self.set_selected_object(None)
            return True
        return False

    def toggle_face_mode(self, active):
        """Toggles the Face Mode in the 3D view."""
        if not hasattr(self, 'view_3d'): return

        self.view_3d.face_mode_active = active

        # Sync the FACE button if Face Mode was toggled some other way (Esc,
        # a shortcut).  It lives on the Surface Inspector, which is created
        # lazily — nothing to sync until the panel has been opened once.
        if self.surface_inspector is not None:
            self.surface_inspector.sync_face_button(active)
        
        if active:
            self.show_toast("FACE MODE: Select a face to texture (Purple) — Page Up/Down rotates it", duration=3000)
            self.set_selected_object(None) # Deselect current object to clear gizmos and allow clean hover
            
            # Change cursor to indicate mode
            self.view_3d.setCursor(Qt.CrossCursor)
        else:
            self.show_toast("FACE MODE: OFF")
            self.view_3d.hovered_face_info = None # Clear highlight
            self.view_3d.setCursor(Qt.ArrowCursor)
            # The panel stays open: it owns the FACE toggle now, and hiding it
            # here would take the button away the moment it was switched off.

        self.view_3d.update()

    def apply_texture_to_specific_face(self, brush, face_name):
        """Applies currently selected asset texture to the specific face of a brush."""
        texture_path = self.asset_browser.get_selected_filepath()
        if not texture_path:
            self.show_toast("Select a texture first", is_error=True)
            return

        texture_name = os.path.basename(texture_path)
        self.save_state()

        if 'textures' not in brush:
            brush['textures'] = {}

        from engine import brush_geometry
        if brush_geometry.brush_has_geometry(brush):
            # Angled brush: write straight to the plane that backs this face so
            # the sloped cut face (which has no box tag) gets textured. Faces
            # that kept a box tag also update brush['textures'] so the box-face
            # render path stays in sync.
            pidx = brush_geometry.face_plane_index(brush, face_name)
            if pidx is not None:
                planes = brush['geometry']['planes']
                planes[pidx]['texture'] = texture_name
                tag = planes[pidx].get('face')
                if tag:
                    brush['textures'][tag] = texture_name
            else:
                # Couldn't resolve (stale hover) — fall back to the tag path.
                brush['textures'][face_name] = texture_name
        else:
            brush['textures'][face_name] = texture_name
        if brush_geometry.brush_has_geometry(brush):
            # The derived faces copied the old texture, and the GPU mesh is
            # keyed by the geometry signature: both must move on.
            brush_geometry.invalidate_geometry_cache(brush)
        # The face may only be hovered, not selected, so the checkpoint above
        # did not journal it for the render projection.
        self.state.mark_lighting_dirty([brush])

        # Remember the last-textured face so the rotate-texture button / Page
        # Up-Down keys know which face to act on when nothing is hovered.
        self.face_texture_target = (brush, face_name)
        self.update_views()
        self.show_toast(f"Applied to {face_name}")

    ALL_FACE_KEYS = ('north', 'south', 'east', 'west', 'top', 'down')

    def _bump_face_angle(self, brush, face_name, delta_deg):
        """Advance one face's texture rotation by ``delta_deg`` degrees."""
        angles = brush.setdefault('uv_angle', {})
        angles[face_name] = (angles.get(face_name, 0.0) + delta_deg) % 360.0
        return angles[face_name]

    def rotate_textures(self, steps=1):
        """Rotate brush-face texture(s) by ``steps`` * 90 degrees (Page Up/Down).

        In face mode the highlighted face (falling back to the last-textured
        face) is rotated on its own. Otherwise, if a brush is selected, every
        face on that brush is rotated together.
        """
        delta = 90.0 if steps >= 0 else -90.0

        # --- Face mode: rotate only the highlighted / last-textured face ---
        if getattr(self.view_3d, 'face_mode_active', False):
            target = getattr(self.view_3d, 'hovered_face_info', None) \
                or getattr(self, 'face_texture_target', None)
            if not target:
                self.show_toast("Hover a face to rotate its texture", is_error=True)
                return
            brush, face_name = target
            self.save_state()
            angle = self._bump_face_angle(brush, face_name, delta)
            self.face_texture_target = (brush, face_name)
            self.update_views()
            if getattr(self, 'surface_inspector', None):
                self.surface_inspector.refresh_from_face()
            self.show_toast(f"{face_name}: texture {int(angle)}°")
            return

        # --- Otherwise: rotate every face of the selected brush together ---
        selected = self.state.selected_object
        if isinstance(selected, dict):
            self.save_state()
            for face_name in self.ALL_FACE_KEYS:
                self._bump_face_angle(selected, face_name, delta)
            self.update_views()
            self.show_toast(f"Brush textures rotated {int(delta):+d}°")

    def show_surface_inspector(self, brush=None, face_name=None,
                               raise_window=True):
        """Open (or re-target) the Surface Inspector.

        With no face it opens empty, its controls greyed out until there is
        something to edit -- the panel is a tool, and a tool should open when
        it is asked for.
        """
        if self.surface_inspector is None:
            from editor.surface_inspector import SurfaceInspector
            self.surface_inspector = SurfaceInspector(self, self)
        self.surface_inspector.set_target(brush, face_name,
                                          raise_window=raise_window)

    def show_entity_inspector(self, entity):
        """Open (or raise) the Entity Inspector for *entity*.

        The inspector is a live, read-only view whose contents plugins supply
        through ``EditorAPI.register_entity_inspector`` (API 1.5.0); an entity
        no plugin describes shows its public properties. One panel per entity:
        asking again for an entity already being inspected raises its panel.
        Plugins open it from their own commands, e.g. a console command's
        ``callback(args, main_window, logic, play_mode)``.
        """
        if entity is None:
            return None
        inspectors = self._entity_inspectors
        panel = inspectors.get(id(entity))
        if panel is not None and panel.entity is entity:
            panel.show()
            panel.raise_()
            panel.activateWindow()
            return panel
        from editor.entity_inspector import EntityInspector

        def _logic():
            # The logic thread lives for the whole editor session; providers
            # are promised it only while Play Mode is running.
            logic = getattr(getattr(self, 'view_3d', None), 'logic_thread', None)
            return logic if getattr(logic, 'play_mode', False) else None

        def _alive(e=entity):
            return any(t is e for t in getattr(self.state, 'things', ()))

        panel = EntityInspector(entity, logic=_logic, alive=_alive, parent=self)
        key = id(entity)
        inspectors[key] = panel
        panel.destroyed.connect(lambda *_a, k=key: inspectors.pop(k, None))
        panel.show()
        return panel

    def begin_actor_pick(self, on_pick=None):
        """Arm the Play Mode click-to-pick of an actor (see
        ``QtGameView.begin_actor_pick``): the world pauses and the next click
        on an actor calls ``on_pick(entity)``, by default opening the Entity
        Inspector on it. Returns False when there is no play session to pick
        in. Plugins arm it from their own commands.
        """
        view = getattr(self, 'view_3d', None)
        begin = getattr(view, 'begin_actor_pick', None)
        return bool(begin(on_pick)) if begin is not None else False

    def sync_surface_inspector(self):
        """Point an open Surface Inspector at something worth editing.

        It can be opened with nothing selected, so it binds as soon as there
        is a brush to bind to.  A panel already pointing into the selection
        is left alone -- re-binding on every click would undo a face picked
        from its dropdown -- and so is one whose brush has been deselected,
        since dropping the target would blank the panel mid-edit.
        """
        inspector = getattr(self, 'surface_inspector', None)
        if inspector is None or not inspector.isVisible():
            return

        brushes = self._selected_brushes()
        if not brushes:
            return
        target = inspector.target
        if target is not None and target[0] in brushes:
            return

        keys = face_texture.face_keys(brushes[0])
        if keys:
            self.show_surface_inspector(brushes[0], keys[0], raise_window=False)

    def toggle_surface_inspector(self):
        """Open (or close) the Surface Inspector on the current texture target.

        T/Shift+S are plain editor shortcuts.  Never let a modified keystroke
        such as Ctrl+Z reach this toggle, even if Qt delivers the QAction while
        another shortcut is being processed.
        """
        if getattr(self.view_3d, 'play_mode', False):
            return

        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return

        inspector = self.surface_inspector
        if inspector is not None and inspector.isVisible():
            inspector.hide()
            return

        target = getattr(self.view_3d, 'hovered_face_info', None) \
            or getattr(self, 'face_texture_target', None)
        if target is None:
            brushes = self._selected_brushes()
            keys = face_texture.face_keys(brushes[0]) if brushes else []
            # Nothing to bind to is not a reason to refuse: the panel opens
            # empty and binds itself as soon as a brush is selected.
            target = (brushes[0], keys[0]) if keys else (None, None)
        self.show_surface_inspector(*target)

    def enter_play_mode(self):
        """Toggle play mode on/off. Called by the Play/Stop button."""
        # If already in play mode, exit instead
        if getattr(self.view_3d, 'play_mode', False):
            self._exit_play_mode()
            return

        self._store_and_switch_to_debug_console()

        player_start = None
        for thing in self.state.things:
            if isinstance(thing, PlayerStart):
                player_start = thing
                break
        
        if not player_start:
            QMessageBox.warning(self, "No Player Start", "Add a Player Start object to the scene before entering play mode.")
            return

        if hasattr(self, 'mode_label'):
            self.mode_label.setText("PLAY MODE")
            self.mode_label.setStyleSheet("""
                QLabel {
                    background-color: #2E7D32;
                    color: white;
                    padding: 5px 10px;
                    border-radius: 4px;
                    font-weight: bold;
                    font-size: 14px;
                    border: 1px solid #1B5E20;
                }
            """)

        self._capture_pre_play_world()
        physics_enabled = self.config.getboolean('Settings', 'physics', fallback=True)
        self.view_3d.toggle_play_mode(player_start.pos, player_start.get_angle(), physics_enabled)
        self.view_3d.setFocus()
        
        # Update play button color
        self.update_play_button_color()
        
        #self.ui.notification_label.setText("ESC = EXIT PLAY MODE  |  F12 = FULLSCREEN")


    def _capture_pre_play_world(self):
        """Remember the world as Play starts, if Stop is to put it back.

        Optional (Settings -> Play Modes -> "Restore the world when leaving
        Play"). By default the editor keeps showing what happened in play --
        dead monsters, killed or hidden objects -- as it always has.
        """
        self._pre_play_world = None
        if not self.config.getboolean('Settings', 'restore_world_on_stop',
                                      fallback=False):
            return
        self._pre_play_world = (
            self.state.snapshot(),
            list(self.state.undo_stack),
            list(self.state.redo_stack),
            self.unsaved_changes,
        )

    def _restore_pre_play_world(self):
        """Put back the world captured by :meth:`_capture_pre_play_world`.

        Runs once the session has fully stopped. The same object replacement
        undo uses, so everything holding a reference is re-pointed the same
        way; the history and the unsaved flag go back too, so a restored
        session leaves no trace.
        """
        captured = getattr(self, '_pre_play_world', None)
        self._pre_play_world = None
        if captured is None:
            return
        world, undo, redo, unsaved = captured
        self.state.restore_state(world)
        self.state.undo_stack.clear()
        self.state.undo_stack.extend(undo)
        self.state.redo_stack = redo
        self._resync_components_after_history()
        self.unsaved_changes = unsaved
        self.update_title()
        self.update_all_ui()

    def _exit_play_mode(self):
        """Exit play mode and return to editor."""
        if hasattr(self.view_3d, 'play_mode') and self.view_3d.play_mode:
            self.view_3d.toggle_play_mode(None, None)
            self.view_3d.play_mode = False  # Force state change before UI update
            self._restore_pre_play_world()

        self.ui.notification_label.setText("")
        self._restore_properties_tab()

        if hasattr(self, 'mode_label'):
            self.mode_label.setText("EDITOR MODE")
            self.mode_label.setStyleSheet("""
                QLabel {
                    background-color: #333333;
                    color: #888888;
                    padding: 5px 10px;
                    border-radius: 4px;
                    font-weight: bold;
                    font-size: 14px;
                    border: 1px solid #444;
                }
            """)

        self.setFocus()
        self.update_play_button_color()

    def _store_and_switch_to_debug_console(self):
        """Store current tab index and switch to Debug Console tab."""
        # Only do this if we are actually entering play mode
        if self.view_3d.play_mode:
            return
        self._prev_properties_tab_index = self.properties_tab_widget.currentIndex()
        debug_console_idx = self.properties_tab_widget.indexOf(self.debug_console)
        if debug_console_idx >= 0:
            self.properties_tab_widget.setCurrentIndex(debug_console_idx)

    def _restore_properties_tab(self):
        """Restore previously active tab after play mode ends."""
        if hasattr(self, '_prev_properties_tab_index') and self._prev_properties_tab_index is not None:
            self.properties_tab_widget.setCurrentIndex(self._prev_properties_tab_index)
            self._prev_properties_tab_index = None


    def update_shortcuts(self):
        save_layout_shortcut = self.config.get('Controls', 'save_layout', fallback='Ctrl+Shift+S')
        if hasattr(self, 'save_layout_action'):
            self.save_layout_action.setShortcut(QKeySequence(save_layout_shortcut))
        restore_layout_shortcut = self.config.get('Controls', 'restore_layout', fallback='Ctrl+Shift+L')
        if hasattr(self, 'restore_layout_action'):
            self.restore_layout_action.setShortcut(QKeySequence(restore_layout_shortcut))
        reset_layout_shortcut = self.config.get('Controls', 'reset_layout', fallback='Ctrl+Shift+R')
        if hasattr(self, 'reset_layout_action'):
            self.reset_layout_action.setShortcut(QKeySequence(reset_layout_shortcut))

    def toggle_system_monitor(self):
        """Toggles the debug system monitor overlay in the 3D view."""
        self.view_3d.sysmon.toggle()
        
        # If in play mode, we need to handle cursor visibility when toggling the menu
        if self.view_3d.play_mode:
            if self.view_3d.sysmon.is_active():
                # Show cursor for menu interaction
                QApplication.restoreOverrideCursor()
                self.view_3d.setCursor(Qt.ArrowCursor)
            else:
                # Hide cursor to resume play
                center_pos = self.view_3d.mapToGlobal(self.view_3d.rect().center())
                QCursor.setPos(center_pos)
                self.view_3d.last_mouse_pos = self.view_3d.mapFromGlobal(center_pos)
                QApplication.setOverrideCursor(Qt.BlankCursor)
        
        self.view_3d.update()

        action = getattr(self, 'system_monitor_action', None)
        if action is not None and action.isChecked() != self.view_3d.sysmon.is_active():
            action.blockSignals(True)
            action.setChecked(self.view_3d.sysmon.is_active())
            action.blockSignals(False)

    def set_grid_size(self, size):
        snapped_size = self._snap_to_power_of_two(size)
        self.grid_size_spinbox.blockSignals(True)       # sync the spinbox
        self.grid_size_spinbox.setValue(snapped_size)
        self.grid_size_spinbox.blockSignals(False)
        for view in [self.view_top, self.view_side, self.view_front, self.view_3d]:
            view.grid_size = snapped_size
        self.view_3d.update_grid()
        self.update_views()

    def set_world_size(self, size):
        snapped_size = self._snap_to_power_of_two(size)
        if snapped_size != size:
            self.world_size_spinbox.blockSignals(True)
            self.world_size_spinbox.setValue(snapped_size)
            self.world_size_spinbox.blockSignals(False)
        for view in [self.view_top, self.view_side, self.view_front, self.view_3d]:
            view.world_size = snapped_size
        self.view_3d.update_grid()
        self.update_views()

    def set_brush_display_mode(self, text):
        self.view_3d.brush_display_mode = text
        self.view_3d.update()

    def set_camera_mode(self, text):
        """Switch the play-mode camera between First Person and Overhead."""
        if hasattr(self.view_3d, "set_camera_mode"):
            self.view_3d.set_camera_mode(text)
        else:
            self.view_3d.camera_mode = text
            self.view_3d.update()

    def set_cull_distance(self, distance):
        """Set Cull Dist and mirror the actual clamped value in the spinner."""
        self.view_3d.set_cull_distance(distance)
        spin = getattr(self, "cull_dist_spinbox", None)
        if spin is not None:
            # ViewDistance is authoritative because it clamps the request.
            # Block the signal so external changes do not recurse through the
            # spinner's valueChanged handler.
            spin.blockSignals(True)
            try:
                spin.setValue(int(round(self.view_3d.view_distance.distance)))
            finally:
                spin.blockSignals(False)

    def save_state(self):
        self.state.save_state()
        # Every scene mutation funnels through here, so this is the cheap,
        # once-per-operation place to tell the component overlay its cached
        # handle positions may be stale.  It is a single integer bump; the
        # overlay itself is only rebuilt the next time something draws it.
        self.components.invalidate()
        self.mark_as_modified() # Mark as dirty when state is saved for undo

    def undo(self):
        if self.state.undo():
            self._resync_components_after_history()
            self.mark_as_modified() # Undo changes state
            self.update_all_ui()

    def redo(self):
        if self.state.redo():
            self._resync_components_after_history()
            self.mark_as_modified() # Redo changes state
            self.update_all_ui()

    def _resync_components_after_history(self):
        """Re-point everything holding an object reference after an undo/redo.

        Undo rebuilds the brush dicts and Things from JSON, so *every* reference
        held from before now points at an object that is no longer in the scene.
        ``EditorState`` has already re-pointed the selection itself by stable
        id; the rest of the editor's references have to follow:

        * component handles, dropped or re-resolved against the new geometry;
        * the Surface Inspector and the "face last worked on", both of which
          hold a brush directly and would otherwise edit a detached dict;
        * the property editor's cached pages and the I/O reverse index, which
          are keyed on objects that no longer exist.
        """
        self.components.cancel_drag()
        self.components.prune(self.state.brushes)
        self.components.invalidate()
        self._rebind_face_targets()
        self.invalidate_entity_caches()

    def _rebind_face_targets(self):
        """Re-point the face-texturing targets at the live scene.

        Both the Surface Inspector's bound face and ``face_texture_target``
        hold ``(brush, face key)``.  After a history step that brush is a
        detached copy, so the panel would go on editing something nothing draws.
        Each is moved to the brush with the same stable id, or dropped.
        """
        live = {}
        for brush in self.state.brushes:
            brush_id = brush.get('id')
            if brush_id:
                live[brush_id] = brush

        def _rebind(target):
            if not target or target[0] is None:
                return target
            brush, key = target
            if any(brush is b for b in self.state.brushes):
                return target
            replacement = live.get(brush.get('id')) if isinstance(brush, dict) else None
            return (replacement, key) if replacement is not None else None

        current = getattr(self, 'face_texture_target', None)
        if current is not None:
            self.face_texture_target = _rebind(current)

        inspector = getattr(self, 'surface_inspector', None)
        if inspector is not None and inspector.target is not None:
            rebound = _rebind(inspector.target)
            # Re-pointed, never re-opened: undo is not a window action.
            if rebound is None:
                inspector.set_target(None, None, raise_window=False,
                                     reveal=False)
            elif rebound is not inspector.target:
                inspector.set_target(rebound[0], rebound[1],
                                     raise_window=False, reveal=False)
            else:
                inspector.refresh_from_face()

    def invalidate_entity_caches(self):
        """Drop caches keyed on the scene's objects.

        For the wholesale swaps — an undo, a map load — where the objects
        themselves are replaced rather than edited, so nothing watching for
        changes *within* an object can notice.
        """
        editor_panel = getattr(self, 'property_editor', None)
        if editor_panel is not None:
            editor_panel.invalidate_cache()
        try:
            from editor import io_system
            io_system.bump_io_revision()
        except ImportError:
            pass

    def set_render_mode(self, mode):
        self.view_3d.render_mode = mode
        self.update_views()

    def show_shortcuts_window(self):
        """Help > Keys: list every shortcut, including the user's own.

        Kept on the window so reopening raises the one already there rather
        than stacking copies; it re-reads its list each time it is shown.
        """
        from editor.shortcuts_window import ShortcutsWindow
        if getattr(self, 'shortcuts_window', None) is None:
            self.shortcuts_window = ShortcutsWindow(self, self)
        self.shortcuts_window.show()
        self.shortcuts_window.raise_()
        self.shortcuts_window.activateWindow()

    def show_about(self):
        try:
            with open('editor/version.txt', 'r') as f:
                version = f.read().strip()
        except FileNotFoundError:
            version = "Version not found"

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("About Fio")
        
        container_widget = QWidget()
        layout = QVBoxLayout(container_widget)

        splash_label = QLabel()
        pixmap = QPixmap('assets/splash.png')
        splash_label.setPixmap(pixmap.scaled(512, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(splash_label)

        subtitle_label = QLabel("Real-time world machine")
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setStyleSheet("""
            QLabel {
                color: #cccccc;
                font-weight: bold;
                padding: 4px 0px;
            }
        """)
        layout.addWidget(subtitle_label)

        version_label = QLabel(
            f"{version}<br>"
            f"<a href='https://github.com/ViciousSquid/Fio' style='color: #F08000; text-decoration: none;'>"
            f"https://github.com/ViciousSquid/Fio"
            f"</a><br>"
            f"<a href='https://github.com/ViciousSquid/Fio/wiki' style='color: #A7B454; text-decoration: none;'>"
            f"view the wiki"
            f"</a>"
        )
        version_label.setTextFormat(Qt.RichText)
        version_label.setAlignment(Qt.AlignCenter)
        version_label.setOpenExternalLinks(True)
        version_label.setStyleSheet("""
            QLabel { color: #f0f0f0; }
            a { color: #F08000; }
        """)
        layout.addWidget(version_label)
        
        msg_box.layout().addWidget(container_widget, 0, 0, 1, msg_box.layout().columnCount())
        
        msg_box.setStandardButtons(QMessageBox.Ok)

        msg_box.exec_()

    # ------------------------------------------------------------------
    #  .fiopak Export Integration
    # ------------------------------------------------------------------

    def setup_package_actions(self):
        """Add package actions to the Tools menu."""
        export_action = QAction("Export Game Package...", self)
        export_action.setShortcut("Ctrl+Shift+E")
        export_action.triggered.connect(self.export_game_package)
        self.tools_menu.addAction(export_action)

        play_action = QAction("Play Game Package...", self)
        play_action.triggered.connect(self.play_game_package)
        self.tools_menu.addAction(play_action)

    def export_game_package(self):
        """Export a game package. If the level is unsaved, create a temporary saved copy first."""
        import tempfile
        import os
        import json

        # Close any open overlay first: its close callback runs now, not
        # after the temporary copy below exists (an earlier export overlay's
        # cleanup would otherwise delete this export's copy).
        self._close_current_overlay()

        # Determine the map path to use for export
        if self.unsaved_changes or self.file_path is None:
            # Unsaved or never saved – create a temporary file
            try:
                # Ensure maps directory exists (optional, temp can go to system temp)
                maps_dir = os.path.join(self.root_dir, "maps")
                if not os.path.exists(maps_dir):
                    os.makedirs(maps_dir)

                # Create a temporary file inside maps/ (or system temp)
                fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="export_temp_", dir=maps_dir)
                os.close(fd)
                self._export_temp_file = temp_path

                # Write current level data to temp file
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.state.get_level_data(), f, indent=4)

                current_map = temp_path
                self.show_toast("Using temporary saved copy for export...")
            except Exception as e:
                self._discard_export_temp_file()
                self.show_toast(f"Failed to create temporary map: {e}", is_error=True)
                return
        else:
            # Already saved – use the existing file
            current_map = self.file_path

        # Proceed with export using current_map (temp or real)
        from editor.package_dialog import PackageMetadataDialog

        # Create container + dialog
        container = QWidget()
        container.setObjectName("ExportContainer")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self._export_dialog = PackageMetadataDialog(
            current_map,
            parent=container,
            close_callback=self._cleanup_export_overlay
        )
        layout.addWidget(self._export_dialog)

        # Replace the export button's default behaviour with actual export
        self._export_dialog.export_btn.clicked.disconnect()
        self._export_dialog.export_btn.clicked.connect(
            lambda: self._run_export(self._export_dialog, current_map)
        )

        # Cancel button and close event should close the overlay
        self._export_dialog.cancel_btn.clicked.disconnect()
        self._export_dialog.cancel_btn.clicked.connect(self._close_current_overlay)
        self._export_dialog.rejected.connect(self._close_current_overlay)

        self._show_overlay(container, close_callback=self._cleanup_export_overlay)

    def _run_export(self, dialog, current_map):
        """Execute the export from the dialog's metadata."""
        metadata = dialog.build_metadata()
        if not metadata:
            return

        # Normalise paths
        abs_map = os.path.abspath(current_map)
        if not os.path.isfile(abs_map):
            QMessageBox.critical(
                dialog, "Export Error",
                f"The map file could not be found:\n\n{abs_map}\n\n"
                "Please save the level and try again."
            )
            return

        # Ask user where to save the package
        packages_dir = os.path.join(self.root_dir, "packages")
        if not os.path.exists(packages_dir):
            os.makedirs(packages_dir)

        output_path, _ = QFileDialog.getSaveFileName(
            dialog,
            "Export Game Package",
            os.path.join(packages_dir, f"{metadata['title']}.fiopak"),
            "Game Packages (*.fiopak)"
        )
        if not output_path:
            # Cancelled the file dialog only: the overlay stays open, so the
            # temporary copy stays too (the overlay's close removes it).
            return

        from editor.package_exporter import PackageExporter
        exporter = PackageExporter(self.state, self.root_dir)
        success, errors = exporter.export(output_path, metadata, abs_map, parent_widget=dialog)

        if success:
            dialog.dep_label.setStyleSheet("color: #4CAF50; font-size: 12px; padding: 4px;")
            dialog.dep_label.setText(f"Export successful!\nSaved to: {os.path.basename(output_path)}")
            dialog.export_btn.setText("Done")
            dialog.export_btn.setEnabled(False)
            self.show_toast(f"Package exported: {os.path.basename(output_path)}")
        else:
            dialog.dep_label.setStyleSheet("color: #f44336; font-size: 12px; padding: 4px;")
            dialog.dep_label.setText("Export failed:\n" + "\n".join(errors[:5]))
            # Error already shown in exporter

    def new_map(self):
        # Check for unsaved changes
        if not self.check_unsaved_changes():
            return

        self._clear_terrain()
        self.state.clear_scene()
        update_all_counters_from_entities([])
        
        self.file_path = None
        self.unsaved_changes = False
        self.update_title()
        self.update_all_ui()
        self._refresh_logic_graph()

    def perform_subtraction(self, push_undo=True, target_brush=None):
        """CSG-subtract the selected brush, optionally from one target brush only.

        ``target_brush`` is used by compound editor operations such as Hollow:
        the temporary cutter must not modify unrelated geometry that happens to
        sit inside the selected brush.

        ``push_undo`` lets a caller that has already opened an undo checkpoint
        (Hollow, which runs a subtract as one step of a larger operation) fold
        this into that single step instead of stacking a second one.
        """
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "Invalid Selection", "Select a brush for CSG Subtract")
            return

        if push_undo:
            self.save_state()

        self.state.selected_object['operation'] = 'subtract'
        subtract_brush = self.state.selected_object
        
        sub_pos = subtract_brush['pos']
        sub_size = subtract_brush['size']
        sub_min = [sub_pos[0] - sub_size[0]/2, sub_pos[1] - sub_size[1]/2, sub_pos[2] - sub_size[2]/2]
        sub_max = [sub_pos[0] + sub_size[0]/2, sub_pos[1] + sub_size[1]/2, sub_pos[2] + sub_size[2]/2]
        
        new_brushes = []
        for brush in self.state.brushes:
            if brush is subtract_brush:
                continue

            # A targeted subtraction is deliberately isolated to the caller's
            # brush.  This is essential for Hollow: an object already inside
            # the outer box is not part of the hollowing operation and must be
            # left completely untouched.
            if target_brush is not None and brush is not target_brush:
                new_brushes.append(brush)
                continue
                
            if brush.get('operation') == 'subtract':
                new_brushes.append(brush)
                continue
        
            pos = brush['pos']
            size = brush['size']
            brush_min = [pos[0] - size[0]/2, pos[1] - size[1]/2, pos[2] - size[2]/2]
            brush_max = [pos[0] + size[0]/2, pos[1] + size[1]/2, pos[2] + size[2]/2]
            
            # No intersection -> keep brush unchanged
            if not (brush_min[0] < sub_max[0] and brush_max[0] > sub_min[0] and
                    brush_min[1] < sub_max[1] and brush_max[1] > sub_min[1] and
                    brush_min[2] < sub_max[2] and brush_max[2] > sub_min[2]):
                new_brushes.append(brush)
                continue
                
            fragments = []
            base_textures = brush['textures'].copy()
            base_color = brush.get('color', None)
            base_name = brush.get('name', '')
            
            # ----- Left slab (x < sub_min[0]) -----
            if brush_min[0] < sub_min[0]:
                left_max = min(brush_max[0], sub_min[0])
                if left_max - brush_min[0] > 0.01:
                    frag = {
                        'pos': [(brush_min[0] + left_max)/2, pos[1], pos[2]],
                        'size': [left_max - brush_min[0], size[1], size[2]],
                        'operation': 'add',
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_left"
                    fragments.append(frag)
            
            # ----- Right slab (x > sub_max[0]) -----
            if brush_max[0] > sub_max[0]:
                right_min = max(brush_min[0], sub_max[0])
                if brush_max[0] - right_min > 0.01:
                    frag = {
                        'pos': [(right_min + brush_max[0])/2, pos[1], pos[2]],
                        'size': [brush_max[0] - right_min, size[1], size[2]],
                        'operation': 'add',
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_right"
                    fragments.append(frag)
            
            # ----- Bottom slab (y < sub_min[1]) -----
            if brush_min[1] < sub_min[1]:
                bottom_max = min(brush_max[1], sub_min[1])
                # X overlap region (the part that hasn't been cut away by left/right)
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                if bottom_max - brush_min[1] > 0.01 and x_max - x_min > 0.01:
                    frag = {
                        'pos': [(x_min + x_max)/2, (brush_min[1] + bottom_max)/2, pos[2]],
                        'size': [x_max - x_min, bottom_max - brush_min[1], size[2]],
                        'operation': 'add',
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_bottom"
                    fragments.append(frag)
            
            # ----- Top slab (y > sub_max[1]) -----
            if brush_max[1] > sub_max[1]:
                top_min = max(brush_min[1], sub_max[1])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                if brush_max[1] - top_min > 0.01 and x_max - x_min > 0.01:
                    frag = {
                        'pos': [(x_min + x_max)/2, (top_min + brush_max[1])/2, pos[2]],
                        'size': [x_max - x_min, brush_max[1] - top_min, size[2]],
                        'operation': 'add',
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_top"
                    fragments.append(frag)
            
            # ----- Front slab (z < sub_min[2]) -----
            if brush_min[2] < sub_min[2]:
                front_max = min(brush_max[2], sub_min[2])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                y_min = max(brush_min[1], sub_min[1])
                y_max = min(brush_max[1], sub_max[1])
                if front_max - brush_min[2] > 0.01 and x_max - x_min > 0.01 and y_max - y_min > 0.01:
                    frag = {
                        'pos': [(x_min + x_max)/2, (y_min + y_max)/2, (brush_min[2] + front_max)/2],
                        'size': [x_max - x_min, y_max - y_min, front_max - brush_min[2]],
                        'operation': 'add',
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_front"
                    fragments.append(frag)
            
            # ----- Back slab (z > sub_max[2]) -----
            if brush_max[2] > sub_max[2]:
                back_min = max(brush_min[2], sub_max[2])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                y_min = max(brush_min[1], sub_min[1])
                y_max = min(brush_max[1], sub_max[1])
                if brush_max[2] - back_min > 0.01 and x_max - x_min > 0.01 and y_max - y_min > 0.01:
                    frag = {
                        'pos': [(x_min + x_max)/2, (y_min + y_max)/2, (back_min + brush_max[2])/2],
                        'size': [x_max - x_min, y_max - y_min, brush_max[2] - back_min],
                        'operation': 'add',