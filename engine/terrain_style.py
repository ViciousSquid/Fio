"""Terrain appearance: height texture layers, terracing and stylised looks.

Everything here is plain Python/NumPy so it can be tested without a GL
context. ``Terrain`` owns one :class:`TerrainAppearance`; the renderer turns
it into shader uniforms with :func:`shader_uniforms` and the mesh builder uses
:func:`terrace_heights` / :func:`build_block_mesh` for the stepped shapes.

The options are independent - a preset is only a convenient starting set of
values, and any option can be changed on its own afterwards.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

Color = Tuple[float, float, float]

# ---------------------------------------------------------------------------
# Option vocabularies
# ---------------------------------------------------------------------------

TERRACE_MODES = ('none', 'smooth', 'sharp', 'blocks')
TERRACE_MODE_LABELS = {
    'none': 'Off',
    'smooth': 'Smooth Terraces',
    'sharp': 'Sharp Terraces',
    'blocks': 'Blocks',
}

COLOR_MODES = ('natural', 'palette', 'bands')
COLOR_MODE_LABELS = {
    'natural': 'Natural (biome / textures)',
    'palette': 'Palette by Height',
    'bands': 'Strata Bands',
}

MAX_PALETTE = 8

PALETTES: Dict[str, List[Color]] = {
    'forest_tiles': [
        (0.20, 0.42, 0.20), (0.26, 0.50, 0.22), (0.33, 0.57, 0.24),
        (0.40, 0.62, 0.27),
    ],
    'meadow': [
        (0.30, 0.48, 0.20), (0.40, 0.58, 0.26), (0.52, 0.64, 0.32),
        (0.66, 0.70, 0.42),
    ],
    'dusty_tiles': [
        (0.66, 0.50, 0.28), (0.76, 0.60, 0.35), (0.84, 0.69, 0.42),
        (0.90, 0.77, 0.50), (0.95, 0.84, 0.58),
    ],
    # Strata palettes run from the lowest ground to the highest, in
    # neighbouring shades, as layered terrain does.
    'highland_strata': [
        (0.86, 0.80, 0.56), (0.74, 0.75, 0.38), (0.56, 0.65, 0.32),
        (0.43, 0.54, 0.28), (0.33, 0.45, 0.25), (0.44, 0.37, 0.28),
        (0.52, 0.48, 0.45), (0.70, 0.68, 0.65),
    ],
    'island_strata': [
        (0.93, 0.87, 0.66), (0.88, 0.84, 0.56), (0.80, 0.80, 0.38),
        (0.67, 0.73, 0.35), (0.54, 0.63, 0.36), (0.42, 0.53, 0.34),
    ],
    'canyon_strata': [
        (0.94, 0.85, 0.64), (0.91, 0.72, 0.47), (0.87, 0.60, 0.35),
        (0.81, 0.50, 0.28), (0.73, 0.44, 0.31), (0.63, 0.38, 0.27),
    ],
    'alpine': [
        (0.22, 0.38, 0.20), (0.36, 0.46, 0.28), (0.48, 0.46, 0.40),
        (0.62, 0.60, 0.56), (0.92, 0.93, 0.95),
    ],
    'autumn': [
        (0.42, 0.40, 0.16), (0.62, 0.44, 0.14), (0.74, 0.34, 0.12),
        (0.56, 0.22, 0.12), (0.80, 0.60, 0.24),
    ],
    'mono': [
        (0.35, 0.35, 0.37), (0.50, 0.50, 0.52), (0.65, 0.65, 0.67),
        (0.80, 0.80, 0.82),
    ],
}

PALETTE_LABELS = {
    'forest_tiles': 'Forest Tiles',
    'meadow': 'Meadow',
    'dusty_tiles': 'Dusty Tiles',
    'highland_strata': 'Highland Strata',
    'island_strata': 'Island Strata',
    'canyon_strata': 'Canyon Strata',
    'alpine': 'Alpine',
    'autumn': 'Autumn',
    'mono': 'Monochrome',
    'custom': 'Custom',
}

MIN_PALETTE = 2

# sand -> grass, grass -> rock, rock -> snow, as fractions of the terrain's
# height range.
DEFAULT_LAYER_HEIGHTS: Tuple[float, float, float] = (0.08, 0.55, 0.85)

# Per-biome starting points for the texture layers. Biomes that should never
# show a layer push its band past the end of the range.
BIOME_LAYER_HEIGHTS: Dict[str, Tuple[float, float, float]] = {
    'grassy_hills': (0.06, 0.70, 1.0),
    'low_poly_valley': (0.05, 0.62, 0.92),
    'dark_cliffs': (0.0, 0.25, 0.9),
    'desert_canyon': (0.72, 0.72, 1.0),
    'jagged_peaks': (0.02, 0.22, 0.72),
    'desert': (0.80, 0.80, 1.0),
    'mountains': (0.0, 0.10, 0.65),
    'gentle_meadow': (0.04, 0.85, 1.0),
    'rolling_highlands': (0.05, 0.60, 0.92),
    'rocky_mountains': (0.0, 0.22, 0.75),
    'desert_mesas': (0.60, 0.60, 1.0),
    'alpine_forest': (0.0, 0.45, 0.80),
    'coastal_cliffs': (0.10, 0.40, 1.0),
}


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _color(v, default: Color) -> Color:
    try:
        values = list(v)
        if len(values) < 3:
            return default
        return tuple(_clamp01(c) for c in values[:3])  # type: ignore[return-value]
    except (TypeError, ValueError):
        return default


@dataclass
class TerrainAppearance:
    """Every look option for the terrain. Serialised with the map."""

    preset: str = 'natural'

    # -- Shape --------------------------------------------------------------
    terrace_mode: str = 'none'
    terrace_step: float = 12.0      # terrain-space height of one step
    terrace_ramp: float = 0.35      # share of each step spent on the riser
    block_size: float = 16.0        # terrain-space footprint of a block
    skirt: bool = True              # blocks: solid sides at the terrain edge

    # -- Colour -------------------------------------------------------------
    color_mode: str = 'natural'
    palette: str = 'meadow'
    #: The colours of the 'custom' palette, in order (2..MAX_PALETTE). Used
    #: for the strata bands (repeating) and the height palette (low to high).
    custom_palette: Optional[List[Color]] = None
    band_height: float = 12.0       # terrain-space height of a colour band

    # -- Texture layers (natural colour mode with textures on) ----------------
    layer_heights: Optional[Tuple[float, float, float]] = None
    layer_blend: float = 0.05
    slope_rock: float = 0.8

    # -- Surface detail -----------------------------------------------------
    contour_lines: float = 0.0      # darkness 0..1
    contour_width: float = 1.5      # pixels
    grid_lines: float = 0.0         # darkness 0..1
    grid_size: float = 16.0         # terrain-space tile size
    cell_variation: float = 0.0     # per-tile brightness jitter 0..1
    wall_color: Color = (0.78, 0.45, 0.16)
    wall_amount: float = 0.0        # how strongly cliffs take the wall colour
    wall_stripes: float = 0.0
    speckle_color: Color = (0.12, 0.30, 0.10)
    speckle_amount: float = 0.0
    patch_color: Color = (0.50, 0.55, 0.30)
    patch_amount: float = 0.0

    # -- Shading ------------------------------------------------------------
    smooth_shading: bool = False
    light_steps: int = 0            # 0 = continuous lighting
    dither_levels: int = 0          # 0 = off, else colour levels per channel

    def copy(self) -> 'TerrainAppearance':
        return TerrainAppearance.from_dict(self.to_dict())

    def to_dict(self) -> dict:
        data = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, list):
                value = [list(v) if isinstance(v, tuple) else v for v in value]
            data[f.name] = value
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> 'TerrainAppearance':
        inst = cls()
        if not isinstance(data, dict):
            return inst
        for f in fields(inst):
            if f.name in data:
                setattr(inst, f.name, data[f.name])
        inst.sanitize()
        return inst

    def sanitize(self) -> 'TerrainAppearance':
        """Clamp every option into range so bad map data cannot break a draw."""
        d = TerrainAppearance.__dataclass_fields__
        if self.terrace_mode not in TERRACE_MODES:
            self.terrace_mode = 'none'
        if self.color_mode not in COLOR_MODES:
            self.color_mode = 'natural'
        self.custom_palette = _palette_list(self.custom_palette)
        if self.palette == 'custom' and self.custom_palette is None:
            self.palette = 'meadow'
        if self.palette not in PALETTES and self.palette != 'custom':
            self.palette = 'meadow'
        if not isinstance(self.preset, str):
            self.preset = 'custom'
        self.terrace_step = float(np.clip(_num(self.terrace_step, 12.0), 1.0, 200.0))
        self.terrace_ramp = float(np.clip(_num(self.terrace_ramp, 0.35), 0.05, 1.0))
        self.block_size = float(np.clip(_num(self.block_size, 16.0), 2.0, 128.0))
        self.band_height = float(np.clip(_num(self.band_height, 12.0), 1.0, 200.0))
        self.skirt = bool(self.skirt)
        if self.layer_heights is not None:
            try:
                lh = sorted(_clamp01(v) for v in list(self.layer_heights)[:3])
                self.layer_heights = (lh[0], lh[1], lh[2]) if len(lh) == 3 else None
            except (TypeError, ValueError):
                self.layer_heights = None
        self.layer_blend = float(np.clip(_num(self.layer_blend, 0.05), 0.005, 0.3))
        self.slope_rock = _clamp01(_num(self.slope_rock, 0.8))
        self.contour_lines = _clamp01(_num(self.contour_lines, 0.0))
        self.contour_width = float(np.clip(_num(self.contour_width, 1.5), 0.5, 6.0))
        self.grid_lines = _clamp01(_num(self.grid_lines, 0.0))
        self.grid_size = float(np.clip(_num(self.grid_size, 16.0), 1.0, 256.0))
        self.cell_variation = _clamp01(_num(self.cell_variation, 0.0))
        self.wall_color = _color(self.wall_color, d['wall_color'].default)
        self.wall_amount = _clamp01(_num(self.wall_amount, 0.0))
        self.wall_stripes = _clamp01(_num(self.wall_stripes, 0.0))
        self.speckle_color = _color(self.speckle_color, d['speckle_color'].default)
        self.speckle_amount = _clamp01(_num(self.speckle_amount, 0.0))
        self.patch_color = _color(self.patch_color, d['patch_color'].default)
        self.patch_amount = _clamp01(_num(self.patch_amount, 0.0))
        self.smooth_shading = bool(self.smooth_shading)
        self.light_steps = int(np.clip(_num(self.light_steps, 0), 0, 8))
        self.dither_levels = int(np.clip(_num(self.dither_levels, 0), 0, 32))
        return self

    def shape_key(self) -> tuple:
        """Options that change the terrain's geometry (and so its collision)."""
        if self.terrace_mode == 'none':
            return ('none',)
        if self.terrace_mode == 'blocks':
            return ('blocks', self.terrace_step, self.block_size, self.skirt)
        return (self.terrace_mode, self.terrace_step, self.terrace_ramp)


