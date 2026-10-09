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
# World and player physics follow Quake 2: the values below are the ones
# qcommon/pmove.c, client/cl_input.c and the server cvars ship with, in the
# same Quake units Fio's maps are built in (16/32/64/128 grid, 18-unit stairs).
# Only the player's hull is Fio's own (50 x 100, see Player); everything that
# Quake 2 measures against the hull is taken in proportion to it.

SV_GRAVITY = 800.0              # sv_gravity: one gravity for player, monsters, props
GRAVITY = -SV_GRAVITY           # signed, for Fio's +Y-up world
SV_MAXVELOCITY = 2000.0         # sv_maxvelocity: per-axis speed limit

# client/cl_input.c: a key press asks for this much speed; +speed (Fio's
# sprint key) doubles it, and pmove then clamps the wish to PM_MAXSPEED.
CL_FORWARDSPEED = 200.0
CL_SIDESPEED = 200.0
CL_UPSPEED = 200.0
CL_RUN_SCALE = 2.0

# qcommon/pmove.c movement parameters.
PM_STOPSPEED = 100.0
PM_MAXSPEED = 300.0
PM_DUCKSPEED = 100.0
PM_ACCELERATE = 10.0
PM_AIRACCELERATE = 1.0          # pm_airaccelerate 0 -> PM_Accelerate(..., 1)
PM_WATERACCELERATE = 10.0
PM_FRICTION = 6.0
PM_WATERFRICTION = 1.0
PM_WATER_WISH_SCALE = 0.5       # PM_WaterMove halves the clamped wish speed
PM_WATER_DRIFT = 60.0           # idle swimmers sink at this wish speed

PM_STEPSIZE = 18.0
PM_MIN_STEP_NORMAL = 0.7        # can't step up onto steeper slopes
PM_GROUND_NORMAL = 0.7          # steeper than this is a wall, not ground
PM_GROUND_PROBE = 0.25          # the ground trace reaches this far down
PM_AIRBORNE_SPEED = 180.0       # rising faster than this leaves the ground
PM_NUM_BUMPS = 4
PM_MAX_CLIP_PLANES = 5
PM_OVERCLIP = 1.01
PM_STOP_EPSILON = 0.1
PM_DIST_EPSILON = 0.03125       # traces stop this far short of a surface

PM_JUMP_SPEED = 270.0
JUMP_STRENGTH = PM_JUMP_SPEED
PM_SWIM_JUMP_SPEED = 100.0      # jump held with the waist under water
PM_SWIM_JUMP_MAX_FALL = -300.0  # sinking faster than this, jump does nothing

# Landing: pm_time counts 8 ms units. A landing faster than 200 u/s locks
# out jumping for 18 units (144 ms), faster than 400 u/s for 25 (200 ms).
PM_LAND_SPEED = -200.0
PM_HARD_LAND_SPEED = -400.0
PM_LAND_TIME = 18 * 0.008
PM_HARD_LAND_TIME = 25 * 0.008

# Waterjump: with the waist under water, a wall ahead whose top is between
# the waist and the eyes throws the player up and over it for up to 255
# units (2.04 s), or until they start to fall or land.
PM_WATERJUMP_UP = 350.0
PM_WATERJUMP_FORWARD = 50.0
PM_WATERJUMP_TIME = 255 * 0.008

# Ducking halves the hull (Quake 2: 56 -> 28 tall) and lowers the eye from
# 46 to 22 units above the feet; both are applied to Fio's hull in proportion.
PM_DUCK_HEIGHT_FRACTION = 28.0 / 56.0
PM_DUCK_EYE_FRACTION = 22.0 / 46.0

# Hull-relative Quake 2 probes, as fractions of the 46-unit standing eye
# height (origin is 24 above the feet): the waterjump wall must be solid 28
# above the feet and clear 44 above them, 14 units ahead of the hull.
PM_WATERJUMP_SOLID_FRACTION = 28.0 / 46.0
PM_WATERJUMP_CLEAR_FRACTION = 44.0 / 46.0
PM_WATERJUMP_REACH = 14.0

# Fio's sprint key is Quake 2's +speed: straight-ahead speed goes from
# cl_forwardspeed (200) to pm_maxspeed (300).
PM_SPRINT_SCALE = PM_MAXSPEED / CL_FORWARDSPEED


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
