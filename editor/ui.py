import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStatusBar, QToolBar,
    QLabel, QSpinBox, QCheckBox, QComboBox, QAction, QMessageBox, QFrame,
    QDockWidget, QTabWidget, QPushButton, QActionGroup, QDialog,
    QDialogButtonBox, QApplication, QSizePolicy, QInputDialog, QMenu
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont, QIcon, QKeySequence, QPixmap
from PyQt5.QtGui import QPalette, QColor

from editor.view_2d import View2D
from engine.qt_game_view import QtGameView
from editor.property_editor import PropertyEditor
from editor.scene_hierarchy import SceneHierarchy
from editor.asset_browser import AssetBrowser
from editor.SettingsWindow import SettingsWindow
from editor.debug_console import DebugConsole

import math

class PowerOfTwoSpinBox(QSpinBox):
    """SpinBox that only allows power-of-2 values (2, 4, 8, 16, 32 …)."""

    def stepBy(self, steps):
        val = self.value()
        if steps > 0:
            new_val = val * 2
        else:
            new_val = val // 2
        new_val = max(self.minimum(), min(self.maximum(), new_val))
        self.setValue(new_val)

    def textFromValue(self, value):
        return str(self._nearest_pow2(value))

    def valueFromText(self, text):
        try:
            return self._nearest_pow2(int(text))
        except ValueError:
            return self.value()

    @staticmethod
    def _nearest_pow2(n):
        if n <= 0:
            return 1
        return int(2 ** round(math.log2(n)))

class GenerateTilemapDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Tilemap")
        layout = QVBoxLayout(self)
        self.save_png_checkbox = QCheckBox("Save a PNG copy of the tilemap")
        layout.addWidget(self.save_png_checkbox)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def save_png_checked(self):
        return self.save_png_checkbox.isChecked()

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        
        # --- 1. Initialize Views and Editors ---
        MainWindow.view_3d = QtGameView(MainWindow)
        MainWindow.view_3d.show_triggers_as_solid = True 
        
        MainWindow.view_top = View2D(MainWindow, MainWindow, "top")
        MainWindow.view_side = View2D(MainWindow, MainWindow, "side")
        MainWindow.view_front = View2D(MainWindow, MainWindow, "front")
        MainWindow.property_editor = PropertyEditor(MainWindow)
        MainWindow.scene_hierarchy = SceneHierarchy(MainWindow)

        # --- 2. Docking Configuration ---
        MainWindow.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        MainWindow.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)
        MainWindow.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        MainWindow.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)

        # Scene Hierarchy Dock (Left)
        MainWindow.scene_hierarchy_dock = QDockWidget("Scene", MainWindow)
        MainWindow.scene_hierarchy_dock.setObjectName("SceneDock")
        MainWindow.scene_hierarchy_dock.setWidget(MainWindow.scene_hierarchy)
        MainWindow.addDockWidget(Qt.LeftDockWidgetArea, MainWindow.scene_hierarchy_dock)
        
        screen_width = QApplication.primaryScreen().geometry().width()
        MainWindow.scene_hierarchy_dock.setMaximumWidth(int(screen_width * 0.10))

        # 3D View Dock (Center/Right)
        MainWindow.view_3d_dock = QDockWidget("3D View", MainWindow)
        MainWindow.view_3d_dock.setObjectName("View3DDock")
        MainWindow.view_3d_dock.setWidget(MainWindow.view_3d)
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.view_3d_dock)

        # 2D Views Dock (Right, Tabbed)
        MainWindow.right_dock = QDockWidget("2D Views", MainWindow)
        MainWindow.right_dock.setObjectName("2DViewsDock")
        MainWindow.right_dock.setMinimumWidth(610)
        MainWindow.right_tabs = QTabWidget()
        MainWindow.right_tabs.addTab(MainWindow.view_top, "Top (XZ)")
        MainWindow.right_tabs.addTab(MainWindow.view_side, "Side (YZ)")
        MainWindow.right_tabs.addTab(MainWindow.view_front, "Front (XY)")
        MainWindow.right_dock.setWidget(MainWindow.right_tabs)
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.right_dock)
        
        # Properties Dock (Right, Bottom) — tabbed with Debug Console
        MainWindow.debug_console = DebugConsole.get_instance(MainWindow)

        MainWindow.properties_tab_widget = QTabWidget()
        MainWindow.properties_tab_widget.addTab(MainWindow.property_editor, "Properties")
        MainWindow.properties_tab_widget.addTab(MainWindow.debug_console, "Debug Console")
        MainWindow.properties_tab_widget.setStyleSheet("""
            QTabBar::tab:selected { background: #F08000; color: white; }
            QTabBar::tab { background: #2b2b2b; color: #ccc; height: 40px; min-width: 120px; padding: 0px 8px; border: 1px solid #222; }
            QTabBar::tab:hover { background: #5a7a82; }
        """)

        MainWindow.properties_dock = QDockWidget(" ", MainWindow)
        MainWindow.properties_dock.setObjectName("PropertiesDock")
        MainWindow.properties_dock.setWidget(MainWindow.properties_tab_widget)
        toggle_action = MainWindow.properties_dock.toggleViewAction()
        toggle_action.setText("Properties/Console")
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.properties_dock)

        # --- 3. Layout Adjustments ---
        MainWindow.splitDockWidget(MainWindow.view_3d_dock, MainWindow.right_dock, Qt.Horizontal)
        MainWindow.splitDockWidget(MainWindow.right_dock, MainWindow.properties_dock, Qt.Vertical)

        MainWindow.resizeDocks([MainWindow.view_3d_dock, MainWindow.right_dock], [800, 600], Qt.Horizontal)
        MainWindow.resizeDocks([MainWindow.right_dock, MainWindow.properties_dock], [600, 300], Qt.Vertical)

        # Tab Styling
        MainWindow.right_tabs.setStyleSheet("""
            QTabBar::tab:selected { background: #F08000; color: white; }
            QTabBar::tab { background: #2b2b2b; color: #ccc; height: 35px; min-width: 150px; padding: 0px; border: 1px solid #222; }
            QTabBar::tab:hover { background: #5a7a82; }
            QTabBar::scroller { width: 0px; }
        """)

        ## --- 4. Asset Browser Dock ---
        MainWindow.asset_browser_dock = QDockWidget("Asset Browser", MainWindow)
        MainWindow.asset_browser_dock.setObjectName("AssetBrowserDock") # Added object name for state saving
        texture_path = os.path.join(MainWindow.root_dir, "assets", "textures")
        
        MainWindow.asset_browser = AssetBrowser(texture_path, editor=MainWindow)
        MainWindow.asset_browser.main_window = MainWindow

        MainWindow.asset_browser_dock.setWidget(MainWindow.asset_browser)
        
        # CHANGED: Allow docking and set initial visibility
        MainWindow.asset_browser_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        MainWindow.asset_browser_dock.setFloating(False)
        MainWindow.asset_browser_dock.setVisible(True)

        # CHANGED: Dock logic to match screenshot (Under 3D View)
        # We add it to the Right area first (same as others) then split the 3D view vertically
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.asset_browser_dock)
        MainWindow.splitDockWidget(MainWindow.view_3d_dock, MainWindow.asset_browser_dock, Qt.Vertical)

        # REMOVED: The manual floating window resize/center logic
        
        # NEW: Set initial height ratio (3D View tall, Browser short)
        MainWindow.resizeDocks([MainWindow.view_3d_dock, MainWindow.asset_browser_dock], [10000, 1], Qt.Vertical)

        # --- 5. Actions Definition ---
        # DEFINED BEFORE create_toolbars so it can be used there
        self.action_asset_browser = QAction(MainWindow)
        self.action_asset_browser.setObjectName("action_asset_browser")
        self.action_asset_browser.setIcon(QIcon("assets/browser.png"))
        self.action_asset_browser.setText("Asset Browser")
        self.action_asset_browser.setToolTip("Toggle Asset Browser (T)")
        self.action_asset_browser.setShortcut("T")
        
        # --- 6. Menus and Toolbars ---
        self.create_menu_bar(MainWindow)
        self.create_toolbars(MainWindow)  # Creates Play button
        self.create_tool_toolbar(MainWindow)  # Single top strip: tools + Play last
        self.create_status_bar(MainWindow)

    def create_menu_bar(self, MainWindow):
        menubar = MainWindow.menuBar()
        menubar.setStyleSheet("""
            QMenuBar::item:selected {
                background-color: #F08000;
            }
            QMenu::item:selected {
                background-color: #F08000;
            }
        """)
        
        MainWindow.file_menu = menubar.addMenu('File')
        edit_menu = menubar.addMenu('Edit')
        view_menu = menubar.addMenu('View')
        MainWindow.tools_menu = menubar.addMenu('Tools')
        help_menu = menubar.addMenu('Help')

        MainWindow.file_menu.addAction(QAction('New Map', MainWindow, shortcut='Ctrl+N', triggered=MainWindow.new_map))
        MainWindow.file_menu.addAction(QAction('&Open...', MainWindow, shortcut='Ctrl+O', triggered=MainWindow.load_level))
        MainWindow.recent_menu = MainWindow.file_menu.addMenu('Recent')
        MainWindow.file_menu.addSeparator()
        
        MainWindow.file_menu.addAction(QAction('&Save', MainWindow, shortcut='Ctrl+S', triggered=MainWindow.save_level))
        MainWindow.file_menu.addAction(QAction('Save &As...', MainWindow, shortcut='Ctrl+Shift+S', triggered=MainWindow.save_level_as))
        MainWindow.file_menu.addSeparator()
        MainWindow.file_menu.addAction(QAction('Settings...', MainWindow, triggered=MainWindow.show_settings_dialog))
        MainWindow.file_menu.addSeparator()
        MainWindow.file_menu.addAction(QAction('Exit', MainWindow, shortcut='Ctrl+Q', triggered=MainWindow.close))

        MainWindow.undo_action = QAction(QIcon("assets/b_undo.png"), 'Undo', MainWindow)
        MainWindow.undo_action.setShortcut('Ctrl+Z')
        MainWindow.undo_action.setObjectName('undo_action')
        MainWindow.undo_action.setToolTip("Undo last action")
        MainWindow.undo_action.triggered.connect(MainWindow.undo)
        edit_menu.addAction(MainWindow.undo_action)
        
        MainWindow.redo_action = QAction(QIcon("assets/b_redo.png"), 'Redo', MainWindow)
        MainWindow.redo_action.setShortcut('Ctrl+Y')
        MainWindow.redo_action.setObjectName('redo_action')
        MainWindow.redo_action.setToolTip("Redo last action")
        MainWindow.redo_action.triggered.connect(MainWindow.redo)
        edit_menu.addAction(MainWindow.redo_action)

        edit_menu.addSeparator()
        edit_menu.addAction(QAction('Hide Brush', MainWindow, shortcut='H', triggered=MainWindow.hide_selected_brush))
        edit_menu.addAction(QAction('Unhide All Brushes', MainWindow, shortcut='Shift+H', triggered=MainWindow.unhide_all_brushes))

        edit_menu.addSeparator()
        grid_colours_action = QAction('Grid colours…', MainWindow)
        grid_colours_action.setToolTip("Customise grid line colours")
        grid_colours_action.triggered.connect(MainWindow.open_grid_colours_dialog)
        edit_menu.addAction(grid_colours_action)
        MainWindow.grid_colours_action = grid_colours_action

        view_menu.addActions([
            MainWindow.scene_hierarchy_dock.toggleViewAction(),
            MainWindow.view_3d_dock.toggleViewAction(), 
            MainWindow.right_dock.toggleViewAction(), 
            MainWindow.properties_dock.toggleViewAction()
        ])
        
        view_menu.addSeparator()
        
        view_menu.addAction(self.action_asset_browser)

        system_monitor_action = QAction('System Monitor', MainWindow, checkable=True)
        system_monitor_action.setShortcut('F3')
        system_monitor_action.triggered.connect(MainWindow.toggle_system_monitor)
        view_menu.addAction(system_monitor_action)
        
        view_menu.addSeparator()
        MainWindow.save_layout_action = QAction("Save Layout", MainWindow)
        MainWindow.save_layout_action.triggered.connect(MainWindow.save_layout)
        view_menu.addAction(MainWindow.save_layout_action)
        
        MainWindow.restore_layout_action = QAction("Restore Layout", MainWindow)
        MainWindow.restore_layout_action.triggered.connect(MainWindow.restore_layout)
        view_menu.addAction(MainWindow.restore_layout_action)
        
        MainWindow.reset_layout_action = QAction("Reset Layout", MainWindow)
        MainWindow.reset_layout_action.triggered.connect(MainWindow.reset_layout)
        view_menu.addAction(MainWindow.reset_layout_action)

        # --- Tools Menu Actions ---

        autocaulk_action = QAction("Autocaulk", MainWindow)
        autocaulk_action.setToolTip("Apply nodraw to all invisible brush faces")
        autocaulk_action.triggered.connect(MainWindow.autocaulk)
        MainWindow.tools_menu.addAction(autocaulk_action)

        MainWindow.logic_graph_action = QAction('Logic Graph Editor…', MainWindow)
        MainWindow.logic_graph_action.setShortcut('Ctrl+L')
        MainWindow.logic_graph_action.setToolTip('Open the visual I/O node graph editor')
        MainWindow.logic_graph_action.triggered.connect(MainWindow.open_logic_graph)

        MainWindow.logic_wizard_action = QAction('Logic Wizard…', MainWindow)
        MainWindow.logic_wizard_action.setShortcut('Ctrl+Shift+W')
        MainWindow.logic_wizard_action.setToolTip('Guided setup for common I/O scenarios')
        MainWindow.logic_wizard_action.triggered.connect(MainWindow.open_logic_wizard)

        MainWindow.validate_action = QAction('Validate All Connections…', MainWindow)
        MainWindow.validate_action.setToolTip('Check for connections with missing target entities')
        MainWindow.validate_action.triggered.connect(MainWindow.validate_io_connections)

        MainWindow.terrain_action = QAction('Terrain Generator…', MainWindow)
        MainWindow.terrain_action.setToolTip('Open the terrain editor (low‑poly terrain generator)')
        MainWindow.terrain_action.triggered.connect(MainWindow.open_terrain_editor)

        MainWindow.procedural_action = QAction('Procedural Map Generator…', MainWindow)
        MainWindow.procedural_action.triggered.connect(MainWindow.show_procedural_map_generator)
        
        MainWindow.tools_menu.addAction(MainWindow.logic_graph_action)
        MainWindow.tools_menu.addAction(MainWindow.logic_wizard_action)
        MainWindow.tools_menu.addAction(MainWindow.validate_action)
        MainWindow.tools_menu.addSeparator()
        MainWindow.tools_menu.addAction(MainWindow.terrain_action)
        MainWindow.tools_menu.addAction(MainWindow.procedural_action)
        MainWindow.tools_menu.addSeparator()

        view_menu.addSeparator()
        toggle_triggers_action = QAction('Opaque Triggers', MainWindow, checkable=True)
        toggle_triggers_action.setChecked(MainWindow.view_3d.show_triggers_as_solid)
        toggle_triggers_action.triggered.connect(MainWindow.toggle_trigger_display)
        view_menu.addAction(toggle_triggers_action)

        modern_action = QAction('Modern (Shaders)', MainWindow, checkable=True, checked=True)
        immediate_action = QAction('Immediate (Legacy)', MainWindow, checkable=True)
        modern_action.triggered.connect(lambda: MainWindow.set_render_mode("Modern (Shaders)"))
        immediate_action.triggered.connect(lambda: MainWindow.set_render_mode("Immediate (Legacy)"))

        help_menu.addAction(QAction('About', MainWindow, triggered=MainWindow.show_about))

    def create_toolbars(self, MainWindow):
        big_toolbar_buttons = MainWindow.config.getboolean('Display', 'big_toolbar_buttons', fallback=False)
        icon_size_val = 50 if big_toolbar_buttons else 35

        MainWindow.play_button = QPushButton(QIcon("assets/b_test.png"), "Play", MainWindow)
        MainWindow.play_button.setIconSize(QSize(icon_size_val, icon_size_val))
        # Remove setFixedSize here — we drive width via stylesheet instead
        MainWindow.play_button.setToolTip("Drop in and play (F5)")
        MainWindow.play_button.setShortcut("f5")
        MainWindow.play_button.clicked.connect(MainWindow.enter_play_mode)

        current_font = MainWindow.play_button.font()
        current_font.setPointSizeF(current_font.pointSizeF() * 1.5)
        MainWindow.play_button.setFont(current_font)
        MainWindow.play_button.setStyleSheet("""
            QPushButton {
                background-color: #22b14c;
                color: white;
                font-weight: bold;
                border: 1px solid #1a8f3d;
                border-radius: 4px;
                padding: 5px 15px;
                min-width: 250px;
                max-width: 250px;
            }
            QPushButton:hover {
                background-color: #28d157;
            }
            QPushButton:pressed {
                background-color: #1a8f3d;
            }
        """)

    def create_tool_toolbar(self, MainWindow):
        """Single tool strip along the top: all editing tools + Play last."""
        tool_toolbar = QToolBar("Tools")
        tool_toolbar.setObjectName("ToolToolbar")
        tool_toolbar.setMovable(True)
        tool_toolbar.setAllowedAreas(Qt.TopToolBarArea | Qt.BottomToolBarArea)
        MainWindow.addToolBar(Qt.TopToolBarArea, tool_toolbar)

        big = MainWindow.config.getboolean('Display', 'big_toolbar_buttons', fallback=False)
        icon_size_val = 50 if big else 35

        def make_btn(icon, tip, on_click=None, checkable=False, checked=False,
                     shortcut=None, styled=False, bottom_color=None):
            b = QPushButton()
            b.setIcon(QIcon(icon))
            b.setIconSize(QSize(icon_size_val, icon_size_val))
            
            # Width: icon + 2px left/right borders + 2px padding
            # Height: icon + 1px top border + 3px bottom border + 4px bottom padding
            b.setFixedSize(icon_size_val + 4, icon_size_val + 8)
            b.setToolTip(tip)
            
            if checkable:
                b.setCheckable(True)
                b.setChecked(checked)
            if shortcut:
                b.setShortcut(shortcut)
                
            if styled or bottom_color:
                border_bottom = f"border-bottom: 3px solid {bottom_color};" if bottom_color else "border-bottom: 1px solid #333;"
                
                b.setStyleSheet(f"""
                    QPushButton {{
                        background-color: #111111; 
                        border: 1px solid #333;
                        {border_bottom}
                        padding: 0px;
                        padding-bottom: 4px;
                    }}
                    QPushButton:hover {{
                        background-color: #3a3a3a;
                    }}
                    QPushButton:checked {{
                        background-color: #2b2b2b;
                        border: 1px solid #F08000;
                        {border_bottom}
                    }}
                    QPushButton:checked:hover {{
                        background-color: #4a4a4a;
                    }}
                """)
                
            if on_click is not None:
                if checkable:
                    b.toggled.connect(on_click)
                else:
                    b.clicked.connect(on_click)
            tool_toolbar.addWidget(b)
            return b

        # --- Base tools: Select + Box (Orange Strip) ---
        group_1_color = "#F08000" 
        MainWindow.select_tool_btn = make_btn(
            "assets/select.png",
            "Select tool (Shift+S)\n"
            "Drag a box to marquee-select; click empty space to deselect",
            on_click=lambda: MainWindow.set_tool_mode('select'),
            checkable=True, checked=MainWindow.tool_mode == 'select',
            shortcut="Shift+S", bottom_color=group_1_color)

        MainWindow.brush_tool_btn = make_btn(
            "assets/box.png",
            "Brush tool (Shift+B)\n"
            "Drag in a 2D view to create geometry",
            on_click=lambda: MainWindow.set_tool_mode('brush'),
            checkable=True, checked=MainWindow.tool_mode == 'brush',
            shortcut="Shift+B", bottom_color=group_1_color)

        tool_toolbar.addSeparator()

        # --- Editing actions (Green Strip) ---
        group_2_color = "#22b14c" 
        make_btn("assets/room.png", "Room (Hollow + Lights)",
                 on_click=MainWindow.create_room_from_brush, bottom_color=group_2_color)
        make_btn("assets/hollow.png", "Hollow",
                 on_click=MainWindow.hollow_selected_brush, bottom_color=group_2_color)
        make_btn("assets/clone.png", "Clone",
                 on_click=MainWindow.clone_selected_object, bottom_color=group_2_color)

        MainWindow.rotate_btn = make_btn(
            "assets/rotate.png",
            "Rotate 15°",
            on_click=MainWindow.rotate_selected_15,
            bottom_color=group_2_color)

        make_btn("assets/subtract.png", "Subtract",
                 on_click=MainWindow.perform_subtraction, bottom_color=group_2_color)

        MainWindow.scissor_btn = make_btn(
            "assets/scissor.png",
            "Scissor",
            on_click=MainWindow.toggle_clip_mode,
            checkable=True, shortcut="X", bottom_color=group_2_color)

        # --- Procedural / View actions (Blue Strip) ---
        group_3_color = "#00A2E8"
        make_btn("assets/tint.png", "Tint brush",
                 on_click=MainWindow.tint_selected_brush, bottom_color=group_2_color)

        tool_toolbar.addSeparator()

        terrain_menu = QMenu(MainWindow)
        terrain_menu.addAction(MainWindow.terrain_action)
        terrain_menu.addAction(MainWindow.procedural_action)
        terrain_btn = make_btn("assets/terrain.png", "Procedural Tools", bottom_color=group_3_color)
        terrain_btn.clicked.connect(lambda: terrain_menu.popup(
            terrain_btn.mapToGlobal(terrain_btn.rect().bottomLeft())))

        MainWindow.grid_btn = make_btn(
            "assets/b_grid.png", "Toggle 3D Grid (G)",
            on_click=MainWindow.toggle_grid,
            checkable=True, checked=True, bottom_color=group_3_color)

        tool_toolbar.addSeparator()
        tool_toolbar.addWidget(MainWindow.play_button)

    def create_status_bar(self, MainWindow):
        status_bar = QStatusBar()
        MainWindow.setStatusBar(status_bar)
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(10, 2, 10, 2)
        
        MainWindow.grid_size_spinbox = PowerOfTwoSpinBox()
        MainWindow.grid_size_spinbox.setRange(2, 128)
        MainWindow.grid_size_spinbox.setValue(16)
        MainWindow.grid_size_spinbox.valueChanged.connect(MainWindow.set_grid_size)
        
        MainWindow.world_size_spinbox = PowerOfTwoSpinBox()
        MainWindow.world_size_spinbox.setRange(512, 16384)
        MainWindow.world_size_spinbox.setValue(1024)
        MainWindow.world_size_spinbox.valueChanged.connect(MainWindow.set_world_size)
        
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("Grid Size:"))
        bottom_layout.addWidget(MainWindow.grid_size_spinbox)
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("World Size:"))
        bottom_layout.addWidget(MainWindow.world_size_spinbox)
        
        # 2. Mid-aligned Controls
        MainWindow.display_mode_combobox = QComboBox()
        MainWindow.display_mode_combobox.addItems(["Wireframe", "Solid Lit", "Textured"])
        MainWindow.display_mode_combobox.setCurrentText("Solid Lit")
        MainWindow.display_mode_combobox.currentTextChanged.connect(MainWindow.set_brush_display_mode)
        
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("Cull Dist:"))
        MainWindow.cull_dist_spinbox = QSpinBox()
        MainWindow.cull_dist_spinbox.setRange(500, 20000)
        MainWindow.cull_dist_spinbox.setValue(4096)
        MainWindow.cull_dist_spinbox.setSingleStep(250)
        MainWindow.cull_dist_spinbox.setToolTip("Objects beyond this distance will not be rendered")
        MainWindow.cull_dist_spinbox.valueChanged.connect(MainWindow.set_cull_distance)
        bottom_layout.addWidget(MainWindow.cull_dist_spinbox)

        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("Display:"))
        bottom_layout.addWidget(MainWindow.display_mode_combobox)

        # Camera mode (native): First Person vs Overhead (top-down).
        MainWindow.camera_mode_combobox = QComboBox()
        MainWindow.camera_mode_combobox.addItems(["First Person", "Overhead"])
        MainWindow.camera_mode_combobox.setCurrentText("First Person")
        MainWindow.camera_mode_combobox.setToolTip(
            "Play-mode camera. 'Overhead' is a top-down view (GTA 1 / Alien Swarm style).")
        MainWindow.camera_mode_combobox.currentTextChanged.connect(MainWindow.set_camera_mode)

        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("Camera:"))
        bottom_layout.addWidget(MainWindow.camera_mode_combobox)
 
        # --- EXPANDING NOTIFICATION AREA (FAR RIGHT) ---
        # Add a small buffer spacing before the label
        bottom_layout.addSpacing(20)
 
        self.notification_label = QLabel("")
        self.notification_label.setAlignment(Qt.AlignCenter)
        
        # CRITICAL: This allows the label to expand and fill all available space
        self.notification_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        # Set a very large maximum width so it isn't capped
        self.notification_label.setMaximumWidth(16777215) 
        
        self.notification_label.setStyleSheet("""
            QLabel {
                background-color: transparent; /* Becomes yellow/red via main_window logic */
                color: black;
                font-weight: bold;
                border-radius: 3px;
                padding: 2px 10px;
                font-size: 14px;
            }
        """)
        bottom_layout.addWidget(self.notification_label)
        
        status_bar.addPermanentWidget(bottom_widget, 1)