def _palette_list(value) -> Optional[List[Color]]:
    """A clean custom palette (MIN..MAX colours), or None if unusable."""
    if value is None:
        return None
    try:
        colors = [_color(c, None) for c in list(value)[:MAX_PALETTE]]
    except TypeError:
        return None
    colors = [c for c in colors if c is not None]
    return colors if len(colors) >= MIN_PALETTE else None


def palette_colors(appearance: 'TerrainAppearance') -> List[Color]:
    """The colours the appearance's palette currently stands for."""
    if appearance.palette == 'custom' and appearance.custom_palette:
        return list(appearance.custom_palette)
    return list(PALETTES.get(appearance.palette) or PALETTES['meadow'])


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESET_LABELS = {
    'natural': 'Natural',
    'voxel_blocks': 'Voxel Blocks',
    'retro_tiles': 'Retro Tiles',
    'painted_strata': 'Painted Strata',
    'island_terraces': 'Island Terraces',
    'canyon_strata': 'Canyon Strata',
}

PRESETS: Dict[str, dict] = {
    # The engine's original look.
    'natural': {},
    # Square columns of land with bright tops and earthen side walls, like a
    # tabletop diorama.
    'voxel_blocks': dict(
        terrace_mode='blocks', terrace_step=8.0, block_size=16.0, skirt=True,
        color_mode='palette', palette='forest_tiles', band_height=8.0,
        grid_lines=0.0, grid_size=16.0, cell_variation=0.35,
        wall_color=(0.86, 0.48, 0.14), wall_amount=1.0, wall_stripes=0.15,
        smooth_shading=False, light_steps=0,
    ),
    # Stepped, tiled hillsides in dusty earth tones with a fine tile grid,
    # banded cliffs, scattered shrubs and a reduced, dithered colour depth.
    'retro_tiles': dict(
        terrace_mode='sharp', terrace_step=10.0, terrace_ramp=0.45,
        color_mode='palette', palette='dusty_tiles', band_height=10.0,
        grid_lines=0.18, grid_size=16.0, cell_variation=0.12,
        wall_color=(0.40, 0.26, 0.13), wall_amount=0.85, wall_stripes=0.6,
        speckle_color=(0.10, 0.34, 0.12), speckle_amount=0.45,
        smooth_shading=False, light_steps=3, dither_levels=12,
    ),
    # Softly terraced land painted in layered beds that climb from sandy
    # shore through grass and olive to earth and grey rock, each terrace
    # edged with a dark contour line, with darker scrub on the flats.
    'painted_strata': dict(
        terrace_mode='smooth', terrace_step=14.0, terrace_ramp=0.6,
        color_mode='bands', palette='highland_strata', band_height=14.0,
        contour_lines=0.55, contour_width=1.4,
        wall_color=(0.36, 0.30, 0.24), wall_amount=0.2,
        patch_color=(0.36, 0.44, 0.24), patch_amount=0.35,
        speckle_color=(0.28, 0.36, 0.20), speckle_amount=0.3,
        smooth_shading=True, light_steps=0,
    ),
    # Broad, gently stepped terraces of sand and fresh grass, soft outlines.
    'island_terraces': dict(
        terrace_mode='smooth', terrace_step=10.0, terrace_ramp=0.35,
        color_mode='bands', palette='island_strata', band_height=10.0,
        contour_lines=0.3, contour_width=1.2,
        patch_color=(0.46, 0.58, 0.32), patch_amount=0.25,
        smooth_shading=True, light_steps=0,
    ),
    # Sandstone canyon: cream, ochre and rust beds with crisp dark lines and
    # patches of olive scrub.
    'canyon_strata': dict(
        terrace_mode='smooth', terrace_step=12.0, terrace_ramp=0.55,
        color_mode='bands', palette='canyon_strata', band_height=12.0,
        contour_lines=0.7, contour_width=1.5,
        wall_color=(0.55, 0.33, 0.24), wall_amount=0.25,
        patch_color=(0.52, 0.56, 0.34), patch_amount=0.3,
        speckle_color=(0.38, 0.42, 0.26), speckle_amount=0.3,
        smooth_shading=True, light_steps=0,
    ),
}


