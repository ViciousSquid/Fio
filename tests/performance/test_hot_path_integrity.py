"""Guards against Python creeping back into paths that are meant to stay cheap.

Fio's performance architecture is deliberate: vectorised NumPy where the data is
bulk, plain Python kept out of per-frame and per-element work, and derived
geometry cached so nothing rebuilds while nothing has changed.  Those are easy
to undo by accident — an abstraction added in one place turns into a rebuild in
another — and hard to notice, because the result is still correct.

So these tests assert *how much work happens*, not what it produces: how many
times the windings are recomputed, whether a cached result is handed back by
identity, and which objects a query was allowed to touch.  They are coarse on
purpose; a test that pinned an exact microsecond count would fail on a slow
runner and teach nobody anything.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine import brush_geometry as bg  # noqa: E402
from engine import spatial  # noqa: E402
from engine.physics import SpatialGrid  # noqa: E402


@pytest.fixture
def count_windings(monkeypatch):
    """Counts how many times the O(planes^2) winding solve actually runs."""
    calls = []
    real = bg.compute_windings

    def counted(planes, eps=bg.EPS):
        calls.append(len(planes))
        return real(planes, eps)

    monkeypatch.setattr(bg, 'compute_windings', counted)
    return calls


def make_angled():
    brush = {'id': 'w', 'pos': [0, 0, 0], 'size': [256, 256, 256]}
    assert bg.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    return brush


# ---------------------------------------------------------------------------
# Derived geometry is derived once
# ---------------------------------------------------------------------------

def test_reading_a_brushs_geometry_repeatedly_solves_it_once(count_windings):
    brush = make_angled()
    count_windings.clear()

    first = bg.get_convex(brush)
    for _ in range(50):
        assert bg.get_convex(brush) is first

    assert len(count_windings) == 0, "a cached read rebuilt the windings"


def test_the_component_overlay_is_not_rebuilt_while_nothing_changes():
    from editor import component_edit as ce

    brush = {'id': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([ce.components(brush, ce.MODE_VERTEX)[0]])

    first = controller.overlay([brush])
    for _ in range(100):
        assert controller.overlay([brush]) is first


def test_setting_the_same_hover_again_does_not_invalidate_the_overlay():
    """Hover is refreshed on every mouse move; it must be free when unchanged."""
    from editor import component_edit as ce

    brush = {'id': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_hover(ce.components(brush, ce.MODE_VERTEX)[0])
    version = controller.version

    for _ in range(10):
        same = ce.components(brush, ce.MODE_VERTEX)[0]
        assert controller.set_hover(same) is False
    assert controller.version == version


def test_a_drag_leaves_other_brushes_caches_alone():
    from editor import component_edit as ce

    dragged = {'id': 'a', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    bystander = {'id': 'b', 'pos': [512, 0, 0], 'size': [64, 64, 64]}
    bg.get_shape(bystander)                       # warm its cache
    cached = bystander['_box_shape']

    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.press(ce.components(dragged, ce.MODE_VERTEX)[0])
    for step in range(1, 5):
        controller.update_drag(np.array([float(step) * 8.0, 0.0, 0.0]))
    controller.commit_drag()

    assert bystander.get('_box_shape') is cached


def test_a_component_pick_only_touches_the_brushes_it_is_given():
    """Picking is O(selection), never O(scene) — that is the whole contract."""
    from editor import component_edit as ce

    targets = [{'id': str(i), 'pos': [i * 128, 0, 0], 'size': [64, 64, 64]}
               for i in range(3)]
    rest = [{'id': 'far-%d' % i, 'pos': [i * 128, 0, 4096], 'size': [64, 64, 64]}
            for i in range(200)]

    ce.pick_component_2d(targets, ce.MODE_VERTEX, (0.0, 0.0), 0, 2, 16.0)

    assert all(b.get('_box_shape') is None for b in rest), \
        "picking walked past the brushes it was given"


def test_a_drag_recomputes_from_its_snapshot_not_from_the_last_frame():
    """Total-delta updates: no accumulated drift, and cancel is exact."""
    from editor import component_edit as ce

    brush = {'id': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    before = np.sort(bg.brush_points(brush).copy(), axis=0)

    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.press(ce.components(brush, ce.MODE_VERTEX)[0])
    for step in range(1, 40):
        controller.update_drag(np.array([step * 0.5, 0.0, 0.0]))
    controller.cancel_drag()

    assert np.allclose(np.sort(bg.brush_points(brush), axis=0), before)


# ---------------------------------------------------------------------------
# Vectorised paths stay vectorised
# ---------------------------------------------------------------------------

def test_the_plane_arrays_are_built_once_per_geometry():
    """Point/AABB queries share one NumPy view rather than rebuilding it."""
    shape = bg.get_convex(make_angled())
    first = shape.plane_arrays()
    for _ in range(20):
        assert shape.plane_arrays() is first


def test_an_aabb_query_is_one_batched_pass():
    """No Python loop over planes: the separating-axis test is a single matmul."""
    shape = bg.get_convex(make_angled())
    normals, offsets = shape._collision_planes_cached()
    assert isinstance(normals, np.ndarray) and normals.ndim == 2
    assert isinstance(offsets, np.ndarray) and offsets.ndim == 1
    assert shape._collision_planes_cached()[0] is normals


def test_triangulation_returns_float32_arrays_ready_for_a_vbo():
    positions, normals, uvs = bg.get_convex(make_angled()).triangulate()
    assert positions.dtype == np.float32
    assert normals.dtype == np.float32
    assert uvs.dtype == np.float32
    assert len(positions) == len(normals) == len(uvs)


def test_the_component_overlay_hands_the_renderer_float32_arrays():
    from editor import component_edit as ce

    brush = {'id': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_EDGE)
    overlay = controller.overlay([brush])
    for key in ('points', 'hot_points', 'lines', 'hot_lines'):
        assert overlay[key].dtype == np.float32


def test_brush_edges_are_derived_once_per_shape():
    shape = bg.get_convex(make_angled())
    first = shape.edges
    for _ in range(20):
        assert shape.edges is first


def test_the_hull_rebuild_stays_vectorised():
    """convex_hull_planes is the per-mouse-move cost of a vertex drag."""
    points = bg.brush_points({'pos': [0, 0, 0], 'size': [64, 64, 64]})
    planes = bg.convex_hull_planes(points)
    assert len(planes) == 6
    # The candidate enumeration is capped so a drag cannot stall the editor.
    assert bg.convex_hull_planes(np.random.rand(bg.MAX_HULL_POINTS + 1, 3)) == []


# ---------------------------------------------------------------------------
# The spatial grid's query path
# ---------------------------------------------------------------------------

def test_a_single_cell_query_returns_that_cells_bucket():
    """The overwhelmingly common case must stay one dict lookup."""
    import glm

    brushes = [{'id': str(i), 'pos': [i * 8, 0, 0], 'size': [4, 4, 4]}
               for i in range(10)]
    grid = SpatialGrid()
    grid.populate(brushes)

    found = grid.get_potential_colliders(glm.vec3(0, 0, 0), glm.vec3(1, 1, 1))
    assert len(found) == 10


def test_the_grid_exposes_its_buckets_directly_to_the_query_methods():
    """No wrapper object may sit between a collision query and the dict."""
    grid = SpatialGrid()
    assert grid.cells is grid._index.cells
    assert isinstance(grid.cells, dict)


def test_a_radius_query_skips_cells_that_hold_nothing():
    """Cost is proportional to occupied local cells, not to the world."""
    cells = {(0, 0): 1, (1000, 1000): 1}
    assert spatial.cells_within(cells, 0.0, 0.0, 1024.0) == {(0, 0)}


def test_cell_maths_agrees_with_floor_division_including_negatives():
    for x, z in ((0.0, 0.0), (511.9, -0.1), (-1.0, -513.0), (1536.0, 2048.0)):
        assert (spatial.cell_of_point(x, z) ==
                (int(x // spatial.CELL_SIZE), int(z // spatial.CELL_SIZE)))


def test_a_brush_spanning_cells_is_referenced_not_copied():
    brushes = [{'id': 'wide', 'pos': [512, 0, 0], 'size': [2048, 64, 64]}]
    grid = SpatialGrid()
    grid.populate(brushes)
    filed = [b for bucket in grid.cells.values() for b in bucket]
    assert len(filed) > 1
    assert all(b is brushes[0] for b in filed)
