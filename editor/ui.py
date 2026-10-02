import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStatusBar, QToolBar,
    QLabel, QSpinBox, QCheckBox, QComboBox, QAction, QMessageBox, QFrame,
    QDockWidget, QTabWidget, QPushButton, QActionGroup, QDialog,
    QDialogButtonBox, QApplication, QSizePolicy
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont, QIcon, QKeySequence, QPixmap

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
        
        # --- Create UI Components FIRST ---
        MainWindow.view_3d = QtGameView(MainWindow)
        MainWindow.view_3d.show_triggers_as_solid = True 
        
        MainWindow.view_top = View2D(MainWindow, MainWindow, "top")
        MainWindow.view_side = View2D(MainWindow, MainWindow, "side")
        MainWindow.view_front = View2D(MainWindow, MainWindow, "front")
        MainWindow.property_editor = PropertyEditor(MainWindow)
        MainWindow.scene_hierarchy = SceneHierarchy(MainWindow)

        # --- Create and Arrange Docks ---
        MainWindow.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        MainWindow.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)

        MainWindow.scene_hierarchy_dock = QDockWidget("Scene", MainWindow)
        MainWindow.scene_hierarchy_dock.setObjectName("SceneDock")
        MainWindow.scene_hierarchy_dock.setWidget(MainWindow.scene_hierarchy)
        MainWindow.addDockWidget(Qt.LeftDockWidgetArea, MainWindow.scene_hierarchy_dock)
        
        screen_width = QApplication.primaryScreen().geometry().width()
        MainWindow.scene_hierarchy_dock.setMaximumWidth(int(screen_width * 0.10))

        MainWindow.view_3d_dock = QDockWidget("3D View", MainWindow)
        MainWindow.view_3d_dock.setObjectName("View3DDock")
        MainWindow.view_3d_dock.setWidget(MainWindow.view_3d)
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.view_3d_dock)

        MainWindow.right_dock = QDockWidget("2D Views", MainWindow)
        MainWindow.right_dock.setObjectName("2DViewsDock")
        MainWindow.right_tabs = QTabWidget()
        MainWindow.right_tabs.addTab(MainWindow.view_top, "Top (XZ)")
        MainWindow.right_tabs.addTab(MainWindow.view_side, "Side (YZ)")
        MainWindow.right_tabs.addTab(MainWindow.view_front, "Front (XY)")
        MainWindow.right_dock.setWidget(MainWindow.right_tabs)
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.right_dock)
        
        MainWindow.properties_dock = QDockWidget("Properties", MainWindow)
        MainWindow.properties_dock.setObjectName("PropertiesDock")
        MainWindow.properties_dock.setWidget(MainWindow.property_editor)
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.properties_dock)

        MainWindow.splitDockWidget(MainWindow.view_3d_dock, MainWindow.right_dock, Qt.Horizontal)
        MainWindow.splitDockWidget(MainWindow.right_dock, MainWindow.properties_dock, Qt.Vertical)

        MainWindow.resizeDocks([MainWindow.view_3d_dock, MainWindow.right_dock], [800, 600], Qt.Horizontal)
        MainWindow.resizeDocks([MainWindow.right_dock, MainWindow.properties_dock], [600, 300], Qt.Vertical)

        MainWindow.right_tabs.setStyleSheet("""
            QTabBar::tab:selected { background: #425F5D; color: white; }
            QTabBar::tab { background: #444; color: #ccc; padding: 5px; border: 1px solid #222; }
        """)

        corner_widget = QWidget()
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        
        rotate_button = QPushButton("Rotate")
        rotate_button.setToolTip("Rotate the selected brush by 90 degrees")
        rotate_button.clicked.connect(MainWindow.rotate_selected_brush)
        corner_layout.addWidget(rotate_button)
        
        subtract_button = QPushButton("Subtract")
        subtract_button.setToolTip("Carve intersecting brushes")
        subtract_button.setStyleSheet("background-color: orange; padding: 2px 8px; color: black;")
        subtract_button.clicked.connect(MainWindow.perform_subtraction)
        corner_layout.addWidget(subtract_button)
        MainWindow.right_tabs.setCornerWidget(corner_widget, Qt.TopRightCorner)
        
        # Asset Browser Setup
        MainWindow.asset_browser_dock = QDockWidget("Asset Browser", MainWindow)

        texture_path = os.path.join(MainWindow.root_dir, "assets", "textures")
        
        MainWindow.asset_browser = AssetBrowser(texture_path, editor=MainWindow)
        MainWindow.asset_browser.main_window = MainWindow

        MainWindow.asset_browser_dock.setWidget(MainWindow.asset_browser)
        MainWindow.asset_browser_dock.setAllowedAreas(Qt.NoDockWidgetArea)
        MainWindow.asset_browser_dock.setFloating(True)
        MainWindow.asset_browser_dock.setVisible(False)
        
        initial_width = 1280
        initial_height = 600
        MainWindow.asset_browser_dock.resize(initial_width, initial_height)
        
        main_window_center = MainWindow.geometry().center()
        MainWindow.asset_browser_dock.move(
            main_window_center.x() - initial_width // 2,
            main_window_center.y() - initial_height // 2
        )
        
        self.create_menu_bar(MainWindow)
        self.create_toolbars(MainWindow)
        self.create_status_bar(MainWindow)

    def create_menu_bar(self, MainWindow):
        menubar = MainWindow.menuBar()
        menubar.setStyleSheet("""
            QMenuBar::item:selected {
                background-color: #425F5D;
            }
            QMenu::item:selected {
                background-color: #425F5D;
            }
        """)
        
        file_menu = menubar.addMenu('File')
        edit_menu = menubar.addMenu('Edit')
        view_menu = menubar.addMenu('View')
        #tools_menu = menubar.addMenu('Tools')
        help_menu = menubar.addMenu('Help')

        file_menu.addAction(QAction('New Map', MainWindow, shortcut='Ctrl+N', triggered=MainWindow.new_map))
        file_menu.addAction(QAction('&Open...', MainWindow, shortcut='Ctrl+O', triggered=MainWindow.load_level))
        file_menu.addAction(QAction('&Save', MainWindow, shortcut='Ctrl+S', triggered=MainWindow.save_level))
        file_menu.addAction(QAction('Save &As...', MainWindow, shortcut='Ctrl+Shift+S', triggered=MainWindow.save_level_as))
        file_menu.addSeparator()
        file_menu.addAction(QAction('Settings...', MainWindow, triggered=MainWindow.show_settings_dialog))
        file_menu.addSeparator()
        file_menu.addAction(QAction('Exit', MainWindow, shortcut='Ctrl+Q', triggered=MainWindow.close))

        # Create Undo/Redo actions once and store them on MainWindow
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
        
        asset_browser_action = MainWindow.asset_browser_dock.toggleViewAction()
        asset_browser_action.setText("Toggle Asset Browser")
        asset_browser_action.setShortcut("T")
        view_menu.addAction(asset_browser_action)
        
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

        # New System Monitor Toggle
        system_monitor_action = QAction('System Monitor', MainWindow, checkable=True)
        system_monitor_action.setShortcut('F3')
        system_monitor_action.triggered.connect(MainWindow.toggle_system_monitor)
        view_menu.addAction(system_monitor_action)

        #tools_menu.addAction(QAction('Generate Collision Tilemap...', MainWindow, triggered=MainWindow.show_generate_tilemap_dialog))

        modern_action = QAction('Modern (Shaders)', MainWindow, checkable=True, checked=True)
        immediate_action = QAction('Immediate (Legacy)', MainWindow, checkable=True)
        modern_action.triggered.connect(lambda: MainWindow.set_render_mode("Modern (Shaders)"))
        immediate_action.triggered.connect(lambda: MainWindow.set_render_mode("Immediate (Legacy)"))

        help_menu.addAction(QAction('About', MainWindow, triggered=MainWindow.show_about))

    def create_toolbars(self, MainWindow):
        top_toolbar = QToolBar("Main Tools")
        top_toolbar.setObjectName("MainToolbar")
        MainWindow.addToolBar(top_toolbar)
        display_mode_widget = QWidget()
        display_mode_layout = QHBoxLayout(display_mode_widget)
        display_mode_layout.setContentsMargins(5,0,5,0)
        MainWindow.display_mode_combobox = QComboBox()
        MainWindow.display_mode_combobox.addItems(["Wireframe", "Solid Lit", "Textured"])
        MainWindow.display_mode_combobox.setCurrentText("Textured")
        MainWindow.display_mode_combobox.currentTextChanged.connect(MainWindow.set_brush_display_mode)
        display_mode_layout.addWidget(QLabel("Display:"))
        display_mode_layout.addWidget(MainWindow.display_mode_combobox)
        top_toolbar.addWidget(display_mode_widget)
        top_toolbar.addSeparator()
        MainWindow.apply_texture_action = QAction(QIcon("assets/b_applytex.png"),"",MainWindow,toolTip="Apply selected texture to brush",triggered=MainWindow.apply_texture_to_brush)
        apply_caulk_action = QAction(QIcon("assets/b_caulk.png"),"",MainWindow,toolTip="Apply caulk texture to brush",triggered=MainWindow.apply_caulk_to_brush)
        top_toolbar.addAction(MainWindow.apply_texture_action)
        top_toolbar.addAction(apply_caulk_action)
        top_toolbar.addSeparator()
        play_button = QPushButton(QIcon("assets/b_test.png"),"Play")
        play_button.setToolTip("Drop in and play the level (F5)")
        play_button.setShortcut("f5")
        play_button.clicked.connect(MainWindow.enter_play_mode)
        top_toolbar.addWidget(play_button)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top_toolbar.addWidget(spacer)

        # Mode Label
        MainWindow.mode_label = QLabel("EDITOR MODE")
        MainWindow.mode_label.setObjectName("modeLabel")
        
        # Default (Editor) Style
        # EDIT HERE TO CHANGE DEFAULT APPEARANCE
        MainWindow.mode_label.setStyleSheet("""
            QLabel {
                background-color: #333333;   /* Background Color */
                color: #888888;              /* Text Color */
                padding: 5px 10px;           /* Spacing around text */
                border-radius: 4px;          /* Rounded corners */
                font-weight: bold;           /* Bold text */
                font-size: 11px;             /* Font Size */
                border: 1px solid #444;      /* Optional Border */
            }
        """)
        top_toolbar.addWidget(MainWindow.mode_label)
        
        right_margin = QWidget()
        right_margin.setFixedWidth(5)
        top_toolbar.addWidget(right_margin)
        

    def create_status_bar(self, MainWindow):
        status_bar = QStatusBar()
        MainWindow.setStatusBar(status_bar)
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(10, 2, 10, 2)
        
        MainWindow.snap_checkbox = QCheckBox("Snap to Grid")
        MainWindow.snap_checkbox.setChecked(True)
        MainWindow.snap_checkbox.stateChanged.connect(MainWindow.toggle_snap_to_grid)
        
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
        
        MainWindow.culling_checkbox = QCheckBox("Enable Culling")
        MainWindow.culling_checkbox.setChecked(False)
        MainWindow.culling_checkbox.stateChanged.connect(MainWindow.toggle_culling)
        
        bottom_layout.addWidget(MainWindow.snap_checkbox)
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("Grid Size:"))
        bottom_layout.addWidget(MainWindow.grid_size_spinbox)
        bottom_layout.addSpacing(20)
        bottom_layout.addWidget(QLabel("World Size:"))
        bottom_layout.addWidget(MainWindow.world_size_spinbox)
        
        bottom_layout.addStretch(1)
        
        bottom_layout.addWidget(MainWindow.culling_checkbox)
        
        status_bar.addPermanentWidget(bottom_widget, 1)