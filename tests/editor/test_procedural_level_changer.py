"""The procedural generator's exit makes the next level.

A generated map ends in a LevelChanger. It used to name a fixed map and was
not usable, so [E] did nothing. Now it is usable and, instead of loading a
map, generates a new random level with the same generator settings (a new
seed each time) and sends the player there -- through the same level change a
map file gets, so Play carries on at the new level's start.

Its settings travel inside the map, so a shared map or package can name any
values: they are sanitised to the generator panel's ranges before use.
"""

import random

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="LevelChanger entities require the Qt-backed editor things")

from editor.editor_state import EditorState                              # noqa: E402
from editor.procedural_generator import (                                # noqa: E402
    DEFAULT_PARAMS, PARAM_LIMITS, create_map_data, sanitize_params,
)
from editor.things import LevelChanger                                   # noqa: E402
from engine.logic_thread import LogicThread                              # noqa: E402
from engine.threaded_game_state import ThreadedGameState                 # noqa: E402
from tests.helpers.worlds import box_brush, make_thing                   # noqa: E402

pytestmark = pytest.mark.qt

SMALL = dict(DEFAULT_PARAMS, world_width=1024, world_height=1024, room_count=8,
             spawn_monsters=False, spawn_health=False)


def _generated(params=SMALL, seed=7):
    random.seed(seed)
    return create_map_data(dict(params))


def _exit(level):
    exits = [t for t in level["things"] if t["type"] == "levelchanger"]
    assert len(exits) == 1
    return exits[0]


# ---------------------------------------------------------------------------
# The generated exit
# ---------------------------------------------------------------------------

def test_the_generated_exit_is_usable_and_generates_the_next_level():
    props = _exit(_generated())["properties"]
    assert props["usable"] is True
    assert props["generate_level"] is True
    assert props["generator_params"] == sanitize_params(SMALL)

    props = {k: v for k, v in props.items() if k != "name"}
    changer = make_thing(LevelChanger, "LevelChanger_Exit", (0, 0, 0), **props)
    assert changer.generates_level()
    assert changer.generator_params() == sanitize_params(SMALL)


def test_a_changelevel_input_naming_a_map_still_goes_to_that_map():
    changer = make_thing(LevelChanger, "Exit", (0, 0, 0), generate_level=True)
    assert changer.generates_level()
    assert not changer.generates_level("maps/next.json")


# ---------------------------------------------------------------------------
# Settings from map data
# ---------------------------------------------------------------------------

def test_hostile_generator_settings_are_held_to_the_panels_ranges():
    params = sanitize_params({
        "world_width": 10 ** 12, "world_height": float("nan"),
        "room_count": -5, "min_room": 10 ** 9, "max_room": "lots",
        "monster_count": 10 ** 9, "health_count": None, "floor_room_count": 99,
        "floor_height": -1, "wall_tex": "../../etc/passwd",
        "floor_tex": "C:/Windows/win.ini", "spawn_monsters": 1,
        "something_else": "dropped",
    })
    assert params["world_width"] == 4096
    assert params["world_height"] == DEFAULT_PARAMS["world_height"]
    for key, (lo, hi) in PARAM_LIMITS.items():
        assert lo <= params[key] <= hi, key
    assert params["max_room"] >= params["min_room"]
    assert params["wall_tex"] == DEFAULT_PARAMS["wall_tex"]
    assert params["floor_tex"] == DEFAULT_PARAMS["floor_tex"]
    assert params["spawn_monsters"] is True
    assert set(params) == set(DEFAULT_PARAMS)


@pytest.mark.parametrize("params", [None, "not a dict", [1, 2], {}])
def test_missing_settings_generate_with_the_defaults(params):
    assert sanitize_params(params) == DEFAULT_PARAMS


def test_textures_inside_the_textures_folder_are_kept():
    assert sanitize_params({"wall_tex": "Dev/128.jpg"})["wall_tex"] == "Dev/128.jpg"


