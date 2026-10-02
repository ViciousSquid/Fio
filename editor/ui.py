import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStatusBar, QToolBar,
    QLabel, QSpinBox, QCheckBox, QComboBox, QAction, QMessageBox, QFrame,
    QDockWidget, QTabWidget, QPushButton, QActionGroup, QDialog,
    QDialogButtonBox, QApplication, QSizePolicy, QInputDialog
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
        
        # Properties Dock (Right, Bottom)
        MainWindow.properties_dock = QDockWidget("Properties", MainWindow)
        MainWindow.properties_dock.setObjectName("PropertiesDock")
        MainWindow.properties_dock.setWidget(MainWindow.property_editor)
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
        self.create_toolbars(MainWindow) # Now includes the browser button
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
        
        file_menu = menubar.addMenu('File')
        edit_menu = menubar.addMenu('Edit')
        view_menu = menubar.addMenu('View')
        help_menu = menubar.addMenu('Help')

        file_menu.addAction(QAction('New Map', MainWindow, shortcut='Ctrl+N', triggered=MainWindow.new_map))
        file_menu.addAction(QAction('&Open...', MainWindow, shortcut='Ctrl+O', triggered=MainWindow.load_level))
        
        MainWindow.recent_menu = file_menu.addMenu('Recent')
        file_menu.addSeparator()
        
        file_menu.addAction(QAction('&Save', MainWindow, shortcut='Ctrl+S', triggered=MainWindow.save_level))
        file_menu.addAction(QAction('Save &As...', MainWindow, shortcut='Ctrl+Shift+S', triggered=MainWindow.save_level_as))
        file_menu.addSeparator()
        file_menu.addAction(QAction('Settings...', MainWindow, triggered=MainWindow.show_settings_dialog))
        file_menu.addSeparator()
        file_menu.addAction(QAction('Exit', MainWindow, shortcut='Ctrl+Q', triggered=MainWindow.close))

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

        view_menu.addActions([
            MainWindow.scene_hierarchy_dock.toggleViewAction(),
            MainWindow.view_3d_dock.toggleViewAction(), 
            MainWindow.right_dock.toggleViewAction(), 
            MainWindow.properties_dock.toggleViewAction()
        ])
        
        view_menu.addSeparator()
        
        # Use our new action for the menu as well
        view_menu.addAction(self.action_asset_browser)
        
        view_menu.addSeparator()
        MainWindow.save_layout_action = QAction("Save Layout", MainWindow)
        MainWindow.save_layout_action.triggered.connect(MainWindow.save_layout)
        view_menu.addAction(MainWindow.save_layout_action)
        
        MainWindow.reset_layout_action = QAction("Reset Layout", MainWindow)
        MainWindow.reset_layout_action.triggered.connect(MainWindow.reset_layout)
        view_menu.addAction(MainWindow.reset_layout_action)
        
        view_menu.addSeparator()
        toggle_triggers_action = QAction('Opaque Triggers', MainWindow, checkable=True)
        toggle_triggers_action.setChecked(MainWindow.view_3d.show_triggers_as_solid)
        toggle_triggers_action.triggered.connect(MainWindow.toggle_trigger_display)
        view_menu.addAction(toggle_triggers_action)

        system_monitor_action = QAction('System Monitor', MainWindow, checkable=True)
        system_monitor_action.setShortcut('F3')
        system_monitor_action.triggered.connect(MainWindow.toggle_system_monitor)
        view_menu.addAction(system_monitor_action)

        # Debug Console action
        debug_console_action = QAction('Debug Console', MainWindow, checkable=True)
        debug_console_action.setShortcut('`')  # Tilde/backtick
        debug_console_action.setToolTip("Toggle I/O debug console (~)")
        debug_console_action.triggered.connect(MainWindow.toggle_debug_console)
        view_menu.addAction(debug_console_action)
        MainWindow.debug_console_action = debug_console_action  # Store reference

        modern_action = QAction('Modern (Shaders)', MainWindow, checkable=True, checked=True)
        immediate_action = QAction('Immediate (Legacy)', MainWindow, checkable=True)
        modern_action.triggered.connect(lambda: MainWindow.set_render_mode("Modern (Shaders)"))
        immediate_action.triggered.connect(lambda: MainWindow.set_render_mode("Immediate (Legacy)"))

        help_menu.addAction(QAction('About', MainWindow, triggered=MainWindow.show_about))

    def create_toolbars(self, MainWindow):
        top_toolbar = QToolBar("Main Tools")
        top_toolbar.setObjectName("MainToolbar")
        MainWindow.addToolBar(top_toolbar)

        # Determine icon size based on config setting
        big_toolbar_buttons = MainWindow.config.getboolean('Display', 'big_toolbar_buttons', fallback=False)
        icon_size_val = 50 if big_toolbar_buttons else 35
        
        room_btn = QPushButton()
        room_btn.setIcon(QIcon("assets/room.png"))
        room_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        room_btn.setFixedSize(icon_size_val, icon_size_val)
        room_btn.setToolTip("Create Room (Hollow + Lights)")
        room_btn.clicked.connect(MainWindow.create_room_from_brush)
        
        hollow_btn = QPushButton()
        hollow_btn.setIcon(QIcon("assets/hollow.png"))
        hollow_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        hollow_btn.setFixedSize(icon_size_val, icon_size_val)
        hollow_btn.setToolTip("Hollow out brush")
        hollow_btn.clicked.connect(MainWindow.hollow_selected_brush)

        clone_btn = QPushButton()
        clone_btn.setIcon(QIcon("assets/clone.png"))
        clone_btn.setIconSize(QSize(50, 50))
        clone_btn.setFixedSize(50, 50)
        clone_btn.setToolTip("Clone selected brush (Space)")
        clone_btn.clicked.connect(MainWindow.clone_selected_object)
        
        rotate_btn = QPushButton()
        rotate_btn.setIcon(QIcon("assets/rotate.png"))
        rotate_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        rotate_btn.setFixedSize(icon_size_val, icon_size_val)
        rotate_btn.setToolTip("Rotate 90 degrees")
        rotate_btn.clicked.connect(MainWindow.rotate_selected_brush)
        
        subtract_btn = QPushButton()
        subtract_btn.setIcon(QIcon("assets/subtract.png"))
        subtract_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        subtract_btn.setFixedSize(icon_size_val, icon_size_val)
        subtract_btn.setToolTip("Subtract")
        subtract_btn.clicked.connect(MainWindow.perform_subtraction)

        tint_btn = QPushButton()
        tint_btn.setIcon(QIcon("assets/tint.png"))
        tint_btn.setIconSize(QSize(50, 50))
        tint_btn.setFixedSize(50, 50)
        tint_btn.setToolTip("Tint selected brush colour")
        tint_btn.clicked.connect(MainWindow.tint_selected_brush)

        top_toolbar.addWidget(room_btn)
        top_toolbar.addWidget(hollow_btn)
        top_toolbar.addWidget(clone_btn) 
        top_toolbar.addWidget(rotate_btn)
        top_toolbar.addWidget(subtract_btn)
        top_toolbar.addWidget(tint_btn)
        
        # Add separator 
        separator_terrain = QFrame()
        separator_terrain.setFrameShape(QFrame.VLine)
        separator_terrain.setFrameShadow(QFrame.Sunken)
        separator_terrain.setFixedWidth(2)
        separator_terrain.setStyleSheet("background-color: transparent;")
        top_toolbar.addWidget(separator_terrain)
        
        # === TERRAIN BUTTON ===
        terrain_btn = QPushButton()
        terrain_btn.setIcon(QIcon("assets/terrain.png"))
        terrain_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        terrain_btn.setFixedSize(icon_size_val, icon_size_val)
        terrain_btn.setToolTip("Terrain Editor")
        terrain_btn.clicked.connect(MainWindow.open_terrain_editor)
        top_toolbar.addWidget(terrain_btn)
        MainWindow.terrain_btn = terrain_btn

        # === ASSET BROWSER BUTTON (NEW) ===
        browser_btn = QPushButton()
        browser_btn.setIcon(QIcon("assets/browser.png"))
        browser_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        browser_btn.setFixedSize(icon_size_val, icon_size_val)
        browser_btn.setToolTip("Asset Browser (T)")
        # Trigger the action we created in setupUi
        browser_btn.clicked.connect(self.action_asset_browser.trigger)
        top_toolbar.addWidget(browser_btn)
        MainWindow.browser_btn = browser_btn

         # === GRID TOGGLE ===
        grid_btn = QPushButton()
        grid_btn.setIcon(QIcon("assets/b_grid.png"))
        grid_btn.setIconSize(QSize(icon_size_val, icon_size_val))
        grid_btn.setFixedSize(icon_size_val, icon_size_val)
        grid_btn.setToolTip("Toggle 3D Grid (G)")
        grid_btn.setCheckable(True)
        grid_btn.setChecked(True)  # Grid visible by default
        grid_btn.setStyleSheet("""
            QPushButton {
                background-color: #555;
                border: 1px solid #666;
            }
            QPushButton:checked {
                background-color: #F08000;
                border: 1px solid #FF9020;
            }
            QPushButton:hover {
                background-color: #6a6a6a;
            }
            QPushButton:checked:hover {
                background-color: #FF9020;
            }
        """)
        grid_btn.toggled.connect(MainWindow.toggle_grid)
        top_toolbar.addWidget(grid_btn)
        MainWindow.grid_btn = grid_btn  # Store reference
        
        # === FLOATING PLAY BUTTON ===
        MainWindow.play_button = QPushButton(QIcon("assets/b_test.png"), "Play", MainWindow)
        MainWindow.play_button.setIconSize(QSize(icon_size_val, icon_size_val))
        MainWindow.play_button.setFixedSize(icon_size_val + 190, icon_size_val)
        MainWindow.play_button.setToolTip("Drop in and play (F5)")
        MainWindow.play_button.setShortcut("f5")
        MainWindow.play_button.clicked.connect(MainWindow.enter_play_mode)
        
        # Style the play button with green background and larger font
        current_font = MainWindow.play_button.font()
        current_font.setPointSize(current_font.pointSize() + 1)
        MainWindow.play_button.setFont(current_font)
        MainWindow.play_button.setStyleSheet("""
            QPushButton {
                background-color: #22b14c;
                color: white;
                font-weight: bold;
                border: 1px solid #1a8f3d;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #28d157;
            }
            QPushButton:pressed {
                background-color: #1a8f3d;
            }
        """)

        # Display dropdown logic placeholder
        display_mode_widget = QWidget()
        display_mode_layout = QHBoxLayout(display_mode_widget)
        display_mode_layout.setContentsMargins(5,0,5,0)
        
        right_margin = QWidget()
        right_margin.setFixedWidth(5)
        top_toolbar.addWidget(right_margin)
        

    def create_status_bar(self, MainWindow):
        status_bar = QStatusBar()
        MainWindow.setStatusBar(status_bar)
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(10, 2, 10, 2)
        
        MainWindow.grid_size_spinbox = QSpinBox()
        MainWindow.grid_size_spinbox.setRange(4, 128)
        MainWindow.grid_size_spinbox.setValue(16)
        MainWindow.grid_size_spinbox.setSingleStep(1)
        MainWindow.grid_size_spinbox.valueChanged.connect(MainWindow.set_grid_size)
        
        MainWindow.world_size_spinbox = QSpinBox()
        MainWindow.world_size_spinbox.setRange(512, 16384)
        MainWindow.world_size_spinbox.setValue(1024)
        MainWindow.world_size_spinbox.setSingleStep(1)
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