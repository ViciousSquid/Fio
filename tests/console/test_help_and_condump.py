"""``help`` lists every command the console accepts; ``condump`` writes the
console history to logs/.

``help`` used to be one hand-written HTML string kept apart from the dispatch
table, and 26 accepted commands (``bind``, ``showcollision``, ``r_vsync``,
``pos``, most short fog aliases...) were missing from it. It is now built from
the dispatcher's own table, so these tests enumerate what the dispatcher
accepts and look for each name in what ``help`` printed.
"""

import os
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor import debug_console                              # noqa: E402
from editor.debug_console import (                            # noqa: E402
    debug_log, debug_log_markup, debug_log_raw, get_debug_logger,
)

pytestmark = pytest.mark.qt

_NAME = re.compile(r'<b style="color:orange;">([^<]+)</b>')


def _help_text(handler):
    logger = get_debug_logger()
    logger.clear_buffer()
    handler.handle_command("help")
    (category, text), = logger.get_buffer()
    assert category == "Info"
    return text


def _listed(text):
    return {name for name in _NAME.findall(text) if " " not in name}


def _dispatcher_accepts(handler):
    """What handle_command runs, worked out the way it dispatches: its own
    table first, then an enabled plugin's command."""
    from plugins.manager import get_manager
    manager = get_manager()
    accepted = set(handler.commands)
    accepted |= {name for name in manager._console_commands
                 if manager.has_console_command(name)}
    return accepted


@pytest.fixture
def plugin_commands(monkeypatch):
    """Register plugin console commands for one test only."""
    from plugins.api import EditorAPI
    from plugins.manager import get_manager
    manager = get_manager()
    monkeypatch.setattr(manager, "_console_commands", dict(manager._console_commands))
    plugin = SimpleNamespace(name="zz_help_test_plugin", enabled=True)
    return plugin, EditorAPI(manager, plugin)


# -- help ----------------------------------------------------------------------

def test_help_lists_every_command_the_dispatcher_accepts(main_window):
    handler = main_window.console_handler
    accepted = _dispatcher_accepts(handler)
    assert accepted == set(handler.accepted_commands())
    listed = _listed(_help_text(handler))
    missing = sorted(accepted - listed)
    assert not missing, f"help leaves out accepted commands: {missing}"
    stale = sorted(listed - accepted)
    assert not stale, f"help lists commands the console rejects: {stale}"


@pytest.mark.parametrize("name", ["bind", "showcollision", "collision", "r_vsync", "vsync",
                                  "pos", "r_info", "phys", "kill_monster", "savegame",
                                  "loadgame", "listsaves", "fogcolour", "r_ambient",
                                  "showglasses", "condump"])
def test_help_names_the_commands_it_used_to_leave_out(main_window, name):
    assert name in main_window.console_handler.commands
    assert name in _listed(_help_text(main_window.console_handler))


def test_every_built_in_command_has_its_own_help_line(main_window):
    """A new command is listed anyway (under Other Commands); this asks that
    it be given a description in HELP_SECTIONS too."""
    titles = [title for title, _intro, _entries in main_window.console_handler.help_sections()]
    assert "Other Commands" not in titles


def test_a_command_the_help_table_does_not_word_is_still_listed(main_window, monkeypatch):
    handler = main_window.console_handler
    monkeypatch.setitem(handler.commands, "zz_undocumented", lambda args: None)
    text = _help_text(handler)
    assert "=== Other Commands ===" in text
    assert "zz_undocumented" in _listed(text)


def test_registering_a_plugin_command_makes_it_appear_in_help(main_window, plugin_commands):
    handler = main_window.console_handler
    plugin, api = plugin_commands
    calls = []
    assert "zz_plugin_cmd" not in _listed(_help_text(handler))

    api.register_console_command(
        "zz_plugin_cmd", lambda args, mw, logic, play: calls.append(args) or "ran",
        "zz_plugin_cmd <x> -- does a test thing")

    text = _help_text(handler)
    assert "=== Plugin Commands ===" in text
    assert "zz_plugin_cmd" in _listed(text)
    assert "does a test thing" in text
    assert "zz_plugin_cmd" in handler.accepted_commands()
    # ...and the console really accepts it.
    handler.handle_command("zz_plugin_cmd hello")
    assert calls == ["hello"]

    # A disabled plugin's command is neither run nor listed.
    plugin.enabled = False
    assert "zz_plugin_cmd" not in _listed(_help_text(handler))
    assert "zz_plugin_cmd" not in handler.accepted_commands()


def test_a_plugin_command_a_built_in_shadows_is_not_listed_as_a_plugin_command(
        main_window, plugin_commands):
    handler = main_window.console_handler
    _plugin, api = plugin_commands
    api.register_console_command("god", lambda *a: "plugin god", "plugin's own god")
    text = _help_text(handler)
    assert "plugin's own god" not in text
    assert "god" not in handler.plugin_commands()


# -- condump -------------------------------------------------------------------

@pytest.fixture
def install(main_window, tmp_path, monkeypatch):
    """A writable install folder of its own, so logs/ is never the repo's."""
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(main_window, "root_dir", str(root))
    get_debug_logger().clear_buffer()
    return root


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _last_message():
    return get_debug_logger().get_buffer()[-1]


def _fill_history():
    debug_log("Info", "first line")
    debug_log("Error", "Usage: map <mapname> & more")
    debug_log_markup("Info", '<b style="color:orange;">bold</b> &amp; markup')
    debug_log_raw("raw line")


