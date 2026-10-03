"""``map <name>`` loads from the project's maps folder and nowhere else.

A map can queue console commands through a ``logic_command`` entity, so the
name is untrusted.  Loading a file makes it the editor's save target: a name
that climbed out of ``maps/`` let a played package point the next Ctrl+S (or
autosave) at an arbitrary JSON file elsewhere on disk.
"""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler   # noqa: E402

pytestmark = pytest.mark.qt


class _MainWindow:
    def __init__(self, root):
        self.root_dir = str(root)
        self.state = object()
        self.loaded = []

    def load_level_file(self, path):
        self.loaded.append(path)
        return True


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "maps" / "chapter2").mkdir(parents=True)
    (root / "maps" / "start.json").write_text("{}")
    (root / "maps" / "chapter2" / "boss.json").write_text("{}")
    (tmp_path / "victim.json").write_text("{}")
    return root


@pytest.mark.parametrize("name, expected", [
    ("start", "maps/start.json"),
    ("start.json", "maps/start.json"),
    ("chapter2/boss", "maps/chapter2/boss.json"),
])
def test_maps_inside_the_folder_load(project, name, expected):
    window = _MainWindow(project)
    ConsoleCommandHandler(window).cmd_map(name)
    assert [p.replace("\\", "/") for p in window.loaded] == \
        [str(project / expected).replace("\\", "/")]


@pytest.mark.parametrize("name", ["../../victim", "../../victim.json"])
def test_a_name_climbing_out_of_maps_is_refused(project, name):
    window = _MainWindow(project)
    ConsoleCommandHandler(window).cmd_map(name)
    assert window.loaded == []


def test_an_absolute_path_is_refused(project, tmp_path):
    window = _MainWindow(project)
    ConsoleCommandHandler(window).cmd_map(str(tmp_path / "victim.json"))
    assert window.loaded == []


def test_a_saved_game_names_its_map_by_basename_in_maps_only(project, tmp_path):
    """The save's ``map`` field is data from a shareable file, not a path."""
    import json

    save = tmp_path / "shared.fiosave"
    save.write_text(json.dumps({"fio_savegame": True, "save_version": 2,
                                "map": str(tmp_path / "victim.json")}))
    window = _MainWindow(project)
    window.enter_play_mode = lambda: None
    window.view_3d = None
    handler = ConsoleCommandHandler(window)

    handler._load_from_editor(str(save))

    assert window.loaded == [], "a save pointed the editor at a file outside maps/"

    save.write_text(json.dumps({"fio_savegame": True, "save_version": 2,
                                "map": "start.json"}))
    handler._load_from_editor(str(save))
    assert window.loaded == [str(project / "maps" / "start.json")]


def test_map_logic_cannot_bind_keys_but_the_user_can(tmp_path):
    """``bind`` persists a key -> command binding into settings.ini.

    Commands a map queues (logic_command entities) run through the same
    console, so a played package could leave keys bound to its commands in
    the user's editor for good.  The queue drain marks them as map-originated.
    """
    import types

    from engine.qt_game_view import QtGameView
    from engine.threaded_game_state import ThreadedGameState

    bound = []
    window = _MainWindow(tmp_path)
    window.set_key_binding = lambda key, command: bound.append((key, command))
    handler = ConsoleCommandHandler(window)

    game_state = ThreadedGameState()
    game_state.queue_console_command("bind K delete everything")
    view = types.SimpleNamespace(game_state=game_state,
                                 editor=types.SimpleNamespace(console_handler=handler))
    QtGameView._process_console_command_queue(view)
    assert bound == []

    handler.handle_command("bind K god")
    assert bound == [("K", "god")]
