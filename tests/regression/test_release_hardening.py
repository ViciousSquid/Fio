"""Regression tests for the 2.5 release-hardening audit.

Each test states the invariant a defect broke, in the area it broke it.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                    # noqa: E402
from engine.logic_thread import LogicThread                    # noqa: E402
from engine.threaded_game_state import ThreadedGameState       # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def logic():
    made = []

    def _build(brushes=(), things=()):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state)
        made.append(thread)
        return thread

    yield _build
    for thread in made:
        thread.stop()


def _published(thread, name):
    snap = thread.game_state.get_render_state()
    try:
        return getattr(snap, name)
    finally:
        thread.game_state.release_render_state(snap)


# ---------------------------------------------------------------------------
# Publication: per-tick state must survive frames that run no tick
# ---------------------------------------------------------------------------

def _add_projectile(thread, pos=(0.0, 100.0, 0.0), vel=(0.0, 0.0, 10.0)):
    thread.combat_runtime._monster_projectiles.add(pos, vel, 0, 5, 10.0)


def test_projectiles_are_published_on_frames_that_run_no_tick(logic):
    """The run loop sleeps 0.9x the remaining tick, so a frame often runs no
    tick at all. Its buffer was reset by the previous swap; projectiles written
    only by the tick then vanished for that frame (visible flicker)."""
    thread = logic()
    thread.session_runtime.play_mode = True
    _add_projectile(thread)
    thread.combat_runtime._update_monster_projectiles(thread.TICK_DURATION)

    for frame in range(4):
        # No whole tick in the accumulator: only the projection runs.
        thread._step_frame(0.0)
        assert thread._publish_frame(), "nothing was reading; the swap must happen"
        projectiles = _published(thread, 'projectiles')
        assert len(projectiles) == 1, (
            "frame %d published %d projectiles; a zero-tick frame must carry "
            "the live projectile set" % (frame, len(projectiles)))


def test_leaving_play_publishes_no_projectiles(logic):
    thread = logic()
    thread.session_runtime.play_mode = True
    _add_projectile(thread)
    thread.combat_runtime._update_monster_projectiles(thread.TICK_DURATION)
    thread.session_runtime.play_mode = False
    thread._step_frame(0.0)
    thread._publish_frame()
    assert len(_published(thread, 'projectiles')) == 0


# ---------------------------------------------------------------------------
# Dense tables: a change must move whatever keys the renderer's caches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edit", [
    dict(scale=(2.0, 2.0)), dict(shift=(8.0, 0.0)), dict(angle=45.0),
    dict(natural=True), dict(texture="brick.png"),
])
def test_any_mapping_edit_to_an_angled_brush_face_moves_its_signature(edit):
    """The convex GPU mesh bakes every face's mapping and is cached by the
    geometry signature; only a retexture used to move it."""
    from engine import brush_geometry as bg
    from editor import face_texture
    from tests.helpers.worlds import box_brush

    ramp = box_brush("ramp", (0, 32, 0), (128, 64, 128))
    bg.clip_brush(ramp, (0.0, 1.0, 1.0), 20.0)
    tagged = next(key for key, _ in bg.iter_surface_faces(ramp)
                  if key in bg.FACE_TAGS)
    before = bg.geometry_signature(ramp)
    face_texture.set_transform(ramp, tagged, **edit)
    assert bg.geometry_signature(ramp) != before, (
        "editing %s on tagged face %r left the geometry signature unchanged; "
        "the renderer would keep drawing the stale mesh" % (edit, tagged))


def test_the_change_journal_does_not_overflow_on_a_large_battle():
    """10 000 monsters moving between two drains must stay a precise set:
    OVERFLOW makes the table re-resolve every row (17x the cost)."""
    from engine.change_journal import ChangeJournal, OVERFLOW, MOVED

    class _Sub:
        pass

    journal = ChangeJournal()
    sub = _Sub()
    journal.subscribe(sub)
    objs = [object() for _ in range(10000)]
    for _ in range(3):
        journal.record_many(objs, MOVED)
    pending = journal.drain(sub)
    assert pending is not OVERFLOW
    assert len(pending) == len(objs)


def test_a_journalled_portal_retarget_reaches_the_entity_table():
    """portal_link / setprop change portal_target at runtime and journal the
    portal; the table resolved links only at a reconcile, so the renderer
    kept drawing the old destination until the next structural edit."""
    from editor.things import Portal
    from engine.change_journal import touch
    from engine.entity_table import EntityTable

    a, b, c = Portal(pos=[0, 0, 0]), Portal(pos=[100, 0, 0]), Portal(pos=[200, 0, 0])
    for p, name in ((a, 'a'), (b, 'b'), (c, 'c')):
        p.properties['name'] = name
        p.properties['id'] = name
    a.properties['portal_target'] = 'b'
    table = EntityTable()
    table.begin_frame([a, b, c], 1)
    assert table.portal_target_slot[0] == 1

    a.properties['portal_target'] = 'c'
    touch(a)
    table.begin_frame([a, b, c], 1)
    assert table.portal_target_slot[0] == 2, (
        "portal a still targets slot %d after a journalled retarget"
        % table.portal_target_slot[0])


# ---------------------------------------------------------------------------
# Monster AI
# ---------------------------------------------------------------------------

def test_monster_table_follows_a_replaced_list_at_a_recycled_address():
    """The table reconciled only when (id(list), len) changed. A rebuilt list
    of the same length can sit at a freed list's address; the table then kept
    driving the previous monster objects."""
    from editor.things import Monster
    from engine.monster_table import MonsterTable

    old = [Monster(pos=[0.0, 0.0, 0.0]), Monster(pos=[10.0, 0.0, 0.0])]
    new = [Monster(pos=[500.0, 0.0, 0.0]), Monster(pos=[600.0, 0.0, 0.0])]
    table = MonsterTable()
    table.gather(old)

    # Stand-in for the recycled address: the same list object, new contents.
    rows = old
    rows[:] = new
    table.gather(rows)
    assert all(a is b for a, b in zip(table.monsters, new)), (
        "the table still holds the previous monster objects")
    assert table.pos[0, 0] == 500.0


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@pytest.mark.gl
def test_a_missing_model_is_not_reloaded_every_frame(monkeypatch, capsys):
    """Every draw, cull and shadow pass asks for a Prop's model; a missing one
    was probed on disk and logged each time (1194 lines in a short soak)."""
    pytest.importorskip("OpenGL")
    from engine.renderer import geometry as renderer_geometry
    from tests.helpers.gl import GLTestContext, make_renderer, reset_texture_cache

    reset_texture_cache()
    with GLTestContext(64, 64):
        renderer = make_renderer()
        try:
            renderer.loaded_models = {}
            probes = []
            real_exists = renderer_geometry.os.path.exists
            monkeypatch.setattr(
                renderer_geometry.os.path,
                "exists",
                lambda p: probes.append(p) or real_exists(p),
            )
            for _ in range(50):
                assert renderer.load_model("no_such_model.glb") is None
            assert len(probes) <= 3, (
                "%d filesystem probes for 50 frames" % len(probes)
            )
            assert capsys.readouterr().out.count("Failed to load model") == 1

            # ...but it is retried later, so a model added mid-session appears.
            monkeypatch.setattr(renderer_geometry, "_MODEL_RETRY_S", 0.0)
            probes.clear()
            renderer.load_model("no_such_model.glb")
            assert probes, "a failed model must be retried after the back-off"
        finally:
            renderer.cleanup()
            reset_texture_cache()


# ---------------------------------------------------------------------------
# Big World parking
# ---------------------------------------------------------------------------

def test_a_visibility_only_change_reaches_both_tables_without_a_cold_resolve(monkeypatch):
    """Parking changes only the live hidden flag. It was journalled as STATE,
    so every parked row was re-resolved (textures, class, materials) in both
    render buffers' tables -- ~180 ms per 10 000 rows per table."""
    from editor.things import Light
    from engine.change_journal import VISIBILITY, touch
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    from tests.helpers.worlds import box_brush

    brushes = [box_brush("b%d" % i, (i * 100.0, 0, 0)) for i in range(40)]
    lights = [Light(pos=[i * 10.0, 0, 0]) for i in range(40)]
    for i, light in enumerate(lights):
        light.properties['id'] = 'l%d' % i
    rt, et = RenderTable(), EntityTable()
    rt.begin_frame(brushes, 1)
    et.begin_frame(lights, 1)

    cold = []
    monkeypatch.setattr(RenderTable, "_resolve_cold_rows",
                        lambda self, slots, b: cold.append(len(list(slots))))
    monkeypatch.setattr(EntityTable, "_resolve_row",
                        lambda self, slot, thing: cold.append(slot))
    for obj in brushes[::2]:
        obj['hidden'] = True
        touch(obj, VISIBILITY)
    for obj in lights[::2]:
        obj.properties['hidden'] = True
        touch(obj, VISIBILITY)
    rt_hidden = rt.begin_frame(brushes, 1)
    et_hidden = et.begin_frame(lights, 1)

    expected = [i % 2 == 0 for i in range(40)]
    assert rt_hidden.tolist() == expected
    assert et_hidden.tolist() == expected
    assert rt.shown()[1].tolist() == [i for i in range(40) if i % 2]
    assert cold == [], "a park re-resolved %d cold rows" % len(cold)


