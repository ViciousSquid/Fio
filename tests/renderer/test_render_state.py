"""Render-state preparation: everything the renderer is handed, built headless.

The logic thread does not draw anything.  It decides *what* will be drawn —
camera, frustum planes, the visible and all-brush lists, the per-frame snapshots
of moving geometry — and publishes that as a :class:`RenderState` for the Qt
view to consume.  None of that needs a GL context, so all of it is tested here;
the drawing itself is the ``gl``-marked visual tier.

The properties that matter are conservativeness (culling may never drop
something that is actually on screen) and isolation (the snapshot the renderer
reads must not change under it while the next frame is being built).
"""

import math

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState             # noqa: E402
from editor.things import Light, Monster, Prop        # noqa: E402
from engine.change_journal import touch                   # noqa: E402
from engine.spatial import set_authored_flag              # noqa: E402
from engine.logic_thread import LogicThread             # noqa: E402
from engine import render_table as render_table_module      # noqa: E402
from engine.threaded_game_state import RenderState, ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, pillar_grid  # noqa: E402

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


def _frustum_looking_down_negative_z(thread, eye=(0, 0, 0), fov=90.0):
    projection = glm.perspective(glm.radians(fov), 1.0, 1.0, 10000.0)
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(eye[0], eye[1], eye[2] - 1.0),
                      glm.vec3(0, 1, 0))
    return thread.render_runtime.extract_frustum_planes(projection * view)


# ---------------------------------------------------------------------------
# Frustum planes
# ---------------------------------------------------------------------------

