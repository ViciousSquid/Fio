"""New objects go where the user is (editor/placement.py), not the origin."""

import pytest

from editor.placement import PLACE_AHEAD, WALL_MARGIN, floor_below, placement_ahead


def box(pos, size, **extra):
    brush = {'pos': list(pos), 'size': list(size)}
    brush.update(extra)
    return brush


FLOOR = box((0, -32, 0), (4096, 64, 4096))           # top at y = 0


def test_placed_ahead_of_the_camera_on_the_floor():
    # yaw 0 looks along +x
    pos = placement_ahead((100.0, 120.0, 50.0), 0.0, [FLOOR])
    assert pos == pytest.approx([100.0 + PLACE_AHEAD, 0.0, 50.0])

    # yaw -90 (the editor's default) looks along -z
    pos = placement_ahead((0.0, 120.0, 0.0), -90.0, [FLOOR])
    assert pos == pytest.approx([0.0, 0.0, -PLACE_AHEAD], abs=1e-6)


def test_stops_short_of_a_wall_in_the_way():
    wall = box((100, 64, 0), (16, 256, 512))           # near face at x = 92
    pos = placement_ahead((0.0, 120.0, 0.0), 0.0, [FLOOR, wall])
    assert pos[0] == pytest.approx(92.0 - WALL_MARGIN)
    assert pos[1] == pytest.approx(0.0)


def test_set_down_on_the_highest_floor_below_not_one_above():
    table = box((PLACE_AHEAD, 20, 0), (64, 40, 64))     # top at y = 40
    ceiling = box((PLACE_AHEAD, 300, 0), (512, 16, 512))
    pos = placement_ahead((0.0, 120.0, 0.0), 0.0, [FLOOR, table, ceiling])
    assert pos[1] == pytest.approx(40.0)


def test_triggers_and_water_are_not_floors():
    trigger = box((PLACE_AHEAD, 50, 0), (64, 100, 64), is_trigger=True)
    pos = placement_ahead((0.0, 120.0, 0.0), 0.0, [FLOOR, trigger])
    assert pos[1] == pytest.approx(0.0)


def test_snapped_to_the_grid():
    pos = placement_ahead((3.0, 120.0, 7.0), 0.0, [FLOOR], grid=16)
    assert pos[0] % 16 == 0 and pos[2] % 16 == 0


def test_a_glm_camera_position_is_accepted():
    glm = pytest.importorskip("glm")
    pos = placement_ahead(glm.vec3(0.0, 120.0, 0.0), 0.0, [FLOOR])
    assert pos == pytest.approx([PLACE_AHEAD, 0.0, 0.0])


def test_with_no_floor_it_stays_at_camera_height():
    assert placement_ahead((0.0, 120.0, 0.0), 0.0, [])[1] == pytest.approx(120.0)


def test_terrain_is_a_floor():
    class Terrain:
        def get_height_at_safe(self, x, z):
            return 12.5
    assert floor_below(0.0, 0.0, 100.0, [], Terrain()) == pytest.approx(12.5)
    assert floor_below(0.0, 0.0, 10.0, [], Terrain()) is None
