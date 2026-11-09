"""Stage 2a: one placement and bounds resolver, and every consumer agrees.

``engine.brush_placement`` answers "where is this brush and what does it
cover" for rendering, culling, collision rows, the physics grid, Big World
and picking (``docs/brush-model-stage2.md``). These tests hold it to an
independent statement and hold every consumer to it:

* the rules -- a box is ``pos``/``size``; a convex brush is its planes'
  bounds whatever its stored copy says; a mesh proxy is its mesh; a convex
  mover is displaced by its live ``pos``; a spin widens the bound to the
  AABB of the turned box;
* extreme orientations, against a quaternion turn of the box's corners
  (not the resolver's ``|R| . half`` and not its Rodrigues matrices);
* what the renderer draws: every drawn vertex of a spinning row lies in its
  derived bounds, and for a box the bound is exactly the drawn AABB;
* bounds agreement: the RenderTable (as culled), the physics grid's rows and
  filing, the line-of-sight AABB cache, Big World filing, the cordon, the
  collision rows and both editor pickers;
* the two shipped brushes whose stored copy is 128 units too wide.
"""

import copy
import math

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import brush_placement as bp
from engine import render_table as rt
from engine.constants import brush_aabb_bounds
from engine.logic_render import LogicRender
from engine.physics import SpatialGrid
from engine.render_table import RenderTable
from engine.view_filters import brush_bounds
from plugins.bigworld.cell import cells_for_aabb
from plugins.bigworld.manager import brush_cells
from tests.helpers import brush_semantics as bs
from tests.helpers import runtime
from tests.helpers.paths import REPO_ROOT
from tests.helpers.worlds import angled_brush, box_brush

#: (axis, angle in degrees): the identity, slivers either side of it,
#: quarter and half turns, near-full and multi turns, negative angles, the
#: three principal axes, skew axes, an unnormalised axis, and two axes too
#: short to rotate (the renderer's own guard).
ORIENTATIONS = [
    ((0.0, 1.0, 0.0), 0.0),
    ((0.0, 1.0, 0.0), 1e-9),
    ((0.0, 1.0, 0.0), 0.5),
    ((0.0, 1.0, 0.0), 45.0),
    ((0.0, 1.0, 0.0), 89.999),
    ((0.0, 1.0, 0.0), 90.0),
    ((0.0, 1.0, 0.0), 135.0),
    ((0.0, 1.0, 0.0), 180.0),
    ((0.0, 1.0, 0.0), 270.0),
    ((0.0, 1.0, 0.0), 359.999),
    ((0.0, 1.0, 0.0), 360.0),
    ((0.0, 1.0, 0.0), -45.0),
    ((0.0, 1.0, 0.0), 725.0),
    ((1.0, 0.0, 0.0), 45.0),
    ((1.0, 0.0, 0.0), 90.0),
    ((0.0, 0.0, 1.0), 60.0),
    ((1.0, 1.0, 1.0), 120.0),
    ((1.0, 2.0, 3.0), 33.0),
    ((-0.3, 0.9, 0.2), 211.0),
    ((0.0, 100.0, 0.0), 45.0),
    ((0.0, 0.0, 0.0), 45.0),
    ((0.0, 1e-4, 0.0), 45.0),
]

ORIENTATION_IDS = ["%s@%g" % (",".join("%g" % v for v in axis), angle)
                   for axis, angle in ORIENTATIONS]


# ---------------------------------------------------------------------------
# The independent statement
# ---------------------------------------------------------------------------

def _quaternion_turn(points, centre, axis, angle):
    """*points* turned about *centre* by a unit quaternion -- a different
    construction from the engine's rotation matrices."""
    axis = np.asarray(axis, dtype=np.float64)
    length = float(np.linalg.norm(axis))
    points = np.asarray(points, dtype=np.float64)
    if not angle or length <= 0.001:
        return points
    half = math.radians(angle) / 2.0
    q = axis / length * math.sin(half)
    w = math.cos(half)
    v = points - np.asarray(centre, dtype=np.float64)
    t = 2.0 * np.cross(q, v)
    return v + w * t + np.cross(q, t) + np.asarray(centre, dtype=np.float64)


