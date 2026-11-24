"""The strata walls at the terrain's edge: options, publication, edge maths.

``engine.terrain`` and ``engine.renderer.core.terrain_gl`` import PyOpenGL at
module level, so this is the ``qt`` tier (PyOpenGL importable, never called).
The walls as drawn are tests/visual/test_terrain_strata.py.
"""

import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine import shaders                                   # noqa: E402
from engine import terrain_style as ts                       # noqa: E402
from engine.renderer.core.terrain_gl import TerrainGL        # noqa: E402
from engine.terrain import Terrain                           # noqa: E402
from engine.terrain_frame import frustum_planes              # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def terrain():
    t = Terrain(seed=11)
    t.set_biome('grassy_hills')
    t.set_bounds(-1, 0, -1, 0)
    return t


def test_strata_are_on_by_default_with_their_layers():
    a = ts.TerrainAppearance()
    assert a.strata is True
    assert ts.STRATA_LAYERS_MIN <= a.strata_layers <= ts.STRATA_LAYERS_MAX


def test_the_options_are_sanitised_and_saved():
    a = ts.TerrainAppearance.from_dict({'strata': 0, 'strata_layers': 10_000})
    assert a.strata is False
    assert a.strata_layers == ts.STRATA_LAYERS_MAX
    assert ts.TerrainAppearance.from_dict({'strata_layers': 'x'}).strata_layers == 24
    assert ts.TerrainAppearance.from_dict({'strata_layers': -3}).strata_layers \
        == ts.STRATA_LAYERS_MIN
    back = ts.TerrainAppearance.from_dict(a.to_dict())
    assert (back.strata, back.strata_layers) == (a.strata, a.strata_layers)


def test_a_preset_keeps_the_strata_and_they_keep_the_preset(terrain):
    terrain.apply_appearance_preset('painted_strata')
    terrain.set_appearance(strata=False, strata_layers=9)
    assert terrain.appearance.preset == 'painted_strata'
    terrain.apply_appearance_preset('natural')
    assert (terrain.appearance.strata, terrain.appearance.strata_layers) == (False, 9)


def test_the_frame_publishes_the_walls(terrain):
    skirt = terrain.publish().skirt
    (min_x, max_x), (min_z, max_z) = terrain.get_terrain_bounds()
    assert skirt[:4] == (min_x, max_x, min_z, max_z)
    floor, low, high, layers = skirt[4:]
    assert floor < low <= high
    assert low - floor >= terrain.SKIRT_MIN_DEPTH - 1e-6
    assert layers == terrain.appearance.strata_layers


def test_no_walls_when_off_or_in_blocks(terrain):
    terrain.set_appearance(strata=False)
    assert terrain.publish().skirt is None
    terrain.set_appearance(strata=True, terrace_mode='blocks')
    assert terrain.publish().skirt is None        # Blocks build their own sides


def test_only_chunks_on_the_edge_have_sides():
    bounds = (0.0, 768.0, 0.0, 768.0)
    world = [(0.0, 0.0), (256.0, 256.0), (512.0, 0.0), (512.0, 512.0), (0.0, 512.0)]
    pairs = TerrainGL.edge_sides(world, [256.0] * 5, bounds)
    by_chunk = {}
    for i, side in pairs:
        by_chunk.setdefault(i, set()).add(side)
    assert by_chunk == {0: {0, 2}, 2: {1, 2}, 3: {1, 3}, 4: {0, 3}}
    assert 1 not in by_chunk                       # the middle chunk


def test_a_wall_is_culled_only_when_its_whole_reach_is_out_of_view():
    import glm
    proj = glm.perspective(glm.radians(60.0), 1.0, 1.0, 5000.0)
    # Looking along +x from the origin, level.
    view = glm.lookAt(glm.vec3(0, 0, 0), glm.vec3(1, 0, 0), glm.vec3(0, 1, 0))
    planes = frustum_planes(proj * view)
    world = [(1000.0, -128.0), (-3000.0, -128.0), (1000.0, -128.0)]
    seen = TerrainGL.skirt_visible(
        world, [256.0] * 3, [400.0, 400.0, 400.0], [500.0, 500.0, 500.0],
        floor=-600.0, lean=0.6, frustum_planes=planes)
    # Ahead: its chunk sits above the view, but the wall reaches down into it.
    assert bool(seen[0])
    assert not bool(seen[1])                       # behind the camera
    assert TerrainGL.skirt_visible(world, [256.0] * 3, [0.0] * 3, [1.0] * 3,
                                   -1.0, 0.6, None).all()


def test_the_layers_set_the_bed_thickness():
    thin = TerrainGL.bed_thickness(-300.0, 200.0, 50)
    thick = TerrainGL.bed_thickness(-300.0, 200.0, 5)
    assert thin == pytest.approx(10.0)
    assert thick == pytest.approx(100.0)
    assert TerrainGL.bed_thickness(0.0, 1.0, 80) == TerrainGL.SKIRT_MIN_BED


def test_the_top_layer_is_the_terrains_own_material():
    """The wall runs terrain.frag's material code at the rim, unchanged, so
    the top layer is always the colour of the ground above it."""
    _, _, body = shaders.DEFAULT_SHADERS['terrain.frag'].partition('void main() {')
    material = body.partition('    // ---- Lighting')[0]
    frag = shaders.DEFAULT_SHADERS['terrain_skirt.frag']
    assert material in frag
    assert 'ground = texColor;' in frag
    head = shaders.DEFAULT_SHADERS['terrain.vert'].partition('void main() {')[0]
    assert shaders.DEFAULT_SHADERS['terrain_skirt.vert'].startswith(head)