def apply_preset(appearance: TerrainAppearance, name: str) -> TerrainAppearance:
    """Reset every look option to ``name``'s values. Texture layers are kept."""
    if name not in PRESETS:
        raise KeyError(name)
    keep = {k: getattr(appearance, k)
            for k in ('layer_heights', 'layer_blend', 'slope_rock')}
    fresh = TerrainAppearance()
    for key, value in PRESETS[name].items():
        setattr(fresh, key, value)
    for key, value in keep.items():
        setattr(fresh, key, value)
    fresh.preset = name
    fresh.sanitize()
    for f in fields(fresh):
        setattr(appearance, f.name, getattr(fresh, f.name))
    return appearance


# ---------------------------------------------------------------------------
# Height texture layers
# ---------------------------------------------------------------------------

def layer_heights_for_biome(biome_key: Optional[str]) -> Tuple[float, float, float]:
    return BIOME_LAYER_HEIGHTS.get(biome_key or '', DEFAULT_LAYER_HEIGHTS)


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / np.maximum(e1 - e0, 1e-6), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def effective_layer_heights(layer_heights: Sequence[float]) -> Tuple[float, float, float]:
    """Sorted boundaries, with 0 and 1 pushed right off the range.

    A boundary at the very top (or bottom) means "this layer never starts"
    (or "the layer below never shows"). Left at exactly 1.0 its blend would
    still leak half a layer onto the highest peak.
    """
    out = []
    for v in sorted(float(x) for x in layer_heights):
        if v >= 0.999:
            v = 2.0
        elif v <= 0.001:
            v = -1.0
        out.append(v)
    return out[0], out[1], out[2]


