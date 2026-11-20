"""A body cannot go faster than Soma's MAX_SPEED, nor be stepped with a
velocity or position that is not a number; and bodies taken away while the
world runs go at its next repack, not one repack each.

The work these save is counted in
``tests/performance/test_physics_bounded_work.py``; this file holds what the
game sees: the speed a body is left with, that nothing raises, and that every
answer about the bodies is right between a removal and the next step.
"""
import math

import numpy as np
import pytest

from engine import spatial_grid as grid_module
from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from tests.physics.test_dynamic_bodies import (
    DRUM_SIZE, _drum_brush, _floor, _prop, _run, _world)

HEIGHT = DRUM_SIZE[1]
SLIDES = {'friction': 0.0, 'linear_damping': 0.0}
STEP = 1.0 / 60.0


def _build(*props, floor=None):
    world = _world(floor or _floor(size=(40000.0, 100.0, 40000.0)))
    world.rebuild([_drum_brush(prop) for prop in props])
    world.step(STEP)
    return world


def _speed(world, entity):
    velocity = world.get_body(entity).velocity
    return math.hypot(*velocity)


# -- the speed limit ----------------------------------------------------------

def test_max_speed_is_far_above_anything_play_makes():
    # A run is 300, Quake's sv_maxvelocity 2000, a default blast on a body of
    # mass 1 throws it at 400.
    assert float(Soma.MAX_SPEED) >= 10 * 2000.0


@pytest.mark.parametrize("push", [1e5, 1e7, 1e30])
def test_a_body_pushed_past_the_limit_moves_at_it_the_way_it_was_pushed(push):
    prop = _prop(**SLIDES)
    world = _build(prop)
    world.impulse(prop, [push, 0.0, push])
    world.step(STEP)

    vx, _vy, vz = world.get_body(prop).velocity
    assert math.hypot(vx, vz) == pytest.approx(float(Soma.MAX_SPEED), rel=1e-4)
    assert vx == pytest.approx(vz, rel=1e-5)            # still along the push
    # And it moved by that speed, not by the push.
    assert prop.pos[0] == pytest.approx(vx * STEP, rel=1e-3)


def test_a_body_under_the_limit_is_untouched():
    prop = _prop(**SLIDES)
    world = _build(prop)
    world.wake(prop, [5000.0, 0.0, -3000.0])
    world.step(STEP)
    assert world.get_body(prop).velocity == pytest.approx([5000.0, 0.0, -3000.0])


def test_a_light_body_in_a_strong_blast_is_held_to_the_limit():
    prop = _prop(mass=0.01, **SLIDES)
    world = _build(prop)
    world.impulse_radial([-10.0, HEIGHT * 0.5, 0.0], 400.0, 1e6)
    world.step(STEP)
    assert _speed(world, prop) <= float(Soma.MAX_SPEED) * (1 + 1e-5)


# -- not a number -------------------------------------------------------------

@pytest.mark.parametrize("bad", [float('nan'), float('inf'), -float('inf')])
def test_a_velocity_that_is_not_a_number_stops_the_body_and_raises_nothing(bad):
    prop = _prop(**SLIDES)
    other = _prop((300.0, 0.0, 0.0), **SLIDES)
    world = _build(prop, other)
    world.wake(other, [90.0, 0.0, 0.0])
    world.get_body(prop).velocity = [bad, 0.0, 0.0]
    world.get_body(prop).awake = True

    _run(world, 3)

    assert world.get_body(prop).velocity == [0.0, 0.0, 0.0]
    assert all(math.isfinite(v) for v in prop.pos)
    assert world.get_body(other).velocity[0] == pytest.approx(90.0)   # the rest step on


@pytest.mark.filterwarnings("ignore:invalid value:RuntimeWarning")
def test_an_infinite_blast_raises_nothing():
    prop = _prop(**SLIDES)
    world = _build(prop)
    world.impulse_radial([10.0, 10.0, 0.0], 200.0, float('inf'))
    _run(world, 2)
    assert all(math.isfinite(v) for v in world.get_body(prop).velocity)


def test_a_body_put_nowhere_sleeps_where_it_is_until_put_somewhere():
    prop = _prop(**SLIDES)
    other = _prop((300.0, 0.0, 0.0), **SLIDES)
    world = _build(prop, other)
    world.wake(other, [90.0, 0.0, 0.0])
    prop.pos = [float('nan'), 0.0, 0.0]
    world.sync_entity_position(prop, wake=True)

    _run(world, 3)                                       # raises nothing
    assert world.get_body(prop).awake is False
    assert other.pos[0] > 300.0

    prop.pos = [0.0, 200.0, 0.0]                         # put back
    world.sync_entity_position(prop, wake=True)
    _run(world, 120)
    assert prop.pos[1] == pytest.approx(0.0, abs=1e-3)   # and simulated again


# -- the broad phase finds the same pairs, however wide the box ----------------

def _pair_set(query, row):
    return sorted(zip(query.tolist(), row.tolist()))


