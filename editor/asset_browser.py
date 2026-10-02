import os
import sys
import math
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame,
                             QHBoxLayout, QGridLayout, QSplitter, QApplication,
                             QMainWindow, QPushButton, QFileDialog, QTreeView, 
                             QFileSystemModel, QTabWidget, QAbstractItemView,
                             QSizePolicy)
from PyQt5.QtCore import Qt, QSize, QDir, QRect, QPointF
from PyQt5.QtGui import QPixmap, QColor, QPainter, QFont, QIcon, QPen, QPolygonF

def render_obj_thumbnail(filepath, width, height):
    """
    Simple software renderer to generate a wireframe thumbnail from an OBJ file.
    """
    vertices = []
    faces = []
    
    try:
        # Limit processing to avoid freezing on huge files
        max_lines = 10000 
        line_count = 0
        
        with open(filepath, 'r') as f:
            for line in f:
                line_count += 1
                if line_count > max_lines: break
                
                if line.startswith('v '):
                    parts = line.split()
                    vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
                elif line.startswith('f '):
                    parts = line.split()
                    face_idxs = []
                    for p in parts[1:]:
                        idx = int(p.split('/')[0]) - 1
                        face_idxs.append(idx)
                    faces.append(face_idxs)
    except Exception:
        return None

    if not vertices:
        return None

    # Normalize vertices to -1..1 range
    min_v = [float('inf')] * 3
    max_v = [float('-inf')] * 3
    
    for v in vertices:
        for i in range(3):
            if v[i] < min_v[i]: min_v[i] = v[i]
            if v[i] > max_v[i]: max_v[i] = v[i]
            
    center = [(min_v[i] + max_v[i]) / 2 for i in range(3)]
    scale = 0
    for i in range(3):
        scale = max(scale, (max_v[i] - min_v[i]) / 2)
    
    if scale == 0: scale = 1
    
    # 3D Transformation (isometric-ish view)
    import math
    angle_y = math.radians(45)
    angle_x = math.radians(30)
    
    cos_y, sin_y = math.cos(angle_y), math.sin(angle_y)
    cos_x, sin_x = math.cos(angle_x), math.sin(angle_x)
    
    projected_points = []
    
    for v in vertices:
        # Center
        x = (v[0] - center[0]) / scale
        y = (v[1] - center[1]) / scale
        z = (v[2] - center[2]) / scale
        
        # Rotate Y
        rx = x * cos_y - z * sin_y
        rz = x * sin_y + z * cos_y
        
        # Rotate X
        ry = y * cos_x - rz * sin_x
        
        # Map to screen
        screen_x = width/2 + rx * (width * 0.4)
        screen_y = height/2 - ry * (height * 0.4) # Invert Y for screen coords
        
        projected_points.append(QPointF(screen_x, screen_y))

    # Draw
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(50, 50, 60))
    
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    
    # Draw Edges (Cyan wireframe)
    pen = QPen(QColor(0, 200, 255))
    pen.setWidthF(1.0)
    painter.setPen(pen)
    
    for face in faces:
        if len(face) < 2: continue
        
        # Check indices bounds
        valid = True
        for idx in face:
            if idx < 0 or idx >= len(projected_points):
                valid = False
                break
        if not valid: continue
        
        poly = QPolygonF()
        for idx in face:
            poly.append(projected_points[idx])
        
        # Close loop
        poly.append(projected_points[face[0]])
        
        painter.drawPolyline(poly)
        
    painter.end()
    return pixmap