def layer_weights(h, layer_heights: Sequence[float], blend: float) -> np.ndarray:
    """Weights of (sand, grass, rock, snow) at normalised height ``h``.

    The same partition-of-unity the terrain shader uses: each boundary is a
    smoothstep of width ``2 * blend`` and the weights always sum to one.
    """
    h = np.asarray(h, dtype=np.float64)
    b0, b1, b2 = effective_layer_heights(layer_heights)
    s0 = _smoothstep(b0 - blend, b0 + blend, h)
    s1 = _smoothstep(b1 - blend, b1 + blend, h)
    s2 = _smoothstep(b2 - blend, b2 + blend, h)
    return np.stack([1.0 - s0, s0 - s1, s1 - s2, s2], axis=-1)


def grass_layer_weight(h, layer_heights: Sequence[float], blend: float) -> np.ndarray:
    return layer_weights(h, layer_heights, blend)[..., 1]


# ---------------------------------------------------------------------------
# Terracing
# ---------------------------------------------------------------------------

def terrace_heights(h, step: float, ramp: float, mode: str):
    """Reshape heights into terraces. Continuous for 'smooth' and 'sharp'.

    Each step of height ``step`` is flat for ``1 - ramp`` of its span and
    climbs to the next level over the remaining ``ramp``: eased for 'smooth',
    a straight riser for 'sharp'. 'blocks' floors to whole steps (use it on
    heights sampled at block centres). Anything else returns ``h`` unchanged.
    """
    if mode not in ('smooth', 'sharp', 'blocks') or step <= 0.0:
        return h
    scalar = np.isscalar(h)
    arr = np.asarray(h, dtype=np.float64)
    q = arr / step
    k = np.floor(q)
    if mode == 'blocks':
        out = k * step
    else:
        f = q - k
        ramp = float(np.clip(ramp, 1e-3, 1.0))
        t = np.clip((f - (1.0 - ramp)) / ramp, 0.0, 1.0)
        if mode == 'smooth':
            t = t * t * (3.0 - 2.0 * t)
        out = (k + t) * step
    if scalar:
        return float(out)
    return out.astype(np.asarray(h).dtype if np.asarray(h).dtype.kind == 'f' else np.float32)


