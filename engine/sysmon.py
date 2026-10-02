"""System Monitor widget for QtGameView.

PERFORMANCE-OPTIMIZED:
  - Pre-allocated numpy ring buffer for frame times (O(1) append)
  - Incremental max tracking (no deque scan)
  - FPS string cached (only rebuilds when value changes)
  - 60-point graph resolution
  - Pre-allocated QPoint array for graph (zero per-frame allocation)
  - All stats text and metrics cached (rebuilt once/sec)
  - All drawing via QPainter directly (no QPixmap cache, no raw GL)
"""

import time

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import Qt, QRect, QPoint
from PyQt5.QtGui import (
    QPainter, QColor, QFont, QPen, QBrush, QPolygon, QFontMetrics
)
import OpenGL.GL as gl
import numpy as np

# OpenGL GPU memory query constants
GL_GPU_MEM_INFO_TOTAL_AVAILABLE_MEM_NVX = 0x9048
GL_GPU_MEM_INFO_CURRENT_AVAILABLE_MEM_NVX = 0x9049
GL_GPU_MEM_INFO_DEDICATED_VIDMEM_NVX = 0x9047
GL_TEXTURE_FREE_MEMORY_ATI = 0x87FC
GL_RENDERBUFFER_FREE_MEMORY_ATI = 0x87FD


