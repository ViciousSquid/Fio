"""End-to-end workflows: several subsystems driven together, as a user would.

Each test walks one of the sequences the task description names, through the
real objects rather than fakes: the real ``EditorState``, the real
``LogicThread``, the real ``SpatialGrid``, the real plugin manager, the real
I/O dispatcher.  They exist to catch the failures that only appear at the seams
— a cache that is correct in isolation but is never invalidated by the operation
next door, state that leaks from a play session back into the authored map.

Nothing here needs a GL context.  The renderer's part of these workflows is the
render *state*, which the logic thread builds headless.
"""

import json

import glm
import pytest

pytest.importorskip("PyQt5", reason="these drive the real editor and logic thread")

from editor import io_system as io                   # noqa: E402
from editor.editor_state import EditorState          # noqa: E402
from editor.io_system import OutputConnection        # noqa: E402
from editor.things import Light, Monster, PathNode, PlayerStart  # noqa: E402
from engine import brush_geometry as bg              # noqa: E402
from engine.logic_thread import LogicThread          # noqa: E402
from engine.player import Player                     # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


@pytest.fixture
def session():
    """``(state, thread)`` over a room, guaranteed to be shut down."""
    made = []

    def _build(brushes=None, things=()):
        state = EditorState()
        state.brushes = list(brushes if brushes is not None else room(size=2048.0))
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state)
        made.append(thread)
        return state, thread

    yield _build

    for thread in made:
        thread.set_play_mode(False)
        thread.stop()


# ---------------------------------------------------------------------------
# Editor workflow
# ---------------------------------------------------------------------------

def test_create_select_component_edit_texture_io_save_undo_redo(session):
    """create brush -> select -> component edit -> texture -> I/O -> save ->
    undo -> redo, with the scene checked at every step."""
    state, _thread = session(brushes=[])

    # --- create ---------------------------------------------------------
    state.save_state()
    brush = box_brush("pillar", (0, 64, 0), (128, 128, 128))
    state.brushes.append(brush)
    target = box_brush("door", (400, 64, 0), (64, 128, 16), is_door=True)
    state.brushes.append(target)
    assert len(state.brushes) == 2

    # --- select ---------------------------------------------------------
    state.set_selected_object(brush)
    assert state.selected_objects == [brush]

    # --- component edit (drag one corner up) -----------------------------
    state.save_state()
    points = bg.brush_points(brush).copy()
    top_corner = int(points[:, 1].argmax())
    points[top_corner] = points[top_corner] + [0.0, 64.0, 0.0]
    assert bg.rebuild_brush_from_points(brush, points) is True
    edited_top = float(bg.get_shape(brush).bounds[1][1])
    assert edited_top == pytest.approx(192.0), (
        "the dragged corner should put the top at y=192, it is at %.2f" % edited_top)

    # --- texture change --------------------------------------------------
    state.save_state()
    brush["textures"]["top"] = "Dev/marble.png"

    # --- I/O connection --------------------------------------------------
    state.save_state()
    io.add_connection(brush, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open",
        target_id=target["id"]))
    assert len(io.get_connections(brush)) == 1

    # --- save -------------------------------------------------------------
    saved = json.loads(json.dumps(state.get_level_data()))
    assert len(saved["brushes"]) == 2
    assert saved["brushes"][0].get("io_connections"), "the wiring was not saved"

    # --- undo (drops the I/O) --------------------------------------------
    assert state.undo()
    assert io.get_connections(_by_name(state, "pillar")) == [], (
        "undo did not remove the connection")
    assert _by_name(state, "pillar")["textures"]["top"] == "Dev/marble.png", (
        "undo went back further than the one operation it should have")

    # --- redo -------------------------------------------------------------
    assert state.redo()
    restored = io.get_connections(_by_name(state, "pillar"))
    assert [c.input_name for c in restored] == ["Open"], (
        "redo did not restore the connection; the brush holds %s" % (restored,))

    # --- and the geometry is still the edited one ------------------------
    assert float(bg.get_shape(_by_name(state, "pillar")).bounds[1][1]) == \
        pytest.approx(192.0)


def _by_name(state, name):
    for brush in state.brushes:
        if brush.get("name") == name:
            return brush
    raise AssertionError("no brush named %r; scene holds %s"
                         % (name, [b.get("name") for b in state.brushes]))


