"""Live numerical instrument panel for Fio's dense render projections.

Loaded only when Debug -> Debug Tables is invoked. The window reads the
published render-state snapshot; it does not add a second world representation
or alter the logic/render hot path. In play mode it also copies the monster
AI's MonsterTable, taken only when the monster lock is free so the instrument
never makes the AI wait.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from types import SimpleNamespace

import numpy as np
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QFileDialog,
    QTabWidget, QTableView, QTextBrowser, QVBoxLayout, QWidget,
)

from engine import monster_table
from engine import render_table as rt
from engine.render_keys import KeyLayout, sort_into_runs


#: Fio's accent colour.
FIO_ORANGE = "#f08000"

_DARK = """
QMainWindow, QWidget { background:#101214; color:#d7dce0; }
QLabel { color:#aeb6bd; }
QTabWidget::pane { border:1px solid #2b3035; background:#101214; }
QTabBar::tab { background:#1a1e22; color:#9ca6ae; padding:8px 16px; border:1px solid #2b3035; }
QTabBar::tab:selected { background:#252b30; color:#f0f3f5; border-bottom:2px solid #f08000; }
QTableView { background:#0b0d0f; alternate-background-color:#111519; color:#d7dce0;
             gridline-color:#252a2e; selection-background-color:#343b42; selection-color:#fff; }
QHeaderView::section { background:#1b2024; color:#aeb6bd; padding:5px; border:0; border-right:1px solid #30363b; }
QComboBox, QPushButton { background:#1b2024; color:#d7dce0; border:1px solid #343a40; padding:5px 8px; }
QCheckBox { color:#b9c1c7; }
QTextBrowser { background:#0b0d0f; color:#cbd2d8; border:1px solid #252a2e; }
"""


class FrozenTable:
    """A copy of one dense table, taken while the published frame was borrowed.

    The published frame is pinned for as long as anything holds it, and with
    double-buffered publication a pinned frame is a frozen renderer: the logic
    thread cannot swap. So the instrument copies what it shows and hands the
    frame straight back, rather than keeping it between refreshes.
    """

    def __init__(self, table):
        self.fields = {}
        for name in _slot_names(type(table)):
            value = getattr(table, name, None)
            # Object columns (``refs``) hold the live objects, not numbers:
            # copying them would keep objects alive from the instrument, and
            # ``np.save`` refuses them, which failed every Export.
            if isinstance(value, np.ndarray) and value.dtype != object:
                self.fields[name] = value.copy()
        # Rows to show. TerrainTable reuses freed slots, so its live rows are
        # not 0..count; it reports how far its allocated rows extend.
        self.count = int(getattr(table, "row_extent", getattr(table, "count", 0)))
        self.live_count = int(getattr(table, "count", 0))
        self.slot_of_id = dict(getattr(table, "slot_of_id", {}))
        self.rows_read = int(getattr(table, "rows_read", 0))
        # MonsterTable's per-tick counters; absent (and unused) elsewhere.
        self.rays_cast = int(getattr(table, "rays_cast", 0))
        self.python_rows = int(getattr(table, "python_rows", 0))
        self.phase_ms = dict(getattr(table, "phase_ms", {}) or {})
        self.team_names = list(getattr(table, "team_names", ()))
        self.path = str(getattr(table, "path", ""))
        self.generation = int(getattr(table, "generation", 0))

    #: Columns a table exposes as views of another: RenderTable's centre and
    #: half-extent live in its ``bounds`` block.
    _VIEWS = {"center": ("bounds", slice(0, 3)), "half": ("bounds", slice(3, 6))}

    def __getattr__(self, name):
        fields = self.__dict__["fields"]
        if name in fields:
            return fields[name]
        view = self._VIEWS.get(name)
        if view is not None and view[0] in fields:
            return fields[view[0]][:, view[1]]
        raise AttributeError(name)


def _slot_names(cls):
    return tuple(name for klass in cls.__mro__
                 for name in getattr(klass, "__slots__", ()))


def _array_fields(table):
    """``(name, ndarray)`` for every column of a live or frozen table."""
    if isinstance(table, FrozenTable):
        return list(table.fields.items())
    return [(name, value) for name in _slot_names(type(table))
            for value in (getattr(table, name, None),)
            if isinstance(value, np.ndarray)]


def _num_bytes(table):
    arrays = _array_fields(table)
    return sum(int(value.nbytes) for _, value in arrays), arrays


def _fmt(value):
    if isinstance(value, (np.integer, int)):
        return str(int(value))
    if isinstance(value, (np.floating, float)):
        return f"{float(value):.5g}"
    if isinstance(value, (np.bool_, bool)):
        return "1" if bool(value) else "0"
    if isinstance(value, (np.str_, str)):
        return str(value)
    return str(value)


def _set_text_preserve_scroll(widget, text):
    """Replace text without throwing the user's vertical scroll position away."""
    bar = widget.verticalScrollBar()
    value = bar.value()
    at_bottom = value >= bar.maximum() - 2
    widget.setText(text)

    def restore():
        try:
            if at_bottom:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(value, bar.maximum()))
        except RuntimeError:
            pass        # the window closed before this deferred restore ran

    QTimer.singleShot(0, restore)


_HIGHLIGHT_BG = QBrush(QColor(FIO_ORANGE))
_HIGHLIGHT_FG = QBrush(QColor("#101214"))


class ArrayModel(QAbstractTableModel):
    """Read-only view over one NumPy column/row array."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.array = np.empty((0, 0), dtype=np.float32)
        self.names = []
        self.offset = 0
        #: The followed selection's row, drawn highlighted; -1 for none.
        self.highlight = -1

    def set_array(self, array, names=None, offset=0):
        self.beginResetModel()
        a = np.asarray(array)
        if a.ndim == 0:
            a = a.reshape(1, 1)
        elif a.ndim == 1:
            a = a.reshape(-1, 1)
        else:
            # Reshaping to (rows, -1) cannot infer a dimension for zero-row
            # arrays. Keep the column count explicit so empty live tables
            # remain valid read-only views.
            width = int(np.prod(a.shape[1:], dtype=np.int64))
            a = a.reshape(a.shape[0], width)
        self.array = a
        self.names = list(names or [f"[{i}]" for i in range(a.shape[1])])
        self.offset = int(offset)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.array)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.array.shape[1]

    def set_highlight(self, row):
        row = -1 if row is None else int(row)
        if row == self.highlight:
            return
        old, self.highlight = self.highlight, row
        for changed in (old, row):
            if 0 <= changed < len(self.array):
                self.dataChanged.emit(self.index(changed, 0),
                                      self.index(changed, self.columnCount() - 1))

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            return _fmt(self.array[index.row(), index.column()])
        if index.row() == self.highlight:
            if role == Qt.BackgroundRole:
                return _HIGHLIGHT_BG
            if role == Qt.ForegroundRole:
                return _HIGHLIGHT_FG
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.names[section] if section < len(self.names) else str(section)
        return str(self.offset + section)


class RawTable(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.table = None
        self.model = ArrayModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(False)
        self.view.verticalHeader().setDefaultSectionSize(20)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.selector = QComboBox()
        self.meta = QLabel()
        self.meta.setFont(QFont("Consolas", 9))
        self.selector.currentIndexChanged.connect(self._field_changed)
        top = QHBoxLayout()
        top.addWidget(QLabel(title))
        top.addWidget(self.meta, 1)
        top.addWidget(QLabel("ARRAY"))
        top.addWidget(self.selector, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)

    def update_table(self, table):
        self.table = table
        fields = [name for name, value in _array_fields(table)
                  if value.ndim >= 1 and value.size]
        current = self.selector.currentText()
        self.selector.blockSignals(True)
        self.selector.clear()
        self.selector.addItems(fields)
        if current in fields:
            self.selector.setCurrentText(current)
        self.selector.blockSignals(False)
        self._field_changed()

    def set_highlight(self, row):
        """Mark the followed selection's *row* (None for none) and show it."""
        self.model.set_highlight(row)
        if row is not None and 0 <= int(row) < self.model.rowCount():
            index = self.model.index(int(row), 0)
            self.view.selectRow(int(row))
            self.view.scrollTo(index, QTableView.PositionAtCenter)

    def _field_changed(self):
        if self.table is None:
            return
        name = self.selector.currentText()
        if not name:
            return
        value = getattr(self.table, name)
        count = int(getattr(self.table, "count", len(value)))
        shown = value[:count]
        shape = tuple(int(x) for x in shown.shape)
        self.meta.setText(
            f"shape={shape}  dtype={value.dtype}  bytes={int(value.nbytes):,}"
        )
        if shown.ndim == 1:
            names = [name]
        else:
            # shown may have zero rows when a table's live count is
            # temporarily empty. Derive the flattened width from the
            # original array shape rather than asking NumPy to infer it from
            # a zero-sized view.
            width = int(np.prod(shown.shape[1:], dtype=np.int64))
            names = [f"{name}[{i}]" for i in range(width)]
        bar = self.view.verticalScrollBar()
        value = bar.value()
        at_bottom = value >= bar.maximum() - 2
        self.model.set_array(shown, names=names)
        if 0 <= self.model.highlight < len(shown):
            return      # set_highlight scrolls to the followed row instead

        def restore():
            try:
                if at_bottom:
                    bar.setValue(bar.maximum())
                else:
                    bar.setValue(min(value, bar.maximum()))
            except RuntimeError:
                pass    # the window closed before this deferred restore ran

        QTimer.singleShot(0, restore)


class BarView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.setMinimumHeight(220)

    def set_items(self, items):
        self.items = list(items)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setFont(QFont("Consolas", 9))
        if not self.items:
            p.drawText(12, 24, "NO DATA")
            return
        maxv = max(v for _, v in self.items) or 1
        row_h = max(18, min(28, self.height() // max(1, len(self.items))))
        label_x = 8
        bar_x = 150
        value_width = 100
        bar_width = max(40, self.width() - bar_x - value_width - 12)
        for i, (label, value) in enumerate(self.items):
            y = 4 + i * row_h
            width = int(bar_width * value / maxv)
            p.setPen(Qt.NoPen)
            p.setBrush(Qt.darkGray)
            p.drawRect(bar_x, y + 3, bar_width, row_h - 7)
            p.setBrush(Qt.gray)
            p.drawRect(bar_x, y + 3, max(1, width), row_h - 7)
            p.setPen(Qt.white)
            p.drawText(label_x, y + row_h - 8, str(label)[:22])
            p.drawText(
                bar_x + bar_width + 8,
                y + row_h - 8,
                f"{value:,}",
            )


class DebugTablesWindow(QMainWindow):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("Fio — Debug Tables")
        self.resize(1250, 780)
        self.setStyleSheet(_DARK)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)

        self.snapshot = None
        self.render = None
        self.entities = None
        self.monsters = None
        self.monster_copied_at = 0.0
        #: TerrainTable copy, or None when the map has no (enabled) terrain --
        #: absence is explicit, there is no empty table.
        self.terrain = None
        self.terrain_info = None
        self.follow = QCheckBox("FOLLOW SELECTION")
        self.follow.setChecked(True)
        self.always_top = QCheckBox("ALWAYS ON TOP")
        self.always_top.setChecked(True)
        self.always_top.toggled.connect(self._set_always_on_top)
        self.status = QLabel("DETACHED — waiting for published render state")
        self.status.setFont(QFont("Consolas", 9))

        tabs = QTabWidget()
        self.dashboard = QTextBrowser()
        self.bars = BarView()
        dash = QWidget()
        dl = QVBoxLayout(dash)
        dl.addWidget(self.status)
        dl.addWidget(self.bars, 1)
        dl.addWidget(self.dashboard, 1)
        tabs.addTab(dash, "PIPELINE")

        self.render_raw = RawTable("RENDERTABLE", self)
        self.entity_raw = RawTable("ENTITYTABLE", self)
        self.monster_raw = RawTable("MONSTERTABLE", self)
        self.terrain_raw = RawTable("TERRAINTABLE", self)
        tabs.addTab(self.render_raw, "RENDERTABLE")
        tabs.addTab(self.entity_raw, "ENTITYTABLE")
        tabs.addTab(self.monster_raw, "MONSTERTABLE")
        tabs.addTab(self.terrain_raw, "TERRAINTABLE")

        self.keys_text = QTextBrowser()
        tabs.addTab(self.keys_text, "KEY MICROSCOPE")
        self.memory_text = QTextBrowser()
        tabs.addTab(self.memory_text, "MEMORY")
        #: The followed selection's dense representation: every column of
        #: every row its id has.
        self.selection_text = QTextBrowser()
        tabs.addTab(self.selection_text, "SELECTION")
        self.tabs = tabs

        controls = QHBoxLayout()
        controls.addWidget(QLabel("DENSE NUMERICAL INSTRUMENT"))
        #: The active renderer's registry name -- FORWARD, DEFERRED, ... The
        #: instrument reads only the Renderer contract, so it works the same
        #: whichever renderer is drawing.
        self.renderer_label = QLabel("-")
        self.renderer_label.setStyleSheet(
            f"color:{FIO_ORANGE}; font-weight:bold; padding:0 10px;")
        self.renderer_label.setToolTip("Active renderer (r_renderer)")
        controls.addWidget(self.renderer_label)
        controls.addStretch(1)
        controls.addWidget(self.follow)
        controls.addWidget(self.always_top)
        refresh = QPushButton("Refresh")
        refresh.setStyleSheet("""
            QPushButton {
                background-color: #F08000;
                color: white;
                font-weight: bold;
                border: 1px solid #d06000;
                border-radius: 4px;
                padding: 5px 12px;
            }
            QPushButton:hover {
                background-color: #ff9800;
            }
            QPushButton:pressed {
                background-color: #d06000;
            }
        """)
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)

        export = QPushButton("Export")
        export.setToolTip("Export the complete numerical snapshot for later analysis")
        export.setStyleSheet("""
            QPushButton {
                background-color: #22b14c;
                color: white;
                font-weight: bold;
                border: 1px solid #1a8f3d;
                border-radius: 4px;
                padding: 5px 12px;
            }
            QPushButton:hover {
                background-color: #28d157;
            }
            QPushButton:pressed {
                background-color: #1a8f3d;
            }
        """)
        export.clicked.connect(self.export_snapshot)
        controls.addWidget(export)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(controls)
        layout.addWidget(tabs, 1)
        self.setCentralWidget(root)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self._last_raw_signature = {}
        self._last_keys_text = None
        self._last_memory_text = None
        self.timer.start()
        # Follow a new selection at once (viewport, Scene Hierarchy, ...)
        # rather than on the next tick.
        main_window.selection_changed.connect(self._selection_changed)
        self.destroyed.connect(self._stop)
        self.refresh()

    def _table_arrays(self, table):
        """Return every NumPy field, including unused capacity, for export."""
        return dict(_array_fields(table))

    def _key_snapshot(self):
        """Build the complete logical key stream represented by KEY MICROSCOPE."""
        t = self.render
        slots = getattr(self.snapshot, "visible_brush_slots", None)
        if t is None or slots is None or not len(slots):
            return None
        slots = np.asarray(slots, dtype=np.int32)
        cube = (t.class_bits[slots] & rt.CLASS_HAS_GEOMETRY) == 0
        slots = slots[cube]
        if not len(slots):
            return None
        ids = t.tex_name_id[slots]
        drawn = (ids >= 0) & (ids != rt.TEX_ID_SKIP)
        if self.snapshot.is_play_mode:
            drawn &= ids != rt.TEX_ID_NODRAW
        row, face = np.nonzero(drawn)
        if not len(row):
            return None
        texture = ids[row, face].astype(np.int64)
        face = face.astype(np.int64)
        layout = KeyLayout([("texture", 32), ("face", 3)])
        keys = layout.pack(texture=texture, face=face)
        order, starts = sort_into_runs(keys)
        sorted_keys = keys[order]
        unique, counts = np.unique(sorted_keys, return_counts=True)
        return {
            "visible_cube_slots": slots,
            "row": row.astype(np.int32),
            "face": face,
            "texture_name_id": texture,
            "logical_keys": keys,
            "sort_order": order.astype(np.int64),
            "run_starts": starts.astype(np.int64),
            "sorted_keys": sorted_keys,
            "unique_keys": unique,
            "key_counts": counts.astype(np.int64),
        }

    def _follow_export_text(self):
        """Return the current FOLLOW SELECTION chain, or an explicit empty state."""
        return "FOLLOW SELECTION\n\n" + self._follow_chain()

    def export_snapshot(self):
        """Export raw table storage plus every derived instrument view."""
        if self.render is None or self.entities is None or self.snapshot is None:
            self.status.setText("EXPORT — no attached dense numerical state")
            return

        default_name = time.strftime("fio_debug_tables_%Y%m%d_%H%M%S.zip")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Fio Debug Tables",
            default_name,
            "Fio debug snapshot (*.zip)",
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"

        stats = getattr(
            self.main_window.view_3d.renderer,
            "render_stats", None
        )
        pipeline = {
            "format": "fio-debug-tables-v1",
            "export_time_unix": time.time(),
            "snapshot_timestamp": self.snapshot.timestamp,
            "frame_age_ms": self._frame_age_ms(),
            "timings_ms": self._timings(),
            "is_play_mode": bool(getattr(self.snapshot, "is_play_mode", False)),
            "renderer": self._renderer_name(),
            "render_rows": int(self.render.count),
            "render_capacity": int(len(self.render.center)),
            "entity_rows": int(self.entities.count),
            "entity_capacity": int(len(self.entities.pos)),
            "render_draw_calls": int(getattr(stats, "draw_calls", 0)) if stats else 0,
            "batched_draws": int(getattr(stats, "batched_draws", 0)) if stats else 0,
            "shadow_draw_calls": int(getattr(stats, "shadow_draw_calls", 0)) if stats else 0,
            "visible_triangles": int(getattr(stats, "visible_tris", 0)) if stats else 0,
            "render_dense_bytes": int(_num_bytes(self.render)[0]),
            "entity_dense_bytes": int(_num_bytes(self.entities)[0]),
        }
        if self.monsters is not None:
            m = self.monsters
            pipeline.update({
                "monster_pass": m.path,
                "monster_rows": int(m.count),
                "monster_capacity": int(len(m.pos)),
                "monster_mode_names": list(monster_table.MODE_NAMES),
                "monster_rays_cast": int(m.rays_cast),
                "monster_python_rows": int(m.python_rows),
                "monster_phase_ms": dict(m.phase_ms),
                "monster_team_names": list(m.team_names),
                "monster_dense_bytes": int(_num_bytes(m)[0]),
            })

        if self.terrain is not None and self.terrain_info is not None:
            t, info = self.terrain, self.terrain_info
            pipeline.update({
                "terrain_resident": int(t.live_count),
                "terrain_rows_allocated": int(t.count),
                "terrain_capacity": int(len(t.live)),
                "terrain_drawn": info.drawn,
                "terrain_culled": info.culled,
                "terrain_drawn_triangles": info.drawn_triangles,
                "terrain_built_triangles": info.built_triangles,
                "terrain_streaming": info.streaming,
                "terrain_stream_radius": info.stream_radius,
                "terrain_gpu_bytes": info.gpu_bytes,
                "terrain_dense_bytes": int(_num_bytes(t)[0]),
            })
        else:
            pipeline["terrain_table"] = None

        key_data = self._key_snapshot()
        key_manifest = {
            "logical_layout": [
                {"name": "texture", "bits": 32},
                {"name": "face", "bits": 3},
            ],
            "key_meaning": (
                "Logical brush key. The renderer's final brush key substitutes "
                "the resolved GL texture id for texture-name-id."
            ),
            "arrays": sorted(key_data.keys()) if key_data else [],
        }

        memory = {"tables": {}}
        for label, table in self._tables():
            count = int(table.count)
            memory["tables"][label] = {
                "count": count,
                "fields": {},
            }
            for name, value in self._table_arrays(table).items():
                memory["tables"][label]["fields"][name] = {
                    "shape": [int(x) for x in value.shape],
                    "live_shape": [int(x) for x in value[:count].shape],
                    "dtype": str(value.dtype),
                    "bytes": int(value.nbytes),
                }

        manifest = {
            "format": "fio-debug-tables-v1",
            "contents": [
                "pipeline.json",
                "pipeline.txt",
                "RenderTable/*.npy",
                "EntityTable/*.npy",
                "MonsterTable/*.npy (play mode)",
                "TerrainTable/*.npy (maps with terrain)",
                "visible_brush_slots.npy",
                "KeyMicroscope/*.npy",
                "key_microscope.json",
                "key_microscope.txt",
                "memory.json",
                "memory.txt",
                "follow_selection.txt",
            ],
            "note": (
                "NumPy table arrays are exported at full allocated capacity, "
                "not truncated to live row count. live_shape/count in memory.json "
                "identify the populated portion."
            ),
        }

        pipeline_text = self.dashboard.toPlainText()
        memory_text = self.memory_text.toPlainText()
        key_text = self.keys_text.toPlainText()
        follow_text = self._follow_export_text()

        try:
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True),
                )
                archive.writestr(
                    "pipeline.json",
                    json.dumps(pipeline, indent=2, sort_keys=True),
                )
                archive.writestr("pipeline.txt", pipeline_text)
                archive.writestr(
                    "memory.json",
                    json.dumps(memory, indent=2, sort_keys=True),
                )
                archive.writestr("memory.txt", memory_text)
                archive.writestr("key_microscope.json", json.dumps(
                    key_manifest, indent=2, sort_keys=True
                ))
                archive.writestr("key_microscope.txt", key_text)
                archive.writestr("follow_selection.txt", follow_text)

                for label, table in self._tables():
                    for name, value in self._table_arrays(table).items():
                        buffer = io.BytesIO()
                        np.save(buffer, value, allow_pickle=False)
                        archive.writestr(
                            f"{label}/{name}.npy", buffer.getvalue()
                        )

                visible = getattr(self.snapshot, "visible_brush_slots", None)
                if visible is not None:
                    buffer = io.BytesIO()
                    np.save(buffer, np.asarray(visible), allow_pickle=False)
                    archive.writestr(
                        "visible_brush_slots.npy", buffer.getvalue()
                    )

                if key_data:
                    for name, value in key_data.items():
                        buffer = io.BytesIO()
                        np.save(buffer, np.asarray(value), allow_pickle=False)
                        archive.writestr(
                            f"KeyMicroscope/{name}.npy", buffer.getvalue()
                        )
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(f"EXPORT FAILED — {exc}")
            return

        self.status.setText(
            self.status.text().split("  |  EXPORT")[0]
            + f"  |  EXPORT {path}"
        )

    def _set_always_on_top(self, checked):
        flags = self.windowFlags()
        flags.setFlag(Qt.WindowStaysOnTopHint, bool(checked))
        self.setWindowFlags(flags)
        self.show()

    def _stop(self, *_):
        if hasattr(self, "timer"):
            self.timer.stop()

    def _game_state(self):
        view = self.main_window.view_3d
        if view is None or view.logic_thread is None:
            return None
        return view.logic_thread.game_state

    def refresh(self):
        started = time.perf_counter()
        self.renderer_label.setText(self._renderer_name().upper())
        game_state = self._game_state()
        if game_state is None:
            self.status.setText("DETACHED — no LogicThread/render state")
            return
        snap = game_state.get_render_state()
        try:
            render = getattr(snap, "render_table", None)
            entities = getattr(snap, "entity_table", None)
            if render is None or entities is None:
                self.status.setText("ATTACHED — dense tables not published yet")
                return
            self.render = FrozenTable(render)
            self.entities = FrozenTable(entities)
            self.snapshot = SimpleNamespace(
                visible_brush_slots=np.array(
                    getattr(snap, "visible_brush_slots", ()), dtype=np.int32),
                is_play_mode=bool(getattr(snap, "is_play_mode", False)),
                timestamp=float(getattr(snap, "timestamp", 0.0)),
                prepare_ms=float(getattr(snap, "prepare_ms", 0.0)),
            )
        finally:
            game_state.release_render_state(snap)
        self._copy_monster_table()
        self._copy_terrain_table()

        self._update_raw_tables()
        self._update_dashboard(started)
        self._update_keys()
        self._update_memory()
        self._update_follow()

    def _monster_ai(self):
        view = self.main_window.view_3d
        if view is None or view.logic_thread is None:
            return None, None
        return view.logic_thread, view.logic_thread.monster_ai

    def _copy_monster_table(self):
        """Copy the AI's MonsterTable, without ever making the AI wait.

        The AI thread writes the table while it holds the monster lock, so
        the copy is taken under that lock -- but only if it is free right now.
        Blocking on it would put the instrument inside the contention it is
        there to measure; when it is busy the previous copy is kept and its
        age shows on the dashboard.
        """
        logic, ai = self._monster_ai()
        if logic is None or ai is None:
            return
        table = ai.table
        lock = logic.session_runtime.monster_lock
        if not lock.acquire(blocking=False):
            return
        try:
            self.monsters = FrozenTable(table)
        finally:
            lock.release()
        self.monster_copied_at = time.perf_counter()

    def _copy_terrain_table(self):
        """Copy the terrain's TerrainTable and the numbers that go with it.

        The terrain builds and draws on the GUI thread -- the thread this
        instrument runs on -- so the copy is taken between frames with no
        lock. A map without terrain, or with terrain switched off, has no
        table: ``self.terrain`` is None.
        """
        terrain = self.main_window.terrain
        table = getattr(terrain, "table", None)
        if terrain is None or table is None or not getattr(terrain, "enabled", False):
            self.terrain = None
            self.terrain_info = None
            return
        self.terrain = FrozenTable(table)
        pages = list(getattr(terrain, "_height_pages", ()) or ())
        layers = int(getattr(terrain, "_page_layers", 0) or 0)
        grid = self.terrain.heights.shape[1] if self.terrain.heights.ndim == 3 else 0
        self.terrain_info = SimpleNamespace(
            streaming=bool(getattr(terrain, "streaming", False)),
            stream_radius=float(getattr(terrain, "stream_radius", 0.0)),
            drawn=int(len(getattr(terrain, "drawn_slots", ()))),
            culled=int(getattr(terrain, "culled_chunks", 0)),
            drawn_triangles=int(getattr(terrain, "total_triangles", 0)),
            built_triangles=int(table.triangle_count()),
            pages=len(pages),
            page_layers=layers,
            gpu_bytes=len(pages) * layers * grid * grid * 4,
            use_textures=bool(getattr(terrain, "use_textures", False)),
            grass=bool(getattr(terrain, "grass_enabled", False)),
            budget_ms=float(getattr(terrain, "UPDATE_BUDGET_MS", 0.0)),
            max_updates=int(getattr(terrain, "MAX_UPDATES_PER_FRAME", 0)),
        )

    def _tables(self):
        """``(label, table)`` for every dense table this refresh copied."""
        tables = [("RenderTable", self.render), ("EntityTable", self.entities)]
        if self.monsters is not None:
            tables.append(("MonsterTable", self.monsters))
        if self.terrain is not None:
            tables.append(("TerrainTable", self.terrain))
        return tables

    def _update_raw_tables(self):
        """Refresh raw models only when their selected array actually changed."""
        for raw, table in (
            (self.render_raw, self.render), (self.entity_raw, self.entities),
            (self.monster_raw, self.monsters),
            (self.terrain_raw, self.terrain),
        ):
            if table is None:
                continue
            name = raw.selector.currentText()
            value = getattr(table, name, None) if name else None
            count = int(getattr(table, "count", 0))
            signature = (
                id(table), name, id(value), count,
                tuple(value.shape) if isinstance(value, np.ndarray) else None,
                str(value.dtype) if isinstance(value, np.ndarray) else None,
            )
            if signature == self._last_raw_signature.get(id(raw)):
                continue
            self._last_raw_signature[id(raw)] = signature
            raw.update_table(table)

    def _frame_age_ms(self):
        """How old the published frame is; its timestamp is ``perf_counter``."""
        stamp = float(self.snapshot.timestamp)
        return max(0.0, (time.perf_counter() - stamp) * 1000.0) if stamp else 0.0

    def _timings(self):
        """Measured stage timings: the logic prepare, the paint, each pass."""
        view = self.main_window.view_3d
        stats = getattr(getattr(view, "renderer", None), "render_stats", None)
        game_state = self._game_state()
        now = time.perf_counter()
        published = int(getattr(game_state, "published_frames", 0))
        declined = int(getattr(game_state, "declined_swaps", 0))
        # Rates over at least half a second: two samples moments apart (a
        # manual Refresh right after the timer's) would otherwise read zero.
        last = getattr(self, "_last_counters", None)
        rates = getattr(self, "_last_rates", (0.0, 0.0))
        if last is None:
            self._last_counters = (now, published, declined)
        elif now - last[0] >= 0.5:
            span = now - last[0]
            rates = ((published - last[1]) / span, (declined - last[2]) / span)
            self._last_counters = (now, published, declined)
            self._last_rates = rates
        logic = view.logic_thread if view is not None else None
        ai = logic.session_runtime.monster_ai_thread if logic is not None else None
        return {
            "tick": float(getattr(logic, "tick_ms", 0.0)),
            "ai": float(getattr(ai, "update_ms", 0.0)) if ai is not None else 0.0,
            "ai_lock_wait": (float(getattr(ai, "lock_wait_ms", 0.0))
                             if ai is not None else 0.0),
            "prepare": float(self.snapshot.prepare_ms),
            "paint": float(getattr(view, "paint_ms", 0.0)),
            "passes": dict(getattr(stats, "pass_ms", {}) or {}),
            "published_per_s": rates[0],
            "declined_per_s": rates[1],
            "render_rows_read": int(self.render.rows_read),
            "entity_rows_read": int(self.entities.rows_read),
        }

    def _update_dashboard(self, started):
        rbytes, _ = _num_bytes(self.render)
        ebytes, _ = _num_bytes(self.entities)
        render_cap = len(self.render.center)
        entity_cap = len(self.entities.pos)
        stats = getattr(
            self.main_window.view_3d.renderer,
            "render_stats", None
        )
        draw_calls = int(getattr(stats, "draw_calls", 0)) if stats else 0
        entity_candidates = int(getattr(stats, "entity_candidates", 0)) if stats else 0
        culled_entities = int(getattr(stats, "culled_entities", 0)) if stats else 0
        # Whatever diagnostics the active renderer publishes; the instrument
        # knows no renderer's internals.
        detail_lines = "".join(
            f"  {name}  {value() if callable(value) else value}\n"
            for name, value in (getattr(stats, "details", None) or {}).items())
        batched = int(getattr(stats, "batched_draws", 0)) if stats else 0
        shadow_draws = int(getattr(stats, "shadow_draw_calls", 0)) if stats else 0
        tris = int(getattr(stats, "visible_tris", 0)) if stats else 0
        total = rbytes + ebytes
        timings = self._timings()
        sample_ms = (time.perf_counter() - started) * 1000.0
        self.status.setText(
            f"ATTACHED  |  frame age {self._frame_age_ms():.1f} ms  | "
            f"inspector sample {sample_ms:.2f} ms"
        )
        passes = sorted(timings["passes"].items(), key=lambda kv: -kv[1])
        self.bars.set_items(
            [("prepare (logic) us", int(timings["prepare"] * 1000)),
             ("paint (UI) us", int(timings["paint"] * 1000))]
            + [(f"{name} us", int(ms * 1000)) for name, ms in passes[:8]]
        )
        pass_lines = "\n".join(
            f"  {name:<28} {ms:8.3f} ms" for name, ms in passes) or "  (no frame drawn yet)"
        _set_text_preserve_scroll(self.dashboard,
            "PIPELINE / PUBLISHED STATE\n\n"
            f"RenderTable   rows={int(self.render.count):,}  "
            f"capacity={render_cap:,}  dense bytes={rbytes:,}  "
            f"rows read last frame={timings['render_rows_read']:,}\n"
            f"EntityTable   rows={int(self.entities.count):,}  "
            f"capacity={entity_cap:,}  dense bytes={ebytes:,}  "
            f"rows read last frame={timings['entity_rows_read']:,}\n"
            f"TOTAL NUMERICAL STORAGE (ndarrays)  {total:,} bytes "
            f"({total/1024/1024:.2f} MiB)\n\n"
            "PUBLICATION (double-buffered)\n"
            f"  frames published   {timings['published_per_s']:7.1f} /s\n"
            f"  swaps declined     {timings['declined_per_s']:7.1f} /s"
            "   (renderer was reading the other buffer)\n\n"
            "TIMINGS (measured, CPU)\n"
            f"  renderer                     {self._renderer_name()}\n"
            f"  simulation tick (logic)      {timings['tick']:8.3f} ms\n"
            f"  monster AI update            {timings['ai']:8.3f} ms\n"
            f"  prepare (logic thread)       {timings['prepare']:8.3f} ms\n"
            f"  paint (UI thread, total)     {timings['paint']:8.3f} ms\n"
            f"  draw calls {draw_calls:,}   shadow-map draws {shadow_draws:,}   "
            f"batched draws {batched:,}   "
            f"visible triangles {tris:,}\n"
            f"  entity rows offered {entity_candidates:,}   "
            f"frustum-culled {culled_entities:,}   "
            f"drawn {entity_candidates - culled_entities:,}\n"
            + detail_lines + "\n"
            + self._monster_lines(timings)
            + self._terrain_lines(timings) +
            "PASSES (inclusive)\n" + pass_lines
        )

    def _renderer_name(self):
        """The registry name of the active renderer, or a dash before one exists."""
        view = self.main_window.view_3d
        return view.renderer_mode if view is not None and view.renderer is not None else "-"

    def _monster_lines(self, timings):
        """The MONSTER AI section of the dashboard: what the dense pass did."""
        m = self.monsters
        if m is None:
            return "MONSTER AI\n  (no MonsterTable — not in play mode)\n\n"
        n = int(m.count)
        age = (time.perf_counter() - self.monster_copied_at) * 1000.0
        mbytes, _ = _num_bytes(m)
        lines = [
            "MONSTER AI (MonsterTable)",
            f"  pass                 {m.path or 'not run'}",
            f"  rows={n:,}  capacity={len(m.pos):,}  dense bytes={mbytes:,}  "
            f"rows read last tick={int(m.rows_read):,}  "
            f"copy age {age:.0f} ms",
            f"  update (AI thread)   {timings['ai']:8.3f} ms   "
            f"of which waiting for the monster lock "
            f"{timings['ai_lock_wait']:8.3f} ms",
        ]
        if n:
            counts = np.bincount(m.mode[:n], minlength=len(monster_table.MODE_NAMES))
            lines.append("  modes  " + "  ".join(
                f"{name} {int(c):,}" for name, c in
                zip(monster_table.MODE_NAMES, counts) if c))
            teams = ", ".join(m.team_names) or "(none)"
            lines.append(
                f"  line-of-sight rays {int(m.rays_cast):,}   "
                f"shots fired {int(m.fired[:n].sum()):,}   "
                f"rows through Python {int(m.python_rows):,}   teams {teams}")
        if m.phase_ms:
            total = sum(m.phase_ms.values())
            lines.append(f"  dense phases (ms, total {total:.3f})  " + "  ".join(
                f"{name} {ms:.3f}" for name, ms in m.phase_ms.items()))
        return "\n".join(lines) + "\n\n"

    def _terrain_lines(self, timings):
        """The TERRAIN section of the dashboard: residency, build, draw."""
        t, info = self.terrain, self.terrain_info
        if t is None or info is None:
            return ("TERRAIN\n  (no terrain on this map — terrain_table is None)\n\n")
        n = int(t.count)
        live = t.live[:n].astype(bool)
        built = live & t.built[:n].astype(bool)
        dirty = live & t.dirty[:n].astype(bool)
        tbytes, _ = _num_bytes(t)
        lod = ", ".join(f"{int(r)}x{int(r)}: {int(c):,}" for r, c in zip(
            *np.unique(t.grid_res[:n][built], return_counts=True))) or "none built"
        mode = (f"streaming r={info.stream_radius:.0f}" if info.streaming
                else "bounded (whole terrain resident)")
        terrain_ms = float(timings["passes"].get("terrain", 0.0))
        lines = [
            "TERRAIN (TerrainTable)",
            f"  residency   {mode}   resident {int(t.live_count):,}   "
            f"built {int(built.sum()):,}   dirty {int(dirty.sum()):,}   "
            f"slots allocated {n:,}   capacity {len(t.live):,}",
            f"  this frame  drawn {info.drawn:,}   frustum-culled {info.culled:,}   "
            f"triangles drawn {info.drawn_triangles:,} of {info.built_triangles:,} built   "
            f"terrain pass {terrain_ms:.3f} ms",
            f"  LOD grids   {lod}",
            f"  build       budget {info.budget_ms:g} ms/frame, at most "
            f"{info.max_updates} chunks   textures {'on' if info.use_textures else 'off'}   "
            f"grass {'on' if info.grass else 'off'}",
            f"  memory      CPU table {tbytes:,} bytes   GPU heightfield "
            f"{info.pages} page(s) x {info.page_layers} layers = "
            f"{info.gpu_bytes:,} bytes",
        ]
        return "\n".join(lines) + "\n\n"

    def _update_keys(self):
        t = self.render
        slots = getattr(self.snapshot, "visible_brush_slots", None)
        if t is None or slots is None or not len(slots):
            _set_text_preserve_scroll(self.keys_text, "NO VISIBLE RENDERTABLE SLOTS")
            return
        slots = np.asarray(slots, dtype=np.int32)
        cube = (t.class_bits[slots] & rt.CLASS_HAS_GEOMETRY) == 0
        slots = slots[cube]
        if not len(slots):
            _set_text_preserve_scroll(self.keys_text, "NO CUBE RENDER ROWS")
            return
        ids = t.tex_name_id[slots]
        drawn = (ids >= 0) & (ids != rt.TEX_ID_SKIP)
        if getattr(self.snapshot, "is_play_mode", False):
            drawn &= ids != rt.TEX_ID_NODRAW
        row, face = np.nonzero(drawn)
        if not len(row):
            _set_text_preserve_scroll(self.keys_text, "NO DRAWABLE FACES")
            return
        texture = ids[row, face].astype(np.int64)
        face = face.astype(np.int64)
        layout = KeyLayout([("texture", 32), ("face", 3)])
        keys = layout.pack(texture=texture, face=face)
        order, starts = sort_into_runs(keys)
        sorted_keys = keys[order]
        unique, counts = np.unique(sorted_keys, return_counts=True)
        lines = [
            "RENDER-KEY MICROSCOPE",
            "",
            "Logical key layout: [ texture-name-id:32 | cube-face:3 ]",
            "The renderer's final brush key substitutes the resolved GL "
            "texture id for texture-name-id.",
            "",
            f"visible cube rows   {len(slots):,}",
            f"drawable faces      {len(keys):,}",
            f"contiguous runs     {len(starts)-1:,}",
            "",
            "RUNS",
        ]
        max_runs = min(len(starts) - 1, 80)
        for i in range(max_runs):
            a, b = int(starts[i]), int(starts[i+1])
            key = int(sorted_keys[a])
            tex = int(layout.field(np.asarray([key]), "texture")[0])
            f = int(layout.field(np.asarray([key]), "face")[0])
            lines.append(
                f"  {i:03d}  rows {a:5d}-{b-1:5d}  n={b-a:4d}  "
                f"key=0x{key:09X}  tex={tex:5d} face={f}"
            )
        if len(starts) - 1 > max_runs:
            lines.append(f"  ... {len(starts)-1-max_runs:,} more runs")
        lines += ["", "KEY DISTRIBUTION"]
        max_items = min(len(unique), 32)
        peak = int(counts.max()) if len(counts) else 1
        for key, count in zip(unique[:max_items], counts[:max_items]):
            bar = "█" * max(1, int(30 * int(count) / peak))
            lines.append(
                f"  0x{int(key):09X} {bar:<30} {int(count):,}"
            )
        text = "\n".join(lines)
        if text != self._last_keys_text:
            _set_text_preserve_scroll(self.keys_text, text)
            self._last_keys_text = text

    def _update_memory(self):
        lines = [
            "DENSE MEMORY MAP", "",
            "TABLE / FIELD                         SHAPE                 DTYPE       BYTES"
        ]
        for label, table in self._tables():
            lines.append("")
            lines.append(label)
            count = int(table.count)
            for name, value in _array_fields(table):
                shown_shape = tuple(value[:count].shape) if value.ndim else ()
                lines.append(
                    f"  {name:<30} {str(shown_shape):<20} "
                    f"{str(value.dtype):<10} {int(value.nbytes):>10,}"
                )
        text = "\n".join(lines)
        if text != self._last_memory_text:
            _set_text_preserve_scroll(self.memory_text, text)
            self._last_memory_text = text

    def _followed_rows(self):
        """``(id, {table label: row})`` for the primary selection.

        Without a selection, or one with no id, ``(None, reason)``.
        """
        selected = next(
            iter(getattr(self.main_window.state,
                         "selected_objects", []) or []),
            None,
        )
        if selected is None:
            return None, "NO SELECTION"
        props = selected if isinstance(selected, dict) else getattr(
            selected, "properties", {}
        )
        ident = props.get("id") if isinstance(props, dict) else None
        if not ident:
            return None, "SELECTION HAS NO ID"
        rows = {}
        for label, table in self._tables():
            slot = table.slot_of_id.get(ident)
            if slot is not None and int(slot) < int(table.count):
                rows[label] = int(slot)
        return ident, rows

    def _follow_chain(self):
        """The selection traced through the copied tables, as one line.

        ``authored id -> render-row -> key -> run`` for a brush (key and run
        when the row is visible), ``-> entity-row -> sprite-key`` for an
        entity, ``-> monster-row`` in play; or why there is no chain.
        """
        ident, rows = self._followed_rows()
        if ident is None:
            return rows
        rslot, eslot, mslot = (rows.get(label) for label in
                               ("RenderTable", "EntityTable", "MonsterTable"))
        chain = [f"FOLLOW id={ident}"]
        if rslot is None and eslot is None and mslot is None:
            chain.append("ID NOT PRESENT IN RENDERTABLE, ENTITYTABLE OR MONSTERTABLE")
            return " -> ".join(chain)
        if rslot is not None:
            chain.append(f"render-row={int(rslot)}")
            slots = np.asarray(
                getattr(self.snapshot, "visible_brush_slots", []),
                dtype=np.int32,
            )
            if len(slots) and bool((slots == int(rslot)).any()):
                tex = self.render.tex_name_id[int(rslot)]
                drawable = (tex >= 0) & (tex != rt.TEX_ID_SKIP)
                if getattr(self.snapshot, "is_play_mode", False):
                    drawable &= tex != rt.TEX_ID_NODRAW
                faces = np.flatnonzero(drawable)
                if len(faces):
                    face = int(faces[0])
                    key_layout = KeyLayout([("texture", 32), ("face", 3)])
                    logical_key = int(key_layout.pack(
                        texture=np.asarray([int(tex[face])], dtype=np.int64),
                        face=np.asarray([face], dtype=np.int64),
                    )[0])
                    vis = self.render.tex_name_id[slots]
                    mask = (vis >= 0) & (vis != rt.TEX_ID_SKIP)
                    if getattr(self.snapshot, "is_play_mode", False):
                        mask &= vis != rt.TEX_ID_NODRAW
                    rr, ff = np.nonzero(mask)
                    run = -1
                    if len(rr):
                        logical_keys = key_layout.pack(
                            texture=vis[rr, ff].astype(np.int64),
                            face=ff.astype(np.int64),
                        )
                        order, starts = sort_into_runs(logical_keys)
                        sorted_keys = logical_keys[order]
                        positions = np.flatnonzero(sorted_keys == logical_key)
                        if len(positions):
                            pos = int(positions[0])
                            run = int(np.searchsorted(
                                starts, pos, side="right") - 1)
                    chain.append(f"key=0x{logical_key:09X}")
                    if run >= 0:
                        chain.append(f"run={run}")
        if eslot is not None:
            chain.append(f"entity-row={int(eslot)}")
            key_id = int(self.entities.sprite_key_id[int(eslot)])
            if key_id >= 0:
                chain.append(f"sprite-key={key_id}")
        if mslot is not None and int(mslot) < int(self.monsters.count):
            row = int(mslot)
            mode = int(self.monsters.mode[row])
            name = (monster_table.MODE_NAMES[mode]
                    if mode < len(monster_table.MODE_NAMES) else str(mode))
            chain.append(f"monster-row={row} mode={name}")
            target = int(self.monsters.target[row])
            if target == monster_table.TARGET_PLAYER:
                chain.append("target=player")
            elif target >= 0:
                chain.append(f"target=monster-row {target}")
        return " -> ".join(chain)

    def _update_follow(self):
        """Append the FOLLOW chain to the status line the dashboard just set.

        Every refresh rewrites the status line, so the chain is appended every
        time, not only when it changes.
        """
        if not self.follow.isChecked():
            for raw in self._raw_views().values():
                raw.set_highlight(None)
            return
        ident, rows = self._followed_rows()
        found = rows if ident is not None else {}
        for label, raw in self._raw_views().items():
            raw.set_highlight(found.get(label))
        _set_text_preserve_scroll(self.selection_text, self._selection_dump(ident, rows))
        chain = self._follow_chain()
        if not chain.startswith("FOLLOW"):
            chain = "FOLLOW: " + chain
        self.status.setText(
            self.status.text().split("  |  FOLLOW")[0] + "  |  " + chain)


    def _raw_views(self):
        return {"RenderTable": self.render_raw, "EntityTable": self.entity_raw,
                "MonsterTable": self.monster_raw, "TerrainTable": self.terrain_raw}

    def _selection_dump(self, ident, rows):
        """The followed object's dense representation, column by column."""
        if ident is None:
            return f"SELECTION\n\n{rows}"
        lines = [f"SELECTION  id={ident}", ""]
        if not rows:
            lines.append("ID NOT PRESENT IN RENDERTABLE, ENTITYTABLE OR MONSTERTABLE")
        tables = dict(self._tables())
        for label, row in rows.items():
            lines.append(f"{label}  row {row}")
            for name, value in _array_fields(tables[label]):
                if value.ndim == 0 or row >= len(value):
                    continue
                cell = value[row]
                shown = (" ".join(_fmt(v) for v in np.ravel(cell))
                         if isinstance(cell, np.ndarray) else _fmt(cell))
                lines.append(f"  {name:<28} {shown}")
            lines.append("")
        return "\n".join(lines)

    def _selection_changed(self):
        """A new selection: follow it now, from the tables already copied."""
        if self.render is not None and self.entities is not None:
            self._update_follow()


_INSTANCE = None


def show_debug_tables(main_window):
    """Open or raise the one live table instrument attached to *main_window*."""
    global _INSTANCE
    if _INSTANCE is not None:
        try:
            if _INSTANCE.main_window is main_window:
                _INSTANCE.show()
                _INSTANCE.raise_()
                _INSTANCE.activateWindow()
                return _INSTANCE
        except RuntimeError:
            pass
    _INSTANCE = DebugTablesWindow(main_window)
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE
