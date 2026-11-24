"""HD_High's sky light volume (engine/renderer/hd_high/skylight.py).

NumPy over a render table, no OpenGL calls: daylight fills what lies under
the open sky, spreads indoors through windows and doors and fades with
distance, and never passes through a wall -- however thin.
"""

import numpy as np
import pytest

from tests.helpers.worlds import angled_brush, box_brush

pytestmark = [pytest.mark.qt]       # the package imports PyOpenGL


def _room(thickness=16.0, window=True, offset=(0.0, 0.0, 0.0)):
    """A 512 x 256 x 512 room on a wide floor, its walls *thickness* thick,
    with a 128-square window in the +x wall (or none)."""
    t = float(thickness)
    out = 256.0 + t / 2.0
    span = 512.0 + 2.0 * t
    boxes = [("floor", (0, -16, 0), (2048, 32, 2048)),
             ("roof", (0, out, 0), (span, t, span)),
             ("west", (-out, 128, 0), (t, 256, span)),
             ("north", (0, 128, -out), (span, 256, t)),
             ("south", (0, 128, out), (span, 256, t)),
             ("east a", (out, 128, -(64 + (span / 2 - 64) / 2)), (t, 256, span / 2 - 64)),
             ("east b", (out, 128, 64 + (span / 2 - 64) / 2), (t, 256, span / 2 - 64)),
             ("sill", (out, 32, 0), (t, 64, 128)),
             ("head", (out, 224, 0), (t, 64, 128))]
    if not window:
        boxes.append(("plug", (out, 128, 0), (t, 128, 128)))
    return [box_brush(name, tuple(np.add(pos, offset)), size) for name, pos, size in boxes]


def _volume(brushes, camera=None):
    from engine.render_table import RenderTable
    from engine.renderer.hd_high import skylight
    table = RenderTable()
    table.begin_frame(brushes, epoch=1)
    low = (table.center - table.half).min(axis=0)
    high = (table.center + table.half).max(axis=0)
    grid = skylight.grid_for(low, high, camera)
    light, solid = skylight.build(table, np.arange(table.count), grid)
    return skylight, grid, light, solid


def _inside_surfaces(offset, reach):
    """Points on the room's inner faces, moved *reach* off them as a shader
    moves a surface point."""
    points = []
    for u in np.linspace(-240.0, 240.0, 25):
        for v in np.linspace(16.0, 240.0, 13):
            points += [(-256 + reach, v, u), (256 - reach, v, u),
                       (u, v, -256 + reach), (u, v, 256 - reach)]
        for w in np.linspace(-240.0, 240.0, 25):
            points += [(u, reach, w), (u, 256 - reach, w)]
    return np.asarray(points) + offset


def test_daylight_comes_in_through_a_window_and_fades_into_the_room():
    skylight, grid, light, solid = _volume(_room())

    def at(*point):
        return skylight.sample(light, solid, grid, point)
    assert at(700.0, 40.0, 0.0) == 1.0                  # under the open sky
    assert at(300.0, 20.0, 150.0) == 1.0                # outside, against the wall
    by_window, middle, far = at(220.0, 128.0, 0.0), at(0.0, 100.0, 0.0), at(-230.0, 100.0, 0.0)
    assert 0.5 < by_window < 1.0
    assert 0.1 < middle < by_window * 0.6
    assert 0.02 < far < middle * 0.6
    # Round the corner from the window, on the window's own wall: less
    # than straight in front of it at the same depth.
    assert at(236.0, 40.0, 200.0) < at(236.0, 128.0, 0.0)


@pytest.mark.parametrize("thickness", [8.0, 12.0, 16.0, 24.0, 32.0, 64.0])
def test_a_closed_room_gets_no_daylight_whatever_its_walls(thickness):
    """No window: nothing inside is lit, wherever the walls fall in the
    grid -- thinner than a cell included -- and the outside keeps its light."""
    rng = np.random.default_rng(int(thickness))
    for _ in range(4):
        offset = rng.uniform(-40.0, 40.0, 3)
        skylight, grid, light, solid = _volume(_room(thickness, window=False, offset=offset))
        reach = grid.cell * skylight.SURFACE_OFFSET
        inside = [skylight.sample(light, solid, grid, p)
                  for p in _inside_surfaces(offset, reach)]
        assert max(inside) == 0.0, (thickness, offset, max(inside))
        assert skylight.sample(light, solid, grid, np.array([0.0, 120.0, 0.0]) + offset) == 0.0
        out = 256.0 + thickness + reach
        for point in ((out, 100.0, 0.0), (0.0, out, 0.0), (-out, 30.0, 100.0)):
            assert skylight.sample(light, solid, grid, np.array(point) + offset) == 1.0


