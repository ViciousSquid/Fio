# engine/constants.py
import math

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