"""Big World forced-delta save/load tests.

Exercises the persistent cell delta registry head-less (plain Python, no Qt/GL):
a world is streamed, cells are modified and unloaded, the world is saved, and a
fresh world is loaded — proving that a cell's gameplay changes survive
unload → save → load → re-stream, which is the core Big World invariant.

Run: ``python -m pytest plugins/bigworld/tests/test_bigworld_saves.py`` or
``python plugins/bigworld/tests/test_bigworld_saves.py``.
"""

import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from engine import savegame                       # noqa: E402
from plugins.bigworld import persistence          # noqa: E402
from plugins.bigworld.runtime import BigWorldSession  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeThing:
    def __init__(self, tid, ttype, pos, props=None):
        self.pos = list(pos)
        self.properties = dict(props or {})
        self.properties["id"] = tid
        self.properties.setdefault("type", ttype)

    def to_dict(self):
        props = {k: v for k, v in self.properties.items() if k != "_io_connections"}
        return {"type": self.properties.get("type"), "pos": list(self.pos),
                "properties": props, "io_connections": []}


class FakePlayer:
    def __init__(self, pos):
        self.pos = list(pos)
        self.velocity = [0.0, 0.0, 0.0]
        self.angle = 0.0
        self.pitch = 0.0
        self.camera_height = 40.0
        self.physics_enabled = True
        self.on_ground = True
        self.in_water = False
        self.swimming = False


class FakeEditorState:
    def __init__(self, logic):
        self.logic = logic

    def get_level_data(self):
        return {
            "version": 3,
            "brushes": [copy.deepcopy(b) for b in self.logic.brushes],
            "things": [t.to_dict() for t in self.logic.things],
        }


class FakeMonsterAI:
    def __init__(self):
        self.monster_states = {}


class FakeLogic:
    def __init__(self, things, brushes, player_pos):
        self.play_mode = True
        self.things = things
        self.brushes = brushes
        self.terrain = None
        self.editor_state = FakeEditorState(self)
        self.player = FakePlayer(player_pos)
        self.player2 = None
        self.god_mode = False
        self.buddha_mode = False
        self.notarget = False
        self.camera_mode = "First Person"
        self.overhead_height = 800.0
        self.overhead_tilt = 0.0
        self.overhead_orientation = "north"
        self.active_weapon = None
        self.current_hud_message = ""
        self.player_health = 100
        self.player_max_health = 100
        self.player_dead = False
        self.player2_health = 100
        self.player2_max_health = 100
        self.player2_dead = False
        self.collected_keys = set()
        self.collected_pickups = set()
        self.door_states = {}
        self.mover_states = {}
        self.monster_ai = FakeMonsterAI()
        self._monster_things = [t for t in things if t.properties.get("type") == "monster"]
        self._bigworld = None

    def _build_entity_caches(self):
        pass


# Cell A around x=100 (cell 0,0); Cell B around x=6000 (cell 11,0). >2 cells and
# far beyond the small activation radius used below, so moving A→B unloads A.
A_POS = (100.0, 0.0, 100.0)
B_POS = (6000.0, 0.0, 100.0)


def make_world():
    things = [
        FakeThing("A-mon", "monster", [110.0, 0.0, 110.0], {"health": 50}),
        FakeThing("A-key", "pickup", [120.0, 0.0, 90.0], {"pickup_type": "gold"}),
        FakeThing("A-light", "light", [100.0, 60.0, 100.0], {"radius": 100.0}),
        FakeThing("B-mon", "monster", [6010.0, 0.0, 110.0], {"health": 80}),
        FakeThing("B-key", "pickup", [6020.0, 0.0, 90.0], {"pickup_type": "silver"}),
        # The Big World opt-in entity (persistent global).
        FakeThing("bw-settings", "bigworldsettings", [0.0, 0.0, 0.0],
                  {"enabled": True, "activation_radius": 600.0,
                   "deactivation_radius": 700.0}),
    ]
    brushes = [
        {"id": "A-door", "pos": [130, 0, 100], "size": [8, 96, 64], "hidden": False},
        {"id": "B-wall", "pos": [6030, 0, 100], "size": [64, 64, 64]},
    ]
    return things, brushes


def new_session(logic):
    s = BigWorldSession(logic, activation_radius=600.0, deactivation_radius=700.0)
    logic._bigworld = s
    return s


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_unloaded_cell_change_survives_in_registry():
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)

    # Modify Cell A while the player is standing in it.
    things[0].properties["dead"] = True            # kill A-mon
    things[1].properties["collected"] = True        # collect A-key
    brushes[0]["hidden"] = True                      # "open" the A door brush

    # Walk to Cell B: Cell A leaves the active set and is committed + parked.
    s.tick(player_pos=B_POS)
    assert "0,0" in s.registry, "Cell A delta not committed on unload"
    a_changed = {t["properties"]["id"] for t in s.registry["0,0"]["things"]}
    assert a_changed == {"A-mon", "A-key"}
    assert {b["id"] for b in s.registry["0,0"]["brushes"]} == {"A-door"}

    # Modify Cell B, now that it is loaded/active.
    things[3].properties["dead"] = True              # kill B-mon

    # commit_all flushes everything (A is unloaded, B is loaded) authoritatively.
    reg = s.commit_all()
    assert "0,0" in reg and "11,0" in reg
    b_changed = {t["properties"]["id"] for t in reg["11,0"]["things"]}
    assert b_changed == {"B-mon"}


