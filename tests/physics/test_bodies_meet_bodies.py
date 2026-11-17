"""Bodies meeting bodies (Soma).

To an awake body another body is one more box in its way: something to stop
at, to rest on. Unlike a wall it can be moved -- knocked on by the momentum
the two share -- and when it goes, what rested on it wakes and falls.
"""
import random

import glm
import pytest

from engine.spatial import set_authored_flag
from tests.physics.test_dynamic_bodies import (
    DRUM_SIZE, _drum_brush, _floor, _player, _prop, _run, _world)

WIDTH = DRUM_SIZE[0]
HEIGHT = DRUM_SIZE[1]
SLIDES = {'friction': 0.0, 'linear_damping': 0.0}


def _build(*props):
    world = _world(_floor())
    world.rebuild([_drum_brush(prop) for prop in props])
    world.step(1.0 / 60.0)
    return world


def _speed(world, prop, axis=0):
    return world.get_body(prop).velocity[axis]


def test_a_body_dropped_on_another_comes_to_rest_on_it():
    below = _prop()
    above = _prop((5.0, 200.0, 0.0))
    world = _build(below, above)
    world.wake(above)
    _run(world, 300)

    assert above.pos[1] == pytest.approx(HEIGHT, abs=1e-3)
    assert below.pos[1] == pytest.approx(0.0, abs=1e-3)
    assert world.get_body(above).awake is False


def test_bodies_dropped_together_pile_up_and_all_come_to_rest():
    rng = random.Random(3)
    props = [_prop((rng.uniform(-60.0, 60.0), 100.0 + 80.0 * k, rng.uniform(-60.0, 60.0)))
             for k in range(30)]
    world = _build(*props)
    for prop in props:
        world.wake(prop)
    _run(world, 1500)

    assert not world._awake.any()
    heights = sorted(prop.pos[1] for prop in props)
    assert heights[0] == pytest.approx(0.0, abs=1e-3)
    # Thirty drums do not fit on the floor of a 120-unit square: they stack,
    # in whole drum heights.
    assert heights[-1] >= 3 * HEIGHT - 1.0
    for height in heights:
        assert height / HEIGHT == pytest.approx(round(height / HEIGHT), abs=1e-3)


def test_a_body_run_into_is_knocked_on_with_the_momentum_they_share():
    """Equal masses: the two leave together at half the speed one arrived at."""
    mover = _prop(**SLIDES)
    struck = _prop((200.0, 0.0, 0.0), **SLIDES)
    world = _build(mover, struck)
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert _speed(world, mover) == pytest.approx(150.0)
    assert _speed(world, struck) == pytest.approx(150.0)
    # One behind the other, never inside it.
    assert struck.pos[0] - mover.pos[0] >= WIDTH


def test_a_heavy_body_barely_notices_a_light_one():
    mover = _prop(**SLIDES)
    struck = _prop((200.0, 0.0, 0.0), mass=9.0, **SLIDES)
    world = _build(mover, struck)
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert _speed(world, struck) == pytest.approx(30.0)
    assert _speed(world, mover) == pytest.approx(30.0)


def test_a_body_too_heavy_to_move_is_a_wall():
    """Knocked on at less than resting speed, it would only be woken to fall
    asleep again: it stays asleep, and what met it stops."""
    mover = _prop(**SLIDES)
    struck = _prop((200.0, 0.0, 0.0), mass=20.0)
    world = _build(mover, struck)
    world.wake(mover, [10.0, 0.0, 0.0])
    _run(world, 1200)

    assert struck.pos[0] == 200.0
    assert world.get_body(struck).awake is False
    assert _speed(world, mover) == 0.0
    assert 0.0 < 200.0 - (mover.pos[0] + WIDTH) <= 0.1


