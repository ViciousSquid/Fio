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