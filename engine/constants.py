# engine/constants.py
import math

import glm

# --- Settings ---
WIDTH, HEIGHT = 1280, 720
FOV = math.pi / 3
MAX_DEPTH = 6000
FLOOR_HEIGHT = -25

# --- Cube World Settings ---
TILE_SIZE = 50.0
WALL_HEIGHT = 100.0
# Standing collision hull, shared by the runtime and the editor spawn marker.
PLAYER_HULL_SIZE = (TILE_SIZE, TILE_SIZE * 2, TILE_SIZE)

# --- Tilemap Constants ---
WALL_TILE = 0
FLOOR_TILE = 1
PORTAL_A_TILE = 3
PORTAL_B_TILE = 4

# --- Render Modes ---
RENDER_MODE_LIT = 0        # Phong Shading
RENDER_MODE_UNLIT = 1      # Fullbright / Textured
RENDER_MODE_WIREFRAME = 2  # Wireframe lines
RENDER_MODE_VERTEX = 3     # Points

# --- Physics Constants ---
#
# Gravity is not here: engine.physics.PhysicsWorld is the one authority for
# it (Quake 2's sv_gravity, 800), and the player, monsters and dropped props
# all read it from the live world.
#
# Player movement follows Quake 2's rules (qcommon/pmove.c, client/cl_input.c)
# in the same Quake units Fio's maps are built in, applied by Fio's own player
# controller and collision (engine.player).

JUMP_STRENGTH = 270.0           # Quake 2's jump speed: ~46 units high
TERMINAL_VELOCITY = -800.0

# A key press asks for this much speed; +speed (Fio's sprint key) doubles the
# request and the wish is then clamped to PM_MAXSPEED (or PM_DUCKSPEED).
CL_FORWARDSPEED = 200.0
CL_SIDESPEED = 200.0
CL_RUN_SCALE = 2.0

PM_STOPSPEED = 100.0
PM_MAXSPEED = 300.0
PM_DUCKSPEED = 100.0
PM_ACCELERATE = 10.0
PM_AIRACCELERATE = 1.0
PM_FRICTION = 6.0
PM_GROUND_NORMAL = 0.7          # a surface steeper than ~45 degrees is not ground

# Landing faster than 200 u/s blocks jumping for 144 ms, faster than 400 u/s
# for 200 ms (Quake 2's pm_time of 18 / 25 eight-millisecond units).
PM_LAND_SPEED = -200.0
PM_HARD_LAND_SPEED = -400.0
PM_LAND_TIME = 18 * 0.008
PM_HARD_LAND_TIME = 25 * 0.008

# Ducking halves the hull (Quake 2: 56 -> 28 tall) and lowers the eye from 46
# to 22 units above the feet, in proportion to Fio's hull.
PM_DUCK_HEIGHT_FRACTION = 28.0 / 56.0
PM_DUCK_EYE_FRACTION = 22.0 / 46.0

# Fio's sprint key is Quake 2's +speed: 200 -> 300 straight ahead.
PM_SPRINT_SCALE = PM_MAXSPEED / CL_FORWARDSPEED

# --- Water Physics Constants ---
WATER_SWIM_SPEED_MULT = 0.55    # Horizontal swim speed as a fraction of run speed
WATER_VERTICAL_SPEED_MULT = 0.85  # Swim up/down speed as a fraction of swim speed
WATER_DRAG = 5.5                # How quickly velocity converges on the swim target (1/s)
WATER_WADE_SPEED_MULT = 0.75    # Speed while wading (knee/waist deep, not swimming)
WATER_MAX_SINK_SPEED = -240.0   # Water resistance caps fall speed while immersed
WATERJUMP_MAX_CLIMB = 120.0     # Highest ledge (above the feet) a waterjump can clear
WATERJUMP_EDGE_ABOVE_SURFACE = 48.0  # Ledge top may be at most this far above the waterline
WATERJUMP_MAX_BOOST = 360.0     # Cap on the vertical launch speed of a waterjump


