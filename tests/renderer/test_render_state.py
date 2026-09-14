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
from editor.things import Light, Monster                # noqa: E402
from engine.logic_thread import LogicThread             # noqa: E402
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
    return thread._extract_frustum_planes(projection * view)


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
    assert thread._aabb_in_frustum(planes, (0, 0, -500), (32, 32, 32)) is True


def test_a_box_behind_the_camera_is_outside_the_frustum(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread._aabb_in_frustum(planes, (0, 0, 500), (32, 32, 32)) is False, (
        "a brush 500 units behind the camera was reported visible")


def test_a_box_far_off_to_the_side_is_outside_the_frustum(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread._aabb_in_frustum(planes, (5000, 0, -100), (32, 32, 32)) is False


def test_a_huge_box_straddling_the_camera_is_inside(logic):
    """Conservativeness: a box the camera is inside must never be culled."""
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert thread._aabb_in_frustum(planes, (0, 0, 0), (10000, 10000, 10000)) is True


def test_the_batched_cull_agrees_with_the_scalar_one_everywhere(logic):
    """The vectorised path is a performance optimisation, not a second answer."""
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    rng = np.random.default_rng(4)
    centers = rng.uniform(-3000, 3000, size=(400, 3))
    halves = rng.uniform(1, 400, size=(400, 3))

    batched = thread._aabb_in_frustum_batch(planes, centers, halves)
    scalar = np.array([thread._aabb_in_frustum(planes, c, h)
                       for c, h in zip(centers, halves)])

    mismatch = np.nonzero(batched != scalar)[0]
    assert mismatch.size == 0, (
        "the batched and scalar frustum tests disagree on %d of %d boxes; "
        "first is centre=%s half=%s (batched=%s scalar=%s)"
        % (mismatch.size, len(centers),
           np.round(centers[mismatch[0]], 2), np.round(halves[mismatch[0]], 2),
           batched[mismatch[0]], scalar[mismatch[0]]))


def test_the_batched_cull_of_an_empty_scene_is_an_empty_result(logic):
    thread = logic()
    planes = _frustum_looking_down_negative_z(thread)
    assert list(thread._aabb_in_frustum_batch(planes, [], [])) == []


# ---------------------------------------------------------------------------
# The published render state
# ---------------------------------------------------------------------------

def test_editor_mode_publishes_the_editor_camera(logic):
    thread = logic(brushes=[box_brush("wall")])
    thread.editor_camera.pos = glm.vec3(10, 20, 30)
    thread.editor_camera.yaw = 45.0

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    assert published.is_play_mode is False
    assert list(published.editor_camera_pos) == pytest.approx([10, 20, 30])
    assert published.editor_camera_yaw == 45.0
    assert published.camera_view_matrix is not None


def test_every_non_hidden_brush_is_in_the_all_brushes_list(logic):
    brushes = pillar_grid(3, 3, spacing=200.0)
    brushes.append(box_brush("hidden_one", (0, 0, 0), hidden=True))
    thread = logic(brushes=brushes)

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    names = {b.get("name") for b in published.all_brushes}
    assert "hidden_one" not in names, "a hidden brush reached the renderer"
    assert len(published.all_brushes) == 9, (
        "expected the 9 visible pillars, got %d" % len(published.all_brushes))
    assert published.total_brushes == 10, (
        "total_brushes should count everything in the scene, it says %d"
        % published.total_brushes)


def test_the_visible_list_is_a_subset_of_the_all_brushes_list(logic):
    thread = logic(brushes=pillar_grid(5, 5, spacing=400.0))
    thread.editor_camera.pos = glm.vec3(0, 200, 1500)

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    all_ids = {id(b) for b in published.all_brushes}
    stray = [b.get("name") for b in published.visible_brushes if id(b) not in all_ids]
    assert not stray, (
        "these brushes are in the visible list but not the all-brushes list: %s"
        % (stray,))


def test_culling_off_makes_everything_visible(logic):
    brushes = pillar_grid(4, 4, spacing=2000.0)
    thread = logic(brushes=brushes)
    thread.culling_enabled = False

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    assert len(published.visible_brushes) == len(brushes), (
        "with culling off all %d brushes should be submitted, %d were"
        % (len(brushes), len(published.visible_brushes)))
    assert published.culled_brushes == 0


def test_culling_on_drops_what_is_behind_the_camera(logic):
    brushes = [box_brush("in_front", (0, 0, -600), (64, 64, 64)),
               box_brush("behind", (0, 0, 6000), (64, 64, 64))]
    thread = logic(brushes=brushes)
    thread.culling_enabled = True
    thread.editor_camera.pos = glm.vec3(0, 0, 0)
    thread.editor_camera.yaw = -90.0        # look down -Z
    thread.editor_camera.pitch = 0.0

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    names = {b.get("name") for b in published.visible_brushes}
    assert "in_front" in names, (
        "the brush in front of the camera was culled; visible set is %s" % (names,))
    assert "behind" not in names, (
        "the brush behind the camera was submitted; visible set is %s" % (names,))


def test_the_culled_count_and_the_visible_list_agree(logic):
    thread = logic(brushes=pillar_grid(6, 6, spacing=500.0))
    thread.editor_camera.pos = glm.vec3(0, 200, 2000)

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    accounted = len(published.visible_brushes) + published.culled_brushes
    assert accounted == published.total_brushes, (
        "%d visible + %d culled = %d, but the scene has %d brushes"
        % (len(published.visible_brushes), published.culled_brushes,
           accounted, published.total_brushes))


def test_a_mover_is_submitted_as_a_snapshot_not_as_the_live_brush(logic):
    """The renderer reads this on another thread while the logic thread moves it."""
    mover = box_brush("lift", (0, 0, -400), (128, 32, 128), is_mover=True)
    thread = logic(brushes=[mover])
    thread.culling_enabled = False

    thread._prepare_render_state()
    published = thread.game_state.get_write_state()
    submitted = published.all_brushes[0]

    assert submitted is not mover, (
        "the live mover dict was handed to the renderer; the logic thread "
        "would rewrite its position mid-frame")
    assert submitted["pos"] == mover["pos"]

    mover["pos"] = [0.0, 500.0, -400.0]
    assert submitted["pos"] != mover["pos"], (
        "the snapshot shares its pos list with the live brush, so moving the "
        "brush changed the frame already published")


def test_a_static_brush_is_submitted_by_reference(logic):
    """Copying every static brush per frame would be the whole cost of a level."""
    wall = box_brush("wall", (0, 0, -400))
    thread = logic(brushes=[wall])
    thread.culling_enabled = False

    thread._prepare_render_state()

    assert thread.game_state.get_write_state().all_brushes[0] is wall


def test_lights_and_entities_reach_the_render_state(logic):
    things = [make_thing(Light, "lamp", (0, 100, 0)),
              make_thing(Monster, "grunt", (0, 96, -300))]
    thread = logic(things=things)

    thread._prepare_render_state()

    published = thread.game_state.get_write_state()
    assert len(published.all_things) == 2
    assert len(published.visible_things) == 2


def test_a_monster_is_submitted_as_a_render_snapshot(logic):
    """The AI thread moves monsters; the renderer must read a stable copy."""
    monster = make_thing(Monster, "grunt", (0, 96, -300))
    thread = logic(things=[monster])

    thread._prepare_render_state()

    submitted = thread.game_state.get_write_state().visible_things[0]
    assert submitted is not monster, (
        "the live Monster object was handed to the renderer while the AI "
        "thread is free to move it")


# ---------------------------------------------------------------------------
# The cull cache
# ---------------------------------------------------------------------------

def test_the_cull_cache_covers_every_brush_in_the_session(logic):
    brushes = pillar_grid(3, 3, spacing=300.0)
    thread = logic(brushes=brushes)
    thread.set_play_mode(True)
    try:
        assert thread._cull_n == len(brushes)
        assert thread._cull_centers.shape == (len(brushes), 3)
        for index, brush in enumerate(brushes):
            assert list(thread._cull_centers[index]) == pytest.approx(brush["pos"])
            assert list(thread._cull_halves[index]) == \
                pytest.approx([v * 0.5 for v in brush["size"]])
    finally:
        thread.set_play_mode(False)


def test_only_movers_and_doors_are_marked_dynamic(logic):
    brushes = [box_brush("static"), box_brush("lift", (200, 0, 0), is_mover=True),
               box_brush("gate", (400, 0, 0), is_door=True)]
    thread = logic(brushes=brushes)
    thread.set_play_mode(True)
    try:
        assert sorted(thread._cull_dynamic_rows) == [1, 2], (
            "dynamic rows are %s; only the mover and the door move"
            % (thread._cull_dynamic_rows,))
    finally:
        thread.set_play_mode(False)


def test_hidden_is_not_baked_into_the_cull_cache(logic):
    """I/O Show/Hide toggles it at runtime, so it is read fresh each frame."""
    brush = box_brush("switchable", (0, 0, -400))
    thread = logic(brushes=[brush])
    thread.set_play_mode(True)
    thread.culling_enabled = False
    try:
        thread._prepare_render_state()
        assert len(thread.game_state.get_write_state().all_brushes) == 1

        brush["hidden"] = True
        thread._prepare_render_state()
        assert thread.game_state.get_write_state().all_brushes == [], (
            "hiding a brush mid-session did not remove it from the frame; the "
            "cull cache baked 'hidden' in at play start")
    finally:
        thread.set_play_mode(False)


def test_the_general_path_is_used_when_the_brush_set_changes_mid_session(logic):
    """A brush added during play invalidates the fixed-size cache by count."""
    thread = logic(brushes=[box_brush("first", (0, 0, -400))])
    thread.set_play_mode(True)
    thread.culling_enabled = False
    try:
        thread.editor_state.brushes.append(box_brush("second", (100, 0, -400)))
        thread._prepare_render_state()
        names = {b.get("name") for b in thread.game_state.get_write_state().all_brushes}
        assert names == {"first", "second"}, (
            "a brush added mid-session did not reach the renderer; the frame "
            "holds %s" % (sorted(names),))
    finally:
        thread.set_play_mode(False)


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
    first.visible_brushes = [box_brush("stale")]
    game_state.request_swap()          # first becomes the read buffer
    second = game_state.get_write_state()
    second.visible_brushes = [box_brush("newer")]
    game_state.request_swap()          # first is recycled as the write buffer

    recycled = game_state.get_write_state()
    assert recycled.visible_brushes == [], (
        "the recycled buffer still holds %s from two frames ago"
        % ([b.get("name") for b in recycled.visible_brushes],))


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
