from engine.asset_archives import (asset_bytes, asset_exists, asset_isdir,
                                   asset_listdir, asset_source)
from engine.asset_qt import load_icon
import io
import os
import sys
import math
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame,
                             QHBoxLayout, QGridLayout, QSplitter, QApplication,
                             QMainWindow, QPushButton, QTreeView,
                             QFileSystemModel, QTabWidget,
                             QSizePolicy, QListWidget, QListWidgetItem, QMenu, QActionGroup)
from PyQt5.QtCore import Qt, QDir, QModelIndex, QRect, QPointF, QSize, QTimer, QFileSystemWatcher
from PyQt5.QtGui import (QPixmap, QColor, QPainter, QFont, QPen, QPolygonF, QIcon, QImage,
                         QImageReader, QStandardItem, QStandardItemModel)
from engine.glb_loader import render_glb_thumbnail
# The Surface Inspector's FACE toggle sets this colour; the INSPECTOR button
# that opens that panel borrows it so the two read as a pair.
from editor.surface_inspector import FACE_BUTTON_STYLE as INSPECTOR_BUTTON_STYLE
from engine.speaker_audio import (AUDIO_EXTENSIONS, SPEAKER_AUDIO_FOLDERS,
                                  list_speaker_audio, speaker_sound_path)

#: Thumbnail edge in pixels, chosen by right-clicking blank grid space.
#: New browsers start at the most compact size; saved choices take precedence.
THUMBNAIL_SIZES = {'medium': 90, 'small': 72, 'smallest': 58}
DEFAULT_THUMBNAIL_SIZE = 'smallest'


def render_obj_thumbnail(filepath, width, height):
    """
    Simple software renderer to generate a wireframe thumbnail from an OBJ file.
    """
    from engine.obj_loader import detect_up_axis, up_axis_tag, z_up_to_y_up
    vertices = []
    faces = []
    up_tag = None
    
    try:
        # Limit processing to avoid freezing on huge files
        max_lines = 10000 
        line_count = 0
        
        data = asset_bytes(filepath) or b''
        with io.StringIO(data.decode('utf-8', 'replace')) as f:
            for line in f:
                line_count += 1
                if line_count > max_lines: break
                
                if line.startswith('#'):
                    up_tag = up_axis_tag(line) or up_tag
                elif line.startswith('v '):
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
    # Drawn the way the editor will place it (engine.obj_loader turns a Z-up
    # model Y-up).
    if detect_up_axis(vertices, up_tag) == 'z':
        vertices = [list(z_up_to_y_up(v)) for v in vertices]

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


