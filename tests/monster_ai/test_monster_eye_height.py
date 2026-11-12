"""Monsters see and shoot from their eyes, measured from the centre of their
billboard (Monster.pos), and other monsters' bodies are where they are drawn.

These heights used to be ``pos + 64`` everywhere, as if pos were the feet: a
192-unit human stood with its eyes at 160 but looked out from 224, and a
flying monster spat projectiles from 64 units over its own head.
"""

import glm
import pytest

from engine.monster_constants import monster_body_rise, monster_eye_rise


def test_eyes_near_the_top_of_a_human_and_in_the_middle_of_a_flyer():
    human = {'monster_type': 'human', 'sprite_height': 192}
    flying = {'monster_type': 'flying', 'sprite_height': 160}
    # A grounded human's centre is 96 above the floor: eyes at 163.2 of 192.
    assert 96 + monster_eye_rise(human) == pytest.approx(163.2)
    assert monster_eye_rise(flying) == 0.0
    # The body's middle matches the player's hit sphere: feet + 0.45 H.
    assert 96 + monster_body_rise(human) == pytest.approx(0.45 * 192)
    # Heights follow a resized sprite.
    assert monster_eye_rise({'sprite_height': 384}) == pytest.approx(134.4)


def test_a_flying_monster_spits_from_its_middle(monster_factory, ai_world, flat_ground):
    flyer = monster_factory('f', (0, 300, 0), monster_type='flying', sprite_height=160)
    ai, logic = ai_world(brushes=flat_ground, things=[flyer])
    launched = []
    logic.combat_runtime._add_monster_projectile = (
        lambda start, *rest: launched.append(start))
    ai._spawn_monster_projectile(flyer, glm.vec3(500, 50, 0), 10, id(flyer))
    assert launched == [pytest.approx((0.0, 300.0, 0.0))]


def test_a_human_looks_out_from_its_eyes(monster_factory, ai_world, flat_ground):
    human = monster_factory('h', (300, 96, 0), sprite_height=192, awake=True)
    ai, logic = ai_world(brushes=flat_ground, things=[human])
    rays = []
    ai._has_line_of_sight = lambda eye, target: rays.append((eye.y, target.y)) or True
    for _ in range(40):
        ai.update(0.05)
    assert rays, 'the monster never looked for the player'
    eye_y, target_y = rays[0]
    assert eye_y == pytest.approx(96 + 0.35 * 192)
    assert target_y == pytest.approx(logic.player_runtime.player.camera_height)


def test_crossfire_strikes_a_monster_where_its_body_is_drawn(monster_factory, ai_world,
                                                             flat_ground):
    shooter = monster_factory('s', (600, 96, 0), sprite_height=192)
    # Standing in the line of fire, its body 0..192 above the floor.
    victim = monster_factory('v', (300, 96, 0), sprite_height=192)
    ai, _logic = ai_world(brushes=flat_ground, things=[shooter, victim])
    eye = glm.vec3(600, 96 + monster_eye_rise(shooter.properties), 0)
    # A low shot crosses the victim about 50 above the floor, at its shins:
    # it hits (the old sphere, centred 160 above the floor, missed it).
    assert ai._find_monster_in_crossfire(shooter, eye, glm.vec3(0, -60, 0)) is victim
    # One that passes well over its head does not.
    assert ai._find_monster_in_crossfire(shooter, eye, glm.vec3(0, 400, 0)) is None
