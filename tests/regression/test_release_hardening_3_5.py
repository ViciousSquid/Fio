"""Hardening for the 3.5.0 stable release, one test per invariant.

* A damaged settings.ini never stops Fio starting: it falls back to the
  defaults, is kept aside, and a value that cannot be converted falls back to
  the caller's default.
* An unhandled exception is written to a crash log (windowed builds have no
  console).
* A map in a folder with a "%" in its name saves and loads without a false
  failure.
* A save whose level cannot be serialised fails, and keeps the slot.
* A map from a newer Fio is refused before the scene is touched.
* Settings > "Middle Click to Drag in 2D Views" is the option the 2D views read.
* A map with a byte-order mark loads; Save As adds the extension.
"""
import json
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# settings.ini
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_problems():
    from editor import settings_file
    before = list(settings_file.problems)
    del settings_file.problems[:]
    yield settings_file.problems
    settings_file.problems[:] = before


def _read(tmp_path, data, **open_kwargs):
    from editor.settings_file import SettingsParser, read_settings
    path = tmp_path / "settings.ini"
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, **open_kwargs)
    config = SettingsParser()
    return config, read_settings(config, str(path)), path


@pytest.mark.parametrize("damage", [
    b"font_size = 11\n[Display]\nvsync = True\n",          # no section header
    b"\x00\xff\xfe\x9d\x81 not text \xc3\x28\n",           # binary garbage
    b"[Display]\nvsync = True\n[Disp",                     # truncated write
], ids=["no-section", "binary", "truncated"])
def test_a_damaged_settings_file_falls_back_to_the_defaults(
        tmp_path, damage, settings_problems):
    config, as_written, path = _read(tmp_path, damage)

    assert not as_written
    assert config.sections() == [], "half of a damaged file was kept"
    assert config.getboolean("Display", "vsync", fallback=True) is True
    assert not path.exists() and (tmp_path / "settings.ini.corrupt").exists(), (
        "the damaged file was not set aside for the user to recover")
    assert settings_problems, "nothing told the user their settings were dropped"


def test_a_settings_file_with_a_byte_order_mark_is_read(tmp_path, settings_problems):
    config, as_written, _ = _read(
        tmp_path, "[Display]\nfont_size = 14\n", encoding="utf-8-sig")

    assert as_written and config.getint("Display", "font_size", fallback=11) == 14


def test_a_duplicated_option_keeps_the_last_value(tmp_path, settings_problems):
    config, as_written, _ = _read(
        tmp_path, "[Display]\nfont_size = 12\nfont_size = 13\n")

    assert as_written and config.getint("Display", "font_size", fallback=11) == 13


@pytest.mark.parametrize("getter,value,fallback", [
    ("getint", "big", 11), ("getboolean", "maybe", True), ("getfloat", "fast", 30.0),
])
def test_a_value_that_cannot_be_converted_falls_back(
        tmp_path, getter, value, fallback, settings_problems):
    config, _, _ = _read(tmp_path, f"[S]\noption = {value}\n")

    assert getattr(config, getter)("S", "option", fallback=fallback) == fallback
    assert any("option" in problem for problem in settings_problems)
    with pytest.raises(ValueError):
        getattr(config, getter)("S", "option")      # no fallback: still an error


def test_a_missing_settings_file_is_not_a_problem(tmp_path, settings_problems):
    from editor.settings_file import SettingsParser, read_settings

    assert read_settings(SettingsParser(), str(tmp_path / "absent.ini"))
    assert not settings_problems


# ---------------------------------------------------------------------------
# Crash log
# ---------------------------------------------------------------------------

def test_an_unhandled_exception_reaches_the_crash_log(tmp_path, monkeypatch):
    from editor import crash_log

    passed_on = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: passed_on.append(args))
    monkeypatch.setattr(crash_log, "_path", None)
    path = crash_log.install(str(tmp_path))

    try:
        raise RuntimeError("the renderer fell over")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    text = open(path, encoding="utf-8").read()
    assert "RuntimeError: the renderer fell over" in text and "Traceback" in text
    assert len(passed_on) == 1, "the crash log swallowed the exception"

    # The editor installs its own hook later; the log goes in front again,
    # once, however often that is asked for.
    monkeypatch.setattr(sys, "excepthook", lambda *args: passed_on.append(args))
    crash_log.wrap_current()
    crash_log.wrap_current()
    sys.excepthook(ValueError, ValueError("second"), None)
    assert open(path, encoding="utf-8").read().count("ValueError: second") == 1


# ---------------------------------------------------------------------------
# Map format version
# ---------------------------------------------------------------------------

