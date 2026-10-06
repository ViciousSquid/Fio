"""Malformed maps and saves are refused before they reach the world.

Core tier: ``engine.level_validation`` has no Qt dependency, so these run in
the fast CI job. Each malformed shape came from fuzzing the shipped maps and
real saves; every one used to load and then fault a tick, or stop Play from
being left.
"""

import copy
import json

import pytest

from engine import savegame
from engine.level_validation import validate_level, validate_snapshot

BRUSH = {"id": "b1", "name": "wall", "pos": [0, 0, 0], "size": [64, 64, 64],
         "textures": {"north": "brick.png"}}
THING = {"type": "prop", "pos": [0, 40, 0],
         "properties": {"type": "prop", "name": "crate", "id": "p1",
                        "_prop_home_pos": [0, 40, 0]}}
SAVE = {
    "fio_savegame": True, "save_version": savegame.SAVE_VERSION,
    "save_mode": "full", "map": "m.json",
    "level": {"version": 3, "brushes": [BRUSH], "things": [THING]},
    "player": {"pos": [1.0, 2.0, 3.0], "angle": 0.5, "pitch": 0.0,
               "velocity": [0.0, 0.0, 0.0], "camera_height": 40.0},
    "player2": None,
    "runtime": {"player_health": 80, "player_ammo": 3, "active_weapon": "gun1",
                "camera_mode": "First Person", "collected_keys": ["blue_key"],
                "door_states": {"0": {"progress": 0.5, "state": "opening",
                                      "direction": [0, 1, 0]}},
                "mover_path_states": {"1": {"lerp_t": 0.2, "wait_remaining": 0.0,
                                            "waiting": False, "current_node": "n0",
                                            "origin": [0, 0, 0]}}},
}


def _with(document, path, value):
    document = copy.deepcopy(document)
    node = document
    for key in path[:-1]:
        node = node[key]
    if value is _DROP:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return document


_DROP = object()
LEVEL = SAVE["level"]

BAD_LEVELS = {
    "brush without size": (("brushes", 0, "size"), _DROP),
    "brush pos with NaN": (("brushes", 0, "pos"), [0, float("nan"), 0]),
    "textures not an object": (("brushes", 0, "textures"), "brick.png"),
    "uv table not an object": (("brushes", 0, "uv_scale"), [1, 1]),
    "door distance not a number": (("brushes", 0, "door_distance"), [128]),
    "mover direction empty": (("brushes", 0, "direction"), []),
    "original_pos too short": (("brushes", 0, "original_pos"), [0, 0]),
    "name is a list": (("brushes", 0, "name"), ["wall"]),
    "thing pos a string": (("things", 0, "pos"), "0,0,0"),
    "home pos a number": (("things", 0, "properties", "_prop_home_pos"), 3.0),
    "thing id an object": (("things", 0, "properties", "id"), {"id": "p1"}),
    "connections hold strings": (("things", 0, "io_connections"), ["OnUse"]),
}

BAD_SAVES = {
    "player pos a string": (("player", "pos"), "here"),
    "player angle a list": (("player", "angle"), [0.5]),
    "health a string": (("runtime", "player_health"), "full"),
    "weapon a number": (("runtime", "active_weapon"), 1),
    "keys not names": (("runtime", "collected_keys"), [1, 2]),
    "door state not an object": (("runtime", "door_states", "0"), 0.5),
    "door progress a string": (("runtime", "door_states", "0", "progress"), "x"),
    "mover path missing its node": (("runtime", "mover_path_states", "1",
                                     "current_node"), _DROP),
    "mover path lerp a string": (("runtime", "mover_path_states", "1", "lerp_t"), "x"),
    "embedded level malformed": (("level", "brushes", 0, "pos"), None),
}


def test_the_well_formed_examples_pass():
    validate_level(LEVEL)
    validate_snapshot(SAVE)


def test_odd_but_harmless_names_are_tolerated():
    validate_level(_with(LEVEL, ("brushes", 0, "name"), 7))


@pytest.mark.parametrize("path, value", BAD_LEVELS.values(), ids=BAD_LEVELS.keys())
def test_a_malformed_map_is_refused(path, value):
    with pytest.raises(ValueError):
        validate_level(_with(LEVEL, path, value))


def test_a_save_delta_need_not_repeat_unchanged_fields():
    """Delta records carry only what changed: no pos/size required."""
    validate_level({"brushes": [{"id": "b1", "hidden": True}], "things": []},
                   partial=True)


@pytest.mark.parametrize("path, value", BAD_SAVES.values(), ids=BAD_SAVES.keys())
def test_a_malformed_save_is_refused_on_read(tmp_path, path, value):
    target = tmp_path / "bad.fiosave"
    target.write_text(json.dumps(_with(SAVE, path, value)))
    with pytest.raises(ValueError):
        savegame.read(str(target))
