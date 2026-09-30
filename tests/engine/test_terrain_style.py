"""``engine.terrain_style``: terracing, texture height layers and look options.

Pure NumPy - no Qt, no OpenGL. What the terrain renders and what the player
collides with both come out of these functions, so their shape guarantees are
checked directly here; ``test_terrain_appearance.py`` covers the wiring into
``Terrain``.
"""

import numpy as np
import pytest

from engine import terrain_style as ts


# ---------------------------------------------------------------------------
# Terracing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["smooth", "sharp"])
def test_terraces_are_continuous_and_never_descend(mode):
    h = np.linspace(-50.0, 250.0, 20001)
    out = ts.terrace_heights(h, 12.0, 0.4, mode)
    assert np.all(np.diff(out) >= -1e-9)
    # No jump anywhere bigger than the input step could explain on a riser.
    assert np.max(np.abs(np.diff(out))) < 12.0 / (0.4 * 20001 / 300.0) * 1.6


@pytest.mark.parametrize("mode", ["smooth", "sharp"])
def test_terraces_have_flats_on_every_step(mode):
    step, ramp = 10.0, 0.3
    # The first 70% of every step is flat, at the step's own level.
    for k in range(-2, 5):
        h = np.linspace(k * step, k * step + step * (1.0 - ramp) - 1e-6, 50)
        np.testing.assert_allclose(ts.terrace_heights(h, step, ramp, mode), k * step)


def test_terraces_meet_the_input_on_every_level():
    levels = np.arange(-5, 6) * 7.0
    for mode in ("smooth", "sharp", "blocks"):
        np.testing.assert_allclose(ts.terrace_heights(levels, 7.0, 0.5, mode), levels)


def test_blocks_floor_to_whole_steps():
    h = np.array([0.0, 7.9, 8.0, 15.99, -0.1])
    np.testing.assert_allclose(ts.terrace_heights(h, 8.0, 0.5, 'blocks'),
                               [0.0, 0.0, 8.0, 8.0, -8.0])


def test_terracing_off_is_the_identity():
    h = np.array([1.0, 2.5, 3.7], dtype=np.float32)
    assert ts.terrace_heights(h, 5.0, 0.5, 'none') is h
    assert ts.terrace_heights(3.3, 5.0, 0.5, 'none') == 3.3


def test_scalar_and_batch_terracing_agree():
    for mode in ("smooth", "sharp", "blocks"):
        batch = ts.terrace_heights(np.array([13.7]), 6.0, 0.35, mode)[0]
        assert ts.terrace_heights(13.7, 6.0, 0.35, mode) == pytest.approx(batch)


def test_block_centres():
    cx, cz = ts.block_cell_centres(np.array([0.0, 15.9, 16.0, -0.1]),
                                   np.array([5.0, 5.0, 5.0, 5.0]), 16.0, 0.0, 0.0)
    np.testing.assert_allclose(cx, [8.0, 8.0, 24.0, -8.0])
    np.testing.assert_allclose(cz, 8.0)


# ---------------------------------------------------------------------------
# Texture height layers
# ---------------------------------------------------------------------------

def test_layer_weights_are_a_partition_of_unity():
    h = np.linspace(-0.2, 1.2, 1001)
    w = ts.layer_weights(h, (0.1, 0.5, 0.8), 0.05)
    assert w.shape == (1001, 4)
    np.testing.assert_allclose(w.sum(axis=-1), 1.0, atol=1e-9)
    assert np.all(w >= -1e-9)


def test_layers_run_sand_grass_rock_snow_with_height():
    w = ts.layer_weights(np.array([0.0, 0.3, 0.65, 0.95]), (0.1, 0.5, 0.8), 0.02)
    assert list(np.argmax(w, axis=-1)) == [0, 1, 2, 3]


def test_a_boundary_at_the_top_means_the_layer_never_appears():
    w = ts.layer_weights(np.array([1.0]), (0.1, 0.5, 1.0), 0.1)
    assert w[0, 3] == 0.0          # no snow even on the highest peak
    w = ts.layer_weights(np.array([0.0]), (0.0, 0.5, 0.8), 0.1)
    assert w[0, 0] == 0.0          # no sand even in the lowest hollow


