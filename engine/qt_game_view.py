import time
import os
import math
from collections import deque
import numpy as np
import ctypes
from typing import Optional
from PyQt5.QtWidgets import QOpenGLWidget, QApplication, QLineEdit
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, QEvent, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QFont, QCursor, QPen, QBrush, QKeySequence, QPixmap, QSurfaceFormat, QFontMetrics, QImage, QLinearGradient, QFontDatabase
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
import glm
from engine.camera import Camera
from editor.things import (
    Thing, Light, PlayerStart, Monster, Prop, Speaker,
    LogicGate, LogicRelay, LogicTimer, LevelChanger, Portal
)
from engine.player import Player

from .renderer_F   import Renderer_F
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
_RENDERER_CLASSES = {
    'Forward':  Renderer_F,
}


def register_renderer(name, cls):
    """Register a swappable renderer class under *name* (used by ``switch_renderer``).

    This is the plugin-facing seam for shipping a whole new renderer (e.g. a
    deferred one) without editing the engine: a plugin calls
    ``api.register_renderer("Deferred", DeferredRenderer)`` and it becomes an
    available render mode. *cls* must implement the renderer interface the
    viewport drives (``render_scene``, ``draw_models_instanced``,
    ``render_shadow_maps``, ``set_sprite_textures``, ``cleanup``, a
    ``lod_manager``, …). ``render_shadow_maps`` receives the dense
    ``(EntityTable, light_slots)`` state plus render config; it must consume
    ``RenderTable``/``EntityTable`` slots rather than authored
    Brush/Thing/Light collections. Returns True.
    """
    _RENDERER_CLASSES[str(name)] = cls
    return True


def available_renderers():
    """The names of all registered renderer modes."""
    return list(_RENDERER_CLASSES.keys())

from engine import brush_geometry
from editor import component_edit
from engine.threaded_game_state import ThreadedGameState, RenderState
from engine.renderer_core import restore_default_pixel_store
from engine.view_distance import ViewDistance
from engine.glasses import (
    DEFAULT_GLASSES, DEFAULT_SPRITE_KEY, GLASSES_STYLES, GLASSES_SUBFOLDER,
    glasses_sprite_key, normalize_glasses,
)
from engine.logic_thread import LogicThread
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.debug_console import DebugConsole, debug_log
from .sysmon import SysMon
from .floating_windows import CallbackWindow, WindowManager

# Pygame for gamepad support
import pygame

# System monitoring (pure Python, no external deps)
import ctypes
import os

# OpenGL GPU memory query constants
GL_GPU_MEM_INFO_TOTAL_AVAILABLE_MEM_NVX = 0x9048
GL_GPU_MEM_INFO_CURRENT_AVAILABLE_MEM_NVX = 0x9049
GL_GPU_MEM_INFO_DEDICATED_VIDMEM_NVX = 0x9047
GL_TEXTURE_FREE_MEMORY_ATI = 0x87FC
GL_RENDERBUFFER_FREE_MEMORY_ATI = 0x87FD

def perspective_projection(fov, aspect, near, far):
    if aspect == 0:
        return glm.mat4(1.0)
    return glm.perspective(glm.radians(fov), aspect, near, far)


