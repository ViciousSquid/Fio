"""2.4.2 maps must load and save without losing entities (audit F1 / A3).

Runs the real EditorState load/save path. The fixture is an excerpt of the
2.4.2.1709 Tidy_Test.json: legacy ``tidyobject`` records plus a receptacle,
goal, light and player start.
"""
import copy
import json
import pathlib

import pytest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "tidy_2_4_2.json"


@pytest.fixture
def state(qt_app):
    from editor.editor_state import EditorState
    return EditorState()


@pytest.fixture
def qt_app():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _legacy():
    return json.loads(FIXTURE.read_text())


def test_2_4_2_tidy_objects_migrate_to_core_props(state):
    from engine.prop_entity import Prop
    legacy = _legacy()
    n_legacy = sum(t["type"] == "tidyobject" for t in legacy["things"])

    state.load_from_data(legacy, save_undo=False)

    assert len(state.things) == len(legacy["things"])
    props = [t for t in state.things if isinstance(t, Prop)]
    assert len(props) == n_legacy
    by_name = {p.properties["name"]: p for p in props}
    assert by_name["Book_1"].properties["tidy_category"] == "book"
    assert by_name["Book_1"].properties["model_path"] == "plugins/tidy/assets/book.obj"
    assert "tidied" not in by_name["Book_1"].properties
    assert by_name["Loose_1"].properties["tidy_category"] == "object"
    # Stable identity survives migration, so I/O targets by UUID still resolve.
    assert by_name["Loose_1"].properties["id"] == "00000000-0000-0000-0000-00000000c0de"


def test_migrated_map_saves_and_reloads_losslessly(state):
    state.load_from_data(_legacy(), save_undo=False)
    saved = json.loads(json.dumps(state.get_level_data()))

    assert not any(t["type"] == "tidyobject" for t in saved["things"])
    state.load_from_data(copy.deepcopy(saved), save_undo=False)
    again = json.loads(json.dumps(state.get_level_data()))
    key = lambda t: t["properties"]["id"]
    assert sorted(saved["things"], key=key) == sorted(again["things"], key=key)


def test_migration_is_idempotent():
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    once = get_manager().migrate_map_data(_legacy())
    twice = get_manager().migrate_map_data(copy.deepcopy(once))
    assert once == twice


def test_unknown_entity_types_are_preserved_not_dropped(state):
    from editor.things import UnresolvedThing
    record = {
        "type": "future_widget",
        "pos": [1.0, 2.0, 3.0],
        "properties": {"type": "future_widget", "name": "W", "id": "w-1",
                       "flag": "True", "nested": {"a": [1, 2]}},
        "io_connections": [{"output": "OnX", "target": "t", "input": "Y",
                            "parameter": "", "delay": 0.0, "fire_once": False,
                            "target_id": ""}],
    }
    level = {"version": 3, "brushes": [], "things": [copy.deepcopy(record)]}

    state.load_from_data(level, save_undo=False)
    assert len(state.things) == 1
    thing = state.things[0]
    assert isinstance(thing, UnresolvedThing)
    assert thing.unresolved_type == "future_widget"

    thing.pos = [9.0, 8.0, 7.0]
    saved = state.get_level_data()["things"][0]
    expected = dict(record, pos=[9.0, 8.0, 7.0])
    assert saved == expected  # verbatim, including the un-coerced "True" string

    clone = thing.duplicate(existing_names={"W"})
    assert clone.to_dict()["properties"]["id"] != "w-1"


def test_standalone_player_migrates_legacy_tidy_objects():
    from engine.prop_entity import Prop
    from player.plugin_host import PlayerPluginHost
    host = PlayerPluginHost()
    assert host.load(package=None) is True
    legacy = _legacy()
    host.build_and_start(legacy)
    try:
        props = [t for t in host.things if isinstance(t, Prop)]
        assert len(props) == 3
        assert all(p.properties["tidy_category"] for p in props)
    finally:
        host.stop()