# ---------------------------------------------------------------------------
# I/O across threads
# ---------------------------------------------------------------------------

def test_outputs_fired_off_the_tick_thread_run_on_the_next_tick(logic):
    """The monster AI thread fires OnDeath/OnSeePlayer/OnAttack while the
    logic thread runs IOManager.update(), which rebuilds pending_events: a
    delayed event appended meanwhile was lost, and the source/activator
    context was shared between two chains. They are now delivered, in order,
    on the tick's thread at its next update()."""
    logic = logic()
    import threading
    from tests.logic.test_io_dispatch import Network

    net = Network()
    logic.session_runtime.play_mode = True
    net.manager.set_logic_thread(logic)
    net.add("monster")
    net.add("now")
    net.add("later")
    ran_on = []
    net.handler(then=lambda e, p, l: ran_on.append(threading.get_ident()))
    net.connect("monster", "OnDeath", "now")
    net.connect("monster", "OnDeath", "later", delay=0.5)

    net.manager.update(0.0)                  # this thread runs the ticks
    ai = threading.Thread(
        target=lambda: net.manager.fire_output(net.entities["monster"], "OnDeath"))
    ai.start()
    ai.join()
    assert net.log == [], "an AI-thread output ran on the AI thread"

    net.manager.update(0.1)
    assert net.log == [("now", "Fire", "")]
    net.manager.update(0.5)
    assert net.log == [("now", "Fire", ""), ("later", "Fire", "")], (
        "the delayed event queued from the AI thread was lost")
    assert set(ran_on) == {threading.get_ident()}