def _corners(centre, half):
    signs = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)],
                     dtype=np.float64)
    return np.asarray(centre, dtype=np.float64) + signs * np.asarray(half, dtype=np.float64)


def _oracle_bounds(centre, half, axis, angle):
    turned = _quaternion_turn(_corners(centre, half), centre, axis, angle)
    return np.concatenate((turned.min(axis=0), turned.max(axis=0)))


def _spun(brush, axis, angle):
    """A copy of *brush* spinning by *angle* about *axis*: its runtime
    transform (Stage 2b), never a key in the dict."""
    brush = copy.deepcopy(brush)
    brush['rotate'] = True
    brush['rot_axis'] = list(axis)
    return runtime.drive(brush, spin=(axis, angle))


def _close(a, b, scale=1.0):
    return np.allclose(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64),
                       atol=1e-9 * max(1.0, scale))


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def test_a_box_is_its_pos_and_size():
    brush = box_brush("box", (10, -20, 30), (64, 32, 128))
    assert bp.world_bounds(brush) == (-22.0, -36.0, -34.0, 42.0, -4.0, 94.0)
    assert bp.authored_bounds(brush) == bp.world_bounds(brush)


def test_a_convex_brush_is_its_planes_not_its_stored_copy():
    brush = angled_brush("cut", (40, 30, -20), (96, 80, 112),
                         clip_normal=(0.4, 1.0, -0.3))
    lo, hi = bg.get_convex(brush).bounds
    expected = tuple(lo.tolist()) + tuple(hi.tolist())
    assert _close(bp.world_bounds(brush), expected, 100)
    brush['size'] = [1.0, 1.0, 1.0]               # a stray write to the copy
    assert _close(bp.world_bounds(brush), expected, 100)
    assert _close(bp.authored_bounds(brush), expected, 100)
    # Since Stage 2d pos is the placement: a write carries the planes.
    brush['pos'] = [p + 1000.0 for p in brush['pos']]
    moved = tuple(v + 1000.0 for v in expected)
    assert _close(bp.world_bounds(brush), moved, 100)
    assert _close(bp.authored_bounds(brush), moved, 100)


def test_an_invalid_plane_set_falls_back_to_pos_and_size_as_it_is_drawn():
    brush = box_brush("bad", (0, 0, 0), (64, 64, 64))
    brush['geometry'] = {'planes': [{'n': [0, 1, 0], 'd': 1.0}]}
    assert bp.shape_convex(brush) is None
    assert bp.world_bounds(brush) == (-32.0, -32.0, -32.0, 32.0, 32.0, 32.0)


def test_a_mesh_proxy_is_its_mesh():
    proxy = {'pos': [0.0, 0.0, 0.0], 'size': [1, 1, 1], '_collision_mode': 'mesh',
             '_mesh_bounds': ([-700.0, 0.0, -50.0], [700.0, 100.0, 50.0])}
    assert bp.world_bounds(proxy) == (-700.0, 0.0, -50.0, 700.0, 100.0, 50.0)


def test_a_convex_mover_is_displaced_by_its_runtime_transform():
    brush = angled_brush("lift", (0, 64, 0), (128, 32, 128),
                         clip_normal=(0.2, 1.0, 0.0), is_mover=True)
    rest = bp.world_bounds(brush)
    authored = copy.deepcopy(brush['pos'])
    runtime.drive(brush, offset=(0.0, 96.0, 0.0))
    moved = bp.world_bounds(brush)
    assert _close(np.subtract(moved, rest), [0, 96, 0, 0, 96, 0], 200)
    # Its home -- what Big World files it under -- does not move, and
    # nothing was written into the brush.
    assert _close(bp.authored_bounds(brush), rest, 200)
    assert brush['pos'] == authored


