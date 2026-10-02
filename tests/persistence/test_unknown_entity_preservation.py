"""An entity this build cannot resolve must survive a load/save round trip.

Not backwards compatibility -- 2.5 is a clean break and carries no migrations
for older formats. This is the forward-looking case: a map that names an entity
type whose plugin is missing, disabled, or simply newer than this build. The
record is kept verbatim and written back unchanged, so opening a map without a
plugin installed cannot quietly delete that plugin's content.

Runs the real EditorState load/save path.
"""
import copy

import pytest


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


