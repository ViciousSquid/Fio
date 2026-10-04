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
    from editor.main_window import MainWindow
    from tests.helpers.paths import REPO_ROOT

    window = MainWindow(str(REPO_ROOT))
    window.state = state
    window.view_3d.logic_thread = logic
    window.view_3d.play_mode = True
    handler = ConsoleCommandHandler(window)
    return handler, window


def _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                       lift_progress):
    for index, brush in logic.mover_runtime.doors + logic.mover_runtime.movers:
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
    assert (logic.mover_runtime.mover_states[state.brushes.index(lift)]["progress"]
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
        progress = logic.mover_runtime.mover_states[state.brushes.index(lift)]["progress"]

        _console(state, logic)[0].cmd_delete("crate")
        logic._tick(logic.TICK_DURATION)

        _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                           progress)
        assert all(b is not crate for b in logic.collision_runtime._collision_brushes_cache)
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
        progress = logic.mover_runtime.mover_states[state.brushes.index(lift)]["progress"]

        state.save_state()
        state.brushes.remove(crate)                      # editor Delete
        _console(state, logic)[0].cmd_set_property("lift label up")
        logic._tick(logic.TICK_DURATION)

        _assert_doors_follow_their_brushes(state, logic, door_a, door_b, lift,
                                           progress)
        assert all(b is not crate for b in logic.collision_runtime._collision_brushes_cache), (
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
        _console(state, logic)[0].cmd_set_property("lamp hidden false")
        assert lamp.properties["hidden"] is False
        logic.render_runtime.prepare_render_state()
        table = logic.game_state.get_write_state().entity_table
        assert not table.hidden[table.slot_of_id[lamp.properties["id"]]], (
            "setprop ... hidden false published the lamp as hidden")

        _console(state, logic)[0].cmd_set_property("lamp hidden TRUE")
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
        console = _console(state, logic)[0]
        console.cmd_set_property("wall hidden true")
        logic._tick(logic.TICK_DURATION)
        assert wall["hidden"] is True
        assert all(b is not wall for b in logic.session_runtime.spatial_grid._all_solid), (
            "a wall hidden with setprop is still solid")

        console.cmd_set_property("wall hidden false")
        logic._tick(logic.TICK_DURATION)
        assert any(b is wall for b in logic.session_runtime.spatial_grid._all_solid)
    finally:
        logic.stop()


def test_setprop_keeps_text_properties_as_text():
    from editor.things import Light

    lamp = Light(pos=[0.0, 100.0, 0.0], properties={"name": "lamp",
                                                     "label": "on"})
    state, logic = _playing([lamp])
    try:
        _console(state, logic)[0].cmd_set_property("lamp label false")
        assert lamp.properties["label"] == "false"
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Console setpos / teleport
# ---------------------------------------------------------------------------

def _setpos_console(logic):
    from editor.console_commands import ConsoleCommandHandler
    from editor.main_window import MainWindow
    from tests.helpers.paths import REPO_ROOT

    window = MainWindow(str(REPO_ROOT))
    window.state = logic.editor_state
    window.view_3d.logic_thread = logic
    window.view_3d.play_mode = True
    window.view_3d.player = logic.player_runtime.player
    return ConsoleCommandHandler(window)


def test_setpos_moves_the_player():
    """setpos/teleport assigned ``player.position``, which Player does not
    have: it reported "Player teleported" and moved nothing."""
    import glm

    state, logic = _playing()
    try:
        logic.player_runtime.player.velocity = glm.vec3(0.0, -300.0, 0.0)
        _setpos_console(logic).handle_command("setpos 100 200 -300")
        assert tuple(logic.player_runtime.player.pos) == (100.0, 200.0, -300.0)
        assert tuple(logic.player_runtime.player.velocity) == (0.0, 0.0, 0.0)
    finally:
        logic.stop()


@pytest.mark.parametrize("coords", ["nan 0 0", "0 inf 0", "0 0 -inf", "1e309 0 0"])
def test_setpos_refuses_non_finite_coordinates(coords):
    state, logic = _playing()
    try:
        before = tuple(logic.player_runtime.player.pos)
        _setpos_console(logic).handle_command(f"teleport {coords}")
        assert tuple(logic.player_runtime.player.pos) == before
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Opening a map that is shaped like one but does not parse
# ---------------------------------------------------------------------------

def _window_host(state):
    """Use the real MainWindow for level replacement regressions."""
    from editor.main_window import MainWindow
    from tests.helpers.paths import REPO_ROOT

    host = MainWindow(str(REPO_ROOT))
    host.state = state
    host.file_path = "maps/open.json"
    host.unsaved_changes = False
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
    try:
        assert host._load_level(corrupt, "maps/corrupt.json") is False

        assert state.brushes == brushes and state.things == things, (
            "a map that failed to parse emptied the open level")
        assert host.file_path == "maps/open.json"
        assert host.unsaved_changes is False
    finally:
        host.unsaved_changes = False
        host.close()
        host.deleteLater()


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
    try:
        host._apply_level_data(level)
        assert state.terrain_data == level["terrain_data"]
        assert host.terrain is not None
    finally:
        host.unsaved_changes = False
        host.close()
        host.deleteLater()


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

        # No event processing: QMenu.clear() deletes the actions it owns at
        # once, and pumping the loop would run other tests' queued events.
        rebuild()
        baseline = len(host.findChildren(QAction))
        for _ in range(20):
            rebuild()
        assert len(host.recent_menu.actions()) == 5
        assert len(host.findChildren(QAction)) == baseline, (
            "Recent Files actions accumulate on every rebuild")
    finally:
        from PyQt5 import sip
        sip.delete(host)                      # now, not via the event loop


# ---------------------------------------------------------------------------
# Debug -> Validate Connections
# ---------------------------------------------------------------------------

def test_validate_connections_names_the_connection_it_reports(main_window, monkeypatch):
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

    main_window.state = state
    main_window.validate_io_connections()

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


# ---------------------------------------------------------------------------
# Console: bind with no arguments
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("accept", [False, True], ids=["cancel", "ok-empty"])
def test_bind_with_no_arguments_opens_the_dialog(main_window, monkeypatch, accept):
    """``bind`` alone is documented to open a key-binding dialog; building it
    called QKeySequenceEdit.setPlaceholderText, which Qt 5 does not have, so
    it raised every time and the dialog never appeared. Found by the hostile
    console fuzz."""
    from PyQt5.QtWidgets import QDialog

    from editor import debug_console
    from editor.console_commands import ConsoleCommandHandler

    shown = []
    monkeypatch.setattr(
        QDialog, "exec_",
        lambda self: (shown.append(self.windowTitle()),
                      QDialog.Accepted if accept else QDialog.Rejected)[1])
    logged = []
    monkeypatch.setattr(debug_console.DebugLogger, "log",
                        lambda self, c, m: logged.append((c, m)))
    ConsoleCommandHandler(main_window).handle_command("bind")
    assert shown == ["Bind Key"]
    if accept:
        assert ("Error", "No key selected") in logged


# ---------------------------------------------------------------------------
# Teleports near a portal
# ---------------------------------------------------------------------------

def _portal_session():
    """Portal_Test.json in play, player physics off so positions are exact."""
    import os

    from editor.editor_state import EditorState
    from editor.things import PlayerStart, Portal
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.threaded_game_state import ThreadedGameState
    from tests.helpers.paths import REPO_ROOT

    state = EditorState()
    with open(os.path.join(REPO_ROOT, "maps", "Portal_Test.json"),
              encoding="utf-8") as f:
        state.load_from_data(json.load(f), save_undo=False)
    logic = LogicThread(ThreadedGameState(), state)
    start = next(t for t in state.things if isinstance(t, PlayerStart))
    logic.player_runtime.player = Player(start.pos[0], start.pos[2])
    logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.stop_monster_ai()
    logic.player_runtime.player.physics_enabled = False
    portal = next(t for t in state.things
                  if isinstance(t, Portal) and t.properties["name"] == "Portal_1")
    n, o = portal.get_normal(), portal.pos
    front = (o[0] + n[0] * 300, o[1] - 60, o[2] + n[2] * 300)
    back = (o[0] - n[0] * 300, o[1] - 60, o[2] - n[2] * 300)
    return state, logic, front, back


def _put(logic, where):
    import glm
    logic.player_runtime.player.pos = glm.vec3(*where)
    logic.player_runtime.player.velocity = glm.vec3(0.0, 0.0, 0.0)


def _at(logic, where):
    return all(abs(a - b) < 1.0 for a, b in zip(logic.player_runtime.player.pos, where))


def test_walking_through_a_portal_still_transits():
    state, logic, front, back = _portal_session()
    try:
        _put(logic, front)
        logic._tick(logic.TICK_DURATION)
        transited = False
        for k in range(1, 13):                     # 50 units a tick
            f = k / 12.0
            step = tuple(a + (b - a) * f for a, b in zip(front, back))
            _put(logic, step)
            logic._tick(logic.TICK_DURATION)
            if not _at(logic, step):               # the portal moved us
                transited = True
                break
        assert transited, "walking through the portal did not transit"
    finally:
        logic.stop()


@pytest.mark.parametrize("route", ["setpos", "trigger_input", "quickload"])
def test_a_teleport_across_a_portal_does_not_transit(route, tmp_path):
    """Portal transit tests the segment from last tick's position to this
    one; a teleport whose straight line crossed an aperture was read as a
    walk through it. `setpos` just behind Portal_1 left the player beside
    Portal_2, and a Teleport trigger or a quickload would do the same."""
    import glm

    from editor.things import PathNode

    state, logic, front, back = _portal_session()
    try:
        if route == "quickload":
            _put(logic, back)
            logic._portal_prev_player_pos = None
            logic._tick(logic.TICK_DURATION)
            ok, msg = logic.session_runtime.save_session(str(tmp_path / "s.fiosave"),
                                         map_name="Portal_Test.json")
            assert ok, msg
        _put(logic, front)
        logic._portal_prev_player_pos = None
        logic._tick(logic.TICK_DURATION)
        assert _at(logic, front)

        if route == "setpos":
            _setpos_console(logic).handle_command("setpos %f %f %f" % back)
        elif route == "trigger_input":
            node = PathNode(pos=list(back), properties={"name": "behind"})
            state.things.append(node)
            logic.world_runtime.build_entity_caches()
            from editor.io_handlers import register_all_input_handlers  # noqa: F401
            handler = logic.io_manager._input_handlers[("trigger", "teleport")]
            handler({"name": "tele", "target_node": "behind"}, "behind", logic)
        else:
            ok, msg = logic.session_runtime.load_session(str(tmp_path / "s.fiosave"),
                                         map_name="Portal_Test.json")
            assert ok, msg
        logic._tick(logic.TICK_DURATION)

        assert _at(logic, back), (
            "a %s just behind Portal_1 sent the player through it, to %s"
            % (route, [round(c) for c in logic.player_runtime.player.pos]))
    finally:
        logic.stop()
