import os
import sys
import math
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame,
                             QHBoxLayout, QGridLayout, QSplitter, QApplication,
                             QMainWindow, QPushButton, QFileDialog, QTreeView, 
                             QFileSystemModel, QTabWidget, QAbstractItemView,
                             QSizePolicy, QListWidget, QListWidgetItem)
from PyQt5.QtCore import Qt, QSize, QDir, QRect, QPointF, pyqtSignal, QTimer
from PyQt5.QtGui import QPixmap, QColor, QPainter, QFont, QIcon, QPen, QPolygonF, QTextCursor, QDesktopServices
from engine.glb_loader import render_glb_thumbnail


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
    Selection is shown by a 3px orange border around the entire item.
    """
    def __init__(self, name, path, browser, is_model=False):
        super().__init__()
        self.name_text = name
        self.file_path = path
        self.browser = browser
        self.is_model = is_model
        
        self.setFixedSize(100, 120)
        # Reduce layout margins so the border has room
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
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
        
        # ---- Orange selection overlay (square on top) ----
        self.selection_overlay = QFrame(self)
        self.selection_overlay.setGeometry(0, 0, self.width(), self.height())
        self.selection_overlay.setStyleSheet("""
            QFrame {
                border: 3px solid #F08000;
                border-radius: 6px;
                background: transparent;
            }
        """)
        self.selection_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)  # clicks pass through
        self.selection_overlay.hide()
        # ----------------------------------------------------
        
        self.selected = False
        self._update_border()

    def _load_thumbnail(self):
        pixmap = QPixmap()

        if self.is_model:
            # 1. Look for .png sidecar
            base_path = os.path.splitext(self.file_path)[0]
            thumb_path = base_path + ".png"

            if os.path.exists(thumb_path):
                pixmap.load(thumb_path)
            else:
                # 2. Generate wireframe from model
                ext = os.path.splitext(self.file_path)[1].lower()
                generated_pix = None

                if ext == '.glb':
                    generated_pix = render_glb_thumbnail(self.file_path, 90, 90)
                elif ext == '.obj':
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
                    label = "GLB" if ext == '.glb' else "OBJ"
                    painter.drawText(QRect(0, 0, 90, 90), Qt.AlignCenter, label)
                    painter.end()
        else:
            # Assume image
            pixmap.load(self.file_path)

        if not pixmap.isNull():
            if self.is_model and not os.path.exists(os.path.splitext(self.file_path)[0] + ".png"):
                self.thumb_label.setPixmap(pixmap)
            else:
                scaled = pixmap.scaled(90, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb_label.setPixmap(scaled)
        else:
            self.thumb_label.setText("?")


    def _update_border(self):
        """Update the name label color and overlay visibility."""
        if self.selected:
            self.selection_overlay.show()
            self.name_label.setStyleSheet("color: #F08000; font-weight: bold; font-size: 10px;")
        else:
            self.selection_overlay.hide()
            self.name_label.setStyleSheet("color: #ccc; font-size: 10px;")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.browser.select_item(self)

    def set_selected(self, selected):
        if self.selected == selected:
            return
        self.selected = selected
        self._update_border()


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
        
        # For debounced resizing and column tracking
        self.current_cols = 0
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.do_relayout)

        if not os.path.exists(self.current_asset_folder):
            try: os.makedirs(self.current_asset_folder)
            except: pass

        # Main layout: action bar at top (below tabs), then splitter (tree + grid)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Action bar — FACE / FIT / TILE buttons immediately below the tabs
        self.action_bar = QFrame()
        self.action_bar.setFixedHeight(50)
        self.action_bar.setStyleSheet("""
            QFrame {
                background-color: #2a2a2a;
                border-bottom: 1px solid #444;
            }
        """)
        banner_layout = QHBoxLayout(self.action_bar)
        banner_layout.setContentsMargins(10, 5, 10, 5)
        banner_layout.setSpacing(12)

        # Tree panel toggle button (collapsible left column)
        self.tree_toggle_btn = QPushButton("☰")
        self.tree_toggle_btn.setFixedSize(32, 32)
        self.tree_toggle_btn.setToolTip("Toggle Folder Panel")
        self.tree_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #333;
                color: white;
                border: 1px solid #555;
                border-radius: 4px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #444; }
            QPushButton:checked { background-color: #F08000; border: 1px solid #F08000; }
        """)
        self.tree_toggle_btn.setCheckable(True)
        self.tree_toggle_btn.setChecked(False)  # collapsed by default
        self.tree_toggle_btn.clicked.connect(self.toggle_tree_panel)
        banner_layout.addWidget(self.tree_toggle_btn)
        banner_layout.addSpacing(8)

        # Button style (DPI‑aware 9pt font, expanding size policy)
        button_style = """
            QPushButton {
                background-color: #2E7D32; 
                color: white; 
                font: 9pt;
                font-weight: bold;
                padding: 6px 12px; 
                border: 1px solid #1B5E20; 
                border-radius: 5px; 
            }
            QPushButton:hover { background-color: #388E3C; }
            QPushButton:pressed { background-color: #1B5E20; }
            QPushButton:disabled { background-color: #444; color: #888; border: 1px solid #555; }
        """
        face_style = """
            QPushButton {
                background-color: #7B1FA2; 
                color: white; 
                font: 9pt;
                font-weight: bold;
                padding: 6px 12px; 
                border: 1px solid #4A148C; 
                border-radius: 5px; 
            }
            QPushButton:hover { background-color: #8E24AA; }
            QPushButton:pressed { background-color: #4A148C; }
            QPushButton:checked { background-color: #D500F9; border: 1px solid white; }
            QPushButton:disabled { background-color: #444; color: #888; border: 1px solid #555; }
        """

        self.fit_btn = None
        self.tile_btn = None
        self.face_btn = None
        self.add_btn = None

        # Create a container widget for buttons to allow stretching
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(12)

        if self.is_model_tab:
            self.add_btn = QPushButton("Add to Scene")
            self.add_btn.setEnabled(False)
            self.add_btn.setStyleSheet(button_style)
            self.add_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.add_btn.clicked.connect(self.on_add_clicked)
            button_layout.addWidget(self.add_btn)
        else:
            self.face_btn = QPushButton("FACE")
            self.face_btn.setCheckable(True)
            self.face_btn.setEnabled(True)
            self.face_btn.setStyleSheet(face_style)
            self.face_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.face_btn.clicked.connect(self.on_face_mode_clicked)
            self.face_btn.setToolTip("Toggle Face Selection Mode\n"
                                     "Page Up / Page Down rotates the highlighted face's texture")
            button_layout.addWidget(self.face_btn)

            self.fit_btn = QPushButton("FIT")
            self.fit_btn.setEnabled(False)
            self.fit_btn.setStyleSheet(button_style)
            self.fit_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.fit_btn.clicked.connect(lambda: self.perform_main_action(tiled=False))
            button_layout.addWidget(self.fit_btn)

            self.tile_btn = QPushButton("TILE")
            self.tile_btn.setEnabled(False)
            self.tile_btn.setStyleSheet(button_style)
            self.tile_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.tile_btn.clicked.connect(lambda: self.perform_main_action(tiled=True))
            button_layout.addWidget(self.tile_btn)

        # Add stretch on both sides to center the button group, but also let buttons expand
        banner_layout.addStretch()
        banner_layout.addWidget(button_container, stretch=1)
        banner_layout.addStretch()

        main_layout.addWidget(self.action_bar)

        # 2. Splitter for folder tree (left) and asset grid (right)
        self.splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self.splitter, stretch=1)

        # --- Left Pane (Folder Tree) ---
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

        # Set initial splitter sizes (tree gets 180, grid gets the rest)
        self.splitter.setSizes([180, self.width() - 180])

        # Collapse tree panel by default
        self.tree_frame.setVisible(False)

        # Load initial directory and setup resize handling for dynamic columns
        self.load_directory(self.current_asset_folder)
        self.scroll_area.resizeEvent = self._on_resize

    def _on_resize(self, event):
        """Debounced resize handler."""
        self.resize_timer.start(50)  # wait 50ms after last resize
        if event:
            super(self.scroll_area.__class__, self.scroll_area).resizeEvent(event)

    def do_relayout(self):
        """Actually perform relayout after resize debouncing."""
        self.relayout_grid()

    def toggle_tree_panel(self, checked):
        """Show or hide the left folder tree panel."""
        self.tree_frame.setVisible(checked)
        if checked:
            # Restore a reasonable width when expanding
            self.splitter.setSizes([180, max(1, self.width() - 180)])

    def relayout_grid(self, force=False):
        """Re-layout items only when column count changes (reduces flicker)."""
        if not self.items:
            return
        
        # Compute number of columns
        item_width = 110  # 100px width + 10px spacing
        available_width = self.grid_container.width() - 20  # margin
        col_count = max(1, available_width // item_width)
        
        if col_count == self.current_cols and not force:
            return  # no change, keep existing layout
        
        self.current_cols = col_count
        
        # Temporarily disable updates to avoid flicker during re-layout
        self.grid_container.setUpdatesEnabled(False)
        
        # Remove all widgets from grid layout without deleting them
        for i in reversed(range(self.grid_layout.count())):
            widget = self.grid_layout.itemAt(i).widget()
            if widget:
                self.grid_layout.removeWidget(widget)
        
        # Re-add items in new positions
        row, col = 0, 0
        for item in self.items:
            self.grid_layout.addWidget(item, row, col)
            col += 1
            if col >= col_count:
                col = 0
                row += 1
        
        self.grid_container.setUpdatesEnabled(True)

    def go_to_root(self):
        self.load_directory(self.root_path)
        self.tree_view.collapseAll()
        self.tree_view.setCurrentIndex(self.dir_model.index(self.root_path))

    def on_tree_clicked(self, index):
        path = self.dir_model.filePath(index)
        self.load_directory(path)

    def load_directory(self, path):
        self.current_asset_folder = path
        # Clear existing items
        for i in reversed(range(self.grid_layout.count())): 
            self.grid_layout.itemAt(i).widget().setParent(None)
        self.items.clear()
        self.selected_item = None
        self.update_buttons_enabled()

        if not os.path.exists(path): 
            return

        try:
            files = os.listdir(path)
            files.sort()
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in self.extensions:
                    full_path = os.path.join(path, f)
                    item = AssetItem(f, full_path, self, is_model=self.is_model_tab)
                    self.items.append(item)
            # After loading, layout the grid dynamically (force initial layout)
            self.relayout_grid(force=True)
        except Exception as e:
            print(f"Error: {e}")

    def select_item(self, item):
        if self.selected_item: 
            self.selected_item.set_selected(False)
        self.selected_item = item
        self.selected_item.set_selected(True)
        self.update_buttons_enabled()

    def update_buttons_enabled(self):
        has_item = self.selected_item is not None
        if self.is_model_tab and self.add_btn:
            self.add_btn.setEnabled(has_item)
        else:
            if self.fit_btn: 
                self.fit_btn.setEnabled(has_item)
            if self.tile_btn: 
                self.tile_btn.setEnabled(has_item)

    def perform_main_action(self, tiled=False):
        if not self.selected_item:
            return
        if self.is_model_tab:
            self.add_current_model()
        else:
            self.apply_texture(tiled)

    def add_current_model(self):
        if self.editor and self.selected_item:
            self.editor.add_model_to_scene(self.selected_item.file_path, [0,0,0], [1,1,1])

    def apply_texture(self, tiled=False):
        if self.editor and hasattr(self.editor, 'apply_texture_to_brush') and self.selected_item:
            rel_path = os.path.relpath(self.selected_item.file_path, self.root_path)
            rel_path = rel_path.replace('\\', '/')
            self.editor.apply_texture_to_brush(rel_path, tiled=tiled)

    def on_add_clicked(self):
        self.add_current_model()

    def on_face_mode_clicked(self):
        if self.editor and hasattr(self.editor, 'toggle_face_mode'):
            self.editor.toggle_face_mode(self.face_btn.isChecked())


class MapsBrowserTab(QWidget):
    """
    Tab that shows .json map files from /maps and .fiopak packages from /packages.
    Clicking the orange hyperlink toggles between the two views.
    Double-clicking a map loads it; double-clicking a package launches it (plays).
    """
    def __init__(self, maps_folder, packages_folder, editor, parent_browser=None):
        super().__init__()
        self.maps_folder = maps_folder
        self.packages_folder = packages_folder
        self.editor = editor
        self.parent_browser = parent_browser
        self.current_mode = 'maps'   # 'maps' or 'packages'

        # Create folders if they don't exist
        for folder in [self.maps_folder, self.packages_folder]:
            if not os.path.exists(folder):
                try:
                    os.makedirs(folder)
                except Exception as e:
                    print(f"Could not create folder {folder}: {e}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header with clickable link - DPI‑aware font (point size 10)
        self.header_label = QLabel()
        self.header_label.setAlignment(Qt.AlignCenter)
        self.header_label.setStyleSheet("""
            QLabel {
                background-color: #252525;
                padding: 8px;
                border-bottom: 1px solid #3d3d3d;
            }
        """)
        self.header_label.setTextFormat(Qt.RichText)
        self.header_label.setOpenExternalLinks(False)
        self.header_label.linkActivated.connect(self.on_header_link_clicked)
        header_font = QFont()
        header_font.setPointSize(10)
        self.header_label.setFont(header_font)
        layout.addWidget(self.header_label)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget {
                background-color: #2b2b2b;
                color: #ddd;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #3d3d3d;
            }
            QListWidget::item:selected {
                background-color: #F08000;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #3c3c3c;
            }
        """)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.list_widget)

        self.refresh_list()

    def update_header(self):
        if self.current_mode == 'maps':
            self.header_label.setText('Showing: <a href="switch" style="color: #F08000; text-decoration: none;">Maps</a>')
        else:
            self.header_label.setText('Showing: <a href="switch" style="color: #F08000; text-decoration: none;">Packages</a>')

    def on_header_link_clicked(self, link):
        # Toggle mode
        self.current_mode = 'packages' if self.current_mode == 'maps' else 'maps'
        self.refresh_list()

    def refresh_list(self):
        self.list_widget.clear()
        if self.current_mode == 'maps':
            self.update_header()
            folder = self.maps_folder
            extension = '.json'
            if not os.path.isdir(folder):
                return
            files = [f for f in os.listdir(folder) if f.lower().endswith(extension)]
            files.sort()
            for filename in files:
                full_path = os.path.join(folder, filename)
                item = QListWidgetItem(filename)
                item.setData(Qt.UserRole, full_path)
                self.list_widget.addItem(item)
            if not files:
                no_item = QListWidgetItem("No maps found")
                no_item.setFlags(Qt.NoItemFlags)
                self.list_widget.addItem(no_item)
        else:  # packages
            self.update_header()
            folder = self.packages_folder
            extension = '.fiopak'
            if not os.path.isdir(folder):
                no_item = QListWidgetItem("No packages found")
                no_item.setFlags(Qt.NoItemFlags)
                self.list_widget.addItem(no_item)
                return
            files = [f for f in os.listdir(folder) if f.lower().endswith(extension)]
            files.sort()
            for filename in files:
                full_path = os.path.join(folder, filename)
                item = QListWidgetItem(filename)
                item.setData(Qt.UserRole, full_path)
                self.list_widget.addItem(item)
            if not files:
                no_item = QListWidgetItem("No packages found")
                no_item.setFlags(Qt.NoItemFlags)
                self.list_widget.addItem(no_item)

    def on_item_double_clicked(self, item):
        file_path = item.data(Qt.UserRole)
        if not file_path or not self.editor:
            return

        if self.current_mode == 'maps':
            # Check unsaved changes before loading map
            if hasattr(self.editor, 'check_unsaved_changes') and not self.editor.check_unsaved_changes():
                return
            # Load the map using the editor's method
            if hasattr(self.editor, 'load_level_file'):
                self.editor.load_level_file(file_path)
        else:  # packages
            # Launch/play the package exactly like "File > Play Game Package"
            if hasattr(self.editor, 'play_package_from_path'):
                self.editor.play_package_from_path(file_path)
            else:
                print("Editor does not support play_package_from_path. Add that method to MainWindow.")
                if hasattr(self.editor, 'show_toast'):
                    self.editor.show_toast("Cannot launch package: method missing in editor", is_error=True)

    def showEvent(self, event):
        self.refresh_list()
        if self.editor and hasattr(self.editor, 'show_toast'):
            self.editor.show_toast("Double-click to open map or package", duration=2000)
        super().showEvent(event)


class AssetBrowser(QWidget):
    def __init__(self, initial_path, editor=None):
        super().__init__()
        self.editor = editor
        
        # Enforce compact vertical size preference
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        if initial_path.endswith("textures"):
            self.assets_root = os.path.dirname(initial_path)
            self.textures_path = initial_path
        else:
            self.assets_root = initial_path
            self.textures_path = os.path.join(initial_path, "textures")
            
        self.models_path = os.path.join(self.assets_root, "models")

        # Determine maps and packages folders based on editor's root_dir if available
        if self.editor and hasattr(self.editor, 'root_dir'):
            root = self.editor.root_dir
            self.maps_folder = os.path.join(root, 'maps')
            self.packages_folder = os.path.join(root, 'packages')
        else:
            # Fallback: use parent of initial_path as root
            parent = os.path.dirname(initial_path)
            self.maps_folder = os.path.join(parent, 'maps')
            self.packages_folder = os.path.join(parent, 'packages')
        
        for p in [self.textures_path, self.models_path, self.maps_folder, self.packages_folder]:
            if not os.path.exists(p):
                try: os.makedirs(p)
                except: pass

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3d3d3d; background-color: #2b2b2b; }
            QTabBar::tab { background: #1e1e1e; color: #aaa; min-width: 100px; padding: 6px; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
            QTabBar::tab:selected { background: #F08000; color: white; font-weight: bold; }
            QTabBar::tab:hover:!selected { background: #333; }
        """)

        # Create tabs, passing editor reference
        self.tab_textures = AssetBrowserTab(self.textures_path, ['.png', '.jpg', '.jpeg', '.tga', '.bmp'], editor, is_model_tab=False, parent_browser=self)
        self.tabs.addTab(self.tab_textures, "Textures")
        
        self.tab_models = AssetBrowserTab(self.models_path, ['.obj', '.glb'], editor, is_model_tab=True, parent_browser=self)
        self.tabs.addTab(self.tab_models, "Models")

        self.tab_maps = MapsBrowserTab(self.maps_folder, self.packages_folder, editor, parent_browser=self)
        self.tabs.addTab(self.tab_maps, "Maps")

    def get_selected_filepath(self):
        """Return the file path of the currently selected asset, or None."""
        w = self.tabs.currentWidget()
        if isinstance(w, MapsBrowserTab):
            return None
        if w and w.selected_item:
            return w.selected_item.file_path
        return None


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