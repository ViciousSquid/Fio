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
                             QSizePolicy, QListWidget, QListWidgetItem, QMenu, QActionGroup,
                             QListView, QStyledItemDelegate, QAbstractItemView)
from collections import OrderedDict
from PyQt5.QtCore import (Qt, QDir, QModelIndex, QRect, QRectF, QPoint, QPointF, QSize, QTimer,
                          QFileSystemWatcher,
                          QAbstractListModel, QObject, QRunnable, QThreadPool, pyqtSignal)
from PyQt5.QtGui import (QPixmap, QColor, QPainter, QFont, QFontMetrics, QPen, QPolygonF, QIcon,
                         QImage, QImageReader, QLinearGradient, QRegion, QStandardItem,
                         QStandardItemModel)
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


class AssetItem:
    """One asset in a tab's grid: its name, path, kind and shape -- a row of
    the tab's model (:class:`AssetGridModel`), painted by
    :class:`AssetDelegate` in the slots :class:`AssetGridView` gives it.

    No widget and no pixels: a folder of 20,000 textures is 20,000 of these,
    and only the tiles on screen ever ask for a thumbnail
    (:class:`ThumbnailLoader`, which decodes them off the UI thread).
    """
    __slots__ = ('name_text', 'file_path', 'is_model', 'is_audio', 'animated', 'span',
                 '_tooltip')

    def __init__(self, name, path, is_model=False, is_audio=False):
        self.name_text = name
        self.file_path = path
        self.is_model = is_model
        self.is_audio = is_audio
        #: An animated GIF (it gets the preview arrow): None until its
        #: thumbnail has been read, which says.
        self.animated = None if self.is_gif else False
        #: Grid slots (across, down): (2, 1) for a wide texture, (1, 2) for
        #: a tall one (editor.asset_thumbnails.spans), from its header.
        self.span = (1, 1)
        if self.is_image:
            from editor.asset_thumbnails import known_size, spans
            size = known_size(path)
            if size is not None:
                self.span = spans(*size)
        self._tooltip = None

    @property
    def is_gif(self):
        return not self.is_model and not self.is_audio and self.file_path.lower().endswith('.gif')

    @property
    def is_image(self):
        return not self.is_model and not self.is_audio

    def tooltip(self):
        """The full name -- a small tile's label is cut short -- and, for a
        texture, its type and size, read from its header when first asked
        (on hover)."""
        if self._tooltip is None:
            if not self.is_image:
                self._tooltip = self.name_text
            else:
                if self.animated is None:
                    from engine.animated_texture import is_animated_gif
                    self.animated = bool(is_animated_gif(self.file_path))
                self._tooltip = (f"{self.name_text}\n"
                                 f"{texture_info(self.file_path, self.animated)}")
        return self._tooltip

    @staticmethod
    def tile_size(thumb_size, span=(1, 1)):
        """Tile (width, height): Metro tiles -- the picture is the tile, the
        name lies over its foot -- and a tile of two slots also covers the
        gutter between them."""
        across, down = span
        return (across * thumb_size + (across - 1) * TILE_SPACING,
                down * thumb_size + (down - 1) * TILE_SPACING)

    def __repr__(self):
        return f"AssetItem({self.name_text!r})"


#: The gutter between tiles, across and down: tight, as Metro tiles are.
TILE_SPACING = 4
#: The name strip laid over a tile's foot.
_LABEL_HEIGHT = 14
#: The play badge on an animated texture: a circle centred on the tile,
#: this share of its shorter side across (never under the minimum).
_PREVIEW_SHARE = 0.42
_PREVIEW_MIN = 20


def _thumb_rect(cell):
    """The picture area of the tile in *cell*: the whole tile."""
    return QRect(cell)


def thumb_box(thumb_size, span):
    """(width, height) of the picture area of a tile of *span* slots."""
    return AssetItem.tile_size(thumb_size, span)


