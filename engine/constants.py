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
GRAVITY = -500.0
JUMP_STRENGTH = 180.0
TERMINAL_VELOCITY = -500.0

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