class AssetItem(QWidget):
    """
    A widget representing a single asset (file) in the grid view.
    """
    def __init__(self, name, path, browser, is_model=False):
        super().__init__()
        self.name_text = name
        self.file_path = path
        self.browser = browser
        self.is_model = is_model
        
        self.setFixedSize(100, 120)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)
        
        # Thumbnail
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(90, 90)
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self.thumb_label.setStyleSheet("background-color: #333; border-radius: 4px;")
        
        self._load_thumbnail()
        
        layout.addWidget(self.thumb_label)
        
        # Name
        self.name_label = QLabel(name)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("color: #ccc; font-size: 10px;")
        layout.addWidget(self.name_label)
        
        self.selected = False

    def _load_thumbnail(self):
        pixmap = QPixmap()
        
        if self.is_model:
            # 1. Look for .png sidecar
            base_path = os.path.splitext(self.file_path)[0]
            thumb_path = base_path + ".png"
            
            if os.path.exists(thumb_path):
                pixmap.load(thumb_path)
            else:
                # 2. Generate wireframe from OBJ
                generated_pix = render_obj_thumbnail(self.file_path, 90, 90)
                if generated_pix:
                    pixmap = generated_pix
                else:
                    # 3. Fallback text
                    pixmap = QPixmap(90, 90)
                    pixmap.fill(QColor(60, 70, 80))
                    painter = QPainter(pixmap)
                    painter.setPen(QColor(200, 200, 200))
                    font = QFont("Arial", 16, QFont.Bold)
                    painter.setFont(font)
                    painter.drawText(QRect(0, 0, 90, 90), Qt.AlignCenter, "OBJ")
                    painter.end()
        else:
            # Assume image
            pixmap.load(self.file_path)

        if not pixmap.isNull():
            if self.is_model and not os.path.exists(os.path.splitext(self.file_path)[0] + ".png"):
                # Already scaled if generated
                self.thumb_label.setPixmap(pixmap)
            else:
                scaled = pixmap.scaled(90, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb_label.setPixmap(scaled)
        else:
            self.thumb_label.setText("?")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.browser.select_item(self)

    def set_selected(self, selected):
        self.selected = selected
        if selected:
            self.setStyleSheet("""
                AssetItem {
                    background-color: #3c3c3c;
                    border: 1px solid #F08000;
                    border-radius: 4px;
                }
            """)
            self.name_label.setStyleSheet("color: #F08000; font-weight: bold; font-size: 10px;")
        else:
            self.setStyleSheet("""
                AssetItem {
                    background-color: transparent;
                    border: none;
                }
                AssetItem:hover {
                    background-color: #333;
                    border-radius: 4px;
                }
            """)
            self.name_label.setStyleSheet("color: #ccc; font-size: 10px;")


class AssetBrowserTab(QWidget):
    """
    A single tab content for the Asset Browser (e.g., Textures or Models).
    """
    def __init__(self, root_path, file_extensions, editor=None, is_model_tab=False, parent_browser=None):
        super().__init__()
        self.root_path = root_path
        self.current_asset_folder = root_path
        self.extensions = file_extensions
        self.is_model_tab = is_model_tab
        self.editor = editor
        self.parent_browser = parent_browser
        self.selected_item = None
        self.items = []

        if not os.path.exists(self.current_asset_folder):
            try: os.makedirs(self.current_asset_folder)
            except: pass

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.splitter = QSplitter(Qt.Horizontal)
        self.layout.addWidget(self.splitter)

        # 1. Left Pane (Folder Tree)
        self.tree_frame = QFrame()
        tree_layout = QVBoxLayout(self.tree_frame)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)
        
        folder_name = os.path.basename(os.path.normpath(self.root_path))
        self.home_btn = QPushButton(f"🏠 {folder_name.capitalize()}")
        self.home_btn.setToolTip(f"Go to root {folder_name} folder")
        self.home_btn.clicked.connect(self.go_to_root)
        self.home_btn.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 6px 12px;
                background-color: #333;
                border: none;
                border-bottom: 1px solid #444;
                color: #ddd;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #444;
                color: white;
            }
        """)
        tree_layout.addWidget(self.home_btn)
        
        self.dir_model = QFileSystemModel()
        self.dir_model.setRootPath(self.root_path)
        self.dir_model.setFilter(QDir.NoDotAndDotDot | QDir.AllDirs)
        
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.dir_model)
        self.tree_view.setRootIndex(self.dir_model.index(self.root_path))
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setColumnHidden(1, True)
        self.tree_view.setColumnHidden(2, True)
        self.tree_view.setColumnHidden(3, True)
        self.tree_view.clicked.connect(self.on_tree_clicked)
        self.tree_view.setStyleSheet("""
            QTreeView { background-color: #252525; color: #ddd; border: none; }
            QTreeView::item { padding: 4px; }
            QTreeView::item:selected { background-color: #444; color: white; }
            QTreeView::item:hover { background-color: #333; }
        """)
        tree_layout.addWidget(self.tree_view)
        self.splitter.addWidget(self.tree_frame)

        # 2. Middle Pane (Grid View)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("background-color: #2b2b2b; border: none;")
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid_layout.setSpacing(10)
        self.scroll_area.setWidget(self.grid_container)
        self.splitter.addWidget(self.scroll_area)

        # 3. Right Pane (Preview) - COMPACT
        self.details_frame = QFrame()
        self.details_frame.setMinimumWidth(180)
        self.details_frame.setStyleSheet("background-color: #333; border-left: 1px solid #444;")
        details_layout = QVBoxLayout(self.details_frame)
        details_layout.setContentsMargins(10, 10, 10, 10)
        
        # Reduced size for preview to save vertical space
        self.preview_label = QLabel("Select Item")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFixedSize(140, 140) 
        self.preview_label.setStyleSheet("background-color: #252525; border: 1px solid #444; border-radius: 4px; color: #666;")
        details_layout.addWidget(self.preview_label)
        
        self.name_info_label = QLabel("")
        self.name_info_label.setWordWrap(True)
        self.name_info_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.name_info_label.setStyleSheet("color: white; font-weight: bold; margin-top: 5px; font-size: 11px;")
        details_layout.addWidget(self.name_info_label)
        
        # Buttons removed from here, now handled by AssetBrowser global button
        
        details_layout.addStretch()
        self.splitter.addWidget(self.details_frame)
        self.splitter.setSizes([180, 600, 180])
        self.load_directory(self.current_asset_folder)

    def go_to_root(self):
        self.load_directory(self.root_path)
        self.tree_view.collapseAll()
        self.tree_view.setCurrentIndex(self.dir_model.index(self.root_path))

    def on_tree_clicked(self, index):
        path = self.dir_model.filePath(index)
        self.load_directory(path)

    def load_directory(self, path):
        self.current_asset_folder = path
        for i in reversed(range(self.grid_layout.count())): 
            self.grid_layout.itemAt(i).widget().setParent(None)
        self.items.clear()
        self.selected_item = None
        self.update_details_pane(None)

        if not os.path.exists(path): return

        try:
            files = os.listdir(path)
            files.sort()
            row, col = 0, 0
            max_cols = 4 
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in self.extensions:
                    full_path = os.path.join(path, f)
                    item = AssetItem(f, full_path, self, is_model=self.is_model_tab)
                    self.grid_layout.addWidget(item, row, col)
                    self.items.append(item)
                    col += 1
                    if col >= max_cols: col, row = 0, row + 1
        except Exception as e:
            print(f"Error: {e}")

    def select_item(self, item):
        if self.selected_item: self.selected_item.set_selected(False)
        self.selected_item = item
        self.selected_item.set_selected(True)
        self.update_details_pane(item)

    def update_details_pane(self, item):
        if not item:
            self.preview_label.setText("Select Item")
            self.preview_label.setPixmap(QPixmap())
            self.name_info_label.setText("")
            # Disable global buttons
            if self.parent_browser: self.parent_browser.set_action_enabled(False)
            return

        self.name_info_label.setText(item.name_text)
        pixmap = QPixmap()
        if self.is_model_tab:
            base = os.path.splitext(item.file_path)[0]
            if os.path.exists(base + ".png"): pixmap.load(base + ".png")
            else: 
                gen = render_obj_thumbnail(item.file_path, 140, 140)
                pixmap = gen if gen else QPixmap()
        else:
            pixmap.load(item.file_path)
            
        if not pixmap.isNull():
            self.preview_label.setPixmap(pixmap.scaled(140, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.preview_label.setText("No Preview")

        # Enable global buttons
        if self.parent_browser: self.parent_browser.set_action_enabled(True)

    def perform_main_action(self, tiled=False):
        """Called by the parent browser's global button."""
        if not self.selected_item: return
        
        if self.is_model_tab:
            self.add_current_model()
        else:
            self.apply_texture(tiled)

    def add_current_model(self):
        if self.editor and self.selected_item:
            self.editor.add_model_to_scene(self.selected_item.file_path, [0,0,0], [1,1,1])

    def apply_texture(self, tiled=False):
        """Apply texture to whole brush"""
        if self.editor and hasattr(self.editor, 'apply_texture_to_brush') and self.selected_item:
            rel_path = os.path.relpath(self.selected_item.file_path, self.root_path)
            rel_path = rel_path.replace('\\', '/')
            self.editor.apply_texture_to_brush(rel_path, tiled=tiled)

class AssetBrowser(QWidget):
    def __init__(self, initial_path, editor=None):
        super().__init__()
        self.editor = editor
        
        # Enforce compact vertical size preference
        # FIX: Changed from Maximum to Expanding to allow resizing when floating
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        if initial_path.endswith("textures"):
            self.assets_root = os.path.dirname(initial_path)
            self.textures_path = initial_path
        else:
            self.assets_root = initial_path
            self.textures_path = os.path.join(initial_path, "textures")
            
        self.models_path = os.path.join(self.assets_root, "models")
        
        for p in [self.textures_path, self.models_path]:
            if not os.path.exists(p):
                try: os.makedirs(p)
                except: pass

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # --- Container for Corner Widgets ---
        self.corner_widget_container = QWidget()
        self.corner_layout = QHBoxLayout(self.corner_widget_container)
        self.corner_layout.setContentsMargins(0, 0, 0, 0)
        self.corner_layout.setSpacing(2)
        
        # --- Global Action Button in Tab Bar ---
        self.action_btn = QPushButton("FIT")
        self.action_btn.setObjectName("AssetActionBtn")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.setEnabled(False) 
        self.action_btn.clicked.connect(self.on_global_action_clicked)
        
        # --- Tile Button ---
        self.tile_btn = QPushButton("TILE")
        self.tile_btn.setObjectName("AssetTileBtn")
        self.tile_btn.setCursor(Qt.PointingHandCursor)
        self.tile_btn.setEnabled(False)
        self.tile_btn.clicked.connect(self.on_tile_action_clicked)
        self.tile_btn.setToolTip("Apply texture and tile it based on brush size")

        # --- Face Mode Button ---
        self.face_btn = QPushButton("FACE")
        self.face_btn.setObjectName("AssetFaceBtn")
        self.face_btn.setCursor(Qt.PointingHandCursor)
        self.face_btn.setCheckable(True)
        self.face_btn.setEnabled(True) # Always enabled to toggle mode
        self.face_btn.clicked.connect(self.on_face_mode_clicked)
        self.face_btn.setToolTip("Toggle Face Selection Mode (Purple highlight). Left click to apply texture.")

        green_style = """
            QPushButton {
                background-color: #2E7D32; 
                color: white; 
                font-weight: bold;
                padding: 4px 15px; 
                border: 1px solid #1B5E20; 
                border-radius: 3px; 
                margin: 2px 2px;
                min-width: 60px;
            }
            QPushButton:hover { background-color: #388E3C; }
            QPushButton:pressed { background-color: #1B5E20; }
            QPushButton:disabled { background-color: #444; color: #888; border: 1px solid #555; }
        """
        
        purple_style = """
            QPushButton {
                background-color: #7B1FA2; 
                color: white; 
                font-weight: bold;
                padding: 4px 15px; 
                border: 1px solid #4A148C; 
                border-radius: 3px; 
                margin: 2px 2px;
                min-width: 60px;
            }
            QPushButton:hover { background-color: #8E24AA; }
            QPushButton:pressed { background-color: #4A148C; }
            QPushButton:checked { background-color: #D500F9; border: 1px solid white; }
            QPushButton:disabled { background-color: #444; color: #888; border: 1px solid #555; }
        """
        
        self.action_btn.setStyleSheet(green_style)
        self.tile_btn.setStyleSheet(green_style)
        self.face_btn.setStyleSheet(purple_style)

        self.corner_layout.addWidget(self.action_btn)
        self.corner_layout.addWidget(self.tile_btn)
        self.corner_layout.addWidget(self.face_btn)
        
        self.tabs.setCornerWidget(self.corner_widget_container, Qt.TopRightCorner)
        
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3d3d3d; background-color: #2b2b2b; }
            QTabBar::tab { background: #1e1e1e; color: #aaa; min-width: 100px; padding: 6px; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
            QTabBar::tab:selected { background: #F08000; color: white; font-weight: bold; }
            QTabBar::tab:hover:!selected { background: #333; }
        """)

        # Pass 'self' as parent_browser so tabs can update button state
        self.tab_textures = AssetBrowserTab(self.textures_path, ['.png', '.jpg', '.jpeg', '.tga', '.bmp'], editor, parent_browser=self)
        self.tabs.addTab(self.tab_textures, "Textures")
        
        self.tab_models = AssetBrowserTab(self.models_path, ['.obj'], editor, is_model_tab=True, parent_browser=self)
        self.tabs.addTab(self.tab_models, "Models")
        
        self.tabs.currentChanged.connect(self.on_tab_changed)

    def on_tab_changed(self, index):
        # Update button text based on tab type
        current_tab = self.tabs.widget(index)
        if current_tab.is_model_tab:
            self.action_btn.setText("Add to Scene")
            self.tile_btn.hide()
            self.face_btn.hide() # Hide face mode for models
        else:
            self.action_btn.setText("FIT")
            self.tile_btn.show()
            self.face_btn.show()
        
        # Update enabled state based on that tab's current selection
        enabled = current_tab.selected_item is not None
        self.action_btn.setEnabled(enabled)
        self.tile_btn.setEnabled(enabled)

    def on_global_action_clicked(self):
        # Delegate click to current tab
        current_tab = self.tabs.currentWidget()
        if current_tab:
            current_tab.perform_main_action(tiled=False)

    def on_tile_action_clicked(self):
        # Delegate click to current tab with tiled=True
        current_tab = self.tabs.currentWidget()
        if current_tab:
            current_tab.perform_main_action(tiled=True)
            
    def on_face_mode_clicked(self):
        if self.editor and hasattr(self.editor, 'toggle_face_mode'):
            self.editor.toggle_face_mode(self.face_btn.isChecked())

    def set_action_enabled(self, enabled):
        self.action_btn.setEnabled(enabled)
        self.tile_btn.setEnabled(enabled)

    @property
    def selected_item(self):
        w = self.tabs.currentWidget()
        return w.selected_item if w else None
    
    def get_selected_filepath(self):
        item = self.selected_item
        return item.file_path if item else None

if __name__ == '__main__':
    app = QApplication(sys.argv)
    test_root = os.path.join(os.getcwd(), "test_assets")
    tex_dir = os.path.join(test_root, "textures")
    mdl_dir = os.path.join(test_root, "models")
    if not os.path.exists(tex_dir): os.makedirs(tex_dir)
    if not os.path.exists(mdl_dir): os.makedirs(mdl_dir)
    window = QMainWindow()
    browser = AssetBrowser(tex_dir)
    window.setCentralWidget(browser)
    window.resize(1000, 300)
    window.show()
    sys.exit(app.exec_())