"""Regression tests for the final 2.5.10.2909 release-hardening pass.

Found by driving the real editor under a virtual display, checked by two
independent oracles: Tools -> Debug Tables sampling each published frame, and
a separate comparison of the published dense tables against a from-scratch
rebuild and against values derived straight from the world's dicts.
"""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="editor state and console are editor-tier")

from tests.helpers.worlds import box_brush                      # noqa: E402
from tests.regression.test_final_audit import _playing          # noqa: E402

pytestmark = pytest.mark.qt


# ---------------------------------------------------------------------------
# Door/mover state after a brush leaves the world through a cache rebuild
# ---------------------------------------------------------------------------

def _doors_world():
    ground = box_brush("ground", (0, -16, 0), (4096, 32, 4096))
    crate = box_brush("crate", (500, 32, 500), (64, 64, 64))
    door_a = box_brush("door_a", (0, 64, 300), (128, 128, 16), is_door=True,
                       door_speed=512.0, door_distance=128.0,
                       door_direction="up", open_time=30.0)
    door_b = box_brush("door_b", (300, 64, 300), (128, 128, 16), is_door=True,
                       door_speed=512.0, door_distance=128.0,
                       door_direction="up", open_time=30.0)
    lift = box_brush("lift", (-400, 16, 0), (128, 32, 128), is_mover=True,
                     start_on=True, speed=64.0, distance=256.0,
                     direction=[0, 1, 0])
    return ground, crate, door_a, door_b, lift


def _console(state, logic):
    from editor.console_commands import ConsoleCommandHandler
    window = SimpleNamespace(state=state,
                             view_3d=SimpleNamespace(logic_thread=logic,
                                                     play_mode=True),
                             update_all_ui=lambda: None)
    return ConsoleCommandHandler(window)


def _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                       lift_progress):
    for index, brush in logic.doors + logic.movers:
        assert state.brushes[index] is brush, (
            f"door/mover {brush['name']} is keyed by index {index}, which now "
            f"holds {state.brushes[index]['name'] if index < len(state.brushes) else 'nothing'}")
    closed_a = list(door_a["pos"])
    logic.io_manager._execute_input("door_b", "Open", "", "test",
                                    target_id=door_b["id"])
    for _ in range(30):
        logic._tick(logic.TICK_DURATION)
    assert door_a["pos"] == closed_a, "Open aimed at door_b opened door_a"
    assert door_b["pos"][1] > 64.0 + 100.0, "the aimed door did not open"
    assert (logic.mover_states[state.brushes.index(lift)]["progress"]
            >= lift_progress), "the lift lost its progress in the re-key"


def test_console_delete_of_a_brush_in_play_keeps_io_aimed_at_the_right_door():
    """``delete <brush>`` rebuilt the session's entity index itself, which
    recorded the new brush list as already indexed: the row watcher then saw
    no change and door/mover state stayed keyed by the old indices. Open aimed
    at door_b opened door_a, and door_b's index pointed past the list."""
    ground, crate, door_a, door_b, lift = _doors_world()
    state, logic = _playing(brushes=[ground, crate, door_a, door_b, lift])
    try:
        for _ in range(20):
            logic._tick(logic.TICK_DURATION)
        progress = logic.mover_states[state.brushes.index(lift)]["progress"]

        _console(state, logic).cmd_delete("crate")
        logic._tick(logic.TICK_DURATION)

        _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                           progress)
        assert all(b is not crate for b in logic._collision_brushes_cache)
    finally:
        logic.stop()


def test_editor_delete_then_a_console_command_before_the_next_tick():
    """An editor Delete in play is picked up by the row watcher on the next
    tick; any console command that rebuilt the entity index first (setprop,
    portal_*, spawn) hid the change from it, so doors were never re-keyed and
    the deleted wall stayed solid."""
    ground, crate, door_a, door_b, lift = _doors_world()
    state, logic = _playing(brushes=[ground, crate, door_a, door_b, lift])
    try:
        for _ in range(20):
            logic._tick(logic.TICK_DURATION)
        progress = logic.mover_states[state.brushes.index(lift)]["progress"]

        state.save_state()
        state.brushes.remove(crate)                      # editor Delete
        _console(state, logic).cmd_set_property("lift label up")
        logic._tick(logic.TICK_DURATION)

        _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                           progress)
        assert all(b is not crate for b in logic._collision_brushes_cache), (
            "the deleted crate is still in the collision set")
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Console setprop of a flag
# ---------------------------------------------------------------------------

