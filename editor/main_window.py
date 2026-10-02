import sys
import json
import os
import subprocess
import random
import numpy as np
import configparser
import math
import copy
import time

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QFileDialog, QWidget, QLabel, QVBoxLayout,
    QGraphicsOpacityEffect, QInputDialog, QColorDialog, QProgressDialog, QAction
)
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtCore import Qt, QByteArray, QTimer, QPropertyAnimation, QEasingCurve, QRect, QPoint
from PyQt5.QtGui import QKeySequence, QPixmap, QCursor, QColor

from editor.things import Light, PlayerStart, Thing, Pickup, Monster, Model
from editor.rand_map_gen_dial import RandomMapGeneratorDialog
from editor.rand_map_gen import generate
from editor.SettingsWindow import SettingsWindow
from editor.ui import Ui_MainWindow, GenerateTilemapDialog
from engine.constants import TILE_SIZE, WALL_TILE, FLOOR_TILE
from editor.view_2d import View2D
from editor.editor_state import EditorState
from editor.terrain_editor import TerrainEditorWindow
from engine.terrain import Terrain
from editor.debug_console import DebugConsole


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
                font-size: 22px;
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
    def __init__(self, root_dir):
        super().__init__()
        self.root_dir = root_dir
        

        self.unsaved_changes = False
        self.file_path = None
        self.recent_files = []


        self.setWindowTitle("RStudio")
        self.setGeometry(100, 100, 1600, 900)
        self.setMinimumSize(1280, 800)

        self.config = configparser.ConfigParser()
        self.config_path = 'settings.ini'
        self.load_config()
        self.state = EditorState()
        self.load_recent_files()
        
        # Initialize selected_objects list for multi-selection support
        if not hasattr(self.state, 'selected_objects'):
            self.state.selected_objects = []
        if not hasattr(self.state, 'selected_object'):
            self.state.selected_object = None
            
        self.keys_pressed = set()
        self.grid_visible = True
        self.preview_timer = QTimer()
        self.preview_timer.timeout.connect(self.update_mover_preview)
        self.preview_data = {} 
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.update_recent_files_menu()
        self.update_title()
        
        QTimer.singleShot(0, self.reposition_overlays)
        self.ctrl_tab_shortcut = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.ctrl_tab_shortcut.activated.connect(self.cycle_2d_view)
        self.setFocus()
        self.update_global_font()
        self.load_layout()
        
        self.terrain = None
        self.terrain_editor_window = None

        self.debug_console = DebugConsole(self)
        
        # Only show debug console if setting is enabled (defaults to False)
        if self.config.getboolean('Display', 'always_show_io_debug', fallback=False):
            self.debug_console.show()
        else:
            self.debug_console.hide()
            
        self.ui.action_asset_browser.triggered.connect(self.toggle_asset_browser)

        # Enable sysmon at launch if configured
        if self.config.getboolean('Display', 'always_show_sysmon', fallback=False):
            self.view_3d.debug_mode_active = True
            self.view_3d.sysmon_expanded = True

        self.show_logic_links = False
        
        # Tooltips
        self.camera_movement_learned = self.config.getboolean('Tooltips', 'camera_movement_learned', fallback=False)
        self.startup_tooltip_shown = False
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
            "T: Toggle Asset Browser",
        ]
        self.last_tooltip_time = 0
        self.tooltip_interval = 120  # Seconds between occasional tooltips
        
        # Timer for occasional tooltips
        self.tooltip_timer = QTimer(self)
        self.tooltip_timer.timeout.connect(self._check_occasional_tooltip)
        self.tooltip_timer.start(30000)  # Check every 30 seconds
        
        # Track right-click state for camera movement detection
        self.right_mouse_held = False
        self.view_3d.installEventFilter(self)
        
        # Show startup tooltip after window is shown
        QTimer.singleShot(1500, self._show_startup_tooltip)
        
        # Autosave Timer
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.setup_autosave()

    def update_title(self):
        """Updates window title with filename and dirty status."""
        fname = os.path.basename(self.file_path) if self.file_path else "Untitled"
        dirty_marker = "*" if self.unsaved_changes else ""
        self.setWindowTitle(f"RStudio - {fname} {dirty_marker}")

    def mark_as_modified(self):
        """Mark the project as having unsaved changes."""
        if not self.unsaved_changes:
            self.unsaved_changes = True
            self.update_title()

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


    def moveEvent(self, event):
        """Handle window move."""
        super().moveEvent(event)

    def toggle_debug_console(self):
        """Toggle the debug console visibility."""
        self.debug_console.toggle()
        # Sync menu checkbox state
        if hasattr(self, 'debug_console_action'):
            self.debug_console_action.setChecked(self.debug_console.isVisible())


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

    def resizeEvent(self, event):
        """Reposition floating UI elements on window resize."""
        super().resizeEvent(event)
        if hasattr(self, 'play_button'):
            bx = self.width() // 2 - self.play_button.width() // 2
            by = 35 
            self.play_button.move(bx, by)
            self.play_button.raise_()

    def reposition_overlays(self):
        """Positions the Play button at the top middle (where the toast used to be)."""
        if hasattr(self, 'play_button'):
            # Centered horizontally, 35 pixels from the top
            px = self.width() // 2 - self.play_button.width() // 2
            py = 35 
            self.play_button.move(px, py)
            self.play_button.raise_()

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
        
        # Create or show editor window
        if self.terrain_editor_window is None:
            from editor.terrain_editor import TerrainEditorWindow
            self.terrain_editor_window = TerrainEditorWindow(self.terrain, self)
            self.terrain_editor_window.terrain_changed.connect(self.on_terrain_changed)
        
        self.terrain_editor_window.show()
        self.terrain_editor_window.raise_()
        self.terrain_editor_window.activateWindow()
    
    def on_terrain_changed(self):
        """Handle terrain changes."""
        if self.terrain:
            if hasattr(self.state, 'terrain_data'):
                self.state.terrain_data = self.terrain.to_dict()
        self.update_views()

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
        self.view_3d.update()
        self.view_top.reset_state()
        self.view_front.reset_state()
        self.view_side.reset_state()

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
                        background-color: #C62828;  /* Red for play mode */
                        color: white;
                        border: 1px solid #B71C1C;
                        border-radius: 3px;
                        padding: 5px 15px;
                        font-weight: bold;
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
                        background-color: #2E7D32;  /* Green for editor mode */
                        color: white;
                        border: 1px solid #1B5E20;
                        border-radius: 3px;
                        padding: 5px 15px;
                        font-weight: bold;
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
        
        # Support both movers and doors
        is_mover = brush.get('is_mover', False)
        is_door = brush.get('is_door', False)
        
        if not is_mover and not is_door:
            return

        self.preview_data = {
            'obj': brush,
            'original_pos': list(brush['pos']), # Deep copy coordinate
            'direction': np.array(brush.get('direction', [0, 1, 0]), dtype=float),
            'distance': brush.get('distance', 128.0),
            'speed': brush.get('speed', 64.0),
            'time': 0.0,
            'is_door': is_door
        }
        
        # Normalize direction
        norm = np.linalg.norm(self.preview_data['direction'])
        if norm > 0:
            self.preview_data['direction'] /= norm

        self.preview_timer.start(16) # ~60 FPS

    def stop_mover_preview(self):
        if self.preview_timer.isActive():
            self.preview_timer.stop()
            if self.preview_data and self.preview_data.get('obj'):
                self.preview_data['obj']['pos'] = self.preview_data['original_pos']
            self.preview_data = {}
            self.update_views()
            
            # Check for Mover button
            m_btn = self.property_editor._widgets.get('mover_preview_btn')
            if m_btn:
                m_btn.blockSignals(True)
                m_btn.setChecked(False)
                m_btn.setText("▶ Preview Movement")
                m_btn.blockSignals(False)
                
            # Check for Door button
            d_btn = self.property_editor._widgets.get('door_preview_btn')
            if d_btn:
                d_btn.blockSignals(True)
                d_btn.setChecked(False)
                d_btn.setText("▶ Preview Door")
                d_btn.blockSignals(False)

    def update_mover_preview(self):
        if not self.preview_data:
            return

        dt = 0.016 # 16ms
        self.preview_data['time'] += dt
        
        # Calculate sine wave movement (0 -> 1 -> 0)
        # Using speed to determine frequency
        speed = self.preview_data['speed']
        distance = self.preview_data['distance']
        
        # Simple Ping-Pong logic
        # d = speed * time
        # We want to oscillate between 0 and distance
        
        if distance == 0: return

        # Cycle duration = (Distance / Speed) * 2
        cycle_duration = (distance / speed) * 2 if speed > 0 else 1.0
        
        # Triangle wave or Sine wave? Mover code in game engines varies.
        # Let's use a Sine wave for smooth preview: 0 to 1
        # sin(t) goes -1 to 1. We want 0 to 1.
        # (sin(t) + 1) / 2
        
        progress = (math.sin(self.preview_data['time'] * (speed / distance) * math.pi - (math.pi/2)) + 1) / 2
        
        current_offset = progress * distance
        
        movement_vector = self.preview_data['direction'] * current_offset
        original_pos = np.array(self.preview_data['original_pos'])
        
        new_pos = original_pos + movement_vector
        
        self.preview_data['obj']['pos'] = new_pos.tolist()
        self.update_views()

    def load_config(self):
        self.config.read(self.config_path)

    def save_config(self):
        with open(self.config_path, 'w') as configfile:
            self.config.write(configfile)

    def update_global_font(self):
        font_size = self.config.getint('Display', 'font_size', fallback=10)
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
        
        # Sync button state if triggered via ESC or other means
        if hasattr(self, 'asset_browser') and hasattr(self.asset_browser, 'face_btn'):
             self.asset_browser.face_btn.blockSignals(True)
             self.asset_browser.face_btn.setChecked(active)
             self.asset_browser.face_btn.blockSignals(False)
        
        if active:
            self.show_toast("FACE MODE: Select a face to texture (Purple)", duration=3000)
            self.set_selected_object(None) # Deselect current object to clear gizmos and allow clean hover
            
            # Change cursor to indicate mode
            self.view_3d.setCursor(Qt.CrossCursor)
        else:
            self.show_toast("FACE MODE: OFF")
            self.view_3d.hovered_face_info = None # Clear highlight
            self.view_3d.setCursor(Qt.ArrowCursor)
            
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

        brush['textures'][face_name] = texture_name
        self.update_views()
        self.show_toast(f"Applied to {face_name}")

    def apply_texture_to_brush(self, texture_name=None, tiled=False):
        if not isinstance(self.state.selected_object, dict):
            QMessageBox.warning(self, "No Brush Selected", "Select a brush to apply the texture to.")
            return

        # Fallback if called without argument (e.g. from shortcut)
        if texture_name is None:
            texture_path = self.asset_browser.get_selected_filepath()
            if not texture_path:
                QMessageBox.warning(self, "No Texture Selected", "Select a texture from the Asset Browser.")
                return
            texture_name = os.path.basename(texture_path)

        self.save_state()
        if 'textures' not in self.state.selected_object:
            self.state.selected_object['textures'] = {}
        for face in ['south', 'north', 'west', 'east', 'down', 'top']:
            self.state.selected_object['textures'][face] = texture_name
            
        # Update tiling property
        self.state.selected_object['texture_tiling'] = tiled
        
        self.update_views()
        
        mode_str = "Tiled" if tiled else "Stretched"
        self.show_toast(f"Applied {texture_name} ({mode_str})")

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
        
        self.ui.notification_label.setText("PRESS ESC TO EXIT PLAY MODE")


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
        reset_layout_shortcut = self.config.get('Controls', 'reset_layout', fallback='Ctrl+Shift+R')
        if hasattr(self, 'reset_layout_action'):
            self.reset_layout_action.setShortcut(QKeySequence(reset_layout_shortcut))
        save_layout_shortcut = self.config.get('Controls', 'save_layout', fallback='Ctrl+Shift+S')
        if hasattr(self, 'save_layout_action'):
            self.save_layout_action.setShortcut(QKeySequence(save_layout_shortcut))

    def toggle_backface_culling(self, state):
        """Toggle OpenGL backface culling."""
        self.view_3d.set_backface_culling(state == Qt.Checked)

    def toggle_frustum_culling(self, state):
        """Toggle CPU frustum culling."""
        self.view_3d.set_frustum_culling(state == Qt.Checked)
    
    def toggle_system_monitor(self):
        """Toggles the debug system monitor overlay in the 3D view."""
        self.view_3d.debug_mode_active = not self.view_3d.debug_mode_active
        
        # If in play mode, we need to handle cursor visibility when toggling the menu
        if self.view_3d.play_mode:
            if self.view_3d.debug_mode_active:
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
        if snapped_size != size:
            self.grid_size_spinbox.blockSignals(True)
            self.grid_size_spinbox.setValue(snapped_size)
            self.grid_size_spinbox.blockSignals(False)
            return
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
            return
        for view in [self.view_top, self.view_side, self.view_front, self.view_3d]:
            view.world_size = snapped_size
        self.view_3d.update_grid()
        self.update_views()

    def set_brush_display_mode(self, text):
        self.view_3d.brush_display_mode = text
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
        msg_box.setWindowTitle("About RStudio")
        
        container_widget = QWidget()
        layout = QVBoxLayout(container_widget)

        splash_label = QLabel()
        pixmap = QPixmap('assets/splash.png')
        splash_label.setPixmap(pixmap.scaled(512, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(splash_label)

        version_label = QLabel(f"{version}<br>https://github.com/ViciousSquid/RStudio")
        version_label.setTextFormat(Qt.RichText)
        version_label.setAlignment(Qt.AlignCenter)
        version_label.setOpenExternalLinks(True)
        layout.addWidget(version_label)
        
        msg_box.layout().addWidget(container_widget, 0, 0, 1, msg_box.layout().columnCount())
        
        msg_box.setStandardButtons(QMessageBox.Ok)

        msg_box.exec_()

    def new_map(self):
        # Check for unsaved changes
        if not self.check_unsaved_changes():
            return
            
        self.state.clear_scene()
        self.file_path = None
        self.unsaved_changes = False # Reset dirty flag
        self.update_title()
        self.update_all_ui()

    def show_random_map_dialog(self):
        dialog = RandomMapGeneratorDialog(self)
        if dialog.exec_():
            params = dialog.get_parameters()
            map_grid = generate(method=params['style'], width=params['width'], height=params['length'], seed=params.get('seed'))
            brushes, things = self.convert_grid_to_level(map_grid)
            self.state.brushes = brushes
            self.state.things = things
            self.view_3d.camera.pos = [0, 150, 400]
            self.set_selected_object(None)
            self.save_state()
            self.update_all_ui()

    def convert_grid_to_level(self, grid, cell_size=128, wall_height=128):
        if not isinstance(grid, np.ndarray):
            print("Error: Invalid grid.")
            return [], []

        brushes, things, floor_locations = [], [], []
        grid_height, grid_width = grid.shape

        total_width = grid_width * cell_size
        total_height = grid_height * cell_size
        brushes.append({
            'pos': [total_width / 2 - cell_size/2, wall_height / 2, total_height / 2 - cell_size/2],
            'size': [total_width, wall_height, total_height],
            'operation': 'subtract',
            'textures': {f: 'assets/textures/default.png' for f in ['north','south','east','west','top','down']}
        })

        for r in range(grid_height):
            for c in range(grid_width):
                pos_x, pos_y, pos_z = c * cell_size, wall_height / 2, r * cell_size
                if grid[r, c] == 0:
                    brushes.append({
                        'pos': [pos_x, pos_y, pos_z],
                        'size': [cell_size, wall_height, cell_size],
                        'operation': 'add',
                        'textures': {f: 'assets/textures/default.png' for f in ['north','south','east','west','top','down']}
                    })
                else:
                    floor_locations.append((pos_x, pos_y, pos_z))

        if floor_locations:
            player_pos = floor_locations[0]
            things.append(PlayerStart(pos=[player_pos[0], 40, player_pos[2]]))
            for _ in range(max(1, len(floor_locations) // 25)):
                light_pos = random.choice(floor_locations)
                things.append(Light(pos=[light_pos[0], wall_height - 40, light_pos[2]]))
        else:
            print("Warning: No floor space generated.")

        return brushes, things

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
            
            if not (brush_min[0] < sub_max[0] and brush_max[0] > sub_min[0] and
                    brush_min[1] < sub_max[1] and brush_max[1] > sub_min[1] and
                    brush_min[2] < sub_max[2] and brush_max[2] > sub_min[2]):
                new_brushes.append(brush)
                continue
                
            fragments = []
            
            if brush_min[0] < sub_min[0]:
                left_max = min(brush_max[0], sub_min[0])
                if left_max - brush_min[0] > 0.01:
                    fragments.append({
                        'pos': [(brush_min[0] + left_max)/2, pos[1], pos[2]],
                        'size': [left_max - brush_min[0], size[1], size[2]],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            if brush_max[0] > sub_max[0]:
                right_min = max(brush_min[0], sub_max[0])
                if brush_max[0] - right_min > 0.01:
                    fragments.append({
                        'pos': [(right_min + brush_max[0])/2, pos[1], pos[2]],
                        'size': [brush_max[0] - right_min, size[1], size[2]],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            if brush_min[1] < sub_min[1]:
                bottom_max = min(brush_max[1], sub_min[1])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                if bottom_max - brush_min[1] > 0.01 and x_max - x_min > 0.01:
                    fragments.append({
                        'pos': [pos[0], (brush_min[1] + bottom_max)/2, pos[2]],
                        'size': [x_max - x_min, bottom_max - brush_min[1], size[2]],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            if brush_max[1] > sub_max[1]:
                top_min = max(brush_min[1], sub_max[1])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                if brush_max[1] - top_min > 0.01 and x_max - x_min > 0.01:
                    fragments.append({
                        'pos': [pos[0], (top_min + brush_max[1])/2, pos[2]],
                        'size': [x_max - x_min, brush_max[1] - top_min, size[2]],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            if brush_min[2] < sub_min[2]:
                front_max = min(brush_max[2], sub_min[2])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                y_min = max(brush_min[1], sub_min[1])
                y_max = min(brush_max[1], sub_max[1])
                if front_max - brush_min[2] > 0.01 and x_max - x_min > 0.01 and y_max - y_min > 0.01:
                    fragments.append({
                        'pos': [pos[0], pos[1], (brush_min[2] + front_max)/2],
                        'size': [x_max - x_min, y_max - y_min, front_max - brush_min[2]],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            if brush_max[2] > sub_max[2]:
                back_min = max(brush_min[2], sub_max[2])
                x_min = max(brush_min[0], sub_min[0])
                x_max = min(brush_max[0], sub_max[0])
                y_min = max(brush_min[1], sub_min[1])
                y_max = min(brush_max[1], sub_max[1])
                if brush_max[2] - back_min > 0.01 and x_max - x_min > 0.01 and y_max - y_min > 0.01:
                    fragments.append({
                        'pos': [pos[0], pos[1], (back_min + brush_max[2])/2],
                        'size': [x_max - x_min, y_max - y_min, brush_max[2] - back_min],
                        'operation': 'add',
                        'textures': brush['textures'].copy()
                    })
            
            new_brushes.extend(fragments)
        
        new_brushes.append(subtract_brush)
        self.state.brushes = new_brushes
        
        self.update_all_ui()

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
        if self.view_3d.play_mode:
            if event.key() == Qt.Key_Escape:
                self.view_3d.toggle_play_mode(None, None)
                
                # FIX: Clear notification label directly
                self.ui.notification_label.setText("")
                
                # Re-show startup tooltip if not yet learned
                if not self.camera_movement_learned:
                    QTimer.singleShot(500, lambda: self.show_tooltip(
                        "Hold right mouse to move camera with WASD", duration=0, toast_id="camera_tip"))
                
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
                # Update play button color when exiting play mode
                self.update_play_button_color()
                
            elif event.key() == Qt.Key_F3:
                self.view_3d.show_sprites_in_play_mode = not self.view_3d.show_sprites_in_play_mode
                self.view_3d.update()
            elif event.key() == Qt.Key_F1:
                # Toggle connection lines visibility in play mode (no toast in play mode)
                self.view_3d.show_connections_in_play_mode = not getattr(self.view_3d, 'show_connections_in_play_mode', False)
                self.update_all_ui()
            elif event.key() == Qt.Key_E:
                # Use key - single shot, send to game state
                if hasattr(self.view_3d, 'game_state') and self.view_3d.game_state:
                    self.view_3d.game_state.set_use_key_pressed()
                self.keys_pressed.add(event.key())
            elif event.key() == Qt.Key_QuoteLeft:  # Tilde/backtick key (~)
                self.toggle_debug_console()
            else:
                self.keys_pressed.add(event.key())
            return

        # Tilde key toggles debug console (works in both modes)
        if event.key() == Qt.Key_QuoteLeft:
            self.toggle_debug_console()
            return

        # NEW: Handle ESC to exit Face Mode
        if event.key() == Qt.Key_Escape:
            if getattr(self.view_3d, 'face_mode_active', False):
                self.toggle_face_mode(False)
                return
            if self.state.selected_object:
                self.set_selected_object(None)
                return

        # Editor mode key presses below
        if self.state.selected_object:
            if event.key() == Qt.Key_Delete:
                self.save_state()
                # Handle multi-selection delete
                for obj in list(self.state.selected_objects):
                    if isinstance(obj, dict):
                        if obj in self.state.brushes:
                            self.state.brushes.remove(obj)
                    else:
                        if obj in self.state.things:
                            self.state.things.remove(obj)
                self.set_selected_objects([])
                return

            if event.key() == Qt.Key_H:
                if event.modifiers() == Qt.ShiftModifier:
                    self.unhide_all_brushes()
                elif isinstance(self.state.selected_object, dict):
                    self.hide_selected_brush()
                return

            if event.key() == Qt.Key_Space:
                self.clone_selected_object()
                return

        # G key toggles grid visibility (editor mode only)
        if event.key() == Qt.Key_G:
            new_state = not self.grid_visible
            self.toggle_grid(new_state)
            # Update the toolbar button state
            if hasattr(self, 'grid_btn'):
                self.grid_btn.blockSignals(True)
                self.grid_btn.setChecked(new_state)
                self.grid_btn.blockSignals(False)
            return

        # Check for camera movement lesson: WASD while right-click held (editor mode only)
        if not self.camera_movement_learned and self.right_mouse_held:
            if event.key() in (Qt.Key_W, Qt.Key_A, Qt.Key_S, Qt.Key_D):
                self.on_camera_moved_with_wasd()

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

    def save_level_as(self):
        # Ensure 'filePath' is defined here
        filePath, _ = QFileDialog.getSaveFileName(self, "Save Level As", "maps", "JSON Files (*.json)")
        
        if filePath:
            self.file_path = filePath
            self.save_level()

    def save_level(self):
        if not self.file_path:
            self.save_level_as()
            return
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
        """Loads the level data from the given path. Used by 'Open' and 'Recent Files'."""
        try:
            with open(filePath, 'r') as f:
                level_data = json.load(f)
            
            # This triggers the fingerprint validation in EditorState
            self.state.load_from_data(level_data)

            # Restore Terrain ---
            if hasattr(self.state, 'terrain_data') and self.state.terrain_data:
                # Initialize terrain system if it doesn't exist yet
                if self.terrain is None:
                    from engine.terrain import Terrain
                    self.terrain = Terrain()
                    
                    # Link to renderer
                    if hasattr(self.view_3d, 'renderer') and self.view_3d.renderer:
                        self.view_3d.renderer.setup_terrain_shader(self.terrain)
                    
                    # Link to logic thread (for collision)
                    if hasattr(self.view_3d, 'logic_thread') and self.view_3d.logic_thread:
                        self.view_3d.logic_thread.set_terrain(self.terrain)

                # Apply the loaded data to the terrain engine
                self.terrain.from_dict(self.state.terrain_data)
                self.terrain.mark_all_dirty()
                
                # Update the terrain editor window if it is currently open
                if self.terrain_editor_window and self.terrain_editor_window.isVisible():
                    self.terrain_editor_window.load_from_terrain()
            
            elif self.terrain:
                # If the loaded map has NO terrain, disable the existing terrain engine
                self.terrain.enabled = False
                self.terrain.mark_all_dirty()

            # Initialize camera view based on PlayerStart
            player_start_pos = None
            for t in self.state.things:
                if isinstance(t, PlayerStart):
                    player_start_pos = t.pos
                    break

            if player_start_pos:
                self.view_3d.camera.pos = [player_start_pos[0], player_start_pos[1] + 50, player_start_pos[2] + 200]
                self.view_3d.camera.pitch = -15
                self.view_3d.camera.yaw = -90

            # --- Update Application State ---
            self.file_path = filePath
            self.set_selected_object(None)
            
            # Update Recent Files List and Title Bar
            self.add_recent_file(filePath) 
            self.unsaved_changes = False
            self.update_title()
            
            filename = os.path.basename(filePath)
            self.show_toast(f"Loaded {filename}")

        except ValueError as ve:
            self.show_toast(f"Rejected: {ve}", is_error=True)
        except Exception as e:
            self.show_toast(f"Error: {e}", is_error=True)

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

    def load_layout(self):
        if self.config.has_section('Layout') and self.config.has_option('Layout', 'geometry'):
            self.restoreGeometry(QByteArray.fromHex(self.config['Layout']['geometry'].encode()))
        if self.config.has_section('Layout') and self.config.has_option('Layout', 'state'):
            self.restoreState(QByteArray.fromHex(self.config['Layout']['state'].encode()))

    def reset_layout(self):
        self.scene_hierarchy_dock.setFloating(False)
        self.view_3d_dock.setFloating(False)
        self.right_dock.setFloating(False)
        self.properties_dock.setFloating(False)
        self.asset_browser_dock.setFloating(False) # CHANGE: Dock it
        
        self.addDockWidget(Qt.LeftDockWidgetArea, self.scene_hierarchy_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.view_3d_dock)
        
        # CHANGE: Add Asset Browser to bottom
        self.addDockWidget(Qt.BottomDockWidgetArea, self.asset_browser_dock)
        self.asset_browser_dock.setVisible(True)

        self.splitDockWidget(self.view_3d_dock, self.right_dock, Qt.Horizontal)
        self.splitDockWidget(self.right_dock, self.properties_dock, Qt.Vertical)
        
        # Resize logic
        self.resizeDocks([self.view_3d_dock, self.right_dock], [800, 600], Qt.Horizontal)
        self.resizeDocks([self.right_dock, self.properties_dock], [600, 300], Qt.Vertical)
        
        # Optional: Set initial height for bottom dock
        self.resizeDocks([self.view_3d_dock, self.asset_browser_dock], [600, 250], Qt.Vertical)

        self.statusBar().showMessage("Layout reset to default.", 2000)

    def closeEvent(self, event):
        # NEW: Check for unsaved changes
        if not self.check_unsaved_changes():
            event.ignore()
            return
            
        # Stop timers
        if hasattr(self, 'tooltip_timer'):
            self.tooltip_timer.stop()
        if hasattr(self, 'autosave_timer'):
            self.autosave_timer.stop()
        
        self.save_layout()

        if hasattr(self, 'debug_console'):
            self.debug_console.close()
        
        if hasattr(self, 'view_3d') and self.view_3d.logic_thread:
            self.view_3d.logic_thread.stop()
            self.view_3d.logic_thread.join(timeout=1.0)
        
        super().closeEvent(event)