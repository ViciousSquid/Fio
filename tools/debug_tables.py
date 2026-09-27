"""Live numerical instrument panel for Fio's dense render projections.

Loaded only when Tools -> Debug Tables is invoked. The window reads the
published render-state snapshot; it does not add a second world representation
or alter the logic/render hot path.
"""
from __future__ import annotations

import time

import numpy as np
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PyQt5.QtGui import QFont, QPainter
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
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


def _num_bytes(table):
    total = 0
    arrays = []
    for name in getattr(table, "__slots__", ()):
        value = getattr(table, name, None)
        if isinstance(value, np.ndarray):
            total += int(value.nbytes)
            arrays.append((name, value))
    return total, arrays


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
            a = a.reshape(a.shape[0], -1)
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
        fields = []
        for name in getattr(table, "__slots__", ()):
            value = getattr(table, name, None)
            if isinstance(value, np.ndarray) and value.ndim >= 1 and value.size:
                fields.append(name)
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
            width = shown.reshape(shown.shape[0], -1).shape[1]
            names = [f"{name}[{i}]" for i in range(width)]
        self.model.set_array(shown, names=names)


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
        for i, (label, value) in enumerate(self.items):
            y = 4 + i * row_h
            width = int((self.width() - 180) * value / maxv)
            p.setPen(Qt.NoPen)
            p.setBrush(Qt.gray)
            p.drawRect(150, y + 3, max(1, width), row_h - 7)
            p.setPen(Qt.lightGray)
            p.drawText(8, y + row_h - 8, str(label)[:22])
            p.drawText(156 + width, y + row_h - 8, f"{value:,}")


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
        refresh = QPushButton("REFRESH NOW")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(controls)
        layout.addWidget(tabs, 1)
        self.setCentralWidget(root)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.destroyed.connect(self._stop)
        self.refresh()

    def _set_always_on_top(self, checked):
        flags = self.windowFlags()
        flags.setFlag(Qt.WindowStaysOnTopHint, bool(checked))
        self.setWindowFlags(flags)
        self.show()

    def _stop(self, *_):
        if hasattr(self, "timer"):
            self.timer.stop()

    def _state(self):
        view = getattr(self.main_window, "view_3d", None)
        logic = getattr(view, "logic_thread", None)
        if logic is None:
            return None
        game_state = getattr(logic, "game_state", None)
        if game_state is None:
            return None
        try:
            return game_state.get_render_state()
        except Exception:
            return None

    def refresh(self):
        started = time.perf_counter()
        snap = self._state()
        if snap is None:
            self.status.setText("DETACHED — no LogicThread/render state")
            return
        self.snapshot = snap
        self.render = getattr(snap, "render_table", None)
        self.entities = getattr(snap, "entity_table", None)
        if self.render is None or self.entities is None:
            self.status.setText("ATTACHED — dense tables not published yet")
            return

        self.render_raw.update_table(self.render)
        self.entity_raw.update_table(self.entities)
        self._update_dashboard(started)
        self._update_keys()
        self._update_memory()
        self._update_follow()

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
        sample_ms = (time.perf_counter() - started) * 1000.0
        frame_time = time.time() - float(
            getattr(self.snapshot, "timestamp", time.time())
        )
        self.status.setText(
            f"ATTACHED  |  frame age {frame_time*1000:.1f} ms  | "
            f"inspector sample {sample_ms:.2f} ms"
        )
        self.bars.set_items([
            ("RenderTable rows", int(self.render.count)),
            ("RenderTable capacity", render_cap),
            ("EntityTable rows", int(self.entities.count)),
            ("EntityTable capacity", entity_cap),
            ("render draw calls", draw_calls),
            ("batched draws", batched),
            ("visible triangles", tris),
        ])
        self.dashboard.setText(
            "PIPELINE / PUBLISHED STATE\n\n"
            f"RenderTable   rows={int(self.render.count):,}  "
            f"capacity={render_cap:,}  dense bytes={rbytes:,}\n"
            f"EntityTable   rows={int(self.entities.count):,}  "
            f"capacity={entity_cap:,}  dense bytes={ebytes:,}\n"
            f"TOTAL NUMERICAL STORAGE (ndarrays)  {total:,} bytes "
            f"({total/1024/1024:.2f} MiB)\n\n"
            "Published path\n"
            "  world / LogicThread\n"
            "       -> RenderTable + EntityTable\n"
            "       -> visible integer slots\n"
            "       -> renderer key generation / sorting\n"
            "       -> contiguous runs\n"
            "       -> instanced GL submission\n\n"
            "TIMINGS\n"
            "  Engine stage timings are intentionally not fabricated here.\n"
            "  This build exposes live row counts, memory, frame age and "
            "renderer submission counters."
        )

    def _update_keys(self):
        t = self.render
        slots = getattr(self.snapshot, "visible_brush_slots", None)
        if t is None or slots is None or not len(slots):
            self.keys_text.setText("NO VISIBLE RENDERTABLE SLOTS")
            return
        slots = np.asarray(slots, dtype=np.int32)
        cube = (t.class_bits[slots] & rt.CLASS_HAS_GEOMETRY) == 0
        slots = slots[cube]
        if not len(slots):
            self.keys_text.setText("NO CUBE RENDER ROWS")
            return
        ids = t.tex_name_id[slots]
        drawn = (ids >= 0) & (ids != rt.TEX_ID_SKIP)
        if getattr(self.snapshot, "is_play_mode", False):
            drawn &= ids != rt.TEX_ID_NODRAW
        row, face = np.nonzero(drawn)
        if not len(row):
            self.keys_text.setText("NO DRAWABLE FACES")
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
        self.keys_text.setText("\n".join(lines))

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
            for name in getattr(table, "__slots__", ()):
                value = getattr(table, name, None)
                if not isinstance(value, np.ndarray):
                    continue
                shown_shape = tuple(value[:count].shape) if value.ndim else ()
                lines.append(
                    f"  {name:<30} {str(shown_shape):<20} "
                    f"{str(value.dtype):<10} {int(value.nbytes):>10,}"
                )
        self.memory_text.setText("\n".join(lines))

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
        self.status.setText(
            self.status.text().split("  |  FOLLOW")[0]
            + f"  |  FOLLOW id={ident}"
            + (f" render-row={int(rslot)}" if rslot is not None else "")
            + (f" entity-row={int(eslot)}" if eslot is not None else "")
        )


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
