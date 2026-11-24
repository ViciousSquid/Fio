"""Rotate and carry state across a real Play session (Reel_To_Reel_Test).

Three things the session got wrong:

* a rotating model stood still through a cutscene, the death screen and the
  level-complete screen (it was stepped with the player's interaction, after
  the tick returned early for those), while movers and light paths went on;
* a save game did not carry how far a model had turned, so a valve opened
  after the save stayed open -- its solid collision with it -- once the save
  was loaded;
* a model carried and turned with the player kept that rotation after Stop
  when the world is not restored on leaving Play: only its place was put back.
"""

import copy

import pytest

pytest.importorskip("PyQt5")

from engine.prop_runtime import _forward            # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "maps/Reel_To_Reel_Test.json"


def _things(session):
    return {thing.properties.get("name"): thing for thing in session.state.things}


def _turned(angle_before, angle_after):
    return (angle_after - angle_before) % 360.0


def test_a_model_keeps_turning_while_the_player_is_dead(fio_session):
    session = fio_session(MAP)
    reel = _things(session)["Wide_Reel_Supply"]         # continuous, from the start
    session.start_play()
    session.step(30)
    start = reel._rotate_angle
    session.step(30)
    alive = _turned(start, reel._rotate_angle)
    assert alive > 1.0

    session.logic.player_runtime.player_dead = True
    start = reel._rotate_angle
    session.step(30)
    assert _turned(start, reel._rotate_angle) == pytest.approx(alive, rel=1e-3)

    session.logic.player_runtime.player_dead = False
    session.logic.interaction_runtime.level_complete_ui = True
    start = reel._rotate_angle
    session.step(30)
    assert _turned(start, reel._rotate_angle) == pytest.approx(alive, rel=1e-3)


def test_a_loaded_save_puts_a_turned_model_back_as_it_was_saved(fio_session, tmp_path):
    session = fio_session(MAP)
    valve = _things(session)["Tall_Reel_Supply"]
    valve.properties.update(rotate_mode="turn", rotate_angle=90.0, rotate_speed=90.0,
                            rotate_autostart=False)
    spinning = _things(session)["Wide_Reel_Supply"]
    session.start_play()
    session.step(30)
    logic, props = session.logic, session.logic.prop_runtime
    assert valve._rotate_angle == 0.0 and not props._motion(valve).running
    spin_at_save = spinning._rotate_angle

    path = str(tmp_path / "turned.fiosave")
    assert logic.session_runtime.save_session(path, map_name="Reel")[0]

    props.start_rotate(valve)                           # opened after the save
    session.step(120)
    assert valve._rotate_angle == pytest.approx(90.0)

    assert logic.session_runtime.load_session(path, map_name="Reel")[0]
    assert valve._rotate_angle == pytest.approx(0.0)
    assert props._motion(valve).turned == 0.0 and not props._motion(valve).running
    assert spinning._rotate_angle == pytest.approx(spin_at_save)
    session.step(5)
    assert valve._rotate_angle == pytest.approx(0.0), "and it stays shut"

    # The other way round: saved open, shut afterwards, loaded open.
    props.start_rotate(valve)
    session.step(120)
    assert logic.session_runtime.save_session(path, map_name="Reel")[0]
    props.stop_rotate(valve)
    session.step(120)
    assert valve._rotate_angle == pytest.approx(0.0)
    assert logic.session_runtime.load_session(path, map_name="Reel")[0]
    assert valve._rotate_angle == pytest.approx(90.0)
    assert props._motion(valve).running


def test_a_carried_model_is_placed_and_faced_as_authored_after_stop(fio_session):
    session = fio_session(MAP)
    session.window.config.set("Settings", "restore_world_on_stop", "False")
    assert not session.window._restores_world_on_stop()
    carried = _things(session)["Tall_Reel_Takeup"]
    carried.properties.update(rotate_enabled=False, carry_enabled=True, render_mode="model")
    rotation, place = copy.deepcopy(carried.properties.get("rotation")), list(carried.pos)

    session.start_play()
    session.step(30)
    logic = session.logic
    player = logic.player_runtime.player
    forward = _forward(player)
    eye = player.pos.y + float(getattr(player, "camera_height", 40.0))
    carried.pos = [player.pos.x + forward[0] * 60, eye + forward[1] * 60,
                   player.pos.z + forward[2] * 60]
    logic.prop_runtime.moved(carried)
    session.step(1, use=True)
    assert logic.prop_runtime.held is carried
    session.step(60, mouse=(4.0, 0.0))                  # the player turns; it turns too
    assert carried.properties["rotation"] != rotation

    session.stop_play()                                 # still in the player's hands
    live = next(thing for thing in session.state.things
                if thing.properties.get("id") == carried.properties.get("id"))
    assert live.properties.get("rotation") == rotation
    assert list(live.pos) == place
    assert "_prop_home_rotation" not in live.properties
    assert "_prop_home_pos" not in live.properties
    assert live._carry_sprite_yaw is None and getattr(live, "_carry_turn", None) is None
