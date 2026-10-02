import numpy as np
import math
import time
from PyQt5.QtWidgets import QWidget, QMenu
from PyQt5.QtGui import QPainter, QPen, QBrush, QColor, QFont, QPolygonF, QPixmap
from PyQt5.QtCore import Qt, QRectF, QPointF, QPoint, QTimer
from editor.things import Thing, Light, PlayerStart, Pickup, Speaker
from editor.scene_hierarchy import SceneHierarchy

class View2D(QWidget):
    def __init__(self, editor, main_window, view_type):
        super().__init__()
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
        self.last_pan_pos = QPoint()      # Last frame's mouse position (for delta)
        self.pan_start_pos = QPoint()     # Position where the mouse button was pressed (for threshold)
        
        self.draw_start_pos = QPointF()
        self.draw_current_pos = QPointF()
        self.drag_start_pos = QPointF()
        self.drag_offset = QPointF()

        self.initial_brush_rect = QRectF() 
        self.grid_size = 16
        self.world_size = 1024
        self.snap_to_grid_enabled = True
        
        # Connection line animation state
        # key = (source_id, target_name), value = {'progress': 0.0-1.0, 'growing': True/False}
        self.connection_animations = {}
        self.last_connections = set()  # Track which connections existed last frame
        
        # Animation timer
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._update_connection_animations)
        self.animation_timer.start(16)  # ~60 FPS

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        
        # DISABLE automatic context menu. We will trigger it manually in mouseReleaseEvent.
        self.setContextMenuPolicy(Qt.NoContextMenu)

        # Initialize color tag icons from SceneHierarchy
        self.color_pixmaps = {}
        for color_name, qicon in SceneHierarchy(main_window).colour_icons.items():
            self.color_pixmaps[color_name] = qicon.pixmap(18, 18) 

    def reset_state(self):
        """
        Resets the internal state of the view.
        """
        self.is_dragging_object = False
        self.is_resizing_brush = False
        self.resize_handle_ix = -1
        self.update()

    def get_axes(self):
        if self.view_type == 'top': return 'x', 'z'
        elif self.view_type == 'side': return 'y', 'z'
        elif self.view_type == 'front': return 'x', 'y'
        return None, None
        
    def world_to_screen(self, p):
        center_x, center_y = self.width() / 2, self.height() / 2
        screen_x = center_x + (p.x() - self.pan_offset.x()) * self.zoom_factor
        
        if self.view_type == 'front':
            screen_y = center_y - (p.y() - self.pan_offset.y()) * self.zoom_factor
        else:
            screen_y = center_y + (p.y() - self.pan_offset.y()) * self.zoom_factor
            
        return QPointF(screen_x, screen_y)

    def screen_to_world(self, p):
        center_x, center_y = self.width() / 2, self.height() / 2
        world_x = (p.x() - center_x) / self.zoom_factor + self.pan_offset.x()
        
        if self.view_type == 'front':
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
        
        self.draw_grid(painter)
        self.draw_brushes(painter)
        self.draw_things(painter)
        self.draw_camera(painter)
        self.draw_trigger_connections(painter)

        if self.is_drawing_brush:
            pen = QPen(QColor(255, 255, 0), 1, Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            start_screen = self.world_to_screen(self.draw_start_pos)
            current_screen = self.world_to_screen(self.draw_current_pos)
            painter.drawRect(QRectF(start_screen, current_screen).normalized())

    def draw_grid(self, painter):
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

    def draw_brushes(self, painter):
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        for brush in self.editor.state.brushes:
            if brush.get('hidden', False):
                continue
            is_selected = (brush is self.editor.state.selected_object)
            is_trigger = brush.get('is_trigger', False)
            is_subtractive = brush.get('operation') == 'subtract'
            is_locked = brush.get('lock', False)
            is_fog = brush.get('is_fog', False)
            is_mover = brush.get('is_mover', False)

            if is_locked:
                pen_color = QColor(255, 105, 97) 
                fill_color = QColor(74,4,4, 20) # 70
            else:
                pen_color = QColor(211, 211, 211)
                fill_color = QColor(200, 200, 200, 30)

            if is_trigger:
                pen_color = QColor(0, 255, 255, 150)
                fill_color = QColor(0, 255, 255, 30)
            elif is_subtractive:
                pen_color = QColor(255, 0, 0)
                fill_color = QColor(255, 0, 0, 30)
            elif is_fog:
                fog_color_rgb = brush.get('fog_color', [0.5, 0.6, 0.7])
                pen_color = QColor.fromRgbF(fog_color_rgb[0], fog_color_rgb[1], fog_color_rgb[2])
                fill_color = QColor.fromRgbF(fog_color_rgb[0], fog_color_rgb[1], fog_color_rgb[2], 0.3)
            # Mover specific color
            elif is_mover:
                pen_color = QColor(0, 120, 255)
                fill_color = QColor(0, 120, 255, 50)


            if is_selected:
                pen_color = QColor(255, 255, 0)
            
            pen = QPen(pen_color, 2 if is_selected else 1)
            if (is_trigger or is_fog) and not is_selected: pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(fill_color))

            pos = brush['pos']
            size = brush['size']
            
            w_pos = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
            w_size = QPointF(size[ax_map[ax1]], size[ax_map[ax2]])
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

            if is_fog:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, "F O G")
                
            if is_mover:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, "M O V E R")
                # Draw Mover Arrow
                self.draw_mover_arrow(painter, brush, ax1, ax2, ax_map)

            if is_selected and not is_locked:
                self.draw_resize_handles(painter, screen_rect)
            
            self.draw_brush_color_tag(painter, brush, screen_rect)

    def draw_mover_arrow(self, painter, brush, ax1, ax2, ax_map):
        direction = brush.get('direction', [0, 1, 0])
        distance = brush.get('distance', 128.0)
        
        # We want to visualize the vector of movement
        # 3D Start Point = brush center
        start_3d = brush['pos']
        
        # 3D End Point = start + (normalized_dir * distance)
        # However, direction stored might not be normalized. 
        # Typically physics engines normalize it or use it as offset.
        # Assuming it's a direction vector, let's normalize it.
        d_vec = np.array(direction, dtype=float)
        norm = np.linalg.norm(d_vec)
        if norm == 0: return # No direction
        
        d_vec = d_vec / norm * distance
        
        end_3d = [start_3d[0] + d_vec[0], start_3d[1] + d_vec[1], start_3d[2] + d_vec[2]]
        
        # Project to 2D
        p_start = QPointF(start_3d[ax_map[ax1]], start_3d[ax_map[ax2]])
        p_end = QPointF(end_3d[ax_map[ax1]], end_3d[ax_map[ax2]])
        
        # If the arrow is perpendicular to the view (e.g. moving up in Top view), p_start == p_end
        if (p_start - p_end).manhattanLength() < 2:
            return 

        s_start = self.world_to_screen(p_start)
        s_end = self.world_to_screen(p_end)
        
        # Draw Line
        arrow_color = QColor(0, 255, 0) # Green for movement
        painter.setPen(QPen(arrow_color, 2))
        painter.drawLine(s_start, s_end)
        
        # Draw Arrow Head
        angle = math.atan2(s_end.y() - s_start.y(), s_end.x() - s_start.x())
        arrow_size = 10
        
        p1 = s_end - QPointF(math.cos(angle - math.pi / 6) * arrow_size,
                             math.sin(angle - math.pi / 6) * arrow_size)
        p2 = s_end - QPointF(math.cos(angle + math.pi / 6) * arrow_size,
                             math.sin(angle + math.pi / 6) * arrow_size)
                             
        painter.setBrush(QBrush(arrow_color))
        painter.drawPolygon(QPolygonF([s_end, p1, p2]))

    def draw_brush_color_tag(self, painter, brush, screen_rect):
        if 'color' in brush and brush['color'] in self.color_pixmaps:
            pixmap = self.color_pixmaps[brush['color']]
            tag_size = 16 
            tag_x = int(screen_rect.bottomRight().x() - tag_size - 2) 
            tag_y = int(screen_rect.bottomRight().y() - tag_size - 2) 
            painter.drawPixmap(tag_x, tag_y, pixmap)

    def draw_things(self, painter):
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        for thing in self.editor.state.things:
            w_pos = QPointF(thing.pos[ax_map[ax1]], thing.pos[ax_map[ax2]])
            s_pos = self.world_to_screen(w_pos)

            if isinstance(thing, Light) and thing.properties.get('show_radius', False):
                r, g, b = thing.properties.get('colour', [255, 255, 255])
                light_color = QColor(r, g, b, 60)
                painter.setBrush(QBrush(light_color))
                painter.setPen(QPen(light_color.darker(120), 1))
                radius = thing.get_radius() * self.zoom_factor
                painter.drawEllipse(s_pos, radius, radius)
            
            if isinstance(thing, Speaker) and thing.properties.get('show_radius', False) and not thing.properties.get('global', False):
                speaker_color = QColor(100, 100, 255, 60) 
                painter.setBrush(QBrush(speaker_color))
                painter.setPen(QPen(speaker_color.darker(120), 1, Qt.DotLine))
                radius = thing.get_radius() * self.zoom_factor
                painter.drawEllipse(s_pos, radius, radius)

            pixmap = thing.get_pixmap()
            if not pixmap: continue

            pixmap_size = pixmap.size()
            draw_rect = QRectF(s_pos.x() - pixmap_size.width() / 2, 
                               s_pos.y() - pixmap_size.height() / 2,
                               pixmap_size.width(), 
                               pixmap_size.height())
            
            painter.drawPixmap(draw_rect.toRect(), pixmap)

            if thing == self.editor.state.selected_object:
                painter.setPen(QPen(QColor(255, 255, 0), 2, Qt.DotLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(draw_rect.adjusted(-2, -2, 2, 2))
    
    def draw_camera(self, painter):
        camera = self.editor.view_3d.camera
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}

        cam_pos_2d = QPointF(camera.pos[ax_map[ax1]], camera.pos[ax_map[ax2]])
        screen_pos = self.world_to_screen(cam_pos_2d)

        yaw = camera.yaw
        pitch = camera.pitch
        
        if self.view_type == 'top': angle_deg = yaw
        elif self.view_type == 'front': angle_deg = -yaw
        elif self.view_type == 'side': angle_deg = -pitch

        fov = camera.fov
        cone_length = 200
        
        left_angle_rad = np.radians(angle_deg - fov / 2)
        right_angle_rad = np.radians(angle_deg + fov / 2)

        left_point = QPointF(cam_pos_2d.x() + cone_length * np.cos(left_angle_rad),
                                 cam_pos_2d.y() + cone_length * np.sin(left_angle_rad))
        right_point = QPointF(cam_pos_2d.x() + cone_length * np.cos(right_angle_rad),
                                  cam_pos_2d.y() + cone_length * np.sin(right_angle_rad))

        screen_left = self.world_to_screen(left_point)
        screen_right = self.world_to_screen(right_point)

        cone_poly = QPolygonF([screen_pos, screen_left, screen_right])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 25))
        painter.drawPolygon(cone_poly)
        
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawEllipse(screen_pos, 8, 8)
        
        forward_angle_rad = np.radians(angle_deg)
        forward_point = QPointF(cam_pos_2d.x() + 20 * np.cos(forward_angle_rad),
                                      cam_pos_2d.y() + 20 * np.sin(forward_angle_rad))
        screen_forward = self.world_to_screen(forward_point)
        painter.drawLine(screen_pos, screen_forward)

    def draw_resize_handles(self, painter, rect):
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        
        handle_size = 8
        handles = self.get_resize_handles(rect)
        for handle in handles:
            handle_rect = QRectF(handle.x() - handle_size/2, handle.y() - handle_size/2, handle_size, handle_size)
            painter.drawRect(handle_rect)

    def draw_trigger_connections(self, painter):
        """Draw animated connection lines between triggers/movers and their targets."""
        # Check if connections should be shown
        show_connections = self.main_window.config.getboolean('Display', 'show_connections', fallback=True)
        if not show_connections:
            return
            
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        current_connections = set()
        
        # Gather all connections (triggers and movers)
        connections_to_draw = []
        
        for i, brush in enumerate(self.editor.state.brushes):
            target_name = brush.get('target')
            if not target_name:
                continue
                
            # Check if this is a trigger or has a name (could be targeted)
            is_source = brush.get('is_trigger') or brush.get('is_mover')
            if not is_source:
                continue
                
            source_id = f"brush_{id(brush)}"
            conn_key = (source_id, target_name)
            current_connections.add(conn_key)
            
            # Find target (could be brush or thing)
            target_pos = None
            
            # Check brushes for target
            for b in self.editor.state.brushes:
                if b.get('name') == target_name:
                    target_pos = b['pos']
                    break
            
            # Check things for target
            if target_pos is None:
                for t in self.editor.state.things:
                    if hasattr(t, 'name') and t.name == target_name:
                        target_pos = t.pos
                        break
            
            if target_pos:
                connections_to_draw.append({
                    'key': conn_key,
                    'source_pos': brush['pos'],
                    'target_pos': target_pos,
                    'is_trigger': brush.get('is_trigger', False)
                })
        
        # Handle new connections (start growing)
        for conn_key in current_connections - self.last_connections:
            self.connection_animations[conn_key] = {'progress': 0.0, 'growing': True}
        
        # Handle removed connections (start ungrowing)
        for conn_key in self.last_connections - current_connections:
            if conn_key in self.connection_animations:
                self.connection_animations[conn_key]['growing'] = False
        
        self.last_connections = current_connections
        
        # Draw connections with animation
        for conn in connections_to_draw:
            conn_key = conn['key']
            
            # Get or create animation state
            if conn_key not in self.connection_animations:
                self.connection_animations[conn_key] = {'progress': 1.0, 'growing': True}
            
            anim = self.connection_animations[conn_key]
            progress = anim['progress']
            
            if progress <= 0:
                continue
            
            source_pos = conn['source_pos']
            target_pos = conn['target_pos']
            
            source_2d = QPointF(source_pos[ax_map[ax1]], source_pos[ax_map[ax2]])
            target_2d = QPointF(target_pos[ax_map[ax1]], target_pos[ax_map[ax2]])
            
            p1 = self.world_to_screen(source_2d)
            p2 = self.world_to_screen(target_2d)
            
            # Interpolate end point based on progress
            animated_p2 = QPointF(
                p1.x() + (p2.x() - p1.x()) * progress,
                p1.y() + (p2.y() - p1.y()) * progress
            )
            
            # Color based on type
            if conn['is_trigger']:
                color = QColor(0, 255, 255, 180)  # Cyan for triggers
            else:
                color = QColor(139, 69, 19, 180)  # Brown for movers
            
            pen = QPen(color, 2, Qt.DotLine)
            painter.setPen(pen)
            painter.drawLine(p1, animated_p2)
            
            # Draw arrowhead at the animated end
            if progress > 0.1:
                self._draw_connection_arrow(painter, p1, animated_p2, color)
        
        # Also draw ungrowing connections that are no longer in current set
        keys_to_remove = []
        for conn_key, anim in self.connection_animations.items():
            if conn_key not in current_connections:
                if anim['progress'] <= 0:
                    keys_to_remove.append(conn_key)
        
        for key in keys_to_remove:
            del self.connection_animations[key]
    
    def _draw_connection_arrow(self, painter, p1, p2, color):
        """Draw an arrowhead at p2 pointing from p1."""
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        length = math.sqrt(dx * dx + dy * dy)
        if length < 10:
            return
            
        angle = math.atan2(dy, dx)
        arrow_size = 8
        
        arrow_p1 = QPointF(
            p2.x() - arrow_size * math.cos(angle - math.pi / 6),
            p2.y() - arrow_size * math.sin(angle - math.pi / 6)
        )
        arrow_p2 = QPointF(
            p2.x() - arrow_size * math.cos(angle + math.pi / 6),
            p2.y() - arrow_size * math.sin(angle + math.pi / 6)
        )
        
        painter.setBrush(QBrush(color))
        painter.drawPolygon(QPolygonF([p2, arrow_p1, arrow_p2]))
    
    def _update_connection_animations(self):
        """Update animation progress for all connections."""
        animation_speed = 0.05  # Progress per frame
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
        
        if needs_update:
            self.update()

    def get_resize_handles(self, rect):
        return [
            rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight(),
            QPointF(rect.center().x(), rect.top()), QPointF(rect.center().x(), rect.bottom()),
            QPointF(rect.left(), rect.center().y()), QPointF(rect.right(), rect.center().y())
        ]

    def mousePressEvent(self, event):
        world_pos = self.screen_to_world(event.pos())
        middle_click_pan_enabled = self.main_window.config.getboolean('Controls', 'MiddleClickDrag', fallback=False)

        if event.button() == Qt.RightButton:
            # Init dragging state. 
            # pan_start_pos is the anchor to check if we moved far enough.
            # last_pan_pos is initialized but not used until drag starts.
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

            # CHANGE: Pass event.pos() (Screen Coordinates) to get_object_at
            clicked_object = self.get_object_at(event.pos())
            self.editor.set_selected_object(clicked_object)

            if clicked_object:
                is_locked = isinstance(clicked_object, dict) and clicked_object.get('lock', False)
                if not is_locked:
                    self.is_dragging_object = True
                    self.drag_start_pos = world_pos
                    ax1, ax2 = self.get_axes()
                    ax_map = {'x': 0, 'y': 1, 'z': 2}
                    pos_ref = clicked_object['pos'] if isinstance(clicked_object, dict) else clicked_object.pos
                    obj_pos_2d = QPointF(pos_ref[ax_map[ax1]], pos_ref[ax_map[ax2]])
                    self.drag_offset = obj_pos_2d - world_pos
            else:
                self.is_drawing_brush = True
                self.draw_start_pos = self.snap_to_grid(world_pos)
                self.draw_current_pos = self.draw_start_pos
        
        self.update()

    def mouseMoveEvent(self, event):
        world_pos = self.screen_to_world(event.pos())
        middle_click_pan_enabled = self.main_window.config.getboolean('Controls', 'MiddleClickDrag', fallback=False)
        
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
            
            # If not yet panning, check if we crossed the threshold from the START position
            if not self.is_panning:
                if (event.pos() - self.pan_start_pos).manhattanLength() > 5:
                    self.is_panning = True
                    # Reset last_pan_pos to current so the delta is smooth from this point
                    self.last_pan_pos = event.pos() 

            # If we ARE panning (either just started or continuing)
            if self.is_panning:
                delta = event.pos() - self.last_pan_pos
                self.last_pan_pos = event.pos()
                
                if self.view_type == 'front':
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
                pos_ref[ax_map[ax1]] = new_obj_pos.x()
                pos_ref[ax_map[ax2]] = new_obj_pos.y()
                # Update 3D view in real-time during drag
                self.main_window.view_3d.update()
        
        elif self.is_resizing_brush:
            obj = self.editor.state.selected_object
            if obj:
                self.resize_brush(world_pos)
                # Update 3D view in real-time during resize
                self.main_window.view_3d.update()
        
        self.update()

    def mouseReleaseEvent(self, event):
        action_taken = self.is_dragging_object or self.is_resizing_brush
        
        # We manually trigger context menu ONLY if we did not pan.
        # Qt.NoContextMenu policy prevents system double-fire.
        if event.button() == Qt.RightButton and not self.is_panning:
            self.contextMenuEvent(event)
        
        self.is_panning = False
        
        if event.button() == Qt.LeftButton:
            if self.is_dragging_object: self.is_dragging_object = False
            if self.is_resizing_brush: self.is_resizing_brush = False

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
        
        add_light_action = menu.addAction("Add Light")
        add_player_start_action = menu.addAction("Add Player Start")
        add_pickup_action = menu.addAction("Add Pickup")
        add_speaker_action = menu.addAction("Add Speaker")

        action = menu.exec_(self.mapToGlobal(event.pos()))
        
        world_pos = self.snap_to_grid(self.screen_to_world(event.pos()))
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        pos_3d = [0, 40, 0]
        pos_3d[ax_map[ax1]] = world_pos.x()
        pos_3d[ax_map[ax2]] = world_pos.y()
        if self.view_type == 'top':
            pos_3d[1] = 40

        new_thing = None
        if action == add_light_action: new_thing = Light(pos=pos_3d)
        elif action == add_player_start_action: new_thing = PlayerStart(pos=pos_3d)
        elif action == add_pickup_action: new_thing = Pickup(pos=pos_3d)
        elif action == add_speaker_action: new_thing = Speaker(pos=pos_3d)

        if new_thing:
            self.main_window.save_state()
            self.editor.state.things.append(new_thing)
            self.editor.set_selected_object(new_thing)
            self.update()

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0: self.zoom_in()
        else: self.zoom_out()

    def get_object_at(self, screen_pos):
        # We calculate world_pos internally for checking brushes
        world_pos = self.screen_to_world(screen_pos)
        
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        candidates = []

        # 1. Collect all valid 'Thing' candidates under the cursor
        for thing in reversed(self.editor.state.things):
            # Get the Thing's center in World Space
            w_pos = QPointF(thing.pos[ax_map[ax1]], thing.pos[ax_map[ax2]])
            # Convert center to Screen Space
            s_pos = self.world_to_screen(w_pos)
            
            # Check a fixed pixel area around the center (e.g., 24x24 pixels)
            # This ensures they are clickable even when zoomed out.
            hit_threshold = 12 # Half-size of the hit box in pixels
            if abs(screen_pos.x() - s_pos.x()) <= hit_threshold and \
               abs(screen_pos.y() - s_pos.y()) <= hit_threshold:
                candidates.append(thing)
        
        # 2. Collect all valid 'Brush' candidates under the cursor
        # Brushes are geometry, so we check them in World Space as before.
        for brush in reversed(self.editor.state.brushes):
            if brush.get('hidden', False):
                continue # Skip hidden brushes so they don't block selection

            pos = brush['pos']
            size = brush['size']
            p1 = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
            p2 = QPointF(pos[ax_map[ax1]] + size[ax_map[ax1]]/2, pos[ax_map[ax2]] + size[ax_map[ax2]]/2)
            brush_rect = QRectF(p1, p2).normalized()
            if brush_rect.contains(world_pos):
                candidates.append(brush)

        if not candidates:
            return None

        # 3. Handle Cycling: If the currently selected object is in the list of candidates,
        # return the NEXT one in the list. Otherwise, return the first one.
        # Since 'Things' are added to candidates first, they naturally take priority 
        # on the initial click.
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
        
        if self.resize_handle_ix in [0, 2, 6]:
            min_x = snapped_pos.x()
        if self.resize_handle_ix in [1, 3, 7]:
            max_x = snapped_pos.x()
        
        if is_front_view:
            if self.resize_handle_ix in [0, 1, 4]: 
                max_y = snapped_pos.y()
            if self.resize_handle_ix in [2, 3, 5]: 
                min_y = snapped_pos.y()
        else:
            if self.resize_handle_ix in [0, 1, 4]: 
                min_y = snapped_pos.y()
            if self.resize_handle_ix in [2, 3, 5]: 
                max_y = snapped_pos.y()

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