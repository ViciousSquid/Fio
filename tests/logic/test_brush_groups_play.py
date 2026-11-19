"""Brush groups in Play: grouped brushes ride their group's mover or door."""
import math

import pytest

from engine import brush_placement

pytestmark = pytest.mark.qt


def _level(leader, *others):
    floor = {"id": "floor", "name": "floor", "pos": [0, -8, 0], "size": [2048, 16, 2048]}
    return {
        "version": 5,
        "brushes": [floor, leader, *others],
        "things": [{"type": "playerstart", "pos": [0, 40, 600],
                    "properties": {"type": "player_start", "name": "start",
                                   "id": "ps-1", "primary": True}}],
    }


def _brush(session, name):
    return next(b for b in session.state.brushes if b.get("name") == name)


def _centre(brush):
    return brush_placement.placed_box(brush)[:3]


def _mover(**extra):
    brush = {"id": "lift", "name": "lift", "pos": [0, 100, 0], "size": [128, 16, 128],
             "is_mover": True, "start_on": True, "direction": [0, 1, 0],
             "distance": 200.0, "speed": 100.0, "brush_group": "lift"}
    brush.update(extra)
    return brush


def _glass(**extra):
    brush = {"id": "glass", "name": "glass", "pos": [40, 130, 0], "size": [16, 32, 16],
             "brush_group": "lift"}
    brush.update(extra)
    return brush


def test_a_grouped_brush_rides_its_group_s_mover(fio_session, monkeypatch):
    session = fio_session(_level(_mover(), _glass()))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    lift, glass = _brush(session, "lift"), _brush(session, "glass")
    session.start_play()
    session.step(20)

    moved = _centre(lift)
    assert moved[1] > 101.0, "the mover should have risen"
    assert _centre(glass) == pytest.approx(
        (moved[0] + 40.0, moved[1] + 30.0, moved[2])), (
        "the grouped brush did not keep its place on the mover")
    assert glass["pos"] == [40, 130, 0], "play moved the authored placement"
    session.stop_play()


def test_a_brush_in_no_group_or_another_group_stays_put(fio_session, monkeypatch):
    session = fio_session(_level(_mover(), _glass(brush_group="other"),
                                 _glass(id="loose", name="loose", brush_group="")))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    session.start_play()
    session.step(20)

    assert _centre(_brush(session, "glass")) == pytest.approx((40.0, 130.0, 0.0))
    assert _centre(_brush(session, "loose")) == pytest.approx((40.0, 130.0, 0.0))
    assert not _brush(session, "glass").get("is_mover")
    session.stop_play()


def test_a_grouped_brush_turns_with_a_rotating_mover(fio_session, monkeypatch):
    session = fio_session(_level(
        _mover(rotate=True, rot_axis=[0, 1, 0], speed=90.0, distance=0.0),
        _glass(pos=[100, 100, 0])))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    lift, glass = _brush(session, "lift"), _brush(session, "glass")
    session.start_play()
    session.step(15)

    spin = brush_placement.runtime_spin(lift)
    assert spin is not None and spin[3], "the mover should be turning"
    turn = brush_placement.rotation_matrix(spin[:3], spin[3])
    expected = tuple(_centre(lift)[i] + turn[i][0] * 100.0 for i in range(3))
    assert _centre(glass) == pytest.approx(expected, abs=1e-3), (
        "the grouped brush did not orbit the mover's centre")
    assert brush_placement.runtime_spin(glass)[3] == pytest.approx(spin[3]), (
        "the grouped brush did not turn with the mover")
    assert math.hypot(_centre(glass)[0], _centre(glass)[2]) == pytest.approx(100.0, abs=1e-3)
    session.stop_play()


def test_a_grouped_brush_rides_a_door(fio_session, monkeypatch):
    door = {"id": "door", "name": "door", "pos": [0, 64, 0], "size": [128, 128, 16],
            "is_door": True, "door_direction": "up", "door_distance": 120.0,
            "door_speed": 200.0, "brush_group": "front door"}
    glass = {"id": "glass", "name": "glass", "pos": [0, 90, 0], "size": [64, 32, 4],
             "brush_group": "front door"}
    session = fio_session(_level(door, glass))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    door, glass = _brush(session, "door"), _brush(session, "glass")
    session.start_play()
    session.step(2)
    runtime = session.logic.mover_runtime
    index = next(i for i, b in runtime.doors if b is door)
    runtime._trigger_door_open(index, door)
    session.step(30)

    opened = _centre(door)
    assert opened[1] > 100.0, "the door should have opened"
    assert _centre(glass) == pytest.approx((opened[0], opened[1] + 26.0, opened[2])), (
        "the glass was left behind by its door")
    session.stop_play()