def test_bouncy_bodies_pass_their_speed_on():
    """Billiards: the one that arrives stops, the one it hits leaves."""
    cue = _prop(bounciness=1.0, **SLIDES)
    ball = _prop((200.0, 0.0, 0.0), bounciness=1.0, **SLIDES)
    world = _build(cue, ball)
    world.wake(cue, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert _speed(world, cue) == pytest.approx(0.0, abs=1e-3)
    assert _speed(world, ball) == pytest.approx(300.0)


def test_carrying_off_what_a_body_rests_on_lets_it_fall():
    below = _prop()
    above = _prop((0.0, 200.0, 0.0))
    world = _build(below, above)
    world.wake(above)
    _run(world, 300)
    assert world.get_body(above).awake is False

    world.set_kinematic(below, True)             # picked up
    below.pos = [500.0, 0.0, 0.0]
    _run(world, 300)

    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_knocking_away_what_a_body_rests_on_lets_it_fall():
    below = _prop(**SLIDES)
    above = _prop((0.0, 200.0, 0.0))
    mover = _prop((-300.0, 0.0, 0.0), **SLIDES)
    world = _build(below, above, mover)
    world.wake(above)
    _run(world, 300)
    assert above.pos[1] == pytest.approx(HEIGHT, abs=1e-3)

    world.wake(mover, [600.0, 0.0, 0.0])
    _run(world, 600)

    assert below.pos[0] > 500.0
    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_hiding_what_a_body_rests_on_lets_it_fall():
    below = _prop()
    above = _prop((0.0, 200.0, 0.0))
    world = _build(below, above)
    world.wake(above)
    _run(world, 300)

    set_authored_flag(below, 'hidden', True)
    _run(world, 300)

    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_a_body_stays_awake_while_what_it_rests_on_is_moving():
    below = _prop(**SLIDES)
    above = _prop((0.0, 200.0, 0.0))
    world = _build(below, above)
    world.wake(above)
    _run(world, 300)

    world.wake(below, [30.0, 0.0, 0.0])          # slides out from under it
    _run(world, 30)
    assert world.get_body(above).awake is True
    _run(world, 600)
    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_bodies_placed_overlapping_stay_free_of_each_other():
    """A map that stood two physics props in the same place is not blown
    apart: a body inside another has no face of it to stop at."""
    mover = _prop(**SLIDES)
    other = _prop((10.0, 0.0, 0.0))
    world = _build(mover, other)
    world.wake(mover, [120.0, 0.0, 0.0])
    _run(world, 60)

    assert mover.pos[0] == pytest.approx(120.0, abs=0.5)
    assert other.pos[0] == 10.0


@pytest.mark.parametrize('flag', ['hidden', 'disabled'])
def test_a_dormant_body_is_in_nobodys_way(flag):
    mover = _prop(**SLIDES)
    other = _prop((200.0, 0.0, 0.0), **{flag: True})
    world = _build(mover, other)
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert mover.pos[0] == pytest.approx(300.0, abs=0.5)
    assert other.pos[0] == 200.0


def test_a_carried_body_is_in_nobodys_way():
    mover = _prop(**SLIDES)
    carried = _prop((200.0, 0.0, 0.0))
    world = _build(mover, carried)
    world.set_kinematic(carried, True)
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert mover.pos[0] == pytest.approx(300.0, abs=0.5)


def test_a_ghost_body_is_in_nobodys_way():
    mover = _prop(**SLIDES)
    ghost = _prop((200.0, 0.0, 0.0), no_collision=True)
    world = _build(mover, ghost)
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 60)

    assert mover.pos[0] == pytest.approx(300.0, abs=0.5)


def test_the_player_pushes_a_row_of_bodies_without_one_entering_another():
    """The push moves a body before the step begins: it is part of the move,
    and must not put the body inside the one in front of it."""
    first = _prop()
    second = _prop((60.0, 0.0, 0.0))
    world = _build(first, second)
    player = _player()
    for _ in range(120):
        player.pos = glm.vec3(player.pos.x + player.velocity.x / 60.0, 50.0, 0.0)
        player.velocity = glm.vec3(120.0, 0.0, 0.0)
        world.step(1.0 / 60.0, player)

    assert second.pos[0] > 60.0                                 # the row moved
    assert second.pos[0] - first.pos[0] >= WIDTH                # in a row
    assert first.pos[0] - player.pos.x >= (WIDTH + 50.0) * 0.5  # player behind


def test_bodies_stacked_in_the_map_do_not_fall_or_fire_onrest_at_play_start():
    below = _prop()
    above = _prop((0.0, HEIGHT, 0.0))
    world = _world(_floor())
    rested = []
    world.rebuild([_drum_brush(below), _drum_brush(above)])
    world.set_rest_callback(above, rested.append)
    _run(world, 60)

    assert world.get_body(above).awake is False
    assert above.pos[1] == pytest.approx(HEIGHT)
    assert rested == []
    # And it still knows what it stands on.
    world.set_kinematic(below, True)
    below.pos = [500.0, 0.0, 0.0]
    _run(world, 300)
    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_a_world_of_sleeping_bodies_asks_nothing_about_them():
    """Bodies meeting bodies costs the awake, not the world: with every body
    asleep the index is never built, let alone asked."""
    props = [_prop((-450.0 + 100.0 * (k % 10), 0.0, -200.0 + 100.0 * (k // 10)))
             for k in range(50)]
    world = _build(*props)
    _run(world, 5)
    world._bodies_dirty = True
    calls = []
    world._bodies_near = lambda *a: calls.append(a)
    _run(world, 60)

    assert calls == []
    assert world._bodies_dirty is True