def test_a_pos_write_carries_a_convex_brush_rigidly():
    """Stage 2d: ``pos`` is the authored placement of every brush, so a raw
    ``pos`` write (a console ``setprop``, a plugin) moves an angled brush as
    it moves a box -- its shape, bounds and draw transform together -- and
    re-solves nothing. (Before Stage 2d its planes were in world space and
    the write moved nothing.)"""
    brush = angled_brush("still", (0, 0, 0), (64, 64, 64))
    shape = bp.shape_convex(brush)
    before = bp.world_bounds(brush)
    brush['pos'] = [brush['pos'][0] + 500.0] + list(brush['pos'][1:])
    after = bp.world_bounds(brush)
    assert after[0] - before[0] == pytest.approx(500.0)
    assert after[3] - before[3] == pytest.approx(500.0)
    assert after[1:3] == before[1:3] and after[4:6] == before[4:6]
    assert bp.shape_convex(brush) is shape


def test_authored_bounds_carry_no_spin():
    brush = _spun(box_brush("bar", (0, 0, 0), (256, 8, 8)), (0, 1, 0), 90.0)
    assert bp.authored_bounds(brush) == (-128.0, -4.0, -4.0, 128.0, 4.0, 4.0)
    lo_x, _, lo_z, hi_x, _, hi_z = bp.world_bounds(brush)
    assert (lo_x, hi_x) == pytest.approx((-4.0, 4.0))
    assert (lo_z, hi_z) == pytest.approx((-128.0, 128.0))


def test_normalise_loaded_rederives_the_copy_and_leaves_the_planes():
    # Stage 2d: a version-5 brush's size is a copy of its local planes'
    # extents and is re-derived; pos is its placement and is left alone.
    brush = angled_brush("drifted", (10, 0, 0), (64, 64, 64))
    planes = copy.deepcopy(brush['geometry']['planes'])
    size = list(brush['size'])
    brush['pos'] = [brush['pos'][0] - 64.0] + list(brush['pos'][1:])
    placed = list(brush['pos'])
    brush['size'] = [brush['size'][0] + 128.0] + brush['size'][1:]
    assert bp.normalise_loaded(brush) == 0.0
    assert brush['size'] == pytest.approx(size)
    assert brush['pos'] == placed
    assert brush['geometry']['planes'] == planes
    box = box_brush("box", (5, 5, 5), (10, 10, 10))
    assert bp.normalise_loaded(box) == 0.0 and box['pos'] == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# Rotation, at extreme orientations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS, ids=ORIENTATION_IDS)
def test_a_turned_half_is_the_aabb_of_the_turned_corners(axis, angle):
    half = (128.0, 4.0, 16.0)
    got = bp.turned_half(*half, axis, angle)
    oracle = _oracle_bounds((0.0, 0.0, 0.0), half, axis, angle)
    assert np.allclose(got, oracle[3:], atol=1e-9)
    assert np.allclose(np.negative(got), oracle[:3], atol=1e-9)


@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS, ids=ORIENTATION_IDS)
def test_the_row_rotations_are_the_scalar_rotation(axis, angle):
    rows, r = bp.rotation_matrices(np.array([list(axis) + [angle]], dtype=np.float64))
    scalar = bp.rotation_matrix(axis, angle)
    if scalar is None:
        assert not len(rows)
    else:
        assert list(rows) == [0]
        assert np.allclose(r[0], np.asarray(scalar), atol=1e-12)