def test_stopping_play_leaves_the_grouped_brush_as_it_was_authored(
        fio_session, monkeypatch):
    session = fio_session(_level(_mover(), _glass()))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    config = session.window.config
    if not config.has_section("Settings"):
        config.add_section("Settings")
    # The world is not restored on Stop: what play wrote must be undone.
    config.set("Settings", "restore_world_on_stop", "False")
    before = dict(_brush(session, "glass"))
    session.start_play()
    session.step(10)
    assert _brush(session, "glass").get("is_mover"), "followers are mover rows in play"
    session.stop_play()

    glass = _brush(session, "glass")
    after = {k: v for k, v in glass.items() if not k.startswith("_")}
    assert after == {k: v for k, v in before.items() if not k.startswith("_")}
    assert _centre(glass) == pytest.approx((40.0, 130.0, 0.0))
    session.window.unsaved_changes = False


def _hollow_box(centre=(0.0, 200.0, 0.0), half=64.0, wall=8.0):
    """Six slabs making a hollow box; the first is a rotating mover."""
    slabs = []
    for axis in range(3):
        for sign in (-1, 1):
            pos = list(centre)
            pos[axis] += sign * (half - wall / 2)
            size = [2 * half] * 3
            size[axis] = wall
            slabs.append({"id": "slab%d" % len(slabs), "name": "slab%d" % len(slabs),
                          "pos": pos, "size": size, "brush_group": "crate"})
    slabs[0].update(is_mover=True, start_on=True, rotate=True, rot_axis=[0, 1, 0],
                    speed=75.0, distance=0.0)
    return slabs


def test_a_hollow_box_of_grouped_brushes_turns_as_one_rigid_body(
        fio_session, monkeypatch):
    """Every slab is carried by the one rotation about the leader's centre:
    the walls stay square to one another and keep meeting at the corners."""
    import numpy as np

    slabs = _hollow_box()
    session = fio_session(_level(*slabs))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    brushes = [_brush(session, "slab%d" % i) for i in range(6)]
    homes = [np.array(brush_placement.authored_box(b)[:3]) for b in brushes]
    session.start_play()
    for _ in range(6):                       # checked at several angles
        session.step(7)
        spin = brush_placement.runtime_spin(brushes[0])
        turn = np.array(brush_placement.rotation_matrix(spin[:3], spin[3]))
        lead_now = np.array(_centre(brushes[0]))
        for brush, home in zip(brushes[1:], homes[1:]):
            expected = lead_now + turn @ (home - homes[0])
            assert np.allclose(_centre(brush), expected, atol=1e-6), (
                "a wall drifted out of the box")
            assert brush_placement.runtime_spin(brush) == pytest.approx(spin), (
                "a wall is not turned by the box's rotation")
    session.stop_play()


def test_a_player_standing_on_a_carried_brush_is_carried(fio_session, monkeypatch):
    """A deck grouped with a sideways mover: the player rides the deck."""
    engine_ = {"id": "engine", "name": "engine", "pos": [0, 300, 0], "size": [32, 32, 32],
               "is_mover": True, "start_on": True, "direction": [1, 0, 0],
               "distance": 400.0, "speed": 100.0, "brush_group": "ferry"}
    deck = {"id": "deck", "name": "deck", "pos": [0, 100, 600], "size": [256, 16, 256],
            "brush_group": "ferry"}
    session = fio_session(_level(engine_, deck))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    deck = _brush(session, "deck")
    session.start_play()
    player = session.logic.player_runtime.player
    player.pos.x, player.pos.y, player.pos.z = 0.0, 160.0, 600.0
    session.step(40)                               # land on the deck
    assert player.ground_object is deck, "the test needs the player on the deck"
    start_x, deck_x = float(player.pos.x), _centre(deck)[0]

    session.step(60)

    travelled = _centre(deck)[0] - deck_x
    assert travelled > 20.0, "the deck should have been carried sideways"
    assert float(player.pos.x) - start_x == pytest.approx(travelled, abs=2.0), (
        "the player was left behind by the deck they stand on")
    assert player.ground_object is deck
    session.stop_play()


# ---------------------------------------------------------------------------
# Entities in a group ride its mover or door too
# ---------------------------------------------------------------------------

