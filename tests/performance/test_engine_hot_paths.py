"""Work-per-frame guards for the engine's hot paths.

The companion module (``test_hot_path_integrity``) guards the geometry and
component-editing paths.  This one covers what the *running game* does every
tick: the cull buffers, the collision-brush concatenation, the I/O reverse
index, the AI's spatial queries, and the per-frame plugin gate.

Like its companion, every assertion here counts work rather than timing it.  A
test that pinned a microsecond budget would fail on a slow runner and teach
nobody anything; a test that says "this rebuilt the array 60 times a second when
it should have built it once" says exactly what regressed.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="these drive the real logic thread")

from editor import io_system as io                  # noqa: E402
from editor.editor_state import EditorState         # noqa: E402
from editor.io_system import OutputConnection       # noqa: E402
from editor.things import Monster                   # noqa: E402
from engine.constants import brush_aabb_bounds      # noqa: E402
from engine.logic_thread import LogicThread         # noqa: E402
from engine.physics import SpatialGrid              # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, pillar_grid, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.perf]


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
        thread.set_play_mode(False)
        thread.stop()


# ---------------------------------------------------------------------------
# The cull buffers
# ---------------------------------------------------------------------------

def test_the_cull_buffers_are_built_once_per_session_not_per_frame(logic):
    thread = logic(brushes=pillar_grid(6, 6, spacing=300.0))
    thread.set_play_mode(True)
    try:
        centers = thread._cull_centers
        halves = thread._cull_halves
        for _ in range(20):
            thread._prepare_render_state()
        assert thread._cull_centers is centers, (
            "the cull centre array was reallocated during a frame; it is built "
            "once per play session")
        assert thread._cull_halves is halves
    finally:
        thread.set_play_mode(False)


def test_only_dynamic_rows_are_refreshed_each_frame(logic):
    """A static brush's centre must not be rewritten 60 times a second."""
    static = box_brush("static", (0, 0, -400))
    mover = box_brush("lift", (100, 0, -400), is_mover=True)
    thread = logic(brushes=[static, mover])
    thread.set_play_mode(True)
    try:
        thread._prepare_render_state()
        static_row = thread._cull_centers[0].copy()

        # Move both brushes behind the cache's back.  Only the mover's row is
        # meant to follow, because only movers are refreshed per frame.
        static["pos"] = [9999.0, 0.0, -400.0]
        mover["pos"] = [8888.0, 0.0, -400.0]
        thread._prepare_render_state()

        assert np.array_equal(thread._cull_centers[0], static_row), (
            "the static brush's cull row was refreshed; the per-frame loop is "
            "walking every brush, not just the movers")
        assert thread._cull_centers[1][0] == pytest.approx(8888.0), (
            "the mover's cull row was not refreshed, so it would be culled "
            "against its old position")
    finally:
        thread.set_play_mode(False)


def test_the_frustum_test_is_one_batched_numpy_pass(logic):
    """Not a Python loop over brushes, and not one array per plane."""
    thread = logic()
    planes = [(0.0, 0.0, 1.0, 1000.0)] * 6
    centers = np.zeros((500, 3))
    halves = np.ones((500, 3))

    result = thread._aabb_in_frustum_batch(planes, centers, halves)

    assert isinstance(result, np.ndarray) and result.dtype == bool, (
        "the batched frustum test returned %r; a NumPy boolean mask is what "
        "the mask-indexing downstream needs" % type(result).__name__)
    assert result.shape == (500,)


def test_the_collision_brush_list_is_concatenated_once_not_per_tick(logic):
    """It used to be rebuilt per tick, and once per active projectile."""
    from engine.player import Player

    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        thread.player = Player(0.0, 0.0)
        before = thread._collision_brushes_cache
        for _ in range(20):
            thread._tick(1.0 / 60.0)
            thread._prepare_render_state()
        assert thread._collision_brushes_cache is before, (
            "the combined collision brush list was rebuilt during 20 ticks; "
            "it only changes on a model-collision toggle or a play-mode "
            "transition")
    finally:
        thread.set_play_mode(False)


# ---------------------------------------------------------------------------
# The AABB cache
# ---------------------------------------------------------------------------

def test_an_unmoved_brushs_aabb_is_computed_once():
    brush = box_brush("wall", (10, 20, 30), (64, 64, 64))
    first = brush_aabb_bounds(brush)
    for _ in range(100):
        assert brush_aabb_bounds(brush) is first, (
            "the AABB tuple was rebuilt for an unchanged brush; the physics "
            "and AI hot paths call this thousands of times a second")


def test_a_moved_brushs_aabb_is_recomputed_exactly_once_per_move():
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64), is_mover=True)
    brush_aabb_bounds(brush)
    brush["pos"] = [100.0, 0.0, 0.0]
    moved = brush_aabb_bounds(brush)
    for _ in range(50):
        assert brush_aabb_bounds(brush) is moved


# ---------------------------------------------------------------------------
# The spatial grid
# ---------------------------------------------------------------------------

def test_a_point_query_touches_only_the_local_cell():
    """Cost is proportional to what is nearby, not to the size of the world."""
    brushes = pillar_grid(12, 12, spacing=700.0)     # spread over many cells
    grid = SpatialGrid()
    grid.populate(brushes)

    found = grid.get_nearby_brushes(0.0, 0.0)

    assert len(found) < len(brushes) / 4, (
        "a point query returned %d of %d brushes; it is scanning the world "
        "rather than the cell" % (len(found), len(brushes)))


