"""Optional "Restore the world when leaving Play" (Settings -> Play Modes).

Off by default: the editor keeps showing what happened in play (dead monsters,
killed or hidden objects), as it always has. On, Stop puts every brush and
entity back as it was when Play started, and the undo history and the unsaved
flag with them, so the session leaves no trace.
"""

import configparser
import types

import pytest

pytest.importorskip("PyQt5", reason="the play toggle lives on the editor window")

from editor.editor_state import EditorState        # noqa: E402
from editor.main_window import MainWindow          # noqa: E402
from editor.things import Monster                  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt


class _Window:
    """The slice of MainWindow the play toggle touches; the logic is real."""

    _capture_pre_play_world = MainWindow._capture_pre_play_world
    _restore_pre_play_world = MainWindow._restore_pre_play_world
    _exit_play_mode = MainWindow._exit_play_mode

    def __init__(self, restore):
        self.config = configparser.ConfigParser()
        self.config.add_section("Settings")
        self.config.set("Settings", "restore_world_on_stop", str(restore))
        self.state = EditorState()
        self.state.brushes = [box_brush("wall", (0, 64, 0))]
        self.state.things = [make_thing(Monster, "grunt", (100, 64, 0), health=50)]
        self.state.save_state()
        self.unsaved_changes = False
        self._pre_play_world = None
        self.view_3d = types.SimpleNamespace(
            play_mode=False,
            toggle_play_mode=lambda *a: setattr(self.view_3d, "play_mode", False))
        self.ui = types.SimpleNamespace(
            notification_label=types.SimpleNamespace(setText=lambda text: None))
        self.resynced = 0

    def play(self):
        self._capture_pre_play_world()
        self.view_3d.play_mode = True

    def _resync_components_after_history(self):
        self.resynced += 1

    def _restore_properties_tab(self):
        pass

    def update_title(self):
        pass

    def update_all_ui(self):
        pass

    def setFocus(self):
        pass

    def update_play_button_color(self):
        pass


def _play_a_session(window):
    wall = window.state.brushes[0]
    grunt = window.state.things[0]
    window.play()
    wall["hidden"] = wall["disabled"] = True          # I/O Kill
    grunt.properties["dead"] = True
    grunt.pos = [500.0, 64.0, 0.0]
    window.state.save_state()                           # e.g. setprop in play
    window.unsaved_changes = True
    window._exit_play_mode()


def test_stop_restores_the_pre_play_world_when_enabled():
    window = _Window(restore=True)
    history = len(window.state.undo_stack)
    _play_a_session(window)
    wall, grunt = window.state.brushes[0], window.state.things[0]
    assert not wall.get("hidden") and not wall.get("disabled")
    assert not grunt.properties.get("dead")
    assert list(grunt.pos) == [100.0, 64.0, 0.0]
    assert len(window.state.undo_stack) == history
    assert window.unsaved_changes is False
    assert window.resynced == 1, "references into the replaced world were not re-pointed"


def test_stop_keeps_what_happened_in_play_by_default():
    window = _Window(restore=False)
    _play_a_session(window)
    assert window.state.brushes[0]["hidden"] is True
    assert window.state.things[0].properties["dead"] is True


def test_a_level_change_drops_the_previous_maps_world():
    """_load_level clears the capture before leaving play, so the new map is
    never replaced by the one the session started on."""
    import inspect
    source = inspect.getsource(MainWindow._load_level)
    assert source.index("self._pre_play_world = None") < source.index(
        "self._exit_play_mode()")


def test_the_setting_round_trips_through_the_settings_window(qt_app):
    from editor.SettingsWindow import SettingsWindow
    config = configparser.ConfigParser()
    config.optionxform = str
    dialog = SettingsWindow(config)
    try:
        assert dialog.restore_world_checkbox.isChecked() is False, "must default off"
        dialog.restore_world_checkbox.setChecked(True)
        dialog._save_settings()
        assert config.getboolean("Settings", "restore_world_on_stop") is True
        dialog.restore_world_checkbox.setChecked(False)
        dialog.load_settings()
        assert dialog.restore_world_checkbox.isChecked() is True
    finally:
        dialog.deleteLater()
