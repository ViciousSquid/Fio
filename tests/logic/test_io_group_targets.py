"""Groups as I/O targets: one signal, every member.

A connection whose target names a group -- a visgroup made in the Scene
Hierarchy, or a brush group -- delivers its input to every member, in scene
order, each exactly as if it had been the target. A member that does not
accept the input ignores it. These drive the real LogicThread with the real
light handlers.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                       # noqa: E402
from editor.editor_state import EditorState               # noqa: E402
from editor.io_system import (                            # noqa: E402
    GroupTarget, OutputConnection, PROBLEM_MISSING_TARGET, PROBLEM_UNKNOWN_INPUT,
    group_input_names, resolve_group_target, validate_connection,
    validate_scene_connections)
from editor.things import Light, Speaker                  # noqa: E402
from engine.io_groups import (                            # noqa: E402
    group_members, group_members_by_id, group_names, groups)
from engine.logic_thread import LogicThread               # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from engine.view_filters import ViewFilters               # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def scene():
    state = EditorState()
    button = box_brush("button", is_trigger=True)
    lamps = [make_thing(Light, f"lamp{i}", (i * 64, 0, 0), state='on') for i in range(3)]
    speaker = make_thing(Speaker, "radio", (0, 0, 128))
    outside = make_thing(Light, "porch", (512, 0, 0), state='on')
    state.brushes.append(button)
    state.things.extend(lamps + [speaker, outside])
    state.view_filters.add_visgroup("hall lights", lamps + [speaker])
    logic = LogicThread(ThreadedGameState(), state)
    logic.world_runtime.build_entity_caches()
    yield SimpleNamespace(state=state, logic=logic, button=button, lamps=lamps,
                          speaker=speaker, outside=outside)
    logic.stop()


def _wire(scene, target, input_name, **kw):
    io.add_connection(scene.button, OutputConnection(
        output_name="OnTrigger", target_name=target, input_name=input_name, **kw))


def _hall(scene):
    return scene.state.view_filters.visgroups[0]


def test_a_group_has_its_own_uuid_saved_with_the_map(scene):
    group = _hall(scene)
    assert len(group.id) == 36 and group.id.count('-') == 4
    saved = scene.state.view_filters.to_level_data()['visgroups'][0]
    assert saved['id'] == group.id
    again = ViewFilters()
    again.load_level_data({'visgroups': [saved]})
    assert again.visgroups[0].id == group.id
    # A map from before group ids: each group is given one.
    old = ViewFilters()
    old.load_level_data({'visgroups': [{'name': 'x', 'ids': []}]})
    assert old.visgroups[0].id


def test_the_group_uuid_is_the_target_and_survives_a_rename(scene):
    group = _hall(scene)
    _wire(scene, "hall lights", "TurnOff", target_id=group.id)
    scene.state.view_filters.rename_visgroup(group, "corridor")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['off'] * 3


def test_the_uuid_tells_apart_two_groups_with_one_name(scene):
    other = scene.state.view_filters.add_visgroup("hall lights", [scene.outside])
    _wire(scene, "hall lights", "TurnOff", target_id=other.id)
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert scene.outside.properties['state'] == 'off'
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['on'] * 3
    assert group_members_by_id(scene.state, other.id) == [scene.outside]
    assert (other.id, "hall lights") in groups(scene.state)


def test_an_entity_uuid_still_wins(scene):
    _wire(scene, "hall lights", "TurnOff", target_id=scene.outside.properties['id'])
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert scene.outside.properties['state'] == 'off'
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['on'] * 3


def test_turn_off_reaches_every_light_in_the_group(scene):
    _wire(scene, "hall lights", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")

    assert [lamp.properties['state'] for lamp in scene.lamps] == ['off'] * 3
    assert scene.outside.properties['state'] == 'on', "a light outside the group changed"


def test_a_member_without_the_input_ignores_it(scene):
    before = dict(scene.speaker.properties)
    _wire(scene, "hall lights", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")    # must not raise

    assert scene.speaker.properties == before


def test_toggle_and_generic_inputs_reach_the_group(scene):
    _wire(scene, "hall lights", "Toggle")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['off'] * 3

    io.get_connections(scene.button).clear()
    _wire(scene, "hall lights", "Hide")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    # Every member accepts the generic Hide, the speaker too.
    assert all(t.properties.get('hidden') for t in scene.lamps + [scene.speaker])


def test_a_delayed_signal_reaches_the_group_when_it_fires(scene):
    _wire(scene, "hall lights", "TurnOff", delay=0.5)
    manager = scene.logic.io_manager
    manager.fire_output(scene.button, "OnTrigger")
    assert scene.lamps[0].properties['state'] == 'on'
    manager.update(0.6)
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['off'] * 3


def test_each_member_fires_its_own_outputs(scene):
    """OnTurnedOff fires once per light: the members are real targets."""
    counter = make_thing(Light, "counter", (0, 0, -256), state='on')
    scene.state.things.append(counter)
    scene.logic.world_runtime.build_entity_caches()
    calls = []
    scene.logic.io_manager.register_input_handler(
        'light', 'setbrightness', lambda entity, p, logic: calls.append(p))
    for lamp in scene.lamps:
        io.add_connection(lamp, OutputConnection(
            output_name="OnTurnedOff", target_name="counter",
            input_name="SetBrightness", parameter=lamp.properties['name']))
    _wire(scene, "hall lights", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert calls == ["lamp0", "lamp1", "lamp2"]


def test_an_entity_name_is_never_shadowed_by_a_group(scene):
    scene.state.view_filters.add_visgroup("porch", scene.lamps)
    _wire(scene, "porch", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")

    assert scene.outside.properties['state'] == 'off'
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['on'] * 3


def test_a_brush_group_is_a_target_too(scene):
    walls = [box_brush(f"wall{i}", (i * 100, 0, 300), brush_group="walls")
             for i in range(2)]
    scene.state.brushes.extend(walls)
    scene.logic.world_runtime.build_entity_caches()
    _wire(scene, "walls", "Hide")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert all(wall.get('hidden') for wall in walls)


def test_play_mode_resolves_groups_the_same_way(scene):
    scene.logic.session_runtime.play_mode = True
    scene.logic.world_runtime.build_entity_caches()
    _wire(scene, "hall lights", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert [lamp.properties['state'] for lamp in scene.lamps] == ['off'] * 3


def test_an_unknown_name_is_still_reported_missing(scene, monkeypatch):
    logged = []
    monkeypatch.setattr(io, "debug_log", lambda kind, message: logged.append(message))
    _wire(scene, "no such group", "TurnOff")
    scene.logic.io_manager.fire_output(scene.button, "OnTrigger")
    assert any("not found" in message for message in logged)


# ---------------------------------------------------------------------------
# Membership, the standalone player, and validation
# ---------------------------------------------------------------------------

def test_members_come_in_scene_order_and_names_are_listed(scene):
    assert group_members(scene.state, "hall lights") == scene.lamps + [scene.speaker]
    assert group_members(scene.state, "nothing") is None
    assert "hall lights" in group_names(scene.state)


def test_the_saved_visgroups_are_groups_in_the_standalone_player():
    """The player has no editor: it loads the map's visgroups itself
    (player/world.py), and group membership reads them the same way."""
    lamps = [make_thing(Light, f"lamp{i}") for i in range(2)]
    data = {'visgroups': [{'id': 'bank-uuid', 'name': 'bank', 'visible': True,
                           'ids': [lamp.properties['id'] for lamp in lamps]}]}
    filters = ViewFilters()
    filters.load_level_data(data)
    world = SimpleNamespace(view_filters=filters, brushes=[], things=lamps)
    assert group_members(world, 'bank') == lamps
    assert group_members_by_id(world, 'bank-uuid') == lamps


def test_validation_accepts_a_group_and_judges_its_inputs(scene):
    group = resolve_group_target(scene.state, "hall lights")
    assert isinstance(group, GroupTarget)
    assert group.id == _hall(scene).id
    assert resolve_group_target(scene.state, "", group.id).name == "hall lights"
    assert "TurnOff" in group_input_names(group.members)

    ok = OutputConnection(output_name="OnTrigger", target_name="hall lights",
                          input_name="TurnOff")
    assert validate_connection(ok, scene.button, group) == []

    bad = OutputConnection(output_name="OnTrigger", target_name="hall lights",
                           input_name="Explode")
    codes = [code for code, _ in validate_connection(bad, scene.button, group)]
    assert codes == [PROBLEM_UNKNOWN_INPUT]


def test_scene_validation_resolves_group_targets(scene):
    _wire(scene, "renamed long ago", "TurnOff", target_id=_hall(scene).id)
    _wire(scene, "hall lights", "TurnOff")
    _wire(scene, "ghosts", "TurnOff")
    report = validate_scene_connections(
        scene.state.brushes, scene.state.things,
        find_group=lambda name, gid='': resolve_group_target(scene.state, name, gid))
    missing = [conn.target_name for _e, conn, code, _m in report
               if code == PROBLEM_MISSING_TARGET]
    assert missing == ["ghosts"]