def is_water_brush(brush):
    """Single source of truth for "is this brush water?".

    Must stay in sync with what the renderer draws as water: the explicit
    is_water flag, the Water shader, or any face textured with a water
    texture. Physics, AI and rendering all use this so a brush can never
    look like water but collide like a solid.
    """
    if brush.get('is_water'):
        return True
    if brush.get('shader') == 'Water':
        return True
    # Every face's texture by its one material record (engine.brush_faces):
    # an angled brush's planes, a box's per-tag dict.
    geometry = brush.get('geometry')
    if isinstance(geometry, dict) and geometry.get('planes'):
        for plane in geometry['planes']:
            tex = plane.get('texture')
            if tex and 'water' in tex.lower():
                return True
        return False
    textures = brush.get('textures')
    if textures:
        for tex in textures.values():
            if tex and 'water' in tex.lower():
                return True
    return False


def water_high_quality(brush):
    """A water brush's "High quality" flag (``water_high_quality``, default on).

    High-quality water copies the scene depth for depth-based colour,
    shoreline foam, caustics and screen-space reflections; off, it is the
    cheap look (waves, refraction, sky reflection). Authored per brush, so
    hand-edited maps may hold strings like ``"false"`` or ``"0"``.
    """
    value = brush.get('water_high_quality', True)
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return bool(value)


def is_solid_world_brush(brush):
    """Single source of truth for "is this brush part of the solid world?".

    The rule every collision consumer shares: a brush is solid unless it is
    hidden, fog, water, or a trigger volume that is not also a mover or a door.
    It was written out identically in five places -- the monster AI's three
    no-grid fallbacks, the monster wall query in the logic thread, and the
    player's own predicate -- which is how paths that are *supposed* to agree
    about what a wall is end up disagreeing.

    Two nearby predicates deliberately do not use this, and should not be
    folded into it:

    * ``SpatialGrid.populate`` asks ``authored_hidden`` rather than ``hidden``,
      because it builds a durable index that has to outlive a streaming layer
      parking a cell (see ``engine.spatial``), and it files water separately
      rather than discarding it.
    * ``Player._blocks_player`` adds ``disabled`` and ``_physics_body`` on top
      of this, because it classifies movers and doors -- which never went
      through the grid -- and because a dynamic body is simulated rather than
      collided with as a wall.
    """
    if brush.get('hidden') or brush.get('is_fog'):
        return False
    if brush.get('is_trigger') and not (brush.get('is_mover') or brush.get('is_door')):
        return False
    return not is_water_brush(brush)


# Runtime-only keys written to brush dicts by the cached-AABB helpers below
# (brush_aabb_bounds -- and for a mover or door the runtime transform its
# box was placed by -- and brush_still_box for a still angled brush), and by
# the mover table, which publishes a moved row's derived state once into the
# same slots: ``_world_bounds`` (world_bounds' float64 box) and
# ``_aabb_turn`` (the rotation its turned box and collision frame use).
# Stripped on serialisation alongside the renderer's own private keys.
AABB_RUNTIME_KEYS = ('_aabb_sig', '_aabb_bounds', '_aabb_box', '_aabb_runtime',
                     '_world_bounds', '_aabb_turn')


#: engine.brush_placement, bound on first use: it imports the geometry
#: module (NumPy), which this module must not pull in at import time.
_brush_placement = None


def _bind_brush_placement():
    global _brush_placement
    from engine import brush_placement
    _brush_placement = brush_placement
    return brush_placement


class _ShapeKey(tuple):
    """An angled brush's or mesh proxy's AABB key: its pos/size, equal to the
    plain key, tagged so a miss knows it was not cached as a plain box."""
    __slots__ = ()


class _MoverKey(tuple):
    """A mover's or door's AABB key: its pos/size, tagged likewise."""
    __slots__ = ()