class AssetFolderModel(QStandardItemModel):
    """The asset folders under a root, on disk and inside the category
    ``.fiopak`` archives (read in place, never extracted) -- the part of
    ``QFileSystemModel``'s interface the browser uses: :meth:`setRootPath`,
    :meth:`index` of a path and :meth:`filePath` of an index."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = None
        self._items = {}
        self._folders = None

    def setRootPath(self, path):
        self._root = os.path.abspath(path)
        self._folders = None
        self.refresh()

    def _scan(self, folder, out):
        for name in asset_listdir(folder):
            path = os.path.join(folder, name)
            if asset_isdir(path):
                out.append(path)
                self._scan(path, out)
        return out

    def refresh(self):
        """Rebuild when the set of folders changed (a folder added or removed,
        on disk or by a repacked archive)."""
        if self._root is None:
            return
        folders = self._scan(self._root, [])
        if folders == self._folders:
            return
        self._folders = folders
        self.clear()
        self._items = {}
        from engine.asset_qt import load_icon as _icon
        from editor.desktop_style import ICON_DIRECTORY
        icon = _icon(str(ICON_DIRECTORY / 'folder.svg'))
        for path in folders:
            item = QStandardItem(icon, os.path.basename(path))
            item.setEditable(False)
            item.setData(path, Qt.UserRole)
            parent = self._items.get(os.path.normcase(os.path.dirname(path)))
            (parent if parent is not None else self.invisibleRootItem()).appendRow(item)
            self._items[os.path.normcase(path)] = item

    def index(self, *args):
        if len(args) == 1 and isinstance(args[0], str):
            item = self._items.get(os.path.normcase(os.path.abspath(args[0])))
            return item.index() if item is not None else QModelIndex()
        return super().index(*args)

    def filePath(self, index):
        if not index.isValid():
            return self._root
        return index.data(Qt.UserRole)

def texture_info(path, animated=False):
    """``"PNG 512x512"``: a texture's type and pixel size, for its tooltip.

    Read from the file's header, not by decoding it. An animated GIF adds
    "animated"; a file whose size cannot be read gives its type alone.
    """
    ext = os.path.splitext(path)[1].lstrip('.').upper()
    kind = {'JPEG': 'JPG'}.get(ext, ext) or 'Image'
    width = height = 0
    from engine.asset_qt import image_size
    size = image_size(path)
    if size.isValid():
        width, height = size.width(), size.height()
    else:
        try:
            from PIL import Image
            with Image.open(asset_source(path) or path) as image:
                width, height = image.size
        except Exception:
            pass
    text = f"{kind} {width}x{height}" if width > 0 and height > 0 else kind
    return f"{text}, animated" if animated else text


def audio_thumbnail(path, size):
    """A note on a tile, the file type under it: sounds teal, music violet."""
    parts = os.path.abspath(path).replace('\\', '/').split('/')
    is_music = 'assets' in parts and parts[parts.index('assets') + 1:][:1] == ['music']
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(70, 50, 95) if is_music else QColor(40, 80, 85))
    painter = QPainter(pixmap)
    painter.setPen(QColor(230, 230, 230))
    painter.setFont(QFont("Fio UI", size // 3, QFont.Bold))
    painter.drawText(QRect(0, 0, size, size * 3 // 4), Qt.AlignCenter, "\u266b" if is_music else "\u266a")
    painter.setFont(QFont("Fio UI", max(7, size // 9), QFont.Bold))
    painter.setPen(QColor(240, 128, 0))
    ext = os.path.splitext(path)[1].lstrip('.').upper()
    painter.drawText(QRect(0, size * 2 // 3, size, size // 3), Qt.AlignCenter, ext)
    painter.end()
    return pixmap


class AssetItem(QWidget):
    """
    A widget representing a single asset (file) in the grid view.
    Selection is shown by a 3px orange border around the entire item.
    """
    def __init__(self, name, path, browser, is_model=False, is_audio=False,
                 thumb_size=THUMBNAIL_SIZES[DEFAULT_THUMBNAIL_SIZE]):
        super().__init__()
        self.name_text = name
        self.file_path = path
        self.browser = browser
        self.is_model = is_model
        self.is_audio = is_audio
        self.thumb_size = thumb_size
        #: The cached thumbnail (editor.asset_thumbnails): a small QImage,
        #: shared with every tile of the same file, kept so a size change is
        #: a rescale. Never the full-resolution image.
        self._source_image = None

        self.setFixedSize(*self.tile_size(thumb_size))
        # Reduce layout margins so the border has room
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        
        # Thumbnail
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(thumb_size, thumb_size)
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

        # An animated GIF shows its first frame; the orange arrow plays it
        # on the thumbnail (and stops it again).
        self.preview_btn = None
        self._preview_frames = None
        self._preview_index = 0
        self._preview_timer = None
        # A copy: QLabel.pixmap() is the label's own, replaced by a preview.
        current = self.thumb_label.pixmap()
        self._static_thumb = QPixmap(current) if current is not None else None
        animated = False
        if not is_model and not is_audio and path.lower().endswith('.gif'):
            from engine.animated_texture import is_animated_gif
            animated = is_animated_gif(path)
            if animated:
                self._add_preview_button()
        if not is_model and not is_audio:
            self.setToolTip(texture_info(path, animated))

    @staticmethod
    def tile_size(thumb_size):
        """Tile (width, height): the thumbnail plus margins and its name."""
        return thumb_size + 10, thumb_size + 30

    def set_thumbnail_size(self, thumb_size):
        if thumb_size == self.thumb_size:
            return
        self.stop_preview()
        self.thumb_size = thumb_size
        self.setFixedSize(*self.tile_size(thumb_size))
        self.thumb_label.setFixedSize(thumb_size, thumb_size)
        self.selection_overlay.setGeometry(0, 0, self.width(), self.height())
        # Animation frames are scaled for one size; decode again on play.
        self._preview_frames = None
        self._show_thumbnail()
        current = self.thumb_label.pixmap()
        self._static_thumb = QPixmap(current) if current is not None else None
        if self.preview_btn is not None:
            self.preview_btn.move(thumb_size - 22, thumb_size - 22)

    def _add_preview_button(self):
        btn = QPushButton("\u25b6", self.thumb_label)
        btn.setFixedSize(20, 20)
        btn.move(self.thumb_label.width() - 22, self.thumb_label.height() - 22)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("Preview the animation")
        btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 0, 0, 150);
                color: #F08000;
                border: 1px solid #F08000;
                border-radius: 10px;
                font-size: 10px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover { background-color: rgba(240, 128, 0, 60); }
        """)
        btn.clicked.connect(self.toggle_preview)
        btn.show()
        self.preview_btn = btn

    @property
    def previewing(self):
        return self._preview_timer is not None and self._preview_timer.isActive()

    def toggle_preview(self):
        """Play the GIF on the thumbnail, or stop it on its first frame."""
        if self.previewing:
            self.stop_preview()
            return
        if self._preview_frames is None:
            from engine.animated_texture import decode_gif_frames
            try:
                images, seconds = decode_gif_frames(self.file_path)
            except Exception as exc:
                print(f"[AssetBrowser] Could not preview '{self.name_text}': {exc}")
                return
            frames = []
            for image in images:
                qimage = QImage(image.tobytes(), image.width, image.height,
                                image.width * 4, QImage.Format_RGBA8888).copy()
                frames.append(QPixmap.fromImage(qimage).scaled(
                    self.thumb_size, self.thumb_size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._preview_frames = list(zip(frames, seconds))
        if not self._preview_frames:
            return
        self._preview_index = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._next_preview_frame)
        self.preview_btn.setText("\u25a0")
        self.preview_btn.setToolTip("Stop the preview")
        self._show_preview_frame()

    def _show_preview_frame(self):
        pixmap, seconds = self._preview_frames[self._preview_index]
        self.thumb_label.setPixmap(pixmap)
        self._preview_timer.start(max(20, int(seconds * 1000)))

    def _next_preview_frame(self):
        if self._preview_timer is None:
            return
        self._preview_index = (self._preview_index + 1) % len(self._preview_frames)
        self._show_preview_frame()

    def stop_preview(self):
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        if self._static_thumb is not None:
            self.thumb_label.setPixmap(self._static_thumb)
        if self.preview_btn is not None:
            self.preview_btn.setText("\u25b6")
            self.preview_btn.setToolTip("Preview the animation")

    def _load_thumbnail(self):
        from editor.asset_thumbnails import cached, image_thumbnail
        if self.is_audio:
            self._show_thumbnail()
            return
        if self.is_model:
            # 1. A .png sidecar; 2. a wireframe made from the model -- cached
            # like any thumbnail, so a folder visit renders it once.
            thumb_path = os.path.splitext(self.file_path)[0] + ".png"
            if asset_exists(thumb_path):
                image = image_thumbnail(thumb_path)
            else:
                image = cached(self.file_path, self._render_model_thumbnail)
            if image.isNull():
                image = self._model_label_image()
        else:
            image = image_thumbnail(self.file_path)
        self._source_image = None if image.isNull() else image
        self._show_thumbnail()

    def _render_model_thumbnail(self):
        """A wireframe of the model (GLB or OBJ), or None."""
        from editor.asset_thumbnails import THUMBNAIL_EDGE
        ext = os.path.splitext(self.file_path)[1].lower()
        if ext == '.glb':
            return render_glb_thumbnail(self.file_path, THUMBNAIL_EDGE, THUMBNAIL_EDGE)
        if ext == '.obj':
            return render_obj_thumbnail(self.file_path, THUMBNAIL_EDGE, THUMBNAIL_EDGE)
        return None

    def _model_label_image(self):
        """3. The model's type in text, when it cannot be drawn."""
        from editor.asset_thumbnails import THUMBNAIL_EDGE as edge
        image = QImage(edge, edge, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(60, 70, 80))
        painter = QPainter(image)
        painter.setPen(QColor(200, 200, 200))
        painter.setFont(QFont("Fio UI", 16, QFont.Bold))
        ext = os.path.splitext(self.file_path)[1].lower()
        painter.drawText(QRect(0, 0, edge, edge), Qt.AlignCenter, "GLB" if ext == '.glb' else "OBJ")
        painter.end()
        return image

    def _show_thumbnail(self):
        size = self.thumb_size
        image = self._source_image
        if self.is_audio:
            self.thumb_label.setPixmap(audio_thumbnail(self.file_path, size))
        elif image is None:
            self.thumb_label.setText("?")
        elif image.width() <= size and image.height() <= size and max(image.width(), image.height()) == size:
            self.thumb_label.setPixmap(QPixmap.fromImage(image))
        else:
            self.thumb_label.setPixmap(QPixmap.fromImage(image.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)))


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

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and hasattr(self.browser, 'activate_item'):
            self.browser.activate_item(self)

    def set_selected(self, selected):
        if self.selected == selected:
            return
        self.selected = selected
        self._update_border()