def preview_button_rect(cell):
    """The play badge of the tile in *cell* (view coordinates): a circle
    centred over the texture."""
    side = max(_PREVIEW_MIN, int(min(cell.width(), cell.height()) * _PREVIEW_SHARE))
    return QRect(cell.center().x() - side // 2 + 1, cell.center().y() - side // 2 + 1, side, side)


class _SizeJob(QRunnable):
    """Read the sizes of a folder's images from their headers, in display
    order, on a pool thread; report them in batches so the grid gives wide
    and tall textures their slots, the top of the folder first."""

    BATCH = 256

    def __init__(self, paths, generation, signals):
        super().__init__()
        self.paths, self.generation, self.signals = paths, generation, signals
        self.cancelled = False
        #: Set when run() returns: until then the loader holds this job (it
        #: is not auto-deleted, so dropping it while it runs would free it
        #: under the pool thread).
        self.done = False
        self.setAutoDelete(False)

    def run(self):
        from editor.asset_thumbnails import read_size
        try:
            batch = []
            for path in self.paths:
                if self.cancelled:
                    return
                try:
                    size = read_size(path)
                except Exception:
                    size = (0, 0)
                batch.append((path, size))
                if len(batch) >= self.BATCH:
                    self.signals.sizes.emit(self.generation, batch)
                    batch = []
            if batch and not self.cancelled:
                self.signals.sizes.emit(self.generation, batch)
            if not self.cancelled:
                self.signals.sizes.emit(self.generation, None)     # finished
        finally:
            self.done = True


def _render_model_thumbnail(path):
    """A wireframe of the model (GLB or OBJ), or None. UI thread: it paints
    on a QPixmap."""
    from editor.asset_thumbnails import THUMBNAIL_EDGE
    ext = os.path.splitext(path)[1].lower()
    if ext == '.glb':
        return render_glb_thumbnail(path, THUMBNAIL_EDGE, THUMBNAIL_EDGE)
    if ext == '.obj':
        return render_obj_thumbnail(path, THUMBNAIL_EDGE, THUMBNAIL_EDGE)
    return None


def _model_label_image(path):
    """A model's type in text, when it cannot be drawn."""
    from editor.asset_thumbnails import THUMBNAIL_EDGE as edge
    image = QImage(edge, edge, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(60, 70, 80))
    painter = QPainter(image)
    painter.setPen(QColor(200, 200, 200))
    painter.setFont(QFont("Fio UI", 16, QFont.Bold))
    ext = os.path.splitext(path)[1].lower()
    painter.drawText(QRect(0, 0, edge, edge), Qt.AlignCenter, "GLB" if ext == '.glb' else "OBJ")
    painter.end()
    return image


class _DecodeJob(QRunnable):
    """Decode one image asset's thumbnail on a pool thread (QImage only:
    QPixmap belongs to the UI thread)."""

    def __init__(self, path, gif, signals):
        super().__init__()
        self.path, self.gif, self.signals = path, gif, signals
        self.setAutoDelete(True)

    def run(self):
        from editor.asset_thumbnails import decode_image
        from engine.asset_archives import asset_stamp
        try:
            stamp = asset_stamp(self.path)
            image = decode_image(self.path) if stamp is not None else QImage()
            animated = False
            if self.gif and stamp is not None:
                from engine.animated_texture import is_animated_gif
                animated = bool(is_animated_gif(self.path))
        except Exception as exc:                          # never lose a tile
            print(f"[AssetBrowser] Could not read '{self.path}': {exc}")
            stamp, image, animated = None, QImage(), False
        self.signals.ready.emit(self.path, stamp, image, animated)


class _DecodeSignals(QObject):
    #: path, stamp, QImage, animated
    ready = pyqtSignal(str, object, object, bool)
    #: generation, [(path, (width, height))]
    sizes = pyqtSignal(int, object)


class ThumbnailLoader(QObject):
    """Thumbnails for one tab's tiles, on demand and off the UI thread.

    :meth:`pixmap` answers at once: the tile's pixmap when its thumbnail is
    known (``editor.asset_thumbnails``' cache, keyed by path and file
    stamp), else None -- and the image is then decoded on a pool thread,
    most recent request first, and the tile repainted when it arrives. Only
    tiles the view paints ask, so a huge folder costs what is on screen.
    Model wireframes are drawn on the UI thread (they paint a QPixmap), one
    an event-loop turn. Pixmaps are kept per size, a bounded few hundred.
    """

    #: Pixmaps kept, by (cached image, size): tiles on screen, plus room
    #: to scroll back.
    PIXMAP_LIMIT = 768
    _pool = None
    #: Whether a GIF is animated (file path -> bool), learnt when it is
    #: decoded; shared by every tab, as the thumbnail cache is.
    animated = {}

    changed = pyqtSignal(str)
    #: [(path, (width, height))] read for the folder shown
    sizes_read = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmaps = OrderedDict()            # (path, size) -> QPixmap
        self._pending = set()
        self._wireframes = []                    # model paths to draw, newest last
        self._wireframe_timer = QTimer(self)
        self._wireframe_timer.setSingleShot(True)
        self._wireframe_timer.timeout.connect(self._draw_next_wireframe)
        # Pool jobs can finish after the tab (and this loader) is destroyed.
        # Let their Python references keep the signal object alive: parenting
        # it to the loader deletes its C++ object underneath a running worker.
        # Qt disconnects the deleted receiver automatically.
        self._signals = _DecodeSignals()
        self._signals.ready.connect(self._decoded)
        self._signals.sizes.connect(self._sizes)
        self._size_job = None
        self._size_jobs = []                     # started, kept until done
        self._generation = 0
        self._priority = 0
        self._labels = {}                        # model path -> its "GLB"/"OBJ" image

    @classmethod
    def pool(cls):
        if cls._pool is None:
            cls._pool = QThreadPool()
            cls._pool.setMaxThreadCount(max(2, QThreadPool.globalInstance().maxThreadCount() - 1))
        return cls._pool

    # -- answers ----------------------------------------------------------
    def pixmap(self, item, box):
        """The tile's pixmap fitted in *box* ``(width, height)`` -- its own
        shape kept -- or None while it is being made.

        Keyed by the cached image itself, which the cache replaces when the
        file's stamp changes: an edited texture shows its new picture."""
        image = self._image(item)
        if image is None:
            self._request(item)
            return None
        key = (image.cacheKey(), box)
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            self._pixmaps.move_to_end(key)
            return pixmap
        return self._keep(key, image, box)

    def image_now(self, item):
        """The tile's thumbnail image, made on the spot if need be (tests,
        a capture that must be complete)."""
        from editor.asset_thumbnails import cached, image_thumbnail
        source = self._source(item)
        if source is None:
            return QImage()
        if item.is_model and source == item.file_path:
            image = cached(source, lambda: _render_model_thumbnail(source))
        else:
            image = image_thumbnail(source)
        if item.is_gif and item.animated is None:
            from engine.animated_texture import is_animated_gif
            item.animated = bool(is_animated_gif(item.file_path))
        return image

    def _source(self, item):
        """The file a tile's thumbnail comes from: the image itself, a
        model's .png sidecar, or the model (drawn as a wireframe)."""
        if item.is_audio:
            return None
        if item.is_model:
            sidecar = os.path.splitext(item.file_path)[0] + ".png"
            return sidecar if asset_exists(sidecar) else item.file_path
        return item.file_path

    def _image(self, item):
        from editor.asset_thumbnails import lookup
        source = self._source(item)
        image = lookup(source) if source is not None else None
        if image is not None and item.is_gif and item.animated is None:
            item.animated = self.animated.get(item.file_path)
            if item.animated is None:
                return None                      # decode again to learn it
        if image is not None and image.isNull() and item.is_model:
            label = self._labels.get(item.file_path)
            if label is None:
                label = self._labels[item.file_path] = _model_label_image(item.file_path)
            return label
        return image

    def _keep(self, key, image, box):
        if image.isNull():
            pixmap = QPixmap()
        else:
            fitted = image.size().scaled(QSize(*box), Qt.KeepAspectRatio)
            scaled = image if fitted == image.size() else image.scaled(
                fitted, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            pixmap = QPixmap.fromImage(scaled)
        self._pixmaps[key] = pixmap
        while len(self._pixmaps) > self.PIXMAP_LIMIT:
            self._pixmaps.popitem(last=False)
        return pixmap

    # -- making -----------------------------------------------------------
    def _request(self, item):
        source = self._source(item)
        if source is None or item.file_path in self._pending:
            return
        self._pending.add(item.file_path)
        if item.is_model and source == item.file_path:
            self._wireframes.append(item.file_path)
            if not self._wireframe_timer.isActive():
                self._wireframe_timer.start(0)
            return
        # Newest first: what has just scrolled into view comes before what
        # scrolled past while it waited.
        self._priority += 1
        job = _DecodeJob(source, item.is_gif, self._signals)
        job.item_path = item.file_path
        self.pool().start(job, min(self._priority, 2 ** 30))
        if source != item.file_path:
            self._sidecars = getattr(self, '_sidecars', {})
            self._sidecars[source] = item.file_path

    def _decoded(self, path, stamp, image, animated):
        from editor import asset_thumbnails
        asset_thumbnails.stats['decoded'] += 1
        asset_thumbnails.store(path, stamp, image)
        item_path = getattr(self, '_sidecars', {}).pop(path, path)
        if path.lower().endswith('.gif'):
            self.animated[item_path] = animated
        self._pending.discard(item_path)
        self.changed.emit(item_path)

    def _draw_next_wireframe(self):
        from editor import asset_thumbnails
        from engine.asset_archives import asset_stamp
        if not self._wireframes:
            return
        path = self._wireframes.pop()            # newest first
        try:
            made = _render_model_thumbnail(path)
        except Exception as exc:
            print(f"[AssetBrowser] Could not draw '{path}': {exc}")
            made = None
        asset_thumbnails.stats['decoded'] += 1
        asset_thumbnails.store(path, asset_stamp(path), made)
        self._pending.discard(path)
        self.changed.emit(path)
        if self._wireframes:
            self._wireframe_timer.start(0)

    def read_sizes(self, paths):
        """Read *paths*' sizes from their headers, in order, off the UI
        thread; :attr:`sizes_read` reports them in batches."""
        self._generation += 1
        self._size_jobs = [job for job in self._size_jobs if not job.done]
        if not paths:
            return
        self._size_job = _SizeJob(list(paths), self._generation, self._signals)
        self._size_jobs.append(self._size_job)
        self.pool().start(self._size_job, 2 ** 30 + 1)     # before any decode

    def _sizes(self, generation, batch):
        if generation != self._generation:
            return
        if batch is None:                        # the read is finished
            self._size_job = None
            return
        self.sizes_read.emit(batch)

    def cancel(self):
        """A new folder: drop decodes not yet started (those running finish
        and are cached), the size read, and the wireframes still to draw."""
        for job in self._size_jobs:
            job.cancelled = True
            if not job.done and self.pool().tryTake(job):
                job.done = True                  # never started: safe to drop
        self._size_job = None
        self._generation += 1
        self.pool().clear()
        self._pending.clear()
        self._wireframes.clear()
        if hasattr(self, '_sidecars'):
            self._sidecars.clear()

    def busy(self):
        return bool(self._pending) or self._size_job is not None

    def finish(self, timeout=10.0):
        """Wait until every requested thumbnail is made (tests, captures)."""
        import time
        deadline = time.monotonic() + timeout
        app = QApplication.instance()
        while self.busy() and time.monotonic() < deadline:
            self.pool().waitForDone(20)
            if app is not None:
                app.processEvents()
        return not self.busy()

    def drop_pixmaps(self):
        self._pixmaps.clear()


class AssetGridModel(QAbstractListModel):
    """A tab's assets, one row each (:class:`AssetItem`)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self._rows = {}                          # file path -> rows

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = entries
        self._rows = {}
        for row, item in enumerate(entries):
            self._rows.setdefault(item.file_path, []).append(row)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.entries[index.row()]
        if role == Qt.DisplayRole:
            return item.name_text
        if role == Qt.ToolTipRole:
            return item.tooltip() or None
        if role == Qt.UserRole:
            return item
        return None

    def row_of(self, item):
        for row in self._rows.get(item.file_path, ()):
            if self.entries[row] is item:
                return row
        return -1

    def refresh_path(self, path):
        for row in self._rows.get(path, ()):
            index = self.index(row)
            self.dataChanged.emit(index, index)


class AssetDelegate(QStyledItemDelegate):
    """Paints a Metro tile: flat and square-cornered, the picture filling
    it in its own shape (never stretched), the name over its foot, a thin
    orange frame when selected, the preview arrow on an animated GIF. A wide
    or tall texture's tile covers two slots (``option.rect`` is all of it)."""

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self._audio = {}

    def sizeHint(self, option, index):
        item = index.data(Qt.UserRole)
        return QSize(*AssetItem.tile_size(self.tab.thumb_size, item.span if item else (1, 1)))

    def paint(self, painter, option, index):
        tab = self.tab
        item = index.data(Qt.UserRole)
        if item is None:
            return
        cell = QRect(option.rect)
        selected = item is tab.selected_item
        painter.save()
        # A flat tile, square-cornered, the picture filling it -- its own
        # shape kept and centred, so a 4:3 texture shows thin bars.
        painter.fillRect(cell, QColor('#333'))
        pixmap = tab.tile_pixmap(item)
        if pixmap is None:
            pass                                 # being made: the bare tile
        elif pixmap.isNull():
            painter.setPen(QColor('#ccc'))       # unreadable
            painter.drawText(cell, Qt.AlignCenter, "?")
        else:
            x = cell.x() + (cell.width() - pixmap.width()) // 2
            y = cell.y() + (cell.height() - pixmap.height()) // 2
            painter.drawPixmap(x, y, pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if item.animated and not tab.is_previewing(item):
            # Animated: a play arrow in a circle over it, until it plays.
            self._paint_play_badge(painter, preview_button_rect(cell))
        # The name over the tile's foot, on a shade so it reads on any picture.
        strip = QRect(cell.x(), cell.bottom() + 1 - _LABEL_HEIGHT, cell.width(), _LABEL_HEIGHT)
        shade = QLinearGradient(0, strip.top() - 4, 0, strip.bottom())
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 190))
        painter.fillRect(strip.adjusted(0, -4, 0, 0), shade)
        font = QFont(painter.font())
        font.setPixelSize(10)
        font.setBold(selected)
        painter.setFont(font)
        painter.setPen(QColor('#F08000') if selected else QColor('#eee'))
        text_rect = strip.adjusted(4, 0, -3, 0)
        text = QFontMetrics(font).elidedText(item.name_text, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text)
        if selected:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor('#F08000'), 2))
            painter.drawRect(cell.adjusted(1, 1, -1, -1))
        painter.restore()

    @staticmethod
    def _paint_play_badge(painter, rect):
        """A play arrow in a circle: a dark disc, an orange ring and arrow."""
        painter.setBrush(QColor(0, 0, 0, 150))
        ring = max(1.5, rect.width() / 16)
        painter.setPen(QPen(QColor('#F08000'), ring))
        painter.drawEllipse(QRectF(rect).adjusted(ring / 2, ring / 2, -ring / 2, -ring / 2))
        # The arrow, nudged right of centre as a play glyph sits.
        r = rect.width() * 0.22
        cx, cy = rect.center().x() + r * 0.18 + 0.5, rect.center().y() + 0.5
        arrow = QPolygonF([QPointF(cx - r * 0.75, cy - r), QPointF(cx - r * 0.75, cy + r),
                           QPointF(cx + r, cy)])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#F08000'))
        painter.drawPolygon(arrow)

    def audio_pixmap(self, item, size):
        parts = item.file_path.replace('\\', '/').split('/')
        key = (item.file_path.rsplit('.', 1)[-1].lower(), 'music' in parts, size)
        pixmap = self._audio.get(key)
        if pixmap is None:
            pixmap = self._audio[key] = audio_thumbnail(item.file_path, size)
        return pixmap


