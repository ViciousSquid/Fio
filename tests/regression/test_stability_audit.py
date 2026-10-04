"""Regressions found by the 2.5 stability audit.

Most of these were found by one invariant both dense projections state and
nothing checked end to end: *a published RenderTable/EntityTable must equal a
table rebuilt from scratch over the same objects*. With two buffers that each
refresh incrementally, any change only one of them sees shows up as the two
published frames alternating between old and new state. ``_diff_rebuild`` is
that check; the Debug Tables instrument shows the same columns by hand.
"""

import json
import random

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor import io_system as io                         # noqa: E402
from editor.editor_state import EditorState               # noqa: E402
from editor.things import Light, Monster, PlayerStart, Portal  # noqa: E402
from engine import entity_table as etm                    # noqa: E402
from engine import render_table as rtm                    # noqa: E402
from engine.change_journal import touch                   # noqa: E402
from engine.entity_table import EntityTable               # noqa: E402
from engine.logic_player import KEY_W
from engine.logic_thread import LogicThread        # noqa: E402
from engine.mover_table import MoverTable                 # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.render_table import RenderTable               # noqa: E402
from engine.spatial import set_authored_flag              # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, pillar_grid  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

#: Entity columns that are a function of wall-clock time (the Effect clock).
_CLOCK_COLUMNS = {"effect_elapsed", "effect_alive", "light_params",
                  "light_enabled", "portal_fade"}
_INTERNED = {"tex_name_id": "texture_names", "sprite_key_id": "sprite_recipes",
             "model_recipe_id": "model_recipes"}


def _named(table, name, ids):
    names = getattr(table, _INTERNED[name])()
    return [tuple(names[int(i)] if i >= 0 else None for i in np.atleast_1d(row))
            for row in ids]


def _diff_columns(pub, fresh, columns, skip=()):
    n = pub.count
    assert n == fresh.count
    bad = []
    for name, *_ in columns:
        if name in skip or not n:
            continue
        a, b = getattr(pub, name)[:n], getattr(fresh, name)[:n]
        if name in _INTERNED:
            same = _named(pub, name, a) == _named(fresh, name, b)
        elif name == "geometry_id":
            same = ([pub.geometry_records[i].signature if i >= 0 else None for i in a]
                    == [fresh.geometry_records[i].signature if i >= 0 else None for i in b])
        elif a.dtype.kind == "f":
            same = np.allclose(a, b, atol=1e-4, equal_nan=True)
        else:
            same = np.array_equal(a, b)
        if not same:
            bad.append(name)
    return bad


def _diff_rebuild(frame):
    """Columns in which the published frame differs from a from-scratch build."""
    fresh = RenderTable()
    fresh.sync(list(frame.render_table.brushes))
    bad = ["RT." + c for c in _diff_columns(frame.render_table, fresh, rtm._COLUMNS)]
    efresh = EntityTable()
    efresh.sync(list(frame.entity_table.things))
    bad += ["ET." + c for c in _diff_columns(frame.entity_table, efresh,
                                               etm._COLUMNS, _CLOCK_COLUMNS)]
    return bad


def _frame(logic, game_state):
    """Step one frame, publish it, and return the frame the renderer sees."""
    logic._step_frame(logic.TICK_DURATION)
    logic._publish_frame()
    game_state.try_swap()
    return game_state.get_render_state()


def _check_frames(logic, game_state, count=2):
    for _ in range(count):
        frame = _frame(logic, game_state)
        try:
            assert _diff_rebuild(frame) == []
        finally:
            game_state.release_render_state(frame)


def _editor(brushes=(), things=()):
    state = EditorState()
    state.brushes = list(brushes)
    state.things = list(things)
    state.mark_world_changed()
    game_state = ThreadedGameState()
    return state, game_state, LogicThread(game_state, state)


def _play(brushes=(), things=()):
    state, game_state, logic = _editor(brushes, things)
    logic.player = Player(0.0, 0.0, 0.0)
    logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.stop_monster_ai()
    return state, game_state, logic


