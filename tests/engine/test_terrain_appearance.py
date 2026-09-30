"""``Terrain`` appearance wiring: terraced collision, grass placement, save/load.

``engine.terrain`` imports PyOpenGL at module level, so this is the ``qt`` tier
(PyOpenGL importable, never called: every GL upload is avoided).
"""

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine import terrain_style as ts                 # noqa: E402
from engine.terrain import Terrain, TerrainChunk, grass_vertex_count  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def terrain():
    t = Terrain(seed=11)
    t.set_bounds(-1, 0, -1, 0)
    return t


def _chunk(t, cx=0, cz=0):
    return TerrainChunk(cx, cz, cx * t.chunk_size + t.offset_x,
                        cz * t.chunk_size + t.offset_z, t.chunk_size)


POINTS = [(12.3, -40.1), (-100.7, 7.9), (55.5, 55.5), (-3.0, -200.0)]


@pytest.mark.parametrize("mode", ["none", "smooth", "sharp", "blocks"])
def test_collision_matches_the_rendered_surface(terrain, mode):
    terrain.set_appearance(terrace_mode=mode, terrace_step=9.0)
    xs = np.array([p[0] for p in POINTS], dtype=np.float32)
    zs = np.array([p[1] for p in POINTS], dtype=np.float32)
    batch = terrain._get_heights_batch(xs, zs)
    for (x, z), b in zip(POINTS, batch):
        assert terrain._get_height_scalar(x, z) == pytest.approx(float(b), abs=1e-3)
        assert terrain.get_height_at(x, z) == pytest.approx(float(b), abs=1e-3)


@pytest.mark.parametrize("mode", ["smooth", "sharp", "blocks"])
def test_height_cache_reads_are_terraced(terrain, mode):
    terrain.set_appearance(terrace_mode=mode, terrace_step=9.0)
    for key in [(-1, -1), (-1, 0), (0, -1), (0, 0)]:
        chunk = terrain._ensure_chunk(*key)
        chunk.height_cache.build_batch(terrain._get_raw_heights_batch)
    for x, z in POINTS:
        # The cache interpolates the raw surface, then terraces it.
        expected = terrain._terrace_scalar(terrain._get_raw_height_scalar(
            *(terrain._block_centre(x, z) if mode == 'blocks' else (x, z))))
        assert terrain.get_height_at(x, z) == pytest.approx(expected, abs=0.5)


def test_blocks_are_flat(terrain):
    terrain.set_appearance(terrace_mode='blocks', block_size=16.0, terrace_step=8.0)
    xs = np.linspace(0.5, 15.5, 16, dtype=np.float32)
    h = terrain._get_heights_batch(xs, np.full_like(xs, 3.0))
    assert np.ptp(h) == 0.0
    assert float(h[0]) % 8.0 == pytest.approx(0.0, abs=1e-3)


def test_terrain_faces_point_up(terrain):
    verts = terrain._generate_chunk_mesh(_chunk(terrain), 16).reshape(-1, 14)
    assert np.all(verts[:, 4] > 0.0)       # face normal y
    assert np.all(verts[:, 12] > 0.0)      # smooth normal y


def test_block_mesh_is_used_in_blocks_mode(terrain):
    terrain.set_appearance(terrace_mode='blocks')
    verts = terrain._generate_chunk_mesh(_chunk(terrain), 16).reshape(-1, 14)
    normals = verts[:, 3:6]
    assert np.any(np.abs(normals[:, 1]) < 0.5)   # has walls
    assert np.all(np.isin(np.round(np.abs(normals), 6), [0.0, 1.0]))


def test_shape_changes_rebuild_and_colour_changes_do_not(terrain):
    chunk = terrain._ensure_chunk(0, 0)
    chunk.is_dirty = False
    terrain.set_appearance(contour_lines=0.5, palette='autumn')
    assert not chunk.is_dirty
    terrain.set_appearance(terrace_mode='sharp')
    assert chunk.is_dirty


def test_manual_change_marks_the_look_custom(terrain):
    terrain.apply_appearance_preset('retro_tiles')
    assert terrain.appearance.preset == 'retro_tiles'
    terrain.set_appearance(slope_rock=0.2)
    assert terrain.appearance.preset == 'retro_tiles'   # layers are separate
    terrain.set_appearance(grid_lines=0.0)
    assert terrain.appearance.preset == 'custom'


