"""Barrels: Props that fall over, roll on their side and are picked back up.

A body that ``topples`` is a box standing up until something knocks it over
and the same box on its side after. Nothing about tipping is simulated: going
over is a decision, a swap of two of the box's dimensions, and a quarter turn
played over a few frames -- so a barrel costs what any other body costs.
"""
import glm
import numpy as np
import pytest

from engine.physics.soma import Soma, quat_from_rotation
from engine.prop_entity import (BALL_MODEL_PATH, DEFAULT_MODEL_PATH, Prop,
                                model_settings, topples_by_default)
from tests.helpers.worlds import box_brush
from tests.physics.test_dynamic_bodies import (
    DRUM_SIZE, _drum_brush, _floor, _player, _prop, _run, _world)
from tests.physics.test_slopes_and_terrain import _ramp
from tests.physics.test_slopes_and_terrain import _world as _shaped_world

RADIUS = DRUM_SIZE[0] * 0.5
HEIGHT = DRUM_SIZE[1]


def _barrel(pos=(0.0, 0.0, 0.0), **props):
    return _prop(pos, topples=True, **props)


def _build(*props, statics=None):
    world = _world(*(statics or [_floor()]))
    world.rebuild([_drum_brush(prop) for prop in props])
    world.step(1.0 / 60.0)
    return world


def _lying(world, prop):
    return int(world._lying[world._indices[id(prop)]])


def _up(prop):
    """Where the Prop's own up points in the world, by how it is drawn."""
    w, x, y, z = quat_from_rotation(prop.properties['rotation'])[0]
    return np.array([2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)])


def _push(world, speed, frames=90):
    player = _player(vx=speed)
    for _ in range(frames):
        player.pos = glm.vec3(player.pos.x + player.velocity.x / 60.0, 50.0, 0.0)
        player.velocity = glm.vec3(speed, 0.0, 0.0)
        world.step(1.0 / 60.0, player)
    return player


# -- going over ---------------------------------------------------------------

def test_walking_into_a_barrel_slides_it_and_running_into_it_knocks_it_over():
    walked = _barrel()
    world = _build(walked)
    _push(world, 200.0)
    assert _lying(world, walked) == 0
    assert walked.pos[0] > 100.0                               # pushed along, upright
    assert walked.properties['rotation'] == [0.0, 0.0, 0.0]

    run_into = _barrel()
    world = _build(run_into)
    _push(world, 300.0)
    _run(world, 60)
    assert _lying(world, run_into) == 1                        # along x: the way it went
    assert _up(run_into) == pytest.approx([1.0, 0.0, 0.0], abs=1e-6)


def test_a_fallen_barrel_is_the_same_box_on_its_side_lying_on_the_floor():
    barrel = _barrel()
    world = _build(barrel)
    body = world.get_body(barrel)
    assert body.half_extents == pytest.approx((RADIUS, HEIGHT * 0.5, RADIUS))

    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 600)

    assert body.half_extents == pytest.approx((HEIGHT * 0.5, RADIUS, RADIUS))
    index = world._indices[id(barrel)]
    bottom = barrel.pos[1] + world._offset[index, 1] - world._half[index, 1]
    assert bottom == pytest.approx(0.0, abs=1e-3)              # on the floor, not in it
    assert body.awake is False


@pytest.mark.parametrize('velocity, axis, toward', [
    ([300.0, 0.0, 0.0], 1, [1.0, 0.0, 0.0]),
    ([-300.0, 0.0, 0.0], 1, [-1.0, 0.0, 0.0]),
    ([0.0, 0.0, 300.0], 2, [0.0, 0.0, 1.0]),
    ([0.0, 0.0, -300.0], 2, [0.0, 0.0, -1.0]),
    ([250.0, 0.0, 120.0], 1, [1.0, 0.0, 0.0]),                 # mostly along x
])
def test_a_barrel_falls_the_way_it_was_going(velocity, axis, toward):
    barrel = _barrel()
    world = _build(barrel)
    world.wake(barrel, velocity)
    _run(world, 60)

    assert _lying(world, barrel) == axis
    assert _up(barrel) == pytest.approx(toward, abs=1e-6)


def test_going_over_is_a_quarter_turn_played_over_a_few_frames():
    barrel = _barrel(friction=0.0, linear_damping=0.0)
    world = _build(barrel)
    world.wake(barrel, [300.0, 0.0, 0.0])
    tilt = []
    for _ in range(int(Soma.TOPPLE_TIME * 60) + 4):
        world.step(1.0 / 60.0)
        tilt.append(float(_up(barrel)[1]))

    assert tilt[0] > 0.9                                       # barely begun
    assert all(b <= a + 1e-9 for a, b in zip(tilt, tilt[1:]))  # steadily over
    assert tilt[-1] == pytest.approx(0.0, abs=1e-6)            # flat


