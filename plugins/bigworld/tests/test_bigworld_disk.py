"""Disk-streaming milestone tests: cells are actually freed from memory.

Unlike the in-RAM tests, these prove that an unloaded cell's objects are
*removed from the live scene and freed*, that each cell's base is captured the
first time it streams in, and that a change survives the full
modify → free → reload and modify → save → load → stream cycle purely through
the persistent registry (the freed objects are re-instantiated from the source
and the saved delta re-applied by UUID).

Run: ``python plugins/bigworld/tests/test_bigworld_disk.py`` or
``python -m pytest plugins/bigworld/tests/test_bigworld_disk.py``.
"""

import copy
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from engine import savegame
from engine.logic_camera import LogicCamera
from engine.logic_collision import LogicCollision
from engine.logic_movers import LogicMovers
from engine.logic_portals import LogicPortals
from engine.logic_triggers import LogicTriggers
from engine.logic_timing import LogicTiming
from engine.logic_world import LogicWorld                                    # noqa: E402
from engine.logic_interaction import LogicInteraction               # noqa: E402
from engine.logic_player import LogicPlayer                           # noqa: E402
from engine.logic_session import LogicSession                         # noqa: E402
from engine.logic_combat import LogicCombat                           # noqa: E402
from engine.prop_runtime import PropSession                           # noqa: E402
from engine.view_distance import ViewDistance                                # noqa: E402
from plugins.bigworld.streaming import (DiskStreamingSession,   # noqa: E402
                                        MemoryCellSource)


pytest.importorskip("PyQt5", reason="Big World disk tests exercise real LogicThread/editor state")

pytestmark = pytest.mark.qt

from editor.editor_state import EditorState
from editor.things import Thing
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState


def make_thing(tid, ttype, pos, props=None):
    properties = dict(props or {})
    properties["id"] = tid
    properties.setdefault("type", ttype)
    return Thing(pos=list(pos), properties=properties)


def make_logic(player_pos):
    """A production LogicThread owns the live scene while DiskStreamingSession mutates it."""
    state = EditorState()
    logic = LogicThread(ThreadedGameState(), state)
    if logic.player_runtime.player is None:
        from engine.player import Player
        logic.player_runtime.player = Player(0.0, 0.0)
    logic.player_runtime.player.pos = [float(player_pos[0]),
                                      float(player_pos[1]),
                                      float(player_pos[2])]
    logic.world_runtime.build_entity_caches()
    return logic



A_POS = (100.0, 0.0, 100.0)         # cell (0,0)
B_POS = (10000.0, 0.0, 100.0)       # cell (19,0) — far beyond the load radius


def make_source():
    """A pristine world (the 'disk'): Cell A, far Cell B, a spanning wall, and a
    persistent global. Returned as a MemoryCellSource."""
    things = [
        make_thing("A-mon", "monster", [110.0, 0.0, 110.0], {"health": 50}),
        make_thing("A-key", "prop", [120.0, 0.0, 90.0], {"collect_enabled": True, "collect_type": "key", "collect_key_name": "gold"}),
        make_thing("B-mon", "monster", [10010.0, 0.0, 110.0], {"health": 80}),
        make_thing("gs", "gamestate", [0.0, 0.0, 0.0], {"score": 0}),  # persistent global
    ]
    brushes = [
        {"id": "A-floor", "pos": [100, 0, 100], "size": [64, 8, 64], "hidden": False},
        {"id": "B-floor", "pos": [10000, 0, 100], "size": [64, 8, 64]},
        # A wall centred on the boundary between cell (0,0) and (1,0): spans both.
        {"id": "span-wall", "pos": [512, 0, 100], "size": [200, 96, 8]},
    ]
    return MemoryCellSource(brushes, things, cell_size=512.0)


def new_session(logic, source):
    s = DiskStreamingSession(logic, source, load_radius=600.0, evict_radius=700.0)
    if logic.plugins is None:
        from plugins.manager import get_manager
        logic.plugins = get_manager()
    logic.plugins.services["bigworld"] = s
    logic.plugins.services["savegame.restore"] = s.restore_saved
    return s