def test_grass_weight_vanishes_at_high_elevation():
    heights = (0.05, 0.6, 0.9)
    assert ts.grass_layer_weight(0.3, heights, 0.05) == pytest.approx(1.0)
    assert ts.grass_layer_weight(0.8, heights, 0.05) == pytest.approx(0.0)
    assert ts.grass_layer_weight(0.0, heights, 0.02) == pytest.approx(0.0)


def test_every_biome_has_sorted_layer_heights():
    for key, lh in ts.BIOME_LAYER_HEIGHTS.items():
        assert list(lh) == sorted(lh), key
        assert all(0.0 <= v <= 1.0 for v in lh), key


# ---------------------------------------------------------------------------
# TerrainAppearance
# ---------------------------------------------------------------------------

def test_default_appearance_is_the_original_look():
    a = ts.TerrainAppearance()
    assert a.terrace_mode == 'none'
    assert a.color_mode == 'natural'
    for option in ('contour_lines', 'grid_lines', 'cell_variation', 'wall_amount',
                   'wall_stripes', 'speckle_amount', 'patch_amount'):
        assert getattr(a, option) == 0.0, option
    assert a.light_steps == 0 and a.dither_levels == 0


def test_appearance_round_trips():
    a = ts.TerrainAppearance()
    ts.apply_preset(a, 'painted_strata')
    a.layer_heights = (0.1, 0.4, 0.9)
    b = ts.TerrainAppearance.from_dict(a.to_dict())
    assert b == a


def test_bad_map_data_is_sanitised():
    a = ts.TerrainAppearance.from_dict({
        'terrace_mode': 'lava', 'color_mode': 7, 'palette': 'nope',
        'terrace_step': -4, 'contour_lines': 9, 'wall_color': 'red',
        'layer_heights': [0.9, 0.2, 0.5], 'light_steps': 'x', 'unknown': 1,
    })
    assert a.terrace_mode == 'none'
    assert a.color_mode == 'natural'
    assert a.palette in ts.PALETTES
    assert a.terrace_step >= 1.0
    assert a.contour_lines == 1.0
    assert a.wall_color == ts.TerrainAppearance().wall_color
    assert a.layer_heights == (0.2, 0.5, 0.9)
    assert a.light_steps == 0


def test_from_dict_of_nothing_is_the_default():
    assert ts.TerrainAppearance.from_dict(None) == ts.TerrainAppearance()


@pytest.mark.parametrize("name", sorted(ts.PRESETS))
def test_presets_apply_and_keep_texture_layers(name):
    a = ts.TerrainAppearance(layer_heights=(0.2, 0.3, 0.4), slope_rock=0.1)
    ts.apply_preset(a, name)
    assert a.preset == name
    assert a.layer_heights == (0.2, 0.3, 0.4)
    assert a.slope_rock == 0.1
    assert a == ts.TerrainAppearance.from_dict(a.to_dict())


def test_presets_are_distinct_looks():
    looks = []
    for name in ts.PRESETS:
        a = ts.apply_preset(ts.TerrainAppearance(), name)
        d = a.to_dict()
        d.pop('preset')
        looks.append(d)
    assert all(looks.count(x) == 1 for x in looks)


def test_shape_key_tracks_only_shape_options():
    a = ts.TerrainAppearance()
    base = a.shape_key()
    a.contour_lines = 1.0
    a.palette = 'autumn'
    assert a.shape_key() == base
    a.terrace_mode = 'sharp'
    assert a.shape_key() != base


def test_labels_cover_every_option():
    assert set(ts.PRESET_LABELS) == set(ts.PRESETS)
    assert set(ts.TERRACE_MODE_LABELS) == set(ts.TERRACE_MODES)
    assert set(ts.COLOR_MODE_LABELS) == set(ts.COLOR_MODES)
    assert set(ts.PALETTE_LABELS) == set(ts.PALETTES)
    assert all(len(p) <= ts.MAX_PALETTE for p in ts.PALETTES.values())


# ---------------------------------------------------------------------------
# Shader uniforms
# ---------------------------------------------------------------------------

def test_shader_uniforms_cover_every_declared_name():
    u = ts.shader_uniforms(ts.TerrainAppearance(), (0.0, 100.0), (0.1, 0.5, 0.8), 1.0)
    assert set(u) == set(ts.UNIFORM_NAMES)
    assert u['uPalette'].shape == (ts.MAX_PALETTE, 3)