def test_a_thin_walled_room_with_a_window_is_lit_through_the_window_only():
    """Walls thinner than a cell: the window still lets the light in."""
    skylight, grid, light, solid = _volume(_room(8.0))
    assert grid.cell > 8.0
    assert skylight.sample(light, solid, grid, (200.0, 128.0, 0.0)) > 0.4
    assert 0.0 < skylight.sample(light, solid, grid, (-200.0, 100.0, 0.0)) < 0.3


def test_light_goes_under_an_overhang_and_fades_with_distance():
    from engine.renderer.hd_high import skylight
    # A roof on no walls: lit near its edge, dark far under it.
    brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
               box_brush("roof", (-1024, 208, 0), (2048, 16, 4096))]
    _, grid, light, solid = _volume(brushes)
    edge = skylight.sample(light, solid, grid, (-60.0, 20.0, 0.0))
    deep = skylight.sample(light, solid, grid, (-1200.0, 20.0, 0.0))
    assert skylight.sample(light, solid, grid, (300.0, 20.0, 0.0)) == 1.0
    assert 0.5 < edge < 1.0 and deep < 0.05
    # The spread is by distance: a tenth of the light about 500 units in.
    assert 0.05 < skylight.sample(light, solid, grid, (-500.0, 20.0, 0.0)) < 0.2


def test_a_turned_wall_and_a_convex_brush_block_light_too():
    """Brushes that are not axis-aligned boxes are voxelised by their own
    shape: a slab turned 45 degrees closes a room's corner."""
    from engine.render_table import RenderTable
    from engine.renderer.hd_high import skylight
    brushes = _room(16.0, window=False)
    table = RenderTable()
    table.begin_frame(brushes + [angled_brush("wedge", (600.0, 64.0, 600.0), (128.0, 128.0, 128.0))],
                      epoch=1)
    slots = np.arange(table.count)
    low = (table.center - table.half).min(axis=0)
    high = (table.center + table.half).max(axis=0)
    grid = skylight.grid_for(low, high)
    solid = skylight.voxelise(table, slots, grid)
    boxes_only = skylight.voxelise(table, slots[:-1], grid)
    wedge = solid & ~boxes_only
    cells = 128.0 / grid.cell
    # The clipped box fills a part of its bounds, not all of them, not none.
    assert 0.15 * cells ** 3 < wedge.sum() < 0.95 * (cells + 1) ** 3


def test_the_grid_is_as_fine_as_the_map_allows_and_follows_the_view_on_a_big_one():
    from engine.renderer.hd_high import skylight
    small = skylight.grid_for((-1000, 0, -1000), (1000, 400, 1000))
    assert small.cell == skylight.CELL_SIZES[0] and not small.follows
    medium = skylight.grid_for((-2300, 0, -1500), (2300, 1200, 1500))
    assert medium.cell > small.cell and not medium.follows
    assert np.prod(medium.shape) <= skylight.MAX_CELLS
    big = skylight.grid_for((-8000, 0, -8000), (8000, 300, 8000), camera=(3000.0, 50.0, -2000.0))
    assert big.follows and np.prod(big.shape) <= skylight.MAX_CELLS
    centre = np.add(big.origin, np.multiply(big.extent, 0.5))
    assert abs(centre[0] - 3000.0) <= big.cell * skylight.FOLLOW_STEP
    assert abs(centre[2] + 2000.0) <= big.cell * skylight.FOLLOW_STEP
    # A few steps of walking keep the same window (no rebuild every frame).
    near = skylight.grid_for((-8000, 0, -8000), (8000, 300, 8000), camera=(3020.0, 50.0, -1990.0))
    assert near.key == big.key
    # Beside the window there is only sky.
    light = np.zeros(big.shape, dtype=np.float32)
    solid = np.zeros(big.shape, dtype=bool)
    assert skylight.sample(light, solid, big, (7900.0, 50.0, 7900.0)) == 1.0


def test_the_texture_holds_light_and_openness_in_gl_order():
    from engine.renderer.hd_high import skylight
    solid = np.zeros((4, 3, 2), dtype=bool)
    solid[1, 2, 0] = True
    light = np.full((4, 3, 2), 0.5, dtype=np.float32)
    light[solid] = 0.0
    texels = skylight.texels(light, solid)
    assert texels.shape == (2, 3, 4, 2) and texels.dtype == np.uint16
    assert tuple(texels[0, 2, 1]) == (0, 0)
    assert tuple(texels[1, 0, 3]) == (32768, 65535)