def brush_aabb_bounds(brush):
    """Return a brush's world-space AABB as ``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``.

    The brush's derived bounds (:func:`engine.brush_placement.world_box`): a
    box's ``pos +/- size*0.5``, a convex brush's planes' bounds, displaced by
    a mover and widened to the AABB of the turned box while it spins.

    PERF: the AABB is invariant for static brushes and only changes when a
    mover/door moves or spins it. Every physics/AI hot path used to rebuild
    two ``glm.vec3`` objects (plus a subtract and an add) per brush per query
    -- thousands of throwaway GLM allocations per second on a busy level.
    Here the result is computed once *through GLM* (so its float32 rounding is
    bit-for-bit identical to the old ``glm.vec3(pos) +/- glm.vec3(size)*0.5``
    path: halving is exact) and cached on the brush dict, keyed by its
    ``pos`` and ``size`` -- Stage 1's key and Stage 1's cost, for every
    brush. What could change a box without changing them drops the key:
    every geometry edit (brush_geometry's invalidation), the mover table
    taking a brush into its rows, a collision mesh being built, and -- for a
    mover or door, whose box follows its runtime transform -- every change a
    runtime source stamps (``brush_placement.note_runtime_change`` drops the
    keys of the movers cached since the last stamp: a source pays for the
    ones a broad phase asked about, not for every row it moved). Cache keys
    start with ``_`` and are stripped by the serialisers, matching the
    renderer's own matrix cache.
    """
    pos = brush['pos']
    size = brush['size']
    sig = (pos[0], pos[1], pos[2], size[0], size[1], size[2])
    cached = brush.get('_aabb_sig')
    if cached == sig:
        # Stage 1's key and Stage 1's cost (see above for what drops it).
        return brush['_aabb_bounds']
    if cached.__class__ is tuple:
        # Cached as a plain box (an angled brush's and a mover's keys are
        # tagged _ShapeKey / _MoverKey), and still one -- what would make it
        # otherwise drops the key -- that has moved: Stage 1's miss, below.
        pass
    elif ('is_mover' in brush and brush['is_mover']) or ('is_door' in brush and brush['is_door']):
        if '_aabb_runtime' in brush:
            # Its mover table published the transform with its last change.
            return mover_box(brush, sig)[0]
        return _brush_aabb_runtime(brush, _brush_placement or _bind_brush_placement(), sig)
    elif 'geometry' in brush or '_mesh_bounds' in brush:
        # An angled brush or mesh proxy: its derived box.
        return _brush_aabb_slow(brush, pos, size, sig)
    # A plain box's miss, inline: Stage 1's computation.
    bp = glm.vec3(pos)
    bh = glm.vec3(size) * 0.5
    lo = bp - bh
    hi = bp + bh
    bounds = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
    brush['_aabb_bounds'] = bounds          # publication order: see below
    brush['_aabb_sig'] = sig
    return bounds


def _brush_aabb_slow(brush, pos, size, plain_sig):
    """:func:`brush_aabb_bounds` past its fast path: a miss, or a mover."""
    placement = _brush_placement or _bind_brush_placement()
    if brush.get('is_mover') or brush.get('is_door'):
        if '_aabb_runtime' in brush:
            return mover_box(brush, plain_sig)[0]
        return _brush_aabb_runtime(brush, placement, plain_sig)
    sig = plain_sig
    if 'geometry' in brush or '_mesh_bounds' in brush:
        sig = _ShapeKey(plain_sig)
        box = placement.world_box_at(brush, None)
        # Kept for brush_still_box under the same key (stored first: a
        # reader that sees the new key finds the box that goes with it).
        brush['_aabb_box'] = box
        cx, cy, cz, hx, hy, hz = box
        bp = glm.vec3(cx, cy, cz)
        bh = glm.vec3(hx, hy, hz)
    else:
        bp = glm.vec3(pos)
        bh = glm.vec3(size) * 0.5
    lo = bp - bh
    hi = bp + bh
    bounds = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
    # PUBLICATION ORDER IS LOAD-BEARING -- do not swap these two stores.
    # The MonsterAI thread reads this cache concurrently with the logic thread
    # (monster_ai._has_line_of_sight -> SpatialGrid.has_line_of_sight), so a
    # reader can observe the dict mid-update. Writing the bounds *first* and the
    # signature *last* gives the invariant:
    #   observed new sig  => the matching new bounds are already stored
    #   observed old sig  => the reader recomputes, which is always safe
    # The reverse order would let a reader latch a new sig against stale bounds
    # and, because the pair is cached, keep returning that stale value until the
    # brush moves again. Each store is a single dict assignment, atomic under
    # the GIL, and CPython does not reorder them -- so no lock is needed and the
    # hot path stays allocation-free.
    brush['_aabb_bounds'] = bounds
    brush['_aabb_sig'] = sig
    return bounds


