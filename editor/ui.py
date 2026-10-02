import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStatusBar, QToolBar,
    QLabel, QSpinBox, QComboBox, QAction, QMessageBox, QFrame,
    QDockWidget, QTabWidget, QPushButton, QActionGroup,
    QApplication, QSizePolicy, QInputDialog, QMenu,
    QStyle, QStyleOptionButton
)
from PyQt5.QtCore import Qt, QSize, QByteArray, QRect
from PyQt5.QtGui import QIcon, QKeySequence, QPainter

from editor.view_2d import View2D
from engine.qt_game_view import QtGameView
from engine.view_distance import (
    DEFAULT_VIEW_DISTANCE, MAX_VIEW_DISTANCE, MIN_VIEW_DISTANCE)
from editor.property_editor import PropertyEditor
from editor.scene_hierarchy import SceneHierarchy
from editor.asset_browser import AssetBrowser
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

class RotatablePlayButton(QPushButton):
    """Play button that rotates its complete presentation for a vertical toolbar."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vertical = False

    def set_vertical(self, vertical):
        vertical = bool(vertical)
        if self._vertical != vertical:
            self._vertical = vertical
            self.setProperty("_vertical", vertical)
            self.update()

    def paintEvent(self, event):
        if not self._vertical:
            super().paintEvent(event)
            return

        # Draw the normal QPushButton in a transposed coordinate system.
        # This rotates both icon and label instead of forcing a 250px-wide
        # horizontal button into a narrow right-hand toolbar.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.translate(self.width(), 0)
        painter.rotate(90)

        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = QRect(0, 0, self.height(), self.width())
        self.style().drawControl(QStyle.CE_PushButton, option, painter, self)
        painter.end()


#: Bumped whenever the default dock arrangement changes.  A layout saved by
#: an older version is dropped once, so a new default actually reaches an
#: install that has been opened before -- settings.ini stores the layout on
#: every close, and restoreState() would otherwise win forever.
LAYOUT_VERSION = 3


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
        #
        # Keep a useful windowTitle for toggleViewAction() in View, but replace
        # the visible dock title bar with a zero-height widget.  The hierarchy
        # itself should start at the top instead of wasting a row on "Scene".
        MainWindow.scene_hierarchy_dock = QDockWidget("Scene Hierarchy", MainWindow)
        MainWindow.scene_hierarchy_dock.setObjectName("SceneDock")
        MainWindow.scene_hierarchy_dock.setWidget(MainWindow.scene_hierarchy)
        scene_title_bar = QWidget()
        scene_title_bar.setFixedHeight(0)
        scene_title_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        MainWindow.scene_hierarchy_dock.setTitleBarWidget(scene_title_bar)
        MainWindow.scene_hierarchy_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        MainWindow.addDockWidget(Qt.LeftDockWidgetArea, MainWindow.scene_hierarchy_dock)
        
        screen_width = QApplication.primaryScreen().geometry().width()
        MainWindow.scene_hierarchy_dock.setMaximumWidth(int(screen_width * 0.10))

        # 3D View Dock (Center/Right)
        MainWindow.view_3d_dock = QDockWidget("3D View", MainWindow)
        MainWindow.view_3d_dock.setObjectName("View3DDock")
        MainWindow.view_3d_dock.setWidget(MainWindow.view_3d)
        MainWindow.view_3d_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
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
        MainWindow.right_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
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
        MainWindow.properties_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        toggle_action = MainWindow.properties_dock.toggleViewAction()
        toggle_action.setText("Properties/Console")
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.properties_dock)

        # --- 3. Layout Adjustments ---
        MainWindow.splitDockWidget(MainWindow.view_3d_dock, MainWindow.right_dock, Qt.Horizontal)
        MainWindow.splitDockWidget(MainWindow.right_dock, MainWindow.properties_dock, Qt.Vertical)

        # 3D view 40%, 2D views 60%.  resizeDocks reads these as proportions
        # rather than pixels, so the split holds at any window size.
        MainWindow.resizeDocks([MainWindow.view_3d_dock, MainWindow.right_dock],
                               [40, 60], Qt.Horizontal)
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
        self.action_asset_browser.setToolTip("Toggle Asset Browser")
        
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
        select_menu = menubar.addMenu('Select')
        view_menu = menubar.addMenu('View')
        MainWindow.tools_menu = menubar.addMenu('Tools')
        MainWindow.debug_menu = menubar.addMenu('Debug')
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

        MainWindow.copy_action = QAction('Copy', MainWindow)
        MainWindow.copy_action.setShortcut('Ctrl+C')
        MainWindow.copy_action.setToolTip('Copy the selected brush or objects')
        MainWindow.copy_action.triggered.connect(MainWindow.copy_selection)
        edit_menu.addAction(MainWindow.copy_action)

        MainWindow.paste_action = QAction('Paste', MainWindow)
        MainWindow.paste_action.setShortcut('Ctrl+V')
        MainWindow.paste_action.setToolTip('Paste the copied brush or objects')
        MainWindow.paste_action.triggered.connect(MainWindow.paste_selection)
        edit_menu.addAction(MainWindow.paste_action)

        edit_menu.addSeparator()
        edit_menu.addAction(QAction('Hide Brush', MainWindow, shortcut='H', triggered=MainWindow.hide_selected_brush))
        edit_menu.addAction(QAction('Unhide All Brushes', MainWindow, shortcut='Shift+H', triggered=MainWindow.unhide_all_brushes))

        edit_menu.addSeparator()
        grid_colours_action = QAction('Grid colours…', MainWindow)
        grid_colours_action.setToolTip("Customise grid line colours")
        grid_colours_action.triggered.connect(MainWindow.open_grid_colours_dialog)
        edit_menu.addAction(grid_colours_action)
        MainWindow.grid_colours_action = grid_colours_action

        # --- Select menu: component modes + Radiant-style area selections ---
        MainWindow.component_mode_actions = {}
        component_group = QActionGroup(MainWindow)
        component_group.setExclusive(True)
        for mode, label, shortcut in (
                ('object', 'Object Mode', 'Shift+O'),
                ('vertex', 'Vertex Mode', 'Shift+V'),
                ('edge', 'Edge Mode', 'Shift+E'),
                ('face', 'Face Mode (geometry)', 'Shift+F')):
            action = QAction(label, MainWindow, checkable=True)
            action.setChecked(mode == 'object')
            action.setShortcut(shortcut)
            action.triggered.connect(
                lambda _checked, m=mode: MainWindow.set_component_mode(m))
            component_group.addAction(action)
            select_menu.addAction(action)
            MainWindow.component_mode_actions[mode] = action

        select_menu.addSeparator()
        MainWindow.surface_inspector_action = QAction(
            'Surface Inspector…', MainWindow)
        # T is the primary key; Shift+S is kept as Radiant's own binding.
        MainWindow.surface_inspector_action.setShortcuts(
            [QKeySequence('T'), QKeySequence('Shift+S')])
        # T must work from every editor child, including OpenGL views and
        # docked/floating panels.  WindowShortcut is the correct scope for a
        # MainWindow action; WidgetWithChildrenShortcut is too narrow once
        # focus moves through Qt's dock/toolbar hierarchy.
        MainWindow.surface_inspector_action.setShortcutContext(
            Qt.WindowShortcut)
        MainWindow.surface_inspector_action.setToolTip(
            'Texture the hovered face, or the selected brush (T)')
        MainWindow.surface_inspector_action.triggered.connect(
            MainWindow.toggle_surface_inspector)
        select_menu.addAction(MainWindow.surface_inspector_action)

        select_menu.addSeparator()
        cycle_action = QAction('Cycle Component Mode', MainWindow, shortcut='Q')
        cycle_action.setToolTip('Step Object -> Vertex -> Edge -> Face')
        cycle_action.triggered.connect(MainWindow.cycle_component_mode)
        select_menu.addAction(cycle_action)

        select_menu.addSeparator()
        for label, slot, shortcut, tip in (
                ('Select Touching', MainWindow.select_touching, 'Ctrl+T',
                 'Select everything whose bounds touch the selected brush'),
                ('Select Inside', MainWindow.select_inside, 'Ctrl+I',
                 'Select everything wholly inside the selected brush '
                 '(the brush is consumed)'),
                ('Select Partial Tall', MainWindow.select_partial_tall,
                 'Ctrl+Shift+T',
                 'Select everything crossing the brush\'s column in the active '
                 '2D view, at any depth'),
                ('Select Complete Tall', MainWindow.select_complete_tall,
                 'Ctrl+Shift+I',
                 'Select everything wholly within the brush\'s column in the '
                 'active 2D view')):
            action = QAction(label, MainWindow, shortcut=shortcut)
            action.setToolTip(tip)
            action.triggered.connect(slot)
            select_menu.addAction(action)

        view_menu.addActions([
            MainWindow.scene_hierarchy_dock.toggleViewAction(),
            MainWindow.view_3d_dock.toggleViewAction(), 
            MainWindow.right_dock.toggleViewAction(), 
            MainWindow.properties_dock.toggleViewAction()
        ])
        
        view_menu.addSeparator()
        
        view_menu.addAction(self.action_asset_browser)

        MainWindow.surface_inspector_view_action = QAction(
            'Surface Inspector (T)', MainWindow)
        MainWindow.surface_inspector_view_action.setToolTip(
            'Show or hide the Surface Inspector (T)')
        MainWindow.surface_inspector_view_action.triggered.connect(
            MainWindow.toggle_surface_inspector)
        view_menu.addAction(MainWindow.surface_inspector_view_action)

        MainWindow.connection_links_action = QAction(
            'Connection Links', MainWindow, checkable=True)
        MainWindow.connection_links_action.setChecked(
            getattr(MainWindow, 'show_logic_links', True))
        MainWindow.connection_links_action.setToolTip(
            'Show I/O connection links in the editor views (F1)')
        MainWindow.connection_links_action.triggered.connect(
            MainWindow.set_connection_links_enabled)
        view_menu.addAction(MainWindow.connection_links_action)

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

        # --- Debug Menu Actions ---

        MainWindow.system_monitor_action = QAction(
            'Sysmon (F3)', MainWindow, checkable=True)
        MainWindow.system_monitor_action.setShortcut('F3')
        MainWindow.system_monitor_action.setChecked(
            MainWindow.view_3d.sysmon.is_active())
        MainWindow.system_monitor_action.triggered.connect(
            MainWindow.toggle_system_monitor)
        MainWindow.debug_menu.addAction(MainWindow.system_monitor_action)

        # --- Tools Menu Actions ---

        autocaulk_action = QAction("Autocaulk", MainWindow)
        autocaulk_action.setToolTip("Apply nodraw to all invisible brush faces")
        autocaulk_action.triggered.connect(MainWindow.autocaulk)
        MainWindow.tools_menu.addAction(autocaulk_action)

        # Benchmark action is inserted by MainWindow immediately below Autocaulk.

        MainWindow.logic_graph_action = QAction('Logic Graph Editor…', MainWindow)
        MainWindow.logic_graph_action.setShortcut('Ctrl+L')
        MainWindow.logic_graph_action.setToolTip('Open the visual I/O node graph editor')
        MainWindow.logic_graph_action.triggered.connect(MainWindow.open_logic_graph)

        MainWindow.cutscene_wizard_action = QAction('Cutscene Wizard…', MainWindow)
        MainWindow.cutscene_wizard_action.setShortcut('Ctrl+Shift+C')
        MainWindow.cutscene_wizard_action.setToolTip('Author a camera-and-actor cutscene from the 3D view')
        MainWindow.cutscene_wizard_action.triggered.connect(MainWindow.open_cutscene_wizard)

        MainWindow.logic_wizard_action = QAction('Logic Wizard…', MainWindow)
        MainWindow.logic_wizard_action.setShortcut('Ctrl+Shift+W')
        MainWindow.logic_wizard_action.setToolTip('Guided setup for common I/O scenarios')
        MainWindow.logic_wizard_action.triggered.connect(MainWindow.open_logic_wizard)

        MainWindow.project_overview_action = QAction('Project Overview…', MainWindow)
        MainWindow.project_overview_action.setToolTip(
            'What this map contains: brushes, movers, lights, monsters, '
            'connections, and when it was created and last saved')
        MainWindow.project_overview_action.triggered.connect(
            MainWindow.open_project_overview)

        MainWindow.validate_action = QAction('Validate All Connections…', MainWindow)
        MainWindow.validate_action.setToolTip(
            'Check every connection for a missing target, or an input or output '
            'the entity type does not have')
        MainWindow.validate_action.triggered.connect(MainWindow.validate_io_connections)

        MainWindow.terrain_action = QAction('Terrain Generator…', MainWindow)
        MainWindow.terrain_action.setToolTip('Open the terrain editor (low‑poly terrain generator)')
        MainWindow.terrain_action.triggered.connect(MainWindow.open_terrain_editor)

        MainWindow.procedural_action = QAction('Procedural Map Generator…', MainWindow)
        MainWindow.procedural_action.triggered.connect(MainWindow.show_procedural_map_generator)
        
        MainWindow.tools_menu.addAction(MainWindow.logic_graph_action)
import os
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStatusBar, QToolBar,
    QLabel, QSpinBox, QComboBox, QAction, QMessageBox, QFrame,
    QDockWidget, QTabWidget, QPushButton, QActionGroup,
    QApplication, QSizePolicy, QInputDialog, QMenu,
    QStyle, QStyleOptionButton
)
from PyQt5.QtCore import Qt, QSize, QByteArray, QRect
from PyQt5.QtGui import QIcon, QKeySequence, QPainter

from editor.view_2d import View2D
from engine.qt_game_view import QtGameView
from engine.view_distance import (
    DEFAULT_VIEW_DISTANCE, MAX_VIEW_DISTANCE, MIN_VIEW_DISTANCE)
from editor.property_editor import PropertyEditor
from editor.scene_hierarchy import SceneHierarchy
from editor.asset_browser import AssetBrowser
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

class RotatablePlayButton(QPushButton):
    """Play button that rotates its complete presentation for a vertical toolbar."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vertical = False

    def set_vertical(self, vertical):
        vertical = bool(vertical)
        if self._vertical != vertical:
            self._vertical = vertical
            self.setProperty("_vertical", vertical)
            self.update()

    def paintEvent(self, event):
        if not self._vertical:
            super().paintEvent(event)
            return

        # Draw the normal QPushButton in a transposed coordinate system.
        # This rotates both icon and label instead of forcing a 250px-wide
        # horizontal button into a narrow right-hand toolbar.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.translate(self.width(), 0)
        painter.rotate(90)

        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = QRect(0, 0, self.height(), self.width())
        self.style().drawControl(QStyle.CE_PushButton, option, painter, self)
        painter.end()


