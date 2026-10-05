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


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "maps" / "chapter2").mkdir(parents=True)
    level = {"version": 3, "brushes": [], "things": []}
    import json
    (root / "maps" / "start.json").write_text(json.dumps(level))
    (root / "maps" / "chapter2" / "boss.json").write_text(json.dumps(level))
    (tmp_path / "victim.json").write_text("{}")
    return root


@pytest.mark.parametrize("name, expected", [
    ("start", "maps/start.json"),
    ("start.json", "maps/start.json"),
    ("chapter2/boss", "maps/chapter2/boss.json"),
])
def test_maps_inside_the_folder_load(main_window, project, name, expected):
    window = main_window
    window.root_dir = str(project)
    handler = ConsoleCommandHandler(window)
    handler.cmd_map(name)
    assert window.file_path == str(project / expected)


@pytest.mark.parametrize("name", ["../../victim", "../../victim.json"])
def test_a_name_climbing_out_of_maps_is_refused(main_window, project, name):
    window = main_window
    window.root_dir = str(project)
    old = window.file_path
    ConsoleCommandHandler(window).cmd_map(name)
    assert window.file_path == old


def test_an_absolute_path_is_refused(main_window, project, tmp_path):
    window = main_window
    window.root_dir = str(project)
    old = window.file_path
    ConsoleCommandHandler(window).cmd_map(str(tmp_path / "victim.json"))
    assert window.file_path == old


@pytest.mark.slow
def test_a_saved_game_names_its_map_by_basename_in_maps_only(main_window, project, tmp_path):
    """The save's ``map`` field is data from a shareable file, not a path."""
    import json

    save = tmp_path / "shared.fiosave"
    save.write_text(json.dumps({"fio_savegame": True, "save_version": 2,
                                "map": str(tmp_path / "victim.json")}))
    window = main_window
    window.root_dir = str(project)
    handler = ConsoleCommandHandler(window)

    handler._load_from_editor(str(save))

    assert window.file_path is None, "a save pointed the editor at a file outside maps/"

    save.write_text(json.dumps({"fio_savegame": True, "save_version": 2,
                                "map": "start.json"}))
    handler._load_from_editor(str(save))
    assert window.file_path == str(project / "maps" / "start.json")


def test_map_logic_cannot_bind_keys_but_the_user_can(main_window, qt_app):
    """A map cannot persist key bindings through the real play console."""
    from engine.qt_game_view import QtGameView

    bound = []
    window = main_window
    window.set_key_binding = lambda key, command: bound.append((key, command))
    handler = ConsoleCommandHandler(window)

    view = QtGameView(window)
    try:
        view.game_state.queue_console_command("bind K delete everything")
        view._process_console_command_queue()
        assert bound == []

        handler.handle_command("bind K god")
        assert bound == [("K", "god")]
    finally:
        view.deleteLater()
        qt_app.processEvents()


