"""The brush frustum cull on a 24 000-brush world, full against grid-assisted.

Prints, per camera pose, how many rows are visible, how many the grid made
candidates, and what each path costs end to end (``_cull_brush_slots``: the
shown mask, the broad phase if any, the narrow phase). Both paths must return
identical slots; the timings are reported, not asserted.

    python -m pytest tests/performance/test_brush_cull_benchmark.py \
        -s --run-benchmarks
"""

import time

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from engine.logic_thread import LogicThread              # noqa: E402
from engine.render_table import RenderTable              # noqa: E402
from tests.helpers.worlds import box_brush               # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.benchmark, pytest.mark.slow]

ROOMS = 28
ROOM = 1024.0
REPEATS = 200
POSES = {
    "first person, level": ((ROOMS * ROOM / 2, 64, ROOMS * ROOM / 2), (1, 0, 0.3)),
    "first person, across": ((-400, 64, -400), (1, 0, 1)),
    "first person, outward": ((0, 64, ROOMS * ROOM / 2), (-1, 0, 0)),
    "overhead, raked": ((ROOMS * ROOM / 2, 800, ROOMS * ROOM / 2), (0, -1.2, -1)),
    "editor, high": ((ROOMS * ROOM / 2, 3000, ROOMS * ROOM / 2 + 3000), (0, -0.6, -1)),
    "straight down": ((ROOMS * ROOM / 2, 800, ROOMS * ROOM / 2), (0.001, -1, 0)),
}


def _rooms():
    """Floors, walls and clutter per room, like ``maps`` at scale."""
    rng = np.random.default_rng(7)
    brushes = []
    for ix in range(ROOMS):
        for iz in range(ROOMS):
            ox, oz = ix * ROOM, iz * ROOM
            brushes.append(box_brush("f%d_%d" % (ix, iz), (ox, -16, oz), (ROOM, 32, ROOM)))
            for side, (dx, dz, sx, sz) in enumerate(
                    ((0, -0.5, ROOM, 32), (0, 0.5, ROOM, 32),
                     (-0.5, 0, 32, ROOM), (0.5, 0, 32, ROOM))):
                brushes.append(box_brush("w%d_%d_%d" % (ix, iz, side),
                                         (ox + dx * ROOM, 128, oz + dz * ROOM),
                                         (sx, 256, sz)))
            for k in range(25):
                s = float(rng.choice([32, 48, 64, 96]))
                brushes.append(box_brush(
                    "c%d_%d_%d" % (ix, iz, k),
                    (ox + rng.uniform(-400, 400), s / 2, oz + rng.uniform(-400, 400)),
                    (s, s, s), is_mover=bool(k == 0 and (ix * 3 + iz) % 6 == 0)))
    return brushes


def _best_ms(fn):
    fn()
    samples = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return float(np.median(samples)) * 1000.0


def test_report_full_against_grid_assisted_culling():
    table = RenderTable()
    table.begin_frame(_rooms(), 1)
    keep, _ = table.shown()
    thread = LogicThread.__new__(LogicThread)
    projection = glm.perspective(glm.radians(75.0), 16.0 / 9.0, 1.0, 10000.0)

    print("\n  %d rows, median of %d\n" % (table.count, REPEATS))
    print("  %-24s %8s %8s %9s %9s %6s"
          % ("pose", "visible", "cands", "full ms", "grid ms", "ratio"))
    for name, (eye, look) in POSES.items():
        eye = glm.vec3(*eye)
        planes = thread._extract_frustum_planes(projection * glm.lookAt(
            eye, eye + glm.normalize(glm.vec3(*look)), glm.vec3(0, 1, 0)))

        def cull(grid):
            thread.brush_cell_culling = grid
            return thread._cull_brush_slots(planes, table, keep)

        full, grid = cull(False), cull(True)
        assert np.array_equal(full, grid), name
        candidates = int(table.cells.candidate_mask(
            lambda boxes: thread._aabb_in_frustum_bounds(planes, boxes),
            table.bounds, table.count, table.dynamic_slots).sum())
        full_ms = _best_ms(lambda: cull(False))
        grid_ms = _best_ms(lambda: cull(True))
        print("  %-24s %8d %8d %9.3f %9.3f %6.2f"
              % (name, len(full), candidates, full_ms, grid_ms, full_ms / grid_ms))