def test_outputs_fired_in_the_editor_still_run_immediately(logic):
    logic = logic()
    import threading
    from types import SimpleNamespace
    from tests.logic.test_io_dispatch import Network

    net = Network()
    logic.session_runtime.play_mode = False
    net.manager.set_logic_thread(logic)
    net.add("a")
    net.add("b")
    net.handler()
    net.connect("a", "OnUse", "b")
    net.manager.update(0.0)
    other = threading.Thread(
        target=lambda: net.manager.fire_output(net.entities["a"], "OnUse"))
    other.start()
    other.join()
    assert net.log == [("b", "Fire", "")]


def test_resetting_monsters_for_play_journals_their_sprite_state(logic):
    """dead / is_shooting pick a monster's sprite; clearing them at play
    start/stop without journalling left the editor drawing a corpse or a
    mid-shot frame (found by the Debug Tables oracle)."""
    from editor.things import Monster
    from engine.entity_table import EntityTable

    monster = Monster(pos=[0.0, 0.0, 0.0])
    monster.properties['id'] = 'm'
    monster.properties['dead'] = True
    thread = logic(things=[monster])
    table = EntityTable()
    table.begin_frame(thread.editor_state.things, 1)
    dead_sprite = table.sprite_recipes()[table.sprite_key_id[0]]

    thread.session_runtime.reset_all_monsters(clear_dead=True)
    table.begin_frame(thread.editor_state.things, 1)
    sprite = table.sprite_recipes()[table.sprite_key_id[0]]
    assert sprite != dead_sprite
    assert any(c[1] == 'idle.png' for c in sprite), sprite


# ---------------------------------------------------------------------------
# Found by the Debug Tables oracle (rebuild-from-world vs published columns)
# ---------------------------------------------------------------------------

def test_play_start_and_stop_keep_an_angled_brushs_geometry_epoch(logic):
    """Reverting mesh collision stripped every GEO_RUNTIME_KEYS entry but the
    convex cache -- including _geo_epoch, the brush's geometry identity. Each
    angled brush then got a new epoch behind the render tables' back."""
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush

    solid = box_brush("ramp", (0, 32, 0), (128, 64, 128))
    bg.clip_brush(solid, (0.0, 1.0, 1.0), 20.0)
    water = box_brush("pool", (400, 32, 0), (128, 64, 128), shader="Water")
    bg.clip_brush(water, (0.0, 1.0, 1.0), 20.0)
    thread = logic(brushes=[solid, water])
    before = [bg.geometry_signature(b) for b in (solid, water)]

    thread.collision_runtime.prepare_angled_brush_collision()     # play start
    assert solid.get('_collision_mode') == 'mesh'
    thread.collision_runtime.clear_angled_brush_collision()       # play stop

    assert '_collision_mode' not in solid
    assert [bg.geometry_signature(b) for b in (solid, water)] == before


def test_a_row_that_is_not_a_light_carries_no_light_columns():
    """A reconcile that moves a non-light entity into a light's old slot left
    the light's colour, intensity and on/off in that row."""
    from editor.things import Light, Prop
    from engine.entity_table import EntityTable

    light = Light(pos=[0.0, 0.0, 0.0])
    light.properties.update(id='l', colour=[255, 0, 0])
    prop = Prop(pos=[5.0, 0.0, 0.0])
    prop.properties['id'] = 'p'
    table = EntityTable()
    table.begin_frame([light, prop], 1)
    assert table.light_enabled[0]

    table.begin_frame([prop, light], 2, dirty_objects={id(prop)})
    assert not table.light_enabled[0]
    assert table.light_color[0].tolist() == [0.0, 0.0, 0.0]
    assert table.light_params[0].tolist() == [0.0, 0.0]
