"""Console commands a user can type that used to raise instead of answering.

Found by fuzzing every registered command with arbitrary arguments in the
editor and in Play. An exception escaping a console command is caught by the
application's excepthook, so nothing crashed -- the command just never did
what it says, and ``bind`` left the stored bindings unwritable.
"""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.debug_console import get_debug_logger           # noqa: E402

pytestmark = pytest.mark.qt


def _logged(handler, line):
    logger = get_debug_logger()
    logger.clear_buffer()
    handler.handle_command(line)
    return "\n".join(message for _, message in logger.get_buffer())


# -- help ----------------------------------------------------------------------

def test_help_lists_the_commands_plugins_registered(main_window):
    """The plugin manager returns its commands as a mapping; help iterated it
    as pairs and raised for any plugin command whose name is not two letters."""
    from plugins.manager import get_manager
    manager = get_manager()
    # Shipped off; a map that uses it (the Deathmatch arenas) turns it on.
    deathmatch = next(p for p in manager.plugins if "addbot" in manager._console_commands
                      and manager._console_commands["addbot"][0] is p)
    manager.set_enabled(deathmatch, True)
    commands = manager.console_commands()
    assert "addbot" in commands
    text = _logged(main_window.console_handler, "help")
    assert "Plugin Commands" in text
    for name, entry in commands.items():
        assert name in text
        if entry["help"]:
            assert entry["help"] in text


# -- showcollision -------------------------------------------------------------

@pytest.mark.parametrize("command", ["showcollision", "collisionvis", "collision"])
def test_the_first_collision_toggle_turns_the_overlay_on(main_window, command):
    view = main_window.view_3d
    assert getattr(view, "_collision_vis_mode", "off") == "off"
    main_window.console_handler.handle_command(command)
    assert view._collision_vis_mode == "all"


def test_showcollision_takes_every_mode_its_usage_names(main_window):
    view = main_window.view_3d
    for argument, mode in [("all", "all"), ("mesh", "mesh"), ("aabb", "aabb"),
                           ("on", "all"), ("off", "off")]:
        main_window.console_handler.handle_command("showcollision " + argument)
        assert view._collision_vis_mode == mode


# -- bind ----------------------------------------------------------------------

def _reloaded_bindings(window):
    """The bindings a restarted editor reads back from this window's settings."""
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(window.config_path)
    fresh = type("Fresh", (), {"config": config, "key_bindings": {}})()
    type(window).load_key_bindings(fresh)
    return fresh.key_bindings


def test_bind_stores_the_key_as_a_key_press_names_it(main_window):
    main_window.console_handler.handle_command("bind f5 quicksave")
    main_window.console_handler.handle_command("bind ctrl+k noclip")
    assert main_window.key_bindings == {"F5": "quicksave", "Ctrl+K": "noclip"}
    assert _reloaded_bindings(main_window) == main_window.key_bindings


@pytest.mark.parametrize("key", ["Ctrl+=", "Ctrl+:", "[", "#", ";", "%", "Alt+#"])
def test_keys_that_are_settings_syntax_can_be_bound_and_read_back(main_window, key):
    main_window.console_handler.handle_command("bind %s god" % key)
    assert main_window.key_bindings == {key: "god"}
    assert _reloaded_bindings(main_window) == {key: "god"}


def test_a_command_with_a_percent_sign_can_be_bound_and_read_back(main_window):
    main_window.console_handler.handle_command("bind F6 message 100% %s done")
    assert main_window.key_bindings == {"F6": "message 100% %s done"}
    assert _reloaded_bindings(main_window) == main_window.key_bindings
    # ... and it does not stop later bindings from being written.
    main_window.console_handler.handle_command("bind F7 god")
    assert _reloaded_bindings(main_window) == {"F6": "message 100% %s done", "F7": "god"}


@pytest.mark.parametrize("key", ["notakey", "C:\\x", "[1,2]", "mouse1"])
def test_bind_refuses_what_is_not_a_key(main_window, key):
    text = _logged(main_window.console_handler, "bind %s god" % key)
    assert "is not a key" in text
    assert main_window.key_bindings == {}


def test_a_hand_edited_binding_with_a_bare_percent_still_loads(main_window, tmp_path):
    config = configparser.ConfigParser()
    config.optionxform = str
    config.read_string("[KeyBindings]\nF8 = message 50% off\n")
    fresh = type("Fresh", (), {"config": config, "key_bindings": {}})()
    type(main_window).load_key_bindings(fresh)
    assert fresh.key_bindings == {"F8": "message 50% off"}