def test_setprop_hidden_false_leaves_the_object_visible():
    """``setprop lamp hidden false`` stored the string "false", which every
    reader takes as ``bool("false")`` -- True: typing false hid the lamp. Found
    by the private oracle (world dicts vs the published EntityTable); Debug
    Tables agreed with the table, since its rebuild reads the flag the same
    way."""
    from editor.things import Light

    lamp = Light(pos=[0.0, 100.0, 0.0], properties={"name": "lamp"})
    state, logic = _playing([lamp])
    try:
        _console(state, logic).cmd_set_property("lamp hidden false")
        assert lamp.properties["hidden"] is False
        logic._prepare_render_state()
        table = logic.game_state.get_write_state().entity_table
        assert not table.hidden[table.slot_of_id[lamp.properties["id"]]], (
            "setprop ... hidden false published the lamp as hidden")

        _console(state, logic).cmd_set_property("lamp hidden TRUE")
        assert lamp.properties["hidden"] is True
    finally:
        logic.stop()


def test_setprop_hidden_on_a_brush_in_play_updates_its_collision():
    """Written directly, a wall hidden with setprop was drawn hidden but stayed
    solid (hide/show rebuild the collision set; setprop did not)."""
    ground = box_brush("ground", (0, -16, 0), (4096, 32, 4096))
    wall = box_brush("wall", (200, 64, 0), (32, 128, 256))
    state, logic = _playing(brushes=[ground, wall])
    try:
        console = _console(state, logic)
        console.cmd_set_property("wall hidden true")
        logic._tick(logic.TICK_DURATION)
        assert wall["hidden"] is True
        assert all(b is not wall for b in logic._spatial_grid._all_solid), (
            "a wall hidden with setprop is still solid")

        console.cmd_set_property("wall hidden false")
        logic._tick(logic.TICK_DURATION)
        assert any(b is wall for b in logic._spatial_grid._all_solid)
    finally:
        logic.stop()


def test_setprop_keeps_text_properties_as_text():
    from editor.things import Light

    lamp = Light(pos=[0.0, 100.0, 0.0], properties={"name": "lamp",
                                                     "label": "on"})
    state, logic = _playing([lamp])
    try:
        _console(state, logic).cmd_set_property("lamp label false")
        assert lamp.properties["label"] == "false"
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Console setpos / teleport
# ---------------------------------------------------------------------------

def _setpos_console(logic):
    from editor.console_commands import ConsoleCommandHandler
    window = SimpleNamespace(
        state=logic.editor_state,
        view_3d=SimpleNamespace(logic_thread=logic, play_mode=True,
                                player=logic.player),
        update_all_ui=lambda: None, show_toast=lambda *a, **k: None)
    return ConsoleCommandHandler(window)


def test_setpos_moves_the_player():
    """setpos/teleport assigned ``player.position``, which Player does not
    have: it reported "Player teleported" and moved nothing."""
    import glm

    state, logic = _playing()
    try:
        logic.player.velocity = glm.vec3(0.0, -300.0, 0.0)
        _setpos_console(logic).handle_command("setpos 100 200 -300")
        assert tuple(logic.player.pos) == (100.0, 200.0, -300.0)
        assert tuple(logic.player.velocity) == (0.0, 0.0, 0.0)
    finally:
        logic.stop()


@pytest.mark.parametrize("coords", ["nan 0 0", "0 inf 0", "0 0 -inf", "1e309 0 0"])
def test_setpos_refuses_non_finite_coordinates(coords):
    state, logic = _playing()
    try:
        before = tuple(logic.player.pos)
        _setpos_console(logic).handle_command(f"teleport {coords}")
        assert tuple(logic.player.pos) == before
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Opening a map that is shaped like one but does not parse
# ---------------------------------------------------------------------------