class QtGameView(QOpenGLWidget):
    _logic_tick_fault_signal = pyqtSignal(str)
    def __init__(self, editor):
        super().__init__(editor)
        self._logic_tick_fault_signal.connect(self._handle_logic_tick_fault)

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        self.setFormat(fmt)

        self.editor = editor
        self.brush_display_mode = "Solid Lit"
        # Play-mode camera: "First Person" or "Overhead" (native top-down),
        # set from the editor's "Camera" dropdown.
        self.camera_mode = "First Person"
        # PERF: _is_overhead() is queried several times per rendered frame
        # (paintGL, sprite draw, HUD). Cache the normalised boolean and only
        # recompute when camera_mode changes — no per-frame string allocation.
        self._camera_mode_raw = None
        self._camera_mode_overhead = False
        # Overhead player sprite (drawn on the ground, facing the heading).
        self.overhead_sprite_enabled = True
        self.overhead_sprite_size = 128.0
        self.overhead_walk_fps = 6.0
        self.overhead_sprite_facing_offset = 0.0
        self._overhead_sprite_ctrl = None
        self._overhead_sprite_renderer = None
        self.show_triggers_as_solid = False
        self.camera = Camera()
        self.camera.pos = glm.vec3(0, 150, 400)
        self.debug_console_window = DebugConsole.get_instance()
        self.grid_size, self.world_size = 16, 2048
        self.grid_dirty = True
        self.culling_enabled = True
        self.show_sprites_in_play_mode = False
        # Cache keys for per-frame expensive rebuilds
        self._io_conn_cache       = None   # last _gather_io_connections result
        self._io_conn_scene_ver   = None   # (len(brushes), len(things)) when cache was built
        self.visibility_system = None
        self.show_visibility_debug = False
        self.grid_visible = True
        self.sysmon = SysMon(self)
        # SysMon-style floating tools share one lightweight window manager.
        self.floating_windows = WindowManager()
        self.pos_window = None


        self.sound_pool = {}
        self._init_sound_system()

        self.render_mode_names = {
            RENDER_MODE_LIT: "Lit",
            RENDER_MODE_UNLIT: "Unlit",
            RENDER_MODE_WIREFRAME: "Wireframe",
            RENDER_MODE_VERTEX: "Vertex"
        }

















        self.game_state = ThreadedGameState()
        self.logic_thread: Optional[LogicThread] = None
        self._thread_started = False

        self.face_mode_active = False
        self.hovered_face_info = None

        # --- Component editing (shares the main window's ComponentController) ---
        # Only the drag anchors are per-view; the mode, hover and component
        # selection live on the controller so the 2D views agree with this one.
        self.component_drag_origin = None   # QPoint, screen press position
        self.component_drag_axes = None     # (world axis for dx, for dy)
        self.component_drag_scale = 1.0     # world units per screen pixel
        self.component_drag_anchor = None   # world position of the grabbed part
        self.component_drag_kind = None
        # Face Mode (texturing) also lets a face be dragged: a press arms this,
        # a drag moves the face's plane, a release without movement textures it.
        self._face_mode_press = None

        self.mouselook_active = False
        self.last_mouse_pos = QPoint()







        self._init_hud_caches()

        self.texture_manager = {}
        self.sprite_textures = {}
        self.gun_hud_pixmaps = {}
        self.gun_flash_pixmaps = {}
        self.weapon_collect_pixmaps = {}   # item_type -> world/collectible QPixmap
        self.monster_debug_active = False
        self.show_spatial_grid = False
        self.renderer = None

        self.debug_shader = None
        self.debug_vao = None
        self.debug_vbo = None

        self.show_render_menu = False
        self.current_render_mode = RENDER_MODE_LIT

        self.play_mode = False
        self.player = None

        self.player2 = None
        self.splitscreen_mode = False
        # Player representation used by split-screen and portal views.
        self.show_glasses = self.editor.config.getboolean(
            'Display', 'show_glasses', fallback=True
        )
        # The glasses player 1 wears (Settings > Appearance); player 2
        # always wears the default pair.
        self.player1_glasses = normalize_glasses(self.editor.config.get(
            'Appearance', 'glasses', fallback=DEFAULT_GLASSES
        ))

        # PYGAME INIT (MUST happen before _init_sound_system)
        pygame.init()
        pygame.joystick.init()
        
        # Initialize pygame mixer BEFORE any sound loading
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            print(f"[Audio] pygame.mixer initialized: {pygame.mixer.get_init()}")
        except pygame.error as e:
            print(f"[Audio] pygame.mixer init failed: {e}")

        self.gamepad = None
        if pygame.joystick.get_count() > 0:
            self.gamepad = pygame.joystick.Joystick(0)
            self.gamepad.init()
            print(f"[Gamepad] Found: {self.gamepad.get_name()}")
        else:
            print("[Gamepad] No gamepad connected – using arrow keys for P2")

        # Timer to poll gamepad state regularly
        self.gamepad_timer = QTimer(self)
        self.gamepad_timer.timeout.connect(self._poll_gamepad)
        self.gamepad_timer.start(16)  # ~60 Hz

        # Store arrow key states for Player 2 when no gamepad
        self.p2_keys_pressed = set()

        # === NOW safe to load sounds ===
        self._init_sound_system()


        self._last_player_start_pos = [0, 0, 0]
        self._last_player_start_angle = 0

        self.fps = 0
        self.frame_count = 0
        self.last_time = time.perf_counter()
        self.last_fps_time = time.perf_counter()
        self.start_time = time.perf_counter()

        self._render_config = {
            "culling_enabled": True,
            "brush_display_mode": "Textured",
            "render_mode": 0,
            "show_triggers_as_solid": False,
            "show_caulk": True,
            "play_mode": False,
            "selected_object": None,
            "time": 0.0,
            "show_sprites_in_play_mode": False,
            "show_glasses": True,
            "player_glasses_positions": (),
            "player_glasses_sprites": (),
            "grid_visible": True,
        }

        self.is_dragging_gizmo = False
        self.gizmo_drag_axis = None
        self.gizmo_object_start_pos = None
        self.drag_start_on_axis = None
        self.terrain_sculpt_active = False
        self.terrain_sculpt_painting = False
        self.terrain_sculpt_mode = 'raise'
        self.terrain_sculpt_radius = 50.0
        self.terrain_sculpt_strength = 20.0
        self.terrain_sculpt_feather = 0.25
        self.projection_matrix = glm.mat4(1.0)
        self.view_matrix = glm.mat4(1.0)
        self._cached_aspect_ratio = 1.0
        #: An armed play-mode actor pick (see begin_actor_pick): ``{'on_pick':
        #: callable}``, or None. ``actor_pick_hover`` is the actor under the
        #: cursor while one is armed.
        self._actor_pick = None
        self.actor_pick_hover = None
        # The camera's draw distance and the fog that hides its far plane, in
        # one object shared with the renderer and the logic thread so the
        # editor spinbox and the r_* console commands take effect on the next
        # frame with nothing to rebuild. `cull_distance` stays as a plain
        # attribute for existing callers and mirrors view_distance.distance.
        self.view_distance = ViewDistance()
        self.cull_distance = self.view_distance.distance

        self._proj_ptr = None
        self._view_ptr = None

        self.console_overlay_active = False
        self._console_input = QLineEdit(self)
        self._console_input.setPlaceholderText("Enter command…   Esc to close")
        self._console_input.setFont(QFont("Consolas", 11))
        self._console_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(10, 10, 10, 220);
                color: #F08000;
                border: none;
                border-top: 2px solid #F08000;
                padding: 6px 10px;
                font-family: Consolas, monospace;
                font-size: 11pt;
            }
        """)
        self._console_input.returnPressed.connect(self._submit_console_command)
        self._console_input.installEventFilter(self)
        self._console_input.hide()

        self._play_mode_hint = ""
        self._play_mode_hint_timer = QTimer(self)
        self._play_mode_hint_timer.setSingleShot(True)
        self._play_mode_hint_timer.timeout.connect(self._clear_play_mode_hint)
        self._cached_hint_text = None
        self._cached_hint_width = 0

        self._muzzle_flash_counter = 0
        self._muzzle_flash_duration_frames = 3

        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)

        timer = QTimer(self)
        timer.setInterval(16)
        timer.timeout.connect(self.update_loop)
        timer.start()

        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(True)

    def _init_sound_system(self):
        """Preload sounds into pygame mixer cache."""
        if not self._ensure_pygame_mixer():
            print("[Audio] Sound system unavailable — mixer could not be initialized")
            return

        sound_dir = os.path.join(os.getcwd(), 'assets', 'sounds')
        if not os.path.exists(sound_dir):
            print("[Audio] Warning: assets/sounds directory not found.")
            return

        print("[Audio] Preloading sounds...")
        count = 0
        for f in os.listdir(sound_dir):
            if f.lower().endswith(('.wav', '.mp3', '.ogg')):
                full_path = os.path.join(sound_dir, f)
                self._load_sound_to_cache(f, full_path)
                count += 1
        print(f"[Audio] Preloaded {count} sound files.")

    #: Seconds between attempts to open the audio device after one failed.
    MIXER_RETRY_SECONDS = 10.0

    def _ensure_pygame_mixer(self) -> bool:
        """Initialize pygame mixer if it isn't already active.

        A failed attempt probes the audio stack for ~100 ms on the UI thread,
        and every sound request asks, so on a machine with no working device
        each gunshot used to stall a frame. The failure is remembered and the
        device retried at most every :data:`MIXER_RETRY_SECONDS`, so one
        plugged in later is still picked up.
        """
        if pygame.mixer.get_init():
            return True
        now = time.perf_counter()
        failed_at = getattr(self, '_mixer_failed_at', None)
        if failed_at is not None and now - failed_at < self.MIXER_RETRY_SECONDS:
            return False
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            print("[Audio] pygame.mixer late-initialized")
            self._mixer_failed_at = None
            return True
        except pygame.error as e:
            if failed_at is None:
                print(f"[Audio] pygame.mixer init failed: {e}")
            self._mixer_failed_at = now
            return False

    def _load_sound_to_cache(self, name, path):
        """Load a sound file into pygame mixer cache."""
        if not self._ensure_pygame_mixer():
            print(f"[Audio] Failed to load {name}: mixer not initialized")
            return False
        if name in self.sound_pool:
            return True
        try:
            sound = pygame.mixer.Sound(path)
            self.sound_pool[name] = sound
            return True
        except pygame.error as e:
            print(f"[Audio] Failed to load {name}: {e}")
            return False

    def _get_sound_instance(self, name):
        """Get a pygame Sound object by name. Loads on-demand if not cached."""
        clean_name = os.path.basename(name)
        
        # Already cached?
        if clean_name in self.sound_pool:
            return self.sound_pool[clean_name]
        # No audio device: nothing can load. Its failure was reported once
        # when the mixer was probed; do not probe the disk and log two more
        # lines for every sound the game asks for.
        if not self._ensure_pygame_mixer():
            return None
        
        # Try to load on-demand
        path = os.path.join(os.getcwd(), 'assets', 'sounds', clean_name)
        if os.path.exists(path):
            if self._load_sound_to_cache(clean_name, path):
                return self.sound_pool[clean_name]
        
        print(f"[Audio] Sound not found: {clean_name}")
        return None
       


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
        self._hud_font_families = {}
        self._hud_font_available = {}
        for style, filename in _HUD_FONT_FILES.items():
            family = _HUD_FONT_FALLBACKS[style]
            available = False
            try:
                path = os.path.join(fonts_dir, filename)
                if os.path.isfile(path):
                    font_id = QFontDatabase.addApplicationFont(path)
                    if font_id >= 0:
                        families = QFontDatabase.applicationFontFamilies(font_id)
                        if families:
                            family = families[0]
                            available = True
            except OSError:
                pass
            self._hud_font_families[style] = family
            self._hud_font_available[style] = available

    def _resolve_hud_font_family(self, selector):
        """Resolve a known font or a safe font filename under assets/fonts."""
        selector = str(selector or "").strip().strip('"').strip("'")
        if not selector:
            return None
        needle = selector.lower()
        for style, family in self._hud_font_families.items():
            filename = _HUD_FONT_FILES[style]
            stem = os.path.splitext(filename)[0]
            if needle in (filename.lower(), stem.lower(), family.lower()):
                return family

        basename = os.path.basename(selector)
        if basename != selector or not basename.lower().endswith((".ttf", ".otf")):
            return None
        path = os.path.join(os.getcwd(), "assets", "fonts", basename)
        try:
            if os.path.isfile(path):
                font_id = QFontDatabase.addApplicationFont(path)
                if font_id >= 0:
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        return families[0]
        except OSError:
            pass
        return None

    def _current_hud_font_family(self):
        return self._hud_font_override or self._hud_font_families.get(
            self._hud_style, self._hud_font_families.get(1, "Rushford Clean")
        )

    def _refresh_hud_status_font(self):
        self._hud_status_font = QFont(self._current_hud_font_family(), 56)

    def _reload_hud_settings(self):
        """Reload persistent HUD settings and reset runtime-only HUD style."""
        config = self.editor.config
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
        self._hud_style = 1
        self._hud_opacity = opacity
        self._hud_fade_enabled = fade
        self._hud_font_override = None
        self._hud_runtime_visible = None

        self._refresh_hud_status_font()
        logic_thread = self.logic_thread
        if logic_thread is not None:
            logic_thread.render_runtime.set_hud_fade_enabled(self._hud_fade_enabled)

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

        self._hud_style = style
        self._hud_font_override = font_override
        self._hud_runtime_visible = style != 0
        self._refresh_hud_status_font()
        self.update()
        return True

    def set_hud_opacity(self, opacity):
        """Apply HUD opacity immediately; valid range is 0..100 inclusive."""
        try:
            opacity = float(opacity)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(opacity) or not 0.0 <= opacity <= 100.0:
            return False
        self._hud_opacity = opacity
        self.update()
        return True

    def set_hud_fade_enabled(self, enabled):
        """Enable/disable the damage-driven health HUD fade immediately."""
        self._hud_fade_enabled = bool(enabled)
        logic_thread = self.logic_thread
        if logic_thread is not None:
            logic_thread.render_runtime.set_hud_fade_enabled(self._hud_fade_enabled)
        self.update()
        return True

    def _load_health_font(self):
        """Return the fixed Rushford font used by message-3."""
        return QFont(self._hud_font_families.get(1, "Rushford Clean"), 56)

    def _init_hud_caches(self):
        self._hud_font = QFont("Arial", 11)
        self._hud_font.setBold(True)
        self._hud_msg_font = QFont("Arial", 14)
        self._hud_msg_font.setBold(True)
        self._fps_font = QFont("Arial", 10)
        self._sprites_font = QFont("Arial", 10)
        self._sprites_font.setBold(True)
        self._death_title_font = QFont("Arial", 64, QFont.Bold)
        self._death_sub_font = QFont("Arial", 18)
        self._load_hud_font_families()
        self._hud_style = 1
        self._hud_opacity = 100.0
        self._hud_fade_enabled = True
        self._hud_font_override = None
        self._hud_runtime_visible = None
        self._hud_health_font = self._load_health_font()
        self._hud_status_font = QFont(
            self._hud_font_families.get(1, "Rushford Clean"), 56
        )
        self._reload_hud_settings()
        self._face_mode_font_top = QFont("Arial", 14, QFont.Bold)
        self._face_mode_font_bot = QFont("Arial", 10, QFont.Bold)

        self._hud_health_orange = QColor(179, 75, 0)
        self._hud_ammo_green = QColor("#0b4519")
        self._hud_count_shadow_pen = QPen(QColor(0, 0, 0, 85))
        self._hud_white_pen = QPen(QColor(255, 255, 255))
        self._hud_black_pen = QPen(QColor(0, 0, 0))
        self._hud_grey_pen = QPen(QColor(200, 200, 200))
        self._hud_shadow_pen = QPen(QColor(0, 0, 0))
        self._hud_pink_pen = QPen(QColor(255, 105, 180))
        self._hud_fps_bg_brush = QBrush(QColor(0, 0, 0, 128))
        self._hud_sprites_bg_brush = QBrush(QColor(0, 0, 0, 128))
        self._face_mode_box_brush = QBrush(QColor(0, 0, 0, 180))
        self._face_mode_pen = QPen(QColor(255, 255, 255))

        self._face_mode_top_width = QFontMetrics(self._face_mode_font_top).horizontalAdvance(
            "Select a FACE for texturing")
        self._face_mode_bot_width = QFontMetrics(self._face_mode_font_bot).horizontalAdvance(
            "Press ESC to cancel")
        self._cached_death_title_width = QFontMetrics(self._death_title_font).horizontalAdvance(
            "DIED")
        self._cached_death_sub_width = QFontMetrics(self._death_sub_font).horizontalAdvance(
            "Press Escape to return to the editor")

        self._cached_hud_message = None
        self._cached_hud_message_width = 0
        self._view_message_text = ""
        self._view_message_started_at = 0.0
        self._view_message_width = 0
        self._view_message_queue = deque()
        self._view_message2_text = ""
        self._view_message2_started_at = 0.0
        self._view_message2_width = 0
        self._view_message2_queue = deque()
        self._view_message3_text = ""
        self._view_message3_started_at = 0.0
        self._view_message3_width = 0
        self._view_message3_queue = deque()
        self._cached_gun_hud = {}
        self._cached_weapon_collect = {}   # (item_type, size) -> scaled QPixmap
        self._cached_key_pixmaps = {}
        self._cached_key_size = 100
        self._cached_prompt_key = None
        self._cached_prompt_key_pixmap = None
        self._cached_prompt_key_loaded = False
        self._cached_prompt_key_size = 64

        self._key_fallback_cache = {
            'blue_key':   (QColor(50, 100, 200), QPen(QColor(40, 80, 160), 2), QBrush(QColor(50, 100, 200))),
            'red_key':    (QColor(200, 50, 50),   QPen(QColor(160, 40, 40), 2),   QBrush(QColor(200, 50, 50))),
            'yellow_key': (QColor(200, 200, 50),  QPen(QColor(160, 160, 40), 2),  QBrush(QColor(200, 200, 50))),
            'green_key':  (QColor(50, 200, 50),   QPen(QColor(40, 160, 40), 2),   QBrush(QColor(50, 200, 50))),
        }
        self._key_fallback_default = (QColor(150, 150, 150), QPen(QColor(120, 120, 120), 2), QBrush(QColor(150, 150, 150)))

    def _poll_gamepad(self):
        """Read gamepad state and send to game_state for Player 2."""
        if not self.gamepad:
            return
        pygame.event.pump()  # Update joystick state

        # Axes: 0=left X, 1=left Y, 2=right X, 3=right Y
        move_x = self.gamepad.get_axis(0)
        move_z = -self.gamepad.get_axis(1)   # Invert Y
        look_dx = self.gamepad.get_axis(2)   # Right stick X
        look_dy = -self.gamepad.get_axis(3)  # Right stick Y (inverted)

        DEAD = 0.15
        move_x = move_x if abs(move_x) > DEAD else 0.0
        move_z = move_z if abs(move_z) > DEAD else 0.0
        look_dx = look_dx if abs(look_dx) > DEAD else 0.0
        look_dy = look_dy if abs(look_dy) > DEAD else 0.0

        jump = self.gamepad.get_button(0)   # A button
        crouch = self.gamepad.get_button(1) # B button (optional)

        self.game_state.set_p2_input(move_x, move_z, look_dx, look_dy, jump, crouch)


    def _start_view_message(self, text: str):
        """Start displaying one transient message-1 immediately."""
        self._view_message_text = text
        self._view_message_started_at = time.perf_counter()
        self._view_message_width = QFontMetrics(
            self._hud_msg_font
        ).horizontalAdvance(text)

    def show_view_message(self, text: str):
        """Show a message-1, queueing it behind the current message-1."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self._view_message_text:
            elapsed = time.perf_counter() - self._view_message_started_at
            if elapsed < 7.0 or self._view_message_queue:
                self._view_message_queue.append(text)
                self.update()
                return

        self._start_view_message(text)
        self.update()

    def _start_view_message2(self, text: str):
        """Start displaying one transient message-2 immediately."""
        self._view_message2_text = text
        self._view_message2_started_at = time.perf_counter()
        self._view_message2_width = QFontMetrics(
            self._hud_msg_font
        ).horizontalAdvance(text)

    def _start_view_message3(self, text: str):
        """Start displaying one transient Rushford-font message-3 immediately."""
        self._view_message3_text = text
        self._view_message3_started_at = time.perf_counter()
        self._view_message3_width = QFontMetrics(
            self._hud_health_font
        ).horizontalAdvance(text)

    def show_view_message2(self, text: str):
        """Show a message-2, queueing it behind the current message-2."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self._view_message2_text:
            elapsed = time.perf_counter() - self._view_message2_started_at
            if elapsed < 7.0 or self._view_message2_queue:
                self._view_message2_queue.append(text)
                self.update()
                return

        self._start_view_message2(text)
        self.update()

    def show_view_message3(self, text):
        """Show a Rushford-font message-3, queueing it behind the current message-3."""
        text = str(text).strip()[:50]
        if not text:
            return

        if self._view_message3_text:
            elapsed = time.perf_counter() - self._view_message3_started_at
            if elapsed < 7.0 or self._view_message3_queue:
                self._view_message3_queue.append(text)
                self.update()
                return

        self._start_view_message3(text)
        self.update()

    def _draw_queued_view_message(
        self, painter, viewport_width, viewport_height,
        text, started_at, width, queue, start_message,
        stack_slot=0, message_font=None, fade_duration=1.0,
    ):
        """Draw one queued transient message in the shared near-bottom stack."""
        if not text:
            return text, started_at, width

        elapsed = time.perf_counter() - started_at
        font = message_font or self._hud_msg_font
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
            QFontMetrics(self._hud_msg_font).height(),
            QFontMetrics(self._hud_health_font).height(),
        )
        row_height = max_stack_height + 6
        baseline = viewport_height - 20 - metrics.descent() - (row_height * stack_slot)
        cx = viewport_width // 2 + int(viewport_width * 0.10)
        text_x = cx - width // 2

        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.setFont(font)
        painter.setPen(self._hud_shadow_pen)
        painter.drawText(text_x + 2, baseline + 2, text)
        painter.setPen(self._hud_grey_pen)
        painter.drawText(text_x, baseline, text)
        painter.restore()
        return text, started_at, width

    def _draw_view_message(self, painter, viewport_width, viewport_height):
        """Draw message-1 at the top of the shared near-bottom message stack."""
        self._view_message_text, self._view_message_started_at, self._view_message_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self._view_message_text,
                self._view_message_started_at,
                self._view_message_width,
                self._view_message_queue,
                self._start_view_message,
                stack_slot=2,
            )
        )

    def _draw_view_message2(self, painter, viewport_width, viewport_height):
        """Draw message-2 in the middle of the shared near-bottom stack."""
        self._view_message2_text, self._view_message2_started_at, self._view_message2_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self._view_message2_text,
                self._view_message2_started_at,
                self._view_message2_width,
                self._view_message2_queue,
                self._start_view_message2,
                stack_slot=1,
            )
        )

    def _draw_view_message3(self, painter, viewport_width, viewport_height):
        """Draw message-3 in Rushford at the bottom of the shared message stack."""
        self._view_message3_text, self._view_message3_started_at, self._view_message3_width = (
            self._draw_queued_view_message(
                painter, viewport_width, viewport_height,
                self._view_message3_text,
                self._view_message3_started_at,
                self._view_message3_width,
                self._view_message3_queue,
                self._start_view_message3,
                message_font=self._hud_health_font,
                stack_slot=0,
                fade_duration=1.25,
            )
        )

    def _clear_play_mode_hint(self):
        self._play_mode_hint = ""
        self._cached_hint_text = None
        self.update()

    def set_camera_mode(self, mode):
        """Select the play-mode camera ('First Person' or 'Overhead').

        Stored on the view and pushed to the logic thread, which builds the
        overhead view matrix and frustum natively (see LogicThread). Takes effect
        immediately in play mode; otherwise it applies on the next play session.
        """
        self.camera_mode = str(mode)
        lt = self.logic_thread
        if lt is not None:
            lt.camera.set_camera_mode(self.camera_mode)
        self.update()

    def _is_overhead(self) -> bool:
        # PERF: cached — recompute only when camera_mode changes.
        cm = self.camera_mode
        if cm != self._camera_mode_raw:
            self._camera_mode_raw = cm
            self._camera_mode_overhead = str(cm).strip().lower() in (
                "overhead", "top-down", "topdown")
        return self._camera_mode_overhead

    def _draw_overhead_sprite(self, render_state):
        """Draw the player sprite on the ground in overhead play mode.

        Runs on the render thread inside the live GL context. Fed from the
        published render state (player ground position + facing); the renderer is
        created lazily and self-disables on any missing asset or GL error, so a
        missing sprite never breaks the frame. No-op outside overhead play mode,
        during a cinematic, or when disabled.
        """
        _lt = self.logic_thread
        _lt_overhead = bool(_lt.camera.is_overhead()) if _lt is not None else False
        if not (self.play_mode and self.overhead_sprite_enabled
                and (self._is_overhead() or _lt_overhead)):
            return
        if render_state is None:
            return
        lt = self.logic_thread
        if lt is not None and lt.cutscene_runtime.state:
            return
        # Suppress the ground sprite mid-tween so it doesn't pop in/out while the
        # camera swoops between first-person and overhead.
        if render_state.camera_transition_active:
            return
        try:
            from engine.overhead_sprite import SpriteController, OverheadSpriteRenderer
        except Exception:
            return
        if self._overhead_sprite_ctrl is None:
            self._overhead_sprite_ctrl = SpriteController(walk_fps=float(self.overhead_walk_fps))
        if self._overhead_sprite_renderer is None:
            self._overhead_sprite_renderer = OverheadSpriteRenderer(
                size=float(self.overhead_sprite_size),
                facing_offset_deg=float(self.overhead_sprite_facing_offset))

        pos = render_state.player_pos
        if pos is None:
            return
        try:
            gpos = (float(pos.x), float(pos.y), float(pos.z))
        except AttributeError:
            gpos = (float(pos[0]), float(pos[1]), float(pos[2]))
        angle = float(render_state.player_angle)
        armed = bool(render_state.active_weapon)
        shooting = bool(render_state.muzzle_flash_active)
        self._overhead_sprite_ctrl.update(gpos, angle, time.perf_counter(),
                                          armed=armed, shooting=shooting)
        self._overhead_sprite_renderer.draw(
            self.projection_matrix, self.view_matrix, gpos,
            self._overhead_sprite_ctrl.facing, self._overhead_sprite_ctrl.frame())


    def initializeGL(self):
        gl.glClearColor(*self.view_distance.fog_color, 1.0)
        config = self.editor.config
        self._renderer_mode = 'Forward'
        self.renderer = Renderer_F(self.load_texture, self.grid_size, self.world_size, config)
        self.set_cull_distance(self.cull_distance)
        self._preload_assets()
        self.load_all_sprite_textures()
        if hasattr(self.editor, 'state') and hasattr(self.editor.state, 'brushes'):
            self.preload_level_textures()
        self._start_logic_thread()
        self._init_debug_resources()
        if hasattr(self, 'debug_console_window'):
            QTimer.singleShot(1000, self.debug_console_window.show)

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
            self.debug_shader = compileProgram(
                compileShader(vs_src, gl.GL_VERTEX_SHADER),
                compileShader(fs_src, gl.GL_FRAGMENT_SHADER),
                validate=False
            )
            # PERF: cache uniform locations once instead of querying them
            # via glGetUniformLocation every frame in the debug draw paths.
            self._debug_proj_loc = gl.glGetUniformLocation(self.debug_shader, 'projection')
            self._debug_view_loc = gl.glGetUniformLocation(self.debug_shader, 'view')
            self._debug_color_loc = gl.glGetUniformLocation(self.debug_shader, 'color')
            self.debug_vao = gl.glGenVertexArrays(1)
            self.debug_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self.debug_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.debug_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 1024 * 1024, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glBindVertexArray(0)
        except Exception as e:
            print(f"Debug Renderer Init Failed: {e}")

    def _preload_assets(self):
        tex_dir = os.path.join('assets', 'textures')
        if os.path.exists(tex_dir):
            for f in os.listdir(tex_dir):
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.tga')):
                    self.renderer.load_texture(f, 'textures')
        terrain_dir = os.path.join('assets', 'textures', 'terrain')
        if os.path.exists(terrain_dir):
            for f in os.listdir(terrain_dir):
                if f.lower().endswith(('.jpg', '.png')):
                    self.renderer.load_texture(os.path.join('terrain', f), 'textures')

    def _handle_logic_tick_fault(self, message):
        """Run fatal-tick play teardown on QtGameView's GUI thread."""
        if self.play_mode:
            self.toggle_play_mode(None, None)
        if message:
            self._play_mode_hint = "Logic tick failed; Play Mode stopped"

    def _start_logic_thread(self):
        if self._thread_started:
            return
        self.logic_thread = LogicThread(self.game_state, self.editor.state, self.visibility_system)
        initial_view_distance = self.view_distance
        render_view_distance = self.logic_thread.render_runtime.view_distance
        render_view_distance.distance = initial_view_distance.distance
        render_view_distance.fog_enabled = initial_view_distance.fog_enabled
        render_view_distance.fog_start = initial_view_distance.fog_start
        render_view_distance.fog_end = initial_view_distance.fog_end
        render_view_distance.fog_density = initial_view_distance.fog_density
        render_view_distance.fog_color = initial_view_distance.fog_color
        render_view_distance.ambient = initial_view_distance.ambient
        render_view_distance.limit = initial_view_distance.limit
        self.view_distance = render_view_distance
        self.logic_thread.set_gui_fault_teardown(self._logic_tick_fault_signal.emit)
        if self.logic_thread is not None:
            self.logic_thread.render_runtime.set_hud_fade_enabled(self._hud_fade_enabled)
        self.logic_thread.camera.set_editor_camera(self.camera.pos, self.camera.yaw, self.camera.pitch, self.camera.fov)
        self.logic_thread.camera.set_camera_mode(self.camera_mode)
        self.logic_thread.session_runtime.apply_play_mode(False)
        self._sync_view_distance()
        self.logic_thread.start()
        self._thread_started = True

    def _stop_logic_thread(self):
        if self.logic_thread:
            self.logic_thread.stop()
            self.logic_thread.join(timeout=1.0)
            self.logic_thread = None
            self._thread_started = False

    def closeEvent(self, event):
        self._stop_logic_thread()
        super().closeEvent(event)

    def preload_level_textures(self):
        if self.renderer:
            self.renderer.preload_level_textures(self.editor.state.brushes)

    def update_grid(self):
        self.grid_dirty = True

    def resizeGL(self, width, height):
        super().resizeGL(width, height)
        if height > 0:
            vp_w = (width // 2) if self.splitscreen_mode else width
            self._cached_aspect_ratio = vp_w / height
        else:
            self._cached_aspect_ratio = 1.0
        if self.logic_thread:
            self.logic_thread.camera.set_frustum_aspect(self._cached_aspect_ratio)
        if self.console_overlay_active:
            self._console_input.setGeometry(0, height - 36, width, 36)

    def update_loop(self):
        current_time = time.perf_counter()
        delta = current_time - self.last_time
        self.last_time = current_time
        self.frame_count += 1
        fps_elapsed = current_time - self.last_fps_time
        if fps_elapsed > 1.0:
            self.fps = self.frame_count / fps_elapsed
            self.frame_count = 0
            self.last_fps_time = current_time
        self.sysmon.record_frame_time(delta * 1000.0)
        self._process_sound_queue()
        self._process_console_command_queue()

        if self.logic_thread:
            keys = set() if self.console_overlay_active else self.editor.keys_pressed
            self.game_state.set_keys(keys)
            # Update Player 2 input from arrow keys (if no gamepad)
            self._update_p2_keyboard_input()
            # Paint only a frame that is new. Repainting the one already on
            # screen draws the same image again, and -- publication being
            # double-buffered -- borrows the published frame for the whole
            # paint, so back-to-back repaints leave the logic thread no gap
            # to publish the next one in. Input that changes only editor
            # overlays asks Qt for a paint on its own (``update()``).
            if self.game_state.try_swap():
                self.update()
                if self.play_mode:
                    self.editor.update_views()
        else:
            self.repaint()

    @staticmethod
    def _sound_radius_gain(distance, radius):
        """Linear falloff: full volume at the source, zero at the radius."""
        try:
            distance = max(0.0, float(distance))
            radius = max(0.0, float(radius))
        except (TypeError, ValueError):
            return 0.0
        if radius <= 0.0:
            return 0.0
        if distance >= radius:
            return 0.0
        return 1.0 - (distance / radius)

    def _spatial_sound_mix(self, position, radius=512.0, global_sound=False):
        """Return (gain, left, right) for a world-space sound source."""
        if global_sound or position is None:
            return 1.0, 1.0, 1.0
        try:
            source = np.asarray(position, dtype=np.float32)
            listener = np.asarray((
                float(self.camera.pos.x), float(self.camera.pos.y),
                float(self.camera.pos.z),
            ), dtype=np.float32)
            delta = source - listener
            distance = float(np.linalg.norm(delta))
        except (TypeError, ValueError, AttributeError):
            return 1.0, 1.0, 1.0

        gain = self._sound_radius_gain(distance, radius)
        if gain <= 0.0:
            return 0.0, 1.0, 1.0

        try:
            front = self.camera.get_front_vector()
            right_vec = glm.normalize(
                glm.cross(front, glm.vec3(0, 1, 0))
            )
            horizontal = glm.vec3(float(delta[0]), 0.0, float(delta[2]))
            if glm.length(horizontal) > 0.0001:
                horizontal = glm.normalize(horizontal)
                pan = float(glm.dot(horizontal, right_vec))
            else:
                pan = 0.0
        except Exception:
            pan = 0.0

        pan = max(-1.0, min(1.0, pan))
        angle = (pan + 1.0) * (math.pi / 4.0)
        return gain, math.cos(angle), math.sin(angle)

    def stop_all_sounds(self):
        """Immediately stop every mixer channel and cancel queued audio."""
        try:
            if pygame.mixer.get_init():
                pygame.mixer.stop()
        except (pygame.error, AttributeError):
            pass

        self.game_state.clear_sounds()

        speaker_channels = getattr(self, '_speaker_channels', None)
        if speaker_channels is not None:
            speaker_channels.clear()
        speaker_mix = getattr(self, '_speaker_mix', None)
        if speaker_mix is not None:
            speaker_mix.clear()

    def _process_sound_queue(self):
        """Drain queued sound requests and keep active speaker channels mixed.

        Speaker requests carry an action ('play'/'stop'), looping,
        position/radius for spatial speakers, and entity_id.
        Looping speakers remain tracked so their attenuation follows the
        listener as the player moves.
        """
        speaker_channels = getattr(self, "_speaker_channels", None)
        if speaker_channels is None:
            speaker_channels = self._speaker_channels = {}
        speaker_mix = getattr(self, "_speaker_mix", None)
        if speaker_mix is None:
            speaker_mix = self._speaker_mix = {}

        def apply_mix(channel, meta):
            gain, left, right = self._spatial_sound_mix(
                meta.get('position'),
                meta.get('radius', 512.0),
                bool(meta.get('global', False)),
            )
            volume = max(0.0, min(1.0, float(meta.get('volume', 1.0))))
            if meta.get('position') is not None and not meta.get('global', False):
                channel.set_volume(
                    volume * gain * left,
                    volume * gain * right,
                )
            else:
                channel.set_volume(volume)

        for request in self.game_state.consume_sounds():
            action = request.get('action', 'play')
            entity_id = request.get('entity_id')

            if action == 'stop':
                channel = speaker_channels.pop(entity_id, None)
                speaker_mix.pop(entity_id, None)
                if channel is not None:
                    try:
                        channel.stop()
                    except Exception as exc:
                        print(f"[QtGameView] speaker stop failed: {exc}")
                continue

            sound_file = request.get('file')
            volume = request.get('volume', 1.0)
            if not sound_file:
                continue

            sound = self._get_sound_instance(sound_file)
            if not sound:
                continue

            loops = -1 if request.get('looping') else 0

            # If this speaker is already looping, stop the old channel first so
            # a re-trigger does not stack a second copy on top of itself.
            if entity_id is not None:
                prev = speaker_channels.pop(entity_id, None)
                speaker_mix.pop(entity_id, None)
                if prev is not None:
                    try:
                        prev.stop()
                    except Exception as exc:
                        print(f"[QtGameView] speaker restart stop failed: {exc}")

            channel = sound.play(loops=loops)
            if channel:
                meta = {
                    'position': request.get('position'),
                    'radius': request.get('radius', 512.0),
                    'global': bool(request.get('global', False)),
                    'volume': volume,
                }
                apply_mix(channel, meta)

                # Track only entity-owned looping channels. One-shots get the
                # correct spatial mix at their start position.
                if entity_id is not None and loops != 0:
                    speaker_channels[entity_id] = channel
                    speaker_mix[entity_id] = meta

        # Re-mix active looping speakers every render frame so walking toward
        # or away from a speaker changes volume without re-triggering playback.
        for entity_id, channel in list(speaker_channels.items()):
            meta = speaker_mix.get(entity_id)
            if meta is not None:
                apply_mix(channel, meta)


    def _process_console_command_queue(self):
        """Run any console commands queued by the I/O system on the UI thread.

        The logic thread enqueues command strings (e.g. a trigger brush firing a
        logic_command entity's RunCommand input). They must execute here, on the
        main thread, because console commands touch Qt widgets and editor state.
        """
        commands = self.game_state.consume_console_commands()
        if not commands:
            return
        handler = getattr(self.editor, 'console_handler', None)
        if handler is None:
            return
        for cmd in commands:
            try:
                handler.handle_command(cmd, from_map=True)
            except Exception as exc:
                print(f"[QtGameView] console command '{cmd}' failed: {exc}")

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
        def find_pos_by_name(name):
            for b in self.editor.state.brushes:
                if b.get('name') == name:
                    return b['pos']
            for t in self.editor.state.things:
                t_name = getattr(t, 'name', t.properties.get('name', ''))
                if t_name == name:
                    return t.pos
            return None
        lines = []
        if io_available:
            for brush in self.editor.state.brushes:
                for conn in get_connections(brush):
                    dst = find_pos_by_name(conn.target_name)
                    if dst:
                        is_logic = brush.get('is_trigger') or brush.get('is_mover') or brush.get('is_door')
                        color = COLOR_LOGIC if is_logic else COLOR_IO
                        lines.append({'src': brush['pos'], 'dst': dst, 'color': color})
            for thing in self.editor.state.things:
                for conn in get_connections(thing):
                    dst = find_pos_by_name(conn.target_name)
                    if dst:
                        is_logic = thing.properties.get('type') == 'logic_gate'
                        color = COLOR_LOGIC if is_logic else COLOR_IO
                        lines.append({'src': thing.pos, 'dst': dst, 'color': color})
        node_lookup = {}
        if PathNode is not None:
            for t in self.editor.state.things:
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
            for t in self.editor.state.things:
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
            for brush in self.editor.state.brushes:
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

    # =========================================================================
    # POST-EFFECT RENDERING METHODS
    # =========================================================================

    def _render_bullet_marks(self, marks, proj_matrix, view_matrix):
        if not marks or 'simple' not in self.renderer.shaders:
            return
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        shader = self.renderer.shaders['simple']
        uniforms = self.renderer.uniforms['simple']
        gl.glUseProgram(shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.renderer.vaos['cube'])
        for mark in marks:
            pos = mark['pos']
            alpha = mark['alpha']
            gl.glUniform3f(uniforms['color'], 0.0, 0.0, 0.0)
            mat = glm.translate(glm.mat4(1.0), glm.vec3(pos[0], pos[1], pos[2]))
            mat = glm.scale(mat, glm.vec3(2.0, 2.0, 2.0))
            gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        gl.glBindVertexArray(0)
        gl.glDisable(gl.GL_BLEND)

    def _render_player_glasses(self, positions, proj_matrix, view_matrix):
        """Draw one or more player bodies as glasses billboards."""
        if not self.show_glasses:
            return
        if not positions or not self.renderer:
            return
        self.renderer.draw_player_glasses(
            proj_matrix,
            view_matrix,
            positions,
            width=40.0,
            height=18.0,
            sprites=self._render_config.get("player_glasses_sprites", ()),
        )
        # render_scene leaves depth testing disabled; restore that state after
        # this explicit post-scene billboard pass.
        gl.glDisable(gl.GL_DEPTH_TEST)

    def _render_projectiles(self, projectiles, proj_matrix, view_matrix):
        if not len(projectiles) or 'sprite_instanced' not in self.renderer.shaders:
            return
        tex_id = (self.sprite_textures.get('projectile') or
                  self.sprite_textures.get('Monster'))
        if not tex_id:
            return
        from engine.monster_constants import MONSTER_PROJECTILE_SPRITE_SIZE
        # Every projectile shares one texture and size, so the lot is one
        # instanced draw of the published position array.
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        self.renderer.draw_billboards_instanced(
            proj_matrix, view_matrix, projectiles, MONSTER_PROJECTILE_SPRITE_SIZE,
            tex_id)
        gl.glDisable(gl.GL_BLEND)

    def _render_monster_debug_rays(self, rays, proj_matrix, view_matrix):
        if not rays or not self.debug_shader:
            return
        gl.glUseProgram(self.debug_shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(self._debug_proj_loc, 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(self._debug_view_loc, 1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.debug_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.debug_vbo)

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
            gl.glUniform3f(self._debug_color_loc, *color)
            data = np.array(pts, dtype=np.float32)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
            gl.glDrawArrays(gl.GL_LINES, 0, len(pts) // 3)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    def _render_spatial_grid(self, proj_matrix, view_matrix):
        if not self.debug_shader or not self.logic_thread:
            return
        grid = self.logic_thread.session_runtime.spatial_grid
        if grid is None:
            return
        gl.glUseProgram(self.debug_shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(self._debug_proj_loc, 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(self._debug_view_loc, 1, gl.GL_FALSE, view_ptr)
        gl.glUniform3f(self._debug_color_loc, 0.0, 0.8, 1.0)
        gl.glBindVertexArray(self.debug_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.debug_vbo)
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

    def paintGL(self):
        if not self.renderer or getattr(self.renderer, '_shader_init_failed', False):
            return
        started = time.perf_counter()
        render_state: Optional[RenderState] = None
        if self.logic_thread:
            render_state = self.game_state.get_render_state()
        if render_state is None:
            return
        try:
            self._paint_frame(render_state)
        finally:
            # The render state is a borrowed snapshot, and publication waits
            # while it is borrowed: release it the moment this synchronous
            # paint is done, even if the paint raised.
            if render_state is not None:
                self.game_state.release_render_state(render_state)
            #: CPU milliseconds the last paint took, for Debug Tables.
            self.paint_ms = (time.perf_counter() - started) * 1000.0

        if self._muzzle_flash_counter > 0:
            self._muzzle_flash_counter -= 1

    def show_pos_window(self):
        """Open or raise the live camera-position floating window."""
        if self.pos_window is not None and self.pos_window.active:
            self.floating_windows.raise_(self.pos_window)
            self.update()
            return

        self.pos_window = CallbackWindow(
            key="pos",
            title="Pos",
            draw_fn=self._draw_pos_window,
            width=230,
            body_height=92,
            x=max(20, self.width() - 250),
            y=20,
            on_close_cb=self._close_pos_window,
        )
        self.floating_windows.add(self.pos_window)
        self.update()

    def _close_pos_window(self):
        self.pos_window = None
        self.update()

    def _draw_pos_window(self, painter, x, y, w, h):
        """Draw only the current camera coordinates inside the Pos window."""
        p = self.camera.pos
        values = (
            ("X", float(p.x)),
            ("Y", float(p.y)),
            ("Z", float(p.z)),
        )
        font = QFont("Arial", 11)
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

    def _paint_frame(self, render_state):
        """Draw one frame from the canonical published render state."""
        if render_state is None:
            return
        # Keep all direct OpenGL rendering inside Qt's native-painting boundary.
        # QPainter owns the GL state again before any HUD/overlay drawing.
        painter = QPainter(self)
        painter.beginNativePainting()

        if render_state:
            self._cached_health = render_state.player_health
            self._cached_max_health = render_state.player_max_health
            self._cached_player_ammo = render_state.player_ammo
            self._cached_shot_ready = render_state.shot_ready
            self._cached_active_weapon = render_state.active_weapon
            self._cached_hud_message = render_state.hud_message
            self._cached_collected_keys = render_state.collected_keys
            if render_state.muzzle_flash_active:
                self._muzzle_flash_counter = self._muzzle_flash_duration_frames
            self._cached_muzzle_flash = self._muzzle_flash_counter > 0
            self._cached_player_dead = render_state.player_dead
            self._cached_monster_debug = render_state.monster_debug_active
            self._cached_bullet_marks = list(render_state.bullet_marks)
            self._cached_projectiles = np.array(render_state.projectiles, dtype=np.float32).reshape(-1, 3)
            self._cached_monster_rays = list(render_state.monster_debug_rays)
            self._cached_level_complete_ui = render_state.level_complete_ui
            self._cached_underwater = render_state.player_underwater
            self._cached_underwater_tint = render_state.underwater_tint
            self._cached_p2_underwater = render_state.player2_underwater
        if self.grid_dirty:
            self.renderer.update_grid_buffers(self.world_size, self.grid_size)
            self.grid_dirty = False
        self.view_matrix = render_state.camera_view_matrix
        if render_state.is_play_mode:
            camera_pos = render_state.player_pos
        else:
            camera_pos = render_state.editor_camera_pos
        if not self.play_mode:
            self.camera.pos = glm.vec3(render_state.editor_camera_pos)
            self.camera.yaw = render_state.editor_camera_yaw
            self.camera.pitch = render_state.editor_camera_pitch
            self.camera.fov = render_state.editor_camera_fov
        else:
            self.camera.pos = glm.vec3(render_state.player_pos)
            self.camera.yaw = 90.0 - math.degrees(render_state.player_angle)
            self.camera.pitch = math.degrees(render_state.player_pitch)
        # In overhead play mode the camera is lifted far above the scene, so a
        # 0.1 near plane wastes almost all depth precision at ground level and
        # coplanar surfaces z-fight ("flicker"). Nothing sits within a fraction
        # of the camera height of the overhead eye, so pull the near plane out to
        # restore precision. First-person keeps the stock 0.1 near plane.
        _near = 0.1
        if self.play_mode and self._is_overhead():
            _lt = self.logic_thread
            _oh = float(_lt.camera.effective_overhead_height() or _lt.camera.overhead_height or 800.0)
            _near = max(1.0, _oh * 0.1)
        # The far plane IS the view distance -- that is what makes "nothing is
        # drawn past it" true of a fragment and not just of a whole object. The
        # broad-phase cull drops objects by the distance to their centre, so a
        # large brush straddling the boundary survives it and is clipped here
        # instead, by which point the fog has already taken it to full opacity.
        _far = max(self.view_distance.far_plane, _near + 1.0)
        # Clear to the fog colour so what geometry dissolves into and what lies
        # beyond the clip are the same pixel, leaving no seam at the boundary.
        # With fog off this is just the background colour, as before.
        _bg = self.view_distance.fog_color
        gl.glClearColor(_bg[0], _bg[1], _bg[2], 1.0)
        self.projection_matrix = perspective_projection(self.camera.fov, self._cached_aspect_ratio, _near, _far)
        _set_fov = self.logic_thread.camera.set_frustum_fov if self.logic_thread is not None else None
        if self.play_mode and _set_fov is not None:
            # Culling and the overhead ground footprint must see the frustum
            # drawn here, not an assumed one.
            _set_fov(self.camera.fov)
        self._proj_ptr = glm.value_ptr(self.projection_matrix)
        self._view_ptr = glm.value_ptr(self.view_matrix)
        self._render_config["culling_enabled"] = self.culling_enabled
        self._render_config["brush_display_mode"] = self.brush_display_mode
        self._render_config["show_triggers_as_solid"] = self.show_triggers_as_solid
        self._render_config["render_mode"] = self.current_render_mode
        self._render_config["play_mode"] = self.play_mode
        self._render_config["selected_object"] = self.editor.primary_selection()
        self._render_config["time"] = time.perf_counter() - self.start_time
        self._render_config["show_sprites_in_play_mode"] = self.show_sprites_in_play_mode
        self._render_config["show_glasses"] = bool(self.show_glasses)
        _glass_positions = []
        _glass_sprites = []
        if render_state is not None and self.play_mode and self._render_config["show_glasses"]:
            if not render_state.player_dead:
                _p = render_state.player_pos
                _glass_positions.append((float(_p.x), float(_p.y) + 40.0, float(_p.z)))
                _glass_sprites.append(glasses_sprite_key(
                    self.player1_glasses))
            if (render_state.splitscreen_active
                    and not render_state.player2_dead):
                _p2 = render_state.player2_pos
                _glass_positions.append((float(_p2.x), float(_p2.y) + 40.0, float(_p2.z)))
                _glass_sprites.append(DEFAULT_SPRITE_KEY)
        self._render_config["player_glasses_positions"] = tuple(_glass_positions)
        self._render_config["player_glasses_sprites"] = tuple(_glass_sprites)
        self._render_config["grid_visible"] = self.grid_visible and not self.play_mode
        self._render_config["terrain"] = self.editor.terrain
        # Both editor and play rendering consume the same canonical dense
        # projection published by LogicRender. There is no editor-side table.
        self._render_config["render_table"] = render_state.render_table
        self._render_config["render_refs"] = render_state.render_refs
        self._render_config["all_brush_slots"] = render_state.all_brush_slots
        _main_brush_slots = render_state.visible_brush_slots
        self._render_config["entity_table"] = render_state.entity_table
        self._render_config["entity_refs"] = render_state.entity_refs
        self._render_config["visible_thing_slots"] = render_state.visible_thing_slots
        self._render_config["thing_hidden"] = render_state.thing_hidden

        _splitscreen = (
            self.play_mode
            and self.splitscreen_mode
            and render_state is not None
            and render_state.splitscreen_active
        )
        # Plugin render hooks. Guarded by has_listeners so an unhooked frame
        # pays a single dict lookup and builds no payload — see the render.*
        # events in the plugin API. The manager handle is fetched once per frame.
        _pmgr = getattr(self.logic_thread, 'plugins', None) \
            if self.logic_thread is not None else None
        if _pmgr is not None and _pmgr.has_listeners("render.pre_scene"):
            _pmgr.emit("render.pre_scene", viewport=self, renderer=self.renderer,
                       projection=self.projection_matrix, view=self.view_matrix,
                       camera_pos=camera_pos, play_mode=self.play_mode)

        if _splitscreen:
            _w, _h = self.width(), self.height()
            _half = _w // 2
            _asp = _half / _h if _h > 0 else 1.0
            _split_proj = perspective_projection(self.camera.fov, _asp, 0.1, _far)

            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glDepthMask(gl.GL_TRUE)
            
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)

            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glScissor(0, 0, _half, _h)
            gl.glViewport(0, 0, _half, _h)
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDepthFunc(gl.GL_LESS)
            gl.glDisable(gl.GL_BLEND)
            gl.glDisable(gl.GL_STENCIL_TEST)

            self.renderer.render_scene(
                _split_proj, self.view_matrix, camera_pos,
                self.editor.primary_selection(), self._render_config,
                clear=False, brush_slots=_main_brush_slots,
            )

            if render_state:
                self._render_bullet_marks(render_state.bullet_marks, _split_proj, self.view_matrix)
            if render_state is not None and len(render_state.projectiles):
                self._render_projectiles(render_state.projectiles, _split_proj, self.view_matrix)
            if render_state and render_state.monster_debug_active:
                self._render_monster_debug_rays(render_state.monster_debug_rays,
                                                _split_proj, self.view_matrix)
            if self.play_mode and self.show_spatial_grid:
                self._render_spatial_grid(_split_proj, self.view_matrix)
            if self.show_glasses and render_state is not None:
                self._render_player_glasses(
                    self._render_config.get("player_glasses_positions", ()),
                    _split_proj,
                    self.view_matrix,
                )


            gl.glScissor(_half, 0, _half, _h)
            gl.glViewport(_half, 0, _half, _h)
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDepthFunc(gl.GL_LESS)
            gl.glDisable(gl.GL_BLEND)
            gl.glDisable(gl.GL_STENCIL_TEST)

            _p2_view = render_state.player2_view_matrix
            _p2_cam_pos = render_state.player2_pos

            # P2 has a different camera, so start from the complete live-hidden
            # dense brush projection. render_scene performs the camera-specific
            # narrowing from these slots; using P1's already-visible slots here
            # would incorrectly hide geometry that only P2 can see.
            _p2_brush_slots = self._render_config.get("all_brush_slots")
            self.renderer.render_scene(
                _split_proj, _p2_view, _p2_cam_pos,
                self.editor.primary_selection(), self._render_config,
                clear=False, brush_slots=_p2_brush_slots
            )

            if render_state:
                self._render_bullet_marks(render_state.bullet_marks, _split_proj, _p2_view)
            if render_state is not None and len(render_state.projectiles):
                self._render_projectiles(render_state.projectiles, _split_proj, _p2_view)
            if render_state and render_state.monster_debug_active:
                self._render_monster_debug_rays(render_state.monster_debug_rays,
                                                _split_proj, _p2_view)
            if self.play_mode and self.show_spatial_grid:
                self._render_spatial_grid(_split_proj, _p2_view)
            if self.show_glasses and render_state is not None:
                self._render_player_glasses(
                    self._render_config.get("player_glasses_positions", ()),
                    _split_proj,
                    _p2_view,
                )


            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glViewport(0, 0, _w, _h)
        else:
            self.renderer.render_scene(
                self.projection_matrix, self.view_matrix, camera_pos,
                self.editor.primary_selection(), self._render_config,
                brush_slots=_main_brush_slots,
            )
            # Native overhead player sprite (top-down mode), depth-tested so
            # walls occlude it correctly.
            self._draw_overhead_sprite(render_state)
            # Collision visualization
            if getattr(self, '_collision_vis_mode', 'off') != 'off':
                # Get collision brushes from logic thread
                collision_brushes = []
                if self.logic_thread is not None:
                    collision_brushes = self.logic_thread.collision_runtime._model_collision_brushes
                    # Build collision brushes on demand if not already built
                    # (needed for editor mode where they aren't auto-built on play start)
                    if not collision_brushes:
                        self.logic_thread.collision_runtime.toggle_model_collision(True)
                        collision_brushes = self.logic_thread.collision_runtime._model_collision_brushes
                if collision_brushes:
                    mode = self._collision_vis_mode
                    filtered = []
                    for b in collision_brushes:
                        b_mode = b.get('_collision_mode', 'aabb')
                        if mode == 'all' or mode == b_mode:
                            filtered.append(b)
                    if filtered:
                        self.renderer.draw_collision_visualization(
                            self.projection_matrix, self.view_matrix, filtered
                        )
            if render_state:
                self._render_bullet_marks(render_state.bullet_marks, self.projection_matrix, self.view_matrix)
            if render_state is not None and len(render_state.projectiles):
                self._render_projectiles(render_state.projectiles, self.projection_matrix, self.view_matrix)
            if render_state and render_state.monster_debug_active:
                self._render_monster_debug_rays(render_state.monster_debug_rays,
                                                self.projection_matrix, self.view_matrix)
            if self.play_mode and self.show_spatial_grid:
                self._render_spatial_grid(self.projection_matrix, self.view_matrix)
        if not self.play_mode and self.editor.show_logic_links:
            _scene_ver = (len(self.editor.state.brushes), len(self.editor.state.things))
            if self._io_conn_cache is None or self._io_conn_scene_ver != _scene_ver:
                self._io_conn_cache     = self._gather_io_connections()
                self._io_conn_scene_ver = _scene_ver
            conn_lines = self._io_conn_cache
            if conn_lines:
                self.renderer.draw_connection_lines(self.projection_matrix, self.view_matrix, conn_lines)
        if self.face_mode_active and self.hovered_face_info:
            brush, face_name = self.hovered_face_info
            if hasattr(self.renderer, 'draw_face_highlight'):
                self.renderer.draw_face_highlight(self.projection_matrix, self.view_matrix, brush, face_name)
        # Component handles.  The arrays come from the editor's controller,
        # which rebuilds them only when the selection, hover or geometry
        # changed; here it is a version check and a draw call.
        if not self.play_mode:
            _components = self._components()
            if _components is not None and _components.is_component_mode() and \
                    hasattr(self.renderer, 'draw_component_overlay'):
                _targets = self._component_targets()
                if _targets:
                    self.renderer.draw_component_overlay(
                        self.projection_matrix, self.view_matrix,
                        _components.overlay(_targets), _components.version)
        if render_state:
            visible = len(render_state.visible_brush_slots)
            actual_total = len(self.editor.state.brushes)
            total = actual_total if actual_total > 0 else render_state.total_brushes
            culled = max(0, total - visible)
            self.sysmon.update_stats(
                visible_brushes=visible,
                culled_brushes=culled,
                total_brushes=total
            )
        # 3D world is done; plugins may add their own passes here (still in the
        # GL context, before the 2D overlay painter opens).
        if _pmgr is not None and _pmgr.has_listeners("render.post_scene"):
            _pmgr.emit("render.post_scene", viewport=self, renderer=self.renderer,
                       projection=self.projection_matrix, view=self.view_matrix,
                       camera_pos=camera_pos, play_mode=self.play_mode)

        restore_default_pixel_store()
        # The native renderer may leave viewport/scissor/depth state from a
        # pass (split-screen, shadows, or an effect). QPainter's GL backend
        # starts from the widget's 2D framebuffer, not that native state.
        # Normalize the native state while native painting is still active;
        # endNativePainting() then hands the context back to Qt cleanly.
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glViewport(0, 0, max(1, int(self.width())), max(1, int(self.height())))
        painter.endNativePainting()
        # Do not let native rendering leak QPainter state into the first 2D
        # overlay draw. Individual HUD elements still use save()/restore().
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if self.play_mode:
            self._draw_underwater_overlay(painter, render_state)
        if self.editor.config.getboolean('Display', 'show_fps', fallback=False):
            self._draw_fps_counter(painter)
        if self.play_mode and self.show_sprites_in_play_mode:
            self._draw_sprites_text(painter)
        if self.play_mode and self.show_render_menu:
            self._draw_render_menu(painter)
        _hud_visible_override = self._hud_runtime_visible
        _hud_visible = (
            _hud_visible_override
            if _hud_visible_override is not None
            else self.editor.config.getboolean('Display', 'show_hud', fallback=True)
        )
        if self.play_mode and self._hud_style != 0 and _hud_visible:
            _ss_hud = (
                self.splitscreen_mode
                and render_state is not None
                and render_state.splitscreen_active
            )
            if _ss_hud:
                self._draw_hud_splitscreen(painter, render_state)
            else:
                self._draw_hud(painter, render_state)
        if self.play_mode and render_state and render_state.player_dead:
            self._draw_death_screen(painter)
        if self.play_mode and getattr(self, '_cached_level_complete_ui', None):
            self._draw_level_complete_overlay(painter)
        if self.play_mode:
            logic_thread = self.logic_thread
            if logic_thread is not None:
                for line, text in logic_thread.cutscene_runtime.consume_cinematic_messages():
                    if line == "message2":
                        self.show_view_message2(text)
                    elif line == "message3":
                        self.show_view_message3(text)
                    else:
                        self.show_view_message(text)
            self._draw_view_message(painter, self.width(), self.height())
            self._draw_view_message2(painter, self.width(), self.height())
            self._draw_view_message3(painter, self.width(), self.height())

        if self.sysmon.is_active():
            self.sysmon.draw(
                painter, self.fps, self.logic_thread, self.renderer,
                self.editor.state, self.editor.terrain
            )

        self.floating_windows.draw_all(painter)

        # Live terrain sculpting hint: draw this inside the 3D viewport rather
        # than using the editor toast system. This is intentionally styled as
        # the compact dark tool-mode banner used by the other viewport tools.
        # Draw the live brush circle last so it sits over the 3D terrain.
        self._draw_terrain_brush_cursor(painter)
        if self.face_mode_active:
            painter.setFont(self._face_mode_font_top)
            ht = self._face_mode_font_top.pointSize() + 6
            painter.setFont(self._face_mode_font_bot)
            hb = self._face_mode_font_bot.pointSize() + 4
            cx = self.width() // 2
            margin_bottom = 30
            spacing = 5
            padding_x = 20
            padding_y = 10
            total_text_h = ht + hb + spacing
            box_w = max(self._face_mode_top_width, self._face_mode_bot_width) + (padding_x * 2)
            box_h = total_text_h + (padding_y * 2)

        # 2D overlay hook: plugins can draw HUD/graphics with the live QPainter
        # (the last thing before the painter closes for the frame).
        if _pmgr is not None and _pmgr.has_listeners("render.overlay"):
            _pmgr.emit("render.overlay", viewport=self, painter=painter,
                       width=self.width(), height=self.height(),
                       play_mode=self.play_mode)

        painter.end()


    def _draw_underwater_overlay(self, painter, render_state):
        """Tint the view while the camera is below a water surface."""
        p1_under = getattr(self, '_cached_underwater', False)
        p2_under = getattr(self, '_cached_p2_underwater', False)
        if not p1_under and not p2_under:
            return
        splitscreen = (
            self.splitscreen_mode
            and render_state is not None
            and render_state.splitscreen_active
        )
        w, h = self.width(), self.height()
        if splitscreen:
            half = w // 2
            if p1_under:
                self._fill_underwater_rect(painter, 0, 0, half, h)
            if p2_under:
                self._fill_underwater_rect(painter, half, 0, half, h)
        elif p1_under:
            self._fill_underwater_rect(painter, 0, 0, w, h)

    def _fill_underwater_rect(self, painter, x, y, w, h):
        tint = getattr(self, '_cached_underwater_tint', None) or [0.0, 0.4, 0.6]
        r = int(max(0.0, min(1.0, tint[0])) * 255)
        g = int(max(0.0, min(1.0, tint[1])) * 255)
        b = int(max(0.0, min(1.0, tint[2])) * 255)
        # Slow "breathing" so the immersion feels alive rather than a static filter
        wobble = math.sin((time.perf_counter() - self.start_time) * 1.7) * 10.0
        grad = QLinearGradient(0, y, 0, y + h)
        grad.setColorAt(0.0, QColor(r, g, b, max(0, min(255, int(95 + wobble)))))
        grad.setColorAt(0.55, QColor(int(r * 0.6), int(g * 0.7), int(b * 0.75), 130))
        grad.setColorAt(1.0, QColor(int(r * 0.3), int(g * 0.4), int(b * 0.5), 165))
        painter.fillRect(x, y, w, h, QBrush(grad))

    def _draw_fps_counter(self, painter):
        painter.setFont(self._fps_font)
        painter.setPen(self._hud_white_pen)
        rect_width = 100
        rect_x = self.width() - rect_width - 5
        painter.fillRect(rect_x, 5, rect_width, 20, self._hud_fps_bg_brush)
        painter.drawText(rect_x + 5, 20, f"FPS: {self.fps:.0f}")

    def _draw_sprites_text(self, painter):
        painter.setFont(self._sprites_font)
        painter.setPen(self._hud_pink_pen)
        painter.fillRect(5, 5, 80, 25, self._hud_sprites_bg_brush)
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
        if self._hud_style == 0:
            return
        # A LogicCamera owns the player's view completely: no HUD is shown
        # while the cinematic is running.
        if render_state is not None and render_state.cinematic_camera_active:
            return
        if viewport_width is None:
            viewport_width = self.width()
        if viewport_height is None:
            viewport_height = self.height()
        # In overhead (top-down) mode there is no first-person view, so the gun
        # HUD sprite makes no sense; the held weapon is shown as a small pickup
        # icon bottom-right instead (see below), alongside any held keys.
        # LogicCamera is the authoritative camera mode.
        overhead = self.logic_thread.camera.is_overhead()
        health = self._cached_health
        max_health = self._cached_max_health
        if health is None or max_health is None:
            return
        hud_margin = 20
        active_weapon = self._cached_active_weapon

        # The entire HUD fades back in for four seconds after a LogicCamera
        # gives control back to the player. The health count has its own
        # independent opacity state, normally resting at 50% when idle.
        hud_alpha = max(
            0.0, min(1.0, float(render_state.hud_alpha))
        )
        hud_opacity = 1.0 if self._hud_style == 3 else self._hud_opacity / 100.0
        health_hud_alpha = 1.0 if self._hud_style == 3 else max(
            0.0, min(1.0, float(render_state.hud_health_alpha))
        )
        # _draw_hud owns this painter opacity for everything it draws: weapon,
        # health/ammo, crosshair, messages, prompts, overhead icons and keys.
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity)

        # Draw the weapon before the status counts so the health indicator is
        # always visually on top of any weapon sprite.
        if active_weapon and not overhead:
            hud_pixmap = self._load_gun_hud_pixmap(active_weapon)
            if hud_pixmap and not hud_pixmap.isNull():
                target_h = int(200 * viewport_height / 600.0)
                cache_key = (active_weapon, target_h)
                scaled = self._cached_gun_hud.get(cache_key)
                if scaled is None or scaled.isNull():
                    if hud_pixmap.height() > 0:
                        target_w = int(hud_pixmap.width() * (target_h / hud_pixmap.height()))
                    else:
                        target_w = target_h
                    img = hud_pixmap.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
                    scaled = QPixmap.fromImage(img).scaled(
                        target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self._cached_gun_hud[cache_key] = scaled
                if active_weapon == 'gun2':
                    x = (viewport_width - scaled.width()) // 2
                    y = viewport_height - scaled.height()
                else:
                    x = viewport_width - scaled.width() - 20
                    y = viewport_height - scaled.height()
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                painter.drawPixmap(x, y, scaled)
                painter.restore()
                if getattr(self, '_cached_muzzle_flash', False):
                    flash_pixmap = self._load_gun_flash_pixmap(active_weapon)
                    if flash_pixmap and not flash_pixmap.isNull():
                        painter.save()
                        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                        painter.drawPixmap(x, y, scaled.width(), scaled.height(), flash_pixmap)
                        painter.restore()

        style = self._hud_style
        health_font = QFont(self._hud_status_font)
        health_font.setPointSize(
            self._hud_health_point_size(style, viewport_height)
        )
        painter.setFont(health_font)
        health_color = self._hud_health_orange
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
        painter.setPen(self._hud_count_shadow_pen)
        painter.drawText(health_x + 2, health_y + 2, health_text)
        painter.setPen(health_color)
        painter.drawText(health_x, health_y, health_text)
        painter.restore()

        if active_weapon in ('gun1', 'gun2'):
            ammo_font = QFont(health_font) if style == 3 else QFont(self._hud_status_font)
            ammo_font.setPointSize(
                24 if style == 3 else max(22, min(36, int(viewport_height * 0.045)))
            )
            ammo_text = (
                "∞"
                if active_weapon == 'gun1'
                else str(max(0, int(getattr(
                    self, '_cached_player_ammo', 0))))
            )
            ammo_x = health_x + metrics.horizontalAdvance(health_text)
            if style == 3:
                # Style 3 uses a single compact status line: separate ammo
                # from health with one font-space and keep the counter white.
                ammo_x += 2 * metrics.horizontalAdvance(" ")
            painter.setFont(ammo_font)
            painter.setPen(self._hud_count_shadow_pen)
            painter.drawText(ammo_x + 2, health_y + 2, ammo_text)
            painter.setPen(Qt.white if style == 3 else self._hud_ammo_green)
            painter.drawText(ammo_x, health_y, ammo_text)

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
        msg = self._cached_hud_message
        prompt_key = render_state.hud_prompt_key if render_state is not None else None
        if msg:
            if self._cached_hud_message != msg:
                self._cached_hud_message = msg
                self._cached_hud_message_width = QFontMetrics(self._hud_msg_font).horizontalAdvance(msg)
            cx = viewport_width // 2
            cy = viewport_height // 2 + 50
            tw = self._cached_hud_message_width
            painter.setFont(self._hud_msg_font)
            painter.setPen(self._hud_shadow_pen)
            painter.drawText(cx - tw // 2 + 2, cy + 2, msg)
            painter.setPen(self._hud_grey_pen)
            painter.drawText(cx - tw // 2, cy, msg)

            if prompt_key:
                prompt_size = self._cached_prompt_key_size
                if self._cached_prompt_key != prompt_key:
                    self._cached_prompt_key = prompt_key
                    self._cached_prompt_key_pixmap = None
                    self._cached_prompt_key_loaded = False
                if not self._cached_prompt_key_loaded:
                    self._cached_prompt_key_loaded = True
                    try:
                        pixmap = Prop.get_key_pixmap(prompt_key)
                        if pixmap and not pixmap.isNull():
                            self._cached_prompt_key_pixmap = pixmap.scaled(
                                prompt_size, prompt_size,
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    except Exception:
                        self._cached_prompt_key_pixmap = None
                if (self._cached_prompt_key_pixmap is not None
                        and not self._cached_prompt_key_pixmap.isNull()):
                    scaled = self._cached_prompt_key_pixmap
                    painter.drawPixmap(
                        cx - scaled.width() // 2,
                        cy + 10,
                        scaled,
                    )
                else:
                    self._draw_key_fallback(
                        painter,
                        prompt_key,
                        cx - prompt_size // 2,
                        cy + 10,
                        prompt_size,
                    )
        hint = self._play_mode_hint
        if hint and not msg:
            if self._cached_hint_text != hint:
                self._cached_hint_text = hint
                self._cached_hint_width = QFontMetrics(self._hud_msg_font).horizontalAdvance(hint)
            cx = viewport_width // 2
            cy = viewport_height // 2 + 50
            tw = self._cached_hint_width
            painter.setFont(self._hud_msg_font)
            painter.setPen(self._hud_shadow_pen)
            painter.drawText(cx - tw // 2 + 2, cy + 2, hint)
            painter.setPen(self._hud_grey_pen)
            painter.drawText(cx - tw // 2, cy, hint)
        if self._actor_pick is not None:
            self._draw_actor_pick_hint(painter, viewport_width)
        # Overhead: held weapon shown as a bottom-right collectible icon (like keys).
        # It takes the rightmost slot; keys shift left so both fit side by side.
        key_slot_offset = 0
        if overhead and active_weapon:
            icon_size = 100
            wx = viewport_width - hud_margin - icon_size
            wy = viewport_height - hud_margin - icon_size
            pm = self._load_weapon_collect_pixmap(active_weapon)
            if pm and not pm.isNull():
                cache_key = (active_weapon, icon_size)
                scaled = self._cached_weapon_collect.get(cache_key)
                if scaled is None or scaled.isNull():
                    scaled = pm.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self._cached_weapon_collect[cache_key] = scaled
                painter.drawPixmap(wx + (icon_size - scaled.width()) // 2,
                                   wy + (icon_size - scaled.height()) // 2, scaled)
                # Reserve the weapon's slot so keys don't overlap it.
                key_slot_offset = icon_size + 15

        collected_keys = self._cached_collected_keys
        if collected_keys:
            key_x = viewport_width - hud_margin - 100 - key_slot_offset
            key_y = viewport_height - hud_margin - 100
            key_size = 100
            key_spacing = 40
            if self._cached_key_size != key_size:
                self._cached_key_pixmaps.clear()
                self._cached_key_size = key_size
            for i, key_name in enumerate(sorted(collected_keys)):
                icon_x = key_x - i * key_spacing
                cached = self._cached_key_pixmaps.get(key_name)
                if cached is not None and not cached.isNull():
                    painter.drawPixmap(icon_x, key_y, cached)
                    continue
                try:
                    pixmap = Prop.get_key_pixmap(key_name)
                    if pixmap and not pixmap.isNull():
                        scaled = pixmap.scaled(key_size, key_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self._cached_key_pixmaps[key_name] = scaled
                        painter.drawPixmap(icon_x, key_y, scaled)
                    else:
                        self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)
                except Exception:
                    self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)

        painter.restore()

    def _draw_hud_splitscreen(self, painter, render_state):
        if self._hud_style == 0:
            return
        if render_state is not None and render_state.cinematic_camera_active:
            return
        w, h = self.width(), self.height()
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
        hud_opacity = 1.0 if self._hud_style == 3 else self._hud_opacity / 100.0
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity)
        painter.setPen(QColor(255, 200, 50))
        painter.setFont(self._hud_font)
        painter.drawText(8, 22, "P1")
        painter.restore()
        p2_health = render_state.player2_health
        p2_max_health = render_state.player2_max_health
        p2_dead = render_state.player2_dead
        health_hud_alpha = 1.0 if self._hud_style == 3 else max(
            0.0, min(1.0, float(render_state.hud_health_alpha))
        )
        painter.save()
        painter.setClipRect(half, 0, half, h)
        margin = 1
        health_font = QFont(self._hud_status_font)
        health_font.setPointSize(
            self._hud_health_point_size(self._hud_style, h)
        )
        painter.save()
        painter.setOpacity(hud_alpha * hud_opacity * health_hud_alpha)
        painter.setFont(health_font)
        p2_health_color = self._hud_health_orange
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
        painter.setFont(self._hud_font)
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(half + 8, 22, "P2")
        painter.restore()
        if p2_dead:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(120, 0, 0, 140)))
            painter.drawRect(half, 0, half, h)
            painter.setFont(self._death_title_font)
            lbl = "P2 DIED"
            lbl_w = QFontMetrics(self._death_title_font).horizontalAdvance(lbl)
            lbl_x = half + (half - lbl_w) // 2
            painter.setPen(QColor(60, 0, 0, 220))
            painter.drawText(lbl_x + 3, h // 2 + 3, lbl)
            painter.setPen(QColor(255, 60, 60))
            painter.drawText(lbl_x, h // 2, lbl)
        painter.restore()

    def _draw_death_screen(self, painter):
        w, h = self.width(), self.height()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(120, 0, 0, 160)))
        painter.drawRect(0, 0, w, h)
        painter.setFont(self._death_title_font)
        title_x = (w - self._cached_death_title_width) // 2
        title_y = h // 2 - 20
        painter.setPen(QColor(60, 0, 0, 220))
        painter.drawText(title_x + 3, title_y + 3, "DIED")
        painter.setPen(QColor(255, 60, 60))
        painter.drawText(title_x, title_y, "DIED")
        painter.setFont(self._death_sub_font)
        sub_x = (w - self._cached_death_sub_width) // 2
        sub_y = title_y + 60
        painter.setPen(QColor(0, 0, 0, 180))
        painter.drawText(sub_x + 2, sub_y + 2, "Press Escape to return to the editor")
        painter.setPen(QColor(220, 180, 180))
        painter.drawText(sub_x, sub_y, "Press Escape to return to the editor")

    def _draw_key_fallback(self, painter, key_name, x, y, size):
        color, pen, brush = self._key_fallback_cache.get(key_name, self._key_fallback_default)
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
        x, y = (self.width() - width) // 2, (self.height() - height) // 2
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
            if self.current_render_mode == mid:
                painter.setPen(QColor(100, 255, 100))
                painter.drawText(x + 20, cy, "> " + txt)
            else:
                painter.setPen(QColor(200, 200, 200))
                painter.drawText(x + 20, cy, "  " + txt)
            cy += 20


    def _draw_level_complete_overlay(self, painter):
        w, h = self.width(), self.height()
        # Dim background
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 180)))
        painter.drawRect(0, 0, w, h)

        # Title
        title = self._cached_level_complete_ui.get('title', 'Complete')
        painter.setFont(QFont("Arial", 48, QFont.Bold))
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

        painter.setFont(QFont("Arial", 16, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        btn_text = self._cached_level_complete_ui.get('button_text', 'Continue to Next Map')
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(btn_text)
        painter.drawText(btn_x + (btn_w - tw) // 2, btn_y + 34, btn_text)

        # Store rect for click detection
        self._level_complete_btn_rect = QRect(btn_x, btn_y, btn_w, btn_h)

        # Hint
        painter.setFont(QFont("Arial", 12))
        painter.setPen(QColor(180, 180, 180))
        hint = "E to continue, Esc to cancel"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(hint)
        painter.drawText((w - tw) // 2, btn_y + btn_h + 30, hint)

    def _confirm_level_complete(self):
        ui = getattr(self, '_cached_level_complete_ui', None)
        if not ui:
            return
        target_map = ui.get('target_map', '')
        if target_map:
            if hasattr(self.editor, 'load_level_signal'):
                self.editor.load_level_signal.emit(target_map)
            elif hasattr(self.editor, 'load_level'):
                self.editor.load_level(target_map)
        self._cached_level_complete_ui = None
        self._level_complete_btn_rect = None
        if self.logic_thread:
            self.logic_thread.interaction_runtime.level_complete_ui = None

    def _cancel_level_complete(self):
        self._cached_level_complete_ui = None
        self._level_complete_btn_rect = None
        if self.logic_thread:
            self.logic_thread.interaction_runtime.level_complete_ui = None
    def load_texture(self, texture_name, subfolder):
        return self.renderer.load_texture(texture_name, subfolder) if self.renderer else 0

    def load_all_sprite_textures(self):
        things = {
            'PlayerStart': 'player.png',
            'Light': 'light.png',
            'Monster': 'monster.png',
            'Prop': 'pickup.png',
            'Speaker': 'speaker.png',
            'LevelChanger': 'levelchanger.png',
            'Portal': 'portal.png',
            'LogicCommand': 'logic_command.png',
        }
        for weapon in ['gun1', 'gun2', 'cig']:
            tid = self.load_texture(f'{weapon}HUD.png', 'sprites')
            if tid:
                self.sprite_textures[f'{weapon}_hud'] = tid
            tid_flash = self.load_texture(f'{weapon}HUD_flash.png', 'sprites')
            if tid_flash:
                self.sprite_textures[f'{weapon}_flash'] = tid_flash
        for cls, fname in things.items():
            tid = self.load_texture(fname, 'sprites')
            if tid:
                self.sprite_textures[cls] = tid
        # Every glasses style, so changing Appearance needs no reload.
        for style, _label, fname in GLASSES_STYLES:
            tid = self.load_texture(fname, GLASSES_SUBFOLDER)
            if tid:
                self.sprite_textures[glasses_sprite_key(style)] = tid
        if 'Portal' not in self.sprite_textures and self.renderer:
            try:
                tex_id = gl.glGenTextures(1)
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                cyan = (gl.GLubyte * (4 * 4))(
                    0, 220, 255, 255,  0, 220, 255, 255,
                    0, 220, 255, 255,  0, 220, 255, 255,
                )
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 2, 2, 0,
                                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, cyan)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
                self.sprite_textures['Portal'] = tex_id
            except Exception:
                pass
        key_textures = {'blue_key': 'bluekey.png', 'red_key': 'redkey.png', 'yellow_key': 'yellowkey.png', 'green_key': 'greenkey.png'}
        for key_name, fname in key_textures.items():
            tid = self.load_texture(fname, 'sprites')
            if tid:
                self.sprite_textures[f'key_{key_name}'] = tid
        proj_tid = self.load_texture('projectile.png', 'sprites')
        if proj_tid:
            self.sprite_textures['projectile'] = proj_tid
        if self.renderer:
            self.renderer.set_sprite_textures(self.sprite_textures)

    def toggle_play_mode(self, player_start_pos, player_start_angle, physics_enabled=True):
        self.play_mode = not self.play_mode
        if self.play_mode:
            # Force split-screen OFF when entering play mode
            self.splitscreen_mode = False
            self._last_player_start_pos = player_start_pos
            self._last_player_start_angle = player_start_angle
            center_pos = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center_pos)
            self.last_mouse_pos = self.mapFromGlobal(center_pos)
            QApplication.setOverrideCursor(Qt.BlankCursor)

            # Convert editor angle (0° = east) to game angle (0° = north).
            # Do not add another 180° flip: that would make every PlayerStart
            # spawn facing exactly opposite to its authored direction.
            player_angle_rad = np.radians(90.0 - player_start_angle) + np.pi

            self.player = Player(
                player_start_pos[0], player_start_pos[2],
                player_angle_rad,
                physics_enabled=physics_enabled
            )
            self.player.pos.y = player_start_pos[1]

            # ─── Flush any mouse input that may have been queued ───
            self.game_state.consume_mouse_delta()
            self.game_state.set_mouse_delta(0.0, 0.0)

            self._play_mode_hint = "ESC to Exit, F12 Fullscreen"
            self._play_mode_hint_timer.start(3000)

            self._reload_hud_settings()
            self._hud_runtime_visible = None
            if self.logic_thread:
                self.logic_thread.player_runtime.player = self.player
                self.logic_thread.render_runtime.set_hud_fade_enabled(self._hud_fade_enabled)
                self.logic_thread.session_runtime.apply_play_mode(True)

            if self.splitscreen_mode:
                self.player2 = Player(
                    player_start_pos[0] + 32, player_start_pos[2],
                    player_angle_rad,
                    physics_enabled=physics_enabled,
                )
                self.player2.pos.y = player_start_pos[1]
                if self.logic_thread:
                    self.logic_thread.player_runtime.player2 = self.player2
                if self.height() > 0:
                    self._cached_aspect_ratio = (self.width() // 2) / self.height()
                    if self.logic_thread:
                        self.logic_thread.camera.set_frustum_aspect(self._cached_aspect_ratio)
        else:
            # Leaving Play Mode is an audio lifecycle boundary: stop both
            # looping speaker channels and one-shot mixer channels, and discard
            # any sound requests queued by the logic thread during teardown.
            self.stop_all_sounds()
            self._actor_pick = None
            self.actor_pick_hover = None
            if self.console_overlay_active:
                self._console_input.hide()
                self.console_overlay_active = False
            self.monster_debug_active = False
            self.show_spatial_grid = False
            if self.logic_thread:
                self.logic_thread.monster_debug_active = False
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self.setCursor(Qt.ArrowCursor)
            if self.logic_thread:
                self.logic_thread.session_runtime.apply_play_mode(False)
                self.logic_thread.player_runtime.player = None
            self.player = None
            self.player2 = None
            if self.logic_thread:
                self.logic_thread.player_runtime.player2 = None
            if self.height() > 0:
                self._cached_aspect_ratio = self.width() / self.height()
                if self.logic_thread:
                    self.logic_thread.camera.set_frustum_aspect(self._cached_aspect_ratio)
            self._play_mode_hint = ""
            self._play_mode_hint_timer.stop()
            self._cached_hint_text = None
            self.update()

    def _toggle_splitscreen(self):
        self.splitscreen_mode = not self.splitscreen_mode
        if self.play_mode:
            pos = self._last_player_start_pos
            angle = self._last_player_start_angle
            if self.splitscreen_mode:
                self.player2 = Player(pos[0] + 32, pos[2], np.radians(90.0 - angle), physics_enabled=True)
                self.player2.pos.y = pos[1]
                if self.logic_thread:
                    self.logic_thread.player_runtime.player2 = self.player2
            else:
                self.player2 = None
                if self.logic_thread:
                    self.logic_thread.player_runtime.player2 = None
            w, h = self.width(), self.height()
            if h > 0:
                vp_w = (w // 2) if self.splitscreen_mode else w
                self._cached_aspect_ratio = vp_w / h
                if self.logic_thread:
                    self.logic_thread.camera.set_frustum_aspect(self._cached_aspect_ratio)
        status = "ON" if self.splitscreen_mode else "OFF"
        if hasattr(self.editor, 'show_toast'):
            self.editor.show_toast(f"Split-Screen: {status}  [F9]")

    def _exit_play_mode(self):
        if not self.play_mode:
            return
        # Prefer the editor's full teardown so the Play button colour, mode
        # label, properties tab and focus are all restored to editor state.
        # Reached e.g. when ESC is pressed after the player dies; without this
        # the Play button would stay red after returning to the editor.
        editor = getattr(self, 'editor', None)
        if editor is not None and hasattr(editor, '_exit_play_mode'):
            editor._exit_play_mode()
            return
        pos = self._last_player_start_pos
        angle = self._last_player_start_angle
        self.toggle_play_mode(pos, angle)

    def set_cull_distance(self, distance):
        """Set the camera's maximum render distance, in world units.

        The single entry point for the editor's "Cull Dist" spinbox, the
        ``r_viewdistance`` console command and anything the I/O system fires at
        them. It moves three things that have to agree — the broad-phase
        object cull, the projection's far plane, and the fog that hides that
        far plane — and nothing else: lighting, textures, LOD detail bands and
        what the world has loaded are all untouched, so a player can pull the
        draw distance in for framerate and get the same scene, just less of it
        at once.

        Fog start and end track this automatically unless they have been pinned
        (see :class:`~engine.view_distance.ViewDistance`), which is what keeps
        the fog opaque before the clip at any distance.
        """
        self.view_distance.distance = float(distance)
        # Read back: ViewDistance clamps to its supported span, and callers
        # (and the value shown in r_list) should see what actually took effect.
        self.cull_distance = self.view_distance.distance
        self._sync_view_distance()
        self.update()

    def _sync_view_distance(self):
        """Push the shared view-distance object at everything that reads it.

        The render runtime and renderer hold the *same* instance rather than
        a copy, so this only has to run when one of them is created or swapped
        — and the per-frame LOD bands, which are plain numbers, are refreshed
        here too.
        """
        distance = self.view_distance.distance
        if self.renderer:
            self.renderer.view_distance = self.view_distance
            self.renderer.lod_manager.cull_dist_sq = distance * distance
            self.renderer.lod_manager.full_dist_sq = (distance * 0.25) ** 2

    def switch_renderer(self, mode: str):
        if mode == self._renderer_mode:
            return
        cls = _RENDERER_CLASSES.get(mode)
        if cls is None:
            print(f"[QtGameView] Unknown renderer mode '{mode}' — ignoring.")
            return
        print(f"[QtGameView] Switching renderer: {self._renderer_mode} → {mode}")
        self.makeCurrent()
        try:
            old = self.renderer
            self.renderer = None
            if old is not None:
                if hasattr(old, 'cleanup'):
                    try:
                        old.cleanup()
                    except Exception as e:
                        print(f"[QtGameView] Renderer cleanup warning: {e}")
                del old
            config = self.editor.config
            self.renderer = cls(
                self.load_texture, self.grid_size, self.world_size, config)
            self.renderer.set_sprite_textures(self.sprite_textures)
            self._sync_view_distance()
            self.grid_dirty = True
            self._renderer_mode = mode
            print(f"[QtGameView] Renderer switched to {mode}.")
        except Exception as exc:
            print(f"[QtGameView] switch_renderer FAILED: {exc}")
            try:
                config = self.editor.config
                self.renderer = Renderer_F(
                    self.load_texture, self.grid_size, self.world_size, config)
                self._renderer_mode = 'Forward'
            except Exception as fe:
                print(f"[QtGameView] Emergency fallback also failed: {fe}")
        finally:
            self.doneCurrent()
        self.update()

    def get_selected_object_pos(self):
        if not self.editor.primary_selection():
            return None
        if isinstance(self.editor.primary_selection(), dict):
            return glm.vec3(self.editor.primary_selection().get('pos', [0, 0, 0]))
        return glm.vec3(self.editor.primary_selection().pos)

    def set_selected_object_pos(self, new_pos_vec):
        """Move the whole selection so the grabbed object lands on ``new_pos_vec``.

        The gizmo drags one object, but everything selected travels with it by
        the same snapped delta — snapping each object to the grid separately
        would pull them onto a common grid line and destroy the arrangement.
        The move goes through the editor's translate helper so an angled
        brush's plane set comes along instead of being left behind by a bare
        write to ``pos``.
        """
        primary = self.editor.primary_selection()
        if not primary:
            return
        grid = self.editor.grid_size_spinbox.value()
        snapped = [round(c / grid) * grid for c in new_pos_vec]
        current = primary['pos'] if isinstance(primary, dict) else primary.pos
        delta = [snapped[i] - current[i] for i in range(3)]
        if not any(delta):
            return

        group = [o for o in self.editor.selected_objects_list()
                 if not (o.get('lock', False) if isinstance(o, dict)
                         else o.properties.get('lock', False))]
        if primary not in group:
            group = [primary]
        for obj in group:
            self.editor._translate_object(obj, delta)
        self.update()

    def set_terrain_sculpt_active(self, active: bool):
        active = bool(active)
        self.terrain_sculpt_active = active
        self.terrain_sculpt_painting = False
        if active:
            self.setCursor(Qt.CrossCursor)
        else:
            self._terrain_brush_mouse_pos = QPoint(-1, -1)
            self._terrain_brush_hit = None
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def raycast_terrain(self, mx: int, my: int):
        terrain = self.editor.terrain
        if terrain is None or not terrain.enabled:
            return None
        ray_o, ray_d = self.get_ray_from_mouse(mx, my)
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

    def _world_to_screen(self, point):
        """Project a world-space point into this viewport's pixel coordinates."""
        clip = self.projection_matrix * self.view_matrix * glm.vec4(
            float(point[0]), float(point[1]), float(point[2]), 1.0
        )
        if abs(float(clip.w)) < 1e-6:
            return None
        ndc_x = float(clip.x) / float(clip.w)
        ndc_y = float(clip.y) / float(clip.w)
        return QPoint(
            int((ndc_x + 1.0) * 0.5 * self.width()),
            int((1.0 - ndc_y) * 0.5 * self.height()),
        )

    def _draw_terrain_brush_cursor(self, painter):
        """Draw a screen-space circle representing the world-space brush."""
        if not self.terrain_sculpt_active or self.play_mode or self.mouselook_active:
            return
        pos = self._terrain_brush_mouse_pos
        if pos.x() < 0 or pos.y() < 0:
            return

        hit = self._terrain_brush_hit
        if hit is None:
            return

        wx, wy, wz = hit
        radius = max(1.0, float(self.terrain_sculpt_radius))
        center = self._world_to_screen((wx, wy + 0.02, wz))
        px = self._world_to_screen((wx + radius, wy + 0.02, wz))
        pz = self._world_to_screen((wx, wy + 0.02, wz + radius))
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

    def _apply_sculpt_at_mouse(self, mx: int, my: int):
        hit = self.raycast_terrain(mx, my)
        if hit is None:
            return
        wx, wy, wz = hit
        terrain = self.editor.terrain
        mode = self.terrain_sculpt_mode
        radius = self.terrain_sculpt_radius
        strength = self.terrain_sculpt_strength

        if mode == 'stamp':
            browser = getattr(self.editor, 'asset_browser', None)
            texture_path = browser.get_selected_filepath() if browser is not None else None
            if not texture_path:
                if hasattr(self.editor, 'show_toast'):
                    self.editor.show_toast("Select a texture in the Asset Browser first", is_error=True)
                return

            abs_texture = os.path.abspath(texture_path)
            rel_texture = os.path.relpath(
                abs_texture, self.editor.root_dir).replace(os.sep, '/')
            texture_name = (
                rel_texture
                if rel_texture != '..' and not rel_texture.startswith('../')
                else abs_texture.replace(os.sep, '/')
            )

            # One paint click is one undo step. The compact terrain snapshot
            # already carries the baked terrain material data.
            if hasattr(self.editor, 'state') and hasattr(self.editor.state, 'terrain_data'):
                self.editor.state.terrain_data = terrain.to_dict()
                if hasattr(self.editor, 'save_state'):
                    self.editor.save_state()

            added = terrain.paint_texture_at(
                wx, wz, radius, texture_name,
                feather=self.terrain_sculpt_feather,
            )
            if not added:
                if hasattr(self.editor, 'state') and hasattr(
                        self.editor.state, 'discard_last_checkpoint'):
                    self.editor.state.discard_last_checkpoint()
                if hasattr(self.editor, 'show_toast'):
                    self.editor.show_toast("Terrain texture paint was not applied", is_error=True)
                return

            if hasattr(self.editor, 'state') and hasattr(self.editor.state, 'terrain_data'):
                self.editor.state.terrain_data = terrain.to_dict()
            if hasattr(self.editor, 'show_toast'):
                self.editor.show_toast(f"Painted {os.path.basename(texture_path)}")
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
        if hasattr(self.editor, 'state') and hasattr(self.editor.state, 'terrain_data'):
            self.editor.state.terrain_data = terrain.to_dict()
        self.update()

    def get_ray_from_mouse(self, mx, my):
        w, h = self.width(), self.height()
        if w == 0 or h == 0:
            return glm.vec3(0), glm.vec3(0, 0, 1)
        ndc_x = (2.0 * mx / w) - 1.0
        ndc_y = 1.0 - (2.0 * my / h)
        clip = glm.vec4(ndc_x, ndc_y, -1.0, 1.0)
        inv_proj = glm.inverse(self.projection_matrix)
        eye = inv_proj * clip
        eye = glm.vec4(eye.x, eye.y, -1.0, 0.0)
        inv_view = glm.inverse(self.view_matrix)
        world = inv_view * eye
        ray_dir = glm.normalize(glm.vec3(world))
        ec = self.logic_thread.camera.get_editor_camera()
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
        for brush in self.editor.state.brushes:
            if brush.get('hidden', False) or brush.get('lock', False):
                continue
            pos = glm.vec3(brush.get('pos', [0, 0, 0]))
            size = glm.vec3(brush.get('size', [64, 64, 64]))
            bmin, bmax = pos - size/2, pos + size/2
            tmin, tmax = 0.0, float('inf')
            hit = True
            for i in range(3):
                if abs(ray_d[i]) < 1e-6:
                    if ray_o[i] < bmin[i] or ray_o[i] > bmax[i]:
                        hit = False
                        break
                else:
                    t1 = (bmin[i] - ray_o[i]) / ray_d[i]
                    t2 = (bmax[i] - ray_o[i]) / ray_d[i]
                    if t1 > t2:
                        t1, t2 = t2, t1
                    tmin = max(tmin, t1)
                    tmax = min(tmax, t2)
                    if tmin > tmax:
                        hit = False
                        break
            if hit:
                hits.append((tmin, brush))
                if tmin < best_t:
                    best_t = tmin
                    best_obj = brush
        for thing in self.editor.state.things:
            if not component_edit.is_selectable(thing):
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
                                             self.editor.primary_selection())
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
        for brush in self.editor.state.brushes:
            if brush.get('hidden', False):
                continue
            # Angled (clipped) brushes: pick against their real convex faces so
            # the sloped cut face is selectable, not just the six sides of the
            # bounding box.  Plain box brushes stay on the fast AABB path below.
            if brush_geometry.brush_has_geometry(brush):
                convex = brush_geometry.get_convex(brush)
                if convex is not None and convex.is_valid:
                    hit = brush_geometry.ray_convex_face(convex, ray_o_t, ray_d_t)
                    if hit is not None and hit[0] < best_t:
                        best_t = hit[0]
                        best_hit = (brush, brush_geometry.face_key(hit[1]))
                    continue
            pos = glm.vec3(brush.get('pos', [0, 0, 0]))
            size = glm.vec3(brush.get('size', [64, 64, 64]))
            bmin, bmax = pos - size/2, pos + size/2
            tmin_b, tmax_b = 0.0, float('inf')
            hit = True
            for i in range(3):
                if abs(ray_d[i]) < 1e-6:
                    if ray_o[i] < bmin[i] or ray_o[i] > bmax[i]:
                        hit = False
                        break
                else:
                    t1 = (bmin[i] - ray_o[i]) / ray_d[i]
                    t2 = (bmax[i] - ray_o[i]) / ray_d[i]
                    if t1 > t2:
                        t1, t2 = t2, t1
                    tmin_b = max(tmin_b, t1)
                    tmax_b = min(tmax_b, t2)
                    if tmin_b > tmax_b:
                        hit = False
                        break
            if not hit or tmin_b >= best_t:
                continue
            t_box = tmin_b
            hit_pt = ray_o + ray_d * t_box
            local = hit_pt - pos
            rel = glm.abs(local) / size
            face = 'north'
            if rel.x > rel.y and rel.x > rel.z:
                face = 'east' if local.x > 0 else 'west'
            elif rel.y > rel.x and rel.y > rel.z:
                face = 'top' if local.y > 0 else 'down'
            else:
                face = 'north' if local.z > 0 else 'south'
            best_t = t_box
            best_hit = (brush, face)
        return best_hit

    # ======================================================================
    # Component editing in the 3D view (object / face / edge / vertex)
    # ======================================================================
    #
    # All of this runs from mouse events.  Picking tests only the brushes in
    # the current selection and reuses the geometry cache the renderer already
    # keeps, so nothing here touches the render loop or the wider scene.

    def _components(self):
        return self.editor.components

    def _component_mode_active(self):
        controller = self._components()
        return controller is not None and controller.is_component_mode()

    def _component_targets(self):
        """Brushes a component pick may test — the selection, never the scene."""
        getter = getattr(self.editor, 'component_drag_targets', None)
        return getter() if getter is not None else []

    def _world_per_pixel(self, distance):
        """World units one screen pixel covers ``distance`` from the eye."""
        height = max(self.height(), 1)
        fov = float(self.camera.fov)
        return 2.0 * max(distance, 1.0) * math.tan(math.radians(fov) * 0.5) / height

    def _pick_component_3d(self, mx, my):
        """Component under the cursor in this viewport, or ``None``."""
        controller = self._components()
        targets = self._component_targets()
        if controller is None or not targets:
            return None
        ray_o, ray_d = self.get_ray_from_mouse(mx, my)
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
        self.editor.save_state()      # checkpoint at mouse-down, like 2D
        self.component_drag_origin = QPoint(press_pos)
        self.component_drag_anchor = np.array(ref.position, dtype=np.float64)
        self.component_drag_kind = ref.kind

        # Screen-to-world mapping, fixed for the whole drag so the geometry
        # cannot drift under a moving camera.
        right = glm.vec3(self.view_matrix[0][0], self.view_matrix[1][0],
                         self.view_matrix[2][0])
        up = glm.vec3(self.view_matrix[0][1], self.view_matrix[1][1],
                      self.view_matrix[2][1])
        self.component_drag_axes = (self._axialize(right), self._axialize(up))
        camera_pos = self._camera_position()
        distance = float(np.linalg.norm(self.component_drag_anchor - camera_pos))
        self.component_drag_scale = self._world_per_pixel(distance)
        self.setCursor(Qt.SizeAllCursor)
        self.update()
        return True

    def _component_snap_grid(self):
        """Grid step a component drag in this viewport snaps to, or 0.

        Deliberately the editor's *snap* setting, not this viewport's grid
        *visibility*: a drag used to snap here whenever the 3D grid happened to
        be drawn, so turning "snap to grid" off left the 3D viewport still
        snapping and hiding the grid silently stopped it — the same gesture
        behaving differently depending on which view it started in.
        """
        getter = getattr(self.editor, 'component_grid_step', None)
        if getter is not None:
            return getter(self.grid_size)
        return self.grid_size

    def _camera_position(self):
        pos = self.logic_thread.camera.get_editor_camera().pos
        return np.array([float(pos.x), float(pos.y), float(pos.z)])

    def _update_component_drag(self, pos):
        """Apply the drag for the cursor's current screen position."""
        controller = self._components()
        if controller is None or controller.drag is None:
            return
        if self.component_drag_origin is None or self.component_drag_axes is None:
            return
        dx = pos.x() - self.component_drag_origin.x()
        dy = pos.y() - self.component_drag_origin.y()
        axis_x, axis_y = self.component_drag_axes
        delta = (axis_x * (dx * self.component_drag_scale) +
                 axis_y * (-dy * self.component_drag_scale))
        grid = self._component_snap_grid()
        if self.component_drag_kind in (component_edit.MODE_VERTEX,
                                        component_edit.MODE_EDGE) and \
                self.component_drag_anchor is not None:
            delta = component_edit.snap_component_delta(
                self.component_drag_anchor, delta, grid)
        else:
            delta = component_edit.snap_delta(delta, grid)
        if controller.update_drag(delta):
            self.editor.refresh_views()
        else:
            self.update()

    def _end_component_drag(self):
        """Commit the drag, dropping the checkpoint when nothing moved."""
        controller = self._components()
        if controller is None or controller.drag is None:
            return False
        changed = controller.commit_drag()
        self.component_drag_origin = None
        self.component_drag_axes = None
        self.component_drag_anchor = None
        self.component_drag_kind = None
        self.setCursor(Qt.ArrowCursor)
        if changed:
            self.editor.unsaved_changes = True
            self.editor.state.mark_lighting_dirty()
        else:
            self.editor.state.discard_last_checkpoint()
        self.editor.refresh_views()
        return changed

    def cancel_component_drag(self):
        controller = self._components()
        if controller is None or controller.drag is None:
            return False
        controller.cancel_drag()
        self.editor.state.discard_last_checkpoint()
        self.component_drag_origin = None
        self.component_drag_axes = None
        self.component_drag_anchor = None
        self.component_drag_kind = None
        self.setCursor(Qt.ArrowCursor)
        self.editor.refresh_views()
        return True

    def _begin_face_mode_drag(self, pos):
        """Turn an armed Face-Mode press into a face-plane drag.

        Face Mode stays the texturing tool it has always been — a click still
        applies the selected texture — but dragging from the same press moves
        the face's supporting plane, so a wall can be pushed into place without
        leaving the mode.
        """
        armed = self._face_mode_press
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
        self._face_mode_press = None
        return self._begin_component_drag(ref, armed['pos'],
                                          shear=armed['shear'])

    def mousePressEvent(self, event):
        if self.play_mode and self._actor_pick is not None:
            if event.button() == Qt.RightButton:
                self.cancel_actor_pick()
            elif event.button() == Qt.LeftButton:
                self._click_actor_pick(event.x(), event.y())
            return
        if (self.play_mode and getattr(self, '_cached_level_complete_ui', None)
                and getattr(self, '_level_complete_btn_rect', None)):
            if self._level_complete_btn_rect.contains(event.pos()):
                self._confirm_level_complete()
                return

        if self.terrain_sculpt_active and not self.play_mode and event.button() == Qt.LeftButton:
            self._terrain_brush_mouse_pos = event.pos()
            self._terrain_brush_hit = self.raycast_terrain(event.x(), event.y())
            self._apply_sculpt_at_mouse(event.x(), event.y())
            return
        if not self.play_mode and self.floating_windows.handle_mouse_press(event):
            if any(getattr(w, "dragging", False) for w in self.floating_windows.windows):
                self.setCursor(Qt.ClosedHandCursor)
            return
        if self.sysmon.handle_mouse_press(event, self.play_mode):
            if self.sysmon.dragging:
                self.setCursor(Qt.ClosedHandCursor)
            return
        # A left click here also drops copies being carried by the cursor, so
        # a clone started in a 2D view can be committed from the 3D view too.
        if (not self.play_mode and event.button() == Qt.LeftButton and
                getattr(self.editor, 'clone_placement_active', None) is not None and
                self.editor.clone_placement_active()):
            self.editor.finish_clone_placement()
            return

        # --- Component modes: the press grabs a vertex / edge / face ---
        # Same contextual rules as the 2D views: plain drags the component,
        # Shift adds it to the component selection, Ctrl on a face shears it.
        if (not self.play_mode and event.button() == Qt.LeftButton and
                self._component_mode_active()):
            ref = self._pick_component_3d(event.x(), event.y())
            if ref is not None:
                modifiers = QApplication.keyboardModifiers()
                shear = bool(modifiers & Qt.ControlModifier) and \
                    ref.kind == component_edit.MODE_FACE
                additive = bool(modifiers & Qt.ShiftModifier)
                if self._begin_component_drag(ref, event.pos(), shear=shear,
                                              additive=additive):
                    return

        if self.face_mode_active and event.button() == Qt.LeftButton:
            if self.hovered_face_info:
                # Arm the press: a drag from here moves the face's plane, a
                # click without movement textures it (the original behaviour,
                # decided on release).
                self._face_mode_press = {
                    'face': self.hovered_face_info,
                    'pos': event.pos(),
                    'shear': bool(QApplication.keyboardModifiers() &
                                  Qt.ControlModifier),
                }
            return
        if event.button() == Qt.LeftButton and QApplication.keyboardModifiers() == Qt.ControlModifier and not self.play_mode:
            face = self.get_face_at(event.pos())
            if face:
                self.editor.selected_face = face
                self.update()
            return
        if self.play_mode and event.button() == Qt.LeftButton:
            if self.console_overlay_active:
                return
            published = self.game_state.published
            if published('player_dead', False):
                return
            active_weapon = published('active_weapon')
            if active_weapon:
                from engine.monster_constants import NON_FIRING_WEAPONS
                # Non-firing weapons (e.g. cig) are display-only. For firing
                # weapons, the published shot_ready flag prevents clicks from
                # piling up while gun2 is cooling down or out of ammo.
                if (
                    active_weapon not in NON_FIRING_WEAPONS
                    and published('shot_ready', False)
                ):
                    self.game_state.queue_shot()
                return
        _shift_select = (Qt.ShiftModifier, Qt.ShiftModifier | Qt.AltModifier)
        if (event.button() == Qt.LeftButton and not self.play_mode and
                QApplication.keyboardModifiers() in _shift_select):
            # Shift+click selects; adding Alt cycles down through whatever else
            # the ray passes through, so buried geometry stays reachable.
            cycle = bool(QApplication.keyboardModifiers() & Qt.AltModifier)
            obj = self.get_object_at_3d(event.x(), event.y(), cycle=cycle)
            if obj:
                self.editor.save_state()
                self.editor.set_selected_objects([obj])
                self.update()
            return
        if event.button() == Qt.LeftButton and self.editor.primary_selection() and not self.play_mode:
            obj_pos = self.get_selected_object_pos()
            if obj_pos:
                ray_o, ray_d = self.get_ray_from_mouse(event.x(), event.y())
                best_dist = float('inf')
                hit_axis = None
                start_pt = None
                for axis, vec in [('x', glm.vec3(1,0,0)), ('y', glm.vec3(0,1,0)), ('z', glm.vec3(0,0,1))]:
                    pt, dist = self.intersect_ray_with_axis(ray_o, ray_d, obj_pos, vec)
                    if pt and dist < 1.5 and glm.distance(pt, obj_pos) < 40.0:
                        if dist < best_dist:
                            best_dist = dist
                            hit_axis = axis
                            start_pt = pt
                if hit_axis:
                    self.editor.save_state()
                    self.is_dragging_gizmo = True
                    self.gizmo_drag_axis = hit_axis
                    self.gizmo_object_start_pos = obj_pos
                    self.drag_start_on_axis = start_pt
                    self.setCursor(Qt.ClosedHandCursor)
                    return
        if not self.play_mode and event.button() == Qt.RightButton:
            self.mouselook_active = True
            self.last_mouse_pos = event.pos()
            self.setCursor(Qt.BlankCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self.play_mode and self.floating_windows.handle_mouse_move(
                event, self.width(), self.height()):
            self.update()
            return
        if self.sysmon.handle_mouse_move(event, self.play_mode, self.width(), self.height()):
            self.update()
            return
        # A component drag owns the mouse until the button comes back up.
        controller = self._components()
        if controller is not None and controller.drag is not None:
            if event.buttons() & Qt.LeftButton:
                self._update_component_drag(event.pos())
                return
            self._end_component_drag()
        # Face Mode: a press that has moved far enough becomes a face drag.
        if self._face_mode_press is not None:
            if not (event.buttons() & Qt.LeftButton):
                self._face_mode_press = None
            elif (event.pos() - self._face_mode_press['pos']).manhattanLength() > 4:
                if self._begin_face_mode_drag(event.pos()):
                    return
                self._face_mode_press = None
        if (self.terrain_sculpt_active and not self.play_mode
                and not (event.buttons() & Qt.RightButton)):
            self._terrain_brush_mouse_pos = event.pos()
            self._terrain_brush_hit = self.raycast_terrain(event.x(), event.y())
            self.update()
            return
        if self.mouselook_active:
            dx, dy = event.x() - self.last_mouse_pos.x(), event.y() - self.last_mouse_pos.y()
            if self.logic_thread:
                self.game_state.set_mouse_delta(float(dx), float(dy))
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)
            return
        if self.play_mode:
            if self.console_overlay_active:
                return
            if self._actor_pick is not None:
                self._update_actor_pick_hover(event.x(), event.y())
                return
            cp = event.pos()
            dx, dy = cp.x() - self.last_mouse_pos.x(), cp.y() - self.last_mouse_pos.y()
            if dx == 0 and dy == 0:
                return
            self.game_state.set_mouse_delta(float(dx), float(dy))
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)
            return
        if (self.terrain_sculpt_painting and self.terrain_sculpt_active
                and self.terrain_sculpt_mode != 'stamp'):
            self._apply_sculpt_at_mouse(event.x(), event.y())
            return
        if self.is_dragging_gizmo:
            ray_o, ray_d = self.get_ray_from_mouse(event.x(), event.y())
            axis_vec = {'x': glm.vec3(1,0,0), 'y': glm.vec3(0,1,0), 'z': glm.vec3(0,0,1)}[self.gizmo_drag_axis]
            pt, _ = self.intersect_ray_with_axis(ray_o, ray_d, self.gizmo_object_start_pos, axis_vec)
            if pt:
                diff = pt - self.drag_start_on_axis
                self.set_selected_object_pos(self.gizmo_object_start_pos + diff)
            return
        # Component hover: highlight what a press would grab.  Selection-scoped
        # and repainted only when the highlight actually changes.
        if self._component_mode_active() and not event.buttons():
            if controller.set_hover(self._pick_component_3d(event.x(), event.y())):
                self.update()
            return
        if self.face_mode_active:
            self.hovered_face_info = self.get_brush_face_at_coords(event.x(), event.y())
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        controller = self._components()
        if (event.button() == Qt.LeftButton and controller is not None and
                controller.drag is not None):
            self._end_component_drag()
            return
        if event.button() == Qt.LeftButton and self._face_mode_press is not None:
            # Released without dragging: this was a texture click after all.
            brush, face = self._face_mode_press['face']
            self._face_mode_press = None
            self.editor.apply_texture_to_specific_face(brush, face)
            if hasattr(self.editor, 'show_surface_inspector'):
                self.editor.show_surface_inspector(brush, face)
            return
        if not self.play_mode and self.floating_windows.handle_mouse_release(event):
            self.setCursor(Qt.ArrowCursor)
            return
        if self.sysmon.handle_mouse_release(event, self.play_mode):
            self.setCursor(Qt.ArrowCursor)
            return
        if self.is_dragging_gizmo:
            self.is_dragging_gizmo = False
            self.setCursor(Qt.ArrowCursor)
            self.editor.save_state()
        if self.mouselook_active and event.button() == Qt.RightButton:
            self.mouselook_active = False
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if self.terrain_sculpt_active and not self.play_mode:
            self._terrain_brush_mouse_pos = QPoint(-1, -1)
            self._terrain_brush_hit = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if not self.play_mode:
            self.camera.fov = np.clip(self.camera.fov - event.angleDelta().y() * 0.05, 30, 120)
            self.editor.update_views()

    def get_face_at(self, mouse_pos):
        if not isinstance(self.editor.primary_selection(), dict):
            return None
        brush = self.editor.primary_selection()
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

    def _load_gun_hud_pixmap(self, gun_type):
        if gun_type in self.gun_hud_pixmaps:
            return self.gun_hud_pixmaps[gun_type]
        path = os.path.join('assets', 'sprites', f'{gun_type}HUD.png')
        if os.path.exists(path):
            pixmap = QPixmap(path)
            self.gun_hud_pixmaps[gun_type] = pixmap
            return pixmap
        return None

    def _load_gun_flash_pixmap(self, gun_type):
        if gun_type in self.gun_flash_pixmaps:
            return self.gun_flash_pixmaps[gun_type]
        path = os.path.join('assets', 'sprites', f'{gun_type}HUD_flash.png')
        if os.path.exists(path):
            pixmap = QPixmap(path)
            self.gun_flash_pixmaps[gun_type] = pixmap
            return pixmap
        return None

    def _load_weapon_collect_pixmap(self, item_type):
        """The world/collectible sprite for a weapon (e.g. 'gun1' -> gun1.png).

        Used by the overhead HUD, which shows the small collectible icon bottom-right
        instead of the first-person gun sprite. Resolved via the Prop
        GUN_SPRITES map so it matches what the weapon looks like in the world.
        """
        if item_type in self.weapon_collect_pixmaps:
            return self.weapon_collect_pixmaps[item_type]
        rel = None
        try:
            from engine.prop_entity import Prop
            rel = Prop.GUN_SPRITES.get(item_type)
        except Exception:
            rel = None
        if not rel:
            rel = os.path.join('assets', 'sprites', f'{item_type}.png')
        pixmap = QPixmap(rel) if os.path.exists(rel) else None
        self.weapon_collect_pixmaps[item_type] = pixmap
        return pixmap

    def eventFilter(self, obj, event):
        if obj is self._console_input and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._close_console_overlay()
                return True
        return super().eventFilter(obj, event)

    def _open_console_overlay(self):
        self.console_overlay_active = True
        QApplication.setOverrideCursor(Qt.ArrowCursor)
        w, h = self.width(), self.height()
        self._console_input.setGeometry(0, h - 36, w, 36)
        self._console_input.show()
        self._console_input.raise_()
        self._console_input.setFocus()
        self._console_input.clear()

    def _close_console_overlay(self):
        self.console_overlay_active = False
        self._console_input.hide()
        self._console_input.clearFocus()
        self.setFocus()
        if self.play_mode:
            if self._actor_pick is not None:
                # A command just armed a pick: the cursor stays free for it.
                self._show_pick_cursor()
            else:
                self._capture_play_cursor()

    def _capture_play_cursor(self):
        """Hide the cursor and re-centre it for mouse look (Play Mode)."""
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        QApplication.setOverrideCursor(Qt.BlankCursor)
        center = self.mapToGlobal(self.rect().center())
        QCursor.setPos(center)
        self.last_mouse_pos = self.mapFromGlobal(center)

    def _show_pick_cursor(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        QApplication.setOverrideCursor(Qt.CrossCursor)

    # =========================================================================
    # ACTOR PICK (Play Mode)
    # =========================================================================

    #: The world-pause owner key an armed pick holds.
    ACTOR_PICK_PAUSE = "actor_pick"

    @property
    def actor_pick_active(self) -> bool:
        return self._actor_pick is not None

    def begin_actor_pick(self, on_pick=None) -> bool:
        """Arm a one-shot click-to-pick of an actor in Play Mode.

        The world pauses (``LogicThread.session_runtime.set_world_paused``) so the actor holds
        still, and the cursor is freed. The next left-click on an actor ends
        the pick and calls ``on_pick(entity)``; by default that opens the
        Entity Inspector on it. A click on nothing keeps the pick armed; Esc
        or a right-click cancels it. Returns False outside Play Mode.
        """
        if not self.play_mode:
            return False
        self._actor_pick = {'on_pick': on_pick}
        self.actor_pick_hover = None
        logic = self.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(self.ACTOR_PICK_PAUSE, True)
        if not self.console_overlay_active:
            self._show_pick_cursor()
        self.update()
        return True

    def cancel_actor_pick(self):
        """Disarm a pick without choosing anything."""
        self._end_actor_pick()

    def _end_actor_pick(self):
        if self._actor_pick is None:
            return
        self._actor_pick = None
        self.actor_pick_hover = None
        logic = self.logic_thread
        if logic is not None:
            logic.session_runtime.set_world_paused(self.ACTOR_PICK_PAUSE, False)
        if self.play_mode and not self.console_overlay_active:
            self._capture_play_cursor()
        self.update()

    def _pick_ray(self, mx, my):
        """World ray through pixel (mx, my) of the frame last drawn.

        Origin and direction both come from the matrices that frame was
        drawn with, so the ray leaves the camera the player is looking
        through -- first person or overhead -- not the editor camera.
        """
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return None
        ndc_x = (2.0 * mx / w) - 1.0
        ndc_y = 1.0 - (2.0 * my / h)
        inv_view = glm.inverse(self.view_matrix)
        eye = glm.inverse(self.projection_matrix) * glm.vec4(ndc_x, ndc_y, -1.0, 1.0)
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
        render_state = self.game_state.get_render_state() if self.logic_thread else None
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
                self.game_state.release_render_state(render_state)

    def _update_actor_pick_hover(self, mx, my):
        hovered = self.actor_at(mx, my)
        if hovered is not self.actor_pick_hover:
            self.actor_pick_hover = hovered
            self.update()

    def _click_actor_pick(self, mx, my) -> bool:
        """Resolve an armed pick at the clicked pixel. True if it ended."""
        actor = self.actor_at(mx, my)
        if actor is None:
            return False
        on_pick = self._actor_pick.get('on_pick') or self._open_inspector_for
        self._end_actor_pick()
        try:
            on_pick(actor)
        except Exception as exc:
            debug_log("Error", f"actor pick handler failed: {exc}")
        return True

    def _open_inspector_for(self, actor):
        show = getattr(self.editor, 'show_entity_inspector', None)
        if show is not None:
            show(actor)

    def _draw_actor_pick_hint(self, painter, viewport_width):
        hovered = self.actor_pick_hover
        name = ""
        if hovered is not None:
            props = getattr(hovered, 'properties', {}) or {}
            name = str(props.get('name') or props.get('type') or "")
        text = (f"Inspect: {name} - click to open (Esc to cancel)" if name
                else "Inspect (paused): click an actor (Esc to cancel)")
        metrics = QFontMetrics(self._hud_msg_font)
        tw = metrics.horizontalAdvance(text)
        x, y = viewport_width // 2 - tw // 2, 40
        painter.setFont(self._hud_msg_font)
        painter.setPen(self._hud_shadow_pen)
        painter.drawText(x + 2, y + 2, text)
        painter.setPen(self._hud_grey_pen)
        painter.drawText(x, y, text)

    def _submit_console_command(self):
        cmd = self._console_input.text().strip()
        if cmd and self.debug_console_window:
            self.debug_console_window.command_input.setText(cmd)
            self.debug_console_window._on_command_entered()
        self._close_console_overlay()


    def _update_p2_keyboard_input(self):
        """If no gamepad is connected and split‑screen is active, read arrow keys and send P2 input.
        Up/Down = forward/backward, Left/Right = turn left/right (no strafing)."""
        if not self.play_mode or not self.splitscreen_mode or self.gamepad:
            return

        move_z = 0.0
        look_dx = 0.0
        if 'up' in self.p2_keys_pressed:
            move_z = 1.0
        if 'down' in self.p2_keys_pressed:
            move_z = -1.0
        if 'left' in self.p2_keys_pressed:
            look_dx = -1.0  # turn left (negative yaw change)
        if 'right' in self.p2_keys_pressed:
            look_dx = 1.0   # turn right

        # No strafing (move_x = 0), no look up/down (look_dy = 0)
        move_x = 0.0
        look_dy = 0.0
        jump = False
        crouch = False

        self.game_state.set_p2_input(move_x, move_z, look_dx, look_dy, jump, crouch)

    def keyPressEvent(self, event):
        # An armed actor pick owns Escape: it cancels the pick, not Play Mode.
        if (event.key() == Qt.Key_Escape and self.play_mode
                and self._actor_pick is not None):
            self.cancel_actor_pick()
            return
        # Sculpt mode owns Escape while active. Do this before normal
        # editor/play-mode escape handling.
        if event.key() == Qt.Key_Escape and self.terrain_sculpt_active and not self.play_mode:
            self.set_terrain_sculpt_active(False)
            self.setFocus()
            return

        if self.play_mode and getattr(self, '_cached_level_complete_ui', None):
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_E):
                self._confirm_level_complete()
                return
            elif event.key() == Qt.Key_Escape:
                self._cancel_level_complete()
                return

        # ----- Player 2 arrow key handling (when no gamepad) -----
        if self.play_mode and not self.gamepad and self.splitscreen_mode:
            if event.key() == Qt.Key_Up:
                self.p2_keys_pressed.add('up')
                return
            elif event.key() == Qt.Key_Down:
                self.p2_keys_pressed.add('down')
                return
            elif event.key() == Qt.Key_Left:
                self.p2_keys_pressed.add('left')
                return
            elif event.key() == Qt.Key_Right:
                self.p2_keys_pressed.add('right')
                return

        def check_key(cfg_key, default):
            key_str = self.editor.config.get('Shortcuts', cfg_key, fallback=default)
            seq = QKeySequence(key_str)
            return QKeySequence(event.key() | int(event.modifiers())) == seq

        if self.play_mode and check_key('key_console', '`'):
            if self.console_overlay_active:
                self._close_console_overlay()
            else:
                self._open_console_overlay()
            return
        if self.console_overlay_active:
            return
        if check_key('key_show_connections', 'F1'):
            current_state = self.editor.show_logic_links
            if hasattr(self.editor, 'set_connection_links_enabled'):
                self.editor.set_connection_links_enabled(not current_state)
            else:
                self.editor.show_logic_links = not current_state
                self.editor.update_views()
            return
        if check_key('key_toggle_wireframe', 'F2'):
            if self.current_render_mode == RENDER_MODE_WIREFRAME:
                self.current_render_mode = RENDER_MODE_LIT
            else:
                self.current_render_mode = RENDER_MODE_WIREFRAME
            mode_name = self.render_mode_names.get(self.current_render_mode, "Unknown")
            if hasattr(self.editor, 'show_toast'):
                self.editor.show_toast(f"Render Mode: {mode_name}")
            self.update()
            return
        if check_key('key_sysmon', 'F3'):
            if hasattr(self.editor, 'toggle_system_monitor'):
                self.editor.toggle_system_monitor()
            else:
                self.sysmon.toggle()
                self.update()
            return
        if self.play_mode and event.key() == Qt.Key_F7:
            self.monster_debug_active = not self.monster_debug_active
            if self.logic_thread:
                self.logic_thread.monster_debug_active = self.monster_debug_active
            if hasattr(self.editor, 'show_toast'):
                status = "ON" if self.monster_debug_active else "OFF"
                self.editor.show_toast(f"Monster Debug: {status}")
            self.update()
            return
        if self.play_mode and event.key() == Qt.Key_F6:
            if self.logic_thread:
                new_state = self.logic_thread.collision_runtime.toggle_model_collision()
                status = "ON" if new_state else "OFF"
                if hasattr(self.editor, 'show_toast'):
                    self.editor.show_toast(f"Model Collision: {status}")
            self.update()
            return
        if self.play_mode and event.key() == Qt.Key_F9:
            self._toggle_splitscreen()
            return
        if self.play_mode and event.key() == Qt.Key_F12:
            if getattr(self.editor, 'is_kiosk_mode', False):
                self.editor.exit_kiosk_mode(keep_play_mode=True)
            else:
                self.editor.enter_kiosk_mode()
            return
        if self.play_mode:
            if self.game_state.published('player_dead', False):
                if event.key() == Qt.Key_Escape:
                    self._exit_play_mode()
                    return
                return
        if not self.play_mode:
            if event.key() == Qt.Key_BracketLeft:
                if hasattr(self.editor, 'set_grid_size'):
                    new_size = max(2, self.grid_size // 2)
                    self.editor.set_grid_size(new_size)
                    if hasattr(self.editor, 'show_toast'):
                        self.editor.show_toast(f"Grid Size: {new_size}")
                return
            elif event.key() == Qt.Key_BracketRight:
                if hasattr(self.editor, 'set_grid_size'):
                    new_size = min(128, self.grid_size * 2)
                    self.editor.set_grid_size(new_size)
                    if hasattr(self.editor, 'show_toast'):
                        self.editor.show_toast(f"Grid Size: {new_size}")
                return
        if self.play_mode:
            if self.show_render_menu:
                if event.key() == Qt.Key_1:
                    self.current_render_mode = RENDER_MODE_LIT
                    self.update()
                elif event.key() == Qt.Key_2:
                    self.current_render_mode = RENDER_MODE_UNLIT
                    self.update()
                elif event.key() == Qt.Key_3:
                    self.current_render_mode = RENDER_MODE_WIREFRAME
                    self.update()
                elif event.key() == Qt.Key_4:
                    self.current_render_mode = RENDER_MODE_VERTEX
                    self.update()
                elif event.key() == Qt.Key_Escape:
                    self.show_render_menu = False
                    self.update()
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        # Remove arrow keys from the set when released
        if self.play_mode and not self.gamepad and self.splitscreen_mode:
            if event.key() == Qt.Key_Up:
                self.p2_keys_pressed.discard('up')
                return
            elif event.key() == Qt.Key_Down:
                self.p2_keys_pressed.discard('down')
                return
            elif event.key() == Qt.Key_Left:
                self.p2_keys_pressed.discard('left')
                return
            elif event.key() == Qt.Key_Right:
                self.p2_keys_pressed.discard('right')
                return
        super().keyReleaseEvent(event)