def test_a_box_too_wide_to_walk_meets_the_same_brushes(monkeypatch):
    rng = np.random.default_rng(7)
    brushes = [{'pos': [float(x), 0.0, float(z)], 'size': [float(s), 64.0, float(s)],
                'is_trigger': False}
               for x, z, s in zip(rng.uniform(-20000, 20000, 300),
                                  rng.uniform(-20000, 20000, 300),
                                  rng.uniform(32, 3000, 300))]
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(brushes)
    centre = rng.uniform(-20000, 20000, (60, 3))
    half = np.concatenate((rng.uniform(1, 300, (30, 3)),           # walked
                           rng.uniform(2000, 15000, (30, 3))))      # too wide
    lo, hi = centre - half, centre + half

    found = _pair_set(*grid.box_pairs(lo, hi)[:2])
    monkeypatch.setattr(grid_module, 'MAX_WALK_CELLS', 10 ** 9)    # walk them all
    walked = _pair_set(*grid.box_pairs(lo, hi)[:2])

    assert found == walked
    assert len(found) > 1000


def test_a_body_too_wide_to_walk_meets_the_same_bodies(monkeypatch):
    rng = np.random.default_rng(3)
    props = [_prop((float(x), 0.0, float(z)))
             for x, z in rng.uniform(-15000, 15000, (400, 2))]
    world = _build(*props)
    world._index_bodies()
    centre = rng.uniform(-15000, 15000, (60, 3))
    half = np.concatenate((rng.uniform(1, 300, (30, 3)),
                           rng.uniform(2000, 12000, (30, 3))))
    lo, hi = centre - half, centre + half

    found = _pair_set(*world._bodies_near(lo, hi))
    monkeypatch.setattr(world, '_WALK_CELLS', 10 ** 9)
    walked = _pair_set(*world._bodies_near(lo, hi))

    assert found == walked
    assert len(found) > 1000


# -- bodies taken away --------------------------------------------------------

def test_between_a_removal_and_the_next_step_every_answer_is_right():
    below = _prop()
    above = _prop((0.0, HEIGHT, 0.0))
    slider = _prop((0.0, 0.0, 400.0), **SLIDES)
    gone = [_prop((600.0 + 100.0 * k, 0.0, 0.0)) for k in range(5)]
    world = _build(below, above, slider, *gone, floor=_floor())
    world.wake(slider, [90.0, 0.0, 0.0])
    _run(world, 20)
    slider_body = world.get_body(slider)
    where = list(slider.pos)

    for entity in gone:
        assert world.remove_body(entity) is True
    assert world.remove_body(gone[0]) is False
    assert world.remove_body(below) is True             # what stood on it wakes

    assert all(world.get_body(entity) is None for entity in gone + [below])
    assert world.get_body(slider) is slider_body
    assert slider_body.velocity[0] == pytest.approx(90.0)
    assert slider_body.position == pytest.approx(where)
    assert world.get_body(above).awake is True
    assert world.get_body(above).resting_on is None
    assert {id(e) for e in world.bodies_in_box([-1000, -10, -1000], [2000, 200, 1000])} \
        == {id(above), id(slider)}

    _run(world, 300)
    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)
    assert slider.pos[0] > where[0]


def test_a_body_removed_and_added_back_before_the_repack_starts_afresh():
    prop = _prop(**SLIDES)
    other = _prop((300.0, 0.0, 0.0))
    world = _build(prop, other)
    world.wake(prop, [90.0, 0.0, 0.0])
    _run(world, 10)

    world.remove_body(prop)
    prop.pos = [0.0, 0.0, 0.0]
    body = world.add_body(_drum_brush(prop))

    assert world.get_body(prop) is body
    assert body.velocity == [0.0, 0.0, 0.0]
    assert len(world._entities) == 2                     # listed once
    _run(world, 2)
    assert world.get_body(other) is not None


def test_a_removal_does_not_hand_one_bodys_flags_to_the_next():
    # The repack used to read each row's last hidden/disabled state by its
    # new position: removing the first body gave the third the second's
    # "was hidden", and with it a placement correction it never needed.
    first = _prop((0.0, 0.0, 0.0))
    hidden = _prop((200.0, 0.0, 0.0), hidden=True)
    third = _prop((400.0, 0.0, 0.0))
    world = _build(first, hidden, third)

    world.remove_body(first)
    world.get_body(third)                                # repacks
    assert not world._pending_settle.any()
    assert world.get_body(hidden) is not None


def test_a_repack_puts_authored_columns_back_and_carries_the_rest():
    light = _prop(mass=0.5, friction=0.2, gravity=False)
    other = _prop((300.0, 0.0, 0.0), mass=3.0)
    world = _build(light, other)
    world.wake(light, [40.0, 0.0, 0.0])
    _run(world, 5)
    velocity = world.get_body(light).velocity

    added = _prop((-300.0, 0.0, 0.0), mass=2.0)
    world.add_body(_drum_brush(added))
    world.remove_body(other)

    body = world.get_body(light)
    assert (body.mass, body.friction) == pytest.approx((0.5, 0.2))
    assert body.gravity is False
    assert body.velocity == pytest.approx(velocity)
    assert world.get_body(added).mass == pytest.approx(2.0)
