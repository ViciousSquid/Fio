"""Grid-assisted brush culling returns exactly what the full cull returns.

The cell index (:mod:`engine.render_cells`) only chooses which rows the frustum
test is run on; it must never change the answer. Every test here builds a
world, drives it through the frame paths that change bounds (movers, journalled
moves, editor drags, hide/show, row-set changes), and checks, frame by frame
and camera by camera, that the visible slots are identical with the index and
without it.
"""

import math
import random

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from engine import render_cells                          # noqa: E402
from engine.change_journal import moved                  # noqa: E402
from engine.logic_thread import LogicThread              # noqa: E402
from engine.render_table import RenderTable              # noqa: E402
from engine.spatial import CELL_SIZE, set_authored_flag  # noqa: E402
from tests.helpers.worlds import box_brush               # noqa: E402

pytestmark = pytest.mark.qt

EXTENT = 12000.0


def _culler():
    return LogicThread.__new__(LogicThread)


def _planes(eye, look, fov=75.0, aspect=16.0 / 9.0, far=10000.0):
    eye = glm.vec3(*eye)
    look = glm.normalize(glm.vec3(*look))
    up = glm.vec3(0, 0, -1) if abs(look.y) > 0.99 else glm.vec3(0, 1, 0)
    projection = glm.perspective(glm.radians(fov), aspect, 1.0, far)
    return _culler()._extract_frustum_planes(
        projection * glm.lookAt(eye, eye + look, up))


def _both(table, planes):
    thread = _culler()
    keep, _ = table.shown()
    thread.brush_cell_culling = False
    full = thread._cull_brush_slots(planes, table, keep)
    thread.brush_cell_culling = True
    grid = thread._cull_brush_slots(planes, table, keep)
    return full, grid


def _assert_same(table, planes, what=""):
    full, grid = _both(table, planes)
    assert np.array_equal(full, grid), (
        "%s: the grid cull kept %d rows, the full cull %d; only in full: %r, "
        "only in grid: %r" % (what, len(grid), len(full),
                              np.setdiff1d(full, grid)[:8].tolist(),
                              np.setdiff1d(grid, full)[:8].tolist()))
    return full


def _world(rng, count=900):
    """Small boxes, cell-straddlers, huge slabs, hidden rows and movers."""
    brushes = []
    for i in range(count):
        kind = rng.random()
        x, z = rng.uniform(-EXTENT, EXTENT), rng.uniform(-EXTENT, EXTENT)
        if kind < 0.5:                                    # small, one cell
            size = (rng.uniform(8, 96),) * 3
        elif kind < 0.75:                                 # spans several cells
            size = (rng.uniform(300, 2500), rng.uniform(8, 400),
                    rng.uniform(300, 2500))
            # Snap some edges onto cell boundaries, where filing is inclusive.
            if rng.random() < 0.5:
                x = round(x / CELL_SIZE) * CELL_SIZE + size[0] * 0.5
        elif kind < 0.78:                                 # too wide to file
            size = (rng.uniform(6000, 30000), 32.0, rng.uniform(6000, 30000))
        else:
            size = (64.0, rng.uniform(64, 900), 64.0)     # tall pillar
        extra = {}
        if rng.random() < 0.1:
            extra["hidden"] = True
        if rng.random() < 0.05:
            extra["is_mover"] = True
        brushes.append(box_brush("b%d" % i, (x, rng.uniform(-200, 600), z),
                                 size, **extra))
    return brushes


def _cameras(rng, count):
    for _ in range(count):
        eye = (rng.uniform(-EXTENT, EXTENT), rng.uniform(-100, 4000),
               rng.uniform(-EXTENT, EXTENT))
        look = (rng.gauss(0, 1), rng.uniform(-1.0, 0.3), rng.gauss(0, 1))
        yield eye, look


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(6))
def test_random_worlds_and_cameras_agree(seed):
    rng = random.Random(seed)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    for eye, look in _cameras(rng, 40):
        for far in (3000.0, 10000.0, 40000.0):
            _assert_same(table, _planes(eye, look, far=far),
                         "eye %r look %r far %r" % (eye, look, far))
    assert len(table.cells.unfiled), "the world should include unfiled slabs"


