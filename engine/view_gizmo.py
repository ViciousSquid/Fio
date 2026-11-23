"""The 3D view's transform gizmo: hover, drag and drawing.

The shape and the maths are :mod:`editor.transform_gizmo`'s; this is the part
that lives in the view. It is drawn with the frame's QPainter after the scene
(QtGameView._paint_frame), so it is never hidden inside the object it moves
and every stroke is triangles -- no GL line is wider than one pixel.

A drag is applied live, one undo step for the whole of it: Move and Rotate
step the selection by the change since the last mouse event, Scale is set
from where the drag started every time. Ctrl inverts grid snapping while
held; Esc or a right click puts everything back where it was.
"""

from __future__ import annotations

import copy
import functools
import math

import glm
import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainterPath, QPen, QPolygonF
from PyQt5.QtWidgets import QApplication

from editor import transform_gizmo as tg
from engine import brush_geometry, brush_placement
from engine.change_journal import touch

#: Blender's active-tool blue, for the mode chips.
CHIP_ACTIVE = (71, 114, 179)


def _qc(rgb, alpha=255):
    c = QColor(*rgb[:3])
    c.setAlpha(int(alpha))
    return c


def _hot(rgb):
    """A hovered handle's colour: lifted towards white, as Blender does."""
    return tuple(int(v + (255 - v) * 0.55) for v in rgb)