def _window_host(state):
    """What MainWindow._load_level/_apply_level_data touch, on a real
    EditorState, bound to the real methods."""
    from editor.main_window import MainWindow

    host = SimpleNamespace(
        state=state, file_path="maps/open.json", unsaved_changes=False,
        terrain=None, terrain_editor_window=None, _current_overlay=None,
        _pre_play_world=None,
        view_3d=SimpleNamespace(play_mode=False, logic_thread=None,
                                terrain=None, renderer=None,
                                camera=SimpleNamespace(pos=None, yaw=0.0,
                                                       pitch=0.0)),
        config=SimpleNamespace(getboolean=lambda *a, **k: False),
        toasts=[])
    host.show_toast = lambda msg, **k: host.toasts.append(msg)
    for name in ("update_title", "update_all_ui", "set_selected_object",
                 "add_recent_file", "center_2d_views_on", "_refresh_logic_graph"):
        setattr(host, name, lambda *a, **k: None)
    for name in ("_load_level", "_apply_level_data", "_clear_terrain"):
        setattr(host, name, getattr(MainWindow, name).__get__(host))
    return host


def test_a_map_that_fails_to_parse_leaves_the_open_level_alone():
    """The shape check passed, then the scene was cleared, then parsing
    failed: "Failed to load level" over an empty scene with no file. Opening
    the file, a LevelChanger and the ``map`` command all go through here, the
    last two with no unsaved-changes prompt."""
    import copy
    import os

    from tests.helpers.paths import REPO_ROOT

    with open(os.path.join(REPO_ROOT, "maps", "Simple_Map_Test.json"),
              encoding="utf-8") as f:
        good = json.load(f)
    from editor.editor_state import EditorState
    state = EditorState()
    state.load_from_data(good)
    brushes, things = list(state.brushes), list(state.things)
    host = _window_host(state)

    corrupt = copy.deepcopy(good)
    corrupt["things"][0]["pos"] = 5            # a map's shape; unparseable
    assert host._load_level(corrupt, "maps/corrupt.json") is False

    assert state.brushes == brushes and state.things == things, (
        "a map that failed to parse emptied the open level")
    assert host.file_path == "maps/open.json"
    assert host.unsaved_changes is False


def test_opening_a_terrain_map_keeps_its_terrain():
    import os

    from editor.editor_state import EditorState
    from tests.helpers.paths import REPO_ROOT

    with open(os.path.join(REPO_ROOT, "maps", "Terrain_Test_small.json"),
              encoding="utf-8") as f:
        level = json.load(f)
    assert level.get("terrain_data")
    state = EditorState()
    host = _window_host(state)
    host._apply_level_data(level)
    assert state.terrain_data == level["terrain_data"]
    assert host.terrain is not None


# ---------------------------------------------------------------------------
# Long session: Recent Files actions
# ---------------------------------------------------------------------------

def test_rebuilding_recent_files_does_not_accumulate_actions(qt_app, tmp_path):
    """Found by a long-session growth run (Debug Tables and the private
    oracle clean, every runtime container flat): QAction count rose by five
    per map load. Every load rebuilds the Recent Files menu, with actions
    parented to the window; QMenu.clear() deletes only the actions the menu
    owns, so every level change added five that lived until the editor
    closed."""
    from PyQt5.QtWidgets import QAction, QMainWindow, QMenu

    from editor.main_window import MainWindow

    host = QMainWindow()
    try:
        host.recent_menu = QMenu(host)
        paths = []
        for i in range(5):
            p = tmp_path / f"map{i}.json"
            p.write_text("{}")
            paths.append(str(p))
        host.recent_files = paths
        rebuild = MainWindow.update_recent_files_menu.__get__(host)

        rebuild()
        qt_app.processEvents()
        baseline = len(host.findChildren(QAction))
        for _ in range(20):
            rebuild()
        qt_app.processEvents()
        assert len(host.recent_menu.actions()) == 5
        assert len(host.findChildren(QAction)) == baseline, (
            "Recent Files actions accumulate on every rebuild")
    finally:
        host.deleteLater()


# ---------------------------------------------------------------------------
# Debug -> Validate Connections
# ---------------------------------------------------------------------------

