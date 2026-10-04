"""Regression tests for the 2.5 final-release audit.

Most of these were found by driving the real editor under a virtual display
and using Tools -> Debug Tables as an oracle: rebuild the dense tables from the
live world, diff them against both published buffers, and watch the
instrument's prepare/tick/AI timings on large maps and long sessions.
"""

import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="editor state and console are editor-tier")

from editor.editor_state import EditorState                    # noqa: E402
from editor.things import Monster, Thing                        # noqa: E402
from engine.entity_table import EntityTable                     # noqa: E402
from engine.render_table import RenderTable                     # noqa: E402
from tests.helpers.worlds import box_brush                      # noqa: E402

pytestmark = pytest.mark.qt


# ---------------------------------------------------------------------------
# Duplicate UUIDs in a loaded map
# ---------------------------------------------------------------------------

def test_loading_a_map_gives_every_object_its_own_id():
    """Maps from older editors hold several brushes under one UUID (the old
    clone copied it): _SHOWCASE.json has six such groups. Undo, I/O targeting,
    saves and the dense tables all key on the id, so the loader must make
    them unique -- keeping the first holder's, so aimed connections still hit
    the same object."""
    a = box_brush("a")
    b = box_brush("b", pos=(128, 0, 0))
    c = box_brush("c", pos=(256, 0, 0))
    b["id"] = c["id"] = a["id"]
    shared = a["id"]
    things = [
        {"type": "light", "pos": [0, 64, 0],
         "properties": {"id": shared, "name": "lamp"}},
        {"type": "light", "pos": [0, 96, 0],
         "properties": {"id": "lamp-2", "name": "lamp2"}},
    ]
    state = EditorState()
    state.load_from_data({"brushes": [a, b, c], "things": things},
                         save_undo=False)

    ids = [brush["id"] for brush in state.brushes]
    ids += [thing.properties["id"] for thing in state.things]
    assert len(set(ids)) == len(ids), ids
    assert state.brushes[0]["id"] == shared
    assert state.things[1].properties["id"] == "lamp-2"

    table = RenderTable()
    table.begin_frame(state.brushes, 1)
    assert len(table.slot_of_id) == table.count == 3


# ---------------------------------------------------------------------------
# Undo / Stop-with-restore publication order
# ---------------------------------------------------------------------------

def test_restore_publishes_the_new_world_before_invalidating(monkeypatch):
    """restore_state (undo, redo, Stop with restore-on-stop) invalidated the
    world first and then rebuilt ``brushes`` in place, so a logic frame landing
    mid-restore rebuilt the dense tables from the outgoing (or half-restored)
    world under the new epoch -- and both buffers rebuilt again afterwards.
    On a 40k-brush map that was ~3.5 s of logic-thread stall per Stop."""
    state = EditorState()
    state.brushes = [box_brush("a"), box_brush("b", pos=(128, 0, 0))]
    state.things = [Thing.from_dict(
        {"type": "light", "pos": [0, 64, 0], "properties": {"name": "lamp"}})]
    snapshot = state.snapshot()
    old_brushes = state.brushes
    epoch_before = state.world_epoch

    seen = []
    real_from_dict = Thing.from_dict

    def from_dict_mid_restore(data):
        # What a logic frame taken right now would see.
        seen.append((state.world_epoch, state.brushes, len(state.brushes)))
        return real_from_dict(data)

    monkeypatch.setattr(Thing, "from_dict", staticmethod(from_dict_mid_restore))
    state.restore_state(snapshot)

    assert seen, "restore did not rebuild any Thing"
    for epoch, brushes, count in seen:
        assert epoch == epoch_before, "invalidated before the new world existed"
        assert brushes is old_brushes and count == 2, "a half-built world was visible"
    assert state.world_epoch > epoch_before
    assert state.brushes is not old_brushes and len(state.brushes) == 2


def _count_reconciles(monkeypatch, cls):
    calls = []
    real = cls._reconcile

    def spy(self, *args, **kwargs):
        calls.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "_reconcile", spy)
    return calls