def _lamp(name="lamp", pos=(40, 150, 0), group="lift", **props):
    properties = {"type": "light", "name": name, "id": "light-" + name,
                  "colour": [255, 200, 150]}
    if group is not None:
        properties["brush_group"] = group
    properties.update(props)
    return {"type": "light", "pos": list(pos), "properties": properties}


def _with_things(level, *things):
    level["things"].extend(things)
    return level


def _thing(session, name):
    return next(t for t in session.state.things if t.properties.get("name") == name)


def test_a_grouped_entity_rides_its_group_s_mover(fio_session, monkeypatch):
    session = fio_session(_with_things(_level(_mover()), _lamp()))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    lift, lamp = _brush(session, "lift"), _thing(session, "lamp")
    session.start_play()
    session.step(20)

    moved = _centre(lift)
    assert moved[1] > 101.0, "the mover should have risen"
    assert list(lamp.pos) == pytest.approx(
        [moved[0] + 40.0, moved[1] + 50.0, moved[2]]), (
        "the grouped entity did not keep its place on the mover")
    session.stop_play()
    assert list(lamp.pos) == pytest.approx([40.0, 150.0, 0.0]), (
        "stopping play did not put the entity back where it was authored")
    assert "_group_home" not in lamp.properties


def test_an_entity_in_no_group_or_another_group_stays_put(fio_session, monkeypatch):
    session = fio_session(_with_things(
        _level(_mover()),
        _lamp("loose", pos=(-40, 150, 0), group=None),
        _lamp("other", pos=(0, 150, 40), group="elsewhere")))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    session.start_play()
    session.step(20)
    assert _centre(_brush(session, "lift"))[1] > 101.0
    assert list(_thing(session, "loose").pos) == pytest.approx([-40.0, 150.0, 0.0])
    assert list(_thing(session, "other").pos) == pytest.approx([0.0, 150.0, 40.0])
    session.stop_play()


def test_a_grouped_entity_orbits_with_a_rotating_mover(fio_session, monkeypatch):
    session = fio_session(_with_things(
        _level(_mover(rotate=True, rot_axis=[0, 1, 0], speed=90.0, distance=0.0)),
        _lamp(pos=(40, 130, 0))))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    lift, lamp = _brush(session, "lift"), _thing(session, "lamp")
    session.start_play()
    session.step(20)

    spin = brush_placement.runtime_spin(lift)
    assert spin is not None and abs(float(spin[3])) > 1.0, "the mover should have turned"
    centre = _centre(lift)
    turn = brush_placement.rotation_matrix(spin[:3], float(spin[3]))
    expected = [centre[i] + turn[i][0] * 40.0 + turn[i][1] * 30.0 for i in range(3)]
    assert list(lamp.pos) == pytest.approx(expected, abs=1e-3), (
        "the grouped entity did not turn about the mover's centre")
    radius = math.hypot(lamp.pos[0] - centre[0], lamp.pos[2] - centre[2])
    assert radius == pytest.approx(40.0, abs=1e-3)
    session.stop_play()
    assert list(lamp.pos) == pytest.approx([40.0, 130.0, 0.0])


def test_a_grouped_entity_rides_a_door(fio_session, monkeypatch):
    door = {"id": "door", "name": "door", "pos": [0, 64, 0], "size": [128, 128, 16],
            "is_door": True, "door_direction": "up", "door_distance": 120.0,
            "door_speed": 200.0, "brush_group": "front door"}
    session = fio_session(_with_things(
        _level(door), _lamp(pos=(0, 140, 20), group="front door")))
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    door, lamp = _brush(session, "door"), _thing(session, "lamp")
    session.start_play()
    session.step(2)
    runtime = session.logic.mover_runtime
    index = next(i for i, b in runtime.doors if b is door)
    runtime._trigger_door_open(index, door)
    session.step(30)

    opened = _centre(door)
    assert opened[1] > 100.0, "the door should have opened"
    assert list(lamp.pos) == pytest.approx([opened[0], opened[1] + 76.0, opened[2] + 20.0]), (
        "the grouped entity was left behind by its door")
    session.stop_play()
    assert list(lamp.pos) == pytest.approx([0.0, 140.0, 20.0])


def test_an_entity_parented_to_a_mover_is_left_to_its_parent(fio_session, monkeypatch):
    from engine.logic_groups import carried_entities
    session = fio_session(_with_things(
        _level(_mover()), _lamp(parent_mover="lift"), _lamp("rider", pos=(0, 150, 40))))
    names = sorted(t.properties["name"] for t, _l in carried_entities(
        session.state.brushes, session.state.things))
    assert names == ["rider"]