#: Bumped whenever the default dock arrangement changes.  A layout saved by
#: an older version is dropped once, so a new default actually reaches an
#: install that has been opened before -- settings.ini stores the layout on
#: every close, and restoreState() would otherwise win forever.
LAYOUT_VERSION = 3


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
        #
        # Keep a useful windowTitle for toggleViewAction() in View, but replace
        # the visible dock title bar with a zero-height widget.  The hierarchy
        # itself should start at the top instead of wasting a row on "Scene".
        MainWindow.scene_hierarchy_dock = QDockWidget("Scene Hierarchy", MainWindow)
        MainWindow.scene_hierarchy_dock.setObjectName("SceneDock")
        MainWindow.scene_hierarchy_dock.setWidget(MainWindow.scene_hierarchy)
        scene_title_bar = QWidget()
        scene_title_bar.setFixedHeight(0)
        scene_title_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        MainWindow.scene_hierarchy_dock.setTitleBarWidget(scene_title_bar)
        MainWindow.scene_hierarchy_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        MainWindow.addDockWidget(Qt.LeftDockWidgetArea, MainWindow.scene_hierarchy_dock)
        
        screen_width = QApplication.primaryScreen().geometry().width()
        MainWindow.scene_hierarchy_dock.setMaximumWidth(int(screen_width * 0.10))

        # 3D View Dock (Center/Right)
        MainWindow.view_3d_dock = QDockWidget("3D View", MainWindow)
        MainWindow.view_3d_dock.setObjectName("View3DDock")
        MainWindow.view_3d_dock.setWidget(MainWindow.view_3d)
        MainWindow.view_3d_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
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
        MainWindow.right_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
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
        MainWindow.properties_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        toggle_action = MainWindow.properties_dock.toggleViewAction()
        toggle_action.setText("Properties/Console")
        MainWindow.addDockWidget(Qt.RightDockWidgetArea, MainWindow.properties_dock)

        # --- 3. Layout Adjustments ---
        MainWindow.splitDockWidget(MainWindow.view_3d_dock, MainWindow.right_dock, Qt.Horizontal)
        MainWindow.splitDockWidget(MainWindow.right_dock, MainWindow.properties_dock, Qt.Vertical)

        # 3D view 40%, 2D views 60%.  resizeDocks reads these as proportions
        # rather than pixels, so the split holds at any window size.
        MainWindow.resizeDocks([MainWindow.view_3d_dock, MainWindow.right_dock],
                               [40, 60], Qt.Horizontal)
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
        self.action_asset_browser.setToolTip("Toggle Asset Browser")
        
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
        select_menu = menubar.addMenu('Select')
        view_menu = menubar.addMenu('View')
        MainWindow.tools_menu = menubar.addMenu('Tools')
        MainWindow.debug_menu = menubar.addMenu('Debug')
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

        MainWindow.copy_action = QAction('Copy', MainWindow)
        MainWindow.copy_action.setShortcut('Ctrl+C')
        MainWindow.copy_action.setToolTip('Copy the selected brush or objects')
        MainWindow.copy_action.triggered.connect(MainWindow.copy_selection)
        edit_menu.addAction(MainWindow.copy_action)

        MainWindow.paste_action = QAction('Paste', MainWindow)
        MainWindow.paste_action.setShortcut('Ctrl+V')
        MainWindow.paste_action.setToolTip('Paste the copied brush or objects')
        MainWindow.paste_action.triggered.connect(MainWindow.paste_selection)
        edit_menu.addAction(MainWindow.paste_action)

        edit_menu.addSeparator()
        edit_menu.addAction(QAction('Hide Brush', MainWindow, shortcut='H', triggered=MainWindow.hide_selected_brush))
        edit_menu.addAction(QAction('Unhide All Brushes', MainWindow, shortcut='Shift+H', triggered=MainWindow.unhide_all_brushes))

        edit_menu.addSeparator()
        grid_colours_action = QAction('Grid colours…', MainWindow)
        grid_colours_action.setToolTip("Customise grid line colours")
        grid_colours_action.triggered.connect(MainWindow.open_grid_colours_dialog)
        edit_menu.addAction(grid_colours_action)
        MainWindow.grid_colours_action = grid_colours_action

        # --- Select menu: component modes + Radiant-style area selections ---
        MainWindow.component_mode_actions = {}
        component_group = QActionGroup(MainWindow)
        component_group.setExclusive(True)
        for mode, label, shortcut in (
                ('object', 'Object Mode', 'Shift+O'),
                ('vertex', 'Vertex Mode', 'Shift+V'),
                ('edge', 'Edge Mode', 'Shift+E'),
                ('face', 'Face Mode (geometry)', 'Shift+F')):
            action = QAction(label, MainWindow, checkable=True)
            action.setChecked(mode == 'object')
            action.setShortcut(shortcut)
            action.triggered.connect(
                lambda _checked, m=mode: MainWindow.set_component_mode(m))
            component_group.addAction(action)
            select_menu.addAction(action)
            MainWindow.component_mode_actions[mode] = action

        select_menu.addSeparator()
        MainWindow.surface_inspector_action = QAction(
            'Surface Inspector…', MainWindow)
        # T is the primary key; Shift+S is kept as Radiant's own binding.
        MainWindow.surface_inspector_action.setShortcuts(
            [QKeySequence('T'), QKeySequence('Shift+S')])
        # T must work from every editor child, including OpenGL views and
        # docked/floating panels.  WindowShortcut is the correct scope for a
        # MainWindow action; WidgetWithChildrenShortcut is too narrow once
        # focus moves through Qt's dock/toolbar hierarchy.
        MainWindow.surface_inspector_action.setShortcutContext(
            Qt.WindowShortcut)
        MainWindow.surface_inspector_action.setToolTip(
            'Texture the hovered face, or the selected brush (T)')
        MainWindow.surface_inspector_action.triggered.connect(
            MainWindow.toggle_surface_inspector)
        select_menu.addAction(MainWindow.surface_inspector_action)

        select_menu.addSeparator()
        cycle_action = QAction('Cycle Component Mode', MainWindow, shortcut='Q')
        cycle_action.setToolTip('Step Object -> Vertex -> Edge -> Face')
        cycle_action.triggered.connect(MainWindow.cycle_component_mode)
        select_menu.addAction(cycle_action)

        select_menu.addSeparator()
        for label, slot, shortcut, tip in (
                ('Select Touching', MainWindow.select_touching, 'Ctrl+T',
                 'Select everything whose bounds touch the selected brush'),
                ('Select Inside', MainWindow.select_inside, 'Ctrl+I',
                 'Select everything wholly inside the selected brush '
                 '(the brush is consumed)'),
                ('Select Partial Tall', MainWindow.select_partial_tall,
                 'Ctrl+Shift+T',
                 'Select everything crossing the brush\'s column in the active '
                 '2D view, at any depth'),
                ('Select Complete Tall', MainWindow.select_complete_tall,
                 'Ctrl+Shift+I',
                 'Select everything wholly within the brush\'s column in the '
                 'active 2D view')):
            action = QAction(label, MainWindow, shortcut=shortcut)
            action.setToolTip(tip)
            action.triggered.connect(slot)
            select_menu.addAction(action)

        view_menu.addActions([
            MainWindow.scene_hierarchy_dock.toggleViewAction(),
            MainWindow.view_3d_dock.toggleViewAction(), 
            MainWindow.right_dock.toggleViewAction(), 
            MainWindow.properties_dock.toggleViewAction()
        ])
        
        view_menu.addSeparator()
        
        view_menu.addAction(self.action_asset_browser)

        MainWindow.surface_inspector_view_action = QAction(
            'Surface Inspector (T)', MainWindow)
        MainWindow.surface_inspector_view_action.setToolTip(
            'Show or hide the Surface Inspector (T)')
        MainWindow.surface_inspector_view_action.triggered.connect(
            MainWindow.toggle_surface_inspector)
        view_menu.addAction(MainWindow.surface_inspector_view_action)

        MainWindow.connection_links_action = QAction(
            'Connection Links', MainWindow, checkable=True)
        MainWindow.connection_links_action.setChecked(
            getattr(MainWindow, 'show_logic_links', True))
        MainWindow.connection_links_action.setToolTip(
            'Show I/O connection links in the editor views (F1)')
        MainWindow.connection_links_action.triggered.connect(
            MainWindow.set_connection_links_enabled)
        view_menu.addAction(MainWindow.connection_links_action)

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

        # --- Debug Menu Actions ---

        MainWindow.system_monitor_action = QAction(
            'Sysmon (F3)', MainWindow, checkable=True)
        MainWindow.system_monitor_action.setShortcut('F3')
        MainWindow.system_monitor_action.setChecked(
            MainWindow.view_3d.sysmon.is_active())
        MainWindow.system_monitor_action.triggered.connect(
            MainWindow.toggle_system_monitor)
        MainWindow.debug_menu.addAction(MainWindow.system_monitor_action)

        # --- Tools Menu Actions ---

        autocaulk_action = QAction("Autocaulk", MainWindow)
        autocaulk_action.setToolTip("Apply nodraw to all invisible brush faces")
        autocaulk_action.triggered.connect(MainWindow.autocaulk)
        MainWindow.tools_menu.addAction(autocaulk_action)

        # Benchmark action is inserted by MainWindow immediately below Autocaulk.

        MainWindow.logic_graph_action = QAction('Logic Graph Editor…', MainWindow)
        MainWindow.logic_graph_action.setShortcut('Ctrl+L')
        MainWindow.logic_graph_action.setToolTip('Open the visual I/O node graph editor')
        MainWindow.logic_graph_action.triggered.connect(MainWindow.open_logic_graph)

        MainWindow.cutscene_wizard_action = QAction('Cutscene Wizard…', MainWindow)
        MainWindow.cutscene_wizard_action.setShortcut('Ctrl+Shift+C')
        MainWindow.cutscene_wizard_action.setToolTip('Author a camera-and-actor cutscene from the 3D view')
        MainWindow.cutscene_wizard_action.triggered.connect(MainWindow.open_cutscene_wizard)

        MainWindow.logic_wizard_action = QAction('Logic Wizard…', MainWindow)
        MainWindow.logic_wizard_action.setShortcut('Ctrl+Shift+W')
        MainWindow.logic_wizard_action.setToolTip('Guided setup for common I/O scenarios')
        MainWindow.logic_wizard_action.triggered.connect(MainWindow.open_logic_wizard)

        MainWindow.project_overview_action = QAction('Project Overview…', MainWindow)
        MainWindow.project_overview_action.setToolTip(
            'What this map contains: brushes, movers, lights, monsters, '
            'connections, and when it was created and last saved')
        MainWindow.project_overview_action.triggered.connect(
            MainWindow.open_project_overview)

        MainWindow.validate_action = QAction('Validate All Connections…', MainWindow)
        MainWindow.validate_action.setToolTip(
            'Check every connection for a missing target, or an input or output '
            'the entity type does not have')
        MainWindow.validate_action.triggered.connect(MainWindow.validate_io_connections)

        MainWindow.terrain_action = QAction('Terrain Generator…', MainWindow)
        MainWindow.terrain_action.setToolTip('Open the terrain editor (low‑poly terrain generator)')
        MainWindow.terrain_action.triggered.connect(MainWindow.open_terrain_editor)

        MainWindow.procedural_action = QAction('Procedural Map Generator…', MainWindow)
        MainWindow.procedural_action.triggered.connect(MainWindow.show_procedural_map_generator)
        
        MainWindow.tools_menu.addAction(MainWindow.logic_graph_action)
        MainWindow.tools_menu.addAction(MainWindow.logic_wizard_action)
        MainWindow.tools_menu.addAction(MainWindow.cutscene_wizard_action)