def test_second_buffer_adopts_a_peer_that_is_one_precise_edit_behind(monkeypatch):
    """After a global invalidation both buffers need every row. The first
    rebuilds; the second copies it -- but only when the two were at the same
    epoch, so one checkpoint between their frames made it rebuild too."""
    state = EditorState()
    state.brushes = [box_brush("b%d" % i, pos=(i * 96, 0, 0)) for i in range(12)]
    state.mark_world_changed()                              # global

    first, second = RenderTable(), RenderTable()
    epoch1, dirty = state.render_dirty_since(None)
    first.begin_frame(state.brushes, epoch1, dirty_objects=dirty)

    edited = state.brushes[3]
    edited["textures"] = dict(edited["textures"], top="Dev/other.jpg")
    state.mark_world_changed([edited])                      # precise

    reconciles = _count_reconciles(monkeypatch, RenderTable)
    epoch2, dirty2 = state.render_dirty_since(second._epoch)
    assert dirty2 is None                  # the second buffer must rebuild...
    peer_dirty = state.render_dirty_since(first._epoch)[1]
    second.begin_frame(state.brushes, epoch2, dirty_objects=dirty2,
                       peer=first, peer_dirty=peer_dirty)
    assert reconciles == []                # ...and copies its peer instead

    fresh = RenderTable()
    fresh.begin_frame(state.brushes, epoch2)
    names = second.texture_names()
    fresh_names = fresh.texture_names()
    for slot in range(fresh.count):
        assert ([names[i] for i in second.tex_name_id[slot]]
                == [fresh_names[i] for i in fresh.tex_name_id[slot]]), slot
    assert (second.bounds[:fresh.count] == fresh.bounds[:fresh.count]).all()
    # A copy, never a share.
    assert second.tex_name_id is not first.tex_name_id


def test_entity_buffer_adopts_a_peer_that_is_one_precise_edit_behind(monkeypatch):
    state = EditorState()
    state.things = [Monster(pos=[i * 64.0, 0.0, 0.0],
                            properties={"name": "m%d" % i, "id": "m%d" % i})
                    for i in range(6)]
    state.mark_world_changed()

    first, second = EntityTable(), EntityTable()
    epoch1, _ = state.render_dirty_since(None)
    first.begin_frame(state.things, epoch1)

    victim = state.things[2]
    victim.properties["dead"] = True
    state.mark_world_changed([victim])

    reconciles = _count_reconciles(monkeypatch, EntityTable)
    epoch2, dirty2 = state.render_dirty_since(second._epoch)
    second.begin_frame(state.things, epoch2, dirty_objects=dirty2, peer=first,
                       peer_dirty=state.render_dirty_since(first._epoch)[1])
    assert reconciles == []

    sprite = second.sprite_recipes()[second.sprite_key_id[2]]
    assert any(candidate[1] == "dead.png" for candidate in sprite), sprite


def test_a_logic_frame_after_undo_rebuilds_the_tables_once(monkeypatch):
    """End to end through LogicThread: restore, then a precise checkpoint
    before the second buffer's frame; exactly one full rebuild."""
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = [box_brush("b%d" % i, pos=(i * 96, 0, 0)) for i in range(20)]
    thread = LogicThread(ThreadedGameState(), state)
    try:
        for _ in range(2):                                  # settle both buffers
            thread.render_runtime.prepare_render_state()
            thread.game_state.request_swap()
        snapshot = state.snapshot()

        reconciles = _count_reconciles(monkeypatch, RenderTable)
        state.restore_state(snapshot)                       # e.g. Stop/undo
        thread.render_runtime.prepare_render_state()
        thread.game_state.request_swap()
        state.mark_world_changed([state.brushes[0]])        # e.g. a selection edit
        for _ in range(2):
            thread.render_runtime.prepare_render_state()
            thread.game_state.request_swap()
        assert len(reconciles) == 1, reconciles
        for buffer in (thread.game_state._read_state, thread.game_state._write_state):
            assert buffer.render_table.brushes == state.brushes
    finally:
        thread.stop()


# ---------------------------------------------------------------------------
# Console: monster_revive / monster_revive_all
# ---------------------------------------------------------------------------