def browser_order(name):
    """The order the browser lists a folder in: by name, except blood decal
    images (``blood_*``, gore's own) always last, after the decals you place."""
    return (name.lower().startswith('blood_'), name.casefold())


def side_button_metrics(size_name):
    """``(button size, Tint icon size, DECAL icon size)`` for the Textures
    tab's Tint and DECAL buttons at 'small' or 'large': exactly the main
    toolbar's small or large button (its icon, plus its padding)."""
    from editor.ui import TOOLBAR_ICON_SIZES
    icon = TOOLBAR_ICON_SIZES[size_name == 'large']
    square = QSize(icon, icon)
    return QSize(icon + 4, icon + 8), square, square


class AssetBrowserTab(QWidget):
    #: settings.ini [AssetBrowser] asset_browser_toolbar_size: the Tint and
    #: DECAL buttons, 'small' (the default) or 'large' -- right-click the empty
    #: space beside them. Independent of the toolbar's big_toolbar_buttons.
    SIDE_BUTTON_SETTING = ('AssetBrowser', 'asset_browser_toolbar_size')
    SIDE_BUTTON_SIZES = ('small', 'large')
    ACTION_BAR_HEIGHT = 50
    #: Pixels between the Tint and DECAL buttons (the toolbar's spacing).
    SIDE_BUTTON_GAP = 4

    """
    A single tab content for the Asset Browser (e.g., Textures or Models).
    """
    def __init__(self, root_path, file_extensions, editor=None, is_model_tab=False,
                 parent_browser=None, audio_folders=None):
        super().__init__()
        #: The Audio tab: ``{'sounds': path, 'music': path}``, toggled by the
        #: header link as the Maps tab toggles Maps and Packages.
        self.audio_folders = audio_folders
        self.is_audio_tab = audio_folders is not None
        self.audio_mode = 'sounds'
        self.assign_btn = None
        self.audio_header = None
        self.root_path = root_path
        self.current_asset_folder = root_path
        self.extensions = file_extensions
        self.is_model_tab = is_model_tab
        self.editor = editor
        self.parent_browser = parent_browser
        self.selected_item = None
        self.items = []
        self.thumb_size = THUMBNAIL_SIZES[getattr(parent_browser, 'thumbnail_size', DEFAULT_THUMBNAIL_SIZE)]

        # Filesystem events drive asset refreshes.  There is deliberately no
        # polling timer: adding/removing an asset emits directoryChanged and
        # the browser reloads the visible folder immediately.
        self.asset_watcher = QFileSystemWatcher(self)
        self.asset_watcher.directoryChanged.connect(self._on_asset_directory_changed)
        self._watched_asset_dirs = set()

        # For debounced resizing and column tracking
        self.current_cols = 0
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.do_relayout)

        if not os.path.exists(self.current_asset_folder):
            try: os.makedirs(self.current_asset_folder)
            except OSError: pass

        # Main layout: action bar at top (below tabs), then splitter (tree + grid)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Action bar — the folder toggle and the Surface Inspector button.
        #    Every texture control lives in the Inspector; FIT and TILE used
        #    to sit here and duplicated its Fit and Natural, one baking a
        #    scale the other keeps live.
        self.action_bar = QFrame()
        self.action_bar.setFixedHeight(self.ACTION_BAR_HEIGHT)
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
        self.add_btn = None
        self.inspector_btn = None
        self.tint_btn = None
        self.decal_btn = None
        #: The folder shown before the Decal tool switched to textures/decals.
        self._folder_before_decals = None

        # Create a container widget for buttons to allow stretching.
        # The texture tab keeps INSPECTOR on the left and Tint brush on the
        # far right; the stretch between them follows the browser width.
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(12)

        if self.is_audio_tab:
            # Sounds | Music, toggled like the Maps tab's Maps | Packages.
            self.audio_header = QLabel()
            self.audio_header.setTextFormat(Qt.RichText)
            self.audio_header.setOpenExternalLinks(False)
            self.audio_header.linkActivated.connect(self.toggle_audio_folder)
            self.audio_header.setStyleSheet("color: #ddd; font-size: 10pt;")
            button_layout.addWidget(self.audio_header)
            button_layout.addStretch()
            self.assign_btn = QPushButton("Give to Speaker")
            self.assign_btn.setEnabled(False)
            self.assign_btn.setStyleSheet(button_style)
            self.assign_btn.setToolTip(
                "Set the selected sound as the sound file of every selected Speaker\n"
                "(double-click a sound does the same). Speakers play only from\n"
                "assets/sounds and assets/music.")
            self.assign_btn.clicked.connect(self.assign_to_speakers)
            button_layout.addWidget(self.assign_btn)
            self._update_audio_header()
        elif self.is_model_tab:
            self.add_btn = QPushButton("Add to Scene")
            self.add_btn.setEnabled(False)
            self.add_btn.setStyleSheet(button_style)
            self.add_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.add_btn.clicked.connect(self.on_add_clicked)
            button_layout.addWidget(self.add_btn)
        else:
            self.inspector_btn = QPushButton("INSPECTOR")
            self.inspector_btn.setStyleSheet(INSPECTOR_BUTTON_STYLE)
            self.inspector_btn.setFixedHeight(32)
            self.inspector_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.inspector_btn.setToolTip(
                "Open the Surface Inspector (T)\n"
                "Fit / Natural / Axial projections, shift, scale and rotation,\n"
                "for one face or every face of the selection")
            self.inspector_btn.clicked.connect(self.on_inspector_clicked)
            button_layout.addWidget(self.inspector_btn)

            button_layout.addStretch()

            # Tint and DECAL are one size, side by side (apply_side_button_size).
            self.tint_btn = QPushButton(load_icon("assets/tint.png"), "")
            self.tint_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.tint_btn.setToolTip("Tint the selected brush")
            self.tint_btn.clicked.connect(self.on_tint_clicked)
            # Tint and DECAL sit close together, as toolbar buttons do.
            pair = QHBoxLayout()
            pair.setContentsMargins(0, 0, 0, 0)
            pair.setSpacing(self.SIDE_BUTTON_GAP)
            pair.addWidget(self.tint_btn)
            button_layout.addLayout(pair)

            # The Decal tool: shows textures/decals while it is on, and goes
            # back to the folder you were in when it is turned off.
            self.decal_btn = QPushButton(load_icon("assets/decal_button.png"), "")
            self.decal_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.decal_btn.setCheckable(True)
            self.decal_btn.setToolTip(
                "Decal Tool\n"
                "Shows textures/decals: pick an image, then click a surface in the\n"
                "3D view to place it. Shift-click a decal to select it, drag or\n"
                "nudge it. Click again to go back to the folder you were in.")
            self.decal_btn.clicked.connect(self.on_decal_clicked)
            pair.addWidget(self.decal_btn)
            self.apply_side_button_size(self.side_button_size())

            # Right-click the empty space beside them: Size > Small / Large.
            self._side_button_row = button_container
            button_container.setContextMenuPolicy(Qt.CustomContextMenu)
            button_container.customContextMenuRequested.connect(self._side_button_menu)

        if self.is_model_tab:
            # "Add to Scene" is this tab's primary action and stays centred.
            banner_layout.addStretch()
            banner_layout.addWidget(button_container, stretch=1)
            banner_layout.addStretch()
        else:
            # The Inspector button sits next to the folder toggle at its natural
            # width; the container's own trailing stretch fills the rest of the
            # bar, so nothing centres it and nothing stretches it.
            banner_layout.addWidget(button_container, stretch=1)

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
        
        self.dir_model = AssetFolderModel(self)
        self.dir_model.setRootPath(self.root_path)
        
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
        self.grid_container.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred
        )
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid_layout.setSpacing(10)
        # Right-click blank space between or around assets for thumbnail size.
        self.grid_container.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid_container.customContextMenuRequested.connect(self._show_grid_menu)
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
        item_width = AssetItem.tile_size(self.thumb_size)[0]
        spacing = self.grid_layout.horizontalSpacing()
        margins = self.grid_layout.contentsMargins()
        available_width = self.scroll_area.viewport().width() - margins.left() - margins.right()
        col_count = max(1, (available_width + spacing) // (item_width + spacing))
        
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

    def set_thumbnail_size(self, thumb_size):
        if thumb_size == self.thumb_size:
            return
        self.thumb_size = thumb_size
        for item in self.items:
            item.set_thumbnail_size(thumb_size)
        self.relayout_grid(force=True)

    def _show_grid_menu(self, pos):
        if self.grid_container.childAt(pos) is not None:
            return  # on an asset, not blank space
        menu = QMenu(self)
        group = QActionGroup(menu)
        for name in THUMBNAIL_SIZES:
            action = menu.addAction(name.capitalize())
            action.setCheckable(True)
            action.setChecked(THUMBNAIL_SIZES[name] == self.thumb_size)
            group.addAction(action)
            action.triggered.connect(lambda _checked, name=name: self._choose_thumbnail_size(name))
        menu.exec_(self.grid_container.mapToGlobal(pos))

    def _choose_thumbnail_size(self, name):
        if hasattr(self.parent_browser, 'set_thumbnail_size'):
            self.parent_browser.set_thumbnail_size(name)
        else:
            self.set_thumbnail_size(THUMBNAIL_SIZES[name])

    def go_to_root(self):
        self.load_directory(self.root_path)
        self.tree_view.collapseAll()
        self.tree_view.setCurrentIndex(self.dir_model.index(self.root_path))

    def on_tree_clicked(self, index):
        path = self.dir_model.filePath(index)
        self.load_directory(path)

    def _watch_asset_directory(self, path):
        """Watch *path* for real filesystem changes; never poll it."""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return
        if path not in self._watched_asset_dirs:
            if self.asset_watcher.addPath(path):
                self._watched_asset_dirs.add(path)

    def _on_asset_directory_changed(self, path):
        """Refresh the visible folder after an OS filesystem event."""
        path = os.path.abspath(path)
        # QFileSystemWatcher removes a watched directory when it disappears.
        self._watched_asset_dirs.discard(path)
        self._watch_asset_directory(path)

        assets_dir = self._assets_dir()
        if assets_dir is not None and assets_dir == path:
            # A .fiopak added, replaced or removed: index the user packs again
            # (engine.asset_archives), then show what they now hold.
            from engine.asset_archives import refresh_asset_archives
            refresh_asset_archives(assets_dir)
            self.dir_model.refresh()
            self.load_directory(self.root_path if self.is_audio_tab
                                else self.current_asset_folder)
            return
        self.dir_model.refresh()
        if os.path.abspath(self.current_asset_folder) == path:
            self.load_directory(path)

    def _assets_dir(self):
        """The project's ``assets`` folder, where user ``.fiopak`` packs live."""
        root = getattr(self.editor, 'root_dir', None)
        if root:
            return os.path.abspath(os.path.join(root, 'assets'))
        parent = os.path.dirname(os.path.abspath(self.root_path))
        return parent if os.path.basename(parent) == 'assets' else None

    def load_directory(self, path):
        archive_path = self._active_audio_archive()
        if archive_path:
            path = self.root_path
        self.current_asset_folder = path
        self._watch_asset_directory(self.root_path)
        self._watch_asset_directory(path)
        assets_dir = self._assets_dir()
        if assets_dir is not None:
            # New or changed .fiopak packs land here, beside the folders.
            self._watch_asset_directory(assets_dir)
        # Clear existing items
        for i in reversed(range(self.grid_layout.count())): 
            self.grid_layout.itemAt(i).widget().setParent(None)
        self.items.clear()
        self.selected_item = None
        self.update_buttons_enabled()

        if archive_path:
            self.tree_toggle_btn.setChecked(False)
            self.tree_toggle_btn.setEnabled(False)
            self.tree_toggle_btn.setToolTip(
                "Archive audio is listed across subfolders; the folder tree is unavailable."
            )
            self.tree_frame.setVisible(False)
            project_root = os.path.abspath(self.editor.root_dir)
            prefix = f"assets/{self.audio_mode}/"
            try:
                members = list_speaker_audio(project_root, self.audio_mode)
            except Exception as exc:
                print(f"[AssetBrowser] Could not list {self.audio_mode}.fiopak: {exc}")
                members = []
            for member in members:
                try:
                    relative = member[len(prefix):]
                    full_path = os.path.join(project_root, *member.split("/"))
                    item = AssetItem(relative, full_path, self, is_audio=True,
                                     thumb_size=self.thumb_size)
                    self.items.append(item)
                except Exception as exc:
                    print(f"[AssetBrowser] Skipped '{member}': {exc}")
            self.relayout_grid(force=True)
            return

        if self.is_audio_tab:
            self.tree_toggle_btn.setEnabled(True)
            self.tree_toggle_btn.setToolTip("Toggle Folder Panel")
        if not asset_isdir(path):
            return

        try:
            # Loose files and the category archive's members, read in place.
            files = sorted(asset_listdir(path), key=browser_order)
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in self.extensions:
                    full_path = os.path.join(path, f)
                    # Per-item guard: a single unreadable asset (bad model, odd
                    # image) must not abort loading the rest of the folder. The
                    # skipped file is named so the failure stays diagnosable.
                    try:
                        item = AssetItem(f, full_path, self, is_model=self.is_model_tab,
                                         is_audio=self.is_audio_tab, thumb_size=self.thumb_size)
                        self.items.append(item)
                    except Exception as e:
                        print(f"[AssetBrowser] Skipped '{f}': {e}")
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
        # Only the model and audio tabs have a button that needs a selection.
        # The Inspector button opens a panel and so is always available.
        if self.is_model_tab and self.add_btn:
            self.add_btn.setEnabled(self.selected_item is not None)
        if self.assign_btn is not None:
            self.assign_btn.setEnabled(self.selected_item is not None)

    # -- Audio tab ---------------------------------------------------------

    def _active_audio_archive(self):
        """Return the active sounds/music archive path, if one exists."""
        if not self.is_audio_tab or self.editor is None:
            return None
        project_root = getattr(self.editor, "root_dir", None)
        if not project_root:
            return None
        path = os.path.join(project_root, "assets", f"{self.audio_mode}.fiopak")
        return path if os.path.isfile(path) else None

    def _update_audio_header(self):
        label = "Sounds" if self.audio_mode == 'sounds' else "Music"
        self.audio_header.setText(
            f'Showing: <a href="switch" style="color: #F08000; text-decoration: none;">{label}</a>')

    def toggle_audio_folder(self, _link=None):
        """Switch the Audio tab between assets/sounds and assets/music."""
        self.set_audio_folder('music' if self.audio_mode == 'sounds' else 'sounds')

    def set_audio_folder(self, mode):
        if not self.is_audio_tab or mode not in self.audio_folders:
            return
        self.audio_mode = mode
        self.root_path = self.audio_folders[mode]
        os.makedirs(self.root_path, exist_ok=True)
        self.dir_model.setRootPath(self.root_path)
        self.tree_view.setRootIndex(self.dir_model.index(self.root_path))
        self.home_btn.setText(f"🏠 {mode.capitalize()}")
        self.home_btn.setToolTip(f"Go to root {mode} folder")
        self._update_audio_header()
        self.load_directory(self.root_path)

    def selected_speaker_sound(self):
        """The selected file as a Speaker's sound_file, or None."""
        if self.selected_item is None or self.editor is None:
            return None
        rel = os.path.relpath(self.selected_item.file_path, self.editor.root_dir)
        rel = rel.replace('\\', '/')
        return rel if speaker_sound_path(rel) == rel else None

    def activate_item(self, item):
        if self.is_audio_tab:
            self.select_item(item)
            self.assign_to_speakers()

    def assign_to_speakers(self):
        """Give every selected Speaker the selected sound (one undo step)."""
        editor = self.editor
        if editor is None:
            return 0
        sound = self.selected_speaker_sound()
        if sound is None:
            return 0
        from editor.things import Speaker
        speakers = [o for o in (editor.state.selected_objects or [])
                    if isinstance(o, Speaker)]
        if not speakers:
            editor.show_toast("Select a Speaker to give it this sound")
            return 0
        editor.save_state()
        for speaker in speakers:
            speaker.properties['sound_file'] = sound
        editor.state.mark_world_changed(speakers)
        editor.update_all_ui()
        editor.show_toast(f"Speaker sound: {os.path.basename(sound)}")
        return len(speakers)

    def add_current_model(self):
        if self.editor and self.selected_item:
            self.editor.add_model_to_scene(self.selected_item.file_path, [0,0,0], [1,1,1])

    def on_add_clicked(self):
        self.add_current_model()

    def on_inspector_clicked(self):
        """Open (or close) the Surface Inspector."""
        self.editor.toggle_surface_inspector()

    # -- the Tint / DECAL button size ------------------------------------------

    def _config(self):
        return getattr(self.editor, 'config', None)

    def side_button_size(self):
        """'small' or 'large', from settings.ini (small when unset or odd)."""
        config = self._config()
        value = (config.get(*self.SIDE_BUTTON_SETTING, fallback='small')
                 if config is not None else 'small')
        value = str(value).strip().lower()
        return value if value in self.SIDE_BUTTON_SIZES else 'small'

    def apply_side_button_size(self, size_name):
        button, tint_icon, decal_icon = side_button_metrics(size_name)
        self.tint_btn.setIconSize(tint_icon)
        self.decal_btn.setIconSize(decal_icon)
        for widget in (self.tint_btn, self.decal_btn):
            widget.setFixedSize(button)
        # The bar grows to hold large buttons (and keeps its usual height).
        margins = self.action_bar.layout().contentsMargins()
        self.action_bar.setFixedHeight(max(
            self.ACTION_BAR_HEIGHT, button.height() + margins.top() + margins.bottom() + 4))

    def set_side_button_size(self, size_name):
        """Resize the Tint and DECAL buttons and keep it in settings.ini."""
        size_name = size_name if size_name in self.SIDE_BUTTON_SIZES else 'small'
        self.apply_side_button_size(size_name)
        config = self._config()
        if config is not None:
            section, key = self.SIDE_BUTTON_SETTING
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, size_name)
            if hasattr(self.editor, 'save_config'):
                self.editor.save_config()

    def _side_button_menu(self, pos):
        """Right-click on the row's empty space (not on a button): Size."""
        row = self._side_button_row
        if isinstance(row.childAt(pos), QPushButton):
            return
        menu = self.side_button_menu()
        menu.exec_(row.mapToGlobal(pos))

    def side_button_menu(self):
        menu = QMenu(self)
        menu.addSection("Size")
        current = self.side_button_size()
        group = QActionGroup(menu)
        for name in self.SIDE_BUTTON_SIZES:
            action = menu.addAction(name.capitalize())
            action.setCheckable(True)
            action.setChecked(name == current)
            group.addAction(action)
            action.triggered.connect(lambda _c=False, n=name: self.set_side_button_size(n))
        return menu

    def on_decal_clicked(self, checked):
        """The DECAL button: the Decal tool on (and textures/decals shown) or off."""
        if self.editor is not None and hasattr(self.editor, 'toggle_decal_tool'):
            self.editor.toggle_decal_tool(bool(checked))
        else:
            self.show_decal_folder(bool(checked))

    def decals_path(self):
        return os.path.join(self.root_path, 'decals')

    def show_decal_folder(self, on):
        """Show textures/decals (remembering the folder shown before), or go
        back to that folder. The DECAL button follows."""
        if self.decal_btn is not None and self.decal_btn.isChecked() != bool(on):
            self.decal_btn.blockSignals(True)
            self.decal_btn.setChecked(bool(on))
            self.decal_btn.blockSignals(False)
        decals = os.path.abspath(self.decals_path())
        if on:
            if os.path.abspath(self.current_asset_folder) != decals:
                self._folder_before_decals = self.current_asset_folder
            self._show_folder(decals)
        elif self._folder_before_decals is not None:
            previous, self._folder_before_decals = self._folder_before_decals, None
            if os.path.abspath(self.current_asset_folder) == decals:
                self._show_folder(previous)

    def _show_folder(self, path):
        self.load_directory(path)
        index = self.dir_model.index(path)
        if index.isValid():
            self.tree_view.setCurrentIndex(index)

    def on_tint_clicked(self):
        """Tint the selected brush."""
        self.editor.tint_selected_brush()


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
            # _SHOWCASE.json is a reserved maps-browser entry: when present,
            # keep it at the very top regardless of normal filename sorting.
            showcase = "_SHOWCASE.json"
            if showcase in files:
                files.remove(showcase)
                files.insert(0, showcase)
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
            if not self.editor.check_unsaved_changes():
                return
            # Load the map using the editor's method
            self.editor.load_level_file(file_path)
        else:  # packages
            # Launch/play the package exactly like "File > Play Game Package"
            self.editor.play_package_from_path(file_path)

    def showEvent(self, event):
        self.refresh_list()
        if self.editor:
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
        if self.editor:
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
                except OSError: pass

        config = getattr(editor, 'config', None)
        saved = config.get('AssetBrowser', 'thumbnail_size', fallback=DEFAULT_THUMBNAIL_SIZE) if config else None
        self.thumbnail_size = saved if saved in THUMBNAIL_SIZES else DEFAULT_THUMBNAIL_SIZE

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
        self.tab_textures = AssetBrowserTab(self.textures_path, ['.png', '.jpg', '.jpeg', '.tga', '.bmp', '.gif'], editor, is_model_tab=False, parent_browser=self)
        self.tabs.addTab(self.tab_textures, "Textures")
        
        self.tab_models = AssetBrowserTab(self.models_path, ['.obj', '.glb'], editor, is_model_tab=True, parent_browser=self)
        self.tabs.addTab(self.tab_models, "Models")

        self.tab_maps = MapsBrowserTab(self.maps_folder, self.packages_folder, editor, parent_browser=self)
        self.tabs.addTab(self.tab_maps, "Maps")

        # Speakers play only from these folders (engine.speaker_audio).
        audio_folders = {name: os.path.join(self.assets_root, name)
                         for name in SPEAKER_AUDIO_FOLDERS}
        self.tab_audio = AssetBrowserTab(audio_folders['sounds'], list(AUDIO_EXTENSIONS),
                                         editor, parent_browser=self,
                                         audio_folders=audio_folders)
        self.tabs.addTab(self.tab_audio, "Audio")

    def set_thumbnail_size(self, name):
        """Resize every grid's thumbnails to ``name`` and remember it."""
        if name not in THUMBNAIL_SIZES:
            return
        self.thumbnail_size = name
        for tab in (self.tab_textures, self.tab_models, self.tab_audio):
            tab.set_thumbnail_size(THUMBNAIL_SIZES[name])
        config = getattr(self.editor, 'config', None)
        if config is not None:
            if not config.has_section('AssetBrowser'):
                config.add_section('AssetBrowser')
            config['AssetBrowser']['thumbnail_size'] = name
            save = getattr(self.editor, 'save_config', None)
            if save is not None:
                save()

    def get_selected_filepath(self):
        """Return the file path of the currently selected asset, or None."""
        w = self.tabs.currentWidget()
        # Maps and sounds are not textures: the texture tools read this.
        if isinstance(w, MapsBrowserTab) or getattr(w, 'is_audio_tab', False):
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
