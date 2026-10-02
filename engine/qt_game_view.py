import time
import os
import numpy as np
import ctypes
from collections import deque
from PyQt5.QtWidgets import QOpenGLWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QPoint, QUrl, QRect
from PyQt5.QtGui import QPainter, QColor, QFont, QCursor, QFontDatabase, QPen, QBrush
from PyQt5.QtMultimedia import QSoundEffect
import OpenGL.GL as gl
import glm
from engine.camera import Camera
from editor.things import Thing, Light, PlayerStart, Monster, Pickup, Speaker
from engine.player import Player
from PIL import Image
from .renderer import Renderer
from engine import shaders
from engine.threaded_game_state import ThreadedGameState, RenderState
from engine.logic_thread import LogicThread
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX

def perspective_projection(fov, aspect, near, far):
    if aspect == 0: return glm.mat4(1.0)
    return glm.perspective(glm.radians(fov), aspect, near, far)


class QtGameView(QOpenGLWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        # Rendering and view state
        self.brush_display_mode = "Textured"
        self.show_triggers_as_solid = False
        self.camera = Camera()
        self.camera.pos = glm.vec3(0, 150, 400)
        self.grid_size, self.world_size = 16, 1024
        self.grid_dirty = True
        self.culling_enabled = False
        self.selected_object = None
        self.show_sprites_in_play_mode = False
        self.visibility_system = None
        self.show_visibility_debug = False

        self.sysmon_expanded = False
        self.sysmon_stats = {
            'visible_brushes': 0,
            'visible_tris': 0,
            'visible_surfaces': 0,
            'culled_brushes': 0,
            'culled_tris': 0,
            'culled_surfaces': 0
        }

        # Threading
        self.game_state = ThreadedGameState()
        self.logic_thread: Optional[LogicThread] = None
        self.use_threading = True

        # Input state
        self.mouselook_active, self.last_mouse_pos = False, QPoint()
        
        # Debug Window Manager State
        self.debug_mode_active = False
        self.debug_window_rect = QRect(20, 20, 400, 200)
        self.debug_drag_active = False
        self.debug_drag_offset = QPoint()
        self.frame_times = deque(maxlen=100) # History for graph
        self.console_font = QFont("Arial", 9)
        self.console_font.setStyleHint(QFont.Monospace)
        
        # Resource management
        self.texture_manager = {}
        self.sprite_textures = {}
        self.noise_texture_id = 0

        # Rendering backend
        self.renderer = None

        # Render Menu states
        self.show_render_menu = False
        self.current_render_mode = RENDER_MODE_LIT
        self.render_mode_names = {
            RENDER_MODE_LIT: "Lit (Phong)",
            RENDER_MODE_UNLIT: "Unlit (Fullbright)",
            RENDER_MODE_WIREFRAME: "Wireframe",
            RENDER_MODE_VERTEX: "Vertex"
        }

        # Game mode state
        self.play_mode = False
        self.player = None
        self.tile_map = None
        self.player_in_triggers = set()
        self.fired_once_triggers = set()
        self.active_sounds = {}
        self.played_once_sounds = set()
        
        # Mover animation state
        self.mover_states = {}  # brush_index -> {'progress': 0.0, 'forward': True}
        
        # Performance tracking
        self.fps = 0
        self.frame_count = 0
        self.last_time = time.time()
        self.last_fps_time = time.time()
        self.start_time = time.time()

        # Gizmo dragging state
        self.is_dragging_gizmo = False
        self.gizmo_drag_axis = None
        self.gizmo_object_start_pos = None
        self.drag_start_on_axis = None
        self.projection_matrix = glm.mat4(1.0)
        self.view_matrix = glm.mat4(1.0)

        timer = QTimer(self)
        timer.setInterval(16) # ~60 FPS
        timer.timeout.connect(self.update_loop)
        timer.start()
        
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(True)

    def initializeGL(self):
        """Initializes OpenGL and the Renderer."""
        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
        self.renderer = Renderer(self.load_texture, self.grid_size, self.world_size)
        self.load_all_sprite_textures()

    def keyPressEvent(self, event):
        # F3 Toggle for Debug Window Manager
        if event.key() == Qt.Key_F3:
            self.debug_mode_active = not self.debug_mode_active
            
            if self.play_mode:
                if self.debug_mode_active:
                    # Release mouse control
                    QApplication.restoreOverrideCursor()
                    self.setCursor(Qt.ArrowCursor)
                else:
                    # Recapture mouse control
                    center_pos = self.mapToGlobal(self.rect().center())
                    QCursor.setPos(center_pos)
                    self.last_mouse_pos = self.mapFromGlobal(center_pos)
                    QApplication.setOverrideCursor(Qt.BlankCursor)
            
            self.update()
            return

        # Only handle these shortcuts if we are currently playing the game
        if self.play_mode:
            # If the render menu is open, handle mode switching
            if getattr(self, 'show_render_menu', False):
                if event.key() == Qt.Key_1:
                    self.current_render_mode = RENDER_MODE_LIT
                elif event.key() == Qt.Key_2:
                    self.current_render_mode = RENDER_MODE_UNLIT
                elif event.key() == Qt.Key_3:
                    self.current_render_mode = RENDER_MODE_WIREFRAME
                elif event.key() == Qt.Key_4:
                    self.current_render_mode = RENDER_MODE_VERTEX
                elif event.key() == Qt.Key_Escape:
                    self.show_render_menu = False
                
                # Force a redraw to show the change immediately
                self.update()
                return 

        # Pass other events (like movement keys) to the default handler
        super().keyPressEvent(event)
        
    def paintGL(self):
        if not self.renderer:
            return

        if self.grid_dirty:
            self.renderer.update_grid_buffers(self.world_size, self.grid_size)
            self.grid_dirty = False

        # --- SETUP CAMERA & SCENE DATA ---
        camera_pos = glm.vec3(0,0,0)
        render_state: Optional[RenderState] = None

        if self.play_mode and self.use_threading:
            # THREADED RENDER PATH
            # Use the thread-safe snapshot for EVERYTHING (Camera + Brushes)
            render_state = self.game_state.get_render_state()
            self.view_matrix = render_state.camera_view_matrix
            camera_pos = render_state.player_pos
            
            # Use snapshot brushes to ensure position matches camera timestamp
            brushes_to_render = render_state.visible_brushes
            things_to_render = render_state.visible_things

        elif self.play_mode and self.player:
            # NON-THREADED PLAY MODE (Legacy/Fallback)
            self.view_matrix = self.player.get_view_matrix()
            camera_pos = self.player.pos
            brushes_to_render = self.editor.state.brushes
            things_to_render = self.editor.state.things
        else:
            # EDITOR MODE
            self.view_matrix = self.camera.get_view_matrix()
            camera_pos = self.camera.pos
            brushes_to_render = self.editor.state.brushes
            things_to_render = self.editor.state.things
        
        aspect_ratio = self.width() / self.height() if self.height() > 0 else 1
        self.projection_matrix = perspective_projection(self.camera.fov, aspect_ratio, 0.1, 10000.0)

        # Culling (Optional override for large scenes)
        if self.play_mode and self.visibility_system and not self.use_threading:
            view_proj = self.projection_matrix * self.view_matrix
            visible_indices = self.visibility_system.get_visible_brushes(
                camera_pos, view_proj, max_portal_depth=4)
            brushes_to_render = [self.editor.state.brushes[i] for i in visible_indices if i < len(self.editor.state.brushes)]

        render_config = {
            "culling_enabled": self.culling_enabled,
            "brush_display_mode": self.brush_display_mode,
            "render_mode": getattr(self, 'current_render_mode', 0), # Default to LIT (0) if not set
            "show_triggers_as_solid": self.show_triggers_as_solid,
            "show_caulk": self.editor.config.getboolean('Display', 'show_caulk', fallback=True),
            "play_mode": self.play_mode,
            "selected_object": self.selected_object,
            "time": time.time() - self.start_time,
            "show_sprites_in_play_mode": self.show_sprites_in_play_mode,
        }

        self.renderer.render_scene(
            self.projection_matrix, self.view_matrix, camera_pos,
            brushes_to_render, things_to_render,
            self.selected_object, render_config
        )

        # Capture rendering statistics for SysMon
        if self.play_mode and self.use_threading and render_state:
            visible_count = len(render_state.visible_brushes)
            self.sysmon_stats['visible_brushes'] = visible_count
            self.sysmon_stats['visible_tris'] = visible_count * 12
            self.sysmon_stats['visible_surfaces'] = visible_count * 6
            self.sysmon_stats['culled_brushes'] = render_state.culled_brushes
            self.sysmon_stats['culled_tris'] = render_state.culled_brushes * 12
            self.sysmon_stats['culled_surfaces'] = render_state.culled_brushes * 6
        else:
            visible_count = len(brushes_to_render)
            total_count = len(self.editor.state.brushes)
            culled_count = total_count - visible_count
            
            self.sysmon_stats['visible_brushes'] = visible_count
            self.sysmon_stats['visible_tris'] = visible_count * 12
            self.sysmon_stats['visible_surfaces'] = visible_count * 6
            self.sysmon_stats['culled_brushes'] = culled_count
            self.sysmon_stats['culled_tris'] = culled_count * 12
            self.sysmon_stats['culled_surfaces'] = culled_count * 6

        # Draw 2D Overlays
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
            
        painter.end()

    def _draw_sprites_text(self, painter):
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 105, 180)) # Pink text
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
        """Draw the in-game HUD showing health bar and other info."""
        if not render_state:
            return
            
        health = render_state.player_health
        max_health = render_state.player_max_health
        health_ratio = health / max_health if max_health > 0 else 0
        
        # HUD positioning
        hud_margin = 20
        bar_width = 200
        bar_height = 20
        bar_x = hud_margin
        bar_y = self.height() - hud_margin - bar_height
        
        # Background bar
        painter.setPen(QPen(QColor(60, 60, 60), 2))
        painter.setBrush(QBrush(QColor(40, 40, 40, 200)))
        painter.drawRect(bar_x, bar_y, bar_width, bar_height)
        
        # Health bar fill
        if health_ratio > 0.6:
            fill_color = QColor(50, 200, 50)  # Green
        elif health_ratio > 0.3:
            fill_color = QColor(255, 200, 50)  # Yellow
        else:
            fill_color = QColor(200, 50, 50)  # Red
        
        fill_width = int(bar_width * health_ratio)
        if fill_width > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(fill_color))
            painter.drawRect(bar_x, bar_y, fill_width, bar_height)
        
        # Health text
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        health_text = f"HEALTH: {health}/{max_health}"
        painter.drawText(bar_x, bar_y - 5, health_text)
        
        # Use key hint (when near usable items)
        hint_text = "[E] Use"
        hint_font = QFont()
        hint_font.setPointSize(9)
        painter.setFont(hint_font)
        painter.setPen(QColor(180, 180, 180, 180))
        painter.drawText(bar_x + bar_width + 20, bar_y + bar_height - 5, hint_text)

    def _draw_window_manager(self, painter):
        """Draws the Debug Window Manager system with expandable statistics."""
        
        # Window Style Configuration
        bg_color = QColor(20, 20, 25, 240)
        border_color = QColor(80, 80, 90)
        header_color = QColor(66, 95, 93) # Muted cyan green
        text_color = QColor(220, 220, 220)
        accent_color = QColor(100, 200, 100) 
        graph_color = QColor(0, 255, 255, 150) 
        
        rect = self.debug_window_rect
        
        # 1. Main Window Body
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(QBrush(bg_color))
        painter.drawRect(rect)
        
        # 2. Header Bar
        header_rect = QRect(rect.x(), rect.y(), rect.width(), 25)
        painter.fillRect(header_rect, header_color)
        painter.setPen(QPen(border_color, 1)) 
        painter.drawLine(rect.x(), rect.y() + 25, rect.right(), rect.y() + 25)
        
        # 3. Title Text
        painter.setFont(self.console_font)
        painter.setPen(QColor(255, 255, 255)) # White text
        painter.drawText(header_rect.adjusted(10, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "SysMon [F3]")
        
        # 4. Control Buttons
        # Expand/Collapse Arrow
        expand_btn_rect = QRect(rect.right() - 50, rect.y(), 25, 25)
        arrow = "▼" if self.sysmon_expanded else "▶"
        painter.drawText(expand_btn_rect, Qt.AlignCenter, arrow)
        
        # Close Button [X]
        close_btn_rect = QRect(rect.right() - 25, rect.y(), 25, 25)
        painter.drawText(close_btn_rect, Qt.AlignCenter, "[X]")
        
        # 5. Content Area
        content_y = rect.y() + 50 
        left_margin = rect.x() + 10
        
        # Logic Thread Status
        status = "STOPPED"
        tps = 0.0
        if self.logic_thread and self.logic_thread.is_alive():
            status = "running"
            tps = getattr(self.logic_thread, 'actual_tps', 0.0)
            
        painter.setPen(text_color)
        painter.drawText(left_margin, content_y, f"Worker:   {status}")
        
        indicator_rect = QRect(left_margin + 160, content_y - 10, 10, 10)
        painter.setBrush(QBrush(accent_color if status == "running" else QColor(200, 50, 50)))
        painter.drawEllipse(indicator_rect)
        painter.setBrush(Qt.NoBrush) 
        
        content_y += 20
        painter.setPen(text_color)
        painter.drawText(left_margin, content_y, f"LOGIC TICK: {tps:.1f} / 60.0 Hz")
        
        content_y += 20
        ft_ms = (1.0 / self.fps * 1000.0) if self.fps > 0 else 0
        painter.drawText(left_margin, content_y, f"Render FPS: {self.fps:.1f} ({ft_ms:.1f} ms)")
        
        # 6. Expanded Statistics
        if self.sysmon_expanded:
            content_y += 20
            painter.setPen(QColor(150, 255, 150))
            painter.drawText(left_margin, content_y, f"Visible: {self.sysmon_stats['visible_tris']} tris, {self.sysmon_stats['visible_surfaces']} faces")
            
            content_y += 20
            painter.setPen(QColor(255, 150, 150))
            painter.drawText(left_margin, content_y, f"Culled:  {self.sysmon_stats['culled_tris']} tris, {self.sysmon_stats['culled_surfaces']} faces")
            
            content_y += 20
            painter.setPen(QColor(150, 150, 255))
            painter.drawText(left_margin, content_y, f"Brushes: {self.sysmon_stats['visible_brushes']} visible, {self.sysmon_stats['culled_brushes']} culled")
        
        # 7. Frame Time Graph
        content_y += 15
        # Reduce graph height when expanded to fit everything in the same window size
        base_graph_height = 40
        if self.sysmon_expanded:
            base_graph_height = 20  # Make room for 4 lines of stats
        
        graph_height = max(base_graph_height, rect.height() - (content_y - rect.y()) - 15)
        graph_rect = QRect(left_margin, content_y, rect.width() - 20, graph_height)
        
        if graph_height > 10: 
            painter.fillRect(graph_rect, QColor(0, 0, 0, 100))
            painter.setPen(QPen(QColor(60, 60, 60), 1))
            painter.drawRect(graph_rect)
            
            if len(self.frame_times) > 1:
                painter.setPen(QPen(graph_color, 1))
                path_step = graph_rect.width() / 100.0
                max_ms = 33.3 
                pts = []
                for i, ms in enumerate(self.frame_times):
                    x = graph_rect.x() + (i * path_step)
                    h_norm = min(ms / max_ms, 1.0) * graph_rect.height()
                    y = graph_rect.bottom() - h_norm
                    pts.append(QPoint(int(x), int(y)))
                if pts: painter.drawPolyline(*pts)

                ref_y = graph_rect.bottom() - (16.6 / max_ms * graph_rect.height())
                painter.setPen(QPen(QColor(255, 100, 100, 100), 1, Qt.DashLine))
                painter.drawLine(graph_rect.left(), int(ref_y), graph_rect.right(), int(ref_y))
                painter.setPen(QPen(QColor(255, 100, 100, 150), 1))
                painter.setFont(QFont("Small Fonts", 7))
                painter.drawText(graph_rect.right() - 25, int(ref_y) - 2, "16ms")

        # 8. Resize Grip (Bottom Right)
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.drawLine(rect.right() - 10, rect.bottom() - 2, rect.right() - 2, rect.bottom() - 10)
        painter.drawLine(rect.right() - 6, rect.bottom() - 2, rect.right() - 2, rect.bottom() - 6)
        painter.drawLine(rect.right() - 2, rect.bottom() - 2, rect.right() - 2, rect.bottom() - 2)

    def _draw_render_menu(self, painter):
        """Draws the render mode selection menu overlay."""
        # Menu Dimensions
        width, height = 220, 200
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2
        
        # Draw Background (Semi-transparent black)
        painter.fillRect(x, y, width, height, QColor(0, 0, 0, 200))
        painter.setPen(QColor(255, 255, 255))
        painter.drawRect(x, y, width, height)
        
        # Draw Title
        font = QFont()
        font.setBold(True)
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(x, y + 25, width, 25, Qt.AlignCenter, "Render Mode")
        
        # Draw Options
        font.setBold(False)
        font.setPointSize(10)
        painter.setFont(font)
        
        # Define options matching the constants in constants.py
        options = [
            (0, "[1] Lit (Phong)"),
            (1, "[2] Unlit (Fullbright)"),
            (2, "[3] Wireframe"),
            (3, "[4] Vertex")
        ]
        
        current_y = y + 55
        active_mode = getattr(self, 'current_render_mode', 0)

        for mode_id, text in options:
            if active_mode == mode_id:
                painter.setPen(QColor(100, 255, 100)) # Green for active
                display_text = "> " + text
            else:
                painter.setPen(QColor(200, 200, 200)) # Grey for inactive
                display_text = "  " + text
            
            painter.drawText(x + 20, current_y, display_text)
            current_y += 20

    def update_grid(self):
        self.grid_dirty = True
        self.update()

    def load_texture(self, texture_name, subfolder):
        tex_cache_name = os.path.join(subfolder, texture_name)
        if tex_cache_name in self.texture_manager: return self.texture_manager[tex_cache_name]
        if texture_name == 'default.png':
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id); pixels = [255, 255, 255, 255]
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, (gl.GLubyte * 4)(*pixels))
            return tex_id
        if texture_name == 'caulk':
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id); pixels = [255,0,255,255, 0,0,0,255, 0,0,0,255, 255,0,255,255]
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 2, 2, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, (gl.GLubyte * 16)(*pixels))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            return tex_id
        texture_path = os.path.join('assets', subfolder, texture_name)
        if not os.path.exists(texture_path): return self.load_texture('default.png', 'textures')
        try:
            img = Image.open(texture_path).convert("RGBA"); img_data = img.tobytes()
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT); gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR); gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img_data)
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            return tex_id
        except Exception as e: print(f"Error loading texture '{texture_name}': {e}"); return self.load_texture('default.png', 'textures')

    def load_all_sprite_textures(self):
        things_with_sprites = {'PlayerStart': 'player.png', 'Light': 'light.png', 'Monster': 'monster.png', 'Pickup': 'pickup.png', 'Speaker': 'speaker.png'}
        for class_name, filename in things_with_sprites.items():
            tex_id = self.load_texture(filename, '')
            if tex_id: self.sprite_textures[class_name] = tex_id
        self.renderer.set_sprite_textures(self.sprite_textures)

    def update_loop(self):
        """Modified update loop - rendering only when threading is enabled."""
        current_time = time.time()
        delta = current_time - self.last_time
        self.last_time = current_time
        
        # FPS tracking and Frame Time Graph Update
        self.frame_count += 1
        if current_time - self.last_fps_time > 1:
            self.fps = self.frame_count / (current_time - self.last_fps_time)
            self.frame_count = 0
            self.last_fps_time = current_time
        
        # Record frame time in ms
        self.frame_times.append(delta * 1000.0)

        if self.play_mode and self.use_threading:
            # Send current keys to logic thread
            self.game_state.set_keys(self.editor.keys_pressed)
            # Swap buffers if logic thread has new data
            if self.game_state.try_swap():
                self.update() # Request redraw with new state
        elif self.play_mode and self.player:
            # Non-threaded fallback
            self.player.update(self.editor.keys_pressed, self.editor.state.brushes, delta)
            self.handle_triggers()
            self.update_movers(delta)
            self.update_speaker_sounds()
            self.update()
        elif self.hasFocus():
            self.handle_keyboard_input(delta)
            self.update()

    def set_tile_map(self, tile_map):
        self.tile_map = tile_map

    def toggle_play_mode(self, player_start_pos, player_start_angle, physics_enabled=True):
        self.play_mode = not self.play_mode
        self.debug_mode_active = False
        
        if hasattr(self.editor, 'show_toast'):
            if self.play_mode:
                self.editor.show_toast("Press ESC to exit play mode", duration=0)
            else:
                # Hide the toast when returning to editor
                if hasattr(self.editor, 'toast'):
                    self.editor.toast.hide_toast()
                self.editor.show_toast("Changed to EDITOR mode")

        if self.play_mode:
            # Center and hide cursor immediately to prevent "jump"
            center_pos = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center_pos)
            self.last_mouse_pos = self.mapFromGlobal(center_pos)
            QApplication.setOverrideCursor(Qt.BlankCursor) # Hide cursor

            # Initialize mover states
            self.init_mover_states()

            # Create player
            self.player = Player(
                player_start_pos[0],
                player_start_pos[2],
                np.radians(player_start_angle),
                physics_enabled=physics_enabled
            )
            self.player.pos.y = player_start_pos[1]
            
            if self.use_threading:
                # Start logic thread
                self.logic_thread = LogicThread(
                    self.game_state,
                    self.editor.state.brushes,
                    self.editor.state.things,
                    self.visibility_system  # Add this later
                )
                self.logic_thread.set_player(self.player)
                self.logic_thread.set_play_mode(True)
                self.logic_thread.start()
        else:
            QApplication.restoreOverrideCursor() # Show cursor

            # Reset movers to original positions
            self.reset_mover_states()

            if self.logic_thread:
                self.logic_thread.stop()
                self.logic_thread.join(timeout=1.0)
                self.logic_thread = None
            self.player = None
            self.update() # Force update to clear visuals

    def set_culling(self, enabled):
        self.culling_enabled = enabled
        self.update()
        
    def handle_triggers(self):
        if not self.player:
            return
        player_pos = self.player.pos
        currently_colliding_triggers = set()
        for i, brush in enumerate(self.editor.state.brushes):
            if not isinstance(brush, dict) or not brush.get('is_trigger'):
                continue
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            half_size = size / 2.0
            min_bounds = pos - half_size
            max_bounds = pos + half_size
            if (min_bounds.x <= player_pos.x <= max_bounds.x and
                min_bounds.y <= player_pos.y <= max_bounds.y and
                min_bounds.z <= player_pos.z <= max_bounds.z):
                trigger_id = i
                currently_colliding_triggers.add(trigger_id)
                if trigger_id not in self.player_in_triggers:
                    self.activate_trigger(brush, trigger_id)
        self.player_in_triggers = currently_colliding_triggers
        
    def activate_trigger(self, brush, trigger_id):
        trigger_frequency = brush.get('trigger_type', 'multiple')
        if trigger_frequency == 'once' and trigger_id in self.fired_once_triggers:
            return
        target_name = brush.get('target')
        if not target_name:
            return

        target_brush = next((b for b in self.editor.state.brushes if b.get('name') == target_name), None)

        if target_brush and target_brush.get('is_mover'):
            if target_brush.get('move_once', False):
                # Toggle between start and end positions
                if 'original_pos' not in target_brush:
                    target_brush['original_pos'] = list(target_brush['pos'])
                
                direction = np.array(target_brush.get('direction', [0, 1, 0]))
                distance = target_brush.get('distance', 128)
                
                if list(target_brush['pos']) == target_brush['original_pos']:
                    target_brush['pos'] = (np.array(target_brush['original_pos']) + direction * distance).tolist()
                else:
                    target_brush['pos'] = target_brush['original_pos']
            else: # Original mover behavior
                target_brush['start_on'] = not target_brush.get('start_on', False)
        
        target_thing = next((t for t in self.editor.state.things if hasattr(t, 'name') and t.name == target_name), None)
        if not target_thing and not target_brush:
            print(f"Play mode warning: Trigger target '{target_name}' not found.")
            return

        if isinstance(target_thing, Light):
            current_state = target_thing.properties.get('state', 'on')
            new_state = 'off' if current_state == 'on' else 'on'
            target_thing.properties['state'] = new_state
        elif isinstance(target_thing, Speaker):
            if target_thing.name in self.active_sounds:
                self.stop_sound_for_speaker(target_thing.name)
            else:
                self.play_sound_for_speaker(target_thing)

        if trigger_frequency == 'once':
            self.fired_once_triggers.add(trigger_id)

    def initialize_sounds(self):
        for thing in self.editor.state.things:
            if isinstance(thing, Speaker):
                is_global = thing.properties.get('global', False)
                play_on_start = thing.properties.get('play_on_start', True)
                if is_global and play_on_start:
                    self.play_sound_for_speaker(thing)
    def stop_all_sounds(self):
        for sound in self.active_sounds.values():
            sound.stop()
        self.active_sounds.clear()
    def play_sound_for_speaker(self, speaker):
        if speaker.name in self.played_once_sounds or speaker.name in self.active_sounds: return
        sound_file_rel = speaker.properties.get('sound_file')
        if not sound_file_rel: return
        sound_path = os.path.join('assets', sound_file_rel)
        if not os.path.exists(sound_path): print(f"Audio Error: Sound file not found at '{sound_path}'"); return
        sound_effect = QSoundEffect(self)
        sound_effect.setSource(QUrl.fromLocalFile(sound_path))
        sound_effect.setLoopCount(QSoundEffect.Infinite if speaker.properties.get('looping', False) else 1)
        sound_effect.setVolume(speaker.properties.get('volume', 1.0))
        if speaker.properties.get('play_once', False):
            def on_status_changed(status):
                if status == QSoundEffect.StoppedState:
                    self.played_once_sounds.add(speaker.name)
                    try: sound_effect.statusChanged.disconnect()
                    except TypeError: pass
            sound_effect.statusChanged.connect(on_status_changed)
        self.active_sounds[speaker.name] = sound_effect
        sound_effect.play()
    def stop_sound_for_speaker(self, speaker_name):
        if speaker_name in self.active_sounds:
            self.active_sounds[speaker_name].stop()
            del self.active_sounds[speaker_name]
    def update_speaker_sounds(self):
        if not self.player: return
        player_pos = self.player.pos
        for thing in self.editor.state.things:
            if isinstance(thing, Speaker) and not thing.properties.get('global', False):
                speaker_pos, radius = glm.vec3(thing.pos), thing.get_radius()
                distance = glm.distance(player_pos, speaker_pos)
                is_playing = thing.name in self.active_sounds
                if distance <= radius:
                    if not is_playing and thing.properties.get('play_on_start', True):
                        self.play_sound_for_speaker(thing)
                        is_playing = thing.name in self.active_sounds
                    if is_playing:
                        attenuation = (1.0 - (distance / radius))**2
                        final_volume = thing.properties.get('volume', 1.0) * attenuation
                        self.active_sounds[thing.name].setVolume(final_volume)
                elif is_playing:
                    self.stop_sound_for_speaker(thing.name)

    def init_mover_states(self):
        """Initialize mover states when entering play mode."""
        self.mover_states = {}
        for i, brush in enumerate(self.editor.state.brushes):
            if brush.get('is_mover') and not brush.get('move_once', False):
                # Store original position if not already stored
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                # Initialize animation state
                self.mover_states[i] = {
                    'progress': 0.0,
                    'forward': True
                }

    def reset_mover_states(self):
        """Reset movers to original positions when exiting play mode."""
        for i, brush in enumerate(self.editor.state.brushes):
            if brush.get('is_mover') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.mover_states = {}

    def update_movers(self, delta):
        """Update all active movers. Call this every frame during play mode."""
        for i, brush in enumerate(self.editor.state.brushes):
            # Skip non-movers and move_once movers (they teleport, don't animate)
            if not brush.get('is_mover') or brush.get('move_once', False):
                continue
            
            # Skip movers that aren't turned on
            if not brush.get('start_on', False):
                continue
            
            # Get or create mover state
            if i not in self.mover_states:
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                self.mover_states[i] = {'progress': 0.0, 'forward': True}
            
            state = self.mover_states[i]
            
            # Get mover properties
            speed = brush.get('speed', 64.0)  # units per second
            distance = brush.get('distance', 128.0)
            direction = np.array(brush.get('direction', [0, 1, 0]), dtype=float)
            
            # Normalize direction
            dir_length = np.linalg.norm(direction)
            if dir_length > 0:
                direction = direction / dir_length
            
            # Calculate progress change this frame
            if distance > 0:
                progress_delta = (speed * delta) / distance
            else:
                progress_delta = 0
            
            # Update progress based on direction of travel
            if state['forward']:
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['forward'] = False  # Reverse direction (ping-pong)
            else:
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['forward'] = True  # Reverse direction (ping-pong)
            
            # Apply eased position (smooth start/stop)
            eased_progress = self._ease_in_out(state['progress'])
            
            # Calculate new position
            original = np.array(brush['original_pos'])
            offset = direction * distance * eased_progress
            new_pos = original + offset
            
            brush['pos'] = new_pos.tolist()

    def _ease_in_out(self, t):
        """Smooth easing function for natural movement."""
        # Cubic ease-in-out
        if t < 0.5:
            return 4 * t * t * t
        else:
            return 1 - pow(-2 * t + 2, 3) / 2

    def get_selected_object_pos(self):
        if not self.editor.state.selected_object: return None
        if isinstance(self.editor.state.selected_object, dict):
            return glm.vec3(self.editor.state.selected_object['pos'])
        return glm.vec3(self.editor.state.selected_object.pos)

    def set_selected_object_pos(self, new_pos_vec):
        if not self.editor.state.selected_object: return
        grid = self.editor.grid_size_spinbox.value()
        snapped_pos_list = [round(c / grid) * grid for c in new_pos_vec]
        if isinstance(self.editor.state.selected_object, dict):
            self.editor.state.selected_object['pos'] = snapped_pos_list
        else:
            self.editor.state.selected_object.pos = snapped_pos_list

    def get_ray_from_mouse(self, x, y):
        win_x, win_y = float(x), float(self.height() - y)
        viewport = glm.vec4(0, 0, self.width(), self.height())
        near_point = glm.unProject(glm.vec3(win_x, win_y, 0.0), self.view_matrix, self.projection_matrix, viewport)
        far_point = glm.unProject(glm.vec3(win_x, win_y, 1.0), self.view_matrix, self.projection_matrix, viewport)
        ray_dir = glm.normalize(far_point - near_point)
        return near_point, ray_dir

    def intersect_ray_with_axis(self, ray_origin, ray_dir, axis_origin, axis_dir):
        cross_axis_ray = glm.cross(axis_dir, ray_dir)
        denominator = glm.dot(cross_axis_ray, cross_axis_ray)
        if abs(denominator) < 1e-6: return None, float('inf')
        t = glm.dot(glm.cross(ray_origin - axis_origin, ray_dir), cross_axis_ray) / denominator
        point_on_axis = axis_origin + t * axis_dir
        t_ray = glm.dot(point_on_axis - ray_origin, ray_dir)
        point_on_ray = ray_origin + t_ray * ray_dir
        distance = glm.distance(point_on_axis, point_on_ray)
        return point_on_axis, distance

    def handle_keyboard_input(self, delta):
        speed, keys = 300 * delta, self.editor.keys_pressed
        if any(key in keys for key in [Qt.Key_W, Qt.Key_S, Qt.Key_A, Qt.Key_D, Qt.Key_Space, Qt.Key_C]):
            if Qt.Key_W in keys: self.camera.move_forward(speed)
            if Qt.Key_S in keys: self.camera.move_forward(-speed)
            if Qt.Key_A in keys: self.camera.strafe(-speed)
            if Qt.Key_D in keys: self.camera.strafe(speed)
            if Qt.Key_Space in keys: self.camera.move_up(speed)
            if Qt.Key_C in keys: self.camera.move_up(-speed)
            self.editor.update_views()

    def get_face_at(self, mouse_pos):
        if not isinstance(self.editor.state.selected_object, dict):
            return None

        brush = self.editor.state.selected_object
        ray_origin, ray_dir = self.get_ray_from_mouse(mouse_pos.x(), mouse_pos.y())

        pos = glm.vec3(brush['pos'])
        size = glm.vec3(brush['size'])
        min_b = pos - size / 2.0
        max_b = pos + size / 2.0

        tmin = 0.0
        tmax = float('inf')

        for i in range(3):
            if abs(ray_dir[i]) < 1e-6:
                if ray_origin[i] < min_b[i] or ray_origin[i] > max_b[i]:
                    return None
            else:
                t1 = (min_b[i] - ray_origin[i]) / ray_dir[i]
                t2 = (max_b[i] - ray_origin[i]) / ray_dir[i]
                
                if t1 > t2: t1, t2 = t2, t1
                
                tmin = max(tmin, t1)
                tmax = min(tmax, t2)

        if tmin > tmax:
            return None

        intersection_point = ray_origin + ray_dir * tmin
        
        local_point = intersection_point - pos
        abs_local = abs(local_point)
        
        face_map = {
            'x': ['west', 'east'],
            'y': ['bottom', 'top'],
            'z': ['south', 'north']
        }
        
        max_coord = max(abs_local.x / size.x, abs_local.y / size.y, abs_local.z / size.z)

        face = ""
        if max_coord == abs_local.x / size.x:
            face = face_map['x'][1] if local_point.x > 0 else face_map['x'][0]
        elif max_coord == abs_local.y / size.y:
            face = face_map['y'][1] if local_point.y > 0 else face_map['y'][0]
        else:
            face = face_map['z'][1] if local_point.z > 0 else face_map['z'][0]
            
        return face

    def mousePressEvent(self, event):
        # 1. Debug Window Manager Interaction
        if self.debug_mode_active and event.button() == Qt.LeftButton:
            rect = self.debug_window_rect
            mouse_pos = event.pos()
            
            # Check Expand/Collapse Arrow
            expand_btn_rect = QRect(rect.right() - 50, rect.y(), 25, 25)
            if expand_btn_rect.contains(mouse_pos):
                self.sysmon_expanded = not self.sysmon_expanded
                self.update()
                return
            
            # Check Close Button [X] (Top right 25x25)
            close_btn_rect = QRect(rect.right() - 25, rect.y(), 25, 25)
            if close_btn_rect.contains(mouse_pos):
                self.debug_mode_active = False
                # Restore Play Mode Cursor if playing
                if self.play_mode:
                    center_pos = self.mapToGlobal(self.rect().center())
                    QCursor.setPos(center_pos)
                    self.last_mouse_pos = self.mapFromGlobal(center_pos)
                    QApplication.setOverrideCursor(Qt.BlankCursor)
                self.update()
                return

            # Check Header Bar (Dragging)
            header_rect = QRect(rect.x(), rect.y(), rect.width() - 25, 25)
            if header_rect.contains(mouse_pos):
                self.debug_drag_active = True
                self.debug_drag_offset = mouse_pos - rect.topLeft()
                return
                
            # If clicking inside window body, swallow event (don't shoot/move)
            if rect.contains(mouse_pos):
                return

        # 2. Existing Interactions
        if event.button() == Qt.LeftButton and QApplication.keyboardModifiers() == Qt.ControlModifier and not self.play_mode:
            face_name = self.get_face_at(event.pos())
            if face_name:
                self.editor.selected_face = face_name
                self.update()
                return
                
        if event.button() == Qt.LeftButton and self.editor.state.selected_object and not self.play_mode:
            if isinstance(self.editor.state.selected_object, dict) and self.editor.state.selected_object.get('lock', False): return
            obj_pos = self.get_selected_object_pos()
            if obj_pos is None: return
            ray_origin, ray_dir = self.get_ray_from_mouse(event.x(), event.y())
            axes = {'x': glm.vec3(1, 0, 0), 'y': glm.vec3(0, 1, 0), 'z': glm.vec3(0, 0, 1)}
            gizmo_render_size = 32.0
            cam_dist = glm.distance(self.camera.pos, obj_pos)
            click_threshold = max(1.0, cam_dist * 0.025)
            min_dist_to_axis, hit_axis, hit_point = float('inf'), None, None
            for name, axis_dir in axes.items():
                point_on_axis, dist = self.intersect_ray_with_axis(ray_origin, ray_dir, obj_pos, axis_dir)
                if point_on_axis is not None:
                    dist_from_origin = glm.distance(point_on_axis, obj_pos)
                    if dist < click_threshold and dist_from_origin <= gizmo_render_size * 1.2:
                        if dist < min_dist_to_axis: min_dist_to_axis, hit_axis, hit_point = dist, name, point_on_axis
            if hit_axis:
                self.editor.save_state()
                self.is_dragging_gizmo = True
                self.gizmo_drag_axis = hit_axis
                self.gizmo_object_start_pos = obj_pos
                self.drag_start_on_axis = hit_point
                self.setCursor(Qt.ClosedHandCursor)
                return
        if not self.play_mode and event.button() == Qt.RightButton:
            self.mouselook_active, self.last_mouse_pos = True, event.pos()
            self.setCursor(Qt.BlankCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 1. Debug Window Dragging
        if self.debug_drag_active:
            new_top_left = event.pos() - self.debug_drag_offset
            self.debug_window_rect.moveTopLeft(new_top_left)
            self.update()
            return
            
        if self.is_dragging_gizmo:
            ray_origin, ray_dir = self.get_ray_from_mouse(event.x(), event.y())
            axis_dir = {'x': glm.vec3(1,0,0), 'y': glm.vec3(0,1,0), 'z': glm.vec3(0,0,1)}[self.gizmo_drag_axis]
            current_point_on_axis, _ = self.intersect_ray_with_axis(ray_origin, ray_dir, self.gizmo_object_start_pos, axis_dir)
            if current_point_on_axis is not None:
                displacement = current_point_on_axis - self.drag_start_on_axis
                new_pos = self.gizmo_object_start_pos + displacement
                self.set_selected_object_pos(new_pos)
                self.editor.update_all_ui()
            return
        
        # --- Handle Play Mode Mouse Look ---
        if self.play_mode:
            # Skip mouse look if we are in Debug Mode
            if self.debug_mode_active:
                return

            current_pos = event.pos()
            dx = current_pos.x() - self.last_mouse_pos.x()
            dy = current_pos.y() - self.last_mouse_pos.y()
            
            # Ignore 0,0 movements (caused by recentering)
            if dx == 0 and dy == 0:
                return

            if self.use_threading:
                # Threaded Mode: Send inputs to GameState.
                # Do NOT update self.player directly to avoid race conditions 
                # between the logic thread and render interpolation.
                self.game_state.set_mouse_delta(float(dx), float(dy))
            elif self.player:
                # Legacy Fallback
                self.player.update_angle(dx, dy)

            # Recenter cursor
            center_pos = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center_pos)
            self.last_mouse_pos = self.mapFromGlobal(center_pos)
            return

        if not self.mouselook_active:
            super().mouseMoveEvent(event)
            return

        # Editor Camera Look (Legacy behavior for editor)
        if not self.play_mode:
            dx, dy = event.x() - self.last_mouse_pos.x(), event.y() - self.last_mouse_pos.y()
            self.camera.rotate(dx, dy)
            center_pos = self.mapToGlobal(self.rect().center())
            QCursor.setPos(center_pos)
            self.last_mouse_pos = self.mapFromGlobal(center_pos)
            self.editor.update_views()

    def mouseReleaseEvent(self, event):
        if self.debug_drag_active:
            self.debug_drag_active = False
            return
            
        if self.is_dragging_gizmo and event.button() == Qt.LeftButton:
            self.is_dragging_gizmo = False
            self.editor.save_state()
            self.setCursor(Qt.ArrowCursor)
            return
        if event.button() == Qt.RightButton and self.mouselook_active and not self.play_mode:
            self.mouselook_active = False
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self.play_mode: return
        self.camera.fov = np.clip(self.camera.fov - event.angleDelta().y() * 0.05, 30, 120)
        self.editor.update_views()