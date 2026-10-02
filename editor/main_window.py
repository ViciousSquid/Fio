import sys
import json
import os
import subprocess
import random
import numpy as np
import configparser
import math
import copy
import glm
import time
from datetime import datetime


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QFileDialog, QDialog, QWidget, QLabel, QVBoxLayout,
    QGraphicsOpacityEffect, QInputDialog, QColorDialog, QProgressDialog, QAction, QToolBar, QDockWidget,
    QPushButton, QDialogButtonBox, QHBoxLayout
)
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtCore import Qt, QByteArray, QTimer, QPropertyAnimation, QEasingCurve, QPoint, pyqtSignal
from PyQt5.QtGui import QKeySequence, QPixmap, QCursor, QColor, QIcon

from editor.things import Light, PlayerStart, Model, update_all_counters_from_entities
from editor.SettingsWindow import SettingsWindow
from editor.ui import Ui_MainWindow
from engine.constants import TILE_SIZE, WALL_TILE, FLOOR_TILE
from editor.view_2d import View2D
from editor.editor_state import EditorState
from editor.terrain_editor import TerrainEditorPanel
from engine.terrain import Terrain
from editor.debug_console import DebugConsole, CommandInput, debug_log
from editor.console_commands import ConsoleCommandHandler
from editor.procedural_generator import ProceduralMapWidget


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
        self.anim.setDuration(600)
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

    def show_message(self, text, parent_widget, is_error=False, duration=None, 
                     is_tooltip=False, toast_id=None):
        """Show toast notification with STRICT bottom-middle positioning."""
        if is_tooltip:
            bg_color = "#2b2b2b"
        elif is_error:
            bg_color = "#8B0000"
        else:
            bg_color = "#2E6F40"
        
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

    def hide_toast(self, toast_id=None):
        """Hide toast, optionally only if matching ID."""
        if self.isVisible():
            if toast_id is not None and self.current_toast_id != toast_id:
                return
            self.current_toast_id = None
            self.fade_out()

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
        self.file_path = None
        self.recent_files = []
        self.load_level_signal.connect(self.load_level_file)

        self.setWindowTitle("Fio")
        self.setWindowIcon(QIcon(os.path.join(self.root_dir, 'assets', 'icon.ico')))
        self.setGeometry(100, 100, 1600, 900)
        self.setMinimumSize(1280, 800)
        self.state = EditorState()
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
        self.tool_mode = 'select'
        self.preview_timer = QTimer(self)  # OPTIMIZATION: Added parent=self for proper cleanup
        self.preview_timer.timeout.connect(self.update_mover_preview)
        self.preview_data = {} 
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.update_recent_files_menu()
        self.setup_package_actions() 
        self.update_title()
        
        self.ctrl_tab_shortcut = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.ctrl_tab_shortcut.activated.connect(self.cycle_2d_view)

        # Page Up / Page Down rotate brush-face textures 90 degrees. Window-
        # level shortcuts so they fire no matter which panel has focus.
        self.tex_rot_cw_shortcut = QShortcut(QKeySequence(Qt.Key_PageUp), self)
        self.tex_rot_cw_shortcut.activated.connect(lambda: self.rotate_textures(1))
        self.tex_rot_ccw_shortcut = QShortcut(QKeySequence(Qt.Key_PageDown), self)
        self.tex_rot_ccw_shortcut.activated.connect(lambda: self.rotate_textures(-1))
        self.setFocus()
        self.update_global_font()
        self.load_layout()
        
        self.terrain = None
        self.terrain_editor_window = None
        self.surface_inspector = None  # lazily created Face-mode Surface Inspector

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

        self.show_logic_links = True
        
        # Tooltips
        self.camera_movement_learned = self.config.getboolean('Tooltips', 'camera_movement_learned', fallback=False)
        self.startup_tooltip_shown = False
        self.tooltip_tips = [
            "Right-click + WASD: Move camera",
            "Mouse wheel: Zoom in/out",
            "Ctrl+Tab: Cycle 2D views",
            "Space: Clone selected brush/object",
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
        """Clean up references after the export overlay is closed."""
        if hasattr(self, '_export_dialog'):
            self._export_dialog = None
        # The overlay itself will be destroyed by _close_current_overlay

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
        
        self.recent_menu.clear()
        
        if not self.recent_files:
            dummy = QAction("No recent files", self)
            dummy.setEnabled(False)
            self.recent_menu.addAction(dummy)
            return
            
        for path in self.recent_files:
            # Check if file still exists
            if not os.path.exists(path):
                continue
                
            fname = os.path.basename(path)
            action = QAction(fname, self)
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
            
            with open(save_path, 'w') as f:
                json.dump(self.state.get_level_data(), f, indent=4)
            
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
            # Load the generated map without closing the generator
            import tempfile, json, os
            fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="procedural_")
            os.close(fd)
            with open(temp_path, 'w') as f:
                json.dump(map_data, f, indent=4)
            self.load_level_file(temp_path)
            os.unlink(temp_path)
            self.show_toast("Generated map loaded – use Save As to keep it")
        else:
            # User closed the generator – close the overlay
            self._close_current_overlay()

    def _on_procedural_map_closed(self, map_data):
        """
        Called when the procedural map widget emits map_generated.
        - If map_data is None → user clicked X → restore original Properties tab.
        - Else → load the generated map into the editor (generator remains open).
        """
        if map_data is None:
            # Restore original content (user closed the generator)
            self.properties_dock.setWidget(self._original_properties_widget)
            self._original_properties_widget = None
        else:
            # Load the generated map without closing the generator
            try:
                # Create a temporary file name (not saved to disk unless user saves later)
                import tempfile
                fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="procedural_", dir=None)
                os.close(fd)
                with open(temp_path, 'w') as f:
                    json.dump(map_data, f, indent=4)

                # Load the temporary file
                self.load_level_file(temp_path)

                # Optionally delete the temp file after load
                # (load_level_file makes a copy of the data, so we can delete)
                os.unlink(temp_path)

                self.show_toast("Generated map loaded – use Save As to keep it")
            except Exception as e:
                self.show_toast(f"Failed to load generated map: {e}", is_error=True)
                import traceback
                traceback.print_exc()

    def load_procedural_map(self, map_data):
        """Save the generated map to maps/ folder and load it."""
        if not self.check_unsaved_changes():
            return
        try:
            # Ensure maps directory exists
            maps_dir = os.path.join(self.root_dir, "maps")
            if not os.path.exists(maps_dir):
                os.makedirs(maps_dir)

            # Generate a unique filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"procedural_{timestamp}.json"
            filepath = os.path.join(maps_dir, filename)

            # Save map data to file
            with open(filepath, 'w') as f:
                json.dump(map_data, f, indent=4)

            # Now load the saved file (reuse existing load_level_file logic)
            self.load_level_file(filepath)

        except Exception as e:
            self.show_toast(f"Failed to save/load procedural map: {e}", is_error=True)
            import traceback
            traceback.print_exc()

    def center_2d_views_on(self, world_pos):
        """Center all 2D views on the given world position (list/tuple of [x, y, z])."""
        from PyQt5.QtCore import QPointF
        self.view_top.pan_offset = QPointF(world_pos[0], world_pos[2])
        self.view_side.pan_offset = QPointF(world_pos[2], world_pos[1])
        self.view_front.pan_offset = QPointF(world_pos[0], world_pos[1])
        self.view_top.update()
        self.view_side.update()
        self.view_front.update()


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
        from PyQt5.QtWidgets import QProgressDialog
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
        """Clone the selected object with offset, identical to pressing Space."""
        if not self.state.selected_object:
            return
            
        self.save_state()
        
        # Clone the object
        if isinstance(self.state.selected_object, dict):
            new_obj = copy.deepcopy(self.state.selected_object)
            self.state.brushes.append(new_obj)
        else:
            new_obj = copy.copy(self.state.selected_object)
            self.state.things.append(new_obj)
            
        # Offset based on current 2D view (uses grid size like Space key)
        current_view = self.right_tabs.currentWidget()
        if isinstance(current_view, View2D):
            axis_map = {'top': ('x', 'z'), 'side': ('y', 'z'), 'front': ('x', 'y')}
            pos_map = {'x': 0, 'y': 1, 'z': 2}
            ax1_name, ax2_name = axis_map.get(current_view.view_type, ('x', 'z'))
            offset = self.grid_size_spinbox.value()
            from engine.brush_geometry import translate_brush, brush_has_geometry
            if isinstance(new_obj, dict) and brush_has_geometry(new_obj):
                # Angled brush: shift its plane set, not just 'pos'.
                delta = [0.0, 0.0, 0.0]
                delta[pos_map[ax1_name]] += offset
                delta[pos_map[ax2_name]] += offset
                translate_brush(new_obj, delta)
            else:
                pos_ref = new_obj['pos'] if isinstance(new_obj, dict) else new_obj.pos
                pos_ref[pos_map[ax1_name]] += offset
                pos_ref[pos_map[ax2_name]] += offset

        self.set_selected_object(new_obj)
        
        # Show toast notification
        self.show_toast("Brush cloned")
        
        # Add flash effect for brushes (hot pink highlight)
        if isinstance(new_obj, dict):
            new_obj['_flash_until'] = time.time() + 0.5  # Flash for 0.5s
            
            # Set timer to remove flash and update view
            QTimer.singleShot(500, lambda: self._clear_flash(new_obj))
            
            # Immediate repaint to show flash
            self.update_all_ui()

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

        # FIX: Initialize with only 'pos', then set properties
        new_model = Model(pos=[0, 0, 0])
        new_model.properties['model_path'] = filepath.replace('\\', '/') # Ensure forward slashes
        new_model.properties['rotation'] = rotation
        new_model.properties['scale'] = scale
        
        # Set a default name based on filename
        model_name = os.path.splitext(os.path.basename(filepath))[0]
        new_model.properties['name'] = model_name
        
        self.state.things.append(new_model)
        self.set_selected_object(new_model)
        self.show_toast(f"Added {model_name}")

    def set_selected_object(self, obj):
        """Set a single selected object (backwards compatibility)."""
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

    def update_scene_hierarchy(self):
        self.scene_hierarchy.refresh_list(self.state.brushes, self.state.things, self.state.selected_object)
    
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

    def apply_caulk_to_brush(self):
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "No Brush Selected", "Select a brush to apply caulk to.")
            return
        self.save_state()
        if 'textures' not in self.state.selected_object:
            self.state.selected_object['textures'] = {}
        for face in ['north','south','east','west','top','down']:
            self.state.selected_object['textures'][face] = 'caulk.jpg'
        self.update_views()

    def toggle_face_mode(self, active):
        """Toggles the Face Mode in the 3D view."""
        if not hasattr(self, 'view_3d'): return

        self.view_3d.face_mode_active = active

        # Sync the FACE button state if triggered via ESC or other means. The
        # FACE button lives on the textures tab of the asset browser.
        tex_tab = getattr(getattr(self, 'asset_browser', None), 'tab_textures', None)
        if tex_tab is not None:
            face_btn = getattr(tex_tab, 'face_btn', None)
            if face_btn is not None:
                face_btn.blockSignals(True)
                face_btn.setChecked(active)
                face_btn.blockSignals(False)
        
        if active:
            self.show_toast("FACE MODE: Select a face to texture (Purple) — Page Up/Down rotates it", duration=3000)
            self.set_selected_object(None) # Deselect current object to clear gizmos and allow clean hover
            
            # Change cursor to indicate mode
            self.view_3d.setCursor(Qt.CrossCursor)
        else:
            self.show_toast("FACE MODE: OFF")
            self.view_3d.hovered_face_info = None # Clear highlight
            self.view_3d.setCursor(Qt.ArrowCursor)
            if self.surface_inspector is not None:
                self.surface_inspector.hide()
            
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

    def show_surface_inspector(self, brush, face_name):
        """Open (or re-target) the Face-mode Surface Inspector for a face."""
        if self.surface_inspector is None:
            from editor.surface_inspector import SurfaceInspector
            self.surface_inspector = SurfaceInspector(self, self)
        self.surface_inspector.set_target(brush, face_name)

    def apply_texture_to_brush(self, texture_path, tiled=False):
        """
        Apply texture to the selected brush.
        If tiled=True: 1 texture pixel = 1 world unit.
        A 512x512 texture on a 512-unit face tiles once.
        A 256x256 texture on a 512-unit face tiles twice.
        If tiled=False: texture is stretched to fit (old behaviour).
        """
        import os
        from PyQt5.QtGui import QPixmap

        selected = self.state.selected_object
        if not isinstance(selected, dict):
            self.show_toast("Select a brush first", is_error=True)
            return

        # --- Load texture to read its pixel dimensions ---
        full_path = os.path.join(self.root_dir, 'assets', 'textures', texture_path)
        pixmap = QPixmap(full_path)
        if pixmap.isNull():
            self.show_toast("Failed to load texture", is_error=True)
            return

        tex_w = pixmap.width()
        tex_h = pixmap.height()

        # --- Ensure brush has texture storage ---
        if 'textures' not in selected:
            selected['textures'] = {}

        faces = ['north', 'south', 'east', 'west', 'top', 'down']
        sx, sy, sz = selected['size']

        # --- Calculate face dimensions in world units ---
        def get_face_size(face):
                """Return (width, height) in world units for the given face."""
                if face in ('north', 'south'):
                    return (sx, sy)        # width = x, height = y
                elif face in ('east', 'west'):
                    return (sz, sy)        # width = z, height = y
                else:  # top, down
                    return (sx, sz)        # width = x, height = z

        # --- Apply to all faces ---
        for face in faces:
            selected['textures'][face] = texture_path

            if tiled:
                face_w, face_h = get_face_size(face)

                # 1 pixel = 1 world unit
                # A 512px texture on a 512-unit face repeats 1.0 times
                # A 256px texture on a 512-unit face repeats 2.0 times
                repeat_u = face_w / tex_w
                repeat_v = face_h / tex_h

                if 'uv_scale' not in selected:
                    selected['uv_scale'] = {}
                selected['uv_scale'][face] = [repeat_u, repeat_v]
            else:
                # FIT mode: remove any custom UV scaling (stretch 0→1)
                if 'uv_scale' in selected:
                    selected['uv_scale'].pop(face, None)

        self.save_state()
        self.update_views()

        mode_str = "tiled (1px = 1 unit)" if tiled else "fitted"
        self.show_toast(f"Applied {mode_str}: {tex_w}x{tex_h}")

    def apply_texture_to_selected_face(self, face_name):
        if not isinstance(self.state.selected_object, dict):
            return

        texture_path = self.asset_browser.get_selected_filepath()
        if not texture_path:
            QMessageBox.warning(self, "No Texture Selected", "Select a texture from the Asset Browser.")
            return

        texture_name = os.path.basename(texture_path)
        self.save_state()
        
        if 'textures' not in self.state.selected_object:
            self.state.selected_object['textures'] = {}

        self.state.selected_object['textures'][face_name] = texture_name
        self.update_views()

    def generate_collision_map(self):
        if not self.state.brushes:
            return None

        min_x_world, max_x_world = float('inf'), float('-inf')
        min_z_world, max_z_world = float('inf'), float('-inf')

        solid_brushes_exist = False
        for brush in self.state.brushes:
            if not brush.get('is_trigger', False) and not brush.get('operation') == 'subtract':
                solid_brushes_exist = True
                pos, size = np.array(brush['pos']), np.array(brush['size'])
                half_size = size / 2.0

                min_x_world = min(min_x_world, pos[0] - half_size[0])
                max_x_world = max(max_x_world, pos[0] + half_size[0])
                min_z_world = min(min_z_world, pos[2] - half_size[2])
                max_z_world = max(max_z_world, pos[2] + half_size[2])

        if not solid_brushes_exist:
            return None

        padding = TILE_SIZE * 2
        padded_min_x = min_x_world - padding
        padded_max_x = max_x_world + padding
        padded_min_z = min_z_world - padding
        padded_max_z = max_z_world + padding

        min_x_tile_idx = int(math.floor(padded_min_x / TILE_SIZE))
        max_x_tile_idx = int(math.ceil(padded_max_x / TILE_SIZE))
        min_z_tile_idx = int(math.floor(padded_min_z / TILE_SIZE))
        max_z_tile_idx = int(math.ceil(padded_max_z / TILE_SIZE))

        map_width_tiles = max_x_tile_idx - min_x_tile_idx
        map_depth_tiles = max_z_tile_idx - min_z_tile_idx

        map_width_tiles = max(1, map_width_tiles)
        map_depth_tiles = max(1, map_depth_tiles)

        collision_tile_map = np.full((map_depth_tiles, map_width_tiles), FLOOR_TILE, dtype=int)

        for brush in self.state.brushes:
            if brush.get('is_trigger', False) or brush.get('operation') == 'subtract':
                continue

            pos, size = np.array(brush['pos']), np.array(brush['size'])
            half_size = size / 2.0

            brush_min_x_world = pos[0] - half_size[0]
            brush_max_x_world = pos[0] + half_size[0]
            brush_min_z_world = pos[2] - half_size[2]
            brush_max_z_world = pos[2] + half_size[2]

            brush_min_x_map_tile = int(math.floor(brush_min_x_world / TILE_SIZE) - min_x_tile_idx)
            brush_max_x_map_tile = int(math.ceil(brush_max_x_world / TILE_SIZE) - min_x_tile_idx)
            brush_min_z_map_tile = int(math.floor(brush_min_z_world / TILE_SIZE) - min_z_tile_idx)
            brush_max_z_map_tile = int(math.ceil(brush_max_z_world / TILE_SIZE) - min_z_tile_idx)

            min_x_idx_clamped = max(0, brush_min_x_map_tile)
            max_x_idx_clamped = min(map_width_tiles, brush_max_x_map_tile)
            min_z_idx_clamped = max(0, brush_min_z_map_tile)
            max_z_idx_clamped = min(map_depth_tiles, brush_max_z_map_tile)

            if min_x_idx_clamped < max_x_idx_clamped and min_z_idx_clamped < max_z_idx_clamped:
                collision_tile_map[min_z_idx_clamped:max_x_idx_clamped, min_x_idx_clamped:max_x_idx_clamped] = WALL_TILE

        return collision_tile_map


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

        physics_enabled = self.config.getboolean('Settings', 'physics', fallback=True)
        self.view_3d.toggle_play_mode(player_start.pos, player_start.get_angle(), physics_enabled)
        self.view_3d.setFocus()
        
        # Update play button color
        self.update_play_button_color()
        
        #self.ui.notification_label.setText("ESC = EXIT PLAY MODE  |  F12 = FULLSCREEN")


    def _exit_play_mode(self):
        """Exit play mode and return to editor."""
        if hasattr(self.view_3d, 'play_mode') and self.view_3d.play_mode:
            self.view_3d.toggle_play_mode(None, None)
            self.view_3d.play_mode = False  # Force state change before UI update

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


    def show_generate_tilemap_dialog(self):
        if not self.file_path:
            self.save_level_as()
            if not self.file_path:
                QMessageBox.warning(self, "File Not Saved", "Please save the level before generating a tilemap.")
                return
        self.generate_and_save_tilemap(save_png=True)


    def generate_and_save_tilemap(self, save_png=False):
        self.save_level()

        generator_script_path = os.path.join(self.root_dir, 'tools', 'generate_tilemap.py')
        if not os.path.exists(generator_script_path):
            QMessageBox.critical(self, "Error", f"Tilemap generator script not found at:\n{generator_script_path}")
            return

        try:
            command = [sys.executable, generator_script_path, self.file_path]
            if save_png:
                command.append('--save-png')
            
            subprocess.run(command, check=True)
            QMessageBox.information(self, "Success", "Collision tilemap generated successfully.")
        except subprocess.CalledProcessError as e:
            QMessageBox.critical(self, "Error", f"Failed to generate tilemap.\n\nError: {e}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An unexpected error occurred:\n{e}")

    def update_shortcuts(self):
        apply_texture_shortcut = self.config.get('Controls', 'apply_texture', fallback='Shift+T')
        if hasattr(self, 'apply_texture_action'):
            self.apply_texture_action.setShortcut(QKeySequence(apply_texture_shortcut))
        save_layout_shortcut = self.config.get('Controls', 'save_layout', fallback='Ctrl+Shift+S')
        if hasattr(self, 'save_layout_action'):
            self.save_layout_action.setShortcut(QKeySequence(save_layout_shortcut))
        restore_layout_shortcut = self.config.get('Controls', 'restore_layout', fallback='Ctrl+Shift+L')
        if hasattr(self, 'restore_layout_action'):
            self.restore_layout_action.setShortcut(QKeySequence(restore_layout_shortcut))
        reset_layout_shortcut = self.config.get('Controls', 'reset_layout', fallback='Ctrl+Shift+R')
        if hasattr(self, 'reset_layout_action'):
            self.reset_layout_action.setShortcut(QKeySequence(reset_layout_shortcut))

    def toggle_backface_culling(self, state):
        """Toggle OpenGL backface culling."""
        self.view_3d.set_backface_culling(state == Qt.Checked)

    def toggle_frustum_culling(self, state):
        """Toggle CPU frustum culling."""
        self.view_3d.set_frustum_culling(state == Qt.Checked)
    
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
        self.view_3d.set_cull_distance(distance)

    def zoom_in_2d(self):
        current_view = self.right_tabs.currentWidget()
        if isinstance(current_view, View2D):
            current_view.zoom_in()

    def zoom_out_2d(self):
        current_view = self.right_tabs.currentWidget()
        if isinstance(current_view, View2D):
            current_view.zoom_out()

    def save_state(self):
        self.state.save_state()
        self.mark_as_modified() # Mark as dirty when state is saved for undo

    def undo(self):
        if self.state.undo():
            self.mark_as_modified() # Undo changes state
            self.update_all_ui()

    def redo(self):
        if self.state.redo():
            self.mark_as_modified() # Redo changes state
            self.update_all_ui()

    def set_render_mode(self, mode):
        self.view_3d.render_mode = mode
        self.update_views()

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

        subtitle_label = QLabel("Liminal World Editor & Procedural Engine")
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

        temp_file = None

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

                # Write current level data to temp file
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.state.get_level_data(), f, indent=4)

                temp_file = temp_path
                current_map = temp_path
                self.show_toast("Using temporary saved copy for export...")
            except Exception as e:
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
            lambda: self._run_export(self._export_dialog, current_map, temp_file)
        )

        # Cancel button and close event should close the overlay
        self._export_dialog.cancel_btn.clicked.disconnect()
        self._export_dialog.cancel_btn.clicked.connect(self._close_current_overlay)
        self._export_dialog.rejected.connect(self._close_current_overlay)

        self._show_overlay(container, close_callback=self._cleanup_export_overlay)

    def _run_export(self, dialog, current_map, temp_file=None):
        """Execute the export. If temp_file is provided, delete it afterwards."""
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

        # metadata['map_path'] is set by the exporter to the correct archive-internal path

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
            # User cancelled – clean up temp file if any
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except:
                    pass
            return

        # Run the export
        from editor.package_exporter import PackageExporter
        exporter = PackageExporter(self.state, self.root_dir)
        success, errors = exporter.export(output_path, metadata, abs_map, parent_widget=dialog)

        # Clean up temporary file if it exists
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except Exception as e:
                print(f"Warning: could not delete temp file {temp_file}: {e}")

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

    def _restore_properties_tabs(self):
        """Restore the Properties / Debug Console tab widget."""
        # Remove the export container
        if hasattr(self, '_export_container') and self._export_container:
            self._export_container.setParent(None)
            self._export_container.deleteLater()
            self._export_container = None
        
        # Restore the original tab widget to the dock
        self.properties_dock.setWidget(self.properties_tab_widget)
        self.properties_tab_widget.setParent(self.properties_dock)
        self.properties_tab_widget.show()
        
        # Restore previous tab if we tracked it
        if hasattr(self, '_original_properties_widget') and self._original_properties_widget:
            idx = self.properties_tab_widget.indexOf(self._original_properties_widget)
            if idx >= 0:
                self.properties_tab_widget.setCurrentIndex(idx)
        
        self._export_dialog = None

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

    def perform_subtraction(self):
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "Invalid Selection", "Select a brush for CSG Subtract")
            return

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
                        'textures': base_textures.copy()
                    }
                    if base_color: frag['color'] = base_color
                    if base_name: frag['name'] = f"{base_name}_back"
                    fragments.append(frag)
            
            new_brushes.extend(fragments)
        
        new_brushes.append(subtract_brush)
        self.state.brushes = new_brushes
        self.update_all_ui()


    def autocaulk(self):
        """Automatically apply nodraw to faces that are never visible."""
        self.save_state()  # Enables undo/redo

        # Collect all solid additive brushes (exclude subtract, trigger, fog)
        brushes = [
            b for b in self.state.brushes
            if b.get('operation') != 'subtract'
            and not b.get('is_trigger', False)
            and not b.get('is_fog', False)
        ]

        caulked_faces = 0
        for brush in brushes:
            # Ensure textures dict exists
            if 'textures' not in brush:
                brush['textures'] = {}

            for face in ['north', 'south', 'east', 'west', 'top', 'down']:
                current_tex = brush['textures'].get(face, '')
                if current_tex == 'nodraw.jpg':
                    continue   # already set

                if self._is_face_occluded(brush, face, brushes):
                    brush['textures'][face] = 'nodraw.jpg'
                    caulked_faces += 1

        if caulked_faces > 0:
            self.update_views()
            self.show_toast(f"Autocaulk applied: caulked {caulked_faces} face(s)", duration=5000)
        else:
            self.show_toast("Autocaulk: no occluded faces found")

    def _is_face_occluded(self, brush, face, all_brushes):
        """
        Returns True if the given face of 'brush' is fully covered by any other
        brush in 'all_brushes'. Uses a point sample just outside the face center.
        """
        pos = brush['pos']
        size = brush['size']
        epsilon = 1.0   # small offset to push sample outside the brush

        # Compute the sample point (center of the face, shifted outward)
        if face == 'north':
            center = [pos[0], pos[1], pos[2] + size[2]/2 + epsilon]
        elif face == 'south':
            center = [pos[0], pos[1], pos[2] - size[2]/2 - epsilon]
        elif face == 'east':
            center = [pos[0] + size[0]/2 + epsilon, pos[1], pos[2]]
        elif face == 'west':
            center = [pos[0] - size[0]/2 - epsilon, pos[1], pos[2]]
        elif face == 'top':
            center = [pos[0], pos[1] + size[1]/2 + epsilon, pos[2]]
        elif face == 'down':
            center = [pos[0], pos[1] - size[1]/2 - epsilon, pos[2]]
        else:
            return False

        # Check if the sample point lies inside any other brush
        for other in all_brushes:
            if other is brush:
                continue
            op = other['pos']
            osize = other['size']
            minx = op[0] - osize[0]/2
            maxx = op[0] + osize[0]/2
            miny = op[1] - osize[1]/2
            maxy = op[1] + osize[1]/2
            minz = op[2] - osize[2]/2
            maxz = op[2] + osize[2]/2

            # Use epsilon tolerance for floating-point safety
            if (minx - epsilon <= center[0] <= maxx + epsilon and
                miny - epsilon <= center[1] <= maxy + epsilon and
                minz - epsilon <= center[2] <= maxz + epsilon):
                return True
        return False

    def hollow_selected_brush(self):
        """Hollow out the selected brush by creating an inner subtraction brush."""
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "Invalid Selection", "Select a brush to hollow.")
            return

        outer_brush = self.state.selected_object
        
        # Check if brush is locked
        if outer_brush.get('lock', False):
            QMessageBox.warning(self, "Brush Locked", "Cannot hollow a locked brush.")
            return

        # Prompt for wall thickness (default 16)
        max_thickness = int(min(outer_brush['size']) // 2 - 1)
        thickness, ok = QInputDialog.getInt(
            self,
            "Hollow Brush",
            "Wall thickness (grid units):",
            value=16,
            min=8,  # Changed from 1 to 8
            max=max(8, max_thickness)  # Ensure at least 8
        )
        
        if not ok:
            return
        
        # Check if the brush is large enough to hollow
        min_size = min(outer_brush['size'])
        if min_size <= thickness * 2:
            QMessageBox.warning(
                self, 
                "Brush Too Small", 
                f"The brush is too small to hollow with thickness {thickness}.\n"
                f"Minimum dimension ({min_size}) must be greater than {thickness * 2}."
            )
            return

        self.save_state()
        
        # Get outer brush properties
        outer_pos = outer_brush['pos']
        outer_size = outer_brush['size']
        
        # Calculate inner brush size (reduced by thickness on each side = thickness * 2 total)
        inner_size = [
            outer_size[0] - thickness * 2,
            outer_size[1] - thickness * 2,
            outer_size[2] - thickness * 2
        ]
        
        # Inner brush has the same center position
        inner_pos = list(outer_pos)
        
        # Create inner brush with subtract operation
        inner_brush = {
            'pos': inner_pos,
            'size': inner_size,
            'operation': 'subtract',
            'textures': outer_brush.get('textures', {}).copy(),
            'name': f"{outer_brush.get('name', 'Brush')}_hollow_sub"  # Mark as temporary
        }
        
        # Add the inner brush to the scene
        self.state.brushes.append(inner_brush)
        
        # Now perform the subtraction using the inner brush
        # Store current selection
        original_selection = self.state.selected_object
        
        # Temporarily select the inner brush and perform subtraction
        self.state.selected_object = inner_brush
        self.perform_subtraction()
        
        # Remove the inner brush after subtraction (it's no longer needed)
        if inner_brush in self.state.brushes:
            self.state.brushes.remove(inner_brush)
        
        # Clear selection since the original brush is now replaced by fragments
        self.set_selected_object(None)
        
        self.show_toast(f"Hollowed with {thickness} unit walls")

    def create_room_from_brush(self):
        """Create a room by hollowing the brush and placing lights inside."""
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "Invalid Selection", "Please select a brush to convert to a room.")
            return

        outer_brush = self.state.selected_object
        
        # Check if brush is locked
        if outer_brush.get('lock', False):
            QMessageBox.warning(self, "Brush Locked", "Cannot modify a locked brush.")
            return

        # Prompt for wall thickness
        max_thickness = int(min(outer_brush['size']) // 2 - 1)
        thickness, ok = QInputDialog.getInt(
            self,
            "Create Room",
            "Wall thickness (grid units):",
            value=16,
            min=8,
            max=max(8, max_thickness)
        )
        
        if not ok:
            return
        
        # Check if the brush is large enough
        min_size = min(outer_brush['size'])
        if min_size <= thickness * 2:
            QMessageBox.warning(
                self, 
                "Brush Too Small", 
                f"The brush is too small to hollow with thickness {thickness}.\n"
                f"Minimum dimension ({min_size}) must be greater than {thickness * 2}."
            )
            return

        self.save_state()
        
        # Get outer brush properties
        outer_pos = outer_brush['pos']
        outer_size = outer_brush['size']
        
        # Store inner dimensions for light placement
        inner_width = outer_size[0] - thickness * 2
        inner_depth = outer_size[2] - thickness * 2
        inner_height = outer_size[1] - thickness * 2
        
        # Perform hollow operation
        inner_brush = {
            'pos': list(outer_pos),
            'size': [inner_width, inner_height, inner_depth],
            'operation': 'subtract',
            'textures': outer_brush.get('textures', {}).copy(),
            'name': f"{outer_brush.get('name', 'Brush')}_hollow_sub"
        }
        
        self.state.brushes.append(inner_brush)
        self.state.selected_object = inner_brush
        self.perform_subtraction()
        
        # Remove the inner brush 
        if inner_brush in self.state.brushes:
            self.state.brushes.remove(inner_brush)
        
        # Calculate number of lights needed (one per 1024x1024 area)
        # Using ceiling to ensure adequate lighting
        import math
        num_lights_x = max(1, math.ceil(inner_width / 1024))
        num_lights_z = max(1, math.ceil(inner_depth / 1024))
        
        # Calculate spacing between lights
        spacing_x = inner_width / num_lights_x if num_lights_x > 0 else 0
        spacing_z = inner_depth / num_lights_z if num_lights_z > 0 else 0
        
        # Place lights at the ceiling of the room (top of inner space)
        light_y = outer_pos[1] + thickness  # Top of inner space
        
        # Add lights
        for i in range(num_lights_x):
            for j in range(num_lights_z):
                # Calculate light position - centered in its grid cell
                x = outer_pos[0] - inner_width/2 + spacing_x/2 + i * spacing_x
                z = outer_pos[2] - inner_depth/2 + spacing_z/2 + j * spacing_z
                
                light_pos = [x, light_y, z]
                new_light = Light(pos=light_pos)
                self.state.things.append(new_light)
        
        # Update UI
        self.set_selected_object(None)
        
        # Show confirmation
        light_count = num_lights_x * num_lights_z
        self.show_toast(f"Created room with {thickness} unit walls and {light_count} light(s)")

    def rotate_selected_15(self):
        """Rotate the current selection by 15 degrees around the active 2D view axis."""
        current_view = self.right_tabs.currentWidget()

        if not isinstance(current_view, View2D):
            self.show_toast("Select a 2D view first", is_error=True)
            return

        selected = getattr(self.state, 'selected_objects', []) or []
        if self.state.selected_object is not None and self.state.selected_object not in selected:
            selected.append(self.state.selected_object)

        if not selected:
            self.show_toast("Select a brush first", is_error=True)
            return

        axis = current_view._rotate_axis_vec()
        if axis is None:
            return

        self.save_state()

        if self.apply_rotation_to_selection(15.0, axis, undoable=False):
            self.unsaved_changes = True
            self.state.mark_lighting_dirty()
            self.update_all_ui()
            self.show_toast("Rotated +15°")

    def rotate_selected_brush(self):
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "Invalid Selection", "Please select a brush to rotate.")
            return

        current_view = self.right_tabs.currentWidget()
        if not isinstance(current_view, View2D):
            QMessageBox.warning(self, "Invalid View", "Select a 2D view (Top, Side, or Front) to define the rotation axis.")
            return

        self.save_state()
        size = self.state.selected_object['size']
        view_type = current_view.view_type

        if view_type == 'top':
            size[0], size[2] = size[2], size[0]
        elif view_type == 'side':
            size[1], size[2] = size[2], size[1]
        elif view_type == 'front':
            size[0], size[1] = size[1], size[0]

        self.update_all_ui()

    def toggle_trigger_display(self, checked):
        self.view_3d.show_triggers_as_solid = checked
        self.view_3d.update()

    def keyPressEvent(self, event):
        # ------------------------------------------------------------------
        # PLAY MODE HANDLING (hardcoded shortcuts first)
        # ------------------------------------------------------------------
        if self.view_3d.play_mode:
            # If the play console overlay is open, swallow all keys except
            # tilde (close it) and Escape (also close it).
            if self._is_play_console_visible():
                if event.key() in (Qt.Key_QuoteLeft, Qt.Key_Escape):
                    self._hide_play_console_overlay()
                # All other keys go to the overlay input — don't process as game input
                return

            if event.key() == Qt.Key_Escape:
                self._exit_play_mode()

                if getattr(self, 'is_kiosk_mode', False):
                    self.exit_kiosk_mode()
                    return

                if not self.camera_movement_learned:
                    QTimer.singleShot(500, lambda: self.show_tooltip(
                        "Hold right mouse to move camera with WASD", duration=0, toast_id="camera_tip"))
                return

            elif event.key() == Qt.Key_F3:
                self.view_3d.show_sprites_in_play_mode = not self.view_3d.show_sprites_in_play_mode
                self.view_3d.update()
                return

            elif event.key() == Qt.Key_F1:
                self.view_3d.show_connections_in_play_mode = not getattr(self.view_3d, 'show_connections_in_play_mode', False)
                self.update_all_ui()
                return

            elif event.key() == Qt.Key_F12:
                if getattr(self, 'is_kiosk_mode', False):
                    self.exit_kiosk_mode(keep_play_mode=True)
                else:
                    self.enter_kiosk_mode()
                return

            elif event.key() == Qt.Key_E:
                if hasattr(self.view_3d, 'game_state') and self.view_3d.game_state:
                    self.view_3d.game_state.set_use_key_pressed()
                self.keys_pressed.add(event.key())
                return

            elif event.key() == Qt.Key_QuoteLeft:  # Tilde/backtick
                self.toggle_debug_console()
                return

            else:
                # Check for user‑defined key bindings (only if console input does NOT have focus)
                console_input = self.debug_console.command_input
                if not console_input.hasFocus():
                    key_seq = QKeySequence(event.key() | int(event.modifiers()))
                    key_str = key_seq.toString()
                    if key_str in self.key_bindings:
                        command = self.key_bindings[key_str]
                        self.console_handler.handle_command(command)
                        return
                # If no binding, just record the key for later use (e.g., movement)
                self.keys_pressed.add(event.key())
                return

        # ------------------------------------------------------------------
        # EDITOR MODE HANDLING (including bindings)
        # ------------------------------------------------------------------

        # Tilde always toggles console (works in both modes)
        if event.key() == Qt.Key_QuoteLeft:
            self.toggle_debug_console()
            return

        # ESC: exit face mode or deselect
        if event.key() == Qt.Key_Escape:
            if getattr(self.view_3d, 'face_mode_active', False):
                self.toggle_face_mode(False)
                return
            if self.state.selected_object:
                self.set_selected_object(None)
                return

        # Ctrl+C: Copy selected brush/entity
        if event.key() == Qt.Key_C and event.modifiers() == Qt.ControlModifier:
            if self.state.selected_object:
                self._brush_clipboard = copy.deepcopy(self.state.selected_object)
                name = ''
                if isinstance(self._brush_clipboard, dict):
                    name = self._brush_clipboard.get('name', 'Brush')
                else:
                    name = self._brush_clipboard.properties.get('name', 'Entity')
                self.show_toast(f"Copied: {name}")
            return

        # Ctrl+V: Paste copied brush/entity
        if event.key() == Qt.Key_V and event.modifiers() == Qt.ControlModifier:
            if self._brush_clipboard is not None:
                self.save_state()
                pasted = copy.deepcopy(self._brush_clipboard)

                # Offset the pasted object so it doesn't sit exactly on top
                offset = self.grid_size_spinbox.value()
                if isinstance(pasted, dict):
                    # Give it a unique name
                    base_name = pasted.get('name', 'Brush')
                    pasted['name'] = f"{base_name}_copy"
                    from engine.brush_geometry import translate_brush, brush_has_geometry
                    if brush_has_geometry(pasted):
                        # Angled brush: move the plane set with the offset.
                        translate_brush(pasted, [offset, 0.0, offset])
                    else:
                        pasted['pos'] = [
                            pasted['pos'][0] + offset,
                            pasted['pos'][1],
                            pasted['pos'][2] + offset,
                        ]
                    # Clear I/O connections on the copy so wires don't duplicate
                    pasted.pop('_io_connections', None)
                    pasted.pop('io_connections', None)
                    self.state.brushes.append(pasted)
                else:
                    base_name = pasted.properties.get('name', 'Entity')
                    pasted.properties['name'] = f"{base_name}_copy"
                    pasted.pos = [
                        pasted.pos[0] + offset,
                        pasted.pos[1],
                        pasted.pos[2] + offset,
                    ]
                    pasted.properties.pop('_io_connections', None)
                    pasted.properties.pop('io_connections', None)
                    self.state.things.append(pasted)

                self.set_selected_object(pasted)
                self.show_toast(f"Pasted: {base_name}")

                # Flash effect for brushes
                if isinstance(pasted, dict):
                    import time as _time
                    pasted['_flash_until'] = _time.time() + 0.5
                    QTimer.singleShot(500, lambda: self._clear_flash(pasted))
            else:
                self.show_toast("Nothing to paste", is_error=True)
            return

        # Delete key
        if self.state.selected_object and event.key() == Qt.Key_Delete:
            self.save_state()
            for obj in list(self.state.selected_objects):
                if isinstance(obj, dict):
                    if obj in self.state.brushes:
                        self.state.brushes.remove(obj)
                else:
                    if obj in self.state.things:
                        self.state.things.remove(obj)
            self.set_selected_objects([])
            return

        # H / Shift+H
        if self.state.selected_object and event.key() == Qt.Key_H:
            if event.modifiers() == Qt.ShiftModifier:
                self.unhide_all_brushes()
            elif isinstance(self.state.selected_object, dict):
                self.hide_selected_brush()
            return

        # Space: clone
        if self.state.selected_object and event.key() == Qt.Key_Space:
            self.clone_selected_object()
            return

        # G: toggle grid
        if event.key() == Qt.Key_G:
            new_state = not self.grid_visible
            self.toggle_grid(new_state)
            if hasattr(self, 'grid_btn'):
                self.grid_btn.blockSignals(True)
                self.grid_btn.setChecked(new_state)
                self.grid_btn.blockSignals(False)
            return

        # [  /  ] : decrease / increase grid size
        if event.key() == Qt.Key_BracketLeft:
            new_size = max(2, self.view_3d.grid_size // 2)
            self.set_grid_size(new_size)
            self.show_toast(f"Grid Size: {new_size}")
            return
        if event.key() == Qt.Key_BracketRight:
            new_size = min(128, self.view_3d.grid_size * 2)
            self.set_grid_size(new_size)
            self.show_toast(f"Grid Size: {new_size}")
            return

        # Camera movement lesson (WASD with right mouse held)
        if not self.camera_movement_learned and self.right_mouse_held:
            if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
                self.on_camera_moved_with_wasd()

        # Check for user‑defined key bindings (only if console input does NOT have focus)
        console_input = self.debug_console.command_input
        if not console_input.hasFocus():
            key_seq = QKeySequence(event.key() | int(event.modifiers()))
            key_str = key_seq.toString()
            if key_str in self.key_bindings:
                command = self.key_bindings[key_str]
                self.console_handler.handle_command(command)
                return

        # If we reach here, no binding consumed the key – record it for normal editor use
        self.keys_pressed.add(event.key())
        super().keyPressEvent(event)

    def hide_selected_brush(self):
        if isinstance(self.state.selected_object, dict):
            self.save_state()
            self.state.selected_object['hidden'] = True
            self.update_all_ui()

    def unhide_all_brushes(self):
        self.save_state()
        for brush in self.state.brushes:
            if 'hidden' in brush:
                brush['hidden'] = False
        self.update_all_ui()

    def keyReleaseEvent(self, event):
        if self.view_3d.play_mode:
            if event.key() in self.keys_pressed:
                self.keys_pressed.remove(event.key())
            return # Consume the event completely in play mode

        # Editor mode key releases below
        if event.key() in self.keys_pressed:
            self.keys_pressed.remove(event.key())
        self.update_views()
        super().keyReleaseEvent(event)

    def toggle_snap_to_grid(self, state):
        enabled = state == Qt.Checked
        for view in [self.view_top, self.view_side, self.view_front]:
            view.snap_to_grid_enabled = enabled

    def toggle_grid(self, visible):
        """Toggle grid visibility in 3D view only."""
        self.grid_visible = visible
        # Update the 3D view grid
        if hasattr(self.view_3d, 'grid_visible'):
            self.view_3d.grid_visible = visible
            self.view_3d.update()

    # ======================================================================
    # Base 2D tool: Select (marquee) vs Brush (draw geometry), Hammer-style
    # ======================================================================

    def set_tool_mode(self, mode):
        """Switch the base 2D interaction tool between 'select' and 'brush'.

        Picking a base tool also exits the Clip/Rotate drag tools (they are
        mutually exclusive with everything else, Hammer/Radiant style) and syncs
        the two toolbar buttons + view cursors.
        """
        mode = 'brush' if mode == 'brush' else 'select'
        self.tool_mode = mode

        # Leaving to a base tool cancels the special drag tools.
        if self.clip_mode:
            self.set_clip_mode(False)
        if self.rotate_mode:
            self.set_rotate_mode(False)

        # Keep both toolbar buttons in sync without re-triggering handlers.
        for name, wanted in (('select_tool_btn', mode == 'select'),
                             ('brush_tool_btn', mode == 'brush')):
            btn = getattr(self, name, None)
            if btn is not None and btn.isChecked() != wanted:
                btn.blockSignals(True)
                btn.setChecked(wanted)
                btn.blockSignals(False)

        cursor = Qt.ArrowCursor if mode == 'select' else Qt.CrossCursor
        for view in (self.view_top, self.view_side, self.view_front):
            view.reset_marquee()
            view.setCursor(cursor)
            view.update()

        self.show_toast("Select tool — drag a box to select, click empty to deselect"
                        if mode == 'select' else
                        "Brush tool — drag in a 2D view to create geometry")

    # ======================================================================
    # Clip / slice tool  (Radiant-style, toggled with X)
    # ======================================================================

    def toggle_clip_mode(self, checked):
        """Toolbar/shortcut handler: enter or leave clip mode."""
        self.set_clip_mode(bool(checked))

    def set_clip_mode(self, active):
        """Enable/disable the clip tool and sync the toolbar button + cursors."""
        active = bool(active)
        if active and self.rotate_mode:
            self.set_rotate_mode(False)  # the two drag tools are exclusive
        self.clip_mode = active
        # Keep the toolbar button's checked state in sync (e.g. when toggled by
        # the Esc key rather than by clicking the button).
        btn = getattr(self, 'scissor_btn', None)
        if btn is not None and btn.isChecked() != active:
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.blockSignals(False)
        for view in (self.view_top, self.view_side, self.view_front):
            view.clear_clip()
            view.setCursor(Qt.CrossCursor if active else Qt.ArrowCursor)
        if active:
            self.show_toast("Clip tool ON — click two points, Enter to cut  (X to exit)")
        else:
            self.show_toast("Clip tool OFF")

    def toggle_rotate_mode(self, checked):
        """Toolbar handler: enter or leave the free-rotate tool."""
        self.set_rotate_mode(bool(checked))

    def set_rotate_mode(self, active):
        """Enable/disable free-rotate and sync the toolbar button + cursors.

        In this mode, dragging with the left mouse in any 2D view spins the
        selected brush(es) about that view's axis, snapped to a fixed angle
        increment while grid snap is on (free/continuous when it is off).
        """
        active = bool(active)
        if active and self.clip_mode:
            self.set_clip_mode(False)  # the two drag tools are exclusive
        self.rotate_mode = active
        btn = getattr(self, 'rotate_btn', None)
        if btn is not None and btn.isChecked() != active:
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.blockSignals(False)
        for view in (self.view_top, self.view_side, self.view_front):
            view.cancel_rotate()
            view.setCursor(Qt.OpenHandCursor if active else Qt.ArrowCursor)
        if active:
            self.show_toast("Rotate tool ON — drag in a 2D view to spin "
                            "(snap toggles free/stepped, Esc exits)")
        else:
            self.show_toast("Rotate tool OFF")

    def apply_rotation_to_selection(self, angle_deg, axis, undoable=True):
        """Rotate every selected brush by ``angle_deg`` about ``axis`` (each
        around its own centre).  Returns the number of brushes rotated.

        ``undoable`` pushes a single undo checkpoint; the live drag passes
        ``False`` for the incremental steps and checkpoints once at the start.
        """
        from engine.brush_geometry import rotate_brush as _rotate
        selected = list(getattr(self.state, 'selected_objects', []) or [])
        if self.state.selected_object and self.state.selected_object not in selected:
            selected.append(self.state.selected_object)
        brushes = [b for b in selected if isinstance(b, dict)]
        if not brushes:
            return 0
        if undoable:
            self.save_state()
        count = 0
        for brush in brushes:
            if _rotate(brush, angle_deg, axis):
                count += 1
        return count

    def apply_clip_to_selection(self, normal, offset, keep_positive):
        """Clip every selected brush with the given plane; one coalesced undo.

        Returns the number of brushes actually cut.  Things and non-brush
        selections are ignored.
        """
        selected = list(getattr(self.state, 'selected_objects', []) or [])
        if self.state.selected_object and self.state.selected_object not in selected:
            selected.append(self.state.selected_object)
        brushes = [b for b in selected if isinstance(b, dict)]
        if not brushes:
            return 0

        from engine.brush_geometry import clip_brush as _clip
        self.save_state()  # single undo checkpoint for the whole operation
        count = 0
        for brush in brushes:
            # Clip in place without an extra per-brush undo snapshot.
            if _clip(brush, normal, offset, keep_positive=keep_positive):
                count += 1
        if count:
            self.state.mark_lighting_dirty()
            self.unsaved_changes = True
            self.update_views()
            if self.state.selected_object in brushes:
                self.property_editor.set_object(self.state.selected_object)
        else:
            # Nothing changed — drop the checkpoint we just pushed.
            if self.state.undo_stack:
                self.state.undo_stack.pop()
        return count

    def save_level_as(self):
        filePath, _ = QFileDialog.getSaveFileName(self, "Save Level As", "maps", "JSON Files (*.json)")
        if filePath:
            self.file_path = filePath
            self.stop_mover_preview()
            self.save_level()

    def save_level(self):
        if not self.file_path:
            self.save_level_as()
            return

        self.stop_mover_preview()

        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.state.get_level_data(), f, indent=4)
            print(f"Level saved to {self.file_path}")
            self.unsaved_changes = False
            self.update_title()
            self.add_recent_file(self.file_path)
            self.show_toast("Saved!")
        except Exception as e:
            self.show_toast(f"Error saving: {e}", is_error=True)
            print(f"Error saving level: {e}")

    def load_level(self):
        """Opens the file dialog to select a level, then loads it."""
        # 1. Check for unsaved changes first
        if not self.check_unsaved_changes():
            return

        # 2. Ask user for the file (Defines 'filePath')
        filePath, _ = QFileDialog.getOpenFileName(self, "Load Level", "maps", "JSON Files (*.json)")
        
        # 3. If the user selected a file (didn't cancel), load it
        if filePath:
            self.load_level_file(filePath)

    def load_level_file(self, filePath):
        """Loads a level. Used for both normal loading and LevelChanger."""
        try:
            print(f"[MainWindow] Loading level: {filePath}")

            # Capture play state BEFORE doing anything
            was_playing = hasattr(self.view_3d, 'play_mode') and self.view_3d.play_mode

            from engine.resource_manager import ResourceManager
            rm = ResourceManager()

            if rm.is_package_mode():
                map_data = rm.get_text_asset(filePath)
                if map_data is None:
                    raise FileNotFoundError(f"Map {filePath} not found in package.")
                level_data = json.loads(map_data)
            else:
                with open(filePath, 'r') as f:
                    level_data = json.load(f)

            # Clear current scene completely
            self.state.clear_scene()

            # --- Clear existing terrain BEFORE loading new data ---
            self._clear_terrain()
            self.view_3d.terrain = None
            if self.view_3d.logic_thread:
                self.view_3d.logic_thread.set_terrain(None)

            # Load new data
            self.state.load_from_data(level_data)

            # Re-initialize terrain if present in the new map
            if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                if self.terrain is None:
                    from engine.terrain import Terrain
                    self.terrain = Terrain()
                self.terrain.from_dict(self.state.terrain_data)

                if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                    self.view_3d.renderer.setup_terrain_shader(self.terrain)

                if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                    self.view_3d.logic_thread.set_terrain(self.terrain)
            else:
                self._clear_terrain()

            # --- Find PlayerStart and reposition camera ---
            player_start_pos = None
            player_angle = 0.0
            for t in self.state.things:
                if isinstance(t, PlayerStart):
                    player_start_pos = t.pos
                    player_angle = t.get_angle()
                    break

            if player_start_pos:
                # Read user preference (default = True)
                place_camera = self.config.getboolean('Display', 'place_camera_at_player_start', fallback=True)

                if place_camera:
                    # Place 3D editor camera exactly at player start
                    self.view_3d.camera.pos = glm.vec3(player_start_pos)
                    self.view_3d.camera.yaw = player_angle      # face the same direction
                    self.view_3d.camera.pitch = 0.0

                    # Center 2D views so the camera frustum is visible
                    self.center_2d_views_on(player_start_pos)
                     # Force a second update after event loop
                    QTimer.singleShot(50, lambda: self.center_2d_views_on(player_start_pos))
                else:
                    # Old behaviour: offset camera behind the spawn
                    self.view_3d.camera.pos = [
                        player_start_pos[0],
                        player_start_pos[1] + 80,
                        player_start_pos[2] + 200
                    ]
                    self.view_3d.camera.pitch = -20
                    self.view_3d.camera.yaw = -90
            else:
                # No player start – reset camera to default position
                self.view_3d.camera.pos = glm.vec3(0, 150, 400)
                self.view_3d.camera.yaw = -90
                self.view_3d.camera.pitch = -20

            # Update file path and UI state
            self.file_path = filePath
            self.unsaved_changes = False
            self.update_title()
            self.add_recent_file(filePath)

            # Force full UI and view refresh
            self.set_selected_object(None)
            self.update_all_ui()

            # Proper, synchronous play mode restart
            if was_playing:
                print("[MainWindow] Restarting Play Mode with new level...")
                if hasattr(self, 'exit_play_mode'):
                    self.exit_play_mode()
                else:
                    self.view_3d.play_mode = False
                self.enter_play_mode()

            print(f"[MainWindow] Successfully loaded {os.path.basename(filePath)}")
            self.show_toast(f"Loaded {os.path.basename(filePath)}")

            # Keep the Logic Graph in sync
            self._refresh_logic_graph()

            return True

        except Exception as e:
            print(f"ERROR loading level {filePath}: {e}")
            import traceback
            traceback.print_exc()
            self.show_toast(f"Failed to load level: {e}", is_error=True)
            return False

    def quicksave_and_launch(self):
        maps_dir = "maps"
        if not os.path.exists(maps_dir):
            os.makedirs(maps_dir)

        quicksave_path = os.path.join(maps_dir, "quick_save.json")
        try:
            with open(quicksave_path, 'w') as f:
                json.dump(self.state.get_level_data(), f, indent=4)
            print(f"Quicksave successful: {quicksave_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not quicksave level:\n{e}")
            return

        game_script_path = 'game.py'
        if not os.path.exists(game_script_path):
            QMessageBox.warning(self, "Warning", f"Could not find '{game_script_path}' to launch.")
            return
        try:
            subprocess.Popen([sys.executable, game_script_path, quicksave_path])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not launch game:\n{e}")
            
    def save_layout(self):
        if not self.config.has_section('Layout'):
            self.config.add_section('Layout')
        self.config['Layout']['geometry'] = self.saveGeometry().toHex().data().decode()
        self.config['Layout']['state'] = self.saveState().toHex().data().decode()
        self.save_config()
        self.statusBar().showMessage("Layout saved.", 2000)

    def restore_layout(self):
        """Restore the previously saved layout from settings.ini without restarting."""
        if not self.config.has_section('Layout') or \
           not (self.config.has_option('Layout', 'geometry') and self.config.has_option('Layout', 'state')):
            self.show_toast("No saved layout found. Save a layout first.", is_error=True)
            return
        
        try:
            # Restore geometry and state
            if self.config.has_option('Layout', 'geometry'):
                self.restoreGeometry(QByteArray.fromHex(self.config['Layout']['geometry'].encode()))
            if self.config.has_option('Layout', 'state'):
                self.restoreState(QByteArray.fromHex(self.config['Layout']['state'].encode()))
            
            # Restore menu bar and status bar visibility (not saved in state)
            if self.menuBar():
                self.menuBar().setVisible(True)
            self.statusBar().setVisible(True)
            
            self.show_toast("Layout restored")
        except Exception as e:
            self.show_toast(f"Failed to restore layout: {e}", is_error=True)
            import traceback
            traceback.print_exc()

    def load_layout(self):
        if self.config.has_section('Layout') and self.config.has_option('Layout', 'geometry'):
            self.restoreGeometry(QByteArray.fromHex(self.config['Layout']['geometry'].encode()))
        if self.config.has_section('Layout') and self.config.has_option('Layout', 'state'):
            self.restoreState(QByteArray.fromHex(self.config['Layout']['state'].encode()))

    def reset_layout(self):
        """Reset layout to default by deleting Layout section from settings.ini and restarting."""
        reply = QMessageBox.question(
            self,
            "Reset Layout",
            "Reset layout to default?\n\nThis will delete saved layout settings and restart the editor.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        try:
            if self.config.has_section('Layout'):
                self.config.remove_section('Layout')
                self.save_config()
                self._resetting_layout = True   # <-- ADD THIS LINE
                self.show_toast("Layout reset. Restarting editor...")
                QTimer.singleShot(500, self._restart_application)
            else:
                self.show_toast("No layout settings to reset.", is_error=True)
                
        except Exception as e:
            self.show_toast(f"Failed to reset layout: {e}", is_error=True)
            import traceback
            traceback.print_exc()

    def _restart_application(self):
        """Restart the application."""
        try:
            executable = sys.executable
            script = os.path.abspath(sys.argv[0])
            args = sys.argv[1:]
            
            self.close()
            subprocess.Popen([executable, script] + args)
            QApplication.quit()
            
        except Exception as e:
            self.show_toast(f"Failed to restart: {e}", is_error=True)

    def _safe_extract_zip(self, zip_path, dest_dir):
        """Extract a zip file safely, rejecting any member that would escape dest_dir."""
        import zipfile
        import os
        import shutil

        dest_dir = os.path.realpath(dest_dir)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                target_path = os.path.realpath(os.path.join(dest_dir, member.filename))
                if not target_path.startswith(dest_dir + os.sep) and target_path != dest_dir:
                    raise ValueError(f"Zip slip attempt detected: {member.filename}")
            zf.extractall(dest_dir)


    def play_game_package(self):
        """Import a .fiopak file and launch it in kiosk mode."""
        import zipfile
        import tempfile
        import shutil

        # Check for unsaved changes first
        if not self.check_unsaved_changes():
            return

        # Default to packages/ folder, fallback to root dir if empty
        packages_dir = os.path.join(self.root_dir, "packages")
        start_dir = packages_dir if os.path.exists(packages_dir) else self.root_dir

        filePath, _ = QFileDialog.getOpenFileName(
            self, "Select Game Package", start_dir, "Game Packages (*.fiopak)"
        )
        if not filePath:
            return

        temp_dir = None
        try:
            if not zipfile.is_zipfile(filePath):
                QMessageBox.critical(self, "Error", "Selected file is not a valid game package.")
                return

            # Extract package to temp directory
            temp_dir = tempfile.mkdtemp(prefix="fio_package_")
            with zipfile.ZipFile(filePath, 'r') as zf:
                self._safe_extract_zip(filePath, temp_dir)

            # Find the map JSON inside the package
            map_path = self._find_map_in_package(temp_dir)
            # Do NOT set file_path to the archive path – saving would corrupt the package.
            # Force a "Save As" dialog the first time the user saves.
            self.file_path = None
            if not map_path:
                QMessageBox.critical(self, "Error", "No map file found in game package.")
                return

            # Try to configure ResourceManager for package assets
            try:
                from engine.resource_manager import ResourceManager
                rm = ResourceManager()
                if hasattr(rm, 'set_package_root'):
                    rm.set_package_root(temp_dir)
                elif hasattr(rm, 'load_package'):
                    rm.load_package(filePath)
            except Exception as e:
                print(f"[Package] ResourceManager setup warning: {e}")

            # Load level data
            with open(map_path, 'r') as f:
                level_data = json.load(f)

            # Clear current scene and load package map
            self.state.clear_scene()
            self._clear_terrain()
            self.state.load_from_data(level_data)

            # ── Re-initialize terrain if present in the package map ───────────
            if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                if self.terrain is None:
                    from engine.terrain import Terrain
                    self.terrain = Terrain()
                self.terrain.from_dict(self.state.terrain_data)

                if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                    self.view_3d.renderer.setup_terrain_shader(self.terrain)

                if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                    self.view_3d.logic_thread.set_terrain(self.terrain)
            else:
                # No terrain in the package map – ensure it is absent from the scene
                self._clear_terrain()
            # ─────────────────────────────────────────────────────────────────

            # Store temp dir for cleanup on application close
            self._package_temp_dir = temp_dir
            temp_dir = None  # Prevent cleanup in finally block

            # Update UI state
            self.file_path = filePath
            self.unsaved_changes = False
            self.update_title()
            self.set_selected_object(None)
            self.update_all_ui()

            # Check if user wants editor mode instead of kiosk
            launch_in_editor = self.config.getboolean('Kiosk', 'launch_in_editor', fallback=False)
            if launch_in_editor:
                # Just load the map in the editor — no kiosk, no play mode
                self.show_toast(f"Loaded package: {os.path.basename(filePath)}")
            else:
                # Hide editor chrome and launch play mode
                self.enter_kiosk_mode()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load game package:\n{e}")
            import traceback
            traceback.print_exc()
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def play_package_from_path(self, file_path):
        """Load and launch a game package from the given file path.
        This is used by the asset browser when double‑clicking a .fiopak.
        """
        import zipfile
        import tempfile
        import shutil

        # Check for unsaved changes first
        if not self.check_unsaved_changes():
            return

        if not os.path.exists(file_path):
            self.show_toast(f"Package not found: {file_path}", is_error=True)
            return

        temp_dir = None
        try:
            if not zipfile.is_zipfile(file_path):
                self.show_toast("Selected file is not a valid game package.", is_error=True)
                return

            # Extract package to temp directory
            temp_dir = tempfile.mkdtemp(prefix="fio_package_")
            with zipfile.ZipFile(file_path, 'r') as zf:
                self._safe_extract_zip(file_path, temp_dir)

            # Find the map JSON inside the package
            map_path = self._find_map_in_package(temp_dir)
            # Force a "Save As" dialog the first time the user saves.
            self.file_path = None
            if not map_path:
                self.show_toast("No map file found in game package.", is_error=True)
                return

            # Configure ResourceManager for package assets
            try:
                from engine.resource_manager import ResourceManager
                rm = ResourceManager()
                if hasattr(rm, 'set_package_root'):
                    rm.set_package_root(temp_dir)
                elif hasattr(rm, 'load_package'):
                    rm.load_package(file_path)
            except Exception as e:
                print(f"[Package] ResourceManager setup warning: {e}")

            # Load level data
            with open(map_path, 'r') as f:
                level_data = json.load(f)

            # Clear current scene and load package map
            self.state.clear_scene()
            self._clear_terrain()
            self.state.load_from_data(level_data)

            # Re-initialize terrain if present in the package map
            if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                if self.terrain is None:
                    from engine.terrain import Terrain
                    self.terrain = Terrain()
                self.terrain.from_dict(self.state.terrain_data)

                if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                    self.view_3d.renderer.setup_terrain_shader(self.terrain)

                if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                    self.view_3d.logic_thread.set_terrain(self.terrain)
            else:
                self._clear_terrain()

            # Store temp dir for cleanup on application close
            self._package_temp_dir = temp_dir
            temp_dir = None  # Prevent cleanup in finally block

            # Update UI state
            self.file_path = file_path
            self.unsaved_changes = False
            self.update_title()
            self.set_selected_object(None)
            self.update_all_ui()

            # Check if user wants editor mode instead of kiosk
            launch_in_editor = self.config.getboolean('Kiosk', 'launch_in_editor', fallback=False)
            if launch_in_editor:
                # Just load the map in the editor — no kiosk, no play mode
                self.show_toast(f"Loaded package: {os.path.basename(file_path)}")
            else:
                # Hide editor chrome and launch play mode
                self.enter_kiosk_mode()

        except Exception as e:
            self.show_toast(f"Failed to load game package: {e}", is_error=True)
            import traceback
            traceback.print_exc()
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
    

    def _find_map_in_package(self, root_dir):
        """Find the first suitable .json map file in an extracted package."""
        best_match = None
        fallback = None
        for root, dirs, files in os.walk(root_dir):
            for f in files:
                if f.endswith('.json'):
                    filepath = os.path.join(root, f)
                    if fallback is None:
                        fallback = filepath
                    # Prefer files inside a 'maps' folder or with 'level' in the name
                    if 'maps' in root.lower() or 'level' in f.lower():
                        best_match = filepath
                        return best_match
        return best_match or fallback

    def enter_kiosk_mode(self):
        """Hide all editor UI and launch play mode fullscreen."""
        self.is_kiosk_mode = True

        # Save layout before hiding
        self.save_layout()

        # Hide menu bar and status bar
        if self.menuBar():
            self.menuBar().setVisible(False)
        self.statusBar().setVisible(False)

        # Hide all toolbars
        for toolbar in self.findChildren(QToolBar):
            toolbar.setVisible(False)

        # Hide all docks except the 3D view
        for dock in self.findChildren(QDockWidget):
            if dock is not self.view_3d_dock:
                dock.setVisible(False)

        # Ensure 3D view is visible
        self.view_3d_dock.setVisible(True)

        # Hide the floating play button
        if hasattr(self, 'play_button'):
            self.play_button.setVisible(False)

        # Hide sysmon overlay by default in kiosk mode (F3 to toggle back on)
        self.view_3d.sysmon.set_active(False)

        # Go fullscreen
        self.showFullScreen()

        # Launch play mode ONLY if not already in play mode
        if not self.view_3d.play_mode:
            self.enter_play_mode()

    def exit_kiosk_mode(self, keep_play_mode=False, confirm=True):
        """Restore editor UI and exit play mode.

        Args:
            keep_play_mode: If True, stay in play mode (F12 toggle).
                            If False, also exit play mode (ESC quit).
            confirm: If True, show a "Quit? Are you sure?" dialog before
                    exiting. Only applies when keep_play_mode=False (ESC flow).
        """
        # Show confirmation dialog when quitting via ESC
        if confirm and not keep_play_mode:
            reply = QMessageBox.question(
                self,
                "Quit Game",
                "Quit game and return to editor?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return  # User cancelled — stay in kiosk mode

        self.is_kiosk_mode = False

        # Exit play mode only if not keeping it (F12 toggle vs Escape)
        if not keep_play_mode and hasattr(self.view_3d, 'play_mode') and self.view_3d.play_mode:
            self._exit_play_mode()
        else:
            # --- Restore previous tab ---
            self._restore_properties_tab()

        # Exit fullscreen FIRST - critical for proper geometry restoration
        self.showNormal()

        # Restore the complete layout state (geometry, docks, toolbars)
        # This must happen BEFORE manual visibility fixes so restoreState()
        # has full control over dock positions and toolbar states
        self.load_layout()

        # restoreState()/restoreGeometry() handle docks and toolbars,
        # but menu bar and status bar visibility are NOT saved in the state
        if self.menuBar():
            self.menuBar().setVisible(True)
        self.statusBar().setVisible(True)

        # Play button is a floating widget, not part of QMainWindow state
        if hasattr(self, 'play_button'):
            self.play_button.setVisible(True)

        # Update play button and mode label — only reset to editor state if
        # we are actually leaving play mode (not an F12 fullscreen toggle).
        # Note: _exit_play_mode() already handles button/mode updates when keep_play_mode=False

        # If keeping play mode, recapture mouse for seamless FPS control
        if keep_play_mode and self.view_3d.play_mode:
            center_pos = self.view_3d.mapToGlobal(self.view_3d.rect().center())
            QCursor.setPos(center_pos)
            self.view_3d.last_mouse_pos = self.view_3d.mapFromGlobal(center_pos)
            QApplication.setOverrideCursor(Qt.BlankCursor)
            self.view_3d.setFocus()

    # =========================================================================
    # LOGIC GRAPH / WIZARD
    # =========================================================================

    def _refresh_logic_graph(self):
        """
        Rebuild the Logic Graph scene to match the current map.
        Called automatically after every map load and New Map.
        If the window is open it reloads immediately; if it is closed the
        stale window is discarded so the next open starts fresh.
        """
        win = getattr(self, '_logic_graph_win', None)
        if win is None:
            return
        if win.isVisible():
            win._reload()
        else:
            # Quietly discard the stale window — a new one will be built on
            # next open(), using the current editor_state automatically.
            win.close()
            self._logic_graph_win = None

    def open_logic_graph(self):
        """Open (or raise) the Logic Graph Editor window."""
        from editor.logic_graph_widget import LogicGraphWindow
        if not hasattr(self, '_logic_graph_win') or self._logic_graph_win is None:
            self._logic_graph_win = LogicGraphWindow(self.state, parent=self)
            self._logic_graph_win.applied.connect(self._on_logic_graph_applied)
        self._logic_graph_win.show()
        self._logic_graph_win.raise_()
        self._logic_graph_win.activateWindow()

    def _on_logic_graph_applied(self):
        """Called when the Logic Graph writes connections back to entities."""
        self.mark_as_modified()
        debug_log("IO", "Logic Graph applied connections to scene")

    def open_logic_wizard(self):
        """Open the Logic Wizard (guided I/O scenario setup)."""
        from editor.logic_graph_widget import LogicGraphWindow, LogicGraphScene
        from editor.logic_wizard import LogicWizard
        # Reuse the existing graph window's scene if it is already open,
        # so that wizard-added connections appear there immediately.
        if hasattr(self, '_logic_graph_win') and self._logic_graph_win is not None:
            scene  = self._logic_graph_win.get_scene()
            parent = self._logic_graph_win
        else:
            # Build a temporary scene — the wizard will still call apply_to_entities
            scene  = LogicGraphScene(self.state)
            parent = self
        wiz = LogicWizard(self.state, scene, parent=parent)
        if wiz.exec_():
            # If the graph window is not yet open, open it so the user can review
            # and press Apply to persist the connections.
            self.open_logic_graph()

    def validate_io_connections(self):
        """Check all entities for connections that point to missing targets (by name or ID)."""
        all_names = set()
        all_ids = set()
        for t in self.state.things:
            n = t.properties.get('name', '')
            if n:
                all_names.add(n)
            eid = getattr(t, 'id', None) or t.properties.get('id')
            if eid is not None:
                all_ids.add(eid)

        for b in self.state.brushes:
            n = b.get('name', '')
            if n:
                all_names.add(n)
            eid = b.get('id')
            if eid is not None:
                all_ids.add(eid)

        broken = []
        all_entities = list(self.state.things) + list(self.state.brushes)
        for entity in all_entities:
            if hasattr(entity, 'properties'):
                conns = entity.properties.get('_io_connections', [])
                src_name = entity.properties.get('name', '?')
            else:
                conns = entity.get('_io_connections', [])
                src_name = entity.get('name', '?')

            for c in conns:
                if isinstance(c, dict):
                    tgt_name = c.get('target', '')
                    tgt_id   = c.get('target_id')
                    out_pin  = c.get('output', '?')
                else:
                    tgt_name = getattr(c, 'target_name', '')
                    tgt_id   = getattr(c, 'target_id', None)
                    out_pin  = getattr(c, 'output_name', '?')

                if tgt_id is not None:
                    if tgt_id not in all_ids:
                        broken.append(f"  {src_name}.{out_pin}  →  (ID:{tgt_id})  NOT FOUND")
                elif tgt_name and tgt_name not in all_names:
                    broken.append(f"  {src_name}.{out_pin}  →  \"{tgt_name}\"  NOT FOUND")

        if broken:
            QMessageBox.warning(
                self, "Validate Connections",
                "Broken connections found — target entity does not exist:\n\n"
                + "\n".join(broken)
            )
        else:
            total = sum(
                len(e.properties.get('_io_connections', [])
                    if hasattr(e, 'properties')
                    else e.get('_io_connections', []))
                for e in all_entities
            )
            QMessageBox.information(
                self, "Validate Connections",
                f"All {total} connection(s) are valid. ✔"
            )

    def closeEvent(self, event):
        try:
            if not self.check_unsaved_changes():
                event.ignore()
                return

            # Stop timers
            if hasattr(self, 'tooltip_timer'):
                self.tooltip_timer.stop()
            if hasattr(self, 'autosave_timer'):
                self.autosave_timer.stop()
            if hasattr(self, '_play_button_sync_timer'):
                self._play_button_sync_timer.stop()

            # Cleanup extracted package temp dir
            if hasattr(self, '_package_temp_dir') and self._package_temp_dir:
                import shutil
                shutil.rmtree(self._package_temp_dir, ignore_errors=True)

            try:
                if not getattr(self, '_resetting_layout', False):
                    self.save_layout()
            except Exception as e:
                print(f"save_layout failed: {e}")

            if hasattr(self, 'view_3d') and self.view_3d and self.view_3d.logic_thread:
                self.view_3d.logic_thread.stop()
                self.view_3d.logic_thread.join(timeout=1.0)

            event.accept()
        except Exception as e:
            import traceback
            traceback.print_exc()
            event.accept()

    def open_grid_colours_dialog(self):
        dialog = GridColoursDialog(self.config, self)
        if dialog.exec_() == QDialog.Accepted:
            # Refresh all views that draw a grid
            self.view_3d.update()
            self.view_top.update()
            self.view_side.update()
            self.view_front.update()
            self.show_toast("Grid colours updated")

class GridColoursDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.parent_window = parent
        self.setWindowTitle("Grid Colours")
        self.setModal(True)
        self.setMinimumWidth(350)

        # Default colours
        self.default_colours = {
            "major": "#5a5a5a",
            "minor": "#404040",
            "background": "#2b2b2b"
        }

        layout = QVBoxLayout(self)

        # Helper to load colour from config with fallback
        def get_color(key, default_hex):
            hex_val = config.get("GridColours", key, fallback=default_hex)
            return QColor(hex_val)

        self.major_colour = get_color("major", self.default_colours["major"])
        major_row = QHBoxLayout()
        major_label = QLabel("Major colour:")
        major_label.setFixedWidth(200)
        major_row.addWidget(major_label)
        major_row.addStretch()
        self.major_btn = QPushButton()
        self.major_btn.setFixedSize(32, 32)
        self.major_btn.setStyleSheet(f"background-color: {self.major_colour.name()}; border: 1px solid #888;")
        self.major_btn.clicked.connect(lambda: self.pick_colour(self.major_btn, "major"))
        major_row.addWidget(self.major_btn)
        layout.addLayout(major_row)

        self.minor_colour = get_color("minor", self.default_colours["minor"])
        minor_row = QHBoxLayout()
        minor_label = QLabel("Minor colour:")
        minor_label.setFixedWidth(200)
        minor_row.addWidget(minor_label)
        minor_row.addStretch()
        self.minor_btn = QPushButton()
        self.minor_btn.setFixedSize(32, 32)
        self.minor_btn.setStyleSheet(f"background-color: {self.minor_colour.name()}; border: 1px solid #888;")
        self.minor_btn.clicked.connect(lambda: self.pick_colour(self.minor_btn, "minor"))
        minor_row.addWidget(self.minor_btn)
        layout.addLayout(minor_row)

        self.bg_colour = get_color("background", self.default_colours["background"])
        bg_row = QHBoxLayout()
        bg_label = QLabel("Background:")
        bg_label.setFixedWidth(200)
        bg_row.addWidget(bg_label)
        bg_row.addStretch()
        self.bg_btn = QPushButton()
        self.bg_btn.setFixedSize(32, 32)
        self.bg_btn.setStyleSheet(f"background-color: {self.bg_colour.name()}; border: 1px solid #888;")
        self.bg_btn.clicked.connect(lambda: self.pick_colour(self.bg_btn, "background"))
        bg_row.addWidget(self.bg_btn)
        layout.addLayout(bg_row)

        layout.addSpacing(12)

        button_row = QHBoxLayout()
        defaults_btn = QPushButton("Defaults")
        defaults_btn.clicked.connect(self.reset_to_defaults)
        button_row.addWidget(defaults_btn)
        button_row.addStretch()
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        button_row.addWidget(self.button_box)
        layout.addLayout(button_row)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

    def pick_colour(self, button, key):
        col = QColorDialog.getColor(button.palette().button().color(), self)
        if col.isValid():
            hex_val = col.name()
            if key == "major":
                self.major_colour = col
            elif key == "minor":
                self.minor_colour = col
            elif key == "background":
                self.bg_colour = col
            button.setStyleSheet(f"background-color: {hex_val}; border: 1px solid #888;")

    def reset_to_defaults(self):
        """Reset all colours to the default dark theme values."""
        self.major_colour = QColor(self.default_colours["major"])
        self.minor_colour = QColor(self.default_colours["minor"])
        self.bg_colour = QColor(self.default_colours["background"])

        self.major_btn.setStyleSheet(f"background-color: {self.default_colours['major']}; border: 1px solid #888;")
        self.minor_btn.setStyleSheet(f"background-color: {self.default_colours['minor']}; border: 1px solid #888;")
        self.bg_btn.setStyleSheet(f"background-color: {self.default_colours['background']}; border: 1px solid #888;")

    def accept(self):
        # Save to config
        if not self.config.has_section("GridColours"):
            self.config.add_section("GridColours")
        self.config.set("GridColours", "major", self.major_colour.name())
        self.config.set("GridColours", "minor", self.minor_colour.name())
        self.config.set("GridColours", "background", self.bg_colour.name())
        self.parent_window.save_config()
        super().accept()