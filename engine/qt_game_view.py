import time
import os
import math
import numpy as np
import ctypes
from collections import deque
from typing import Optional
from PyQt5.QtWidgets import QOpenGLWidget, QApplication, QLineEdit
from PyQt5.QtCore import Qt, QTimer, QPoint, QUrl, QRect, QEvent
from PyQt5.QtGui import QPainter, QColor, QFont, QCursor, QFontDatabase, QPen, QBrush, QPolygon, QKeySequence, QPixmap, QSurfaceFormat, QFontMetrics, QImage, QLinearGradient
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
import glm
from engine.camera import Camera
from editor.things import (
    Thing, Light, PlayerStart, Monster, Pickup, Speaker,
    LogicGate, LogicRelay, LogicTimer, LevelChanger, Portal
)
from engine.player import Player
from PIL import Image

from .renderer_F   import Renderer_F
_RENDERER_CLASSES = {
    'Forward':  Renderer_F,
}


def register_renderer(name, cls):
    """Register a swappable renderer class under *name* (used by ``switch_renderer``).

    This is the plugin-facing seam for shipping a whole new renderer (e.g. a
    deferred one) without editing the engine: a plugin calls
    ``api.register_renderer("Deferred", DeferredRenderer)`` and it becomes an
    available render mode. *cls* must implement the renderer interface the
    viewport drives (``render_scene``, ``draw_models``, ``render_shadow_maps``,
    ``set_sprite_textures``, ``cleanup``, a ``lod_manager``, …). Returns True.
    """
    _RENDERER_CLASSES[str(name)] = cls
    return True


def available_renderers():
    """The names of all registered renderer modes."""
    return list(_RENDERER_CLASSES.keys())

from engine import shaders
from engine import brush_geometry
from engine.threaded_game_state import ThreadedGameState, RenderState
from engine.logic_thread import LogicThread
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.debug_console import DebugConsole, get_debug_logger
from .sysmon import SysMon

# Pygame for gamepad support
import pygame

