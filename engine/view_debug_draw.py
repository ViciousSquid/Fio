"""Debug and editor-aid drawing of the 3D view, delegated from QtGameView.

The I/O connection records behind the logic-link lines, bullet marks,
player glasses and projectiles handed to the renderer, monster debug rays,
the spatial grid, and the position window.
"""

import atexit
from engine.asset_archives import asset_exists, asset_listdir
from engine.asset_qt import add_application_font, load_pixmap
import contextlib
import io
import time
import zipfile
import weakref
import os
import sys
import math
from collections import deque
import numpy as np
import ctypes
from typing import Optional
from PyQt5 import sip
from PyQt5.QtWidgets import QOpenGLWidget, QApplication, QLineEdit
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, QEvent, QThread, pyqtSignal
from PyQt5.QtGui import QOpenGLContext, QPainter, QColor, QFont, QCursor, QPen, QBrush, QKeySequence, QPixmap, QSurfaceFormat, QFontMetrics, QImage, QLinearGradient, QFontDatabase
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
import glm
from engine.camera import Camera
from editor.things import (
    Thing, Light, PlayerStart, Monster, Prop, Speaker,
    LogicGate, LogicRelay, LogicTimer, LevelChanger, Portal
)
from engine.sdl_runtime import init_input as init_sdl_input
from engine.player import Player
from engine.renderer import DEFAULT_RENDERER, available_renderers, create_renderer
from editor.selection_overlay import describe_selection
_HUD_FONT_FILES = {
    1: "Rushfordclean-rgz89.otf",
    2: "O.K.Retro.otf",
    3: "HornetDisplay-Regular.ttf",
    4: "LCDAT&TPhoneTimeDate.ttf",
}
_HUD_FONT_FALLBACKS = {
    1: "Rushford Clean",
    2: "O.K. Retro",
    3: "HornetDisplay",
    4: "LCD AT&T Phone Time/Date",
}
from engine import brush_geometry, brush_placement
from editor import component_edit
from engine.threaded_game_state import ThreadedGameState, RenderState
from engine.view_distance import ViewDistance
from engine.glasses import (
    DEFAULT_GLASSES, DEFAULT_SPRITE_KEY, GLASSES_STYLES, GLASSES_SUBFOLDER,
    glasses_sprite_key, normalize_glasses,
)
from engine.logic_thread import LogicThread
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.debug_console import CommandInput, DebugConsole, debug_log
from .sysmon import SysMon
from .view_audio import ViewAudio
from .terrain_frame import frustum_planes
from .overlay_geometry import (
    collision_wireframes, connection_segments, face_highlight_triangles)
from .floating_windows import CallbackWindow, WindowManager
import pygame
import ctypes
import os
GL_GPU_MEM_INFO_TOTAL_AVAILABLE_MEM_NVX = 0x9048
GL_GPU_MEM_INFO_CURRENT_AVAILABLE_MEM_NVX = 0x9049
GL_GPU_MEM_INFO_DEDICATED_VIDMEM_NVX = 0x9047
GL_TEXTURE_FREE_MEMORY_ATI = 0x87FC
GL_RENDERBUFFER_FREE_MEMORY_ATI = 0x87FD