def block_cell_centres(x, z, cell: float, origin_x: float, origin_z: float):
    """Centre of the block that contains each point."""
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    cx = origin_x + (np.floor((x - origin_x) / cell) + 0.5) * cell
    cz = origin_z + (np.floor((z - origin_z) / cell) + 0.5) * cell
    return cx, cz


def build_block_mesh(world_x: float, world_z: float, size: float, cell: float,
                     origin_x: float, origin_z: float,
                     heights_fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
                     colors_fn: Callable[[np.ndarray], np.ndarray],
                     bounds: Tuple[float, float, float, float],
                     floor_height: Optional[float],
                     tiling: float, max_cells: int = 128):
    """Vertex data (N, 14) for a chunk of square land columns.

    ``heights_fn`` gives the (already stepped) height of a block from its
    centre. A block belongs to the chunk that holds its centre, so blocks on
    a chunk seam are built exactly once. A wall is emitted on every side
    where the neighbouring column is lower; beyond the terrain ``bounds``
    the neighbour is ``floor_height`` (a solid skirt), or no wall at all when
    that is ``None``.

    Layout per vertex: position(3) normal(3) colour(3) uv(2) smooth normal(3).
    Returns ``(vertices, min_y, max_y)``.
    """
    cells_across = size / cell
    if cells_across > max_cells:
        cell = size / max_cells
    k0x = int(np.ceil((world_x - origin_x) / cell - 0.5))
    k1x = int(np.ceil((world_x + size - origin_x) / cell - 0.5))
    k0z = int(np.ceil((world_z - origin_z) / cell - 0.5))
    k1z = int(np.ceil((world_z + size - origin_z) / cell - 0.5))
    nx, nz = k1x - k0x, k1z - k0z
    if nx <= 0 or nz <= 0:
        return np.zeros((0, 14), dtype=np.float32), 0.0, 0.0

    # Heights on a grid padded by one block for the neighbour tests.
    kx = np.arange(k0x - 1, k1x + 1)
    kz = np.arange(k0z - 1, k1z + 1)
    kxg, kzg = np.meshgrid(kx, kz, indexing='ij')
    cx = origin_x + (kxg + 0.5) * cell
    cz = origin_z + (kzg + 0.5) * cell
    heights = np.asarray(heights_fn(cx.ravel().astype(np.float32),
                                    cz.ravel().astype(np.float32)),
                         dtype=np.float64).reshape(cx.shape)
    min_x, max_x, min_z, max_z = bounds
    outside = (cx < min_x) | (cx > max_x) | (cz < min_z) | (cz > max_z)
    if floor_height is None:
        # No skirt: an outside neighbour never makes a wall.
        heights = np.where(outside, np.inf, heights)
    else:
        heights = np.where(outside, min(floor_height, np.nanmin(heights)), heights)

    inner = (slice(1, -1), slice(1, -1))
    h = heights[inner]
    x0 = origin_x + kxg[inner] * cell
    z0 = origin_z + kzg[inner] * cell
    x1 = x0 + cell
    z1 = z0 + cell
    inside = ~outside[inner]

    parts = []

    # Tops.
    hx0, hz0, hx1, hz1, hy = (a[inside] for a in (x0, z0, x1, z1, h))
    if hy.size:
        cols = colors_fn(hy.astype(np.float32))
        up = np.array([0.0, 1.0, 0.0])
        corners = [(hx0, hz0), (hx1, hz0), (hx0, hz1),
                   (hx1, hz0), (hx1, hz1), (hx0, hz1)]
        parts.append(_quad_vertices(
            [np.stack([a, hy, b], -1) for a, b in corners], up, cols,
            [np.stack([a / tiling, b / tiling], -1) for a, b in corners]))

    # Walls. (neighbour slice, outward normal, wall edge as two xz points)
    sides = [
        ((slice(2, None), slice(1, -1)), (1.0, 0.0, 0.0), (x1, z0), (x1, z1)),
        ((slice(0, -2), slice(1, -1)), (-1.0, 0.0, 0.0), (x0, z1), (x0, z0)),
        ((slice(1, -1), slice(2, None)), (0.0, 0.0, 1.0), (x1, z1), (x0, z1)),
        ((slice(1, -1), slice(0, -2)), (0.0, 0.0, -1.0), (x0, z0), (x1, z0)),
    ]
    for nb_slice, normal, (ax, az), (bx, bz) in sides:
        nb = heights[nb_slice]
        mask = inside & np.isfinite(nb) & (nb < h - 1e-6)
        if not np.any(mask):
            continue
        top = h[mask]
        bottom = nb[mask]
        pax, paz, pbx, pbz = ax[mask], az[mask], bx[mask], bz[mask]
        cols = colors_fn(top.astype(np.float32))
        along_a = (pax + paz) / tiling
        along_b = (pbx + pbz) / tiling
        corners = [(pax, bottom, paz, along_a), (pbx, bottom, pbz, along_b),
                   (pax, top, paz, along_a), (pbx, bottom, pbz, along_b),
                   (pbx, top, pbz, along_b), (pax, top, paz, along_a)]
        parts.append(_quad_vertices(
            [np.stack([cx_, cy_, cz_], -1) for cx_, cy_, cz_, _ in corners],
            np.array(normal), cols,
            [np.stack([u, cy_ / tiling], -1) for _, cy_, _, u in corners]))

    if not parts:
        return np.zeros((0, 14), dtype=np.float32), 0.0, 0.0
    verts = np.concatenate(parts, axis=0)
    ys = verts[:, 1]
    return verts, float(ys.min()), float(ys.max())