def live_ids(logic):
    return {t.properties["id"] for t in logic.editor_state.things}


def live_brush_ids(logic):
    return {b["id"] for b in logic.editor_state.brushes}


def find_thing(logic, tid):
    for t in logic.editor_state.things:
        if t.properties["id"] == tid:
            return t
    return None


# ---------------------------------------------------------------------------
# freeing
# ---------------------------------------------------------------------------

def test_far_cells_never_resident_and_globals_stay():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    ids = live_ids(logic)
    assert "A-mon" in ids and "A-key" in ids     # local cell streamed in
    assert "B-mon" not in ids                      # far cell never loaded
    assert "gs" in ids                             # persistent global resident
    assert "A-floor" in live_brush_ids(logic)
    assert "B-floor" not in live_brush_ids(logic)


def test_leaving_a_cell_frees_its_objects():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    assert "A-mon" in live_ids(logic)

    s.tick(player_pos=B_POS)                        # walk far away
    # Cell A's objects are genuinely gone from the live scene and freed.
    assert "A-mon" not in live_ids(logic)
    assert "A-mon" not in s._live_by_id
    assert "A-floor" not in live_brush_ids(logic)
    # Cell B streamed in.
    assert "B-mon" in live_ids(logic)


def test_base_captured_on_first_stream_in():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    # A's UUIDs have a captured base; B's (never streamed) do not.
    assert ("A-mon" in s._base_by_uuid) and ("A-key" in s._base_by_uuid)
    assert "B-mon" not in s._base_by_uuid


# ---------------------------------------------------------------------------
# the invariant: a change survives free → reload
# ---------------------------------------------------------------------------

def test_change_survives_free_and_reload():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)

    mon = find_thing(logic, "A-mon")
    mon.properties["dead"] = True                   # kill it while Cell A is loaded
    original_obj = mon

    s.tick(player_pos=B_POS)                         # Cell A freed (committed first)
    assert "A-mon" not in s._live_by_id             # truly gone from memory
    assert "0,0" in s.serialize_registry()          # its change is in the registry

    s.tick(player_pos=A_POS)                         # return: Cell A re-streamed
    reloaded = find_thing(logic, "A-mon")
    assert reloaded is not None
    assert reloaded is not original_obj             # a fresh instance from source
    assert reloaded.properties.get("dead") is True  # …yet the change persisted


def test_spanning_brush_freed_only_when_both_cells_leave():
    logic = make_logic((512.0, 0.0, 100.0))          # start on the cell boundary
    s = new_session(logic, make_source())
    s.start(player_pos=(512.0, 0.0, 100.0))
    # The spanning wall is resident and referenced by both cells (0,0) and (1,0).
    assert "span-wall" in live_brush_ids(logic)
    assert s._load_ref["span-wall"] >= 2
    # Nudge so cell (1,0) leaves but (0,0) stays: the wall must remain resident.
    s.tick(player_pos=(120.0, 0.0, 100.0))
    assert "span-wall" in live_brush_ids(logic)
    # Walk far: both cells gone → the wall is finally freed.
    s.tick(player_pos=B_POS)
    assert "span-wall" not in live_brush_ids(logic)


# ---------------------------------------------------------------------------
# save with unloaded cells → load → stream reconstructs
# ---------------------------------------------------------------------------

def _save_dict(logic, s, map_name="world.json"):
    s.commit_all()
    return savegame.build_snapshot(
        logic, map_name=map_name,
        world_mode=savegame.WORLD_MODE_BIGWORLD,
        cell_deltas=s.serialize_registry(),
        base_world=s.base_identity(map_name))


