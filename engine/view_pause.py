"""The in-game pause menu, delegated from QtGameView.

Opening and closing it (which pauses the world), its pages (root, save
slots, options, video, plugin items), keyboard and mouse handling, the
blurred backdrop and the fade in and out.
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


class ViewPause:
    #: Bundled face the menu is set in (assets/fonts), and the fallback.
    PAUSE_MENU_FONT = "HornetDisplay-Regular.ttf"
    PAUSE_MENU_FALLBACK_FONT = "Fio UI"
    #: Seconds the blurred backdrop takes to fade in, and out again.
    PAUSE_FADE_SECONDS = 1.0
    #: How far the backdrop is shrunk before it is scaled back up: the blur.
    PAUSE_BLUR_FACTOR = 16

    def __init__(self, view):
        self.view = view

    @property
    def pause_menu_active(self) -> bool:
        return self.view.pause_menu is not None

    def open_pause_menu(self) -> bool:
        """Open the pause menu: the world pauses and the cursor is freed.

        Returns False outside Play Mode or when it is already open.
        """
        if not self.view.play_mode or self.view.pause_menu is not None:
            return False
        from engine.pause_menu import PauseMenu
        level = self._pause_fade_level()
        if self.view._pause_backdrop is None:
            # Grabbed before the menu exists, so the frame is the game alone.
            # (Reopened while the last one still fades out, it keeps that one.)
            self.view._pause_backdrop = self._blurred_backdrop()
            level = 0.0
        self._start_pause_fade(level, 1.0)
        self.view.pause_menu = PauseMenu(self._pause_root_page())
        from engine.pause_menu_window import PauseMenuWindow
        self.view._pause_window = PauseMenuWindow(self.view.pause_menu, self._pause_font)
        if self.view._pause_window_pos is None:
            self.view._pause_window.centre_in(self.view.width(), self.view.height())
        else:
            self.view._pause_window.window_rect.moveTo(*self.view._pause_window_pos)
        logic = self.view.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(logic.session_runtime.PAUSE_MENU, True)
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        QApplication.setOverrideCursor(Qt.ArrowCursor)
        self.view.update()
        return True

    def close_pause_menu(self) -> bool:
        """Close the pause menu and play on."""
        if self.view.pause_menu is None:
            return False
        level = self._pause_fade_level()
        self.view.pause_menu = None
        self._drop_pause_window()
        # Play resumes now; the paused picture fades out over it.
        self._start_pause_fade(level, 0.0)
        logic = self.view.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(logic.session_runtime.PAUSE_MENU, False)
        if self.view.play_mode and not self.view.console_overlay_active:
            if self.view._actor_pick is not None:
                self.view.picking._show_pick_cursor()
            else:
                self.view._capture_play_cursor()
        self.view.update()
        return True

    def _pause_root_page(self):
        """Resume, Save, Load, Options, the plugins' items, then Exit.

        Exit leaves Play for the editor -- or quits the game in kiosk mode,
        as Esc did before the menu existed.
        """
        from engine.pause_menu import Item, Page
        editor = self.view.editor
        items = [
            Item("Resume", self.close_pause_menu),
            Item("Save Game", page=lambda: self._pause_slots_page(save=True)),
            Item("Load Game", page=lambda: self._pause_slots_page(save=False)),
            Item("Options", page=self._pause_options_page),
        ]
        mgr = self._plugin_manager()
        if mgr is not None:
            for label, callback, close_menu in mgr.pause_menu_items():
                items.append(Item(label, self._plugin_pause_action(mgr, callback, close_menu)))
        kiosk = bool(getattr(editor, 'is_kiosk_mode', False))
        items.append(Item("Quit" if kiosk else "Exit to Editor", self._exit_from_pause_menu))
        return Page("Paused", items)

    def _pause_slots_page(self, save):
        from engine.pause_menu import Item, Page
        editor = self.view.editor

        def label(slot):
            stamp = editor.save_slot_time(slot)
            return f"Slot {slot}    {stamp}" if stamp else f"Slot {slot}    Empty"

        def run(slot):
            def action():
                if save:
                    ok = editor.save_to_slot(slot)
                    self.view.pause_menu.notice = (f"Saved to Slot {slot}" if ok
                                              else "Save failed (see the console)")
                    return
                # A load restores the saved session into this one; play on.
                if editor.load_from_slot(slot):
                    self.close_pause_menu()
                elif self.view.pause_menu is not None:
                    self.view.pause_menu.notice = "Load failed (see the console)"
            return action

        items = [Item((lambda n=n: label(n)), run(n),
                      enabled=True if save else (lambda n=n: editor.save_slot_time(n) is not None))
                 for n in range(1, editor.SAVE_SLOTS + 1)]
        return Page("Save Game" if save else "Load Game", items)

    def _pause_options_page(self):
        from engine.pause_menu import Item, Page
        editor = self.view.editor
        return Page("Options", [
            Item("Sound", slider=(editor.sound_volume, editor.set_sound_volume, 0, 100, 5)),
            Item("Music", slider=(editor.music_volume, editor.set_music_volume, 0, 100, 5)),
            Item("Video", page=self._pause_video_page),
        ])

    def _pause_video_page(self):
        """The window mode of Settings > Play Modes > Display Mode."""
        from engine.pause_menu import Item, Page
        editor = self.view.editor
        return Page("Video", [
            Item(mode, (lambda m=mode: editor.set_kiosk_window_mode(m)),
                 checked=(lambda m=mode: editor.kiosk_window_mode() == m))
            for mode in editor.KIOSK_WINDOW_MODES
        ])

    @staticmethod
    def _plugin_manager():
        try:
            from plugins.manager import get_manager
            return get_manager()
        except Exception:
            return None

    def _plugin_pause_action(self, mgr, callback, close_menu):
        def action():
            mgr.run_pause_menu_item(callback, main_window=self.view.editor,
                                    logic=self.view.logic_thread)
            if close_menu:
                self.close_pause_menu()
        return action

    def _exit_from_pause_menu(self):
        # Kiosk mode asks before quitting; a No keeps the menu open.
        self.view.editor.leave_play_mode_from_escape()
        if not self.view.play_mode:
            self.view.pause_menu = None
            self._end_pause_fade()
            self._drop_pause_window()

    @staticmethod
    def _pause_clock():
        return time.perf_counter()

    def _start_pause_fade(self, from_level, to_level):
        self.view._pause_fade = (self._pause_clock(), float(from_level), float(to_level))
        self.view._pause_fade_timer.start()        # repaints: a paused world draws no new frames

    def _pause_fade_level(self):
        """How far the backdrop has faded in, 0..1; finishing a fade ends it."""
        fade = self.view._pause_fade
        if fade is None:
            return 1.0 if self.view.pause_menu is not None or self.view._pause_backdrop is not None else 0.0
        start, from_level, to_level = fade
        duration = self.PAUSE_FADE_SECONDS
        t = 1.0 if duration <= 0 else min(1.0, max(0.0, (self._pause_clock() - start) / duration))
        level = from_level + (to_level - from_level) * t
        if t >= 1.0:
            self.view._pause_fade = None
            self.view._pause_fade_timer.stop()
            if to_level <= 0.0:
                self.view._pause_backdrop = None
        return level

    def _end_pause_fade(self):
        self.view._pause_fade = None
        self.view._pause_fade_timer.stop()
        self.view._pause_backdrop = None

    def _drop_pause_window(self):
        """Forget the window, remembering where it was for the next opening."""
        window = self.view._pause_window
        if window is not None:
            self.view._pause_window_pos = (window.window_rect.x(), window.window_rect.y())
        self.view._pause_window = None

    def handle_pause_menu_key(self, event) -> bool:
        """Pause-menu keys in Play. True when the key was used.

        Esc opens the menu -- unless the player is dead, when it leaves Play
        as before -- and goes back a page (closing it from the first).
        Up/Down or W/S choose, Left/Right or A/D move a slider, Enter, Space
        or E activates. Every other key is swallowed while the menu is open.
        """
        key = event.key()
        menu = self.view.pause_menu
        if menu is None:
            if key == Qt.Key_Escape and not event.isAutoRepeat() \
                    and not self.view.game_state.published('player_dead', False):
                return self.open_pause_menu()
            return False
        if key == Qt.Key_Escape:
            if not event.isAutoRepeat() and not menu.back():
                self.close_pause_menu()
        elif key in (Qt.Key_Up, Qt.Key_W):
            menu.move(-1)
        elif key in (Qt.Key_Down, Qt.Key_S):
            menu.move(1)
        elif key in (Qt.Key_Left, Qt.Key_A):
            menu.adjust(-1)
        elif key in (Qt.Key_Right, Qt.Key_D):
            menu.adjust(1)
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_E):
            if not event.isAutoRepeat():
                menu.activate()
        self.view.update()
        return True

    def _click_pause_menu(self, x, y):
        menu = self.view.pause_menu
        index, fraction = menu.hit(x, y)
        if index is None:
            return
        menu.selected = index
        item = menu.item
        if item.slider is not None:
            if fraction is not None and item.enabled:
                item.set_slider_fraction(fraction)
        else:
            menu.activate()
        self.view.update()

    def _hover_pause_menu(self, x, y, drag=False):
        menu = self.view.pause_menu
        index, fraction = menu.hit(x, y)
        if index is None or not menu.page.items[index].enabled:
            return
        if index != menu.selected:
            menu.selected = index
            self.view.update()
        item = menu.item
        if drag and item.slider is not None and fraction is not None:
            item.set_slider_fraction(fraction)
            self.view.update()

    def _blurred_backdrop(self):
        """The current frame, blurred, or None (no GL frame to grab).

        The world is paused while the menu is open, so one grab serves the
        whole time it is shown. Shrinking with smooth filtering in two steps
        and drawing it back at full size with smooth filtering is a cheap,
        even blur.
        """
        try:
            image = self.view.grabFramebuffer()
        except Exception:
            return None
        if image is None or image.isNull():
            return None
        w, h = image.width(), image.height()
        f = self.PAUSE_BLUR_FACTOR
        small = image.scaled(max(1, w // f), max(1, h // f),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return small.scaled(max(1, w // 4), max(1, h // 4),
                            Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def _pause_font(self, size):
        if self.view._pause_menu_family is None:
            self.view._pause_menu_family = (self.view.hud._resolve_hud_font_family(self.PAUSE_MENU_FONT)
                                       or self.PAUSE_MENU_FALLBACK_FONT)
        font = QFont(self.view._pause_menu_family)
        font.setPixelSize(size)
        return font

    def _draw_pause_menu(self, painter):
        """The blurred, dimmed game, and the menu's floating window over it.

        The backdrop and dim fade in over PAUSE_FADE_SECONDS when the menu
        opens, and out again over the resumed game when it closes.
        """
        w, h = self.view.width(), self.view.height()
        level = self._pause_fade_level()
        backdrop = self.view._pause_backdrop
        painter.save()
        dim = 170
        if backdrop is not None:
            painter.setOpacity(level)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(QRect(0, 0, w, h), backdrop)
            painter.setOpacity(1.0)
            dim = 110
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, int(dim * level))))
        painter.drawRect(0, 0, w, h)
        window = self.view._pause_window
        if window is not None:
            window.clamp_to(w, h)
            window.draw(painter, focused=True)
        painter.restore()
