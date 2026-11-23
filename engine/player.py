"""The player controller: Fio's collision, Quake 2's movement rules.

Collision is Fio's own -- the spatial grid's colliders, per-axis box
resolution with the stair lip, capsule depenetration against angled brushes,
terrain, and Fio's water. Gravity comes from the live physics, the one
authority (``engine.physics.world_gravity``; Quake 2's 800 by default).

On top of that, movement follows Quake 2 (qcommon/pmove.c, cl_input.c):
200 walk / 300 run (sprint is +speed) / 100 ducked, with diagonal input
adding up as Quake 2's does; ground acceleration 10 and friction 6 with stop
speed 100, air acceleration 1, the wish shortened by a third of the view
pitch; a 270 jump that must be released between jumps and is locked out
briefly after a hard landing; stepping up 18 units in the air as well as on
the ground; ducking that halves the hull; and ground no steeper than ~45
degrees.
"""

import math
import glm
from .constants import (
    TILE_SIZE, PLAYER_HULL_SIZE, JUMP_STRENGTH, TERMINAL_VELOCITY,
    CL_FORWARDSPEED, CL_SIDESPEED, CL_RUN_SCALE,
    PM_STOPSPEED, PM_MAXSPEED, PM_DUCKSPEED, PM_ACCELERATE, PM_AIRACCELERATE,
    PM_FRICTION, PM_GROUND_NORMAL,
    PM_LAND_SPEED, PM_HARD_LAND_SPEED, PM_LAND_TIME, PM_HARD_LAND_TIME,
    PM_DUCK_HEIGHT_FRACTION, PM_DUCK_EYE_FRACTION,
    PM_SPRINT_SCALE,
    WATER_SWIM_SPEED_MULT, WATER_VERTICAL_SPEED_MULT, WATER_DRAG,
    WATER_WADE_SPEED_MULT, WATER_MAX_SINK_SPEED,
    WATERJUMP_MAX_CLIMB, WATERJUMP_EDGE_ABOVE_SURFACE, WATERJUMP_MAX_BOOST,
    is_solid_world_brush, is_water_brush, brush_aabb_bounds,
)
from .constants import _UNPUBLISHED
from . import brush_placement as _placement
from .brush_placement import (
    collision_shape, collision_shape_at, off_range, overridden, resolved_runtime,
    world_bounds,
)
from .physics import world_gravity


# =============================================================================
# Mesh Collision Helpers
# =============================================================================

def _point_in_triangle(p, a, b, c):
    """Barycentric test: is point p inside triangle abc? All are (x,y,z) tuples."""
    def sub(v1, v2):
        return (v1[0]-v2[0], v1[1]-v2[1], v1[2]-v2[2])
    def cross(v1, v2):
        return (v1[1]*v2[2]-v1[2]*v2[1], v1[2]*v2[0]-v1[0]*v2[2], v1[0]*v2[1]-v1[1]*v2[0])
    def dot(v1, v2):
        return v1[0]*v2[0]+v1[1]*v2[1]+v1[2]*v2[2]
    
    ab = sub(b, a)
    ac = sub(c, a)
    ap = sub(p, a)
    n = cross(ab, ac)
    d = dot(n, n)
    
    if d < 0.0001:
        return False
        
    w = dot(cross(ab, ap), n) / d
    v = dot(cross(ap, ac), n) / d
    u = 1.0 - w - v
    
    return u >= -0.001 and v >= -0.001 and w >= -0.001


def _closest_point_on_triangle(p, a, b, c):
    """Closest point on triangle ABC to point P (all plain 3-vectors).

    Degenerate triangles are treated as their longest segment, avoiding the
    zero denominators in the standard triangle Voronoi-region calculation.
    """
    def sub(u, v):
        return (u[0] - v[0], u[1] - v[1], u[2] - v[2])

    def dot(u, v):
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]

    def closest_on_segment(start, end):
        edge = sub(end, start)
        length_sq = dot(edge, edge)
        if length_sq <= 1e-20:
            return start
        t = max(0.0, min(1.0, dot(sub(p, start), edge) / length_sq))
        return (start[0] + edge[0] * t,
                start[1] + edge[1] * t,
                start[2] + edge[2] * t)

    ab, ac, ap = sub(b, a), sub(c, a), sub(p, a)
    bc = sub(c, b)
    nx = ab[1] * ac[2] - ab[2] * ac[1]
    ny = ab[2] * ac[0] - ab[0] * ac[2]
    nz = ab[0] * ac[1] - ab[1] * ac[0]
    area_sq = nx * nx + ny * ny + nz * nz
    edge_scale_sq = max(dot(ab, ab), dot(ac, ac), dot(bc, bc), 1.0)
    if area_sq <= 1e-16 * edge_scale_sq * edge_scale_sq:
        candidates = (closest_on_segment(a, b),
                      closest_on_segment(b, c),
                      closest_on_segment(c, a))
        return min(candidates, key=lambda q: dot(sub(p, q), sub(p, q)))

    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a
    bp = sub(p, b)
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3)
        return (a[0] + ab[0] * v, a[1] + ab[1] * v, a[2] + ab[2] * v)
    cp = sub(p, c)
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6)
        return (a[0] + ac[0] * w, a[1] + ac[1] * w, a[2] + ac[2] * w)
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return (b[0] + bc[0] * w, b[1] + bc[1] * w, b[2] + bc[2] * w)
    denom = 1.0 / (va + vb + vc)
    v, w = vb * denom, vc * denom
    return (a[0] + ab[0] * v + ac[0] * w,
            a[1] + ab[1] * v + ac[1] * w,
            a[2] + ab[2] * v + ac[2] * w)


def _closest_point_on_triangle_xz(x, z, triangle):
    """Return the triangle height and squared XZ distance to its projection.

    Used by model stair stepping: a tread may begin just ahead of the player's
    centre, while the capsule footprint already reaches its leading edge.
    """
    a, b, c = triangle
    abx, abz = b[0] - a[0], b[2] - a[2]
    acx, acz = c[0] - a[0], c[2] - a[2]
    det = abx * acz - abz * acx
    dx, dz = x - a[0], z - a[2]

    if abs(det) > 1e-12:
        u = (dx * acz - dz * acx) / det
        v = (abx * dz - abz * dx) / det
        if u >= -1e-7 and v >= -1e-7 and u + v <= 1.0 + 1e-7:
            height = a[1] + u * (b[1] - a[1]) + v * (c[1] - a[1])
            return height, 0.0

    best = (None, float("inf"))
    for start, end in ((a, b), (b, c), (c, a)):
        ex, ez = end[0] - start[0], end[2] - start[2]
        length_sq = ex * ex + ez * ez
        if length_sq <= 1e-20:
            t = 0.0
        else:
            t = max(0.0, min(1.0, (
                (x - start[0]) * ex + (z - start[2]) * ez
            ) / length_sq))
        qx, qz = start[0] + ex * t, start[2] + ez * t
        distance_sq = (x - qx) ** 2 + (z - qz) ** 2
        if distance_sq < best[1]:
            height = start[1] + (end[1] - start[1]) * t
            best = (height, distance_sq)
    return best


def _oriented_triangle_normal(normal, point, reference, velocity):
    """Orient a triangle normal toward the side occupied by the capsule centre.

    Face winding is not collision state. In particular, using each capsule
    sphere's displacement independently can send the head sphere through a
    ceiling while the capsule centre is still below it.
    """
    length = math.sqrt(sum(float(v) * float(v) for v in normal))
    if length <= 1e-10:
        return (0.0, 1.0, 0.0)
    n = tuple(float(v) / length for v in normal)
    side = sum((reference[i] - point[i]) * n[i] for i in range(3))
    eps = 1e-6
    if side > eps:
        sign = 1.0
    elif side < -eps:
        sign = -1.0
    else:
        into = sum(float(velocity[i]) * n[i] for i in range(3))
        if into > eps:
            sign = -1.0
        elif into < -eps:
            sign = 1.0
        elif abs(n[1]) > eps:
            # With no side or motion information, horizontal decks resolve up.
            sign = 1.0 if n[1] > 0.0 else -1.0
        else:
            first = next((v for v in n if abs(v) > eps), 1.0)
            sign = 1.0 if first > 0.0 else -1.0
    return (n[0] * sign, n[1] * sign, n[2] * sign)


def _model_triangle_contact_normal(displacement, distance, face_normal,
                                   contact_point, capsule_centre, velocity):
    """Return a closest-point normal that stays on the capsule's side of a face.

    Edge and vertex contacts retain their tangential displacement, but a
    sphere that has crossed a thin face cannot reverse the whole capsule
    through that face.
    """
    face_side = _oriented_triangle_normal(
        face_normal, contact_point, capsule_centre, velocity
    )
    if distance <= 1e-6:
        return face_side
    inv_distance = 1.0 / distance
    n = tuple(float(v) * inv_distance for v in displacement)
    into = sum(n[i] * face_side[i] for i in range(3))
    if into < 0.0:
        n = tuple(n[i] - 2.0 * into * face_side[i] for i in range(3))
    length = math.sqrt(sum(v * v for v in n))
    if length <= 1e-10:
        return face_side
    return (n[0] / length, n[1] / length, n[2] / length)


def _ground_slide_normal(contact_normal, face_normal):
    """The normal a swept capsule slides along after a touch.

    A ground-like touch -- the contact normal within walking slope of up,
    which is what the ground check already counts as standing -- slides as
    ground: along a walkable triangle's own face, or straight up off the top
    edge of a wall or riser. Sliding along the closest-point normal instead
    tilted it back toward the player at a stair's nose, turning walking speed
    into a hop (200 units a second forward became 22 upward) that left the
    player airborne for two ticks at every step of a model staircase.
    Steeper contacts keep their own normal: a capsule still rolls off a ledge
    and still meets the top of a step too high to climb.
    """
    if contact_normal[1] < PM_GROUND_NORMAL:
        return contact_normal
    fy = float(face_normal[1])
    if abs(fy) < PM_GROUND_NORMAL:
        return (0.0, 1.0, 0.0)
    sign = 1.0 if fy > 0.0 else -1.0
    fx, fz = float(face_normal[0]) * sign, float(face_normal[2]) * sign
    fy = abs(fy)
    length = math.sqrt(fx * fx + fy * fy + fz * fz)
    if length <= 1e-10:
        return contact_normal
    return (fx / length, fy / length, fz / length)


def _intersect_swept_sphere_triangle(sphere_pos, sphere_vel, radius, triangle,
                                    normal, capsule_centre=None):
    """Continuous two-sided sphere/triangle contact.

    Returns (hit, hit_time, closest_point, slide_normal). The face is swept
    against both sides and the edges are swept as capsules, including endpoint
    spheres, so a fast player cannot cross a thin model wall between ticks.
    """
    v0, v1, v2 = triangle
    p = (float(sphere_pos.x), float(sphere_pos.y), float(sphere_pos.z))
    movement = (float(sphere_vel.x), float(sphere_vel.y), float(sphere_vel.z))
    r = float(radius)
    eps = 1e-9

    def sub(u, v):
        return (u[0] - v[0], u[1] - v[1], u[2] - v[2])

    def dot(u, v):
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]

    def scale(v, amount):
        return (v[0] * amount, v[1] * amount, v[2] * amount)

    def add(u, v):
        return (u[0] + v[0], u[1] + v[1], u[2] + v[2])

    best = None

    def offer(t, point, contact_normal):
        nonlocal best
        length = math.sqrt(dot(contact_normal, contact_normal))
        if length <= 1e-10:
            return
        n = scale(contact_normal, 1.0 / length)
        if dot(n, movement) > 0.0:
            n = scale(n, -1.0)
        if best is None or t < best[0]:
            best = (max(0.0, min(1.0, t)), point, n)

    ab, ac = sub(v1, v0), sub(v2, v0)
    face = (ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0])
    face_len = math.sqrt(dot(face, face))

    # Sweep against both face offsets.
    if face_len > 1e-8:
        n = scale(face, 1.0 / face_len)
        distance = dot(sub(p, v0), n)
        normal_speed = dot(movement, n)
        if abs(normal_speed) > eps:
            if abs(distance) > eps:
                side = 1.0 if distance > 0.0 else -1.0
            elif capsule_centre is not None:
                body_side = dot(sub(capsule_centre, v0), n)
                side = (1.0 if body_side > eps else -1.0
                        if body_side < -eps else
                        (1.0 if normal_speed > 0.0 else -1.0))
            else:
                side = 1.0 if normal_speed > 0.0 else -1.0
            # Only the capsule's current side is eligible. Testing the
            # opposite plane makes a jump away from a touching deck collide.
            approach_speed = side * normal_speed
            if approach_speed < -eps:
                separation = side * distance
                if separation > r:
                    t = (r - separation) / approach_speed
                elif separation >= -r:
                    t = 0.0
                else:
                    t = None
                if t is not None and -eps <= t <= 1.0 + eps:
                    centre = add(p, scale(movement, t))
                    contact = sub(centre, scale(n, side * r))
                    if _point_in_triangle(contact, v0, v1, v2):
                        offer(t, contact, scale(n, side))

    # Sweep against the cylindrical part of each edge capsule.
    for a, b in ((v0, v1), (v1, v2), (v2, v0)):
        edge = sub(b, a)
        edge_sq = dot(edge, edge)
        if edge_sq <= 1e-20:
            continue
        offset = sub(p, a)
        along = dot(offset, edge) / edge_sq
        along_speed = dot(movement, edge) / edge_sq
        perpendicular = sub(offset, scale(edge, along))
        perpendicular_speed = sub(movement, scale(edge, along_speed))
        qa = dot(perpendicular_speed, perpendicular_speed)
        qb = 2.0 * dot(perpendicular, perpendicular_speed)
        qc = dot(perpendicular, perpendicular) - r * r
        roots = []
        if qc <= 0.0:
            if dot(perpendicular, perpendicular_speed) < -eps:
                roots.append(0.0)
        elif qa > 1e-20:
            discriminant = qb * qb - 4.0 * qa * qc
            if discriminant >= 0.0:
                root = math.sqrt(discriminant)
                roots.extend(((-qb - root) / (2.0 * qa),
                              (-qb + root) / (2.0 * qa)))
        for t in sorted(roots):
            if t < -eps or t > 1.0 + eps:
                continue
            u = along + along_speed * t
            if u < -eps or u > 1.0 + eps:
                continue
            centre = add(p, scale(movement, t))
            contact = add(a, scale(edge, max(0.0, min(1.0, u))))
            delta = sub(centre, contact)
            if dot(delta, delta) <= (r + 1e-5) ** 2:
                fallback = face if face_len > 1e-8 else scale(movement, -1.0)
                offer(t, contact, delta if dot(delta, delta) > 1e-16 else fallback)
                break

    # Endpoint spheres complete the rounded ends of the edge capsules.
    movement_sq = dot(movement, movement)
    if movement_sq > 1e-20:
        for vertex in (v0, v1, v2):
            offset = sub(p, vertex)
            qa = movement_sq
            qb = 2.0 * dot(offset, movement)
            qc = dot(offset, offset) - r * r
            roots = []
            if qc <= 0.0:
                if dot(offset, movement) < -eps:
                    roots.append(0.0)
            else:
                discriminant = qb * qb - 4.0 * qa * qc
                if discriminant >= 0.0:
                    roots.append((-qb - math.sqrt(discriminant)) / (2.0 * qa))
            for t in roots:
                if -eps <= t <= 1.0 + eps:
                    centre = add(p, scale(movement, t))
                    delta = sub(centre, vertex)
                    fallback = face if face_len > 1e-8 else scale(movement, -1.0)
                    offer(t, vertex, delta if dot(delta, delta) > 1e-16 else fallback)
                    break

    if best is None:
        return False, 1.0, None, None
    t, point, n = best
    return True, t, glm.vec3(*point), glm.vec3(*n)