def _quad_vertices(positions, normal, colors, uvs) -> np.ndarray:
    """Interleave six-vertex quads: positions/uvs are lists of (n, k) arrays."""
    n = positions[0].shape[0]
    out = np.zeros((n, 6, 14), dtype=np.float32)
    for i in range(6):
        out[:, i, 0:3] = positions[i]
        out[:, i, 3:6] = normal
        out[:, i, 6:9] = colors
        out[:, i, 9:11] = uvs[i]
        out[:, i, 11:14] = normal
    return out.reshape(-1, 14)


# ---------------------------------------------------------------------------
# Shader uniforms
# ---------------------------------------------------------------------------

COLOR_MODE_IDS = {'natural': 0, 'palette': 1, 'bands': 2}

#: Every terrain.frag uniform that this module drives.
UNIFORM_NAMES = (
    'uHeightRange', 'uLayerHeights', 'uLayerBlend', 'uSlopeRock',
    'uColorMode', 'uPalette', 'uPaletteSize', 'uBandHeight',
    'uContour', 'uContourWidth', 'uGrid', 'uGridSize', 'uCellVariation',
    'uWallColor', 'uWall', 'uWallStripes',
    'uSpeckleColor', 'uSpeckle', 'uPatchColor', 'uPatch',
    'uSmoothShading', 'uLightSteps', 'uDither', 'uGridOrigin',
)