# ---------------------------------------------------------------------------
# Double-buffered projection
# ---------------------------------------------------------------------------

def test_a_dragged_brush_does_not_flicker_after_it_is_deselected():
    """An edited row is re-read only by the buffer being written.

    After a drag the other buffer still held the pre-drag transform, and once
    the selection moved on nothing re-read it: the two published frames
    alternated between the old and the new position every frame.
    """
    state, game_state, logic = _editor(pillar_grid(3, 3))
    _check_frames(logic, game_state)
    brush = state.brushes[4]
    state.set_selected_object(brush)
    state.save_state()
    _check_frames(logic, game_state)
    brush["pos"] = [brush["pos"][0], brush["pos"][1] + 4.0, brush["pos"][2]]
    _check_frames(logic, game_state, 1)
    state.set_selected_object(None)
    _check_frames(logic, game_state, 4)


def test_a_reused_entity_slot_does_not_keep_its_previous_light():
    light = make_thing(Light, "lamp", (0, 64, 0))
    table = EntityTable()
    table.sync([light], epoch=1)
    assert table.light_color[0].any()
    monster = make_thing(Monster, "grunt", (0, 64, 0))
    table.sync([monster], epoch=2)
    fresh = EntityTable()
    fresh.sync([monster])
    assert _diff_columns(table, fresh, etm._COLUMNS, _CLOCK_COLUMNS) == []


def test_random_editor_edits_publish_tables_equal_to_a_rebuild():
    """Edits, undo/redo, adds, deletes and hides, with the renderer holding a
    frame across publishes: every published frame must equal a rebuild."""
    rng = random.Random(7)
    state, game_state, logic = _editor(
        pillar_grid(6, 6, spacing=200.0),
        [make_thing(Light if i % 2 else Monster, "t%d" % i, (i * 40.0, 64, 0))
         for i in range(12)])
    pending = []
    state.post_event = pending.append
    held = None
    for step in range(150):
        roll = rng.random()
        if roll < 0.3:
            brush = rng.choice(state.brushes)
            state.set_selected_object(brush)
            state.save_state()
            brush["pos"] = [brush["pos"][0] + 8.0, brush["pos"][1], brush["pos"][2]]
        elif roll < 0.4:
            state.save_state()
            state.brushes.append(box_brush("new%d" % step, (rng.uniform(-600, 600), 32, 0)))
        elif roll < 0.5 and len(state.brushes) > 1:
            state.save_state()
            state.brushes.remove(rng.choice(state.brushes))
        elif roll < 0.6:
            state.undo()
        elif roll < 0.65:
            state.redo()
        elif roll < 0.75:
            brush = rng.choice(state.brushes)
            set_authored_flag(brush, "hidden", not brush.get("hidden", False))
        elif roll < 0.85:
            thing = rng.choice(state.things)
            thing.pos = [thing.pos[0] + 3.0, thing.pos[1], thing.pos[2]]
        else:
            state.set_selected_object(None)
        while pending:
            pending.pop(0)()
        logic._step_frame(logic.TICK_DURATION)
        logic._publish_frame()
        if held is not None and rng.random() < 0.6:
            game_state.release_render_state(held)
            held = None
        if held is None:
            game_state.try_swap()
            frame = game_state.get_render_state()
            assert _diff_rebuild(frame) == [], "step %d" % step
            if rng.random() < 0.3:
                held = frame
            else:
                game_state.release_render_state(frame)
    if held is not None:
        game_state.release_render_state(held)