# ---------------------------------------------------------------------------
# Using it in Play
# ---------------------------------------------------------------------------

@pytest.fixture
def logic(request):
    logic = LogicThread(ThreadedGameState(), EditorState())
    request.addfinalizer(logic.stop)
    return logic


def test_using_the_exit_offers_a_generated_level(logic):
    props = dict(_exit(_generated())["properties"])
    props.pop("name", None)
    changer = make_thing(LevelChanger, "LevelChanger_Exit", (0.0, 0.0, 0.0), **props)
    logic.editor_state.things = [changer]
    logic.editor_state.brushes = []
    from engine.player import Player
    logic.player_runtime.player = Player(0.0, 96.0, float(np.pi))
    logic.player_runtime.player.pos = glm.vec3(0.0, 0.0, 96.0)
    logic.world_runtime.build_entity_caches()

    logic.interaction_runtime.handle(False)
    assert logic.interaction_runtime.current_hud_message == "[E] Complete Level"

    logic.interaction_runtime.handle(True)
    ui = logic.interaction_runtime.level_complete_ui
    assert ui["generate_level"] is True
    assert ui["target_map"] == ""
    assert ui["generator_params"] == sanitize_params(SMALL)


def _play_generated(window, monkeypatch):
    """Play on a freshly generated level, as the generator panel leaves it."""
    toasts = []
    monkeypatch.setattr(window, "show_toast",
                        lambda text, is_error=False, **_: toasts.append((text, is_error)))
    window._on_procedural_map_generated(_generated())
    window.enter_play_mode()
    assert window.view_3d.play_mode
    return toasts


def test_continue_generates_a_new_level_and_play_carries_on_there(
        main_window, tmp_path, monkeypatch):
    window = main_window
    monkeypatch.setattr(window, "root_dir", str(tmp_path))
    toasts = _play_generated(window, monkeypatch)
    before = [list(b["pos"]) for b in window.state.brushes]
    exit_thing = next(t for t in window.state.things if isinstance(t, LevelChanger))

    view = window.view_3d
    view._cached_level_complete_ui = {
        "active": True, "target_map": "", "destination_spawn": "",
        "generate_level": True, "generator_params": exit_thing.generator_params(),
    }
    view._confirm_level_complete()

    assert window.view_3d.play_mode, "Play should carry on in the new level"
    assert window.file_path is None
    assert [list(b["pos"]) for b in window.state.brushes] != before
    new_exit = [t for t in window.state.things if isinstance(t, LevelChanger)]
    assert len(new_exit) == 1 and new_exit[0].generates_level()
    assert any("Generated a new level" in text for text, _ in toasts)
    # Untouched generator output is not "unsaved work" to autosave.
    assert not (tmp_path / "maps").exists() or not list((tmp_path / "maps").iterdir())
    window._exit_play_mode()


def test_a_changelevel_input_generates_too(main_window, monkeypatch):
    window = main_window
    _play_generated(window, monkeypatch)
    requested = []
    window.generate_level_signal.connect(requested.append)
    exit_thing = next(t for t in window.state.things if isinstance(t, LevelChanger))
    exit_thing._main_window = window

    assert exit_thing.on_input("ChangeLevel") is True

    assert requested == [exit_thing.generator_params()]
    assert window.view_3d.play_mode
    window._exit_play_mode()


def test_in_the_editor_generating_asks_before_replacing_unsaved_work(
        main_window, monkeypatch):
    window = main_window
    window.state.brushes.append(box_brush("wall", (0, 64, 0)))
    window.unsaved_changes = True
    asked = []
    monkeypatch.setattr(window, "check_unsaved_changes",
                        lambda: asked.append(True) or False)
    brushes = list(window.state.brushes)

    assert window._on_generate_level_requested(dict(SMALL)) is False

    assert asked == [True]
    assert window.state.brushes == brushes
