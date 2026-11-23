"""The 3D view's transform gizmo: its shape on screen and the maths of a drag.

Modelled on Blender's gizmo. Three modes, each one set of handles around the
selection's centre, the same size on screen at any distance:

* **Move**: an arrow along each axis; a small square for each pair of axes
  (coloured by the axis it does *not* move, as Blender does); a white circle
  in the middle that drags in the view plane.
* **Rotate**: a ring about each axis, only its front half drawn; a white ring
  around them that turns about the view direction; inside the sphere a free
  trackball rotation.
* **Scale**: a line ending in a box along each axis; a square per pair of
  axes; the outer white circle scales evenly.

Only positive axes are drawn, and an axis pointing at the camera fades out
(it could not be dragged usefully).

Nothing here draws or touches Qt or GL: a layout is computed from a
*project* function (world point -> widget pixels) and the camera's axes, and
handles are hit-tested in pixels. The view (engine.view_gizmo) paints the
layout -- with QPainter, which turns every stroke into triangles, so no line
is wider than the 1 pixel some GPUs (Adreno, macOS core profile) draw.
"""

from __future__ import annotations

import math

import numpy as np

MOVE, ROTATE, SCALE = 'move', 'rotate', 'scale'
MODES = (MOVE, ROTATE, SCALE)
MODE_NAMES = {MOVE: 'Move', ROTATE: 'Rotate', SCALE: 'Scale'}

#: Blender's axis colours: X red, Y green, Z blue.
AXIS_COLOURS = ((255, 51, 82), (139, 220, 0), (40, 144, 255))
AXIS_NAMES = 'XYZ'
WHITE = (255, 255, 255)
#: The outline of what a drag is moving, where it will land.
DRAG_OUTLINE = (255, 140, 0)       # dark orange

#: The gizmo's radius on screen, in pixels (Blender's default Gizmo Size).
RADIUS_PX = 75.0
#: How near (pixels) the cursor has to be to a handle to grab it.
PICK_PX = 9.0
#: Radius of the white view ring (and the uniform-scale circle) as a
#: fraction of the gizmo's.
VIEW_RING = 1.2
#: Radius of the move gizmo's white centre circle (fraction of the radius).
CENTRE_RING = 0.17
#: Where the arrows start and the scale boxes sit (fractions of the radius).
SHAFT_START = 0.22
#: The two-axis squares: their near and far corners along each axis.
PLANE_NEAR, PLANE_FAR = 0.30, 0.48
#: An axis drawn shorter than this fraction of the radius points (nearly)
#: at the camera: it fades out between here and FADE_FULL and cannot be
#: grabbed while faded more than half.
FADE_START, FADE_FULL = 0.12, 0.32

AXES = (np.array((1.0, 0.0, 0.0)), np.array((0.0, 1.0, 0.0)), np.array((0.0, 0.0, 1.0)))


# ---------------------------------------------------------------------------
# Handles
# ---------------------------------------------------------------------------
#
# A handle is a tuple: (kind, axis) with axis an index 0..2, or (kind,) for
# the handles that have none.

def axis_handle(mode, axis):
    return ({MOVE: 'move_axis', ROTATE: 'rotate_axis', SCALE: 'scale_axis'}[mode], axis)


PLANE_KINDS = ('move_plane', 'scale_plane')


def plane_axes(normal):
    """The two axes a plane handle (named by its normal) moves."""
    return tuple(i for i in range(3) if i != normal)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

class Layout:
    """Where every handle of one mode is on screen this frame.

    Built by :func:`layout`. Points are ``(x, y)`` widget pixels.
    """

    def __init__(self, mode, centre, centre_px, radius_world, to_camera,
                 camera_right, camera_up):
        self.mode = mode
        #: World centre and its pixel position.
        self.centre = np.asarray(centre, dtype=float)
        self.centre_px = centre_px
        #: The gizmo's radius in world units at this distance.
        self.radius_world = radius_world
        self.radius_px = RADIUS_PX
        #: Unit vector from the centre towards the camera.
        self.to_camera = to_camera
        self.camera_right = camera_right
        self.camera_up = camera_up
        #: axis -> (start_px, tip_px, alpha) for each positive axis.
        self.axes = {}
        #: normal axis -> [4 corner px] for each visible two-axis square.
        self.planes = {}
        #: axis -> [(px, front)] around each rotate ring.
        self.rings = {}


def camera_axes(view_matrix):
    """``(right, up, back)`` unit vectors of a view matrix given as a
    4x4 nested sequence indexed ``[column][row]`` (GLM's layout)."""
    m = view_matrix
    right = np.array([m[0][0], m[1][0], m[2][0]], dtype=float)
    up = np.array([m[0][1], m[1][1], m[2][1]], dtype=float)
    back = np.array([m[0][2], m[1][2], m[2][2]], dtype=float)
    return right, up, back


