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
from editor.things import Light, Monster            # noqa: E402
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
        thread._prepare_render_state()
        table = thread._render_table
        centers = table.center
        halves = table.half
        generation = table.generation
        for _ in range(20):
            thread._prepare_render_state()
        assert table.center is centers, (
            "the projection's centre array was reallocated during a frame; it "
            "is built once and refreshed in place")
        assert table.half is halves
        assert table.generation == generation, (
            "the projection reconciled during a steady-state frame; the world "
            "epoch has not moved, so sync should be a couple of comparisons")
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
        table = thread._render_table
        static_row = table.center[0].copy()

        # Move both brushes behind the projection's back.  Only the mover's row
        # is meant to follow, because only movers are refreshed per frame.
        static["pos"] = [9999.0, 0.0, -400.0]
        mover["pos"] = [8888.0, 0.0, -400.0]
        thread._prepare_render_state()

        assert np.array_equal(table.center[0], static_row), (
            "the static brush's row was refreshed; the per-frame loop is "
            "walking every brush, not just the movers")
        assert table.center[1][0] == pytest.approx(8888.0), (
            "the mover's row was not refreshed, so it would be culled "
            "against its old position")
    finally:
        thread.set_play_mode(False)


def test_classification_is_not_re_resolved_per_frame(logic):
    """The cold columns are the expensive half; they must not move per frame.

    This is the property the whole projection exists for: ``is_water_brush``
    and the texture scan used to run per visible brush per frame.  Mutating a
    classification field behind the projection's back and seeing the column
    stay put is what proves the work is no longer happening.
    """
    brush = box_brush("wall", (0, 0, -400))
    thread = logic(brushes=[brush])
    thread.set_play_mode(True)
    try:
        thread._prepare_render_state()
        table = thread._render_table
        before = int(table.class_bits[0])

        brush["shader"] = "Glass"          # no epoch bump: nobody was told
        for _ in range(10):
            thread._prepare_render_state()
        assert int(table.class_bits[0]) == before, (
            "the classification columns were re-resolved during a frame")

        # ...and the editor's coarse change signal is what picks it up.
        thread.editor_state.mark_world_changed()
        thread._prepare_render_state()
        from engine.render_table import CLASS_GLASS
        assert int(table.class_bits[0]) & CLASS_GLASS, (
            "a world-epoch bump did not re-resolve the cold columns")
    finally:
        thread.set_play_mode(False)


def test_entity_classification_is_not_re_resolved_per_frame(logic):
    """The entity projection's cold column, held to the same rule as the brush one.

    ``_sort_objects`` asked every entity what it was on every frame -- four
    isinstance tests, a ``str().lower()`` and a tuple compare each.  The column
    answering instead is only worth having if it stays put between edits.
    """
    from engine import entity_table as et

    thing = make_thing(Light, "lamp", (0, 100, 0))
    thread = logic(things=[thing])
    thread.set_play_mode(True)
    try:
        thread._prepare_render_state()
        table = thread._entity_table
        before = int(table.class_bits[0])

        thing.properties["render_mode"] = "billboard"   # nobody was told
        thing.properties["sprite_path"] = "s.png"
        for _ in range(10):
            thread._prepare_render_state()
        assert int(table.class_bits[0]) == before, (
            "the entity classification column was re-resolved during a frame")

        thread.editor_state.mark_world_changed()
        thread._prepare_render_state()
        assert int(table.class_bits[0]) & et.ENT_MODE_BILLBOARD, (
            "a world-epoch bump did not re-resolve the entity column")
    finally:
        thread.set_play_mode(False)


def test_only_monster_rows_are_republished_each_frame(logic):
    """Entities whose reference cannot change are handed over by identity."""
    lamp = make_thing(Light, "lamp", (0, 100, 0))
    grunt = make_thing(Monster, "grunt", (0, 96, -300))
    thread = logic(things=[lamp, grunt])
    thread.set_play_mode(True)
    try:
        thread._prepare_render_state()
        first = list(thread.game_state.get_write_state().visible_things)
        thread._prepare_render_state()
        second = list(thread.game_state.get_write_state().visible_things)

        assert first[0] is second[0] is lamp, (
            "a Light was copied between frames; only Monsters need a snapshot")
        assert first[1] is not second[1], (
            "the Monster snapshot was not refreshed, so the renderer would "
            "read a frame-old copy")
        assert list(thread._entity_table.monster_slots) == [1]
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