# editor.editor_state imports editor.things, which is PyQt-backed: the no-Qt
# tier cannot import it.
@pytest.mark.qt
def test_a_map_from_a_newer_fio_is_refused():
    from editor.editor_state import EditorState
    from engine.brush_placement import MAP_VERSION

    EditorState.validate_level_data({"version": MAP_VERSION, "brushes": [], "things": []})
    EditorState.validate_level_data({"brushes": [], "things": []})      # legacy: no version
    with pytest.raises(ValueError, match="newer version of Fio"):
        EditorState.validate_level_data(
            {"version": MAP_VERSION + 1, "brushes": [], "things": []})


# ---------------------------------------------------------------------------
# The editor window
# ---------------------------------------------------------------------------

_LEVEL = {
    "version": 5,
    "brushes": [{"id": "floor", "name": "floor", "pos": [0, -8, 0], "size": [512, 16, 512]}],
    "things": [{"type": "playerstart", "pos": [0, 40, 0],
                "properties": {"type": "player_start", "name": "start",
                               "id": "ps-1", "primary": True}}],
}


@pytest.mark.qt
def test_a_newer_map_leaves_the_open_level_alone(main_window, tmp_path):
    from engine.brush_placement import MAP_VERSION

    assert main_window._load_level(json.loads(json.dumps(_LEVEL)))
    brushes = len(main_window.state.brushes)
    newer = dict(_LEVEL, version=MAP_VERSION + 1, brushes=[])
    path = tmp_path / "newer.json"
    path.write_text(json.dumps(newer), encoding="utf-8")

    assert not main_window.load_level_file(str(path))
    assert len(main_window.state.brushes) == brushes, "the open level was replaced"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == MAP_VERSION + 1


@pytest.mark.qt
def test_a_percent_sign_in_the_folder_does_not_fail_a_save_or_a_load(
        main_window, tmp_path):
    folder = tmp_path / "100% done"
    folder.mkdir()
    path = str(folder / "map.json")
    toasts = []
    main_window.show_toast = lambda message, is_error=False, **_kw: toasts.append(
        (message, is_error))
    assert main_window._load_level(json.loads(json.dumps(_LEVEL)))

    main_window.file_path = path
    main_window.save_level()
    assert not main_window.unsaved_changes and os.path.exists(path)
    assert main_window.load_level_file(path), "a load that worked reported failure"
    assert not [t for t in toasts if t[1]], toasts

    main_window.load_recent_files()
    assert os.path.abspath(path) in main_window.recent_files


@pytest.mark.qt
def test_a_map_with_a_byte_order_mark_loads(main_window, tmp_path):
    path = tmp_path / "bom.json"
    path.write_text(json.dumps(_LEVEL), encoding="utf-8-sig")

    assert main_window.load_level_file(str(path))


@pytest.mark.qt
def test_save_as_adds_the_extension_to_a_bare_name(main_window, tmp_path, monkeypatch):
    from editor import main_window as module

    assert main_window._load_level(json.loads(json.dumps(_LEVEL)))
    bare = str(tmp_path / "my_map")
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *args, **kwargs: (bare, "")))

    main_window.save_level_as()

    assert main_window.file_path == bare + ".json" and os.path.exists(bare + ".json")
    main_window.unsaved_changes = False


@pytest.mark.qt
@pytest.mark.parametrize("options,expected", [
    ({"middle_click_drag": "True"}, True),
    ({"middle_click_drag": "False"}, False),
    ({"middle_click_drag": "False", "MiddleClickDrag": "True"}, False),
    ({"MiddleClickDrag": "True"}, True),
    ({}, False),
])
def test_the_middle_click_drag_setting_is_the_one_the_views_read(
        main_window, options, expected):
    config = main_window.config
    if config.has_section("Controls"):
        config.remove_section("Controls")
    config.add_section("Controls")
    for name, value in options.items():
        config.set("Controls", name, value)

    assert main_window.view_top._middle_click_pan_enabled() is expected


# ---------------------------------------------------------------------------
# Save Game
# ---------------------------------------------------------------------------

@pytest.mark.qt
def test_a_save_that_cannot_serialise_the_level_fails_and_keeps_the_slot(
        fio_session, tmp_path, monkeypatch):
    session = fio_session(json.loads(json.dumps(_LEVEL)))
    session.start_play()
    session.step(2)
    path = str(tmp_path / "slot.fiosave")
    assert session.logic.session_runtime.save_session(path)[0]
    good = open(path, "rb").read()

    def broken():
        raise RuntimeError("an entity would not serialise")

    monkeypatch.setattr(session.state, "get_level_data", broken)
    ok, message = session.logic.session_runtime.save_session(path)
    monkeypatch.undo()

    assert not ok, f"the save reported success: {message}"
    assert open(path, "rb").read() == good, "the slot was overwritten"
    session.stop_play()
    session.window.unsaved_changes = False
