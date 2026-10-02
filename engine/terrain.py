import numpy as np
import OpenGL.GL as gl
import glm
import ctypes
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import math
import random
from OpenGL.GL.shaders import compileProgram, compileShader
from . import shaders

# ============================================================================
# COMPATIBILITY EXPORTS
# ============================================================================
TERRAIN_VERTEX_SHADER = shaders.DEFAULT_SHADERS['terrain.vert']
TERRAIN_FRAGMENT_SHADER = shaders.DEFAULT_SHADERS['terrain.frag']

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
    'grassy_hills': BiomeConfig(name='Grassy Hills 🌿', height_scale=200, hills_scale=0.005, hills_intensity=1.2, blend_weights=(1.5, 0.5, 0.1, 0.0), terrain_height_scale=1.0/300.0, trees_enabled=True, tree_density=0.2),
    'low_poly_valley': BiomeConfig(name='Low Poly Valley', base_height=10.0, height_scale=250.0, hills_scale=0.004, hills_intensity=0.8, mountains_enabled=True, mountains_scale=0.006, mountains_intensity=1.2, mountains_sharpness=0.2, valleys_enabled=True, valleys_depth=0.2, color_gradient=[(0.0, (0.38, 0.60, 0.25)), (0.25, (0.35, 0.55, 0.22)), (0.28, (0.55, 0.40, 0.25)), (1.0, (0.65, 0.45, 0.30))], blend_weights=(1.0, 0.0, 0.0, 0.0), trees_enabled=True, tree_density=0.15),
    'dark_cliffs': BiomeConfig(name='Dark Cliffs', base_height=0.0, height_scale=350.0, hills_scale=0.008, hills_intensity=0.5, mountains_enabled=True, mountains_scale=0.005, mountains_intensity=1.8, mountains_sharpness=0.9, plateaus_enabled=True, plateaus_scale=0.01, plateaus_intensity=0.5, color_gradient=[(0.0, (0.15, 0.15, 0.18)), (0.4, (0.25, 0.25, 0.28)), (0.7, (0.10, 0.10, 0.12)), (0.95, (0.28, 0.35, 0.25))], trees_enabled=False),
    'desert_canyon': BiomeConfig(name='Desert Canyon', base_height=20.0, height_scale=200.0, hills_scale=0.002, hills_intensity=0.3, mountains_enabled=False, plateaus_enabled=True, plateaus_scale=0.005, plateaus_intensity=1.5, plateaus_flatness=0.95, valleys_enabled=True, valleys_scale=0.004, valleys_depth=0.4, color_gradient=[(0.0, (0.60, 0.40, 0.25)), (0.2, (0.60, 0.40, 0.25)), (0.21, (0.50, 0.30, 0.15)), (0.4, (0.50, 0.30, 0.15)), (0.41, (0.70, 0.50, 0.35)), (0.6, (0.70, 0.50, 0.35)), (0.61, (0.45, 0.25, 0.15)), (1.0, (0.45, 0.25, 0.15))], trees_enabled=False),
    'jagged_peaks': BiomeConfig(name='Jagged Peaks', base_height=50.0, height_scale=500.0, hills_scale=0.005, hills_intensity=0.2, mountains_enabled=True, mountains_scale=0.004, mountains_intensity=1.5, mountains_sharpness=1.0, valleys_enabled=True, valleys_scale=0.002, valleys_depth=0.3, color_gradient=[(0.0, (0.25, 0.30, 0.20)), (0.15, (0.35, 0.35, 0.35)), (0.6, (0.50, 0.50, 0.55)), (0.8, (0.90, 0.90, 0.95))], trees_enabled=False),
    'desert': BiomeConfig(name='Desert 🏜️', height_scale=100, hills_scale=0.008, hills_intensity=0.8, blend_weights=(0.2, 0.3, 1.2, 0.0), terrain_height_scale=1.0/150.0, trees_enabled=False, tree_density=0.0),
    'mountains': BiomeConfig(name='Mountains ⛰️', height_scale=500, mountains_enabled=True, mountains_intensity=1.5, blend_weights=(0.0, 1.0, 0.0, 0.6), terrain_height_scale=1.0/700.0, trees_enabled=True, tree_density=0.15),
    'gentle_meadow': BiomeConfig(name='Gentle Meadow', base_height=0.0, height_scale=80.0, hills_scale=0.003, hills_intensity=0.8, mountains_enabled=False, valleys_enabled=True, valleys_scale=0.002, valleys_depth=0.15, plateaus_enabled=False, color_gradient=[(0.0, (0.30, 0.50, 0.22)), (0.2, (0.38, 0.58, 0.28)), (0.5, (0.45, 0.62, 0.32)), (0.7, (0.50, 0.65, 0.35)), (1.0, (0.55, 0.68, 0.38))], blend_weights=(1.2, 0.2, 0.1, 0.0), terrain_height_scale=1.0/120.0, trees_enabled=True, tree_density=0.4),
    'rolling_highlands': BiomeConfig(name='Rolling Highlands', base_height=20.0, height_scale=150.0, hills_scale=0.004, hills_intensity=0.7, mountains_enabled=True, mountains_scale=0.006, mountains_intensity=0.5, mountains_sharpness=0.3, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.25, plateaus_enabled=False, color_gradient=[(0.0, (0.28, 0.48, 0.20)), (0.25, (0.38, 0.55, 0.28)), (0.5, (0.45, 0.60, 0.32)), (0.7, (0.50, 0.55, 0.38)), (0.85, (0.55, 0.52, 0.42)), (1.0, (0.65, 0.62, 0.55))], blend_weights=(1.0, 0.4, 0.2, 0.1), terrain_height_scale=1.0/225.0, trees_enabled=True, tree_density=0.3),
    'rocky_mountains': BiomeConfig(name='Rocky Mountains', base_height=50.0, height_scale=300.0, hills_scale=0.003, hills_intensity=0.4, mountains_enabled=True, mountains_scale=0.008, mountains_intensity=1.0, mountains_sharpness=0.6, valleys_enabled=True, valleys_scale=0.004, valleys_depth=0.35, plateaus_enabled=False, color_gradient=[(0.0, (0.30, 0.45, 0.22)), (0.15, (0.38, 0.52, 0.28)), (0.3, (0.42, 0.40, 0.32)), (0.5, (0.50, 0.45, 0.38)), (0.7, (0.58, 0.52, 0.45)), (0.85, (0.68, 0.65, 0.58)), (1.0, (0.88, 0.86, 0.82))], blend_weights=(0.1, 1.2, 0.0, 0.5), terrain_height_scale=1.0/450.0, trees_enabled=True, tree_density=0.15),
    'desert_mesas': BiomeConfig(name='Desert Mesas', base_height=10.0, height_scale=180.0, hills_scale=0.004, hills_intensity=0.5, mountains_enabled=False, plateaus_enabled=True, plateaus_scale=0.005, plateaus_intensity=0.9, plateaus_flatness=0.85, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.2, color_gradient=[(0.0, (0.35, 0.50, 0.25)), (0.15, (0.55, 0.42, 0.28)), (0.3, (0.68, 0.50, 0.32)), (0.5, (0.75, 0.55, 0.35)), (0.7, (0.80, 0.62, 0.40)), (1.0, (0.85, 0.70, 0.48))], blend_weights=(0.0, 0.4, 1.5, 0.0), terrain_height_scale=1.0/270.0, trees_enabled=False, tree_density=0.0),
    'alpine_forest': BiomeConfig(name='Alpine Forest', base_height=30.0, height_scale=220.0, hills_scale=0.004, hills_intensity=0.6, mountains_enabled=True, mountains_scale=0.007, mountains_intensity=0.8, mountains_sharpness=0.45, valleys_enabled=True, valleys_scale=0.003, valleys_depth=0.3, plateaus_enabled=False, color_gradient=[(0.0, (0.18, 0.35, 0.15)), (0.2, (0.25, 0.45, 0.20)), (0.4, (0.35, 0.52, 0.28)), (0.55, (0.42, 0.48, 0.32)), (0.7, (0.52, 0.50, 0.45)), (0.85, (0.65, 0.63, 0.58)), (1.0, (0.92, 0.94, 0.96))], blend_weights=(0.8, 0.6, 0.0, 0.4), terrain_height_scale=1.0/330.0, trees_enabled=True, tree_density=0.8),
    'coastal_cliffs': BiomeConfig(name='Coastal Cliffs', base_height=0.0, height_scale=120.0, hills_scale=0.005, hills_intensity=0.5, mountains_enabled=True, mountains_scale=0.01, mountains_intensity=0.6, mountains_sharpness=0.7, valleys_enabled=False, plateaus_enabled=True, plateaus_scale=0.008, plateaus_intensity=0.4, plateaus_flatness=0.6, color_gradient=[(0.0, (0.25, 0.45, 0.20)), (0.2, (0.35, 0.52, 0.28)), (0.4, (0.40, 0.38, 0.30)), (0.6, (0.48, 0.44, 0.38)), (0.8, (0.55, 0.50, 0.42)), (1.0, (0.62, 0.58, 0.48))], blend_weights=(0.3, 0.8, 0.2, 0.0), terrain_height_scale=1.0/180.0, trees_enabled=True, tree_density=0.1),
}