# ---------------------------------------------------------------------------
# Portals
# ---------------------------------------------------------------------------

def test_portal_fades_tick_off_the_cache_not_the_thing_list(logic):
    """``_update_portals`` runs every frame and used to isinstance-scan every
    Thing in the map to find the portals — on a map with none at all."""
    from editor.things import Portal
    from engine.player import Player

    portals = [Portal(pos=[0, 0, float(i) * 200.0],
                      properties={'name': 'P%d' % i}) for i in range(3)]
    filler = [make_thing(Light, "L%d" % i) for i in range(50)]
    thread = logic(brushes=room(), things=filler + portals)
    thread.set_player(Player(0.0, 0.0, 0.0))
    thread.set_play_mode(True)
    try:
        assert thread._portal_things == portals
        assert thread._portal_slots.tolist() == [50, 51, 52]
        assert thread._portal_target_slots.tolist() == [-1, -1, -1]

        # Fades still advance, and they advance for portals the name index
        # cannot hold (an unnamed portal is still a portal).
        unnamed = Portal(pos=[500, 0, 0])
        thread.editor_state.things.append(unnamed)
        thread._build_entity_caches()
        assert unnamed in thread._portal_things
        assert '' not in [p.properties.get('name', '') for p in thread._portal_things]

        for p in thread._portal_things:
            p._fade_alpha, p._fade_target = 0.0, 1.0
        thread._update_portals(1.0 / 60.0)
        assert all(p._fade_alpha > 0.0 for p in thread._portal_things)

        # And the per-frame path must not walk the level to find them.
        scanned = []
        original = type(thread).things
        try:
            type(thread).things = property(
                lambda self: (scanned.append(1), original.fget(self))[1])
            thread._update_portals(1.0 / 60.0)
        finally:
            type(thread).things = original
        assert scanned == [], (
            "_update_portals read the full thing list %d times in one frame"
            % len(scanned))
    finally:
        thread.set_play_mode(False)


def test_portal_transit_keeps_player_at_mapped_plane_not_body_clearance(logic):
    """Walking through a portal must not add a player-sized camera jump."""
    from editor.things import Portal
    from engine.player import Player
    from engine.portal_transform import map_direction, map_point
    import glm

    portal_a = Portal(pos=[0.0, 0.0, 0.0], properties={
        'name': 'A', 'portal_target': 'B',
        'rotation': [0.0, 0.0, 0.0],
    })
    portal_b = Portal(pos=[100.0, 0.0, 0.0], properties={
        'name': 'B', 'portal_target': 'A',
        'rotation': [180.0, 0.0, 0.0],
    })
    thread = logic(brushes=[], things=[portal_a, portal_b])
    player = Player(0.0, -10.0, 0.0)
    player.pos = glm.vec3(0.0, 20.0, -4.0)
    player.velocity = glm.vec3(0.0, 0.0, -120.0)
    thread.set_player(player)
    thread.set_play_mode(True)
    try:
        expected = map_point(
            portal_a.pos, portal_a.get_basis(),
            portal_b.pos, portal_b.get_basis(),
            tuple(player.pos),
        )
        expected_velocity = map_direction(
            portal_a.get_basis(), portal_b.get_basis(), tuple(player.velocity))

        thread._execute_portal_transit(portal_a, portal_b)

        actual = tuple(thread.player.pos)
        displacement = ((actual[0] - expected[0]) * portal_b.get_normal()[0]
                        + (actual[1] - expected[1]) * portal_b.get_normal()[1]
                        + (actual[2] - expected[2]) * portal_b.get_normal()[2])
        assert np.isclose(displacement, 0.05, atol=1e-6)
        assert np.allclose(tuple(thread.player.velocity), expected_velocity)
    finally:
        thread.set_play_mode(False)


def test_a_map_with_no_portals_pays_nothing_for_the_portal_system(logic):
    from engine.player import Player

    thread = logic(brushes=room(),
                   things=[make_thing(Light, "L%d" % i) for i in range(20)])
    thread.set_player(Player(0.0, 0.0, 0.0))
    thread.set_play_mode(True)
    try:
        assert thread._portal_things == []
        thread._portal_prev_player_pos = None
        thread._update_portals(1.0 / 60.0)
        assert thread._portal_prev_player_pos is None, (
            "the portal system did per-frame work on a map with no portals")
    finally:
        thread.set_play_mode(False)
