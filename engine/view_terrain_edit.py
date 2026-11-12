"""Terrain editing in the 3D view, delegated from QtGameView.

Sculpting under the mouse, the brush cursor, picking the terrain with a ray,
and selecting and dragging paint stamps.
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


class ViewTerrainEdit:
    def __init__(self, view):
        self.view = view

    def set_terrain_sculpt_active(self, active: bool):
        active = bool(active)
        self.view.terrain_sculpt_active = active
        self.view.terrain_sculpt_painting = False
        if not active:
            self._end_stamp_drag()
            self.clear_stamp_selection()
        if active:
            tool = getattr(self.view, 'decal_tool', None)
            if tool is not None:
                tool.set_active(False)              # one placing tool at a time
            self.view.setCursor(Qt.CrossCursor)
        else:
            self.view._terrain_brush_mouse_pos = QPoint(-1, -1)
            self.view._terrain_brush_hit = None
            self.view.setCursor(Qt.ArrowCursor)
        self.view.update()

    def raycast_terrain(self, mx: int, my: int):
        terrain = self.view.editor.terrain
        if terrain is None or not terrain.enabled:
            return None
        ray_o, ray_d = self.view.picking.get_ray_from_mouse(mx, my)
        step = 4.0
        max_dist = 5000.0
        t = 1.0
        prev_above = True
        while t < max_dist:
            px = ray_o.x + ray_d.x * t
            py = ray_o.y + ray_d.y * t
            pz = ray_o.z + ray_d.z * t
            h = terrain.get_height_at_safe(px, pz)
            if h is not None:
                above = py >= h
                if not above and prev_above:
                    lo, hi = t - step, t
                    for _ in range(12):
                        mid = (lo + hi) * 0.5
                        mpx = ray_o.x + ray_d.x * mid
                        mpy = ray_o.y + ray_d.y * mid
                        mpz = ray_o.z + ray_d.z * mid
                        mh = terrain.get_height_at_safe(mpx, mpz)
                        if mh is not None and mpy < mh:
                            hi = mid
                        else:
                            lo = mid
                    mid = (lo + hi) * 0.5
                    fx = ray_o.x + ray_d.x * mid
                    fy = ray_o.y + ray_d.y * mid
                    fz = ray_o.z + ray_d.z * mid
                    return (fx, fy, fz)
                prev_above = above
            t += step
            if t > 500:
                step = 16.0
            elif t > 200:
                step = 8.0
        return None

    def _draw_terrain_brush_cursor(self, painter):
        """Draw a screen-space circle representing the world-space brush."""
        if not self.view.terrain_sculpt_active or self.view.play_mode or self.view.mouselook_active:
            return
        pos = self.view._terrain_brush_mouse_pos
        if pos.x() < 0 or pos.y() < 0:
            return

        hit = self.view._terrain_brush_hit
        if hit is None:
            return

        wx, wy, wz = hit
        radius = max(1.0, float(self.view.terrain_sculpt_radius))
        center = self.view.picking._world_to_screen((wx, wy + 0.02, wz))
        px = self.view.picking._world_to_screen((wx + radius, wy + 0.02, wz))
        pz = self.view.picking._world_to_screen((wx, wy + 0.02, wz + radius))
        if center is None or px is None or pz is None:
            return

        dx = math.hypot(px.x() - center.x(), px.y() - center.y())
        dz = math.hypot(pz.x() - center.x(), pz.y() - center.y())
        screen_radius = max(3.0, (dx + dz) * 0.5)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 210), 4))
        painter.drawEllipse(
            int(round(pos.x() - screen_radius)),
            int(round(pos.y() - screen_radius)),
            int(round(screen_radius * 2.0)),
            int(round(screen_radius * 2.0)),
        )
        painter.setPen(QPen(QColor(240, 128, 0, 235), 2))
        painter.drawEllipse(
            int(round(pos.x() - screen_radius)),
            int(round(pos.y() - screen_radius)),
            int(round(screen_radius * 2.0)),
            int(round(screen_radius * 2.0)),
        )
        painter.restore()

    def clear_stamp_selection(self):
        terrain = getattr(self.view.editor, 'terrain', None)
        if terrain is not None and terrain.selected_stamp_ids:
            terrain.set_selected_stamps(())
            self.view.update()
            return True
        return False

    def _press_terrain_stamp(self, event):
        """Shift-click selects a stamp; pressing a selected one starts a drag.

        Returns True when the press was a stamp edit, False when it should
        place a new stamp.
        """
        terrain = self.view.editor.terrain
        hit = self.view._terrain_brush_hit
        if terrain is None or hit is None:
            return False
        stamp_id = terrain.paint_stamp_at(hit[0], hit[2])
        selected = set(terrain.selected_stamp_ids)
        if event.modifiers() & Qt.ShiftModifier:
            if stamp_id is None:
                self.clear_stamp_selection()
                return True
            toggle_off = stamp_id if stamp_id in selected else None
            if toggle_off is None:
                terrain.set_selected_stamps(selected | {stamp_id})
        elif stamp_id is not None and stamp_id in selected:
            toggle_off = None
        else:
            return False
        self.view._stamp_drag = {'last': (hit[0], hit[2]), 'moved': False,
                            'toggle_off': toggle_off}
        self.view.update()
        return True

    def _drag_terrain_stamps(self, mx, my):
        drag = self.view._stamp_drag
        terrain = self.view.editor.terrain
        hit = self.raycast_terrain(mx, my)
        if drag is None or terrain is None or hit is None:
            return
        dx, dz = hit[0] - drag['last'][0], hit[2] - drag['last'][1]
        if not drag['moved']:
            if math.hypot(dx, dz) < 0.5:
                return
            # The whole drag is one undo step.
            self.view.editor.state.terrain_data = terrain.to_dict()
            self.view.editor.save_state()
            drag['moved'] = True
        terrain.move_paint_stamps(terrain.selected_stamp_ids, dx, dz, defer=True)
        drag['last'] = (hit[0], hit[2])
        self.view.update()

    def _end_stamp_drag(self):
        drag, self.view._stamp_drag = self.view._stamp_drag, None
        terrain = getattr(self.view.editor, 'terrain', None)
        if drag is None or terrain is None:
            return
        if drag['moved']:
            terrain.finish_stamp_edit()
            self.view.editor.state.terrain_data = terrain.to_dict()
        elif drag['toggle_off'] is not None:
            terrain.set_selected_stamps(
                terrain.selected_stamp_ids - {drag['toggle_off']})
        self.view.update()

    def _apply_sculpt_at_mouse(self, mx: int, my: int):
        hit = self.raycast_terrain(mx, my)
        if hit is None:
            return
        wx, wy, wz = hit
        terrain = self.view.editor.terrain
        mode = self.view.terrain_sculpt_mode
        radius = self.view.terrain_sculpt_radius
        strength = self.view.terrain_sculpt_strength

        if mode == 'stamp':
            browser = self.view.editor.asset_browser
            texture_path = browser.get_selected_filepath() if browser is not None else None
            if not texture_path:
                self.view.editor.show_toast("Select a texture in the Asset Browser first", is_error=True)
                return

            abs_texture = os.path.abspath(texture_path)
            rel_texture = os.path.relpath(
                abs_texture, self.view.editor.root_dir).replace(os.sep, '/')
            texture_name = (
                rel_texture
                if rel_texture != '..' and not rel_texture.startswith('../')
                else abs_texture.replace(os.sep, '/')
            )

            # One paint click is one undo step. The compact terrain snapshot
            # already carries the baked terrain material data.
            self.view.editor.state.terrain_data = terrain.to_dict()
            self.view.editor.save_state()

            terrain.set_selected_stamps(())
            added = terrain.add_paint_stamp(
                wx, wz, radius, texture_name,
                feather=self.view.terrain_sculpt_feather,
            )
            if added is None:
                self.view.editor.state.discard_last_checkpoint()
                self.view.editor.show_toast("Terrain texture paint was not applied", is_error=True)
                return

            self.view.editor.state.terrain_data = terrain.to_dict()
            self.view.editor.show_toast(f"Painted {os.path.basename(texture_path)}")
            # The viewport already renders on its regular 60 Hz update loop.
            # Do not force a second repaint for every stamp click; the extra
            # update request makes the terrain change appear as a visual flash.
            return

        if mode == 'raise':
            terrain.apply_sculpt_at(wx, wz, radius, strength)
        elif mode == 'lower':
            terrain.apply_sculpt_at(wx, wz, radius, -strength)
        elif mode == 'smooth':
            terrain.smooth_sculpt_at(wx, wz, radius, min(strength / 20.0, 1.0))
        elif mode == 'flatten':
            terrain.flatten_sculpt_at(wx, wz, radius, min(strength / 20.0, 1.0))
        self.view.editor.state.terrain_data = terrain.to_dict()
        self.view.update()