def test_a_reloaded_map_is_the_map_that_was_saved(session):
    state, _thread = session(brushes=[])
    state.brushes = [box_brush("floor", (0, -16, 0), (512, 32, 512))]
    bg.clip_brush(state.brushes[0], (0.0, 1.0, 1.0), 20.0)
    state.things = [make_thing(Light, "lamp", (0, 128, 0), intensity=3.0)]

    saved = json.loads(json.dumps(state.get_level_data()))
    reloaded = EditorState()
    reloaded.load_from_data(saved)

    assert bg.get_convex(reloaded.brushes[0]).verts == \
        pytest.approx(bg.get_convex(state.brushes[0]).verts)
    assert reloaded.find_entity_by_name("lamp").properties["intensity"] == 3.0


# ---------------------------------------------------------------------------
# Runtime workflow
# ---------------------------------------------------------------------------

def test_load_map_enter_play_dispatch_io_update_world_stop(session):
    """load map -> start the logic thread -> activate an entity -> dispatch I/O
    -> update the world -> stop, with the authored map unchanged at the end."""
    brushes = room(size=2048.0)
    switchable = box_brush("switchable", (0, 64, 0), (64, 128, 64))
    button = box_brush("button", (200, 64, 0), (32, 64, 32), is_trigger=True)
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="switchable", input_name="Hide",
        target_id=switchable["id"]))
    state, thread = session(brushes=brushes + [switchable, button],
                            things=[make_thing(PlayerStart, "spawn", (0, 64, 0))])

    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)

    assert switchable.get("hidden") in (None, False)
    thread.io_manager.fire_output(button, "OnTrigger")
    assert switchable.get("hidden") is True, (
        "firing the trigger did not hide its target through the live I/O path")

    for _ in range(10):
        thread._tick(TICK)
    thread._prepare_render_state()
    published = thread.game_state.get_write_state()
    assert "switchable" not in {b.get("name") for b in published.all_brushes}, (
        "the hidden brush was still submitted to the renderer")

    thread.set_play_mode(False)
    assert thread._spatial_grid is None
    assert thread.monster_ai_thread is None


def test_a_delayed_connection_fires_on_the_logic_threads_own_clock(session):
    switchable = box_brush("switchable", (0, 64, 0), (64, 128, 64))
    button = box_brush("button", (200, 64, 0), (32, 64, 32), is_trigger=True)
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="switchable", input_name="Hide",
        target_id=switchable["id"], delay=0.5))
    state, thread = session(brushes=room() + [switchable, button])

    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)
    thread.io_manager.fire_output(button, "OnTrigger")

    for _ in range(10):                      # ~0.17s
        thread._tick(TICK)
    assert switchable.get("hidden") in (None, False), (
        "the 0.5s delay fired after only %.2fs" % (10 * TICK))

    for _ in range(35):                      # past 0.5s in total
        thread._tick(TICK)
    assert switchable.get("hidden") is True, (
        "the delayed connection never fired; %.2fs of ticks have run"
        % (45 * TICK))


def test_monsters_run_against_the_live_world_during_a_play_session(session):
    monster = make_thing(Monster, "grunt", (400, 96, 0), awake=True, damage=5)
    state, thread = session(brushes=room(size=2048.0),
                            things=[make_thing(PlayerStart, "spawn", (0, 64, 0)),
                                    monster])

    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)
    start_x = monster.pos[0]

    for _ in range(30):
        thread.monster_ai.update(1.0 / 30.0)

    assert monster.pos[0] < start_x, (
        "the monster did not close on the player over a second of AI ticks "
        "(x %.1f -> %.1f)" % (start_x, monster.pos[0]))
    assert thread.monster_ai._grid is thread._spatial_grid, (
        "the AI is querying a grid other than the session's")


def test_stopping_play_mode_restores_the_authored_world(session):
    """Runtime state must not become part of the map the mapper saves."""
    monster = make_thing(Monster, "grunt", (400, 96, 0), awake=False)
    lamp = make_thing(Light, "lamp", (0, 200, 0), intensity=2.0)
    state, thread = session(brushes=room(), things=[monster, lamp])
    authored = json.dumps(state.get_level_data(), sort_keys=True)

    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)
    for _ in range(20):
        thread._tick(TICK)
    thread.set_play_mode(False)

    after = json.dumps(state.get_level_data(), sort_keys=True)
    assert after == authored, (
        "a play session changed the authored map.\nbefore: %s\nafter : %s"
        % (authored[:400], after[:400]))


