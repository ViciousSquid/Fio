"""Collision Shape: Mesh, as physics honours it.

A Prop's Collision Shape is AABB (the default: a box derived from its model,
the cheap path) or Mesh (the model's triangles). With Mesh, a body rests on
the model's real surface and is stopped by its real sides -- for a static
Prop, and for another physics body -- where AABB keeps the box exactly as it
was. Physics asks a mesh the one question it asks an angled brush: over this
point, between what heights is it solid (``engine.mesh_columns``).
"""
import numpy as np
import pytest

from engine import mesh_columns
from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from tests.physics.test_dynamic_bodies import (DRUM_HALF_Y, DRUM_SIZE, _drum_brush,
                                               _floor, _prop, _run)

pytestmark = pytest.mark.qt          # Prop is editor.things', which imports Qt

#: The ramp: 100 wide and deep, its top climbing along +x from 0 at x = -50
#: to 100 at x = 50. Its box is therefore 100 tall everywhere.
RAMP_TOP = 100.0


def _ramp_triangles(at=(0.0, 0.0, 0.0)):
    ox, oy, oz = at
    a, b, c, d = (-50, 0, -50), (50, 0, -50), (50, 0, 50), (-50, 0, 50)
    e, f = (50, 100, -50), (50, 100, 50)
    faces = [(a, b, c), (a, c, d),          # the bottom
             (b, e, f), (b, f, c),          # the tall back, at x = 50
             (a, d, f), (a, f, e),          # the slope
             (a, e, b), (d, c, f)]          # the two sides
    return [(tuple((x + ox, y + oy, z + oz) for x, y, z in face), (0.0, 1.0, 0.0))
            for face in faces]


def _slope(x):
    return x + 50.0


def _proxy(shape, at=(0.0, 0.0, 0.0), entity=None):
    """The collision proxy ``build_model_collision_brushes`` makes for a ramp
    Prop at *at*: its triangles for Mesh, a box around them for AABB."""
    brush = {'hidden': False, 'is_trigger': False, '_model_collision': True,
             '_physics_entity': entity, '_physics_body': entity is not None}
    if shape == 'mesh':
        triangles = _ramp_triangles(at)
        points = np.asarray([tri[0] for tri in triangles]).reshape(-1, 3)
        brush.update(pos=list(at), size=[1, 1, 1], _collision_mode='mesh',
                     _mesh_triangles=triangles,
                     _mesh_bounds=(points.min(axis=0).tolist(), points.max(axis=0).tolist()))
    else:
        brush.update(pos=[at[0], at[1] + 50.0, at[2]], size=[100.0, 100.0, 100.0],
                     _collision_mode='aabb')
    return brush


def _world(*statics):
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([_floor(), *statics])
    return Soma(grid), grid


def _settle(world, bodies, drum, frames=500):
    world.rebuild(bodies)
    world.wake(drum)
    _run(world, frames)


# -- the question ------------------------------------------------------------

def test_a_mesh_s_column_is_its_surface():
    mesh = mesh_columns.prepare(_ramp_triangles())
    x = np.array([[-49.0, -30.0, 0.0, 49.0, 60.0]])

    bottom, top = mesh_columns.column(mesh, x, np.zeros_like(x))

    assert top[0, :4] == pytest.approx(_slope(x[0, :4]), abs=1e-6)
    assert bottom[0, :4] == pytest.approx(0.0, abs=1e-6)
    assert np.isnan(top[0, 4]) and np.isnan(bottom[0, 4]), "past its end is a miss"


def test_a_moved_mesh_s_column_moves_with_it():
    mesh = mesh_columns.prepare(_ramp_triangles())
    x = np.array([[70.0, 100.0]])

    bottom, top = mesh_columns.column(mesh, x, np.full_like(x, 5.0), shift=(100.0, 8.0, 5.0))

    assert top[0] == pytest.approx([_slope(-30.0) + 8.0, _slope(0.0) + 8.0])
    assert bottom[0] == pytest.approx([8.0, 8.0])