def test_a_barrel_does_not_fall_asleep_half_way_over():
    barrel = _barrel(friction=1.0, linear_damping=20.0)        # stops almost at once
    world = _build(barrel)
    world.wake(barrel, [600.0, 0.0, 0.0])
    _run(world, 600)

    assert _lying(world, barrel) == 1
    assert _up(barrel)[1] == pytest.approx(0.0, abs=1e-6)
    assert world.get_body(barrel).awake is False


def test_a_barrel_dropped_while_moving_goes_over_and_one_dropped_straight_does_not():
    straight = _barrel((0.0, 400.0, 0.0))
    moving = _barrel((200.0, 400.0, 200.0), friction=0.0, linear_damping=0.0)
    world = _build(straight, moving)
    world.wake(straight)
    world.wake(moving, [60.0, 0.0, 0.0])
    _run(world, 300)

    assert _lying(world, straight) == 0
    assert _lying(world, moving) == 1


def test_a_barrel_on_ground_too_steep_goes_over_downhill():
    world, _grid = _shaped_world(box_brush('floor', (0, -50, 0), (4000, 100, 4000)),
                                 _ramp(0.9))
    barrel = _barrel((0.0, 400.0, 0.0), friction=1.0)
    world.rebuild([_drum_brush(barrel)])
    world.wake(barrel)
    _run(world, 60)

    assert _lying(world, barrel) == 1
    assert _up(barrel)[0] < -0.99                              # the ramp rises along +x


def test_a_barrel_with_no_room_to_lie_stays_standing():
    wall = _floor((80.0, 200.0, 0.0), (20.0, 400.0, 400.0))    # just ahead of it
    barrel = _barrel(friction=0.0, linear_damping=0.0)
    world = _build(barrel, statics=[_floor(), wall])
    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 30)

    assert _lying(world, barrel) == 0
    assert barrel.pos[0] + RADIUS <= 70.0


def test_what_stood_on_a_barrel_falls_when_it_goes_over():
    barrel = _barrel()
    above = _prop((0.0, HEIGHT, 0.0))
    world = _build(barrel, above)
    _run(world, 30)
    assert above.pos[1] == pytest.approx(HEIGHT)

    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 600)
    assert above.pos[1] < HEIGHT - 1.0


# -- on its side --------------------------------------------------------------

def test_a_lying_barrel_rolls_across_its_length_and_slides_along_it():
    def travel(velocity, axis):
        barrel = _barrel()
        world = _build(barrel)
        world.wake(barrel, [300.0, 0.0, 0.0])
        _run(world, 600)                                       # over, along x, at rest
        start = barrel.pos[axis]
        world.wake(barrel, velocity)
        _run(world, 900)
        return abs(barrel.pos[axis] - start)

    rolled = travel([0.0, 0.0, 150.0], 2)
    slid = travel([150.0, 0.0, 0.0], 0)
    assert rolled > 3.0 * slid
    assert slid > 5.0


def test_a_rolling_barrel_turns_about_its_length_by_how_far_it_rolls():
    barrel = _barrel()
    world = _build(barrel)
    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 600)
    before = quat_from_rotation(barrel.properties['rotation'])[0]
    start = barrel.pos[2]
    world.wake(barrel, [0.0, 0.0, 60.0])
    _run(world, 20)

    assert _up(barrel) == pytest.approx([1.0, 0.0, 0.0], abs=1e-6)   # still along x
    after = quat_from_rotation(barrel.properties['rotation'])[0]
    turned = 2.0 * np.arccos(min(1.0, abs(float(before @ after))))
    assert turned == pytest.approx((barrel.pos[2] - start) / RADIUS, rel=0.02)


def test_a_lying_barrel_rolls_down_a_slope_it_would_stand_on():
    """Upright, friction holds it on a slope of 0.3; on its side across the
    slope, it rolls."""
    world, _grid = _shaped_world(box_brush('floor', (0, -50, 0), (4000, 100, 4000)),
                                 _ramp(0.3))
    upright = _barrel((0.0, 300.0, 0.0))
    world.rebuild([_drum_brush(upright)])
    world.wake(upright)
    _run(world, 400)
    assert _lying(world, upright) == 0
    assert upright.pos[0] == pytest.approx(0.0, abs=0.5)

    world, _grid = _shaped_world(box_brush('floor', (0, -50, 0), (4000, 100, 4000)),
                                 _ramp(0.3))
    lying = _barrel((0.0, 300.0, 0.0), rotation=[90.0, 0.0, 0.0])   # along z
    world.rebuild([_drum_brush(lying)])
    assert _lying(world, lying) == 2
    world.wake(lying)
    _run(world, 900)
    assert lying.pos[0] < -250.0                               # off the foot of it


