"""On-screen messages of the 3D view, delegated from QtGameView.

The three queued message lines a map or the console can show over the game
(show_view_message and its numbered siblings): their timers, fades and
drawing.
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


class ViewMessages:
    def __init__(self, view):
        self.view = view

    def _start_view_message(self, text: str):
        """Start displaying one transient message-1 immediately."""
        self.view._view_message_text = text
        self.view._view_message_started_at = time.perf_counter()
        self.view._view_message_width = QFontMetrics(
            self.view._hud_msg_font
        ).horizontalAdvance(text)

    def show_view_message(self, text: str):
        """Show a message-1, queueing it behind the current message-1."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self.view._view_message_text:
            elapsed = time.perf_counter() - self.view._view_message_started_at
            if elapsed < 7.0 or self.view._view_message_queue:
                self.view._view_message_queue.append(text)
                self.view.update()
                return

        self._start_view_message(text)
        self.view.update()

    def _start_view_message2(self, text: str):
        """Start displaying one transient message-2 immediately."""
        self.view._view_message2_text = text
        self.view._view_message2_started_at = time.perf_counter()
        self.view._view_message2_width = QFontMetrics(
            self.view._hud_msg_font
        ).horizontalAdvance(text)

    def _start_view_message3(self, text: str):
        """Start displaying one transient Rushford-font message-3 immediately."""
        self.view._view_message3_text = text
        self.view._view_message3_started_at = time.perf_counter()
        self.view._view_message3_width = QFontMetrics(
            self.view._hud_health_font
        ).horizontalAdvance(text)

    def show_view_message2(self, text: str):
        """Show a message-2, queueing it behind the current message-2."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self.view._view_message2_text:
            elapsed = time.perf_counter() - self.view._view_message2_started_at
            if elapsed < 7.0 or self.view._view_message2_queue:
                self.view._view_message2_queue.append(text)
                self.view.update()
                return

        self._start_view_message2(text)
        self.view.update()

    def show_view_message3(self, text):
        """Show a Rushford-font message-3, queueing it behind the current message-3."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self.view._view_message3_text:
            elapsed = time.perf_counter() - self.view._view_message3_started_at
            if elapsed < 7.0 or self.view._view_message3_queue:
                self.view._view_message3_queue.append(text)
                self.view.update()
                return

        self._start_view_message3(text)
        self.view.update()

    def _draw_queued_view_message(
        self, painter, viewport_width, viewport_height,
        text, started_at, width, queue, start_message,
        stack_slot=0, message_font=None, fade_duration=1.0,
    ):
        """Draw one queued transient message in the shared near-bottom stack."""
        if not text:
            return text, started_at, width

        elapsed = time.perf_counter() - started_at
        font = message_font or self.view._hud_msg_font
        if elapsed >= 7.0:
            if queue:
                text = queue.popleft()
                start_message(text)
                started_at = time.perf_counter()
                elapsed = 0.0
                width = QFontMetrics(font).horizontalAdvance(text)
            else:
                return "", 0.0, 0

        fade_duration = max(0.0, min(float(fade_duration), 3.5))
        fade_out_start = 7.0 - fade_duration
        if elapsed < fade_duration:
            opacity = elapsed / fade_duration if fade_duration else 1.0
        elif elapsed < fade_out_start:
            opacity = 1.0
        else:
            opacity = (7.0 - elapsed) / fade_duration if fade_duration else 0.0

        # All three transient messages share one horizontal anchor slightly
        # right of centre.  Use the largest message font height for row spacing
        # so the three lines cannot overlap even when message-3 uses Rushford.
        metrics = QFontMetrics(font)
        max_stack_height = max(
            QFontMetrics(self.view._hud_msg_font).height(),
            QFontMetrics(self.view._hud_health_font).height(),
        )
        row_height = max_stack_height + 6
        baseline = viewport_height - 20 - metrics.descent() - (row_height * stack_slot)
        cx = viewport_width // 2 + int(viewport_width * 0.10)
        text_x = cx - width // 2

        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.setFont(font)
        painter.setPen(self.view._hud_shadow_pen)
        painter.drawText(text_x + 2, baseline + 2, text)
        painter.setPen(self.view._hud_grey_pen)
        painter.drawText(text_x, baseline, text)
        painter.restore()
        return text, started_at, width

    def _draw_view_message(self, painter, viewport_width, viewport_height):
        """Draw message-1 at the top of the shared near-bottom message stack."""
        self.view._view_message_text, self.view._view_message_started_at, self.view._view_message_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self.view._view_message_text,
                self.view._view_message_started_at,
                self.view._view_message_width,
                self.view._view_message_queue,
                self._start_view_message,
                stack_slot=2,
            )
        )

    def _draw_view_message2(self, painter, viewport_width, viewport_height):
        """Draw message-2 in the middle of the shared near-bottom stack."""
        self.view._view_message2_text, self.view._view_message2_started_at, self.view._view_message2_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self.view._view_message2_text,
                self.view._view_message2_started_at,
                self.view._view_message2_width,
                self.view._view_message2_queue,
                self._start_view_message2,
                stack_slot=1,
            )
        )

    def _draw_view_message3(self, painter, viewport_width, viewport_height):
        """Draw message-3 in Rushford at the bottom of the shared message stack."""
        self.view._view_message3_text, self.view._view_message3_started_at, self.view._view_message3_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self.view._view_message3_text,
                self.view._view_message3_started_at,
                self.view._view_message3_width,
                self.view._view_message3_queue,
                self._start_view_message3,
                message_font=self.view._hud_health_font,
                stack_slot=0,
                fade_duration=1.25,
            )
        )