class SysMon:
    """Self-contained system-monitor overlay — performance-optimized.

    Usage (inside QtGameView):
        self.sysmon = SysMon(self)          # self == QtGameView instance
        # in paintGL:
        if self.sysmon.is_active():
            self.sysmon.draw(painter, fps, logic_thread, renderer, editor_state, terrain)
        # in mouse events, key events, etc.:
        self.sysmon.handle_mouse_press(event, play_mode)
        self.sysmon.handle_mouse_move(event, play_mode, view_width, view_height)
        self.sysmon.handle_mouse_release(event, play_mode)
        self.sysmon.toggle()
    """

    # Graph configuration
    GRAPH_POINTS = 60
    GRAPH_WIDTH = 390
    GRAPH_HEIGHT = 50
    CACHE_INTERVAL_SEC = 1.0    # Stats text cache rebuild interval

    def __init__(self, parent_view):
        self.parent = parent_view

        self.expanded = False
        self.stats = {
            'visible_brushes': 0,
            'visible_tris': 0,
            'visible_surfaces': 0,
            'culled_brushes': 0,
            'culled_tris': 0,
            'culled_surfaces': 0,
            'total_brushes': 0,
        }
        self.dragging = False
        self.drag_offset = QPoint(0, 0)
        self.active = False
        self.window_rect = QRect(20, 20, 400, 200)

        # Frame time ring buffer (pre-allocated numpy array)
        self._ft_buffer = np.zeros(self.GRAPH_POINTS, dtype=np.float32)
        self._ft_index = 0
        self._ft_count = 0
        self._ft_max = 16.67
        self._ft_max_age = 0

        # Font & metrics
        self.font = QFont("Arial", 9)
        self.font.setStyleHint(QFont.Monospace)
        self._font_metrics = QFontMetrics(self.font)

        # Pre-allocated graph points (eliminates per-frame QPoint creation)
        self._graph_points = [QPoint(0, 0) for _ in range(self.GRAPH_POINTS)]
        self._graph_poly = QPolygon(self._graph_points + [QPoint(0, 0), QPoint(0, 0)])

        # --- Caching systems ---

        # VRAM cache
        self._vram_cache = (None, None)
        self._vram_cache_time = 0

        # FPS string cache
        self._fps_str = ""
        self._fps_cached_val = -1
        self._ft_cached_val = -1.0

        # Stats text cache (rebuilt once/sec)
        self._stats_cache = {}
        self._stats_cache_time = 0

        # Pre-computed colors (avoid per-frame QColor creation)
        self._bg_color = QColor(20, 20, 25, 235)
        self._border_color = QColor(80, 80, 90)
        self._header_color = QColor(66, 95, 93)
        self._text_color = QColor(220, 220, 220)
        self._graph_bg_color = QColor(10, 10, 15, 200)
        self._graph_line_color = QColor(100, 255, 100)
        self._graph_fill_color = QColor(100, 200, 100, 40)
        self._white = QColor(255, 255, 255)
        self._vram_color = QColor(255, 200, 100)
        self._green = QColor(50, 200, 50)
        self._grey = QColor(180, 180, 180)

        # Reusable pens/brushes (avoid per-frame creation)
        self._border_pen = QPen(self._border_color, 1)
        self._bg_brush = QBrush(self._bg_color)
        self._header_brush = QBrush(self._header_color)
        self._graph_bg_brush = QBrush(self._graph_bg_color)
        self._graph_fill_brush = QBrush(self._graph_fill_color)
        self._graph_line_pen = QPen(self._graph_line_color, 1)
        self._no_pen = QPen(Qt.NoPen)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def toggle(self):
        self.active = not self.active
        return self.active

    def set_active(self, active):
        self.active = active

    def is_active(self):
        return self.active

    def set_expanded(self, expanded):
        if self.expanded != expanded:
            self.expanded = expanded

    def record_frame_time(self, delta_ms):
        """Record a frame time. Uses ring buffer — O(1)."""
        old_val = self._ft_buffer[self._ft_index]
        self._ft_buffer[self._ft_index] = delta_ms
        self._ft_index = (self._ft_index + 1) % self.GRAPH_POINTS
        if self._ft_count < self.GRAPH_POINTS:
            self._ft_count += 1

        # Incremental max tracking
        if delta_ms >= self._ft_max:
            self._ft_max = delta_ms
            self._ft_max_age = self._ft_index
        elif self._ft_max_age == self._ft_index:
            if self._ft_count > 0:
                count = self._ft_count
                self._ft_max = np.max(self._ft_buffer[:count])
                self._ft_max_age = int(np.argmax(self._ft_buffer[:count]))
            else:
                self._ft_max = 16.67

    def update_stats(self, visible_brushes=0, culled_brushes=0, total_brushes=0):
        self.stats['visible_brushes'] = visible_brushes
        self.stats['culled_brushes'] = culled_brushes
        self.stats['total_brushes'] = total_brushes

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def handle_mouse_press(self, event, play_mode):
        if play_mode or not self.active or event.button() != Qt.LeftButton:
            return False
        if not self.window_rect.contains(event.pos()):
            return False

        # Close button
        if (event.x() > self.window_rect.right() - 25
                and event.y() < self.window_rect.y() + 25):
            self.active = False
            return True

        # Title bar
        title_bar = QRect(self.window_rect.x(), self.window_rect.y(),
                          self.window_rect.width(), 25)
        if title_bar.contains(event.pos()):
            if event.x() > self.window_rect.right() - 50:
                self.expanded = not self.expanded
                return True
            self.dragging = True
            self.drag_offset = event.pos() - QPoint(self.window_rect.x(),
                                                    self.window_rect.y())
            return True
        return True

    def handle_mouse_move(self, event, play_mode, view_width, view_height):
        if play_mode or not self.dragging:
            return False
        new_pos = event.pos() - self.drag_offset
        new_x = max(5, min(new_pos.x(), view_width - self.window_rect.width() - 5))
        new_y = max(5, min(new_pos.y(), view_height - 120))
        self.window_rect.moveTo(new_x, new_y)
        return True

    def handle_mouse_release(self, event, play_mode):
        if play_mode or not self.dragging or event.button() != Qt.LeftButton:
            return False
        self.dragging = False
        return True

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------

    def _rebuild_stats_cache(self, fps, logic_thread, renderer, editor_state, terrain):
        """Recompute all expensive stats. Called once per second."""
        cache = {}
        fm = self._font_metrics

        brush_tris = len(editor_state.brushes) * 12
        terrain_total_tris = 0
        terrain_visible_tris = 0
        if terrain is not None:
            terrain_total_tris = terrain.get_tri_count()
            terrain_visible_tris = terrain.total_triangles

        total_tris = brush_tris + terrain_total_tris
        visible_tris = (renderer.render_stats.visible_tris if renderer else 0) + terrain_visible_tris

        cache['total_tris'] = total_tris
        cache['visible_tris'] = visible_tris

        vram_used, vram_total = self._get_vram_info()
        cache['vram_str'] = self._format_memory(vram_used, vram_total)

        cache['things_count'] = len(editor_state.things)
        cache['draw_calls'] = renderer.render_stats.draw_calls if renderer else 0
        cache['total_brushes'] = self.stats.get('total_brushes', 0)
        cache['visible_brushes'] = self.stats.get('visible_brushes', 0)
        cache['culled_brushes'] = self.stats.get('culled_brushes', 0)
        cache['tps'] = getattr(logic_thread, 'actual_tps', 0.0)

        cache['brush_text'] = f"Brushes:  {cache['total_brushes']} "
        cache['tri_text'] = f"Tris:     {total_tris} "
        cache['visible_brush_text'] = f"(Visible: {cache['visible_brushes']})"
        cache['visible_tri_text'] = f"(Visible: {visible_tris})"
        cache['things_text'] = f"Things:   {cache['things_count']}"
        cache['draws_text'] = f"Draws:    {cache['draw_calls']}"
        cache['vram_text'] = f"VRAM:     {cache['vram_str']}"
        cache['tps_text'] = f"TPS: {cache['tps']:.1f}"

        culled = cache['culled_brushes']
        total = cache['total_brushes']
        cull_pct = (culled / total * 100) if total > 0 else 0
        cache['cull_text'] = f"Culled: {cull_pct:.1f}%"

        cache['brush_text_width'] = fm.horizontalAdvance(cache['brush_text'])
        cache['tri_text_width'] = fm.horizontalAdvance(cache['tri_text'])
        cache['tps_text_width'] = fm.horizontalAdvance(cache['tps_text'])

        self._stats_cache = cache
        self._stats_cache_time = time.perf_counter()

    def _get_fps_str(self, current_ft, fps):
        """Cache FPS string — only rebuild when values change meaningfully."""
        fps_int = int(fps)
        ft_tenths = round(current_ft, 1)
        if fps_int != self._fps_cached_val or ft_tenths != self._ft_cached_val:
            self._fps_cached_val = fps_int
            self._ft_cached_val = ft_tenths
            self._fps_str = f"Frame: {ft_tenths:.1f}ms  FPS: {fps_int}"
        return self._fps_str

    def _draw_graph(self, painter, graph_x, graph_y):
        """Draw graph line using pre-allocated QPoint array — zero allocation."""
        if self._ft_count < 2:
            return

        count = self._ft_count
        max_ft = max(self._ft_max, 16.67)
        step = self.GRAPH_WIDTH / max(count - 1, 1)
        buf = self._ft_buffer
        idx = self._ft_index
        gh = self.GRAPH_HEIGHT

        # Mutate pre-allocated QPoint objects in-place
        points = self._graph_points
        for i in range(count):
            buf_idx = (idx - count + i) % self.GRAPH_POINTS
            ft = buf[buf_idx]
            points[i].setX(int(graph_x + i * step))
            points[i].setY(int(graph_y + gh - (ft / max_ft) * gh))

        # Fill polygon (reuse pre-allocated poly)
        poly = self._graph_poly
        for i in range(count):
            poly.setPoint(i, points[i])
        poly.setPoint(count, QPoint(points[count - 1].x(), graph_y + gh))
        poly.setPoint(count + 1, QPoint(points[0].x(), graph_y + gh))

        painter.setPen(self._no_pen)
        painter.setBrush(self._graph_fill_brush)
        painter.drawPolygon(poly)

        # Line
        painter.setPen(self._graph_line_pen)
        painter.drawPolyline(QPolygon(points[:count]))

    # ------------------------------------------------------------------
    # Drawing — ALWAYS draws every frame called, no early returns
    # ------------------------------------------------------------------

    def draw(self, painter, fps, logic_thread, renderer, editor_state, terrain):
        """Draw the sysmon overlay. Always paints — caller controls call rate."""
        target_height = 275 if self.expanded else 30
        rect = QRect(self.window_rect.x(), self.window_rect.y(),
                     self.window_rect.width(), target_height)

        # --- Background ---
        painter.setPen(self._border_pen)
        painter.setBrush(self._bg_brush)
        painter.drawRect(rect)

        # --- Header ---
        header_rect = QRect(rect.x(), rect.y(), rect.width(), 25)
        painter.fillRect(header_rect, self._header_color)
        painter.setPen(self._border_pen)
        painter.drawLine(rect.x(), rect.y() + 25, rect.right(), rect.y() + 25)

        painter.setFont(self.font)
        painter.setPen(self._white)
        painter.drawText(header_rect.adjusted(10, 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, "SysMon [F3]")
        painter.drawText(QRect(rect.right() - 50, rect.y(), 25, 25),
                         Qt.AlignCenter, "▼" if self.expanded else "▶")
        painter.drawText(QRect(rect.right() - 25, rect.y(), 25, 25),
                         Qt.AlignCenter, "[X]")

        if not self.expanded:
            return

        # --- Graph background ---
        graph_x = rect.x() + 5
        graph_y = rect.y() + 30
        painter.setPen(self._border_pen)
        painter.setBrush(self._graph_bg_brush)
        painter.drawRect(graph_x, graph_y, self.GRAPH_WIDTH, self.GRAPH_HEIGHT)

        # --- Graph line ---
        self._draw_graph(painter, graph_x, graph_y)

        # --- Stats text (cached once/sec) ---
        now = time.perf_counter()
        if now - self._stats_cache_time >= self.CACHE_INTERVAL_SEC:
            self._rebuild_stats_cache(fps, logic_thread, renderer, editor_state, terrain)
        cache = self._stats_cache

        left_margin = rect.x() + 10
        stats_start_y = graph_y + self.GRAPH_HEIGHT + 25
        line_height = 18
        current_ft = self._ft_buffer[(self._ft_index - 1) % self.GRAPH_POINTS] if self._ft_count > 0 else 0

        # FPS (cached string)
        painter.setPen(self._white)
        painter.drawText(left_margin, stats_start_y, self._get_fps_str(current_ft, fps))

        # Cached stats
        painter.setPen(self._vram_color)
        painter.drawText(left_margin, stats_start_y + line_height * 1,
                         cache.get('vram_text', 'VRAM:     N/A'))

        painter.setPen(self._text_color)
        painter.drawText(left_margin, stats_start_y + line_height * 2,
                         cache.get('things_text', 'Things:   0'))
        painter.drawText(left_margin, stats_start_y + line_height * 3,
                         cache.get('draws_text', 'Draws:    0'))
        painter.drawText(left_margin, stats_start_y + line_height * 4,
                         cache.get('brush_text', 'Brushes:  0 '))
        painter.drawText(left_margin, stats_start_y + line_height * 5,
                         cache.get('tri_text', 'Tris:     0 '))

        painter.setPen(self._green)
        painter.drawText(left_margin + cache.get('brush_text_width', 0),
                         stats_start_y + line_height * 4,
                         cache.get('visible_brush_text', '(Visible: 0)'))
        painter.drawText(left_margin + cache.get('tri_text_width', 0),
                         stats_start_y + line_height * 5,
                         cache.get('visible_tri_text', '(Visible: 0)'))

        painter.setPen(self._grey)
        painter.drawText(left_margin, rect.bottom() - 10,
                         cache.get('cull_text', 'Culled: 0.0%'))
        painter.drawText(rect.right() - cache.get('tps_text_width', 0) - 10,
                         rect.bottom() - 10,
                         cache.get('tps_text', 'TPS: 0.0'))

    # ------------------------------------------------------------------
    # VRAM helper
    # ------------------------------------------------------------------

    def _get_vram_info(self):
        """Get GPU VRAM info. Cached to avoid per-frame GL queries."""
        current_time = time.perf_counter()
        if current_time - getattr(self, '_vram_cache_time', 0) < 1.0:
            return getattr(self, '_vram_cache', (None, None))

        result = (None, None)
        try:
            total_kb = gl.glGetIntegerv(GL_GPU_MEM_INFO_DEDICATED_VIDMEM_NVX)
            available_kb = gl.glGetIntegerv(GL_GPU_MEM_INFO_CURRENT_AVAILABLE_MEM_NVX)
            gl.glGetError()
            if total_kb > 0:
                total_mb = total_kb // 1024
                used_mb = total_mb - (available_kb // 1024)
                result = (used_mb, total_mb)
        except Exception:
            gl.glGetError()
            try:
                mem_info = gl.glGetIntegerv(GL_TEXTURE_FREE_MEMORY_ATI)
                gl.glGetError()
                if mem_info and len(mem_info) > 0 and mem_info[0] > 0:
                    free_mb = mem_info[0] // 1024
                    result = (None, free_mb)
            except Exception:
                gl.glGetError()

        self._vram_cache = result
        self._vram_cache_time = current_time
        return result

    def _format_memory(self, used_mb, total_mb):
        if used_mb is None and total_mb is None:
            return "N/A"
        if used_mb is None:
            return f"{total_mb}MB free"
        if total_mb is None:
            return f"{used_mb}MB used"
        pct = (used_mb / total_mb * 100) if total_mb > 0 else 0
        return f"{used_mb}/{total_mb}MB ({pct:.0f}%)"