class ViewDebugDraw:
    def __init__(self, view):
        self.view = view

    def _init_debug_resources(self):
        try:
            vs_src = """
            #version 330 core
            layout (location = 0) in vec3 aPos;
            uniform mat4 view;
            uniform mat4 projection;
            void main() { gl_Position = projection * view * vec4(aPos, 1.0); }
            """
            fs_src = """
            #version 330 core
            out vec4 FragColor;
            uniform vec3 color;
            void main() { FragColor = vec4(color, 1.0); }
            """
            self.view.debug_shader = compileProgram(
                compileShader(vs_src, gl.GL_VERTEX_SHADER),
                compileShader(fs_src, gl.GL_FRAGMENT_SHADER),
                validate=False
            )
            # PERF: cache uniform locations once instead of querying them
            # via glGetUniformLocation every frame in the debug draw paths.
            self.view._debug_proj_loc = gl.glGetUniformLocation(self.view.debug_shader, 'projection')
            self.view._debug_view_loc = gl.glGetUniformLocation(self.view.debug_shader, 'view')
            self.view._debug_color_loc = gl.glGetUniformLocation(self.view.debug_shader, 'color')
            self.view.debug_vao = gl.glGenVertexArrays(1)
            self.view.debug_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self.view.debug_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.view.debug_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 1024 * 1024, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glBindVertexArray(0)
        except Exception as e:
            print(f"Debug Renderer Init Failed: {e}")

    def _gather_io_connections(self):
        COLOR_LOGIC   = (1.0, 1.0, 0.0)
        COLOR_IO      = (0.0, 1.0, 1.0)
        COLOR_PATROL  = (0.15, 0.65, 0.60)
        COLOR_PATHNODE = (128.0 / 255.0, 128.0 / 255.0, 0.0)
        try:
            from editor.io_system import get_connections
            io_available = True
        except ImportError:
            io_available = False
        try:
            from editor.things import PathNode, Monster
        except ImportError:
            PathNode = None
            Monster = None
        # A link is drawn only between objects the view filters show.
        filters = self.view.editor.state.view_filters
        brushes = [b for b in self.view.editor.state.brushes if not filters.hides_brush(b)]
        things = [t for t in self.view.editor.state.things if not filters.hides_thing(t)]

        def find_pos_by_name(name):
            for b in brushes:
                if b.get('name') == name:
                    return b['pos']
            for t in things:
                t_name = getattr(t, 'name', t.properties.get('name', ''))
                if t_name == name:
                    return t.pos
            return None
        lines = []
        if io_available:
            for brush in brushes:
                for conn in get_connections(brush):
                    dst = find_pos_by_name(conn.target_name)
                    if dst:
                        is_logic = brush.get('is_trigger') or brush.get('is_mover') or brush.get('is_door')
                        color = COLOR_LOGIC if is_logic else COLOR_IO
                        lines.append({'src': brush['pos'], 'dst': dst, 'color': color})
            for thing in things:
                for conn in get_connections(thing):
                    dst = find_pos_by_name(conn.target_name)
                    if dst:
                        is_logic = thing.properties.get('type') == 'logic_gate'
                        color = COLOR_LOGIC if is_logic else COLOR_IO
                        lines.append({'src': thing.pos, 'dst': dst, 'color': color})
        node_lookup = {}
        if PathNode is not None:
            for t in things:
                if isinstance(t, PathNode):
                    n = t.properties.get('name', '') or ''
                    if n:
                        node_lookup[n] = t
            for name, node in node_lookup.items():
                next_name = node.get_next_node_name()
                if not next_name:
                    continue
                next_node = node_lookup.get(next_name)
                if next_node is None:
                    continue
                lines.append({'src': node.pos, 'dst': next_node.pos, 'color': COLOR_PATHNODE})
        if Monster is not None and PathNode is not None:
            for t in things:
                if not isinstance(t, Monster):
                    continue
                if not t.properties.get('patrol', False):
                    continue
                target_name = t.properties.get('patrol_target', '') or ''
                if not target_name:
                    continue
                dst = find_pos_by_name(target_name)
                if dst:
                    lines.append({'src': t.pos, 'dst': dst, 'color': COLOR_PATROL})
        COLOR_TELEPORT = (0.78, 0.39, 1.0)
        if PathNode is not None:
            for brush in brushes:
                if not brush.get('is_trigger', False):
                    continue
                if brush.get('trigger_action') != 'teleport':
                    continue
                target_name = brush.get('target_node', '')
                if not target_name:
                    continue
                dst_node = node_lookup.get(target_name)
                if dst_node:
                    lines.append({'src': brush['pos'], 'dst': dst_node.pos, 'color': COLOR_TELEPORT})
        return lines

    def _render_bullet_marks(self, marks, proj_matrix, view_matrix):
        self.view.renderer.draw_bullet_marks(proj_matrix, view_matrix, marks)

    def _render_player_glasses(self, positions, proj_matrix, view_matrix, *, sprites=None, force=False):
        """Draw one or more player bodies as glasses billboards."""
        if not force and not self.view.show_glasses:
            return
        if not positions or not self.view.renderer:
            return
        self.view.renderer.draw_player_glasses(
            proj_matrix,
            view_matrix,
            positions,
            width=40.0,
            height=18.0,
            sprites=self.view._render_config.get("player_glasses_sprites", ()) if sprites is None else sprites,
        )
        # render_scene leaves depth testing disabled; restore that state after
        # this explicit post-scene billboard pass.
        gl.glDisable(gl.GL_DEPTH_TEST)

    def _render_projectiles(self, projectiles, proj_matrix, view_matrix):
        if not len(projectiles):
            return
        tex_id = (self.view.sprite_textures.get('projectile') or
                  self.view.sprite_textures.get('Monster'))
        if not tex_id:
            return
        from engine.monster_constants import MONSTER_PROJECTILE_SPRITE_SIZE
        # Every projectile shares one texture and size, so the lot is one
        # instanced draw of the published position array.
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        self.view.renderer.draw_billboards(
            proj_matrix, view_matrix, projectiles, MONSTER_PROJECTILE_SPRITE_SIZE,
            tex_id)
        gl.glDisable(gl.GL_BLEND)

    def _render_monster_debug_rays(self, rays, proj_matrix, view_matrix):
        if not rays or not self.view.debug_shader:
            return
        gl.glUseProgram(self.view.debug_shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(self.view._debug_proj_loc, 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(self.view._debug_view_loc, 1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.view.debug_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.view.debug_vbo)

        # PERF: group rays by color and upload+draw each group in one call
        # instead of one glBufferSubData + glDrawArrays per ray.
        green_pts = []
        red_pts = []
        for ray in rays:
            s, e = ray['start'], ray['end']
            dst = green_pts if ray.get('color') == 'green' else red_pts
            dst.extend((s[0], s[1], s[2], e[0], e[1], e[2]))

        for pts, color in ((green_pts, (0.0, 1.0, 0.0)), (red_pts, (1.0, 0.0, 0.0))):
            if not pts:
                continue
            gl.glUniform3f(self.view._debug_color_loc, *color)
            data = np.array(pts, dtype=np.float32)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
            gl.glDrawArrays(gl.GL_LINES, 0, len(pts) // 3)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    #: ``hitboxes 1``: the outline colour of a monster's hit sphere.
    HITBOX_COLOR = (1.0, 0.08, 0.72)          # bright pink
    HITBOX_SEGMENTS = 32

    @classmethod
    def hitbox_lines(cls, centres, radii):
        """Line-segment endpoints, ``(N * 3 * SEGMENTS * 2, 3)`` float32,
        outlining each sphere by its three axis-aligned great circles."""
        centres = np.asarray(centres, dtype=np.float32).reshape(-1, 3)
        radii = np.asarray(radii, dtype=np.float32).reshape(-1)
        n = cls.HITBOX_SEGMENTS
        angles = np.linspace(0.0, 2.0 * np.pi, n + 1, dtype=np.float32)
        cos, sin, zero = np.cos(angles), np.sin(angles), np.zeros(n + 1, np.float32)
        rings = np.stack([np.stack(axes, axis=1) for axes in
                          ((cos, sin, zero), (cos, zero, sin), (zero, cos, sin))])
        # Each ring's consecutive points as segment pairs: (3, n, 2, 3).
        segments = np.stack((rings[:, :-1], rings[:, 1:]), axis=2)
        unit = segments.reshape(-1, 3)
        points = (centres[:, None, :]
                  + radii[:, None, None] * unit[None, :, :])
        return np.ascontiguousarray(points.reshape(-1, 3), dtype=np.float32)

    def _render_monster_hitboxes(self, hitboxes, proj_matrix, view_matrix):
        """Outline the monsters' hit spheres, over everything (``hitboxes``)."""
        if hitboxes is None or not self.view.debug_shader:
            return
        centres, radii = hitboxes
        if not len(radii):
            return
        data = self.hitbox_lines(centres, radii)
        gl.glUseProgram(self.view.debug_shader)
        gl.glUniformMatrix4fv(self.view._debug_proj_loc, 1, gl.GL_FALSE,
                              glm.value_ptr(proj_matrix))
        gl.glUniformMatrix4fv(self.view._debug_view_loc, 1, gl.GL_FALSE,
                              glm.value_ptr(view_matrix))
        gl.glUniform3f(self.view._debug_color_loc, *self.HITBOX_COLOR)
        gl.glBindVertexArray(self.view.debug_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.view.debug_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
        depth = gl.glIsEnabled(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)        # the sprite must not hide it
        gl.glDrawArrays(gl.GL_LINES, 0, len(data))
        if depth:
            gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    def _render_spatial_grid(self, proj_matrix, view_matrix):
        if not self.view.debug_shader or not self.view.logic_thread:
            return
        grid = self.view.logic_thread.session_runtime.spatial_grid
        if grid is None:
            return
        gl.glUseProgram(self.view.debug_shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(self.view._debug_proj_loc, 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(self.view._debug_view_loc, 1, gl.GL_FALSE, view_ptr)
        gl.glUniform3f(self.view._debug_color_loc, 0.0, 0.8, 1.0)
        gl.glBindVertexArray(self.view.debug_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.view.debug_vbo)
        cs = grid.cell_size
        draw_y = 1.0

        # PERF: accumulate every cell edge into one buffer and issue a single
        # draw call instead of one glBufferSubData + glDrawArrays per edge.
        pts = []
        for (cx, cz) in grid.cells:
            x0 = cx * cs
            z0 = cz * cs
            x1 = x0 + cs
            z1 = z0 + cs
            pts.extend((
                x0, draw_y, z0, x1, draw_y, z0,
                x1, draw_y, z0, x1, draw_y, z1,
                x1, draw_y, z1, x0, draw_y, z1,
                x0, draw_y, z1, x0, draw_y, z0,
            ))

        if pts:
            data = np.array(pts, dtype=np.float32)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
            gl.glDrawArrays(gl.GL_LINES, 0, len(pts) // 3)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    def show_pos_window(self):
        """Open or raise the live camera-position floating window."""
        if self.view.pos_window is not None and self.view.pos_window.active:
            self.view.floating_windows.raise_(self.view.pos_window)
            self.view.update()
            return

        self.view.pos_window = CallbackWindow(
            key="pos",
            title="Pos",
            draw_fn=self._draw_pos_window,
            width=230,
            body_height=92,
            x=max(20, self.view.width() - 250),
            y=20,
            on_close_cb=self._close_pos_window,
        )
        self.view.floating_windows.add(self.view.pos_window)
        self.view.update()

    def _close_pos_window(self):
        self.view.pos_window = None
        self.view.update()

    def _draw_pos_window(self, painter, x, y, w, h):
        """Draw only the current camera coordinates inside the Pos window."""
        p = self.view.camera.pos
        values = (
            ("X", float(p.x)),
            ("Y", float(p.y)),
            ("Z", float(p.z)),
        )
        font = QFont("Fio UI", 11)
        font.setStyleHint(QFont.Monospace)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        axis_color = QColor(240, 128, 0)
        value_color = QColor(255, 255, 255)
        baseline = y + 21
        axis_x = x + 12
        value_x = axis_x + metrics.horizontalAdvance("X  ")
        for axis, value in values:
            painter.setPen(axis_color)
            painter.drawText(axis_x, baseline, axis)
            painter.setPen(value_color)
            painter.drawText(value_x, baseline, f"{value:.3f}")
            baseline += 26
