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


# Runtime-only keys written to brush dicts by the cached-AABB helper below.
# Stripped on serialisation alongside the renderer's own private keys.
AABB_RUNTIME_KEYS = ('_aabb_sig', '_aabb_bounds')


def brush_aabb_bounds(brush):
    """Return a brush's world-space AABB as ``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``.

    PERF: the AABB (``pos +/- size*0.5``) is invariant for static brushes and
    only changes when a mover/door writes a new ``pos``. Every physics/AI hot
    path used to rebuild two ``glm.vec3`` objects (plus a subtract and an add)
    per brush per query -- thousands of throwaway GLM allocations per second on
    a busy level. Here the result is computed once *through GLM* (so its float32
    rounding is bit-for-bit identical to the old ``glm.vec3(pos) +/- ...`` path)
    and cached on the brush dict, keyed by the current pos/size values so a
    moved brush is transparently refreshed. Cache keys start with ``_`` and are
    stripped by the serialisers, matching the renderer's own matrix cache.
    """
    pos = brush['pos']
    size = brush['size']
    sig = (pos[0], pos[1], pos[2], size[0], size[1], size[2])
    if brush.get('_aabb_sig') == sig:
        return brush['_aabb_bounds']
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