def test_a_radius_query_visits_only_occupied_cells():
    grid = SpatialGrid()
    # Wholly inside cell (0, 0), so exactly one cell is occupied.
    grid.populate([box_brush("lonely", (256, 0, 256), (64, 64, 64))])
    # A radius covering millions of cells, only one of which exists.
    assert grid._index.cells_within(0.0, 0.0, 200000.0) == {(0, 0)}, (
        "a 200000-unit radius query walked the whole square instead of the "
        "occupied cells; it visited %s"
        % (sorted(grid._index.cells_within(0.0, 0.0, 200000.0)),))


def test_populating_the_grid_stores_references_not_copies():
    brushes = pillar_grid(4, 4, spacing=300.0)
    grid = SpatialGrid()
    grid.populate(brushes)
    originals = {id(b) for b in brushes}
    stored = {id(b) for bucket in grid.cells.values() for b in bucket}
    assert stored <= originals, (
        "the grid holds %d objects that are not the scene's brushes; it copied "
        "them" % len(stored - originals))


# ---------------------------------------------------------------------------
# The I/O reverse index
# ---------------------------------------------------------------------------

def test_the_reverse_index_is_built_once_while_nothing_changes():
    brushes = [box_brush("b%d" % i, (i * 100, 0, 0), is_trigger=True)
               for i in range(20)]
    things = []
    for brush in brushes:
        io.add_connection(brush, OutputConnection(
            output_name="OnTrigger", target_name="target", input_name="Open"))

    first = io.target_index(brushes, things)
    for _ in range(50):
        assert io.target_index(brushes, things) is first, (
            "the reverse index was rebuilt with no connection change; the "
            "property panel rebuilds it on every selection")


def test_the_reverse_index_is_rebuilt_when_a_connection_changes():
    brushes = [box_brush("a", is_trigger=True)]
    first = io.target_index(brushes, [])
    io.add_connection(brushes[0], OutputConnection(
        output_name="OnTrigger", target_name="t", input_name="Open"))
    assert io.target_index(brushes, []) is not first, (
        "adding a connection did not invalidate the cached index")


# ---------------------------------------------------------------------------
# MonsterAI
# ---------------------------------------------------------------------------

def test_the_ai_routes_its_queries_through_the_grid_not_the_brush_list(logic):
    """Its fallback is an O(all brushes) scan, and must never be what runs."""
    brushes = room(size=4096.0) + pillar_grid(6, 6, spacing=500.0)
    monster = make_thing(Monster, "grunt", (0, 96, 0), awake=True)
    thread = logic(brushes=brushes, things=[monster])
    thread.set_play_mode(True)
    try:
        from engine.player import Player
        thread.player = Player(0.0, 0.0)
        grid = thread._spatial_grid
        calls = {"wall": 0, "ground": 0}
        real_wall, real_ground = grid.overlaps_wall, grid.raycast_down

        def _wall(*args, **kwargs):
            calls["wall"] += 1
            return real_wall(*args, **kwargs)

        def _ground(*args, **kwargs):
            calls["ground"] += 1
            return real_ground(*args, **kwargs)

        grid.overlaps_wall = _wall
        grid.raycast_down = _ground

        thread.monster_ai.update(1.0 / 30.0)

        assert calls["ground"] > 0, (
            "the AI's ground query did not go through the spatial grid; it "
            "fell back to scanning all %d brushes" % len(brushes))
    finally:
        thread.set_play_mode(False)


def test_the_precomputed_monster_list_is_used_rather_than_a_type_scan(logic):
    """Scanning every thing for ``isinstance(Monster)`` every tick was the
    old shape; the list is built once on play-mode enter."""
    monsters = [make_thing(Monster, "m%d" % i, (i * 100, 96, 0), awake=True)
                for i in range(5)]
    thread = logic(brushes=room(), things=monsters)
    thread.set_play_mode(True)
    try:
        assert len(thread._monster_things) == 5, (
            "the monster cache holds %d of 5 monsters"
            % len(thread._monster_things))
        assert all(m in thread._monster_things for m in monsters)
    finally:
        thread.set_play_mode(False)


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

def test_a_session_with_no_ticking_plugin_gates_the_per_frame_call(logic):
    """``wants_tick`` is what makes an idle plugin system free per frame."""
    thread = logic(brushes=room())
    if thread.plugins is None:
        pytest.skip("the plugin system is unavailable in this build")
    for plugin in thread.plugins.plugins:
        thread.plugins.set_enabled(plugin, False)
    assert thread.plugins.wants_tick() is False, (
        "every plugin is disabled but the engine would still dispatch a "
        "per-frame tick to them")


def test_the_tick_gate_answer_is_cached_across_frames(logic):
    thread = logic(brushes=room())
    if thread.plugins is None:
        pytest.skip("the plugin system is unavailable in this build")
    manager = thread.plugins
    first = manager.wants_tick()
    generation = manager._tick_work_gen
    for _ in range(100):
        assert manager.wants_tick() is first
    assert manager._tick_work_gen == generation, (
        "the tick gate recomputed itself with nothing changed; it is meant to "
        "be one integer compare per frame")