# System monitoring (pure Python, no external deps)
import ctypes
import os
import platform

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
    def __init__(self, editor):
        super().__init__(editor)

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
        self.selected_object = None
        self.show_sprites_in_play_mode = False
        # Cache keys for per-frame expensive rebuilds
        self._instance_tex_hash   = None   # hash of last things-state snapshot
        self._io_conn_cache       = None   # last _gather_io_connections result
        self._io_conn_scene_ver   = None   # (len(brushes), len(things)) when cache was built
        self.visibility_system = None
        self.show_visibility_debug = False
        self.grid_visible = True
        self.sysmon = SysMon(self)


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
        self.use_threading = True
        self._thread_started = False

        self.face_mode_active = False
        self.hovered_face_info = None

        self.mouselook_active = False
        self.last_mouse_pos = QPoint()







        self._init_hud_caches()

        self.texture_manager = {}
        self.sprite_textures = {}
        self.gun_hud_pixmaps = {}
        self.gun_flash_pixmaps = {}
        self.weapon_pickup_pixmaps = {}   # item_type -> world/pickup QPixmap
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
        self.projection_matrix = glm.mat4(1.0)
        self.view_matrix = glm.mat4(1.0)
        self._cached_aspect_ratio = 1.0
        self.cull_distance = 4096

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

    def _ensure_pygame_mixer(self) -> bool:
        """Initialize pygame mixer if it isn't already active."""
        if pygame.mixer.get_init():
            return True
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            print("[Audio] pygame.mixer late-initialized")
            return True
        except pygame.error as e:
            print(f"[Audio] pygame.mixer init failed: {e}")
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
        
        # Try to load on-demand
        path = os.path.join(os.getcwd(), 'assets', 'sounds', clean_name)
        if os.path.exists(path):
            if self._load_sound_to_cache(clean_name, path):
                return self.sound_pool[clean_name]
        
        print(f"[Audio] Sound not found: {clean_name}")
        return None
       


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
        self._face_mode_font_top = QFont("Arial", 14, QFont.Bold)
        self._face_mode_font_bot = QFont("Arial", 10, QFont.Bold)

        self._hud_bar_bg_pen = QPen(QColor(60, 60, 60), 2)
        self._hud_bar_bg_brush = QBrush(QColor(40, 40, 40, 200))
        self._hud_health_green = QColor(50, 200, 50)
        self._hud_health_yellow = QColor(255, 200, 50)
        self._hud_health_red = QColor(200, 50, 50)
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
        self._cached_gun_hud = {}
        self._cached_weapon_pickup = {}   # (item_type, size) -> scaled QPixmap
        self._cached_key_pixmaps = {}
        self._cached_key_size = 100

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
        lt = getattr(self, "logic_thread", None)
        if lt is not None and hasattr(lt, "set_camera_mode"):
            lt.set_camera_mode(self.camera_mode)
        self.update()

    def _is_overhead(self) -> bool:
        # PERF: cached — recompute only when camera_mode changes.
        cm = getattr(self, "camera_mode", "")
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
        if not (self.play_mode and self.overhead_sprite_enabled and self._is_overhead()):
            return
        if render_state is None:
            return
        lt = getattr(self, "logic_thread", None)
        if lt is not None and getattr(lt, "cinematic_state", None):
            return
        # Suppress the ground sprite mid-tween so it doesn't pop in/out while the
        # camera swoops between first-person and overhead.
        if getattr(render_state, "camera_transition_active", False):
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

        pos = getattr(render_state, "player_pos", None)
        if pos is None:
            return
        try:
            gpos = (float(pos.x), float(pos.y), float(pos.z))
        except AttributeError:
            gpos = (float(pos[0]), float(pos[1]), float(pos[2]))
        angle = float(getattr(render_state, "player_angle", 0.0))
        armed = bool(getattr(render_state, "active_weapon", None))
        shooting = bool(getattr(render_state, "muzzle_flash_active", False))
        self._overhead_sprite_ctrl.update(gpos, angle, time.perf_counter(),
                                          armed=armed, shooting=shooting)
        self._overhead_sprite_renderer.draw(
            self.projection_matrix, self.view_matrix, gpos,
            self._overhead_sprite_ctrl.facing, self._overhead_sprite_ctrl.frame())


    def initializeGL(self):
        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
        config = getattr(self.editor, 'config', None)
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

    def _start_logic_thread(self):
        if self._thread_started:
            return
        self.logic_thread = LogicThread(self.game_state, self.editor.state, self.visibility_system)
        self.logic_thread.set_editor_camera(self.camera.pos, self.camera.yaw, self.camera.pitch, self.camera.fov)
        if hasattr(self.logic_thread, "set_camera_mode"):
            self.logic_thread.set_camera_mode(getattr(self, "camera_mode", "First Person"))
        self.logic_thread.set_play_mode(False)
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
            vp_w = (width // 2) if getattr(self, 'splitscreen_mode', False) else width
            self._cached_aspect_ratio = vp_w / height
        else:
            self._cached_aspect_ratio = 1.0
        if self.logic_thread:
            self.logic_thread.set_frustum_aspect(self._cached_aspect_ratio)
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
        if self.use_threading and self.logic_thread:
            keys = set() if self.console_overlay_active else self.editor.keys_pressed
            self.game_state.set_keys(keys)
            # Update Player 2 input from arrow keys (if no gamepad)
            self._update_p2_keyboard_input()
            has_new = self.game_state.try_swap()
            self.repaint()
            if has_new and self.play_mode:
                self.editor.update_views()
        else:
            self.repaint()

    def _process_sound_queue(self):
        """Drain the logic thread's sound queue and play via pygame mixer."""
        for request in self.game_state.consume_sounds():
            sound_file = request.get('file')
            volume = request.get('volume', 1.0)
            if not sound_file:
                continue
            
            sound = self._get_sound_instance(sound_file)
            if sound:
                # pygame mixer channels auto-manage, but we can set volume per-play
                channel = sound.play()
                if channel:
                    channel.set_volume(volume)

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
                handler.handle_command(cmd)
            except Exception as exc:
                print(f"[QtGameView] console command '{cmd}' failed: {exc}")

    def _gather_io_connections(self):
        COLOR_LOGIC   = (1.0, 1.0, 0.0)
        COLOR_IO      = (0.0, 1.0, 1.0)
        COLOR_PATROL  = (0.15, 0.65, 0.60)
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
                lines.append({'src': node.pos, 'dst': next_node.pos, 'color': COLOR_PATROL})
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

    def _render_projectiles(self, projectiles, proj_matrix, view_matrix):
        if not projectiles or 'sprite' not in self.renderer.shaders:
            return
        tex_id = (self.sprite_textures.get('projectile') or
                  self.sprite_textures.get('Monster'))
        if not tex_id:
            return
        from engine.monster_constants import MONSTER_PROJECTILE_SPRITE_SIZE
        pw, ph = MONSTER_PROJECTILE_SPRITE_SIZE
        shader = self.renderer.shaders['sprite']
        uniforms = self.renderer.uniforms['sprite']
        gl.glUseProgram(shader)
        proj_ptr = glm.value_ptr(proj_matrix)
        view_ptr = glm.value_ptr(view_matrix)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glBindVertexArray(self.renderer.vaos['sprite'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        pos_loc = uniforms['sprite_pos_world']
        size_loc = uniforms['sprite_size']
        for proj in projectiles:
            pos = proj['pos']
            gl.glUniform3f(pos_loc, pos[0], pos[1], pos[2])
            gl.glUniform2f(size_loc, pw, ph)
            gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glBindVertexArray(0)
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
        grid = getattr(self.logic_thread, '_spatial_grid', None)
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
        render_state: Optional[RenderState] = None
        if self.use_threading and self.logic_thread:
            render_state = self.game_state.get_render_state()
            if render_state:
                self._cached_health = render_state.player_health
                self._cached_max_health = render_state.player_max_health
                self._cached_active_weapon = getattr(render_state, 'active_weapon', None)
                self._cached_hud_message = getattr(render_state, 'hud_message', '')
                self._cached_collected_keys = getattr(render_state, 'collected_keys', set())
                if getattr(render_state, 'muzzle_flash_active', False):
                    self._muzzle_flash_counter = self._muzzle_flash_duration_frames
                self._cached_muzzle_flash = self._muzzle_flash_counter > 0
                self._cached_player_dead = getattr(render_state, 'player_dead', False)
                self._cached_monster_debug = getattr(render_state, 'monster_debug_active', False)
                self._cached_bullet_marks = list(getattr(render_state, 'bullet_marks', []))
                self._cached_projectiles = list(getattr(render_state, 'projectiles', []))
                self._cached_monster_rays = list(getattr(render_state, 'monster_debug_rays', []))
                self._cached_level_complete_ui = getattr(render_state, 'level_complete_ui', None)
                self._cached_underwater = getattr(render_state, 'player_underwater', False)
                self._cached_underwater_tint = getattr(render_state, 'underwater_tint', [0.0, 0.4, 0.6])
                self._cached_p2_underwater = getattr(render_state, 'player2_underwater', False)
        if self.grid_dirty:
            self.renderer.update_grid_buffers(self.world_size, self.grid_size)
            self.grid_dirty = False
        if self.use_threading and self.logic_thread:
            self.view_matrix = render_state.camera_view_matrix
            if render_state.is_play_mode:
                camera_pos = render_state.player_pos
            else:
                camera_pos = render_state.editor_camera_pos
            brushes_to_render = render_state.visible_brushes
            things_to_render = render_state.visible_things
            if not self.play_mode:
                self.camera.pos = glm.vec3(render_state.editor_camera_pos)
                self.camera.yaw = render_state.editor_camera_yaw
                self.camera.pitch = render_state.editor_camera_pitch
                self.camera.fov = render_state.editor_camera_fov
            else:
                self.camera.pos = glm.vec3(render_state.player_pos)
                self.camera.yaw = 90.0 - math.degrees(render_state.player_angle)
                self.camera.pitch = math.degrees(render_state.player_pitch)
        else:
            self.view_matrix = self.camera.get_view_matrix()
            camera_pos = self.camera.pos
            brushes_to_render = self.editor.state.brushes
            things_to_render = self.editor.state.things
        # In overhead play mode the camera is lifted far above the scene, so a
        # 0.1 near plane wastes almost all depth precision at ground level and
        # coplanar surfaces z-fight ("flicker"). Nothing sits within a fraction
        # of the camera height of the overhead eye, so pull the near plane out to
        # restore precision. First-person keeps the stock 0.1 near plane.
        _near = 0.1
        if self.play_mode and self._is_overhead():
            _oh = float(getattr(getattr(self, 'logic_thread', None), 'overhead_height', 800.0) or 800.0)
            _near = max(1.0, _oh * 0.1)
        self.projection_matrix = perspective_projection(self.camera.fov, self._cached_aspect_ratio, _near, 10000.0)
        self._proj_ptr = glm.value_ptr(self.projection_matrix)
        self._view_ptr = glm.value_ptr(self.view_matrix)
        self._render_config["culling_enabled"] = self.culling_enabled
        self._render_config["brush_display_mode"] = self.brush_display_mode
        self._render_config["show_triggers_as_solid"] = self.show_triggers_as_solid
        self._render_config["render_mode"] = getattr(self, 'current_render_mode', 0)
        self._render_config["play_mode"] = self.play_mode
        self._render_config["selected_object"] = self.selected_object
        self._render_config["time"] = time.perf_counter() - self.start_time
        self._render_config["show_sprites_in_play_mode"] = self.show_sprites_in_play_mode
        self._render_config["grid_visible"] = getattr(self, 'grid_visible', True) and not self.play_mode
        self._render_config["terrain"] = getattr(self.editor, 'terrain', None)
        if render_state and hasattr(render_state, 'all_brushes'):
            self._render_config["all_brushes"] = render_state.all_brushes
        else:
            self._render_config["all_brushes"] = self.editor.state.brushes
        if render_state and hasattr(render_state, 'all_things'):
            self._render_config["all_things"] = render_state.all_things
        else:
            self._render_config["all_things"] = self.editor.state.things
        self.update_instance_textures(things_to_render)

        # Plugin render hooks. Guarded by has_listeners so an unhooked frame
        # pays a single dict lookup and builds no payload — see the render.*
        # events in the plugin API. The manager handle is fetched once per frame.
        _pmgr = getattr(self.logic_thread, 'plugins', None) \
            if getattr(self, 'logic_thread', None) is not None else None
        if _pmgr is not None and _pmgr.has_listeners("render.pre_scene"):
            _pmgr.emit("render.pre_scene", viewport=self, renderer=self.renderer,
                       projection=self.projection_matrix, view=self.view_matrix,
                       camera_pos=camera_pos, play_mode=self.play_mode)

        _splitscreen = (
            self.play_mode
            and getattr(self, 'splitscreen_mode', False)
            and render_state is not None
            and getattr(render_state, 'splitscreen_active', False)
        )
        if _splitscreen:
            _w, _h = self.width(), self.height()
            _half = _w // 2
            _asp = _half / _h if _h > 0 else 1.0
            _split_proj = perspective_projection(self.camera.fov, _asp, 0.1, 10000.0)

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
                brushes_to_render, things_to_render,
                self.selected_object, self._render_config,
                clear=False
            )

            if render_state and hasattr(render_state, 'bullet_marks'):
                self._render_bullet_marks(render_state.bullet_marks, _split_proj, self.view_matrix)
            if render_state and hasattr(render_state, 'projectiles') and render_state.projectiles:
                self._render_projectiles(render_state.projectiles, _split_proj, self.view_matrix)
            if render_state and getattr(render_state, 'monster_debug_active', False):
                self._render_monster_debug_rays(getattr(render_state, 'monster_debug_rays', []),
                                                _split_proj, self.view_matrix)
            if self.play_mode and getattr(self, 'show_spatial_grid', False):
                self._render_spatial_grid(_split_proj, self.view_matrix)

            gl.glScissor(_half, 0, _half, _h)
            gl.glViewport(_half, 0, _half, _h)
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDepthFunc(gl.GL_LESS)
            gl.glDisable(gl.GL_BLEND)
            gl.glDisable(gl.GL_STENCIL_TEST)

            p2_brushes = render_state.all_brushes if hasattr(render_state, 'all_brushes') else brushes_to_render
            _p2_view = render_state.player2_view_matrix
            _p2_cam_pos = render_state.player2_pos

            self.renderer.render_scene(
                _split_proj, _p2_view, _p2_cam_pos,
                p2_brushes, things_to_render,
                self.selected_object, self._render_config,
                clear=False
            )

            if render_state and hasattr(render_state, 'bullet_marks'):
                self._render_bullet_marks(render_state.bullet_marks, _split_proj, _p2_view)
            if render_state and hasattr(render_state, 'projectiles') and render_state.projectiles:
                self._render_projectiles(render_state.projectiles, _split_proj, _p2_view)
            if render_state and getattr(render_state, 'monster_debug_active', False):
                self._render_monster_debug_rays(getattr(render_state, 'monster_debug_rays', []),
                                                _split_proj, _p2_view)
            if self.play_mode and getattr(self, 'show_spatial_grid', False):
                self._render_spatial_grid(_split_proj, _p2_view)

            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glViewport(0, 0, _w, _h)
        else:
            self.renderer.render_scene(
                self.projection_matrix, self.view_matrix, camera_pos,
                brushes_to_render, things_to_render,
                self.selected_object, self._render_config,
            )
            # Native overhead player sprite (top-down mode), depth-tested so
            # walls occlude it correctly.
            self._draw_overhead_sprite(render_state)
            # Collision visualization
            if getattr(self, '_collision_vis_mode', 'off') != 'off':
                # Get collision brushes from logic thread
                collision_brushes = []
                if hasattr(self, 'logic_thread') and self.logic_thread:
                    collision_brushes = getattr(self.logic_thread, '_model_collision_brushes', [])
                    # Build collision brushes on demand if not already built
                    # (needed for editor mode where they aren't auto-built on play start)
                    if not collision_brushes:
                        self.logic_thread.model_collision_enabled = True
                        self.logic_thread._model_collision_brushes = self.logic_thread._build_model_collision_brushes()
                        if hasattr(self.logic_thread, '_refresh_collision_brushes_cache'):
                            self.logic_thread._refresh_collision_brushes_cache()
                        collision_brushes = self.logic_thread._model_collision_brushes
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
            if render_state and hasattr(render_state, 'bullet_marks'):
                self._render_bullet_marks(render_state.bullet_marks, self.projection_matrix, self.view_matrix)
            if render_state and hasattr(render_state, 'projectiles') and render_state.projectiles:
                self._render_projectiles(render_state.projectiles, self.projection_matrix, self.view_matrix)
            if render_state and getattr(render_state, 'monster_debug_active', False):
                self._render_monster_debug_rays(getattr(render_state, 'monster_debug_rays', []),
                                                self.projection_matrix, self.view_matrix)
            if self.play_mode and getattr(self, 'show_spatial_grid', False):
                self._render_spatial_grid(self.projection_matrix, self.view_matrix)
        if not self.play_mode and getattr(self.editor, 'show_logic_links', False):
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
        if render_state:
            visible = len(render_state.visible_brushes)
            total = render_state.total_brushes
            actual_total = len(self.editor.state.brushes)
            if total == 0 and actual_total > 0:
                pass
            else:
                self.sysmon.update_stats(
                    visible_brushes=visible,
                    culled_brushes=render_state.culled_brushes,
                    total_brushes=total
                )
        # 3D world is done; plugins may add their own passes here (still in the
        # GL context, before the 2D overlay painter opens).
        if _pmgr is not None and _pmgr.has_listeners("render.post_scene"):
            _pmgr.emit("render.post_scene", viewport=self, renderer=self.renderer,
                       projection=self.projection_matrix, view=self.view_matrix,
                       camera_pos=camera_pos, play_mode=self.play_mode)

        painter = QPainter(self)
        if self.play_mode:
            self._draw_underwater_overlay(painter, render_state)
        if self.editor.config.getboolean('Display', 'show_fps', fallback=False):
            self._draw_fps_counter(painter)
        if self.play_mode and self.show_sprites_in_play_mode:
            self._draw_sprites_text(painter)
        if self.play_mode and getattr(self, 'show_render_menu', False):
            self._draw_render_menu(painter)
        if self.play_mode and self.editor.config.getboolean('Display', 'show_hud', fallback=True):
            _ss_hud = (
                getattr(self, 'splitscreen_mode', False)
                and render_state is not None
                and getattr(render_state, 'splitscreen_active', False)
            )
            if _ss_hud:
                self._draw_hud_splitscreen(painter, render_state)
            else:
                self._draw_hud(painter, render_state)
        if self.play_mode and render_state and getattr(render_state, 'player_dead', False):
            self._draw_death_screen(painter)
        if self.play_mode and getattr(self, '_cached_level_complete_ui', None):
            self._draw_level_complete_overlay(painter)
        if self.sysmon.is_active():
            self.sysmon.draw(
                painter, self.fps, self.logic_thread, self.renderer,
                self.editor.state, getattr(self.editor, 'terrain', None)
            )
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
        if self._muzzle_flash_counter > 0:
            self._muzzle_flash_counter -= 1

    def _draw_underwater_overlay(self, painter, render_state):
        """Tint the view while the camera is below a water surface."""
        p1_under = getattr(self, '_cached_underwater', False)
        p2_under = getattr(self, '_cached_p2_underwater', False)
        if not p1_under and not p2_under:
            return
        splitscreen = (
            getattr(self, 'splitscreen_mode', False)
            and render_state is not None
            and getattr(render_state, 'splitscreen_active', False)
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

    def _draw_hud(self, painter, render_state, viewport_width=None, viewport_height=None):
        if viewport_width is None:
            viewport_width = self.width()
        if viewport_height is None:
            viewport_height = self.height()
        # In overhead (top-down) mode there is no first-person view, so the gun
        # HUD sprite makes no sense; the held weapon is shown as a small pickup
        # icon bottom-right instead (see below), alongside any held keys.
        overhead = self._is_overhead()
        health = getattr(self, '_cached_health', 0)
        max_health = getattr(self, '_cached_max_health', 100)
        if health is None or max_health is None:
            return
        health_ratio = health / max_health if max_health > 0 else 0
        hud_margin = 20
        bar_width = 200
        bar_height = 20
        bar_x = hud_margin
        bar_y = viewport_height - hud_margin - bar_height
        painter.setPen(self._hud_bar_bg_pen)
        painter.setBrush(self._hud_bar_bg_brush)
        painter.drawRect(bar_x, bar_y, bar_width, bar_height)
        fill_width = int(bar_width * health_ratio)
        if fill_width > 0:
            painter.setPen(Qt.NoPen)
            if health_ratio > 0.6:
                painter.setBrush(QBrush(self._hud_health_green))
            elif health_ratio > 0.3:
                painter.setBrush(QBrush(self._hud_health_yellow))
            else:
                painter.setBrush(QBrush(self._hud_health_red))
            painter.drawRect(bar_x, bar_y, fill_width, bar_height)
        painter.setFont(self._hud_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(bar_x, bar_y - 5, f"HEALTH: {health}/{max_health}")
        active_weapon = getattr(self, '_cached_active_weapon', None)
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
        msg = getattr(self, '_cached_hud_message', '')
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
        hint = getattr(self, '_play_mode_hint', '')
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
                    scaled = QPixmap.fromImage(img).scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self._cached_gun_hud[cache_key] = scaled
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
        # Overhead: held weapon shown as a bottom-right pickup icon (like keys).
        # It takes the rightmost slot; keys shift left so both fit side by side.
        key_slot_offset = 0
        if overhead and active_weapon:
            icon_size = 100
            wx = viewport_width - hud_margin - icon_size
            wy = viewport_height - hud_margin - icon_size
            pm = self._load_weapon_pickup_pixmap(active_weapon)
            if pm and not pm.isNull():
                cache_key = (active_weapon, icon_size)
                scaled = self._cached_weapon_pickup.get(cache_key)
                if scaled is None or scaled.isNull():
                    scaled = pm.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self._cached_weapon_pickup[cache_key] = scaled
                painter.drawPixmap(wx + (icon_size - scaled.width()) // 2,
                                   wy + (icon_size - scaled.height()) // 2, scaled)
                # Reserve the weapon's slot so keys don't overlap it.
                key_slot_offset = icon_size + 15

        collected_keys = getattr(self, '_cached_collected_keys', set())
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
                    pixmap = Pickup.get_key_pixmap(key_name)
                    if pixmap and not pixmap.isNull():
                        scaled = pixmap.scaled(key_size, key_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self._cached_key_pixmaps[key_name] = scaled
                        painter.drawPixmap(icon_x, key_y, scaled)
                    else:
                        self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)
                except Exception:
                    self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)

    def _draw_hud_splitscreen(self, painter, render_state):
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
        painter.setPen(QColor(255, 200, 50))
        painter.setFont(self._hud_font)
        painter.drawText(8, 22, "P1")
        p2_health = getattr(render_state, 'player2_health', 100)
        p2_max_health = getattr(render_state, 'player2_max_health', 100)
        p2_dead = getattr(render_state, 'player2_dead', False)
        p2_ratio = (p2_health / p2_max_health) if p2_max_health > 0 else 0.0
        painter.save()
        painter.setClipRect(half, 0, half, h)
        margin = 20
        bar_w = 200
        bar_h = 20
        bar_x = half + margin
        bar_y = h - margin - bar_h
        painter.setPen(self._hud_bar_bg_pen)
        painter.setBrush(self._hud_bar_bg_brush)
        painter.drawRect(bar_x, bar_y, bar_w, bar_h)
        fill = int(bar_w * p2_ratio)
        if fill > 0:
            painter.setPen(Qt.NoPen)
            if p2_ratio > 0.6:
                painter.setBrush(QBrush(self._hud_health_green))
            elif p2_ratio > 0.3:
                painter.setBrush(QBrush(self._hud_health_yellow))
            else:
                painter.setBrush(QBrush(self._hud_health_red))
            painter.drawRect(bar_x, bar_y, fill, bar_h)
        painter.setFont(self._hud_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(bar_x, bar_y - 5, f"P2  {p2_health}/{p2_max_health}")
        cx = half + half // 2
        cy = h // 2
        sz = 10
        painter.setPen(QPen(QColor(0, 0, 0), 4))
        painter.drawLine(cx - sz, cy, cx + sz, cy)
        painter.drawLine(cx, cy - sz, cx, cy + sz)
        painter.setPen(QPen(QColor(0, 200, 255), 2))
        painter.drawLine(cx - sz, cy, cx + sz, cy)
        painter.drawLine(cx, cy - sz, cx, cy + sz)
        painter.setFont(self._hud_font)
        painter.setPen(QColor(0, 200, 255))
        painter.drawText(half + 8, 22, "P2")
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
            if getattr(self, 'current_render_mode', 0) == mid:
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
            self.logic_thread.level_complete_ui = None

    def _cancel_level_complete(self):
        self._cached_level_complete_ui = None
        self._level_complete_btn_rect = None
        if self.logic_thread:
            self.logic_thread.level_complete_ui = None
    def load_texture(self, texture_name, subfolder):
        return self.renderer.load_texture(texture_name, subfolder) if self.renderer else 0

    def load_all_sprite_textures(self):
        things = {
            'PlayerStart': 'player.png',
            'Light': 'light.png',
            'Monster': 'monster.png',
            'Pickup': 'pickup.png',
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

    def _pixmap_to_texture(self, pixmap):
        image = pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
        width, height = image.width(), image.height()
        ptr = image.constBits()
        try:
            nbytes = image.sizeInBytes()
        except AttributeError:
            nbytes = image.byteCount()
        data = ptr.asstring(nbytes)
        tex_id = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        return tex_id

    def update_instance_textures(self, things):
        if not self.renderer:
            return

        # Build a cheap state hash: captures thing identity, monster
        # state flags (dead/shooting), and logic gate type.
        # If it matches the last frame we can reuse the cached result.
        def _state_hash():
            parts = []
            for t in things:
                if isinstance(t, Monster):
                    parts.append((id(t), t.properties.get('dead', False), t.properties.get('is_shooting', False)))
                elif isinstance(t, LogicGate):
                    parts.append((id(t), t.properties.get('logic_type', 'and')))
                elif isinstance(t, Pickup):
                    parts.append((id(t), t.properties.get('item_type', ''), t.properties.get('key_name', ''), t.properties.get('custom_sprite', '')))
                else:
                    parts.append(id(t))
            return hash(tuple(parts))

        h = _state_hash()
        if h == self._instance_tex_hash:
            return   # nothing changed – skip the rebuild entirely

        self._instance_tex_hash = h
        instance_textures = {}
        for thing in things:
            if isinstance(thing, Monster):
                mtype = thing.properties.get('monster_type', 'human')
                is_dead = thing.properties.get('dead', False)
                is_shooting = thing.properties.get('is_shooting', False)
                if is_dead:
                    state_key = 'dead'
                elif is_shooting:
                    state_key = 'shooting'
                else:
                    state_key = 'alive'
                sprite_path = thing.get_sprite_path()
                tex_key = f"msprite__{sprite_path.replace('/', '__').replace('.', '_')}"
                if tex_key not in self.sprite_textures:
                    rel_path = sprite_path.replace('assets/', '')
                    dirname = os.path.dirname(rel_path)
                    filename = os.path.basename(rel_path)
                    tid = self.load_texture(filename, dirname)
                    if tid:
                        self.sprite_textures[tex_key] = tid
                if tex_key in self.sprite_textures:
                    instance_textures[id(thing)] = self.sprite_textures[tex_key]
                continue
            if isinstance(thing, LogicGate):
                l_type = thing.properties.get('logic_type', 'and').lower()
                filename = f"logic_{l_type}.png"
                tex_key = f"logic_{l_type}"
                if tex_key not in self.sprite_textures:
                    tid = self.load_texture(filename, 'sprites')
                    if tid:
                        self.sprite_textures[tex_key] = tid
                if tex_key in self.sprite_textures:
                    instance_textures[id(thing)] = self.sprite_textures[tex_key]
            elif isinstance(thing, Pickup):
                if thing.is_key():
                    key_name = thing.get_key_name()
                    tex_key = f'key_{key_name}'
                    if tex_key in self.sprite_textures:
                        instance_textures[id(thing)] = self.sprite_textures[tex_key]
                elif thing.properties.get('custom_sprite'):
                    sprite_path = thing.properties.get('custom_sprite')
                    filename = os.path.basename(sprite_path.replace('\\', '/'))
                    tex_id = self.load_texture(filename, 'sprites')
                    if tex_id:
                        instance_textures[id(thing)] = tex_id
            elif isinstance(thing, LevelChanger):
                tex_key = 'LevelChanger'
                if tex_key in self.sprite_textures:
                    instance_textures[id(thing)] = self.sprite_textures[tex_key]
                else:
                    fallback_key = 'logic_relay'
                    if fallback_key in self.sprite_textures:
                        instance_textures[id(thing)] = self.sprite_textures[fallback_key]
            elif isinstance(thing, Portal):
                tex_key = 'Portal'
                if tex_key in self.sprite_textures:
                    instance_textures[id(thing)] = self.sprite_textures[tex_key]
        self.renderer.set_instance_textures(instance_textures)

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

            # Convert editor angle (0° = east) to game angle (0° = north) and flip 180°
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

            if self.logic_thread:
                self.logic_thread.set_player(self.player)
                self.logic_thread.set_play_mode(True)

            if self.splitscreen_mode:
                self.player2 = Player(
                    player_start_pos[0] + 32, player_start_pos[2],
                    player_angle_rad,
                    physics_enabled=physics_enabled,
                )
                self.player2.pos.y = player_start_pos[1]
                if self.logic_thread:
                    self.logic_thread.set_player2(self.player2)
                if self.height() > 0:
                    self._cached_aspect_ratio = (self.width() // 2) / self.height()
                    if self.logic_thread:
                        self.logic_thread.set_frustum_aspect(self._cached_aspect_ratio)
        else:
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
                self.logic_thread.set_play_mode(False)
                self.logic_thread.set_player(None)
            self.player = None
            self.player2 = None
            if self.logic_thread:
                self.logic_thread.set_player2(None)
            if self.height() > 0:
                self._cached_aspect_ratio = self.width() / self.height()
                if self.logic_thread:
                    self.logic_thread.set_frustum_aspect(self._cached_aspect_ratio)
            self._play_mode_hint = ""
            self._play_mode_hint_timer.stop()
            self._cached_hint_text = None
            self.update()

    def _toggle_splitscreen(self):
        self.splitscreen_mode = not self.splitscreen_mode
        if self.play_mode:
            pos = getattr(self, '_last_player_start_pos', [0, 0, 0])
            angle = getattr(self, '_last_player_start_angle', 0)
            if self.splitscreen_mode:
                self.player2 = Player(pos[0] + 32, pos[2], np.radians(90.0 - angle), physics_enabled=True)
                self.player2.pos.y = pos[1]
                if self.logic_thread:
                    self.logic_thread.set_player2(self.player2)
            else:
                self.player2 = None
                if self.logic_thread:
                    self.logic_thread.set_player2(None)
            w, h = self.width(), self.height()
            if h > 0:
                vp_w = (w // 2) if self.splitscreen_mode else w
                self._cached_aspect_ratio = vp_w / h
                if self.logic_thread:
                    self.logic_thread.set_frustum_aspect(self._cached_aspect_ratio)
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
        pos = getattr(self, '_last_player_start_pos', [0, 0, 0])
        angle = getattr(self, '_last_player_start_angle', 0)
        self.toggle_play_mode(pos, angle)

    def set_culling(self, enabled):
        self.culling_enabled = enabled
        self.update()

    def set_cull_distance(self, distance):
        self.cull_distance = distance
        if self.renderer:
            self.renderer.lod_manager.cull_dist_sq = distance * distance
            self.renderer.lod_manager.full_dist_sq = (distance * 0.25) ** 2
        self.update()

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
            config = getattr(self.editor, 'config', None)
            self.renderer = cls(
                self.load_texture, self.grid_size, self.world_size, config)
            self.renderer.set_sprite_textures(self.sprite_textures)
            self.renderer.set_instance_textures(self.sprite_textures)
            self.renderer.lod_manager.cull_dist_sq = self.cull_distance * self.cull_distance
            self.renderer.lod_manager.full_dist_sq = (self.cull_distance * 0.25) ** 2
            self.grid_dirty = True
            self._renderer_mode = mode
            print(f"[QtGameView] Renderer switched to {mode}.")
        except Exception as exc:
            print(f"[QtGameView] switch_renderer FAILED: {exc}")
            try:
                config = getattr(self.editor, 'config', None)
                self.renderer = Renderer_F(
                    self.load_texture, self.grid_size, self.world_size, config)
                self._renderer_mode = 'Forward'
            except Exception as fe:
                print(f"[QtGameView] Emergency fallback also failed: {fe}")
        finally:
            self.doneCurrent()
        self.update()

    def get_selected_object_pos(self):
        if not self.editor.state.selected_object:
            return None
        if isinstance(self.editor.state.selected_object, dict):
            return glm.vec3(self.editor.state.selected_object.get('pos', [0, 0, 0]))
        return glm.vec3(self.editor.state.selected_object.pos)

    def set_selected_object_pos(self, new_pos_vec):
        if not self.editor.state.selected_object:
            return
        grid = self.editor.grid_size_spinbox.value()
        snapped = [round(c / grid) * grid for c in new_pos_vec]
        if isinstance(self.editor.state.selected_object, dict):
            self.editor.state.selected_object['pos'] = snapped
        else:
            self.editor.state.selected_object.pos = snapped
        self.update()

    def set_terrain_sculpt_active(self, active: bool):
        self.terrain_sculpt_active = active
        if active:
            self.setCursor(Qt.CrossCursor)
        else:
            self.terrain_sculpt_painting = False
            self.setCursor(Qt.ArrowCursor)

    def raycast_terrain(self, mx: int, my: int):
        terrain = getattr(self.editor, 'terrain', None)
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

    def _apply_sculpt_at_mouse(self, mx: int, my: int):
        hit = self.raycast_terrain(mx, my)
        if hit is None:
            return
        wx, wy, wz = hit
        terrain = self.editor.terrain
        mode = self.terrain_sculpt_mode
        radius = self.terrain_sculpt_radius
        strength = self.terrain_sculpt_strength
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
        if self.use_threading and self.logic_thread:
            ec = self.logic_thread.get_editor_camera()
            ray_origin = ec.pos
        else:
            ray_origin = self.camera.pos
        return ray_origin, ray_dir

    def get_object_at_3d(self, mx, my):
        ray_o, ray_d = self.get_ray_from_mouse(mx, my)
        best_obj, best_t = None, float('inf')
        for brush in self.editor.state.brushes:
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
            if hit and tmin < best_t:
                best_t = tmin
                best_obj = brush
        for thing in self.editor.state.things:
            tp = glm.vec3(thing.pos)
            radius = 32.0
            oc = ray_o - tp
            a = glm.dot(ray_d, ray_d)
            b = 2.0 * glm.dot(oc, ray_d)
            c = glm.dot(oc, oc) - radius * radius
            disc = b * b - 4 * a * c
            if disc >= 0:
                t = (-b - disc**0.5) / (2.0 * a)
                if 0 < t < best_t:
                    best_t = t
                    best_obj = thing
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

    def mousePressEvent(self, event):
        if (self.play_mode and getattr(self, '_cached_level_complete_ui', None)
                and getattr(self, '_level_complete_btn_rect', None)):
            if self._level_complete_btn_rect.contains(event.pos()):
                self._confirm_level_complete()
                return

        if self.terrain_sculpt_active and not self.play_mode and event.button() == Qt.LeftButton:
            self.terrain_sculpt_painting = True
            self._apply_sculpt_at_mouse(event.x(), event.y())
            return
        if self.sysmon.handle_mouse_press(event, self.play_mode):
            if self.sysmon.dragging:
                self.setCursor(Qt.ClosedHandCursor)
            return
        if self.face_mode_active and event.button() == Qt.LeftButton:
            if self.hovered_face_info:
                brush, face = self.hovered_face_info
                self.editor.apply_texture_to_specific_face(brush, face)
                # Open the Radiant-style Surface Inspector for the clicked face.
                if hasattr(self.editor, 'show_surface_inspector'):
                    self.editor.show_surface_inspector(brush, face)
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
            render_state = self.game_state.get_render_state()
            if getattr(render_state, 'player_dead', False):
                return
            active_weapon = getattr(render_state, 'active_weapon', None)
            if active_weapon:
                from engine.monster_constants import WEAPON_SHOOT_SOUND, NON_FIRING_WEAPONS
                # Non-firing weapons (e.g. cig) are display-only: clicking
                # equips nothing to shoot — no shot, no muzzle flash, no sound.
                if active_weapon not in NON_FIRING_WEAPONS:
                    self.game_state.queue_shot()
                    sound_file = WEAPON_SHOOT_SOUND.get(active_weapon, 'shoot.wav')
                    sound = self._get_sound_instance(sound_file)
                    if sound:
                        sound.play()
                return
        if event.button() == Qt.LeftButton and QApplication.keyboardModifiers() == Qt.ShiftModifier and not self.play_mode:
            obj = self.get_object_at_3d(event.x(), event.y())
            if obj:
                self.editor.save_state()
                self.editor.set_selected_object(obj)
                self.update()
            return
        if event.button() == Qt.LeftButton and self.editor.state.selected_object and not self.play_mode:
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
        if self.sysmon.handle_mouse_move(event, self.play_mode, self.width(), self.height()):
            self.update()
            return
        if self.mouselook_active:
            dx, dy = event.x() - self.last_mouse_pos.x(), event.y() - self.last_mouse_pos.y()
            if self.use_threading and self.logic_thread:
                self.game_state.set_mouse_delta(float(dx), float(dy))
            else:
                self.camera.rotate(dx, dy)
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)
            self.editor.update_views()
            return
        if self.play_mode:
            if self.console_overlay_active:
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
        if self.terrain_sculpt_painting and self.terrain_sculpt_active:
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
        if self.face_mode_active:
            self.hovered_face_info = self.get_brush_face_at_coords(event.x(), event.y())
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.terrain_sculpt_painting and event.button() == Qt.LeftButton:
            self.terrain_sculpt_painting = False
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

    def wheelEvent(self, event):
        if not self.play_mode:
            self.camera.fov = np.clip(self.camera.fov - event.angleDelta().y() * 0.05, 30, 120)
            self.editor.update_views()

    def get_face_at(self, mouse_pos):
        if not isinstance(self.editor.state.selected_object, dict):
            return None
        brush = self.editor.state.selected_object
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

    def _load_weapon_pickup_pixmap(self, item_type):
        """The world/pickup sprite for a weapon (e.g. 'gun1' -> gun1.png).

        Used by the overhead HUD, which shows the small pickup icon bottom-right
        instead of the first-person gun sprite. Resolved via the Pickup entity's
        GUN_SPRITES map so it matches what the weapon looks like in the world.
        """
        if item_type in self.weapon_pickup_pixmaps:
            return self.weapon_pickup_pixmaps[item_type]
        rel = None
        try:
            from editor.things import Pickup
            rel = Pickup.GUN_SPRITES.get(item_type)
        except Exception:
            rel = None
        if not rel:
            rel = os.path.join('assets', 'sprites', f'{item_type}.png')
        pixmap = QPixmap(rel) if os.path.exists(rel) else None
        self.weapon_pickup_pixmaps[item_type] = pixmap
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
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            QApplication.setOverrideCursor(Qt.BlankCursor)
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)

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
            current_state = getattr(self.editor, 'show_logic_links', False)
            self.editor.show_logic_links = not current_state
            self.editor.update_views()
            if hasattr(self.editor, 'show_toast'):
                status = "ON" if self.editor.show_logic_links else "OFF"
                self.editor.show_toast(f"Logic Links: {status}")
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
                new_state = self.logic_thread.toggle_model_collision()
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
            render_state = self.game_state.get_render_state()
            if getattr(render_state, 'player_dead', False):
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
            if getattr(self, 'show_render_menu', False):
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