def test_projectiles_are_published_on_a_frame_that_runs_no_tick():
    """Projectile positions were written into the write buffer only inside a
    tick, so the render loop's zero-tick frames published none: flicker."""
    _state, game_state, logic = _play()
    for i in range(3):
        logic.combat_runtime._monster_projectiles.add(
            (0.0, 50.0, 100.0 + i), (0.0, 0.0, 10.0), 0, 1, 50.0)
    try:
        counts = []
        for accumulator in (logic.TICK_DURATION, 0.0, logic.TICK_DURATION, 0.0):
            logic._step_frame(accumulator)
            logic._publish_frame()
            game_state.try_swap()
            frame = game_state.get_render_state()
            counts.append(len(frame.projectiles))
            game_state.release_render_state(frame)
        assert counts == [3, 3, 3, 3]
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_mover_slot_maps_are_cached_per_render_buffer():
    mover = box_brush("lift", (0, 32, 0), is_mover=True, start_on=True)
    _state, _game_state, logic = _play([mover])
    try:
        tables = [RenderTable(), RenderTable()]
        for table in tables:
            table.sync([mover], epoch=1)
        movers = logic.mover_runtime._movers()
        movers.publish(logic, tables[0])
        first = movers._slot_cache[tables[0]]
        movers.publish(logic, tables[1])
        movers.publish(logic, tables[0])
        assert movers._slot_cache[tables[0]] is first
        assert len(movers._slot_cache) == 2
    finally:
        logic.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_io_parameters_are_ignored(value):
    mover = box_brush("lift", (0, 32, 0), is_mover=True, start_on=True,
                      rotate=True, speed=45.0)
    monster = make_thing(Monster, "grunt", (0, 64, 0), health=50)
    _state, game_state, logic = _play([mover], [monster])
    try:
        manager = logic.io_manager
        manager._execute_input("lift", "SetSpeed", value, "t", target_id=mover["id"])
        manager._execute_input("grunt", "SetHealth", value, "t",
                               target_id=monster.properties["id"])
        assert mover["speed"] == 45.0
        assert monster.properties["health"] == 50
        for _ in range(3):
            logic._step_frame(logic.TICK_DURATION)
        assert np.isfinite(logic.mover_runtime._movers().movers.rot_angle).all()
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_portal_set_target_relinks_the_renderer_and_transit():
    a = make_thing(Portal, "a", (0, 64, 0), portal_target="b")
    b = make_thing(Portal, "b", (500, 64, 0), portal_target="a")
    c = make_thing(Portal, "c", (-500, 64, 0), portal_target="a")
    _state, game_state, logic = _play([], [a, b, c])
    try:
        _check_frames(logic, game_state)
        logic.io_manager._execute_input("a", "SetTarget", "c", "t",
                                        target_id=a.properties["id"])
        assert logic.portal_runtime.portal_target_things[logic.portal_runtime.portal_things.index(a)] is c
        frame = _frame(logic, game_state)
        try:
            assert _diff_rebuild(frame) == []
            table = frame.entity_table
            assert table.portal_target_slot[table.slot_of_id[a.properties["id"]]] == \
                table.slot_of_id[c.properties["id"]]
        finally:
            game_state.release_render_state(frame)
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_brush_kill_removes_it_from_the_running_world():
    wall = box_brush("wall", (0, 64, 200))
    _state, _game_state, logic = _play([wall])
    try:
        logic.io_manager._execute_input("wall", "Kill", "", "t", target_id=wall["id"])
        assert wall.get("hidden") is True and wall.get("disabled") is True
        assert "_kill" not in wall
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_a_wall_revealed_by_io_show_is_solid():
    """The collision grid files brushes by authored ``hidden`` once, at play
    start; an I/O Show drew the wall but the player walked through it."""
    floor = box_brush("floor", (0, -16, 0), (4000, 32, 4000))
    wall = box_brush("wall", (0, 128, 300), (2000, 256, 32), hidden=True)
    _state, game_state, logic = _play([floor, wall])
    try:
        logic.io_manager._execute_input("wall", "Show", "", "t", target_id=wall["id"])
        game_state.set_keys({KEY_W})
        for _ in range(240):
            logic._step_frame(logic.TICK_DURATION)
        assert logic.player.pos.z < 284.0
    finally:
        logic.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _trigger(**props):
    brush = box_brush("trig", (0, 32, 0), (64, 64, 64), is_trigger=True,
                      trigger_action="target", **props)
    _state, game_state, logic = _play([brush])
    return brush, game_state, logic


