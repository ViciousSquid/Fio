"""The player's mesh passes walk only the triangles near the capsule.

A model's Mesh collision is a triangle list every pass of the player's
resolve used to walk whole, rejecting each triangle by its bounds in Python:
7 ms a tick against a 1,924-triangle model, on a tick that otherwise costs
0.08 ms. ``engine.brush_placement.triangles_near`` selects the triangles
whose bounds meet a pass's reach with one NumPy test over bounds kept beside
the mesh; the pass's own tests then run on those alone.

The selection must change nothing: it is a superset of what each pass
accepts, in the list's order. Small meshes keep the loop
(``MESH_CULL_VECTOR_FROM``, a registered small-N crossover).
"""

import math
import random

import glm
import pytest

from engine import brush_placement
from engine.brush_placement import triangles_near
from engine.player import Player


def _soup(count, seed, spread=260.0, size=24.0):
    """Random triangles round the origin, with their normals."""
    rng = random.Random(seed)
    triangles = []
    for _ in range(count):
        centre = [rng.uniform(-spread, spread), rng.uniform(0.0, 120.0),
                  rng.uniform(-spread, spread)]
        a, b, c = (tuple(centre[i] + rng.uniform(-size, size) for i in range(3))
                   for _ in range(3))
        e1 = [b[i] - a[i] for i in range(3)]
        e2 = [c[i] - a[i] for i in range(3)]
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2],
             e1[0] * e2[1] - e1[1] * e2[0])
        length = math.sqrt(sum(v * v for v in n)) or 1.0
        triangles.append(((a, b, c), tuple(v / length for v in n)))
    return triangles


def _proxy(triangles):
    points = [v for tri, _normal in triangles for v in tri]
    return {
        'pos': [0, 0, 0], 'size': [1, 1, 1], 'hidden': False, 'is_trigger': False,
        'is_mover': False, 'is_door': False, 'is_water': False, 'is_fog': False,
        '_model_collision': True, '_physics_entity': object(), '_physics_body': False,
        '_collision_mode': 'mesh', '_mesh_triangles': triangles,
        '_mesh_bounds': ([min(p[i] for p in points) for i in range(3)],
                         [max(p[i] for p in points) for i in range(3)]),
    }


def _meets(triangle, lo, hi):
    return all(min(v[i] for v in triangle) <= hi[i] and max(v[i] for v in triangle) >= lo[i]
               for i in range(3))


def test_the_cull_keeps_exactly_the_triangles_whose_bounds_meet_the_box():
    triangles = _soup(400, seed=3)
    brush = _proxy(triangles)
    rng = random.Random(9)
    for _ in range(40):
        centre = [rng.uniform(-250, 250), rng.uniform(0, 120), rng.uniform(-250, 250)]
        half = [rng.uniform(1, 80) for _ in range(3)]
        lo = tuple(c - h for c, h in zip(centre, half))
        hi = tuple(c + h for c, h in zip(centre, half))
        near = triangles_near(brush, triangles, lo, hi)
        assert near == [entry for entry in triangles if _meets(entry[0], lo, hi)]
        assert all(any(kept is entry for entry in triangles) for kept in near)


def test_touching_counts_and_a_point_is_a_box():
    flat = [(((0.0, 5.0, 0.0), (10.0, 5.0, 0.0), (0.0, 5.0, 10.0)), (0.0, 1.0, 0.0))] * 40
    brush = _proxy(flat)
    assert len(triangles_near(brush, flat, (10.0, 5.0, 0.0), (20.0, 9.0, 5.0))) == 40
    assert len(triangles_near(brush, flat, (2.0, 5.0, 2.0), (2.0, 5.0, 2.0))) == 40
    assert triangles_near(brush, flat, (10.001, 5.0, 0.0), (20.0, 9.0, 5.0)) == []


def test_a_short_list_and_a_list_that_is_all_near_come_back_as_they_are():
    few = _soup(brush_placement.MESH_CULL_VECTOR_FROM - 1, seed=5)
    assert triangles_near(_proxy(few), few, (1e6, 1e6, 1e6), (2e6, 2e6, 2e6)) is few
    many = _soup(200, seed=6)
    assert triangles_near(_proxy(many), many, (-1e6,) * 3, (1e6,) * 3) is many


def test_the_bounds_follow_the_list_they_were_made_for():
    """A carried or turned mesh is a new list each time it moves; the bounds
    kept on the proxy are for one list and no other."""
    first = _soup(100, seed=7)
    brush = _proxy(first)
    lo, hi = (-40.0, 0.0, -40.0), (40.0, 120.0, 40.0)
    assert triangles_near(brush, first, lo, hi) == [e for e in first if _meets(e[0], lo, hi)]
    moved = [(tuple((v[0] + 500.0, v[1], v[2]) for v in tri), normal) for tri, normal in first]
    assert triangles_near(brush, moved, lo, hi) == []
    assert triangles_near(brush, first, lo, hi) == [e for e in first if _meets(e[0], lo, hi)]


def _walk(brushes, ticks=420):
    player = Player(0.0, 0.0, angle=0.3)
    player.pos = glm.vec3(-240.0, 200.0, -240.0)
    path = []
    for tick in range(ticks):
        move = type("Input", (), {"x": 1 if (tick // 90) % 2 else 0, "z": 1})()
        if tick % 75 == 0:
            player.angle += 0.9
        player.update(1.0 / 60.0, move, tick % 130 == 0, False, brushes)
        path.append((player.pos.x, player.pos.y, player.pos.z, player.on_ground))
    return path


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_the_player_goes_the_same_way_with_and_without_the_cull(monkeypatch, seed):
    """Both sides of the crossover: the whole list walked by each pass, and
    the culled one, put the player in the same place every tick, to the bit."""
    floor = {"pos": [0.0, -50.0, 0.0], "size": [4000.0, 100.0, 4000.0]}

    def run(crossover):
        monkeypatch.setattr(brush_placement, "MESH_CULL_VECTOR_FROM", crossover)
        return _walk([floor, _proxy(_soup(600, seed))])

    looped, culled = run(10 ** 9), run(1)
    assert looped == culled
    assert len({(round(x), round(z)) for x, _y, z, _g in culled}) > 20, "it moved"


def test_a_pass_walks_a_fraction_of_the_mesh(monkeypatch):
    """The work, counted: closest-point tests are what a pass does with a
    triangle it could not reject, and they are as many as before; the
    triangles handed to the passes are far fewer than the mesh has."""
    floor = {"pos": [0.0, -50.0, 0.0], "size": [4000.0, 100.0, 4000.0]}
    triangles = _soup(600, seed=11)
    handed = []
    real = brush_placement.triangles_near

    def counting(brush, tris, lo, hi):
        near = real(brush, tris, lo, hi)
        handed.append((len(near), len(tris)))
        return near
    monkeypatch.setattr("engine.player.triangles_near", counting)
    _walk([floor, _proxy(triangles)], ticks=240)
    assert handed, "the passes ran"
    kept = sum(near for near, _all in handed)
    whole = sum(count for _near, count in handed)
    assert kept * 10 < whole, (kept, whole)