def test_a_mesh_with_nothing_to_stand_on_is_no_mesh():
    wall = [(((0, 0, 0), (0, 10, 0), (0, 10, 10)), (1.0, 0.0, 0.0))]     # edge on

    assert mesh_columns.prepare(wall) is None
    assert mesh_columns.prepare([]) is None and mesh_columns.prepare(None) is None


def test_the_grid_gives_a_mesh_prop_s_surface_and_an_aabb_prop_s_box():
    _w, grid = _world(_proxy('mesh', (0, 0, 0)), _proxy('aabb', (400, 0, 0)))
    rows = grid._rows_now()
    assert rows.shaped.tolist() == [False, True, False], (
        "AABB is the box path; only Mesh is asked for its surface")

    x = np.array([[-30.0, 20.0]])
    _bottom, top = grid.columns(rows, np.array([1]), x, np.zeros_like(x))
    assert top[0] == pytest.approx([_slope(-30.0), _slope(20.0)])

    x = np.array([[370.0, 420.0]])
    _bottom, top = grid.columns(rows, np.array([2]), x, np.zeros_like(x))
    assert top[0] == pytest.approx([RAMP_TOP, RAMP_TOP])


# -- a static Prop -----------------------------------------------------------

def test_a_body_dropped_on_a_static_mesh_prop_meets_its_slope():
    world, _grid = _world(_proxy('mesh'))
    drum = _prop((-30.0, 300.0, 0.0))

    _settle(world, [_drum_brush(drum)], drum)

    assert drum.pos[1] < RAMP_TOP * 0.5, (
        "the drum is resting %.1f up: on the ramp's box, not its slope" % drum.pos[1])
    assert drum.pos[0] < -30.0, "a 45-degree slope should have sent it down and off"


def test_a_body_dropped_on_a_static_aabb_prop_rests_on_its_box():
    world, _grid = _world(_proxy('aabb'))
    drum = _prop((-30.0, 300.0, 0.0))

    _settle(world, [_drum_brush(drum)], drum)

    assert drum.pos[1] == pytest.approx(RAMP_TOP, abs=1.0)
    assert drum.pos[0] == pytest.approx(-30.0, abs=1.0)


# -- another body -------------------------------------------------------------

def _ramp_body(shape, **props):
    props.setdefault('mass', 50.0)
    ramp = _prop((0.0, 0.0, 0.0), **props)
    return ramp, _proxy(shape, (0.0, 0.0, 0.0), entity=ramp)


def test_a_body_dropped_on_a_mesh_body_meets_its_slope():
    world, _grid = _world()
    ramp, ramp_brush = _ramp_body('mesh')
    drum = _prop((-30.0, 300.0, 0.0))

    _settle(world, [ramp_brush, _drum_brush(drum)], drum)

    assert world._any_mesh
    assert drum.pos[1] < RAMP_TOP * 0.5, (
        "the drum is resting %.1f up: on the other body's box" % drum.pos[1])


def test_a_body_dropped_on_an_aabb_body_rests_on_its_box():
    world, _grid = _world()
    ramp, ramp_brush = _ramp_body('aabb')
    drum = _prop((-30.0, 300.0, 0.0))

    _settle(world, [ramp_brush, _drum_brush(drum)], drum)

    assert not world._any_mesh, "an AABB world must not look at meshes at all"
    assert drum.pos[1] == pytest.approx(RAMP_TOP, abs=1.0)


def test_a_mesh_body_that_rolls_is_met_as_its_box():
    """Its triangles are where it was made; a body that turns as it moves is
    the box around it."""
    world, _grid = _world()
    ramp, ramp_brush = _ramp_body('mesh', rolls=True, gravity=False)
    drum = _prop((-30.0, 300.0, 0.0))

    _settle(world, [ramp_brush, _drum_brush(drum)], drum, frames=120)

    assert drum.pos[1] > RAMP_TOP * 0.8, drum.pos


def test_a_body_run_into_a_mesh_body_s_side_knocks_it_on():
    world, _grid = _world()
    ramp, ramp_brush = _ramp_body('mesh', mass=1.0, friction=0.0, linear_damping=0.0)
    drum = _prop((50.0 + DRUM_SIZE[0], 0.0, 0.0), friction=0.0, linear_damping=0.0)
    world.rebuild([ramp_brush, _drum_brush(drum)])
    _run(world, 20)

    world.impulse(drum, (-150.0, 0.0, 0.0))        # at the ramp's tall back face
    _run(world, 60)

    assert ramp.pos[0] < -1.0, (
        "the ramp was not knocked on: a Mesh body must share momentum as a box does")
    # Wherever the two have got to, the drum is still behind the ramp's back
    # face (x = 50 from the ramp's origin), give or take its support points.
    assert drum.pos[0] - ramp.pos[0] > 50.0 + 10.0, (
        "the drum went into the ramp's back face", drum.pos, ramp.pos)


# -- world brushes are never a Prop's business --------------------------------

def _body_on_ramp(shape, rise, at, **props):
    """A physics Prop of the given Collision Shape above a ramp brush."""
    from tests.physics.test_slopes_and_terrain import _ramp

    grid = SpatialGrid(cell_size=512.0)
    grid.populate([_floor((0, -50, 0), (4000, 100, 4000)), _ramp(rise)])
    world = Soma(grid)
    prop = _prop(at, **props)
    body = _drum_brush(prop)
    body['_collision_mode'] = shape
    if shape == 'mesh':
        # A Mesh body: its own triangles (a drum-sized block here).
        hx, hz = DRUM_SIZE[0] / 2.0, DRUM_SIZE[2] / 2.0
        x, y, z = prop.pos
        a, b, c, d = ((x - hx, y, z - hz), (x + hx, y, z - hz),
                      (x + hx, y, z + hz), (x - hx, y, z + hz))
        top = [(px, py + DRUM_SIZE[1], pz) for px, py, pz in (a, b, c, d)]
        faces = [(a, b, c), (a, c, d), (top[0], top[1], top[2]), (top[0], top[2], top[3])]
        body['_mesh_triangles'] = [(face, (0.0, 1.0, 0.0)) for face in faces]
        body['_mesh_bounds'] = ([x - hx, y, z - hz], [x + hx, y + DRUM_SIZE[1], z + hz])
        body.update(pos=list(prop.pos), size=[1, 1, 1])
    world.rebuild([body])
    world.wake(prop)
    return world, prop


@pytest.mark.parametrize("shape", ["aabb", "mesh"])
@pytest.mark.parametrize("rolls", [False, True], ids=["slides", "rolls"])
def test_a_prop_of_either_shape_goes_down_a_slope(shape, rolls):
    """A slope steeper than a body's friction sends it downhill -- sliding,
    or rolling if it is set to roll -- whichever Collision Shape it has."""
    from tests.physics.test_slopes_and_terrain import _surface

    rise = 1.0                                      # 45 degrees: steeper than friction 0.55
    world, prop = _body_on_ramp(shape, rise, (100.0, _surface(100.0, rise) + 60.0, 0.0),
                                rolls=rolls)
    _run(world, 30)
    landed = prop.pos[0]

    _run(world, 400)

    assert prop.pos[0] < landed - 100.0, (
        "the %s prop stayed on the slope: x %.1f -> %.1f" % (shape, landed, prop.pos[0]))
    assert prop.pos[1] < 30.0, ("it should have reached the bottom", prop.pos)


@pytest.mark.parametrize("shape", ["aabb", "mesh"])
def test_a_ramp_brush_is_a_ramp_whatever_the_prop_s_collision_shape(shape):
    """Collision Shape is about the Prop's own model. A world brush collides
    as the shape it is: a Prop set to AABB stands on a ramp brush's slope,
    never on the box the ramp is filed by."""
    from tests.physics.test_slopes_and_terrain import _surface

    rise = 0.25                                     # gentle: it stays where it lands
    world, prop = _body_on_ramp(shape, rise, (-100.0, 300.0, 0.0), friction=1.0)
    _run(world, 400)

    ramp_box_top = 400.0 * rise
    surface = _surface(prop.pos[0], rise)
    assert prop.pos[1] < ramp_box_top - 20.0, (
        "the prop is resting %.1f up: on the ramp brush's box (%.0f), not its slope"
        % (prop.pos[1], ramp_box_top))
    assert prop.pos[1] == pytest.approx(surface, abs=12.0), (
        "the prop is not on the ramp's surface", prop.pos, surface)