def test_shader_uniforms_scale_lengths_into_world_units():
    a = ts.TerrainAppearance(band_height=10.0, grid_size=4.0)
    u = ts.shader_uniforms(a, (0.0, 100.0), (0.1, 0.5, 0.8), 2.5)
    assert u['uBandHeight'] == pytest.approx(25.0)
    assert u['uGridSize'] == pytest.approx(10.0)


def test_shader_uniforms_push_edge_layers_off_the_range():
    u = ts.shader_uniforms(ts.TerrainAppearance(), (0.0, 1.0), (0.0, 0.5, 1.0), 1.0)
    lo, mid, hi = u['uLayerHeights']
    assert lo < 0.0 and mid == 0.5 and hi > 1.0


def test_degenerate_height_range_is_widened():
    u = ts.shader_uniforms(ts.TerrainAppearance(), (5.0, 5.0), (0.1, 0.5, 0.8), 1.0)
    assert u['uHeightRange'][1] > u['uHeightRange'][0]


# ---------------------------------------------------------------------------
# Block mesh
# ---------------------------------------------------------------------------

def _block_mesh(heights_fn, world_x=0.0, world_z=0.0, size=64.0, cell=16.0,
                bounds=(-1e9, 1e9, -1e9, 1e9), floor=None):
    return ts.build_block_mesh(
        world_x, world_z, size, cell, 0.0, 0.0, heights_fn,
        lambda h: np.full((len(h), 3), 0.5, dtype=np.float32),
        bounds, floor, 20.0)


def _faces(verts):
    """Normals of each six-vertex quad."""
    return verts.reshape(-1, 6, 14)[:, 0, 3:6]


def test_flat_ground_is_tops_only():
    verts, lo, hi = _block_mesh(lambda x, z: np.full(len(x), 32.0))
    faces = _faces(verts)
    assert len(faces) == 16                      # 4 x 4 blocks
    np.testing.assert_allclose(faces, [[0.0, 1.0, 0.0]] * 16)
    assert lo == hi == 32.0


def test_a_raised_block_gets_four_walls():
    def heights(x, z):
        return np.where((np.abs(x - 24.0) < 1) & (np.abs(z - 24.0) < 1), 16.0, 0.0)
    verts, lo, hi = _block_mesh(heights)
    faces = _faces(verts)
    walls = faces[np.abs(faces[:, 1]) < 0.5]
    assert len(walls) == 4
    np.testing.assert_allclose(sorted(map(tuple, walls)), sorted([
        (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)]))
    wall_y = verts.reshape(-1, 6, 14)[np.abs(faces[:, 1]) < 0.5][:, :, 1]
    assert wall_y.min() == 0.0 and wall_y.max() == 16.0


def test_each_block_is_built_by_exactly_one_chunk():
    def heights(x, z):
        return (np.floor(x / 16.0) % 3) * 8.0 + (np.floor(z / 16.0) % 2) * 4.0

    def quads(v):
        return sorted(tuple(q) for q in v.reshape(-1, 6, 14)[:, :, 0:3].reshape(-1, 18).round(4))

    whole, _, _ = _block_mesh(heights, size=128.0)
    parts = []
    for wx in (0.0, 64.0):
        for wz in (0.0, 64.0):
            v, _, _ = _block_mesh(heights, world_x=wx, world_z=wz, size=64.0)
            parts.extend(quads(v))
    # The same tops and walls, none twice and none missing at the seams.
    assert sorted(parts) == quads(whole)


def test_skirt_closes_the_terrain_edge():
    flat = lambda x, z: np.full(len(x), 20.0)  # noqa: E731
    no_skirt, _, _ = _block_mesh(flat, bounds=(0.0, 64.0, 0.0, 64.0), floor=None)
    skirt, lo, _ = _block_mesh(flat, bounds=(0.0, 64.0, 0.0, 64.0), floor=-10.0)
    assert len(_faces(no_skirt)) == 16
    walls = _faces(skirt)[np.abs(_faces(skirt)[:, 1]) < 0.5]
    assert len(walls) == 16                      # 4 edges x 4 blocks
    assert lo == -10.0