class ViewGizmo:
    def __init__(self, view):
        self.view = view
        self.mode = tg.MOVE
        #: The handle under the cursor (None: none).
        self.hover = None
        #: The drag in progress (a dict) or None.
        self.drag = None
        self._chip_rects = []

    # -- state ------------------------------------------------------------

    @property
    def editor(self):
        return self.view.editor

    @property
    def dragging(self):
        return self.drag is not None

    def active(self):
        """Whether the gizmo is shown and can be grabbed."""
        view = self.view
        if view.play_mode or self.editor.primary_selection() is None:
            return False
        if (view.face_mode_active or view.terrain_sculpt_active
                or view.decal_tool.active or view.components._component_mode_active()):
            return False
        if getattr(self.editor, 'clip_mode', False):
            return False
        return True

    def set_mode(self, mode):
        if mode not in tg.MODES or mode == self.mode:
            return
        if self.dragging:
            self.cancel()
        self.mode = mode
        self.hover = None
        self.view.update()

    def cycle_mode(self):
        self.set_mode(tg.MODES[(tg.MODES.index(self.mode) + 1) % len(tg.MODES)])
        toast = getattr(self.editor, 'show_toast', None)
        if toast is not None:
            toast("Gizmo: %s  (R to change)" % tg.MODE_NAMES[self.mode])

    # -- geometry ---------------------------------------------------------

    def _project_fn(self):
        view = self.view
        pv = view.projection_matrix * view.view_matrix
        w, h = float(view.width()), float(view.height())

        def project(p):
            c = pv * glm.vec4(float(p[0]), float(p[1]), float(p[2]), 1.0)
            if c.w <= 1e-6:
                return None
            return ((c.x / c.w * 0.5 + 0.5) * w, (1.0 - (c.y / c.w * 0.5 + 0.5)) * h)
        return project

    def _camera(self):
        right, up, back = tg.camera_axes(self.view.view_matrix)
        pos = self.view.camera.pos
        return np.array([pos.x, pos.y, pos.z], dtype=float), right, up, back

    def centre(self):
        """Where the gizmo sits: the pivot while rotating, otherwise the
        centre of the selection's bounds (Blender's bounding-box centre)."""
        if self.drag is not None and self.drag.get('pivot') is not None:
            return self.drag['pivot']
        centre = self.editor.selection_centre()
        if centre is None:
            return None
        return np.asarray(centre, dtype=float)

    def layout(self):
        centre = self.centre()
        if centre is None:
            return None
        cam, right, up, _ = self._camera()
        return tg.layout(self.mode, centre, self._project_fn(), cam, right, up)

    def _ray(self, x, y):
        o, d = self.view.picking.get_ray_from_mouse(x, y)
        return np.array([o.x, o.y, o.z], dtype=float), np.array([d.x, d.y, d.z], dtype=float)

    def _group(self):
        """What a drag moves: the unlocked selection and its group mates."""
        editor = self.editor
        chosen = [o for o in editor.selected_objects_list()
                  if not (o.get('lock', False) if isinstance(o, dict)
                          else o.properties.get('lock', False))]
        return editor.movable_with_mates(chosen)

    def _snap_on(self):
        snap_on = bool(self.editor.snap_to_grid_enabled())
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            snap_on = not snap_on
        return snap_on

    def _grid(self):
        return float(self.editor.grid_size_spinbox.value())

    # -- mouse ------------------------------------------------------------

    def hover_at(self, x, y):
        """Update the hovered handle; True when it changed (repaint)."""
        if self.dragging:
            return False
        handle = None
        if self.active():
            handle = self._chip_at(x, y) or tg.hit_test(self.layout(), (x, y))
        if handle != self.hover:
            self.hover = handle
            return True
        return False

    def _chip_at(self, x, y):
        for rect, mode in self._chip_rects:
            if rect.contains(QPointF(x, y)):
                return ('chip', mode)
        return None

    def press(self, x, y):
        """A left press: True when the gizmo took it."""
        if not self.active():
            return False
        chip = self._chip_at(x, y)
        if chip is not None:
            self.set_mode(chip[1])
            return True
        lay = self.layout()
        handle = tg.hit_test(lay, (x, y))
        if handle is None:
            return False
        drag = self._start(handle, lay, x, y)
        if drag is None:
            return False
        self.editor.save_state()            # one undo step for the drag
        self.drag = drag
        self.hover = handle
        self.view.update()
        return True

    def _start(self, handle, lay, x, y):
        kind = handle[0]
        ray_o, ray_d = self._ray(x, y)
        centre = lay.centre.copy()
        drag = {'handle': handle, 'mouse0': (x, y), 'last': (x, y),
                'centre0': centre, 'centre0_px': lay.centre_px,
                'to_camera': lay.to_camera.copy(), 'right': lay.camera_right,
                'up': lay.camera_up, 'radius': lay.radius_world,
                'group': self._group(), 'readout': ''}
        if not drag['group']:
            return None
        drag['restore'] = _snapshot(self.editor.with_group_mates(
            list(self.editor.selected_objects_list()) + drag['group']))
        if kind == 'move_axis':
            t0 = tg.ray_axis_param(ray_o, ray_d, centre, tg.AXES[handle[1]])
            if t0 is None:
                return None
            drag.update(t0=t0, applied=np.zeros(3))
        elif kind in ('move_plane', 'move_view'):
            normal = tg.AXES[handle[1]] if kind == 'move_plane' else lay.to_camera
            p0 = tg.ray_plane(ray_o, ray_d, centre, normal)
            if p0 is None:
                return None
            drag.update(p0=p0, normal=np.array(normal), applied=np.zeros(3))
        elif kind == 'rotate_axis':
            axis = tg.AXES[handle[1]]
            drag.update(pivot=centre, axis=axis, raw=0.0, applied=0.0)
            if abs(float(lay.to_camera @ axis)) > 0.3:
                p0 = tg.ray_plane(ray_o, ray_d, centre, axis)
                if p0 is None:
                    return None
                drag.update(method='plane', prev=p0 - centre, start_vec=p0 - centre)
            else:
                # Seen edge-on the ring's plane is a line: drag along the
                # ring's tangent at the grabbed point instead.
                grab = self._nearest_ring_point(lay, handle[1], x, y)
                tangent = np.cross(axis, grab - centre)
                project = self._project_fn()
                a = project(grab)
                b = project(grab + tangent / max(np.linalg.norm(tangent), 1e-9) * lay.radius_world * 0.1)
                if a is None or b is None:
                    return None
                t = np.array([b[0] - a[0], b[1] - a[1]])
                n = np.linalg.norm(t)
                if n < 1e-6:
                    return None
                drag.update(method='tangent', tangent_px=t / n, start_vec=grab - centre)
        elif kind == 'rotate_view':
            cx, cy = lay.centre_px
            drag.update(pivot=centre, axis=lay.to_camera.copy(), raw=0.0, applied=0.0,
                        method='screen', prev=math.atan2(-(y - cy), x - cx))
        elif kind == 'rotate_trackball':
            drag.update(pivot=centre, turns=[], total=0.0)
        elif kind in ('scale_axis', 'scale_plane', 'scale_uniform'):
            cx, cy = lay.centre_px
            if kind == 'scale_axis':
                start, tip, _ = lay.axes[handle[1]]
                a = np.array([tip[0] - cx, tip[1] - cy])
                a /= max(np.linalg.norm(a), 1e-9)
                d0 = float(np.array([x - cx, y - cy]) @ a)
                drag.update(axis_px=a)
            else:
                d0 = math.hypot(x - cx, y - cy)
            if abs(d0) < 4.0:
                return None
            drag.update(d0=d0, snapshot=self._scale_snapshot(drag['group']),
                        factors=(1.0, 1.0, 1.0))
        else:
            return None
        return drag

    def _nearest_ring_point(self, lay, axis, x, y):
        a, b = tg.plane_axes(axis)
        project = self._project_fn()
        best = None
        for k in range(96):
            t = 2.0 * math.pi * k / 96
            world = lay.centre + (tg.AXES[a] * math.cos(t) + tg.AXES[b] * math.sin(t)) * lay.radius_world
            px = project(world)
            if px is None:
                continue
            d = math.hypot(px[0] - x, px[1] - y)
            if best is None or d < best[0]:
                best = (d, world)
        return best[1]

    def drag_to(self, x, y):
        """Follow the cursor during a drag."""
        drag = self.drag
        if drag is None:
            return
        kind = drag['handle'][0]
        if kind.startswith('move'):
            self._drag_move(drag, x, y)
        elif kind.startswith('rotate'):
            self._drag_rotate(drag, x, y)
        else:
            self._drag_scale(drag, x, y)
        drag['last'] = (x, y)
        refresh = getattr(self.editor, 'refresh_views', None)
        if refresh is not None:
            refresh()
        else:
            self.view.update()

    def release(self):
        """The left button came up: keep what the drag did."""
        drag = self.drag
        if drag is None:
            return False
        self.drag = None
        if 'snapshot' in drag:
            # A box brush scaled as a plane set is a box again.
            for obj, lo0, hi0, geo, pos0, size0 in drag['snapshot']['brushes']:
                if geo is None:
                    self.editor.state.simplify_brush_geometry(obj)
        if not self._changed(drag):
            self.editor.state.discard_last_checkpoint()
        else:
            self.editor.unsaved_changes = True
            self.editor.state.mark_lighting_dirty()
            primary = self.editor.primary_selection()
            editor_panel = getattr(self.editor, 'property_editor', None)
            if primary is not None and editor_panel is not None:
                editor_panel.set_object(primary)
        refresh = getattr(self.editor, 'refresh_views', None)
        if refresh is not None:
            refresh()
        self.view.update()
        return True

    def cancel(self):
        """Esc or a right click mid-drag: everything back where it was."""
        drag = self.drag
        if drag is None:
            return False
        # Exactly as it was: stepping back by the inverse would leave float
        # dust, and a box brush that was turned would stay an angled brush.
        _restore(drag['restore'])
        self.drag = None
        self.editor.state.discard_last_checkpoint()
        refresh = getattr(self.editor, 'refresh_views', None)
        if refresh is not None:
            refresh()
        self.view.update()
        return True

    @staticmethod
    def _changed(drag):
        kind = drag['handle'][0]
        if kind.startswith('move'):
            return bool(np.any(drag['applied']))
        if kind == 'rotate_trackball':
            return bool(drag['turns'])
        if kind.startswith('rotate'):
            return abs(drag['applied']) > 1e-9
        return any(abs(f - 1.0) > 1e-9 for f in drag['factors'])

    # -- move -------------------------------------------------------------

    def _drag_move(self, drag, x, y):
        ray_o, ray_d = self._ray(x, y)
        kind, centre = drag['handle'][0], drag['centre0']
        grid = self._grid() if self._snap_on() else 0.0
        if kind == 'move_axis':
            i = drag['handle'][1]
            t = tg.ray_axis_param(ray_o, ray_d, centre, tg.AXES[i])
            if t is None:
                return
            delta = tg.AXES[i] * tg.snap(t - drag['t0'], grid)
        else:
            p = tg.ray_plane(ray_o, ray_d, centre, drag['normal'])
            if p is None:
                return
            delta = p - drag['p0']
            if kind == 'move_plane':
                delta[drag['handle'][1]] = 0.0
            delta = np.array([tg.snap(float(v), grid) for v in delta])
        step = delta - drag['applied']
        if np.any(np.abs(step) > 1e-9):
            self._translate(drag['group'], step)
            drag['applied'] = delta
        parts = ["%s %g" % (tg.AXIS_NAMES[k], round(float(drag['applied'][k]), 3))
                 for k in range(3) if abs(drag['applied'][k]) > 1e-9]
        drag['readout'] = "Move   " + ("   ".join(parts) if parts else "0")

    def _translate(self, group, delta):
        delta = [float(v) for v in delta]
        for obj in group:
            self.editor._translate_object(obj, delta)

    # -- rotate -----------------------------------------------------------

    def _drag_rotate(self, drag, x, y):
        kind = drag['handle'][0]
        if kind == 'rotate_trackball':
            lx, ly = drag['last']
            dx, dy = x - lx, y - ly
            length = math.hypot(dx, dy)
            if length < 1e-6:
                return
            axis = drag['up'] * dx + drag['right'] * dy
            axis /= np.linalg.norm(axis)
            angle = math.degrees(length / tg.RADIUS_PX)
            self._rotate(drag, angle, axis)
            drag['turns'].append((axis, angle))
            drag['total'] += angle
            drag['readout'] = "Rotate   trackball  %.0f°" % drag['total']
            return
        if drag['method'] == 'plane':
            ray_o, ray_d = self._ray(x, y)
            p = tg.ray_plane(ray_o, ray_d, drag['pivot'], drag['axis'])
            if p is not None:
                v = p - drag['pivot']
                if np.linalg.norm(v) > 1e-6:
                    drag['raw'] += tg.signed_angle(drag['prev'], v, drag['axis'])
                    drag['prev'] = v
        elif drag['method'] == 'tangent':
            mx, my = drag['mouse0']
            along = float(np.array([x - mx, y - my]) @ drag['tangent_px'])
            drag['raw'] = math.degrees(along / tg.RADIUS_PX)
        else:
            cx, cy = drag['centre0_px']
            a = math.atan2(-(y - cy), x - cx)
            drag['raw'] += tg.wrap_degrees(math.degrees(a - drag['prev']))
            drag['prev'] = a
        step_deg = self._rotate_step() if self._snap_on() else 0.0
        total = tg.snap(drag['raw'], step_deg)
        step = total - drag['applied']
        if abs(step) > 1e-9:
            self._rotate(drag, step, drag['axis'])
            drag['applied'] = total
        about = ("view" if kind == 'rotate_view'
                 else tg.AXIS_NAMES[drag['handle'][1]])
        drag['readout'] = "Rotate   %g°   about %s" % (round(drag['applied'], 2), about)

    def _rotate_step(self):
        from editor.view_2d import rotate_step_for_grid
        return rotate_step_for_grid(self._grid())

    def _rotate(self, drag, angle, axis):
        """Turn the selection *angle* degrees about *axis* through the pivot:
        brushes and entity positions (the 2D tool's rotation), and the
        facing of models and of entities with a heading."""
        axis = [float(v) for v in axis]
        pivot = [float(v) for v in drag['pivot']]
        self.editor.apply_rotation_to_selection(angle, axis, undoable=False, pivot=pivot)
        for obj in self.editor.with_group_mates(self.editor.selected_objects_list()):
            if isinstance(obj, dict):
                continue
            props = obj.properties
            if props.get('model_path') and 'rotation' in props:
                props['rotation'] = tg.turn_model(props.get('rotation') or (0, 0, 0),
                                                  axis, angle)
                touch(obj)
            elif props.get('type') != 'portal' and 'angle' in props and abs(axis[1]) > 0.999:
                try:
                    heading = float(props.get('angle') or 0.0)
                except (TypeError, ValueError):
                    heading = 0.0
                props['angle'] = round((heading + angle * (1 if axis[1] > 0 else -1)) % 360.0, 6)
                touch(obj)

    # -- scale ------------------------------------------------------------

    def _scale_snapshot(self, group):
        brushes, things = [], []
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        for obj in group:
            if isinstance(obj, dict):
                pos = np.asarray(obj.get('pos', (0, 0, 0)), dtype=float)
                size = np.asarray(obj.get('size', (64, 64, 64)), dtype=float)
                b_lo, b_hi = pos - size / 2, pos + size / 2
                geo = (copy.deepcopy(obj['geometry'])
                       if brush_geometry.brush_has_geometry(obj) else None)
                brushes.append((obj, b_lo, b_hi, geo, list(obj.get('pos')), list(obj.get('size'))))
                lo, hi = np.minimum(lo, b_lo), np.maximum(hi, b_hi)
            else:
                pos = np.asarray(obj.pos, dtype=float)
                things.append((obj, pos, copy.deepcopy(obj.properties.get('scale'))))
                if hasattr(obj, 'get_volume_size'):
                    # A light volume: its box is what scaling resizes.
                    half = np.asarray(obj.get_volume_size(), dtype=float) / 2
                    lo, hi = np.minimum(lo, pos - half), np.maximum(hi, pos + half)
                else:
                    lo, hi = np.minimum(lo, pos), np.maximum(hi, pos)
        sizes = {id(obj): list(obj.get_volume_size())
                 for obj, _, _ in things if hasattr(obj, 'get_volume_size')}
        return {'brushes': brushes, 'things': things, 'extent': hi - lo,
                'volume_sizes': sizes}

    def _drag_scale(self, drag, x, y):
        cx, cy = drag['centre0_px']
        kind = drag['handle'][0]
        if kind == 'scale_axis':
            d = float(np.array([x - cx, y - cy]) @ drag['axis_px'])
        else:
            d = math.hypot(x - cx, y - cy)
        f = max(0.01, d / drag['d0'])
        if kind == 'scale_axis':
            axes = (drag['handle'][1],)
        elif kind == 'scale_plane':
            axes = tg.plane_axes(drag['handle'][1])
        else:
            axes = (0, 1, 2)
        snapshot = drag['snapshot']
        factors = [1.0, 1.0, 1.0]
        snap_on = self._snap_on()
        grid = self._grid()
        for k in axes:
            fk = f
            extent = float(snapshot['extent'][k])
            if snap_on and (snapshot['brushes'] or snapshot['volume_sizes']) and extent > 1e-6:
                # Brushes stay on the grid: the selection's size along each
                # scaled axis becomes a whole number of grid squares.
                fk = max(grid, tg.snap(extent * f, grid)) / extent
            elif snap_on:
                fk = max(0.1, tg.snap(f, 0.1))        # Blender's increment
            factors[k] = fk
        self._apply_scale(drag, factors)
        drag['factors'] = tuple(factors)
        if kind == 'scale_uniform' and len(set(round(v, 4) for v in factors)) == 1:
            drag['readout'] = "Scale   ×%.2f" % factors[0]
        else:
            drag['readout'] = "Scale   " + "   ".join(
                "%s ×%.2f" % (tg.AXIS_NAMES[k], factors[k]) for k in axes)

    def _apply_scale(self, drag, factors):
        pivot = drag['centre0']
        f = np.asarray(factors, dtype=float)
        snapshot = drag['snapshot']
        for obj, lo0, hi0, geo, pos0, size0 in snapshot['brushes']:
            lo = pivot + (lo0 - pivot) * f
            hi = pivot + (hi0 - pivot) * f
            # Through brush_geometry, the shape's owner: a box is fitted as
            # a plane set for the drag and goes back to a box on release.
            if not brush_geometry.brush_has_geometry(obj):
                brush_geometry.box_to_geometry(obj)
            brush_geometry.fit_brush_to_bounds(obj, lo.tolist(), hi.tolist())
        for obj, pos0, scale0 in snapshot['things']:
            obj.pos = (pivot + (pos0 - pivot) * f).tolist()
            props = obj.properties
            if hasattr(obj, 'get_volume_size'):
                size0 = snapshot['volume_sizes'][id(obj)]
                props['volume_size'] = [max(obj.MIN_SIZE, size0[k] * float(f[k])) for k in range(3)]
                touch(obj)
                continue
            if props.get('model_path') and scale0 is not None:
                local = tg.local_scale_factors(props.get('rotation') or (0, 0, 0), f)
                if isinstance(scale0, (int, float)) and len(set(local)) == 1:
                    props['scale'] = float(scale0) * local[0]
                else:
                    base = ([float(scale0)] * 3 if isinstance(scale0, (int, float))
                            else [float(v) for v in scale0])
                    props['scale'] = [base[j] * local[j] for j in range(3)]
                touch(obj)

    # -- drawing ----------------------------------------------------------

    def paint(self, painter):
        self._chip_rects = []
        if not self.active():
            return
        lay = self.layout()
        painter.save()
        painter.setRenderHint(painter.Antialiasing, True)
        try:
            if self.drag is not None:
                self._paint_drag_outline(painter)
                self._paint_constraint(painter, lay)
            if lay is not None:
                {tg.MOVE: self._paint_move, tg.ROTATE: self._paint_rotate,
                 tg.SCALE: self._paint_scale}[self.mode](painter, lay)
            self._paint_chips(painter)
            if self.drag is not None and self.drag['readout']:
                self._paint_readout(painter, self.drag['readout'])
        finally:
            painter.restore()

    def _shows(self, handle):
        """While dragging only the handle being dragged is drawn."""
        return self.drag is None or self.drag['handle'] == handle

    def _colour(self, handle, rgb):
        if self.hover == handle or (self.drag is not None and self.drag['handle'] == handle):
            return _hot(rgb)
        return rgb

    @staticmethod
    def _pen(rgb, width, alpha=255, style=Qt.SolidLine):
        pen = QPen(_qc(rgb, alpha), width)
        pen.setStyle(style)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _paint_arrow(self, painter, start, tip, rgb, alpha, width):
        painter.setPen(self._pen(rgb, width, alpha * 255))
        sx, sy = start
        tx, ty = tip
        dx, dy = tx - sx, ty - sy
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return
        ux, uy = dx / length, dy / length
        head = min(16.0, length * 0.45)
        base = (tx - ux * head, ty - uy * head)
        painter.drawLine(QPointF(sx, sy), QPointF(*base))
        half = head * 0.38
        cone = QPolygonF([QPointF(tx, ty),
                          QPointF(base[0] - uy * half, base[1] + ux * half),
                          QPointF(base[0] + uy * half, base[1] - ux * half)])
        painter.setPen(Qt.NoPen)
        painter.setBrush(_qc(rgb, alpha * 255))
        painter.drawPolygon(cone)

    def _paint_planes(self, painter, lay, kind):
        for normal, corners in lay.planes.items():
            handle = (kind, normal)
            if not self._shows(handle):
                continue
            rgb = self._colour(handle, tg.AXIS_COLOURS[normal])
            hot = rgb != tg.AXIS_COLOURS[normal]
            painter.setPen(self._pen(rgb, 1.0, 230))
            painter.setBrush(_qc(rgb, 200 if hot else 120))
            painter.drawPolygon(QPolygonF([QPointF(*c) for c in corners]))

    def _paint_move(self, painter, lay):
        cx, cy = lay.centre_px
        self._paint_planes(painter, lay, 'move_plane')
        for i, (start, tip, alpha) in sorted(lay.axes.items(), key=lambda kv: -kv[1][2]):
            handle = ('move_axis', i)
            if alpha <= 0.0 or not self._shows(handle):
                continue
            hot = self.hover == handle or (self.drag and self.drag['handle'] == handle)
            self._paint_arrow(painter, start, tip, self._colour(handle, tg.AXIS_COLOURS[i]),
                              alpha, 3.0 if hot else 2.2)
        handle = ('move_view',)
        if self._shows(handle):
            rgb = self._colour(handle, tg.WHITE)
            painter.setPen(self._pen(rgb, 2.6 if rgb != tg.WHITE else 1.8, 255))
            painter.setBrush(Qt.NoBrush)
            r = tg.CENTRE_RING * tg.RADIUS_PX
            painter.drawEllipse(QPointF(cx, cy), r, r)

    def _paint_rotate(self, painter, lay):
        cx, cy = lay.centre_px
        drag = self.drag
        handle = ('rotate_trackball',)
        if self.hover == handle or (drag is not None and drag['handle'] == handle):
            painter.setPen(Qt.NoPen)
            painter.setBrush(_qc(tg.WHITE, 28))
            painter.drawEllipse(QPointF(cx, cy), tg.RADIUS_PX, tg.RADIUS_PX)
        handle = ('rotate_view',)
        if self._shows(handle) or (drag is not None and drag['handle'] == ('rotate_trackball',)):
            rgb = self._colour(handle, tg.WHITE)
            painter.setPen(self._pen(rgb, 2.6 if rgb != tg.WHITE else 1.6, 230))
            painter.setBrush(Qt.NoBrush)
            r = tg.VIEW_RING * tg.RADIUS_PX
            painter.drawEllipse(QPointF(cx, cy), r, r)
        for i, points in lay.rings.items():
            handle = ('rotate_axis', i)
            if not self._shows(handle):
                continue
            rgb = self._colour(handle, tg.AXIS_COLOURS[i])
            hot = rgb != tg.AXIS_COLOURS[i]
            dragged = drag is not None and drag['handle'] == handle
            painter.setPen(self._pen(rgb, 3.2 if hot else 2.4))
            path = QPainterPath()
            pen_down = False
            for (x, y), front in points:
                if front or dragged:          # the dragged ring: all of it
                    if pen_down:
                        path.lineTo(x, y)
                    else:
                        path.moveTo(x, y)
                        pen_down = True
                else:
                    pen_down = False
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        if drag is not None and drag['handle'][0] in ('rotate_axis', 'rotate_view'):
            self._paint_dial(painter, lay, drag)

    def _paint_dial(self, painter, lay, drag):
        """Blender's dial: a wedge from where the drag started to now."""
        angle = drag['applied']
        if abs(angle) < 1e-6:
            return
        kind = drag['handle'][0]
        cx, cy = lay.centre_px
        if kind == 'rotate_view':
            rgb = tg.WHITE
            r = tg.VIEW_RING * tg.RADIUS_PX
            mx, my = drag['mouse0']
            start = math.degrees(math.atan2(-(my - cy), mx - cx))
            painter.setPen(self._pen(rgb, 1.2, 200))
            painter.setBrush(_qc(rgb, 50))
            painter.drawPie(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                            int(start * 16), int(angle * 16))
            return
        rgb = _hot(tg.AXIS_COLOURS[drag['handle'][1]])
        project = self._project_fn()
        v0 = drag['start_vec']
        v0 = v0 / max(np.linalg.norm(v0), 1e-9) * lay.radius_world
        steps = max(4, int(abs(angle) / 3))
        poly = [QPointF(cx, cy)]
        for k in range(steps + 1):
            p = project(lay.centre + tg.rotation_matrix(drag['axis'], angle * k / steps) @ v0)
            if p is not None:
                poly.append(QPointF(*p))
        painter.setPen(self._pen(rgb, 1.2, 220))
        painter.setBrush(_qc(rgb, 70))
        painter.drawPolygon(QPolygonF(poly))

    def _paint_scale(self, painter, lay):
        cx, cy = lay.centre_px
        self._paint_planes(painter, lay, 'scale_plane')
        for i, (start, tip, alpha) in sorted(lay.axes.items(), key=lambda kv: -kv[1][2]):
            handle = ('scale_axis', i)
            if alpha <= 0.0 or not self._shows(handle):
                continue
            rgb = self._colour(handle, tg.AXIS_COLOURS[i])
            hot = rgb != tg.AXIS_COLOURS[i]
            if self.drag is not None and self.drag['handle'] == handle:
                # As in Blender, the dragged handle stretches with the scale.
                stretched = self._project_fn()(
                    lay.centre + tg.AXES[i] * lay.radius_world * self.drag['factors'][i])
                if stretched is not None:
                    tip = stretched
            painter.setPen(self._pen(rgb, 3.0 if hot else 2.2, alpha * 255))
            painter.drawLine(QPointF(*start), QPointF(*tip))
            s = 6.0 if hot else 5.0
            painter.setPen(Qt.NoPen)
            painter.setBrush(_qc(rgb, alpha * 255))
            painter.drawRect(QRectF(tip[0] - s, tip[1] - s, 2 * s, 2 * s))
        handle = ('scale_uniform',)
        if self._shows(handle):
            rgb = self._colour(handle, tg.WHITE)
            painter.setPen(self._pen(rgb, 2.6 if rgb != tg.WHITE else 1.6, 230))
            painter.setBrush(Qt.NoBrush)
            r = tg.VIEW_RING * tg.RADIUS_PX
            if self.drag is not None and self.drag['handle'] == handle:
                r *= max(self.drag['factors'])
            painter.drawEllipse(QPointF(cx, cy), r, r)

    def _paint_constraint(self, painter, lay):
        """The dashed line (or lines) a move or scale is held to."""
        drag = self.drag
        kind = drag['handle'][0]
        if kind in ('move_axis', 'scale_axis'):
            axes = (drag['handle'][1],)
        elif kind in ('move_plane', 'scale_plane'):
            axes = tg.plane_axes(drag['handle'][1])
        else:
            return
        project = self._project_fn()
        centre = drag['centre0']
        c = project(centre)
        if c is None:
            return
        span = float(self.view.width() + self.view.height())
        for k in axes:
            tip = project(centre + tg.AXES[k] * drag['radius'])
            if tip is None:
                continue
            dx, dy = tip[0] - c[0], tip[1] - c[1]
            n = math.hypot(dx, dy)
            if n < 1e-6:
                continue
            dx, dy = dx / n * span, dy / n * span
            painter.setPen(self._pen(tg.AXIS_COLOURS[k], 1.4, 210, Qt.DashLine))
            painter.drawLine(QPointF(c[0] - dx, c[1] - dy), QPointF(c[0] + dx, c[1] + dy))

    def _paint_drag_outline(self, painter):
        """Where the dragged brushes are going: their edges in dark orange."""
        project = self._project_fn()
        painter.setPen(self._pen(tg.DRAG_OUTLINE, 2.0, 255))
        painter.setBrush(Qt.NoBrush)
        for obj in self.drag['group']:
            edges = brush_edges(obj) if isinstance(obj, dict) else self._thing_edges(obj)
            for a, b in edges:
                pa, pb = project(a), project(b)
                if pa is not None and pb is not None:
                    painter.drawLine(QPointF(*pa), QPointF(*pb))

    def _thing_edges(self, thing):
        """A dragged entity's box: a model's own bounds, turned and scaled
        as it is drawn; a small cube for anything else."""
        from engine.logic_collision import model_basis
        if hasattr(thing, 'get_bounds') and hasattr(thing, 'get_volume_size'):
            from engine.view_light_volumes import box_edges
            return box_edges(thing)                 # a light volume: its box
        props = thing.properties
        pos = np.asarray(thing.pos, dtype=float)
        lo, hi = np.full(3, -8.0), np.full(3, 8.0)
        basis = np.eye(3)
        path = props.get('model_path')
        if path:
            bounds = model_file_bounds(path)
            if bounds is not None:
                lo, hi = np.asarray(bounds[0], dtype=float), np.asarray(bounds[1], dtype=float)
                scale = props.get('scale', 1.0)
                scale = [float(scale)] * 3 if isinstance(scale, (int, float)) else [float(v) for v in scale]
                basis = model_basis(props.get('rotation') or (0, 0, 0), scale)
        corners = [pos + basis @ np.array((x, y, z))
                   for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        return [(corners[a], corners[b]) for a in range(8) for b in range(a + 1, 8)
                if bin(a ^ b).count('1') == 1]

    def _paint_chips(self, painter):
        """Move / Rotate / Scale, top left: click one, or press R."""
        font = QFont(painter.font())
        font.setPointSizeF(9.0)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()
        x, y = 8.0, 8.0
        h = fm.height() + 8.0
        for mode in tg.MODES:
            text = tg.MODE_NAMES[mode]
            w = fm.horizontalAdvance(text) + 18.0
            rect = QRectF(x, y, w, h)
            on = mode == self.mode
            hot = self.hover == ('chip', mode)
            bg = CHIP_ACTIVE if on else ((70, 70, 70) if hot else (38, 38, 38))
            painter.setPen(Qt.NoPen)
            painter.setBrush(_qc(bg, 235))
            painter.drawRoundedRect(rect, 4.0, 4.0)
            painter.setPen(_qc((255, 255, 255) if on else (205, 205, 205)))
            painter.drawText(rect, Qt.AlignCenter, text)
            self._chip_rects.append((rect, mode))
            x += w + 3.0
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(_qc((170, 170, 170)))
        painter.drawText(QRectF(x + 4.0, y, 40.0, h), Qt.AlignVCenter | Qt.AlignLeft, "R")

    def _paint_readout(self, painter, text):
        font = QFont(painter.font())
        font.setPointSizeF(10.0)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()
        hint = "   Ctrl: snap %s   Esc: cancel" % ("off" if self.editor.snap_to_grid_enabled() else "on")
        w = fm.horizontalAdvance(text + hint) + 20.0
        h = fm.height() + 10.0
        rect = QRectF(8.0, 8.0 + fm.height() + 14.0, w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_qc((20, 20, 20), 210))
        painter.drawRoundedRect(rect, 4.0, 4.0)
        painter.setPen(_qc((255, 255, 255)))
        painter.drawText(rect.adjusted(10, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.setPen(_qc((160, 160, 160)))
        tw = fm.horizontalAdvance(text)
        painter.drawText(rect.adjusted(10 + tw, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, hint)


@functools.lru_cache(maxsize=64)
def model_file_bounds(path):
    """``(lo, hi)`` of an OBJ model's vertices, read straight from its asset,
    or None (another format, or unreadable).

    Deliberately its own reader: asking the logic thread's collision code
    (or any loader the renderer shares) from the paint, on the GUI thread,
    stopped the model being drawn while it was dragged.
    """
    if not str(path).lower().endswith('.obj'):
        return None
    from engine.asset_archives import asset_bytes, asset_exists
    for candidate in (path, 'assets/models/' + str(path)):
        try:
            if not asset_exists(candidate):
                continue
            text = asset_bytes(candidate).decode('utf-8', 'replace')
        except Exception:
            continue
        verts = []
        for line in text.splitlines():
            if line.startswith('v '):
                parts = line.split()
                try:
                    verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except (IndexError, ValueError):
                    continue
        if verts:
            v = np.asarray(verts, dtype=float)
            return v.min(axis=0), v.max(axis=0)
    return None


def _snapshot(objects):
    """Everything a gizmo drag can change on *objects*, to put back."""
    out = []
    for obj in objects:
        if isinstance(obj, dict):
            out.append((obj, {k: copy.deepcopy(v) for k, v in obj.items()
                              if k not in brush_geometry.GEO_RUNTIME_KEYS}))
        else:
            props = obj.properties
            out.append((obj, (list(obj.pos), {k: copy.deepcopy(props[k])
                                              for k in ('rotation', 'angle', 'scale',
                                                        'volume_size', 'source_offset')
                                              if k in props})))
    return out


def _restore(snapshot):
    for obj, saved in snapshot:
        if isinstance(obj, dict):
            for key in [k for k in obj if k not in saved]:
                del obj[key]
            obj.update(copy.deepcopy(saved))
            brush_geometry.invalidate_geometry_cache(obj)
        else:
            pos, props = saved
            obj.pos = list(pos)
            obj.properties.update(copy.deepcopy(props))
            touch(obj)


def brush_edges(brush):
    """World-space ``(a, b)`` edges of a brush: its real faces' edges for an
    angled brush, the twelve of its box otherwise."""
    convex = brush_placement.shape_convex(brush)
    if convex is not None:
        pos = np.asarray(brush.get('pos') or (0, 0, 0), dtype=float)
        verts = np.asarray(convex.verts, dtype=float) + pos
        seen, out = set(), []
        for face in convex.faces:
            ring = list(face['indices'])
            for k in range(len(ring)):
                a, b = ring[k], ring[(k + 1) % len(ring)]
                key = (min(a, b), max(a, b))
                if key not in seen:
                    seen.add(key)
                    out.append((verts[a], verts[b]))
        return out
    pos = np.asarray(brush.get('pos') or (0, 0, 0), dtype=float)
    half = np.asarray(brush.get('size') or (64, 64, 64), dtype=float) / 2
    corners = [pos + half * np.array((sx, sy, sz))
               for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    return [(corners[a], corners[b]) for a in range(8) for b in range(a + 1, 8)
            if bin(a ^ b).count('1') == 1]