def palette_array(palette) -> Tuple[np.ndarray, int]:
    """``(8x3 array, count)`` for a palette name or a TerrainAppearance."""
    if isinstance(palette, TerrainAppearance):
        colors = palette_colors(palette)
    else:
        colors = PALETTES.get(palette) or PALETTES['meadow']
    colors = colors[:MAX_PALETTE]
    arr = np.zeros((MAX_PALETTE, 3), dtype=np.float32)
    arr[:len(colors)] = colors
    return arr, len(colors)


def shader_uniforms(appearance: TerrainAppearance,
                    height_range: Tuple[float, float],
                    layer_heights: Sequence[float],
                    mesh_scale: float,
                    grid_origin: Tuple[float, float] = (0.0, 0.0)) -> Dict[str, object]:
    """Uniform values for terrain.frag, keyed by uniform name.

    Values are ``int`` (glUniform1i), ``float`` (glUniform1f), a 2/3-tuple
    (glUniform2f/3f) or, for ``uPalette``, an ``(8, 3)`` float32 array.
    Lengths given in terrain-space are scaled into world units here.
    """
    a = appearance
    scale = max(1e-6, float(mesh_scale))
    lo, hi = float(height_range[0]), float(height_range[1])
    if hi - lo < 1e-3:
        hi = lo + 1.0
    palette, palette_size = palette_array(a)
    lh = effective_layer_heights(layer_heights)
    return {
        'uHeightRange': (lo, hi),
        'uLayerHeights': (lh[0], lh[1], lh[2]),
        'uLayerBlend': float(a.layer_blend),
        'uSlopeRock': float(a.slope_rock),
        'uColorMode': COLOR_MODE_IDS.get(a.color_mode, 0),
        'uPalette': palette,
        'uPaletteSize': int(palette_size),
        'uBandHeight': float(a.band_height * scale),
        'uContour': float(a.contour_lines),
        'uContourWidth': float(a.contour_width),
        'uGrid': float(a.grid_lines),
        'uGridSize': float(a.grid_size * scale),
        'uCellVariation': float(a.cell_variation),
        'uWallColor': tuple(float(c) for c in a.wall_color),
        'uWall': float(a.wall_amount),
        'uWallStripes': float(a.wall_stripes),
        'uSpeckleColor': tuple(float(c) for c in a.speckle_color),
        'uSpeckle': float(a.speckle_amount),
        'uPatchColor': tuple(float(c) for c in a.patch_color),
        'uPatch': float(a.patch_amount),
        'uSmoothShading': 1 if a.smooth_shading else 0,
        'uLightSteps': int(a.light_steps),
        'uDither': int(a.dither_levels),
        'uGridOrigin': (float(grid_origin[0]), float(grid_origin[1])),
    }


