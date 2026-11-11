"""Vertex, edge and face editing in the 3D view, delegated from QtGameView.

Picking a component handle under the mouse and dragging it (the editor's
component controller holds the selection and applies the edit), and the
face-mode drag.
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


class ViewComponents:
    def __init__(self, view):
        self.view = view

    def _components(self):
        return self.view.editor.components

    def _component_mode_active(self):
        controller = self._components()
        return controller is not None and controller.is_component_mode()

    def _component_targets(self):
        """Brushes a component pick may test — the selection, never the scene."""
        return self.view.editor.component_drag_targets()

    def _world_per_pixel(self, distance):
        """World units one screen pixel covers ``distance`` from the eye."""
        height = max(self.view.height(), 1)
        fov = float(self.view.camera.fov)
        return 2.0 * max(distance, 1.0) * math.tan(math.radians(fov) * 0.5) / height

    def _pick_component_3d(self, mx, my):
        """Component under the cursor in this viewport, or ``None``."""
        controller = self._components()
        targets = self._component_targets()
        if controller is None or not targets:
            return None
        ray_o, ray_d = self.view.picking.get_ray_from_mouse(mx, my)
        origin = (float(ray_o.x), float(ray_o.y), float(ray_o.z))
        direction = (float(ray_d.x), float(ray_d.y), float(ray_d.z))
        # Pick radius of roughly COMPONENT_GRAB_PIXELS at any depth: the
        # tolerance is expressed per world unit of distance and scaled by the
        # hit distance inside the picker.
        tolerance = self._world_per_pixel(1.0) * 9.0
        return component_edit.pick_component_3d(
            targets, controller.mode, origin, direction, tolerance)

    @staticmethod
    def _axialize(vec):
        """Snap a direction to the world axis it points most nearly along.

        Radiant maps a camera-window drag onto whole world axes so a drag in a
        perspective view still moves geometry along the grid instead of along
        some diagonal nobody asked for.
        """
        values = (abs(float(vec.x)), abs(float(vec.y)), abs(float(vec.z)))
        axis = values.index(max(values))
        out = np.zeros(3)
        out[axis] = 1.0 if float(vec[axis]) >= 0 else -1.0
        return out

    def _begin_component_drag(self, ref, press_pos, shear=False, additive=False):
        """Start a component drag from this viewport.  One undo step per drag.

        The selection policy and the drag itself come from the shared
        controller (:meth:`ComponentController.press`), so a press means exactly
        what it means in a 2D view.  This viewport contributes only what it
        alone knows: the screen-to-world mapping for the drag.
        """
        controller = self._components()
        if controller is None:
            return False
        drag = controller.press(ref, shear=shear, additive=additive)
        if drag is None:
            return False
        self.view.editor.save_state()      # checkpoint at mouse-down, like 2D
        self.view.component_drag_origin = QPoint(press_pos)
        self.view.component_drag_anchor = np.array(ref.position, dtype=np.float64)
        self.view.component_drag_kind = ref.kind

        # Screen-to-world mapping, fixed for the whole drag so the geometry
        # cannot drift under a moving camera.
        right = glm.vec3(self.view.view_matrix[0][0], self.view.view_matrix[1][0],
                         self.view.view_matrix[2][0])
        up = glm.vec3(self.view.view_matrix[0][1], self.view.view_matrix[1][1],
                      self.view.view_matrix[2][1])
        self.view.component_drag_axes = (self._axialize(right), self._axialize(up))
        camera_pos = self._camera_position()
        distance = float(np.linalg.norm(self.view.component_drag_anchor - camera_pos))
        self.view.component_drag_scale = self._world_per_pixel(distance)
        self.view.setCursor(Qt.SizeAllCursor)
        self.view.update()
        return True

    def _component_snap_grid(self):
        """Grid step a component drag in this viewport snaps to, or 0.

        Deliberately the editor's *snap* setting, not this viewport's grid
        *visibility*: a drag used to snap here whenever the 3D grid happened to
        be drawn, so turning "snap to grid" off left the 3D viewport still
        snapping and hiding the grid silently stopped it — the same gesture
        behaving differently depending on which view it started in.
        """
        return self.view.editor.component_grid_step(self.view.grid_size)

    def _camera_position(self):
        pos = self.view.logic_thread.camera.get_editor_camera().pos
        return np.array([float(pos.x), float(pos.y), float(pos.z)])

    def _update_component_drag(self, pos):
        """Apply the drag for the cursor's current screen position."""
        controller = self._components()
        if controller is None or controller.drag is None:
            return
        if self.view.component_drag_origin is None or self.view.component_drag_axes is None:
            return
        dx = pos.x() - self.view.component_drag_origin.x()
        dy = pos.y() - self.view.component_drag_origin.y()
        axis_x, axis_y = self.view.component_drag_axes
        delta = (axis_x * (dx * self.view.component_drag_scale) +
                 axis_y * (-dy * self.view.component_drag_scale))
        grid = self._component_snap_grid()
        if self.view.component_drag_kind in (component_edit.MODE_VERTEX,
                                        component_edit.MODE_EDGE) and \
                self.view.component_drag_anchor is not None:
            delta = component_edit.snap_component_delta(
                self.view.component_drag_anchor, delta, grid)
        else:
            delta = component_edit.snap_delta(delta, grid)
        if controller.update_drag(delta):
            self.view.editor.refresh_views()
        else:
            self.view.update()

    def _end_component_drag(self):
        """Commit the drag, dropping the checkpoint when nothing moved."""
        controller = self._components()
        if controller is None or controller.drag is None:
            return False
        changed = controller.commit_drag()
        self.view.component_drag_origin = None
        self.view.component_drag_axes = None
        self.view.component_drag_anchor = None
        self.view.component_drag_kind = None
        self.view.setCursor(Qt.ArrowCursor)
        if changed:
            self.view.editor.unsaved_changes = True
            self.view.editor.state.mark_lighting_dirty()
        else:
            self.view.editor.state.discard_last_checkpoint()
        self.view.editor.refresh_views()
        return changed

    def cancel_component_drag(self):
        controller = self._components()
        if controller is None or controller.drag is None:
            return False
        controller.cancel_drag()
        self.view.editor.state.discard_last_checkpoint()
        self.view.component_drag_origin = None
        self.view.component_drag_axes = None
        self.view.component_drag_anchor = None
        self.view.component_drag_kind = None
        self.view.setCursor(Qt.ArrowCursor)
        self.view.editor.refresh_views()
        return True

    def _begin_face_mode_drag(self, pos):
        """Turn an armed Face-Mode press into a face-plane drag.

        Face Mode stays the texturing tool it has always been — a click still
        applies the selected texture — but dragging from the same press moves
        the face's supporting plane, so a wall can be pushed into place without
        leaving the mode.
        """
        armed = self.view._face_mode_press
        if not armed:
            return False
        brush, face_key = armed['face']
        plane_index = brush_geometry.face_plane_index(brush, face_key)
        if plane_index is None:
            brush_geometry.box_to_geometry(brush)
            plane_index = brush_geometry.face_plane_index(brush, face_key)
        if plane_index is None:
            return False
        ring = brush_geometry.plane_face_vertex_indices(brush, plane_index)
        if len(ring) < 3:
            return False
        points = component_edit.brush_points(brush)
        ref = component_edit.ComponentRef(
            brush, component_edit.MODE_FACE, ring, plane=plane_index,
            position=points[ring].mean(axis=0))
        self.view._face_mode_press = None
        return self._begin_component_drag(ref, armed['pos'],
                                          shear=armed['shear'])