@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS, ids=ORIENTATION_IDS)
@pytest.mark.parametrize("kind", ["box", "convex"])
def test_a_spinning_row_is_drawn_inside_its_derived_bounds(kind, axis, angle):
    base = (box_brush("bar", (300, -40, 1200), (256, 8, 32)) if kind == "box" else
            angled_brush("bar", (300, -40, 1200), (256, 8, 32),
                         clip_normal=(0.3, 1.0, 0.2)))
    brush = _spun(base, axis, angle)
    table = RenderTable()
    table.sync([brush], 1)
    models, _ = rt.model_matrices(table, np.array([0]))
    model = models[0].reshape(4, 4).T.astype(np.float64)
    if kind == "box":
        local = _corners((0, 0, 0), (0.5, 0.5, 0.5))
    else:
        record = table.geometry_records[int(table.geometry_id[0])]
        local = (record.convex.verts - record.origin) / record.scale
    drawn = (model[:3, :3] @ local.T).T + model[:3, 3]
    bounds = np.asarray(bp.world_bounds(brush))
    tolerance = 1e-3            # float32 matrices
    assert (drawn >= bounds[:3] - tolerance).all()
    assert (drawn <= bounds[3:] + tolerance).all()
    oracle = _oracle_bounds(bp.placed_box(brush)[:3], bp.placed_box(brush)[3:],
                            axis, angle)
    assert np.allclose(bounds, oracle, atol=1e-6)
    if kind == "box":       # the bound is tight: the drawn box's own AABB
        assert np.allclose(bounds[:3], drawn.min(axis=0), atol=tolerance)
        assert np.allclose(bounds[3:], drawn.max(axis=0), atol=tolerance)
    # The cull tests exactly this bound (the table's angle is float32).
    culled = bp.turned_bounds(table.bounds[:1], table.rot[:1])[0]
    assert np.allclose(culled[:3] - culled[3:], bounds[:3], atol=1e-4)
    assert np.allclose(culled[:3] + culled[3:], bounds[3:], atol=1e-4)


def test_a_turned_bar_outside_its_unturned_box_is_not_culled():
    """A bar along Z turned a quarter about Y lies along X. A frustum whose
    only plane keeps x >= 100 sees it; its unturned box does not reach."""
    bar = _spun(box_brush("bar", (0, 0, 0), (8, 8, 400)), (0, 1, 0), 90.0)
    table = RenderTable()
    table.sync([bar], 1)
    planes = [(1.0, 0.0, 0.0, -100.0)]
    assert not LogicRender.aabb_in_frustum_bounds(planes, table.bounds[:1])[0]
    assert LogicRender.aabb_in_frustum_bounds(
        planes, bp.turned_bounds(table.bounds[:1], table.rot[:1]))[0]


def test_turned_bounds_leaves_an_unturned_table_alone():
    table = RenderTable()
    table.sync([box_brush("a", (0, 0, 0), (8, 8, 8))], 1)
    view = table.bounds[:1]
    assert bp.turned_bounds(view, table.rot[:1]) is view


# ---------------------------------------------------------------------------
# Every consumer agrees
# ---------------------------------------------------------------------------

def _world(axis=(0.0, 1.0, 0.0), angle=37.0):
    drifted = angled_brush("drifted", (2000, 0, 0), (128, 64, 128),
                           clip_normal=(0.3, 1.0, 0.2))
    drifted['pos'] = [drifted['pos'][0] - 64.0] + drifted['pos'][1:]
    drifted['size'] = [drifted['size'][0] + 128.0] + drifted['size'][1:]
    lift = runtime.drive(angled_brush("lift", (-900, 64, 300), (128, 32, 128),
                                      clip_normal=(0.2, 1.0, 0.0), is_mover=True),
                         offset=(0.0, 96.0, 0.0))
    return [
        box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
        angled_brush("ramp", (600, 40, -700), (256, 80, 256), clip_normal=(0.5, 1.0, 0.1)),
        drifted,
        lift,
        _spun(box_brush("sweeper", (-300, 50, -300), (512, 16, 16)), axis, angle),
        _spun(angled_brush("fan", (900, 120, 900), (300, 20, 60),
                           clip_normal=(0.1, 1.0, 0.3)), axis, angle),
        {'id': 'proxy', 'pos': [1500.0, 0.0, -1500.0], 'size': [1, 1, 1],
         '_collision_mode': 'mesh', '_model_collision': True,
         '_mesh_bounds': ([1300.0, 0.0, -1600.0], [1700.0, 250.0, -1400.0])},
    ]


def _expected_cells(bounds, cell_size):
    return set(cells_for_aabb(bounds[0], bounds[2], bounds[3], bounds[5], cell_size))


