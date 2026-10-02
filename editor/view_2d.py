import numpy as np
import math
import time
import os
from PyQt5.QtWidgets import QWidget, QMenu, QFileDialog
from PyQt5.QtGui import QPainter, QPen, QBrush, QColor, QFont, QPolygonF, QPixmap
from PyQt5.QtCore import Qt, QRectF, QPointF, QPoint, QTimer
from editor.things import (Thing, Light, PlayerStart, Pickup, Speaker, Model, Monster,
                          LogicGate, LogicRelay, LogicTimer, LogicCommand, LevelChanger, PathNode,
                          LogicCamera, LogicSpawner, Portal, LogicKeyValueStore)
from editor.scene_hierarchy import SceneHierarchy
from engine import brush_geometry as bg  # convex/angled-brush geometry
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
        # Objects moved together during a group drag, plus the "grab" object
        # whose point stays under the cursor.
        self.drag_group = []
        self.drag_primary = None

        # --- Select tool: rubber-band marquee state ---
        # marquee_start/current are world-space (axis1, axis2) points; marquee
        # _hits is the live set of objects the box currently encloses/touches so
        # they can be highlighted before the selection is committed on release.
        self.is_marquee_select = False
        self.marquee_start = QPointF()
        self.marquee_current = QPointF()
        self.marquee_hits = []
        self._maybe_toggle_manip = False

        # --- Tier-2 group manipulation (multi-selection bounding box) ---
        # manip_mode flips between 'resize' (scale handles) and 'rotate' (spin
        # handles) each time the user clicks inside an existing selection.
        self.manip_mode = 'resize'
        self.is_group_resizing = False
        self.group_resize_handle = -1
        self._group_start = None      # snapshot of bounds + per-object state
        self.is_group_rotating = False
        self.group_rotate_pivot = None
        self.group_rotate_start_ang = 0.0
        self.group_rotate_applied = 0.0

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

        # ---- Arrow-key nudge undo coalescing ----
        # A rapid burst of nudges (tapping or holding an arrow key) should be a
        # single, cheap undo step rather than one full-scene save_state() per
        # keypress.  We save state once when a burst starts, then let this idle
        # timer end the burst after a short pause so the next nudge starts fresh.
        self._nudge_in_progress = False
        self._nudge_idle_timer = QTimer(self)
        self._nudge_idle_timer.setSingleShot(True)
        self._nudge_idle_timer.setInterval(500)  # ms of inactivity ends a burst
        self._nudge_idle_timer.timeout.connect(self._end_nudge_burst)

        # ---- Camera tracking for smooth viewcone updates ----
        # Watches camera position/yaw/pitch and repaints 2D views only when changed
        self._camera_tracking_timer = QTimer(self)
        self._camera_tracking_timer.setInterval(16)  # ~60 FPS check
        self._camera_tracking_timer.timeout.connect(self._check_camera_changed)
        self._camera_tracking_timer.start()
        # Cached camera state for change detection
        self._last_camera_pos = None
        self._last_camera_yaw = None
        self._last_camera_pitch = None

        # CTRL+drag connection state
        self.is_connecting = False
        self.connection_source = None  # The trigger brush being connected
        self.connection_drag_pos = QPointF()  # Current mouse position during drag
        self.connection_snap_target = None  # Target object we're snapping to
        self.connection_snap_threshold = 30  # Pixels to snap within

        # --- Clip / slice tool state (Radiant-style, toggled globally with X) ---
        # clip_points holds up to two world-space (axis1, axis2) points defining
        # the cut line in THIS view; the plane is that line extruded along the
        # view's depth axis.  clip_hover tracks the cursor so the kept side can
        # be previewed live and frozen on Enter.
        self.clip_points = []          # list[QPointF] in this view's 2D world coords
        self.clip_hover = None         # QPointF current cursor in 2D world coords
        self.clip_keep_positive = False  # which half-space to keep

        # --- Free-rotate tool state (toggled globally by the rotate button) ---
        # A left-drag spins the selection about this view's depth axis.  We keep
        # the on-screen pivot, the cursor angle where the drag began, and the
        # net snapped angle already applied so each mouse move only rotates by
        # the delta (rotations compose exactly about a fixed pivot/axis).
        self.rotate_dragging = False
        self.rotate_pivot = None       # QPointF pivot in this view's 2D world coords
        self.rotate_start_ang = 0.0    # cursor angle (radians) at drag start
        self.rotate_applied = 0.0      # net snapped degrees applied so far
        self.rotate_snap_deg = 15.0    # step size while grid snap is enabled

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setContextMenuPolicy(Qt.NoContextMenu)

        # Initialize color tag icons - load directly from assets (don't depend on scene_hierarchy)
        self.color_pixmaps = {}
        colour_icon_map = {
            'red': "assets/circ_red.png",
            'orange': "assets/circ_orange.png",
            'yellow': "assets/circ_yellow.png",
            'green': "assets/circ_green.png",
            'blue': "assets/circ_blue.png",
            'pink': "assets/circ_pink.png",
            'white': "assets/circ_white.png",
        }
        for color_name, path in colour_icon_map.items():
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.color_pixmaps[color_name] = pixmap.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation) 

    def reset_state(self):
        self.is_dragging_object = False
        self.is_resizing_brush = False
        self.resize_handle_ix = -1
        self.is_connecting = False
        self.connection_source = None
        self.connection_snap_target = None
        self.clip_points = []
        self.clip_hover = None
        self.rotate_dragging = False
        self.rotate_pivot = None
        # Select-tool / group-manipulation transient state.
        self.is_marquee_select = False
        self.marquee_hits = []
        self.is_group_resizing = False
        self.group_resize_handle = -1
        self._group_start = None
        self.is_group_rotating = False
        self.update()

    # ======================================================================
    # Clip / slice tool
    # ======================================================================

    def _clip_active(self):
        """True when the global clip mode is on (toggled by the X button)."""
        return bool(getattr(self.main_window, 'clip_mode', False))

    def _axis_indices(self):
        """(axis1_idx, axis2_idx, depth_idx) for this ortho view."""
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return None
        a1, a2 = ax_map[ax1], ax_map[ax2]
        depth = ({0, 1, 2} - {a1, a2}).pop()
        return a1, a2, depth

    def clear_clip(self):
        """Discard the in-progress cut (points/preview) but stay in clip mode."""
        self.clip_points = []
        self.clip_hover = None
        self.update()

    # ------------------------------------------------------------------
    # Free-rotate tool
    # ------------------------------------------------------------------
    def _rotate_active(self):
        """True when the global free-rotate mode is on (rotate toolbar button)."""
        return bool(getattr(self.main_window, 'rotate_mode', False))

    def _rotate_axis_vec(self):
        """3D rotation axis for this view: e_axis1 x e_axis2, so a positive drag
        angle (measured with atan2 in the view's 2D world frame) spins the
        geometry the same way in that frame."""
        idx = self._axis_indices()
        if idx is None:
            return None
        a1, a2, _ = idx
        e1 = [0.0, 0.0, 0.0]; e1[a1] = 1.0
        e2 = [0.0, 0.0, 0.0]; e2[a2] = 1.0
        return [e1[1]*e2[2] - e1[2]*e2[1],
                e1[2]*e2[0] - e1[0]*e2[2],
                e1[0]*e2[1] - e1[1]*e2[0]]

    def _selected_brush(self):
        obj = self.editor.state.selected_object
        return obj if isinstance(obj, dict) else None

    # ------------------------------------------------------------------
    # Base tool (Select / Brush) + marquee helpers
    # ------------------------------------------------------------------
    def _select_tool_active(self):
        """True when the Select (marquee) base tool is active and no special
        drag tool (clip/rotate) is overriding it."""
        if self._clip_active() or self._rotate_active():
            return False
        return getattr(self.main_window, 'tool_mode', 'select') == 'select'

    def _brush_tool_active(self):
        """True when the Brush/Block (draw geometry) base tool is active."""
        if self._clip_active() or self._rotate_active():
            return False
        return getattr(self.main_window, 'tool_mode', 'select') == 'brush'

    def reset_marquee(self):
        """Cancel any in-progress rubber-band selection."""
        self.is_marquee_select = False
        self.marquee_hits = []
        self.update()

    def _selected_list(self):
        """Current multi-selection as a plain list (never None)."""
        objs = list(getattr(self.editor.state, 'selected_objects', []) or [])
        sel = self.editor.state.selected_object
        if sel is not None and sel not in objs:
            objs.append(sel)
        return objs

    def _obj_bounds_2d(self, obj, i1, i2):
        """(min1, min2, max1, max2) footprint of a brush or point of a thing in
        the current view plane."""
        if isinstance(obj, dict):
            pos, size = obj['pos'], obj['size']
            return (pos[i1] - size[i1] / 2, pos[i2] - size[i2] / 2,
                    pos[i1] + size[i1] / 2, pos[i2] + size[i2] / 2)
        p = obj.pos
        return (p[i1], p[i2], p[i1], p[i2])

    def _objects_in_rect(self, world_rect, enclose=False):
        """Objects whose footprint intersects (default) or is fully enclosed by
        ``world_rect`` in the current view plane.  Skips hidden/locked-out ones."""
        idx = self._axis_indices()
        if idx is None:
            return []
        i1, i2, _ = idx
        r = world_rect.normalized()
        locked_out = self.main_window.config.getboolean(
            'Display', 'locked_not_selectable_2d', fallback=False)
        hits = []

        for brush in self.editor.state.brushes:
            if brush.get('hidden', False):
                continue
            if locked_out and brush.get('lock', False):
                continue
            b1, b2, B1, B2 = self._obj_bounds_2d(brush, i1, i2)
            brect = QRectF(QPointF(b1, b2), QPointF(B1, B2)).normalized()
            inside = (r.left() <= brect.left() and r.right() >= brect.right() and
                      r.top() <= brect.top() and r.bottom() >= brect.bottom()) \
                if enclose else r.intersects(brect)
            if inside:
                hits.append(brush)

        for thing in self.editor.state.things:
            if thing.properties.get('hidden', False):
                continue
            if locked_out and thing.properties.get('lock', False):
                continue
            p = thing.pos
            if r.contains(QPointF(p[i1], p[i2])):
                hits.append(thing)
        return hits

    # ------------------------------------------------------------------
    # Tier-2 group bounding box + scale / rotate manipulation
    # ------------------------------------------------------------------
    def _selection_bounds_2d(self):
        """Combined world-space AABB (QRectF) of the whole selection in the view
        plane, or None if nothing is selected."""
        idx = self._axis_indices()
        if idx is None:
            return None
        i1, i2, _ = idx
        objs = self._selected_list()
        if not objs:
            return None
        mn1 = mn2 = float('inf')
        mx1 = mx2 = float('-inf')
        for o in objs:
            b1, b2, B1, B2 = self._obj_bounds_2d(o, i1, i2)
            mn1, mn2 = min(mn1, b1), min(mn2, b2)
            mx1, mx2 = max(mx1, B1), max(mx2, B2)
        return QRectF(QPointF(mn1, mn2), QPointF(mx1, mx2)).normalized()

    def _group_manip_active(self):
        """The unified group box (scale/rotate handles) is shown when 2+ objects
        are selected.  A lone brush keeps its own resize handles; a lone point
        entity has nothing to scale."""
        return len(self._selected_list()) >= 2

    def _group_handle_at(self, screen_pos):
        """Index (0-7) of the group bbox handle under the cursor, or -1."""
        if not self._group_manip_active():
            return -1
        wb = self._selection_bounds_2d()
        if wb is None:
            return -1
        p1 = self.world_to_screen(wb.topLeft())
        p2 = self.world_to_screen(wb.bottomRight())
        srect = QRectF(p1, p2).normalized()
        for i, h in enumerate(self.get_resize_handles(srect)):
            if (screen_pos - h).manhattanLength() < 10:
                return i
        return -1

    def _begin_group_resize(self, handle_ix):
        """Snapshot the selection so a handle drag can scale it about the
        opposite edge/corner."""
        idx = self._axis_indices()
        wb = self._selection_bounds_2d()
        if idx is None or wb is None:
            return
        i1, i2, _ = idx
        self.main_window.save_state()
        objs = self._selected_list()
        snap = []
        for o in objs:
            if isinstance(o, dict):
                snap.append((o, list(o['pos']), list(o['size'])))
            else:
                snap.append((o, list(o.pos), None))
        self._group_start = {
            'rect': QRectF(wb),
            'objs': snap,
            'i1': i1, 'i2': i2,
        }
        self.is_group_resizing = True
        self.group_resize_handle = handle_ix

    def _update_group_resize(self, world_pos):
        """Scale every selected object about the anchor edge/corner so the group
        bbox tracks the dragged handle (snapped to grid)."""
        gs = self._group_start
        if not gs:
            return
        i1, i2 = gs['i1'], gs['i2']
        rect0 = gs['rect']
        snapped = self.snap_to_grid(world_pos)
        h = self.group_resize_handle

        # Which edges this handle moves.  Handles: 0 TL,1 TR,2 BL,3 BR,
        # 4 topMid,5 botMid,6 leftMid,7 rightMid.
        moves_left = h in (0, 2, 6)
        moves_right = h in (1, 3, 7)
        moves_top = h in (0, 1, 4)
        moves_bottom = h in (2, 3, 5)

        left, right = rect0.left(), rect0.right()
        top, bottom = rect0.top(), rect0.bottom()
        if moves_left:
            left = min(snapped.x(), right - self.grid_size)
        if moves_right:
            right = max(snapped.x(), left + self.grid_size)
        if moves_top:
            top = min(snapped.y(), bottom - self.grid_size)
        if moves_bottom:
            bottom = max(snapped.y(), top + self.grid_size)

        ow, oh = rect0.width(), rect0.height()
        nw, nh = (right - left), (bottom - top)
        sx = nw / ow if ow > 1e-6 else 1.0
        sy = nh / oh if oh > 1e-6 else 1.0
        ox, oy = rect0.left(), rect0.top()   # map old->new: n = new_o + (p-old_o)*s

        for entry in gs['objs']:
            o, pos0, size0 = entry
            if isinstance(o, dict):
                c1 = pos0[i1]                 # brush centre on the two view axes
                c2 = pos0[i2]
                n1 = left + (c1 - ox) * sx
                n2 = top + (c2 - oy) * sy
                ns1 = max(self.grid_size, size0[i1] * sx)
                ns2 = max(self.grid_size, size0[i2] * sy)
                if bg.brush_has_geometry(o):
                    lo = [pos0[k] - size0[k] / 2 for k in range(3)]
                    hi = [pos0[k] + size0[k] / 2 for k in range(3)]
                    lo[i1], hi[i1] = n1 - ns1 / 2, n1 + ns1 / 2
                    lo[i2], hi[i2] = n2 - ns2 / 2, n2 + ns2 / 2
                    bg.fit_brush_to_bounds(o, lo, hi)
                else:
                    o['pos'][i1] = n1
                    o['pos'][i2] = n2
                    o['size'][i1] = ns1
                    o['size'][i2] = ns2
            else:
                # A point entity: scale its position about the anchor, keep size.
                p1 = pos0[i1]
                p2 = pos0[i2]
                newp = list(o.pos)
                newp[i1] = left + (p1 - ox) * sx
                newp[i2] = top + (p2 - oy) * sy
                o.pos = newp

    def _end_group_resize(self):
        self.is_group_resizing = False
        self.group_resize_handle = -1
        self._group_start = None

    def _begin_group_rotate(self, world_pos):
        """Start a rotate-mode drag spinning the whole selection about the group
        centre."""
        wb = self._selection_bounds_2d()
        if wb is None:
            return False
        self.group_rotate_pivot = wb.center()
        self.group_rotate_start_ang = math.atan2(
            world_pos.y() - self.group_rotate_pivot.y(),
            world_pos.x() - self.group_rotate_pivot.x())
        self.group_rotate_applied = 0.0
        self.is_group_rotating = True
        self.main_window.save_state()
        return True

    def _update_group_rotate(self, world_pos):
        """Rotate the selection (brush geometry + every object's centre) about
        the group pivot to follow the cursor."""
        if not self.is_group_rotating or self.group_rotate_pivot is None:
            return
        axis = self._rotate_axis_vec()
        idx = self._axis_indices()
        if axis is None or idx is None:
            return
        i1, i2, _ = idx
        piv = self.group_rotate_pivot
        ang = math.atan2(world_pos.y() - piv.y(), world_pos.x() - piv.x())
        total = math.degrees(ang - self.group_rotate_start_ang)
        if self.snap_to_grid_enabled and self.rotate_snap_deg > 0:
            total = round(total / self.rotate_snap_deg) * self.rotate_snap_deg
        delta = total - self.group_rotate_applied
        if abs(delta) < 1e-6:
            return
        pivot3 = [0.0, 0.0, 0.0]
        pivot3[i1] = piv.x()
        pivot3[i2] = piv.y()
        # Depth of the pivot is irrelevant for a rotation about the view axis.
        rad = math.radians(delta)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        for o in self._selected_list():
            if isinstance(o, dict):
                bg.rotate_brush(o, delta, axis, pivot=pivot3)
            else:
                p = list(o.pos)
                dx, dy = p[i1] - piv.x(), p[i2] - piv.y()
                p[i1] = piv.x() + dx * cos_a - dy * sin_a
                p[i2] = piv.y() + dx * sin_a + dy * cos_a
                o.pos = p
        self.group_rotate_applied = total
        self.editor.update_views()
        self.main_window.view_3d.update()

    def _end_group_rotate(self):
        applied = self.group_rotate_applied
        self.is_group_rotating = False
        if abs(applied) < 1e-6:
            if getattr(self.editor.state, 'undo_stack', None):
                self.editor.state.undo_stack.pop()
        else:
            self.main_window.unsaved_changes = True
            self.main_window.state.mark_lighting_dirty()
            self.main_window.show_toast(f"Rotated group {applied:+.0f}°")

    def begin_rotate(self, world_pos):
        """Start a free-rotate drag around the selected brush's centre."""
        brush = self._selected_brush()
        idx = self._axis_indices()
        if brush is None or idx is None:
            self.main_window.show_toast("Rotate: select a brush first", is_error=True)
            return False
        a1, a2, _ = idx
        pos = brush.get('pos', [0, 0, 0])
        self.rotate_pivot = QPointF(float(pos[a1]), float(pos[a2]))
        self.rotate_start_ang = math.atan2(world_pos.y() - self.rotate_pivot.y(),
                                           world_pos.x() - self.rotate_pivot.x())
        self.rotate_applied = 0.0
        self.rotate_dragging = True
        self.main_window.save_state()  # single undo checkpoint for the whole drag
        self.update()
        return True

    def update_rotate(self, world_pos):
        """Apply the incremental rotation to reach the cursor's current angle."""
        if not self.rotate_dragging or self.rotate_pivot is None:
            return
        axis = self._rotate_axis_vec()
        if axis is None:
            return
        ang = math.atan2(world_pos.y() - self.rotate_pivot.y(),
                         world_pos.x() - self.rotate_pivot.x())
        total_deg = math.degrees(ang - self.rotate_start_ang)
        if self.snap_to_grid_enabled and self.rotate_snap_deg > 0:
            total_deg = round(total_deg / self.rotate_snap_deg) * self.rotate_snap_deg
        delta = total_deg - self.rotate_applied
        if abs(delta) < 1e-6:
            return
        if self.main_window.apply_rotation_to_selection(delta, axis, undoable=False):
            self.rotate_applied = total_deg
            self.editor.update_views()
            sel = self._selected_brush()
            if sel is not None and hasattr(self.main_window, 'property_editor'):
                self.main_window.property_editor.set_object(sel)

    def commit_rotate(self):
        """Finish the drag, keeping the applied rotation (undo already staged)."""
        if not self.rotate_dragging:
            return
        applied = self.rotate_applied
        self.rotate_dragging = False
        if abs(applied) < 1e-6:
            # Nothing actually rotated — drop the checkpoint we pushed.
            if getattr(self.editor.state, 'undo_stack', None):
                self.editor.state.undo_stack.pop()
        else:
            self.main_window.unsaved_changes = True
            self.main_window.state.mark_lighting_dirty()
            self.main_window.show_toast(f"Rotated {applied:.0f}°")
        self.update()

    def cancel_rotate(self):
        """Abort an in-progress drag, restoring the pre-drag orientation."""
        if not self.rotate_dragging:
            self.rotate_pivot = None
            return
        axis = self._rotate_axis_vec()
        if axis is not None and abs(self.rotate_applied) > 1e-6:
            self.main_window.apply_rotation_to_selection(-self.rotate_applied, axis,
                                                         undoable=False)
        if getattr(self.editor.state, 'undo_stack', None):
            self.editor.state.undo_stack.pop()
        self.rotate_dragging = False
        self.rotate_pivot = None
        self.rotate_applied = 0.0
        self.editor.update_views()

    def _clip_plane(self):
        """Build the 3D cut plane from the two placed points.

        Returns ``(normal_list, offset, keep_positive)`` where a point ``p`` is
        kept when ``dot(normal, p) <= offset`` (unless keep_positive), matching
        ``brush_geometry``'s convention.  Returns ``None`` until two points exist.
        """
        if len(self.clip_points) < 2:
            return None
        idx = self._axis_indices()
        if idx is None:
            return None
        a1, a2, depth = idx
        A, B = self.clip_points[0], self.clip_points[1]
        dx, dy = B.x() - A.x(), B.y() - A.y()
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return None
        # Normal perpendicular to the cut line, lying in the view plane.
        nx, ny = dy, -dx
        length = math.hypot(nx, ny)
        nx, ny = nx / length, ny / length
        normal = [0.0, 0.0, 0.0]
        normal[a1] = nx
        normal[a2] = ny
        # Offset for the (already unit) normal: dot(n, point_on_plane).
        offset = nx * A.x() + ny * A.y()
        # Keep the side the cursor is on.
        keep_positive = self.clip_keep_positive
        return normal, offset, keep_positive

    def _update_clip_keep_side(self):
        """Set keep_positive from which side of the cut line the cursor is on."""
        if len(self.clip_points) < 2 or self.clip_hover is None:
            return
        A, B = self.clip_points[0], self.clip_points[1]
        dx, dy = B.x() - A.x(), B.y() - A.y()
        nx, ny = dy, -dx
        side = nx * (self.clip_hover.x() - A.x()) + ny * (self.clip_hover.y() - A.y())
        # Cursor on the +n side -> keep the +n (positive) half.
        self.clip_keep_positive = side > 0

    def apply_clip(self):
        """Perform the cut on the current selection, keeping the previewed side."""
        plane = self._clip_plane()
        if plane is None:
            self.main_window.show_toast("Clip: place two points first", is_error=True)
            return
        normal, offset, keep_positive = plane
        n = self.main_window.apply_clip_to_selection(normal, offset, keep_positive)
        if n:
            self.main_window.show_toast(f"Clipped {n} brush(es)")
        else:
            self.main_window.show_toast("Clip: select a brush to slice", is_error=True)
        self.clear_clip()

    def draw_clip_overlay(self, painter):
        """Draw the clip-tool banner, cut line, points and kept-side arrow."""
        painter.save()

        # Banner / hint in the top-left corner.
        painter.setPen(QColor(255, 200, 0))
        font = painter.font()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        if len(self.clip_points) == 0:
            hint = "CLIP MODE — click two points to set the cut  (X to exit)"
        elif len(self.clip_points) == 1:
            hint = "CLIP MODE — click the second point"
        else:
            hint = "CLIP MODE — move to pick the side to KEEP, Enter to cut  (Esc cancels)"
        painter.drawText(10, 20, hint)

        cut_pen = QPen(QColor(255, 210, 0), 1, Qt.DashLine)
        pt_pen = QPen(QColor(255, 255, 255), 2)

        # First point placed: rubber-band to the cursor.
        if len(self.clip_points) == 1:
            p0 = self.world_to_screen(self.clip_points[0])
            painter.setPen(cut_pen)
            if self.clip_hover is not None:
                painter.drawLine(p0, self.world_to_screen(self.clip_hover))
            painter.setPen(pt_pen)
            painter.drawEllipse(p0, 4, 4)

        # Both points placed: full cut line + kept-side arrow.
        elif len(self.clip_points) >= 2:
            A, B = self.clip_points[0], self.clip_points[1]
            pA, pB = self.world_to_screen(A), self.world_to_screen(B)
            # Extend the line across the whole widget.
            dx, dy = pB.x() - pA.x(), pB.y() - pA.y()
            length = math.hypot(dx, dy) or 1.0
            ux, uy = dx / length, dy / length
            far = float(self.width() + self.height())
            painter.setPen(cut_pen)
            painter.drawLine(QPointF(pA.x() - ux * far, pA.y() - uy * far),
                             QPointF(pB.x() + ux * far, pB.y() + uy * far))
            painter.setPen(pt_pen)
            painter.drawEllipse(pA, 4, 4)
            painter.drawEllipse(pB, 4, 4)

            # Kept-side arrow: offset the midpoint in world space toward the kept
            # half, then map to screen so the view's axis flips are handled.
            mid = QPointF((A.x() + B.x()) * 0.5, (A.y() + B.y()) * 0.5)
            # Perpendicular in world coords (kept-side nudge direction):
            wdx, wdy = B.x() - A.x(), B.y() - A.y()
            wnx, wny = wdy, -wdx
            wlen = math.hypot(wnx, wny) or 1.0
            wnx, wny = wnx / wlen, wny / wlen
            sign = 1.0 if self.clip_keep_positive else -1.0
            keep_world = QPointF(mid.x() + wnx * sign * 32.0 / self.zoom_factor,
                                 mid.y() + wny * sign * 32.0 / self.zoom_factor)
            s_mid = self.world_to_screen(mid)
            s_keep = self.world_to_screen(keep_world)
            painter.setPen(QPen(QColor(60, 220, 90), 2))
            painter.drawLine(s_mid, s_keep)
            painter.drawEllipse(s_keep, 5, 5)
            painter.setPen(QColor(60, 220, 90))
            painter.drawText(s_keep + QPointF(8, 4), "keep")

        painter.restore()

    def draw_rotate_overlay(self, painter):
        """Draw the rotate-tool banner, pivot marker and live angle readout."""
        painter.save()
        painter.setPen(QColor(255, 200, 0))
        font = painter.font()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        mode = "stepped" if self.snap_to_grid_enabled else "free"
        painter.drawText(10, 20, f"ROTATE MODE ({mode}) — drag to spin the "
                                 f"selection  (Esc to exit)")

        if self.rotate_pivot is not None:
            c = self.world_to_screen(self.rotate_pivot)
            painter.setPen(QPen(QColor(255, 210, 0), 1, Qt.DashLine))
            painter.drawEllipse(c, 26, 26)
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.drawLine(QPointF(c.x() - 6, c.y()), QPointF(c.x() + 6, c.y()))
            painter.drawLine(QPointF(c.x(), c.y() - 6), QPointF(c.x(), c.y() + 6))
            if self.rotate_dragging:
                painter.setPen(QColor(60, 220, 90))
                painter.drawText(c + QPointF(30, 4), f"{self.rotate_applied:+.0f}°")
        painter.restore()

    def _store_previous_tab_index(self):
        """Store the current tab index before switching to Properties."""
        if hasattr(self.main_window, 'properties_tab_widget'):
            self.main_window._previous_tab_index = self.main_window.properties_tab_widget.currentIndex()

    def _focus_properties_tab(self):
        """Focus the Properties tab in the properties dock."""
        if hasattr(self.main_window, 'properties_tab_widget'):
            self._store_previous_tab_index()
            self.main_window.properties_tab_widget.setCurrentIndex(0)

    def _restore_previous_tab(self):
        """Restore focus to the previously active tab before Properties was focused."""
        if hasattr(self.main_window, 'properties_tab_widget'):
            prev_idx = getattr(self.main_window, '_previous_tab_index', None)
            if prev_idx is not None and prev_idx < self.main_window.properties_tab_widget.count():
                self.main_window.properties_tab_widget.setCurrentIndex(prev_idx)

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
        # --- Free-rotate tool keys (only while rotate mode is active) ---
        if self._rotate_active():
            if event.key() == Qt.Key_Escape:
                if self.rotate_dragging:
                    self.cancel_rotate()            # abort the current spin
                else:
                    self.main_window.set_rotate_mode(False)  # exit rotate mode
                return

        # --- Clip / slice tool keys (only while clip mode is active) ---
        if self._clip_active():
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.apply_clip()
                return
            if event.key() == Qt.Key_Escape:
                if self.clip_points:
                    self.clear_clip()               # cancel the pending cut
                else:
                    self.main_window.set_clip_mode(False)  # exit clip mode
                return

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

        # --- Arrow Key Nudging ---
        # Only process arrow keys if we have a selected object and we're not in play mode
        selected = self.editor.state.selected_object
        if selected and not getattr(self.editor.view_3d, 'play_mode', False):
            arrow_key = None
            if event.key() == Qt.Key_Up:
                arrow_key = 'up'
            elif event.key() == Qt.Key_Down:
                arrow_key = 'down'
            elif event.key() == Qt.Key_Left:
                arrow_key = 'left'
            elif event.key() == Qt.Key_Right:
                arrow_key = 'right'

            if arrow_key:
                # Determine move distance
                # If grid is visible, use grid size; otherwise use a default of 16
                if self.grid_visible:
                    base_distance = self.grid_size
                else:
                    base_distance = 16

                # Shift = 5x distance for bigger jumps
                if event.modifiers() & Qt.ShiftModifier:
                    distance = base_distance * 5
                else:
                    distance = base_distance

                # Get the axes for this view
                ax1, ax2 = self.get_axes()
                if ax1 and ax2:
                    ax_map = {'x': 0, 'y': 1, 'z': 2}
                    a1_idx = ax_map[ax1]
                    a2_idx = ax_map[ax2]

                    # Determine direction multipliers based on view type
                    # Top view: x right, z down (screen Y increases downward)
                    # Front view: x right, y up (screen Y increases downward, so y decreases upward)
                    # Side view: z right, y up
                    if self.view_type == 'top':
                        dx_map = {'left': -1, 'right': 1, 'up': 0, 'down': 0}
                        dz_map = {'left': 0, 'right': 0, 'up': -1, 'down': 1}
                        dy_map = {'left': 0, 'right': 0, 'up': 0, 'down': 0}
                    elif self.view_type == 'front':
                        dx_map = {'left': -1, 'right': 1, 'up': 0, 'down': 0}
                        dy_map = {'left': 0, 'right': 0, 'up': 1, 'down': -1}
                        dz_map = {'left': 0, 'right': 0, 'up': 0, 'down': 0}
                    elif self.view_type == 'side':
                        dz_map = {'left': -1, 'right': 1, 'up': 0, 'down': 0}
                        dy_map = {'left': 0, 'right': 0, 'up': 1, 'down': -1}
                        dx_map = {'left': 0, 'right': 0, 'up': 0, 'down': 0}
                    else:
                        dx_map = dy_map = dz_map = {'left': 0, 'right': 0, 'up': 0, 'down': 0}

                    # Calculate delta for each axis
                    delta_x = dx_map[arrow_key] * distance
                    delta_y = dy_map[arrow_key] * distance
                    delta_z = dz_map[arrow_key] * distance

                    # Begin (or continue) a nudge burst.  save_state() is a
                    # full-scene serialization, so we call it only once per
                    # burst; the idle timer below closes the burst after a pause.
                    self._begin_nudge_burst()

                    # Apply to selected object
                    if isinstance(selected, dict):
                        # It's a brush
                        pos = selected['pos']
                        new_pos = [pos[0] + delta_x, pos[1] + delta_y, pos[2] + delta_z]
                        # Snap to grid if grid is visible
                        if self.grid_visible:
                            grid = self.grid_size
                            new_pos = [round(v / grid) * grid for v in new_pos]

                        # Angled brushes must move their geometry too, not just pos.
                        if bg.brush_has_geometry(selected):
                            bg.translate_brush(selected, [new_pos[0] - pos[0],
                                                          new_pos[1] - pos[1],
                                                          new_pos[2] - pos[2]])
                        else:
                            pos[0], pos[1], pos[2] = new_pos
                    else:
                        # It's a Thing
                        selected.pos[0] += delta_x
                        selected.pos[1] += delta_y
                        selected.pos[2] += delta_z

                        # Snap to grid if grid is visible
                        if self.grid_visible:
                            grid = self.grid_size
                            selected.pos[0] = round(selected.pos[0] / grid) * grid
                            selected.pos[1] = round(selected.pos[1] / grid) * grid
                            selected.pos[2] = round(selected.pos[2] / grid) * grid

                    # Update views
                    self.update()
                    self.main_window.view_3d.update()

                    # NOTE: We intentionally do NOT rebuild the property editor
                    # here.  set_object() tears down and recreates the entire
                    # property panel (scroll area, tabs, dozens of widgets), which
                    # made repeated nudges very slow.  The panel does not display
                    # the object's live position, so there is nothing to refresh;
                    # the 2D/3D repaints above already reflect the new position.
                    return

        super().keyPressEvent(event)

    def _begin_nudge_burst(self):
        """Start a nudge burst if one isn't already running, and keep it alive.

        The first nudge of a burst records a single undo snapshot; subsequent
        nudges within the idle window reuse it instead of serializing the whole
        scene again.  Each nudge (re)starts the idle timer so a held key or a
        rapid tap sequence stays in one burst.
        """
        if not self._nudge_in_progress:
            self.main_window.save_state()
            self._nudge_in_progress = True
        # Restart the inactivity countdown; the burst ends only after a pause.
        self._nudge_idle_timer.start()

    def _end_nudge_burst(self):
        """Close the current nudge burst so the next nudge starts a new undo step."""
        self._nudge_in_progress = False

    def keyReleaseEvent(self, event):
        # A key release alone must not end the nudge burst: Qt auto-repeat fires
        # a release between every repeated press, and tapping an arrow key
        # releases between taps.  Ending the burst here would force a fresh
        # full-scene save_state() on the very next nudge, which is exactly the
        # slowdown we are avoiding.  The idle timer ends the burst after a pause.
        super().keyReleaseEvent(event)

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
        """Check if a thing's position (plus its radius) is within the visible area."""
        pos = thing.pos
        x = pos[axis1_idx]
        y = pos[axis2_idx]

        # Include the radius if the thing has one (Light, Speaker, etc.)
        radius = 0
        if hasattr(thing, 'get_radius'):
            radius = thing.get_radius()
        # Also add a small fixed margin for sprites
        margin = max(32.0, radius) / self.zoom_factor if self.zoom_factor > 0 else 32.0

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
        # Use the custom grid background colour (or fallback to dark grey)
        bg_hex = self.main_window.config.get('GridColours', 'background', fallback='#2b2b2b')
        painter.fillRect(self.rect(), QColor(bg_hex))
        
        visible_bounds = self.get_visible_world_bounds()
        if visible_bounds.width() <= 0 or visible_bounds.height() <= 0:
            return
        
        self.draw_grid(painter)
        self.draw_terrain(painter, visible_bounds)
        self.draw_brushes(painter, visible_bounds)
        self.draw_things(painter, visible_bounds)
        self.draw_camera(painter)
        if self._clip_active():
            self.draw_clip_overlay(painter)
        if self._rotate_active():
            self.draw_rotate_overlay(painter)

        # --- REVISED: Logic/Trigger Connections ---
        # Only draw if the global toggle is ON (F1)
        show_f1_key = getattr(self.editor, 'show_logic_links', False)
        
        if show_f1_key:
            self.draw_logic_connections(painter, visible_bounds)
            self.draw_patrol_paths(painter, visible_bounds)

            # Teleporter connections (Action=teleport with target_node) ---
            ax1, ax2 = self.get_axes()
            if not ax1 or not ax2:
                return
            ax_map = {'x': 0, 'y': 1, 'z': 2}
            a1 = ax_map[ax1]
            a2 = ax_map[ax2]

            # Build node lookup once
            node_lookup = {}
            for t in self.editor.state.things:
                if isinstance(t, PathNode):
                    n = t.properties.get('name', '') or ''
                    if n:
                        node_lookup[n] = t

            teleporter_color = QColor(200, 100, 255, 200)  # Purple-ish
            teleporter_pen = QPen(teleporter_color, 2, Qt.DashLine)

            for brush in self.editor.state.brushes:
                if not brush.get('is_trigger', False):
                    continue
                if brush.get('trigger_action') != 'teleport':
                    continue
                target_name = brush.get('target_node', '')
                if not target_name:
                    continue
                target_node = node_lookup.get(target_name)
                if target_node is None:
                    continue

                src_w = QPointF(brush['pos'][a1], brush['pos'][a2])
                dst_w = QPointF(target_node.pos[a1], target_node.pos[a2])

                # Culling
                margin = 100.0
                sr = QRectF(src_w.x() - margin, src_w.y() - margin, margin * 2, margin * 2)
                dr = QRectF(dst_w.x() - margin, dst_w.y() - margin, margin * 2, margin * 2)
                if not (visible_bounds.intersects(sr) or visible_bounds.intersects(dr)):
                    continue

                p1 = self.world_to_screen(src_w)
                p2 = self.world_to_screen(dst_w)

                painter.setPen(teleporter_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawLine(p1, p2)

                # Draw an arrowhead at the destination
                self._draw_connection_arrow(painter, p1, p2, teleporter_color)

                # Optional: small "teleport" label at midpoint
                mid = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
                painter.save()
                painter.setPen(QPen(teleporter_color.lighter(150)))
                font = QFont()
                font.setPointSize(7)
                painter.setFont(font)
                painter.drawText(mid + QPointF(4, -4), "teleport")
                painter.restore()

        if self.is_drawing_brush:
            pen = QPen(QColor(255, 255, 0), 1, Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            start_screen = self.world_to_screen(self.draw_start_pos)
            current_screen = self.world_to_screen(self.draw_current_pos)
            painter.drawRect(QRectF(start_screen, current_screen).normalized())

        if self.is_marquee_select:
            self.draw_marquee(painter)

        # Unified group bounding box + scale/rotate handles for a multi-selection.
        if self._group_manip_active():
            self.draw_group_bbox(painter)

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
        
        # Precompute a name -> position lookup once (was an O(N) linear scan
        # over every brush and thing per connection, i.e. O(N*M) per repaint).
        pos_by_name = {}
        for b in self.editor.state.brushes:
            b_name = b.get('name')
            if b_name and b_name not in pos_by_name:
                pos_by_name[b_name] = b['pos']
        for t in self.editor.state.things:
            t_name = getattr(t, 'name', t.properties.get('name'))
            if t_name and t_name not in pos_by_name:
                pos_by_name[t_name] = t.pos

        def get_pos_by_name(name):
            return pos_by_name.get(name)

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

    def draw_patrol_paths(self, painter, visible_bounds):
        """
        Draw dashed teal lines between connected PathNodes (next_node chains)
        and thin dotted lines from patrolling Monsters to their patrol_target.
        """
        ax1, ax2 = self.get_axes()
        if not ax1 or not ax2:
            return
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        a1 = ax_map[ax1]
        a2 = ax_map[ax2]

        # Build a lookup: node-name → thing, for fast resolution
        node_lookup = {}
        for t in self.editor.state.things:
            if isinstance(t, PathNode):
                n = t.properties.get('name', '') or ''
                if n:
                    node_lookup[n] = t

        # --- 1. PathNode → next_node chain lines (teal, dashed) ----------
        teal = QColor(38, 166, 154, 200)
        teal_dim = QColor(38, 166, 154, 80)
        chain_pen = QPen(teal, 2, Qt.DashLine)

        for name, node in node_lookup.items():
            next_name = node.get_next_node_name()
            if not next_name:
                continue
            next_node = node_lookup.get(next_name)
            if next_node is None:
                continue

            src_w = QPointF(node.pos[a1], node.pos[a2])
            dst_w = QPointF(next_node.pos[a1], next_node.pos[a2])

            # Cull if both endpoints are off-screen
            margin = 100.0
            sr = QRectF(src_w.x() - margin, src_w.y() - margin, margin * 2, margin * 2)
            dr = QRectF(dst_w.x() - margin, dst_w.y() - margin, margin * 2, margin * 2)
            if not (visible_bounds.intersects(sr) or visible_bounds.intersects(dr)):
                continue

            p1 = self.world_to_screen(src_w)
            p2 = self.world_to_screen(dst_w)

            painter.setPen(chain_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(p1, p2)

            # Small arrowhead at destination
            self._draw_connection_arrow(painter, p1, p2, teal)

            # Tiny "next" label at midpoint
            mid = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
            painter.save()
            painter.setPen(QPen(teal_dim))
            font = QFont()
            font.setPointSize(7)
            painter.setFont(font)
            painter.drawText(mid + QPointF(4, -4), "next")
            painter.restore()

        # --- 2. Monster → patrol_target lines (teal, dotted, thinner) ----
        patrol_pen = QPen(QColor(38, 166, 154, 120), 1, Qt.DotLine)
        for t in self.editor.state.things:
            if not isinstance(t, Monster):
                continue
            if not t.properties.get('patrol', False):
                continue
            target_name = t.properties.get('patrol_target', '') or ''
            if not target_name:
                continue
            target_node = node_lookup.get(target_name)
            if target_node is None:
                continue

            src_w = QPointF(t.pos[a1], t.pos[a2])
            dst_w = QPointF(target_node.pos[a1], target_node.pos[a2])

            margin = 100.0
            sr = QRectF(src_w.x() - margin, src_w.y() - margin, margin * 2, margin * 2)
            dr = QRectF(dst_w.x() - margin, dst_w.y() - margin, margin * 2, margin * 2)
            if not (visible_bounds.intersects(sr) or visible_bounds.intersects(dr)):
                continue

            p1 = self.world_to_screen(src_w)
            p2 = self.world_to_screen(dst_w)

            painter.setPen(patrol_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(p1, p2)
            self._draw_connection_arrow(painter, p1, p2, QColor(38, 166, 154, 120))

    def draw_grid(self, painter):
        # Don't draw grid if hidden or in play mode
        if not self.grid_visible:
            return
        if hasattr(self.editor, 'view_3d') and self.editor.view_3d.play_mode:
            return
            
        config = self.main_window.config
        minor_hex = config.get("GridColours", "minor", fallback="#404040")
        major_hex = config.get("GridColours", "major", fallback="#5a5a5a")
        bg_hex = config.get("GridColours", "background", fallback="#2b2b2b")

        grid_color = QColor(minor_hex)          # thin lines
        thick_grid_color = QColor(major_hex)    # every 8th line
        world_origin_color = QColor(0, 255, 0)  # keep origin green
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

        # Computed once: when several objects are selected the unified group box
        # owns the handles, so individual brushes must not draw their own.
        group_active = self._group_manip_active()

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
            is_door = brush.get('is_door', False)
            
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

            # Angled (clipped) brushes draw their true convex outline; plain box
            # brushes draw the fast bounding rectangle.  screen_rect is still
            # computed above for labels/handles/colour tags.
            hull = None
            if bg.brush_has_geometry(brush):
                convex = bg.get_convex(brush)
                if convex is not None and convex.is_valid:
                    hull = convex.silhouette(axis1_idx, axis2_idx)
            if hull:
                poly = QPolygonF([self.world_to_screen(QPointF(a, b)) for a, b in hull])
                painter.drawPolygon(poly)
            else:
                painter.drawRect(screen_rect)

                        # Build combined type label for trigger/mover/door
            type_labels = []
            if is_trigger:
                type_labels.append("TRIGGER")
            if is_mover:
                type_labels.append("MOVER")
            if is_door:
                type_labels.append("DOOR")
            
            if type_labels:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                label_text = " / ".join(type_labels)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, label_text)

            # Add mover label
            if is_mover:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, "MOVER")
            
            # Add door label
            if is_door:
                painter.setPen(QColor(255, 255, 255, 180))
                font = painter.font()
                font.setPointSize(10)
                painter.setFont(font)
                painter.drawText(screen_rect.adjusted(0, 0, -5, -5), Qt.AlignRight | Qt.AlignBottom, "DOOR")
            
            
            # Draw glow light direction arrow
            if brush.get('shader') == 'Glow':
                # Hide arrows in play mode unless F5 toggle is enabled
                play_mode = getattr(self.main_window.view_3d, 'play_mode', False)
                show_arrows = getattr(self.main_window.view_3d, 'show_glow_arrows_in_play_mode', False)
                if not play_mode or show_arrows:
                    self.draw_glow_light_arrow(painter, brush, ax1, ax2, ax_map)

            # A lone selected brush keeps its own resize handles; when several
            # objects are selected the unified group box owns the handles instead.
            if is_selected and not is_locked and not group_active:
                self.draw_resize_handles(painter, screen_rect)

            self.draw_brush_color_tag(painter, brush, screen_rect)

    def draw_mover_arrow(self, painter, brush, ax1, ax2, ax_map):
        direction = brush.get('direction', [0, 1, 0])
        distance = brush.get('distance', 128.0)

        play_mode = getattr(self.main_window.view_3d, 'play_mode', False)
        if play_mode and 'original_pos' in brush:
            start_3d = brush['original_pos']
        else:
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
        
        if model_path not in renderer.loaded_models:
            return None
            
        obj = renderer.loaded_models.get(model_path)
        if not obj or not hasattr(obj, 'cpu_vertices') or obj.cpu_vertices is None or len(obj.cpu_vertices) == 0:
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
        model_path = model_thing.properties.get('model_path')
        if model_path and hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.renderer:
            renderer = self.main_window.view_3d.renderer
            if model_path not in renderer.loaded_models:
                renderer.load_model(model_path)

        coords = self._compute_model_screen_coords(model_thing, ax_map, ax1, ax2)

        if coords is None:
            painter.setPen(QPen(QColor(200, 200, 200), 1))
            s_pos = self.world_to_screen(QPointF(model_thing.pos[ax_map[ax1]], model_thing.pos[ax_map[ax2]]))
            painter.drawRect(QRectF(s_pos.x()-10, s_pos.y()-10, 20, 20))
            return

        pts_x, pts_y = coords
        n_pts = len(pts_x)

        painter.setPen(QPen(QColor(0, 255, 255, 100), 1))
        width, height = self.width(), self.height()

        # Prefer the model's index buffer (cpu_triangles) for correct wireframe on
        # indexed geometry (all GLBs; most OBJs).  Without this, the loop treats
        # unique shared vertices as sequential flat triangles, which gives wrong
        # edges AND crashes when vertex_count % 3 != 0.
        obj = None
        if model_path and hasattr(self.main_window, 'view_3d') and self.main_window.view_3d.renderer:
            obj = self.main_window.view_3d.renderer.loaded_models.get(model_path)
        cpu_triangles = getattr(obj, 'cpu_triangles', None)

        if cpu_triangles:
            for tri in cpu_triangles:
                i0, i1, i2 = tri
                # Guard against malformed index data
                if i0 >= n_pts or i1 >= n_pts or i2 >= n_pts:
                    continue
                # Frustum cull
                if (pts_x[i0] < 0 and pts_x[i1] < 0 and pts_x[i2] < 0) or \
                (pts_x[i0] > width and pts_x[i1] > width and pts_x[i2] > width) or \
                (pts_y[i0] < 0 and pts_y[i1] < 0 and pts_y[i2] < 0) or \
                (pts_y[i0] > height and pts_y[i1] > height and pts_y[i2] > height):
                    continue
                p0 = QPointF(pts_x[i0], pts_y[i0])
                p1 = QPointF(pts_x[i1], pts_y[i1])
                p2 = QPointF(pts_x[i2], pts_y[i2])
                painter.drawLine(p0, p1)
                painter.drawLine(p1, p2)
                painter.drawLine(p2, p0)
        else:
            # Fallback for non-indexed meshes where vertices are laid out as
            # sequential flat triangles.  Use n_pts - 2 as the bound so a
            # vertex count not divisible by 3 never reads past the end of the array.
            for i in range(0, n_pts - 2, 3):
                if (pts_x[i] < 0 and pts_x[i+1] < 0 and pts_x[i+2] < 0) or \
                (pts_x[i] > width and pts_x[i+1] > width and pts_x[i+2] > width) or \
                (pts_y[i] < 0 and pts_y[i+1] < 0 and pts_y[i+2] < 0) or \
                (pts_y[i] > height and pts_y[i+1] > height and pts_y[i+2] > height):
                    continue
                p0 = QPointF(pts_x[i],   pts_y[i])
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

            # --- PORTAL RENDERING ---
            elif isinstance(thing, Portal):
                draw_rect = self._draw_portal_gizmo(painter, thing, s_pos, axis1_idx, axis2_idx, ax_map, ax1, ax2, visible_bounds)

            # --- SPRITE RENDERING ---
            else:
                # 1. Draw Radius
                if (isinstance(thing, Light) or isinstance(thing, Speaker)) and thing.properties.get('show_radius', False):
                    default_col = [255, 255, 0] if isinstance(thing, Speaker) else [255, 255, 255]
                    col = thing.properties.get('colour', default_col)

                    # Normalise to 0-255 integers
                    if isinstance(col, (list, tuple)) and len(col) >= 3:
                        r, g, b = col[0], col[1], col[2]
                        if max(r, g, b) > 1.0:
                            # Already 0-255 range
                            r, g, b = int(r), int(g), int(b)
                        else:
                            # Convert 0.0-1.0 to 0-255
                            r, g, b = int(r * 255), int(g * 255), int(b * 255)
                    else:
                        r, g, b = 255, 255, 255

                    viz_color = QColor(r, g, b, 60)
                    painter.setBrush(QBrush(viz_color))
                    painter.setPen(QPen(viz_color.darker(120), 1))
                    radius = thing.get_radius() * self.zoom_factor
                    painter.drawEllipse(s_pos, radius, radius)

                # 1b. Draw Monster Sight Radius
                if isinstance(thing, Monster) and getattr(self.editor, '_sight_preview_thing', None) is thing:
                    sight = thing.properties.get('sight', 300)
                    sight_px = sight * self.zoom_factor
                    # Translucent fill
                    painter.setBrush(QBrush(QColor(255, 80, 40, 25)))
                    # Dashed orange-red border
                    painter.setPen(QPen(QColor(255, 100, 40, 200), 1, Qt.DashLine))
                    painter.drawEllipse(s_pos, sight_px, sight_px)
                    # Distance label at right-hand edge of the circle
                    painter.save()
                    painter.setPen(QPen(QColor(255, 140, 80)))
                    font = QFont()
                    font.setPointSize(8)
                    painter.setFont(font)
                    painter.drawText(QPointF(s_pos.x() + sight_px + 4, s_pos.y() + 4), f"{sight} u")
                    painter.restore()

                # 1c. Draw PathNode Radius
                if isinstance(thing, PathNode) and thing.properties.get('show_radius', False):
                    radius = thing.get_radius() * self.zoom_factor
                    painter.setBrush(QBrush(QColor(38, 166, 154, 30)))
                    painter.setPen(QPen(QColor(38, 166, 154, 220), 1, Qt.DashLine))
                    painter.drawEllipse(s_pos, radius, radius)
                    painter.save()
                    painter.setPen(QPen(QColor(77, 208, 196)))
                    font = QFont()
                    font.setPointSize(8)
                    painter.setFont(font)
                    label = f"{int(thing.get_radius())} u  [{thing.get_affects_type()}]"
                    painter.drawText(QPointF(s_pos.x() + radius + 4, s_pos.y() + 4), label)
                    painter.restore()

                # 2. Draw Sprite (or orange cube marker for PathNode)
                pixmap = None
                if isinstance(thing, PathNode):
                    # PathNode renders as a small solid orange square — no sprite
                    half = 10
                    painter.save()
                    painter.setBrush(QBrush(QColor(255, 128, 0)))
                    painter.setPen(QPen(QColor(200, 80, 0), 1))
                    node_rect = QRectF(s_pos.x() - half, s_pos.y() - half, half * 2, half * 2)
                    painter.drawRect(node_rect)
                    # Label with node name
                    painter.setPen(QPen(QColor(255, 180, 80)))
                    font = QFont()
                    font.setPointSize(8)
                    font.setBold(True)
                    painter.setFont(font)
                    node_name = thing.properties.get('name', '')
                    painter.drawText(QPointF(s_pos.x() + half + 4, s_pos.y() + 4), node_name)
                    painter.restore()
                    draw_rect = node_rect
                elif hasattr(thing, 'get_icon_pixmap'):
                    pixmap = thing.get_icon_pixmap()
                else:
                    pixmap = thing.get_instance_pixmap()

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

                # 2b. Draw Monster Name beneath sprite
                if isinstance(thing, Monster):
                    painter.save()
                    painter.setPen(QPen(QColor(255, 100, 100)))
                    font = QFont()
                    font.setPointSize(8)
                    font.setBold(True)
                    painter.setFont(font)
                    monster_name = thing.properties.get('name', '') or getattr(thing, 'name', '') or ''
                    if monster_name:
                        fm = painter.fontMetrics()
                        text_height = fm.height()
                        # Position at left edge of sprite, with DPI-aware gap
                        text_x = s_pos.x() - 30
                        text_y = s_pos.y() + 30 + text_height + 2
                        painter.drawText(QPointF(text_x, text_y), monster_name)
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

            # 4. Overlays (selection highlight + color tag)
            if draw_rect:
                self.draw_thing_color_tag(painter, thing, draw_rect)
                is_selected = thing in getattr(self.editor.state, 'selected_objects', []) or thing == self.editor.state.selected_object
                if is_selected:
                    painter.setPen(QPen(QColor(255, 255, 0), 2, Qt.DotLine))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(draw_rect.adjusted(-2, -2, 2, 2))

        # Draw portal pair link lines on top of all things (F1 toggle respects this too)
        self._draw_portal_links(painter, axis1_idx, axis2_idx, visible_bounds)

    def _draw_portal_gizmo(self, painter, thing, s_pos, axis1_idx, axis2_idx, ax_map, ax1, ax2, visible_bounds):
        """
        Draw the Portal aperture as a thick line segment in the 2D view,
        with a small normal arrow showing the facing direction and the portal name.
        Returns the bounding QRectF used for selection/hit-testing.
        """
        # ---- Color handling (FIXED) ----
        raw_col = thing.properties.get('color', [255, 255, 255])

        # Handle string values that may come from property editor text fields
        if isinstance(raw_col, str):
            try:
                import ast
                raw_col = ast.literal_eval(raw_col)
            except (ValueError, SyntaxError):
                raw_col = [255, 255, 255]

        # Ensure valid list/tuple with at least 3 numeric components
        if not isinstance(raw_col, (list, tuple)) or len(raw_col) < 3:
            raw_col = [255, 255, 255]

        # Safely convert each component to 0-255 int
        def _to_color_int(val, default=255):
            try:
                return max(0, min(255, int(float(val))))
            except (TypeError, ValueError):
                return default

        r = _to_color_int(raw_col[0] if len(raw_col) > 0 else 255)
        g = _to_color_int(raw_col[1] if len(raw_col) > 1 else 255)
        b = _to_color_int(raw_col[2] if len(raw_col) > 2 else 255)

        # Dim inactive portals
        if not thing.is_active():
            r, g, b = int(r * 0.4), int(g * 0.4), int(b * 0.4)

        # Warn visually when portal_target is missing or points to a nonexistent portal
        target_name = thing.properties.get('portal_target', '')
        target_exists = target_name and any(
            isinstance(t, Portal) and t.properties.get('name') == target_name
            for t in self.editor.state.things
            if t is not thing
        )
        is_broken = not target_exists
        if is_broken:
            r, g, b = 220, 60, 60   # red tint — unlinked or broken target

        portal_color = QColor(r, g, b, 220)
        dim_color    = QColor(r, g, b, 80)

        is_active = thing.is_active()

        # Top view: aperture is a horizontal line (X axis), normal points along Z.
        # Front/side views: aperture is a vertical line (Y axis), normal shows as a dot.
        # For the top view we can show a proper line + normal; for other views a square.
        yaw = thing.get_yaw_radians()
        w2  = thing.get_width() / 2.0

        if ax1 == 'x' and ax2 == 'z':
            # Top view — draw full aperture line and normal arrow
            rx =  math.cos(yaw)   # right vector X
            rz = -math.sin(yaw)   # right vector Z (Z is our screen-Y in top view)
            nx =  math.sin(yaw)   # normal X
            nz =  math.cos(yaw)   # normal Z

            ox, oz = float(thing.pos[0]), float(thing.pos[2])

            left_w  = QPointF(ox - rx * w2, oz - rz * w2)
            right_w = QPointF(ox + rx * w2, oz + rz * w2)
            left_s  = self.world_to_screen(left_w)
            right_s = self.world_to_screen(right_w)

            # Aperture line
            pen_style = Qt.SolidLine if is_active else Qt.DashLine
            painter.save()
            painter.setPen(QPen(portal_color, 3, pen_style))
            painter.drawLine(left_s, right_s)

            # Normal arrow (shows facing direction)
            normal_len = 24.0  # screen pixels
            mid_s = QPointF((left_s.x() + right_s.x()) / 2,
                            (left_s.y() + right_s.y()) / 2)
            # In top view, Z maps to screen-Y (downward positive in Qt)
            tip_s = QPointF(mid_s.x() + nx * normal_len,
                            mid_s.y() + nz * normal_len)
            painter.setPen(QPen(portal_color, 2))
            painter.drawLine(mid_s, tip_s)
            # Arrowhead
            angle = math.atan2(tip_s.y() - mid_s.y(), tip_s.x() - mid_s.x())
            hs = 8
            ah1 = tip_s - QPointF(math.cos(angle - math.pi/6)*hs, math.sin(angle - math.pi/6)*hs)
            ah2 = tip_s - QPointF(math.cos(angle + math.pi/6)*hs, math.sin(angle + math.pi/6)*hs)
            painter.setBrush(QBrush(portal_color))
            painter.drawPolygon(QPolygonF([tip_s, ah1, ah2]))

            # Name label
            painter.setPen(QPen(portal_color.lighter(150)))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(right_s + QPointF(5, 4), thing.properties.get('name', ''))

            painter.restore()

            # Bounding rect for selection
            xs = [left_s.x(), right_s.x(), tip_s.x()]
            ys = [left_s.y(), right_s.y(), tip_s.y()]
            margin = 6
            draw_rect = QRectF(min(xs) - margin, min(ys) - margin,
                            max(xs) - min(xs) + margin*2,
                            max(ys) - min(ys) + margin*2)

        else:
            # Front / side view — draw as a tall rectangle indicating the aperture
            h2 = thing.get_height() / 2.0
            # In front view ax1=x, ax2=y;  in side view ax1=z, ax2=y
            px = float(thing.pos[axis1_idx])
            py = float(thing.pos[axis2_idx])

            # Width in this projection depends on the view:
            # front view: portal width is along X, so project w2 onto X
            # side view:  portal width is along Z, project onto Z
            if ax1 == 'x':
                proj_half_w = w2 * abs(math.cos(yaw))
            else:
                proj_half_w = w2 * abs(math.sin(yaw))
            proj_half_w = max(4.0, proj_half_w)

            top_left_w  = QPointF(px - proj_half_w, py + h2)
            bot_right_w = QPointF(px + proj_half_w, py - h2)
            tl_s = self.world_to_screen(top_left_w)
            br_s = self.world_to_screen(bot_right_w)
            rect_s = QRectF(tl_s, br_s).normalized()

            pen_style = Qt.SolidLine if is_active else Qt.DashLine
            painter.save()
            painter.setPen(QPen(portal_color, 2, pen_style))
            painter.setBrush(QBrush(dim_color))
            painter.drawRect(rect_s)

            # Name label
            painter.setPen(QPen(portal_color.lighter(150)))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(rect_s.topRight() + QPointF(4, 12), thing.properties.get('name', ''))

            painter.restore()
            draw_rect = rect_s

        return draw_rect

    def _draw_portal_links(self, painter, axis1_idx, axis2_idx, visible_bounds):
        """
        Draw a dashed cyan line between each linked portal pair
        """
        portals_by_name = {}
        for t in self.editor.state.things:
            if isinstance(t, Portal):
                name = t.properties.get('name', '')
                if name:
                    portals_by_name[name] = t

        drawn_pairs = set()
        link_pen = QPen(QColor(0, 200, 255, 140), 1, Qt.DashLine)

        for name, pa in portals_by_name.items():
            target = pa.properties.get('portal_target', '')
            if not target or target not in portals_by_name:
                continue
            pair = frozenset({name, target})
            if pair in drawn_pairs:
                continue
            drawn_pairs.add(pair)

            pb = portals_by_name[target]

            ax_w = QPointF(float(pa.pos[axis1_idx]), float(pa.pos[axis2_idx]))
            bx_w = QPointF(float(pb.pos[axis1_idx]), float(pb.pos[axis2_idx]))

            # Cull if both endpoints out of view
            margin = 80.0
            ar = QRectF(ax_w.x()-margin, ax_w.y()-margin, margin*2, margin*2)
            br = QRectF(bx_w.x()-margin, bx_w.y()-margin, margin*2, margin*2)
            if not (visible_bounds.intersects(ar) or visible_bounds.intersects(br)):
                continue

            p1 = self.world_to_screen(ax_w)
            p2 = self.world_to_screen(bx_w)

            painter.setPen(link_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(p1, p2)

            # Small label at midpoint
            mid = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
            painter.save()
            painter.setPen(QPen(QColor(0, 200, 255, 100)))
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            link_label = f"{name} ↔ {target}"
            painter.drawText(mid + QPointF(4, -4), link_label)
            painter.restore()

    def draw_terrain(self, painter, visible_bounds):
        """Draw terrain outline/profile in 2D view."""
        # Check if terrain exists and is enabled
        if not hasattr(self.editor, 'terrain') or not self.editor.terrain:
            return
        terrain = self.editor.terrain
        # Keep the Big World "fill world with terrain" preview live as the view
        # pans (the top view is the world map). Cheap + idempotent; expands the
        # terrain to the world and streams it around the editor camera.
        if self.view_type == 'top' and hasattr(self.editor, 'sync_bigworld_terrain'):
            self.editor.sync_bigworld_terrain()
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
            # TOP VIEW - Draw terrain boundary rectangle. Clamp to the visible
            # viewport: an infinite/world-filled terrain has bounds millions of
            # units across, and handing Qt an astronomically large rectangle is
            # slow and precision-glitchy — the on-screen part is all that shows.
            fill_min_x = max(t_min_x, min1); fill_max_x = min(t_max_x, max1)
            fill_min_z = max(t_min_z, min2); fill_max_z = min(t_max_z, max2)
            p1 = self.world_to_screen(QPointF(fill_min_x, fill_min_z))
            p2 = self.world_to_screen(QPointF(fill_max_x, fill_max_z))
            rect = QRectF(p1, p2).normalized()

            painter.setBrush(QBrush(terrain_fill))
            painter.drawRect(rect)
            
            # Draw a chunk grid to indicate terrain. Clip to the visible
            # viewport (a world-filled terrain spans thousands of chunks — only
            # the on-screen lines are worth drawing) and skip it entirely when
            # chunks would be sub-pixel at the current zoom.
            chunk_size = terrain.chunk_size
            if chunk_size * self.zoom_factor >= 4:
                painter.setPen(QPen(QColor(139, 90, 43, 60), 1))
                cx_start = max(terrain.min_chunk_x,
                               int(math.floor((min1 - terrain.offset_x) / chunk_size)))
                cx_end = min(terrain.max_chunk_x + 1,
                             int(math.ceil((max1 - terrain.offset_x) / chunk_size)))
                z0 = max(t_min_z, min2); z1 = min(t_max_z, max2)
                for cx in range(cx_start, cx_end + 1):
                    x = cx * chunk_size + terrain.offset_x
                    if x < t_min_x or x > t_max_x:
                        continue
                    p1 = self.world_to_screen(QPointF(x, z0))
                    p2 = self.world_to_screen(QPointF(x, z1))
                    painter.drawLine(p1, p2)
                cz_start = max(terrain.min_chunk_z,
                               int(math.floor((min2 - terrain.offset_z) / chunk_size)))
                cz_end = min(terrain.max_chunk_z + 1,
                             int(math.ceil((max2 - terrain.offset_z) / chunk_size)))
                x0 = max(t_min_x, min1); x1 = min(t_max_x, max1)
                for cz in range(cz_start, cz_end + 1):
                    z = cz * chunk_size + terrain.offset_z
                    if z < t_min_z or z > t_max_z:
                        continue
                    p1 = self.world_to_screen(QPointF(x0, z))
                    p2 = self.world_to_screen(QPointF(x1, z))
                    painter.drawLine(p1, p2)
            
            # Label — anchored to the visible top-left of the terrain so it
            # stays on screen even when the terrain is effectively infinite.
            painter.setPen(QPen(terrain_color, 1))
            label_world = QPointF(max(t_min_x, min1) + 10, max(t_min_z, min2) + 10)
            infinite = (terrain.max_chunk_x - terrain.min_chunk_x) > 100000
            painter.drawText(self.world_to_screen(label_world),
                             "TERRAIN (∞)" if infinite else "TERRAIN")
            
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
    
    def _check_camera_changed(self):
        """
        Check if the 3D camera has moved/rotated since last frame.
        If so, schedule a repaint of this 2D view (only the camera indicator).
        This runs at ~60 FPS but only repaints when the camera actually changes,
        keeping CPU usage minimal while providing smooth viewcone updates.
        """
        if not self.isVisible():
            return

        camera = self.editor.view_3d.camera

        # Get current camera state
        current_pos = (float(camera.pos.x), float(camera.pos.y), float(camera.pos.z))
        current_yaw = float(camera.yaw)
        current_pitch = float(camera.pitch)

        # Check if anything changed (with small epsilon for float comparison)
        EPSILON = 0.01
        changed = False

        if self._last_camera_pos is None:
            changed = True
        else:
            if any(abs(current_pos[i] - self._last_camera_pos[i]) > EPSILON for i in range(3)):
                changed = True
            if abs(current_yaw - self._last_camera_yaw) > EPSILON:
                changed = True
            if abs(current_pitch - self._last_camera_pitch) > EPSILON:
                changed = True

        if changed:
            # Update cache
            self._last_camera_pos = current_pos
            self._last_camera_yaw = current_yaw
            self._last_camera_pitch = current_pitch
            # Schedule repaint - Qt will coalesce multiple update() calls
            self.update()

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

    def draw_marquee(self, painter):
        """Draw the live rubber-band box and outline the objects it will catch."""
        idx = self._axis_indices()
        if idx is None:
            return
        i1, i2, _ = idx
        painter.save()

        # Highlight the caught objects so the selection is previewed live.
        hi_pen = QPen(QColor(0, 220, 255), 2)
        painter.setPen(hi_pen)
        painter.setBrush(Qt.NoBrush)
        for o in self.marquee_hits:
            b1, b2, B1, B2 = self._obj_bounds_2d(o, i1, i2)
            p1 = self.world_to_screen(QPointF(b1, b2))
            p2 = self.world_to_screen(QPointF(B1, B2))
            r = QRectF(p1, p2).normalized()
            if r.width() < 6 and r.height() < 6:      # a point entity
                r = r.adjusted(-7, -7, 7, 7)
            painter.drawRect(r)

        # The marquee rectangle itself.
        start = self.world_to_screen(self.marquee_start)
        cur = self.world_to_screen(self.marquee_current)
        rect = QRectF(start, cur).normalized()
        painter.setPen(QPen(QColor(0, 200, 255), 1, Qt.DashLine))
        painter.setBrush(QBrush(QColor(0, 180, 255, 30)))
        painter.drawRect(rect)
        painter.restore()

    def draw_group_bbox(self, painter):
        """Draw one bounding box with 8 handles around the whole selection.

        Yellow square handles in scale mode; orange round handles in rotate mode
        (click the selection again to toggle)."""
        wb = self._selection_bounds_2d()
        if wb is None:
            return
        p1 = self.world_to_screen(wb.topLeft())
        p2 = self.world_to_screen(wb.bottomRight())
        srect = QRectF(p1, p2).normalized()

        painter.save()
        rotate = (self.manip_mode == 'rotate')
        box_color = QColor(255, 150, 40) if rotate else QColor(255, 235, 0)
        painter.setPen(QPen(box_color, 1, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(srect)

        hs = 9
        painter.setPen(QPen(box_color.darker(160), 1))
        painter.setBrush(QBrush(box_color))
        for h in self.get_resize_handles(srect):
            if rotate:
                painter.drawEllipse(h, hs / 2, hs / 2)
            else:
                painter.drawRect(QRectF(h.x() - hs / 2, h.y() - hs / 2, hs, hs))
        painter.restore()

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
            # --- Free-rotate tool: left-drag spins the selection ---
            if self._rotate_active():
                self.begin_rotate(world_pos)
                return

            # --- Clip / slice tool: left clicks place the two cut points ---
            if self._clip_active():
                snapped = self.snap_to_grid(world_pos)
                if len(self.clip_points) >= 2:
                    self.clip_points = []   # start a fresh cut
                self.clip_points.append(snapped)
                self.clip_hover = snapped
                self._update_clip_keep_side()
                self.update()
                return

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
            
            # --- Group bounding-box handles (multi-selection) take priority ---
            g_handle = self._group_handle_at(event.pos())
            if g_handle != -1:
                if self.manip_mode == 'rotate':
                    if self._begin_group_rotate(world_pos):
                        self.update()
                        return
                else:
                    self._begin_group_resize(g_handle)
                    self.update()
                    return

            # Single-brush resize handles only apply to a lone selection; a
            # multi-selection is handled by the group box above.
            handle_ix = -1 if self._group_manip_active() else self.get_handle_at(event.pos())
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

            # Alt-click cycles through stacked objects; a plain click takes the top.
            cycle = bool(event.modifiers() & Qt.AltModifier)
            clicked_object = self.get_object_at(event.pos(), highlight_locked=True, cycle=cycle)

            # A second plain click inside an existing group (without dragging)
            # flips the group handles between scale and rotate — decided on release.
            self._maybe_toggle_manip = False

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
                # Focus Properties tab when selecting objects
                if selected_objects and hasattr(self.main_window, 'properties_tab_widget'):
                    self._focus_properties_tab()
            else:
                # If the clicked object is already part of a multi-selection,
                # keep the whole group so it can be dragged together.  Otherwise
                # fall back to normal single selection.
                current_selection = getattr(self.editor.state, 'selected_objects', []) or []
                if clicked_object and clicked_object in current_selection and len(current_selection) > 1:
                    # Preserve the group; a click-without-drag toggles handle mode.
                    self._maybe_toggle_manip = True
                else:
                    self.editor.set_selected_object(clicked_object)
                    self.manip_mode = 'resize'  # fresh selection starts in scale mode
                    # Focus Properties tab when selecting an object
                    if clicked_object and hasattr(self.main_window, 'properties_tab_widget'):
                        self._focus_properties_tab()

            if clicked_object and not (event.modifiers() & Qt.ShiftModifier):
                # Check if object is locked (works for both brushes and things)
                if isinstance(clicked_object, dict):
                    is_locked = clicked_object.get('lock', False)
                else:
                    is_locked = clicked_object.properties.get('lock', False)

                if not is_locked:
                    ax1, ax2 = self.get_axes()
                    ax_map = {'x': 0, 'y': 1, 'z': 2}
                    i1, i2 = ax_map[ax1], ax_map[ax2]

                    # Drag every selected object together (a group drag), moving
                    # only the unlocked members.  When the click landed on an
                    # object outside the current selection, just drag that one.
                    group = getattr(self.editor.state, 'selected_objects', []) or []
                    if clicked_object not in group:
                        group = [clicked_object]
                    self.drag_group = [
                        o for o in group
                        if not (o.get('lock', False) if isinstance(o, dict)
                                else o.properties.get('lock', False))
                    ]
                    self.drag_primary = clicked_object

                    self.is_dragging_object = True
                    self.drag_start_pos = world_pos
                    pos_ref = clicked_object['pos'] if isinstance(clicked_object, dict) else clicked_object.pos
                    obj_pos_2d = QPointF(pos_ref[i1], pos_ref[i2])
                    self.drag_offset = obj_pos_2d - world_pos
            elif not clicked_object:
                # Empty space: the Brush tool draws new geometry, the Select tool
                # (default) drags a rubber-band marquee instead.
                if self._brush_tool_active():
                    self.is_drawing_brush = True
                    self.draw_start_pos = self.snap_to_grid(world_pos)
                    self.draw_current_pos = self.draw_start_pos
                else:
                    self.is_marquee_select = True
                    self.marquee_start = world_pos
                    self.marquee_current = world_pos
                    self.marquee_hits = []
        self.update()

    def mouseMoveEvent(self, event):
        world_pos = self.screen_to_world(event.pos())
        middle_click_pan_enabled = self.main_window.config.getboolean('Controls', 'MiddleClickDrag', fallback=False)

        # Free-rotate tool: a left-drag updates the spin; otherwise fall through.
        if self.rotate_dragging:
            if event.buttons() & Qt.LeftButton:
                self.update_rotate(world_pos)
                return
            # Button already released elsewhere — finish the drag.
            self.commit_rotate()

        # Clip tool: track the cursor so the preview line and kept side follow it.
        if self._clip_active():
            self.clip_hover = world_pos
            self._update_clip_keep_side()
            if self.clip_points:
                self.update()
            # fall through so panning (right/middle drag) still works below

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
            g_handle = self._group_handle_at(event.pos())
            single_handle = -1 if self._group_manip_active() else self.get_handle_at(event.pos())
            handle_ix = g_handle if g_handle != -1 else single_handle
            if handle_ix != -1:
                if self._group_manip_active() and self.manip_mode == 'rotate':
                    self.setCursor(Qt.CrossCursor)   # rotate handles
                elif handle_ix in [0, 3]: self.setCursor(Qt.SizeFDiagCursor)
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
        
        elif self.is_marquee_select:
            # Live rubber-band: track the corner and recompute the caught set so
            # it can be highlighted before the selection is committed on release.
            self.marquee_current = world_pos
            enclose = bool(event.modifiers() & Qt.AltModifier)  # Alt = enclose-only
            rect = QRectF(self.marquee_start, self.marquee_current)
            self.marquee_hits = self._objects_in_rect(rect, enclose=enclose)

        elif self.is_group_resizing:
            self._update_group_resize(world_pos)
            current_time = time.time()
            if current_time - self.last_3d_update_time > 0.016:
                self.main_window.view_3d.update()
                self.last_3d_update_time = current_time

        elif self.is_group_rotating:
            self._update_group_rotate(world_pos)

        elif self.is_drawing_brush:
            self.draw_current_pos = self.snap_to_grid(world_pos)

        elif self.is_dragging_object:
            # The "grab" object stays under the cursor; every other member of
            # the drag group follows by the same (snapped) delta so the whole
            # box-selection moves as one and keeps its relative layout.
            primary = self.drag_primary or self.editor.state.selected_object
            group = self.drag_group or ([primary] if primary else [])
            if primary:
                ax1, ax2 = self.get_axes()
                ax_map = {'x': 0, 'y': 1, 'z': 2}
                i1, i2 = ax_map[ax1], ax_map[ax2]

                p_ref = primary['pos'] if isinstance(primary, dict) else primary.pos
                new_primary_pos = self.snap_to_grid(world_pos + self.drag_offset)
                d1 = new_primary_pos.x() - p_ref[i1]
                d2 = new_primary_pos.y() - p_ref[i2]

                if d1 != 0 or d2 != 0:
                    self._maybe_toggle_manip = False  # a real drag, not a toggle-click
                    for obj in group:
                        self._translate_object_2d(obj, d1, d2, i1, i2)

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
            # Free-rotate tool: releasing the button commits the spin.
            if self.rotate_dragging:
                self.commit_rotate()
                return

            # Group bounding-box scale released.
            if self.is_group_resizing:
                self._end_group_resize()
                self.main_window.view_3d.update()
                self.update()
                return

            # Group bounding-box rotate released.
            if self.is_group_rotating:
                self._end_group_rotate()
                self.update()
                return

            # Rubber-band marquee released: commit the caught set, or treat a
            # tiny box as a plain empty-click and deselect.
            if self.is_marquee_select:
                self.is_marquee_select = False
                rect = QRectF(self.marquee_start, self.marquee_current).normalized()
                min_world = 3.0 / max(self.zoom_factor, 1e-6)
                self.marquee_hits = []
                if rect.width() < min_world and rect.height() < min_world:
                    self.editor.set_selected_object(None)   # click empty = deselect
                    self.update()
                    return
                enclose = bool(event.modifiers() & Qt.AltModifier)
                hits = self._objects_in_rect(rect, enclose=enclose)
                if hits:
                    self.editor.set_selected_objects(hits)
                    self.manip_mode = 'resize'
                    if hasattr(self.main_window, 'properties_tab_widget'):
                        self._focus_properties_tab()
                    self.main_window.show_toast(f"Selected {len(hits)} object(s)")
                else:
                    self.editor.set_selected_object(None)
                self.update()
                return

            if self.is_dragging_object:
                self.is_dragging_object = False
                self.drag_group = []
                self.drag_primary = None
                # A click (no drag) inside an existing group flips scale/rotate.
                if getattr(self, '_maybe_toggle_manip', False):
                    self._maybe_toggle_manip = False
                    if self._group_manip_active():
                        self.manip_mode = ('rotate' if self.manip_mode == 'resize'
                                           else 'resize')
                        self.main_window.show_toast(
                            "Group handles: ROTATE (drag a corner to spin)"
                            if self.manip_mode == 'rotate' else
                            "Group handles: SCALE (drag a handle to resize)")
                        action_taken = False   # nothing moved; don't stack an undo
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
        # Capture the exact position
        click_pos = event.pos()
        
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

        # Check if we clicked on a brush for brush-specific options using click_pos
        clicked_brush = self.get_brush_at(click_pos)
        select_inside_action = None
        
        if clicked_brush:
            select_inside_action = menu.addAction("Select Inside")
            menu.addSeparator()
        
        # Standard Things
        add_light_action = menu.addAction("Light")
        add_player_start_action = menu.addAction("PlayerStart")
        add_pickup_action = menu.addAction("Pickup")
        add_monster_action = menu.addAction("Monster")
        add_speaker_action = menu.addAction("Speaker")
        add_logic_spawner_action = menu.addAction("Spawner")
        add_levelchanger_action = menu.addAction("LevelChanger")
        
        menu.addSeparator()
        
        # Advanced / Import
        # add_model_action = menu.addAction("Model...")
        
        # Logic Menu Sub-section
        menu.addSeparator()
        logic_menu = menu.addMenu("Logic Entities")
        add_logic_relay_action = logic_menu.addAction("LogicRelay")
        add_logic_timer_action = logic_menu.addAction("LogicTimer")
        add_logic_gate_action = logic_menu.addAction("LogicGate")
        add_logic_command_action = logic_menu.addAction("LogicCommand")
        add_logic_keyvalue_action = logic_menu.addAction("KeyValue Store")

        # Node / Special submenu
        ai_menu = menu.addMenu("Nodes")
        add_logic_camera_action = ai_menu.addAction("_Camera")
        add_path_node_action = ai_menu.addAction("PathNode")

        # Portal submenu
        portal_menu = menu.addMenu("Portal")
        add_portal_action = portal_menu.addAction("Portal (single)")
        add_portal_pair_action = portal_menu.addAction("Portal Pair (linked)")
        add_portal_pair_action.setToolTip(
            "Create two portals already cross-linked and facing each other"
        )

        # Open the menu using the captured position
        action = menu.exec_(self.mapToGlobal(click_pos))
        
        if action is None:
            return
        
        # Handle Select Inside
        if select_inside_action and action == select_inside_action:
            self.select_brushes_inside(clicked_brush)
            return
        
        # Calculate World Position for new object using the captured position
        world_pos = self.snap_to_grid(self.screen_to_world(click_pos))
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        pos_3d = [0, 40, 0]
        pos_3d[ax_map[ax1]] = world_pos.x()
        pos_3d[ax_map[ax2]] = world_pos.y()
        if self.view_type == 'top':
            pos_3d[1] = 40

        new_thing = None
        
        # Handle Object Creation
        if action == add_light_action: 
            new_thing = Light(pos=pos_3d)
        elif action == add_player_start_action: 
            new_thing = PlayerStart(pos=pos_3d)
        elif action == add_pickup_action: 
            new_thing = Pickup(pos=pos_3d)
        elif action == add_speaker_action: 
            new_thing = Speaker(pos=pos_3d)
        elif action == add_levelchanger_action:
            new_thing = LevelChanger(pos=pos_3d)
            new_thing.properties['target_map'] = ""
        
        # Logic Entities
        elif action == add_logic_relay_action: new_thing = LogicRelay(pos=pos_3d)
        elif action == add_logic_timer_action: new_thing = LogicTimer(pos=pos_3d)
        elif action == add_logic_command_action: new_thing = LogicCommand(pos=pos_3d)
        elif action == add_monster_action:
            new_thing = Monster(pos=pos_3d)
        elif action == add_logic_gate_action:
            new_thing = LogicGate(pos=pos_3d)
            new_thing.properties['logic_type'] = 'AND' 
        elif action == add_logic_keyvalue_action:
            new_thing = LogicKeyValueStore(pos=pos_3d)
            new_thing.properties['initial_data'] = {}
        elif action == add_logic_camera_action:
            new_thing = LogicCamera(pos=pos_3d)
        elif action == add_logic_spawner_action:
            new_thing = LogicSpawner(pos=pos_3d)

        # AI / Navigation entities
        elif action == add_path_node_action:
            new_thing = PathNode(pos=pos_3d)

        # Portal
        elif action == add_portal_action:
            new_thing = Portal(pos=pos_3d)
            new_thing.properties['rotation'] = [0.0, 0.0, 0.0]

        elif action == add_portal_pair_action:
            # Create portal A at the clicked position
            pa = Portal(pos=list(pos_3d))
            pa.properties['rotation'] = [0.0, 0.0, 0.0]
            pa.properties['angle'] = 0.0

            # Create portal B 256 units away, facing back toward A
            pb_pos = list(pos_3d)
            pb_pos[0] += 256
            pb = Portal(pos=pb_pos)
            pb.properties['rotation'] = [180.0, 0.0, 0.0]
            pb.properties['angle'] = 180.0

            # Name pa first, then add pb to state so its name uniqueness check
            # correctly avoids colliding with pa's name.
            self._ensure_object_name(pa)
            self.editor.state.things.append(pb)
            self._ensure_object_name(pb)

            # Cross-link the pair
            pa.properties['portal_target'] = pb.properties['name']
            pb.properties['portal_target'] = pa.properties['name']

            # pa goes through the standard finalize path (append + select);
            # pb is already in state above.
            new_thing = pa
        
        # Model
        elif action == add_model_action:
            filepath, _ = QFileDialog.getOpenFileName(self, "Select OBJ Model", "assets/models", "OBJ Files (*.obj)")
            if filepath:
                try:
                    rel_path = os.path.relpath(filepath, "assets")
                    if rel_path.startswith(".."): rel_path = filepath
                    else: rel_path = os.path.join("assets", rel_path)
                except Exception:
                    rel_path = filepath
                
                new_thing = Model(pos=pos_3d)
                new_thing.properties['model_path'] = rel_path

        # Finalize Creation
        if new_thing:
            self.main_window.save_state()
            self.editor.state.things.append(new_thing)
            self.editor.set_selected_object(new_thing)
            # Focus the Properties tab when creating a new thing
            if hasattr(self.main_window, 'properties_tab_widget'):
                self._focus_properties_tab()
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

    def _translate_object_2d(self, obj, d1, d2, i1, i2):
        """Move a single brush or entity by (d1, d2) along the two view axes.

        Handles all three storage shapes seen in the scene: angled brushes with
        a world-space plane set (moved via ``translate_brush`` so geometry keeps
        up), plain box brushes (dict ``pos``), and Things (whose ``pos`` may be a
        list or a glm vector — normalised to a list to stay JSON-serialisable).
        """
        if isinstance(obj, dict):
            if bg.brush_has_geometry(obj):
                delta = [0.0, 0.0, 0.0]
                delta[i1] = d1
                delta[i2] = d2
                bg.translate_brush(obj, delta)
            else:
                pos = obj['pos']
                pos[i1] += d1
                pos[i2] += d2
        else:
            pos = obj.pos
            if not isinstance(pos, list):
                # glm vector or tuple — copy to a list so it stays serialisable.
                pos = [pos[0], pos[1], pos[2]]
                obj.pos = pos
            pos[i1] += d1
            pos[i2] += d2

    def select_brushes_inside(self, container_brush):
        """Select every brush and entity enclosed by the drawn box, then delete
        the box itself.

        Containment is tested in the plane of *this* 2D view (the two visible
        axes) rather than in full 3D.  The box the user drags out is only a thin
        slab on the third axis, so a strict 3D test would reject entities that
        sit at a different depth even though they clearly fall inside the box
        on screen.  Entities (Things) are point objects, so an entity counts as
        inside when its centre lies within the box; brushes count as inside
        when their footprint is fully enclosed.
        """
        self.main_window.save_state()

        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        i1, i2 = ax_map[ax1], ax_map[ax2]

        # Container bounds in the view plane.
        c_pos = container_brush['pos']
        c_size = container_brush['size']
        c_min1, c_max1 = c_pos[i1] - c_size[i1] / 2, c_pos[i1] + c_size[i1] / 2
        c_min2, c_max2 = c_pos[i2] - c_size[i2] / 2, c_pos[i2] + c_size[i2] / 2

        inside = []

        # Brushes: fully enclosed footprint.
        for brush in self.editor.state.brushes:
            if brush is container_brush:
                continue
            if brush.get('hidden', False):
                continue

            b_pos = brush['pos']
            b_size = brush['size']
            b_min1, b_max1 = b_pos[i1] - b_size[i1] / 2, b_pos[i1] + b_size[i1] / 2
            b_min2, b_max2 = b_pos[i2] - b_size[i2] / 2, b_pos[i2] + b_size[i2] / 2

            if (b_min1 >= c_min1 and b_max1 <= c_max1 and
                    b_min2 >= c_min2 and b_max2 <= c_max2):
                inside.append(brush)

        # Entities (Things): centre inside the box.
        for thing in self.editor.state.things:
            if thing.properties.get('hidden', False):
                continue
            p = thing.pos
            if (c_min1 <= p[i1] <= c_max1 and c_min2 <= p[i2] <= c_max2):
                inside.append(thing)

        # The box was only a lasso — remove it.
        if container_brush in self.editor.state.brushes:
            self.editor.state.brushes.remove(container_brush)

        if inside:
            self.editor.set_selected_objects(inside)
            if hasattr(self.main_window, 'show_toast'):
                self.main_window.show_toast(f"Selected {len(inside)} object(s) inside box")
        else:
            self.editor.set_selected_object(None)
            if hasattr(self.main_window, 'show_toast'):
                self.main_window.show_toast("No objects inside box", is_error=True)

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
        
        # Default zoom behavior — zoom toward the cursor (Radiant/Hammer style)
        # so the world point under the mouse stays fixed on screen.
        factor = 1.25 if delta > 0 else 0.8
        self.zoom_at(event.pos(), factor)

    def get_object_at(self, screen_pos, highlight_locked=False, cycle=False):
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
            # Use sprite size for accurate hit detection so entities on brushes
            # are always selectable when clicked on their visible sprite.
            if not is_hit:
                w_pos = QPointF(thing.pos[ax_map[ax1]], thing.pos[ax_map[ax2]])
                s_pos = self.world_to_screen(w_pos)

                # Determine hit size based on the thing's actual visual representation
                if isinstance(thing, PathNode):
                    # PathNode draws as a 20px square (half=10)
                    hit_half = 10
                elif hasattr(thing, 'get_icon_pixmap'):
                    pixmap = thing.get_icon_pixmap()
                    hit_half = max(pixmap.size().width(), pixmap.size().height()) / 2.0 if pixmap else 12
                elif hasattr(thing, 'get_instance_pixmap'):
                    pixmap = thing.get_instance_pixmap()
                    hit_half = max(pixmap.size().width(), pixmap.size().height()) / 2.0 if pixmap else 12
                else:
                    hit_half = 12  # Default fallback

                if abs(screen_pos.x() - s_pos.x()) <= hit_half and abs(screen_pos.y() - s_pos.y()) <= hit_half:
                    is_hit = True

            if is_hit:
                # Track locked things separately if setting is enabled
                if locked_not_selectable and thing.properties.get('lock', False):
                    locked_at_pos.append(thing)
                else:
                    candidates.append(thing)
        
        for brush in reversed(self.editor.state.brushes):
            if brush.get('hidden', False): 
                continue

            # FIX: Use .get() to avoid KeyError if 'pos' or 'size' are missing
            pos = brush.get('pos')
            size = brush.get('size')
            
            if pos is None or size is None:
                continue

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

        if not candidates:
            return None

        # Alt-click walks through stacked objects at the cursor; a plain click
        # always takes the topmost so selection stays predictable.
        if cycle:
            current_selection = self.editor.state.selected_object
            if current_selection in candidates:
                idx = candidates.index(current_selection)
                return candidates[(idx + 1) % len(candidates)]

        return candidates[0]

    def get_handle_at(self, screen_pos):
        brush = self.editor.state.selected_object
        if not isinstance(brush, dict) or brush.get('lock', False): 
            return -1
        
        # FIX: Use .get() to prevent KeyError if 'pos' or 'size' are missing
        pos = brush.get('pos')
        size = brush.get('size')
        
        # If the brush is missing essential data, we cannot calculate handles
        if pos is None or size is None:
            return -1
            
        ax1, ax2 = self.get_axes()
        ax_map = {'x': 0, 'y': 1, 'z': 2}
        
        # Accessing indices is now safe because we verified pos/size exist
        w_pos = QPointF(pos[ax_map[ax1]] - size[ax_map[ax1]]/2, 
                        pos[ax_map[ax2]] - size[ax_map[ax2]]/2)
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

        is_front_view = self.view_type in ['front', 'side']
        
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

        # Angled brushes: scale the convex geometry to the new handle box (its
        # slope/shape is preserved, just refitted) rather than only resizing
        # the AABB — otherwise the plane set would be left behind, exactly like
        # the move bug.  The depth axis is untouched (handles only edit ix1/ix2).
        if bg.brush_has_geometry(brush):
            new_lo = [old_pos[k] - old_size[k] / 2 for k in range(3)]
            new_hi = [old_pos[k] + old_size[k] / 2 for k in range(3)]
            new_lo[ix1], new_hi[ix1] = min_x, min_x + new_size_x
            new_lo[ix2], new_hi[ix2] = min_y, min_y + new_size_y
            if bg.fit_brush_to_bounds(brush, new_lo, new_hi):
                return

        brush['pos'][ix1] = min_x + new_size_x / 2
        brush['pos'][ix2] = min_y + new_size_y / 2
        brush['size'][ix1] = new_size_x
        brush['size'][ix2] = new_size_y

    def zoom_at(self, screen_pos, factor):
        """Scale the zoom by ``factor`` while keeping the world point currently
        under ``screen_pos`` pinned to that same pixel (zoom-to-cursor)."""
        old_zoom = self.zoom_factor
        new_zoom = max(0.02, min(200.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-9:
            return
        before = self.screen_to_world(screen_pos)   # world point under cursor
        self.zoom_factor = new_zoom
        after = self.screen_to_world(screen_pos)     # where it landed after zoom
        # Shift the pan so the point doesn't move on screen.  screen_y is flipped
        # for front/side views, but screen_to_world already accounts for that, so
        # the pan correction is a plain difference in world space.
        self.pan_offset += QPointF(before.x() - after.x(), before.y() - after.y())
        self.update()

    def zoom_in(self):
        # Zoom toward the view centre (used by buttons/keys without a cursor).
        self.zoom_at(QPointF(self.width() / 2, self.height() / 2), 1.25)

    def zoom_out(self):
        self.zoom_at(QPointF(self.width() / 2, self.height() / 2), 0.8)