def test_a_once_trigger_as_the_editor_authors_it_fires_once():
    brush, _game_state, logic = _trigger(trigger_type="Once")
    fired = []
    logic.io_manager.fire_output = lambda *a, **k: fired.append(a[1])
    try:
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        assert fired == ["OnStartTouch", "OnTrigger"]
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_hurt_trigger_uses_the_editors_damage_amount():
    brush, _game_state, logic = _trigger(trigger_type="Multiple", hurt_amount=37)
    brush["trigger_action"] = "hurt"
    try:
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        assert logic.player_health == 100 - 37
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_trigger_activation_falls_back_to_the_key_older_editors_wrote():
    from engine.logic_thread import _trigger_activation
    assert _trigger_activation({"trigger_collect_activation": "use"}) == "use"
    assert _trigger_activation({"trigger_activation": "Touch",
                                "trigger_collect_activation": "use"}) == "touch"
    assert _trigger_activation({}) == "touch"


@pytest.mark.parametrize("save", ["quicksave", "quickload"])
def test_a_trigger_can_quicksave_or_quickload(save):
    brush, game_state, logic = _trigger(trigger_type="Once", trigger_save=save)
    try:
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        assert list(game_state.consume_console_commands()) == [save]
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_a_trigger_saves_nothing_by_default():
    brush, game_state, logic = _trigger(trigger_type="Multiple")
    try:
        logic.trigger_runtime._on_trigger_enter(brush, brush["id"])
        assert not game_state.consume_console_commands()
    finally:
        logic.session_runtime.apply_play_mode(False)


def test_trigger_tab_writes_the_keys_the_engine_reads(qt_app):
    """The Activation combo wrote ``trigger_collect_activation``, which nothing
    read: a trigger set to 'use' in the editor still fired on touch."""
    import types
    from PyQt5.QtWidgets import QWidget
    from editor.property_editor import PropertyEditor

    editor = types.SimpleNamespace(
        state=types.SimpleNamespace(things=[], brushes=[]), view_3d=QWidget())
    panel = PropertyEditor(editor)
    try:
        brush = box_brush("trig", is_trigger=True, trigger_type="Once")
        panel.current_object = brush
        panel.populate_for_brush(brush)
        panel._widgets["trigger_collect_activation_combo"].setCurrentText("use")
        panel._widgets["trigger_save_combo"].setCurrentText("quicksave")
        assert brush["trigger_activation"] == "use"
        assert "trigger_collect_activation" not in brush
        assert brush["trigger_save"] == "quicksave"
    finally:
        panel.deleteLater()


def test_a_hidden_light_does_not_light_the_running_world():
    """Big World parks an out-of-range light by hiding it; the dense light
    selection ignored ``hidden``, so parked lights kept lighting and kept
    taking light and shadow slots. The editor preview still shows them."""
    from engine.renderer_F import Renderer_F
    renderer = Renderer_F.__new__(Renderer_F)
    lamps = [make_thing(Light, "lamp%d" % i, (i * 100.0, 64, 0)) for i in range(3)]
    lamps[1].properties["hidden"] = True
    table = EntityTable()
    hidden = table.begin_frame(lamps, epoch=1)
    config = {"entity_table": table, "thing_hidden": hidden, "play_mode": True}
    _table, slots = renderer._get_active_lights(config)
    assert sorted(slots.tolist()) == [0, 2]
    config["play_mode"] = False
    _table, slots = Renderer_F._get_active_lights(config)
    assert sorted(slots.tolist()) == [0, 1, 2]


def test_console_hide_and_show_go_through_the_authored_writer():
    import types
    from editor.console_commands import ConsoleCommandHandler
    wall = box_brush("wall", (0, 64, 200))
    wall["name"] = "wall"
    state = EditorState()
    state.brushes = [wall]
    marked = []
    logic = types.SimpleNamespace(collision_runtime=types.SimpleNamespace(mark_dirty=lambda: marked.append(1)))
    handler = ConsoleCommandHandler.__new__(ConsoleCommandHandler)
    handler.editor_state = state
    handler._logic_thread = lambda: logic
    handler.cmd_hide("wall")
    assert wall["hidden"] is True and marked == [1]
    wall["_bw_parked_hidden"] = True     # parked by a streaming layer
    handler.cmd_show("wall")
    assert wall["_bw_parked_hidden"] is False, "Show landed on the parked value"