def pack_tiles(spans, columns):
    """Place tiles of *spans* ``(across, down)`` on a grid *columns* slots
    wide, in order, each at the first free place it fits from the first row
    with a free slot -- so a square later in the folder fills the gap a wide
    tile left at a row's end. Returns ``[(column, row)]`` and the rows used.

    Linear: each shape's search starts where that shape last fitted (rows
    only fill up, so it cannot fit any earlier), or at the first row with a
    free slot -- a folder of wide textures never rescans the holes above."""
    columns = max(1, columns)
    rows = []                                    # bytearray of taken slots a row
    first_open = 0
    start = {}                                   # (across, down) -> first row to try
    places = []

    def taken(r):
        while r >= len(rows):
            rows.append(bytearray(columns))
        return rows[r]

    for across, down in spans:
        across = min(across, columns)
        shape = (across, down)
        r = max(first_open, start.get(shape, 0))
        placed = None
        while placed is None:
            row = taken(r)
            c = 0
            while c <= columns - across:
                if not any(row[c:c + across]) and all(
                        not any(taken(r + k)[c:c + across]) for k in range(1, down)):
                    placed = (c, r)
                    break
                c += 1
            if placed is None:
                r += 1
        c, r = placed
        start[shape] = r
        for k in range(down):
            row = taken(r + k)
            row[c:c + across] = b'\x01' * across
        places.append(placed)
        while first_open < len(rows) and all(rows[first_open]):
            first_open += 1
    return places, len(rows)