# ============================================================================
# HEIGHT CACHE FOR FAST COLLISION
# ============================================================================

class HeightCache:
    def __init__(self, world_x: float, world_z: float, size: float, resolution: int = 17):
        self.world_x = world_x
        self.world_z = world_z
        self.size = size
        self.resolution = resolution
        self.step = size / (resolution - 1)
        self.heights: Optional[np.ndarray] = None
        self.is_valid = False
    
    def build(self, height_func):
        self.heights = np.zeros((self.resolution, self.resolution), dtype=np.float32)
        for iz in range(self.resolution):
            for ix in range(self.resolution):
                wx = self.world_x + ix * self.step
                wz = self.world_z + iz * self.step
                self.heights[ix, iz] = height_func(wx, wz)
        self.is_valid = True

    def build_batch(self, height_func_batch):
        """Vectorised equivalent of build(): evaluate the whole grid in one
        batched call instead of resolution*resolution scalar Python calls.
        The scalar path evaluates a few fbm/ridge octaves per point, so the
        pure-Python double loop dominates chunk upload time and stalls the
        render thread; the batched noise path is 1-2 orders of magnitude
        faster for the same result."""
        ix = np.arange(self.resolution, dtype=np.float32)
        iz = np.arange(self.resolution, dtype=np.float32)
        ixg, izg = np.meshgrid(ix, iz, indexing='ij')
        wx = (self.world_x + ixg * self.step).ravel()
        wz = (self.world_z + izg * self.step).ravel()
        heights = height_func_batch(wx, wz)
        self.heights = np.asarray(heights, dtype=np.float32).reshape(
            (self.resolution, self.resolution))
        self.is_valid = True
    
    def get_height(self, world_x: float, world_z: float) -> Optional[float]:
        if not self.is_valid or self.heights is None:
            return None
        lx = (world_x - self.world_x) / self.step
        lz = (world_z - self.world_z) / self.step
        if lx < 0 or lx >= self.resolution - 1 or lz < 0 or lz >= self.resolution - 1:
            return None
        ix = int(lx)
        iz = int(lz)
        fx = lx - ix
        fz = lz - iz
        h00 = self.heights[ix, iz]
        h10 = self.heights[ix + 1, iz]
        h01 = self.heights[ix, iz + 1]
        h11 = self.heights[ix + 1, iz + 1]
        h0 = h00 + fx * (h10 - h00)
        h1 = h01 + fx * (h11 - h01)
        return h0 + fz * (h1 - h0)
    
    def invalidate(self):
        self.is_valid = False

