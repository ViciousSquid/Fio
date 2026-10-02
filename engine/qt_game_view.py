import time
import os
import numpy as np
import ctypes
from collections import deque
from typing import Optional
from PyQt5.QtWidgets import QOpenGLWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QPoint, QUrl, QRect
from PyQt5.QtGui import QPainter, QColor, QFont, QCursor, QFontDatabase, QPen, QBrush, QPolygon, QKeySequence, QPixmap
from PyQt5.QtMultimedia import QSoundEffect
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
import glm
from engine.camera import Camera
from editor.things import Thing, Light, PlayerStart, Monster, Pickup, Speaker, LogicGate
from engine.player import Player
from PIL import Image
from .renderer import Renderer
from engine import shaders
from engine.threaded_game_state import ThreadedGameState, RenderState
from engine.logic_thread import LogicThread
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.debug_console import DebugConsole, get_debug_logger


def perspective_projection(fov, aspect, near, far):
    if aspect == 0:
        return glm.mat4(1.0)
    return glm.perspective(glm.radians(fov), aspect, near, far)


class QtGameView(QOpenGLWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        
        # Rendering and view state
        self.brush_display_mode = "Solid Lit"
        self.show_triggers_as_solid = False
        self.camera = Camera()
        self.camera.pos = glm.vec3(0, 150, 400)
        self.debug_console_window = DebugConsole()
        self.grid_size, self.world_size = 16, 2048
        self.grid_dirty = True
        self.culling_enabled = True 
        self.selected_object = None
        self.show_sprites_in_play_mode = False
        self.visibility_system = None
        self.show_visibility_debug = False
        self.grid_visible = True
        
        # Audio setup: Initialize Sound Pool
        self.sound_pool = {} # Map of filename -> list of QSoundEffect
        self._init_sound_system()

        # Initialize render mode names mapping for notifications
        self.render_mode_names = {
            RENDER_MODE_LIT: "Lit",
            RENDER_MODE_UNLIT: "Unlit",
            RENDER_MODE_WIREFRAME: "Wireframe",
            RENDER_MODE_VERTEX: "Vertex"
        }

        self.sysmon_expanded = False
        self.sysmon_stats = {
            'visible_brushes': 0,
            'visible_tris': 0,
            'visible_surfaces': 0,
            'culled_brushes': 0,
            'culled_tris': 0,
            'culled_surfaces': 0
        }

        # Threading Initialization
        self.game_state = ThreadedGameState()
        self.game_state.sound_queue = [] 
        
        self.logic_thread: Optional[LogicThread] = None
        self.use_threading = True 
        self._thread_started = False  # <--- THIS WAS MISSING causing the error

        # Face Mode Initialization
        self.face_mode_active = False
        self.hovered_face_info = None

        # Input state
        self.mouselook_active = False
        self.last_mouse_pos = QPoint()
        
        # Debug Window Manager State
        self.debug_mode_active = False
        self.debug_window_rect = QRect(20, 20, 400, 200)
        self.frame_times = deque(maxlen=100)
        self.console_font = QFont("Arial", 9)
        self.console_font.setStyleHint(QFont.Monospace)
        
        self.texture_manager = {}
        self.sprite_textures = {}
        self.gun_hud_pixmaps = {}
        self.renderer = None

        # Debug Rendering Resources
        self.debug_shader = None
        self.debug_vao = None
        self.debug_vbo = None

        self.show_render_menu = False
        self.current_render_mode = RENDER_MODE_LIT
        
        self.play_mode = False
        self.player = None
        
        self.fps = 0
        self.frame_count = 0
        self.last_time = time.perf_counter()
        self.last_fps_time = time.perf_counter()
        self.start_time = time.perf_counter()
        
        # Pre-allocated render config
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
        self.projection_matrix = glm.mat4(1.0)
        self.view_matrix = glm.mat4(1.0)
        self._cached_aspect_ratio = 1.0
        self.cull_distance = 4096
        
        # Matrix pointers for raw OpenGL calls
        self._proj_ptr = None
        self._view_ptr = None

        timer = QTimer(self)
        timer.setInterval(16)
        timer.timeout.connect(self.update_loop)
        timer.start()
        
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(True)

    def _init_sound_system(self):
        """Preload all sounds in assets/sounds to avoid lag during playback."""
        sound_dir = os.path.join(os.getcwd(), 'assets', 'sounds')
        if not os.path.exists(sound_dir):
            print("Warning: assets/sounds directory not found.")
            return

        print("Preloading sounds...")
        count = 0
        for f in os.listdir(sound_dir):
            if f.lower().endswith(('.wav', '.mp3')):
                full_path = os.path.join(sound_dir, f)
                # Create pool for this file
                self._preload_sound_file(f, full_path)
                count += 1
        print(f"Preloaded {count} sound files.")

    def _preload_sound_file(self, name, path, pool_size=4):
        """Creates a pool of QSoundEffects for a specific file to allow polyphony."""
        if name in self.sound_pool:
            return

        self.sound_pool[name] = []
        url = QUrl.fromLocalFile(path)
        
        for _ in range(pool_size):
            effect = QSoundEffect(self)
            effect.setSource(url)
            self.sound_pool[name].append(effect)

    def _get_sound_instance(self, name):
        """Retrieve an available sound instance from the pool."""
        # Handle full paths by stripping directory
        clean_name = os.path.basename(name)
        
        # Lazy load if not found (e.g. added during runtime or missed)
        if clean_name not in self.sound_pool:
            path = os.path.join(os.getcwd(), 'assets', 'sounds', clean_name)
            if os.path.exists(path):
                self._preload_sound_file(clean_name, path)
            else:
                return None
        
        pool = self.sound_pool[clean_name]
        
        # 1. Find an instance that isn't playing
        for effect in pool:
            if not effect.isPlaying():
                return effect
        
        # 2. If all are playing, create a new one using the same source (efficient)
        #    and add it to the pool for future use.
        if pool:
            source_url = pool[0].source()
            new_effect = QSoundEffect(self)
            new_effect.setSource(source_url)
            pool.append(new_effect)
            return new_effect
            
        return None

    def initializeGL(self):
        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
        try:
            pass
        except: pass

        # Pass config to renderer for ARM mode and shadow settings
        config = getattr(self.editor, 'config', None)
        self.renderer = Renderer(self.load_texture, self.grid_size, self.world_size, config)
        self.set_cull_distance(self.cull_distance)
        
        self._preload_assets()
        self.load_all_sprite_textures()
        if hasattr(self.editor, 'state') and hasattr(self.editor.state, 'brushes'):
            self.preload_level_textures()
        
        self._start_logic_thread()
        self._init_debug_resources()

        # Show the debug console immediately after loading finishes.
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
                compileShader(fs_src, gl.GL_FRAGMENT_SHADER)
            )
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
        if self._thread_started: return
        self.logic_thread = LogicThread(self.game_state, self.editor.state, self.visibility_system)
        self.logic_thread.set_editor_camera(self.camera.pos, self.camera.yaw, self.camera.pitch, self.camera.fov)
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
        if height > 0: self._cached_aspect_ratio = width / height
        else: self._cached_aspect_ratio = 1.0
        if self.logic_thread: self.logic_thread.set_frustum_aspect(self._cached_aspect_ratio)

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
        self.frame_times.append(delta * 1000.0)
        self._process_sound_queue()
        if self.use_threading and self.logic_thread:
            self.game_state.set_keys(self.editor.keys_pressed)
            if self.game_state.try_swap(): self.update()
        else: self.update()

    def _process_sound_queue(self):
        """Checks the game state for new sound requests and plays them using pooled objects."""
        if not hasattr(self.game_state, 'sound_queue'):
            return

        while self.game_state.sound_queue:
            # Pop the next sound request
            request = self.game_state.sound_queue.pop(0)
            sound_file = request.get('file')
            volume = request.get('volume', 1.0)
            
            if not sound_file:
                continue

            # Retrieve preloaded instance from pool
            effect = self._get_sound_instance(sound_file)
            
            if effect:
                effect.setVolume(volume)
                effect.play()
            else:
                # Debug only: warn if sound missing
                pass


    def paintGL(self):
        if not self.renderer: return

        if self.grid_dirty:
            self.renderer.update_grid_buffers(self.world_size, self.grid_size)
            self.grid_dirty = False

        camera_pos = glm.vec3(0, 0, 0)
        render_state: Optional[RenderState] = None

        if self.use_threading and self.logic_thread:
            render_state = self.game_state.get_render_state()
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
            self.view_matrix = self.camera.get_view_matrix()
            camera_pos = self.camera.pos
            brushes_to_render = self.editor.state.brushes
            things_to_render = self.editor.state.things
        
        self.projection_matrix = perspective_projection(self.camera.fov, self._cached_aspect_ratio, 0.1, 10000.0)

        # Update matrix pointers for raw OpenGL calls
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

        self.update_instance_textures(things_to_render)

        self.renderer.render_scene(
            self.projection_matrix, self.view_matrix, camera_pos,
            brushes_to_render, things_to_render,
            self.selected_object, self._render_config
        )

        # Render Bullet Marks
        if render_state and hasattr(render_state, 'bullet_marks'):
             self._render_bullet_marks(render_state.bullet_marks)

        # Face Mode Highlight
        if self.face_mode_active and self.hovered_face_info:
            brush, face_name = self.hovered_face_info
            if hasattr(self.renderer, 'draw_face_highlight'):
                self.renderer.draw_face_highlight(self.projection_matrix, self.view_matrix, brush, face_name)

        if render_state:
            self.sysmon_stats['visible_brushes'] = len(render_state.visible_brushes)
            self.sysmon_stats['culled_brushes'] = render_state.culled_brushes
            self.sysmon_stats['total_brushes'] = render_state.total_brushes

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self.editor.config.getboolean('Display', 'show_fps', fallback=False):
            self._draw_fps_counter(painter)
        if self.play_mode and self.show_sprites_in_play_mode:
            self._draw_sprites_text(painter)
        if self.play_mode and getattr(self, 'show_render_menu', False):
            self._draw_render_menu(painter)
        if self.play_mode and self.editor.config.getboolean('Display', 'show_hud', fallback=True):
            self._draw_hud(painter, render_state)
        if self.debug_mode_active:
            self._draw_window_manager(painter)
            
        if getattr(self.editor, 'show_logic_links', False):
            painter.setPen(QColor(255, 255, 0))
            painter.setFont(QFont("Arial", 10, QFont.Bold))
            painter.drawText(10, self.height() - 40, "LINKS VISIBLE [F1]")
            
        # Draw Face Mode UI Text
        if self.face_mode_active:
            # Fonts
            font_top = QFont("Arial", 14, QFont.Bold) 
            font_bot = QFont("Arial", 10, QFont.Bold)
            
            msg_top = "Select a FACE for texturing"
            msg_bot = "Press ESC to cancel"
            
            # Metrics for Top Line
            painter.setFont(font_top)
            mt = painter.fontMetrics()
            wt = mt.horizontalAdvance(msg_top)
            ht = mt.height()
            
            # Metrics for Bottom Line
            painter.setFont(font_bot)
            mb = painter.fontMetrics()
            wb = mb.horizontalAdvance(msg_bot)
            hb = mb.height()
            
            # Layout
            cx = self.width() // 2
            margin_bottom = 30
            spacing = 5
            padding_x = 20
            padding_y = 10
            
            total_text_h = ht + hb + spacing
            box_w = max(wt, wb) + (padding_x * 2)
            box_h = total_text_h + (padding_y * 2)
            
            rect_x = cx - box_w // 2
            rect_y = self.height() - box_h - margin_bottom
            
            # Background
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 180))
            painter.drawRoundedRect(rect_x, rect_y, box_w, box_h, 8, 8)
            
            # Draw Text
            painter.setPen(QColor(255, 255, 255))
            
            # Draw Top Line
            painter.setFont(font_top)
            # Center horizontally relative to box, vertical offset includes padding + ascent
            painter.drawText(rect_x + (box_w - wt)//2, rect_y + padding_y + mt.ascent(), msg_top)
            
            # Draw Bottom Line
            painter.setPen(QColor(200, 200, 200)) # Slightly dimmer
            painter.setFont(font_bot)
            painter.drawText(rect_x + (box_w - wb)//2, rect_y + padding_y + ht + spacing + mb.ascent(), msg_bot)

        painter.end()

    def _render_bullet_marks(self, marks):
        """Draw simple black dots at hit locations."""
        if not marks or 'simple' not in self.renderer.shaders: return
        
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        
        # Use Simple shader (Uniform Color)
        shader = self.renderer.shaders['simple']
        uniforms = self.renderer.uniforms['simple']
        gl.glUseProgram(shader)
        
        # Set Matrices
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        
        # We will draw small cubes or points. 
        # Using a small scale on the cube VAO is easiest as we already have it.
        gl.glBindVertexArray(self.renderer.vaos['cube'])
        
        for mark in marks:
            pos = mark['pos']
            alpha = mark['alpha']
            
            # Simple Black Dot
            gl.glUniform3f(uniforms['color'], 0.0, 0.0, 0.0) 
            
            # Calculate transform: Translate to hit point, Scale down to a dot
            mat = glm.translate(glm.mat4(1.0), glm.vec3(pos[0], pos[1], pos[2]))
            mat = glm.scale(mat, glm.vec3(2.0, 2.0, 2.0)) # 2 unit size dot
            
            gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
            
            # Draw
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            
        gl.glBindVertexArray(0)
        gl.glDisable(gl.GL_BLEND)


    def keyPressEvent(self, event):
        def check_key(cfg_key, default):
            key_str = self.editor.config.get('Shortcuts', cfg_key, fallback=default)
            seq = QKeySequence(key_str)
            return QKeySequence(event.key() | int(event.modifiers())) == seq
        if check_key('key_show_connections', 'F1'):
            current_state = getattr(self.editor, 'show_logic_links', False)
            self.editor.show_logic_links = not current_state
            self.editor.update_views()
            if hasattr(self.editor, 'show_toast'):
                status = "ON" if self.editor.show_logic_links else "OFF"
                self.editor.show_toast(f"Logic Links: {status}")
            return
        if check_key('key_toggle_wireframe', 'F2'):
             if self.current_render_mode == RENDER_MODE_WIREFRAME: self.current_render_mode = RENDER_MODE_LIT
             else: self.current_render_mode = RENDER_MODE_WIREFRAME
             mode_name = self.render_mode_names.get(self.current_render_mode, "Unknown")
             if hasattr(self.editor, 'show_toast'): self.editor.show_toast(f"Render Mode: {mode_name}")
             self.update()
             return
        if check_key('key_sysmon', 'F3'):
            self.debug_mode_active = not self.debug_mode_active
            if self.play_mode:
                if self.debug_mode_active: QApplication.restoreOverrideCursor(); self.setCursor(Qt.ArrowCursor)
                else: center_pos = self.mapToGlobal(self.rect().center()); QCursor.setPos(center_pos); self.last_mouse_pos = self.mapFromGlobal(center_pos); QApplication.setOverrideCursor(Qt.BlankCursor)
            self.update()
            return
        if not self.play_mode:
            if event.key() == Qt.Key_BracketLeft:
                if hasattr(self.editor, 'set_grid_size'):
                    new_size = max(1, self.grid_size // 2)
                    self.editor.set_grid_size(new_size)
                    if hasattr(self.editor, 'show_toast'): self.editor.show_toast(f"Grid Size: {new_size}")
                return
            elif event.key() == Qt.Key_BracketRight:
                if hasattr(self.editor, 'set_grid_size'):
                    new_size = min(2048, self.grid_size * 2)
                    self.editor.set_grid_size(new_size)
                    if hasattr(self.editor, 'show_toast'): self.editor.show_toast(f"Grid Size: {new_size}")
                return
        if self.play_mode:
            if getattr(self, 'show_render_menu', False):
                if event.key() == Qt.Key_1: self.current_render_mode = RENDER_MODE_LIT; self.update()
                elif event.key() == Qt.Key_2: self.current_render_mode = RENDER_MODE_UNLIT; self.update()
                elif event.key() == Qt.Key_3: self.current_render_mode = RENDER_MODE_WIREFRAME; self.update()
                elif event.key() == Qt.Key_4: self.current_render_mode = RENDER_MODE_VERTEX; self.update()
                elif event.key() == Qt.Key_Escape: self.show_render_menu = False; self.update()
                return 
        super().keyPressEvent(event)

    def _draw_sprites_text(self, painter):
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 105, 180))
        painter.fillRect(5, 5, 80, 25, QColor(0, 0, 0, 128))
        painter.drawText(10, 20, "Sprites")

    def _draw_fps_counter(self, painter):
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        rect_width = 100
        rect_x = self.width() - rect_width - 5
        painter.fillRect(rect_x, 5, rect_width, 20, QColor(0, 0, 0, 128))
        painter.drawText(rect_x + 5, 20, f"FPS: {self.fps:.0f}")

    def _draw_hud(self, painter, render_state):
        if not render_state: return
        health = render_state.player_health
        max_health = render_state.player_max_health
        health_ratio = health / max_health if max_health > 0 else 0
        hud_margin = 20
        bar_width = 200
        bar_height = 20
        bar_x = hud_margin
        bar_y = self.height() - hud_margin - bar_height
        painter.setPen(QPen(QColor(60, 60, 60), 2))
        painter.setBrush(QBrush(QColor(40, 40, 40, 200)))
        painter.drawRect(bar_x, bar_y, bar_width, bar_height)
        if health_ratio > 0.6: fill_color = QColor(50, 200, 50)
        elif health_ratio > 0.3: fill_color = QColor(255, 200, 50)
        else: fill_color = QColor(200, 50, 50)
        fill_width = int(bar_width * health_ratio)
        if fill_width > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawRect(bar_x, bar_y, fill_width, bar_height)
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(bar_x, bar_y - 5, f"HEALTH: {health}/{max_health}")
        active_weapon = getattr(render_state, 'active_weapon', None)
        if active_weapon:
            cx, cy = self.width() // 2, self.height() // 2
            size = 10
            painter.setPen(QPen(QColor(0, 255, 0, 200), 2))
            painter.drawLine(cx - size, cy, cx + size, cy)
            painter.drawLine(cx, cy - size, cx, cy + size)
        if hasattr(render_state, 'hud_message') and render_state.hud_message:
            msg = render_state.hud_message
            msg_font = QFont()
            msg_font.setPointSize(14)
            msg_font.setBold(True)
            painter.setFont(msg_font)
            fm = painter.fontMetrics()
            text_width = fm.horizontalAdvance(msg) 
            cx = self.width() // 2
            cy = self.height() // 2 + 50 
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(cx - text_width//2 + 2, cy + 2, msg)
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(cx - text_width//2, cy, msg)
        if active_weapon:
            hud_pixmap = self._load_gun_hud_pixmap(active_weapon)
            if hud_pixmap and not hud_pixmap.isNull():
                scale_factor = self.height() / 600.0  
                target_h = int(200 * scale_factor)
                if hud_pixmap.height() > 0: target_w = int(hud_pixmap.width() * (target_h / hud_pixmap.height()))
                else: target_w = target_h
                x = self.width() - target_w - 20
                y = self.height() - target_h
                painter.drawPixmap(x, y, target_w, target_h, hud_pixmap)
        collected_keys = getattr(render_state, 'collected_keys', set())
        if collected_keys:
            key_x = self.width() - hud_margin - 100
            key_y = self.height() - hud_margin - 100
            key_size = 100
            key_spacing = 40
            for i, key_name in enumerate(sorted(collected_keys)):
                icon_x = key_x - i * key_spacing
                try:
                    pixmap = Pickup.get_key_pixmap(key_name)
                    if pixmap and not pixmap.isNull():
                        scaled_pixmap = pixmap.scaled(key_size, key_size)
                        painter.drawPixmap(icon_x, key_y, scaled_pixmap)
                    else: self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)
                except: self._draw_key_fallback(painter, key_name, icon_x, key_y, key_size)
    
    def _draw_key_fallback(self, painter, key_name, x, y, size):
        key_colors = {'blue_key': QColor(50, 100, 200), 'red_key': QColor(200, 50, 50), 'yellow_key': QColor(200, 200, 50), 'green_key': QColor(50, 200, 50)}
        color = key_colors.get(key_name, QColor(150, 150, 150))
        painter.setPen(QPen(color.darker(120), 2))
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(x, y, size, size, 4, 4)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(x + 8, y + size//2, x + size - 8, y + size//2)
        painter.drawEllipse(x + 4, y + size//2 - 6, 12, 12)
        painter.drawLine(x + size - 10, y + size//2, x + size - 10, y + size//2 + 6)
        painter.drawLine(x + size - 14, y + size//2, x + size - 14, y + size//2 + 4)

    def _draw_window_manager(self, painter):
        bg_color = QColor(20, 20, 25, 235)      
        border_color = QColor(80, 80, 90)       
        header_color = QColor(66, 95, 93)       
        text_color = QColor(220, 220, 220)      
        graph_bg_color = QColor(10, 10, 15, 200) 
        target_height = 240 if self.sysmon_expanded else 30
        rect = QRect(self.debug_window_rect.x(), self.debug_window_rect.y(), self.debug_window_rect.width(), target_height)
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(QBrush(bg_color))
        painter.drawRect(rect) 
        header_rect = QRect(rect.x(), rect.y(), rect.width(), 25)
        painter.fillRect(header_rect, header_color)
        painter.setPen(QPen(border_color, 1))
        painter.drawLine(rect.x(), rect.y() + 25, rect.right(), rect.y() + 25)
        painter.setFont(self.console_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(header_rect.adjusted(10, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "SysMon [F3]")
        painter.drawText(QRect(rect.right() - 50, rect.y(), 25, 25), Qt.AlignCenter, "▼" if self.sysmon_expanded else "▶")
        painter.drawText(QRect(rect.right() - 25, rect.y(), 25, 25), Qt.AlignCenter, "[X]")
        if not self.sysmon_expanded: return
        graph_x, graph_y = rect.x() + 5, rect.y() + 30
        graph_width, graph_height = rect.width() - 10, 50
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(QBrush(graph_bg_color))
        painter.drawRect(graph_x, graph_y, graph_width, graph_height)
        if len(self.frame_times) > 1:
            max_ft = max(max(self.frame_times), 16.67)
            step = graph_width / max(len(self.frame_times) - 1, 1)
            points = [QPoint(int(graph_x + i * step), int(graph_y + graph_height - (ft / max_ft) * graph_height)) for i, ft in enumerate(self.frame_times)]
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(100, 200, 100, 40)))
            poly = QPolygon([QPoint(graph_x, graph_y + graph_height)] + points + [QPoint(points[-1].x(), graph_y + graph_height)])
            painter.drawPolygon(poly)
            painter.setPen(QPen(QColor(100, 255, 100), 1))
            painter.drawPolyline(QPolygon(points))
        brush_tris = len(self.editor.state.brushes) * 12 
        terrain_total_tris = 0
        terrain_visible_tris = 0
        if hasattr(self.editor, 'terrain') and self.editor.terrain is not None:
            terrain_total_tris = self.editor.terrain.get_tri_count()
            terrain_visible_tris = self.editor.terrain.total_triangles
        total_tris = brush_tris + terrain_total_tris
        visible_tris = (self.renderer.render_stats.visible_tris if self.renderer else 0) + terrain_visible_tris
        left_margin = rect.x() + 10
        stats_start_y = graph_y + graph_height + 25
        line_height = 18
        current_ft = self.frame_times[-1] if self.frame_times else 0
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(left_margin, stats_start_y, f"Frame: {current_ft:.1f}ms  FPS: {self.fps:.0f}")
        painter.setPen(text_color)
        painter.drawText(left_margin, stats_start_y + line_height * 2, f"Things:   {len(self.editor.state.things)}")
        painter.drawText(left_margin, stats_start_y + line_height * 3, f"Draws:    {self.renderer.render_stats.draw_calls if self.renderer else 0}")
        brush_text = f"Brushes:  {self.sysmon_stats.get('total_brushes', 0)} "
        painter.drawText(left_margin, stats_start_y + line_height * 4, brush_text)
        tri_text = f"Tris:     {total_tris} "
        painter.drawText(left_margin, stats_start_y + line_height * 5, tri_text)
        fm = painter.fontMetrics()
        painter.setPen(QColor(50, 200, 50))
        painter.drawText(left_margin + fm.horizontalAdvance(brush_text), stats_start_y + line_height * 4, f"(Visible: {self.sysmon_stats.get('visible_brushes', 0)})")
        painter.drawText(left_margin + fm.horizontalAdvance(tri_text), stats_start_y + line_height * 5, f"(Visible: {visible_tris})")
        painter.setPen(QColor(180, 180, 180))
        culled_brushes = self.sysmon_stats.get('culled_brushes', 0)
        total_brushes = self.sysmon_stats.get('total_brushes', 0)
        cull_pct = (culled_brushes / total_brushes * 100) if total_brushes > 0 else 0
        painter.drawText(left_margin, rect.bottom() - 10, f"Culled: {cull_pct:.1f}%")
        tps_text = f"TPS: {getattr(self.logic_thread, 'actual_tps', 0.0):.1f}"
        painter.drawText(rect.right() - fm.horizontalAdvance(tps_text) - 10, rect.bottom() - 10, tps_text)

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

    def load_texture(self, texture_name, subfolder):
        return self.renderer.load_texture(texture_name, subfolder) if self.renderer else 0

    def load_all_sprite_textures(self):
        things = {'PlayerStart': 'player.png', 'Light': 'light.png', 'Monster': 'monster.png', 'Pickup': 'pickup.png', 'Speaker': 'speaker.png'}
        for cls, fname in things.items():
            tid = self.load_texture(fname, 'sprites')
            if tid: self.sprite_textures[cls] = tid
        key_textures = {'blue_key': 'bluekey.png', 'red_key': 'redkey.png', 'yellow_key': 'yellowkey.png', 'green_key': 'greenkey.png'}
        for key_name, fname in key_textures.items():
            tid = self.load_texture(fname, 'sprites')
            if tid: self.sprite_textures[f'key_{key_name}'] = tid
        if self.renderer:
            self.renderer.set_sprite_textures(self.sprite_textures)
    
    def update_instance_textures(self, things):
        if not self.renderer: return
        instance_textures = {}
        for thing in things:
            if isinstance(thing, LogicGate):
                l_type = thing.properties.get('logic_type', 'and').lower()
                filename = f"logic_{l_type}.png"
                tex_key = f"logic_{l_type}"
                if tex_key not in self.sprite_textures:
                    tid = self.load_texture(filename, 'sprites')
                    if tid: self.sprite_textures[tex_key] = tid
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
                    if tex_id: instance_textures[id(thing)] = tex_id
        self.renderer.set_instance_textures(instance_textures)

    def toggle_play_mode(self, player_start_pos, player_start_angle, physics_enabled=True):
        self.play_mode = not self.play_mode
        self.debug_mode_active = False
        if self.play_mode:
            center_pos = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center_pos)
            self.last_mouse_pos = self.mapFromGlobal(center_pos)
            QApplication.setOverrideCursor(Qt.BlankCursor)
            self.player = Player(player_start_pos[0], player_start_pos[2], np.radians(player_start_angle), physics_enabled=physics_enabled)
            self.player.pos.y = player_start_pos[1]
            if self.logic_thread:
                self.logic_thread.set_player(self.player)
                self.logic_thread.set_play_mode(True)
        else:
            QApplication.restoreOverrideCursor()
            if self.logic_thread:
                self.logic_thread.set_play_mode(False)
                self.logic_thread.set_player(None)
            self.player = None
            self.update()

    def set_culling(self, enabled):
        self.culling_enabled = enabled
        self.update()

    def set_cull_distance(self, distance):
        self.cull_distance = distance
        if self.renderer:
            self.renderer.lod_manager.cull_dist_sq = distance * distance
            self.renderer.lod_manager.full_dist_sq = (distance * 0.25) ** 2
        self.update()

    def get_selected_object_pos(self):
        if not self.editor.state.selected_object: return None
        if isinstance(self.editor.state.selected_object, dict): return glm.vec3(self.editor.state.selected_object['pos'])
        return glm.vec3(self.editor.state.selected_object.pos)

    def set_selected_object_pos(self, new_pos_vec):
        if not self.editor.state.selected_object: return
        grid = self.editor.grid_size_spinbox.value()
        snapped = [round(c / grid) * grid for c in new_pos_vec]
        if isinstance(self.editor.state.selected_object, dict):
            self.editor.state.selected_object['pos'] = snapped
        else:
            obj = self.editor.state.selected_object
            if hasattr(obj, 'pos'): obj.pos = snapped
        if self.logic_thread and self.logic_thread.is_alive() and type(self.editor.state.selected_object).__name__ == 'Light':
            self.game_state.request_swap()
        self.editor.update_all_ui()

    def get_ray_from_mouse(self, x, y):
        win_x, win_y = float(x), float(self.height() - y)
        vp = glm.vec4(0, 0, self.width(), self.height())
        near = glm.unProject(glm.vec3(win_x, win_y, 0.0), self.view_matrix, self.projection_matrix, vp)
        far = glm.unProject(glm.vec3(win_x, win_y, 1.0), self.view_matrix, self.projection_matrix, vp)
        return near, glm.normalize(far - near)

    def intersect_ray_with_axis(self, ray_origin, ray_dir, axis_origin, axis_dir):
        cross = glm.cross(axis_dir, ray_dir)
        denom = glm.dot(cross, cross)
        if abs(denom) < 1e-6: return None, float('inf')
        t = glm.dot(glm.cross(ray_origin - axis_origin, ray_dir), cross) / denom
        pt = axis_origin + t * axis_dir
        t_ray = glm.dot(pt - ray_origin, ray_dir)
        pt_ray = ray_origin + t_ray * ray_dir
        return pt, glm.distance(pt, pt_ray)

    def intersect_ray_aabb(self, ray_origin, ray_dir, box_min, box_max):
        t_min, t_max = 0.0, float('inf')
        for i in range(3):
            if abs(ray_dir[i]) < 1e-8:
                if ray_origin[i] < box_min[i] or ray_origin[i] > box_max[i]: return False, float('inf')
            else:
                t1 = (box_min[i] - ray_origin[i]) / ray_dir[i]
                t2 = (box_max[i] - ray_origin[i]) / ray_dir[i]
                if t1 > t2: t1, t2 = t2, t1
                t_min = max(t_min, t1)
                t_max = min(t_max, t2)
                if t_min > t_max: return False, float('inf')
        return True, t_min
    
    def intersect_ray_sphere(self, ray_origin, ray_dir, center, radius):
        oc = ray_origin - center
        a = glm.dot(ray_dir, ray_dir)
        b = 2.0 * glm.dot(oc, ray_dir)
        c = glm.dot(oc, oc) - radius * radius
        disc = b * b - 4 * a * c
        if disc < 0: return False, float('inf')
        t = (-b - np.sqrt(disc)) / (2.0 * a)
        if t < 0: t = (-b + np.sqrt(disc)) / (2.0 * a)
        return (True, t) if t >= 0 else (False, float('inf'))

    def get_object_at_3d(self, mouse_x, mouse_y):
        ray_origin, ray_dir = self.get_ray_from_mouse(mouse_x, mouse_y)
        closest_thing, closest_thing_t = None, float('inf')
        closest_brush, closest_brush_t = None, float('inf')
        for thing in self.editor.state.things:
            if thing.properties.get('hidden', False): continue
            hit, t = self.intersect_ray_sphere(ray_origin, ray_dir, glm.vec3(*thing.pos), 24.0)
            if hit and t < closest_thing_t:
                closest_thing_t = t
                closest_thing = thing
        for brush in self.editor.state.brushes:
            if brush.get('hidden', False): continue
            pos, size = brush['pos'], brush['size']
            h = [s/2 for s in size]
            bmin = glm.vec3(pos[0]-h[0], pos[1]-h[1], pos[2]-h[2])
            bmax = glm.vec3(pos[0]+h[0], pos[1]+h[1], pos[2]+h[2])
            hit, t = self.intersect_ray_aabb(ray_origin, ray_dir, bmin, bmax)
            if hit and t < closest_brush_t:
                closest_brush_t = t
                closest_brush = brush
        return closest_thing if closest_thing else closest_brush

    def get_brush_face_at_coords(self, x, y):
        """Raycasts to find the closest brush face under the mouse coordinates."""
        ray_o, ray_d = self.get_ray_from_mouse(x, y)
        best_t = float('inf')
        best_hit = None # (brush, face_name)
        
        for brush in self.editor.state.brushes:
            if brush.get('hidden', False): continue
            
            # 1. AABB intersection
            pos, size = glm.vec3(brush['pos']), glm.vec3(brush['size'])
            bmin, bmax = pos - size/2.0, pos + size/2.0
            hit_box, t_box = self.intersect_ray_aabb(ray_o, ray_d, bmin, bmax)
            
            if hit_box and t_box < best_t:
                # 2. Determine which face was hit
                hit_pt = ray_o + ray_d * t_box
                local = hit_pt - pos
                half = size * 0.5
                
                # Normalize 0..1 relative to half-extents
                # Add small epsilon to avoid div/0
                rel = abs(local) / (half + 0.0001) 
                
                # The component closest to 1.0 indicates the axis of the face
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
        if self.debug_mode_active and event.button() == Qt.LeftButton:
            if self.debug_window_rect.contains(event.pos()):
                if event.x() > self.debug_window_rect.right() - 25 and event.y() < self.debug_window_rect.y() + 25:
                    self.debug_mode_active = False
                    if self.play_mode: QApplication.setOverrideCursor(Qt.BlankCursor)
                return
        
        # Face Mode Click - Apply Texture (Left Click Only)
        if self.face_mode_active and event.button() == Qt.LeftButton:
            if self.hovered_face_info:
                brush, face = self.hovered_face_info
                self.editor.apply_texture_to_specific_face(brush, face)
            return

        # Legacy Face Selection (Ctrl+Click) - Keep compatibility
        if event.button() == Qt.LeftButton and QApplication.keyboardModifiers() == Qt.ControlModifier and not self.play_mode:
            face = self.get_face_at(event.pos())
            if face: self.editor.selected_face = face; self.update(); return

        if self.play_mode and event.button() == Qt.LeftButton:
            render_state = self.game_state.get_render_state()
            active_weapon = getattr(render_state, 'active_weapon', None)
            if active_weapon:
                self.game_state.queue_shot()
                effect = self._get_sound_instance('shoot.wav')
                if effect: effect.play()
                return 

        if event.button() == Qt.LeftButton and QApplication.keyboardModifiers() == Qt.ShiftModifier and not self.play_mode:
            obj = self.get_object_at_3d(event.x(), event.y())
            if obj: self.editor.save_state(); self.editor.set_selected_object(obj); self.update(); return
            
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
                        if dist < best_dist: best_dist = dist; hit_axis = axis; start_pt = pt
                if hit_axis:
                    self.editor.save_state()
                    self.is_dragging_gizmo = True
                    self.gizmo_drag_axis = hit_axis
                    self.gizmo_object_start_pos = obj_pos
                    self.drag_start_on_axis = start_pt
                    self.setCursor(Qt.ClosedHandCursor)
                    return
        
        # Right click enables mouselook (works in Face Mode because face logic only traps LeftButton)
        if not self.play_mode and event.button() == Qt.RightButton:
            self.mouselook_active = True
            self.last_mouse_pos = event.pos()
            self.setCursor(Qt.BlankCursor)
            
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 1. Handle Mouselook (Priority over Face Hover)
        # This ensures we can look around while holding RMB even in face mode
        if self.mouselook_active:
            dx, dy = event.x() - self.last_mouse_pos.x(), event.y() - self.last_mouse_pos.y()
            if self.use_threading and self.logic_thread: self.game_state.set_mouse_delta(float(dx), float(dy))
            else: self.camera.rotate(dx, dy)
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)
            self.editor.update_views()
            return

        # 2. Handle Play Mode Mouse
        if self.play_mode:
            if self.debug_mode_active: return
            cp = event.pos()
            dx, dy = cp.x() - self.last_mouse_pos.x(), cp.y() - self.last_mouse_pos.y()
            if dx == 0 and dy == 0: return
            self.game_state.set_mouse_delta(float(dx), float(dy))
            center = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center)
            self.last_mouse_pos = self.mapFromGlobal(center)
            return

        # 3. Handle Gizmo Drag
        if self.is_dragging_gizmo:
            ray_o, ray_d = self.get_ray_from_mouse(event.x(), event.y())
            axis_vec = {'x': glm.vec3(1,0,0), 'y': glm.vec3(0,1,0), 'z': glm.vec3(0,0,1)}[self.gizmo_drag_axis]
            pt, _ = self.intersect_ray_with_axis(ray_o, ray_d, self.gizmo_object_start_pos, axis_vec)
            if pt:
                diff = pt - self.drag_start_on_axis
                self.set_selected_object_pos(self.gizmo_object_start_pos + diff)
            return

        # 4. Handle Face Mode Hover (Lowest priority for movement)
        if self.face_mode_active:
            self.hovered_face_info = self.get_brush_face_at_coords(event.x(), event.y())
            self.update() # Force redraw to show highlight
            return
        
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.is_dragging_gizmo: self.is_dragging_gizmo = False; self.setCursor(Qt.ArrowCursor); self.editor.save_state()
        if self.mouselook_active and event.button() == Qt.RightButton: self.mouselook_active = False; self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)
    
    def wheelEvent(self, event):
        if not self.play_mode:
            self.camera.fov = np.clip(self.camera.fov - event.angleDelta().y() * 0.05, 30, 120)
            self.editor.update_views()

    def get_face_at(self, mouse_pos):
        if not isinstance(self.editor.state.selected_object, dict): return None
        brush = self.editor.state.selected_object
        ray_o, ray_d = self.get_ray_from_mouse(mouse_pos.x(), mouse_pos.y())
        pos, size = glm.vec3(brush['pos']), glm.vec3(brush['size'])
        bmin, bmax = pos - size/2, pos + size/2
        tmin, tmax = 0.0, float('inf')
        for i in range(3):
            if abs(ray_d[i]) < 1e-6:
                if ray_o[i] < bmin[i] or ray_o[i] > bmax[i]: return None
            else:
                t1 = (bmin[i] - ray_o[i]) / ray_d[i]
                t2 = (bmax[i] - ray_o[i]) / ray_d[i]
                if t1 > t2: t1, t2 = t2, t1
                tmin = max(tmin, t1)
                tmax = min(tmax, t2)
        if tmin > tmax: return None
        hit = ray_o + ray_d * tmin
        local = hit - pos
        rel = abs(local) / size
        if rel.x > rel.y and rel.x > rel.z: return 'east' if local.x > 0 else 'west'
        if rel.y > rel.x and rel.y > rel.z: return 'top' if local.y > 0 else 'bottom'
        return 'north' if local.z > 0 else 'south'

    def _load_gun_hud_pixmap(self, gun_type):
        """Lazy load HUD pixmaps for guns."""
        if gun_type in self.gun_hud_pixmaps:
            return self.gun_hud_pixmaps[gun_type]
        path = os.path.join('assets', 'sprites', f'{gun_type}HUD.png')
        if os.path.exists(path):
            pixmap = QPixmap(path)
            self.gun_hud_pixmaps[gun_type] = pixmap
            return pixmap
        return None