def test_light_change_committed_on_unload():
    """A light lives in the manager's separate cell.lights list; a change to it
    must still enter the registry when its cell unloads (not only at save)."""
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)

    # A-light is in Cell A. Change it (a "switched off" light is a real change).
    a_light = {t.properties["id"]: t for t in things}["A-light"]
    a_light.properties["on"] = False

    # Walk to B so Cell A unloads. Assert *before* any commit_all().
    s.tick(player_pos=B_POS)
    assert "0,0" in s.registry
    committed = {t["properties"]["id"] for t in s.registry["0,0"]["things"]}
    assert "A-light" in committed, "light change lost when its cell unloaded"


def test_registry_populated_on_unload_without_save():
    """The invariant, isolated: after a cell unloads, its changes are in the
    persistent registry with no save/commit_all having been called."""
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)
    things[0].properties["dead"] = True     # kill A-mon while in Cell A

    assert s.registry == {}                   # nothing committed yet
    s.tick(player_pos=B_POS)                   # cross into B → Cell A unloads
    # No commit_all() here — purely the on-unload commit.
    assert "0,0" in s.registry
    assert {t["properties"]["id"] for t in s.registry["0,0"]["things"]} == {"A-mon"}


def test_forced_delta_save_structure():
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)
    things[0].properties["dead"] = True
    s.commit_all()

    snap = savegame.build_snapshot(
        logic, map_name="world.json",
        world_mode=savegame.WORLD_MODE_BIGWORLD,
        cell_deltas=s.serialize_registry(),
        base_world=s.base_identity("world.json"))

    assert snap["fio_savegame"] is True
    assert snap["save_version"] == 2
    assert snap["save_mode"] == "delta"          # forced
    assert snap["world_mode"] == "bigworld"
    assert "level" not in snap                     # never a full snapshot
    assert snap["cell_deltas"]                      # non-empty
    assert snap["base_map"]["fingerprint"]
    assert snap["base_map"]["world_mode"] == "bigworld"


def test_full_unload_save_load_reload_sequence():
    """The headline invariant: A modified, A unloaded, B modified, save, load,
    return to A — all of A's (and B's) changes are present."""
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)

    things[0].properties["dead"] = True
    things[1].properties["collected"] = True
    brushes[0]["hidden"] = True
    s.tick(player_pos=B_POS)          # A unloads (committed)
    things[3].properties["dead"] = True  # modify B
    s.commit_all()

    snap = savegame.build_snapshot(
        logic, map_name="world.json",
        world_mode=savegame.WORLD_MODE_BIGWORLD,
        cell_deltas=s.serialize_registry(),
        base_world=s.base_identity("world.json"))

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bw.fiosave")
        savegame.write(path, snap)
        loaded = savegame.read(path)

    # Fresh, pristine world + fresh session; player back near Cell A.
    things2, brushes2 = make_world()
    logic2 = FakeLogic(things2, brushes2, A_POS)
    s2 = new_session(logic2)
    s2.start(player_pos=A_POS)

    report = savegame.restore_auto(logic2, loaded, current_map_name="world.json")
    assert report["mode"] == "delta"
    assert report["world_mode"] == "bigworld"

    by_id = {t.properties["id"]: t for t in logic2.things}
    # Cell A changes present even though A was unloaded when saved.
    assert by_id["A-mon"].properties.get("dead") is True
    assert by_id["A-key"].properties.get("collected") is True
    assert {b["id"]: b for b in logic2.brushes}["A-door"]["hidden"] is True
    # Cell B change present too.
    assert by_id["B-mon"].properties.get("dead") is True
    # The registry is handed to the live session for later streaming.
    assert s2.registry == loaded["cell_deltas"]


def test_registry_converges_when_change_reverts():
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)

    things[0].properties["dead"] = True
    s.commit_all()
    assert "0,0" in s.registry

    # Revert to base: the delta must drop out (current − base == nothing).
    del things[0].properties["dead"]
    s.commit_all()
    assert "0,0" not in s.registry
    assert s.registry == {}


def test_unchanged_world_saves_empty_registry():
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)
    s.commit_all()
    assert s.registry == {}
    snap = savegame.build_snapshot(
        logic, map_name="world.json",
        world_mode=savegame.WORLD_MODE_BIGWORLD,
        cell_deltas=s.serialize_registry(),
        base_world=s.base_identity("world.json"))
    assert snap["cell_deltas"] == {}
    assert snap["save_mode"] == "delta"


def test_wrong_world_fails_safely():
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)
    things[0].properties["dead"] = True
    s.commit_all()
    snap = savegame.build_snapshot(
        logic, map_name="world.json",
        world_mode=savegame.WORLD_MODE_BIGWORLD,
        cell_deltas=s.serialize_registry(),
        base_world=s.base_identity("world.json"))

    # A different world: different UUIDs and name.
    other_things = [
        FakeThing("X-1", "monster", [0, 0, 0], {}),
        FakeThing("X-2", "pickup", [0, 0, 0], {}),
    ]
    other = FakeLogic(other_things, [{"id": "X-b"}], A_POS)
    new_session(other).start(player_pos=A_POS)
    try:
        savegame.restore_auto(other, snap, current_map_name="other.json")
    except ValueError as exc:
        assert "different world" in str(exc)
    else:
        raise AssertionError("expected a ValueError for the wrong base world")


def test_streaming_state_not_mistaken_for_change():
    """A parked (unloaded) but unmodified cell must not appear in the registry."""
    things, brushes = make_world()
    logic = FakeLogic(things, brushes, A_POS)
    s = new_session(logic)
    s.start(player_pos=A_POS)   # Cell B is parked (hidden/disabled markers set)

    # B is parked but nothing about B changed. commit_all must yield nothing for B.
    s.commit_all()
    assert "11,0" not in s.registry
    # Confirm B really is parked (streaming state present on the live object).
    b_mon = {t.properties["id"]: t for t in things}["B-mon"]
    assert b_mon.properties.get("bw_active") is False


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
