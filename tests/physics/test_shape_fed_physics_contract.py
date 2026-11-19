"""What the physics contract gives an implementation over a general solver
(a Bullet adapter): the world as shapes, the game's rules as values, and the
bookkeeping a solver does not keep -- all without engine internals."""

import ast
import pathlib

import pytest

from engine import brush_placement as bp
from engine import physics
from engine.physics import api, bookkeeping
from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from tests.helpers.worlds import angled_brush, box_brush

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _mesh_proxy(pos=(0.0, 0.0, 0.0)):
    x, y, z = pos
    tris = [(((x - 10, y, z - 10), (x + 10, y, z - 10), (x, y + 20, z + 10)), (0, 1, 0))]
    return {'pos': list(pos), 'size': [1, 1, 1], '_collision_mode': 'mesh',
            '_mesh_triangles': tris,
            '_mesh_bounds': ([x - 10, y, z - 10], [x + 10, y + 20, z + 10])}


def _grid(*brushes):
    grid = SpatialGrid()
    grid.populate(list(brushes))
    return grid


# -- the world as shapes ------------------------------------------------------

def test_every_solid_brush_is_one_shape_about_its_authored_centre():
    wall = box_brush('wall', (100, 32, 0), (16, 64, 128))
    ramp = angled_brush('ramp', (300, 32, 0), (64, 64, 64))
    prop = _mesh_proxy((0, 0, 500))
    water = box_brush('water', (0, 0, 0), (64, 64, 64), is_water=True)
    body = dict(box_brush('crate', (0, 0, 0)), _physics_body=True)
    shapes = {key: rest for key, *rest in _grid(wall, ramp, prop, water, body).solid_shapes()}

    assert len(shapes) == 3, "water and physics bodies are not solid shapes"
    by_kind = {kind: (data, centre, moves) for kind, data, centre, moves in shapes.values()}
    data, centre, moves = by_kind['box']
    assert data == (8.0, 32.0, 64.0) and centre == (100.0, 32.0, 0.0) and not moves
    data, centre, _ = by_kind['convex']
    lo, hi = bp.world_bounds(ramp)[:3], bp.world_bounds(ramp)[3:]
    for vertex in data:
        world = [centre[i] + vertex[i] for i in range(3)]
        assert all(lo[i] - 1e-6 <= world[i] <= hi[i] + 1e-6 for i in range(3))
    data, centre, _ = by_kind['mesh']
    assert centre == (0.0, 10.0, 500.0)
    assert data == (((-10.0, -10.0, -10.0), (10.0, -10.0, -10.0), (0.0, 10.0, 10.0)),)


def test_a_moving_shape_is_posed_live_and_a_new_world_has_a_new_version():
    lift = box_brush('lift', (0, 64, 0), (128, 16, 128), is_mover=True)
    wall = box_brush('wall', (500, 0, 0))
    grid = _grid(lift, wall)
    version = grid.world_version
    shapes = {kind_key: moves for kind_key, _k, _d, _c, moves in grid.solid_shapes()}
    lift_key, wall_key = id(lift), id(wall)
    assert shapes == {lift_key: True, wall_key: False}

    assert grid.shape_pose(wall_key) == ((500.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.0)
    bp.PREVIEW.set(lift, offset=(0.0, 100.0, 0.0), spin=((0.0, 1.0, 0.0), 30.0))
    try:
        centre, axis, angle = grid.shape_pose(lift_key)
        assert centre == pytest.approx((0.0, 164.0, 0.0))
        assert axis == pytest.approx((0.0, 1.0, 0.0)) and angle == pytest.approx(30.0)
        assert grid.world_version == version, "a mover moving is not a new world"
    finally:
        bp.PREVIEW.clear()

    grid.populate([wall])
    assert grid.world_version != version
    assert grid.shape_pose(lift_key) is None


# -- the game's rules as values ----------------------------------------------

@pytest.mark.parametrize('bounciness, mass', [
    (0.5, 1.0), (0.8, 5.5), (1.0, 10.0), (0.3, 0.2), (1.2, 3.0), (-1.0, 2.0)])
def test_restitution_is_the_contract_s_and_soma_s_alike(bounciness, mass):
    assert float(Soma.restitution(bounciness, mass)) == pytest.approx(
        api.restitution(bounciness, mass), abs=1e-6)


def test_rules_are_named_and_declared():
    assert Soma.PUSH_SCALE == physics.PUSH_SCALE == 0.85
    assert physics.physics_rules(Soma(SpatialGrid())) == frozenset(physics.GAME_RULES)
    assert physics.physics_rules(physics.NO_PHYSICS) == frozenset()

    class Partial:
        rules = ('heavy', 'push')
    assert physics.physics_rules(Partial()) == {'heavy', 'push'}
    assert physics.physics_rules(object()) == frozenset(physics.GAME_RULES), (
        "one that does not say was asked for every rule")


def test_soma_reaches_the_world_only_through_the_contract():
    """No brush_placement or mesh_columns in Soma: the box comes from
    BODY_BRUSH and the mesh surface from WORLD_QUERIES."""
    tree = ast.parse((ROOT / 'engine/physics/soma.py').read_text(encoding='utf-8'))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or '')
            modules.update(f"{node.module or ''}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    for internal in ('brush_placement', 'mesh_columns'):
        assert not any(internal in m for m in modules), (internal, sorted(modules))
    for name in ('_mesh_triangles', '_mesh_bounds'):
        assert name in api.BODY_BRUSH
    for query in ('prepare_mesh(triangles)', 'mesh_columns(mesh, x, z, shift)',
                  'world_version', 'solid_shapes()', 'shape_pose(key)'):
        assert query in api.WORLD_QUERIES


# -- bookkeeping ---------------------------------------------------------------

class _E:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


def test_the_support_graph_knows_who_rests_on_whom_and_what_to_wake():
    floor_crate, mid, top, apart = _E('floor'), _E('mid'), _E('top'), _E('apart')
    graph = bookkeeping.SupportGraph()
    graph.begin()
    graph.support(mid, floor_crate)
    graph.support(top, mid)
    graph.support(top, apart)              # a second support: the first stands
    assert graph.resting_on(top) is mid and graph.resting_on(floor_crate) is None
    assert graph.riders(floor_crate) == [mid, top]
    assert graph.riders(apart) == []

    graph.forget(mid)
    assert graph.resting_on(top) is None and graph.riders(floor_crate) == []
    graph.begin()
    assert graph.resting_on(top) is None


def test_the_cell_tracker_reports_column_crossings_once():
    a, b = _E('a'), _E('b')
    tracker = bookkeeping.CellTracker(512.0)
    assert not tracker.place(a, (10, 0, 10)) and not tracker.place(b, (10, 0, 10))
    assert tracker.changed() == []
    assert not tracker.place(a, (500, 90, 10))
    assert tracker.place(a, (520, 0, 10))
    assert tracker.place(b, (10, 0, -1))
    assert tracker.changed() == [a, b] and tracker.changed() == []
    tracker.place(b, (10, 0, 600))
    tracker.forget(b)
    assert tracker.changed() == []