# ============================================================================
# TERRAIN CHUNK
# ============================================================================

@dataclass
class TerrainChunk:
    chunk_x: int
    chunk_z: int
    world_x: float
    world_z: float
    size: float
    vao: int = 0
    vbo: int = 0
    vertex_count: int = 0
    min_y: float = 0.0
    max_y: float = 0.0
    center: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    lod_level: int = 0
    target_lod: int = 0
    lod_stable_frames: int = 0
    is_dirty: bool = True
    is_uploaded: bool = False
    needs_lod_update: bool = False
    height_cache: Optional[HeightCache] = None

# ============================================================================
# MAIN TERRAIN CLASS
# ============================================================================

class Terrain:
    LOD_DISTANCES_SQ = [400**2, 800**2, 1600**2, 3200**2]
    LOD_RESOLUTIONS = [48, 32, 16, 8]
    LOD_HYSTERESIS_FRAMES = 10
    HEIGHT_CACHE_RESOLUTION = 33
    MAX_UPDATES_PER_FRAME = 2
    TILING_SCALE = 20.0
    
    def __init__(self, texture_manager=None, seed: int = 42):
        self.seed = seed
        self.noise = PerlinNoise(seed)
        self.features = TerrainFeatures(self.noise, seed)
        self.biome: BiomeConfig = BIOMES['grassy_hills']
        self.chunk_size: float = 256.0
        self.base_resolution: int = 48
        self.offset_x: float = 0.0
        self.offset_z: float = 0.0
        self.offset_y: float = 0.0
        self.use_textures: bool = True
        self.flat_mode: bool = False
        self.chunks: Dict[Tuple[int, int], TerrainChunk] = {}
        self.min_chunk_x: int = -2
        self.max_chunk_x: int = 2
        self.min_chunk_z: int = -2
        self.max_chunk_z: int = 2
        self.tree_positions: List[Tuple[float, float, float]] = []
        self.total_triangles: int = 0
        self.visible_chunks: int = 0
        self.culled_chunks: int = 0
        self.shader_program: int = 0
        self.uniforms: Dict[str, int] = {}
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
        self._update_queue: List[Tuple[int, int]] = []
        # -- Chunk streaming (Big World "fill world with terrain") -----------
        # When ``streaming`` is on, only the chunks within ``stream_radius`` of
        # the camera are kept resident; chunks beyond ``stream_radius +
        # stream_evict_padding`` are freed. This lets a world-spanning terrain
        # (bounds widened by ``set_world_extent``) render without tessellating
        # the whole grid up-front — meshes appear around the camera as it moves.
        self.streaming: bool = False
        self.stream_radius: float = 1536.0
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
    
    def _init_shader(self):
        try:
            vertex_code = shaders.DEFAULT_SHADERS['terrain.vert']
            fragment_code = shaders.DEFAULT_SHADERS['terrain.frag']
            vertex_shader = compileShader(vertex_code, gl.GL_VERTEX_SHADER)
            fragment_shader = compileShader(fragment_code, gl.GL_FRAGMENT_SHADER)
            self.shader_program = compileProgram(vertex_shader, fragment_shader, validate=False)
            if not self.shader_program:
                print("ERROR: Failed to compile terrain shader program!")
                self.shader_program = 0
                return
        except Exception as e:
            print(f"ERROR: Exception during terrain shader compilation: {e}")
            self.shader_program = 0
            return
        
        self.uniforms = {
            'projection':        gl.glGetUniformLocation(self.shader_program, 'projection'),
            'view':              gl.glGetUniformLocation(self.shader_program, 'view'),
            'active_lights':     gl.glGetUniformLocation(self.shader_program, 'active_lights'),
            'use_textures':      gl.glGetUniformLocation(self.shader_program, 'use_textures'),
            'lod_level':         gl.glGetUniformLocation(self.shader_program, 'lod_level'),
            'texGrass':          gl.glGetUniformLocation(self.shader_program, 'texGrass'),
            'texRock':           gl.glGetUniformLocation(self.shader_program, 'texRock'),
            'texSand':           gl.glGetUniformLocation(self.shader_program, 'texSand'),
            'texSnow':           gl.glGetUniformLocation(self.shader_program, 'texSnow'),
            'biomeWeights':      gl.glGetUniformLocation(self.shader_program, 'biomeWeights'),
            'terrainHeightScale': gl.glGetUniformLocation(self.shader_program, 'terrainHeightScale'),
        }
        for i in range(8):
            base = f'lights[{i}]'
            self.uniforms[f'{base}.position']  = gl.glGetUniformLocation(self.shader_program, f'{base}.position')
            self.uniforms[f'{base}.color']     = gl.glGetUniformLocation(self.shader_program, f'{base}.color')
            self.uniforms[f'{base}.intensity'] = gl.glGetUniformLocation(self.shader_program, f'{base}.intensity')
            self.uniforms[f'{base}.radius']    = gl.glGetUniformLocation(self.shader_program, f'{base}.radius')
            self.uniforms[f'{base}.shadowIndex'] = gl.glGetUniformLocation(self.shader_program, f'{base}.shadowIndex')
        # Depth cube-map samplers for point-light shadows.
        for i in range(shaders.MAX_SHADOW_LIGHTS):
            self.uniforms[f'shadowMaps[{i}]'] = gl.glGetUniformLocation(self.shader_program, f'shadowMaps[{i}]')
    
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
        total = 0
        for chunk in self.chunks.values():
            if chunk.is_uploaded:
                total += chunk.vertex_count // 3
        return total
    
    def _get_height_scalar(self, world_x: float, world_z: float) -> float:
        x = world_x - self.offset_x
        z = world_z - self.offset_z
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
        # Heightmap contribution
        base += self._sample_heightmap_scalar(world_x, world_z, base)
        # Sculpt deformation contribution
        base += self._sample_sculpt_scalar(world_x, world_z)
        return base
    
    def get_height_at(self, world_x: float, world_z: float) -> float:
        chunk_x = int(math.floor((world_x - self.offset_x) / self.chunk_size))
        chunk_z = int(math.floor((world_z - self.offset_z) / self.chunk_size))
        key = (chunk_x, chunk_z)
        chunk = self.chunks.get(key)
        if chunk and chunk.height_cache and chunk.height_cache.is_valid:
            cached_height = chunk.height_cache.get_height(world_x, world_z)
            if cached_height is not None:
                return cached_height
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
    
    def set_biome(self, biome_name: str):
        if biome_name in BIOMES:
            self.biome = BIOMES[biome_name]
            self.mark_all_dirty()
    
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

    def _stream_chunks(self, camera_pos):
        """Keep only the chunks near ``camera_pos`` resident (streaming mode).

        Ensures every in-bounds chunk whose nearest point is within
        ``stream_radius`` of the camera, and evicts any resident chunk beyond
        ``stream_radius + stream_evict_padding``. Bounded work per call and
        bounded residency regardless of how far the camera has travelled, so a
        world-spanning terrain never tessellates its whole grid. Touches no GL
        for chunks that were never uploaded (``_delete_chunk`` guards on the
        chunk's VAO/VBO), so the maths is exercisable headlessly.
        """
        cam_x = float(camera_pos.x) - self.offset_x
        cam_z = float(camera_pos.z) - self.offset_z
        cs = self.chunk_size
        radius = self.stream_radius
        keep = radius + self.stream_evict_padding
        r2 = radius * radius
        keep2 = keep * keep

        cam_cx = int(math.floor(cam_x / cs))
        cam_cz = int(math.floor(cam_z / cs))
        reach = int(math.ceil(radius / cs)) + 1
        lo_x = max(self.min_chunk_x, cam_cx - reach)
        hi_x = min(self.max_chunk_x, cam_cx + reach)
        lo_z = max(self.min_chunk_z, cam_cz - reach)
        hi_z = min(self.max_chunk_z, cam_cz + reach)

        def _nearest_dist_sq(cx: int, cz: int) -> float:
            chunk_min_x = cx * cs
            chunk_min_z = cz * cs
            nx = min(max(cam_x, chunk_min_x), chunk_min_x + cs)
            nz = min(max(cam_z, chunk_min_z), chunk_min_z + cs)
            dx = nx - cam_x
            dz = nz - cam_z
            return dx * dx + dz * dz

        for cx in range(lo_x, hi_x + 1):
            for cz in range(lo_z, hi_z + 1):
                if _nearest_dist_sq(cx, cz) <= r2:
                    self._ensure_chunk(cx, cz)

        to_evict = [key for key, chunk in self.chunks.items()
                    if _nearest_dist_sq(chunk.chunk_x, chunk.chunk_z) > keep2]
        for key in to_evict:
            self._delete_chunk(key)
        self.streamed_chunks = len(self.chunks)

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
        for chunk in self.chunks.values():
            chunk.is_dirty = True
            if chunk.height_cache:
                chunk.height_cache.invalidate()
    
    def _remove_out_of_bounds_chunks(self):
        to_remove = []
        for key, chunk in self.chunks.items():
            if (chunk.chunk_x < self.min_chunk_x or chunk.chunk_x > self.max_chunk_x or
                chunk.chunk_z < self.min_chunk_z or chunk.chunk_z > self.max_chunk_z):
                to_remove.append(key)
        for key in to_remove:
            self._delete_chunk(key)
    
    def _delete_chunk(self, key: Tuple[int, int]):
        if key in self.chunks:
            chunk = self.chunks[key]
            if chunk.vao:
                gl.glDeleteVertexArrays(1, [chunk.vao])
            if chunk.vbo:
                gl.glDeleteBuffers(1, [chunk.vbo])
            del self.chunks[key]
    
    def _ensure_chunk(self, cx: int, cz: int) -> TerrainChunk:
        key = (cx, cz)
        if key not in self.chunks:
            world_x = cx * self.chunk_size + self.offset_x
            world_z = cz * self.chunk_size + self.offset_z
            chunk = TerrainChunk(chunk_x=cx, chunk_z=cz, world_x=world_x, world_z=world_z, size=self.chunk_size)
            chunk.height_cache = HeightCache(world_x, world_z, self.chunk_size, resolution=self.HEIGHT_CACHE_RESOLUTION)
            self.chunks[key] = chunk
        return self.chunks[key]
    
    def _get_heights_batch(self, world_x: np.ndarray, world_z: np.ndarray) -> np.ndarray:
        x = world_x - self.offset_x
        z = world_z - self.offset_z
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
        # Heightmap contribution (batch)
        result = result + self._sample_heightmap_batch(world_x, world_z, result)
        # Sculpt deformation contribution (batch)
        result = result + self._sample_sculpt_batch(world_x, world_z)
        return result
    
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
    
    def _generate_chunk_mesh(self, chunk: TerrainChunk, resolution: int) -> np.ndarray:
        step = chunk.size / resolution
        base_x = chunk.world_x
        base_z = chunk.world_z
        
        ix_vals = np.arange(resolution + 1, dtype=np.float32)
        iz_vals = np.arange(resolution + 1, dtype=np.float32)
        ix_grid, iz_grid = np.meshgrid(ix_vals, iz_vals, indexing='ij')
        
        wx = base_x + ix_grid * step
        wz = base_z + iz_grid * step
        
        wx_flat = wx.flatten().astype(np.float32)
        wz_flat = wz.flatten().astype(np.float32)
        
        # --- FIXED: Use real heights even in flat mode ---
        heights_flat = self._get_heights_batch(wx_flat, wz_flat)
        heights = heights_flat.reshape((resolution + 1, resolution + 1))
        
        grad_x, grad_z = np.gradient(heights, step)
        sn_x = -grad_x
        sn_y = np.ones_like(grad_x)
        sn_z = -grad_z
        len_sn = np.sqrt(sn_x**2 + sn_y**2 + sn_z**2)
        sn_x /= len_sn
        sn_y /= len_sn
        sn_z /= len_sn
        smooth_normals = np.stack([sn_x, sn_y, sn_z], axis=-1)
        
        min_height = float(heights.min())
        max_height = float(heights.max())
        height_range = max_height - min_height if max_height > min_height else 1.0
        
        y00 = heights[:-1, :-1]
        y10 = heights[1:, :-1]
        y01 = heights[:-1, 1:]
        y11 = heights[1:, 1:]
        
        sn00 = smooth_normals[:-1, :-1]
        sn10 = smooth_normals[1:, :-1]
        sn01 = smooth_normals[:-1, 1:]
        sn11 = smooth_normals[1:, 1:]
        
        ix_q = np.arange(resolution, dtype=np.float32)
        iz_q = np.arange(resolution, dtype=np.float32)
        ix_qg, iz_qg = np.meshgrid(ix_q, iz_q, indexing='ij')
        
        x0_grid = base_x + ix_qg * step
        x1_grid = base_x + (ix_qg + 1) * step
        z0_grid = base_z + iz_qg * step
        z1_grid = base_z + (iz_qg + 1) * step
        
        num_quads = resolution * resolution
        
        t1_v0 = np.stack([x0_grid, y00, z0_grid], axis=-1).reshape(-1, 3)
        t1_v1 = np.stack([x1_grid, y10, z0_grid], axis=-1).reshape(-1, 3)
        t1_v2 = np.stack([x0_grid, y01, z1_grid], axis=-1).reshape(-1, 3)
        t1_sn0 = sn00.reshape(-1, 3)
        t1_sn1 = sn10.reshape(-1, 3)
        t1_sn2 = sn01.reshape(-1, 3)
        
        edge1_t1 = t1_v1 - t1_v0
        edge2_t1 = t1_v2 - t1_v0
        n1 = np.cross(edge1_t1, edge2_t1)
        n1_len = np.linalg.norm(n1, axis=1, keepdims=True)
        n1_len[n1_len == 0] = 1
        n1 = n1 / n1_len
        
        if self.flat_mode:
            colors1 = np.full((num_quads, 3), 0.7, dtype=np.float32)
        else:
            centroid_y1 = (y00 + y10 + y01).flatten() / 3.0
            norm_h1 = (centroid_y1 - min_height) / height_range
            colors1 = self._get_colors_batch(centroid_y1, norm_h1)
            var_seed1 = (ix_qg.flatten() * 1000 + iz_qg.flatten()) % 100
            variation1 = (var_seed1 / 100.0 - 0.5) * 0.08
            colors1 = np.clip(colors1 + variation1[:, np.newaxis], 0, 1)
        
        t2_v0 = np.stack([x1_grid, y10, z0_grid], axis=-1).reshape(-1, 3)
        t2_v1 = np.stack([x1_grid, y11, z1_grid], axis=-1).reshape(-1, 3)
        t2_v2 = np.stack([x0_grid, y01, z1_grid], axis=-1).reshape(-1, 3)
        t2_sn0 = sn10.reshape(-1, 3)
        t2_sn1 = sn11.reshape(-1, 3)
        t2_sn2 = sn01.reshape(-1, 3)
        
        edge1_t2 = t2_v1 - t2_v0
        edge2_t2 = t2_v2 - t2_v0
        n2 = np.cross(edge1_t2, edge2_t2)
        n2_len = np.linalg.norm(n2, axis=1, keepdims=True)
        n2_len[n2_len == 0] = 1
        n2 = n2 / n2_len
        
        if self.flat_mode:
            colors2 = np.full((num_quads, 3), 0.7, dtype=np.float32)
        else:
            centroid_y2 = (y10 + y11 + y01).flatten() / 3.0
            norm_h2 = (centroid_y2 - min_height) / height_range
            colors2 = self._get_colors_batch(centroid_y2, norm_h2)
            var_seed2 = ((ix_qg.flatten() + 1000) * 1000 + iz_qg.flatten() + 1000) % 100
            variation2 = (var_seed2 / 100.0 - 0.5) * 0.08
            colors2 = np.clip(colors2 + variation2[:, np.newaxis], 0, 1)
        
        ux0 = x0_grid.flatten() / self.TILING_SCALE
        uz0 = z0_grid.flatten() / self.TILING_SCALE
        ux1 = x1_grid.flatten() / self.TILING_SCALE
        uz1 = z1_grid.flatten() / self.TILING_SCALE
        
        vertices = np.zeros((num_quads * 6, 14), dtype=np.float32)
        vertices[0::6, 0:3] = t1_v0;  vertices[0::6, 3:6] = n1;  vertices[0::6, 6:9] = colors1;  vertices[0::6, 9:11] = np.stack([ux0, uz0], axis=1); vertices[0::6, 11:14] = t1_sn0
        vertices[1::6, 0:3] = t1_v1;  vertices[1::6, 3:6] = n1;  vertices[1::6, 6:9] = colors1;  vertices[1::6, 9:11] = np.stack([ux1, uz0], axis=1); vertices[1::6, 11:14] = t1_sn1
        vertices[2::6, 0:3] = t1_v2;  vertices[2::6, 3:6] = n1;  vertices[2::6, 6:9] = colors1;  vertices[2::6, 9:11] = np.stack([ux0, uz1], axis=1); vertices[2::6, 11:14] = t1_sn2
        vertices[3::6, 0:3] = t2_v0;  vertices[3::6, 3:6] = n2;  vertices[3::6, 6:9] = colors2;  vertices[3::6, 9:11] = np.stack([ux1, uz0], axis=1); vertices[3::6, 11:14] = t2_sn0
        vertices[4::6, 0:3] = t2_v1;  vertices[4::6, 3:6] = n2;  vertices[4::6, 6:9] = colors2;  vertices[4::6, 9:11] = np.stack([ux1, uz1], axis=1); vertices[4::6, 11:14] = t2_sn1
        vertices[5::6, 0:3] = t2_v2;  vertices[5::6, 3:6] = n2;  vertices[5::6, 6:9] = colors2;  vertices[5::6, 9:11] = np.stack([ux0, uz1], axis=1); vertices[5::6, 11:14] = t2_sn2
        
        chunk.min_y = min_height
        chunk.max_y = max_height
        chunk.center = (base_x + chunk.size / 2, (min_height + max_height) / 2, base_z + chunk.size / 2)
        return vertices.flatten()
    
    def _upload_chunk(self, chunk: TerrainChunk, resolution: int):
        if chunk.height_cache and not chunk.height_cache.is_valid:
            chunk.height_cache.build_batch(self._get_heights_batch)
        vertex_data = self._generate_chunk_mesh(chunk, resolution)
        chunk.vertex_count = len(vertex_data) // 14
        if not chunk.vao:
            chunk.vao = gl.glGenVertexArrays(1)
            chunk.vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(chunk.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, chunk.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)
        stride = 14 * 4
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glVertexAttribPointer(3, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(36))
        gl.glEnableVertexAttribArray(3)
        gl.glVertexAttribPointer(4, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(44))
        gl.glEnableVertexAttribArray(4)
        gl.glBindVertexArray(0)
        chunk.is_dirty = False
        chunk.is_uploaded = True
        chunk.lod_level = self.LOD_RESOLUTIONS.index(resolution) if resolution in self.LOD_RESOLUTIONS else 0
    
    def _get_lod_resolution(self, dist_sq: float) -> int:
        for i, threshold in enumerate(self.LOD_DISTANCES_SQ):
            if dist_sq < threshold:
                return self.LOD_RESOLUTIONS[i]
        return self.LOD_RESOLUTIONS[-1]
    
    def _is_chunk_visible(self, chunk: TerrainChunk, frustum_planes) -> bool:
        if frustum_planes is None: return True
        half_size = chunk.size / 2
        cx, cy, cz = chunk.center
        half_y = (chunk.max_y - chunk.min_y) / 2 + 10
        for plane in frustum_planes:
            a, b, c, d = plane
            px = cx + half_size if a >= 0 else cx - half_size
            py = cy + half_y if b >= 0 else cy - half_y
            pz = cz + half_size if c >= 0 else cz - half_size
            if a * px + b * py + c * pz + d < 0: return False
        return True
    
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
                          shadow_cubemaps=None, shadow_index_map=None, shadow_unit_base=4):
        if not self.enabled: return
        if not self.shader_program:
            self._init_shader()
            if not self.shader_program: return

        # Re-resolve late-bound uniforms against the *current* program.  The
        # renderer can swap in an externally-compiled program whose uniform
        # table only covers a subset of locations (see
        # Renderer.setup_terrain_shader), so any sampler we rely on must be
        # looked up here or it stays unbound.  An unassigned ``samplerCube``
        # defaults to texture unit 0, collides with the ``sampler2D`` terrain
        # textures bound there, and makes glDrawArrays raise
        # GL_INVALID_OPERATION.
        for name in ('use_textures', 'lod_level'):
            if name not in self.uniforms:
                self.uniforms[name] = gl.glGetUniformLocation(self.shader_program, name)
        for i in range(8):
            key = f'lights[{i}].shadowIndex'
            if key not in self.uniforms:
                self.uniforms[key] = gl.glGetUniformLocation(self.shader_program, key)
        for i in range(shaders.MAX_SHADOW_LIGHTS):
            key = f'shadowMaps[{i}]'
            if key not in self.uniforms:
                self.uniforms[key] = gl.glGetUniformLocation(self.shader_program, key)

        self.visible_chunks = 0
        self.culled_chunks = 0
        self.total_triangles = 0

        # Apply any bounds change that deferred its prune to the GL thread.
        if self._pending_prune:
            self._remove_out_of_bounds_chunks()
            self._pending_prune = False

        if self.streaming:
            # Stream only the chunks around the camera; a world-spanning terrain
            # never tessellates its whole grid up-front.
            self._stream_chunks(camera_pos)
        else:
            for cz in range(self.min_chunk_z, self.max_chunk_z + 1):
                for cx in range(self.min_chunk_x, self.max_chunk_x + 1):
                    self._ensure_chunk(cx, cz)
        
        gl.glUseProgram(self.shader_program)
        gl.glUniformMatrix4fv(self.uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(self.uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.grass_tex); gl.glUniform1i(self.uniforms['texGrass'], 0)
        gl.glActiveTexture(gl.GL_TEXTURE1); gl.glBindTexture(gl.GL_TEXTURE_2D, self.rock_tex);  gl.glUniform1i(self.uniforms['texRock'],  1)
        gl.glActiveTexture(gl.GL_TEXTURE2); gl.glBindTexture(gl.GL_TEXTURE_2D, self.sand_tex);  gl.glUniform1i(self.uniforms['texSand'],  2)
        gl.glActiveTexture(gl.GL_TEXTURE3); gl.glBindTexture(gl.GL_TEXTURE_2D, self.snow_tex);  gl.glUniform1i(self.uniforms['texSnow'],  3)
        gl.glUniform4f(self.uniforms['biomeWeights'], *self.biome.blend_weights)
        gl.glUniform1f(self.uniforms['terrainHeightScale'], self.biome.terrain_height_scale)
        
        # Force textures off if flat_mode is enabled or textures aren't loaded
        textures_loaded = (self.grass_tex != 0 and self.rock_tex != 0
                           and self.sand_tex != 0 and self.snow_tex != 0)
        use_tex = 0 if self.flat_mode or not textures_loaded else (1 if getattr(self, 'use_textures', True) else 0)
        gl.glUniform1i(self.uniforms['use_textures'], use_tex)
        
        gl.glUniform1i(self.uniforms['active_lights'], active_lights_count)
        shadow_index_map = shadow_index_map or {}
        for i in range(active_lights_count):
            light = lights[i]
            base = f'lights[{i}]'
            gl.glUniform3fv(self.uniforms[f'{base}.position'], 1, light.pos)
            gl.glUniform3fv(self.uniforms[f'{base}.color'],    1, light.get_color())
            gl.glUniform1f(self.uniforms[f'{base}.intensity'],    light.get_intensity())
            gl.glUniform1f(self.uniforms[f'{base}.radius'],       light.get_radius())
            sidx_loc = self.uniforms.get(f'{base}.shadowIndex', -1)
            if sidx_loc is not None and sidx_loc != -1:
                gl.glUniform1i(sidx_loc, shadow_index_map.get(id(light), -1))

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
            loc = self.uniforms.get(f'shadowMaps[{i}]', -1)
            if loc is None or loc == -1:
                continue
            cm = shadow_cubemaps[i] if (i < len(shadow_cubemaps) and shadow_cubemaps[i]) else placeholder
            gl.glActiveTexture(gl.GL_TEXTURE0 + shadow_unit_base + i)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
            gl.glUniform1i(loc, shadow_unit_base + i)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        
        lod_level_loc = self.uniforms.get('lod_level', -1)

        if self.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        chunks_to_update = []
        for key, chunk in self.chunks.items():
            if not self._is_chunk_visible(chunk, frustum_planes):
                self.culled_chunks += 1
                continue
            dx = chunk.center[0] - camera_pos.x
            dz = chunk.center[2] - camera_pos.z
            dist_sq = dx * dx + dz * dz
            target_resolution = self._get_lod_resolution(dist_sq)
            current_resolution = self.LOD_RESOLUTIONS[chunk.lod_level] if chunk.is_uploaded else 0
            needs_update = chunk.is_dirty or not chunk.is_uploaded
            if not needs_update and current_resolution != target_resolution:
                if chunk.target_lod != target_resolution:
                    chunk.target_lod = target_resolution
                    chunk.lod_stable_frames = 0
                else:
                    chunk.lod_stable_frames += 1
                    if chunk.lod_stable_frames >= self.LOD_HYSTERESIS_FRAMES:
                        needs_update = True
                        chunk.lod_stable_frames = 0
            if needs_update:
                chunks_to_update.append((key, target_resolution, dist_sq))
            if chunk.vao and chunk.vertex_count > 0:
                # Upload the chunk's current LOD level so the fragment shader
                # can choose the appropriate shading path.
                if lod_level_loc != -1:
                    gl.glUniform1i(lod_level_loc, chunk.lod_level)
                gl.glBindVertexArray(chunk.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, chunk.vertex_count)
                self.visible_chunks += 1
                self.total_triangles += chunk.vertex_count // 3
        gl.glBindVertexArray(0)
        if self.wireframe: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        chunks_to_update.sort(key=lambda x: (not self.chunks[x[0]].is_dirty, x[2]))
        for i, (key, resolution, _) in enumerate(chunks_to_update[:self.MAX_UPDATES_PER_FRAME]):
            chunk = self.chunks[key]
            self._upload_chunk(chunk, resolution)
    
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

    def _sculpt_key(self, world_x: float, world_z: float) -> Tuple[int, int]:
        """Quantize world position to sculpt grid key."""
        gx = int(math.floor(world_x / self.sculpt_grid_resolution))
        gz = int(math.floor(world_z / self.sculpt_grid_resolution))
        return (gx, gz)

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
        self.mark_all_dirty()

    def _mark_sculpt_region_dirty(self, world_x: float, world_z: float, radius: float):
        """Mark chunks overlapping a sculpted region as dirty."""
        for key, chunk in self.chunks.items():
            cx = chunk.world_x + chunk.size / 2
            cz = chunk.world_z + chunk.size / 2
            half = chunk.size / 2 + radius
            if abs(cx - world_x) < half and abs(cz - world_z) < half:
                chunk.is_dirty = True
                if chunk.height_cache:
                    chunk.height_cache.invalidate()

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
        for key in list(self.chunks.keys()):
            self._delete_chunk(key)
        self.chunks.clear()
    
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
        self.chunk_size = data.get('chunk_size', 256.0)
        self.base_resolution = data.get('base_resolution', 48)
        self.offset_x = data.get('offset_x', 0.0)
        self.offset_z = data.get('offset_z', 0.0)
        self.offset_y = data.get('offset_y', 0.0)
        self.min_chunk_x = data.get('min_chunk_x', -2)
        self.max_chunk_x = data.get('max_chunk_x', 2)
        self.min_chunk_z = data.get('min_chunk_z', -2)
        self.max_chunk_z = data.get('max_chunk_z', 2)
        self.use_textures = data.get('use_textures', True)
        self.flat_mode = data.get('flat_mode', False)
        if 'custom_biome' in data: self.biome = BiomeConfig.from_dict(data['custom_biome'])
        # Sculpt offsets
        self.sculpt_offsets = {}
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