EXPECTED = [
    "[Info] first line",
    "[Error] Usage: map <mapname> & more",
    "[Info] bold & markup",
    "raw line",
]


def test_condump_writes_the_whole_history_to_logs_console_txt(main_window, install):
    _fill_history()
    main_window.console_handler.handle_command("condump")
    path = install / "logs" / "console.txt"
    assert _read(path) == "\n".join(EXPECTED) + "\n"
    category, message = _last_message()
    assert category == "Info"
    assert "Dumped 4 console messages" in message
    assert str(path) in message


def test_condump_n_writes_only_the_last_n_messages(main_window, install):
    _fill_history()
    main_window.console_handler.handle_command("condump 2")
    assert _read(install / "logs" / "console.txt") == "\n".join(EXPECTED[-2:]) + "\n"
    assert "Dumped 2 console messages" in _last_message()[1]


def test_condump_n_larger_than_the_history_writes_all_of_it(main_window, install):
    _fill_history()
    main_window.console_handler.handle_command("condump 50")
    assert _read(install / "logs" / "console.txt") == "\n".join(EXPECTED) + "\n"
    message = _last_message()[1]
    assert "Dumped 4 console messages" in message and "asked for 50" in message


def test_condump_never_overwrites_an_existing_dump(main_window, install):
    logs = install / "logs"
    logs.mkdir()
    (logs / "console.txt").write_text("keep me\n", encoding="utf-8")
    (logs / "console_1.txt").write_text("keep me too\n", encoding="utf-8")
    handler = main_window.console_handler

    debug_log("Info", "one")
    handler.handle_command("condump")
    assert _read(logs / "console_2.txt") == "[Info] one\n"
    assert str(logs / "console_2.txt") in _last_message()[1]

    handler.handle_command("condump 1")      # the last message: the confirmation
    assert _read(logs / "console_3.txt").startswith("[Info] Dumped 1 console message to ")

    assert _read(logs / "console.txt") == "keep me\n"
    assert _read(logs / "console_1.txt") == "keep me too\n"
    assert sorted(os.listdir(logs)) == ["console.txt", "console_1.txt",
                                        "console_2.txt", "console_3.txt"]


@pytest.mark.parametrize("argument", ["abc", "0", "-3", "2.5", "1 2", "five"])
def test_condump_rejects_a_bad_count_with_its_usage(main_window, install, argument):
    debug_log("Info", "something to dump")
    main_window.console_handler.handle_command(f"condump {argument}")
    category, message = _last_message()
    assert category == "Error"
    assert "Usage: condump [N]" in message
    assert not (install / "logs").exists()


def test_condump_with_an_empty_history_writes_nothing(main_window, install):
    main_window.console_handler.handle_command("condump")
    assert _last_message()[0] == "Warning"
    assert not (install / "logs").exists()


def test_typed_commands_are_in_the_history_and_the_dump(main_window, install):
    """What the console shows includes the ``] command`` echo of each line typed
    into it; the history keeps it (a rebuilt console redraws it) and condump
    writes it -- all but the echo of the condump line itself."""
    console = main_window.debug_console
    for line in ("zz_not_a_command", "condump"):
        console.command_input.setText(line)
        console._on_command_entered()
    lines = _read(install / "logs" / "console.txt").splitlines()
    assert lines == [
        "] zz_not_a_command",
        "[Error] Unknown command: zz_not_a_command. Type 'help' for list.",
    ]
    console._refresh_console()
    assert "] zz_not_a_command" in console.console.toPlainText()


def test_condump_writes_help_as_the_plain_text_the_console_shows(main_window, install):
    handler = main_window.console_handler
    handler.handle_command("help")
    handler.handle_command("condump")
    text = _read(install / "logs" / "console.txt")
    assert "<b" not in text and "&lt;" not in text
    assert "help — Show this help" in text
    assert "map <name> — Load a different map" in text
    assert "r_fogcolor / r_fogcolour / fogcolor / fogcolour <R> <G> <B>" in text
    assert "=== Debug ===" in text


def test_the_history_is_capped_and_condump_says_so(main_window, install):
    logger = get_debug_logger()
    assert logger.history_limit() == debug_console.HISTORY_LIMIT
    for i in range(debug_console.HISTORY_LIMIT + 5):
        debug_log("Info", f"line {i}")
    main_window.console_handler.handle_command("condump")
    lines = _read(install / "logs" / "console.txt").splitlines()
    assert len(lines) == debug_console.HISTORY_LIMIT
    assert lines[-1] == f"[Info] line {debug_console.HISTORY_LIMIT + 4}"
    assert f"keeps only its last {debug_console.HISTORY_LIMIT}" in _last_message()[1]


def test_logs_go_to_the_user_data_folder_when_the_install_is_read_only(
        main_window, tmp_path, monkeypatch):
    from editor import user_paths
    handler = main_window.console_handler
    monkeypatch.setattr(main_window, "root_dir", str(tmp_path / "install"))
    assert handler._logs_dir() == str(tmp_path / "install" / "logs")

    monkeypatch.setattr(user_paths, "_can_write", lambda path: False)
    monkeypatch.setattr(user_paths, "user_data_dir", lambda: str(tmp_path / "userdata"))
    assert handler._logs_dir() == str(tmp_path / "userdata" / "logs")