def test_unknown_option_is_rejected(terrain):
    with pytest.raises(AttributeError):
        terrain.set_appearance(sparkle=1.0)


def test_biome_brings_its_own_layer_heights(terrain):
    terrain.set_appearance(layer_heights=(0.1, 0.2, 0.3))
    terrain.set_biome('mountains')
    assert terrain._layer_heights() == ts.BIOME_LAYER_HEIGHTS['mountains']


def test_height_range_spans_the_terrain(terrain):
    lo, hi = terrain._layer_height_range()
    xs = np.random.default_rng(0).uniform(-256, 256, 400).astype(np.float32)
    zs = np.random.default_rng(1).uniform(-256, 256, 400).astype(np.float32)
    h = terrain._get_raw_heights_batch(xs, zs)
    assert lo < hi
    assert np.mean((h >= lo - 1.0) & (h <= hi + 1.0)) > 0.97


def test_appearance_survives_save_and_load(terrain):
    terrain.apply_appearance_preset('painted_strata')
    terrain.set_appearance(layer_heights=(0.1, 0.45, 0.9), wall_color=(0.1, 0.2, 0.3))
    loaded = Terrain(seed=1)
    loaded.from_dict(terrain.to_dict())
    assert loaded.appearance == terrain.appearance


def test_old_maps_load_with_the_original_look(terrain):
    data = terrain.to_dict()
    data.pop('appearance')
    terrain.apply_appearance_preset('voxel_blocks')
    terrain.from_dict(data)
    assert terrain.appearance == ts.TerrainAppearance()


# ---------------------------------------------------------------------------
# Grass
# ---------------------------------------------------------------------------

def test_grass_vertex_counts():
    assert grass_vertex_count(1) == 3
    assert grass_vertex_count(5) == 27


def test_grass_blades_sit_on_the_ground(terrain):
    terrain.set_grass(True, density=0.05)
    for mode in ('none', 'blocks'):
        terrain.set_appearance(terrace_mode=mode)
        blades = terrain._generate_grass_blades(_chunk(terrain))
        assert len(blades) > 0
        ground = terrain._get_heights_batch(blades[:, 0], blades[:, 2])
        np.testing.assert_allclose(blades[:, 1], ground, atol=1e-3)


def test_grass_is_deterministic(terrain):
    terrain.set_grass(True, density=0.05)
    a = terrain._generate_grass_blades(_chunk(terrain))
    b = terrain._generate_grass_blades(_chunk(terrain))
    np.testing.assert_array_equal(a, b)


def test_no_grass_at_high_elevation(terrain):
    """Grass grows only in the grass height layer - never on the heights."""
    terrain.set_grass(True, density=0.06)
    terrain.set_appearance(layer_heights=(0.0, 0.4, 1.0), layer_blend=0.02)
    blades = []
    for cx in (-1, 0):
        for cz in (-1, 0):
            blades.append(terrain._generate_grass_blades(_chunk(terrain, cx, cz)))
    blades = np.concatenate(blades)
    assert len(blades) > 0
    raw = terrain._get_raw_heights_batch(blades[:, 0], blades[:, 2])
    frac = terrain._normalized_layer_height(raw)
    # Boundary + blend band, plus the slope across a tuft's spread.
    assert frac.max() < 0.4 + 2 * 2 * 0.02 + 0.05

    # And the high ground really exists here, so the check above is not vacuous.
    xs = np.linspace(-256, 255, 64, dtype=np.float32)
    gx, gz = np.meshgrid(xs, xs)
    ground = terrain._normalized_layer_height(
        terrain._get_raw_heights_batch(gx.ravel(), gz.ravel()))
    assert np.mean(ground > 0.5) > 0.1


def test_grass_leaves_clearings(terrain):
    """Even on all-grass ground the cover is patchy, not an even carpet."""
    terrain.set_grass(True, density=0.06)
    terrain.set_appearance(layer_heights=(0.0, 1.0, 1.0))
    terrain.GRASS_MIN_NORMAL_Y = 0.0
    blades = terrain._generate_grass_blades(_chunk(terrain))
    tufts = len(blades) / terrain.GRASS_BLADES_PER_TUFT
    placed = min(terrain.GRASS_MAX_PER_CHUNK, int(0.06 * terrain.chunk_size ** 2))
    assert 0.2 * placed < tufts < 0.9 * placed
