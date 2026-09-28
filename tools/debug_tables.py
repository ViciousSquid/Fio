"""Live numerical instrument panel for Fio's dense render projections.

Loaded only when Tools -> Debug Tables is invoked. The window reads the
published render-state snapshot; it does not add a second world representation
or alter the logic/render hot path.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from types import SimpleNamespace

import numpy as np
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PyQt5.QtGui import QFont, QPainter
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QFileDialog,
    QTabWidget, QTableView, QTextBrowser, QVBoxLayout, QWidget,
)

from engine import render_table as rt
from engine.render_keys import KeyLayout, sort_into_runs


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
            if isinstance(value, np.ndarray):
                self.fields[name] = value.copy()
        self.count = int(getattr(table, "count", 0))
        self.slot_of_id = dict(getattr(table, "slot_of_id", {}))
        self.rows_read = int(getattr(table, "rows_read", 0))

    def __getattr__(self, name):
        try:
            return self.__dict__["fields"][name]
        except KeyError:
            raise AttributeError(name) from None


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
        if at_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))

    QTimer.singleShot(0, restore)


class ArrayModel(QAbstractTableModel):
    """Read-only view over one NumPy column/row array."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.array = np.empty((0, 0), dtype=np.float32)
        self.names = []
        self.offset = 0

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

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        return _fmt(self.array[index.row(), index.column()])

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

        def restore():
            if at_bottom:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(value, bar.maximum()))

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
        tabs.addTab(self.render_raw, "RENDERTABLE")
        tabs.addTab(self.entity_raw, "ENTITYTABLE")

        self.keys_text = QTextBrowser()
        tabs.addTab(self.keys_text, "KEY MICROSCOPE")
        self.memory_text = QTextBrowser()
        tabs.addTab(self.memory_text, "MEMORY")

        controls = QHBoxLayout()
        controls.addWidget(QLabel("DENSE NUMERICAL INSTRUMENT"))
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
        self._last_follow_text = None
        self.timer.start()
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
        if getattr(self.snapshot, "is_play_mode", False):
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
        selected = getattr(
            getattr(self.main_window, "state", None),
            "selected_object", None
        )
        if selected is None:
            selected = next(
                iter(getattr(
                    getattr(self.main_window, "state", None),
                    "selected_objects", []
                ) or []),
                None
            )
        if selected is None:
            return "FOLLOW SELECTION\n\nNO SELECTION"
        props = selected if isinstance(selected, dict) else getattr(
            selected, "properties", {}
        )
        ident = props.get("id") if isinstance(props, dict) else None
        if not ident:
            return "FOLLOW SELECTION\n\nSELECTION HAS NO ID"
        lines = [f"FOLLOW id={ident}"]
        rslot = self.render.slot_of_id.get(ident) if self.render is not None else None
        eslot = self.entities.slot_of_id.get(ident) if self.entities is not None else None
        if rslot is not None:
            lines.append(f"render-row={int(rslot)}")
        if eslot is not None:
            lines.append(f"entity-row={int(eslot)}")
            key_id = int(self.entities.sprite_key_id[int(eslot)])
            if key_id >= 0:
                lines.append(f"sprite-key={key_id}")
        if rslot is None and eslot is None:
            lines.append("ID NOT PRESENT IN RENDERTABLE OR ENTITYTABLE")
        return "FOLLOW SELECTION\n\n" + " -> ".join(lines)

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
            getattr(self.main_window.view_3d, "renderer", None),
            "render_stats", None
        )
        pipeline = {
            "format": "fio-debug-tables-v1",
            "export_time_unix": time.time(),
            "snapshot_timestamp": self.snapshot.timestamp,
            "frame_age_ms": self._frame_age_ms(),
            "timings_ms": self._timings(),
            "is_play_mode": bool(getattr(self.snapshot, "is_play_mode", False)),
            "render_rows": int(self.render.count),
            "render_capacity": int(len(self.render.center)),
            "entity_rows": int(self.entities.count),
            "entity_capacity": int(len(self.entities.pos)),
            "render_draw_calls": int(getattr(stats, "draw_calls", 0)) if stats else 0,
            "batched_draws": int(getattr(stats, "batched_draws", 0)) if stats else 0,
            "visible_triangles": int(getattr(stats, "visible_tris", 0)) if stats else 0,
            "render_dense_bytes": int(_num_bytes(self.render)[0]),
            "entity_dense_bytes": int(_num_bytes(self.entities)[0]),
        }

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
        for label, table in (
            ("RenderTable", self.render), ("EntityTable", self.entities)
        ):
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

        render_arrays = self._table_arrays(self.render)
        entity_arrays = self._table_arrays(self.entities)
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

                for label, arrays in (
                    ("RenderTable", render_arrays),
                    ("EntityTable", entity_arrays),
                ):
                    for name, value in arrays.items():
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
        view = getattr(self.main_window, "view_3d", None)
        logic = getattr(view, "logic_thread", None)
        return getattr(logic, "game_state", None) if logic is not None else None

    def refresh(self):
        started = time.perf_counter()
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

        self._update_raw_tables()
        self._update_dashboard(started)
        self._update_keys()
        self._update_memory()
        self._update_follow()

    def _update_raw_tables(self):
        """Refresh raw models only when their selected array actually changed."""
        for raw, table in (
            (self.render_raw, self.render), (self.entity_raw, self.entities)
        ):
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
        stamp = float(getattr(self.snapshot, "timestamp", 0.0))
        return max(0.0, (time.perf_counter() - stamp) * 1000.0) if stamp else 0.0

    def _timings(self):
        """Measured stage timings: the logic prepare, the paint, each pass."""
        view = getattr(self.main_window, "view_3d", None)
        stats = getattr(getattr(view, "renderer", None), "render_stats", None)
        game_state = self._game_state()
        now = time.perf_counter()
        published = int(getattr(game_state, "published_frames", 0))
        declined = int(getattr(game_state, "declined_swaps", 0))
        last = getattr(self, "_last_counters", None)
        rates = (0.0, 0.0)
        if last is not None and now > last[0]:
            span = now - last[0]
            rates = ((published - last[1]) / span, (declined - last[2]) / span)
        self._last_counters = (now, published, declined)
        return {
            "prepare": float(getattr(self.snapshot, "prepare_ms", 0.0)),
            "paint": float(getattr(view, "paint_ms", 0.0)),
            "passes": dict(getattr(stats, "pass_ms", {}) or {}),
            "published_per_s": rates[0],
            "declined_per_s": rates[1],
            "render_rows_read": int(getattr(self.render, "rows_read", 0)),
            "entity_rows_read": int(getattr(self.entities, "rows_read", 0)),
        }

    def _update_dashboard(self, started):
        rbytes, _ = _num_bytes(self.render)
        ebytes, _ = _num_bytes(self.entities)
        render_cap = len(self.render.center)
        entity_cap = len(self.entities.pos)
        stats = getattr(
            getattr(self.main_window.view_3d, "renderer", None),
            "render_stats", None
        )
        draw_calls = int(getattr(stats, "draw_calls", 0)) if stats else 0
        batched = int(getattr(stats, "batched_draws", 0)) if stats else 0
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
            f"  prepare (logic thread)       {timings['prepare']:8.3f} ms\n"
            f"  paint (UI thread, total)     {timings['paint']:8.3f} ms\n"
            f"  draw calls {draw_calls:,}   batched draws {batched:,}   "
            f"visible triangles {tris:,}\n\n"
            "PASSES (inclusive)\n" + pass_lines
        )

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
        for label, table in (
            ("RenderTable", self.render), ("EntityTable", self.entities)
        ):
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

    def _update_follow(self):
        if not self.follow.isChecked():
            return
        selected = getattr(
            getattr(self.main_window, "state", None),
            "selected_object", None
        )
        if selected is None:
            selected = next(
                iter(getattr(
                    getattr(self.main_window, "state", None),
                    "selected_objects", []
                ) or []),
                None
            )
        if selected is None:
            return
        props = selected if isinstance(selected, dict) else getattr(
            selected, "properties", {}
        )
        ident = props.get("id") if isinstance(props, dict) else None
        if not ident:
            return
        rslot = self.render.slot_of_id.get(ident) if self.render is not None else None
        eslot = self.entities.slot_of_id.get(ident) if self.entities is not None else None
        if rslot is None and eslot is None:
            return
        chain = [f"FOLLOW id={ident}"]
        if rslot is not None:
            chain.append(f"render-row={int(rslot)}")
            slots = np.asarray(
                getattr(self.snapshot, "visible_brush_slots", []),
                dtype=np.int32,
            )
            if len(slots) and int(rslot) in set(int(x) for x in slots):
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
        follow_text = " -> ".join(chain)
        if follow_text != self._last_follow_text:
            self.status.setText(
                self.status.text().split("  |  FOLLOW")[0]
                + "  |  " + follow_text
            )
            self._last_follow_text = follow_text


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