def test_a_nan_view_distance_is_ignored():
    """``r_viewdistance nan`` reached the shared model (NaN passes any clamp),
    giving every view a NaN far plane before the UI raised."""
    from engine.view_distance import ViewDistance
    vd = ViewDistance(4000.0)
    vd.distance = float("nan")
    assert vd.distance == 4000.0
    vd.fog_color = (float("nan"), 0.5, 2.0)
    assert vd.fog_color == (0.0, 0.5, 1.0)


def test_getprop_with_extra_arguments_prints_usage():
    from editor.console_commands import ConsoleCommandHandler
    handler = ConsoleCommandHandler.__new__(ConsoleCommandHandler)
    handler.editor_state = EditorState()
    handler.cmd_get_property("a b c")      # used to raise ValueError


def test_a_lease_finalizer_inside_the_swap_lock_does_not_deadlock():
    """The lease finalizer is a GC safety net, and a collection can run on a
    thread that is inside the render-state lock (a swap allocates in there).
    Re-taking the non-reentrant lock would hang that thread for good."""
    import threading
    game_state = ThreadedGameState()
    snap = game_state.get_render_state()
    finalizer = snap._render_lease_finalizer
    done = threading.Event()

    def collect_inside_the_lock():
        with game_state._render_state_lock:
            finalizer()                 # what the GC would run here
        done.set()

    worker = threading.Thread(target=collect_inside_the_lock, daemon=True)
    worker.start()
    worker.join(2.0)
    assert done.is_set(), "the lease finalizer deadlocked inside the lock"
    # The deferred release is folded in: the next swap is not declined.
    assert game_state.request_swap() is True
    assert game_state._read_leases == 0


@pytest.mark.parametrize("props", [
    {"is_water": True, "water_opacity": "abc"},
    {"shader": "Glass", "glass_opacity": None},
    {"shader": "Fog", "fog_density": "x"},
    {"shader": "Glow", "glow_intensity": "bright"},
])
def test_a_malformed_shader_value_projects_as_its_default(props):
    table = RenderTable()
    table.sync([box_brush("b", **props)], epoch=1)
    assert np.isfinite(table.water_params[0]).all()
    assert np.isfinite(table.glass_params[0]).all()
    assert np.isfinite(table.fog_params[0]).all()


def test_a_frame_that_cannot_be_prepared_does_not_kill_the_logic_thread():
    """One unprojectable value (``setprop wall size 64 a 64``) raised out of
    _prepare_render_state and ended the logic thread: the game froze for good,
    even after the value was put right."""
    wall = box_brush("wall", (0, 0, 0))
    state, game_state, logic = _editor([wall])
    _check_frames(logic, game_state, 1)
    published = game_state.published_frames
    wall["size"] = [64, "a", 64]
    touch(wall)
    logic._step_frame(logic.TICK_DURATION)          # must not raise
    assert logic._publish_frame() is False, "a half-built frame was published"
    assert game_state.published_frames == published
    wall["size"] = [64, 64, 64]
    touch(wall)
    _check_frames(logic, game_state, 2)


def test_an_exception_in_a_qt_callback_is_reported_not_fatal(qt_app, monkeypatch):
    """PyQt5 aborts the process when an exception escapes a slot unless an
    excepthook is installed; main.py installs this one."""
    import sys
    from editor import debug_console
    logged = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(debug_console, "debug_log", lambda c, m: logged.append((c, m)))
    hook = debug_console.install_excepthook()
    assert sys.excepthook is hook
    try:
        raise ValueError("boom in a slot")
    except ValueError:
        hook(*sys.exc_info())
    assert logged and logged[0][0] == "Error" and "boom in a slot" in logged[0][1]