# =============================================================================
# Player Class
# =============================================================================

def _water_bounds(brush):
    """A water volume's derived bounds (world_bounds): almost always a plain
    box, whose bounds are its pos -/+ size/2 -- the box rule, inline, as
    Stage 1 read it -- and the resolver's for anything else."""
    if ('geometry' in brush or '_mesh_bounds' in brush
            or 'is_mover' in brush or 'is_door' in brush):
        return world_bounds(brush)
    pos = brush['pos']
    size = brush['size']
    hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
    return (pos[0] - hx, pos[1] - hy, pos[2] - hz, pos[0] + hx, pos[1] + hy, pos[2] + hz)


def _blocks_player(brush):
    """Single source of truth for "does the player collide with this brush?".

    Every collision pass the player runs — horizontal sweep, step-up, vertical
    resolve, capsule depenetration, waterjump probe — used to re-derive this per
    brush, so a brush near the player was classified five or six times a frame,
    ``is_water_brush`` (which lower-cases every face texture) included.  It is
    derived once per frame now, in ``Player.update``, and the passes just walk
    the list they are handed.

    The expensive water test goes last deliberately: ``SpatialGrid.populate``
    already keeps water, fog, non-dynamic triggers and physics bodies out of the
    grid, so on the normal path the only brushes that reach it are the movers
    and doors appended afterwards.  The flags *are* still tested here, because
    ``hidden``/``disabled`` are runtime state the grid cannot filter on (it
    indexes by ``authored_hidden`` precisely so a streamed-out brush can come
    back), and because the no-grid fallback path hands over the raw brush list.
    """
    if brush.get('_physics_body'):
        # Simulated by the active physics as a dynamic body, not a static wall.
        return False
    if brush.get('disabled'):
        return False
    return is_solid_world_brush(brush)


def _gather(colliders, moving):
    """``(aabb_brushes, mesh_brushes, mesh_shapes)``: one pass over
    everything that could collide this tick -- *colliders* in the order the
    grid gave them, then each mover and door in *moving* (canonical order:
    the mover list, then the door list) that the colliders did not already
    hold, so every brush is gathered once and narrow-phased at most once a
    pass -- dropping what the player does not collide with at all (see
    _blocks_player) and splitting the rest by shape
    (brush_placement.collision_shape). A solid shape -- an angled brush,
    static or moving, a spinning box, a model's collision mesh -- is
    resolved in its own frame; any other box keeps the AABB fast path, its
    derived bounds being exact."""
    mesh_brushes = []
    mesh_shapes = []
    aabb_brushes = []
    held = None
    for source in (colliders, moving):
        if not source:
            continue
        for b in source:
            if 'is_mover' in b or 'is_door' in b:
                # Gathered once: from the grid's cells, or from the movers.
                if held is None:
                    held = {id(b)}
                elif id(b) in held:
                    continue
                else:
                    held.add(id(b))
            if not _blocks_player(b):
                continue
            # A static box -- most of a level -- is told by three dict
            # reads; only a brush with planes, a mesh or a runtime
            # transform asks for its shape (once: the resolve below is
            # handed what it answered).
            if ('_collision_mode' not in b
                    and (('is_mover' in b and b['is_mover'])
                         or ('is_door' in b and b['is_door']))):
                # A mover or door: its box and the transform it was placed
                # by, as its mover table published them with its last
                # change (the AABB passes then read the same box). A box
                # is a solid shape only while it is turned; a convex brush
                # always is.
                runtime = b.get('_aabb_runtime', _UNPUBLISHED)
                if runtime is _UNPUBLISHED:
                    # No table published it: the resolver finds its
                    # transform (its box is derived by a pass that needs it).
                    runtime = resolved_runtime(b)
                if 'geometry' not in b and (runtime is None or not runtime[6]):
                    aabb_brushes.append(b)
                    continue
                shape = collision_shape_at(b, runtime)
                if shape is not None:
                    mesh_brushes.append(b)
                    mesh_shapes.append(shape)
                else:
                    aabb_brushes.append(b)
                continue
            if ('geometry' in b or '_collision_mode' in b
                    or 'is_mover' in b or 'is_door' in b):
                shape = collision_shape(b)
                if shape is not None:
                    mesh_brushes.append(b)
                    mesh_shapes.append(shape)
                    continue
            aabb_brushes.append(b)
    return aabb_brushes, mesh_brushes, mesh_shapes


class _Reach:
    """What the tick's mover broad phase (Player._near_movers: the spatial
    grid's cells within the player's reach, movers filed there for their
    whole travel) covers: the movers and doors near the player where the
    tick began, and how far the player may move from there before a pass
    could test beyond them.

    The broad phase keeps every mover whose box can come within the
    player's standing hull, plus the capsule's reach and :attr:`room`, of
    ``(x, y, z)``. A pass tests boxes centred on where the player is (a
    step-up and an un-duck a step or a duck higher), so while the player is
    within :attr:`room` of where it began, a mover the broad phase left out
    touches nothing a pass tests: the passes run on the near lists alone and
    answer what they would over every mover. :meth:`holds` is asked before
    each pass and after each push; once it fails, the rest of the tick runs
    on the full lists (:attr:`full`)."""

    __slots__ = ('x', 'y', 'z', 'room', 'full')

    def holds(self, x, y, z):
        room = self.room
        return (-room <= x - self.x <= room and -room <= y - self.y <= room
                and -room <= z - self.z <= room)


def _reach_pads(width, height, depth, step, room):
    """``(pad, pad_turned, allowed)`` for a standing hull of *width* x
    *height* x *depth*: how close (per axis) a mover's box must come to the
    player's centre for any pass to test it while the player stays within
    *room* of that centre, and how far the centre itself may then move.

    * An AABB test (a pass, a step-up, an un-duck) overlaps the hull, at most
      the standing one; the capsule's broad reject in a translated frame
      widens it by the capsule's radius. So *pad* is the standing half-extents
      plus that radius plus *room*.
    * In a turned frame the capsule's broad reject is a cube of half-width
      ``e`` (the hull's circumscribed radius plus the capsule's) about the
      player's centre in the shape's frame; whatever the turn, that keeps the
      centre within ``|half + e|`` of the shape's centre, which is at most
      ``|half| + e * sqrt(3)`` -- the box's radius (the table's measure for a
      row that can turn) plus *pad_turned*. It also covers an AABB test of
      the turned box, whose half-extents never exceed its radius.
    * The centre may move *room*, less a step up (a step-up tests the hull a
      step higher than where the pass put it) and a margin for rounding
      (float32 boxes, an un-turned frame's offsets): *allowed*."""
    hx, hy, hz = width * 0.5, height * 0.5, depth * 0.5
    radius = min(hx, hz)
    pad = (hx + radius + room, hy + radius + room, hz + radius + room)
    turned = (math.sqrt(hx * hx + hy * hy + hz * hz) + radius) * math.sqrt(3.0) + room
    return pad, (turned, turned, turned), room - (step + 0.001) - 1.0