def test_six_normalised_planes_come_out_of_a_projection(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert len(planes) == 6, "a frustum has six planes, got %d" % len(planes)
    for index, (a, b, c, _d) in enumerate(planes):
        length = math.sqrt(a * a + b * b + c * c)
        assert length == pytest.approx(1.0, abs=1e-6), (
            "plane %d has normal length %.6f; the distance test assumes unit "
            "normals" % (index, length))


def test_a_box_in_front_of_the_camera_is_inside_the_frustum(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread.render_runtime.aabb_in_frustum(planes, (0, 0, -500), (32, 32, 32)) is True


def test_a_box_behind_the_camera_is_outside_the_frustum(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread.render_runtime.aabb_in_frustum(planes, (0, 0, 500), (32, 32, 32)) is False, (
        "a brush 500 units behind the camera was reported visible")


def test_a_box_far_off_to_the_side_is_outside_the_frustum(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread.render_runtime.aabb_in_frustum(planes, (5000, 0, -100), (32, 32, 32)) is False


def test_a_huge_box_straddling_the_camera_is_inside(logic):
    """Conservativeness: a box the camera is inside must never be culled."""
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread.render_runtime.aabb_in_frustum(planes, (0, 0, 0), (10000, 10000, 10000)) is True


def test_the_batched_cull_agrees_with_the_scalar_one_everywhere(logic):
    """The vectorised path is a performance optimisation, not a second answer."""
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    rng = np.random.default_rng(4)
    centers = rng.uniform(-3000, 3000, size=(400, 3))
    halves = rng.uniform(1, 400, size=(400, 3))

    batched = thread.render_runtime.aabb_in_frustum_batch(planes, centers, halves)
    scalar = np.array([thread.render_runtime.aabb_in_frustum(planes, c, h)
                       for c, h in zip(centers, halves)])

    mismatch = np.nonzero(batched != scalar)[0]
    assert mismatch.size == 0, (
        "the batched and scalar frustum tests disagree on %d of %d boxes; "
        "first is centre=%s half=%s (batched=%s scalar=%s)"
        % (mismatch.size, len(centers),
           np.round(centers[mismatch[0]], 2), np.round(halves[mismatch[0]], 2),
           batched[mismatch[0]], scalar[mismatch[0]]))


@pytest.mark.parametrize("seed", range(4))
def test_the_batched_cull_agrees_with_the_scalar_one_near_the_planes(logic, seed):
    """Boxes crowded against every plane, from cameras looking every way.

    The batched test is evaluated plane-major; the per-plane scalar loop is the
    definition it has to reproduce, box for box, where it matters most -- on
    the boundary.
    """
    thread = logic()
    rng = np.random.default_rng(seed)
    for _ in range(6):
        eye = glm.vec3(*rng.uniform(-2000, 2000, 3))
        look = glm.vec3(*rng.normal(size=3))
        up = glm.vec3(0, 1, 0) if abs(glm.normalize(look).y) < 0.99 else glm.vec3(1, 0, 0)
        projection = glm.perspective(glm.radians(float(rng.uniform(40, 110))),
                                     float(rng.uniform(1.0, 2.4)), 1.0,
                                     float(rng.uniform(1500, 6000)))
        planes = thread.render_runtime.extract_frustum_planes(
            projection * glm.lookAt(eye, eye + look, up))
        halves = rng.uniform(1, 300, size=(1500, 3))
        # Put each box's positive vertex within a few units of a random plane.
        which = rng.integers(0, 6, size=1500)
        p = np.asarray(planes)[which]
        centers = np.asarray(eye) + rng.uniform(-6000, 6000, size=(1500, 3))
        signed = (centers * p[:, :3]).sum(1) + (halves * np.abs(p[:, :3])).sum(1) + p[:, 3]
        centers -= p[:, :3] * (signed - rng.uniform(-3, 3, 1500))[:, None]

        batched = thread.render_runtime.aabb_in_frustum_batch(planes, centers, halves)
        scalar = np.array([thread.render_runtime.aabb_in_frustum(planes, c, h)
                           for c, h in zip(centers, halves)])
        assert np.array_equal(batched, scalar)


def test_the_batched_cull_takes_gathered_rows(logic):
    """The cull hands it rows gathered by slot as well as the table's prefix."""
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    bounds = np.array([[0, 0, -500, 32, 32, 32], [0, 0, 500, 32, 32, 32],
                       [5000, 0, -100, 32, 32, 32], [0, 0, -900, 8, 8, 8]], float)
    picked = bounds.take([3, 1, 0], axis=0)
    assert thread.render_runtime.aabb_in_frustum_bounds(planes, picked).tolist() == [True, False, True]
    assert thread.render_runtime.aabb_in_frustum_bounds(planes, bounds[:1]).tolist() == [True]


def test_the_batched_cull_of_an_empty_scene_is_an_empty_result(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert list(thread.render_runtime.aabb_in_frustum_batch(planes, [], [])) == []


# ---------------------------------------------------------------------------
# The published render state
# ---------------------------------------------------------------------------

def test_editor_mode_publishes_the_editor_camera_and_dense_projection(logic):
    wall = box_brush("wall")
    lamp = make_thing(Light, "lamp", (0, 100, 0))
    thread = logic(brushes=[wall], things=[lamp])
    editor_camera = thread.camera.get_editor_camera()
    editor_camera.pos = glm.vec3(10, 20, 30)
    editor_camera.yaw = 45.0

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    assert published.is_play_mode is False
    assert list(published.editor_camera_pos) == pytest.approx([10, 20, 30])
    assert published.editor_camera_yaw == 45.0
    assert published.camera_view_matrix is not None

    # Editor rendering consumes the same canonical dense projections as play
    # rendering; QtGameView must not reconstruct reduced editor-side tables.
    assert published.render_table.count == 1
    assert published.render_refs[0] is wall
    assert published.visible_brush_slots.tolist() == [0]
    assert published.entity_table.count == 1
    assert published.entity_refs[0] is lamp
    assert published.visible_thing_slots.tolist() == [0]


def test_qt_game_view_has_no_editor_side_render_projection():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "engine" / "qt_game_view.py"
    source = path.read_text(encoding="utf-8")

    assert "_editor_render_table" not in source
    assert "_editor_entity_table" not in source
    assert "self._editor_entity_refs" not in source
    assert "entity_table.begin_frame(" not in source
    assert ".render_table.sync(" not in source

    paint_start = source.index("    def paintGL")
    paint_end = source.index("    def show_pos_window", paint_start)
    paint_gl = source[paint_start:paint_end]
    assert "get_render_state()" in paint_gl
    assert "if self.use_threading and self.logic_thread:" not in paint_gl




def test_culling_off_makes_everything_visible(logic):
    brushes = pillar_grid(4, 4, spacing=2000.0)
    thread = logic(brushes=brushes)
    thread.render_runtime.culling_enabled = False

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    assert len(published.visible_brush_slots) == len(brushes), (
        "with culling off all %d brushes should be submitted, %d were"
        % (len(brushes), len(published.visible_brush_slots)))
    assert published.culled_brushes == 0


def test_culling_on_drops_what_is_behind_the_camera(logic):
    brushes = [box_brush("in_front", (0, 0, -600), (64, 64, 64)),
               box_brush("behind", (0, 0, 6000), (64, 64, 64))]
    thread = logic(brushes=brushes)
    thread.render_runtime.culling_enabled = True
    thread.camera.get_editor_camera().pos = glm.vec3(0, 0, 0)
    thread.camera.get_editor_camera().yaw = -90.0        # look down -Z
    thread.camera.get_editor_camera().pitch = 0.0

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    names = {published.render_refs[int(slot)]["name"] for slot in published.visible_brush_slots}
    assert "in_front" in names, (
        "the brush in front of the camera was culled; visible set is %s" % (names,))
    assert "behind" not in names, (
        "the brush behind the camera was submitted; visible set is %s" % (names,))


def test_the_culled_count_and_the_visible_list_agree(logic):
    thread = logic(brushes=pillar_grid(6, 6, spacing=500.0))
    thread.camera.get_editor_camera().pos = glm.vec3(0, 200, 2000)

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    accounted = len(published.visible_brush_slots) + published.culled_brushes
    assert accounted == published.total_brushes, (
        "%d visible + %d culled = %d, but the scene has %d brushes"
        % (len(published.visible_brush_slots), published.culled_brushes,
           accounted, published.total_brushes))


def test_a_mover_is_snapshotted_into_the_dense_render_table(logic):
    """The published compatibility view may reference the world; the dense table is the frame snapshot."""
    mover = box_brush("lift", (0, 0, -400), (128, 32, 128), is_mover=True)
    thread = logic(brushes=[mover])
    thread.render_runtime.culling_enabled = False

    thread.render_runtime.prepare_render_state()
    state = thread.game_state.get_write_state()
    table = state.render_table
    slot = int(state.all_brush_slots[0])
    first = table.center[slot].copy()
    assert first.tolist() == [0.0, 0.0, -400.0]

    mover["pos"] = [0.0, 500.0, -400.0]
    touch(mover)            # movers are not polled; a mover moved by hand says so
    assert table.center[slot].tolist() == first.tolist(), (
        "the dense frame projection changed before the next render-state publish")

    thread.render_runtime.prepare_render_state()
    assert table.center[slot].tolist() == [0.0, 500.0, -400.0]

def test_a_static_brush_is_submitted_by_reference(logic):
    """Copying every static brush per frame would be the whole cost of a level."""
    wall = box_brush("wall", (0, 0, -400))
    thread = logic(brushes=[wall])
    thread.render_runtime.culling_enabled = False

    thread.render_runtime.prepare_render_state()

    state = thread.game_state.get_write_state()
    assert state.render_table.refs[int(state.all_brush_slots[0])] is wall


def test_lights_and_entities_reach_the_render_state(logic):
    things = [make_thing(Light, "lamp", (0, 100, 0)),
              make_thing(Monster, "grunt", (0, 96, -300))]
    thread = logic(things=things)

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    assert published.entity_table.count == 2
    assert len(published.visible_thing_slots) == 2


def test_entity_refs_follow_the_dense_snapshot_when_things_are_appended_between_frames(logic):
    """Entity references are rebuilt from the authoritative table on reconciliation."""
    thread = logic(things=[])
    thread.render_runtime.prepare_render_state()

    monster = make_thing(Monster, "late_monster", (0, 96, -300))
    thread.editor_state.things.append(monster)
    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    assert published.entity_table.count == 1
    assert published.entity_refs[0] is monster


def test_published_entity_rows_carry_their_positions(logic):
    lamp = make_thing(Light, "lamp", (100, 200, -300))
    monster = make_thing(Monster, "grunt", (-50, 96, 700))
    thread = logic(things=[lamp, monster])

    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    slots = published.visible_thing_slots
    assert published.entity_table.pos[slots].tolist() == [
        [100.0, 200.0, -300.0], [-50.0, 96.0, 700.0]]
    assert published.visible_thing_slots.tolist() == [0, 1]


def test_a_moved_entity_updates_its_row_without_reconciling(logic):
    monster = make_thing(Monster, "grunt", (0, 96, -300))
    thread = logic(things=[monster])

    thread.render_runtime.prepare_render_state()
    table = thread.game_state.get_write_state().entity_table
    generation = table.generation

    monster.pos = [800.0, 96.0, -900.0]
    thread.render_runtime.prepare_render_state()

    assert table.pos[0].tolist() == [800.0, 96.0, -900.0]
    assert table.generation == generation


def test_a_same_length_swap_of_the_entity_list_re_rows_the_table(logic):
    """A removal and an addition in one frame leave the count unchanged."""
    first_thing = make_thing(Light, "first", (0, 100, 0))
    second_thing = make_thing(Light, "second", (100, 100, 0))
    thread = logic(things=[first_thing, second_thing])
    thread.render_runtime.prepare_render_state()

    thread.editor_state.things.remove(second_thing)
    third_thing = make_thing(Light, "third", (900, 100, -700))
    thread.editor_state.things.append(third_thing)
    thread.render_runtime.prepare_render_state()

    published = thread.game_state.get_write_state()
    table = published.entity_table
    assert table.ids == [first_thing.properties["id"], third_thing.properties["id"]]
    assert table.pos[1].tolist() == [900.0, 100.0, -700.0]
    assert published.visible_thing_slots.tolist() == [0, 1]


def _publish(thread):
    thread.render_runtime.prepare_render_state()
    assert thread.game_state.request_swap() is True
    return thread.game_state.get_render_state()


def test_a_published_monster_row_is_stable_while_the_ai_moves_it(logic):
    """The renderer draws monsters from the published table's rows.

    The AI thread moves the live Monster whenever it likes; what the renderer
    reads is the row the frame was published with, which nothing writes until
    the renderer hands the frame back.
    """
    monster = make_thing(Monster, "grunt", (0, 96, -300))
    thread = logic(things=[monster])

    frame = _publish(thread)
    monster.pos = [10.0, 96.0, -300.0]            # the AI moves it
    monster.properties["dead"] = True
    touch(monster)
    thread.render_runtime.prepare_render_state()                # the next frame is built

    assert frame.entity_table.pos[0].tolist() == [0.0, 96.0, -300.0]
    assert frame.entity_table is not thread.game_state.get_write_state().entity_table
    thread.game_state.release_render_state(frame)
    assert thread.game_state.request_swap() is True
    moved = thread.game_state.get_render_state()
    assert moved.entity_table.pos[0].tolist() == [10.0, 96.0, -300.0]
    # Sprite ids are per-table intern ids; compare the recipes they name.
    def recipe(table):
        return table.sprite_recipes()[int(table.sprite_key_id[0])][0][0]
    assert recipe(frame.entity_table) == 'msprite_human_<None>_idle_'
    assert recipe(moved.entity_table) == 'msprite_human_<None>_dead_'


# ---------------------------------------------------------------------------
# The dense render projection
# ---------------------------------------------------------------------------

def test_the_projection_covers_every_brush_in_the_session(logic):
    brushes = pillar_grid(3, 3, spacing=300.0)
    thread = logic(brushes=brushes)
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.render_runtime.prepare_render_state()
        table = thread.game_state.get_write_state().render_table
        assert table.count == len(brushes)
        for index, brush in enumerate(brushes):
            assert list(table.center[index]) == pytest.approx(brush["pos"])
            assert list(table.half[index]) == \
                pytest.approx([v * 0.5 for v in brush["size"]])
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_only_movers_and_doors_are_marked_dynamic(logic):
    brushes = [box_brush("static"), box_brush("lift", (200, 0, 0), is_mover=True),
               box_brush("gate", (400, 0, 0), is_door=True)]
    thread = logic(brushes=brushes)
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.render_runtime.prepare_render_state()
        dynamic = sorted(int(i) for i in thread.game_state.get_write_state().render_table.dynamic_slots)
        assert dynamic == [1, 2], (
            "dynamic slots are %s; only the mover and the door move" % (dynamic,))
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_visibility_is_published_as_slots_into_the_projection(logic):
    """The numerical result crosses the thread boundary, not just objects."""
    brushes = pillar_grid(3, 3, spacing=300.0)
    thread = logic(brushes=brushes)
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.render_runtime.prepare_render_state()
        state = thread.game_state.get_write_state()
        table = state.render_table
        slots = state.visible_brush_slots
        assert table is thread.game_state.get_write_state().render_table
        assert len(slots) == len(state.visible_brush_slots)
        # Every slot indexes the row of the brush it was published beside, so a
        # consumer can classify from the columns instead of the dicts.
        for i, slot in enumerate(state.visible_brush_slots):
            assert table.ids[int(slot)] == table.ids[int(slots[i])]
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_hiding_a_brush_mid_session_reaches_the_frame(logic):
    """I/O Show/Hide toggles ``hidden`` at runtime, through the parking-aware
    writer Big World and save restores share, which journals the change."""
    brush = box_brush("switchable", (0, 0, -400))
    thread = logic(brushes=[brush])
    thread.session_runtime.apply_play_mode(True)
    thread.render_runtime.culling_enabled = False
    try:
        thread.render_runtime.prepare_render_state()
        assert len(thread.game_state.get_write_state().all_brush_slots) == 1

        set_authored_flag(brush, "hidden", True)
        thread.render_runtime.prepare_render_state()
        assert len(thread.game_state.get_write_state().all_brush_slots) == 0, (
            "hiding a brush mid-session did not remove it from the frame")
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_the_general_path_is_used_when_the_brush_set_changes_mid_session(logic):
    """A brush added during play invalidates the fixed-size cache by count."""
    thread = logic(brushes=[box_brush("first", (0, 0, -400))])
    thread.session_runtime.apply_play_mode(True)
    thread.render_runtime.culling_enabled = False
    try:
        thread.editor_state.brushes.append(box_brush("second", (100, 0, -400)))
        thread.render_runtime.prepare_render_state()
        state = thread.game_state.get_write_state()
        names = {state.render_refs[int(slot)]["name"] for slot in state.all_brush_slots}
        assert names == {"first", "second"}, (
            "a brush added mid-session did not reach the renderer; the frame "
            "holds %s" % (sorted(names),))
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_a_new_threaded_state_exposes_empty_dense_projections():
    """The renderer may paint before the first logic frame is published."""
    game_state = ThreadedGameState()
    state = game_state.get_render_state()

    assert state.render_table is not None
    assert state.render_table.count == 0
    assert state.entity_table is not None
    assert state.entity_table.count == 0
    assert len(state.render_refs) == 0
    assert len(state.visible_brush_slots) == 0
    assert len(state.all_brush_slots) == 0
    assert len(state.visible_thing_slots) == 0
    assert len(state.thing_hidden) == 0


# ---------------------------------------------------------------------------
# Double buffering
# ---------------------------------------------------------------------------

def test_a_published_frame_is_readable_and_the_next_one_is_separate():
    game_state = ThreadedGameState()
    write = game_state.get_write_state()
    write.total_brushes = 7
    game_state.request_swap()

    read = game_state.get_render_state()
    assert read.total_brushes == 7

    next_write = game_state.get_write_state()
    assert next_write is not read, (
        "the logic thread was handed the buffer the renderer is reading")
    next_write.total_brushes = 9
    assert read.total_brushes == 7, (
        "writing the next frame changed the frame already published")


def test_try_swap_reports_a_new_frame_exactly_once():
    game_state = ThreadedGameState()
    game_state.request_swap()
    assert game_state.try_swap() is True
    assert game_state.try_swap() is False, (
        "the same frame was reported as new twice")


def test_peeking_does_not_consume_the_new_frame_flag():
    game_state = ThreadedGameState()
    game_state.request_swap()
    assert game_state.peek_has_new_frame() is True
    assert game_state.peek_has_new_frame() is True
    assert game_state.try_swap() is True


def test_the_recycled_write_buffer_is_reset():
    """Otherwise last-but-one frame's lists leak into the new frame."""
    game_state = ThreadedGameState()
    first = game_state.get_write_state()
    first.visible_brush_slots = np.array([0], dtype=np.int32)
    game_state.request_swap()          # first becomes the read buffer
    second = game_state.get_write_state()
    second.visible_brush_slots = np.array([0], dtype=np.int32)
    game_state.request_swap()          # first is recycled as the write buffer

    recycled = game_state.get_write_state()
    assert len(recycled.visible_brush_slots) == 0, (
        "the recycled buffer still holds visible brush slots")


def test_a_render_state_snapshot_is_independent_of_later_writes():
    game_state = ThreadedGameState()
    write = game_state.get_write_state()
    write.hud_message = "one"
    game_state.request_swap()
    snapshot = game_state.get_render_state()

    game_state.get_write_state().hud_message = "two"
    game_state.request_swap()

    assert snapshot.hud_message == "one", (
        "a snapshot handed to the renderer changed when the next frame was "
        "published; it now reads %r" % snapshot.hud_message)


def test_dense_projections_are_double_buffered():
    """The renderer's published tables must stay immutable while the next frame is built."""
    game_state = ThreadedGameState()
    brush = box_brush("wall")

    first_write = game_state.get_write_state()
    first_write.render_table.sync([brush], epoch=1)
    first_table = first_write.render_table
    assert not bool(first_table.class_bits[0] & render_table_module.CLASS_FOG)

    game_state.request_swap()
    published = game_state.get_render_state()
    next_write = game_state.get_write_state()

    assert published.render_table is first_table
    assert next_write.render_table is not first_table

    brush["shader"] = "Fog"
    brush["is_fog"] = True
    next_write.render_table.sync([brush], epoch=2)

    assert bool(next_write.render_table.class_bits[0] & render_table_module.CLASS_FOG)
    assert not bool(first_table.class_bits[0] & render_table_module.CLASS_FOG), (
        "editing the write-side table changed the table already published "
        "to the renderer"
    )


def test_recycled_render_state_keeps_dense_projection_objects():
    """Resetting a free buffer must not drop the dense renderer contract."""
    game_state = ThreadedGameState()
    initial_read = game_state.get_render_state()
    render_table = initial_read.render_table
    entity_table = initial_read.entity_table

    # The initial read is borrowed by the caller, so explicitly release it
    # before asking the logic side to recycle that buffer.
    game_state.release_render_state(initial_read)
    game_state.request_swap()
    recycled = game_state.get_write_state()

    assert recycled.render_table is render_table
    assert recycled.entity_table is entity_table
    assert len(recycled.visible_brush_slots) == 0
    assert len(recycled.all_brush_slots) == 0
    assert len(recycled.visible_thing_slots) == 0
    assert len(recycled.thing_hidden) == 0


def _publish_frame(game_state, brushes, things, epoch, visible):
    write = game_state.get_write_state()
    write.render_table.sync(brushes, epoch=epoch)
    write.all_brush_slots = np.arange(len(brushes), dtype=np.int32)
    write.visible_brush_slots = np.array(visible, dtype=np.int32)
    write.entity_table.begin_frame(things, epoch=epoch)
    write.visible_thing_slots = np.array(visible, dtype=np.int32)
    return write, game_state.request_swap()


def test_there_are_exactly_two_buffers():
    """Double buffering: every publication alternates the same two states."""
    game_state = ThreadedGameState()
    seen = set()
    for _ in range(6):
        seen.add(id(game_state.get_write_state()))
        assert game_state.request_swap() is True
    assert len(seen) == 2


def test_a_borrowed_frame_is_not_swapped_until_it_is_returned():
    """While the renderer reads, the logic thread keeps (and rewrites) its buffer.

    The renderer borrows the read buffer for a paint. Publishing then would
    make that buffer the next write buffer, and the next tick would rewrite the
    tables being drawn -- so the swap declines, the logic thread keeps the
    write buffer, and the frame after the paint is the one that gets out.
    """
    game_state = ThreadedGameState()
    brush = box_brush("wall")
    second_brush = box_brush("wall2", (128, 0, 0))
    lamp = make_thing(Light, "lamp", (0, 100, 0))
    second_thing = make_thing(Monster, "grunt", (0, 96, -300))

    _, published = _publish_frame(game_state, [brush], [lamp], 1, [0])
    assert published is True
    assert game_state.try_swap() is True

    snapshot = game_state.get_render_state()
    first_render_table = snapshot.render_table
    first_entity_table = snapshot.entity_table
    first_slots = snapshot.all_brush_slots
    writer = game_state.get_write_state()
    assert writer.render_table is not first_render_table

    brush["shader"] = "Fog"
    brush["is_fog"] = True
    for epoch in (2, 3):
        write, published = _publish_frame(
            game_state, [brush, second_brush], [lamp, second_thing], epoch, [1])
        assert published is False, "swapped while the renderer was reading"
        assert write is writer, "the logic thread lost its write buffer"
        assert game_state.try_swap() is False

        # The borrowed frame is untouched.
        assert snapshot.render_table is first_render_table
        assert first_render_table.count == 1
        assert first_entity_table.count == 1
        assert not (first_render_table.class_bits[0]
                    & render_table_module.CLASS_FOG)
        assert first_slots.tolist() == [0]

    # Letting go publishes the frame that was held back for it.
    game_state.release_render_state(snapshot)
    assert game_state.try_swap() is True
    latest = game_state.get_render_state()
    assert latest.render_table is writer.render_table
    assert latest.render_table.count == 2
    assert latest.visible_thing_slots.tolist() == [1]
    assert game_state.get_write_state().render_table is first_render_table


def test_a_frame_held_back_by_a_paint_is_published_when_the_paint_ends():
    """No waiting for the logic thread's next tick once the renderer is free."""
    game_state = ThreadedGameState()
    assert game_state.request_swap() is True
    assert game_state.try_swap() is True

    painting = game_state.get_render_state()
    game_state.get_write_state().hud_message = "finished"
    assert game_state.request_swap() is False        # the paint is running
    game_state.release_render_state(painting)          # the paint ends

    assert game_state.try_swap() is True
    assert game_state.published("hud_message") == "finished"


def test_a_buffer_the_logic_thread_is_writing_is_never_published_under_it():
    game_state = ThreadedGameState()
    painting = game_state.get_render_state()
    assert game_state.request_swap() is False        # held back, finished
    game_state.get_write_state()                      # the next tick starts on it
    game_state.release_render_state(painting)

    assert game_state.try_swap() is False, (
        "the renderer published a buffer the logic thread had started writing")


def test_a_dropped_snapshot_returns_its_borrow():
    """The finalizer is the safety net for a caller that never releases."""
    game_state = ThreadedGameState()
    snapshot = game_state.get_render_state()
    assert game_state.request_swap() is False
    del snapshot
    assert game_state.request_swap() is True


def test_published_reads_a_field_without_borrowing_the_frame():
    game_state = ThreadedGameState()
    game_state.get_write_state().player_dead = True
    assert game_state.request_swap() is True

    assert game_state.published("player_dead") is True
    assert game_state.published("no_such_field", 7) == 7
    assert game_state.request_swap() is True, (
        "reading one published field pinned the frame")


def test_a_muzzle_flash_is_published_even_if_the_renderer_was_busy(logic):
    """One-shot state waits for a publication instead of expiring per tick.

    The flash was cleared at the start of every play tick, so it reached the
    renderer only if the frame of the tick that fired was the one published --
    not when that swap was declined mid-paint, nor when a catch-up frame ran a
    second tick before publishing.
    """
    thread = logic(brushes=[box_brush("floor", (0, -16, 0), (512, 32, 512))])
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.muzzle_flash_active = True
        busy = thread.game_state.get_render_state()     # renderer mid-paint
        thread._step_frame(0.0)
        assert thread._publish_frame() is False
        thread.game_state.release_render_state(busy)

        thread._step_frame(thread.TICK_DURATION)        # one more play tick
        assert thread._publish_frame() is True
        assert thread.game_state.published("muzzle_flash_active") is True, (
            "the shot's muzzle flash never reached the renderer")
        assert thread.muzzle_flash_active is False
    finally:
        thread.session_runtime.apply_play_mode(False)



# ---------------------------------------------------------------------------
# The dense entity projection
# ---------------------------------------------------------------------------

def test_the_entity_projection_reaches_the_renderer(logic):
    """The production handoff: the renderer classifies from these or not at all.

    Everything the numeric entity path needs has to arrive on the render state
    together -- the table, the per-slot references, the published slots and the
    live hidden mask.  Any one of them missing and ``render_scene`` silently
    falls back to walking the entity list, which is the thing this replaced.
    """
    things = [make_thing(Light, "lamp", (0, 100, 0)),
              make_thing(Monster, "grunt", (0, 96, -300))]
    thread = logic(things=things)
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.render_runtime.prepare_render_state()
        state = thread.game_state.get_write_state()

        assert state.entity_table is thread.game_state.get_write_state().entity_table
        assert state.entity_refs is not None
        assert state.visible_thing_slots is not None
        assert state.thing_hidden is not None
        assert len(state.entity_refs) >= state.entity_table.count
        assert len(state.thing_hidden) >= state.entity_table.count
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_entity_slots_index_the_rows_they_were_published_beside(logic):
    lamp = make_thing(Light, "lamp", (100, 200, -300))
    monster = make_thing(Monster, "grunt", (-50, 96, 700))
    thread = logic(things=[lamp, monster])

    thread.render_runtime.prepare_render_state()
    state = thread.game_state.get_write_state()
    table, slots = state.entity_table, state.visible_thing_slots

    assert len(slots) == len(state.visible_thing_slots)
    assert table.ids[int(slots[0])] == lamp.properties["id"]
    assert table.ids[int(slots[1])] == monster.properties["id"]
    assert np.allclose(table.pos[int(slots[0])], lamp.pos)


def test_a_collected_prop_is_not_published(logic):
    keep = make_thing(Light, "lamp", (0, 100, 0))
    taken = make_thing(Prop, "medkit", (200, 0, 0), collect_enabled=True, collect_collected=True)
    thread = logic(things=[keep, taken])
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.prop_runtime.collected_ids.add(id(taken))
        thread.render_runtime.prepare_render_state()
        state = thread.game_state.get_write_state()

        assert len(state.visible_thing_slots) == 1
        assert len(state.visible_thing_slots) == 1
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_the_light_list_comes_off_the_projection_not_a_scan(logic):
    lamp = make_thing(Light, "lamp", (0, 100, 0))
    thread = logic(things=[lamp, make_thing(Monster, "grunt", (0, 96, -300))])

    thread.render_runtime.prepare_render_state()
    state = thread.game_state.get_write_state()

    assert state.entity_table.light_slots.tolist() == [0]
    assert list(thread.game_state.get_write_state().entity_table.light_slots) == [0]


def test_whether_the_map_has_portals_is_published(logic):
    """The portal virtual views draw sprites through the object path, so the
    view deciding whether to skip the texture overrides has to know."""
    pytest.importorskip("editor.things")
    from editor.things import Portal

    plain = logic(things=[make_thing(Light, "lamp", (0, 100, 0))])
    plain.render_runtime.prepare_render_state()
    assert plain.game_state.get_write_state().has_portals is False

    with_portal = logic(things=[make_thing(Portal, "door", (0, 0, 0))])
    with_portal.session_runtime.apply_play_mode(True)
    try:
        with_portal.render_runtime.prepare_render_state()
        assert with_portal.game_state.get_write_state().has_portals is True
    finally:
        with_portal.session_runtime.apply_play_mode(False)


def test_the_renderer_does_not_reuse_texture_ids_across_an_adopt():
    """Its per-table cache resolved a prefix of the old name list."""
    from engine import render_table as rt
    from engine.renderer_F import Renderer_F

    renderer = Renderer_F.__new__(Renderer_F)
    renderer._gl_tex_by_table = {}
    renderer.texture_manager = {}
    renderer._tex_cache_path = lambda name: name
    renderer.load_texture_callback = lambda name, _folder: {
        'a.png': 11, 'b.png': 22}.get(name, 0)

    table = rt.RenderTable()
    table.intern_texture('a.png')
    before = renderer._gl_texture_ids(table).tolist()

    peer = rt.RenderTable()
    peer.intern_texture('b.png')              # same id, different name
    table.adopt(peer)

    after = renderer._gl_texture_ids(table).tolist()
    assert before[-1] == 11 and after[-1] == 22
