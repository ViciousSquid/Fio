import numpy as np
import OpenGL.GL as gl
import glm
import ctypes
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import math
import time
import random
from OpenGL.GL.shaders import compileProgram, compileShader
from . import shaders
from . import terrain_style
from .terrain_table import GRID_BORDER, STORED_GRID, TerrainTable

# ============================================================================
# COMPATIBILITY EXPORTS
# ============================================================================
TERRAIN_VERTEX_SHADER = shaders.DEFAULT_SHADERS['terrain.vert']
TERRAIN_FRAGMENT_SHADER = shaders.DEFAULT_SHADERS['terrain.frag']


def grass_vertex_count(segments: int) -> int:
    """Vertices per blade instance: (segments - 1) quads plus the tip."""
    return (max(1, int(segments)) - 1) * 6 + 3

# ============================================================================
# OPTIMIZED NOISE FUNCTIONS
# ============================================================================

class PerlinNoise:
    """Perlin noise with both scalar and batch methods."""
    
    def __init__(self, seed: int = 42):
        self.seed = seed
        np.random.seed(seed)
        self.perm = np.arange(256, dtype=np.int32)
        np.random.shuffle(self.perm)
        self.perm = np.tile(self.perm, 2)
        self._grad_x = np.array([1, -1, 1, -1], dtype=np.float32)
        self._grad_y = np.array([1, 1, -1, -1], dtype=np.float32)
    
    def _fade(self, t):
        return t * t * t * (t * (t * 6 - 15) + 10)
    
    def _fade_scalar(self, t: float) -> float:
        return t * t * t * (t * (t * 6 - 15) + 10)
    
    def _grad_scalar(self, hash_val: int, x: float, y: float) -> float:
        h = hash_val & 3
        if h == 0: return x + y
        elif h == 1: return -x + y
        elif h == 2: return x - y
        else: return -x - y
    
    def _grad(self, hash_arr, x, y):
        h = hash_arr & 3
        result = np.zeros_like(x)
        result[h == 0] = (x + y)[h == 0]
        result[h == 1] = (-x + y)[h == 1]
        result[h == 2] = (x - y)[h == 2]
        result[h == 3] = (-x - y)[h == 3]
        return result
    
    def noise2d_scalar(self, x: float, y: float) -> float:
        xi = int(math.floor(x)) & 255
        yi = int(math.floor(y)) & 255
        xf = x - math.floor(x)
        yf = y - math.floor(y)
        u = self._fade_scalar(xf)
        v = self._fade_scalar(yf)
        aa = self.perm[self.perm[xi] + yi]
        ab = self.perm[self.perm[xi] + yi + 1]
        ba = self.perm[self.perm[xi + 1] + yi]
        bb = self.perm[self.perm[xi + 1] + yi + 1]
        g_aa = self._grad_scalar(aa, xf, yf)
        g_ba = self._grad_scalar(ba, xf - 1, yf)
        g_ab = self._grad_scalar(ab, xf, yf - 1)
        g_bb = self._grad_scalar(bb, xf - 1, yf - 1)
        x1 = g_aa + u * (g_ba - g_aa)
        x2 = g_ab + u * (g_bb - g_ab)
        return x1 + v * (x2 - x1)
    
    def fbm_scalar(self, x: float, y: float, octaves: int = 4, persistence: float = 0.5, lacunarity: float = 2.0) -> float:
        total = 0.0
        amplitude = 1.0
        frequency = 1.0
        max_value = 0.0
        for _ in range(octaves):
            total += self.noise2d_scalar(x * frequency, y * frequency) * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_value
    
    def ridge_scalar(self, x: float, y: float, octaves: int = 4, persistence: float = 0.5, lacunarity: float = 2.0) -> float:
        total = 0.0
        amplitude = 1.0
        frequency = 1.0
        max_value = 0.0
        for _ in range(octaves):
            n = 1.0 - abs(self.noise2d_scalar(x * frequency, y * frequency))
            n = n * n
            total += n * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_value
    
    def noise2d_batch(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        xi = np.floor(x).astype(np.int32) & 255
        yi = np.floor(y).astype(np.int32) & 255
        xf = x - np.floor(x)
        yf = y - np.floor(y)
        u = self._fade(xf)
        v = self._fade(yf)
        aa = self.perm[self.perm[xi] + yi]
        ab = self.perm[self.perm[xi] + yi + 1]
        ba = self.perm[self.perm[xi + 1] + yi]
        bb = self.perm[self.perm[xi + 1] + yi + 1]
        g_aa = self._grad(aa, xf, yf)
        g_ba = self._grad(ba, xf - 1, yf)
        g_ab = self._grad(ab, xf, yf - 1)
        g_bb = self._grad(bb, xf - 1, yf - 1)
        x1 = g_aa + u * (g_ba - g_aa)
        x2 = g_ab + u * (g_bb - g_ab)
        return x1 + v * (x2 - x1)
    
    def fbm_batch(self, x: np.ndarray, y: np.ndarray, octaves: int = 4, persistence: float = 0.5, lacunarity: float = 2.0) -> np.ndarray:
        total = np.zeros_like(x)
        amplitude = 1.0
        frequency = 1.0
        max_value = 0.0
        for _ in range(octaves):
            total += self.noise2d_batch(x * frequency, y * frequency) * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_value
    
    def ridge_batch(self, x: np.ndarray, y: np.ndarray, octaves: int = 4, persistence: float = 0.5, lacunarity: float = 2.0) -> np.ndarray:
        total = np.zeros_like(x)
        amplitude = 1.0
        frequency = 1.0
        max_value = 0.0
        for _ in range(octaves):
            n = 1.0 - np.abs(self.noise2d_batch(x * frequency, y * frequency))
            n = n * n
            total += n * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_value

# ============================================================================
# TERRAIN FEATURE GENERATION
# ============================================================================

class TerrainFeatures:
    def __init__(self, noise: PerlinNoise, seed: int = 42):
        self.noise = noise
        self.seed = seed
        self.feature_noise = PerlinNoise(seed + 1000)
        self.valley_noise = PerlinNoise(seed + 2000)
    
    def get_rolling_hills_scalar(self, x: float, z: float, scale: float = 0.005) -> float:
        hills = self.noise.fbm_scalar(x * scale, z * scale, octaves=2, persistence=0.4, lacunarity=2.0)
        big_hills = self.noise.fbm_scalar(x * scale * 0.3, z * scale * 0.3, octaves=1, persistence=0.5, lacunarity=2.0)
        return hills * 0.7 + big_hills * 0.3
    
    def get_mountains_scalar(self, x: float, z: float, scale: float = 0.008, sharpness: float = 0.5) -> float:
        mask_value = self.feature_noise.fbm_scalar(x * scale * 0.5, z * scale * 0.5, octaves=2, persistence=0.5, lacunarity=2.0)
        mask = max(0, (mask_value - 0.1) * 2.0)
        mask = mask ** 1.5
        mask = min(1, mask)
        ridge = self.noise.ridge_scalar(x * scale, z * scale, octaves=3, persistence=0.5, lacunarity=2.2)
        detail = self.noise.fbm_scalar(x * scale * 2, z * scale * 2, octaves=2, persistence=0.4, lacunarity=2.0)
        mountain_height = ridge * 0.8 + detail * 0.2 * (1 - sharpness) + ridge * sharpness * 0.2
        return mountain_height * mask
    
    def get_valleys_scalar(self, x: float, z: float, scale: float = 0.003, depth: float = 0.5) -> float:
        valley = self.valley_noise.fbm_scalar(x * scale, z * scale, octaves=2, persistence=0.4, lacunarity=2.0)
        valley = abs(valley)
        valley = 1.0 - valley
        valley = valley ** 3
        return -valley * depth
    
    def get_plateaus_scalar(self, x: float, z: float, scale: float = 0.004, flatness: float = 0.7) -> float:
        mask_value = self.feature_noise.fbm_scalar(x * scale * 0.4 + 50, z * scale * 0.4 + 50, octaves=2, persistence=0.5, lacunarity=2.0)
        mask = max(0, min(1, (mask_value - 0.2) * 2.5))
        base = self.noise.fbm_scalar(x * scale, z * scale, octaves=1, persistence=0.5, lacunarity=2.0)
        if base > 0.3:
            plateau_height = 0.3 + (base - 0.3) * (1.0 - flatness)
        else:
            plateau_height = base
        return plateau_height * mask
    
    def get_rolling_hills_batch(self, x: np.ndarray, z: np.ndarray, scale: float = 0.005) -> np.ndarray:
        hills = self.noise.fbm_batch(x * scale, z * scale, octaves=2, persistence=0.4, lacunarity=2.0)
        big_hills = self.noise.fbm_batch(x * scale * 0.3, z * scale * 0.3, octaves=1, persistence=0.5, lacunarity=2.0)
        return hills * 0.7 + big_hills * 0.3
    
    def get_mountains_batch(self, x: np.ndarray, z: np.ndarray, scale: float = 0.008, sharpness: float = 0.5) -> np.ndarray:
        mask_value = self.feature_noise.fbm_batch(x * scale * 0.5, z * scale * 0.5, octaves=2, persistence=0.5, lacunarity=2.0)
        mask = np.clip((mask_value - 0.1) * 2.0, 0, None)
        mask = np.power(mask, 1.5)
        mask = np.clip(mask, 0, 1)
        ridge = self.noise.ridge_batch(x * scale, z * scale, octaves=3, persistence=0.5, lacunarity=2.2)
        detail = self.noise.fbm_batch(x * scale * 2, z * scale * 2, octaves=2, persistence=0.4, lacunarity=2.0)
        mountain_height = ridge * 0.8 + detail * 0.2 * (1 - sharpness) + ridge * sharpness * 0.2
        return mountain_height * mask
    
    def get_valleys_batch(self, x: np.ndarray, z: np.ndarray, scale: float = 0.003, depth: float = 0.5) -> np.ndarray:
        valley = self.valley_noise.fbm_batch(x * scale, z * scale, octaves=2, persistence=0.4, lacunarity=2.0)
        valley = np.abs(valley)
        valley = 1.0 - valley
        valley = np.power(valley, 3)
        return -valley * depth
    
    def get_plateaus_batch(self, x: np.ndarray, z: np.ndarray, scale: float = 0.004, flatness: float = 0.7) -> np.ndarray:
        mask_value = self.feature_noise.fbm_batch(x * scale * 0.4 + 50, z * scale * 0.4 + 50, octaves=2, persistence=0.5, lacunarity=2.0)
        mask = np.clip((mask_value - 0.2) * 2.5, 0, 1)
        base = self.noise.fbm_batch(x * scale, z * scale, octaves=1, persistence=0.5, lacunarity=2.0)
        plateau_height = np.where(base > 0.3, 0.3 + (base - 0.3) * (1.0 - flatness), base)
        return plateau_height * mask

# ============================================================================
# BIOME DEFINITIONS
# ============================================================================

@dataclass
class BiomeConfig:
    name: str
    base_height: float = 0.0
    height_scale: float = 200.0
    hills_scale: float = 0.005
    hills_intensity: float = 1.0
    mountains_enabled: bool = False
    mountains_scale: float = 0.002
    mountains_intensity: float = 1.0
    mountains_sharpness: float = 1.0
    valleys_enabled: bool = False
    valleys_scale: float = 0.003
    valleys_depth: float = 0.5
    plateaus_enabled: bool = False
    plateaus_scale: float = 0.004
    plateaus_intensity: float = 0.5
    plateaus_flatness: float = 0.7
    trees_enabled: bool = False
    tree_density: float = 0.0
    color_gradient: List[Tuple[float, Tuple[float, float, float]]] = field(default_factory=lambda: [
        (0.0, (0.2, 0.3, 0.1)),
        (0.4, (0.1, 0.5, 0.2)),
        (0.7, (0.3, 0.3, 0.3)),
        (1.0, (1.0, 1.0, 1.0))
    ])
    blend_weights: Tuple[float, float, float, float] = (1.0, 0.3, 0.0, 0.0)
    terrain_height_scale: float = 0.003

    def to_dict(self):
        return {
            "name": self.name,
            "base_height": self.base_height,
            "height_scale": self.height_scale,
            "hills_scale": self.hills_scale,
            "hills_intensity": self.hills_intensity,
            "mountains_enabled": self.mountains_enabled,
            "mountains_scale": self.mountains_scale,
            "mountains_intensity": self.mountains_intensity,
            "mountains_sharpness": self.mountains_sharpness,
            "valleys_enabled": self.valleys_enabled,
            "valleys_scale": self.valleys_scale,
            "valleys_depth": self.valleys_depth,
            "plateaus_enabled": self.plateaus_enabled,
            "plateaus_scale": self.plateaus_scale,
            "plateaus_intensity": self.plateaus_intensity,
            "plateaus_flatness": self.plateaus_flatness,
            "trees_enabled": self.trees_enabled,
            "tree_density": self.tree_density,
            "color_gradient": self.color_gradient,
            "blend_weights": self.blend_weights,
            "terrain_height_scale": self.terrain_height_scale
        }

    @classmethod
    def from_dict(cls, data):
        instance = cls(name=data.get("name", "Custom"))
        for k, v in data.items():
            if hasattr(instance, k):
                setattr(instance, k, v)
        return instance

BIOMES = {
    'grassy_hills': BiomeConfig(name='Grassy Hills', height_scale=200, hills_scale=0.005, hills_intensity=1.2, blend_weights=(1.5, 0.5, 0.1, 0.0), terrain_height_scale=1.0/300.0, trees_enabled=True, tree_density=0.2),
    'low_poly_valley': BiomeConfig(name='Low Poly Valley', base_height=10.0, height_scale=250.0, hills_scale=0.004, hills_intensity=0.8, mountains_enabled=True, mountains_scale=0.006, mountains_intensity=1.2, mountains_sharpness=0.2, valleys_enabled=True, valleys_depth=0.2, color_gradient=[(0.0, (0.38, 0.60, 0.25)), (0.25, (0.35, 0.55, 0.22)), (0.28, (0.55, 0.40, 0.25)), (1.0, (0.65, 0.45, 0.30))], blend_weights=(1.0, 0.0, 0.0, 0.0), trees_enabled=True, tree_density=0.15),
    'dark_cliffs': BiomeConfig(name='Dark Cliffs', base_height=0.0, height_scale=350.0, hills_scale=0.008, hills_intensity=0.5, mountains_enabled=True, mountains_scale=0.005, mountains_intensity=1.8, mountains_sharpness=0.9, plateaus_enabled=True, plateaus_scale=0.01, plateaus_intensity=0.5, color_gradient=[(0.0, (0.15, 0.15, 0.18)), (0.4, (0.25, 0.25, 0.28)), (0.7, (0.10, 0.10, 0.12)), (0.95, (0.28, 0.35, 0.25))], trees_enabled=False),
    'desert_canyon': BiomeConfig(name='Desert Canyon', base_height=20.0, height_scale=200.0, hills_scale=0.002, hills_intensity=0.3, mountains_enabled=False, plateaus_enabled=True, plateaus_scale=0.005, plateaus_intensity=1.5, plateaus_flatness=0.95, valleys_enabled=True, valleys_scale=0.004, valleys_depth=0.4, color_gradient=[(0.0, (0.60, 0.40, 0.25)), (0.2, (0.60, 0.40, 0.25)), (0.21, (0.50, 0.30, 0.15)), (0.4, (0.50, 0.30, 0.15)), (0.41, (0.70, 0.50, 0.35)), (0.6, (0.70, 0.50, 0.35)), (0.61, (0.45, 0.25, 0.15)), (1.0, (0.45, 0.25, 0.15))], trees_enabled=False),
    'jagged_peaks': BiomeConfig(name='Jagged Peaks', base_height=50.0, height_scale=500.0, hills_scale=0.005, hills_intensity=0.2, mountains_enabled=True, mountains_scale=0.004, mountains_intensity=1.5, mountains_sharpness=1.0, valleys_enabled=True, valleys_scale=0.002, valleys_depth=0.3, color_gradient=[(0.0, (0.25, 0.30, 0.20)), (0.15, (0.35, 0.35, 0.35)), (0.6, (0.50, 0.50, 0.55)), (0.8, (0.90, 0.90, 0.95))], trees_enabled=False),
    'desert': BiomeConfig(name='Desert', height_scale=100, hills_scale=0.008, hills_intensity=0.8, blend_weights=(0.2, 0.3, 1.2, 0.0), terrain_height_scale=1.0/150.0, trees_enabled=False, tree_density=0.0),
    'mountains': BiomeConfig(name='Mountains', height_scale=500, mountains_enabled=True, mountains_intensity=1.5, blend_weights=(0.0, 1.0, 0.0, 0.6), terrain_height_scale=1.0/700.0, trees_enabled=True, tree_density=0.15),
    'gentle_meadow': BiomeConfig(name='Gentle Meadow', base_height=0.0, height_scale=80.0, hills_scale=0.003, hills_intensity=0.8, mountains_enabled=False, valleys_enabled=True, valleys_scale=0.002, valleys_depth=0.15, plateaus_enabled=False, color_gradient=[(0.0, (0.30, 0.50, 0.22)), (0.2, (0.38, 0.58, 0.28)), (0.5, (0.45, 0.62, 0.32)), (0.7, (0.50, 0.65, 0.35)), (1.0, (0.55, 0.68, 0.38))], blend_weights=(1.2, 0.2, 0.1, 0.0), terrain_height_scale=1.0/120.0, trees_enabled=True, tree_density=0.4),
    'rolling_highlands': BiomeConfig(name='Rolling Highlands', base_height=20.0, height_scale=150.0, hills_scale=0.004, hills_intensity=0.7, mountains_enabled=True, mountains_scale=0.006, mountains_intensity=0.5, mountains_sharpness=0.3, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.25, plateaus_enabled=False, color_gradient=[(0.0, (0.28, 0.48, 0.20)), (0.25, (0.38, 0.55, 0.28)), (0.5, (0.45, 0.60, 0.32)), (0.7, (0.50, 0.55, 0.38)), (0.85, (0.55, 0.52, 0.42)), (1.0, (0.65, 0.62, 0.55))], blend_weights=(1.0, 0.4, 0.2, 0.1), terrain_height_scale=1.0/225.0, trees_enabled=True, tree_density=0.3),
    'rocky_mountains': BiomeConfig(name='Rocky Mountains', base_height=50.0, height_scale=300.0, hills_scale=0.003, hills_intensity=0.4, mountains_enabled=True, mountains_scale=0.008, mountains_intensity=1.0, mountains_sharpness=0.6, valleys_enabled=True, valleys_scale=0.004, valleys_depth=0.35, plateaus_enabled=False, color_gradient=[(0.0, (0.30, 0.45, 0.22)), (0.15, (0.38, 0.52, 0.28)), (0.3, (0.42, 0.40, 0.32)), (0.5, (0.50, 0.45, 0.38)), (0.7, (0.58, 0.52, 0.45)), (0.85, (0.68, 0.65, 0.58)), (1.0, (0.88, 0.86, 0.82))], blend_weights=(0.1, 1.2, 0.0, 0.5), terrain_height_scale=1.0/450.0, trees_enabled=True, tree_density=0.15),
    'desert_mesas': BiomeConfig(name='Desert Mesas', base_height=10.0, height_scale=180.0, hills_scale=0.004, hills_intensity=0.5, mountains_enabled=False, plateaus_enabled=True, plateaus_scale=0.005, plateaus_intensity=0.9, plateaus_flatness=0.85, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.2, color_gradient=[(0.0, (0.35, 0.50, 0.25)), (0.15, (0.55, 0.42, 0.28)), (0.3, (0.68, 0.50, 0.32)), (0.5, (0.75, 0.55, 0.35)), (0.7, (0.80, 0.62, 0.40)), (1.0, (0.85, 0.70, 0.48))], blend_weights=(0.0, 0.4, 1.5, 0.0), terrain_height_scale=1.0/270.0, trees_enabled=False, tree_density=0.0),
    'alpine_forest': BiomeConfig(name='Alpine Forest', base_height=30.0, height_scale=220.0, hills_scale=0.004, hills_intensity=0.6, mountains_enabled=True, mountains_scale=0.007, mountains_intensity=0.8, mountains_sharpness=0.45, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.3, plateaus_enabled=False, color_gradient=[(0.0, (0.18, 0.35, 0.15)), (0.2, (0.25, 0.45, 0.20)), (0.4, (0.35, 0.52, 0.28)), (0.55, (0.42, 0.48, 0.32)), (0.7, (0.52, 0.50, 0.45)), (0.85, (0.65, 0.63, 0.58)), (1.0, (0.92, 0.94, 0.96))], blend_weights=(0.8, 0.6, 0.0, 0.4), terrain_height_scale=1.0/330.0, trees_enabled=True, tree_density=0.8),
    'coastal_cliffs': BiomeConfig(name='Coastal Cliffs', base_height=0.0, height_scale=120.0, hills_scale=0.005, hills_intensity=0.5, mountains_enabled=True, mountains_scale=0.01, mountains_intensity=0.6, mountains_sharpness=0.7, valleys_enabled=False, plateaus_enabled=True, plateaus_scale=0.008, plateaus_intensity=0.4, plateaus_flatness=0.6, color_gradient=[(0.0, (0.25, 0.45, 0.20)), (0.2, (0.35, 0.52, 0.28)), (0.4, (0.40, 0.38, 0.30)), (0.6, (0.48, 0.44, 0.38)), (0.8, (0.55, 0.50, 0.42)), (1.0, (0.62, 0.58, 0.48))], blend_weights=(0.3, 0.8, 0.2, 0.0), terrain_height_scale=1.0/180.0, trees_enabled=True, tree_density=0.1),
}

def biome_display_name(name) -> str:
    """A biome name without decoration: printable ASCII, trimmed.

    The built-in names used to carry emoji ("Grassy Hills 🌿"), and maps saved
    then still do; this is the name those maps mean.
    """
    return ''.join(ch for ch in str(name) if ' ' <= ch <= '~').strip()


def biome_key_for_name(name) -> str:
    """The BIOMES key a biome display name refers to ("Low Poly Valley" ->
    "low_poly_valley"), ignoring any decoration an older map saved with it."""
    return biome_display_name(name).lower().replace(' ', '_')


#: What a newly created terrain starts as. A map that saves its terrain keeps
#: whatever it saved; these apply to terrain created fresh in the editor.
DEFAULT_BIOME = 'low_poly_valley'
DEFAULT_USE_TEXTURES = True

# ============================================================================
# MAIN TERRAIN CLASS
# ============================================================================

class Terrain:
    # Terrain inside this radius is a protected high-detail zone. A chunk is
    # never allowed to change LOD while any part of it lies within 4096 world
    # units of the camera -- or within the stream radius, if streaming keeps
    # less than that resident (see _near_detail_radius).
    NEAR_DETAIL_RADIUS = 4096.0
    NEAR_DETAIL_RADIUS_SQ = NEAR_DETAIL_RADIUS ** 2
    LOD_DISTANCES_SQ = [4608**2, 6144**2, 8192**2, 12288**2]
    LOD_RESOLUTIONS = [48, 32, 16, 8]
    LOD_HYSTERESIS_FRAMES = 10
    MAX_UPDATES_PER_FRAME = 2
    #: Milliseconds of chunk meshing a frame may spend before the rest waits
    #: for the next frame. One chunk is always built, so terrain keeps
    #: streaming however slow the machine; a second only fits on a fast one.
    #: Two builds back to back in one frame were the hitch felt when walking
    #: into new terrain.
    UPDATE_BUDGET_MS = 4.0
    #: Largest colour gradient the heightfield shader holds (MAX_GRADIENT_STOPS).
    MAX_GRADIENT_STOPS = 32
    #: Texture layers per height-grid page; clamped to the driver's limit.
    HEIGHT_PAGE_LAYERS = 512
    #: Every uniform the terrain programs are driven through.
    _UNIFORM_NAMES = (
        'projection', 'view', 'active_lights', 'use_textures', 'lod_level',
        'texGrass', 'texRock', 'texSand', 'texSnow',
        'uHeights', 'uChunkI', 'uChunkX', 'uChunkY', 'uTiling', 'uFlatMode',
        'uGradCount', 'uGradH', 'uGradC', 'uGradW', 'uGradD',
    ) + terrain_style.UNIFORM_NAMES
    #: World units per repeat of the terrain textures (they are 1024 px).
    #: At 20 a tile was half a player-height across, so at any ordinary
    #: viewing distance the texture was minified to its average colour and
    #: textured terrain looked untextured.
    TILING_SCALE = 64.0
    # Physical mesh scale is a true uniform terrain scale. It changes the
    # world-space footprint and vertical relief together; procedural sampling
    # remains in terrain-space so enlarging the mesh cannot flatten it.
    DEFAULT_CHUNK_SIZE = 256.0
    
    def __init__(self, texture_manager=None, seed: int = 42):
        self.seed = seed
        self.noise = PerlinNoise(seed)
        self.features = TerrainFeatures(self.noise, seed)
        self.biome: BiomeConfig = BIOMES[DEFAULT_BIOME]
        self.chunk_size: float = self.DEFAULT_CHUNK_SIZE
        self.mesh_scale: float = 1.0
        self.base_resolution: int = 48
        self.offset_x: float = 0.0
        self.offset_z: float = 0.0
        self.offset_y: float = 0.0
        # New terrain starts textured; maps that saved use_textures keep it.
        self.use_textures: bool = DEFAULT_USE_TEXTURES
        self.flat_mode: bool = False
        #: Dense per-chunk state (residency, LOD, the one heightfield both
        #: rendering and collision read). See engine.terrain_table.
        self.table = TerrainTable()
        # GL objects per table slot. The table is GL-free; these arrays are
        # the terrain renderer's half, indexed by the same slot.
        self._grass_vao = np.zeros(0, dtype=np.int64)
        self._grass_vbo = np.zeros(0, dtype=np.int64)
        self._grass_count = np.zeros(0, dtype=np.int64)
        self.min_chunk_x: int = -2
        self.max_chunk_x: int = 2
        self.min_chunk_z: int = -2
        self.max_chunk_z: int = 2
        self.tree_positions: List[Tuple[float, float, float]] = []
        self.total_triangles: int = 0
        self.drawn_slots = np.zeros(0, dtype=np.intp)
        self.visible_chunks: int = 0
        self.culled_chunks: int = 0
        self.shader_program: int = 0
        self.uniforms: Dict[str, int] = {}
        # Height-grid texture pages (GL_TEXTURE_2D_ARRAY, R32F), slot ->
        # (page, layer); the table version each slot was last uploaded at;
        # the empty VAO an attribute-less draw needs in a core profile.
        self._height_pages: List[int] = []
        self._page_layers = 0
        self._gpu_version = np.zeros(0, dtype=np.int64)
        self._empty_vao = 0
        self._gradient_warned = False
        # Grass is a separate GL 3.3 instanced pass. The CPU scatters tufts,
        # places every blade of a tuft on the terrain surface and uploads one
        # compact position/size/phase record per blade; the GPU builds the
        # tapered, curved blade and animates it (see shaders 'grass.vert').
        self.grass_enabled: bool = False
        self.grass_density: float = 0.02
        self.grass_color: Tuple[float, float, float] = tuple(self.biome.color_gradient[0][1])
        self.grass_color_custom: bool = False
        #: Colour of the blade tips; None derives a sun-bleached shade of
        #: grass_color.
        self.grass_tip_color: Optional[Tuple[float, float, float]] = None
        self.grass_shader_program: int = 0
        self.grass_uniforms: Dict[str, int] = {}
        self.grass_time = 0.0
        self.GRASS_MAX_PER_CHUNK = 4096          # tufts per chunk
        self.GRASS_BLADES_PER_TUFT = 5
        self.GRASS_TUFT_RADIUS = 2.5             # world units
        self.GRASS_BLADE_HEIGHT = 5.0            # world units at size 1.0
        self.GRASS_BLADE_WIDTH = 0.35            # half width at the root
        self.GRASS_MAX_DISTANCE = 3072.0
        # Chunks nearer than this draw detailed blades; blades morph onto the
        # single-triangle far blade between LOD_START and LOD_DISTANCE.
        self.GRASS_SEGMENTS = 5
        self.GRASS_LOD_START = 350.0
        self.GRASS_LOD_DISTANCE = 700.0
        # Blades shrink away over the last stretch before the cut-off
        # instead of popping out.
        self.GRASS_FADE_START = 2400.0
        # Grass only grows where the ground is flatter than this (normal.y).
        self.GRASS_MIN_NORMAL_Y = 0.78
        # Look options: texture layers, terracing and the stylised modes.
        self.appearance = terrain_style.TerrainAppearance()
        self._height_range: Optional[Tuple[float, float]] = None
        # 'blocks' terracing draws square columns from per-chunk meshes with
        # a mesh vertex shader instead of the heightfield.
        self.block_program: int = 0
        self.block_uniforms: Dict[str, int] = {}
        self._block_vao = np.zeros(0, dtype=np.int64)
        self._block_vbo = np.zeros(0, dtype=np.int64)
        self._block_count = np.zeros(0, dtype=np.int64)
        self.enabled: bool = True
        self.wireframe: bool = False
        self.solid: bool = True
        # Sculpt deformation map — sparse dict of (grid_x, grid_z) -> height offset
        self.sculpt_offsets: Dict[Tuple[int, int], float] = {}
        self.sculpt_grid_resolution: float = 4.0  # world units per grid cell
        # Heightmap overlay
        self.heightmap_data: Optional[np.ndarray] = None  # 2D float32, 0..1
        self.heightmap_strength: float = 100.0
        self.heightmap_blend: str = 'additive'  # 'additive' or 'replace'
        # -- Chunk streaming (Big World "fill world with terrain") -----------
        # When ``streaming`` is on, only the chunks within ``stream_radius`` of
        # the camera are kept resident; chunks beyond ``stream_radius +
        # stream_evict_padding`` are freed. This lets a world-spanning terrain
        # (bounds widened by ``set_world_extent``) render without tessellating
        # the whole grid up-front — meshes appear around the camera as it moves.
        self.streaming: bool = False
        # Keep the protected high-detail zone resident while the camera moves.
        self.stream_radius: float = 4096.0
        self.stream_evict_padding: float = 512.0
        self.streamed_chunks: int = 0
        # A ``set_bounds(..., prune=False)`` defers its out-of-bounds chunk
        # deletion (a GL op) to the next render on the GL thread.
        self._pending_prune: bool = False
        # Authored chunk bounds / enabled flag stashed while the editor is
        # previewing a world fill, so the expansion is reversible and never
        # saved to the map file.
        self._authored_bounds: Optional[Tuple[int, int, int, int]] = None
        self._authored_enabled: Optional[bool] = None
        self.grass_tex = 0
        self.rock_tex = 0
        self.sand_tex = 0
        self.snow_tex = 0
        self._placeholder_cubemap = 0
        if texture_manager:
            self.load_terrain_textures(texture_manager)
        self._init_shader()
    
    def _compile_program(self, vertex_name):
        """Compile the terrain fragment shader against *vertex_name*.

        Returns the program, or 0 when there is no GL context yet (harmless:
        update_and_render() recompiles on the GL thread at first draw) or the
        compile genuinely failed (reported).
        """
        try:
            vertex_code = shaders.DEFAULT_SHADERS[vertex_name]
            fragment_code = shaders.light_ubo_source(
                shaders.DEFAULT_SHADERS['terrain.frag'])
            vertex_shader = compileShader(vertex_code, gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(fragment_code, gl.GL_FRAGMENT_SHADER)
            program = compileProgram(vertex_shader, fragment_shader, validate=False)
            if not program:
                print("ERROR: Failed to compile terrain shader program!")
                return 0
        except Exception as e:
            msg = str(e)
            # The Terrain can be constructed before the view's GL context is
            # current (e.g. during map load), so glCreateShader isn't bound yet.
            # Stay quiet then; only a genuine compile failure is worth reporting.
            if ("glCreateShader" in msg or "undefined alternate function" in msg
                    or "context" in msg.lower()):
                return 0
            print(f"ERROR: Exception during terrain shader compilation: {e}")
            return 0
        block_index = gl.glGetUniformBlockIndex(program, 'FioLightBlock')
        invalid = getattr(gl, 'GL_INVALID_INDEX', 0xFFFFFFFF)
        if block_index != invalid:
            gl.glUniformBlockBinding(program, block_index, shaders.LIGHT_UBO_BINDING)
        return program

    def _init_shader(self):
        self.shader_program = self._compile_program('terrain.vert')
        if not self.shader_program:
            return
        self._init_grass_shader()
        self.uniforms = {}

    def _init_grass_shader(self):
        try:
            vertex_code = shaders.DEFAULT_SHADERS['grass.vert']
            fragment_code = shaders.DEFAULT_SHADERS['grass.frag']
            vertex_shader = compileShader(vertex_code, gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(fragment_code, gl.GL_FRAGMENT_SHADER)
            self.grass_shader_program = compileProgram(
                vertex_shader, fragment_shader, validate=False)
        except Exception as e:
            msg = str(e)
            if ("glCreateShader" in msg or "undefined alternate function" in msg
                    or "context" in msg.lower()):
                self.grass_shader_program = 0
                return
            print(f"ERROR: Exception during grass shader compilation: {e}")
            self.grass_shader_program = 0
            return

        self.grass_uniforms = {
            'projection': gl.glGetUniformLocation(self.grass_shader_program, 'projection'),
            'view': gl.glGetUniformLocation(self.grass_shader_program, 'view'),
            'time': gl.glGetUniformLocation(self.grass_shader_program, 'time'),
            'grassColor': gl.glGetUniformLocation(self.grass_shader_program, 'grassColor'),
            'cameraPos': gl.glGetUniformLocation(self.grass_shader_program, 'cameraPos'),
            'windStrength': gl.glGetUniformLocation(self.grass_shader_program, 'windStrength'),
            'uFogEnabled': gl.glGetUniformLocation(self.grass_shader_program, 'uFogEnabled'),
            'uFogColor': gl.glGetUniformLocation(self.grass_shader_program, 'uFogColor'),
            'uFogStart': gl.glGetUniformLocation(self.grass_shader_program, 'uFogStart'),
            'uFogEnd': gl.glGetUniformLocation(self.grass_shader_program, 'uFogEnd'),
            'uFogDensity': gl.glGetUniformLocation(self.grass_shader_program, 'uFogDensity'),
            'uFogCamPos': gl.glGetUniformLocation(self.grass_shader_program, 'uFogCamPos'),
            'uAmbient': gl.glGetUniformLocation(self.grass_shader_program, 'uAmbient'),
        }
        for name in ('grassTipColor', 'uSegments', 'uLodStart', 'uLodEnd',
                     'uFadeStart', 'uFadeEnd', 'uBladeHeight', 'uBladeWidth'):
            self.grass_uniforms[name] = gl.glGetUniformLocation(
                self.grass_shader_program, name)

    def load_terrain_textures(self, tex_manager):
        self.grass_tex = tex_manager.get('assets/textures/terrain/grass.jpg')
        self.rock_tex = tex_manager.get('assets/textures/terrain/rock.jpg')
        self.sand_tex = tex_manager.get('assets/textures/terrain/sand.jpg')
        self.snow_tex = tex_manager.get('assets/textures/terrain/snow.jpg')
        if any(t == -1 for t in [self.grass_tex, self.rock_tex, self.sand_tex, self.snow_tex]):
            print("Warning: Some terrain textures failed to load!")
    
    def is_solid(self) -> bool:
        return self.enabled and self.solid

    def get_tri_count(self) -> int:
        return self.table.triangle_count()
    
    def _get_height_scalar(self, world_x: float, world_z: float) -> float:
        """Surface height including any terracing - what collision sees."""
        mode = self.appearance.terrace_mode
        if mode == 'none':
            return self._get_raw_height_scalar(world_x, world_z)
        if mode == 'blocks':
            world_x, world_z = self._block_centre(world_x, world_z)
        return self._terrace_scalar(self._get_raw_height_scalar(world_x, world_z))

    def _get_raw_height_scalar(self, world_x: float, world_z: float) -> float:
        x = (world_x - self.offset_x) / self.mesh_scale
        z = (world_z - self.offset_z) / self.mesh_scale
        height = self.features.get_rolling_hills_scalar(x, z, self.biome.hills_scale)
        height = height * self.biome.hills_intensity
        if self.biome.mountains_enabled:
            mountain_h = self.features.get_mountains_scalar(x, z, self.biome.mountains_scale, self.biome.mountains_sharpness)
            height = height + mountain_h * self.biome.mountains_intensity
        if self.biome.valleys_enabled:
            valley = self.features.get_valleys_scalar(x, z, self.biome.valleys_scale, self.biome.valleys_depth)
            height = height + valley
        if self.biome.plateaus_enabled:
            plateau_h = self.features.get_plateaus_scalar(x, z, self.biome.plateaus_scale, self.biome.plateaus_flatness)
            height = height + plateau_h * self.biome.plateaus_intensity
        height = (height + 1.0) * 0.5
        height = max(0.0, min(1.0, height))
        base = self.biome.base_height + height * self.biome.height_scale + self.offset_y
        # All height sources are authored in terrain-space. Apply physical
        # scale once at the representation boundary so horizontal enlargement
        # preserves the same vertical proportions.
        base += self._sample_heightmap_scalar(world_x, world_z, base)
        base += self._sample_sculpt_scalar(world_x, world_z)
        return base * self.mesh_scale
    
    def get_height_at(self, world_x: float, world_z: float) -> float:
        """Terrain height at a world point, as collision should see it.

        Where the chunk has been built this is the built surface itself --
        the table's heightfield, interpolated over the triangles the renderer
        draws -- so the player stands on what is on screen. Elsewhere (a chunk
        not resident, or not built yet) it is the height function.
        """
        if self.appearance.terrace_mode == 'blocks':
            # A block is flat at the height its centre was built at. The
            # heightfield ramps between blocks over one grid cell, so read it
            # where it is flat - the block's centre, which may lie in the
            # neighbouring chunk.
            world_x, world_z = self._block_centre(world_x, world_z)
        chunk_x = int(math.floor((world_x - self.offset_x) / self.chunk_size))
        chunk_z = int(math.floor((world_z - self.offset_z) / self.chunk_size))
        height = self.table.height_at(chunk_x, chunk_z, world_x, world_z)
        if height is not None:
            return height
        return self._get_height_scalar(world_x, world_z)
    
    def get_height_at_safe(self, world_x: float, world_z: float) -> Optional[float]:
        if not self.enabled: return None
        min_world_x = self.min_chunk_x * self.chunk_size + self.offset_x
        max_world_x = (self.max_chunk_x + 1) * self.chunk_size + self.offset_x
        min_world_z = self.min_chunk_z * self.chunk_size + self.offset_z
        max_world_z = (self.max_chunk_z + 1) * self.chunk_size + self.offset_z
        if world_x < min_world_x or world_x > max_world_x: return None
        if world_z < min_world_z or world_z > max_world_z: return None
        return self.get_height_at(world_x, world_z)
    
    def get_terrain_bounds(self) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        min_x = self.min_chunk_x * self.chunk_size + self.offset_x
        max_x = (self.max_chunk_x + 1) * self.chunk_size + self.offset_x
        min_z = self.min_chunk_z * self.chunk_size + self.offset_z
        max_z = (self.max_chunk_z + 1) * self.chunk_size + self.offset_z
        return ((min_x, max_x), (min_z, max_z))
    
    def set_mesh_scale(self, factor: float):
        """Set uniform physical terrain scale without changing morphology."""
        factor = max(0.01, float(factor))
        self.mesh_scale = factor
        self.chunk_size = self.DEFAULT_CHUNK_SIZE * factor
        self.cleanup()
        self.mark_all_dirty()

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------
    def _textures_active(self) -> bool:
        return bool(getattr(self, 'use_textures', False)) and not self.flat_mode

    def _layer_heights(self) -> Tuple[float, float, float]:
        """Texture layer boundaries: the user's, else the biome's defaults."""
        if self.appearance.layer_heights is not None:
            return tuple(self.appearance.layer_heights)
        return terrain_style.layer_heights_for_biome(biome_key_for_name(self.biome.name))

    def _layer_height_range(self) -> Tuple[float, float]:
        """World-space (low, high) of the terrain surface, estimated once.

        Sampled on a coarse grid over the authored bounds from the raw surface
        (sculpting and heightmaps included), so layers sit at the same place
        on every chunk. Cached until the shape of the whole terrain changes;
        sculpting does not move the layers mid-stroke.
        """
        if self._height_range is None:
            if self._authored_bounds is not None:
                min_cx, max_cx, min_cz, max_cz = self._authored_bounds
            else:
                min_cx, max_cx, min_cz, max_cz = self._chunk_bounds()
            xs = np.linspace(min_cx * self.chunk_size + self.offset_x,
                             (max_cx + 1) * self.chunk_size + self.offset_x, 48)
            zs = np.linspace(min_cz * self.chunk_size + self.offset_z,
                             (max_cz + 1) * self.chunk_size + self.offset_z, 48)
            gx, gz = np.meshgrid(xs, zs, indexing='ij')
            h = self._get_raw_heights_batch(gx.ravel().astype(np.float32),
                                            gz.ravel().astype(np.float32))
            lo, hi = float(np.min(h)), float(np.max(h))
            if hi - lo < 1.0:
                hi = lo + 1.0
            self._height_range = (lo, hi)
        return self._height_range

    def _normalized_layer_height(self, heights: np.ndarray) -> np.ndarray:
        lo, hi = self._layer_height_range()
        return np.clip((np.asarray(heights) - lo) / max(hi - lo, 1e-3), 0.0, 1.0)

    def invalidate_height_range(self):
        self._height_range = None

    def set_appearance(self, **options):
        """Change any appearance options by name (see TerrainAppearance).

        Options that change the terrain's shape rebuild every chunk; the rest
        are shader uniforms and apply on the next frame. Changing an option
        by hand marks the look as 'custom' unless ``preset`` is given too.
        """
        a = self.appearance
        old_shape = a.shape_key()
        old_grass = (a.color_mode, a.layer_heights, a.layer_blend)
        if (options.get('palette') == 'custom' and 'custom_palette' not in options
                and not a.custom_palette):
            # Start a custom palette from the colours on screen.
            options['custom_palette'] = terrain_style.palette_colors(a)
        for key, value in options.items():
            if not hasattr(a, key):
                raise AttributeError(f"unknown terrain appearance option: {key}")
            setattr(a, key, value)
        if 'preset' not in options and any(
                k not in ('layer_heights', 'layer_blend', 'slope_rock') for k in options):
            a.preset = 'custom'
        a.sanitize()
        self._appearance_changed(old_shape, old_grass)

    def set_palette_color(self, index: int, color):
        """Change one palette colour (a strata band, or a height step).

        Editing a built-in palette turns it into a custom palette that starts
        from that palette's colours.
        """
        colors = terrain_style.palette_colors(self.appearance)
        if not 0 <= index < len(colors):
            raise IndexError(index)
        colors[index] = tuple(float(np.clip(c, 0.0, 1.0)) for c in color[:3])
        self.set_appearance(palette='custom', custom_palette=colors)

    def resize_palette(self, count: int):
        """Grow or shrink the palette to *count* colours (2..8), as custom."""
        colors = terrain_style.palette_colors(self.appearance)
        count = int(np.clip(count, terrain_style.MIN_PALETTE, terrain_style.MAX_PALETTE))
        while len(colors) < count:
            colors.append(colors[len(colors) % max(1, len(colors) - 1)])
        self.set_appearance(palette='custom', custom_palette=colors[:count])

    def apply_appearance_preset(self, name: str):
        a = self.appearance
        old_shape = a.shape_key()
        old_grass = (a.color_mode, a.layer_heights, a.layer_blend)
        terrain_style.apply_preset(a, name)
        self._appearance_changed(old_shape, old_grass)

    def _appearance_changed(self, old_shape, old_grass):
        a = self.appearance
        if a.shape_key() != old_shape:
            self.mark_all_dirty()
        elif (a.color_mode, a.layer_heights, a.layer_blend) != old_grass:
            self.table.mark_grass_dirty()

    def appearance_uniforms(self) -> Dict[str, object]:
        return terrain_style.shader_uniforms(
            self.appearance, self._layer_height_range(), self._layer_heights(),
            self.mesh_scale, (self.offset_x, self.offset_z))

    def _upload_appearance_uniforms(self, u):
        for name, value in self.appearance_uniforms().items():
            loc = u.get(name, -1)
            if loc is None or loc == -1:
                continue
            if name == 'uPalette':
                gl.glUniform3fv(loc, len(value), np.ascontiguousarray(value, dtype=np.float32))
            elif isinstance(value, int):
                gl.glUniform1i(loc, value)
            elif isinstance(value, float):
                gl.glUniform1f(loc, value)
            elif len(value) == 2:
                gl.glUniform2f(loc, *value)
            else:
                gl.glUniform3f(loc, *value)

    def set_biome(self, biome_name: str):
        if biome_name in BIOMES:
            self.biome = BIOMES[biome_name]
            if not self.grass_color_custom:
                self.grass_color = tuple(self.biome.color_gradient[0][1])
            # Each biome brings its own texture layer heights.
            self.appearance.layer_heights = None
            self.invalidate_height_range()
            self.table.mark_all_dirty()

    def set_grass(self, enabled: bool, density: Optional[float] = None,
                  color: Optional[Tuple[float, float, float]] = None,
                  tip_color=None):
        """Configure terrain grass; geometry is rebuilt lazily on the GL thread.

        ``color`` is the blade colour, ``tip_color`` the colour the blades
        fade to at their tips. Pass ``tip_color='auto'`` to go back to a
        sun-bleached shade of the blade colour. Colours alone need no rebuild.
        """
        rebuild = (bool(enabled) != self.grass_enabled or density is not None)
        self.grass_enabled = bool(enabled)
        if density is not None:
            self.grass_density = float(np.clip(density, 0.0, 0.06))
        if color is not None:
            self.grass_color = tuple(float(np.clip(c, 0.0, 1.0)) for c in color)
            self.grass_color_custom = True
        if isinstance(tip_color, str) and tip_color == 'auto':
            self.grass_tip_color = None
        elif tip_color is not None:
            self.grass_tip_color = tuple(float(np.clip(c, 0.0, 1.0)) for c in tip_color)
        if rebuild:
            self.table.mark_grass_dirty()

    def grass_tip_colour(self) -> Tuple[float, float, float]:
        """The tip colour in use: the chosen one, else a sun-bleached shade."""
        if self.grass_tip_color is not None:
            return tuple(self.grass_tip_color)
        base = np.asarray(self.grass_color, dtype=np.float64)
        tip = np.clip(base * 1.3, 0.0, 1.0) * 0.7 + np.array([0.80, 0.78, 0.55]) * 0.3
        return tuple(float(c) for c in tip)
    
    def set_seed(self, seed: int):
        self.seed = seed
        self.noise = PerlinNoise(seed)
        self.features = TerrainFeatures(self.noise, seed)
        self.mark_all_dirty()
    
    def set_bounds(self, min_x: int, max_x: int, min_z: int, max_z: int,
                   prune: bool = True):
        self.min_chunk_x = min_x
        self.max_chunk_x = max_x
        self.min_chunk_z = min_z
        self.max_chunk_z = max_z
        self.invalidate_height_range()
        if prune:
            self._remove_out_of_bounds_chunks()
        else:
            # Defer the GL chunk deletion to the render thread's next frame.
            self._pending_prune = True

    def set_streaming(self, enabled: bool, radius: Optional[float] = None):
        """Turn chunk streaming on/off (see :attr:`streaming`).

        ``radius`` (world units) sets how far terrain is kept resident around
        the camera; ``None``/0 leaves the current radius untouched.
        """
        self.streaming = bool(enabled)
        if radius is not None and radius > 0:
            self.stream_radius = float(radius)

    def set_world_extent(self, min_wx: float, min_wz: float,
                         max_wx: float, max_wz: float, prune: bool = True):
        """Widen (or shrink) the terrain to cover a world-space XZ rectangle.

        Converts world coordinates to chunk indices and calls :meth:`set_bounds`,
        so the terrain height field — a pure function of world position — spans
        the whole rectangle. Paired with :meth:`set_streaming` this fills a Big
        World map's terrain without meshing the entire grid at once.
        """
        cs = self.chunk_size
        self.set_bounds(
            int(math.floor((min_wx - self.offset_x) / cs)),
            int(math.floor((max_wx - self.offset_x) / cs)),
            int(math.floor((min_wz - self.offset_z) / cs)),
            int(math.floor((max_wz - self.offset_z) / cs)),
            prune=prune,
        )

    def _chunk_bounds(self):
        return (self.min_chunk_x, self.max_chunk_x,
                self.min_chunk_z, self.max_chunk_z)

    def _stream_chunks(self, camera_pos):
        """Keep only the chunks near ``camera_pos`` resident (streaming mode).

        Ensures every in-bounds chunk whose nearest point is within
        ``stream_radius`` of the camera, and evicts any resident chunk beyond
        ``stream_radius + stream_evict_padding``. Bounded work per call and
        bounded residency regardless of how far the camera has travelled, so a
        world-spanning terrain never tessellates its whole grid. The maths is
        :meth:`TerrainTable.stream`; this frees the GL side of what it evicts
        (nothing, for a chunk that was never uploaded), so it runs headlessly.
        """
        freed = self.table.stream(
            camera_pos.x, camera_pos.z, self.chunk_size, self.stream_radius,
            self.stream_evict_padding, self._chunk_bounds(),
            self.offset_x, self.offset_z)
        self._free_gl(freed)
        self.streamed_chunks = self.table.count

    # -- Editor "fill world with terrain" preview -------------------------
    def editor_fill_world(self, min_wx: float, min_wz: float,
                          max_wx: float, max_wz: float, stream_radius: float):
        """Expand + stream the terrain in the editor, reversibly.

        Unlike the runtime session (which snapshots/restores across play), the
        editor preview must not persist the widened bounds: the authored bounds
        are stashed the first time this runs and re-emitted by :meth:`to_dict`,
        so saving a filled map writes exactly the terrain the author set up.
        Idempotent — safe to call every repaint.
        """
        cs = self.chunk_size
        new_bounds = (
            int(math.floor((min_wx - self.offset_x) / cs)),
            int(math.floor((max_wx - self.offset_x) / cs)),
            int(math.floor((min_wz - self.offset_z) / cs)),
            int(math.floor((max_wz - self.offset_z) / cs)),
        )
        if self._authored_bounds is None:
            self._authored_bounds = (self.min_chunk_x, self.max_chunk_x,
                                     self.min_chunk_z, self.max_chunk_z)
            self._authored_enabled = self.enabled
        cur = (self.min_chunk_x, self.max_chunk_x,
               self.min_chunk_z, self.max_chunk_z)
        if new_bounds != cur:
            self.set_bounds(*new_bounds, prune=False)
        # The user asked to see terrain — make sure it's drawn during the
        # preview (the authored enabled flag is restored on unfill / save).
        self.enabled = True
        self.set_streaming(True, stream_radius)

    def editor_unfill_world(self):
        """Undo :meth:`editor_fill_world`, restoring the authored bounds/state."""
        if self._authored_bounds is None:
            return
        mnx, mxx, mnz, mxz = self._authored_bounds
        authored_enabled = self._authored_enabled
        self._authored_bounds = None
        self._authored_enabled = None
        self.set_bounds(mnx, mxx, mnz, mxz, prune=False)
        if authored_enabled is not None:
            self.enabled = authored_enabled
        self.set_streaming(False)
    
    def mark_all_dirty(self):
        self.invalidate_height_range()
        self.table.mark_all_dirty()

    def _remove_out_of_bounds_chunks(self):
        self._free_gl(self.table.prune_out_of_bounds(self._chunk_bounds()))

    def _sync_gl_columns(self):
        """Grow the per-slot GL arrays to the table's capacity."""
        cap = self.table.capacity
        if len(getattr(self, '_grass_count', ())) >= cap:
            return
        for name in ('_grass_vao', '_grass_vbo', '_grass_count', '_gpu_version',
                     '_block_vao', '_block_vbo', '_block_count'):
            old = getattr(self, name, np.zeros(0, dtype=np.int64))
            new = np.zeros(cap, dtype=np.int64)
            new[:len(old)] = old
            setattr(self, name, new)

    def _free_gl(self, slots):
        """Delete the GL objects of freed table slots (a GL-thread call)."""
        if not len(slots):
            return
        self._sync_gl_columns()
        for slot in slots:
            if self._grass_vao[slot]:
                gl.glDeleteVertexArrays(1, [int(self._grass_vao[slot])])
            if self._grass_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._grass_vbo[slot])])
            self._grass_vao[slot] = self._grass_vbo[slot] = 0
            self._grass_count[slot] = 0
            if self._block_vao[slot]:
                gl.glDeleteVertexArrays(1, [int(self._block_vao[slot])])
            if self._block_vbo[slot]:
                gl.glDeleteBuffers(1, [int(self._block_vbo[slot])])
            self._block_vao[slot] = self._block_vbo[slot] = 0
            self._block_count[slot] = 0

    def _get_heights_batch(self, world_x: np.ndarray, world_z: np.ndarray) -> np.ndarray:
        """Surface heights including any terracing."""
        mode = self.appearance.terrace_mode
        if mode == 'none':
            return self._get_raw_heights_batch(world_x, world_z)
        if mode == 'blocks':
            cx, cz = terrain_style.block_cell_centres(
                world_x, world_z, self._block_world_size(), self.offset_x, self.offset_z)
            raw = self._get_raw_heights_batch(cx.astype(np.float32), cz.astype(np.float32))
        else:
            raw = self._get_raw_heights_batch(world_x, world_z)
        return np.asarray(self._terrace_batch(raw), dtype=np.float32)

    # -- Terracing helpers ----------------------------------------------------
    def _terrace_step_world(self) -> float:
        return self.appearance.terrace_step * self.mesh_scale

    def _block_world_size(self) -> float:
        return self.appearance.block_size * self.mesh_scale

    def _block_centre(self, world_x: float, world_z: float) -> Tuple[float, float]:
        cell = self._block_world_size()
        cx = self.offset_x + (math.floor((world_x - self.offset_x) / cell) + 0.5) * cell
        cz = self.offset_z + (math.floor((world_z - self.offset_z) / cell) + 0.5) * cell
        return cx, cz

    def _terrace_scalar(self, h: float) -> float:
        a = self.appearance
        return terrain_style.terrace_heights(
            float(h), self._terrace_step_world(), a.terrace_ramp, a.terrace_mode)

    def _terrace_batch(self, h: np.ndarray) -> np.ndarray:
        a = self.appearance
        return terrain_style.terrace_heights(
            h, self._terrace_step_world(), a.terrace_ramp, a.terrace_mode)

    def _get_raw_heights_batch(self, world_x: np.ndarray, world_z: np.ndarray) -> np.ndarray:
        x = (world_x - self.offset_x) / self.mesh_scale
        z = (world_z - self.offset_z) / self.mesh_scale
        height = self.features.get_rolling_hills_batch(x, z, self.biome.hills_scale)
        height = height * self.biome.hills_intensity
        if self.biome.mountains_enabled:
            mountain_h = self.features.get_mountains_batch(x, z, self.biome.mountains_scale, self.biome.mountains_sharpness)
            height = height + mountain_h * self.biome.mountains_intensity
        if self.biome.valleys_enabled:
            valley = self.features.get_valleys_batch(x, z, self.biome.valleys_scale, self.biome.valleys_depth)
            height = height + valley
        if self.biome.plateaus_enabled:
            plateau_h = self.features.get_plateaus_batch(x, z, self.biome.plateaus_scale, self.biome.plateaus_flatness)
            height = height + plateau_h * self.biome.plateaus_intensity
        height = (height + 1.0) * 0.5
        height = np.clip(height, 0.0, 1.0)
        result = self.biome.base_height + height * self.biome.height_scale + self.offset_y
        # Keep the generator in terrain-space; physical scaling is applied once
        # after all height sources have been combined.
        result = result + self._sample_heightmap_batch(world_x, world_z, result)
        result = result + self._sample_sculpt_batch(world_x, world_z)
        return result * self.mesh_scale
    
    def _get_colors_batch(self, heights: np.ndarray, normalized_heights: np.ndarray) -> np.ndarray:
        colors = self.biome.color_gradient
        if not colors:
            return np.full((len(heights), 3), 0.5, dtype=np.float32)
        h = np.clip(normalized_heights, 0.0, 1.0)
        result = np.zeros((len(h), 3), dtype=np.float32)
        # FIX: allocate t once outside the loop; reset in-place each iteration
        t = np.zeros(len(h), dtype=np.float32)
        for i in range(len(colors) - 1):
            h0, c0 = colors[i]
            h1, c1 = colors[i + 1]
            mask = (h >= h0) & (h <= h1)
            if not np.any(mask): continue
            t[:] = 0.0
            if h1 > h0:
                t[mask] = (h[mask] - h0) / (h1 - h0)
            for j in range(3):
                result[mask, j] = c0[j] + t[mask] * (c1[j] - c0[j])
        last_h, last_c = colors[-1]
        above_mask = h > last_h
        if np.any(above_mask):
            result[above_mask] = last_c
        return result
    
    def _chunk_heights(self, slot: int, resolution: int) -> np.ndarray:
        """The bordered height grid of a table slot.

        The one heightfield per chunk: the renderer draws it and collision
        reads it. The drawn ``(resolution + 1)^2`` grid is sampled at exactly
        the float32 grid positions the mesh has always used, so its heights are
        bit-for-bit the ones it was built from; ``GRID_BORDER`` more samples,
        at the same spacing, run beyond each edge for the smooth normals.
        """
        table = self.table
        step = float(table.size[slot]) / resolution
        base_x = float(table.world[slot, 0])
        base_z = float(table.world[slot, 1])
        b = GRID_BORDER
        m = resolution + 1 + 2 * b

        ix_vals = np.arange(-b, resolution + 1 + b, dtype=np.float32)
        iz_vals = np.arange(-b, resolution + 1 + b, dtype=np.float32)
        ix_grid, iz_grid = np.meshgrid(ix_vals, iz_vals, indexing='ij')

        wx = base_x + ix_grid * step
        wz = base_z + iz_grid * step

        wx_flat = wx.flatten().astype(np.float32)
        wz_flat = wz.flatten().astype(np.float32)

        # --- FIXED: Use real heights even in flat mode ---
        heights_flat = self._get_heights_batch(wx_flat, wz_flat)
        return heights_flat.reshape((m, m))

    def _upload_chunk(self, slot: int, resolution: int):
        """Build one chunk at *resolution*: its heights, then its GPU copy.

        The height grid is the whole of a chunk: the vertex shader rebuilds
        position, normals, colour and UVs from it (see terrain.vert).
        """
        heights = self._chunk_heights(slot, resolution)
        lod_index = (self.LOD_RESOLUTIONS.index(resolution)
                     if resolution in self.LOD_RESOLUTIONS else 0)
        self.table.store(slot, resolution, lod_index, heights)
        self._upload_heightfield(slot)
        if self.appearance.terrace_mode == 'blocks':
            self._upload_block_mesh(slot)

    # -- Blocks ---------------------------------------------------------------

    def _block_mesh(self, slot: int) -> np.ndarray:
        """Vertex data (N, 14) of one chunk's land columns and their walls."""
        table = self.table
        lo, hi = self._layer_height_range()
        span = max(hi - lo, 1e-3)

        def colors(h):
            if self.flat_mode:
                return np.full((len(h), 3), 0.7, dtype=np.float32)
            return self._get_colors_batch(h, (h - lo) / span)

        (min_x, max_x), (min_z, max_z) = self.get_terrain_bounds()
        floor_height = None
        if self.appearance.skirt:
            floor_height = lo - 2.0 * self._terrace_step_world()
        vertices, _, _ = terrain_style.build_block_mesh(
            float(table.world[slot, 0]), float(table.world[slot, 1]),
            float(table.size[slot]), self._block_world_size(),
            self.offset_x, self.offset_z, self._get_heights_batch, colors,
            (min_x, max_x, min_z, max_z), floor_height, self.TILING_SCALE)
        return vertices

    def _upload_block_mesh(self, slot: int):
        self._sync_gl_columns()
        data = np.ascontiguousarray(self._block_mesh(slot), dtype=np.float32)
        if not self._block_vao[slot]:
            self._block_vao[slot] = int(gl.glGenVertexArrays(1))
            self._block_vbo[slot] = int(gl.glGenBuffers(1))
        gl.glBindVertexArray(int(self._block_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._block_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, max(data.nbytes, 4),
                        data if data.nbytes else None, gl.GL_STATIC_DRAW)
        stride = 14 * 4
        for loc, size, offset in ((0, 3, 0), (1, 3, 12), (2, 3, 24), (3, 2, 36), (4, 3, 44)):
            gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(loc)
        gl.glBindVertexArray(0)
        self._block_count[slot] = len(data)

    def _ensure_block_program(self) -> int:
        if not self.block_program:
            self.block_program = self._compile_program('terrain_mesh.vert')
            self.block_uniforms = {}
        return self.block_program

    def draw_block_slots(self, slots) -> int:
        """Draw built *slots* from their block meshes (the program is current)."""
        triangles = 0
        for slot in slots:
            slot = int(slot)
            if not self._block_vao[slot] or self._block_count[slot] <= 0:
                continue
            gl.glBindVertexArray(int(self._block_vao[slot]))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, int(self._block_count[slot]))
            triangles += int(self._block_count[slot]) // 3
        gl.glBindVertexArray(0)
        return triangles

    # -- GPU heightfield --------------------------------------------------

    def _ensure_height_page(self, page: int) -> int:
        """The texture array holding height-grid page *page*, created on demand.

        One layer per table slot (slot = page * layers + layer), R32F, every
        layer the bordered LOD-0 grid size; a lower LOD uses the top-left
        corner.
        """
        if not self._page_layers:
            limit = int(gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS))
            self._page_layers = max(1, min(self.HEIGHT_PAGE_LAYERS, limit))
        while len(self._height_pages) <= page:
            tex = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_R32F,
                            STORED_GRID, STORED_GRID, self._page_layers, 0,
                            gl.GL_RED, gl.GL_FLOAT, None)
            for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
            self._height_pages.append(tex)
        return self._height_pages[page]

    def _upload_heightfield(self, slot: int):
        """Copy a slot's height grid into its texture layer."""
        table = self.table
        self._sync_gl_columns()
        if not self._page_layers:
            self._ensure_height_page(0)
        page, layer = divmod(int(slot), self._page_layers)
        tex = self._ensure_height_page(page)
        n = int(table.grid_res[slot]) + 1 + 2 * GRID_BORDER
        # Stored row i is texel row y = i, column k is x = k; the drawn grid
        # starts GRID_BORDER texels in (terrain.vert offsets by the same).
        data = np.ascontiguousarray(table.heights[slot, :n, :n], dtype=np.float32)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, layer, n, n, 1,
                           gl.GL_RED, gl.GL_FLOAT, data)
        self._gpu_version[slot] = table.version[slot]

    def gradient_uniforms(self):
        """``(count, H, C, W, D)``: the biome gradient as the shader reads it.

        Each value is cast the way the old NumPy colour code cast it: stop
        heights and colours to float32, and per segment the width ``h1 - h0``
        (0 when ``h1 <= h0``) and the colour delta ``c1 - c0`` computed in
        double precision and then rounded once to float32.
        """
        stops = list(self.biome.color_gradient or ())
        limit = self.MAX_GRADIENT_STOPS
        if len(stops) > limit and not self._gradient_warned:
            print(f"[Terrain] colour gradient has {len(stops)} stops; the "
                  f"heightfield shader uses the first {limit}")
            self._gradient_warned = True
        stops = stops[:limit]
        H = np.zeros(limit, dtype=np.float32)
        C = np.zeros((limit, 3), dtype=np.float32)
        W = np.zeros(limit, dtype=np.float32)
        D = np.zeros((limit, 3), dtype=np.float32)
        for i, (h, c) in enumerate(stops):
            H[i] = h
            C[i] = [c[0], c[1], c[2]]
        for i in range(len(stops) - 1):
            h0, c0 = stops[i]
            h1, c1 = stops[i + 1]
            W[i] = (h1 - h0) if h1 > h0 else 0.0
            D[i] = [c1[j] - c0[j] for j in range(3)]
        return len(stops), H, C, W, D

    def chunk_uniforms(self, slot: int):
        """``(uChunkI, uChunkX, uChunkY)`` for one slot, as float32/int values."""
        table = self.table
        res = int(table.grid_res[slot])
        layer = int(slot) % self._page_layers if self._page_layers else 0
        lo = float(table.min_y[slot])
        hi = float(table.max_y[slot])
        height_range = hi - lo if hi > lo else 1.0
        f = lambda v: float(np.float32(v))
        return ((res, layer),
                (f(table.world[slot, 0]), f(table.world[slot, 1]),
                 f(float(table.size[slot]) / res)),
                (f(lo), f(height_range)))

    def set_heightfield_frame_uniforms(self, u, unit):
        """Per-frame heightfield uniforms: sampler unit, tiling, gradient."""
        count, H, C, W, D = self.gradient_uniforms()
        limit = self.MAX_GRADIENT_STOPS
        gl.glUniform1i(u['uHeights'], unit)
        gl.glUniform1f(u['uTiling'], float(np.float32(self.TILING_SCALE)))
        gl.glUniform1i(u['uFlatMode'], 1 if self.flat_mode else 0)
        gl.glUniform1i(u['uGradCount'], count)
        gl.glUniform1fv(u['uGradH'], limit, H)
        gl.glUniform3fv(u['uGradC'], limit, C)
        gl.glUniform1fv(u['uGradW'], limit, W)
        gl.glUniform3fv(u['uGradD'], limit, D)

    def draw_heightfield_slots(self, u, slots, unit, lod_level_loc=-1):
        """Draw built *slots* from their height grids (the program is current)."""
        table = self.table
        stale = slots[self._gpu_version[slots] != table.version[slots]]
        for slot in stale:
            self._upload_heightfield(int(slot))
        if not self._empty_vao:
            self._empty_vao = int(gl.glGenVertexArrays(1))
        gl.glBindVertexArray(self._empty_vao)
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        bound_page = -1
        triangles = 0
        for slot in slots:
            slot = int(slot)
            page = slot // self._page_layers
            if page != bound_page:
                gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._height_pages[page])
                bound_page = page
            (res, layer), cx, cy = self.chunk_uniforms(slot)
            if lod_level_loc != -1:
                gl.glUniform1i(lod_level_loc, int(table.lod[slot]))
            gl.glUniform2i(u['uChunkI'], res, layer)
            gl.glUniform3f(u['uChunkX'], *cx)
            gl.glUniform2f(u['uChunkY'], *cy)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6 * res * res)
            triangles += 2 * res * res
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindVertexArray(0)
        return triangles

    def _generate_grass_blades(self, slot: int) -> np.ndarray:
        """Deterministic per-blade instance records ``(x, y, z, size, yaw, tint)``.

        Tufts are scattered over the chunk and each spreads a handful of
        blades around its centre, so a field reads as clumps rather than an
        even carpet. Every blade root is sampled on the (possibly terraced)
        surface. Tufts are dropped on steep ground, outside the grass height
        layer (high rock and snow, low sand) and in broad noise clearings.
        """
        table = self.table
        size = float(table.size[slot])
        tufts = min(
            self.GRASS_MAX_PER_CHUNK,
            int(max(0.0, self.grass_density) * size * size)
        )
        if tufts <= 0:
            return np.empty((0, 6), dtype=np.float32)

        seed = (
            (int(self.seed) * 73856093)
            ^ (int(table.coord[slot, 0]) * 19349663)
            ^ (int(table.coord[slot, 1]) * 83492791)
        ) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        tx = float(table.world[slot, 0]) + rng.random(tufts) * size
        tz = float(table.world[slot, 1]) + rng.random(tufts) * size
        edge_roll = rng.random(tufts)

        # Slope from the unterraced surface: terrace risers are a styling of
        # the ground, not cliffs, and should not strip the grass off a hill.
        eps = 1.0
        h0 = self._get_raw_heights_batch(tx, tz)
        gx = (self._get_raw_heights_batch(tx + eps, tz) - h0) / eps
        gz = (self._get_raw_heights_batch(tx, tz + eps) - h0) / eps
        normal_y = 1.0 / np.sqrt(1.0 + gx * gx + gz * gz)
        keep = normal_y >= self.GRASS_MIN_NORMAL_Y
        # Grass grows in the grass height layer only - never on the high
        # rock and snow, nor down on the sand - thinning out across each
        # boundary. The same layers drive the terrain textures, so with
        # textures on the grass sits exactly on the grass texture.
        weight = terrain_style.grass_layer_weight(
            self._normalized_layer_height(h0), self._layer_heights(),
            self.appearance.layer_blend * 2.0)
        # Broad clearings so a meadow is never an even carpet.
        clearing = self.noise.noise2d_batch(
            (tx - self.offset_x) * 0.0035 + 91.7, (tz - self.offset_z) * 0.0035 - 13.3)
        weight = weight * np.clip((clearing + 0.3) / 0.45, 0.0, 1.0)
        keep &= edge_roll < weight
        tx = tx[keep]
        tz = tz[keep]
        if len(tx) == 0:
            return np.empty((0, 6), dtype=np.float32)

        per_tuft = self.GRASS_BLADES_PER_TUFT
        count = len(tx) * per_tuft
        angle = rng.uniform(0.0, 6.2831853, count)
        radius = np.sqrt(rng.random(count)) * self.GRASS_TUFT_RADIUS
        bx = np.repeat(tx, per_tuft) + np.cos(angle) * radius
        bz = np.repeat(tz, per_tuft) + np.sin(angle) * radius

        data = np.empty((count, 6), dtype=np.float32)
        data[:, 0] = bx
        data[:, 1] = self._get_heights_batch(bx, bz)
        data[:, 2] = bz
        data[:, 3] = rng.uniform(0.65, 1.25, count)
        data[:, 4] = rng.uniform(0.0, 6.2831853, count)
        data[:, 5] = rng.uniform(0.82, 1.12, count)
        return data

    def _upload_grass_chunk(self, slot: int):
        """Generate deterministic grass instances for one chunk and upload them."""
        table = self.table
        self._sync_gl_columns()
        if not self.grass_enabled:
            self._grass_count[slot] = 0
            table.grass_dirty[slot] = False
            return
        if not self.grass_shader_program:
            self._init_grass_shader()
            if not self.grass_shader_program:
                return

        data = self._generate_grass_blades(slot)
        count = len(data)
        if count <= 0:
            self._grass_count[slot] = 0
            table.grass_dirty[slot] = False
            return

        if not self._grass_vao[slot]:
            self._grass_vao[slot] = int(gl.glGenVertexArrays(1))
            self._grass_vbo[slot] = int(gl.glGenBuffers(1))
        gl.glBindVertexArray(int(self._grass_vao[slot]))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, int(self._grass_vbo[slot]))
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STATIC_DRAW)
        stride = 6 * 4
        gl.glVertexAttribPointer(2, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(2)
        gl.glVertexAttribDivisor(2, 1)
        gl.glVertexAttribPointer(3, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(3)
        gl.glVertexAttribDivisor(3, 1)
        gl.glVertexAttribPointer(4, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(16))
        gl.glEnableVertexAttribArray(4)
        gl.glVertexAttribDivisor(4, 1)
        gl.glVertexAttribPointer(5, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(20))
        gl.glEnableVertexAttribArray(5)
        gl.glVertexAttribDivisor(5, 1)
        gl.glBindVertexArray(0)
        self._grass_count[slot] = count
        table.grass_dirty[slot] = False

    def _draw_grass(self, visible_slots, projection, view, camera_pos, env_uniforms):
        if not self.grass_enabled or not self.grass_shader_program:
            return
        # A monotonic frame clock keeps wind speed independent of actual FPS.
        import time
        now = time.perf_counter()
        last = getattr(self, '_grass_last_time', now)
        self.grass_time = (self.grass_time + max(0.0, min(now - last, 0.1))) % 100000.0
        self._grass_last_time = now
        gl.glUseProgram(self.grass_shader_program)
        u = self.grass_uniforms
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(u['time'], self.grass_time)
        gl.glUniform3f(u['grassColor'], *self.grass_color)
        gl.glUniform3f(u['cameraPos'], float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        gl.glUniform1f(u['windStrength'], 0.65)
        gl.glUniform3f(u['grassTipColor'], *self.grass_tip_colour())
        gl.glUniform1f(u['uLodStart'], self.GRASS_LOD_START)
        gl.glUniform1f(u['uLodEnd'], self.GRASS_LOD_DISTANCE)
        gl.glUniform1f(u['uFadeStart'], min(self.GRASS_FADE_START, self.GRASS_MAX_DISTANCE))
        gl.glUniform1f(u['uFadeEnd'], self.GRASS_MAX_DISTANCE)
        gl.glUniform1f(u['uBladeHeight'], self.GRASS_BLADE_HEIGHT)
        gl.glUniform1f(u['uBladeWidth'], self.GRASS_BLADE_WIDTH)

        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc == -1:
                    continue
                if isinstance(value, int):
                    gl.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl.glUniform1f(loc, value)
                else:
                    gl.glUniform3f(loc, *value)

        # Opaque tapered blades. Explicitly restore the depth state here:
        # grass must test against the already-rendered terrain, not inherit
        # depth state from a preceding renderer pass.
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glDepthMask(gl.GL_TRUE)
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        dist_sq = self.table.nearest_dist_sq(
            visible_slots, float(camera_pos.x), float(camera_pos.z))
        max_sq = self.GRASS_MAX_DISTANCE * self.GRASS_MAX_DISTANCE
        for slot, d in zip(visible_slots, dist_sq):
            if self._grass_count[slot] <= 0 or not self._grass_vao[slot]:
                continue
            if d > max_sq:
                continue
            # Whole chunks swap between detailed and single-triangle blades.
            # The shader has already morphed every blade past
            # GRASS_LOD_DISTANCE onto the far shape, and a chunk's nearest
            # point is never further than any of its blades, so the swap
            # cannot be seen.
            lod_sq = self.GRASS_LOD_DISTANCE * self.GRASS_LOD_DISTANCE
            segments = self.GRASS_SEGMENTS if d < lod_sq else 1
            gl.glUniform1i(u['uSegments'], segments)
            gl.glBindVertexArray(int(self._grass_vao[slot]))
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLES, 0, grass_vertex_count(segments),
                int(self._grass_count[slot]))
        gl.glBindVertexArray(0)
        # Grass blades are double-sided. Put culling back the way it was
        # found -- the terrain pass runs with it off. Forcing it on leaked
        # into Qt's overlay painter, which then culled the SysMon panel.
        if cull_was:
            gl.glEnable(gl.GL_CULL_FACE)

    def _get_lod_resolution(self, dist_sq: float) -> int:
        for i, threshold in enumerate(self.LOD_DISTANCES_SQ):
            if dist_sq < threshold:
                return self.LOD_RESOLUTIONS[i]
        return self.LOD_RESOLUTIONS[-1]

    def _near_detail_radius(self) -> float:
        """Radius of the protected full-detail zone for this frame.

        :data:`NEAR_DETAIL_RADIUS` normally, but never wider than the stream
        radius while streaming: a chunk outside the stream radius is evicted,
        so protecting it only forced chunks to be resident that the streamer
        was told not to keep.
        """
        if self.streaming and self.stream_radius > 0.0:
            return min(self.NEAR_DETAIL_RADIUS, float(self.stream_radius))
        return self.NEAR_DETAIL_RADIUS

    def _ensure_placeholder_cubemap(self) -> int:
        """Lazily create a 1x1 complete cube-map used for shadow sampler units
        that have no real depth cube-map (shadows off, or fewer cube-maps than
        sampler slots).  A complete texture on every unit guarantees no
        samplerCube is left referencing texture unit 0 or an incomplete texture,
        either of which can make glDrawArrays raise GL_INVALID_OPERATION."""
        if self._placeholder_cubemap:
            return self._placeholder_cubemap
        tex = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
        black = (ctypes.c_ubyte * 4)(0, 0, 0, 255)
        for face in range(6):
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0, gl.GL_RGBA,
                            1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, black)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        self._placeholder_cubemap = tex
        return self._placeholder_cubemap

    def update_and_render(self, projection: glm.mat4, view: glm.mat4, camera_pos: glm.vec3, frustum_planes=None, lights=None, active_lights_count=0,
                          shadow_cubemaps=None, shadow_index_map=None, shadow_unit_base=4,
                          env_uniforms=None):
        if not self.enabled: return
        if not self.shader_program:
            self._init_shader()
        prog = self.shader_program
        if not prog:
            return
        u = self.uniforms
        # Blocks are drawn from per-chunk meshes by their own vertex shader;
        # everything else from the heightfield.
        blocks = (self.appearance.terrace_mode == 'blocks'
                  and bool(self._ensure_block_program()))
        if blocks:
            prog = self.block_program
            u = self.block_uniforms

        # Terrain can be constructed before the GL context exists. In that
        # case _init_shader() cannot create the grass program either, and the
        # renderer may later inject an externally compiled terrain program
        # without calling _init_shader() again. Retry grass independently once
        # a real GL context is current.
        if self.grass_enabled and not self.grass_shader_program:
            self._init_grass_shader()

        # Re-resolve late-bound uniforms against the *current* program.  The
        # renderer can swap in an externally-compiled program whose uniform
        # table only covers a subset of locations (see
        # Renderer.setup_terrain_shader), so any sampler we rely on must be
        # looked up here or it stays unbound.  An unassigned ``samplerCube``
        # defaults to texture unit 0, collides with the ``sampler2D`` terrain
        # textures bound there, and makes glDrawArrays raise
        # GL_INVALID_OPERATION.
        names = self._UNIFORM_NAMES + tuple(env_uniforms or ())
        names += tuple(f'shadowMaps[{i}]' for i in range(shaders.MAX_SHADOW_LIGHTS))
        for name in names:
            if name not in u:
                u[name] = gl.glGetUniformLocation(prog, name)

        self.visible_chunks = 0
        self.culled_chunks = 0
        self.total_triangles = 0

        # Apply any bounds change that deferred its prune to the GL thread.
        if self._pending_prune:
            self._remove_out_of_bounds_chunks()
            self._pending_prune = False

        if self.streaming:
            # The stream radius is the caller's to set (Big World derives it
            # from its activation radius) and is not inflated here. It used to
            # be raised to the 4096-unit protected zone plus a chunk every
            # frame, which on a Big World map meant ~1000 resident full-detail
            # chunks whatever the map asked for. The protected zone shrinks to
            # the stream radius instead -- see _near_detail_radius().
            self._stream_chunks(camera_pos)
        else:
            self.table.ensure_bounds(self._chunk_bounds(), self.chunk_size,
                                     self.offset_x, self.offset_z)
        
        gl.glUseProgram(prog)
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.grass_tex); gl.glUniform1i(u['texGrass'], 0)
        gl.glActiveTexture(gl.GL_TEXTURE1); gl.glBindTexture(gl.GL_TEXTURE_2D, self.rock_tex);  gl.glUniform1i(u['texRock'],  1)
        gl.glActiveTexture(gl.GL_TEXTURE2); gl.glBindTexture(gl.GL_TEXTURE_2D, self.sand_tex);  gl.glUniform1i(u['texSand'],  2)
        gl.glActiveTexture(gl.GL_TEXTURE3); gl.glBindTexture(gl.GL_TEXTURE_2D, self.snow_tex);  gl.glUniform1i(u['texSnow'],  3)
        self._upload_appearance_uniforms(u)
        
        # Force textures off if flat_mode is enabled or textures aren't loaded
        textures_loaded = (self.grass_tex != 0 and self.rock_tex != 0
                           and self.sand_tex != 0 and self.snow_tex != 0)
        use_tex = 0 if self.flat_mode or not textures_loaded else (1 if getattr(self, 'use_textures', True) else 0)
        gl.glUniform1i(u['use_textures'], use_tex)

        # Distance fog + global ambient (engine.shaders.FOG_GLSL). The terrain
        # owns its program and its own uniform table, so the renderer hands the
        # already-resolved values across rather than writing them itself --
        # otherwise a terrain running its own fallback program would be the one
        # surface in the level that ignored the fog and stayed sharp right up
        # to the far plane.
        if env_uniforms:
            for name, value in env_uniforms.items():
                loc = u.get(name, -1)
                if loc is None or loc == -1:
                    continue
                if isinstance(value, int):
                    gl.glUniform1i(loc, value)
                elif isinstance(value, float):
                    gl.glUniform1f(loc, value)
                else:
                    gl.glUniform3f(loc, *value)
        
        # The renderer has already selected the terrain light subset and
        # packed it into the shared std140 light UBO.
        gl.glUniform1i(u['active_lights'], active_lights_count)

        # Bind depth cube-maps so terrain receives point-light shadows.
        #
        # Every ``samplerCube shadowMaps[i]`` uniform must be pointed at its own
        # reserved texture unit *unconditionally*.  GLSL samplers default to
        # texture unit 0, which already holds a ``sampler2D`` (texGrass).  The
        # spec forbids two different sampler types referencing the same texture
        # image unit, so leaving the cube samplers on unit 0 makes the driver
        # raise GL_INVALID_OPERATION on the very next draw call.  We therefore
        # always assign the units and bind a *complete* cube-map to each — the
        # real depth cube-map when available, otherwise a 1x1 placeholder — even
        # when shadows are disabled or fewer cube-maps than sampler slots are
        # supplied.  Binding the incomplete default texture (name 0) would leave
        # an active samplerCube pointing at an incomplete texture, which some
        # drivers also reject at draw time.
        shadow_cubemaps = shadow_cubemaps or []
        placeholder = self._ensure_placeholder_cubemap()
        for i in range(shaders.MAX_SHADOW_LIGHTS):
            loc = u.get(f'shadowMaps[{i}]', -1)
            if loc is None or loc == -1:
                continue
            cm = shadow_cubemaps[i] if (i < len(shadow_cubemaps) and shadow_cubemaps[i]) else placeholder
            gl.glActiveTexture(gl.GL_TEXTURE0 + shadow_unit_base + i)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
            gl.glUniform1i(loc, shadow_unit_base + i)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        
        lod_level_loc = u.get('lod_level', -1)
        table = self.table
        self._sync_gl_columns()
        slots = table.live_slots()
        cam_x = float(camera_pos.x)
        cam_z = float(camera_pos.z)
        # Frustum (far plane included, so the view distance applies) only
        # decides what is *drawn*. Mesh upkeep still runs for every resident
        # chunk, so turning round never exposes a chunk that was skipped while
        # it was behind the camera. Nearest chunk-point distance, so an edge
        # cannot drop LOD while it is still inside the protected radius; the
        # protected zone is pre-promoted a whole chunk ring wide, so there is
        # no visible LOD upgrade as the player crosses its boundary.
        visible = table.visible(slots, frustum_planes)
        dist_sq = table.nearest_dist_sq(slots, cam_x, cam_z)
        queue_slots, queue_res = table.schedule(
            slots, dist_sq, visible, self._near_detail_radius(),
            self.LOD_RESOLUTIONS, self.LOD_DISTANCES_SQ,
            self.LOD_HYSTERESIS_FRAMES)
        self.culled_chunks = int(len(slots) - np.count_nonzero(visible))

        if self.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        # The height grids live in texture units above the shadow cube-maps.
        unit = shadow_unit_base + shaders.MAX_SHADOW_LIGHTS
        drawn = slots[visible & table.built[slots]]
        if blocks:
            self.total_triangles = self.draw_block_slots(drawn)
        else:
            self.set_heightfield_frame_uniforms(u, unit)
            self.total_triangles = self.draw_heightfield_slots(
                u, drawn, unit, lod_level_loc)
        self.visible_chunks = int(len(drawn))
        #: The slots drawn this frame, in draw order (tests, Debug Tables).
        self.drawn_slots = drawn
        if self.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        # Dirty meshes first, then what the camera can see, nearest first.
        budget_end = time.perf_counter() + self.UPDATE_BUDGET_MS / 1000.0
        limit = self.MAX_UPDATES_PER_FRAME
        for i, (slot, resolution) in enumerate(zip(queue_slots[:limit], queue_res[:limit])):
            if i and time.perf_counter() >= budget_end:
                break
            self._upload_chunk(int(slot), int(resolution))

        # Grass has its own dirty queue. It is rebuilt in the same bounded
        # fashion as terrain meshes, so moving the density slider cannot spike
        # the frame by rebuilding every resident chunk at once.
        if self.grass_enabled:
            grass_updates = slots[table.grass_dirty[slots]]
            for slot in grass_updates[:self.MAX_UPDATES_PER_FRAME]:
                self._upload_grass_chunk(int(slot))
        else:
            self._grass_count[slots] = 0

        if self.grass_enabled:
            # After this frame's builds, as the chunk bounds may have moved.
            candidates = slots[table.built[slots] & (self._grass_count[slots] > 0)]
            visible_grass = candidates[table.visible(candidates, frustum_planes)]
            self._draw_grass(
                visible_grass, projection, view, camera_pos, env_uniforms)

    def get_2d_contours(self, axis1: str, axis2: str, view_bounds: Tuple[float, float, float, float], resolution: int = 32) -> List[Tuple[List[float], List[float], float]]:
        if not self.enabled: return []
        min1, max1, min2, max2 = view_bounds
        contours = []
        if axis1 == 'x' and axis2 == 'z':
            t_bounds = self.get_terrain_bounds()
            t_min_x, t_max_x = t_bounds[0]
            t_min_z, t_max_z = t_bounds[1]
            sample_min_x = max(min1, t_min_x)
            sample_max_x = min(max1, t_max_x)
            sample_min_z = max(min2, t_min_z)
            sample_max_z = min(max2, t_max_z)
            if sample_max_x <= sample_min_x or sample_max_z <= sample_min_z: return []
            boundary_x = []
            boundary_z = []
            for x in np.linspace(sample_min_x, sample_max_x, resolution):
                boundary_x.append(x); boundary_z.append(sample_min_z)
            for z in np.linspace(sample_min_z, sample_max_z, resolution):
                boundary_x.append(sample_max_x); boundary_z.append(z)
            for x in np.linspace(sample_max_x, sample_min_x, resolution):
                boundary_x.append(x); boundary_z.append(sample_max_z)
            for z in np.linspace(sample_max_z, sample_min_z, resolution):
                boundary_x.append(sample_min_x); boundary_z.append(z)
            if boundary_x: contours.append((boundary_x, boundary_z, 0))
        elif axis1 == 'x' and axis2 == 'y':
            t_bounds = self.get_terrain_bounds()
            t_min_x, t_max_x = t_bounds[0]
            t_min_z, t_max_z = t_bounds[1]
            center_z = (t_min_z + t_max_z) / 2
            sample_min_x = max(min1, t_min_x)
            sample_max_x = min(max1, t_max_x)
            if sample_max_x > sample_min_x:
                xs = []
                ys = []
                for x in np.linspace(sample_min_x, sample_max_x, resolution * 2):
                    h = self.get_height_at(x, center_z)
                    xs.append(x)
                    ys.append(h)
                if xs: contours.append((xs, ys, center_z))
        elif axis1 == 'z' and axis2 == 'y':
            t_bounds = self.get_terrain_bounds()
            t_min_x, t_max_x = t_bounds[0]
            t_min_z, t_max_z = t_bounds[1]
            center_x = (t_min_x + t_max_x) / 2
            sample_min_z = max(min1, t_min_z)
            sample_max_z = min(max1, t_max_z)
            if sample_max_z > sample_min_z:
                zs = []
                ys = []
                for z in np.linspace(sample_min_z, sample_max_z, resolution * 2):
                    h = self.get_height_at(center_x, z)
                    zs.append(z)
                    ys.append(h)
                if zs: contours.append((zs, ys, center_x))
        return contours
    
    def generate_trees(self) -> List[Tuple[float, float, float]]:
        self.tree_positions = []
        if self.biome.tree_density <= 0: return []
        data = self.generate_tree_data()
        self.tree_positions = [tuple(d['pos']) for d in data]
        return self.tree_positions

    def generate_tree_data(self) -> List[Dict]:
        if not self.biome.trees_enabled or self.biome.tree_density <= 0: return []
        trees = []
        random.seed(self.seed + 12345) 
        chunk_width_units = self.chunk_size
        world_min_x = self.min_chunk_x * chunk_width_units + self.offset_x
        world_max_x = (self.max_chunk_x + 1) * chunk_width_units + self.offset_x
        world_min_z = self.min_chunk_z * chunk_width_units + self.offset_z
        world_max_z = (self.max_chunk_z + 1) * chunk_width_units + self.offset_z
        width = world_max_x - world_min_x
        depth = world_max_z - world_min_z
        area = abs(width * depth)
        if area <= 0: return []
        count = int(area * 0.01 * self.biome.tree_density)
        tree_options = [{"path": "assets/models/LowPoly_Tree_v1.obj", "rot": [270, 0, 0]}, {"path": "assets/models/Tree low.obj", "rot": [0, 0, 0]}]
        for _ in range(count):
            tx = random.uniform(world_min_x, world_max_x)
            tz = random.uniform(world_min_z, world_max_z)
            ty = self.get_height_at(tx, tz)
            if ty < self.biome.base_height + 2: continue
            model_def = random.choice(tree_options)
            s = random.uniform(0.5, 1.0)
            trees.append({"model_path": model_def["path"], "pos": [tx, ty, tz], "rotation": list(model_def["rot"]), "scale": [s, s, s]})
        return trees
    
    # =========================================================================
    # SCULPT DEFORMATION
    # =========================================================================

    def _sample_sculpt_scalar(self, world_x: float, world_z: float) -> float:
        """Get interpolated sculpt offset at a world position."""
        if not self.sculpt_offsets:
            return 0.0
        res = self.sculpt_grid_resolution
        gx_f = world_x / res
        gz_f = world_z / res
        gx0 = int(math.floor(gx_f))
        gz0 = int(math.floor(gz_f))
        fx = gx_f - gx0
        fz = gz_f - gz0
        h00 = self.sculpt_offsets.get((gx0, gz0), 0.0)
        h10 = self.sculpt_offsets.get((gx0 + 1, gz0), 0.0)
        h01 = self.sculpt_offsets.get((gx0, gz0 + 1), 0.0)
        h11 = self.sculpt_offsets.get((gx0 + 1, gz0 + 1), 0.0)
        top = h00 + fx * (h10 - h00)
        bot = h01 + fx * (h11 - h01)
        return top + fz * (bot - top)

    #: Largest sculpt bounding box (grid cells) packed into a dense array for
    #: vectorised sampling -- 16M float32 cells is 64 MB. A sparser, wider
    #: sculpt falls back to per-point dictionary lookups.
    MAX_DENSE_SCULPT_CELLS = 16 * 1024 * 1024

    def _touch_sculpt(self):
        """Invalidate the dense sculpt grid; call after any sculpt change."""
        self._sculpt_version = getattr(self, '_sculpt_version', 0) + 1

    def _dense_sculpt(self):
        """``(min_gx, min_gz, grid)`` for the sculpt offsets, or None.

        The offsets are a sparse dict keyed by grid cell, and sampling them one
        vertex at a time in Python cost ~6 ms per call -- twice per chunk, for
        every chunk streamed in anywhere in the world, sculpted or not. Packed
        once into a dense array over their bounding box, sampling a chunk is a
        handful of NumPy operations. Rebuilt only when the offsets change.
        """
        offsets = self.sculpt_offsets
        key = (id(offsets), len(offsets), getattr(self, '_sculpt_version', 0))
        cache = getattr(self, '_sculpt_cache', None)
        if cache is not None and cache[0] == key:
            return cache[1]
        n = len(offsets)
        keys = np.fromiter((c for k in offsets for c in k),
                           dtype=np.int64, count=2 * n).reshape(n, 2)
        vals = np.fromiter(offsets.values(), dtype=np.float32, count=n)
        min_gx, min_gz = (int(v) for v in keys.min(axis=0))
        max_gx, max_gz = (int(v) for v in keys.max(axis=0))
        w, h = max_gx - min_gx + 1, max_gz - min_gz + 1
        dense = None
        if w * h <= self.MAX_DENSE_SCULPT_CELLS:
            grid = np.zeros((w, h), dtype=np.float32)
            grid[keys[:, 0] - min_gx, keys[:, 1] - min_gz] = vals
            dense = (min_gx, min_gz, grid)
        self._sculpt_cache = (key, dense)
        return dense

    def _sample_sculpt_batch(self, world_x: np.ndarray, world_z: np.ndarray) -> np.ndarray:
        """Get interpolated sculpt offsets for arrays of world positions."""
        if not self.sculpt_offsets:
            return np.zeros(len(world_x), dtype=np.float32)
        res = self.sculpt_grid_resolution
        gx_f = world_x / res
        gz_f = world_z / res
        gx0 = np.floor(gx_f).astype(np.int32)
        gz0 = np.floor(gz_f).astype(np.int32)
        fx = gx_f - gx0
        fz = gz_f - gz0
        dense = self._dense_sculpt()
        if dense is not None:
            min_gx, min_gz, grid = dense
            w, h = grid.shape
            ix = gx0.astype(np.int64) - min_gx
            iz = gz0.astype(np.int64) - min_gz
            # Nothing sculpted within reach of these points: the common case
            # for a streamed chunk away from the sculpted area.
            if (ix.max() < -1 or ix.min() >= w
                    or iz.max() < -1 or iz.min() >= h):
                return np.zeros(len(world_x), dtype=np.float32)

            def at(ax, az):
                inside = (ax >= 0) & (ax < w) & (az >= 0) & (az < h)
                out = np.zeros(len(ax), dtype=np.float32)
                out[inside] = grid[ax[inside], az[inside]]
                return out

            h00 = at(ix, iz)
            h10 = at(ix + 1, iz)
            h01 = at(ix, iz + 1)
            h11 = at(ix + 1, iz + 1)
            top = h00 + fx * (h10 - h00)
            bot = h01 + fx * (h11 - h01)
            return (top + fz * (bot - top)).astype(np.float32)
        result = np.zeros(len(world_x), dtype=np.float32)
        for i in range(len(world_x)):
            h00 = self.sculpt_offsets.get((int(gx0[i]), int(gz0[i])), 0.0)
            h10 = self.sculpt_offsets.get((int(gx0[i]) + 1, int(gz0[i])), 0.0)
            h01 = self.sculpt_offsets.get((int(gx0[i]), int(gz0[i]) + 1), 0.0)
            h11 = self.sculpt_offsets.get((int(gx0[i]) + 1, int(gz0[i]) + 1), 0.0)
            top = h00 + fx[i] * (h10 - h00)
            bot = h01 + fx[i] * (h11 - h01)
            result[i] = top + fz[i] * (bot - top)
        return result

    def apply_sculpt_at(self, world_x: float, world_z: float, radius: float, strength: float):
        """Raise/lower terrain in a circular area. Negative strength lowers."""
        res = self.sculpt_grid_resolution
        grid_radius = int(math.ceil(radius / res)) + 1
        center_gx = world_x / res
        center_gz = world_z / res
        for dx in range(-grid_radius, grid_radius + 1):
            for dz in range(-grid_radius, grid_radius + 1):
                gx = int(math.floor(center_gx)) + dx
                gz = int(math.floor(center_gz)) + dz
                wx = gx * res
                wz = gz * res
                dist = math.sqrt((wx - world_x) ** 2 + (wz - world_z) ** 2)
                if dist > radius:
                    continue
                # Smooth falloff
                falloff = 1.0 - (dist / radius)
                falloff = falloff * falloff  # quadratic
                key = (gx, gz)
                current = self.sculpt_offsets.get(key, 0.0)
                self.sculpt_offsets[key] = current + strength * falloff
        self._mark_sculpt_region_dirty(world_x, world_z, radius)

    def smooth_sculpt_at(self, world_x: float, world_z: float, radius: float, strength: float):
        """Smooth sculpt offsets by averaging neighbours."""
        res = self.sculpt_grid_resolution
        grid_radius = int(math.ceil(radius / res)) + 1
        center_gx = int(math.floor(world_x / res))
        center_gz = int(math.floor(world_z / res))
        new_offsets = {}
        for dx in range(-grid_radius, grid_radius + 1):
            for dz in range(-grid_radius, grid_radius + 1):
                gx = center_gx + dx
                gz = center_gz + dz
                wx = gx * res
                wz = gz * res
                dist = math.sqrt((wx - world_x) ** 2 + (wz - world_z) ** 2)
                if dist > radius:
                    continue
                falloff = 1.0 - (dist / radius)
                # Average with neighbours
                avg = 0.0
                count = 0
                for nx, nz in [(gx-1, gz), (gx+1, gz), (gx, gz-1), (gx, gz+1), (gx, gz)]:
                    avg += self.sculpt_offsets.get((nx, nz), 0.0)
                    count += 1
                avg /= count
                current = self.sculpt_offsets.get((gx, gz), 0.0)
                new_offsets[(gx, gz)] = current + (avg - current) * strength * falloff
        self.sculpt_offsets.update(new_offsets)
        self._mark_sculpt_region_dirty(world_x, world_z, radius)

    def flatten_sculpt_at(self, world_x: float, world_z: float, radius: float, strength: float):
        """Push sculpt offsets toward zero (flattening the deformation)."""
        res = self.sculpt_grid_resolution
        grid_radius = int(math.ceil(radius / res)) + 1
        center_gx = int(math.floor(world_x / res))
        center_gz = int(math.floor(world_z / res))
        for dx in range(-grid_radius, grid_radius + 1):
            for dz in range(-grid_radius, grid_radius + 1):
                gx = center_gx + dx
                gz = center_gz + dz
                key = (gx, gz)
                if key not in self.sculpt_offsets:
                    continue
                wx = gx * res
                wz = gz * res
                dist = math.sqrt((wx - world_x) ** 2 + (wz - world_z) ** 2)
                if dist > radius:
                    continue
                falloff = 1.0 - (dist / radius)
                current = self.sculpt_offsets[key]
                self.sculpt_offsets[key] = current * (1.0 - strength * falloff)
                # Clean up near-zero entries
                if abs(self.sculpt_offsets[key]) < 0.01:
                    del self.sculpt_offsets[key]
        self._mark_sculpt_region_dirty(world_x, world_z, radius)

    def clear_sculpt(self):
        """Remove all sculpt deformations."""
        self.sculpt_offsets.clear()
        self._touch_sculpt()
        self.mark_all_dirty()

    def _mark_sculpt_region_dirty(self, world_x: float, world_z: float, radius: float):
        """Mark chunks overlapping a sculpted region as dirty."""
        self._touch_sculpt()
        self.table.mark_dirty_region(world_x, world_z, radius)

    # =========================================================================
    # HEIGHTMAP OVERLAY
    # =========================================================================

    def load_heightmap(self, image_path: str):
        """Load a grayscale image as a heightmap overlay.
        Bright pixels = high, dark pixels = low. Values are normalised to 0..1."""
        try:
            from PIL import Image
        except ImportError:
            # Fallback to Qt
            from PyQt5.QtGui import QImage
            img = QImage(image_path)
            if img.isNull():
                raise ValueError(f"Could not load image: {image_path}")
            img = img.convertToFormat(QImage.Format_Grayscale8)
            w, h = img.width(), img.height()
            ptr = img.bits()
            ptr.setsize(w * h)
            arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, w))
            self.heightmap_data = arr.astype(np.float32) / 255.0
            self.mark_all_dirty()
            return
        img = Image.open(image_path).convert('L')
        arr = np.array(img, dtype=np.float32) / 255.0
        self.heightmap_data = arr
        self.mark_all_dirty()

    def clear_heightmap(self):
        """Remove the heightmap overlay."""
        self.heightmap_data = None
        self.mark_all_dirty()

    def _sample_heightmap_scalar(self, world_x: float, world_z: float, proc_height: float) -> float:
        """Sample the heightmap at a world position. Returns height offset."""
        if self.heightmap_data is None:
            return 0.0
        u, v = self._world_to_heightmap_uv(world_x, world_z)
        if u < 0 or u > 1 or v < 0 or v > 1:
            return 0.0
        h = self._bilinear_sample(u, v)
        if self.heightmap_blend == 'replace':
            # Replace: heightmap value replaces procedural, return delta
            target = self.biome.base_height + h * self.heightmap_strength + self.offset_y
            return target - proc_height
        else:
            # Additive (default)
            return (h - 0.5) * self.heightmap_strength

    def _sample_heightmap_batch(self, world_x: np.ndarray, world_z: np.ndarray, proc_heights: np.ndarray) -> np.ndarray:
        """Sample the heightmap for arrays of world positions."""
        if self.heightmap_data is None:
            return np.zeros(len(world_x), dtype=np.float32)
        u, v = self._world_to_heightmap_uv_batch(world_x, world_z)
        mask = (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
        result = np.zeros(len(world_x), dtype=np.float32)
        if not np.any(mask):
            return result
        h_vals = self._bilinear_sample_batch(u[mask], v[mask])
        if self.heightmap_blend == 'replace':
            target = self.biome.base_height + h_vals * self.heightmap_strength + self.offset_y
            result[mask] = target - proc_heights[mask]
        else:
            result[mask] = (h_vals - 0.5) * self.heightmap_strength
        return result

    def _world_to_heightmap_uv(self, world_x: float, world_z: float) -> Tuple[float, float]:
        """Map world XZ to heightmap UV (0..1) based on terrain bounds."""
        bounds = self.get_terrain_bounds()
        min_x, max_x = bounds[0]
        min_z, max_z = bounds[1]
        range_x = max_x - min_x
        range_z = max_z - min_z
        if range_x == 0 or range_z == 0:
            return (0.5, 0.5)
        u = (world_x - min_x) / range_x
        v = (world_z - min_z) / range_z
        return (u, v)

    def _world_to_heightmap_uv_batch(self, world_x: np.ndarray, world_z: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        bounds = self.get_terrain_bounds()
        min_x, max_x = bounds[0]
        min_z, max_z = bounds[1]
        range_x = max_x - min_x
        range_z = max_z - min_z
        if range_x == 0 or range_z == 0:
            return (np.full_like(world_x, 0.5), np.full_like(world_z, 0.5))
        u = (world_x - min_x) / range_x
        v = (world_z - min_z) / range_z
        return (u, v)

    def _bilinear_sample(self, u: float, v: float) -> float:
        """Bilinear sample from heightmap_data at normalised UV."""
        h, w = self.heightmap_data.shape
        px = u * (w - 1)
        py = v * (h - 1)
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, w - 1)
        y1 = min(y0 + 1, h - 1)
        x0 = max(0, x0)
        y0 = max(0, y0)
        fx = px - x0
        fy = py - y0
        top = self.heightmap_data[y0, x0] * (1 - fx) + self.heightmap_data[y0, x1] * fx
        bot = self.heightmap_data[y1, x0] * (1 - fx) + self.heightmap_data[y1, x1] * fx
        return top * (1 - fy) + bot * fy

    def _bilinear_sample_batch(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Bilinear sample from heightmap_data for arrays of UV coords."""
        h, w = self.heightmap_data.shape
        px = np.clip(u * (w - 1), 0, w - 1)
        py = np.clip(v * (h - 1), 0, h - 1)
        x0 = np.floor(px).astype(np.int32)
        y0 = np.floor(py).astype(np.int32)
        x1 = np.minimum(x0 + 1, w - 1)
        y1 = np.minimum(y0 + 1, h - 1)
        fx = px - x0
        fy = py - y0
        top = self.heightmap_data[y0, x0] * (1 - fx) + self.heightmap_data[y0, x1] * fx
        bot = self.heightmap_data[y1, x0] * (1 - fx) + self.heightmap_data[y1, x1] * fx
        return top * (1 - fy) + bot * fy

    def cleanup(self):
        self._free_gl(self.table.clear())
        if self.block_program:
            try:
                gl.glDeleteProgram(self.block_program)
            except Exception:
                pass
            self.block_program = 0
            self.block_uniforms = {}
        if self._height_pages:
            gl.glDeleteTextures(len(self._height_pages), self._height_pages)
            self._height_pages = []
        if self._empty_vao:
            gl.glDeleteVertexArrays(1, [self._empty_vao])
            self._empty_vao = 0
        self._gpu_version[:] = 0
    
    def to_dict(self) -> dict:
        # While the editor is previewing a Big World fill the live bounds are
        # widened to the whole world; persist the *authored* bounds instead so a
        # save never bakes the preview expansion into the map file.
        if self._authored_bounds is not None:
            min_cx, max_cx, min_cz, max_cz = self._authored_bounds
            enabled_out = self.enabled if self._authored_enabled is None else self._authored_enabled
        else:
            min_cx, max_cx = self.min_chunk_x, self.max_chunk_x
            min_cz, max_cz = self.min_chunk_z, self.max_chunk_z
            enabled_out = self.enabled
        data = {
            'enabled': enabled_out,
            'solid': self.solid,
            'seed': self.seed,
            'biome': self.biome.name,
            'chunk_size': self.chunk_size,
            'mesh_scale': self.mesh_scale,
            'base_resolution': self.base_resolution,
            'offset_x': self.offset_x,
            'offset_z': self.offset_z,
            'offset_y': self.offset_y,
            'min_chunk_x': min_cx,
            'max_chunk_x': max_cx,
            'min_chunk_z': min_cz,
            'max_chunk_z': max_cz,
            'use_textures': self.use_textures,
            'flat_mode': self.flat_mode,
            'grass_enabled': self.grass_enabled,
            'grass_density': self.grass_density,
            'grass_color': list(self.grass_color),
            'grass_color_custom': self.grass_color_custom,
            'grass_tip_color': (list(self.grass_tip_color)
                                if self.grass_tip_color is not None else None),
            'appearance': self.appearance.to_dict(),
            'custom_biome': self.biome.to_dict()
        }
        # Sculpt offsets — serialise sparse dict as list of [gx, gz, offset]
        if self.sculpt_offsets:
            data['sculpt_offsets'] = [[gx, gz, val] for (gx, gz), val in self.sculpt_offsets.items()]
            data['sculpt_grid_resolution'] = self.sculpt_grid_resolution
        # Heightmap settings (image data is NOT saved — only the path is
        # stored by the editor so the user can re-load it)
        if self.heightmap_data is not None:
            import base64, io
            buf = io.BytesIO()
            np.save(buf, self.heightmap_data)
            data['heightmap_blob'] = base64.b64encode(buf.getvalue()).decode('ascii')
            data['heightmap_strength'] = self.heightmap_strength
            data['heightmap_blend'] = self.heightmap_blend
        return data
    
    def from_dict(self, data: dict):
        self.enabled = data.get('enabled', True)
        self.solid = data.get('solid', True)
        self.seed = data.get('seed', 42)
        self.noise = PerlinNoise(self.seed)
        self.features = TerrainFeatures(self.noise, self.seed)
        biome_name = data.get('biome', 'grassy_hills')
        if biome_name in BIOMES: self.biome = BIOMES[biome_name]
        self.chunk_size = data.get('chunk_size', self.DEFAULT_CHUNK_SIZE)
        self.mesh_scale = float(data.get('mesh_scale', self.chunk_size / self.DEFAULT_CHUNK_SIZE))
        self.base_resolution = data.get('base_resolution', 48)
        self.offset_x = data.get('offset_x', 0.0)
        self.offset_z = data.get('offset_z', 0.0)
        self.offset_y = data.get('offset_y', 0.0)
        self.min_chunk_x = data.get('min_chunk_x', -2)
        self.max_chunk_x = data.get('max_chunk_x', 2)
        self.min_chunk_z = data.get('min_chunk_z', -2)
        self.max_chunk_z = data.get('max_chunk_z', 2)
        # Texture blending is opt-in for terrain, including older maps that
        # do not contain an explicit use_textures field.
        self.use_textures = data.get('use_textures', False)
        self.flat_mode = data.get('flat_mode', False)
        self.grass_enabled = data.get('grass_enabled', False)
        self.grass_density = float(np.clip(data.get('grass_density', 0.02), 0.0, 0.06))
        saved_grass_color = data.get('grass_color')
        if saved_grass_color is not None and len(saved_grass_color) >= 3:
            self.grass_color = tuple(float(np.clip(c, 0.0, 1.0)) for c in saved_grass_color[:3])
        else:
            self.grass_color = tuple(self.biome.color_gradient[0][1])
        self.grass_color_custom = bool(data.get('grass_color_custom', saved_grass_color is not None))
        saved_tip = data.get('grass_tip_color')
        if saved_tip is not None and len(saved_tip) >= 3:
            self.grass_tip_color = tuple(float(np.clip(c, 0.0, 1.0)) for c in saved_tip[:3])
        else:
            self.grass_tip_color = None
        # Maps saved before appearance options existed load with the
        # original look.
        self.appearance = terrain_style.TerrainAppearance.from_dict(data.get('appearance'))
        if 'custom_biome' in data:
            self.biome = BiomeConfig.from_dict(data['custom_biome'])
            self.biome.name = biome_display_name(self.biome.name)
        # Sculpt offsets
        self.sculpt_offsets = {}
        self._touch_sculpt()
        self.sculpt_grid_resolution = data.get('sculpt_grid_resolution', 4.0)
        for entry in data.get('sculpt_offsets', []):
            gx, gz, val = entry
            self.sculpt_offsets[(int(gx), int(gz))] = float(val)
        # Heightmap
        if 'heightmap_blob' in data:
            import base64, io
            buf = io.BytesIO(base64.b64decode(data['heightmap_blob']))
            self.heightmap_data = np.load(buf)
            self.heightmap_strength = data.get('heightmap_strength', 100.0)
            self.heightmap_blend = data.get('heightmap_blend', 'additive')
        else:
            self.heightmap_data = None
        self.mark_all_dirty()