def mover_box(brush, sig=None):
    """``(aabb, bounds)``: a mover's or door's world box worked out once from
    the transform its mover table published with its last change -- the
    float32 AABB :func:`brush_aabb_bounds` returns and the float64 box
    :func:`engine.brush_placement.world_bounds` returns -- and kept, with the
    rotation a turned row's collision frame uses, in the brush's
    runtime-only slots until the row next changes (the table drops the key
    then). Every reader shares the one derivation: no runtime source is
    asked, nothing is memoised per angle, nothing is registered for a stamp.

    The arithmetic is the resolver's (world_box_at, turned_half,
    brush_aabb_bounds' GLM float32), so the numbers are the same to the
    bit."""
    placement = _brush_placement or _bind_brush_placement()
    if sig is None:
        pos = brush['pos']
        size = brush['size']
        sig = (pos[0], pos[1], pos[2], size[0], size[1], size[2])
    runtime = brush.get('_aabb_runtime', _UNPUBLISHED)
    if runtime is _UNPUBLISHED:
        bounds = _brush_aabb_runtime(brush, placement, sig)
        return bounds, brush.get('_world_bounds')
    turn = None
    half = None
    if runtime is not None:
        if 'geometry' not in brush and '_mesh_bounds' not in brush:
            size = brush['size']
            half = (size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
        elif '_mesh_bounds' not in brush:
            convex = brush.get('_geo_cache')
            if convex is not None and convex.is_valid:
                box = convex.shape_box
                half = (box[3], box[4], box[5])
    if half is not None:
        hx, hy, hz = half
        if runtime[6]:
            turn = placement.turn_of(brush, runtime)      # once per transform
            if turn is not None:
                # turned_half's arithmetic, in its order.
                (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = turn
                hx, hy, hz = (abs(r00) * hx + abs(r01) * hy + abs(r02) * hz,
                              abs(r10) * hx + abs(r11) * hy + abs(r12) * hz,
                              abs(r20) * hx + abs(r21) * hy + abs(r22) * hz)
        cx, cy, cz = runtime[0], runtime[1], runtime[2]
    else:
        cx, cy, cz, hx, hy, hz = placement.world_box_at(brush, runtime)
    bp = glm.vec3(cx, cy, cz)
    bh = glm.vec3(hx, hy, hz)
    lo = bp - bh
    hi = bp + bh
    aabb = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
    bounds = (cx - hx, cy - hy, cz - hz, cx + hx, cy + hy, cz + hz)
    # Values first, key last (see _brush_aabb_slow); then, if the table
    # published another transform meanwhile (another thread asked), the key
    # is dropped again: never a stale box under a live key.
    brush['_world_bounds'] = bounds
    brush['_aabb_bounds'] = aabb
    brush['_aabb_sig'] = _MoverKey(sig)
    if brush.get('_aabb_runtime', _UNPUBLISHED) is not runtime:
        brush.pop('_aabb_sig', None)
    return aabb, bounds


#: What a brush's ``_aabb_runtime`` slot holds when no mover table has
#: published a transform into it.
_UNPUBLISHED = object()


def _brush_aabb_runtime(brush, placement, sig):
    """A mover's or door's AABB: where its runtime transform puts it now,
    cached under the plain pos/size key and registered with the resolver so
    the next stamped runtime change drops the key.

    The stamps are read before the transform, and again after the key is
    stored: a change that landed meanwhile (on another thread) either finds
    the brush registered and drops the key itself, or has moved a stamp,
    and this drops it -- never a stale box under a live key."""
    # Its transform as resolved for the current placement stamps -- shared
    # with every other reader this tick (brush_placement.resolved_runtime)
    # -- read after the stamps it is checked against below.
    stamps = placement.placement_stamps
    asleep = placement.asleep
    if asleep and id(brush) in asleep:
        # Parked (brush_placement.asleep): it moves with no stamp, so where
        # it is now is worked out for each reader and nothing is cached.
        runtime = placement.runtime_of(brush)
        cx, cy, cz, hx, hy, hz = placement.world_box_at(brush, runtime)
        bp = glm.vec3(cx, cy, cz)
        bh = glm.vec3(hx, hy, hz)
        lo = bp - bh
        hi = bp + bh
        return (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
    cache = placement._runtime_cache          # resolved_runtime, inline
    key = id(brush)
    entry = cache.get(key)
    if entry is not None and entry[0] is brush:
        runtime = entry[1]
    else:
        # brush_placement.runtime_of, inline: the first source that knows it.
        runtime = None
        for ref in placement._sources:
            source = ref()
            if source is not None:
                entries = source.runtime_entries
                if entries:
                    found = entries.get(key)
                    if found is not None and found[0] is brush:
                        runtime = found[1]
                        break
        cache[key] = (brush, runtime)
    half = None
    if runtime is not None:
        if 'geometry' not in brush and '_mesh_bounds' not in brush:
            # A box carried (and turned): world_box_at's box rule, inline.
            size = brush['size']
            half = (size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
        elif '_mesh_bounds' not in brush:
            # A convex brush carried: only its shape's half-extents matter
            # (authored_box's, from the cached shape every geometry edit
            # drops), not its authored centre.
            convex = brush.get('_geo_cache')
            if convex is not None and convex.is_valid:
                box = convex.shape_box
                half = (box[3], box[4], box[5])
    if half is not None:
        hx, hy, hz = half
        if runtime[6]:
            # Turned once per transform, shared with the collision frame
            # (brush_placement.turn_of; turned_half's arithmetic, in its
            # order: the same bits).
            turn = placement.turn_of(brush, runtime)
            if turn is not None:
                (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = turn
                hx, hy, hz = (abs(r00) * hx + abs(r01) * hy + abs(r02) * hz,
                              abs(r10) * hx + abs(r11) * hy + abs(r12) * hz,
                              abs(r20) * hx + abs(r21) * hy + abs(r22) * hz)
        cx, cy, cz = runtime[0], runtime[1], runtime[2]
    else:
        cx, cy, cz, hx, hy, hz = placement.world_box_at(brush, runtime)
    bp = glm.vec3(cx, cy, cz)
    bh = glm.vec3(hx, hy, hz)
    lo = bp - bh
    hi = bp + bh
    bounds = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
    # The transform it was placed by, kept with it (brush_aabb_runtime), and
    # the float64 box world_bounds reads under the same key -- consistent
    # with this transform, whatever a mover table published before. (A
    # rotation kept from another transform is told apart by turn_of.)
    brush['_aabb_runtime'] = runtime
    brush['_world_bounds'] = (cx - hx, cy - hy, cz - hz, cx + hx, cy + hy, cz + hz)
    brush['_aabb_bounds'] = bounds          # publication order: see above
    brush['_aabb_sig'] = _MoverKey(sig)
    placement._runtime_cached[key] = brush        # remember_runtime_cached
    if placement.placement_stamps is not stamps:
        brush.pop('_aabb_sig', None)
    return bounds


def brush_aabb_runtime(brush):
    """A mover's or door's runtime transform as its cached AABB was placed
    by -- ``(cx, cy, cz, ax, ay, az, angle)`` or ``None`` -- found with the
    box (:func:`brush_aabb_bounds`), so a caller that needs both asks the
    runtime sources once: a hit is the transform the last miss found, and no
    change has been stamped since."""
    brush_aabb_bounds(brush)
    return brush.get('_aabb_runtime')


def brush_still_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: :func:`engine.brush_placement.world_box`
    of a still angled brush or mesh proxy -- one with no runtime transform
    (not a mover or a door) -- remembered under :func:`brush_aabb_bounds`'
    pos/size key, so the invalidation that covers that cache covers this
    (every geometry edit drops both).

    What a structure built over the whole level (the collision rows) reads
    for each such brush: a derived box is a function of the planes and the
    placement, which a rebuild does not change.
    """
    pos = brush['pos']
    size = brush['size']
    if brush.get('_aabb_sig') == (pos[0], pos[1], pos[2], size[0], size[1], size[2]):
        box = brush.get('_aabb_box')
        if box is not None:
            return box
    brush_aabb_bounds(brush)
    box = brush.get('_aabb_box')
    if box is None:            # not an angled brush or mesh proxy after all
        placement = _brush_placement or _bind_brush_placement()
        box = placement.world_box_at(brush, None)
    return box


def normalize_color(rgb, default=None):
    """Normalise an RGB colour to 0.0-1.0 floats.

    Accepts [0-255] int or [0.0-1.0] float components.  Returns *default* (or
    ``[0.8, 0.8, 0.8]``) if *rgb* is None or malformed.

    Lives here rather than in the renderer because the dense render projection
    resolves brush colours at edit time and must not import a module that pulls
    in OpenGL.
    """
    if default is None:
        default = [0.8, 0.8, 0.8]
    if not rgb or not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
        return list(default)
    return [c / 255.0 if c > 1.0 else c for c in rgb[:3]]