@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS[::3], ids=ORIENTATION_IDS[::3])
def test_every_headless_consumer_reads_the_resolvers_bounds(axis, angle):
    brushes = _world(axis, angle)
    expected = [np.asarray(bp.world_bounds(b)) for b in brushes]

    # Rendering, as the main view culls it (proxies are not drawn).
    drawn = [b for b in brushes if not b.get('_model_collision')]
    table = RenderTable()
    table.sync(drawn, 1)
    culled = bp.turned_bounds(table.bounds[:len(drawn)], table.rot[:len(drawn)])
    for slot, brush in enumerate(drawn):
        want = expected[brushes.index(brush)]
        assert np.allclose(culled[slot, :3] - culled[slot, 3:], want[:3], atol=1e-4), brush.get('name')
        assert np.allclose(culled[slot, :3] + culled[slot, 3:], want[3:], atol=1e-4), brush.get('name')

    # The physics grid: per-row boxes, filing, and the line-of-sight cache.
    grid = SpatialGrid()
    grid.populate(brushes)
    rows = grid._cell_rows
    for brush, want in zip(brushes, expected):
        row = rows.brushes.index(brush)
        assert np.array_equal(rows.lo[row], want[:3]), brush.get('name')
        assert np.array_equal(rows.hi[row], want[3:]), brush.get('name')
        filed = {coord for coord, bucket in grid.cells.items()
                 if any(b is brush for b in bucket)}
        assert filed == _expected_cells(want, grid.cell_size), brush.get('name')
        got = np.asarray(brush_aabb_bounds(brush))
        assert np.allclose(got, want, rtol=1e-6, atol=1e-4), brush.get('name')
        assert np.allclose(brush_bounds(brush)[0], want[:3]) and \
            np.allclose(brush_bounds(brush)[1], want[3:]), brush.get('name')

    # Big World files a brush at home: its authored bounds.
    for brush in brushes:
        home = np.asarray(bp.authored_bounds(brush))
        assert set(brush_cells(brush, 512.0)) == _expected_cells(home, 512.0)


@pytest.mark.qt                  # engine.logic_collision imports the editor
@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS[::3], ids=ORIENTATION_IDS[::3])
def test_the_collision_rows_read_the_resolvers_bounds(axis, angle):
    from engine.logic_collision import CollisionRows
    brushes = _world(axis, angle)
    rows = CollisionRows(brushes, 1)
    for slot, brush in enumerate(brushes):
        want = np.asarray(bp.world_bounds(brush))
        assert np.array_equal(rows.lo[:, slot], want[:3]), brush.get('name')
        assert np.array_equal(rows.hi[:, slot], want[3:]), brush.get('name')
        lo32 = np.asarray(brush_aabb_bounds(brush))
        assert np.array_equal(rows.lo32[:, slot], lo32[:3]), brush.get('name')
        assert np.array_equal(rows.hi32[:, slot], lo32[3:]), brush.get('name')


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("axis", "angle"), ORIENTATIONS, ids=ORIENTATION_IDS)
def test_a_ray_hits_a_spinning_box_where_it_is_drawn(axis, angle):
    """Rays aimed at points just inside the turned box hit it; rays aimed
    at points just outside it (but inside its AABB) miss it."""
    centre, half = np.array([40.0, 10.0, -30.0]), np.array([96.0, 8.0, 24.0])
    brush = _spun(box_brush("spinner", centre, half * 2), axis, angle)
    rng = np.random.default_rng(7)
    eye = centre + np.array([0.0, 500.0, 700.0])
    for _ in range(64):
        local = rng.uniform(-1.0, 1.0, 3)
        inside = local * 0.98
        outside = local / np.abs(local).max() * 1.05      # just past a face
        for point, hit in ((inside, True), (outside, False)):
            target = _quaternion_turn([centre + point * half], centre, axis, angle)[0]
            d = target - eye
            d /= np.linalg.norm(d)
            o, dd = bp.ray_to_shape(brush, eye, d)
            t = bp.ray_box(bp.authored_box(brush), o, dd)
            if hit:
                assert t is not None and t <= np.linalg.norm(target - eye) + 1e-6
            elif t is not None:
                # The ray may still cross the box elsewhere -- but never
                # at the target, which lies outside it.
                exit_point = o + np.asarray(dd) * t
                assert np.linalg.norm(exit_point - target) > 1e-6


