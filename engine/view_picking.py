"""Picking in the 3D view, delegated from QtGameView.

Rays from the mouse, the object, brush face or axis under it in the editor,
and the play-mode actor pick (arming it, the hover, the click, its hint).
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


class ViewPicking:
    #: The world-pause owner key an armed pick holds.
    ACTOR_PICK_PAUSE = "actor_pick"

    def __init__(self, view):
        self.view = view

    def get_ray_from_mouse(self, mx, my):
        w, h = self.view.width(), self.view.height()
        if w == 0 or h == 0:
            return glm.vec3(0), glm.vec3(0, 0, 1)
        ndc_x = (2.0 * mx / w) - 1.0
        ndc_y = 1.0 - (2.0 * my / h)
        clip = glm.vec4(ndc_x, ndc_y, -1.0, 1.0)
        inv_proj = glm.inverse(self.view.projection_matrix)
        eye = inv_proj * clip
        eye = glm.vec4(eye.x, eye.y, -1.0, 0.0)
        inv_view = glm.inverse(self.view.view_matrix)
        world = inv_view * eye
        ray_dir = glm.normalize(glm.vec3(world))
        ec = self.view.logic_thread.camera.get_editor_camera()
        ray_origin = ec.pos
        return ray_origin, ray_dir

    def get_object_at_3d(self, mx, my, cycle=False):
        """Object under the cursor.

        ``cycle`` walks through everything the ray passes through, nearest
        first, so a brush hidden behind another can be reached by clicking
        again — Radiant's drill-select.  Hidden and locked objects are skipped
        either way, so they never appear as a dead stop in the cycle.
        """
        ray_o, ray_d = self.get_ray_from_mouse(mx, my)
        best_obj, best_t = None, float('inf')
        hits = []
        filters = self.view.editor.state.view_filters
        for brush in self.view.editor.state.brushes:
            if (brush.get('hidden', False) or brush.get('lock', False)
                    or filters.hides_brush(brush)):
                continue
            # The brush's own box (brush_placement): an angled brush's
            # planes' bounds, tested in the frame its shape is authored in,
            # so a moved or spinning brush is picked where it is drawn.
            o, d = brush_placement.ray_to_shape(brush, ray_o, ray_d)
            tmin = brush_placement.ray_box(
                brush_placement.authored_box(brush), o, d)
            if tmin is not None:
                hits.append((tmin, brush))
                if tmin < best_t:
                    best_t = tmin
                    best_obj = brush
        for thing in self.view.editor.state.things:
            if not component_edit.is_selectable(thing) or filters.hides_thing(thing):
                continue
            if getattr(thing, 'editor_model_bounds', None):
                from engine.entity_models import editor_model_ray_distance
                t = editor_model_ray_distance(thing, ray_o, ray_d)
                if t is not None:
                    hits.append((t, thing))
                    if t < best_t:
                        best_t, best_obj = t, thing
                continue
            tp = glm.vec3(thing.pos)
            radius = 32.0
            oc = ray_o - tp
            a = glm.dot(ray_d, ray_d)
            b = 2.0 * glm.dot(oc, ray_d)
            c = glm.dot(oc, oc) - radius * radius
            disc = b * b - 4 * a * c
            if disc >= 0:
                t = (-b - disc**0.5) / (2.0 * a)
                if t > 0:
                    hits.append((t, thing))
                    if t < best_t:
                        best_t = t
                        best_obj = thing
        if cycle and hits:
            # Sorted by hit distance so the walk order is the same every time
            # the cursor is in the same place.
            hits.sort(key=lambda h: h[0])
            candidates = [obj for _, obj in hits]
            return component_edit.cycle_pick(candidates,
                                             self.view.editor.primary_selection())
        return best_obj

    def intersect_ray_with_axis(self, ray_o, ray_d, obj_pos, axis_vec):
        perp = glm.cross(ray_d, axis_vec)
        denom = glm.dot(perp, perp)
        if denom < 1e-6:
            return None, float('inf')
        diff = obj_pos - ray_o
        t = glm.dot(glm.cross(diff, axis_vec), perp) / denom
        closest = ray_o + ray_d * t
        dist = glm.distance(closest, obj_pos + axis_vec * glm.dot(closest - obj_pos, axis_vec))
        return closest, dist

    def get_brush_face_at_coords(self, mx, my):
        ray_o, ray_d = self.get_ray_from_mouse(mx, my)
        best_t = float('inf')
        best_hit = None
        ray_o_t = (float(ray_o.x), float(ray_o.y), float(ray_o.z))
        ray_d_t = (float(ray_d.x), float(ray_d.y), float(ray_d.z))
        filters = self.view.editor.state.view_filters
        for brush in self.view.editor.state.brushes:
            if brush.get('hidden', False) or filters.hides_brush(brush):
                continue
            # Angled (clipped) brushes: pick against their real convex faces so
            # the sloped cut face is selectable, not just the six sides of the
            # bounding box.  Plain box brushes stay on the fast AABB path below.
            # Tested in the frame the brush's shape is authored in
            # (brush_placement), so a moved or spinning brush is picked where
            # it is drawn.
            o, d = brush_placement.ray_to_shape(brush, ray_o_t, ray_d_t)
            convex = brush_placement.shape_convex(brush)
            if convex is not None:
                hit = brush_geometry.ray_convex_face(convex, o, d)
                if hit is not None and hit[0] < best_t:
                    best_t = hit[0]
                    best_hit = (brush, brush_geometry.face_key(hit[1]))
                continue
            box = brush_placement.authored_box(brush)
            t_box = brush_placement.ray_box(box, o, d)
            if t_box is None or t_box >= best_t:
                continue
            local = [o[i] + d[i] * t_box - box[i] for i in range(3)]
            rel = [abs(local[i]) / (2.0 * box[i + 3]) if box[i + 3] else 0.0
                   for i in range(3)]
            face = 'north'
            if rel[0] > rel[1] and rel[0] > rel[2]:
                face = 'east' if local[0] > 0 else 'west'
            elif rel[1] > rel[0] and rel[1] > rel[2]:
                face = 'top' if local[1] > 0 else 'down'
            else:
                face = 'north' if local[2] > 0 else 'south'
            best_t = t_box
            best_hit = (brush, face)
        return best_hit

    def get_face_at(self, mouse_pos):
        if not isinstance(self.view.editor.primary_selection(), dict):
            return None
        brush = self.view.editor.primary_selection()
        ray_o, ray_d = self.get_ray_from_mouse(mouse_pos.x(), mouse_pos.y())
        pos = glm.vec3(brush.get('pos', [0, 0, 0]))
        size = glm.vec3(brush.get('size', [64, 64, 64]))
        bmin, bmax = pos - size/2, pos + size/2
        tmin, tmax = 0.0, float('inf')
        for i in range(3):
            if abs(ray_d[i]) < 1e-6:
                if ray_o[i] < bmin[i] or ray_o[i] > bmax[i]:
                    return None
            else:
                t1 = (bmin[i] - ray_o[i]) / ray_d[i]
                t2 = (bmax[i] - ray_o[i]) / ray_d[i]
                if t1 > t2:
                    t1, t2 = t2, t1
                tmin = max(tmin, t1)
                tmax = min(tmax, t2)
        if tmin > tmax:
            return None
        hit = ray_o + ray_d * tmin
        local = hit - pos
        rel = glm.abs(local) / size
        if rel.x > rel.y and rel.x > rel.z:
            return 'east' if local.x > 0 else 'west'
        if rel.y > rel.x and rel.y > rel.z:
            return 'top' if local.y > 0 else 'bottom'
        return 'north' if local.z > 0 else 'south'

    def _world_to_screen(self, point):
        """Project a world-space point into this viewport's pixel coordinates."""
        clip = self.view.projection_matrix * self.view.view_matrix * glm.vec4(
            float(point[0]), float(point[1]), float(point[2]), 1.0
        )
        if abs(float(clip.w)) < 1e-6:
            return None
        ndc_x = float(clip.x) / float(clip.w)
        ndc_y = float(clip.y) / float(clip.w)
        return QPoint(
            int((ndc_x + 1.0) * 0.5 * self.view.width()),
            int((1.0 - ndc_y) * 0.5 * self.view.height()),
        )

    def _show_pick_cursor(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        QApplication.setOverrideCursor(Qt.CrossCursor)

    @property
    def actor_pick_active(self) -> bool:
        return self.view._actor_pick is not None

    def begin_actor_pick(self, on_pick=None) -> bool:
        """Arm a one-shot click-to-pick of an actor in Play Mode.

        The world pauses (``LogicThread.session_runtime.set_world_paused``) so the actor holds
        still, and the cursor is freed. The next left-click on an actor ends
        the pick and calls ``on_pick(entity)``; by default that opens the
        Entity Inspector on it. A click on nothing keeps the pick armed; Esc
        or a right-click cancels it. Returns False outside Play Mode.
        """
        if not self.view.play_mode:
            return False
        self.view._actor_pick = {'on_pick': on_pick}
        self.view.actor_pick_hover = None
        logic = self.view.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(self.ACTOR_PICK_PAUSE, True)
        if not self.view.console_overlay_active:
            self._show_pick_cursor()
        self.view.update()
        return True

    def cancel_actor_pick(self):
        """Disarm a pick without choosing anything."""
        self._end_actor_pick()

    def _end_actor_pick(self):
        if self.view._actor_pick is None:
            return
        self.view._actor_pick = None
        self.view.actor_pick_hover = None
        logic = self.view.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(self.ACTOR_PICK_PAUSE, False)
        if self.view.play_mode and not self.view.console_overlay_active:
            self.view._capture_play_cursor()
        self.view.update()

    def _pick_ray(self, mx, my):
        """World ray through pixel (mx, my) of the frame last drawn.

        Origin and direction both come from the matrices that frame was
        drawn with, so the ray leaves the camera the player is looking
        through -- first person or overhead -- not the editor camera.
        """
        w, h = self.view.width(), self.view.height()
        if w <= 0 or h <= 0:
            return None
        ndc_x = (2.0 * mx / w) - 1.0
        ndc_y = 1.0 - (2.0 * my / h)
        inv_view = glm.inverse(self.view.view_matrix)
        eye = glm.inverse(self.view.projection_matrix) * glm.vec4(ndc_x, ndc_y, -1.0, 1.0)
        direction = glm.vec3(inv_view * glm.vec4(eye.x, eye.y, -1.0, 0.0))
        if glm.length(direction) == 0.0:
            return None
        origin = glm.vec3(inv_view[3])
        return origin, glm.normalize(direction)

    def actor_at(self, mx, my):
        """The actor under pixel (mx, my) in the frame last drawn, or None.

        Reads the published entity and render tables (borrowed only for the
        test) through :func:`engine.actor_pick.pick_actor`: brushes only
        occlude, so the floor under an actor never wins the click.
        """
        ray = self._pick_ray(mx, my)
        if ray is None:
            return None
        from engine.actor_pick import NO_SLOT, pick_actor
        render_state = self.view.game_state.get_render_state() if self.view.logic_thread else None
        try:
            table = render_state.entity_table
            if table is None:
                return None
            slot = pick_actor(ray[0], ray[1], table,
                              render_state.render_table)
            if slot == NO_SLOT or slot >= len(table.things):
                return None
            return table.things[slot]
        finally:
            if render_state is not None:
                self.view.game_state.release_render_state(render_state)

    def _update_actor_pick_hover(self, mx, my):
        hovered = self.actor_at(mx, my)
        if hovered is not self.view.actor_pick_hover:
            self.view.actor_pick_hover = hovered
            self.view.update()

    def _click_actor_pick(self, mx, my) -> bool:
        """Resolve an armed pick at the clicked pixel. True if it ended."""
        actor = self.actor_at(mx, my)
        if actor is None:
            return False
        on_pick = self.view._actor_pick.get('on_pick') or self._open_inspector_for
        self._end_actor_pick()
        try:
            on_pick(actor)
        except Exception as exc:
            debug_log("Error", f"actor pick handler failed: {exc}")
        return True

    def _open_inspector_for(self, actor):
        self.view.editor.show_entity_inspector(actor)

    def _draw_actor_pick_hint(self, painter, viewport_width):
        hovered = self.view.actor_pick_hover
        name = ""
        if hovered is not None:
            props = getattr(hovered, 'properties', {}) or {}
            name = str(props.get('name') or props.get('type') or "")
        text = (f"Inspect: {name} - click to open (Esc to cancel)" if name
                else "Inspect (paused): click an actor (Esc to cancel)")
        metrics = QFontMetrics(self.view._hud_msg_font)
        tw = metrics.horizontalAdvance(text)
        x, y = viewport_width // 2 - tw // 2, 40
        painter.setFont(self.view._hud_msg_font)
        painter.setPen(self.view._hud_shadow_pen)
        painter.drawText(x + 2, y + 2, text)
        painter.setPen(self.view._hud_grey_pen)
        painter.drawText(x, y, text)
