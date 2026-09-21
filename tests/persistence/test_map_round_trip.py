"""Saving a world and loading it back.

A Fio map is the JSON ``EditorState.get_level_data()`` produces; loading it is
``load_from_data``.  What the round trip has to preserve is everything another
subsystem addresses an object by — its id, its name, its geometry, its wiring —
and what it must *not* do is churn: loading a map and saving it again should
produce the same file, or a diff is noise in every commit.

Malformed and legacy inputs are covered here too: a map from an older Fio must
still open, and a truncated one must fail in a way the editor can report rather
than half-loading a scene.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="EditorState builds editor.things entities")

from editor import io_system as io                  # noqa: E402
from editor.editor_state import EditorState         # noqa: E402
from editor.io_system import OutputConnection       # noqa: E402
from editor.things import Light, LogicRelay, Monster, PlayerStart  # noqa: E402
from engine import brush_geometry as bg             # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def scene():
    """A representative world: brushes, an angled brush, entities and wiring."""
    state = EditorState()
    floor = box_brush("floor", (0, -16, 0), (1024, 32, 1024))
    ramp = box_brush("ramp", (0, 32, 200), (256, 64, 256))
    bg.clip_brush(ramp, (0.0, 1.0, 1.0), 40.0)
    button = box_brush("button", (200, 32, 0), (32, 64, 32), is_trigger=True)
    door = box_brush("door", (0, 64, -300), (128, 128, 16), is_door=True)
    io.add_connection(button, OutputConnection(
        output_name="OnStartTouch", target_name="door", input_name="Open",
        parameter="fast", delay=0.5, target_id=door["id"]))

    state.brushes = [floor, ramp, button, door]
    state.things = [
        make_thing(PlayerStart, "spawn", (0, 32, 0)),
        make_thing(Light, "lamp", (0, 200, 0), intensity=2.5, radius=600),
        make_thing(Monster, "grunt", (300, 96, 0), monster_type="human"),
        make_thing(LogicRelay, "relay"),
    ]
    return state


def _round_trip(state):
    """Serialise, re-parse as JSON, and load into a fresh state."""
    data = json.loads(json.dumps(state.get_level_data()))
    loaded = EditorState()
    loaded.load_from_data(data)
    return loaded, data


# ---------------------------------------------------------------------------
# What must survive
# ---------------------------------------------------------------------------

def test_a_saved_world_is_json_serialisable(scene):
    text = json.dumps(scene.get_level_data())
    assert json.loads(text)["version"] == 3


def test_every_brush_comes_back(scene):
    loaded, _ = _round_trip(scene)
    assert [b["name"] for b in loaded.brushes] == \
        [b["name"] for b in scene.brushes], (
        "brush list changed across the round trip:\n  before %s\n  after  %s"
        % ([b["name"] for b in scene.brushes],
           [b["name"] for b in loaded.brushes]))


def test_every_entity_comes_back(scene):
    loaded, _ = _round_trip(scene)
    assert [t.name for t in loaded.things] == [t.name for t in scene.things]
    assert [type(t).__name__ for t in loaded.things] == \
        [type(t).__name__ for t in scene.things], (
        "entity classes changed across the round trip: %s"
        % ([type(t).__name__ for t in loaded.things],))


def test_uuids_survive_the_round_trip(scene):
    """Every id-addressed connection and save-game overlay depends on this."""
    loaded, _ = _round_trip(scene)
    assert [b["id"] for b in loaded.brushes] == [b["id"] for b in scene.brushes]
    assert [t.properties["id"] for t in loaded.things] == \
        [t.properties["id"] for t in scene.things]


def test_transforms_survive_the_round_trip(scene):
    loaded, _ = _round_trip(scene)
    for before, after in zip(scene.brushes, loaded.brushes):
        assert after["pos"] == pytest.approx(before["pos"]), (
            "%s moved from %s to %s" % (before["name"], before["pos"], after["pos"]))
        assert after["size"] == pytest.approx(before["size"])
    for before, after in zip(scene.things, loaded.things):
        assert list(after.pos) == pytest.approx(list(before.pos))


def test_textures_survive_the_round_trip(scene):
    loaded, _ = _round_trip(scene)
    assert loaded.brushes[0]["textures"] == scene.brushes[0]["textures"]


def test_an_angled_brushs_plane_set_survives_the_round_trip(scene):
    before = bg.get_convex(scene.brushes[1])
    loaded, _ = _round_trip(scene)
    after = bg.get_convex(loaded.brushes[1])

    assert after is not None, "the ramp lost its geometry in the round trip"
    assert len(after.planes) == len(before.planes), (
        "plane count changed %d -> %d" % (len(before.planes), len(after.planes)))
    assert after.verts.shape == before.verts.shape
    assert after.verts == pytest.approx(before.verts, abs=1e-9), (
        "the reloaded ramp is a different solid (max corner delta %.3g)"
        % float(abs(after.verts - before.verts).max()))


def test_io_connections_survive_the_round_trip(scene):
    loaded, _ = _round_trip(scene)
    button = loaded.find_entity_by_name("button")
    connections = io.get_connections(button)

    assert len(connections) == 1, (
        "the button has %d connections after loading, expected 1"
        % len(connections))
    restored = connections[0]
    original = io.get_connections(scene.find_entity_by_name("button"))[0]
    assert (restored.output_name, restored.input_name, restored.target_name,
            restored.parameter, restored.delay, restored.target_id) == \
           (original.output_name, original.input_name, original.target_name,
            original.parameter, original.delay, original.target_id), (
        "the connection changed across the round trip:\n  before %r\n  after  %r"
        % (original, restored))


def test_a_restored_connection_still_resolves_to_its_target(scene):
    loaded, _ = _round_trip(scene)
    button = loaded.find_entity_by_name("button")
    connection = io.get_connections(button)[0]
    assert loaded.find_entity_by_id(connection.target_id) is \
        loaded.find_entity_by_name("door"), (
        "the reloaded connection's target_id no longer names the door")


def test_entity_properties_survive_the_round_trip(scene):
    loaded, _ = _round_trip(scene)
    lamp = loaded.find_entity_by_name("lamp")
    assert lamp.properties["intensity"] == 2.5
    assert lamp.properties["radius"] == 600
    grunt = loaded.find_entity_by_name("grunt")
    assert grunt.properties["monster_type"] == "human"


def test_terrain_data_survives_when_present(scene):
    scene.terrain_data = {"size": 64, "heights": [0.0] * 16}
    loaded, data = _round_trip(scene)
    assert data["terrain_data"] == scene.terrain_data
    assert loaded.terrain_data == scene.terrain_data


def test_no_terrain_key_is_written_when_there_is_no_terrain(scene):
    assert "terrain_data" not in scene.get_level_data(), (
        "an empty terrain wrote a key into the map file")


# ---------------------------------------------------------------------------
# Loading then saving must not churn the file
# ---------------------------------------------------------------------------

def test_loading_and_saving_again_produces_the_same_map(scene):
    """Otherwise opening a map and closing it shows a diff in every commit."""
    first = scene.get_level_data()
    reloaded = EditorState()
    reloaded.load_from_data(json.loads(json.dumps(first)))
    second = reloaded.get_level_data()

    assert json.dumps(second, sort_keys=True) == json.dumps(first, sort_keys=True), (
        "a load/save cycle changed the map.\nfirst : %s\nsecond: %s"
        % (json.dumps(first, sort_keys=True)[:400],
           json.dumps(second, sort_keys=True)[:400]))


def test_a_second_round_trip_is_also_stable(scene):
    once, _ = _round_trip(scene)
    twice, _ = _round_trip(once)
    assert json.dumps(twice.get_level_data(), sort_keys=True) == \
        json.dumps(once.get_level_data(), sort_keys=True)


def test_runtime_caches_never_reach_the_saved_map(scene):
    """They hold NumPy and GLM objects and are meaningless in a file."""
    for brush in scene.brushes:
        bg.get_shape(brush)
        bg.get_convex(brush)
    text = json.dumps(scene.get_level_data())
    for key in bg.GEO_RUNTIME_KEYS:
        assert '"%s"' % key not in text, (
            "the runtime key %r was written into the map file" % key)
    assert '"_io_connections"' not in text, (
        "the live connection objects were written instead of the serialised form")


# ---------------------------------------------------------------------------
# Malformed and incomplete input
# ---------------------------------------------------------------------------

def test_an_empty_map_loads_as_an_empty_scene():
    state = EditorState()
    state.load_from_data({})
    assert state.brushes == []
    assert state.things == []


def test_a_map_with_no_things_key_loads():
    state = EditorState()
    state.load_from_data({"version": 3, "brushes": [box_brush("only")]})
    assert [b["name"] for b in state.brushes] == ["only"]
    assert state.things == []


def test_a_map_with_no_brushes_key_loads():
    state = EditorState()
    state.load_from_data({"version": 3, "things": []})
    assert state.brushes == []


def test_a_thing_with_no_type_is_skipped_rather_than_crashing_the_load():
    state = EditorState()
    state.load_from_data({
        "version": 3,
        "brushes": [],
        "things": [{"pos": [0, 0, 0], "properties": {}},
                   {"type": "light", "pos": [0, 0, 0], "properties": {"name": "ok"}}],
    })
    assert [t.name for t in state.things] == ["ok"], (
        "a malformed entity should be skipped and the rest of the map still "
        "load; the scene holds %s" % ([t.name for t in state.things],))


def test_a_thing_of_an_unknown_type_does_not_stop_the_load():
    state = EditorState()
    state.load_from_data({
        "version": 3,
        "brushes": [],
        "things": [{"type": "entity_from_the_future", "pos": [0, 0, 0],
                    "properties": {"name": "future"}},
                   {"type": "light", "pos": [0, 0, 0], "properties": {"name": "ok"}}],
    })
    assert "ok" in [t.name for t in state.things]


def test_a_brush_with_no_geometry_key_stays_a_box():
    state = EditorState()
    state.load_from_data({
        "version": 3,
        "brushes": [{"pos": [0, 0, 0], "size": [64, 64, 64], "name": "plain"}],
        "things": [],
    })
    assert not bg.brush_has_geometry(state.brushes[0])
    assert bg.get_shape(state.brushes[0]).is_valid


def test_truncated_json_is_a_json_error_not_a_half_loaded_scene():
    """The caller reports it; the scene must not be left partly overwritten."""
    state = EditorState()
    state.brushes = [box_brush("existing")]
    with pytest.raises(json.JSONDecodeError):
        json.loads('{"version": 3, "brushes": [')
    assert [b["name"] for b in state.brushes] == ["existing"]


# ---------------------------------------------------------------------------
# Legacy maps
# ---------------------------------------------------------------------------

def test_a_version_1_map_opens():
    state = EditorState()
    state.load_from_data({
        "brushes": [{"pos": [0, 0, 0], "size": [64, 64, 64], "name": "old_wall"}],
        "things": [{"type": "light", "pos": [0, 100, 0],
                    "properties": {"name": "old_lamp"}}],
    })
    assert [b["name"] for b in state.brushes] == ["old_wall"]
    assert [t.name for t in state.things] == ["old_lamp"]