def test_validate_connections_names_the_connection_it_reports(monkeypatch):
    """The report listed an I/O problem as just "targets 'ghost', which is not
    in this map" -- nothing said which entity or output held the connection.
    A merge replaced the report body and left the previous one (which named
    ``entity.output`` and put missing targets first) unreachable after a
    ``return``; found by removing that dead code."""
    import editor.main_window as mw
    from editor.editor_state import EditorState

    state = EditorState()
    state.load_from_data({"brushes": [], "things": [
        {"type": "light", "pos": [0, 0, 0],
         "properties": {"type": "light", "name": "lamp"}},
        {"type": "playerstart", "pos": [0, 0, 0],
         "properties": {"type": "playerstart", "name": "Start"},
         "io_connections": [
             {"output": "OnPlayerSpawn", "target": "lamp", "input": "Opne"},
             {"output": "OnPlayerSpawn", "target": "ghost", "input": "Trigger"},
         ]},
    ]})
    shown = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        staticmethod(lambda _p, _t, text: shown.append(text)))

    mw.MainWindow.validate_io_connections(SimpleNamespace(state=state))

    lines = [l.strip() for l in shown[0].splitlines()[2:] if l.strip()]
    assert lines[0].startswith("Start.OnPlayerSpawn targets 'ghost'"), lines
    assert lines[1].startswith("Start.OnPlayerSpawn calls 'Opne'"), lines


# ---------------------------------------------------------------------------
# Shipped content
# ---------------------------------------------------------------------------

def _shipped_maps():
    import os

    from tests.helpers.paths import REPO_ROOT
    folder = os.path.join(REPO_ROOT, "maps")
    return sorted(os.path.join(folder, name) for name in os.listdir(folder)
                  if name.endswith(".json"))


@pytest.mark.parametrize("path", _shipped_maps(),
                         ids=lambda p: p.rsplit("/", 1)[-1])
def test_every_shipped_connection_resolves(path):
    """_SHOWCASE.json's PlayerStart fired Trigger at 'intro_messages', an
    entity no version of the map has contained: every Play of the showcase
    logged "Target 'intro_messages' not found". Debug -> Validate
    Connections, run over every shipped map."""
    from editor.editor_state import EditorState
    from editor.io_system import validate_all_scene_connections

    with open(path, encoding="utf-8") as f:
        level = json.load(f)
    state = EditorState()
    state.load_from_data(level, save_undo=False)
    report = validate_all_scene_connections(state.brushes, state.things)
    assert report["problems"] == [], [m for *_, m in report["problems"]]


def test_the_big_world_map_is_big_and_playable():
    """The shipped Big World map was four brushes and could not enter Play (no
    PlayerStart); it is now a streamed world large enough to exercise cell
    parking, with its original terrain and settings."""
    import os

    from tests.helpers.paths import REPO_ROOT

    with open(os.path.join(REPO_ROOT, "maps", "BigWorld_streaming_test.json"),
              encoding="utf-8") as f:
        level = json.load(f)
    types = [t["type"].lower() for t in level["things"]]
    assert "playerstart" in types and "bigworldsettings" in types
    assert level.get("terrain_data")
    assert len(level["brushes"]) > 3000
    xs = [b["pos"][0] for b in level["brushes"]]
    assert max(xs) - min(xs) > 10000, "not spread across many streaming cells"


# ---------------------------------------------------------------------------
# Cross-thread: the property panel reads a dict the logic thread writes to
# ---------------------------------------------------------------------------

def test_property_signature_survives_a_key_added_while_it_reads():
    """Seen once in the real editor: Redo -> update_all_ui -> the property
    panel's signature iterated the selected brush while the logic thread,
    projecting that freshly restored brush for the first time, added its
    geometry cache keys: "dictionary changed size during iteration". The
    write is simulated at the one point the loop runs other code: a value's
    repr()."""
    from editor.property_editor import PropertyEditor

    brush = {"id": "b", "name": "wall", "pos": [0.0, 0.0, 0.0]}

    class Intruder:
        def __repr__(self):
            brush.setdefault("_geo_epoch", 1)       # as the logic thread does
            return "Intruder"

    brush["note"] = Intruder()
    panel = SimpleNamespace(_SIGNATURE_IGNORED=PropertyEditor._SIGNATURE_IGNORED,
                            _hashable=PropertyEditor._hashable)
    panel._connection_signature = (
        lambda c: PropertyEditor._connection_signature(panel, c))

    signature = PropertyEditor._object_signature(panel, brush)
    assert signature[0] == "brush"