def test_a_ray_hits_a_displaced_convex_mover_where_it_is_drawn():
    brush = angled_brush("lift", (0, 64, 0), (128, 32, 128),
                         clip_normal=(0.2, 1.0, 0.0), is_mover=True)
    top_before = bp.world_bounds(brush)[4]
    runtime.drive(brush, offset=(0.0, 96.0, 0.0))
    o, d = bp.ray_to_shape(brush, (0.0, 1000.0, 0.0), (0.0, -1.0, 0.0))
    hit = bg.ray_convex_face(bp.shape_convex(brush), o, d)
    assert hit is not None
    assert 1000.0 - hit[0] == pytest.approx(top_before + 96.0, abs=32.0)
    assert 1000.0 - hit[0] > top_before


def _aim(view, monkeypatch, ray_o, ray_d):
    """Point the 3D view's mouse ray (the one thing a test cannot click)."""
    import glm
    monkeypatch.setattr(view, 'get_ray_from_mouse',
                        lambda mx, my: (glm.vec3(*ray_o), glm.vec3(*ray_d)))


@pytest.mark.qt
def test_the_editor_pickers_hit_a_spinning_bar_where_it_is_drawn(main_window, monkeypatch):
    view = main_window.view_3d
    bar = _spun(box_brush("bar", (0, 0, 0), (8, 8, 400)), (0, 1, 0), 90.0)
    main_window.state.brushes[:] = [bar]
    main_window.state.things[:] = []
    # Straight down at x = 150: inside the turned bar (it lies along X),
    # outside its unturned box (which lies along Z).
    _aim(view, monkeypatch, (150.0, 500.0, 0.0), (0.0, -1.0, 0.0))
    assert view.get_object_at_3d(0, 0) is bar
    assert view.get_brush_face_at_coords(0, 0) == (bar, 'top')
    _aim(view, monkeypatch, (0.0, 500.0, 150.0), (0.0, -1.0, 0.0))
    assert view.get_object_at_3d(0, 0) is None
    assert view.get_brush_face_at_coords(0, 0) is None


@pytest.mark.qt
def test_the_editor_pickers_ignore_a_stale_copy(main_window, monkeypatch):
    view = main_window.view_3d
    # Since Stage 2d pos places the planes; size is the copy that can go stale.
    brush = angled_brush("drifted", (0, 0, 0), (128, 64, 128))
    brush['size'] = [brush['size'][0] + 256.0] + brush['size'][1:]
    main_window.state.brushes[:] = [brush]
    main_window.state.things[:] = []
    lo_x = bp.world_bounds(brush)[0]
    # Down through the phantom strip the stale copy claims: nothing there.
    _aim(view, monkeypatch, (lo_x - 30.0, 500.0, 0.0), (0.0, -1.0, 0.0))
    assert view.get_object_at_3d(0, 0) is None
    _aim(view, monkeypatch, (lo_x + 30.0, 500.0, 0.0), (0.0, -1.0, 0.0))
    assert view.get_object_at_3d(0, 0) is brush


# ---------------------------------------------------------------------------
# The two shipped brushes with a 128-unit stale copy
# ---------------------------------------------------------------------------

CLIPPED_TERRAIN = '1a817c94'


