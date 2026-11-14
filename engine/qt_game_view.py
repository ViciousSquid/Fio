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
def restore_default_pixel_store():
    """Put the pixel-store state Qt's painter relies on back to GL defaults.

    The renderer shares its context with the QPainter that draws the HUD,
    and Qt uploads text glyphs into a texture assuming 4-byte row alignment
    and no row length. Any pass that changes those for its own uploads and
    leaves them changed shears every glyph that is not a multiple of four
    pixels wide -- small HUD and ``message`` text came out garbled. Called
    once before the painter opens, so no pass can leak into it.
    """
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 4)
    gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
    gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, 0)
    gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)

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
from .view_hud import ViewHud
from .view_messages import ViewMessages
from .view_pause import ViewPause
from .view_terrain_edit import ViewTerrainEdit
from .view_components import ViewComponents
from .view_picking import ViewPicking
from .view_debug_draw import ViewDebugDraw
from .terrain_frame import frustum_planes
from .overlay_geometry import (
    collision_wireframes, connection_segments, face_highlight_triangles)
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
    #: Weapon slot keys in Play: slot n holds item ITEM_IDS[n - 1].
    _SLOT_KEYS = {Qt.Key_1: 1, Qt.Key_2: 2, Qt.Key_3: 3, Qt.Key_4: 4}
    #: macOS: frames to wait for the capture re-centre to land before the
    #: cursor is detached anyway (about half a second).
    _MAC_CENTRE_MAX_TRIES = 30

    def __init__(self, editor):
        super().__init__(editor)
        # The view's delegated responsibilities (engine/view_*.py), made first:
        # the rest of construction calls into them.
        self.hud = ViewHud(self)
        self.messages = ViewMessages(self)
        self.pause = ViewPause(self)
        self.terrain_edit = ViewTerrainEdit(self)
        self.components = ViewComponents(self)
        self.picking = ViewPicking(self)
        self.debug_draw = ViewDebugDraw(self)
        self._logic_tick_fault_signal.connect(self._handle_logic_tick_fault)

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        self.setFormat(fmt)

        self.editor = editor
        #: Frames left whose GL errors are reported (see _report_gl_errors).
        self._gl_error_checks_left = 3
        self.brush_display_mode = "Textured"
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
        self.show_visibility_debug = False
        self.grid_visible = True
        self.sysmon = SysMon(self)
        # SysMon-style floating tools share one lightweight window manager.
        self.floating_windows = WindowManager()
        self.pos_window = None


        #: The mixer, the sound cache and the speakers' mix (engine/view_audio.py).
        self.audio = ViewAudio(self)
        self._frame_terrain = None

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
        #: Z latches editor mouse look; the held right button also drives it.
        self.editor_freelook_active = False
        self._editor_right_mouse_held = False
        self.last_mouse_pos = QPoint()
        #: Set when the cursor has just been sent to the centre (or the window
        #: was away): the next poll only re-centres (see _poll_mouse_look).
        self._skip_next_look_poll = True
        #: macOS: the native filter summing mouse movement, and whether the
        #: cursor is detached from the mouse (see _poll_mouse_look_macos).
        self._mac_mouse = None
        self._mac_cursor_detached = False
        #: macOS: frames spent waiting for the re-centre to land before the
        #: cursor is detached (see _poll_mouse_look_macos).
        self._mac_centre_tries = 0
        if sys.platform == 'darwin':
            import macos_app
            self._mac_mouse = macos_app.mouse_movement_filter()
            if self._mac_mouse is not None:
                QApplication.instance().installNativeEventFilter(self._mac_mouse)
                # Never leave the system cursor detached after Fio exits.
                atexit.register(macos_app.set_cursor_follows_mouse, True)


        self.hud._init_hud_caches()

        self.texture_manager = {}
        self.sprite_textures = {}
        self.hud_pixmaps = {}              # sprite path -> QPixmap (None if missing)
        self.show_spatial_grid = False
        self.renderer = None
        #: Registry name of the renderer initializeGL creates and switch_renderer replaces.
        self._renderer_mode = DEFAULT_RENDERER

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

        # Qt owns native events; SDL supplies controllers and audio only.
        init_sdl_input()
        
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
        self.audio._init_sound_system()


        self._last_player_start_pos = [0, 0, 0]
        self._last_player_start_angle = 0

        self.fps = 0.0
        self.start_time = time.perf_counter()

        self._render_config = {
            "culling_enabled": True,
            "brush_display_mode": "Textured",
            "render_mode": 0,
            "show_triggers_as_solid": False,
            "show_caulk": True,
            "play_mode": False,
            "splitscreen_active": False,
            "primary_selection": None,
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
        # Where the sculpt cursor is; off-screen until the mouse moves over the
        # view. Read by every paint while sculpting, which can begin before it.
        self._terrain_brush_mouse_pos = QPoint(-1, -1)
        self._terrain_brush_hit = None
        self.terrain_sculpt_painting = False
        self.terrain_sculpt_mode = 'raise'
        self.terrain_sculpt_radius = 50.0
        self.terrain_sculpt_strength = 20.0
        self.terrain_sculpt_feather = 0.25
        # A press on a placed stamp: {'last': (x, z), 'moved', 'toggle_off'}.
        self._stamp_drag = None
        # Decals: placed, selected, dragged and nudged as stamps are.
        from editor.decal_tool import DecalTool
        self.decal_tool = DecalTool(self)
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
        #: The open pause menu (Esc in Play), an engine.pause_menu.PauseMenu,
        #: or None. It holds the world pause owner PAUSE_MENU while open.
        self.pause_menu = None
        self._pause_menu_family = None
        #: The frame the menu opened over, blurred: drawn behind the menu.
        self._pause_backdrop = None
        #: The backdrop's fade: ``(start time, from level, to level)`` while
        #: it fades in (menu opened) or out (play resumed), else None.
        self._pause_fade = None
        self._pause_fade_timer = QTimer(self)
        self._pause_fade_timer.setInterval(16)
        self._pause_fade_timer.timeout.connect(self.update)
        #: Seconds of unpaused Play, for animated textures (_animate_textures).
        self._texture_clock = 0.0
        self._texture_clock_last = None
        #: The menu's floating window (SysMon-style), and where it was left.
        self._pause_window = None
        self._pause_window_pos = None
        # Up repeats the last command (the history every console shares).
        self._console_input = CommandInput(self)
        self._console_input.setPlaceholderText("Enter command…   Esc to close")
        self._console_input.setFont(QFont("IBM Plex Mono", 11))
        self._console_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(10, 10, 10, 220);
                color: #F08000;
                border: none;
                border-top: 2px solid #F08000;
                padding: 6px 10px;
                font-family: "IBM Plex Mono";
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
        #: The weapon-slot flash: which switch was last seen, and when the
        #: one being shown started.
        self._weapon_switch_seen = 0
        self._weapon_switch_shown_at = None
        self._cached_player_ammo = 0
        self._cached_player_armor = 0
        self._cached_level_complete_ui = None
        self._muzzle_flash_duration_frames = 3

        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)

        self._play_views_due = 0.0
        timer = QTimer(self)
        timer.setInterval(16)
        timer.timeout.connect(self.update_loop)
        timer.start()

        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(True)


    #: How often the editor's 2D views are repainted while a game is played.
    PLAY_2D_VIEWS_HZ = 10.0


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
        self._log_gl_context()
        config = self.editor.config
        self._renderer_mode = DEFAULT_RENDERER
        self.renderer = create_renderer(DEFAULT_RENDERER, config)
        if not self.renderer.ready:
            debug_log("Error", "3D view: the renderer's shaders did not build, so "
                               "the view stays empty; the error is in the "
                               "terminal output.")
        self.set_cull_distance(self.cull_distance)
        self.load_all_sprite_textures()
        self.preload_level_textures()
        self._start_logic_thread()
        self.debug_draw._init_debug_resources()
        if hasattr(self, 'debug_console_window'):
            QTimer.singleShot(1000, self.debug_console_window.show)

    def _log_gl_context(self):
        """Name the OpenGL driver in the Debug Console.

        A 3D view that clears and then draws nothing is a driver disagreeing
        with the renderer, and the first thing a report of one needs is which
        driver; it is not otherwise visible anywhere.
        """
        def _string(name):
            try:
                value = gl.glGetString(name)
                return value.decode(errors='replace') if value else '?'
            except Exception:
                return '?'
        debug_log("Info", f"OpenGL {_string(gl.GL_VERSION)} | "
                          f"{_string(gl.GL_RENDERER)} | {_string(gl.GL_VENDOR)} | "
                          f"3D view {self.width()}x{self.height()} "
                          f"@{self.devicePixelRatioF():g}x")

    _GL_ERROR_NAMES = {
        0x0500: 'GL_INVALID_ENUM', 0x0501: 'GL_INVALID_VALUE',
        0x0502: 'GL_INVALID_OPERATION', 0x0505: 'GL_OUT_OF_MEMORY',
        0x0506: 'GL_INVALID_FRAMEBUFFER_OPERATION',
    }

    def _report_gl_errors(self):
        """Log the GL errors the frame just drawn left behind.

        PyOpenGL's per-call checks are off outside ``FIO_GL_DEBUG=1``, so a
        driver that rejects a call does it silently and the frame comes out
        empty. Asked for the first few frames only: glGetError stalls.
        """
        errors = []
        for _ in range(8):
            code = int(gl.glGetError())
            if code == gl.GL_NO_ERROR:
                break
            errors.append(self._GL_ERROR_NAMES.get(code, hex(code)))
        if errors:
            debug_log("Error", "3D view: OpenGL reported " + ", ".join(errors)
                      + " while drawing; run with FIO_GL_DEBUG=1 to find the call.")


    def _handle_logic_tick_fault(self, message):
        """Run fatal-tick play teardown on QtGameView's GUI thread.

        Through the editor's own Stop path, not just this view's toggle: Stop
        also puts back the world captured at Play (when the editor is set to)
        and returns the editor's mode label, tabs and Play button.
        """
        if self.play_mode:
            self.editor._exit_play_mode()
        if message:
            self._play_mode_hint = "Logic tick failed; Play Mode stopped"

    def set_editor_camera(self, pos, yaw=None, pitch=None):
        """Move the editor camera, keeping *yaw*/*pitch* unless given.

        The logic thread owns the editor camera: every paint copies this
        view's camera back from the published render state. A move made here
        alone is undone by the next frame, so it is made on both.
        """
        camera = self.camera
        camera.pos = glm.vec3(pos)
        if yaw is not None:
            camera.yaw = float(yaw)
        if pitch is not None:
            camera.pitch = float(pitch)
        if self.logic_thread is not None:
            self.logic_thread.camera.set_editor_camera(
                camera.pos, camera.yaw, camera.pitch, camera.fov)

    def _start_logic_thread(self):
        if self._thread_started:
            return
        self.logic_thread = LogicThread(self.game_state, self.editor.state)
        self.logic_thread.player_runtime.p2_turn_sensitivity = float(
            self.editor.config.get(
                "Controls",
                "p2_turn_sensitivity",
                fallback=10.0,
            )
        )
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
            if self.logic_thread.is_alive():
                self.logic_thread.join(timeout=1.0)
            self.logic_thread = None
            self._thread_started = False

    def closeEvent(self, event):
        if not self.play_mode:
            self.stop_editor_mouselook()
        self._release_play_mouse_grab()
        self._attach_mac_cursor()
        self._stop_logic_thread()
        super().closeEvent(event)

    def preload_level_textures(self):
        """Load the textures the open map's brushes name, in this view's GL
        context: called when a map has loaded and as Play starts, under the
        loading bar, so no frame stops to decode one. Nothing else is loaded
        up front -- every texture in ``assets/textures`` used to be, at
        start-up, whatever the map (0.3 s and 53 MB of a 1.3 s start). A
        texture applied later is loaded when a frame first draws it.
        """
        if not self.renderer or not self.renderer.ready:
            return
        with self.gl_context():
            self._preload_level_textures()

    def _preload_level_textures(self):
        texture_set = set()
        for brush in self.editor.state.brushes:
            for face_tex in brush.get('textures', {}).values():
                if face_tex and face_tex != 'caulk.jpg':
                    texture_set.add(face_tex)
            # Angled brushes can carry per-plane textures (e.g. on cut faces).
            geo = brush.get('geometry')
            if geo:
                for plane in geo.get('planes', []):
                    tex = plane.get('texture')
                    if tex and tex != 'caulk.jpg':
                        texture_set.add(tex)
        for tex_name in texture_set:
            self.renderer.load_texture(tex_name, 'textures')

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
        # A Big World map follows the editor camera from cell to cell.
        if not self.play_mode and self.editor.state.stream_editor_camera(self.camera.pos):
            self.editor.update_all_ui()
        current_time = time.perf_counter()
        # FPS and frame-time samples are recorded only after Qt has composed
        # a fresh, successfully painted frame; this timer is not a frame clock.
        self.audio._process_sound_queue()
        self._process_console_command_queue()

        if self.logic_thread:
            keys = (set() if self.console_overlay_active or self.pause_menu is not None
                    else self.editor.keys_pressed)
            self.game_state.set_keys(keys)
            self._poll_mouse_look()
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
                if self.play_mode and current_time >= self._play_views_due:
                    # The editor's 2D views follow play at PLAY_2D_VIEWS_HZ,
                    # not at the frame rate: one repaint of the visible one
                    # is 4-9 ms of this thread (more than the 3D frame's own
                    # Python on most maps), and it holds the GIL throughout.
                    self._play_views_due = current_time + 1.0 / self.PLAY_2D_VIEWS_HZ
                    self.editor.update_views()
        else:
            self.repaint()


    def _process_console_command_queue(self):
        """Run any console commands queued by the I/O system on the UI thread.

        The logic thread enqueues command strings (e.g. a trigger brush firing a
        logic_command entity's RunCommand input). They must execute here, on the
        main thread, because console commands touch Qt widgets and editor state.
        """
        commands = self.game_state.consume_console_commands()
        if not commands:
            return
        handler = self.editor.console_handler
        for cmd in commands:
            try:
                handler.handle_command(cmd, from_map=True)
            except Exception as exc:
                print(f"[QtGameView] console command '{cmd}' failed: {exc}")


    # =========================================================================
    # POST-EFFECT RENDERING METHODS
    # =========================================================================


    def _publish_terrain(self, projection, view, camera_pos):
        """Bring the terrain up to date for one view's camera and put what it
        publishes in the frame input. The renderer is handed a TerrainFrame
        (engine/terrain_frame.py), never the terrain: streaming, LOD and
        chunk building happen here, before the scene is drawn."""
        terrain = self._frame_terrain
        if terrain is None or not terrain.enabled:
            self._render_config["terrain"] = None
            return
        terrain.advance(camera_pos, frustum_planes(projection * view))
        self._render_config["terrain"] = terrain.publish()

    def _collect_terrain_stats(self):
        """What the renderer drew of the terrain, kept on it for the panels
        that show it (SysMon, the terrain editor, Debug Tables)."""
        terrain = self._frame_terrain
        stats = getattr(self.renderer, 'render_stats', None)
        if terrain is None or stats is None:
            return
        terrain.visible_chunks = stats.terrain_visible_chunks
        terrain.culled_chunks = stats.terrain_culled_chunks
        terrain.total_triangles = stats.terrain_triangles
        terrain.drawn_slots = stats.terrain_drawn_slots

    def paintGL(self):
        if not self.renderer or not self.renderer.ready:
            return
        started = time.perf_counter()
        render_state: Optional[RenderState] = None
        if self.logic_thread:
            render_state = self.game_state.get_render_state()
        if render_state is None:
            return
        paint_succeeded = False
        try:
            self._animate_textures()
            self._paint_frame(render_state)
            paint_succeeded = True
        finally:
            # The render state is a borrowed snapshot, and publication waits
            # while it is borrowed: release it the moment this synchronous
            # paint is done, even if the paint raised.
            if render_state is not None:
                self.game_state.release_render_state(render_state)
            self._collect_terrain_stats()
            #: CPU milliseconds the last paint took, for Debug Tables.
            self.paint_ms = (time.perf_counter() - started) * 1000.0
        if paint_succeeded:
            # paintGL is the successful render boundary for QOpenGLWidget.
            self.fps = self.sysmon.record_rendered_frame(time.perf_counter())
        if self._gl_error_checks_left:
            self._gl_error_checks_left -= 1
            self._report_gl_errors()

        if self._muzzle_flash_counter > 0:
            self._muzzle_flash_counter -= 1

    def _animate_textures(self):
        """Advance animated (GIF) textures by the play clock.

        The clock runs only in Play and stops while the world is paused (the
        pause menu, an actor pick); outside Play every animated texture shows
        its first frame. A renderer without ``animate_textures`` has none.
        """
        animate = getattr(self.renderer, 'animate_textures', None)
        now = time.perf_counter()
        last, self._texture_clock_last = self._texture_clock_last, now
        if not self.play_mode:
            self._texture_clock = 0.0
        elif last is not None:
            logic = self.logic_thread
            paused = logic is not None and logic.session_runtime.world_paused
            if not paused:
                self._texture_clock += min(now - last, 0.25)
        if animate is not None:
            animate(self._texture_clock)


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
            self._cached_player_armor = render_state.player_armor
            self._cached_shot_ready = render_state.shot_ready
            self._cached_active_weapon = render_state.active_weapon
            if render_state.weapon_switch_serial != self._weapon_switch_seen:
                self._weapon_switch_seen = render_state.weapon_switch_serial
                self._weapon_switch_shown_at = time.perf_counter()
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
            self.renderer.set_grid(self.world_size, self.grid_size)
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
        # The Display box is published as-is while editing. In Play the game
        # is drawn textured; only the debug views (Wireframe, Points, Overlay)
        # carry over.
        _display = self.brush_display_mode
        if self.play_mode and _display not in ("Wireframe", "Points", "Overlay"):
            _display = "Textured"
        self._render_config["brush_display_mode"] = _display
        self._render_config["show_triggers_as_solid"] = self.show_triggers_as_solid
        self._render_config["render_mode"] = self.current_render_mode
        self._render_config["play_mode"] = self.play_mode
        # The renderer sees the selection only as this descriptor, on both
        # channels: render_scene's argument and the frame input.
        _selection = describe_selection(self.editor.primary_selection())
        self._render_config["primary_selection"] = _selection
        self._render_config["time"] = time.perf_counter() - self.start_time
        self._render_config["show_sprites_in_play_mode"] = self.show_sprites_in_play_mode
        self._render_config["show_glasses"] = bool(self.show_glasses)
        from engine.glasses import world_glasses
        self.logic_thread.player_runtime.glasses_style = normalize_glasses(self.player1_glasses)
        (_glass_positions, _glass_sprites, _spectator_positions, _spectator_sprites,
         _spectator_active) = world_glasses(render_state if self.play_mode else None,
                                          self.show_glasses, self.player1_glasses)
        self._render_config["player_glasses_positions"] = _glass_positions
        self._render_config["player_glasses_sprites"] = _glass_sprites
        self._render_config["spectator_glasses_active"] = _spectator_active
        self._render_config['player_glasses_self_candidates'] = (
            (True,) * (len(_glass_positions)-len(_spectator_positions))
            + (False,) * len(_spectator_positions))
        self._render_config["grid_visible"] = self.grid_visible and not self.play_mode
        _filters = self.editor.state.view_filters
        # The terrain this frame shows; each view publishes it for its own
        # camera just before it is drawn (_publish_terrain).
        self._frame_terrain = (
            None if not self.play_mode and not _filters.shows('terrain')
            else self.editor.terrain)
        self._render_config["terrain"] = None
        # Both editor and play rendering consume the same canonical dense
        # projection published by LogicRender. There is no editor-side table.
        self._render_config["render_table"] = render_state.render_table
        self._render_config["decals"] = render_state.decals
        self._render_config["all_brush_slots"] = render_state.all_brush_slots
        _main_brush_slots = render_state.visible_brush_slots
        self._render_config["entity_table"] = render_state.entity_table
        self._render_config["visible_thing_slots"] = render_state.visible_thing_slots
        self._render_config["thing_hidden"] = render_state.thing_hidden
        # In play only the lights near the player light (nearest first), so a
        # map may hold more lights than the shaders have room for.
        _focus = None
        if self.play_mode:
            _fp = render_state.player_pos
            _focus = (float(_fp.x), float(_fp.y), float(_fp.z))
        self._render_config["light_focus"] = _focus
        self._render_config["light_fade_range"] = render_state.light_fade_range

        _splitscreen = (
            self.play_mode
            and self.splitscreen_mode
            and render_state is not None
            and render_state.splitscreen_active
        )
        self._render_config["splitscreen_active"] = bool(_splitscreen)
        # Plugin render hooks. Guarded by has_listeners so an unhooked frame
        # pays a single dict lookup and builds no payload — see the render.*
        # events in the plugin API. The manager handle is fetched once per frame.
        _pmgr = self.logic_thread.plugins if self.logic_thread is not None else None
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

            self._publish_terrain(_split_proj, self.view_matrix, camera_pos)
            self.renderer.render_scene(
                _split_proj, self.view_matrix, camera_pos,
                _selection, self._render_config,
                clear=False, brush_slots=_main_brush_slots,
            )

            if render_state:
                self.debug_draw._render_bullet_marks(render_state.bullet_marks, _split_proj, self.view_matrix)
            if render_state is not None and len(render_state.projectiles):
                self.debug_draw._render_projectiles(render_state.projectiles, _split_proj, self.view_matrix)
            if render_state and render_state.monster_debug_active:
                self.debug_draw._render_monster_debug_rays(render_state.monster_debug_rays,
                                                _split_proj, self.view_matrix)
            if render_state is not None and render_state.monster_hitboxes is not None:
                self.debug_draw._render_monster_hitboxes(render_state.monster_hitboxes,
                                                         _split_proj, self.view_matrix)
            if self.play_mode and self.show_spatial_grid:
                self.debug_draw._render_spatial_grid(_split_proj, self.view_matrix)
            if (self.show_glasses or _spectator_active) and render_state is not None:
                self.debug_draw._render_player_glasses(
                    self._render_config.get("player_glasses_positions", ()),
                    _split_proj,
                    self.view_matrix,
                    force=_spectator_active,
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
            self._publish_terrain(_split_proj, _p2_view, _p2_cam_pos)
            self.renderer.render_scene(
                _split_proj, _p2_view, _p2_cam_pos,
                _selection, self._render_config,
                clear=False, brush_slots=_p2_brush_slots
            )

            if render_state:
                self.debug_draw._render_bullet_marks(render_state.bullet_marks, _split_proj, _p2_view)
            if render_state is not None and len(render_state.projectiles):
                self.debug_draw._render_projectiles(render_state.projectiles, _split_proj, _p2_view)
            if render_state and render_state.monster_debug_active:
                self.debug_draw._render_monster_debug_rays(render_state.monster_debug_rays,
                                                _split_proj, _p2_view)
            if render_state is not None and render_state.monster_hitboxes is not None:
                self.debug_draw._render_monster_hitboxes(render_state.monster_hitboxes,
                                                         _split_proj, _p2_view)
            if self.play_mode and self.show_spatial_grid:
                self.debug_draw._render_spatial_grid(_split_proj, _p2_view)
            if (self.show_glasses or _spectator_active) and render_state is not None:
                self.debug_draw._render_player_glasses(
                    self._render_config.get("player_glasses_positions", ()),
                    _split_proj,
                    _p2_view,
                    force=_spectator_active,
                )


            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glViewport(0, 0, _w, _h)
        else:
            self._publish_terrain(self.projection_matrix, self.view_matrix, camera_pos)
            self.renderer.render_scene(
                self.projection_matrix, self.view_matrix, camera_pos,
                _selection, self._render_config,
                brush_slots=_main_brush_slots,
            )
            # Native overhead player sprite (top-down mode), depth-tested so
            # walls occlude it correctly.
            self._draw_overhead_sprite(render_state)
            if self.play_mode and render_state is not None:
                self.debug_draw._render_player_glasses(_spectator_positions, self.projection_matrix, self.view_matrix,
                                            sprites=_spectator_sprites, force=True)
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
                        # The collision records stay here: the renderer is
                        # handed line vertices (engine/overlay_geometry.py).
                        _lines, _runs = collision_wireframes(filtered)
                        self.renderer.draw_collision_visualization(
                            self.projection_matrix, self.view_matrix, _lines, _runs)
            if render_state:
                self.debug_draw._render_bullet_marks(render_state.bullet_marks, self.projection_matrix, self.view_matrix)
            if render_state is not None and len(render_state.projectiles):
                self.debug_draw._render_projectiles(render_state.projectiles, self.projection_matrix, self.view_matrix)
            if render_state and render_state.monster_debug_active:
                self.debug_draw._render_monster_debug_rays(render_state.monster_debug_rays,
                                                self.projection_matrix, self.view_matrix)
            if render_state is not None and render_state.monster_hitboxes is not None:
                self.debug_draw._render_monster_hitboxes(render_state.monster_hitboxes,
                                                         self.projection_matrix, self.view_matrix)
            if self.play_mode and self.show_spatial_grid:
                self.debug_draw._render_spatial_grid(self.projection_matrix, self.view_matrix)
        if not self.play_mode and self.editor.show_logic_links:
            _scene_ver = (len(self.editor.state.brushes), len(self.editor.state.things),
                          self.editor.state.view_filters.version)
            if self._io_conn_cache is None or self._io_conn_scene_ver != _scene_ver:
                self._io_conn_cache     = self.debug_draw._gather_io_connections()
                self._io_conn_scene_ver = _scene_ver
            conn_lines = self._io_conn_cache
            if conn_lines:
                # The records' ends are the live position lists of brushes
                # and Things, so a dragged object's line follows it; they
                # are read here, and the renderer is handed arrays.
                _segments, _colours = connection_segments(conn_lines)
                self.renderer.draw_connection_lines(
                    self.projection_matrix, self.view_matrix, _segments, _colours)
        if self.face_mode_active and self.hovered_face_info:
            brush, face_name = self.hovered_face_info
            self.renderer.draw_face_highlight(
                self.projection_matrix, self.view_matrix,
                face_highlight_triangles(brush, face_name))
        # Component handles.  The arrays come from the editor's controller,
        # which rebuilds them only when the selection, hover or geometry
        # changed; here it is a version check and a draw call.
        if not self.play_mode:
            _components = self.components._components()
            if _components is not None and _components.is_component_mode():
                _targets = self.components._component_targets()
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
            self.hud._draw_underwater_overlay(painter, render_state)
        if self.editor.config.getboolean('Display', 'show_fps', fallback=False):
            self.hud._draw_fps_counter(painter)
        if self.play_mode and self.show_sprites_in_play_mode:
            self.hud._draw_sprites_text(painter)
        if self.play_mode and self.show_render_menu:
            self.hud._draw_render_menu(painter)
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
                self.hud._draw_hud_splitscreen(painter, render_state)
            else:
                self.hud._draw_hud(painter, render_state)
        if (self.play_mode and render_state is not None
                and render_state.damage_marks and not render_state.player_dead):
            # Not part of the HUD's style or opacity: its own setting
            # ([Display] damage_indicators). Player one's, over the whole view.
            self.hud._draw_damage_indicators(painter, render_state)
        if self.play_mode and render_state and render_state.player_dead:
            self.hud._draw_death_screen(painter)
        if self.play_mode and self._cached_level_complete_ui:
            self.hud._draw_level_complete_overlay(painter)
        if self.play_mode:
            logic_thread = self.logic_thread
            if logic_thread is not None:
                for line, text in logic_thread.cutscene_runtime.consume_cinematic_messages():
                    if line == "message2":
                        self.messages.show_view_message2(text)
                    elif line == "message3":
                        self.messages.show_view_message3(text)
                    else:
                        self.messages.show_view_message(text)
            self.messages._draw_view_message(painter, self.width(), self.height())
            self.messages._draw_view_message2(painter, self.width(), self.height())
            self.messages._draw_view_message3(painter, self.width(), self.height())

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
        self.terrain_edit._draw_terrain_brush_cursor(painter)
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

        # The pause menu covers everything, plugin overlays included; its
        # backdrop outlives it while it fades out over the resumed game.
        if self.play_mode and (self.pause_menu is not None or self._pause_backdrop is not None
                               or self._pause_fade is not None):
            self.pause._draw_pause_menu(painter)

        painter.end()


    # =========================================================================
    # PAUSE MENU (Esc in Play Mode; plugin API 1.7.0)
    # =========================================================================


    # -- pages -----------------------------------------------------------------


    # -- the backdrop's fade ---------------------------------------------------


    # -- input -----------------------------------------------------------------


    # -- drawing ---------------------------------------------------------------


    def load_texture(self, texture_name, subfolder):
        return self.renderer.load_texture(texture_name, subfolder) if self.renderer else 0

    def load_all_sprite_textures(self):
        things = {
            'Light': 'light.png',
            'Monster': 'monster.png',
            'Prop': 'pickup.png',
            'Speaker': 'speaker.png',
            'LevelChanger': 'levelchanger.png',
            'LogicCommand': 'logic_command.png',
        }
        for cls, fname in things.items():
            tid = self.load_texture(fname, 'sprites')
            if tid:
                self.sprite_textures[cls] = tid
        # Every glasses style, so changing Appearance needs no reload.
        for style, _label, fname in GLASSES_STYLES:
            tid = self.load_texture(fname, GLASSES_SUBFOLDER)
            if tid:
                self.sprite_textures[glasses_sprite_key(style)] = tid
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

    def toggle_play_mode(self, player_start=None, physics_enabled=True):
        """Enter Play with the player at *player_start* (a PlayerStart), or leave it."""
        if not self.play_mode:
            self.stop_editor_mouselook()
            self.decal_tool.set_active(False)
        self.play_mode = not self.play_mode
        if self.play_mode:
            player_start_pos = player_start.pos
            player_start_angle = player_start.get_angle()
            # Force split-screen OFF when entering play mode
            self.splitscreen_mode = False
            self._last_player_start_pos = player_start_pos
            self._last_player_start_angle = player_start_angle
            self._recentre_look_cursor()
            QApplication.setOverrideCursor(Qt.BlankCursor)
            # Establish mouse capture as part of entering Play, not on the
            # next 16 ms update-loop poll. The toolbar's activating click has
            # already been handled; subsequent clicks belong to the game view.
            self._sync_play_mouse_grab(self.window().isActiveWindow())

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

            self._play_mode_hint = "ESC to Pause, F12 Fullscreen"
            self._play_mode_hint_timer.start(3000)

            self.hud._reload_hud_settings()
            self._hud_runtime_visible = None
            if self.logic_thread:
                self.logic_thread.player_runtime.player = self.player
                self.logic_thread.render_runtime.set_hud_fade_enabled(self._hud_fade_enabled)
                self.logic_thread.session_runtime.apply_play_mode(
                    True, spawn_start=player_start)

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
            self.audio.stop_all_sounds()
            self._actor_pick = None
            self.actor_pick_hover = None
            if self.console_overlay_active:
                self._console_input.hide()
                self.console_overlay_active = False
            # The world pause it held ends with the session.
            self.pause_menu = None
            self.pause._end_pause_fade()
            self.pause._drop_pause_window()
            self.show_spatial_grid = False
            if self.logic_thread:
                self.logic_thread.monster_ai.monster_debug_active = False
                self.logic_thread.combat_runtime.show_hitboxes = False
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self.setCursor(Qt.ArrowCursor)
            self._release_play_mouse_grab()
            self._attach_mac_cursor()
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
        self.editor.show_toast(f"Split-Screen: {status}  [F9]")

    def _exit_play_mode(self):
        if not self.play_mode:
            return
        # Prefer the editor's full teardown so the Play button colour, mode
        # label, properties tab and focus are all restored to editor state.
        # Reached e.g. when ESC is pressed after the player dies; without this
        # the Play button would stay red after returning to the editor.
        self.editor._exit_play_mode()

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
        a copy, so this only has to run when one of them is created or swapped.
        """
        if self.renderer:
            self.renderer.view_distance = self.view_distance

    @property
    def renderer_mode(self):
        """The registry name of the active renderer."""
        return self._renderer_mode

    def switch_renderer(self, mode: str):
        """Replace the active renderer with the one registered as *mode*, live.

        Works in the editor and in play: the frame input is rebuilt every
        frame, so a renderer needs nothing from its predecessor. The new
        renderer is created first, in this view's GL context; only once it is
        ready does it take over and the old one release its resources, so a
        renderer that fails to start leaves the current one running.
        Session settings (view distance, shadows, water quality) carry over,
        and the sprite table is reloaded through the new renderer, because the
        old renderer's textures die with it. Returns True when *mode* is the
        active renderer afterwards.
        """
        if self.renderer is not None and mode == self._renderer_mode:
            return True
        if mode not in available_renderers():
            print(f"[QtGameView] Unknown renderer '{mode}'; available: "
                  f"{', '.join(available_renderers())}")
            return False
        print(f"[QtGameView] Switching renderer: {self._renderer_mode} → {mode}")
        self.makeCurrent()
        try:
            try:
                new = create_renderer(mode, self.editor.config)
            except Exception as exc:
                print(f"[QtGameView] Renderer '{mode}' failed to start: {exc}")
                return False
            if not new.ready:
                print(f"[QtGameView] Renderer '{mode}' is not ready; keeping "
                      f"{self._renderer_mode}.")
                new.cleanup()
                return False
            old = self.renderer
            if old is not None:
                new.shadows_enabled = old.shadows_enabled
                new.water_quality = old.water_quality
            # Drop every GL name the host holds from the old renderer before it
            # is retired; the new one hands out its own.
            self.renderer = new
            self._renderer_mode = mode
            self._sync_view_distance()
            self.sprite_textures = {}
            self.load_all_sprite_textures()
            self.preload_level_textures()
            self.grid_dirty = True
            if old is not None:
                try:
                    old.cleanup()
                except Exception as exc:
                    print(f"[QtGameView] Renderer cleanup warning: {exc}")
            print(f"[QtGameView] Renderer switched to {mode}.")
            return True
        finally:
            self.doneCurrent()
            self.update()

    @contextlib.contextmanager
    def gl_context(self):
        """Make this view's GL context current for GL work outside paintGL.

        Outside paintGL the current context is usually Qt's own compositing
        context, which shares buffers and textures with this one but not VAOs:
        GL calls made there free the wrong objects and corrupt driver state.
        A no-op when this context is already current (inside paintGL), before
        the context exists or after the view is deleted, or off the GUI
        thread, where makeCurrent is unsafe.
        """
        ctx = None if sip.isdeleted(self) else self.context()
        if (ctx is None or not ctx.isValid()
                or QOpenGLContext.currentContext() is ctx
                or QThread.currentThread() is not self.thread()):
            yield
            return
        self.makeCurrent()
        try:
            yield
        finally:
            self.doneCurrent()

    def get_primary_selection_pos(self):
        if not self.editor.primary_selection():
            return None
        if isinstance(self.editor.primary_selection(), dict):
            return glm.vec3(self.editor.primary_selection().get('pos', [0, 0, 0]))
        return glm.vec3(self.editor.primary_selection().pos)

    def set_primary_selection_pos(self, new_pos_vec):
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


    # -- Placed texture stamps: select, drag, nudge ---------------------------


    # ======================================================================
    # Component editing in the 3D view (object / face / edge / vertex)
    # ======================================================================
    #
    # All of this runs from mouse events.  Picking tests only the brushes in
    # the current selection and reuses the geometry cache the renderer already
    # keeps, so nothing here touches the render loop or the wider scene.


    def mousePressEvent(self, event):
        if self.play_mode and self.pause_menu is not None:
            window = self._pause_window
            if event.button() == Qt.LeftButton and window is not None and window.hit(event.pos()):
                in_body = event.y() >= window.window_rect.y() + window.HEADER_H
                window.handle_mouse_press(event)
                if not window.active:              # its [X]: play on
                    self.pause.close_pause_menu()
                elif in_body and not window.dragging:
                    self.pause._click_pause_menu(event.x(), event.y())
                self.update()
            return
        if self.play_mode and self._actor_pick is not None:
            if event.button() == Qt.RightButton:
                self.picking.cancel_actor_pick()
            elif event.button() == Qt.LeftButton:
                self.picking._click_actor_pick(event.x(), event.y())
            return
        if (self.play_mode and getattr(self, '_cached_level_complete_ui', None)
                and getattr(self, '_level_complete_btn_rect', None)):
            if self._level_complete_btn_rect.contains(event.pos()):
                self.hud._confirm_level_complete()
                return

        if self.terrain_sculpt_active and not self.play_mode and event.button() == Qt.LeftButton:
            self._terrain_brush_mouse_pos = event.pos()
            self._terrain_brush_hit = self.terrain_edit.raycast_terrain(event.x(), event.y())
            if self.terrain_sculpt_mode == 'stamp' and self.terrain_edit._press_terrain_stamp(event):
                return
            self.terrain_edit._apply_sculpt_at_mouse(event.x(), event.y())
            return
        if self.decal_tool.active and not self.play_mode and event.button() == Qt.LeftButton:
            self.decal_tool.press(event)
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
                self.editor.clone_placement_active()):
            self.editor.finish_clone_placement()
            return

        # --- Component modes: the press grabs a vertex / edge / face ---
        # Same contextual rules as the 2D views: plain drags the component,
        # Shift adds it to the component selection, Ctrl on a face shears it.
        if (not self.play_mode and event.button() == Qt.LeftButton and
                self.components._component_mode_active()):
            ref = self.components._pick_component_3d(event.x(), event.y())
            if ref is not None:
                modifiers = QApplication.keyboardModifiers()
                shear = bool(modifiers & Qt.ControlModifier) and \
                    ref.kind == component_edit.MODE_FACE
                additive = bool(modifiers & Qt.ShiftModifier)
                if self.components._begin_component_drag(ref, event.pos(), shear=shear,
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
            face = self.picking.get_face_at(event.pos())
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
            if published('active_weapon'):
                # shot_ready is the held weapon's own verdict: False for an item
                # that never fires, during a cooldown, or out of ammunition --
                # so clicks never pile up behind any of them.
                if published('shot_ready', False):
                    self.game_state.queue_shot()
                return
        _shift_select = (Qt.ShiftModifier, Qt.ShiftModifier | Qt.AltModifier)
        if (event.button() == Qt.LeftButton and not self.play_mode and
                QApplication.keyboardModifiers() in _shift_select):
            # Shift+click selects; adding Alt cycles down through whatever else
            # the ray passes through, so buried geometry stays reachable.
            cycle = bool(QApplication.keyboardModifiers() & Qt.AltModifier)
            obj = self.picking.get_object_at_3d(event.x(), event.y(), cycle=cycle)
            if obj:
                self.editor.save_state()
                self.editor.set_selected_objects([obj])
                self.update()
            return
        if event.button() == Qt.LeftButton and self.editor.primary_selection() and not self.play_mode:
            obj_pos = self.get_primary_selection_pos()
            if obj_pos:
                ray_o, ray_d = self.picking.get_ray_from_mouse(event.x(), event.y())
                best_dist = float('inf')
                hit_axis = None
                start_pt = None
                for axis, vec in [('x', glm.vec3(1,0,0)), ('y', glm.vec3(0,1,0)), ('z', glm.vec3(0,0,1))]:
                    pt, dist = self.picking.intersect_ray_with_axis(ray_o, ray_d, obj_pos, vec)
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
            self._editor_right_mouse_held = True
            self._sync_editor_mouselook()
        super().mousePressEvent(event)

    def _recentre_look_cursor(self):
        """Put the hidden mouse-look cursor in the middle of the view."""
        self._attach_mac_cursor()
        centre = self.rect().center()
        self.last_mouse_pos = QPoint(centre)
        QCursor.setPos(self.mapToGlobal(centre))
        self._skip_next_look_poll = True

    def _mouse_look_captured(self):
        """Whether mouse movement turns the camera, with the cursor held."""
        if self.mouselook_active:
            return True
        return (self.play_mode and self.pause_menu is None
                and not self.console_overlay_active and self._actor_pick is None)

    def _poll_mouse_look(self):
        """Turn by how far the cursor moved since the last frame; re-centre it.

        Read from the live cursor once a frame, never from the positions in
        mouse-move events. Re-centring with QCursor.setPos() is immediate on
        Windows but on macOS lands behind the moves already queued (many a
        frame from a trackpad), so measuring events against the centre
        counted those moves several times over, and letting the cursor roam
        between re-centres let a fast flick carry it out of the view, where
        it showed and the view stopped seeing the mouse. Polled, the cursor
        is back in the middle every frame and each movement counts once.
        """
        held = self._mouse_look_captured() and self.window().isActiveWindow()
        self._sync_play_mouse_grab(held)
        if self._mac_mouse is not None:
            self._poll_mouse_look_macos()
            return
        if not held:
            self._skip_next_look_poll = True   # it may be anywhere on return
            return
        centre = self.mapToGlobal(self.rect().center())
        if self._skip_next_look_poll:
            # The cursor was just sent here (or comes back from elsewhere):
            # nothing it shows yet is movement.
            self._skip_next_look_poll = False
            QCursor.setPos(centre)
            return
        cursor = QCursor.pos()
        dx, dy = cursor.x() - centre.x(), cursor.y() - centre.y()
        if dx or dy:
            self.game_state.set_mouse_delta(float(dx), float(dy))
            QCursor.setPos(centre)

    def _sync_play_mouse_grab(self, held):
        """Play: while the look holds the mouse, every click belongs to this view.

        The look only hides the cursor; it does not confine it. Wherever the
        real pointer is -- on macOS it is detached from the mouse where it
        stood, which can be over the Play button just clicked -- a click went
        to the widget under it, pressing Stop instead of firing. Grabbing the
        mouse sends every press to this view, as editor look already does
        (_sync_editor_mouselook). Released as soon as the look lets go
        (pause menu, console, actor pick, another window) and on leaving Play.
        """
        if not self.play_mode or self.mouselook_active:
            return              # editor look keeps its own grab
        grabber = self.mouseGrabber()
        if held and grabber is not self and self.isVisible():
            self.grabMouse()
        elif not held and grabber is self:
            self.releaseMouse()

    def _release_play_mouse_grab(self):
        if not self.mouselook_active and self.mouseGrabber() is self:
            self.releaseMouse()

    def restore_play_mouse_capture(self):
        """Reclaim the view after an enclosing UI operation has finished.

        A map transition can restart Play inside the outer map-loading
        overlay. The first grab then happens underneath that overlay and may
        be lost when it is finally hidden. This is called after the operation
        returns to the event loop, when the view can own focus and the mouse.
        """
        if (not self.play_mode or not self._mouse_look_captured()
                or not self.isVisible() or not self.window().isActiveWindow()):
            return False
        self.setFocus(Qt.OtherFocusReason)
        self._recentre_look_cursor()
        self._sync_play_mouse_grab(True)
        return self.mouseGrabber() is self

    def _attach_mac_cursor(self):
        """macOS: let the mouse move the cursor again, if it was detached."""
        if self._mac_cursor_detached:
            import macos_app
            macos_app.set_cursor_follows_mouse(True)
            self._mac_cursor_detached = False

    def _poll_mouse_look_macos(self):
        """macOS mouse look: a detached cursor, turned by each event's own movement.

        Re-centring the cursor every frame does not work here: QCursor.setPos()
        lands asynchronously, so the position read back could predate it and
        count movement twice or jump. Instead the cursor is put in the middle
        of the view once and detached from the mouse -- it cannot wander out
        of the view or show -- and the turn is the movement every NSEvent
        reports (deltaX/deltaY), summed by the native event filter.
        """
        mouse = self._mac_mouse
        if not self._mouse_look_captured() or not self.window().isActiveWindow():
            self._attach_mac_cursor()
            mouse.take()                       # movement made with the cursor free
            self._skip_next_look_poll = True
            return
        if self._skip_next_look_poll:
            # Capture starts: put the cursor in the middle, detach it once
            # that move has landed.
            self._skip_next_look_poll = False
            self._mac_centre_tries = 0
            QCursor.setPos(self.mapToGlobal(self.rect().center()))
            mouse.take()
            return
        if not self._mac_cursor_detached:
            # Qt sends the move as an event, which need not have landed by
            # the next frame (above all straight after a long load). Detached
            # before it lands, the cursor stays pinned where it was -- over
            # the Play button just clicked -- so wait for it, a few frames at
            # most, asking again in case a mouse movement overtook it.
            centre = self.mapToGlobal(self.rect().center())
            if ((QCursor.pos() - centre).manhattanLength() > 2
                    and self._mac_centre_tries < self._MAC_CENTRE_MAX_TRIES):
                self._mac_centre_tries += 1
                QCursor.setPos(centre)
                mouse.take()
                return
            import macos_app
            self._mac_cursor_detached = macos_app.set_cursor_follows_mouse(False)
            mouse.take()
            return
        dx, dy = mouse.take()
        if dx or dy:
            self.game_state.set_mouse_delta(dx, dy)

    def mouseMoveEvent(self, event):
        if not self.play_mode and self.floating_windows.handle_mouse_move(
                event, self.width(), self.height()):
            self.update()
            return
        if self.sysmon.handle_mouse_move(event, self.play_mode, self.width(), self.height()):
            self.update()
            return
        # A component drag owns the mouse until the button comes back up.
        controller = self.components._components()
        if controller is not None and controller.drag is not None:
            if event.buttons() & Qt.LeftButton:
                self.components._update_component_drag(event.pos())
                return
            self.components._end_component_drag()
        # Face Mode: a press that has moved far enough becomes a face drag.
        if self._face_mode_press is not None:
            if not (event.buttons() & Qt.LeftButton):
                self._face_mode_press = None
            elif (event.pos() - self._face_mode_press['pos']).manhattanLength() > 4:
                if self.components._begin_face_mode_drag(event.pos()):
                    return
                self._face_mode_press = None
        if (self._stamp_drag is not None and self.terrain_sculpt_active
                and not self.play_mode and event.buttons() & Qt.LeftButton):
            self._terrain_brush_mouse_pos = event.pos()
            self._terrain_brush_hit = self.terrain_edit.raycast_terrain(event.x(), event.y())
            self.terrain_edit._drag_terrain_stamps(event.x(), event.y())
            return
        if (self.decal_tool.dragging() and not self.play_mode
                and event.buttons() & Qt.LeftButton):
            self.decal_tool.drag(event.x(), event.y())
            return
        if (self.terrain_sculpt_active and not self.play_mode
                and not (event.buttons() & Qt.RightButton)):
            self._terrain_brush_mouse_pos = event.pos()
            self._terrain_brush_hit = self.terrain_edit.raycast_terrain(event.x(), event.y())
            self.update()
            return
        if self.play_mode and self.pause_menu is not None:
            window = self._pause_window
            if window is not None and window.handle_mouse_move(event, self.width(), self.height()):
                self.update()
                return
            self.pause._hover_pause_menu(event.x(), event.y(),
                                   drag=bool(event.buttons() & Qt.LeftButton))
            return
        if self.mouselook_active:
            return              # turning is read once a frame: _poll_mouse_look
        if self.play_mode:
            if self.console_overlay_active:
                return
            if self._actor_pick is not None:
                self.picking._update_actor_pick_hover(event.x(), event.y())
                return
            return              # turning is read once a frame: _poll_mouse_look
        if (self.terrain_sculpt_painting and self.terrain_sculpt_active
                and self.terrain_sculpt_mode != 'stamp'):
            self.terrain_edit._apply_sculpt_at_mouse(event.x(), event.y())
            return
        if self.is_dragging_gizmo:
            ray_o, ray_d = self.picking.get_ray_from_mouse(event.x(), event.y())
            axis_vec = {'x': glm.vec3(1,0,0), 'y': glm.vec3(0,1,0), 'z': glm.vec3(0,0,1)}[self.gizmo_drag_axis]
            pt, _ = self.picking.intersect_ray_with_axis(ray_o, ray_d, self.gizmo_object_start_pos, axis_vec)
            if pt:
                diff = pt - self.drag_start_on_axis
                self.set_primary_selection_pos(self.gizmo_object_start_pos + diff)
            return
        # Component hover: highlight what a press would grab.  Selection-scoped
        # and repainted only when the highlight actually changes.
        if self.components._component_mode_active() and not event.buttons():
            if controller.set_hover(self.components._pick_component_3d(event.x(), event.y())):
                self.update()
            return
        if self.face_mode_active:
            self.hovered_face_info = self.picking.get_brush_face_at_coords(event.x(), event.y())
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.play_mode and self.pause_menu is not None:
            if self._pause_window is not None:
                self._pause_window.handle_mouse_release(event)
            return
        if event.button() == Qt.LeftButton and self._stamp_drag is not None:
            self.terrain_edit._end_stamp_drag()
            return
        if event.button() == Qt.LeftButton and self.decal_tool.dragging():
            self.decal_tool.release()
            return
        controller = self.components._components()
        if (event.button() == Qt.LeftButton and controller is not None and
                controller.drag is not None):
            self.components._end_component_drag()
            return
        if event.button() == Qt.LeftButton and self._face_mode_press is not None:
            # Released without dragging: this was a texture click after all.
            brush, face = self._face_mode_press['face']
            self._face_mode_press = None
            self.editor.apply_texture_to_specific_face(brush, face)
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
        if not self.play_mode and event.button() == Qt.RightButton:
            self._editor_right_mouse_held = False
            self._sync_editor_mouselook()
        super().mouseReleaseEvent(event)

    def _sync_editor_mouselook(self):
        """Editor mouse look is on while Z has latched it or right is held."""
        active = self.editor_freelook_active or self._editor_right_mouse_held
        if active and not self.mouselook_active:
            self.mouselook_active = True
            self._recentre_look_cursor()
            self.game_state.consume_mouse_delta()
            self.setCursor(Qt.BlankCursor)
        elif not active and self.mouselook_active:
            self.mouselook_active = False
            self._attach_mac_cursor()
            self.game_state.consume_mouse_delta()
            self.setCursor(Qt.ArrowCursor)
        # A latched look keeps the mouse even off the view: a click anywhere
        # must not land in another widget while the cursor is hidden.
        if active and self.mouseGrabber() is not self:
            self.grabMouse()
        elif not active and self.mouseGrabber() is self:
            self.releaseMouse()

    def set_editor_freelook(self, active):
        """Latch editor mouse look independently of the held right button."""
        if self.play_mode:
            return
        self.editor_freelook_active = bool(active)
        if active:
            self.setFocus(Qt.ShortcutFocusReason)
        self._sync_editor_mouselook()

    def stop_editor_mouselook(self):
        self.editor_freelook_active = False
        self._editor_right_mouse_held = False
        self._sync_editor_mouselook()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self.play_mode:
            held = (self._mouse_look_captured()
                    and self.window().isActiveWindow())
            self._sync_play_mouse_grab(held)

    def focusOutEvent(self, event):
        if self.play_mode:
            # Release immediately on a focus transition (another window,
            # console or pause UI); the next focus-in restores capture.
            self._sync_play_mouse_grab(False)
            self._attach_mac_cursor()
            self.game_state.consume_mouse_delta()
            self._skip_next_look_poll = True
        elif self.mouselook_active:
            self.stop_editor_mouselook()
            self.editor.keys_pressed.clear()
            self.game_state.set_keys(set())
        super().focusOutEvent(event)

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
        self._console_input.reset_history_position()

    def _close_console_overlay(self):
        self.console_overlay_active = False
        self._console_input.hide()
        self._console_input.clearFocus()
        self.setFocus()
        if self.play_mode:
            if self._actor_pick is not None:
                # A command just armed a pick: the cursor stays free for it.
                self.picking._show_pick_cursor()
            else:
                self._capture_play_cursor()

    def _capture_play_cursor(self):
        """Hide the cursor and re-centre it for mouse look (Play Mode)."""
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        QApplication.setOverrideCursor(Qt.BlankCursor)
        self._recentre_look_cursor()


    # =========================================================================
    # ACTOR PICK (Play Mode)
    # =========================================================================


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
        if not self.play_mode and event.key() == Qt.Key_Escape and self.mouselook_active:
            self.stop_editor_mouselook()
            event.accept()
            return
        # An armed actor pick owns Escape: it cancels the pick, not Play Mode.
        if (event.key() == Qt.Key_Escape and self.play_mode
                and self._actor_pick is not None):
            self.picking.cancel_actor_pick()
            return
        # The Decal tool owns Escape and Delete while it is on.
        if self.decal_tool.active and not self.play_mode:
            if event.key() == Qt.Key_Escape:
                self.decal_tool.escape()
                self.setFocus()
                return
            if (event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                    and self.decal_tool.delete_selected()):
                return
        # Sculpt mode owns Escape while active. Do this before normal
        # editor/play-mode escape handling.
        if event.key() == Qt.Key_Escape and self.terrain_sculpt_active and not self.play_mode:
            if self.terrain_edit.clear_stamp_selection():
                return
            self.terrain_edit.set_terrain_sculpt_active(False)
            self.setFocus()
            return

        if self.play_mode and self._cached_level_complete_ui:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_E):
                self.hud._confirm_level_complete()
                return
            elif event.key() == Qt.Key_Escape:
                self.hud._cancel_level_complete()
                return

        if (self.play_mode and not self.console_overlay_active
                and self.pause.handle_pause_menu_key(event)):
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
            self.editor.set_connection_links_enabled(not current_state)
            return
        if check_key('key_toggle_wireframe', 'F2'):
            if self.current_render_mode == RENDER_MODE_WIREFRAME:
                self.current_render_mode = RENDER_MODE_LIT
            else:
                self.current_render_mode = RENDER_MODE_WIREFRAME
            mode_name = self.render_mode_names.get(self.current_render_mode, "Unknown")
            self.editor.show_toast(f"Render Mode: {mode_name}")
            self.update()
            return
        if check_key('key_sysmon', 'F3'):
            self.editor.toggle_system_monitor()
            return
        if self.play_mode and event.key() == Qt.Key_F7:
            # The Monster Debug window, with the sight lines it switches on.
            from editor.monster_debug import toggle_monster_debug
            opened = toggle_monster_debug(self.editor)
            self.editor.show_toast(f"Monster Debug: {'ON' if opened else 'OFF'}")
            self.setFocus()                  # keep playing: keys stay with the game
            self.update()
            return
        if self.play_mode and event.key() == Qt.Key_F6:
            if self.logic_thread:
                new_state = self.logic_thread.collision_runtime.toggle_model_collision()
                status = "ON" if new_state else "OFF"
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
                new_size = max(2, self.grid_size // 2)
                self.editor.set_grid_size(new_size)
                self.editor.show_toast(f"Grid Size: {new_size}")
                return
            elif event.key() == Qt.Key_BracketRight:
                new_size = min(128, self.grid_size * 2)
                self.editor.set_grid_size(new_size)
                self.editor.show_toast(f"Grid Size: {new_size}")
                return
        if self.play_mode and not self.show_render_menu and event.key() in self._SLOT_KEYS:
            # Weapon slots 1-4 (gun1, gun2, custom1, custom2): the logic thread
            # takes the weapon in hand if the player has it.
            self.game_state.queue_weapon_slot(self._SLOT_KEYS[event.key()])
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

    # -- delegated responsibilities ------------------------------------------
    # The HUD, messages, pause menu, terrain editing, component editing,
    # picking and debug drawing live in engine/view_*.py, each a small class
    # holding this view. These names are kept here for callers outside.

    ACTOR_PICK_PAUSE = ViewPicking.ACTOR_PICK_PAUSE

    HUD_CORNER_GAP = ViewHud.HUD_CORNER_GAP

    HUD_CORNER_ICON = ViewHud.HUD_CORNER_ICON

    HUD_KEY_STEP = ViewHud.HUD_KEY_STEP

    PAUSE_BLUR_FACTOR = ViewPause.PAUSE_BLUR_FACTOR

    PAUSE_FADE_SECONDS = ViewPause.PAUSE_FADE_SECONDS

    PAUSE_MENU_FALLBACK_FONT = ViewPause.PAUSE_MENU_FALLBACK_FONT

    PAUSE_MENU_FONT = ViewPause.PAUSE_MENU_FONT

    WEAPON_SWITCH_FADE_IN = ViewHud.WEAPON_SWITCH_FADE_IN

    WEAPON_SWITCH_FADE_OUT = ViewHud.WEAPON_SWITCH_FADE_OUT

    WEAPON_SWITCH_HOLD = ViewHud.WEAPON_SWITCH_HOLD

    _axialize = staticmethod(ViewComponents._axialize)

    def _component_targets(self, *args, **kwargs):
        return self.components._component_targets(*args, **kwargs)

    def _components(self, *args, **kwargs):
        return self.components._components(*args, **kwargs)

    _hud_corner_layout = staticmethod(ViewHud._hud_corner_layout)

    _hud_count_baseline = staticmethod(ViewHud._hud_count_baseline)

    _hud_health_point_size = staticmethod(ViewHud._hud_health_point_size)

    _hud_stat_lines = staticmethod(ViewHud._hud_stat_lines)

    def _init_debug_resources(self, *args, **kwargs):
        return self.debug_draw._init_debug_resources(*args, **kwargs)

    _pause_clock = staticmethod(ViewPause._pause_clock)

    _plugin_manager = staticmethod(ViewPause._plugin_manager)

    _weapon_switch_alpha = staticmethod(ViewHud._weapon_switch_alpha)

    def _world_per_pixel(self, *args, **kwargs):
        return self.components._world_per_pixel(*args, **kwargs)

    def _world_to_screen(self, *args, **kwargs):
        return self.picking._world_to_screen(*args, **kwargs)

    @property
    def actor_pick_active(self):
        return self.picking.actor_pick_active

    def begin_actor_pick(self, *args, **kwargs):
        return self.picking.begin_actor_pick(*args, **kwargs)

    def cancel_component_drag(self, *args, **kwargs):
        return self.components.cancel_component_drag(*args, **kwargs)

    def clear_stamp_selection(self, *args, **kwargs):
        return self.terrain_edit.clear_stamp_selection(*args, **kwargs)

    def close_pause_menu(self, *args, **kwargs):
        return self.pause.close_pause_menu(*args, **kwargs)

    def get_object_at_3d(self, *args, **kwargs):
        return self.picking.get_object_at_3d(*args, **kwargs)

    def handle_pause_menu_key(self, *args, **kwargs):
        return self.pause.handle_pause_menu_key(*args, **kwargs)

    def open_pause_menu(self, *args, **kwargs):
        return self.pause.open_pause_menu(*args, **kwargs)

    @property
    def pause_menu_active(self):
        return self.pause.pause_menu_active

    def set_hud_fade_enabled(self, *args, **kwargs):
        return self.hud.set_hud_fade_enabled(*args, **kwargs)

    def set_hud_opacity(self, *args, **kwargs):
        return self.hud.set_hud_opacity(*args, **kwargs)

    def set_hud_style(self, *args, **kwargs):
        return self.hud.set_hud_style(*args, **kwargs)

    def set_terrain_sculpt_active(self, *args, **kwargs):
        return self.terrain_edit.set_terrain_sculpt_active(*args, **kwargs)

    def show_pos_window(self, *_signal_args):
        # Debug > Position (P) is connected here, and QAction.triggered
        # passes its "checked" flag, which the window has no use for.
        return self.debug_draw.show_pos_window()

    def show_view_message(self, *args, **kwargs):
        return self.messages.show_view_message(*args, **kwargs)

    def show_view_message2(self, *args, **kwargs):
        return self.messages.show_view_message2(*args, **kwargs)

    def show_view_message3(self, *args, **kwargs):
        return self.messages.show_view_message3(*args, **kwargs)