# ---------------------------------------------------------------------------
# Ground colour (what the terrain shader paints at a point) - for grass
# ---------------------------------------------------------------------------

def palette_at(colors: Sequence[Color], t) -> np.ndarray:
    """Palette colours interpolated at fractions ``t`` (the shader's paletteAt)."""
    colors = np.asarray(colors, dtype=np.float64)
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    if len(colors) == 1:
        return np.repeat(colors, len(t), axis=0)
    x = t * (len(colors) - 1)
    i = np.minimum(np.floor(x).astype(int), len(colors) - 1)
    j = np.minimum(i + 1, len(colors) - 1)
    f = (x - i)[:, None]
    return colors[i] * (1.0 - f) + colors[j] * f


def palette_ground_colors(appearance: 'TerrainAppearance', y,
                          height_range: Tuple[float, float],
                          mesh_scale: float) -> np.ndarray:
    """Base colour of the 'palette' and 'bands' colour modes at heights *y*.

    Mirrors terrain.frag: the palette is sampled at the height of the band a
    point belongs to, and strata bands alternate slightly darker.
    """
    y = np.asarray(y, dtype=np.float64)
    band_h = max(appearance.band_height * max(mesh_scale, 1e-6), 1e-3)
    lo, hi = float(height_range[0]), float(height_range[1])
    band = np.floor(y / band_h + 0.12)
    t = (band * band_h - lo) / max(hi - lo, 1e-3)
    out = palette_at(palette_colors(appearance), t)
    if appearance.color_mode == 'bands':
        out *= np.where(np.mod(band, 2.0) < 0.5, 1.0, 0.93)[:, None]
    return out


_TEXTURE_AVERAGES: Dict[str, Optional[np.ndarray]] = {}
TERRAIN_TEXTURE_FILES = ('sand.jpg', 'grass.jpg', 'rock.jpg', 'snow.jpg')


def texture_average(filename: str) -> Optional[np.ndarray]:
    """Mean RGB (0..1) of a terrain texture file, cached; None if unreadable.

    The mean is what the texture looks like from any distance (it is its
    smallest mip level), read from the file so no GL readback is needed.
    """
    if filename in _TEXTURE_AVERAGES:
        return _TEXTURE_AVERAGES[filename]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = None
    for root in (here, os.getcwd()):
        path = os.path.join(root, 'assets', 'textures', 'terrain', filename)
        if os.path.isfile(path):
            try:
                from PIL import Image
                with Image.open(path) as img:
                    small = img.convert('RGB').resize((64, 64))
                    result = np.asarray(small, dtype=np.float64).reshape(-1, 3).mean(0) / 255.0
            except Exception:
                result = None
            break
    _TEXTURE_AVERAGES[filename] = result
    return result


def terrain_texture_averages() -> Optional[np.ndarray]:
    """(4, 3) mean colours of sand, grass, rock and snow, or None."""
    rows = [texture_average(name) for name in TERRAIN_TEXTURE_FILES]
    if any(r is None for r in rows):
        return None
    return np.stack(rows)


def textured_ground_colors(h, slope, layer_heights: Sequence[float], blend: float,
                           slope_rock: float, averages: np.ndarray) -> np.ndarray:
    """Colour of the height-layered textures at normalised heights *h*.

    The layer weights of terrain.frag (without its edge noise), rock on steep
    ground, applied to each texture's mean colour, times the shader's 1.1.
    """
    w = layer_weights(h, layer_heights, blend)
    steep = _smoothstep(0.25, 0.5, np.asarray(slope, dtype=np.float64)) * slope_rock
    w = w * (1.0 - steep)[:, None]
    w[:, 2] += steep
    return (w @ averages) * 1.1