@pytest.mark.parametrize("name", ["Terrain_Test_small.json", "Topdown_example.json"])
def test_the_clipped_terrain_is_drawn_and_bounded_by_its_planes(name):
    raws = bs.shipped_maps(REPO_ROOT)[name]
    brushes = [bs.loaded(b) for b in raws]
    slot = next(i for i, b in enumerate(brushes)
                if str(b.get('id')).startswith(CLIPPED_TERRAIN))
    brush = brushes[slot]
    raw = raws[slot]
    stored_lo_x = raw['pos'][0] - raw['size'][0] / 2      # the file's copy
    planes = bg.get_convex(brush)
    lo, hi = planes.bounds
    assert lo[0] - stored_lo_x == pytest.approx(128.0)
    assert np.allclose(lo, bs.world_convex(raw).bounds[0], atol=1e-9)
    # Since Stage 2d the load places the planes about their own centre, so
    # what the editor holds no longer drifts from them.
    assert brush['pos'][0] - brush['size'][0] / 2 == pytest.approx(lo[0], abs=1e-9)

    # Drawn exactly where its planes are.
    table = RenderTable()
    table.sync(brushes, 1)
    record = table.geometry_records[int(table.geometry_id[slot])]
    models, _ = rt.model_matrices(table, np.array([slot]))
    model = models[0].reshape(4, 4).T.astype(np.float64)
    local = ((record.convex.verts - record.origin) / record.scale).astype(np.float32)
    drawn = (model[:3, :3] @ local.astype(np.float64).T).T + model[:3, 3]
    assert np.allclose(drawn, planes.verts, atol=1e-3)

    # Every consumer's bound is the planes'.
    want = np.concatenate((lo, hi))
    assert np.allclose(bp.world_bounds(brush), want, atol=1e-9)
    assert np.allclose(table.bounds[slot, :3] - table.bounds[slot, 3:], lo, atol=1e-9)
    grid = SpatialGrid()
    grid.populate(brushes)
    assert np.allclose(grid._cell_rows.lo[grid._cell_rows.brushes.index(brush)], lo)
    assert np.allclose(brush_aabb_bounds(brush), want, rtol=1e-6)
    assert set(brush_cells(brush, 512.0)) == _expected_cells(want, 512.0)

    # The phantom strip the copy claimed collides with nothing; the solid
    # beside it still does.
    alone = SpatialGrid()
    alone.populate([brush])
    y, z = (lo[1] + hi[1]) / 2.0, (lo[2] + hi[2]) / 2.0
    assert not alone.overlaps_wall((stored_lo_x + lo[0]) / 2.0, y, z, 1.0)
    assert alone.overlaps_wall(lo[0] + 8.0, y, z, 1.0)


def test_a_turned_box_is_the_same_from_the_turn_memo():
    """``turned_half`` keeps ``|R|`` per (axis, angle) so a spinner's trig
    runs once per angle; the memo must change no bit, and stays bounded.
    ``turned_half_now`` (no memo) must agree with it to the bit."""
    import random
    rng = random.Random(7)
    bp._TURNS.clear()
    for _ in range(500):
        axis = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
        angle = rng.choice((0.0, 90.0, 45.0, rng.uniform(-720, 720)))
        half = (rng.uniform(1, 500), rng.uniform(1, 500), rng.uniform(1, 500))
        r = bp.rotation_matrix(axis, angle)
        if r is None:
            expected = half
        else:
            expected = tuple(abs(r[i][0]) * half[0] + abs(r[i][1]) * half[1]
                             + abs(r[i][2]) * half[2] for i in range(3))
        for _again in range(2):                 # a miss, then a hit
            assert bp.turned_half(*half, axis, angle) == expected
        # Worked out now, remembering nothing (the mover table's spinners,
        # whose angle is new every tick): the same bits.
        assert bp.turned_half_now(*half, *axis, angle) == expected
    for k in range(bp._TURNS_LIMIT + 10):
        bp.turned_half(1.0, 1.0, 1.0, (0.0, 1.0, 0.0), 0.001 * (k + 1))
    assert len(bp._TURNS) <= bp._TURNS_LIMIT
    # The vertical axis (turned_half_now's short form): the general rule's
    # bits, at every scale of axis and angle.
    for _ in range(20000):
        ay = rng.choice((1.0, -1.0, 2.0, 0.25, -7.5, 0.0015, rng.uniform(-50, 50)))
        angle = rng.choice((rng.uniform(-1e5, 1e5), rng.uniform(-360, 360),
                            float(rng.randint(-720, 720)), 90.0, 180.0, 1e-9))
        half = (rng.uniform(0, 600), rng.uniform(0, 600), rng.uniform(0, 600))
        assert bp.turned_half_now(*half, 0.0, ay, 0.0, angle) == bp.turned_half(
            *half, (0.0, ay, 0.0), angle)