def pixels_per_unit(project, centre, right):
    """Pixels one world unit covers at *centre*, across the screen."""
    a = project(centre)
    b = project(np.asarray(centre, dtype=float) + right)
    if a is None or b is None:
        return None
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    return d if d > 1e-9 else None


def _fade(length_px):
    t = (length_px / RADIUS_PX - FADE_START) / (FADE_FULL - FADE_START)
    return max(0.0, min(1.0, t))


def _polygon_area(points):
    area = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        area += x0 * y1 - x1 * y0
    return abs(area) * 0.5


def layout(mode, centre, project, camera_pos, camera_right, camera_up):
    """The :class:`Layout` of *mode*'s handles about *centre*, or None when
    the centre is behind the camera.

    *project* maps a world point to widget pixels ``(x, y)`` (None behind
    the camera).
    """
    centre = np.asarray(centre, dtype=float)
    centre_px = project(centre)
    ppu = pixels_per_unit(project, centre, camera_right)
    if centre_px is None or ppu is None:
        return None
    to_camera = np.asarray(camera_pos, dtype=float) - centre
    dist = float(np.linalg.norm(to_camera))
    if dist < 1e-6:
        return None
    to_camera /= dist
    radius = RADIUS_PX / ppu
    out = Layout(mode, centre, centre_px, radius, to_camera,
                 np.asarray(camera_right, dtype=float), np.asarray(camera_up, dtype=float))
    for i, axis in enumerate(AXES):
        tip = project(centre + axis * radius)
        start = project(centre + axis * radius * SHAFT_START)
        if tip is None or start is None:
            continue
        length = math.hypot(tip[0] - centre_px[0], tip[1] - centre_px[1])
        out.axes[i] = (start, tip, _fade(length))
    if mode in (MOVE, SCALE):
        for normal in range(3):
            a, b = plane_axes(normal)
            corners = [project(centre + (AXES[a] * s + AXES[b] * t) * radius)
                       for s, t in ((PLANE_NEAR, PLANE_NEAR), (PLANE_FAR, PLANE_NEAR),
                                    (PLANE_FAR, PLANE_FAR), (PLANE_NEAR, PLANE_FAR))]
            if any(c is None for c in corners):
                continue
            side = (PLANE_FAR - PLANE_NEAR) * RADIUS_PX
            # Seen edge-on a square is a sliver: not drawn, not grabbed.
            if _polygon_area(corners) > 0.2 * side * side:
                out.planes[normal] = corners
    if mode == ROTATE:
        for i in range(3):
            a, b = plane_axes(i)
            points = []
            for k in range(97):
                t = 2.0 * math.pi * k / 96
                world = centre + (AXES[a] * math.cos(t) + AXES[b] * math.sin(t)) * radius
                px = project(world)
                if px is None:
                    continue
                # Blender draws only the half facing the viewer.
                points.append((px, float(np.dot(world - centre, to_camera)) >= -1e-6))
            out.rings[i] = points
    return out


# ---------------------------------------------------------------------------
# Hit testing
# ---------------------------------------------------------------------------

def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 < 1e-12 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _inside(p, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > p[1]) != (y1 > p[1]):
            x = x0 + (p[1] - y0) * (x1 - x0) / (y1 - y0)
            if x > p[0]:
                inside = not inside
    return inside


def hit_test(lay, point):
    """The handle of *lay* under *point* (widget pixels), or None.

    Nearest wins; a handle faded out by more than half is never hit.
    """
    if lay is None:
        return None
    cx, cy = lay.centre_px
    r_centre = math.hypot(point[0] - cx, point[1] - cy)
    best = None

    def offer(handle, dist):
        nonlocal best
        if dist <= PICK_PX and (best is None or dist < best[0]):
            best = (dist, handle)

    if lay.mode == MOVE:
        offer(('move_view',), abs(r_centre - CENTRE_RING * RADIUS_PX)
              if r_centre > CENTRE_RING * RADIUS_PX else 0.0)
        for i, (start, tip, alpha) in lay.axes.items():
            if alpha > 0.5:
                offer(('move_axis', i), _seg_dist(point, start, tip))
        for normal, corners in lay.planes.items():
            if _inside(point, corners):
                offer(('move_plane', normal), 0.0)
    elif lay.mode == SCALE:
        offer(('scale_uniform',), abs(r_centre - VIEW_RING * RADIUS_PX))
        for i, (start, tip, alpha) in lay.axes.items():
            if alpha > 0.5:
                offer(('scale_axis', i), min(_seg_dist(point, start, tip),
                                              max(0.0, math.hypot(point[0] - tip[0],
                                                                  point[1] - tip[1]) - 6.0)))
        for normal, corners in lay.planes.items():
            if _inside(point, corners):
                offer(('scale_plane', normal), 0.0)
    elif lay.mode == ROTATE:
        offer(('rotate_view',), abs(r_centre - VIEW_RING * RADIUS_PX))
        for i, points in lay.rings.items():
            front = [p for p, f in points]
            dist = min((_seg_dist(point, front[k], front[k + 1])
                        for k in range(len(front) - 1)
                        if points[k][1] and points[k + 1][1]), default=float('inf'))
            offer(('rotate_axis', i), dist)
        if best is None and r_centre < RADIUS_PX:
            # Inside the sphere, off every ring: Blender's free trackball.
            return ('rotate_trackball',)
    return None if best is None else best[1]


