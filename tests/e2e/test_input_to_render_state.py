"""Game input -> player and monster AI -> world -> published render state.

Keys and mouse go in where Qt's handlers put them; the player and the monster
AI run their production updates; the published frame is checked against the
authoritative world (the Player object, the Monster things) and against
itself (the player walks where the published camera looks).
"""

import math

import glm
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                  # noqa: E402

from editor.things import Monster, PlayerStart              # noqa: E402
from engine.player import PM_SPRINT_SCALE                    # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

SPAWN = (0.0, 40.0, 0.0)


def _arena():
    things = [make_thing(PlayerStart, "spawn", SPAWN, angle=0.0)]
    things += [make_thing(Monster, "m%d" % i, (x, 40.0, 700.0))
               for i, x in enumerate((-500.0, 0.0, 500.0))]
    return level_data(brushes=room(size=2048.0, height=512.0), things=things)


def _published(session):
    with session.render_state() as frame:
        rows = frame.entity_table
        monsters = {thing.properties["id"]: tuple(float(v) for v in rows.pos[slot])
                    for thing in session.state.things if isinstance(thing, Monster)
                    for slot in (rows.slot_of_id[thing.properties["id"]],)}
        view = frame.camera_view_matrix
        forward = -glm.vec3(view[0][2], view[1][2], view[2][2])
        return (glm.vec3(frame.player_pos), float(frame.player_angle),
                glm.normalize(forward), monsters)


def _walk(session, ticks, keys):
    player = session.logic.player_runtime.player
    start = glm.vec3(player.pos)
    session.step(ticks, keys=keys)
    moved = glm.vec3(player.pos) - start
    return glm.length(glm.vec3(moved.x, 0.0, moved.z)), moved


def test_walking_turning_and_sprinting_reach_the_published_frame(fio_session):
    session = fio_session(_arena()).start_play()
    player = session.logic.player_runtime.player
    session.step(10)                                  # land on the floor

    pos, angle, forward, _ = _published(session)
    assert glm.length(pos - player.pos) < 1e-3
    assert angle == pytest.approx(player.angle)

    # W walks the player the way the published camera faces.
    walked, moved = _walk(session, 30, {Qt.Key_W})
    assert walked > 50.0, "W moved the player %.1f units in half a second" % walked
    flat_forward = glm.normalize(glm.vec3(forward.x, 0.0, forward.z))
    alignment = glm.dot(glm.normalize(glm.vec3(moved.x, 0.0, moved.z)), flat_forward)
    assert alignment > 0.99, alignment
    pos, _, _, _ = _published(session)
    assert glm.length(pos - player.pos) < 1e-3

    # Mouse look turns by the documented sensitivity, every tick.
    before = player.angle
    session.step(20, mouse=(10.0, 0.0))
    assert player.angle == pytest.approx(before - 20 * 10.0 * 0.002)
    _, angle, _, _ = _published(session)
    assert angle == pytest.approx(player.angle)

    # Shift sprints at the sprint scale (steady state, after acceleration).
    _walk(session, 15, {Qt.Key_W})
    walk, _ = _walk(session, 20, {Qt.Key_W})
    _walk(session, 15, {Qt.Key_W, Qt.Key_Shift})
    sprint, _ = _walk(session, 20, {Qt.Key_W, Qt.Key_Shift})
    assert sprint / walk == pytest.approx(PM_SPRINT_SCALE, rel=0.05)


def test_monster_ai_moves_reach_the_published_frame(fio_session):
    session = fio_session(_arena()).start_play()
    session.step(4)
    _, _, _, start = _published(session)
    spawn = glm.vec3(*SPAWN)

    session.step(120)                                 # two seconds of play
    _, _, _, now = _published(session)
    monsters = [t for t in session.state.things if isinstance(t, Monster)]
    # The published rows are the monsters' authoritative positions.
    for monster in monsters:
        published = now[monster.properties["id"]]
        assert published == pytest.approx(tuple(float(v) for v in monster.pos), abs=1e-3)
    # The AI ran: in an open room in plain sight, the monsters close in.
    closer = [ident for ident in now
              if glm.distance(glm.vec3(*now[ident]), spawn)
              < glm.distance(glm.vec3(*start[ident]), spawn) - 50.0]
    assert closer, "no monster approached the player: %r -> %r" % (start, now)


def test_gun2_is_published_as_not_ready_while_it_cools_down(fio_session):
    """The published shot_ready gates Qt's click queue and the HUD; it must
    follow the combat runtime's own one-shot-per-second cooldown."""
    session = fio_session(_arena()).start_play()
    combat = session.logic.combat_runtime
    combat.active_weapon = "gun2"
    combat.player_ammo = 3
    session.step(1)
    with session.render_state() as frame:
        assert frame.shot_ready

    session.step(1, shoot=True)
    assert combat.player_ammo == 2, "the shot did not fire"
    with session.render_state() as frame:
        assert not frame.shot_ready, "gun2 published as ready mid-cooldown"

    combat._last_player_shot_time -= 1.0         # the second has passed
    session.step(1)
    with session.render_state() as frame:
        assert frame.shot_ready