def _console(things, states):
    from editor.console_commands import ConsoleCommandHandler
    from editor.main_window import MainWindow
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    window = MainWindow(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    window.state.things = list(things)
    game_state = ThreadedGameState()
    logic = LogicThread(game_state, window.state)
    logic.world_runtime.monster_things = list(things)
    logic.monster_ai.monster_states = states
    window.view_3d.logic_thread = logic
    window.view_3d.play_mode = True
    return ConsoleCommandHandler(window), window, logic


def test_monster_revive_drops_the_ai_state_of_that_monster():
    """The AI state lives on MonsterAI; the command reset ``lt.monster_states``
    (no such attribute), so a revived monster kept its near-zero shoot timer
    and fired the instant it came back."""
    grunt = Monster(pos=[0.0, 0.0, 0.0],
                    properties={"name": "grunt", "dead": True, "health": 0})
    other = Monster(pos=[64.0, 0.0, 0.0], properties={"name": "other"})
    states = {id(grunt): {"shoot_timer": 0.01}, id(other): {"shoot_timer": 0.5}}
    handler, window, logic = _console([grunt, other], states)
    try:
        handler.cmd_monster_revive("grunt")
    finally:
        logic.stop()
        window.unsaved_changes = False
        window.close()
        window.deleteLater()

    assert id(grunt) not in states
    assert id(other) in states
    assert grunt.properties["dead"] is False


def test_monster_revive_all_clears_the_ai_state():
    monsters = [Monster(pos=[i * 64.0, 0.0, 0.0],
                        properties={"name": "m%d" % i, "dead": True})
                for i in range(3)]
    states = {id(m): {"shoot_timer": 0.0} for m in monsters}
    handler, window, logic = _console(monsters, states)
    try:
        handler.cmd_monster_revive_all("")
    finally:
        logic.stop()
        window.unsaved_changes = False
        window.close()
        window.deleteLater()
    assert states == {}


def test_monster_revive_keeps_a_parked_monster_parked():
    """Revive wrote ``hidden`` directly; on a Big World-parked monster that is
    undone the moment its cell returns (or unparks it early)."""
    from engine.spatial import PARKED_HIDDEN_KEY, authored_hidden

    grunt = Monster(pos=[0.0, 0.0, 0.0],
                    properties={"name": "grunt", "dead": True, "hidden": True,
                                PARKED_HIDDEN_KEY: True})
    handler, window, logic = _console([grunt], {})
    try:
        handler.cmd_monster_revive("grunt")
    finally:
        logic.stop()
        window.unsaved_changes = False
        window.close()
        window.deleteLater()

    assert grunt.properties["hidden"] is True          # still parked
    assert authored_hidden(grunt) is False             # but authored visible


# ---------------------------------------------------------------------------
# Monster AI session lifetime
# ---------------------------------------------------------------------------

def test_leaving_play_releases_the_sessions_monsters():
    """The MonsterTable (and the nearest-enemy batch) kept the last session's
    monster objects until the next Play -- across a map load -- and Debug
    Tables showed that stale table as live in the editor."""
    import gc
    import weakref

    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    state.things = [Monster(pos=[i * 200.0, 64.0, 0.0],
                            properties={"name": "m%d" % i, "team": "red" if i % 2 else "blue",
                                        "awake": True})
                    for i in range(4)]
    thread = LogicThread(ThreadedGameState(), state)
    try:
        thread.world_runtime.build_entity_caches()
        ai = thread.monster_ai
        ai.update(1.0 / 30.0)                 # no player: nothing gathered yet
        ai.table.gather(thread.world_runtime.monster_things)
        ai._enemy_monsters = tuple(thread.world_runtime.monster_things)
        ai.monster_states = {id(m): {} for m in thread.world_runtime.monster_things}
        refs = [weakref.ref(m) for m in state.things]

        thread.session_runtime.apply_play_mode(False)                     # Stop
        assert ai.table.count == 0 and ai.table.monsters == []
        assert ai.monster_states == {} and ai._enemy_monsters == ()

        thread.world_runtime.monster_things = []
        state.things = []
        gc.collect()
        assert all(ref() is None for ref in refs)
    finally:
        thread.stop()


# ---------------------------------------------------------------------------
# I/O dispatched outside a play session (console ent_fire / send / trigger)
# ---------------------------------------------------------------------------

def test_editor_io_reaches_the_live_entity():
    """The I/O manager resolves targets through the LogicThread's caches,
    which only Play builds: in the editor an input failed with "not found",
    or -- after a session -- reached that session's replaced objects."""
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    def lamp():
        return Thing.from_dict({"type": "light", "pos": [0, 64, 0],
                                "properties": {"name": "lamp", "id": "lamp-id",
                                               "state": "on"}})

    state = EditorState()
    state.things = [lamp()]
    thread = LogicThread(ThreadedGameState(), state)
    try:
        thread.io_manager._execute_input("lamp", "TurnOff", "", "console",
                                         target_id="lamp-id")
        assert state.things[0].properties["state"] == "off"

        # A session's caches, then the world replaced (restore / map load).
        thread.world_runtime.build_entity_caches()
        ghost = state.things[0]
        state.things = [lamp()]
        thread.io_manager._execute_input("lamp", "TurnOff", "", "console",
                                         target_id="lamp-id")
        assert state.things[0].properties["state"] == "off"
        assert thread.world_runtime.find_entity_by_name("lamp") is not ghost
    finally:
        thread.stop()


# ---------------------------------------------------------------------------
# Invariant: nothing the runtime owns outlives its play session / its map
# ---------------------------------------------------------------------------

def _session_world(state):
    objs = {}
    for brush in state.brushes:
        objs[id(brush)] = brush
    for thing in state.things:
        objs[id(thing)] = thing
        objs[id(thing.properties)] = thing
    return objs


def _play(state, ticks=240):
    """A real headless session: walk, turn, shoot and use, with the AI."""
    from engine.logic_player import KEY_D, KEY_W
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.threaded_game_state import ThreadedGameState
    from editor.things import PlayerStart

    game_state = ThreadedGameState()
    logic = LogicThread(game_state, state)
    start = next((t for t in state.things if isinstance(t, PlayerStart)), None)
    pos = start.pos if start is not None else [0.0, 64.0, 0.0]
    logic.player_runtime.player = Player(pos[0], pos[2])
    logic.player_runtime.player.pos.y = pos[1]
    logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.stop_monster_ai()                   # this test drives the AI itself
    logic.player_runtime.god_mode = True                      # play start resets it
    seen = {}
    for tick in range(ticks):
        game_state.set_keys([{KEY_W}, {KEY_W, KEY_D}, set(), {KEY_D}][(tick // 40) % 4])
        game_state.set_mouse_delta(15.0, 0.0)
        if tick % 15 == 0:
            game_state.queue_shot()
        if tick % 25 == 0:
            game_state.set_use_key_pressed()
        with logic._tick_lock:
            logic._tick(logic.TICK_DURATION)
        with logic.session_runtime.monster_lock:
            logic.monster_ai.update(logic.TICK_DURATION)
        logic.render_runtime.prepare_render_state()
        game_state.request_swap()
        if tick % 40 == 0:
            seen.update(_session_world(state))       # includes the spawned/killed
    seen.update(_session_world(state))
    return logic, game_state, seen


@pytest.mark.parametrize("map_name", [
    "_SHOWCASE.json", "MonsterTest.json", "Portal_Test.json",
    "Spawner_Test.json", "BigWorld_streaming_test.json",
])
@pytest.mark.parametrize("restore", [False, True], ids=["keep", "restore"])
def test_no_runtime_cache_outlives_its_session_or_map(map_name, restore):
    """After Stop no LogicThread/MonsterAI-owned cache may reference an object
    of the finished session, and after a map load no runtime cache may
    resolve an entity of the previous map. Before the fix the per-type entity
    lists, the name/id caches, the collision set, the MonsterTable, the AI's
    enemy batch, the queued I/O and the player's ground brush all did."""
    import os

    from tests.helpers.paths import REPO_ROOT
    from tests.helpers.reachability import paths_to

    state = EditorState()
    with open(os.path.join(REPO_ROOT, "maps", map_name), encoding="utf-8") as f:
        state.load_from_data(json.load(f), save_undo=False)
    before_play = state.snapshot() if restore else None
    logic, game_state, session = _play(state)
    try:
        # Work still in flight at Stop: a delayed event and a cross-thread output.
        io = logic.io_manager
        source = next(t for t in state.things)
        io.pending_events.append(SimpleNamespace(connection=source))
        io._foreign_outputs.append((source, "OnTrigger", None, None))
        names = [obj.properties["name"] for obj in session.values()
                 if isinstance(obj, Thing) and obj.properties.get("name")]
        ids = [obj.properties["id"] for obj in session.values()
               if isinstance(obj, Thing) and obj.properties.get("id")]

        logic.session_runtime.apply_play_mode(False)                              # Stop
        if restore:
            state.restore_state(before_play)
        for _ in range(2):
            logic.render_runtime.prepare_render_state()
            game_state.request_swap()
        roots = {"logic": logic, "monster_ai": logic.monster_ai}
        # Without a restore the session's objects *are* the editor's world,
        # which the render projection rightly shows.
        exclude = [state, state.brushes, state.things]
        if not restore:
            exclude += [game_state, game_state.get_write_state().render_table, game_state.get_write_state().entity_table]
        leaks = paths_to(roots, session, exclude)
        assert leaks == [], "after Stop:\n  " + "\n  ".join(leaks[:20])

        state.load_from_data({"brushes": [], "things": []}, save_undo=False)
        for _ in range(2):
            logic.render_runtime.prepare_render_state()
            game_state.request_swap()
        leaks = paths_to(roots, session, [state, state.brushes, state.things])
        assert leaks == [], "after a map load:\n  " + "\n  ".join(leaks[:20])
        assert [n for n in names if logic.world_runtime.find_entity_by_name(n)] == []
        assert [i for i in ids if logic.world_runtime.find_entity_by_id(i)] == []
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Entities added or removed during a session
# ---------------------------------------------------------------------------

def _playing(things=(), brushes=()):
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = list(brushes) or [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    state.things = list(things)
    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player = Player(0.0, 0.0)
    logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.stop_monster_ai()                   # deterministic: no AI thread
    return state, logic


def test_plugin_despawn_and_spawn_update_the_sessions_entity_index():
    """PluginAPI.spawn/despawn changed the thing list without telling the
    session: a despawned monster kept being simulated (and shooting) from the
    stale index, and a spawned one had no AI and no name for I/O."""
    from plugins.api import RuntimeAPI

    grunt = Monster(pos=[300.0, 64.0, 0.0], properties={"name": "grunt"})
    state, logic = _playing([grunt])
    try:
        from plugins.manager import get_manager, load_plugins
        load_plugins()
        manager = get_manager()
        plugin = manager.find_plugin("tidy")
        if plugin is None:
            pytest.skip("Tidy plugin is not present in this build")
        api = RuntimeAPI(manager, logic, plugin)
        logic.monster_ai.monster_states[id(grunt)] = {"shoot_timer": 0.0}

        assert api.despawn(grunt) is True
        assert grunt not in logic.world_runtime.monster_things
        assert logic.world_runtime.find_entity_by_name("grunt") is None
        assert id(grunt) not in logic.monster_ai.monster_states

        spawned = api.spawn(Monster, (100.0, 64.0, 0.0), {"name": "fresh"})
        assert spawned in logic.world_runtime.monster_things
        assert logic.world_runtime.find_entity_by_name("fresh") is spawned
    finally:
        logic.stop()


def test_console_delete_during_play_removes_the_entity_from_the_session(main_window):
    from editor.console_commands import ConsoleCommandHandler

    grunt = Monster(pos=[300.0, 64.0, 0.0], properties={"name": "grunt"})
    wall = box_brush("wall", (200, 64, 0), (32, 128, 256))
    ground = box_brush("ground", (0, -16, 0), (4096, 32, 4096))
    main_window.state.brushes = [ground, wall]
    main_window.state.things = [grunt]

    logic = LogicThread(ThreadedGameState(), main_window.state)
    main_window.view_3d.logic_thread = logic
    main_window.view_3d.play_mode = True
    logic.player_runtime.player = Player(0.0, 0.0)
    logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.stop_monster_ai()
    console = ConsoleCommandHandler(main_window)
    try:
        console.cmd_delete("grunt")
        assert grunt not in logic.world_runtime.monster_things
        assert logic.world_runtime.find_entity_by_name("grunt") is None

        console.cmd_delete("wall")
        logic._tick(logic.TICK_DURATION)
        assert all(
            b is not wall for b in logic.collision_runtime._collision_brushes_cache
        )
    finally:
        logic.stop()


def test_objects_added_or_removed_in_the_editor_during_play_join_the_session():
    """Clone/paste/place/Delete in the editor during play change the world's
    lists with only an undo checkpoint -- taken before the change."""
    grunt = Monster(pos=[300.0, 64.0, 0.0], properties={"name": "grunt"})
    ground = box_brush("ground", (0, -16, 0), (4096, 32, 4096))
    state, logic = _playing([grunt], [ground])
    try:
        state.save_state()                         # checkpoint, then the edit
        logic._tick(logic.TICK_DURATION)           # a tick lands in between
        added = Monster(pos=[0.0, 64.0, 300.0], properties={"name": "added"})
        wall = box_brush("wall", (200, 64, 0), (32, 128, 256))
        state.things.append(added)
        state.brushes.append(wall)
        logic._tick(logic.TICK_DURATION)
        assert added in logic.world_runtime.monster_things
        assert logic.world_runtime.find_entity_by_name("added") is added
        assert any(b is wall for b in logic.collision_runtime._collision_brushes_cache)

        state.save_state()
        state.things.remove(grunt)
        logic._tick(logic.TICK_DURATION)
        assert grunt not in logic.world_runtime.monster_things
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Save / load: moving brushes come back where they were saved
# ---------------------------------------------------------------------------

def test_loading_a_save_puts_doors_and_movers_back_where_they_were(tmp_path):
    """Only ``progress`` was restored, and a door or mover is repositioned from
    it only while moving: a door saved closed but open when the save was
    loaded stayed open -- drawn and solid in the wrong place -- and a stopped
    mover stayed wherever it had got to. Found by a save/continue/load oracle
    over every shipped map."""
    door = box_brush("door", (0, 64, 300), (128, 128, 16), is_door=True,
                     door_speed=256.0, door_distance=128.0, door_direction="up",
                     open_time=30.0)
    lift = box_brush("lift", (400, 16, 0), (128, 32, 128), is_mover=True,
                     start_on=False, speed=128.0, distance=256.0,
                     direction=[0, 1, 0])
    state, logic = _playing(brushes=[box_brush("ground", (0, -16, 0), (4096, 32, 4096)),
                                     door, lift])
    try:
        saved_door, saved_lift = list(door["pos"]), list(lift["pos"])
        path = str(tmp_path / "s.fiosave")
        assert logic.session_runtime.save_session(path)[0]

        door_idx = state.brushes.index(door)
        logic.mover_runtime._trigger_door_open(door_idx, door)
        lift["start_on"] = True
        from engine.change_journal import moved
        moved(lift)                                 # I/O Start
        for _ in range(60):
            logic._tick(logic.TICK_DURATION)
        lift["start_on"] = False                    # I/O Stop, mid-travel
        moved(lift)
        logic._tick(logic.TICK_DURATION)
        assert door["pos"] != saved_door and lift["pos"] != saved_lift

        assert logic.session_runtime.load_session(path)[0]
        for _ in range(30):                         # and it stays there
            logic._tick(logic.TICK_DURATION)
        assert door["pos"] == pytest.approx(saved_door)
        assert lift["pos"] == pytest.approx(saved_lift)
        assert logic.mover_runtime.door_states[door_idx]["state"] == "closed"
        assert lift["start_on"] is False
    finally:
        logic.stop()


def _kill_after_save_then_load(tmp_path, save_mode):
    from engine import savegame

    alive = Monster(pos=[300.0, 64.0, 0.0],
                    properties={"name": "alive", "id": "alive-id", "health": 50})
    state, logic = _playing([alive])
    try:
        base = savegame.normalize_base_level(state.get_level_data())
        path = str(tmp_path / "s.fiosave")
        assert logic.session_runtime.save_session(path, save_mode=save_mode, base_level=base)[0]

        alive.properties.update(dead=True, health=0, _aggro_target=123)
        ok, msg = logic.session_runtime.load_session(path, base_level=base)
        assert ok, msg
        return alive
    finally:
        logic.stop()


@pytest.mark.parametrize("save_mode", ["full", "delta", "both"])
def test_loading_a_save_revives_a_monster_killed_after_it(tmp_path, save_mode):
    """The overlay wrote the saved properties but kept any the object gained
    later, so a monster killed after the save stayed dead through loading it;
    a delta (which omits what matched the base map) never revisited it."""
    monster = _kill_after_save_then_load(tmp_path, save_mode)
    assert not monster.properties.get("dead", False)
    assert monster.properties["health"] == 50
    assert "_aggro_target" not in monster.properties


def test_deleting_a_brush_in_play_keeps_io_aimed_at_the_right_door():
    """Door/mover state is keyed by brush index, taken at Play start, and I/O
    finds a door by its current index: deleting an earlier brush shifted
    every later one, so Open aimed at door B opened door A."""
    ground = box_brush("ground", (0, -16, 0), (4096, 32, 4096))
    crate = box_brush("crate", (500, 32, 500), (64, 64, 64))
    door_a = box_brush("door_a", (0, 64, 300), (128, 128, 16), is_door=True,
                       door_speed=512.0, door_distance=128.0, door_direction="up",
                       open_time=30.0)
    door_b = box_brush("door_b", (300, 64, 300), (128, 128, 16), is_door=True,
                       door_speed=512.0, door_distance=128.0, door_direction="up",
                       open_time=30.0)
    lift = box_brush("lift", (-400, 16, 0), (128, 32, 128), is_mover=True,
                     start_on=True, speed=64.0, distance=256.0, direction=[0, 1, 0])
    state, logic = _playing(brushes=[ground, crate, door_a, door_b, lift])
    try:
        for _ in range(20):
            logic._tick(logic.TICK_DURATION)
        lift_progress = logic.mover_runtime.mover_states[state.brushes.index(lift)]["progress"]
        assert lift_progress > 0

        state.save_state()
        state.brushes.remove(crate)                  # editor Delete during play
        logic._tick(logic.TICK_DURATION)

        closed_a = list(door_a["pos"])
        logic.io_manager._execute_input("door_b", "Open", "", "test",
                                        target_id=door_b["id"])
        for _ in range(30):
            logic._tick(logic.TICK_DURATION)
        assert door_a["pos"] == closed_a, "the wrong door opened"
        assert door_b["pos"][1] > 64.0 + 100.0, "the aimed door did not open"
        # The mover carried its progress across the re-index.
        assert logic.mover_runtime.mover_states[state.brushes.index(lift)]["progress"] >= lift_progress
    finally:
        logic.stop()


def test_the_sound_queue_keeps_only_recent_requests_while_the_ui_is_stalled():
    """Found by a 5000-tick run with 1000 monsters: nothing but the UI drains
    the queue, so a stalled UI let it grow without bound and then play every
    stale sound at once."""
    from engine.threaded_game_state import ThreadedGameState

    game_state = ThreadedGameState()
    limit = ThreadedGameState.SOUND_QUEUE_LIMIT
    for i in range(limit * 4):
        game_state.queue_sound({"file": "shot.mp3", "n": i})
    drained = game_state.consume_sounds()
    assert len(drained) == limit
    assert drained[-1]["n"] == limit * 4 - 1          # the newest survive
    assert game_state.consume_sounds() == ()


def test_a_level_changer_can_be_cloned_and_copied_in_the_editor(qt_app):
    """LevelChanger stored the MainWindow on itself, so clone (Shift+Space)
    and copy (Ctrl+C), which deep-copy entities, raised on any LevelChanger
    in a running editor. Found by cloning entities during play."""
    import copy

    from PyQt5.QtWidgets import QMainWindow

    from editor.things import LevelChanger

    class MainWindow(QMainWindow):
        pass

    window = MainWindow()
    try:
        changer = LevelChanger(pos=[0.0, 0.0, 0.0], properties={"name": "exit"})
        clone = changer.duplicate(existing_names={"exit"})
        assert clone.properties["name"] == "exit (copy)"
        copy.deepcopy(changer)
    finally:
        window.deleteLater()