class Player:
    def __init__(self, x, z, angle=math.pi, physics_enabled=True):
        # Initialize position (Y is set to 2 tiles high by default)
        self.pos = glm.vec3(float(x), float(TILE_SIZE) * 2, float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.angle = angle
        self.pitch = 0.0

        # Walking speed (Quake 2's cl_forwardspeed); sprint runs at PM_MAXSPEED.
        self.speed = CL_FORWARDSPEED
        self.mouse_sensitivity = 0.0015
        self.width, self.height, self.depth = PLAYER_HULL_SIZE
        self.camera_height = 40.0  # Offset from pos.y
        # The standing hull and eye; ducking derives its own from these.
        self.stand_height = self.height
        self.stand_camera_height = self.camera_height
        self.ducked = False
        # The hull ducks at once, as Quake 2's does; the view follows over
        # DUCK_VIEW_TIME. view_offset is how far the eye still has to travel
        # to camera_height (0 when it has arrived); see view_height.
        self.view_offset = 0.0
        # Quake 2's landing lockout: no jumping for a moment after a hard landing.
        self.landing = False
        self.pm_time = 0.0

        # Physics state
        self.on_ground = False
        self.ground_object = None  # Reference to the brush we are standing on
        # The mover broad phase's caches (see _near_movers).
        self._moving_cache = None
        self._moving_cells = None
        self._near_span = None
        self._cover = None
        self.physics_enabled = physics_enabled
        self.step_height = 18.0  # Max height the player can step up automatically
        # Downward allowance for mesh-footprint support, refreshed per physics tick.
        self._mesh_support_snap_distance = 0.25

        # Water / swimming state (refreshed every update)
        self.in_water = False          # any meaningful immersion (wading or deeper)
        self.swimming = False          # deep enough that swim physics take over
        self.eye_underwater = False    # camera below a water surface (drives the underwater overlay)
        self.water_surface_y = None    # world Y of the surface we're swimming under
        self.water_depth_frac = 0.0    # 0..1 fraction of feet->eye span that is submerged
        self.water_tint = [0.0, 0.4, 0.6]
        self._waterjump_timer = 0.0    # while > 0, swim drag leaves the launch arc alone
        self._waterjump_cooldown = 0.0 # prevents re-triggering every frame

        # Pre-computed half-extents (they change only when ducking)
        self._half = glm.vec3(self.width / 2.0, self.height / 2.0, self.depth / 2.0)

        # Quake 2's jump latch: set only by a jump, so holding jump does not
        # repeat on landing, but a press made in the air jumps on landing.
        self._jump_held = False
        self._gravity = world_gravity()

        # Out of the world (leave_void): below kill_height, back to
        # respawn_point. The logic thread sets both from the level.
        self.kill_height = self.KILL_HEIGHT_DEFAULT
        self.respawn_point = None

    #: Seconds the view takes to move between the standing and ducked eye.
    DUCK_VIEW_TIME = 0.5

    #: The kill height when nothing better is known: the clamp the engine
    #: always had. In a game the logic thread sets ``kill_height`` from the
    #: level (LogicPlayer.kill_height): below its lowest geometry.
    KILL_HEIGHT_DEFAULT = -2000.0
    #: Where a player out of the world goes when no ``respawn_point`` is set.
    RESPAWN_DEFAULT = (0.0, 100.0, 0.0)

    def leave_void(self):
        """Put the player back at ``respawn_point`` if it has left the world:
        fallen below ``kill_height`` with nothing under it, or with a position
        or velocity that is not a finite number (one NaN would reach every
        collision query). Returns whether it did."""
        pos, vel = self.pos, self.velocity
        finite = all(math.isfinite(v) for v in (pos.x, pos.y, pos.z, vel.x, vel.y, vel.z))
        kill_height = getattr(self, 'kill_height', self.KILL_HEIGHT_DEFAULT)
        if finite and (pos.y >= kill_height or self.on_ground):
            return False
        x, y, z = getattr(self, 'respawn_point', None) or self.RESPAWN_DEFAULT
        self.pos = glm.vec3(float(x), float(y), float(z))
        self.velocity = glm.vec3(0, 0, 0)
        self.on_ground = False
        self.ground_object = None
        return True

    @property
    def view_height(self):
        """The eye the player sees from, above ``pos``: ``camera_height``,
        except while the view is still travelling after a duck or a stand."""
        return self.camera_height + self.view_offset

    def _duck_eye_travel(self):
        """How far the eye moves, in world units, between standing and ducked."""
        stand_eye = self.stand_height * 0.5 + self.stand_camera_height
        duck_height = self.stand_height * PM_DUCK_HEIGHT_FRACTION
        duck_eye = stand_eye * PM_DUCK_EYE_FRACTION - duck_height * 0.5
        return self._duck_drop() + self.stand_camera_height - duck_eye

    def _settle_view(self, delta):
        """Move the view toward the eye at the rate that covers a whole duck
        in DUCK_VIEW_TIME, so turning back half way takes half as long."""
        if not self.view_offset:
            return
        step = self._duck_eye_travel() / self.DUCK_VIEW_TIME * delta
        # The tolerance absorbs rounding, so the last step lands on 0.
        if abs(self.view_offset) <= step * (1.0 + 1e-9):
            self.view_offset = 0.0
        elif self.view_offset > 0.0:
            self.view_offset -= step
        else:
            self.view_offset += step

    def _duck_drop(self):
        """How far the centre moves when the hull shrinks from the top."""
        return self.stand_height * (1.0 - PM_DUCK_HEIGHT_FRACTION) * 0.5

    def set_ducked(self, ducked):
        """Set the hull and eye for ducking, leaving the centre where it is."""
        ducked = bool(ducked)
        stand_eye = self.stand_height * 0.5 + self.stand_camera_height
        if ducked:
            height = self.stand_height * PM_DUCK_HEIGHT_FRACTION
            self.camera_height = stand_eye * PM_DUCK_EYE_FRACTION - height * 0.5
        else:
            height = self.stand_height
            self.camera_height = self.stand_camera_height
        self.ducked = ducked
        self.height = height
        self._half = glm.vec3(self.width / 2.0, height / 2.0, self.depth / 2.0)

    #: How far (per axis) a tick's mover broad phase reaches past the
    #: player's hull: what the player may move in a tick from where it began
    #: and still have its passes covered (a step-up and a rounding margin
    #: come off it: _reach_pads) -- well past a tick of terminal fall (13
    #: units at 60 Hz) or of a run (5). A move beyond it (a dash, a long
    #: push) runs that pass on every mover instead.
    MOVER_ROOM = 80.0

    #: Fewer movers and doors than this are each looked at: the broad phase
    #: costs a few microseconds a tick to ask, about what two rows cost the
    #: passes (measured: tools/bench_player_table.py, one_mover).
    MOVER_NEAR_FROM = 3

    def release_movers(self):
        """Play stopped: forget the level's movers and doors (the broad
        phase's lists of them), so nothing of the finished session is kept."""
        self._moving_cache = None
        self._moving_cells = None
        self._near_span = None
        self._cover = None

    def _moving(self, movers, doors):
        """The level's movers and doors, each once, in canonical order (the
        mover list, then the door list; a brush in both comes once, as a
        mover), with each one's place in it -- worked out again only when
        the lists are replaced."""
        cached = getattr(self, '_moving_cache', None)
        if (cached is not None and cached[0] is movers and cached[1] is doors
                and cached[2] == ((len(movers) if movers else 0), (len(doors) if doors else 0))):
            return cached[3]
        rank = {}
        for source in (movers, doors):
            for brush in source or ():
                rank.setdefault(id(brush), (len(rank), brush))
        moving = tuple(brush for _place, brush in rank.values())
        self._moving_cache = (movers, doors, ((len(movers) if movers else 0),
                                              (len(doors) if doors else 0)),
                              moving, {key: place for key, (place, _b) in rank.items()})
        self._moving_cells = None
        self._near_span = None
        return moving

    def _near_movers(self, grid, colliders, moving):
        """``(cover, near)``: this tick's movers and doors near enough for a
        pass to reach -- those the spatial grid filed (for their whole
        travel) in the cells within the player's reach, and any off range
        (brush_placement.off_range) -- in canonical order, and the
        :class:`_Reach` that says how far the player may move before a pass
        needs every one."""
        if overridden:
            place = self._moving_cache[4]
            if any(key in place for key in overridden):
                # Another source moves one of these brushes (the editor
                # preview, a test): it keeps no travel the grid could file,
                # so every mover is looked at, as before the broad phase.
                return None, moving
        hull = (self.width, self.stand_height, self.depth, self.step_height)
        pads = getattr(self, '_reach_pads', None)
        if pads is None or pads[0] != hull:
            pad, pad_turned, allowed = _reach_pads(*hull, self.MOVER_ROOM)
            pads = self._reach_pads = (hull, pad, pad_turned, allowed,
                                       max(max(pad), pad_turned[0]))
        x, y, z = self.pos.x, self.pos.y, self.pos.z
        reach = pads[4]
        size = grid.cell_size
        # Floor division of floats is the floor (math.floor, without the
        # calls).
        span = ((x - reach) // size, (x + reach) // size,
                (z - reach) // size, (z + reach) // size)
        rows = grid._boxes()
        held = self._near_span
        off = _placement.off_range_version
        if (held is None or held[0] is not rows or held[1] is not pads
                or held[2] != span or held[4] != off):
            entries = self._span_entries(grid, pads, span)
            if entries is None:
                return None, moving             # no cell holds it: every mover
            held = self._near_span = (rows, pads, span, entries, off)
        # Each mover filed in those cells, with the box it can reach over its
        # travel padded by the player's pads (_cells_moving), and each off
        # range, in canonical order: those whose box holds the player.
        near = [brush for brush, bx0, by0, bz0, bx1, by1, bz1 in held[3]
                if bx0 <= x <= bx1 and by0 <= y <= by1 and bz0 <= z <= bz1]
        cover = self._cover
        if cover is None:
            cover = self._cover = _Reach()
        cover.x = x
        cover.y = y
        cover.z = z
        # Far from the origin a float32 box is coarser: keep a margin for it.
        cover.room = pads[3] - 1e-6 * max(abs(x), abs(y), abs(z))
        cover.full = (colliders, moving)
        return cover, near

    def _span_entries(self, grid, pads, span):
        """The :meth:`_cells_moving` entries of every cell in *span*
        (``(x0, x1, z0, z1)``), each mover once, in canonical order --
        worked out when the player's cells, the grid's rows or the lists
        change, not every tick -- or None for a span no cell can hold."""
        try:
            x0, x1, z0, z1 = (int(v) for v in span)
        except (ValueError, OverflowError):
            return None                         # a NaN or infinite position
        cells = self._cells_moving(grid, pads)
        if x0 == x1 and z0 == z1 and not off_range:
            return cells.get((x0, z0), ())
        found = {}
        for cx in range(x0, x1 + 1):
            for cz in range(z0, z1 + 1):
                for entry in cells.get((cx, cz), ()):
                    found.setdefault(id(entry[0]), entry)
        place = self._moving_cache[4]
        # A row off its filed travel (brush_placement.off_range) may be
        # anywhere: kept whatever the player's position.
        for brush in off_range.values():
            if id(brush) in place:
                found[id(brush)] = (brush, -math.inf, -math.inf, -math.inf,
                                    math.inf, math.inf, math.inf)
        return tuple(found[key] for key in sorted(found, key=place.__getitem__))

    def _in_order(self, brushes):
        """*brushes* (some listed twice) once each, in canonical order; any
        not in the lists left out."""
        place = self._moving_cache[4]
        seen = {}
        for brush in brushes:
            key = id(brush)
            if key in place and key not in seen:
                seen[key] = brush
        return tuple(seen[key] for key in sorted(seen, key=place.__getitem__))

    def _cells_moving(self, grid, pads):
        """The grid's movers and doors cell by cell, each cell's in canonical
        order, each as ``(brush, x0, y0, z0, x1, y1, z1)``: the box it can
        reach anywhere on the travel the grid filed it for (``_travel_filed``)
        widened by the player's *pads* -- a turning one's by the turned pad
        -- so the player's centre is in it if a pass can reach the brush.
        Worked out again only when the grid's rows, the lists or the pads
        change."""
        rows = grid._boxes()
        cached = self._moving_cells
        if cached is not None and cached[0] is rows and cached[1] is pads:
            return cached[2]
        pad, turned = pads[1], pads[2]
        cells = {}
        for cell, bucket in rows.moving.items():
            entries = []
            for brush in self._in_order(bucket):
                filed = brush.get('_travel_filed')
                if filed is None:
                    box = world_bounds(brush)
                    p = turned if brush.get('rotate') else pad
                else:
                    box = filed[3]
                    p = turned if filed[2] else pad
                entries.append((brush, box[0] - p[0], box[1] - p[1], box[2] - p[2],
                                box[3] + p[0], box[4] + p[1], box[5] + p[2]))
            if entries:
                cells[cell] = entries
        self._moving_cells = (rows, pads, cells)
        return cells

    def _check_duck(self, crouch, aabb_brushes):
        """Duck on the ground with crouch held; stand up only where there is room."""
        if crouch and self.on_ground:
            if not self.ducked:
                eye = self.pos.y + self.camera_height
                self.pos.y -= self._duck_drop()
                self.set_ducked(True)
                # The view starts where the eye was and follows it down.
                self.view_offset += eye - (self.pos.y + self.camera_height)
            return
        if not self.ducked:
            return
        lift = self._duck_drop()
        half = glm.vec3(self.width / 2.0, self.stand_height / 2.0, self.depth / 2.0)
        centre = glm.vec3(self.pos.x, self.pos.y + lift, self.pos.z)
        lo, hi = centre - half, centre + half
        if all(self._has_headroom(b, lo, hi) for b in aabb_brushes):
            eye = self.pos.y + self.camera_height
            self.pos.y += lift
            self.set_ducked(False)
            self.view_offset += eye - (self.pos.y + self.camera_height)

    def get_view_matrix(self):
        """Calculate the view matrix for rendering."""
        cam_pos = self.pos + glm.vec3(0, self.view_height, 0)
        direction = glm.vec3(
            math.sin(self.angle) * math.cos(self.pitch),
            math.sin(self.pitch),
            math.cos(self.angle) * math.cos(self.pitch),
        )
        return glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))

    def update(self, delta, move_input, jump, crouch, brushes, movers=None, doors=None, terrain=None,
           spatial_grid=None, sprint=False, gravity=None):
        """
        Update player physics.

        *gravity* is the live physics' (``engine.physics.world_gravity``);
        None means that world's default.

        PERF: If spatial_grid is provided, static brush colliders are fetched
        from the grid (only nearby cells) instead of iterating every brush.
        Movers and doors are always included since they're dynamic.
        """
        # Before this tick's duck check: a duck made now shows its whole first
        # step of travel next tick, so a full duck takes DUCK_VIEW_TIME.
        self._settle_view(delta)
        # Before anything queries the world with a position that is not one.
        self.leave_void()
        if gravity is None:
            gravity = world_gravity()
        self._gravity = gravity
        # A grounded player receives gravity before collision resolution. Permit
        # the footprint probe to recover that one-tick drop at lower frame rates.
        self._mesh_support_snap_distance = max(
            0.25, abs(float(gravity)) * float(delta) * float(delta) + 1e-3
        )
        # --- Build collider list ---
        if spatial_grid:
            # Use the grid to get only nearby static brushes
            half = self._half
            # The player can carry momentum well above the nominal walk speed
            # (explosions and body contacts both add velocity). Bound the
            # displacement after this tick's maximum movement acceleration,
            # jump impulse and gravity change, not self.speed (the input wish).
            max_wish_speed = max(
                PM_MAXSPEED, abs(float(self.speed)) * WATER_SWIM_SPEED_MULT
            )
            horizontal_accel = max(PM_ACCELERATE, PM_AIRACCELERATE) * max_wish_speed
            pad_x = (abs(float(self.velocity.x)) * delta
                     + horizontal_accel * delta * delta + 32.0)
            pad_z = (abs(float(self.velocity.z)) * delta
                     + horizontal_accel * delta * delta + 32.0)
            max_vertical_impulse = max(JUMP_STRENGTH, WATERJUMP_MAX_BOOST)
            pad_y = ((abs(float(self.velocity.y)) + max_vertical_impulse
                      + abs(float(gravity)) * delta) * delta + 32.0)
            movement_pad = glm.vec3(pad_x, pad_y, pad_z)
            player_min = self.pos - half - movement_pad
            player_max = self.pos + half + movement_pad
            colliders = spatial_grid.get_potential_colliders(player_min, player_max)
        else:
            # Fallback: all brushes (old behaviour)
            colliders = list(brushes)

        # Movers and doors are dynamic, so the static grid does not answer for
        # them: the mover broad phase does (_near_movers), from the columns
        # that hold where they are, and the gather sees only those near the
        # player. A pass that would test beyond what it covers switches to
        # every mover first (_Reach), so the passes answer what they would
        # over every mover and door in the level.
        cover = None
        moving = self._moving(movers, doors)
        if spatial_grid and len(moving) >= self.MOVER_NEAR_FROM:
            cover, moving = self._near_movers(spatial_grid, colliders, moving)
        aabb_brushes, mesh_brushes, mesh_shapes = _gather(colliders, moving)

        # --- Water immersion (before movement so swim physics can use it) ---
        if spatial_grid is not None:
            water_brushes = getattr(spatial_grid, 'water_brushes', None)
            if water_brushes is None:
                water_brushes = [b for b in brushes if is_water_brush(b)]
        else:
            water_brushes = [b for b in brushes if is_water_brush(b)]
        self._update_water_state(water_brushes)
        if self._waterjump_timer > 0.0:
            self._waterjump_timer = max(0.0, self._waterjump_timer - delta)
        if self._waterjump_cooldown > 0.0:
            self._waterjump_cooldown = max(0.0, self._waterjump_cooldown - delta)
        if self.pm_time > 0.0:
            self.pm_time -= delta
            if self.pm_time <= 0.0:
                self.pm_time = 0.0
                self.landing = False

        # --- 1. Movement Physics ---

        forward_vec = glm.vec3(math.sin(self.angle), 0, math.cos(self.angle))
        right_vec   = glm.vec3(math.cos(self.angle), 0, -math.sin(self.angle))

        # The wish as Quake 2 builds it: each key asks for 200 (400 with
        # +speed), so diagonals add up; the forward part shortens by a third
        # of the view pitch; then it is clamped to 300 (100 ducked).
        run = CL_RUN_SCALE if sprint else 1.0
        fmove = max(-1.0, min(1.0, float(move_input.z))) * CL_FORWARDSPEED * run
        smove = max(-1.0, min(1.0, float(move_input.x))) * CL_SIDESPEED * run
        wish = (forward_vec * (fmove * math.cos(self.pitch / 3.0))
                + right_vec * smove)
        wish_speed = glm.length(wish)
        wish_dir = wish / wish_speed if wish_speed > 0.0 else glm.vec3(0.0)
        wish_speed = min(wish_speed, PM_DUCKSPEED if self.ducked else PM_MAXSPEED)

        swimming = self.swimming and self.physics_enabled

        # Preserve the existing kinematic/no-physics path.
        if not self.physics_enabled:
            self._jump_held = bool(jump)
            target_speed = min(wish_speed, PM_DUCKSPEED) if crouch else wish_speed
            if self.in_water:
                target_speed *= WATER_WADE_SPEED_MULT
            self.velocity.x = wish_dir.x * target_speed
            self.velocity.z = wish_dir.z * target_speed
            self.pos += self.velocity * delta
            return

        # The un-duck test stands the hull up a duck higher (still where the
        # tick began: nothing has moved the player since the gather).
        if cover is not None and self.ducked and not cover.holds(
                self.pos.x, self.pos.y + self._duck_drop(), self.pos.z):
            full, cover = cover.full, None
            aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
        self._check_duck(crouch, aabb_brushes + mesh_brushes)

        if swimming:
            self._apply_swim_physics(delta, move_input, right_vec, jump, crouch)
        else:
            if self.in_water:
                wish_speed *= WATER_WADE_SPEED_MULT

            # Ground friction and projected acceleration; weak air
            # acceleration keeps momentum and allows strafing.
            if self.on_ground:
                self._apply_move_friction(delta)
                self._accelerate(wish_dir, wish_speed, PM_ACCELERATE, delta)
            else:
                self._accelerate(wish_dir, wish_speed, PM_AIRACCELERATE, delta)

        # --- 2. Jumping & gravity (gravity suspended while swimming) ---

        if not swimming:
            self._check_jump(jump)

            self.velocity.y += gravity * delta

            if self.velocity.y < TERMINAL_VELOCITY:
                self.velocity.y = TERMINAL_VELOCITY

            # Wading: water resistance breaks a fall almost immediately
            if self.in_water and self.velocity.y < WATER_MAX_SINK_SPEED:
                self.velocity.y = WATER_MAX_SINK_SPEED
        elif not jump:
            self._jump_held = False

        # --- 2b. Waterjump: climbing out of pools ---
        # Holding jump while pushing against a wall whose top edge is near the
        # waterline launches the player hard enough to actually clear it.
        # Works while wading AND swimming; without it, any pool whose rim is
        # taller than a normal jump becomes an inescapable trap.
        if self.in_water and jump:
            # Probes a body's width ahead and a climb above the feet: on
            # every mover (rare enough not to bound it).
            if cover is not None:
                full, cover = cover.full, None
                aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
            self._try_water_jump(aabb_brushes, wish_dir)

        # --- 3. Collision Resolution ---

        was_on_ground = self.on_ground
        fall_speed = float(self.velocity.y)
        mesh_sweep_start = (float(self.pos.x), float(self.pos.y), float(self.pos.z))

        # A. Horizontal movement with AABB collision. This integrates X/Z
        # once, even with an empty brush list. Mesh brushes do not integrate a
        # second time: their already-integrated displacement is swept below.
        # (Each pass tests where its move puts the player, and a step up
        # from there: the broad phase must cover that first.)
        if cover is not None and not cover.holds(
                self.pos.x + self.velocity.x * delta, self.pos.y, self.pos.z):
            full, cover = cover.full, None
            aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
        self._move_with_collision(delta, aabb_brushes, axis='x')
        if cover is not None and not cover.holds(
                self.pos.x, self.pos.y, self.pos.z + self.velocity.z * delta):
            full, cover = cover.full, None
            aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
        self._move_with_collision(delta, aabb_brushes, axis='z')

        # B. Vertical Y Movement
        self.pos.y += self.velocity.y * delta

        # Reset ground state before Y collision check
        self.on_ground     = False
        self.ground_object = None

        # 1. AABB / mover collision for Y
        if cover is not None and not cover.holds(self.pos.x, self.pos.y, self.pos.z):
            full, cover = cover.full, None
            aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
        if cover is None:
            self._resolve_collision(aabb_brushes, axis='y', delta=delta)
        else:
            # A push can carry the player past what the broad phase covers:
            # then the pass runs again from where it began, on every mover.
            saved = (self.pos.y, self.velocity.y)
            if self._resolve_collision(aabb_brushes, axis='y', delta=delta, cover=cover):
                self.pos.y, self.velocity.y = saved
                self.on_ground = False
                self.ground_object = None
                full, cover = cover.full, None
                aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
                self._resolve_collision(aabb_brushes, axis='y', delta=delta)

        # 2. Angled (mesh) brushes: capsule depenetration for X/Y/Z together —
        #    pushes the player out of ramps/wedges and lands them on slopes.
        #    (Likewise: a push past the broad phase runs it again in full.)
        if self._resolve_mesh_capsule(
                mesh_brushes, mesh_shapes, cover,
                sweep_start=mesh_sweep_start, aabb_brushes=aabb_brushes,
                terrain=terrain
        ):
            full, cover = cover.full, None
            aabb_brushes, mesh_brushes, mesh_shapes = _gather(*full)
            self._resolve_mesh_capsule(
                mesh_brushes, mesh_shapes, sweep_start=mesh_sweep_start,
                aabb_brushes=aabb_brushes, terrain=terrain
            )

        # 2. Terrain collision
        if terrain and terrain.is_solid():
            terrain_height = terrain.get_height_at_safe(self.pos.x, self.pos.z)
            if terrain_height is not None:
                feet_y = self.pos.y - self.height / 2.0
                if feet_y < terrain_height:
                    self.pos.y         = terrain_height + self.height / 2.0
                    self.velocity.y    = 0
                    self.on_ground     = True
                    self.ground_object = terrain

        # A landing faster than 200 u/s holds off the next jump briefly.
        if self.on_ground and not was_on_ground and fall_speed < PM_LAND_SPEED:
            self.landing = True
            self.pm_time = (PM_HARD_LAND_TIME if fall_speed < PM_HARD_LAND_SPEED
                            else PM_LAND_TIME)

        # Out of the world: below the level, or not a number.
        self.leave_void()

    # ------------------------------------------------------------------
    # Quake-style ground / air movement
    # ------------------------------------------------------------------

    def _check_jump(self, jump):
        """Quake 2's jump: released between jumps, never just after a hard landing."""
        if self.landing:
            return
        if not jump:
            self._jump_held = False
            return
        if self._jump_held or not self.on_ground:
            return
        self._jump_held = True
        self.velocity.y = max(self.velocity.y + JUMP_STRENGTH, JUMP_STRENGTH)
        self.on_ground = False
        self.ground_object = None

    def _apply_move_friction(self, delta):
        """Quake 2 ground friction on horizontal velocity (6, stop speed 100)."""
        speed = math.hypot(float(self.velocity.x), float(self.velocity.z))
        if speed < 1.0:
            self.velocity.x = 0.0
            self.velocity.z = 0.0
            return

        control = max(speed, PM_STOPSPEED)
        drop = control * PM_FRICTION * delta
        newspeed = max(speed - drop, 0.0)

        if newspeed == 0.0:
            self.velocity.x = 0.0
            self.velocity.z = 0.0
            return

        scale = newspeed / speed
        self.velocity.x *= scale
        self.velocity.z *= scale

    def _accelerate(self, wish_dir, wish_speed, accelerate, delta):
        """Quake 2's projected acceleration step."""
        if wish_speed <= 0.0 or glm.length(wish_dir) < 0.0001:
            return

        current_speed = (
            float(self.velocity.x) * float(wish_dir.x)
            + float(self.velocity.z) * float(wish_dir.z)
        )
        add_speed = wish_speed - current_speed
        if add_speed <= 0.0:
            return

        accel_speed = min(accelerate * delta * wish_speed, add_speed)
        self.velocity.x += accel_speed * wish_dir.x
        self.velocity.z += accel_speed * wish_dir.z

    # ------------------------------------------------------------------
    # Water / swimming
    # ------------------------------------------------------------------

    def _update_water_state(self, water_brushes):
        """Measure how deep the player is in any water volume this frame."""
        feet_y = self.pos.y - self._half.y
        eye_y = self.pos.y + self.camera_height
        px, pz = self.pos.x, self.pos.z

        depth = 0.0
        surface_y = None
        tint = None
        eye_under = False

        for brush in water_brushes:
            if brush.get('hidden'):
                continue
            if ('geometry' in brush or '_mesh_bounds' in brush
                    or 'is_mover' in brush or 'is_door' in brush):
                b_lo_x, b_bot, b_lo_z, b_hi_x, b_top, b_hi_z = world_bounds(brush)
            else:
                # A plain water box (almost every one): the box rule, inline.
                bpos = brush['pos']
                bsize = brush['size']
                hx, hy, hz = bsize[0] * 0.5, bsize[1] * 0.5, bsize[2] * 0.5
                b_lo_x, b_hi_x = bpos[0] - hx, bpos[0] + hx
                b_lo_z, b_hi_z = bpos[2] - hz, bpos[2] + hz
                b_bot, b_top = bpos[1] - hy, bpos[1] + hy
            if not (b_lo_x <= px <= b_hi_x and b_lo_z <= pz <= b_hi_z):
                continue

            overlap_top = min(eye_y, b_top)
            overlap_bot = max(feet_y, b_bot)
            if overlap_top <= overlap_bot:
                continue

            depth = max(depth, overlap_top - overlap_bot)
            if surface_y is None or b_top > surface_y:
                surface_y = b_top
                tint = brush.get('water_tint')
            if b_bot <= eye_y <= b_top:
                eye_under = True

        span = max(eye_y - feet_y, 0.001)
        frac = depth / span
        self.water_depth_frac = frac
        self.in_water = frac > 0.12
        self.swimming = frac > 0.55
        self.eye_underwater = eye_under
        self.water_surface_y = surface_y
        if tint is not None:
            self.water_tint = list(tint)

    def _apply_swim_physics(self, delta, move_input, right_vec, jump, crouch):
        """Buoyant, drag-damped movement while submerged.

        Forward motion follows the view pitch (look down + forward = dive),
        jump swims up, crouch sinks, and idling drifts gently toward a
        floating position at the surface. Velocity converges on the swim
        target through drag instead of snapping, which gives water its
        characteristic weight.
        """
        cos_p = math.cos(self.pitch)
        swim_fwd = glm.vec3(math.sin(self.angle) * cos_p,
                            math.sin(self.pitch),
                            math.cos(self.angle) * cos_p)

        wish = swim_fwd * move_input.z + right_vec * move_input.x
        if glm.length(wish) > 0.1:
            wish = glm.normalize(wish)

        swim_speed = self.speed * WATER_SWIM_SPEED_MULT
        target = wish * swim_speed

        vertical_speed = swim_speed * WATER_VERTICAL_SPEED_MULT
        if jump:
            target.y += vertical_speed
        if crouch:
            target.y -= vertical_speed

        # Idle buoyancy: bob up until the eyes sit just above the surface
        if not jump and not crouch and abs(move_input.z) < 0.1 and self.water_surface_y is not None:
            float_target_y = self.water_surface_y - self.camera_height * 0.35
            err = float_target_y - self.pos.y
            target.y += max(min(err * 1.8, 40.0), -25.0)

        k = min(1.0, WATER_DRAG * delta)
        self.velocity.x += (target.x - self.velocity.x) * k
        self.velocity.z += (target.z - self.velocity.z) * k
        # A waterjump launch is ballistic: leave its vertical arc alone or
        # drag would smother the boost before the player clears the edge
        if self._waterjump_timer <= 0.0:
            self.velocity.y += (target.y - self.velocity.y) * k

        self.on_ground = False
        self.ground_object = None

    def _try_water_jump(self, aabb_brushes, wish_dir):
        """Vault out of water onto a nearby ledge.

        Probes one step ahead in the movement direction for a solid wall whose
        top edge is (a) above the feet, (b) within climbing reach, and (c) not
        far above the waterline, then launches with exactly the vertical speed
        needed for the feet to clear that edge. This is what lets the player
        get OUT of a pool: the rim is usually taller than a normal jump from
        the pool floor, and swimming alone can never lift the body over it.
        """
        if self._waterjump_cooldown > 0.0:
            return
        if glm.length(wish_dir) < 0.1:
            return  # must be pushing toward the edge, not just treading water

        feet_y = self.pos.y - self._half.y
        surface_y = self.water_surface_y if self.water_surface_y is not None else feet_y
        body_r = max(self._half.x, self._half.z)

        # Two probe depths so thin rims aren't stepped over by a single point
        for probe_dist in (body_r + 10.0, body_r + 26.0):
            px = self.pos.x + wish_dir.x * probe_dist
            pz = self.pos.z + wish_dir.z * probe_dist

            # Highest climbable ledge at this probe point
            ledge_top = None
            for brush in aabb_brushes:
                b = _water_bounds(brush)
                if not (b[0] <= px <= b[3] and b[2] <= pz <= b[5]):
                    continue
                top = b[4]
                bottom = b[1]
                if bottom > feet_y + 8.0:
                    continue  # floating overhang, not a wall rising from below
                if top <= feet_y + 4.0:
                    continue  # already below our feet
                if top > feet_y + WATERJUMP_MAX_CLIMB:
                    continue  # too tall to vault — needs stairs
                if top > surface_y + WATERJUMP_EDGE_ABOVE_SURFACE:
                    continue  # rim too far above the waterline
                if ledge_top is None or top > ledge_top:
                    ledge_top = top

            if ledge_top is None:
                continue

            # Headroom: the player must fit standing on the ledge
            blocked = False
            for brush in aabb_brushes:
                b = _water_bounds(brush)
                if not (b[0] <= px <= b[3] and b[2] <= pz <= b[5]):
                    continue
                top = b[4]
                bottom = b[1]
                if top > ledge_top + 4.0 and bottom < ledge_top + self.height:
                    blocked = True
                    break
            if blocked:
                continue

            rise = (ledge_top - feet_y) + 12.0
            self.velocity.y = min(math.sqrt(2.0 * abs(self._gravity) * rise), WATERJUMP_MAX_BOOST)
            self._waterjump_timer = 0.6
            self._waterjump_cooldown = 0.7
            return

    def _move_with_collision(self, delta, aabb_brushes, axis):
        """
        Handles movement along a specific axis with collision detection.
        """
        # 1. Determine velocity component based on axis
        if axis == 'z':
            move_val = self.velocity.z * delta
        elif axis == 'x':
            move_val = self.velocity.x * delta
        else:
            move_val = self.velocity.y * delta

        if abs(move_val) < 0.0001:
            return

        # Store current position to revert if collision occurs
        orig_pos = glm.vec3(self.pos)
        
        # 2. Apply movement
        if axis == 'z':
            self.pos.z += move_val
        elif axis == 'x':
            self.pos.x += move_val
        else:
            self.pos.y += move_val

        # 3. Calculate current player bounds using individual dimensions
        half_dims = glm.vec3(self.width * 0.5, self.height * 0.5, self.depth * 0.5)
        p_min = self.pos - half_dims
        p_max = self.pos + half_dims

        # 4. Check for collisions with potential colliders
        for brush in aabb_brushes:
            # Check headroom using the fixed signature (brush, p_min, p_max)
            if not self._has_headroom(brush, p_min, p_max):
                # Blocked — before treating this as a wall, try the automatic
                # stair "lip": a low obstacle is climbed instead of walked into.
                if axis in ('x', 'z') and self._try_step_up(aabb_brushes, orig_pos):
                    return
                # Collision detected: revert position and stop movement
                self.pos = orig_pos
                if axis == 'z':
                    self.velocity.z = 0
                elif axis == 'x':
                    self.velocity.x = 0
                else:
                    self.velocity.y = 0
                return

    def _try_step_up(self, aabb_brushes, orig_pos):
        """
        Automatic stair "lip": when horizontal movement is blocked, check
        whether every blocking brush is a low step (top edge no more than
        step_height above the feet). If so — and there is headroom to stand
        on it — lift the player onto the step and let the move stand.

        Called with self.pos already at the blocked destination. Returns
        True if the player was stepped up (caller keeps the move), False
        if this is a real wall (caller reverts as before).
        """
        # Quake 2 steps in the air too (how a jump lands on a ledge a step
        # above its peak); never while swimming.
        if self.swimming:
            return False

        half = self._half
        feet_y = orig_pos.y - half.y
        p_min = self.pos - half
        p_max = self.pos + half

        # Every brush blocking the destination must qualify as a step;
        # a single taller wall means this is not a staircase lip.
        step_top = None
        for brush in aabb_brushes:
            if self._has_headroom(brush, p_min, p_max):
                continue
            if ('geometry' in brush or '_mesh_bounds' in brush
                    or 'is_mover' in brush or 'is_door' in brush):
                top = world_bounds(brush)[4]
            else:
                # A plain box -- most steps: world_bounds' box rule, inline.
                top = brush['pos'][1] + brush['size'][1] * 0.5
            rise = top - feet_y
            if rise <= 0.0 or rise > self.step_height:
                return False
            if step_top is None or top > step_top:
                step_top = top
        if step_top is None:
            return False

        # Headroom: the player must fit standing with feet on the step
        lifted_y = step_top + half.y + 0.001
        l_min = glm.vec3(p_min.x, lifted_y - half.y, p_min.z)
        l_max = glm.vec3(p_max.x, lifted_y + half.y, p_max.z)
        for brush in aabb_brushes:
            if not self._has_headroom(brush, l_min, l_max):
                return False

        self.pos.y = lifted_y
        return True

    def _capsule_offsets(self):
        """Vertical sample points + radius approximating the player as a capsule.

        The player is a 50x100x50 box; a single centre sphere (radius ~half
        width) leaves the feet 50 units below it, so on a ramp the body sinks
        in until that centre sphere finally touches the slope.  Sampling the
        vertical axis at feet / mid / head — each a sphere of the footprint's
        inscribed radius — lets the lowest sphere rest the feet on the surface
        and the upper spheres block the torso against angled walls.
        """
        r = min(self._half.x, self._half.z)
        span = max(self._half.y - r, 0.0)
        offsets = (glm.vec3(0.0, -span, 0.0),
                   glm.vec3(0.0, 0.0, 0.0),
                   glm.vec3(0.0, span, 0.0))
        return offsets, r

    def _resolve_mesh_capsule(self, mesh_brushes, mesh_shapes=None, cover=None,
                              sweep_start=None, _resolve_models=True,
                              aabb_brushes=None, terrain=None):
        """Resolve the player capsule against angled (mesh) brushes by pushing
        it out of any penetration, and set ground/ceiling state from the
        contact normals.

        This runs after the shared X/Y/Z position integration. Model surfaces
        are swept from the frame's starting position to the integrated endpoint
        before discrete depenetration, preventing a fast player from crossing a
        thin triangle wall between ticks.

        Convex brushes remain solids and use their separating planes: the plane
        of greatest signed distance gives the minimum push out, including when
        the capsule centre is inside the solid. Non-convex model Props instead
        use their actual triangle surfaces. The two constraint sets are
        alternated after contact so a model correction cannot leave the player
        embedded in a convex brush.

        With *cover* (the tick's mover broad phase, :class:`_Reach`), a
        relaxation pass that leaves the covered area restores position,
        velocity and ground state to their entry values and returns True; the
        caller then retries against the complete mover list. Model corrections
        also recheck AABBs, alternating both constraint sets when a slide
        redirects the player into a box.
        """
        if not mesh_brushes:
            return False
        # The capsule's spheres as plain floats, worked out again only when
        # the hull changes (a duck): _capsule_offsets' values.
        half = self._half
        hull = (half.x, half.y, half.z)
        cached = getattr(self, '_capsule_cache', None)
        if cached is None or cached[0] != hull:
            offsets, radius = self._capsule_offsets()
            cached = self._capsule_cache = (
                hull, [(float(o.x), float(o.y), float(o.z)) for o in offsets], radius)
        offsets, radius = cached[1], cached[2]

        # Each shape once per resolve (*mesh_shapes*: what the tick's gather
        # already asked collision_shape for these brushes): its planes and
        # bounds in its own frame, the frame a world query is carried into
        # (None: world space), and the capsule's sphere offsets as that frame
        # sees them -- a direction is only turned, never moved -- so a pass
        # carries the player's centre into the frame once, not every sphere.
        if mesh_shapes is None:
            mesh_shapes = [collision_shape(brush) for brush in mesh_brushes]
        shapes = []
        model_meshes = []
        for brush, shape in zip(mesh_brushes, mesh_shapes):
            if shape is None:
                continue
            planes, triangles, bounds, frame = shape
            # A model Prop is an arbitrary surface mesh, not a convex solid.
            # Its triangle list is authoritative; an empty plane list must not
            # make the player discard it. Model triangles are already in world
            # space (compute_model_collision_mesh applies the Prop transform).
            if (brush.get('_model_collision') and not planes and triangles
                    and frame is None):
                model_meshes.append((brush, triangles, bounds))
                continue
            if not planes:
                continue
            # A turned frame's offsets are worked out when a sphere first
            # reaches its bounds (most shapes in reach never are).
            turned = offsets if frame is None or frame.r is None else None
            shapes.append([brush, planes, bounds, frame, turned])
        entry_pos = (float(self.pos.x), float(self.pos.y), float(self.pos.z))
        entry_velocity = (float(self.velocity.x), float(self.velocity.y),
                          float(self.velocity.z))
        entry_on_ground = self.on_ground
        entry_ground_object = self.ground_object

        def restore_entry():
            self.pos = glm.vec3(*entry_pos)
            self.velocity = glm.vec3(*entry_velocity)
            self.on_ground = entry_on_ground
            self.ground_object = entry_ground_object

        def rollback_unresolved():
            # A non-convergent constraint set must not publish a penetrated
            # endpoint. Fall back to the frame start, retaining any velocity
            # component already clipped by the surface contact.
            resolved_velocity = (float(self.velocity.x), float(self.velocity.y),
                                 float(self.velocity.z))
            if sweep_start is None:
                fallback = entry_pos
            elif hasattr(sweep_start, "x"):
                fallback = (float(sweep_start.x), float(sweep_start.y),
                            float(sweep_start.z))
            else:
                fallback = tuple(float(v) for v in sweep_start)
            self.pos = glm.vec3(*fallback)
            self.velocity = glm.vec3(*resolved_velocity)
            self.on_ground = False
            self.ground_object = None
            self._apply_final_ground_state(
                model_meshes, shapes, aabb_brushes, offsets, radius, terrain
            )

        def apply_final_ground_state():
            if _resolve_models:
                self._apply_final_ground_state(
                    model_meshes, shapes, aabb_brushes, offsets, radius,
                    terrain
                )

        start = entry_pos if cover is not None else None

        # Sweep the frame displacement before discrete depenetration; otherwise
        # a fast player can cross a thin model wall completely between ticks.
        if _resolve_models and model_meshes and sweep_start is not None:
            # A model stair's riser and tread are separate triangles. When a
            # horizontal move meets the riser, step onto the tread first and
            # raise the sweep origin by the same amount so the face sweep
            # follows the raised path rather than colliding with the riser.
            step_rise = self._try_step_up_model_meshes(
                model_meshes, aabb_brushes, sweep_start
            )
            if step_rise:
                if hasattr(sweep_start, "x"):
                    sweep_start = (
                        float(sweep_start.x), float(sweep_start.y) + step_rise,
                        float(sweep_start.z)
                    )
                else:
                    sweep_start = (
                        float(sweep_start[0]), float(sweep_start[1]) + step_rise,
                        float(sweep_start[2])
                    )
            self._sweep_model_triangle_meshes(
                model_meshes, offsets, radius, sweep_start
            )
            if cover is not None and not cover.holds(
                    self.pos.x, self.pos.y, self.pos.z):
                restore_entry()
                return True

        def finish_model_constraints():
            if not _resolve_models or not model_meshes:
                # Preserve the established convex-only relaxation path. The
                # final cross-constraint validation is required when model
                # corrections have introduced another constraint interaction.
                return None
            # Model corrections can re-enter convex brushes or AABBs. Resolve
            # all three constraint types together until the endpoint is stable.
            for _ in range(8):
                before = (float(self.pos.x), float(self.pos.y), float(self.pos.z))
                model_retry = self._resolve_model_triangle_meshes(
                    model_meshes, offsets, radius, cover, start
                )
                if model_retry is True:
                    return "retry"
                if shapes and self._resolve_mesh_capsule(
                        mesh_brushes, mesh_shapes, cover,
                        _resolve_models=False, terrain=terrain
                ):
                    return "retry"
                if aabb_brushes:
                    for axis in ("x", "y", "z"):
                        if self._resolve_collision(
                                aabb_brushes, axis=axis, delta=0.0, cover=cover):
                            return "retry"
                after = (float(self.pos.x), float(self.pos.y), float(self.pos.z))
                if sum((after[i] - before[i]) ** 2 for i in range(3)) <= 1e-8:
                    break
            # Exhausting the relaxation limit is not success. Never leave a
            # residual model penetration hidden behind the pass cap.
            if self._model_mesh_max_penetration(
                    model_meshes, offsets, radius) > 1e-4:
                return "unresolved"
            if self._convex_max_penetration(shapes, offsets, radius) > 1e-4:
                return "unresolved"
            if aabb_brushes:
                player_min = self.pos - self._half
                player_max = self.pos + self._half
                if any(not self._has_headroom(brush, player_min, player_max)
                       for brush in aabb_brushes):
                    return "unresolved"
            return None

        if not shapes:
            status = finish_model_constraints()
            if status == "retry":
                restore_entry()
                return True
            if status == "unresolved":
                rollback_unresolved()
                return False
            apply_final_ground_state()
            return False

        ground_normal = None      # steepest-up contact this resolve
        ground_brush = None       # ... and the shape it came from
        wall_normal = None        # last horizontal contact, for velocity slide
        hit_ceiling = False
        half = self._half
        # The capsule's box turned into a shape's frame is no longer axis
        # aligned there: bound it by its circumscribed radius instead.
        hx, hy, hz = float(half.x), float(half.y), float(half.z)
        reach = math.sqrt(hx * hx + hy * hy + hz * hz)

        # A few relaxation passes: each pushes out of the deepest overlap, then
        # re-tests, so a capsule touching several faces settles cleanly.
        for _ in range(4):
            if cover is not None and not cover.holds(self.pos.x, self.pos.y, self.pos.z):
                restore_entry()
                return True
            deepest = 1e-4
            push = None
            push_n = None
            push_brush = None
            wx, wy, wz = self.pos.x, self.pos.y, self.pos.z
            for shape in shapes:
                brush, planes, bounds, frame, turned = shape
                if frame is None:
                    px, py, pz = wx, wy, wz
                elif frame.r is None:
                    # Only translated (a still angled brush in its local
                    # frame, a carried one): one offset.
                    px, py, pz = wx + frame.ox, wy + frame.oy, wz + frame.oz
                else:
                    px, py, pz = frame.to_shape(wx, wy, wz)
                if bounds:
                    mn, mx = bounds
                    if frame is None:
                        pmn = self.pos - half
                        pmx = self.pos + half
                        if (pmx.x < mn[0] - radius or pmn.x > mx[0] + radius or
                            pmx.y < mn[1] - radius or pmn.y > mx[1] + radius or
                            pmx.z < mn[2] - radius or pmn.z > mx[2] + radius):
                            continue
                    else:
                        if frame.r is None:
                            # Only translated (a still angled brush in its
                            # local frame, a carried one): the box stays
                            # axis aligned, so the tight test applies.
                            ex, ey, ez = hx + radius, hy + radius, hz + radius
                        else:
                            ex = ey = ez = reach + radius
                        if (px < mn[0] - ex or px > mx[0] + ex or
                            py < mn[1] - ey or py > mx[1] + ey or
                            pz < mn[2] - ez or pz > mx[2] + ez):
                            continue
                if turned is None:
                    turned = shape[4] = [frame.dir_to_shape(ox, oy, oz)
                                         for ox, oy, oz in offsets]
                for ox, oy, oz in turned:
                    # The sphere's centre in the shape's frame.
                    cx = px + ox
                    cy = py + oy
                    cz = pz + oz
                    # Sphere vs convex: largest signed distance across faces.
                    best_sd = -1e30
                    best_n = None
                    separated = False
                    for (nx, ny, nz, d) in planes:
                        sd = nx * cx + ny * cy + nz * cz - d
                        if sd > radius:
                            separated = True   # a separating plane exists
                            break
                        if sd > best_sd:
                            best_sd = sd
                            best_n = (nx, ny, nz)
                    if separated or best_n is None:
                        continue
                    pen = radius - best_sd
                    if pen > deepest:
                        if frame is not None and frame.r is not None:
                            # The face normal, in world space (a translation
                            # leaves a direction as it is).
                            best_n = frame.to_world_dir(*best_n)
                        deepest = pen
                        push = (best_n[0] * pen, best_n[1] * pen, best_n[2] * pen)
                        push_n = best_n
                        push_brush = brush

            if push is None:
                break

            self.pos.x += push[0]
            self.pos.y += push[1]
            self.pos.z += push[2]

            if push_n[1] >= PM_GROUND_NORMAL:
                if ground_normal is None or push_n[1] > ground_normal[1]:
                    ground_normal = push_n
                    ground_brush = push_brush
            elif push_n[1] < -0.3:
                hit_ceiling = True
            else:
                wall_normal = push_n

        # Apply the resulting contact state to velocity + ground flags.
        if ground_normal is not None:
            if self.velocity.y < 0.0:
                self.velocity.y = 0.0
            self.on_ground = True
            # The shape the player stands on: a mover carries what it holds.
            self.ground_object = ground_brush
        if hit_ceiling and self.velocity.y > 0.0:
            self.velocity.y = 0.0
        if wall_normal is not None:
            # Cancel the horizontal velocity heading into the wall so the
            # player slides along it instead of jamming.
            vn = self.velocity.x * wall_normal[0] + self.velocity.z * wall_normal[2]
            if vn < 0.0:
                self.velocity.x -= wall_normal[0] * vn
                self.velocity.z -= wall_normal[2] * vn
        status = finish_model_constraints()
        if status == "retry":
            restore_entry()
            return True
        if status == "unresolved":
            rollback_unresolved()
            return False
        apply_final_ground_state()
        return False

    def _try_step_up_model_meshes(self, model_meshes, aabb_brushes, start):
        """Step onto a nearby model tread when its mesh blocks horizontal travel.

        Model collision is a non-convex surface mesh, so its riser cannot use
        the AABB resolver's top bound. Find an actual walkable triangle within
        the capsule footprint, lift by no more than the normal step height, and
        require the complete standing capsule to clear every nearby shape.
        """
        if self.swimming or not model_meshes:
            return 0.0
        if hasattr(start, "x"):
            start_x, start_y, start_z = (
                float(start.x), float(start.y), float(start.z)
            )
        else:
            start_x, start_y, start_z = (float(v) for v in start)
        if ((float(self.pos.x) - start_x) ** 2
                + (float(self.pos.z) - start_z) ** 2 <= 1e-8):
            return 0.0

        half = self._half
        player_min = glm.vec3(
            self.pos.x - half.x, self.pos.y - half.y, self.pos.z - half.z
        )
        player_max = glm.vec3(
            self.pos.x + half.x, self.pos.y + half.y, self.pos.z + half.z
        )
        blocked = []
        for brush, triangles, _bounds in model_meshes:
            if not self._has_headroom(brush, player_min, player_max):
                blocked.append((brush, triangles))
        if not blocked:
            return 0.0

        feet_y = start_y - float(half.y)
        radius = min(float(half.x), float(half.z))
        radius_sq = radius * radius
        step_top = None
        for _brush, triangles in blocked:
            brush_top = None
            for triangle, normal in triangles:
                # Face winding is not authoritative for a model's collision
                # surface (reversed OBJ decks are supported elsewhere too).
                if abs(float(normal[1])) < PM_GROUND_NORMAL:
                    continue
                top, distance_sq = _closest_point_on_triangle_xz(
                    float(self.pos.x), float(self.pos.z), triangle
                )
                if distance_sq > radius_sq + 1e-6:
                    continue
                rise = top - feet_y
                if rise <= 1e-4 or rise > self.step_height + 1e-4:
                    continue
                if brush_top is None or top > brush_top:
                    brush_top = top
            # Every mesh that blocks this destination must provide a real
            # tread; a nearby low surface cannot hide a separate tall wall.
            if brush_top is None:
                return 0.0
            if step_top is None or brush_top > step_top:
                step_top = brush_top

        if step_top is None:
            return 0.0
        lifted_y = max(
            float(self.pos.y), step_top + float(half.y) + 1e-3
        )
        rise = lifted_y - float(self.pos.y)
        if rise <= 1e-4:
            return 0.0

        lifted_min = glm.vec3(
            player_min[0], lifted_y - half.y, player_min[2]
        )
        lifted_max = glm.vec3(
            player_max[0], lifted_y + half.y, player_max[2]
        )
        # The lift happens where the capsule starts this tick and the sweep
        # then carries it forward, so the lifted hull must be clear there as
        # well as at the destination: a ceiling edge cleared at the end but
        # not at the start would be crossed by the lift and swept out of
        # again, where nothing afterwards can see it. It is lifted there by
        # the same rise from its own height, which differs from the
        # destination's when it fell (or rose) this tick.
        start_lifted_y = start_y + rise
        start_min = glm.vec3(
            start_x - half.x, start_lifted_y - half.y, start_z - half.z
        )
        start_max = glm.vec3(
            start_x + half.x, start_lifted_y + half.y, start_z + half.z
        )
        for lo, hi in ((lifted_min, lifted_max), (start_min, start_max)):
            for brush, _triangles, _bounds in model_meshes:
                if not self._has_headroom(brush, lo, hi):
                    return 0.0
            for brush in aabb_brushes or ():
                if not self._has_headroom(brush, lo, hi):
                    return 0.0

        self.pos.y = lifted_y
        return rise

    def _sweep_model_triangle_meshes(self, model_meshes, offsets, radius, sweep_start):
        """Sweep the capsule through its integrated displacement against model triangles."""
        if not model_meshes:
            return False
        if hasattr(sweep_start, "x"):
            position = (float(sweep_start.x), float(sweep_start.y),
                        float(sweep_start.z))
        else:
            position = tuple(float(v) for v in sweep_start)
        target = (float(self.pos.x), float(self.pos.y), float(self.pos.z))
        remaining = (target[0] - position[0],
                     target[1] - position[1],
                     target[2] - position[2])
        if sum(v * v for v in remaining) <= 1e-12:
            return False

        hit_any = False
        half = self._half
        for _ in range(4):
            if sum(v * v for v in remaining) <= 1e-12:
                break
            best = None
            sweep_min = (
                min(position[0], position[0] + remaining[0]) - half.x - radius,
                min(position[1], position[1] + remaining[1]) - half.y - radius,
                min(position[2], position[2] + remaining[2]) - half.z - radius,
            )
            sweep_max = (
                max(position[0], position[0] + remaining[0]) + half.x + radius,
                max(position[1], position[1] + remaining[1]) + half.y + radius,
                max(position[2], position[2] + remaining[2]) + half.z + radius,
            )
            sweep_inputs = []
            movement_vec = glm.vec3(*remaining)
            for ox, oy, oz in offsets:
                sphere_min = (
                    min(position[0], position[0] + remaining[0]) + ox - radius,
                    min(position[1], position[1] + remaining[1]) + oy - radius,
                    min(position[2], position[2] + remaining[2]) + oz - radius,
                )
                sphere_max = (
                    max(position[0], position[0] + remaining[0]) + ox + radius,
                    max(position[1], position[1] + remaining[1]) + oy + radius,
                    max(position[2], position[2] + remaining[2]) + oz + radius,
                )
                sphere_start = glm.vec3(position[0] + ox,
                                        position[1] + oy,
                                        position[2] + oz)
                sweep_inputs.append((sphere_min, sphere_max, sphere_start))

            for brush, triangles, bounds in model_meshes:
                if bounds:
                    mn, mx = bounds
                    if (sweep_max[0] < mn[0] or sweep_min[0] > mx[0]
                            or sweep_max[1] < mn[1] or sweep_min[1] > mx[1]
                            or sweep_max[2] < mn[2] or sweep_min[2] > mx[2]):
                        continue
                for tri, face_normal in triangles:
                    v0, v1, v2 = tri
                    tri_min = (min(v0[0], v1[0], v2[0]),
                               min(v0[1], v1[1], v2[1]),
                               min(v0[2], v1[2], v2[2]))
                    tri_max = (max(v0[0], v1[0], v2[0]),
                               max(v0[1], v1[1], v2[1]),
                               max(v0[2], v1[2], v2[2]))
                    for sphere_min, sphere_max, sphere_start in sweep_inputs:
                        if (sphere_max[0] < tri_min[0] or sphere_min[0] > tri_max[0]
                                or sphere_max[1] < tri_min[1] or sphere_min[1] > tri_max[1]
                                or sphere_max[2] < tri_min[2] or sphere_min[2] > tri_max[2]):
                            continue
                        hit, hit_time, _point, normal = _intersect_swept_sphere_triangle(
                            sphere_start, movement_vec, radius, tri, face_normal,
                            capsule_centre=glm.vec3(*position)
                        )
                        if hit and (best is None or hit_time < best[0]):
                            best = (hit_time, (float(normal.x), float(normal.y),
                                               float(normal.z)), brush,
                                    face_normal)

            if best is None:
                position = (position[0] + remaining[0],
                            position[1] + remaining[1],
                            position[2] + remaining[2])
                remaining = (0.0, 0.0, 0.0)
                break

            hit_any = True
            hit_time, normal, brush, face_normal = best
            normal = _ground_slide_normal(normal, face_normal)
            position = (position[0] + remaining[0] * hit_time + normal[0] * 1e-3,
                        position[1] + remaining[1] * hit_time + normal[1] * 1e-3,
                        position[2] + remaining[2] * hit_time + normal[2] * 1e-3)
            remaining = (remaining[0] * (1.0 - hit_time),
                         remaining[1] * (1.0 - hit_time),
                         remaining[2] * (1.0 - hit_time))
            into = (remaining[0] * normal[0] + remaining[1] * normal[1]
                    + remaining[2] * normal[2])
            if into < 0.0:
                remaining = (remaining[0] - normal[0] * into,
                             remaining[1] - normal[1] * into,
                             remaining[2] - normal[2] * into)
            velocity_into = (self.velocity.x * normal[0]
                             + self.velocity.y * normal[1]
                             + self.velocity.z * normal[2])
            if velocity_into < 0.0:
                self.velocity.x -= normal[0] * velocity_into
                self.velocity.y -= normal[1] * velocity_into
                self.velocity.z -= normal[2] * velocity_into
            if normal[1] <= -0.3 and self.velocity.y > 0.0:
                self.velocity.y = 0.0

        self.pos = glm.vec3(*position)
        return hit_any

    def _find_model_mesh_support(self, model_meshes, offsets, radius,
                                 clearance=None):
        """Return (supporting model, outward contact normal), if supported.

        A tread found under the hull's footprint lifts the player onto it
        (the snap below). *clearance*, if given, is asked whether the hull is
        clear of everything once lifted; when it is not, the lift is undone
        and that tread does not count as support this tick.
        """
        if not model_meshes or not offsets:
            return None
        ox, oy, oz = min(offsets, key=lambda offset: offset[1])
        sphere = (float(self.pos.x + ox), float(self.pos.y + oy),
                  float(self.pos.z + oz))
        centre = (float(self.pos.x), float(self.pos.y), float(self.pos.z))
        velocity = (float(self.velocity.x), float(self.velocity.y),
                    float(self.velocity.z))
        support_radius = radius + 0.25
        limit_sq = support_radius * support_radius
        feet_y = float(self.pos.y - self._half.y)
        best_normal_y = PM_GROUND_NORMAL
        best_brush = None
        best_normal = None
        best_footprint_top = None
        # Treads that reach under the hull's edge but not under its centre:
        # (normal_y, brush, normal, top), decided once the loop is done.
        edge_treads = []
        # The best support by capsule contact alone, which needs no lift:
        # what remains if a footprint tread's lift is refused.
        contact_best = None
        support_snap_distance = max(
            0.25, float(self._mesh_support_snap_distance)
        )

        for brush, triangles, bounds in model_meshes:
            if bounds:
                mn, mx = bounds
                if (sphere[0] + support_radius < mn[0] or sphere[0] - support_radius > mx[0]
                        or sphere[1] + support_radius < mn[1] or sphere[1] - support_radius > mx[1]
                        or sphere[2] + support_radius < mn[2] or sphere[2] - support_radius > mx[2]):
                    continue
            for tri, face_normal in triangles:
                v0, v1, v2 = tri
                tri_min = (min(v0[0], v1[0], v2[0]),
                           min(v0[1], v1[1], v2[1]),
                           min(v0[2], v1[2], v2[2]))
                tri_max = (max(v0[0], v1[0], v2[0]),
                           max(v0[1], v1[1], v2[1]),
                           max(v0[2], v1[2], v2[2]))
                if (sphere[0] + support_radius < tri_min[0] or sphere[0] - support_radius > tri_max[0]
                        or sphere[1] + support_radius < tri_min[1] or sphere[1] - support_radius > tri_max[1]
                        or sphere[2] + support_radius < tri_min[2] or sphere[2] - support_radius > tri_max[2]):
                    continue
                closest = _closest_point_on_triangle(sphere, v0, v1, v2)
                delta = (sphere[0] - closest[0], sphere[1] - closest[1],
                         sphere[2] - closest[2])
                distance_sq = sum(v * v for v in delta)
                if distance_sq <= limit_sq:
                    distance = math.sqrt(max(0.0, distance_sq))
                    contact_normal = _model_triangle_contact_normal(
                        delta, distance, face_normal, closest, centre, velocity
                    )
                    if contact_normal[1] > best_normal_y:
                        best_normal_y = contact_normal[1]
                        best_brush = brush
                        best_normal = contact_normal
                        best_footprint_top = None
                    if (contact_normal[1] > PM_GROUND_NORMAL
                            and (contact_best is None
                                 or contact_normal[1] > contact_best[0])):
                        contact_best = (contact_normal[1], brush, contact_normal)

                # The player hull has a flat supporting footprint. At the
                # leading edge of a stepped tread, the lower capsule sphere
                # can be more than one radius from the triangle even though
                # the tread is directly under the overlapping hull. Only
                # inspect walkable triangles whose height range reaches the
                # feet plane; then use the actual triangle projection.
                if (abs(float(face_normal[1])) >= PM_GROUND_NORMAL
                        and tri_min[1] - support_snap_distance <= feet_y
                        <= tri_max[1] + 0.25):
                    top, footprint_distance_sq = _closest_point_on_triangle_xz(
                        centre[0], centre[2], tri
                    )
                    support_gap = top - feet_y
                    if (footprint_distance_sq <= radius * radius + 1e-6
                            and -0.25 <= support_gap <= support_snap_distance + 1e-6):
                        support_point = (centre[0], top, centre[2])
                        contact_normal = _oriented_triangle_normal(
                            face_normal, support_point, centre, velocity
                        )
                        if footprint_distance_sq > 1e-6:
                            edge_treads.append(
                                (contact_normal[1], brush, contact_normal, top))
                        elif contact_normal[1] > best_normal_y:
                            best_normal_y = contact_normal[1]
                            best_brush = brush
                            best_normal = contact_normal
                            best_footprint_top = top
        # A tread under only the edge of the hull holds the player on a
        # staircase -- where there is a lower tread under its centre, within
        # a step -- but not over a ledge: a player whose centre has gone past
        # the edge of a deck, with nothing within a step below it, falls.
        if edge_treads and self._model_mesh_ground_beneath(
                model_meshes, centre[0], centre[2], feet_y):
            for normal_y, brush, contact_normal, top in edge_treads:
                if normal_y > best_normal_y:
                    best_normal_y = normal_y
                    best_brush = brush
                    best_normal = contact_normal
                    best_footprint_top = top
        # Snap only downward/inward motion onto the actual accepted surface.
        # Positive separating velocity is left untouched so jumping still detaches.
        if (best_footprint_top is not None and velocity[1] <= 1e-3
                and feet_y < best_footprint_top):
            lowered = self.pos.y
            self.pos.y = best_footprint_top + float(self._half.y)
            if clearance is not None and not clearance():
                # Lifted into a ceiling, a railing or a wall: stay put, and
                # stand only on what the capsule itself touches.
                self.pos.y = lowered
                if contact_best is None:
                    return None
                return (contact_best[1], contact_best[2])
        return (best_brush, best_normal) if best_brush is not None else None

    def _model_mesh_ground_beneath(self, model_meshes, x, z, feet_y):
        """Whether a walkable model triangle lies under (x, z) within a step
        below *feet_y* (or just above it)."""
        low = feet_y - float(self.step_height) - 1e-3
        high = feet_y + 0.25
        for _brush, triangles, bounds in model_meshes:
            if bounds:
                mn, mx = bounds
                if (x < mn[0] or x > mx[0] or z < mn[2] or z > mx[2]
                        or mx[1] < low or mn[1] > high):
                    continue
            for tri, face_normal in triangles:
                if abs(float(face_normal[1])) < PM_GROUND_NORMAL:
                    continue
                v0, v1, v2 = tri
                if (max(v0[1], v1[1], v2[1]) < low
                        or min(v0[1], v1[1], v2[1]) > high
                        or x < min(v0[0], v1[0], v2[0])
                        or x > max(v0[0], v1[0], v2[0])
                        or z < min(v0[2], v1[2], v2[2])
                        or z > max(v0[2], v1[2], v2[2])):
                    continue
                top, distance_sq = _closest_point_on_triangle_xz(x, z, tri)
                if distance_sq <= 1e-6 and low <= top <= high:
                    return True
        return False

    def _model_mesh_max_penetration(self, model_meshes, offsets, radius):
        """Maximum sphere/triangle penetration at the current capsule position."""
        deepest = 0.0
        radius_sq = radius * radius
        half = self._half
        player_min = (self.pos.x - half.x, self.pos.y - half.y,
                      self.pos.z - half.z)
        player_max = (self.pos.x + half.x, self.pos.y + half.y,
                      self.pos.z + half.z)
        for _brush, triangles, bounds in model_meshes:
            if bounds:
                mn, mx = bounds
                if (player_max[0] < mn[0] - radius or player_min[0] > mx[0] + radius
                        or player_max[1] < mn[1] - radius or player_min[1] > mx[1] + radius
                        or player_max[2] < mn[2] - radius or player_min[2] > mx[2] + radius):
                    continue
            for ox, oy, oz in offsets:
                sphere = (float(self.pos.x + ox), float(self.pos.y + oy),
                          float(self.pos.z + oz))
                for tri, _face_normal in triangles:
                    v0, v1, v2 = tri
                    tri_min = (min(v0[0], v1[0], v2[0]),
                               min(v0[1], v1[1], v2[1]),
                               min(v0[2], v1[2], v2[2]))
                    tri_max = (max(v0[0], v1[0], v2[0]),
                               max(v0[1], v1[1], v2[1]),
                               max(v0[2], v1[2], v2[2]))
                    if (sphere[0] + radius < tri_min[0] or sphere[0] - radius > tri_max[0]
                            or sphere[1] + radius < tri_min[1] or sphere[1] - radius > tri_max[1]
                            or sphere[2] + radius < tri_min[2] or sphere[2] - radius > tri_max[2]):
                        continue
                    closest = _closest_point_on_triangle(sphere, v0, v1, v2)
                    dx = sphere[0] - closest[0]
                    dy = sphere[1] - closest[1]
                    dz = sphere[2] - closest[2]
                    distance_sq = dx * dx + dy * dy + dz * dz
                    if distance_sq < radius_sq:
                        deepest = max(deepest, radius - math.sqrt(distance_sq))
        return deepest

    def _find_aabb_support(self, aabb_brushes):
        """Find a box whose top supports the capsule at its current position."""
        half = self._half
        feet_y = float(self.pos.y - half.y)
        px, pz = float(self.pos.x), float(self.pos.z)
        for brush in aabb_brushes or ():
            bounds = brush_aabb_bounds(brush)
            if abs(feet_y - float(bounds[4])) > 0.25:
                continue
            if (px + half.x > bounds[0] and px - half.x < bounds[3]
                    and pz + half.z > bounds[2] and pz - half.z < bounds[5]):
                return brush
        return None

    def _find_convex_support(self, shapes, offsets, radius):
        """Return (supporting convex brush, world-space support normal)."""
        if not shapes or not offsets:
            return None
        bottom_index = min(range(len(offsets)), key=lambda i: offsets[i][1])
        for shape in shapes:
            brush, planes, _bounds, frame, turned = shape
            if not planes:
                continue
            if frame is None:
                px, py, pz = float(self.pos.x), float(self.pos.y), float(self.pos.z)
                local_offsets = offsets
            elif frame.r is None:
                px = float(self.pos.x + frame.ox)
                py = float(self.pos.y + frame.oy)
                pz = float(self.pos.z + frame.oz)
                local_offsets = offsets
            else:
                px, py, pz = frame.to_shape(
                    float(self.pos.x), float(self.pos.y), float(self.pos.z)
                )
                if turned is None:
                    turned = [frame.dir_to_shape(*offset) for offset in offsets]
                    shape[4] = turned
                local_offsets = turned

            ox, oy, oz = local_offsets[bottom_index]
            cx, cy, cz = px + ox, py + oy, pz + oz
            best_sd = -1e30
            support_normal = None
            support_normal_y = -1.0
            for nx, ny, nz, distance in planes:
                signed_distance = nx * cx + ny * cy + nz * cz - distance
                if signed_distance > best_sd:
                    best_sd = signed_distance
                    if frame is not None and frame.r is not None:
                        world_normal = frame.to_world_dir(nx, ny, nz)
                        support_normal = tuple(float(world_normal[i]) for i in range(3))
                    else:
                        support_normal = (float(nx), float(ny), float(nz))
                    support_normal_y = support_normal[1]
            if (abs(best_sd - radius) <= 0.25
                    and best_sd <= radius + 0.25
                    and support_normal_y >= PM_GROUND_NORMAL):
                return brush, support_normal
        return None

    def _convex_max_penetration(self, shapes, offsets, radius):
        """Maximum capsule-sphere penetration into the supplied convex brushes."""
        deepest = 0.0
        half = self._half
        for shape in shapes:
            brush, planes, bounds, frame, turned = shape
            if not planes:
                continue
            if frame is None:
                px, py, pz = float(self.pos.x), float(self.pos.y), float(self.pos.z)
                if bounds:
                    pmn = self.pos - half
                    pmx = self.pos + half
                    mn, mx = bounds
                    if (pmx.x < mn[0] - radius or pmn.x > mx[0] + radius
                            or pmx.y < mn[1] - radius or pmn.y > mx[1] + radius
                            or pmx.z < mn[2] - radius or pmn.z > mx[2] + radius):
                        continue
            elif frame.r is None:
                px = float(self.pos.x + frame.ox)
                py = float(self.pos.y + frame.oy)
                pz = float(self.pos.z + frame.oz)
            else:
                px, py, pz = frame.to_shape(
                    float(self.pos.x), float(self.pos.y), float(self.pos.z)
                )
            if turned is None:
                if frame is None or frame.r is None:
                    turned = offsets
                else:
                    turned = [frame.dir_to_shape(*offset) for offset in offsets]
                    shape[4] = turned
            for ox, oy, oz in turned:
                cx, cy, cz = px + ox, py + oy, pz + oz
                best_sd = -1e30
                separated = False
                for nx, ny, nz, distance in planes:
                    signed_distance = nx * cx + ny * cy + nz * cz - distance
                    if signed_distance > radius:
                        separated = True
                        break
                    if signed_distance > best_sd:
                        best_sd = signed_distance
                if not separated and best_sd > -1e29:
                    deepest = max(deepest, radius - best_sd)
        return deepest

    def _apply_final_ground_state(self, model_meshes, shapes, aabb_brushes,
                                  offsets, radius, terrain=None):
        """Publish grounded state only while velocity is not leaving support.

        The separating test is measured along the actual support normal, not
        world Y: motion tangent to a slope can have positive or negative Y
        velocity without leaving the surface.
        """
        self.on_ground = False
        self.ground_object = None
        support = None
        support_normal = None

        def hull_is_clear():
            half = self._half
            lo = glm.vec3(self.pos.x - half.x, self.pos.y - half.y, self.pos.z - half.z)
            hi = glm.vec3(self.pos.x + half.x, self.pos.y + half.y, self.pos.z + half.z)
            return (self._model_mesh_max_penetration(model_meshes, offsets, radius) <= 1e-4
                    and self._convex_max_penetration(shapes, offsets, radius) <= 1e-4
                    and all(self._has_headroom(b, lo, hi) for b in aabb_brushes or ()))

        model_support = self._find_model_mesh_support(
            model_meshes, offsets, radius, clearance=hull_is_clear)
        if model_support is not None:
            support, support_normal = model_support

        if support is None:
            support = self._find_aabb_support(aabb_brushes)
            if support is not None:
                support_normal = (0.0, 1.0, 0.0)

        if support is None:
            convex_support = self._find_convex_support(shapes, offsets, radius)
            if convex_support is not None:
                support, support_normal = convex_support

        if support is None and terrain is not None and terrain.is_solid():
            terrain_height = terrain.get_height_at_safe(self.pos.x, self.pos.z)
            if (terrain_height is not None
                    and abs(float(self.pos.y - self._half.y) - terrain_height) <= 0.25):
                support = terrain
                # Approximate the local heightfield normal using the same safe
                # scalar surface query used for the contact height.
                step = 1.0
                left = terrain.get_height_at_safe(self.pos.x - step, self.pos.z)
                right = terrain.get_height_at_safe(self.pos.x + step, self.pos.z)
                back = terrain.get_height_at_safe(self.pos.x, self.pos.z - step)
                front = terrain.get_height_at_safe(self.pos.x, self.pos.z + step)
                if all(value is not None for value in (left, right, back, front)):
                    nx = (float(left) - float(right)) / (2.0 * step)
                    nz = (float(back) - float(front)) / (2.0 * step)
                    length = math.sqrt(nx * nx + 1.0 + nz * nz)
                    support_normal = (nx / length, 1.0 / length, nz / length)
                else:
                    support_normal = (0.0, 1.0, 0.0)

        if support is None:
            return

        normal_speed = (
            float(self.velocity.x) * support_normal[0]
            + float(self.velocity.y) * support_normal[1]
            + float(self.velocity.z) * support_normal[2]
        )
        # A positive component along the outward normal means a real jump or
        # other separation. Tangential uphill/downhill motion remains grounded.
        if normal_speed > 1e-3:
            return
        # Remove only motion into the support. Zeroing world-Y would destroy
        # the vertical component of otherwise tangential slope movement.
        if normal_speed < 0.0:
            self.velocity.x -= support_normal[0] * normal_speed
            self.velocity.y -= support_normal[1] * normal_speed
            self.velocity.z -= support_normal[2] * normal_speed
        self.on_ground = True
        self.ground_object = support

    def _resolve_model_triangle_meshes(self, model_meshes, offsets, radius,
                                      cover=None, start=None):
        """Resolve the capsule against non-convex model surfaces.

        Convex brushes use their separating planes above. A model Prop's Mesh
        collision is deliberately different: its triangles describe the
        surfaces that exist, not a closed convex hull. Test the three vertical
        capsule spheres against the nearest point on each nearby triangle, so
        decks, individual stair treads and railings collide while openings in
        the grating remain openings.
        """
        if not model_meshes:
            return False
        if start is None:
            start = (self.pos.x, self.pos.y, self.pos.z)

        radius_sq = radius * radius
        ceiling_hit = False
        wall_normal = None
        half = self._half

        # A few relaxation passes handle a capsule meeting more than one
        # triangle at a deck seam or where a tread meets its riser.
        for _ in range(4):
            if cover is not None and not cover.holds(
                    self.pos.x, self.pos.y, self.pos.z):
                self.pos = glm.vec3(*start)
                return True

            deepest = 1e-4
            push = None
            push_normal = None
            push_brush = None
            player_min = (self.pos.x - half.x, self.pos.y - half.y,
                          self.pos.z - half.z)
            player_max = (self.pos.x + half.x, self.pos.y + half.y,
                          self.pos.z + half.z)

            for brush, triangles, bounds in model_meshes:
                # One broad-phase rejection for the whole model.
                if bounds:
                    mn, mx = bounds
                    if (player_max[0] < mn[0] or player_min[0] > mx[0]
                            or player_max[1] < mn[1] or player_min[1] > mx[1]
                            or player_max[2] < mn[2] or player_min[2] > mx[2]):
                        continue

                for tri, face_normal in triangles:
                    v0, v1, v2 = tri
                    # Cheap per-triangle bounds rejection before the closest
                    # point calculation. The sphere's radius is the capsule's
                    # horizontal half-width; offsets supply its vertical span.
                    tri_min_x = min(v0[0], v1[0], v2[0])
                    tri_max_x = max(v0[0], v1[0], v2[0])
                    tri_min_y = min(v0[1], v1[1], v2[1])
                    tri_max_y = max(v0[1], v1[1], v2[1])
                    tri_min_z = min(v0[2], v1[2], v2[2])
                    tri_max_z = max(v0[2], v1[2], v2[2])

                    nx, ny, nz = face_normal
                    n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
                    if n_len <= 1e-8:
                        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                        nx = e1[1] * e2[2] - e1[2] * e2[1]
                        ny = e1[2] * e2[0] - e1[0] * e2[2]
                        nz = e1[0] * e2[1] - e1[1] * e2[0]
                        n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
                        if n_len <= 1e-8:
                            continue
                    nx, ny, nz = nx / n_len, ny / n_len, nz / n_len

                    for ox, oy, oz in offsets:
                        sx, sy, sz = (self.pos.x + ox, self.pos.y + oy,
                                      self.pos.z + oz)
                        if (sx + radius < tri_min_x or sx - radius > tri_max_x
                                or sy + radius < tri_min_y or sy - radius > tri_max_y
                                or sz + radius < tri_min_z or sz - radius > tri_max_z):
                            continue

                        closest = _closest_point_on_triangle(
                            (sx, sy, sz), v0, v1, v2
                        )
                        dx, dy, dz = (sx - closest[0], sy - closest[1],
                                      sz - closest[2])
                        distance_sq = dx * dx + dy * dy + dz * dz
                        if distance_sq > radius_sq + 1e-6:
                            continue
                        distance = math.sqrt(max(0.0, distance_sq))

                        contact_normal = _model_triangle_contact_normal(
                            (dx, dy, dz), distance, (nx, ny, nz), closest,
                            (float(self.pos.x), float(self.pos.y), float(self.pos.z)),
                            (float(self.velocity.x), float(self.velocity.y),
                             float(self.velocity.z)),
                        )

                        if contact_normal[1] <= -0.3 and self.velocity.y > 0.0:
                            ceiling_hit = True
                        elif abs(contact_normal[1]) < PM_GROUND_NORMAL:
                            wall_normal = contact_normal

                        penetration = radius - distance
                        if penetration > deepest:
                            deepest = penetration
                            push = (contact_normal[0] * penetration,
                                    contact_normal[1] * penetration,
                                    contact_normal[2] * penetration)
                            push_normal = contact_normal
                            push_brush = brush

            if push is None:
                break
            self.pos.x += push[0]
            self.pos.y += push[1]
            self.pos.z += push[2]
            if push_normal[1] <= -0.3:
                ceiling_hit = True
            elif abs(push_normal[1]) < PM_GROUND_NORMAL:
                wall_normal = push_normal

        if ceiling_hit and self.velocity.y > 0.0:
            self.velocity.y = 0.0
        if wall_normal is not None:
            vn = (self.velocity.x * wall_normal[0]
                  + self.velocity.z * wall_normal[2])
            if vn < 0.0:
                self.velocity.x -= wall_normal[0] * vn
                self.velocity.z -= wall_normal[2] * vn
        return False

    def _has_headroom(self, brush, player_min, player_max):
        # A model proxy is a non-convex surface mesh. Its overall bounds can
        # include distant railings or legs that do not occupy the standing
        # hull. Test the same three-sphere capsule used by player collision
        # against the actual triangles instead of treating the bounds as solid.
        if brush.get('_collision_mode') == 'mesh' and 'geometry' not in brush:
            triangles = brush.get('_mesh_triangles') or ()
            if not triangles:
                return True

            bounds = brush.get('_mesh_bounds')
            if bounds:
                min_b, max_b = bounds
                if (player_max.x < min_b[0] or player_min.x > max_b[0]
                        or player_max.y < min_b[1] or player_min.y > max_b[1]
                        or player_max.z < min_b[2] or player_min.z > max_b[2]):
                    return True

            half_x = (float(player_max.x) - float(player_min.x)) * 0.5
            half_y = (float(player_max.y) - float(player_min.y)) * 0.5
            half_z = (float(player_max.z) - float(player_min.z)) * 0.5
            radius = min(half_x, half_z)
            if radius <= 0.0:
                return True
            centre = (
                (float(player_min.x) + float(player_max.x)) * 0.5,
                (float(player_min.y) + float(player_max.y)) * 0.5,
                (float(player_min.z) + float(player_max.z)) * 0.5,
            )
            span = max(half_y - radius, 0.0)
            spheres = tuple(
                (centre[0], centre[1] + offset, centre[2])
                for offset in (-span, 0.0, span)
            )
            # Permit exact contact (the deck beneath the player's feet), but
            # reject a real intersection with a wall, railing or ceiling.
            contact_radius = max(0.0, radius - 1e-3)
            radius_sq = contact_radius * contact_radius

            for triangle, _normal in triangles:
                v0, v1, v2 = triangle
                tri_min = (
                    min(v0[0], v1[0], v2[0]),
                    min(v0[1], v1[1], v2[1]),
                    min(v0[2], v1[2], v2[2]),
                )
                tri_max = (
                    max(v0[0], v1[0], v2[0]),
                    max(v0[1], v1[1], v2[1]),
                    max(v0[2], v1[2], v2[2]),
                )
                for sphere in spheres:
                    if (sphere[0] + contact_radius < tri_min[0]
                            or sphere[0] - contact_radius > tri_max[0]
                            or sphere[1] + contact_radius < tri_min[1]
                            or sphere[1] - contact_radius > tri_max[1]
                            or sphere[2] + contact_radius < tri_min[2]
                            or sphere[2] - contact_radius > tri_max[2]):
                        continue
                    closest = _closest_point_on_triangle(sphere, v0, v1, v2)
                    dx = sphere[0] - closest[0]
                    dy = sphere[1] - closest[1]
                    dz = sphere[2] - closest[2]
                    if dx * dx + dy * dy + dz * dz < radius_sq:
                        return False
            return True

        # An angled brush, static or moving: its derived bounds
        # (brush_placement), where it is now. (Callers pass the brushes the
        # per-tick gather left on the AABB path, so this is for a caller
        # that hands one in directly; a spinning box's turned bounds come
        # from the AABB cache below, without asking for its shape again.)
        if 'geometry' in brush and collision_shape(brush) is not None:
            b = world_bounds(brush)
            return not (player_max.x > b[0] and player_min.x < b[3] and
                        player_max.y > b[1] and player_min.y < b[4] and
                        player_max.z > b[2] and player_min.z < b[5])

        # Standard AABB check for solid world brushes.
        # PERF: cached float32 bounds (bit-identical to glm.vec3(pos) +/- size*0.5)
        # instead of constructing four throwaway glm.vec3 per brush per axis pass.
        b = brush_aabb_bounds(brush)

        if (player_max.x > b[0] and player_min.x < b[3] and
            player_max.y > b[1] and player_min.y < b[4] and
            player_max.z > b[2] and player_min.z < b[5]):
            return False

        return True

    def _resolve_collision(self, brushes, axis, delta, ignore_brush=None, cover=None):
        """Push the player out of any overlapping brush on one axis.

        Takes brushes the caller has already filtered with ``_blocks_player``.

        With *cover* (the tick's mover broad phase, :class:`_Reach`), a push
        that leaves the player where the broad phase no longer covers the
        tests that follow stops the pass and returns True: the caller puts
        the player back and runs it again on every mover.
        """
        half = self._half

        for brush in brushes:
            if ignore_brush and brush is ignore_brush:
                continue

            # === MESH COLLISION ===
            if brush.get('_collision_mode') == 'mesh':
                mesh_tris = brush.get('_mesh_triangles', [])
                if not mesh_tris:
                    continue
                
                # Broad-phase: check AABB first
                bounds = brush.get('_mesh_bounds')
                if bounds:
                    min_b, max_b = bounds
                    player_min = self.pos - half
                    player_max = self.pos + half
                    if (player_max.x < min_b[0] or player_min.x > max_b[0] or
                        player_max.y < min_b[1] or player_min.y > max_b[1] or
                        player_max.z < min_b[2] or player_min.z > max_b[2]):
                        continue

                # Build velocity for this frame on the current axis
                if axis == 'x':
                    vel = glm.vec3(self.velocity.x * delta, 0, 0)
                elif axis == 'z':
                    vel = glm.vec3(0, 0, self.velocity.z * delta)
                else:
                    vel = glm.vec3(0, self.velocity.y * delta, 0)
                
                # Use a small radius for the player capsule (slightly smaller than half-extents)
                radius = min(half.x, half.z) * 0.9
                
                nearest_hit = 1.0
                nearest_normal = None
                
                for tri, normal in mesh_tris:
                    # Only test triangles facing the movement direction (optimization)
                    if axis == 'y' and normal[1] <= 0.1:
                        # For ground collision, only care about upward-facing triangles
                        continue
                    
                    hit, t, hit_point, hit_normal = _intersect_swept_sphere_triangle(
                        self.pos, vel, radius, tri, normal
                    )
                    if hit and t < nearest_hit:
                        nearest_hit = t
                        nearest_normal = hit_normal
                
                if nearest_normal is not None:
                    # Push player out along the collision normal
                    penetration = radius * 0.5  # push to clear the surface
                    
                    if axis == 'x':
                        self.pos.x += nearest_normal.x * penetration
                        self.velocity.x = 0
                    elif axis == 'z':
                        self.pos.z += nearest_normal.z * penetration
                        self.velocity.z = 0
                    elif axis == 'y':
                        # For Y, we need to know if we hit from above (floor) or below (ceiling)
                        if nearest_normal.y > 0.3:  # Floor or slope
                            self.pos.y += nearest_normal.y * penetration
                            self.velocity.y = 0
                            self.on_ground = True
                            self.ground_object = brush
                        else:  # Ceiling or steep wall
                            self.pos.y += nearest_normal.y * penetration
                            if self.velocity.y > 0:
                                self.velocity.y = 0
                    if cover is not None and not cover.holds(self.pos.x, self.pos.y, self.pos.z):
                        return True

                continue  # Done with this mesh brush

            # === AABB COLLISION ===
            player_min = self.pos - half
            player_max = self.pos + half

            # PERF: cached float32 bounds (bit-identical to glm.vec3(pos) +/-
            # size*0.5) instead of four throwaway glm.vec3 per brush per axis
            # pass — the same cache _has_headroom already reads.
            b = brush_aabb_bounds(brush)

            if axis == 'x':
                if not (player_min.x < b[3] and player_max.x > b[0]
                        and player_min.y < b[4] and player_max.y > b[1]
                        and player_min.z < b[5] and player_max.z > b[2]):
                    continue
            elif axis == 'z':
                if not (player_min.x < b[3] and player_max.x > b[0]
                        and player_min.y < b[4] and player_max.y > b[1]
                        and player_min.z < b[5] and player_max.z > b[2]):
                    continue
            elif (player_max.x <= b[0] or player_min.x >= b[3]
                  or player_max.z <= b[2] or player_min.z >= b[5]
                  or player_max.y < b[1] or player_min.y > b[4]):
                continue

            # Resolve on the relevant axis
            if axis == 'x':
                dx1 = player_max.x - b[0]
                dx2 = b[3] - player_min.x
                if dx1 < dx2:
                    self.pos.x -= dx1 + 0.001
                else:
                    self.pos.x += dx2 + 0.001
                self.velocity.x = 0

            elif axis == 'z':
                dz1 = player_max.z - b[2]
                dz2 = b[5] - player_min.z
                if dz1 < dz2:
                    self.pos.z -= dz1 + 0.001
                else:
                    self.pos.z += dz2 + 0.001
                self.velocity.z = 0

            elif axis == 'y':
                dy1 = player_max.y - b[1]
                dy2 = b[4] - player_min.y
                if dy1 < dy2:
                    self.pos.y -= dy1 + 0.001
                    if self.velocity.y > 0:
                        self.velocity.y = 0
                else:
                    self.pos.y        += dy2
                    self.velocity.y    = 0
                    self.on_ground     = True
                    self.ground_object = brush

            if cover is not None and not cover.holds(self.pos.x, self.pos.y, self.pos.z):
                return True
        return False