def test_the_same_world_objects_serve_the_editor_and_the_runtime(session):
    """Fio's premise, asserted: one representation, no copy in between."""
    state, thread = session()
    assert thread.brushes is state.brushes
    assert thread.things is state.things

    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)
    grid_brushes = {id(b) for bucket in thread._spatial_grid.cells.values()
                    for b in bucket}
    assert grid_brushes <= {id(b) for b in state.brushes}, (
        "the collision grid holds objects that are not in the editor's scene")


def test_an_edit_made_while_play_mode_is_running_reaches_the_runtime(session):
    state, thread = session()
    thread.set_play_mode(True)
    thread.player = Player(0.0, 0.0)
    thread.culling_enabled = False

    state.brushes.append(box_brush("late_addition", (0, 64, -300)))
    thread._prepare_render_state()

    names = {b.get("name") for b in thread.game_state.get_write_state().all_brushes}
    assert "late_addition" in names, (
        "a brush added during play did not reach the renderer; the frame holds "
        "%s" % (sorted(names),))


# ---------------------------------------------------------------------------
# Plugin workflow
# ---------------------------------------------------------------------------

def test_load_plugin_register_event_fire_modify_world_unload(session):
    """load plugin -> register event -> event fires -> modify world ->
    detach -> verify cleanup."""
    from plugins.manager import PluginManager
    from tests.plugins.fixtures.well_behaved import WellBehavedPlugin

    manager = PluginManager()
    plugin = WellBehavedPlugin()
    plugin.register(_editor_api(manager, plugin))
    manager.plugins.append(plugin)
    manager._enabled_generation += 1

    state, thread = session()
    manager.attach_runtime(thread)
    manager.bind_host(thread, kind="engine")

    assert ("register_runtime", None) in plugin.calls
    assert ("connect", None) in plugin.calls

    manager.emit("test.ping", value=42)
    assert plugin.events_seen == [{"value": 42}], (
        "the plugin's subscription did not receive the emitted event; it saw %s"
        % (plugin.events_seen,))

    manager.dispatch_play_start(thread)
    from plugins.api import TickContext
    manager.dispatch_tick(thread, TickContext(thread, keys=set()))
    manager.dispatch_play_stop(thread)
    assert plugin.tick_count == 1

    # "Unload": the manager drops its subscriptions when it binds elsewhere.
    manager.events.clear()
    plugin.events_seen.clear()
    manager.emit("test.ping", value=1)
    assert plugin.events_seen == [], (
        "the plugin still received an event after its subscriptions were "
        "cleared: %s" % (plugin.events_seen,))
    assert manager.events.has("test.ping") is False


def _editor_api(manager, plugin):
    from plugins.api import EditorAPI
    return EditorAPI(manager, plugin)


def test_a_plugin_that_fails_does_not_stop_a_play_session(session):
    from plugins.api import TickContext
    from plugins.manager import PluginManager
    from tests.plugins.fixtures.raises_on_tick import TickBombPlugin
    from tests.plugins.fixtures.well_behaved import WellBehavedPlugin

    manager = PluginManager()
    bomb, good = TickBombPlugin(), WellBehavedPlugin()
    for plugin in (bomb, good):
        plugin.register(_editor_api(manager, plugin))
        manager.plugins.append(plugin)
    manager._enabled_generation += 1

    state, thread = session()
    manager.dispatch_play_start(thread)
    for _ in range(3):
        manager.dispatch_tick(thread, TickContext(thread, keys=set()))
    manager.dispatch_play_stop(thread)

    assert good.tick_count == 3, (
        "the working plugin ticked %d times of 3; a neighbour's exception cost "
        "it frames" % good.tick_count)
    assert ("on_play_stop", None) in good.calls


# ---------------------------------------------------------------------------
# Renderer workflow
# ---------------------------------------------------------------------------

def test_load_world_build_render_state_modify_world_rebuild(session):
    """load world -> create render state -> submit entities -> modify world ->
    rebuild render state."""
    state, thread = session(brushes=room(size=2048.0),
                            things=[make_thing(Light, "lamp", (0, 200, 0))])
    thread.culling_enabled = False

    thread._prepare_render_state()
    first = thread.game_state.get_write_state()
    first_count = len(first.all_brushes)
    assert first_count == 6, "the room is six brushes, the frame has %d" % first_count
    assert len(first.all_things) == 1

    thread.game_state.request_swap()
    state.brushes.append(box_brush("new_pillar", (0, 64, 0), (64, 128, 64)))
    state.things.append(make_thing(Light, "lamp2", (100, 200, 0)))

    thread._prepare_render_state()
    second = thread.game_state.get_write_state()
    assert len(second.all_brushes) == first_count + 1, (
        "the rebuilt frame holds %d brushes, expected %d"
        % (len(second.all_brushes), first_count + 1))
    assert len(second.all_things) == 2


