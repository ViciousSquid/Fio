"""A malformed map is refused at load, before it can reach the simulation.

Maps are shareable input. Each shape below came from mutating the shipped
maps: every one used to load and then raise as Play started (or inside a
tick), from code that indexes these fields without checking them.
"""

import copy

import pytest

pytest.importorskip("PyQt5", reason="EditorState builds editor Things")

from editor.editor_state import EditorState  # noqa: E402

pytestmark = pytest.mark.qt

BRUSH = {"id": "b1", "name": "wall", "pos": [0, 0, 0], "size": [64, 64, 64],
         "textures": {"north": "brick.png"}}
LIGHT = {"type": "light", "pos": [0, 64, 0],
         "properties": {"type": "light", "name": "lamp", "id": "l1",
                        "colour": [255, 200, 150]}}


def _level(brush=None, thing=None):
    return {"version": 3,
            "brushes": [brush if brush is not None else copy.deepcopy(BRUSH)],
            "things": [thing if thing is not None else copy.deepcopy(LIGHT)]}


def _brush(**changes):
    brush = copy.deepcopy(BRUSH)
    for key, value in changes.items():
        if value is _DROP:
            brush.pop(key, None)
        else:
            brush[key] = value
    return brush


def _light(**changes):
    light = copy.deepcopy(LIGHT)
    light["properties"].update(changes)
    return light


_DROP = object()

MALFORMED = {
    "brush without a size": _level(brush=_brush(size=_DROP)),
    "brush pos is None": _level(brush=_brush(pos=None)),
    "brush pos holds NaN": _level(brush=_brush(pos=[0, float("nan"), 0])),
    "brush size holds a string": _level(brush=_brush(size=[64, "64", 64])),
    "textures is a string": _level(brush=_brush(textures="brick.png")),
    "uv_scale is a list": _level(brush=_brush(uv_scale=[1, 1])),
    "geometry is a number": _level(brush=_brush(geometry=3)),
    "door_distance is a list": _level(brush=_brush(door_distance=[128])),
    "speed is a bool": _level(brush=_brush(speed=True)),
    "brush name is a list": _level(brush=_brush(name=["wall"])),
    "connections hold strings": _level(brush=_brush(io_connections=["OnTrigger"])),
    "thing properties is a list": _level(thing={"type": "light", "pos": [0, 0, 0],
                                                "properties": []}),
    "thing id is a number": _level(thing=_light(id=7)),
    "light colour is an object": _level(thing=_light(colour={"r": 255})),
    "light colour is too short": _level(thing=_light(colour=[255, 0])),
}


@pytest.mark.parametrize("level", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_map_is_refused_and_the_scene_is_kept(level):
    state = EditorState()
    state.load_from_data(_level(), save_undo=False)
    before = (list(state.brushes), list(state.things))

    with pytest.raises(ValueError):
        state.load_from_data(level, save_undo=False)

    assert (state.brushes, state.things) == before


def test_a_well_formed_map_still_loads():
    state = EditorState()
    state.load_from_data(_level(), save_undo=False)
    assert [b["id"] for b in state.brushes] == ["b1"]
    assert [t.properties["name"] for t in state.things] == ["lamp"]