def test_a_brush_spanning_many_cells_is_found_from_any_of_them():
    """Looking at only the far corner of a straddling brush still keeps it."""
    wide = box_brush("wide", (3 * CELL_SIZE, 0, 0), (5 * CELL_SIZE, 64, 64))
    table = RenderTable()
    table.begin_frame([wide], 1)
    # Standing just past its +X end, looking back at the last few units.
    visible = _assert_same(table, _planes((5.5 * CELL_SIZE + 40, 0, 0), (-1, 0, 0),
                                          far=100.0), "end-on")
    assert visible.tolist() == [0]


def test_corner_false_positives_of_the_plane_test_are_kept():
    """The per-plane AABB test also keeps some boxes wholly outside the frustum.

    The index must keep exactly those too -- which is why it tests cell boxes
    with the same planes rather than intersecting an XZ region.
    """
    planes = _planes((0, 64, 0), (1, 0, 0), fov=60.0, aspect=1.0, far=2000.0)
    thread = _culler()
    # Boxes straddling the far-left corner's two planes, outside the frustum.
    reach = 2000.0 * math.tan(math.radians(30.0))
    boxes = [box_brush("c%d" % i, (2000.0 + 300.0 * i, 64, reach + 150.0),
                       (1200.0 + 300.0 * i, 64, 100.0)) for i in range(4)]
    table = RenderTable()
    table.begin_frame(boxes, 1)
    bounds = table.bounds[:4]
    assert thread._aabb_in_frustum_bounds(planes, bounds).any(), (
        "setup: no box triggers the plane test's corner case")
    _assert_same(table, planes, "corner")


def test_camera_transitions_agree_frame_by_frame():
    """First person to overhead and back, the way a camera toggle eases."""
    rng = random.Random(11)
    table = RenderTable()
    table.begin_frame(_world(rng), 1)
    start = np.array([0.0, 64.0, 0.0])
    end = np.array([0.0, 2400.0, 900.0])
    for step in range(61):
        t = 0.5 - 0.5 * math.cos(math.pi * (step % 31) / 30.0)
        eye = start + (end - start) * t
        pitch = -1.4 * t
        look = (math.cos(pitch) * 0.6, math.sin(pitch), -math.cos(pitch))
        _assert_same(table, _planes(tuple(eye), look), "step %d" % step)


# ---------------------------------------------------------------------------
# Rows that change
# ---------------------------------------------------------------------------

def _sweep(table, rng, what, cameras=12):
    for eye, look in _cameras(rng, cameras):
        _assert_same(table, _planes(eye, look), what)


def test_movers_agree_while_they_move_and_never_rebuild_the_index():
    rng = random.Random(3)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "first frame")
    builds = table.cells.builds
    movers = [b for b in brushes if b.get("is_mover")]
    assert movers
    for frame in range(30):
        for mover in movers:          # movers write pos in place, per tick
            mover["pos"] = [mover["pos"][0] + 700.0, mover["pos"][1],
                            mover["pos"][2] - 350.0]
        table.begin_frame(brushes, 1)
        _sweep(table, rng, "frame %d" % frame, cameras=3)
    assert table.cells.builds == builds, "moving the movers rebuilt the index"
    assert not len(table.cells.loose), "movers are not tracked by the index"


def test_a_journalled_move_agrees_without_a_rebuild():
    rng = random.Random(4)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "before")
    builds = table.cells.builds
    statics = [b for b in brushes if not b.get("is_mover")]
    for brush in rng.sample(statics, 20):
        # A runtime move: a Big World shift, a plugin, a console command.
        brush["pos"] = [brush["pos"][0] + 5000.0, brush["pos"][1],
                        brush["pos"][2] + 5000.0]
        moved(brush)
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "after")
    assert table.cells.builds == builds
    assert len(table.cells.loose) == 20


def test_a_state_change_that_also_moves_a_row_agrees():
    """An I/O or console change re-resolves the whole row, bounds included."""
    from engine.change_journal import touch
    rng = random.Random(13)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "before", cameras=1)
    statics = [b for b in brushes if not b.get("is_mover")]
    for brush in rng.sample(statics, 15):
        brush["pos"] = [-brush["pos"][0], brush["pos"][1], -brush["pos"][2]]
        brush["size"] = [brush["size"][0] * 3.0, brush["size"][1],
                         brush["size"][2] * 3.0]
        touch(brush)
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "after")
    assert len(table.cells.loose) == 15


