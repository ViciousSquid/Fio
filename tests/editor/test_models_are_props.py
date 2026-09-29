"""Every model in a Fio world is a Prop.

There is no separate ``Model`` entity. A model placed from the Asset Browser
or the 2D view is a Prop with ``render_mode='model'`` and a Prop's defaults
(neither solid nor carryable); a map written when the ``model`` type still
existed loads its models as Props that keep what they were -- solid scenery
the player does not pick up.
"""

import json
import os
import types

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from editor.things import ENTITY_CATEGORIES, ENTITY_TYPES, Prop, Thing  # noqa: E402

pytestmark = pytest.mark.qt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DRUM = "assets/models/Oil_Drum.obj"


def _legacy_record(**props):
    return {"type": "model", "pos": [1, 2, 3],
            "properties": {"type": "model", "name": "drum",
                           "model_path": DRUM, **props}}


def test_there_is_no_model_entity_type():
    assert "Model" not in ENTITY_TYPES
    assert all("Model" not in names for names in ENTITY_CATEGORIES.values())


def test_a_legacy_model_record_loads_as_a_solid_scenery_prop():
    thing = Thing.from_dict(_legacy_record())
    assert type(thing) is Prop
    props = thing.properties
    assert props["type"] == "prop"
    assert props["render_mode"] == "model"
    assert props["model_path"] == DRUM
    # What the Model entity was: it collided, and it was not carryable.
    assert props["no_collision"] is False
    assert props["carry_enabled"] is False
    assert thing.pos == [1, 2, 3]
    # And it saves as a Prop.
    assert thing.to_dict()["type"] == "prop"


def test_a_legacy_record_keeps_what_it_authored():
    thing = Thing.from_dict(_legacy_record(no_collision=True,
                                           scale=[2, 2, 2]))
    assert thing.properties["no_collision"] is True
    assert thing.properties["scale"] == [2, 2, 2]


def test_the_player_tier_reads_a_legacy_model_as_the_same_prop():
    """Without PyQt5 (the standalone player) the fallback base resolves it too."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('PyQt5', 'OpenGL'):\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import plugins.entitybase as eb\n"
        "from engine.prop_entity import Prop, EDITOR_TIER\n"
        "t = eb.Thing.from_dict({'type': 'model', 'pos': [0, 0, 0],"
        " 'properties': {'type': 'model', 'model_path': 'm.obj'}})\n"
        "print(EDITOR_TIER, type(t) is Prop, t.properties['render_mode'],"
        " t.properties['carry_enabled'], t.properties['no_collision'])"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == \
        "False True model False False"


@pytest.mark.parametrize("name", ["_SHOWCASE.json", "DevTest.json",
                                  "modeltest.json"])
def test_shipped_maps_load_their_models_as_props(name):
    with open(os.path.join(ROOT, "maps", name), encoding="utf-8") as handle:
        data = json.load(handle)
    legacy = [t for t in data.get("things", [])
              if str(t.get("type", "")).lower() == "model"]
    assert legacy, "the map no longer has legacy model records to check"
    for record in legacy:
        thing = Thing.from_dict(record)
        assert type(thing) is Prop
        assert thing.properties["render_mode"] == "model"
        assert thing.properties["model_path"] == \
            record["properties"].get("model_path")


def test_the_asset_browser_places_a_model_as_a_prop():
    from editor.main_window import MainWindow

    placed = []
    host = types.SimpleNamespace(
        root_dir=ROOT,
        save_state=lambda: None,
        state=types.SimpleNamespace(things=placed),
        set_selected_object=lambda obj: None,
        show_toast=lambda text: None,
    )
    MainWindow.add_model_to_scene(host, os.path.join(ROOT, DRUM),
                                  [0, 90, 0], [1, 1, 1])
    assert len(placed) == 1
    prop = placed[0]
    assert type(prop) is Prop
    assert prop.properties["render_mode"] == "model"
    assert prop.properties["model_path"] == DRUM
    assert prop.properties["rotation"] == [0, 90, 0]
    assert prop.properties["name"] == "Oil_Drum"
    # A Prop's defaults: neither solid nor carryable until the author says so.
    assert prop.properties["no_collision"] is True
    assert prop.properties["carry_enabled"] is False


def test_prop_for_model_is_the_one_model_factory():
    prop = Prop.for_model("assets\\models\\x.obj", pos=(4, 5, 6))
    assert prop.properties["model_path"] == "assets/models/x.obj"
    assert prop.pos == [4, 5, 6]
    assert prop.properties["render_mode"] == "model"


def test_resetting_collection_restores_the_authored_carry_setting():
    """Starting play used to force every Prop carryable, scenery included."""
    scenery = Prop.for_model(DRUM)
    scenery.reset_collection()
    assert scenery.properties["carry_enabled"] is False

    collected = Prop(properties={"collect_enabled": True,
                                 "collect_collected": True,
                                 "carry_enabled": True})
    assert collected.properties["carry_enabled"] is False
    collected.reset_collection()
    assert collected.properties["collect_collected"] is False
    assert collected.properties["carry_enabled"] is True


def test_io_written_against_a_model_still_shows_and_hides_it():
    """A Model's Enable/Disable hid and showed it; a Prop's do not, so the
    connections are re-aimed at Show/Hide when the old map loads."""
    from editor.editor_state import EditorState
    from editor.io_system import get_connections

    data = {
        "brushes": [],
        "things": [
            _legacy_record(id="drum-id"),
            {"type": "logic_relay", "pos": [0, 0, 0],
             "properties": {"type": "logic_relay", "name": "relay"},
             "io_connections": [
                 {"output": "OnTrigger", "target": "drum",
                  "target_id": "drum-id", "input": "Disable"},
                 {"output": "OnTrigger", "target": "drum", "input": "Enable"},
                 {"output": "OnTrigger", "target": "drum",
                  "input": "SetSkin", "parameter": "2"},
             ]},
        ],
    }
    state = EditorState()
    state.load_from_data(data, save_undo=False)
    relay = next(t for t in state.things if t.properties["name"] == "relay")
    inputs = [c.input_name for c in get_connections(relay)]
    assert inputs == ["Hide", "Show", "SetSkin"]