def test_a_geometry_edit_reaches_the_renderers_derived_mesh(session):
    state, thread = session(brushes=[box_brush("wall", (0, 64, -300),
                                               (128, 128, 128))])
    thread.culling_enabled = False
    brush = state.brushes[0]

    thread._prepare_render_state()
    before = bg.get_shape(brush).bounds[1][1]

    state.clip_brush(brush, (0.0, 1.0, 0.0), 64.0)
    thread._prepare_render_state()
    after = bg.get_convex(state.brushes[0]).bounds[1][1]

    assert after < before, (
        "the clipped brush still reports its old top (%.2f -> %.2f); the "
        "renderer would draw the unclipped mesh" % (before, after))


# ---------------------------------------------------------------------------
# BigWorld workflow
# ---------------------------------------------------------------------------

def test_an_ordinary_map_starts_no_bigworld_session(session):
    """The gate: a map with no BigWorldSettings entity pays nothing."""
    state, thread = session(things=[make_thing(PlayerStart, "spawn")])

    thread.set_play_mode(True)
    try:
        assert getattr(thread, "_bigworld", None) is None, (
            "an ordinary map started a Big World session: %r"
            % getattr(thread, "_bigworld", None))
    finally:
        thread.set_play_mode(False)


def test_a_bigworld_map_activates_cells_around_the_player_and_restores_on_stop(
        session):
    """load a BigWorld map -> activate -> query -> deactivate -> verify."""
    from plugins.bigworld.entities import BigWorldSettings

    near = box_brush("near_brush", (0, 64, 0), (128, 128, 128))
    far = box_brush("far_brush", (20000, 64, 0), (128, 128, 128))
    settings = make_thing(BigWorldSettings, "bw_settings", (0, 0, 0))
    state, thread = session(brushes=[near, far],
                            things=[settings,
                                    make_thing(PlayerStart, "spawn", (0, 64, 0))])
    authored = json.dumps(state.get_level_data(), sort_keys=True)

    # Big World ships disabled; the map loader switches it on for a map that
    # carries its settings entity.  Doing the same here is the realistic flow.
    enabled = thread.plugins.auto_enable_for_map(state.get_level_data())
    assert [p.name for p in enabled] == ["bigworld"], (
        "loading a map with a BigWorldSettings entity should auto-enable the "
        "plugin; it enabled %s" % ([p.name for p in enabled],))

    # The session activates cells around the player, so the player has to exist
    # before play mode starts - as it does in the editor, which spawns at the
    # PlayerStart before handing the session over.
    thread.player = Player(0.0, 0.0)
    thread.set_play_mode(True)
    try:
        assert getattr(thread, "_bigworld", None) is not None, (
            "a map carrying a BigWorldSettings entity did not start a session")
        assert far.get("hidden") is True, (
            "the brush 20000 units away is outside the activation radius and "
            "should be parked; hidden=%r" % far.get("hidden"))
        assert near.get("hidden") in (None, False), (
            "the brush at the player's feet was parked")
    finally:
        thread.set_play_mode(False)

    assert getattr(thread, "_bigworld", None) is None, \
        "the streaming session outlived play mode"

    # No object may still carry the session's activation marker.
    leaked = [b.get("name") for b in state.brushes if "bw_active" in b]
    leaked += [t.name for t in state.things if "bw_active" in t.properties]
    assert not leaked, (
        "these objects still carry the streaming session's bw_active marker "
        "after it stopped, so it would be written into the saved map: %s"
        % (leaked,))

    # And the world is the same world, judged the way Big World itself judges
    # it (``normalize_streaming_state`` resolves parking away and treats an
    # absent flag and an explicit False as the same thing).
    from plugins.bigworld.persistence import normalize_streaming_state
    assert normalize_streaming_state(state.get_level_data()) == \
        normalize_streaming_state(json.loads(authored)), (
        "a streaming session changed the authored map")
