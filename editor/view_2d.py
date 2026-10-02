import numpy as np
import math
import time
import os
from PyQt5.QtWidgets import QWidget, QMenu, QFileDialog
from PyQt5.QtGui import QPainter, QPen, QBrush, QColor, QFont, QPolygonF, QPixmap
from PyQt5.QtCore import Qt, QRectF, QPointF, QPoint, QTimer
from editor.things import (Thing, Light, PlayerStart, Pickup, Speaker, Model, 
                          LogicGate, LogicRelay, LogicTimer)
from editor.scene_hierarchy import SceneHierarchy
# I/O System imports for drawing connections
try:
    from editor.io_system import get_connections
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False

class View2D(QWidget):
    def __init__(self, editor, main_window, view_type):
        # Only pass 'editor' (which acts as the parent widget) to super().__init__
        super().__init__(editor)
        self.editor = editor
        self.main_window = main_window
        self.view_type = view_type
        
        self.zoom_factor = 1.0
        self.pan_offset = QPointF(0.0, 0.0)
        
        # State variables for mouse actions
        self.is_panning = False
        self.is_drawing_brush = False
        self.is_dragging_object = False
        self.is_resizing_brush = False
        self.resize_handle_ix = -1
        
        # Coordinates for tracking mouse movement
        self.last_pan_pos = QPoint()
        self.pan_start_pos = QPoint()
        
        self.draw_start_pos = QPointF()
        self.draw_current_pos = QPointF()
        self.drag_start_pos = QPointF()
        self.drag_offset = QPointF()

        self.initial_brush_rect = QRectF() 
        self.grid_size = 16
        self.world_size = 1024
        self.snap_to_grid_enabled = True
        self.grid_visible = True  # Grid visibility (controlled by toggle button)

        # Throttle tracker for 3D updates during drag
        self.last_3d_update_time = 0.0

        # Add timer-based smooth updating
        self.smooth_update_timer = QTimer(self)
        self.smooth_update_timer.setInterval(33)  # ~30 FPS
        self.smooth_update_timer.timeout.connect(self._smooth_update_tick)
        self.smooth_update_timer_active = False
        
        # Camera tracking for efficient updates
        self.last_camera_pos = None
        self.last_camera_yaw = None
        
        # Connection line animation state
        self.connection_animations = {}
        self.last_connections = set()
        
        # Animated arrow state - arrows traveling along connection lines
        self.arrow_travel_progress = {}  # {conn_key: [arrow_positions]}
        
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._update_connection_animations)
        self.animation_timer.start(16)
        
        # CTRL+drag connection state
        self.is_connecting = False
        self.connection_source = None  # The trigger brush being connected
        self.connection_drag_pos = QPointF()  # Current mouse position during drag
        self.connection_snap_target = None  # Target object we're snapping to
        self.connection_snap_threshold = 30  # Pixels to snap within

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setContextMenuPolicy(Qt.NoContextMenu)

        # Initialize color tag icons
        self.color_pixmaps = {}
        if hasattr(main_window, 'scene_hierarchy'):
            for color_name, qicon in SceneHierarchy(main_window).colour_icons.items():
                self.color_pixmaps[color_name] = qicon.pixmap(18, 18) 

    def reset_state(self):
        self.is_dragging_object = False
        self.is_resizing_brush = False
        self.resize_handle_ix = -1
        self.is_connecting = False
        self.connection_source = None
        self.connection_snap_target = None
        self.update()

    def start_connection_mode(self, source_obj):
        """Start connection mode programmatically (e.g., from property editor)."""
        if not source_obj:
            return
        
        self.is_connecting = True
        self.connection_source = source_obj
        self.connection_snap_target = None
        
        # Set initial drag position to object center
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        # FIX: Handle Brush (dict) vs Thing (object)
        if isinstance(source_obj, dict):
            source_pos = source_obj['pos']
        else:
            source_pos = source_obj.pos
            
        self.connection_drag_pos = QPointF(source_pos[ax_map[ax1]], source_pos[ax_map[ax2]])
        
        self.setCursor(Qt.CrossCursor)
        self.setFocus()  # Take focus so we can receive key events
        self.update()

    def keyPressEvent(self, event):
        # F1 Synchronization ---
        if event.key() == Qt.Key_F1:
            # Toggle the global flag on the editor state
            current_state = getattr(self.editor, 'show_logic_links', False)
            self.editor.show_logic_links = not current_state
            
            # Force redraw of both views (2D and 3D)
            self.editor.update_views()
            
            # Show toast
            if hasattr(self.main_window, 'show_toast'):
                status = "ON" if self.editor.show_logic_links else "OFF"
                self.main_window.show_toast(f"Logic Links: {status}")
            return

        super().keyPressEvent(event)

    def _smooth_update_tick(self):
        """Check for camera changes and repaint only when needed."""
        if not self.isVisible():
            return
            
        current_pos, current_yaw = self.get_camera_state_in_2d()
        
        # Only repaint if camera moved significantly
        if (self.last_camera_pos is None or 
            (self.last_camera_pos - current_pos).manhattanLength() > 0.5 or
            self.last_camera_yaw != current_yaw):
            
            self.last_camera_pos = current_pos
            self.last_camera_yaw = current_yaw
            self.update()

    def get_camera_state_in_2d(self):
        """Get camera position and relevant rotation for this 2D view."""
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return QPointF(0, 0), 0
            
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        camera = self.editor.view_3d.camera
        
        pos_2d = QPointF(camera.pos[ax_map[ax1]], camera.pos[ax_map[ax2]])
        
        # Extract relevant rotation
        if self.view_type == 'top':
            rotation = camera.yaw
        elif self.view_type == 'front':
            rotation = -camera.yaw
        elif self.view_type == 'side':
            rotation = -camera.pitch
        else:
            rotation = 0
            
        return pos_2d, rotation

    def start_smooth_updates(self):
        """Enable smooth 30 FPS updates when camera is moving."""
        if not self.smooth_update_timer_active:
            self.smooth_update_timer_active = True
            # Initialize tracking
            self.last_camera_pos, self.last_camera_yaw = self.get_camera_state_in_2d()
            self.smooth_update_timer.start()

    def stop_smooth_updates(self):
        """Disable smooth updates when camera is stationary."""
        if self.smooth_update_timer_active:
            self.smooth_update_timer_active = False
            self.smooth_update_timer.stop()
            self.last_camera_pos = None
            self.last_camera_yaw = None

    def get_visible_world_bounds(self):
        """Returns a QRectF of the visible world area in this 2D view."""
        width = self.width()
        height = self.height()
        
        # Handle invalid size
        if width <= 0 or height <= 0 or self.zoom_factor <= 0:
            return QRectF(0, 0, 0, 0)
        
        # Calculate half dimensions in world space
        half_width_world = width / (2.0 * self.zoom_factor)
        half_height_world = height / (2.0 * self.zoom_factor)
        
        # Create bounds rect centered on pan_offset
        bounds = QRectF(
            self.pan_offset.x() - half_width_world,
            self.pan_offset.y() - half_height_world,
            half_width_world * 2,
            half_height_world * 2
        )
        
        return bounds

    def is_brush_visible(self, brush, visible_bounds, axis1_idx, axis2_idx):
        """Check if a brush's projected bounding box intersects the visible area."""
        if brush.get('hidden', False):
            return False
        
        # Get brush bounds in the 2D view's coordinate system
        pos = brush.get('pos', [0, 0, 0])
        size = brush.get('size', [0, 0, 0])
        
        # Skip degenerate brushes
        if size[axis1_idx] <= 0 or size[axis2_idx] <= 0:
            return False
        
        # Calculate min/max in world coordinates for the two axes
        min_x = pos[axis1_idx] - size[axis1_idx] / 2.0
        max_x = pos[axis1_idx] + size[axis1_idx] / 2.0
        min_y = pos[axis2_idx] - size[axis2_idx] / 2.0
        max_y = pos[axis2_idx] + size[axis2_idx] / 2.0
        
        # Create brush bounds rect
        brush_bounds = QRectF(min_x, min_y, max_x - min_x, max_y - min_y)
        
        # Check for intersection with visible bounds
        return visible_bounds.intersects(brush_bounds)

    def is_thing_visible(self, thing, visible_bounds, axis1_idx, axis2_idx):
        """Check if a thing's position is within the visible area (with margin)."""
        # Get thing position in the 2D view's coordinate system
        pos = thing.pos
        x = pos[axis1_idx]
        y = pos[axis2_idx]
        
        # Add margin for sprite size (approximate 32px sprite)
        margin = 32.0 / self.zoom_factor if self.zoom_factor > 0 else 32.0
        
        point_rect = QRectF(x - margin, y - margin, margin * 2, margin * 2)
        return visible_bounds.intersects(point_rect)

    def get_axes(self):
        if self.view_type == 'top': return 'x', 'z'
        elif self.view_type == 'side': return 'z', 'y'
        elif self.view_type == 'front': return 'x', 'y'
        return None, None
        
    def world_to_screen(self, p):
        center_x, center_y = self.width() / 2, self.height() / 2
        screen_x = center_x + (p.x() - self.pan_offset.x()) * self.zoom_factor
        
        if self.view_type in ['front', 'side']:
            screen_y = center_y - (p.y() - self.pan_offset.y()) * self.zoom_factor
        else:
            screen_y = center_y + (p.y() - self.pan_offset.y()) * self.zoom_factor
            
        return QPointF(screen_x, screen_y)

    def screen_to_world(self, p):
        center_x, center_y = self.width() / 2, self.height() / 2
        world_x = (p.x() - center_x) / self.zoom_factor + self.pan_offset.x()
        
        if self.view_type in ['front', 'side']:
            world_y = (center_y - p.y()) / self.zoom_factor + self.pan_offset.y()
        else:
            world_y = (p.y() - center_y) / self.zoom_factor + self.pan_offset.y()
            
        return QPointF(world_x, world_y)

    def snap_to_grid(self, pos):
        if not self.snap_to_grid_enabled:
            return pos
        grid = self.grid_size
        return QPointF(round(pos.x() / grid) * grid, round(pos.y() / grid) * grid)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(50, 50, 50))
        
        visible_bounds = self.get_visible_world_bounds()
        if visible_bounds.width() <= 0 or visible_bounds.height() <= 0:
            return
        
        self.draw_grid(painter)
        self.draw_terrain(painter, visible_bounds)
        self.draw_brushes(painter, visible_bounds)
        self.draw_things(painter, visible_bounds)
        self.draw_camera(painter)
        
        # --- REVISED: Logic/Trigger Connections ---
        # Only draw if the global toggle is ON (F1)
        show_f1_key = getattr(self.editor, 'show_logic_links', False)
        
        if show_f1_key:
            self.draw_logic_connections(painter, visible_bounds)

        if self.is_drawing_brush:
            pen = QPen(QColor(255, 255, 0), 1, Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            start_screen = self.world_to_screen(self.draw_start_pos)
            current_screen = self.world_to_screen(self.draw_current_pos)
            painter.drawRect(QRectF(start_screen, current_screen).normalized())
        
        if self.is_connecting and self.connection_source:
            self.draw_connection_drag(painter)

    def draw_logic_connections(self, painter, visible_bounds):
        """
        Draws I/O connections between entities.
        Respects 'Animate Connections' setting.
        """
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2: return
            
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        axis1_idx = ax_map[ax1]
        axis2_idx = ax_map[ax2]
        
        # Helper to find position by name
        def get_pos_by_name(name):
            for b in self.editor.state.brushes:
                if b.get('name') == name: return b['pos']
            for t in self.editor.state.things:
                t_name = getattr(t, 'name', t.properties.get('name'))
                if t_name == name: return t.pos
            return None

        # Check Animation Setting
        should_animate = self.main_window.config.getboolean('Display', 'animate_connections', fallback=False)

        current_connections = set()
        connections_to_draw = []
        
        # Collect I/O System Connections
        if IO_AVAILABLE:
            # From brushes
            for brush in self.editor.state.brushes:
                io_conns = get_connections(brush)
                for conn in io_conns:
                    target_pos = get_pos_by_name(conn.target_name)
                    if target_pos:
                        # Determine if this is a logic entity
                        is_logic = brush.get('is_trigger', False) or brush.get('is_mover', False) or brush.get('is_door', False)
                        connections_to_draw.append({
                            'id': f"io_brush_{id(brush)}_{conn.output_name}",
                            'target': conn.target_name,
                            'src': brush['pos'],
                            'dst': target_pos,
                            'is_logic': is_logic
                        })
            
            # From things
            for thing in self.editor.state.things:
                io_conns = get_connections(thing)
                for conn in io_conns:
                    target_pos = get_pos_by_name(conn.target_name)
                    if target_pos:
                        is_logic = thing.properties.get('type') == 'logic_gate'
                        connections_to_draw.append({
                            'id': f"io_thing_{id(thing)}_{conn.output_name}",
                            'target': conn.target_name,
                            'src': thing.pos,
                            'dst': target_pos,
                            'is_logic': is_logic
                        })

        # Draw all collected connections
        for conn in connections_to_draw:
            source_2d = QPointF(conn['src'][axis1_idx], conn['src'][axis2_idx])
            target_2d = QPointF(conn['dst'][axis1_idx], conn['dst'][axis2_idx])
            
            # Culling
            margin = 100.0 
            s_rect = QRectF(source_2d.x()-margin, source_2d.y()-margin, margin*2, margin*2)
            t_rect = QRectF(target_2d.x()-margin, target_2d.y()-margin, margin*2, margin*2)
            if not (visible_bounds.intersects(s_rect) or visible_bounds.intersects(t_rect)):
                continue

            conn_key = (conn['id'], conn['target'])
            current_connections.add(conn_key)
            
            p1 = self.world_to_screen(source_2d)
            p2 = self.world_to_screen(target_2d)
            
            # Select Color
            if conn['is_logic']:
                color = QColor(255, 255, 0, 200) # Yellow for Logic
            else:
                color = QColor(0, 255, 255, 180) # Cyan for Standard Triggers
                
            pen = QPen(color, 2, Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(p1, p2)
            
            # Draw Arrows based on setting
            if should_animate:
                # Initialize animation state if needed
                if conn_key not in self.connection_animations:
                    self.connection_animations[conn_key] = {'progress': 1.0, 'growing': True}
                    self.arrow_travel_progress[conn_key] = [0.0]
                
                self._draw_traveling_arrows(painter, p1, p2, color, conn_key)
            else:
                # Draw a single static arrow head at the target end
                self._draw_connection_arrow(painter, p1, p2, color)
        
        self.last_connections = current_connections

    def draw_grid(self, painter):
        # Don't draw grid if hidden or in play mode
        if not self.grid_visible:
            return
        if hasattr(self.editor, 'view_3d') and self.editor.view_3d.play_mode:
            return
            
        grid_color = QColor(70, 70, 70)
        thick_grid_color = QColor(90, 90, 90)
        world_origin_color = QColor(0, 255, 0)
        painter.setPen(QPen(grid_color, 1))

        screen_rect = self.rect()
        top_left_world = self.screen_to_world(screen_rect.topLeft())
        bottom_right_world = self.screen_to_world(screen_rect.bottomRight())
        
        grid = self.grid_size
        if grid * self.zoom_factor < 4: return 

        start_x = int(top_left_world.x() / grid) * grid
        end_x = int(bottom_right_world.x() / grid) * grid
        start_y = int(top_left_world.y() / grid) * grid
        end_y = int(bottom_right_world.y() / grid) * grid

        for x in range(start_x, end_x + 1, grid):
            is_thick = (x % (grid * 8)) == 0
            is_origin = x == 0
            pen = QPen(thick_grid_color if is_thick else grid_color, 1)
            if is_origin: pen.setColor(world_origin_color)
            painter.setPen(pen)
            p1 = self.world_to_screen(QPointF(x, top_left_world.y()))
            p2 = self.world_to_screen(QPointF(x, bottom_right_world.y()))
            painter.drawLine(p1, p2)
        
        for y in range(start_y, end_y + 1, grid):
            is_thick = (y % (grid * 8)) == 0
            is_origin = y == 0
            pen = QPen(thick_grid_color if is_thick else grid_color, 1)
            if is_origin: pen.setColor(world_origin_color)
            painter.setPen(pen)
            p1 = self.world_to_screen(QPointF(top_left_world.x(), y))
            p2 = self.world_to_screen(QPointF(bottom_right_world.x(), y))
            painter.drawLine(p1, p2)

    def draw_brushes(self, painter, visible_bounds):
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return
            
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        axis1_idx = ax_map[ax1]
        axis2_idx = ax_map[ax2]
        
        for brush in self.editor.state.brushes:
            # CULL brush if not visible
            if not self.is_brush_visible(brush, visible_bounds, axis1_idx, axis2_idx):
                continue
                
            # Check if brush is in selected_objects list (for multi-select support)
            is_selected = brush in getattr(self.editor.state, 'selected_objects', []) or brush is self.editor.state.selected_object
            is_trigger = brush.get('is_trigger', False)
            is_subtractive = brush.get('operation') == 'subtract'
            is_locked = brush.get('lock', False)
            is_fog = brush.get('is_fog', False)
            is_mover = brush.get('is_mover', False)
            
            # Check for flash effect (takes precedence over other colors)
            is_flashing = False
            flash_until = brush.get('_flash_until', 0)
            if flash_until > time.time():
                is_flashing = True
            
            # Apply colors based on state (flash overrides everything)
            if is_flashing:
                pen_color = QColor(255, 105, 180)  # Hot pink
                fill_color = QColor(255, 105, 180, 60)
            elif is_locked:
                pen_color = QColor(255, 105, 97) 
                fill_color = QColor(74,4,4, 20)
            elif is_trigger:
                pen_color = QColor(0, 255, 255, 150)
                fill_color = QColor(0, 255, 255, 30)
            elif is_subtractive:
                pen_color = QColor(255, 0, 0)
                fill_color = QColor(255, 0, 0, 30)
            elif is_fog:
                fog_color_rgb = brush.get('fog_color', [0.5, 0.6, 0.7])
                pen_color = QColor.fromRgbF(fog_color_rgb[0], fog_color_rgb[1], fog_color_rgb[2])
                fill_color = QColor.fromRgbF(fog_color_rgb[0], fog_color_rgb[1], fog_color_rgb[2], 0.3)
            elif is_mover:
                pen_color = QColor(0, 120, 255)
                fill_color = QColor(0, 120, 255, 50)
            else:
                pen_color = QColor(211, 211, 211)
                fill_color = QColor(200, 200, 200, 30)

            if is_selected and not is_flashing:  # Don't override flash with selection
                pen_color = QColor(255, 255, 0)
            
            pen = QPen(pen_color, 2 if is_selected else 1)
            if (is_trigger or is_fog) and not is_selected and not is_flashing: 
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(fill_color))

            pos = brush['pos']
            size = brush['size']
            
            w_pos = QPointF(pos[axis1_idx] - size[axis1_idx]/2, pos[axis2_idx] - size[axis2_idx]/2)
            w_size = QPointF(size[axis1_idx], size[axis2_idx])
            p1 = self.world_to_screen(w_pos)
            p2 = self.world_to_screen(w_pos + w_size)
            screen_rect = QRectF(p1, p2).normalized()
            painter.drawRect(screen_rect)

            if is_trigger:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, "t r i g g e r")
            
            if is_mover:
                self.draw_mover_arrow(painter, brush, ax1, ax2, ax_map)
            
            # Draw glow light direction arrow
            if brush.get('shader') == 'Glow':
                # Hide arrows in play mode unless F5 toggle is enabled
                play_mode = getattr(self.main_window.view_3d, 'play_mode', False)
                show_arrows = getattr(self.main_window.view_3d, 'show_glow_arrows_in_play_mode', False)
                if not play_mode or show_arrows:
                    self.draw_glow_light_arrow(painter, brush, ax1, ax2, ax_map)

            if is_selected and not is_locked:
                self.draw_resize_handles(painter, screen_rect)
            
            self.draw_brush_color_tag(painter, brush, screen_rect)

    def draw_mover_arrow(self, painter, brush, ax1, ax2, ax_map):
        direction = brush.get('direction', [0, 1, 0])
        distance = brush.get('distance', 128.0)
        start_3d = brush['pos']
        d_vec = np.array(direction, dtype=float)
        norm = np.linalg.norm(d_vec)
        if norm == 0: return 
        d_vec = d_vec / norm * distance
        end_3d = [start_3d[0] + d_vec[0], start_3d[1] + d_vec[1], start_3d[2] + d_vec[2]]
        
        p_start = QPointF(start_3d[ax_map[ax1]], start_3d[ax_map[ax2]])
        p_end = QPointF(end_3d[ax_map[ax1]], end_3d[ax_map[ax2]])
        
        if (p_start - p_end).manhattanLength() < 2: return 

        s_start = self.world_to_screen(p_start)
        s_end = self.world_to_screen(p_end)
        
        arrow_color = QColor(0, 255, 0)
        painter.setPen(QPen(arrow_color, 2))
        painter.drawLine(s_start, s_end)
        
        angle = math.atan2(s_end.y() - s_start.y(), s_end.x() - s_start.x())
        arrow_size = 10
        p1 = s_end - QPointF(math.cos(angle - math.pi / 6) * arrow_size, math.sin(angle - math.pi / 6) * arrow_size)
        p2 = s_end - QPointF(math.cos(angle + math.pi / 6) * arrow_size, math.sin(angle + math.pi / 6) * arrow_size)
        painter.setBrush(QBrush(arrow_color))
        painter.drawPolygon(QPolygonF([s_end, p1, p2]))

    def draw_glow_light_arrow(self, painter, brush, ax1, ax2, ax_map):
        """Draw a wide colored arrow radiating from the glow brush's light emission face."""
        light_direction = brush.get('light_direction', 'top')
        brush_pos = brush['pos']
        brush_size = brush['size']
        
        # Get arrow scale from settings (default 100%)
        arrow_scale = self.main_window.config.getint('Display', 'glow_arrow_scale', fallback=100) / 100.0
        
        # Map face name to direction vector and offset to face center
        face_directions = {
            'top':    ([0, 1, 0], [0, brush_size[1]/2, 0]),
            'bottom': ([0, -1, 0], [0, -brush_size[1]/2, 0]),
            'north':  ([0, 0, 1], [0, 0, brush_size[2]/2]),
            'south':  ([0, 0, -1], [0, 0, -brush_size[2]/2]),
            'east':   ([1, 0, 0], [brush_size[0]/2, 0, 0]),
            'west':   ([-1, 0, 0], [-brush_size[0]/2, 0, 0]),
        }
        
        if light_direction not in face_directions:
            return
        
        direction, face_offset = face_directions[light_direction]
        
        # Calculate start point (center of the emitting face)
        start_3d = [
            brush_pos[0] + face_offset[0],
            brush_pos[1] + face_offset[1],
            brush_pos[2] + face_offset[2]
        ]
        
        # Arrow length based on brush size and scale setting
        base_arrow_length = max(brush_size[0], brush_size[1], brush_size[2]) * 0.8
        arrow_length = base_arrow_length * arrow_scale
        
        # Calculate end point
        end_3d = [
            start_3d[0] + direction[0] * arrow_length,
            start_3d[1] + direction[1] * arrow_length,
            start_3d[2] + direction[2] * arrow_length
        ]
        
        # Cone position - close to the emitting surface (20% along the arrow)
        cone_3d = [
            start_3d[0] + direction[0] * arrow_length * 0.2,
            start_3d[1] + direction[1] * arrow_length * 0.2,
            start_3d[2] + direction[2] * arrow_length * 0.2
        ]
        
        # Project to 2D
        p_start = QPointF(start_3d[ax_map[ax1]], start_3d[ax_map[ax2]])
        p_end = QPointF(end_3d[ax_map[ax1]], end_3d[ax_map[ax2]])
        p_cone = QPointF(cone_3d[ax_map[ax1]], cone_3d[ax_map[ax2]])
        
        # Skip if arrow is too small in this view
        if (p_start - p_end).manhattanLength() < 2:
            return
        
        s_start = self.world_to_screen(p_start)
        s_end = self.world_to_screen(p_end)
        s_cone = self.world_to_screen(p_cone)
        
        # Get brush colour for the arrow (use the glow colour)
        brush_colour = brush.get('colour', [1.0, 0.9, 0.5])  # Default warm glow color
        arrow_color = QColor(
            int(min(255, brush_colour[0] * 255 * 1.2)),  # Slightly brighter
            int(min(255, brush_colour[1] * 255 * 1.2)),
            int(min(255, brush_colour[2] * 255 * 1.2))
        )
        
        # Draw arrow shaft (from cone to tip)
        painter.setPen(QPen(arrow_color, 2))
        painter.drawLine(s_cone, s_end)
        
        # Draw big arrowhead/cone near the surface
        angle = math.atan2(s_end.y() - s_start.y(), s_end.x() - s_start.x())
        arrow_head_size = max(12, 18 * arrow_scale)  # Bigger arrowhead
        arrow_head_width = math.pi / 2  # Wide arrowhead (60 degrees)
        
        p1 = s_cone - QPointF(
            math.cos(angle - arrow_head_width) * arrow_head_size,
            math.sin(angle - arrow_head_width) * arrow_head_size
        )
        p2 = s_cone - QPointF(
            math.cos(angle + arrow_head_width) * arrow_head_size,
            math.sin(angle + arrow_head_width) * arrow_head_size
        )
        
        # Cone tip extends forward from s_cone
        cone_tip = s_cone + QPointF(
            math.cos(angle) * arrow_head_size * 0.6,
            math.sin(angle) * arrow_head_size * 0.6
        )
        
        painter.setPen(QPen(arrow_color, 2))
        painter.setBrush(QBrush(arrow_color))
        painter.drawPolygon(QPolygonF([cone_tip, p1, p2]))

    def draw_brush_color_tag(self, painter, brush, screen_rect):
        color = brush.get('color')
        if isinstance(color, str) and color in self.color_pixmaps:
            pixmap = self.color_pixmaps[color]
            tag_size = 16 
            tag_x = int(screen_rect.bottomRight().x() - tag_size - 2) 
            tag_y = int(screen_rect.bottomRight().y() - tag_size - 2) 
            painter.drawPixmap(tag_x, tag_y, pixmap)
    
    def draw_thing_color_tag(self, painter, thing, screen_rect):
        """Draw color tag for things (same as brushes)"""
        color = thing.properties.get('color')
        if isinstance(color, str) and color in self.color_pixmaps:
            pixmap = self.color_pixmaps[color]
            tag_size = 16 
            tag_x = int(screen_rect.bottomRight().x() - tag_size - 2) 
            tag_y = int(screen_rect.bottomRight().y() - tag_size - 2) 
            painter.drawPixmap(tag_x, tag_y, pixmap)

    def _compute_model_screen_coords(self, model_thing, ax_map, ax1, ax2):
        """Helper to compute screen coordinates for a model's wireframe.
        Returns (pts_x, pts_y) numpy arrays, or None if failed."""
        model_path = model_thing.properties.get('model_path')
        if not model_path: return None

        # Access loaded model from renderer via main window reference
        if not hasattr(self.main_window, 'view_3d') or not self.main_window.view_3d.renderer:
            return None
            
        renderer = self.main_window.view_3d.renderer
        
        # Check if loaded (don't force load during hit test to prevent lag)
        # But ensure loaded for draw. 
        # Since this helper is used by both, we might need to handle load triggering externally or check context.
        # For now, if not loaded, return None. 
        if model_path not in renderer.loaded_models:
            return None
            
        obj = renderer.loaded_models.get(model_path)
        if not obj or not hasattr(obj, 'cpu_vertices') or not obj.cpu_vertices:
            return None

        # Optimization: Too many vertices check
        if len(obj.cpu_vertices) > 2000:
            return None # Treat as box fallback elsewhere

        # Transform parameters
        pos = model_thing.pos
        scale = model_thing.properties.get('scale', 1.0)
        if isinstance(scale, (int, float)): scale = [scale, scale, scale]
        rot = model_thing.properties.get('rotation', [0, 0, 0])
        
        # Build Rotation Matrix
        def rotate_x(a):
            c, s = np.cos(a), np.sin(a)
            return np.array([[1,0,0], [0,c,-s], [0,s,c]])
        def rotate_y(a):
            c, s = np.cos(a), np.sin(a)
            return np.array([[c,0,s], [0,1,0], [-s,0,c]])
        def rotate_z(a):
            c, s = np.cos(a), np.sin(a)
            return np.array([[c,-s,0], [s,c,0], [0,0,1]])

        rx = rotate_x(np.radians(rot[0]))
        ry = rotate_y(np.radians(rot[1]))
        rz = rotate_z(np.radians(rot[2]))
        
        # Rotation Order: Z * X * Y
        R = rz @ rx @ ry 

        # Prepare vertices array from cached OBJ data
        verts = np.array(obj.cpu_vertices)
        
        # Apply Transforms
        verts = verts * np.array(scale)
        verts = verts @ R.T 
        verts = verts + np.array(pos)
        
        # Extract relevant axes for this 2D view
        ix1, ix2 = ax_map[ax1], ax_map[ax2]
        
        # Project to screen coordinates
        center_x, center_y = self.width() / 2, self.height() / 2
        pan_x, pan_y = self.pan_offset.x(), self.pan_offset.y()
        zoom = self.zoom_factor
        
        pts_x = center_x + (verts[:, ix1] - pan_x) * zoom
        if self.view_type in ['front', 'side']:
            pts_y = center_y - (verts[:, ix2] - pan_y) * zoom
        else:
            pts_y = center_y + (verts[:, ix2] - pan_y) * zoom
            
        return pts_x, pts_y

    def _draw_model_wireframe(self, painter, model_thing, ax_map, ax1, ax2):
        """Draws the projected wireframe of a 3D model in the 2D view."""
        # Ensure model is loaded before trying to compute coords
        model_path = model_thing.properties.get('model_path')
        if model_path and hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.renderer:
            renderer = self.main_window.view_3d.renderer
            if model_path not in renderer.loaded_models:
                renderer.load_model(model_path)

        coords = self._compute_model_screen_coords(model_thing, ax_map, ax1, ax2)
        
        if coords is None:
             # Fallback: Draw small placeholder box
             painter.setPen(QPen(QColor(200, 200, 200), 1))
             s_pos = self.world_to_screen(QPointF(model_thing.pos[ax_map[ax1]], model_thing.pos[ax_map[ax2]]))
             painter.drawRect(QRectF(s_pos.x()-10, s_pos.y()-10, 20, 20))
             return

        pts_x, pts_y = coords
        
        painter.setPen(QPen(QColor(0, 255, 255, 100), 1))
        
        width, height = self.width(), self.height()
        
        # Iterate over triangles
        for i in range(0, len(pts_x), 3):
            # Culling optimization
            if (pts_x[i] < 0 and pts_x[i+1] < 0 and pts_x[i+2] < 0) or \
               (pts_x[i] > width and pts_x[i+1] > width and pts_x[i+2] > width) or \
               (pts_y[i] < 0 and pts_y[i+1] < 0 and pts_y[i+2] < 0) or \
               (pts_y[i] > height and pts_y[i+1] > height and pts_y[i+2] > height):
                continue

            p0 = QPointF(pts_x[i], pts_y[i])
            p1 = QPointF(pts_x[i+1], pts_y[i+1])
            p2 = QPointF(pts_x[i+2], pts_y[i+2])
            
            painter.drawLine(p0, p1)
            painter.drawLine(p1, p2)
            painter.drawLine(p2, p0)

    def draw_things(self, painter, visible_bounds):
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return
            
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        axis1_idx = ax_map[ax1]
        axis2_idx = ax_map[ax2]
        
        for thing in self.editor.state.things:
            # Skip hidden things
            if thing.properties.get('hidden', False):
                continue
            
            # CULL thing if not visible
            if not self.is_thing_visible(thing, visible_bounds, axis1_idx, axis2_idx):
                continue
            
            w_pos = QPointF(thing.pos[axis1_idx], thing.pos[axis2_idx])
            s_pos = self.world_to_screen(w_pos)
            
            draw_rect = None

            # --- MODEL RENDERING ---
            if isinstance(thing, Model):
                self._draw_model_wireframe(painter, thing, ax_map, ax1, ax2)
                # Selection box for models
                draw_rect = QRectF(s_pos.x() - 16, s_pos.y() - 16, 32, 32)

            # --- SPRITE RENDERING ---
            else:
                # 1. Draw Radius
                if (isinstance(thing, Light) or isinstance(thing, Speaker)) and thing.properties.get('show_radius', False):
                    default_col = [255, 255, 0] if isinstance(thing, Speaker) else [255, 255, 255]
                    r, g, b = thing.properties.get('colour', default_col)
                    viz_color = QColor(r, g, b, 60)
                    painter.setBrush(QBrush(viz_color))
                    painter.setPen(QPen(viz_color.darker(120), 1))
                    radius = thing.get_radius() * self.zoom_factor
                    painter.drawEllipse(s_pos, radius, radius)

                # 2. Draw Sprite
                if hasattr(thing, 'get_instance_pixmap'):
                    pixmap = thing.get_instance_pixmap()
                else:
                    pixmap = thing.get_pixmap()
                
                if pixmap:
                    pixmap_size = pixmap.size()
                    draw_rect = QRectF(s_pos.x() - pixmap_size.width() / 2, 
                                       s_pos.y() - pixmap_size.height() / 2,
                                       pixmap_size.width(), 
                                       pixmap_size.height())
                    
                    painter.save()
                    painter.translate(s_pos)
                    target_rect = QRectF(-pixmap_size.width() / 2, 
                                         -pixmap_size.height() / 2, 
                                         pixmap_size.width(), 
                                         pixmap_size.height())
                    painter.drawPixmap(target_rect.toRect(), pixmap)
                    painter.restore()

                # 3. Direction Arrow
                angle_deg = None
                if 'angle' in thing.properties:
                    angle_deg = float(thing.properties.get('angle', 0.0))
                
                if angle_deg is not None:
                    painter.save()
                    arrow_color = QColor(0, 255, 255) if thing.__class__.__name__ == 'PlayerStart' else QColor(255, 128, 0)
                    angle_rad = math.radians(angle_deg)
                    arrow_len = 35 * self.zoom_factor
                    if arrow_len < 15: arrow_len = 15
                    
                    dx = math.cos(angle_rad) * arrow_len
                    dy = math.sin(angle_rad) * arrow_len
                    if self.view_type == 'top': dy = -dy 
                    
                    s_end = s_pos + QPointF(dx, dy)
                    painter.setPen(QPen(arrow_color, 2))
                    painter.drawLine(s_pos, s_end)
                    
                    head_angle = math.atan2(s_end.y() - s_pos.y(), s_end.x() - s_pos.x())
                    head_size = 8
                    p1 = s_end - QPointF(math.cos(head_angle - math.pi / 6) * head_size, 
                                         math.sin(head_angle - math.pi / 6) * head_size)
                    p2 = s_end - QPointF(math.cos(head_angle + math.pi / 6) * head_size, 
                                         math.sin(head_angle + math.pi / 6) * head_size)
                    
                    painter.setBrush(QBrush(arrow_color))
                    painter.drawPolygon(QPolygonF([s_end, p1, p2]))
                    painter.restore()

            # 4. Overlays
            if draw_rect:
                self.draw_thing_color_tag(painter, thing, draw_rect)
                is_selected = thing in getattr(self.editor.state, 'selected_objects', []) or thing == self.editor.state.selected_object
                if is_selected:
                    painter.setPen(QPen(QColor(255, 255, 0), 2, Qt.DotLine))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(draw_rect.adjusted(-2, -2, 2, 2))

    def draw_terrain(self, painter, visible_bounds):
        """Draw terrain outline/profile in 2D view."""
        # Check if terrain exists and is enabled
        if not hasattr(self.editor, 'terrain') or not self.editor.terrain:
            return
        terrain = self.editor.terrain
        if not terrain.enabled:
            return
        
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return
        
        # Get view bounds
        min1 = visible_bounds.left()
        max1 = visible_bounds.right()
        min2 = visible_bounds.top()
        max2 = visible_bounds.bottom()
        
        # Set terrain drawing style
        terrain_color = QColor(139, 90, 43, 180)  # Brown with transparency
        terrain_fill = QColor(139, 90, 43, 40)
        
        if terrain.solid:
            pen = QPen(terrain_color, 2)
        else:
            pen = QPen(terrain_color, 1, Qt.DashLine)
        
        painter.setPen(pen)
        
        # Get terrain bounds
        t_bounds = terrain.get_terrain_bounds()
        t_min_x, t_max_x = t_bounds[0]
        t_min_z, t_max_z = t_bounds[1]
        
        if ax1 == 'x' and ax2 == 'z':
            # TOP VIEW - Draw terrain boundary rectangle
            p1 = self.world_to_screen(QPointF(t_min_x, t_min_z))
            p2 = self.world_to_screen(QPointF(t_max_x, t_max_z))
            rect = QRectF(p1, p2).normalized()
            
            painter.setBrush(QBrush(terrain_fill))
            painter.drawRect(rect)
            
            # Draw a grid pattern to indicate terrain
            painter.setPen(QPen(QColor(139, 90, 43, 60), 1))
            chunk_size = terrain.chunk_size
            for cx in range(terrain.min_chunk_x, terrain.max_chunk_x + 2):
                x = cx * chunk_size + terrain.offset_x
                p1 = self.world_to_screen(QPointF(x, t_min_z))
                p2 = self.world_to_screen(QPointF(x, t_max_z))
                painter.drawLine(p1, p2)
            for cz in range(terrain.min_chunk_z, terrain.max_chunk_z + 2):
                z = cz * chunk_size + terrain.offset_z
                p1 = self.world_to_screen(QPointF(t_min_x, z))
                p2 = self.world_to_screen(QPointF(t_max_x, z))
                painter.drawLine(p1, p2)
            
            # Label
            painter.setPen(QPen(terrain_color, 1))
            label_pos = self.world_to_screen(QPointF(t_min_x + 10, t_min_z + 10))
            painter.drawText(label_pos, "TERRAIN")
            
        elif ax1 == 'x' and ax2 == 'y':
            # FRONT VIEW - Draw height profile
            center_z = (t_min_z + t_max_z) / 2
            
            # Sample terrain heights
            resolution = 64
            points = []
            sample_min_x = max(min1, t_min_x)
            sample_max_x = min(max1, t_max_x)
            
            if sample_max_x > sample_min_x:
                for x in np.linspace(sample_min_x, sample_max_x, resolution):
                    h = terrain.get_height_at(x, center_z)
                    screen_pt = self.world_to_screen(QPointF(x, h))
                    points.append(screen_pt)
                
                if len(points) >= 2:
                    # Draw filled area under terrain
                    bottom_y = self.world_to_screen(QPointF(0, terrain.offset_y)).y()
                    polygon = QPolygonF()
                    polygon.append(QPointF(points[0].x(), bottom_y))
                    for pt in points:
                        polygon.append(pt)
                    polygon.append(QPointF(points[-1].x(), bottom_y))
                    
                    painter.setBrush(QBrush(terrain_fill))
                    painter.drawPolygon(polygon)
                    
                    # Draw terrain line
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    for i in range(len(points) - 1):
                        painter.drawLine(points[i], points[i + 1])
                        
        elif ax1 == 'z' and ax2 == 'y':
            # SIDE VIEW - Draw height profile
            center_x = (t_min_x + t_max_x) / 2
            
            resolution = 64
            points = []
            sample_min_z = max(min1, t_min_z)
            sample_max_z = min(max1, t_max_z)
            
            if sample_max_z > sample_min_z:
                for z in np.linspace(sample_min_z, sample_max_z, resolution):
                    h = terrain.get_height_at(center_x, z)
                    screen_pt = self.world_to_screen(QPointF(z, h))
                    points.append(screen_pt)
                
                if len(points) >= 2:
                    bottom_y = self.world_to_screen(QPointF(0, terrain.offset_y)).y()
                    polygon = QPolygonF()
                    polygon.append(QPointF(points[0].x(), bottom_y))
                    for pt in points:
                        polygon.append(pt)
                    polygon.append(QPointF(points[-1].x(), bottom_y))
                    
                    painter.setBrush(QBrush(terrain_fill))
                    painter.drawPolygon(polygon)
                    
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    for i in range(len(points) - 1):
                        painter.drawLine(points[i], points[i + 1])
    
    def draw_camera(self, painter):
        """Optimized camera drawing with early exit for off-screen cameras."""
        camera = self.editor.view_3d.camera
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return

        ax_map = {'x': 0, 'y': 1, 'z': 2}
        axis1_idx = ax_map[ax1]
        axis2_idx = ax_map[ax2]

        cam_pos_2d = QPointF(camera.pos[axis1_idx], camera.pos[axis2_idx])
        screen_pos = self.world_to_screen(cam_pos_2d)

        # Early exit if camera is far off-screen
        margin = 100
        if not self.rect().adjusted(-margin, -margin, margin, margin).contains(screen_pos.toPoint()):
            return

        # Determine view angle for this 2D projection
        yaw = camera.yaw
        pitch = camera.pitch
        
        if self.view_type == 'top':
            angle_deg = yaw
        elif self.view_type == 'front':
            angle_deg = -yaw
        elif self.view_type == 'side':
            angle_deg = -pitch

        fov = camera.fov
        cone_length = 200
        
        # Calculate view cone corners
        left_angle_rad = np.radians(angle_deg - fov / 2)
        right_angle_rad = np.radians(angle_deg + fov / 2)

        left_point = QPointF(cam_pos_2d.x() + cone_length * np.cos(left_angle_rad),
                             cam_pos_2d.y() + cone_length * np.sin(left_angle_rad))
        right_point = QPointF(cam_pos_2d.x() + cone_length * np.cos(right_angle_rad),
                              cam_pos_2d.y() + cone_length * np.sin(right_angle_rad))

        screen_left = self.world_to_screen(left_point)
        screen_right = self.world_to_screen(right_point)

        # Draw view cone (semi-transparent)
        cone_poly = QPolygonF([screen_pos, screen_left, screen_right])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 25))
        painter.drawPolygon(cone_poly)
        
        # Draw camera body
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawEllipse(screen_pos, 8, 8)

    def draw_resize_handles(self, painter, rect):
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        handle_size = 8
        handles = self.get_resize_handles(rect)
        for handle in handles:
            handle_rect = QRectF(handle.x() - handle_size/2, handle.y() - handle_size/2, handle_size, handle_size)
            painter.drawRect(handle_rect)

    def draw_trigger_connections(self, painter, visible_bounds):
        show_connections = self.main_window.config.getboolean('Display', 'show_connections', fallback=True)
        
        # Check play mode visibility
        play_mode = getattr(self.main_window.view_3d, 'play_mode', False)
        show_in_play = getattr(self.main_window.view_3d, 'show_connections_in_play_mode', False)
        
        if play_mode and not show_in_play:
            return
        
        if not show_connections: 
            return
        
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return
            
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        axis1_idx = ax_map[ax1]
        axis2_idx = ax_map[ax2]
        
        current_connections = set()
        connections_to_draw = []
        
        for i, brush in enumerate(self.editor.state.brushes):
            target_name = brush.get('target')
            if not target_name: 
                continue
            is_source = brush.get('is_trigger') or brush.get('is_mover')
            if not is_source: 
                continue
            
            # Get source position
            source_pos = brush['pos']
            source_2d = QPointF(source_pos[axis1_idx], source_pos[axis2_idx])
            
            # Find target position
            target_pos = None
            for b in self.editor.state.brushes:
                if b.get('name') == target_name:
                    target_pos = b['pos']
                    break
            if target_pos is None:
                for t in self.editor.state.things:
                    if hasattr(t, 'name') and t.name == target_name:
                        target_pos = t.pos
                        break
            
            if not target_pos:
                continue
                
            target_2d = QPointF(target_pos[axis1_idx], target_pos[axis2_idx])
            
            # CULL connection if both source and target are outside visible bounds
            margin = 10.0 / self.zoom_factor if self.zoom_factor > 0 else 10.0
            source_rect = QRectF(source_2d.x() - margin, source_2d.y() - margin, margin * 2, margin * 2)
            target_rect = QRectF(target_2d.x() - margin, target_2d.y() - margin, margin * 2, margin * 2)
            
            if not (visible_bounds.intersects(source_rect) or visible_bounds.intersects(target_rect)):
                continue
            
            # Connection is visible - add to tracking and drawing list
            source_id = f"brush_{id(brush)}"
            conn_key = (source_id, target_name)
            current_connections.add(conn_key)
            connections_to_draw.append({
                'key': conn_key,
                'source_pos': source_2d,
                'target_pos': target_2d,
                'is_trigger': brush.get('is_trigger', False)
            })
        
        # Animation tracking (only for visible connections)
        for conn_key in current_connections - self.last_connections:
            self.connection_animations[conn_key] = {'progress': 0.0, 'growing': True}
            # Initialize traveling arrows for this connection
            self.arrow_travel_progress[conn_key] = [0.0]  # Start with one arrow at 0
        for conn_key in self.last_connections - current_connections:
            if conn_key in self.connection_animations:
                self.connection_animations[conn_key]['growing'] = False
        
        self.last_connections = current_connections
        
        # Draw visible connections
        for conn in connections_to_draw:
            conn_key = conn['key']
            if conn_key not in self.connection_animations:
                self.connection_animations[conn_key] = {'progress': 1.0, 'growing': True}
                self.arrow_travel_progress[conn_key] = [0.0]
            
            anim = self.connection_animations[conn_key]
            progress = anim['progress']
            if progress <= 0: 
                continue
            
            p1 = self.world_to_screen(conn['source_pos'])
            p2 = self.world_to_screen(conn['target_pos'])
            
            animated_p2 = QPointF(p1.x() + (p2.x() - p1.x()) * progress, p1.y() + (p2.y() - p1.y()) * progress)
            color = QColor(0, 255, 255, 180) if conn['is_trigger'] else QColor(139, 69, 19, 180)
            
            pen = QPen(color, 2, Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(p1, animated_p2)
            
            # Draw traveling arrows along the line
            if progress >= 1.0 and conn_key in self.arrow_travel_progress:
                self._draw_traveling_arrows(painter, p1, p2, color, conn_key)
            elif progress > 0.1:
                self._draw_connection_arrow(painter, p1, animated_p2, color)
        
        # Clean up finished animations
        keys_to_remove = []
        for conn_key, anim in self.connection_animations.items():
            if conn_key not in current_connections and anim['progress'] <= 0:
                keys_to_remove.append(conn_key)
        for key in keys_to_remove: 
            del self.connection_animations[key]
            if key in self.arrow_travel_progress:
                del self.arrow_travel_progress[key]
    
    def _draw_traveling_arrows(self, painter, p1, p2, color, conn_key):
        """Draw arrows that travel along the connection line."""
        if conn_key not in self.arrow_travel_progress:
            return
        
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        length = math.sqrt(dx * dx + dy * dy)
        if length < 20:
            return
        
        angle = math.atan2(dy, dx)
        arrow_size = 8
        
        # Draw each traveling arrow
        for arrow_pos in self.arrow_travel_progress[conn_key]:
            # Calculate position along line
            ax = p1.x() + dx * arrow_pos
            ay = p1.y() + dy * arrow_pos
            arrow_tip = QPointF(ax, ay)
            
            # Draw arrow
            arrow_p1 = QPointF(
                ax - arrow_size * math.cos(angle - math.pi / 6),
                ay - arrow_size * math.sin(angle - math.pi / 6)
            )
            arrow_p2 = QPointF(
                ax - arrow_size * math.cos(angle + math.pi / 6),
                ay - arrow_size * math.sin(angle + math.pi / 6)
            )
            
            # Slightly brighter color for arrows
            arrow_color = QColor(color.red(), color.green(), color.blue(), 220)
            painter.setPen(QPen(arrow_color, 1))
            painter.setBrush(QBrush(arrow_color))
            painter.drawPolygon(QPolygonF([arrow_tip, arrow_p1, arrow_p2]))
    
    def _draw_connection_arrow(self, painter, p1, p2, color):
        """Draws a static arrowhead at the end of the line (p2)."""
        angle = math.atan2(p2.y() - p1.y(), p2.x() - p1.x())
        arrow_size = 10
        
        # Calculate arrow points
        p_arrow1 = QPointF(p2.x() - arrow_size * math.cos(angle - math.pi / 6),
                           p2.y() - arrow_size * math.sin(angle - math.pi / 6))
        p_arrow2 = QPointF(p2.x() - arrow_size * math.cos(angle + math.pi / 6),
                           p2.y() - arrow_size * math.sin(angle + math.pi / 6))
        
        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(color))
        painter.drawPolygon(QPolygonF([p2, p_arrow1, p_arrow2]))
    
    def _update_connection_animations(self):
        animation_speed = 0.05
        arrow_speed = 0.015  # Speed of traveling arrows
        arrow_spacing = 0.25  # Spacing between arrows (as fraction of line length)
        needs_update = False
        
        for conn_key, anim in self.connection_animations.items():
            if anim['growing']:
                if anim['progress'] < 1.0:
                    anim['progress'] = min(1.0, anim['progress'] + animation_speed)
                    needs_update = True
            else:
                if anim['progress'] > 0.0:
                    anim['progress'] = max(0.0, anim['progress'] - animation_speed)
                    needs_update = True
        
        # Update traveling arrows
        for conn_key in list(self.arrow_travel_progress.keys()):
            if conn_key not in self.connection_animations:
                del self.arrow_travel_progress[conn_key]
                continue
            
            anim = self.connection_animations.get(conn_key)
            if not anim or anim['progress'] < 1.0:
                continue
            
            arrows = self.arrow_travel_progress[conn_key]
            
            # Move all arrows forward
            new_arrows = []
            for pos in arrows:
                new_pos = pos + arrow_speed
                if new_pos < 1.0:
                    new_arrows.append(new_pos)
            
            # Add new arrow at start if there's room
            if len(new_arrows) == 0 or new_arrows[0] >= arrow_spacing:
                new_arrows.insert(0, 0.0)
            
            self.arrow_travel_progress[conn_key] = new_arrows
            needs_update = True
        
        if needs_update: 
            self.update()

    def draw_connection_drag(self, painter):
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        if isinstance(self.connection_source, dict):
            source_pos = self.connection_source['pos']
        else:
            source_pos = self.connection_source.pos

        source_2d = QPointF(source_pos[ax_map[ax1]], source_pos[ax_map[ax2]])
        
        p1 = self.world_to_screen(source_2d)
        p2 = self.world_to_screen(self.connection_drag_pos)
        
        if self.connection_snap_target:
            line_color = QColor(0, 255, 0)
            pen_width = 3
        else:
            line_color = QColor(255, 80, 80)
            pen_width = 2
        
        pen = QPen(line_color, pen_width)
        painter.setPen(pen)
        painter.drawLine(p1, p2)
        
        self._draw_connection_arrow(painter, p1, p2, line_color)
        
        painter.setBrush(QBrush(QColor(line_color.red(), line_color.green(), line_color.blue(), 100)))
        painter.drawEllipse(p1, 8, 8)
        
        if self.connection_snap_target:
            painter.setPen(QPen(QColor(0, 255, 0), 2))
            painter.setBrush(QBrush(QColor(0, 255, 0, 80)))
            painter.drawEllipse(p2, 12, 12)

    def get_resize_handles(self, rect):
        return [rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight(),
                QPointF(rect.center().x(), rect.top()), QPointF(rect.center().x(), rect.bottom()),
                QPointF(rect.left(), rect.center().y()), QPointF(rect.right(), rect.center().y())]

    def mousePressEvent(self, event):
        world_pos = self.screen_to_world(event.pos())
        middle_click_pan_enabled = self.main_window.config.getboolean('Controls', 'MiddleClickDrag', fallback=False)

        if event.button() == Qt.RightButton:
            self.is_panning = False 
            self.pan_start_pos = event.pos()
            self.last_pan_pos = event.pos() 
            return
        
        if event.button() == Qt.MiddleButton and middle_click_pan_enabled:
            self.is_panning = False
            self.pan_start_pos = event.pos()
            self.last_pan_pos = event.pos()
            return

        elif event.button() == Qt.LeftButton:
            # If we're in connection mode (started from property editor), complete on click
            if self.is_connecting:
                # Complete the connection
                target_object = self.connection_snap_target
                if not target_object:
                    target_object = self.get_object_at(event.pos())
                
                if target_object and target_object != self.connection_source:
                    self.main_window.save_state()
                    
                    # Ensure target has a name
                    target_name = self._ensure_object_name(target_object)
                    
                    # Set the trigger's target
                    self.connection_source['target'] = target_name
                    
                    # Show toast
                    self.main_window.show_toast(f"Connected to '{target_name}'")
                    
                    # Refresh property editor
                    self.main_window.property_editor.set_object(self.connection_source)
                
                # Reset connection state
                self.is_connecting = False
                self.connection_source = None
                self.connection_snap_target = None
                self.setCursor(Qt.ArrowCursor)
                self.update()
                return
            
            # Check for CTRL+click to start connection dragging
            if event.modifiers() & Qt.ControlModifier:
                clicked_object = self.get_object_at(event.pos())
                # Check if clicked object is a trigger brush
                if isinstance(clicked_object, dict) and clicked_object.get('is_trigger', False):
                    self.is_connecting = True
                    self.connection_source = clicked_object
                    self.connection_drag_pos = world_pos
                    self.setCursor(Qt.CrossCursor)
                    self.update()
                    return
            
            handle_ix = self.get_handle_at(event.pos())
            if handle_ix != -1:
                self.is_resizing_brush = True
                self.resize_handle_ix = handle_ix
                brush = self.editor.state.selected_object
                ax1, ax2 = self.get_axes()
                ax_map = {'x': 0, 'y': 1, 'z': 2}
                pos = brush['pos']
                size = brush['size']
                self.initial_brush_rect = QRectF(
                    pos[ax_map[ax1]] - size[ax_map[ax1]]/2,
                    pos[ax_map[ax2]] - size[ax_map[ax2]]/2,
                    size[ax_map[ax1]],
                    size[ax_map[ax2]]
                ).normalized()
                self.update()
                return

            clicked_object = self.get_object_at(event.pos(), highlight_locked=True)
            
            # Handle shift-click for multi-selection
            if event.modifiers() & Qt.ShiftModifier and clicked_object:
                # Get current selected_objects list
                selected_objects = getattr(self.editor.state, 'selected_objects', [])
                if not selected_objects:
                    selected_objects = []
                    if self.editor.state.selected_object:
                        selected_objects = [self.editor.state.selected_object]
                
                # Toggle selection: add if not present, remove if present
                if clicked_object in selected_objects:
                    selected_objects.remove(clicked_object)
                else:
                    selected_objects.append(clicked_object)
                
                self.editor.set_selected_objects(selected_objects)
            else:
                # Normal click - single selection
                self.editor.set_selected_object(clicked_object)

            if clicked_object and not (event.modifiers() & Qt.ShiftModifier):
                # Check if object is locked (works for both brushes and things)
                if isinstance(clicked_object, dict):
                    is_locked = clicked_object.get('lock', False)
                else:
                    is_locked = clicked_object.properties.get('lock', False)
                    
                if not is_locked:
                    self.is_dragging_object = True
                    self.drag_start_pos = world_pos
                    ax1, ax2 = self.get_axes()
                    ax_map = {'x': 0, 'y': 1, 'z': 2}
                    pos_ref = clicked_object['pos'] if isinstance(clicked_object, dict) else clicked_object.pos
                    obj_pos_2d = QPointF(pos_ref[ax_map[ax1]], pos_ref[ax_map[ax2]])
                    self.drag_offset = obj_pos_2d - world_pos
            elif not clicked_object:
                self.is_drawing_brush = True
                self.draw_start_pos = self.snap_to_grid(world_pos)
                self.draw_current_pos = self.draw_start_pos
        self.update()

    def mouseMoveEvent(self, event):
        world_pos = self.screen_to_world(event.pos())
        middle_click_pan_enabled = self.main_window.config.getboolean('Controls', 'MiddleClickDrag', fallback=False)
        
        # Handle connection mode first - works with or without button pressed
        if self.is_connecting:
            # Update connection drag line endpoint with snap detection
            snap_target, snap_screen_pos = self._find_snap_target(event.pos())
            
            if snap_target:
                # Snap to target - convert screen pos back to world
                self.connection_snap_target = snap_target
                self.connection_drag_pos = self.screen_to_world(snap_screen_pos)
            else:
                self.connection_snap_target = None
                self.connection_drag_pos = world_pos
            self.update()
            return
        
        if not event.buttons():
            handle_ix = self.get_handle_at(event.pos())
            if handle_ix != -1:
                if handle_ix in [0, 3]: self.setCursor(Qt.SizeFDiagCursor)
                elif handle_ix in [1, 2]: self.setCursor(Qt.SizeBDiagCursor)
                elif handle_ix in [4, 5]: self.setCursor(Qt.SizeVerCursor)
                elif handle_ix in [6, 7]: self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

        elif (event.buttons() & Qt.RightButton) or \
             (event.buttons() & Qt.MiddleButton and middle_click_pan_enabled):
            if not self.is_panning:
                if (event.pos() - self.pan_start_pos).manhattanLength() > 5:
                    self.is_panning = True
                    self.last_pan_pos = event.pos() 

            if self.is_panning:
                delta = event.pos() - self.last_pan_pos
                self.last_pan_pos = event.pos()
                if self.view_type in ['front', 'side']:
                    self.pan_offset -= QPointF(delta.x() / self.zoom_factor, -delta.y() / self.zoom_factor)
                else:
                    self.pan_offset -= QPointF(delta.x() / self.zoom_factor, delta.y() / self.zoom_factor)
        
        elif self.is_drawing_brush:
            self.draw_current_pos = self.snap_to_grid(world_pos)

        elif self.is_dragging_object:
            obj = self.editor.state.selected_object
            if obj:
                ax1, ax2 = self.get_axes()
                ax_map = {'x': 0, 'y': 1, 'z': 2}
                new_obj_pos = self.snap_to_grid(world_pos + self.drag_offset)
                pos_ref = obj['pos'] if isinstance(obj, dict) else obj.pos
                
                # Always store as list to maintain JSON serializability
                if isinstance(pos_ref, list):
                    pos_ref[ax_map[ax1]] = new_obj_pos.x()
                    pos_ref[ax_map[ax2]] = new_obj_pos.y()
                else:
                    # If it's a glm vector, convert to list
                    if hasattr(pos_ref, 'x'):  # It's a glm vector
                        new_pos_list = [pos_ref[0], pos_ref[1], pos_ref[2]]
                        new_pos_list[ax_map[ax1]] = new_obj_pos.x()
                        new_pos_list[ax_map[ax2]] = new_obj_pos.y()
                        obj.pos = new_pos_list  # Store as list, not glm vector
                    else:
                        # Already a list/tuple from somewhere else
                        pos_ref[ax_map[ax1]] = new_obj_pos.x()
                        pos_ref[ax_map[ax2]] = new_obj_pos.y()
                
                # THROTTLE FIX: Only update 3D view if enough time has passed (approx 60 FPS)
                current_time = time.time()
                if current_time - self.last_3d_update_time > 0.016:
                    self.main_window.view_3d.update()
                    self.last_3d_update_time = current_time
        
        elif self.is_resizing_brush:
            self.resize_brush(world_pos)
            # Apply same throttle to brush resizing for consistency
            current_time = time.time()
            if current_time - self.last_3d_update_time > 0.016:
                self.main_window.view_3d.update()
                self.last_3d_update_time = current_time
        
        self.update()

    def mouseReleaseEvent(self, event):
        action_taken = self.is_dragging_object or self.is_resizing_brush
        
        if event.button() == Qt.RightButton and not self.is_panning:
            self.contextMenuEvent(event)
        
        self.is_panning = False
        
        if event.button() == Qt.LeftButton:
            if self.is_dragging_object: self.is_dragging_object = False
            if self.is_resizing_brush: self.is_resizing_brush = False
            
            # Handle connection completion
            if self.is_connecting:
                self.is_connecting = False
                self.setCursor(Qt.ArrowCursor)
                
                target_object = self.connection_snap_target
                if not target_object:
                    target_object = self.get_object_at(event.pos())
                
                if target_object and target_object != self.connection_source:
                    self.main_window.save_state()
                    
                    # Ensure target has a name
                    target_name = self._ensure_object_name(target_object)
                    
                    # Set target on Brush OR Thing
                    if isinstance(self.connection_source, dict):
                        self.connection_source['target'] = target_name
                    else:
                        self.connection_source.properties['target'] = target_name
                    
                    self.main_window.show_toast(f"Connected to '{target_name}'")
                    
                    # Refresh property editor if needed
                    if self.editor.state.selected_object == self.connection_source:
                        self.main_window.property_editor.set_object(self.connection_source)
                    elif self.editor.state.selected_object == target_object:
                        self.main_window.property_editor.set_object(target_object)
                
                self.connection_source = None
                self.connection_snap_target = None
                self.update()
                return

            if self.is_drawing_brush:
                self.is_drawing_brush = False
                rect = QRectF(self.draw_start_pos, self.draw_current_pos).normalized()

                if rect.width() >= self.grid_size and rect.height() >= self.grid_size:
                    action_taken = True
                    self.main_window.save_state()
                    ax1, ax2 = self.get_axes()
                    ax_map = {'x': 0, 'y': 1, 'z': 2}
                    pos = [0, 0, 0]
                    size = [self.grid_size, 128, self.grid_size]
                    pos[ax_map[ax1]] = rect.center().x()
                    pos[ax_map[ax2]] = rect.center().y()
                    size[ax_map[ax1]] = rect.width()
                    size[ax_map[ax2]] = rect.height()

                    new_brush = {'pos': pos, 'size': size, 'textures': {f: 'default.png' for f in ['north','south','east','west','top','down']}}
                    self.editor.state.brushes.append(new_brush)
                    self.editor.set_selected_object(new_brush)
            
            if action_taken:
                self.main_window.save_state()
        self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        
        # --- Styling ---
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c2c;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-top: 3px solid #FF8C00; /* Orange Strip */
                padding-bottom: 2px;
            }
            QMenu::item {
                padding: 6px 28px 6px 12px;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #3e3e3e;
            }
            /* REVISED: Large margin creates the "Gap" you wanted */
            QMenu::separator {
                height: 1px;
                background: #555;
                margin: 12px 0px; 
            }
        """)

        # Check if we clicked on a brush for brush-specific options
        clicked_brush = self.get_brush_at(event.pos())
        select_inside_action = None
        
        if clicked_brush:
            select_inside_action = menu.addAction("Select Inside")
            menu.addSeparator()
        
        # Standard Things
        add_light_action = menu.addAction("Light")
        add_player_start_action = menu.addAction("Player Start")
        add_pickup_action = menu.addAction("Pickup")
        add_speaker_action = menu.addAction("Speaker")
        
        # --- The Gap (One separator is now enough due to CSS) ---
        menu.addSeparator()
        
        # Advanced / Import
        add_model_action = menu.addAction("Model...")
        
        # Logic Menu Sub-section
        menu.addSeparator()
        logic_menu = menu.addMenu("Logic Entities")
        add_logic_relay_action = logic_menu.addAction("LogicRelay")
        add_logic_timer_action = logic_menu.addAction("LogicTimer")
        add_logic_gate_action = logic_menu.addAction("LogicGate")

        action = menu.exec_(self.mapToGlobal(event.pos()))
        
        if action is None:
            return
        
        # Handle Select Inside
        if select_inside_action and action == select_inside_action:
            self.select_brushes_inside(clicked_brush)
            return
        
        # Calculate World Position for new object
        world_pos = self.snap_to_grid(self.screen_to_world(event.pos()))
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        pos_3d = [0, 40, 0]
        pos_3d[ax_map[ax1]] = world_pos.x()
        pos_3d[ax_map[ax2]] = world_pos.y()
        if self.view_type == 'top':
            pos_3d[1] = 40

        new_thing = None
        
        # Handle Object Creation
        if action == add_light_action: new_thing = Light(pos=pos_3d)
        elif action == add_player_start_action: new_thing = PlayerStart(pos=pos_3d)
        elif action == add_pickup_action: new_thing = Pickup(pos=pos_3d)
        elif action == add_speaker_action: new_thing = Speaker(pos=pos_3d)
        
        # Logic Entities
        elif action == add_logic_relay_action: new_thing = LogicRelay(pos=pos_3d)
        elif action == add_logic_timer_action: new_thing = LogicTimer(pos=pos_3d)
        elif action == add_logic_gate_action:
            new_thing = LogicGate(pos=pos_3d)
            new_thing.properties['logic_type'] = 'AND' 
        
        # Model
        elif action == add_model_action:
            filepath, _ = QFileDialog.getOpenFileName(self, "Select OBJ Model", "assets/models", "OBJ Files (*.obj)")
            if filepath:
                try:
                    rel_path = os.path.relpath(filepath, "assets")
                    if rel_path.startswith(".."): rel_path = filepath
                    else: rel_path = os.path.join("assets", rel_path)
                except:
                    rel_path = filepath
                
                new_thing = Model(pos=pos_3d)
                new_thing.properties['model_path'] = rel_path

        # Finalize Creation
        if new_thing:
            self.main_window.save_state()
            self.editor.state.things.append(new_thing)
            self.editor.set_selected_object(new_thing)
            self.update()

    def get_brush_at(self, screen_pos):
        """Returns the brush at the given screen position, or None."""
        world_pos = self.screen_to_world(screen_pos)
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        # Check if locked items should be skipped
        locked_not_selectable = self.main_window.config.getboolean('Display', 'locked_not_selectable_2d', fallback=False)
        
        for brush in reversed(self.editor.state.brushes):
            if brush.get('hidden', False):
                continue
            # Skip locked brushes if setting is enabled
            if locked_not_selectable and brush.get('lock', False):
                continue
            pos = brush['pos']
            size = brush['size']
            p1 = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
            p2 = QPointF(pos[ax_map[ax1]] + size[ax_map[ax1]]/2, pos[ax_map[ax2]] + size[ax_map[ax2]]/2)
            brush_rect = QRectF(p1, p2).normalized()
            if brush_rect.contains(world_pos):
                return brush
        return None

    def _ensure_object_name(self, obj):
        """Ensure an object has a name, auto-generating one if needed. Returns the name."""
        if isinstance(obj, dict):
            # It's a brush
            existing_name = obj.get('name', '')
            if existing_name:
                return existing_name
            
            # Generate a unique name
            base_name = 'brush'
            counter = 1
            while True:
                new_name = f"{base_name}_{counter}"
                # Check if name is unique
                name_exists = False
                for b in self.editor.state.brushes:
                    if b.get('name') == new_name:
                        name_exists = True
                        break
                if not name_exists:
                    for t in self.editor.state.things:
                        if hasattr(t, 'name') and t.name == new_name:
                            name_exists = True
                            break
                if not name_exists:
                    obj['name'] = new_name
                    return new_name
                counter += 1
        else:
            # It's a Thing
            existing_name = getattr(obj, 'name', '') or obj.properties.get('name', '')
            if existing_name:
                return existing_name
            
            # Generate a unique name based on thing type
            thing_type = type(obj).__name__.lower()
            base_name = thing_type
            counter = 1
            while True:
                new_name = f"{base_name}_{counter}"
                # Check if name is unique
                name_exists = False
                for b in self.editor.state.brushes:
                    if b.get('name') == new_name:
                        name_exists = True
                        break
                if not name_exists:
                    for t in self.editor.state.things:
                        if hasattr(t, 'name') and t.name == new_name:
                            name_exists = True
                            break
                if not name_exists:
                    obj.name = new_name
                    obj.properties['name'] = new_name
                    return new_name
                counter += 1

    def _find_snap_target(self, screen_pos):
        """Find the nearest valid target object within snap threshold.
        Returns (object, screen_position) or (None, None)."""
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        best_target = None
        best_distance = self.connection_snap_threshold
        best_screen_pos = None
        
        # Check all brushes (except the source)
        for brush in self.editor.state.brushes:
            if brush is self.connection_source:
                continue
            if brush.get('hidden', False):
                continue
            
            # Get brush center in screen coords
            pos = brush['pos']
            brush_center = QPointF(pos[ax_map[ax1]], pos[ax_map[ax2]])
            brush_screen = self.world_to_screen(brush_center)
            
            # Calculate distance
            dx = screen_pos.x() - brush_screen.x()
            dy = screen_pos.y() - brush_screen.y()
            distance = math.sqrt(dx * dx + dy * dy)
            
            if distance < best_distance:
                best_distance = distance
                best_target = brush
                best_screen_pos = brush_screen
        
        # Check all things
        for thing in self.editor.state.things:
            if thing.properties.get('hidden', False):
                continue
            
            # Get thing position in screen coords
            thing_center = QPointF(thing.pos[ax_map[ax1]], thing.pos[ax_map[ax2]])
            thing_screen = self.world_to_screen(thing_center)
            
            # Calculate distance
            dx = screen_pos.x() - thing_screen.x()
            dy = screen_pos.y() - thing_screen.y()
            distance = math.sqrt(dx * dx + dy * dy)
            
            if distance < best_distance:
                best_distance = distance
                best_target = thing
                best_screen_pos = thing_screen
        
        return best_target, best_screen_pos

    def select_brushes_inside(self, container_brush):
        """Select all brushes fully contained within container_brush, then delete it."""
        self.main_window.save_state()
        
        # Get container bounds in 3D
        c_pos = container_brush['pos']
        c_size = container_brush['size']
        c_min = [c_pos[i] - c_size[i]/2 for i in range(3)]
        c_max = [c_pos[i] + c_size[i]/2 for i in range(3)]
        
        inside_brushes = []
        
        for brush in self.editor.state.brushes:
            if brush is container_brush:
                continue
            if brush.get('hidden', False):
                continue
                
            # Get brush bounds
            b_pos = brush['pos']
            b_size = brush['size']
            b_min = [b_pos[i] - b_size[i]/2 for i in range(3)]
            b_max = [b_pos[i] + b_size[i]/2 for i in range(3)]
            
            # Check if fully contained (all corners inside container)
            fully_inside = all(
                b_min[i] >= c_min[i] and b_max[i] <= c_max[i]
                for i in range(3)
            )
            
            if fully_inside:
                inside_brushes.append(brush)
        
        # Remove the container brush
        if container_brush in self.editor.state.brushes:
            self.editor.state.brushes.remove(container_brush)
        
        # Select the inside brushes
        if inside_brushes:
            self.editor.set_selected_objects(inside_brushes)
        else:
            self.editor.set_selected_object(None)
        
        self.update()
        self.main_window.view_3d.update()

    def wheelEvent(self, event):
        modifiers = event.modifiers()
        delta = event.angleDelta().y()
        
        # Check if a Light thing is selected
        selected = self.editor.state.selected_object
        if isinstance(selected, Light):
            # SHIFT + wheel: adjust radius
            if modifiers & Qt.ShiftModifier:
                current_radius = float(selected.properties.get('radius', 512.0))
                step = 32.0  # Radius adjustment step
                if delta > 0:
                    new_radius = current_radius + step
                else:
                    new_radius = max(32.0, current_radius - step)  # Minimum radius of 32
                selected.properties['radius'] = new_radius
                # Update property editor if visible
                if hasattr(self.main_window, 'property_editor'):
                    self.main_window.property_editor.set_object(selected)
                self.update()
                self.main_window.view_3d.update()
                if hasattr(self.main_window, 'show_toast'):
                    self.main_window.show_toast(f"Light radius: {new_radius:.0f}")
                event.accept()
                return
            
            # CTRL + wheel: adjust intensity
            if modifiers & Qt.ControlModifier:
                current_intensity = float(selected.properties.get('intensity', 1.0))
                step = 0.1  # Intensity adjustment step
                if delta > 0:
                    new_intensity = min(10.0, current_intensity + step)  # Max intensity 10
                else:
                    new_intensity = max(0.1, current_intensity - step)  # Min intensity 0.1
                selected.properties['intensity'] = round(new_intensity, 2)
                # Update property editor if visible
                if hasattr(self.main_window, 'property_editor'):
                    self.main_window.property_editor.set_object(selected)
                self.update()
                self.main_window.view_3d.update()
                if hasattr(self.main_window, 'show_toast'):
                    self.main_window.show_toast(f"Light intensity: {new_intensity:.2f}")
                event.accept()
                return
        
        # Default zoom behavior
        if delta > 0: self.zoom_in()
        else: self.zoom_out()

    def get_object_at(self, screen_pos, highlight_locked=False):
        world_pos = self.screen_to_world(screen_pos)
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        candidates = []
        locked_at_pos = []  # Track locked objects at this position
        
        # Check if locked items should be skipped
        locked_not_selectable = self.main_window.config.getboolean('Display', 'locked_not_selectable_2d', fallback=False)

        for thing in reversed(self.editor.state.things):
            if thing.properties.get('hidden', False):
                continue
            
            is_hit = False
            
            # Standard Thing Hit Test
            if isinstance(thing, Model):
                # Advanced Model Hit Test: Check Bounding Box of projected vertices
                coords = self._compute_model_screen_coords(thing, ax_map, ax1, ax2)
                if coords:
                    pts_x, pts_y = coords
                    # Compute BBox
                    min_x, max_x = np.min(pts_x), np.max(pts_x)
                    min_y, max_y = np.min(pts_y), np.max(pts_y)
                    # Check contains
                    sx, sy = screen_pos.x(), screen_pos.y()
                    if sx >= min_x and sx <= max_x and sy >= min_y and sy <= max_y:
                        is_hit = True
            
            # Fallback (or non-model) hit test
            if not is_hit:
                w_pos = QPointF(thing.pos[ax_map[ax1]], thing.pos[ax_map[ax2]])
                s_pos = self.world_to_screen(w_pos)
                hit_threshold = 12 
                if abs(screen_pos.x() - s_pos.x()) <= hit_threshold and abs(screen_pos.y() - s_pos.y()) <= hit_threshold:
                    is_hit = True

            if is_hit:
                # Track locked things separately if setting is enabled
                if locked_not_selectable and thing.properties.get('lock', False):
                    locked_at_pos.append(thing)
                else:
                    candidates.append(thing)
        
        for brush in reversed(self.editor.state.brushes):
            if brush.get('hidden', False): continue
            pos = brush['pos']
            size = brush['size']
            p1 = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
            p2 = QPointF(pos[ax_map[ax1]] + size[ax_map[ax1]]/2, pos[ax_map[ax2]] + size[ax_map[ax2]]/2)
            brush_rect = QRectF(p1, p2).normalized()
            if brush_rect.contains(world_pos):
                # Track locked brushes separately if setting is enabled
                if locked_not_selectable and brush.get('lock', False):
                    locked_at_pos.append(brush)
                else:
                    candidates.append(brush)

        # If highlight_locked is True and we found locked objects, highlight them in hierarchy
        if highlight_locked and locked_at_pos and locked_not_selectable:
            # Highlight the first locked object in the hierarchy (without selecting)
            if hasattr(self.main_window, 'highlight_in_hierarchy'):
                self.main_window.highlight_in_hierarchy(locked_at_pos[0])

        if not candidates: return None
        current_selection = self.editor.state.selected_object
        if current_selection in candidates:
            idx = candidates.index(current_selection)
            next_idx = (idx + 1) % len(candidates)
            return candidates[next_idx]
        return candidates[0]

    def get_handle_at(self, screen_pos):
        brush = self.editor.state.selected_object
        if not isinstance(brush, dict) or brush.get('lock', False): return -1
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        pos, size = brush['pos'], brush['size']
        w_pos = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
        w_size = QPointF(size[ax_map[ax1]], size[ax_map[ax2]])
        p1, p2 = self.world_to_screen(w_pos), self.world_to_screen(w_pos + w_size)
        screen_rect = QRectF(p1, p2).normalized()
        handles = self.get_resize_handles(screen_rect)
        handle_size = 10
        for i, handle in enumerate(handles):
            if (screen_pos - handle).manhattanLength() < handle_size:
                return i
        return -1
        
    def resize_brush(self, world_pos):
        brush = self.editor.state.selected_object
        if not brush: return
        snapped_pos = self.snap_to_grid(world_pos)
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        ix1, ix2 = ax_map[ax1], ax_map[ax2]

        old_pos, old_size = list(brush['pos']), list(brush['size'])
        min_x, max_x = old_pos[ix1] - old_size[ix1]/2, old_pos[ix1] + old_size[ix1]/2
        min_y, max_y = old_pos[ix2] - old_size[ix2]/2, old_pos[ix2] + old_size[ix2]/2

        is_front_view = self.view_type == 'front'
        
        if self.resize_handle_ix in [0, 2, 6]: min_x = snapped_pos.x()
        if self.resize_handle_ix in [1, 3, 7]: max_x = snapped_pos.x()
        
        if is_front_view:
            if self.resize_handle_ix in [0, 1, 4]: max_y = snapped_pos.y()
            if self.resize_handle_ix in [2, 3, 5]: min_y = snapped_pos.y()
        else:
            if self.resize_handle_ix in [0, 1, 4]: min_y = snapped_pos.y()
            if self.resize_handle_ix in [2, 3, 5]: max_y = snapped_pos.y()

        if max_x < min_x: min_x, max_x = max_x, min_x
        if max_y < min_y: min_y, max_y = max_y, min_y

        if self.resize_handle_ix in [4, 5]:
            min_x, max_x = old_pos[ix1] - old_size[ix1]/2, old_pos[ix1] + old_size[ix1]/2
        if self.resize_handle_ix in [6, 7]: 
            min_y, max_y = old_pos[ix2] - old_size[ix2]/2, old_pos[ix2] + old_size[ix2]/2
        
        new_size_x = max_x - min_x
        new_size_y = max_y - min_y
        
        if new_size_x < self.grid_size: new_size_x = self.grid_size
        if new_size_y < self.grid_size: new_size_y = self.grid_size
        
        brush['pos'][ix1] = min_x + new_size_x / 2
        brush['pos'][ix2] = min_y + new_size_y / 2
        brush['size'][ix1] = new_size_x
        brush['size'][ix2] = new_size_y

    def zoom_in(self):
        self.zoom_factor *= 1.25
        self.update()

    def zoom_out(self):
        self.zoom_factor *= 0.8
        self.update()