class AssetGridView(QAbstractItemView):
    """The tab's grid: slots of one size, a wide texture across two and a
    tall one down two (:func:`pack_tiles`). It lays a folder of any length
    out in one pass and paints only the tiles in view; only those ask for
    their thumbnails."""

    MARGIN = TILE_SPACING

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self._rects = []                         # row -> QRect (content coordinates)
        self._by_band = {}                       # slot row -> rows starting in it
        self._content_height = 0
        self._columns = 0
        self.setSelectionMode(QAbstractItemView.NoSelection)  # the tab draws its selection
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QAbstractItemView { background-color: #2b2b2b; border: none; }")
        self.apply_size(tab.thumb_size)

    # -- layout ------------------------------------------------------------
    def slot(self):
        width, height = AssetItem.tile_size(self.tab.thumb_size)
        return width + TILE_SPACING, height + TILE_SPACING

    def apply_size(self, thumb_size):
        self.verticalScrollBar().setSingleStep(max(8, self.slot()[1] // 3))
        self.relayout()

    def relayout(self):
        """Place every tile again (a new folder, size or width, or tiles
        whose shape has just been read)."""
        model = self.model()
        slot_w, slot_h = self.slot()
        width = self.viewport().width() - 2 * self.MARGIN
        self._columns = max(1, (width + TILE_SPACING) // slot_w)
        entries = model.entries if model is not None else []
        places, rows = pack_tiles([item.span for item in entries], self._columns)
        thumb = self.tab.thumb_size
        rects = []
        bands = {}
        for index, (item, (c, r)) in enumerate(zip(entries, places)):
            span = (min(item.span[0], self._columns), item.span[1])
            w, h = AssetItem.tile_size(thumb, span)
            rects.append(QRect(self.MARGIN + c * slot_w, self.MARGIN + r * slot_h, w, h))
            bands.setdefault(r, []).append(index)
        self._rects, self._by_band = rects, bands
        self._content_height = 2 * self.MARGIN + rows * slot_h
        self.updateGeometries()
        self.viewport().update()

    def updateGeometries(self):
        bar = self.verticalScrollBar()
        page = self.viewport().height()
        bar.setPageStep(max(1, page))
        bar.setRange(0, max(0, self._content_height - page))
        super().updateGeometries()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        slot_w = self.slot()[0]
        columns = max(1, (self.viewport().width() - 2 * self.MARGIN + TILE_SPACING) // slot_w)
        if columns != self._columns:
            self.relayout()
        else:
            self.updateGeometries()

    def reset(self):
        super().reset()
        self.verticalScrollBar().setValue(0)
        self.relayout()

    def dataChanged(self, top_left, bottom_right, roles=()):
        for row in range(top_left.row(), bottom_right.row() + 1):
            if row < len(self._rects):
                self.viewport().update(self.visualRect(self.model().index(row)))

    def visible_rows(self):
        """The rows whose tiles lie (partly) in view, in layout order."""
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        slot_h = self.slot()[1]
        first = max(0, (top - self.MARGIN) // slot_h - 1)    # a tall tile from the row above
        last = (bottom - self.MARGIN) // slot_h
        rows = []
        for band in range(first, last + 1):
            for row in self._by_band.get(band, ()):
                rect = self._rects[row]
                if rect.bottom() >= top and rect.top() <= bottom:
                    rows.append(row)
        return rows

    # -- QAbstractItemView ---------------------------------------------------
    def visualRect(self, index):
        if not index.isValid() or index.row() >= len(self._rects):
            return QRect()
        return self._rects[index.row()].translated(0, -self.verticalScrollBar().value())

    def indexAt(self, point):
        y = point.y() + self.verticalScrollBar().value()
        content = QPoint(point.x(), y)
        slot_h = self.slot()[1]
        band = (y - self.MARGIN) // slot_h
        for b in (band, band - 1):               # a tall tile starts a row up
            for row in self._by_band.get(b, ()):
                if self._rects[row].contains(content):
                    return self.model().index(row)
        return QModelIndex()

    def scrollTo(self, index, hint=QAbstractItemView.EnsureVisible):
        rect = self.visualRect(index)
        if rect.isNull():
            return
        bar = self.verticalScrollBar()
        if rect.top() < 0:
            bar.setValue(bar.value() + rect.top() - self.MARGIN)
        elif rect.bottom() > self.viewport().height():
            bar.setValue(bar.value() + rect.bottom() - self.viewport().height() + self.MARGIN)

    def moveCursor(self, action, modifiers):
        return QModelIndex()

    def horizontalOffset(self):
        return 0

    def verticalOffset(self):
        return self.verticalScrollBar().value()

    def isIndexHidden(self, index):
        return False

    def setSelection(self, rect, flags):
        pass

    def visualRegionForSelection(self, selection):
        return QRegion()

    def scrollContentsBy(self, dx, dy):
        self.viewport().scroll(dx, dy)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        option = self.viewOptions()
        delegate = self.itemDelegate()
        model = self.model()
        region = event.rect()
        for row in self.visible_rows():
            rect = self.visualRect(model.index(row))
            if not rect.intersects(region):
                continue
            option.rect = rect
            delegate.paint(painter, option, model.index(row))
        painter.end()

    # -- the mouse -----------------------------------------------------------
    def item_at(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return None, None
        return index.data(Qt.UserRole), self.visualRect(index)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        item, _cell = self.item_at(event.pos())
        if item is None:
            return
        self.tab.select_item(item)
        if item.animated:
            # An animated texture plays when clicked, and stops on its first
            # frame when clicked again.
            self.tab.toggle_preview(item)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mouseDoubleClickEvent(event)
        item, _cell = self.item_at(event.pos())
        if item is None:
            return
        self.tab.activate_item(item)

    def contextMenuEvent(self, event):
        item, _cell = self.item_at(event.pos())
        if item is None:
            self.tab._show_grid_menu(event.pos())


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

        #: Thumbnails on demand, decoded off the UI thread; and the GIF
        #: previews playing (file path -> [frames, index, timer]).
        self.loader = ThumbnailLoader(self)
        self.loader.sizes_read.connect(self._sizes_read)
        self._previews = {}
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.timeout.connect(self.relayout_grid)

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

        # 2. Middle Pane (Grid View): a list view over the tab's assets. Only
        # the tiles on screen are painted or ask for a thumbnail, so a folder
        # of any size opens at once. Right-click blank space for the size.
        self.model = AssetGridModel(self)
        self.view = AssetGridView(self)
        self.delegate = AssetDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.view.setModel(self.model)
        self.loader.changed.connect(self.model.refresh_path)
        self.splitter.addWidget(self.view)

        # Set initial splitter sizes (tree gets 180, grid gets the rest)
        self.splitter.setSizes([180, self.width() - 180])

        # Collapse tree panel by default
        self.tree_frame.setVisible(False)

        # Load the initial directory; the view reflows itself on resize.
        self.load_directory(self.current_asset_folder)

    def toggle_tree_panel(self, checked):
        """Show or hide the left folder tree panel."""
        self.tree_frame.setVisible(checked)
        if checked:
            # Restore a reasonable width when expanding
            self.splitter.setSizes([180, max(1, self.width() - 180)])

    def relayout_grid(self, force=False):
        """Lay the tiles out again (the view does it on resize by itself)."""
        self.view.relayout()

    def _sizes_read(self, batch):
        """Image sizes from the headers: wide and tall textures take two
        slots. One relayout for the changes of an event-loop turn."""
        from editor.asset_thumbnails import spans
        changed = False
        rows = self.model._rows
        for path, size in batch:
            span = spans(*size)
            for row in rows.get(path, ()):
                item = self.model.entries[row]
                if item.span != span:
                    item.span = span
                    changed = True
        if changed and not self._relayout_timer.isActive():
            self._relayout_timer.start(30)

    def set_thumbnail_size(self, thumb_size):
        if thumb_size == self.thumb_size:
            return
        self.stop_previews()
        self.thumb_size = thumb_size
        self.loader.drop_pixmaps()
        self.view.apply_size(thumb_size)

    def _show_grid_menu(self, pos):
        """Right-click blank space between or around assets: the size."""
        if self.view.item_at(pos)[0] is not None:
            return  # on an asset, not blank space
        menu = QMenu(self)
        group = QActionGroup(menu)
        for name in THUMBNAIL_SIZES:
            action = menu.addAction(name.capitalize())
            action.setCheckable(True)
            action.setChecked(THUMBNAIL_SIZES[name] == self.thumb_size)
            group.addAction(action)
            action.triggered.connect(lambda _checked, name=name: self._choose_thumbnail_size(name))
        menu.exec_(self.view.viewport().mapToGlobal(pos))

    # -- tiles: thumbnails, previews ----------------------------------------
    def tile_pixmap(self, item):
        """What the delegate paints for *item*: a preview frame while one
        plays, its thumbnail when known, None while it is being made."""
        if item.is_audio:
            return self.delegate.audio_pixmap(item, self.thumb_size)
        preview = self._previews.get(item.file_path)
        if preview is not None:
            return preview[0][preview[1]][0]
        return self.loader.pixmap(item, thumb_box(self.thumb_size, item.span))

    def thumbnail_pixmap(self, item):
        """*item*'s tile pixmap now, made on the spot if it is not yet."""
        if item.is_audio or item.file_path in self._previews:
            return self.tile_pixmap(item)
        pixmap = self.tile_pixmap(item)
        if pixmap is None:
            self.loader.finish()
            pixmap = self.tile_pixmap(item)
        if pixmap is None:
            self.loader.image_now(item)
            pixmap = self.tile_pixmap(item)
        return pixmap

    def finish_thumbnails(self, timeout=10.0):
        """Wait until every thumbnail the view asked for is made, then
        repaint (tests, and captures that must show them)."""
        import time
        deadline = time.monotonic() + timeout
        while True:
            QApplication.processEvents()
            self.loader.finish(max(0.1, deadline - time.monotonic()))
            if self._relayout_timer.isActive():          # shapes just read
                self._relayout_timer.stop()
                self.relayout_grid()
                continue
            QApplication.processEvents()                 # paint: newly shown tiles ask
            if not self.loader.busy() and not self._relayout_timer.isActive():
                break
            if time.monotonic() >= deadline:
                break
        self.view.viewport().update()
        QApplication.processEvents()

    def is_previewing(self, item):
        return item.file_path in self._previews

    def toggle_preview(self, item):
        """Play an animated GIF on its tile, or stop it on the first frame."""
        if self.is_previewing(item):
            self.stop_preview(item)
            return
        from engine.animated_texture import decode_gif_frames
        try:
            images, seconds = decode_gif_frames(item.file_path)
        except Exception as exc:
            print(f"[AssetBrowser] Could not preview '{item.name_text}': {exc}")
            return
        frames = []
        for image in images:
            qimage = QImage(image.tobytes(), image.width, image.height,
                            image.width * 4, QImage.Format_RGBA8888).copy()
            box = QSize(*thumb_box(self.thumb_size, item.span))
            frames.append((QPixmap.fromImage(qimage).scaled(
                box, Qt.KeepAspectRatio, Qt.SmoothTransformation),
                seconds[len(frames)] if len(frames) < len(seconds) else 0.1))
        if not frames:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = [frames, 0, timer]
        self._previews[item.file_path] = state
        timer.timeout.connect(lambda path=item.file_path: self._next_preview_frame(path))
        timer.start(max(20, int(frames[0][1] * 1000)))
        self.model.refresh_path(item.file_path)

    def _next_preview_frame(self, path):
        state = self._previews.get(path)
        if state is None:
            return
        state[1] = (state[1] + 1) % len(state[0])
        state[2].start(max(20, int(state[0][state[1]][1] * 1000)))
        self.model.refresh_path(path)

    def stop_preview(self, item):
        state = self._previews.pop(item.file_path, None)
        if state is not None:
            state[2].stop()
            state[2].deleteLater()
            self.model.refresh_path(item.file_path)

    def stop_previews(self):
        for path in list(self._previews):
            state = self._previews.pop(path)
            state[2].stop()
            state[2].deleteLater()

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
        # Clear existing items: decodes not yet started for the old folder
        # are dropped (finished ones stay cached for a revisit).
        self.stop_previews()
        self.loader.cancel()
        self.items = []
        self.model.set_entries(self.items)
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
            items = []
            for member in members:
                relative = member[len(prefix):]
                full_path = os.path.join(project_root, *member.split("/"))
                items.append(AssetItem(relative, full_path, is_audio=True))
            self.items = items
            self.model.set_entries(items)
            return

        if self.is_audio_tab:
            self.tree_toggle_btn.setEnabled(True)
            self.tree_toggle_btn.setToolTip("Toggle Folder Panel")
        if not asset_isdir(path):
            return

        try:
            # Loose files and the category archive's members, read in place.
            # Nothing is opened here: a tile reads its file when it is shown,
            # and one that cannot be read shows "?".
            files = sorted(asset_listdir(path), key=browser_order)
            extensions = self.extensions
            items = [AssetItem(f, os.path.join(path, f), is_model=self.is_model_tab,
                               is_audio=self.is_audio_tab)
                     for f in files if os.path.splitext(f)[1].lower() in extensions]
            self.items = items
            self.model.set_entries(items)
            # Their shapes, from the headers, off the UI thread: wide and
            # tall textures then take two slots (the top of the folder first).
            self.loader.read_sizes([item.file_path for item in items if item.is_image])
        except Exception as e:
            print(f"Error: {e}")

    def select_item(self, item):
        previous, self.selected_item = self.selected_item, item
        if previous is not None and previous is not item:
            self.stop_preview(previous)          # one animation plays at a time
        for tile in (previous, item):
            if tile is not None:
                self.model.refresh_path(tile.file_path)
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