# ---------------------------------------------------------------------------
# Drag maths
# ---------------------------------------------------------------------------

def ray_axis_param(ray_o, ray_d, origin, axis):
    """Parameter *t* of the point ``origin + t*axis`` nearest the ray, or
    None when the ray runs (nearly) along the axis."""
    o = np.asarray(ray_o, dtype=float)
    d = np.asarray(ray_d, dtype=float)
    p = np.asarray(origin, dtype=float)
    a = np.asarray(axis, dtype=float)
    w = p - o
    aa, ad, dd = a @ a, a @ d, d @ d
    denom = aa * dd - ad * ad
    if abs(denom) < 1e-9 * aa * dd:
        return None
    return float((ad * (w @ d) - dd * (w @ a)) / denom)


def ray_plane(ray_o, ray_d, point, normal):
    """Where the ray meets the plane through *point* with *normal*, or None
    (parallel, or behind the ray)."""
    o = np.asarray(ray_o, dtype=float)
    d = np.asarray(ray_d, dtype=float)
    n = np.asarray(normal, dtype=float)
    denom = float(d @ n)
    if abs(denom) < 1e-9:
        return None
    t = float((np.asarray(point, dtype=float) - o) @ n) / denom
    if t <= 0.0:
        return None
    return o + d * t


def signed_angle(v0, v1, axis):
    """Angle in degrees from *v0* to *v1* about *axis* (right-handed)."""
    v0 = np.asarray(v0, dtype=float)
    v1 = np.asarray(v1, dtype=float)
    return math.degrees(math.atan2(float(np.cross(v0, v1) @ np.asarray(axis, dtype=float)),
                                   float(v0 @ v1)))


def wrap_degrees(angle):
    """*angle* brought into (-180, 180]."""
    angle = math.fmod(angle, 360.0)
    if angle <= -180.0:
        angle += 360.0
    elif angle > 180.0:
        angle -= 360.0
    return angle


def snap(value, step):
    """*value* to the nearest multiple of *step* (unchanged for no step)."""
    if not step:
        return value
    return round(value / step) * step


def rotation_matrix(axis, angle_deg):
    """Right-handed rotation about *axis* -- the same matrix
    :func:`engine.brush_geometry.rotate_point` turns brushes with."""
    a = np.asarray(axis, dtype=float)
    x, y, z = a / np.linalg.norm(a)
    theta = math.radians(angle_deg)
    c, s = math.cos(theta), math.sin(theta)
    C = 1.0 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


def model_matrix(rotation):
    """A model's ``[pitch, yaw, roll]`` (degrees) as ``Ry @ Rx @ Rz``, the
    order the renderer and collision draw it in
    (engine.logic_collision.model_basis)."""
    pitch, yaw, roll = (math.radians(float(v)) for v in rotation[:3])
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)))
    rx = np.array(((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp)))
    rz = np.array(((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0)))
    return ry @ rx @ rz


def model_rotation_from_matrix(m):
    """``[pitch, yaw, roll]`` (degrees) of a rotation ``Ry @ Rx @ Rz``."""
    m = np.asarray(m, dtype=float)
    sp = max(-1.0, min(1.0, -m[1][2]))
    pitch = math.asin(sp)
    if abs(sp) < 1.0 - 1e-9:
        yaw = math.atan2(m[0][2], m[2][2])
        roll = math.atan2(m[1][0], m[1][1])
    else:
        # Gimbal lock: pitch +-90 and yaw and roll turn about one axis.
        roll = 0.0
        yaw = math.atan2(-m[2][0], m[0][0])
    return [_tidy(math.degrees(pitch)), _tidy(math.degrees(yaw)), _tidy(math.degrees(roll))]


def _tidy(deg):
    """Round away float dust (89.99999999 -> 90) and -0."""
    r = round(deg, 6)
    return 0.0 if r == 0 else r


def turn_model(rotation, axis, angle_deg):
    """A model's ``[pitch, yaw, roll]`` after turning it *angle_deg* about the
    world *axis*."""
    return model_rotation_from_matrix(
        rotation_matrix(axis, angle_deg) @ model_matrix(rotation))


def local_scale_factors(rotation, world_factors):
    """Per-local-axis factors for a model turned by *rotation* that a world
    per-axis scale stretches: each local axis takes the factor of the world
    axis it lies closest to (a model's scale is in its own frame)."""
    m = model_matrix(rotation)
    out = []
    for j in range(3):
        k = int(np.argmax(np.abs(m[:, j])))
        out.append(float(world_factors[k]))
    return out