# -- picking it back up -------------------------------------------------------

def test_picking_a_fallen_barrel_up_stands_it_up():
    barrel = _barrel()
    world = _build(barrel)
    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 600)
    assert _lying(world, barrel) == 1

    world.set_kinematic(barrel, True)                          # picked up

    assert _lying(world, barrel) == 0
    assert barrel.properties['rotation'] == [0.0, 0.0, 0.0]
    assert world.get_body(barrel).half_extents == pytest.approx(
        (RADIUS, HEIGHT * 0.5, RADIUS))

    barrel.pos = [300.0, 200.0, 300.0]                         # carried, then dropped
    world.set_kinematic(barrel, False)
    world.wake(barrel)
    _run(world, 300)
    assert _lying(world, barrel) == 0
    assert barrel.pos[1] == pytest.approx(0.0, abs=1e-3)       # standing on the floor


def test_a_barrel_lying_in_a_saved_game_is_still_lying_when_it_is_loaded():
    """Told by how the entity is turned, so nothing new has to be saved."""
    barrel = _barrel()
    world = _build(barrel)
    world.wake(barrel, [300.0, 0.0, 0.0])
    _run(world, 600)
    index = world._indices[id(barrel)]
    saved_pos, saved_rotation = list(barrel.pos), list(barrel.properties['rotation'])
    box_centre = world._position[index] + world._offset[index]
    box_half = world._half[index].copy()

    reloaded = _barrel(tuple(saved_pos), rotation=saved_rotation)
    # The collision runtime derives the body's box from the turned model.
    brush = {'pos': box_centre.tolist(), 'size': (box_half * 2.0).tolist(),
             '_physics_body': True, '_physics_entity': reloaded}
    again = _world(_floor())
    again.rebuild([brush])
    assert _lying(again, reloaded) == 1
    again.set_kinematic(reloaded, True)
    assert again.get_body(reloaded).half_extents == pytest.approx(
        (RADIUS, HEIGHT * 0.5, RADIUS))

    # And one whose box was derived from the model as authored is laid down.
    upright_box = _barrel(tuple(saved_pos), rotation=saved_rotation)
    brush = {'pos': [saved_pos[0], saved_pos[1] + HEIGHT * 0.5, saved_pos[2]],
             'size': list(DRUM_SIZE), '_physics_body': True, '_physics_entity': upright_box}
    again = _world(_floor())
    again.rebuild([brush])
    assert _lying(again, upright_box) == 1
    assert again.get_body(upright_box).half_extents == pytest.approx(
        (HEIGHT * 0.5, RADIUS, RADIUS))


# -- which props are barrels --------------------------------------------------

def test_the_oil_barrels_topple_by_default_and_a_ball_does_not():
    assert topples_by_default(DEFAULT_MODEL_PATH)
    assert topples_by_default('assets/models/Oil_Drum_Grey.obj')
    assert not topples_by_default(BALL_MODEL_PATH)
    assert model_settings(DEFAULT_MODEL_PATH)['topples'] is True
    assert model_settings(BALL_MODEL_PATH)['topples'] is False

    # A map saved before barrels could fall over says nothing: its barrels do.
    assert Prop(properties={'model_path': DEFAULT_MODEL_PATH}).properties['topples'] is True
    assert Prop.for_model(BALL_MODEL_PATH).properties['topples'] is False
    assert Prop(properties={'sprite_path': 'x.png'}).properties['topples'] is False
    # And what a Prop says is what it does.
    told = Prop(properties={'model_path': DEFAULT_MODEL_PATH, 'topples': False})
    assert told.properties['topples'] is False


def test_a_body_that_does_not_topple_never_does():
    crate = _prop()                                            # the same box, no topples
    world = _build(crate)
    world.wake(crate, [600.0, 0.0, 0.0])
    _run(world, 120)
    assert _lying(world, crate) == 0
    assert world._any_topples is False


def test_a_rolling_ball_is_not_a_barrel():
    ball = _prop(rolls=True, topples=True)
    world = _build(ball)
    world.wake(ball, [600.0, 0.0, 0.0])
    _run(world, 60)
    assert _lying(world, ball) == 0