def test_a_touched_row_that_did_not_move_stays_filed():
    rng = random.Random(5)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "before", cameras=1)
    for brush in brushes[:50]:
        moved(brush)                  # told it moved; it did not
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "after", cameras=1)
    assert not len(table.cells.loose)


def test_an_editor_drag_agrees_every_frame():
    rng = random.Random(6)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    selection = [b for b in brushes if not b.get("is_mover")][:3]
    for frame in range(20):
        for brush in selection:       # a tool writes the dicts in place
            brush["pos"] = [brush["pos"][0] + 400.0, brush["pos"][1],
                            brush["pos"][2]]
            brush["size"] = [brush["size"][0] + 50.0, brush["size"][1],
                             brush["size"][2]]
        table.begin_frame(brushes, 1, edited=selection)
        _sweep(table, rng, "drag frame %d" % frame, cameras=2)


def test_hide_and_show_agree():
    rng = random.Random(7)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "before hiding", cameras=1)
    builds = table.cells.builds
    for brush in rng.sample(brushes, 200):
        set_authored_flag(brush, "hidden", not brush.get("hidden", False))
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "after hiding")
    assert table.cells.builds == builds, "hide/show rebuilt the index"


def test_a_mover_that_stops_being_one_is_culled_where_it_ended_up():
    brush = box_brush("lift", (0, 0, 0), (64, 64, 64), is_mover=True)
    table = RenderTable()
    table.begin_frame([brush], 1)
    _assert_same(table, _planes((0, 0, 300), (0, 0, -1)), "at home")
    brush["pos"] = [8000.0, 0.0, 0.0]
    table.begin_frame([brush], 1)
    brush["is_mover"] = False         # a runtime reclassification
    from engine.change_journal import touch
    touch(brush)
    table.begin_frame([brush], 1)
    visible = _assert_same(table, _planes((8000, 0, 300), (0, 0, -1)), "moved away")
    assert visible.tolist() == [0]


def test_many_moves_rebuild_once_past_the_loose_limit(monkeypatch):
    monkeypatch.setattr(render_cells, "REBUILD_LOOSE", 10)
    rng = random.Random(8)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "before", cameras=1)
    builds = table.cells.builds
    for brush in [b for b in brushes if not b.get("is_mover")][:40]:
        brush["pos"] = [brush["pos"][0] + 3000.0, brush["pos"][1], brush["pos"][2]]
        moved(brush)
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "after")
    assert table.cells.builds == builds + 1
    assert not len(table.cells.loose)


def test_row_set_changes_rebuild_and_agree():
    rng = random.Random(9)
    brushes = _world(rng)
    table = RenderTable()
    epoch = 1
    table.begin_frame(brushes, epoch)
    for round_ in range(6):
        builds = table.cells.builds
        del brushes[rng.randrange(len(brushes))]
        brushes.insert(rng.randrange(len(brushes)),
                       box_brush("new%d" % round_, (rng.uniform(-9000, 9000), 0,
                                                    rng.uniform(-9000, 9000))))
        epoch += 1
        table.begin_frame(brushes, epoch)
        _sweep(table, rng, "round %d" % round_, cameras=4)
        assert table.cells.builds == builds + 1


def test_the_peer_buffer_shares_the_index_without_rebuilding():
    rng = random.Random(10)
    brushes = _world(rng)
    first, second = RenderTable(), RenderTable()
    first.begin_frame(brushes, 1)
    _sweep(first, rng, "first", cameras=1)
    second.begin_frame(brushes, 1, dirty_objects=None, peer=first)
    assert second.cells.members is first.cells.members
    builds = second.cells.builds
    _sweep(second, rng, "second")
    assert second.cells.builds == builds


def test_still_frames_do_no_index_work():
    rng = random.Random(12)
    brushes = _world(rng)
    table = RenderTable()
    table.begin_frame(brushes, 1)
    _sweep(table, rng, "first", cameras=1)
    builds = table.cells.builds
    for _ in range(20):
        table.begin_frame(brushes, 1)
        _sweep(table, rng, "still", cameras=1)
    assert table.cells.builds == builds
    assert not table.cells._touched
