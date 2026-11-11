"""The 3D view's HUD and screen overlays, delegated from QtGameView.

Draws the play HUD (health, armour, ammo, items, keys, the split-screen
variant), the death and level-complete screens, the underwater tint, the FPS
and sprite counters and the render menu, with the fonts and cached pixmaps
they need. QtGameView calls these from its frame, with the frame's QPainter.
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
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, QRectF, QEvent, QThread, pyqtSignal
from PyQt5.QtGui import QOpenGLContext, QPainter, QColor, QFont, QCursor, QPen, QBrush, QKeySequence, QPixmap, QSurfaceFormat, QFontMetrics, QImage, QLinearGradient, QFontDatabase, QPainterPath, QConicalGradient
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


class ViewHud:
    #: The weapon-slot flash: seconds to fade in, to hold, and to fade out.
    WEAPON_SWITCH_FADE_IN = 0.08
    WEAPON_SWITCH_HOLD = 0.35
    WEAPON_SWITCH_FADE_OUT = 0.15
    #: The bottom-right HUD icons: their size, the gap between the item icon
    #: and the keys, and how far each further key steps left (keys overlap).
    HUD_CORNER_ICON = 100
    HUD_CORNER_GAP = 15
    HUD_KEY_STEP = 40

    def __init__(self, view):
        self.view = view

    @staticmethod
    def _hud_count_baseline(metrics, text, bottom):
        """Baseline that sets the health count's digits just above *bottom*.

        The digits' own ink is measured, not the font's descent: digits end
        at the baseline, so a baseline raised by the (large) descent of the
        HUD font left a band of empty screen under the numbers. A small
        margin, in proportion to the view, keeps them off the very edge.
        """
        ink_bottom = metrics.tightBoundingRect(text).bottom()
        margin = max(4, int(bottom * 0.012))
        return bottom - margin - max(0, ink_bottom)

    def _load_hud_font_families(self):
        """Load the bundled HUD fonts once and remember their Qt families."""
        fonts_dir = os.path.join(os.getcwd(), "assets", "fonts")
        self.view._hud_font_families = {}
        self.view._hud_font_available = {}
        for style, filename in _HUD_FONT_FILES.items():
            family = _HUD_FONT_FALLBACKS[style]
            available = False
            try:
                path = os.path.join(fonts_dir, filename)
                if asset_exists(path):
                    font_id = add_application_font(path)
                    if font_id >= 0:
                        families = QFontDatabase.applicationFontFamilies(font_id)
                        if families:
                            family = families[0]
                            available = True
            except OSError:
                pass
            self.view._hud_font_families[style] = family
            self.view._hud_font_available[style] = available

    def _resolve_hud_font_family(self, selector):
        """Resolve a known font or a safe font filename under assets/fonts."""
        selector = str(selector or "").strip().strip('"').strip("'")
        if not selector:
            return None
        needle = selector.lower()
        for style, family in self.view._hud_font_families.items():
            filename = _HUD_FONT_FILES[style]
            stem = os.path.splitext(filename)[0]
            if needle in (filename.lower(), stem.lower(), family.lower()):
                return family

        basename = os.path.basename(selector)
        if basename != selector or not basename.lower().endswith((".ttf", ".otf")):
            return None
        path = os.path.join(os.getcwd(), "assets", "fonts", basename)
        try:
            if asset_exists(path):
                font_id = add_application_font(path)
                if font_id >= 0:
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        return families[0]
        except OSError:
            pass
        return None

    def _current_hud_font_family(self):
        return self.view._hud_font_override or self.view._hud_font_families.get(
            self.view._hud_style, self.view._hud_font_families.get(1, "Rushford Clean")
        )

    def _refresh_hud_status_font(self):
        self.view._hud_status_font = QFont(self._current_hud_font_family(), 56)

    def _reload_hud_settings(self):
        """Reload persistent HUD settings and reset runtime-only HUD style."""
        config = self.view.editor.config
        try:
            opacity = float(config.get("Display", "hudopacity", fallback="100"))
        except (AttributeError, TypeError, ValueError):
            opacity = 100.0
        if not math.isfinite(opacity):
            opacity = 100.0
        opacity = max(0.0, min(100.0, opacity))

        try:
            fade = config.getboolean("Display", "hudfade", fallback=True)
        except (AttributeError, TypeError, ValueError):
            fade = True

        # HUD style/font are deliberately runtime-only.  A Play start or
        # settings reload returns to the normal Rushford style rather than
        # resurrecting a previous console command from settings.ini.
        self.view._hud_style = 1
        self.view._hud_opacity = opacity
        self.view._hud_fade_enabled = fade
        self.view._hud_font_override = None
        self.view._hud_runtime_visible = None

        self._refresh_hud_status_font()
        logic_thread = self.view.logic_thread
        if logic_thread is not None:
            logic_thread.render_runtime.set_hud_fade_enabled(self.view._hud_fade_enabled)

    def set_hud_style(self, style, font_name=None):
        """Apply a HUD style immediately for the current runtime session."""
        try:
            style = int(style)
        except (TypeError, ValueError):
            return False
        if style not in (0, 1, 2, 3, 4):
            return False

        font_override = None
        if style == 4 and font_name:
            font_override = self._resolve_hud_font_family(font_name)
            if font_override is None:
                return False

        self.view._hud_style = style
        self.view._hud_font_override = font_override
        self.view._hud_runtime_visible = style != 0
        self._refresh_hud_status_font()
        self.view.update()
        return True

    def set_hud_opacity(self, opacity):
        """Apply HUD opacity immediately; valid range is 0..100 inclusive."""
        try:
            opacity = float(opacity)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(opacity) or not 0.0 <= opacity <= 100.0:
            return False
        self.view._hud_opacity = opacity
        self.view.update()
        return True

    def set_hud_fade_enabled(self, enabled):
        """Enable/disable the damage-driven health HUD fade immediately."""
        self.view._hud_fade_enabled = bool(enabled)
        logic_thread = self.view.logic_thread
        if logic_thread is not None:
            logic_thread.render_runtime.set_hud_fade_enabled(self.view._hud_fade_enabled)
        self.view.update()
        return True

    def _load_health_font(self):
        """Return the fixed Rushford font used by message-3."""
        return QFont(self.view._hud_font_families.get(1, "Rushford Clean"), 56)

    def _init_hud_caches(self):
        self.view._hud_font = QFont("Fio UI", 11)
        self.view._hud_font.setBold(True)
        self.view._hud_msg_font = QFont("Fio UI", 14)
        self.view._hud_msg_font.setBold(True)
        self.view._fps_font = QFont("Fio UI", 10)
        self.view._sprites_font = QFont("Fio UI", 10)
        self.view._sprites_font.setBold(True)
        self.view._death_title_font = QFont("Fio UI", 64, QFont.Bold)
        self.view._death_sub_font = QFont("Fio UI", 18)
        self._load_hud_font_families()
        self.view._hud_style = 1
        self.view._hud_opacity = 100.0
        self.view._hud_fade_enabled = True
        self.view._hud_font_override = None
        self.view._hud_runtime_visible = None
        self.view._hud_health_font = self._load_health_font()
        self.view._hud_status_font = QFont(
            self.view._hud_font_families.get(1, "Rushford Clean"), 56
        )
        self._reload_hud_settings()
        self.view._face_mode_font_top = QFont("Fio UI", 14, QFont.Bold)
        self.view._face_mode_font_bot = QFont("Fio UI", 10, QFont.Bold)

        self.view._hud_health_orange = QColor(179, 75, 0)
        self.view._hud_ammo_green = QColor("#0b4519")
        self.view._hud_count_shadow_pen = QPen(QColor(0, 0, 0, 85))
        self.view._hud_white_pen = QPen(QColor(255, 255, 255))
        self.view._hud_black_pen = QPen(QColor(0, 0, 0))
        self.view._hud_grey_pen = QPen(QColor(200, 200, 200))
        self.view._hud_shadow_pen = QPen(QColor(0, 0, 0))
        self.view._hud_pink_pen = QPen(QColor(255, 105, 180))
        self.view._hud_fps_bg_brush = QBrush(QColor(0, 0, 0, 128))
        self.view._hud_sprites_bg_brush = QBrush(QColor(0, 0, 0, 128))
        self.view._face_mode_box_brush = QBrush(QColor(0, 0, 0, 180))
        self.view._face_mode_pen = QPen(QColor(255, 255, 255))

        self.view._face_mode_top_width = QFontMetrics(self.view._face_mode_font_top).horizontalAdvance(
            "Select a FACE for texturing")
        self.view._face_mode_bot_width = QFontMetrics(self.view._face_mode_font_bot).horizontalAdvance(
            "Press ESC to cancel")
        self.view._cached_death_title_width = QFontMetrics(self.view._death_title_font).horizontalAdvance(
            "DIED")
        self.view._cached_death_sub_width = QFontMetrics(self.view._death_sub_font).horizontalAdvance(
            "Press Escape to return to the editor")

        self.view._cached_hud_message = None
        self.view._cached_hud_message_width = 0
        self.view._view_message_text = ""
        self.view._view_message_started_at = 0.0
        self.view._view_message_width = 0
        self.view._view_message_queue = deque()
        self.view._view_message2_text = ""
        self.view._view_message2_started_at = 0.0
        self.view._view_message2_width = 0
        self.view._view_message2_queue = deque()
        self.view._view_message3_text = ""
        self.view._view_message3_started_at = 0.0
        self.view._view_message3_width = 0
        self.view._view_message3_queue = deque()
        self.view._cached_gun_hud = {}          # (sprite path, height) -> scaled QPixmap
        self.view._cached_item_icons = {}   # (sprite path, size) -> scaled QPixmap
        self.view._cached_key_pixmaps = {}
        self.view._cached_key_size = 100
        self.view._cached_prompt_key = None
        self.view._cached_prompt_key_pixmap = None
        self.view._cached_prompt_key_loaded = False
        self.view._cached_prompt_key_size = 64

        self.view._key_fallback_cache = {
            'blue_key':   (QColor(50, 100, 200), QPen(QColor(40, 80, 160), 2), QBrush(QColor(50, 100, 200))),
            'red_key':    (QColor(200, 50, 50),   QPen(QColor(160, 40, 40), 2),   QBrush(QColor(200, 50, 50))),
            'yellow_key': (QColor(200, 200, 50),  QPen(QColor(160, 160, 40), 2),  QBrush(QColor(200, 200, 50))),
            'green_key':  (QColor(50, 200, 50),   QPen(QColor(40, 160, 40), 2),   QBrush(QColor(50, 200, 50))),
        }
        self.view._key_fallback_default = (QColor(150, 150, 150), QPen(QColor(120, 120, 120), 2), QBrush(QColor(150, 150, 150)))

    def _draw_underwater_overlay(self, painter, render_state):
        """Tint the view while the camera is below a water surface."""
        p1_under = getattr(self.view, '_cached_underwater', False)
        p2_under = getattr(self.view, '_cached_p2_underwater', False)
        if not p1_under and not p2_under:
            return
        splitscreen = (
            self.view.splitscreen_mode
            and render_state is not None
            and render_state.splitscreen_active
        )
        w, h = self.view.width(), self.view.height()
        if splitscreen:
            half = w // 2
            if p1_under:
                self._fill_underwater_rect(painter, 0, 0, half, h)
            if p2_under:
                self._fill_underwater_rect(painter, half, 0, half, h)
        elif p1_under:
            self._fill_underwater_rect(painter, 0, 0, w, h)

    def _fill_underwater_rect(self, painter, x, y, w, h):
        tint = getattr(self.view, '_cached_underwater_tint', None) or [0.0, 0.4, 0.6]
        r = int(max(0.0, min(1.0, tint[0])) * 255)
        g = int(max(0.0, min(1.0, tint[1])) * 255)
        b = int(max(0.0, min(1.0, tint[2])) * 255)
        # Slow "breathing" so the immersion feels alive rather than a static filter
        wobble = math.sin((time.perf_counter() - self.view.start_time) * 1.7) * 10.0
        grad = QLinearGradient(0, y, 0, y + h)
        grad.setColorAt(0.0, QColor(r, g, b, max(0, min(255, int(95 + wobble)))))
        grad.setColorAt(0.55, QColor(int(r * 0.6), int(g * 0.7), int(b * 0.75), 130))
        grad.setColorAt(1.0, QColor(int(r * 0.3), int(g * 0.4), int(b * 0.5), 165))
        painter.fillRect(x, y, w, h, QBrush(grad))

    #: Directional damage indicators: an arc at the very edge of the view, on
    #: the side a hit came from, with an arrow head pointing out at it. Never
    #: opaque: the game stays visible through them.
    DAMAGE_ARC_SPAN = 34.0        # degrees
    DAMAGE_ARC_OPACITY = 0.55     # at the hit; fades to nothing
    DAMAGE_ARC_RADIUS = 0.46      # of the view's shorter side: the arc's curve
    DAMAGE_ARC_WIDTH = 0.030
    DAMAGE_ARC_COLOUR = (225, 24, 24)

    @staticmethod
    def damage_bearing(view, player_pos, source):
        """Screen bearing of *source* from the player, radians clockwise from up.

        The horizontal offset is turned into view space; what lies ahead of
        the camera (-z) and what lies up the screen (+y) both count as "up".
        So one rule serves both cameras: in first person the offset has no
        height and "ahead" decides; looking straight down from overhead it
        has no depth and "up the screen" decides; a tilted overhead camera
        has both, and they agree. None when the source is on the player.
        """
        dx = float(source[0]) - float(player_pos[0])
        dz = float(source[2]) - float(player_pos[2])
        if dx * dx + dz * dz < 1.0:
            return None
        vx = view[0][0] * dx + view[2][0] * dz
        vy = view[0][1] * dx + view[2][1] * dz
        vz = view[0][2] * dx + view[2][2] * dz
        return math.atan2(vx, vy - vz)

    @staticmethod
    def damage_edge_point(bearing, width, height, margin):
        """Where a line from the view's middle along *bearing* meets its edge,
        *margin* pixels in: (x, y) from the middle, y down."""
        dx, dy = math.sin(bearing), -math.cos(bearing)
        half_w = max(1.0, width / 2.0 - margin)
        half_h = max(1.0, height / 2.0 - margin)
        reach = min(half_w / abs(dx) if abs(dx) > 1e-9 else math.inf,
                    half_h / abs(dy) if abs(dy) > 1e-9 else math.inf)
        return dx * reach, dy * reach

    def _draw_damage_indicators(self, painter, render_state,
                                viewport_width=None, viewport_height=None):
        marks = render_state.damage_marks if render_state is not None else ()
        if not marks or render_state.cinematic_camera_active:
            return
        if not self.view.editor.config.getboolean(
                'Display', 'damage_indicators', fallback=True):
            return
        width = viewport_width or self.view.width()
        height = viewport_height or self.view.height()
        side = min(width, height)
        radius = side * self.DAMAGE_ARC_RADIUS
        band = side * self.DAMAGE_ARC_WIDTH
        outer, inner = radius + band / 2.0, radius - band / 2.0
        span = self.DAMAGE_ARC_SPAN
        start = 90.0 - span / 2.0

        # The arc, pointing up, the tip of its arrow head at the origin; each
        # mark puts that on the edge of the view and turns it to its bearing.
        drop = outer + band * 1.15
        margin = max(3.0, band * 0.25)
        path = QPainterPath()
        ring = QRectF(-outer, drop - outer, 2 * outer, 2 * outer)
        hole = QRectF(-inner, drop - inner, 2 * inner, 2 * inner)
        path.arcMoveTo(ring, start)
        path.arcTo(ring, start, span)
        path.arcTo(hole, start + span, -span)
        path.closeSubpath()
        head = QPainterPath()
        head.moveTo(0.0, 0.0)
        head.lineTo(band * 0.95, band * 0.95)
        head.lineTo(-band * 0.95, band * 0.95)
        head.closeSubpath()

        red, green, blue = self.DAMAGE_ARC_COLOUR
        view = render_state.camera_view_matrix
        player_pos = render_state.player_pos
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.translate(width / 2.0, height / 2.0)
        for x, y, z, strength in marks:
            bearing = self.damage_bearing(view, player_pos, (x, y, z))
            if bearing is None:
                continue
            # Full for the first half of its life, then fading out.
            alpha = self.DAMAGE_ARC_OPACITY * max(0.0, min(1.0, strength * 2.0))
            solid = QColor(red, green, blue, int(255 * alpha))
            clear = QColor(red, green, blue, 0)
            # Strongest in the middle of the arc, gone at its ends.
            sweep = QConicalGradient(0.0, drop, start)
            sweep.setColorAt(0.0, clear)
            sweep.setColorAt(span / 720.0, solid)
            sweep.setColorAt(span / 360.0, clear)
            sweep.setColorAt(1.0, clear)
            edge_x, edge_y = self.damage_edge_point(bearing, width, height, margin)
            painter.save()
            painter.translate(edge_x, edge_y)
            painter.rotate(math.degrees(bearing))
            painter.fillPath(path, QBrush(sweep))
            painter.fillPath(head, solid)
            painter.restore()
        painter.restore()

    def _draw_fps_counter(self, painter):
        painter.setFont(self.view._fps_font)
        painter.setPen(self.view._hud_white_pen)
        rect_width = 100
        rect_x = self.view.width() - rect_width - 5
        painter.fillRect(rect_x, 5, rect_width, 20, self.view._hud_fps_bg_brush)
        painter.drawText(rect_x + 5, 20, f"FPS: {self.view.fps:.0f}")

    def _draw_sprites_text(self, painter):
        painter.setFont(self.view._sprites_font)
        painter.setPen(self.view._hud_pink_pen)
        painter.fillRect(5, 5, 80, 25, self.view._hud_sprites_bg_brush)
        painter.drawText(10, 20, "Sprites")

    @staticmethod
    def _hud_health_point_size(style, viewport_height):
        """Return the responsive health-text size for a HUD style."""
        if style == 3:
            return 24
        if style == 2:
            # Style 2 is deliberately more compact than the default HUD.
            return max(32, min(52, int(viewport_height * 0.065)))
        return max(42, min(68, int(viewport_height * 0.085)))

    def _draw_hud(self, painter, render_state, viewport_width=None, viewport_height=None):
        if self.view._hud_style == 0:
            return
        # A LogicCamera owns the player's view completely: no HUD is shown
        # while the cinematic is running.
        if render_state is not None and render_state.cinematic_camera_active:
            return
        if viewport_width is None:
            viewport_width = self.view.width()
        if viewport_height is None:
            viewport_height = self.view.height()
        # In overhead (top-down) mode there is no first-person view, so the gun
        # HUD sprite makes no sense; the held weapon is shown as a small pickup
        # icon bottom-right instead (see below), alongside any held keys.
        # LogicCamera is the authoritative camera mode.
        overhead = self.view.logic_thread.camera.is_overhead()
        health = self.view._cached_health
        max_health = self.view._cached_max_health
        if health is None or max_health is None:
            return
        hud_margin = 20
        active_weapon = self.view._cached_active_weapon
        # What the item in hand is, as this session compiled it.
        held = self._session_item(active_weapon)

        # The entire HUD fades back in for four seconds after a LogicCamera
        # gives control back to the player. The health count has its own
        # independent opacity state, normally resting at 50% when idle.
        hud_alpha = max(
            0.0, min(1.0, float(render_state.hud_alpha))
        )
        hud_opacity = 1.0 if self.view._hud_style == 3 else self.view._hud_opacity / 100.0
        health_hud_alpha = 1.0 if self.view._hud_style == 3 else max(
            0.0, min(1.0, float(render_state.hud_health_alpha))
        )
        # _draw_hud owns this painter opacity for everything it draws: weapon,
        # health/ammo, crosshair, messages, prompts, overhead icons and keys.
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity)

        # Draw the weapon before the status counts so the health indicator is
        # always visually on top of any weapon sprite.
        if active_weapon and not overhead and held is not None:
            hud_pixmap = self._hud_pixmap(held.hud_sprite)
            if hud_pixmap and not hud_pixmap.isNull():
                # The definition places the item: right-hand, centred, or
                # left-hand, at its own height on a 600-pixel view.
                target_h = int(held.hud_height * viewport_height / 600.0)
                cache_key = (held.hud_sprite, target_h)
                scaled = self.view._cached_gun_hud.get(cache_key)
                if scaled is None or scaled.isNull():
                    if hud_pixmap.height() > 0:
                        target_w = int(hud_pixmap.width() * (target_h / hud_pixmap.height()))
                    else:
                        target_w = target_h
                    img = hud_pixmap.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
                    scaled = QPixmap.fromImage(img).scaled(
                        target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self.view._cached_gun_hud[cache_key] = scaled
                if held.hud_align == 'center':
                    x = (viewport_width - scaled.width()) // 2
                elif held.hud_align == 'left':
                    x = 20
                else:
                    x = viewport_width - scaled.width() - 20
                y = viewport_height - scaled.height()
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                painter.drawPixmap(x, y, scaled)
                painter.restore()
                if (getattr(self.view, '_cached_muzzle_flash', False)
                        and held.weapon is not None and held.weapon.muzzle_flash):
                    flash_pixmap = self._hud_pixmap(held.weapon.muzzle_flash)
                    if flash_pixmap and not flash_pixmap.isNull():
                        painter.save()
                        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                        painter.drawPixmap(x, y, scaled.width(), scaled.height(), flash_pixmap)
                        painter.restore()

        style = self.view._hud_style
        health_font = QFont(self.view._hud_status_font)
        health_font.setPointSize(
            self._hud_health_point_size(style, viewport_height)
        )
        painter.setFont(health_font)
        health_color = self.view._hud_health_orange
        painter.setPen(health_color)
        health_text = str(int(health))
        metrics = QFontMetrics(health_font)
        # Pin health against the bottom-left edge of the viewport.
        health_x = 0
        health_y = self._hud_count_baseline(metrics, health_text, viewport_height)

        # Health remains the Fio orange count. Only health gets the independent
        # damage fade; ammo follows the normal whole-HUD opacity.
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity * health_hud_alpha)
        painter.setPen(self.view._hud_count_shadow_pen)
        painter.drawText(health_x + 2, health_y + 2, health_text)
        painter.setPen(health_color)
        painter.drawText(health_x, health_y, health_text)
        painter.restore()

        lines = self._hud_stat_lines(
            style, held.weapon if held is not None else None,
            self.view._cached_player_ammo, self.view._cached_player_armor)
        if lines:
            ammo_font = QFont(health_font) if style == 3 else QFont(self.view._hud_status_font)
            ammo_font.setPointSize(
                24 if style == 3 else max(22, min(36, int(viewport_height * 0.045)))
            )
            ammo_x = health_x + metrics.horizontalAdvance(health_text)
            if style == 3:
                # Style 3 uses a single compact status line: separate ammo
                # from health with two spaces and keep the counters white.
                ammo_x += 2 * metrics.horizontalAdvance(" ")
            painter.setFont(ammo_font)
            line_height = QFontMetrics(ammo_font).ascent()
            baseline = health_y - line_height * (len(lines) - 1)
            for text in lines:
                painter.setPen(self.view._hud_count_shadow_pen)
                painter.drawText(ammo_x + 2, baseline + 2, text)
                painter.setPen(Qt.white if style == 3 else self.view._hud_ammo_green)
                painter.drawText(ammo_x, baseline, text)
                baseline += line_height

        # The centre-screen crosshair is a first-person aiming reticle: it marks
        # where the camera-forward hitscan lands. In overhead (top-down) mode the
        # shot travels along the player's ground heading, not through screen
        # centre, so the reticle would be misleading — draw it only first-person.
        if active_weapon and not overhead:
            cx = viewport_width // 2
            cy = viewport_height // 2
            size = 10
            painter.setPen(QPen(QColor(0, 0, 0), 4))
            painter.drawLine(cx - size, cy, cx + size, cy)
            painter.drawLine(cx, cy - size, cx, cy + size)
            painter.setPen(QPen(QColor(0, 255, 0), 2))
            painter.drawLine(cx - size, cy, cx + size, cy)
            painter.drawLine(cx, cy - size, cx, cy + size)
        msg = self.view._cached_hud_message
        prompt_key = render_state.hud_prompt_key if render_state is not None else None
        if msg:
            if self.view._cached_hud_message != msg:
                self.view._cached_hud_message = msg
                self.view._cached_hud_message_width = QFontMetrics(self.view._hud_msg_font).horizontalAdvance(msg)
            cx = viewport_width // 2
            cy = viewport_height // 2 + 50
            tw = self.view._cached_hud_message_width
            painter.setFont(self.view._hud_msg_font)
            painter.setPen(self.view._hud_shadow_pen)
            painter.drawText(cx - tw // 2 + 2, cy + 2, msg)
            painter.setPen(self.view._hud_grey_pen)
            painter.drawText(cx - tw // 2, cy, msg)

            if prompt_key:
                # cy is the text baseline, not its lower edge. Position
                # the key from the actual ink bounds so every HUD font keeps
                # the icon directly underneath the prompt rather than relying
                # on a font-independent magic Y offset.
                text_bottom = QFontMetrics(self.view._hud_msg_font).tightBoundingRect(msg).bottom()
                prompt_y = cy + text_bottom + 8
                prompt_size = self.view._cached_prompt_key_size
                if self.view._cached_prompt_key != prompt_key:
                    self.view._cached_prompt_key = prompt_key
                    self.view._cached_prompt_key_pixmap = None
                    self.view._cached_prompt_key_loaded = False
                if not self.view._cached_prompt_key_loaded:
                    self.view._cached_prompt_key_loaded = True
                    try:
                        pixmap = Prop.get_key_pixmap(prompt_key)
                        if pixmap and not pixmap.isNull():
                            self.view._cached_prompt_key_pixmap = pixmap.scaled(
                                prompt_size, prompt_size,
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    except Exception:
                        self.view._cached_prompt_key_pixmap = None
                if (self.view._cached_prompt_key_pixmap is not None
                        and not self.view._cached_prompt_key_pixmap.isNull()):
                    scaled = self.view._cached_prompt_key_pixmap
                    painter.drawPixmap(
                        cx - scaled.width() // 2,
                        prompt_y,
                        scaled,
                    )
                else:
                    self._draw_key_fallback(
                        painter,
                        prompt_key,
                        cx - prompt_size // 2,
                        prompt_y,
                        prompt_size,
                    )
        hint = self.view._play_mode_hint
        if hint and not msg:
            if self.view._cached_hint_text != hint:
                self.view._cached_hint_text = hint
                self.view._cached_hint_width = QFontMetrics(self.view._hud_msg_font).horizontalAdvance(hint)
            cx = viewport_width // 2
            cy = viewport_height // 2 + 50
            tw = self.view._cached_hint_width
            painter.setFont(self.view._hud_msg_font)
            painter.setPen(self.view._hud_shadow_pen)
            painter.drawText(cx - tw // 2 + 2, cy + 2, hint)
            painter.setPen(self.view._hud_grey_pen)
            painter.drawText(cx - tw // 2, cy, hint)
        if self.view._actor_pick is not None:
            self.view.picking._draw_actor_pick_hint(painter, viewport_width)
        # Overhead: held weapon shown as a bottom-right collectible icon (like keys).
        # It takes the rightmost slot; keys shift left so both fit side by side.
        # The bottom-right corner holds the item icon (overhead: the weapon in
        # hand; first-person: the slot-key flash, while it shows) and the
        # collected keys; the icon takes the rightmost slot and the keys sit
        # to its left, so the two never overlap.
        item_alpha = 0.0
        if held is not None:
            if overhead:
                item_alpha = 1.0
            elif self.view._weapon_switch_shown_at is not None:
                # A slot key took this weapon in hand: show it for a moment,
                # fading in and out quickly.
                item_alpha = self._weapon_switch_alpha(
                    time.perf_counter() - self.view._weapon_switch_shown_at)
                if item_alpha <= 0.0:
                    self.view._weapon_switch_shown_at = None
        item_pixmap = self._hud_pixmap(held.world_sprite) if item_alpha > 0.0 else None
        item_shown = item_pixmap is not None and not item_pixmap.isNull()
        collected_keys = self.view._cached_collected_keys
        item_rect, key_rects = self._hud_corner_layout(
            viewport_width, viewport_height, hud_margin, item_shown,
            len(collected_keys) if collected_keys else 0)
        if item_rect is not None:
            painter.save()
            painter.setOpacity(painter.opacity() * item_alpha)
            self._draw_item_icon(painter, held, item_rect)
            painter.restore()

        if collected_keys:
            key_size = self.HUD_CORNER_ICON
            if self.view._cached_key_size != key_size:
                self.view._cached_key_pixmaps.clear()
                self.view._cached_key_size = key_size
            for key_name, key_rect in zip(sorted(collected_keys), key_rects):
                icon_x, key_y = key_rect.x(), key_rect.y()
                cached = self.view._cached_key_pixmaps.get(key_name)
                if cached is not None and not cached.isNull():
                    painter.drawPixmap(icon_x, key_y, cached)
                    continue
                try:
                    pixmap = Prop.get_key_pixmap(key_name)
                    if pixmap and not pixmap.isNull():
                        scaled = pixmap.scaled(key_size, key_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self.view._cached_key_pixmaps[key_name] = scaled
                        painter.drawPixmap(icon_x, key_y, scaled)
                    else:
                        self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)
                except Exception:
                    self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)

        painter.restore()

    def _draw_hud_splitscreen(self, painter, render_state):
        if self.view._hud_style == 0:
            return
        if render_state is not None and render_state.cinematic_camera_active:
            return
        w, h = self.view.width(), self.view.height()
        half = w // 2
        painter.setPen(QPen(QColor(0, 0, 0), 4))
        painter.drawLine(half, 0, half, h)
        painter.setPen(QPen(QColor(80, 80, 80), 2))
        painter.drawLine(half, 0, half, h)
        painter.save()
        painter.setClipRect(0, 0, half, h)
        self._draw_hud(painter, render_state, viewport_width=half, viewport_height=h)
        painter.restore()
        hud_alpha = max(
            0.0, min(1.0, float(render_state.hud_alpha))
        )
        hud_opacity = 1.0 if self.view._hud_style == 3 else self.view._hud_opacity / 100.0
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity)
        painter.setPen(QColor(255, 200, 50))
        painter.setFont(self.view._hud_font)
        painter.drawText(8, 22, "P1")
        painter.restore()
        p2_health = render_state.player2_health
        p2_max_health = render_state.player2_max_health
        p2_dead = render_state.player2_dead
        health_hud_alpha = 1.0 if self.view._hud_style == 3 else max(
            0.0, min(1.0, float(render_state.hud_health_alpha))
        )
        painter.save()
        painter.setClipRect(half, 0, half, h)
        margin = 1
        health_font = QFont(self.view._hud_status_font)
        health_font.setPointSize(
            self._hud_health_point_size(self.view._hud_style, h)
        )
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity * health_hud_alpha)
        painter.setFont(health_font)
        p2_health_color = self.view._hud_health_orange
        painter.setPen(p2_health_color)
        health_text = str(int(p2_health))
        metrics = QFontMetrics(health_font)
        painter.drawText(
            half + margin,
            self._hud_count_baseline(metrics, health_text, h),
            health_text,
        )
        painter.restore()
        cx = half + half // 2
        cy = h // 2
        sz = 10
        painter.setPen(QPen(QColor(0, 0, 0), 4))
        painter.drawLine(cx - sz, cy, cx + sz, cy)
        painter.drawLine(cx, cy - sz, cx, cy + sz)
        painter.setPen(QPen(QColor(0, 200, 255), 2))
        painter.drawLine(cx - sz, cy, cx + sz, cy)
        painter.drawLine(cx, cy - sz, cx, cy + sz)
        painter.save()
        painter.setOpacity(hud_alpha)
        painter.setFont(self.view._hud_font)
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(half + 8, 22, "P2")
        painter.restore()
        if p2_dead:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(120, 0, 0, 140)))
            painter.drawRect(half, 0, half, h)
            painter.setFont(self.view._death_title_font)
            lbl = "P2 DIED"
            lbl_w = QFontMetrics(self.view._death_title_font).horizontalAdvance(lbl)
            lbl_x = half + (half - lbl_w) // 2
            painter.setPen(QColor(60, 0, 0, 220))
            painter.drawText(lbl_x + 3, h // 2 + 3, lbl)
            painter.setPen(QColor(255, 60, 60))
            painter.drawText(lbl_x, h // 2, lbl)
        painter.restore()

    def _draw_death_screen(self, painter):
        w, h = self.view.width(), self.view.height()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(120, 0, 0, 160)))
        painter.drawRect(0, 0, w, h)
        painter.setFont(self.view._death_title_font)
        title_x = (w - self.view._cached_death_title_width) // 2
        title_y = h // 2 - 20
        painter.setPen(QColor(60, 0, 0, 220))
        painter.drawText(title_x + 3, title_y + 3, "DIED")
        painter.setPen(QColor(255, 60, 60))
        painter.drawText(title_x, title_y, "DIED")
        painter.setFont(self.view._death_sub_font)
        sub_x = (w - self.view._cached_death_sub_width) // 2
        sub_y = title_y + 60
        painter.setPen(QColor(0, 0, 0, 180))
        painter.drawText(sub_x + 2, sub_y + 2, "Press Escape to return to the editor")
        painter.setPen(QColor(220, 180, 180))
        painter.drawText(sub_x, sub_y, "Press Escape to return to the editor")

    def _draw_key_fallback(self, painter, key_name, x, y, size):
        color, pen, brush = self.view._key_fallback_cache.get(key_name, self.view._key_fallback_default)
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawRoundedRect(x, y, size, size, 4, 4)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        mid = y + size // 2
        painter.drawLine(x + 8, mid, x + size - 8, mid)
        painter.drawEllipse(x + 4, mid - 6, 12, 12)
        painter.drawLine(x + size - 10, mid, x + size - 10, mid + 6)
        painter.drawLine(x + size - 14, mid, x + size - 14, mid + 4)

    def _draw_render_menu(self, painter):
        width, height = 200, 120
        x, y = (self.view.width() - width) // 2, (self.view.height() - height) // 2
        painter.fillRect(x, y, width, height, QColor(20, 20, 20, 230))
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.drawRect(x, y, width, height)
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(x + 10, y + 25, "Render Mode")
        font.setBold(False)
        font.setPointSize(10)
        painter.setFont(font)
        options = [(RENDER_MODE_LIT, "[1] Lit"), (RENDER_MODE_UNLIT, "[2] Unlit"), (RENDER_MODE_WIREFRAME, "[3] Wire"), (RENDER_MODE_VERTEX, "[4] Vert")]
        cy = y + 55
        for mid, txt in options:
            if self.view.current_render_mode == mid:
                painter.setPen(QColor(100, 255, 100))
                painter.drawText(x + 20, cy, "> " + txt)
            else:
                painter.setPen(QColor(200, 200, 200))
                painter.drawText(x + 20, cy, "  " + txt)
            cy += 20

    def _draw_level_complete_overlay(self, painter):
        w, h = self.view.width(), self.view.height()
        # Dim background
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 180)))
        painter.drawRect(0, 0, w, h)

        # Title
        title = self.view._cached_level_complete_ui.get('title', 'Complete')
        painter.setFont(QFont("Fio UI", 48, QFont.Bold))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(title)
        painter.setPen(QColor(255, 215, 0))
        painter.drawText((w - tw) // 2, h // 2 - 60, title)

        # Button
        btn_w, btn_h = 280, 50
        btn_x = (w - btn_w) // 2
        btn_y = h // 2
        painter.setPen(QPen(QColor(200, 200, 200), 2))
        painter.setBrush(QBrush(QColor(60, 60, 60, 220)))
        painter.drawRoundedRect(btn_x, btn_y, btn_w, btn_h, 8, 8)

        painter.setFont(QFont("Fio UI", 16, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        btn_text = self.view._cached_level_complete_ui.get('button_text', 'Continue to Next Map')
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(btn_text)
        painter.drawText(btn_x + (btn_w - tw) // 2, btn_y + 34, btn_text)

        # Store rect for click detection
        self.view._level_complete_btn_rect = QRect(btn_x, btn_y, btn_w, btn_h)

        # Hint
        painter.setFont(QFont("Fio UI", 12))
        painter.setPen(QColor(180, 180, 180))
        hint = "E to continue, Esc to cancel"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(hint)
        painter.drawText((w - tw) // 2, btn_y + btn_h + 30, hint)

    def _confirm_level_complete(self):
        ui = getattr(self.view, '_cached_level_complete_ui', None)
        if not ui:
            return
        target_map = ui.get('target_map', '')
        if ui.get('generate_level'):
            self.view.editor.generate_level_signal.emit(dict(ui.get('generator_params') or {}))
        elif target_map:
            self.view.editor.load_level_signal.emit(target_map, ui.get('destination_spawn', ''))
        self.view._cached_level_complete_ui = None
        self.view._level_complete_btn_rect = None
        if self.view.logic_thread:
            self.view.logic_thread.interaction_runtime.level_complete_ui = None

    def _cancel_level_complete(self):
        self.view._cached_level_complete_ui = None
        self.view._level_complete_btn_rect = None
        if self.view.logic_thread:
            self.view.logic_thread.interaction_runtime.level_complete_ui = None

    def _session_item(self, item_id):
        """The compiled item *item_id* of the running session, or None."""
        if not item_id or self.view.logic_thread is None:
            return None
        return self.view.logic_thread.combat_runtime.items.resolve(item_id)

    def _hud_pixmap(self, path):
        """The QPixmap of the HUD sprite at *path*, or None if there is none."""
        if path not in self.view.hud_pixmaps:
            self.view.hud_pixmaps[path] = load_pixmap(path) if path and asset_exists(path) else None
        return self.view.hud_pixmaps[path]

    @staticmethod
    def _hud_stat_lines(style, weapon, ammo, armor):
        """The ammo and armor counts beside health, top line first.

        Ammo shows for a weapon that fires ('∞' if it spends nothing per
        shot), armor when the player has any; they share the ammo column and
        colour, armor underneath. Styles 2 and 3 put them on the bottom line
        instead, two spaces apart.
        """
        stats = []
        if weapon is not None and weapon.fires:
            stats.append("∞" if weapon.ammo_per_shot == 0 else str(max(0, int(ammo))))
        if armor > 0:
            stats.append(str(int(armor)))
        if style in (2, 3) and stats:
            return ["  ".join(stats)]
        return stats

    @classmethod
    def _weapon_switch_alpha(cls, elapsed):
        """Opacity of the weapon-slot flash *elapsed* seconds in; 0 when over."""
        if elapsed < 0.0:
            return 0.0
        if elapsed < cls.WEAPON_SWITCH_FADE_IN:
            return elapsed / cls.WEAPON_SWITCH_FADE_IN
        elapsed -= cls.WEAPON_SWITCH_FADE_IN
        if elapsed < cls.WEAPON_SWITCH_HOLD:
            return 1.0
        elapsed -= cls.WEAPON_SWITCH_HOLD
        return max(0.0, 1.0 - elapsed / cls.WEAPON_SWITCH_FADE_OUT)

    @classmethod
    def _hud_corner_layout(cls, viewport_width, viewport_height, margin,
                           item_shown, key_count):
        """``(item rect or None, [key rects])`` of the bottom-right HUD icons.

        The item icon, when shown, takes the rightmost slot; the keys start
        left of it, so the two never overlap.
        """
        size = cls.HUD_CORNER_ICON
        right = viewport_width - margin
        y = viewport_height - margin - size
        item = QRect(right - size, y, size, size) if item_shown else None
        if item is not None:
            right = item.x() - cls.HUD_CORNER_GAP
        keys = [QRect(right - size - i * cls.HUD_KEY_STEP, y, size, size)
                for i in range(key_count)]
        return item, keys

    def _draw_item_icon(self, painter, item, rect):
        """Draw *item*'s world sprite centred in *rect*."""
        pm = self._hud_pixmap(item.world_sprite)
        if not pm or pm.isNull():
            return
        size = rect.width()
        cache_key = (item.world_sprite, size)
        scaled = self.view._cached_item_icons.get(cache_key)
        if scaled is None or scaled.isNull():
            scaled = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.view._cached_item_icons[cache_key] = scaled
        painter.drawPixmap(rect.x() + (size - scaled.width()) // 2,
                           rect.y() + (size - scaled.height()) // 2, scaled)