def test_save_includes_unloaded_cell_then_load_streams_it_back():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)

    find_thing(logic, "A-mon").properties["dead"] = True
    find_thing(logic, "A-key").properties["collected"] = True
    s.tick(player_pos=B_POS)                         # Cell A now unloaded/freed
    find_thing(logic, "B-mon").properties["dead"] = True

    snap = _save_dict(logic, s)
    assert snap["save_mode"] == "delta" and snap["world_mode"] == "bigworld"
    # Both cells present though A is not resident at save time.
    assert "0,0" in snap["cell_deltas"] and "19,0" in snap["cell_deltas"]

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "w.fiosave")
        savegame.write(path, snap)
        loaded = savegame.read(path)

    # Fresh world + fresh disk session; restore, which streams cells at the
    # saved player position (Cell A, since the player was at A_POS at save).
    logic2 = make_logic(A_POS)
    s2 = new_session(logic2, make_source())
    report = savegame.restore_auto(logic2, loaded, current_map_name="world.json")
    assert report["world_mode"] == "bigworld"

    # Player at A: Cell A streamed with its saved changes applied.
    assert find_thing(logic2, "A-mon").properties.get("dead") is True
    assert find_thing(logic2, "A-key").properties.get("collected") is True
    assert "B-mon" not in live_ids(logic2)          # B not resident yet

    # Stream over to B: its saved change is applied when the cell loads.
    s2.tick(player_pos=B_POS)
    assert find_thing(logic2, "B-mon").properties.get("dead") is True


def test_load_into_already_started_session():
    """The plugin starts the session at play, then loads a save. Cells streamed
    before the registry was known must still end up with their saved deltas."""
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    find_thing(logic, "A-mon").properties["dead"] = True
    snap = _save_dict(logic, s)

    # Fresh world, session already STARTED (streamed pristine Cell A) before load.
    logic2 = make_logic(A_POS)
    s2 = new_session(logic2, make_source())
    s2.start(player_pos=A_POS)
    assert find_thing(logic2, "A-mon").properties.get("dead") is None  # pristine

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "w.fiosave")
        savegame.write(path, snap)
        loaded = savegame.read(path)
    savegame.restore_auto(logic2, loaded, current_map_name="world.json")
    # The already-streamed cell now carries the saved change.
    assert find_thing(logic2, "A-mon").properties.get("dead") is True


def test_registry_converges_on_revert_in_loaded_cell():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    mon = find_thing(logic, "A-mon")
    mon.properties["dead"] = True
    s.commit_all()
    assert "0,0" in s.serialize_registry()
    del mon.properties["dead"]                       # revert to base
    s.commit_all()
    assert s.serialize_registry() == {}


def test_wrong_world_fails_safely():
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    find_thing(logic, "A-mon").properties["dead"] = True
    snap = _save_dict(logic, s)

    # A different world: different UUIDs and name.
    other_things = [make_thing("X-1", "monster", [100, 0, 100], {})]
    other_brushes = [{"id": "X-f", "pos": [100, 0, 100], "size": [64, 8, 64]}]
    other_src = MemoryCellSource(other_brushes, other_things, cell_size=512.0)
    logic2 = make_logic(A_POS)
    s2 = new_session(logic2, other_src)
    try:
        savegame.restore_auto(logic2, snap, current_map_name="other.json")
    except ValueError as exc:
        assert "different world" in str(exc)
    else:
        raise AssertionError("expected a ValueError for the wrong world")


def test_reload_after_revert_shows_base():
    """Free a cell with a change, revert it via a second visit, and confirm the
    registry no longer carries it (delta cleared, base restored on reload)."""
    logic = make_logic(A_POS)
    s = new_session(logic, make_source())
    s.start(player_pos=A_POS)
    find_thing(logic, "A-mon").properties["dead"] = True
    s.tick(player_pos=B_POS)                          # freeze the change into registry
    assert "0,0" in s.serialize_registry()

    s.tick(player_pos=A_POS)                          # reload; delta re-applied
    assert find_thing(logic, "A-mon").properties.get("dead") is True
    del find_thing(logic, "A-mon").properties["dead"]  # revert
    s.tick(player_pos=B_POS)                           # commit-on-unload converges
    assert "0,0" not in s.serialize_registry()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
