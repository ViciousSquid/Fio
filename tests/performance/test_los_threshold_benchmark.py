"""Where line of sight should switch narrow phases, measured.

``SpatialGrid.LOS_DENSE_MIN_CANDIDATES`` is the one number deciding whether a
ray's candidate brushes are walked in Python or tested as dense rows, and it is
a number about *this machine's* NumPy dispatch cost against *this machine's*
interpreter. It is checked in as a measurement, so it needs a way to be
re-measured rather than argued about.

This module prints the table the constant's docstring quotes: scalar against
dense over the same rays, at rising candidate densities, with the crossing
marked. Nothing is asserted -- a threshold that suits one machine is not a
defect on another -- so it lives in the benchmark tier.

    python -m pytest tests/performance/test_los_threshold_benchmark.py \
        -s --run-benchmarks

To try a different value without editing the engine, set it on the class::

    SpatialGrid.LOS_DENSE_MIN_CANDIDATES = 512

Zero means "always dense" and is safe; both paths answer identically, which
``tests/physics/test_line_of_sight.py`` is what establishes.
"""

import gc
import time

import numpy as np
import pytest

pytest.importorskip("glm")

import glm                                                   # noqa: E402

from engine.physics import SpatialGrid                       # noqa: E402
from engine.render_table import RenderTable                  # noqa: E402
from tests.helpers.worlds import box_brush                    # noqa: E402

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

#: Rays timed per density. The real AI tick issues one per awake monster.
RAYS = 240

#: Repeats; the minimum is reported, as the least contaminated sample.
REPEATS = 7

#: (brush count, extent they are spread over) -- rising candidates per ray.
DENSITIES = ((60, 3000), (200, 3000), (600, 3000), (600, 1200), (1200, 1200),
             (1800, 1200), (2400, 1200), (3200, 1200))


def _world(n_brushes, spread):
    brushes = [
        box_brush("b%d" % i,
                  (((i * 53) % spread) - spread / 2.0, 32.0,
                   ((i * 97) % spread) - spread / 2.0),
                  (64.0, 128.0, 64.0))
        for i in range(n_brushes)]
    table = RenderTable()
    table.sync(brushes, 1)
    grid = SpatialGrid()
    grid.populate(brushes)
    return grid, table


def _rays(rng):
    out = []
    for _ in range(RAYS):
        start = glm.vec3(*rng.uniform(-600, 600, 3))
        direction = rng.normal(size=3)
        direction = direction / np.linalg.norm(direction) * 400.0
        end = start + glm.vec3(*direction)
        if glm.length(end - start) >= 0.001:
            out.append((start, end))
    return out


def _candidates_per_ray(grid, table, rays):
    slots_by_cell = grid.cell_slots(table)
    total = 0
    for start, end in rays:
        direction = end - start
        length = glm.length(direction)
        for coord in grid._cells_along_ray(start, direction / length, length):
            found = slots_by_cell.get(coord)
            if found is not None:
                total += len(found)
    return total / float(len(rays))


def _best(fn):
    fn()
    best = None
    for _ in range(REPEATS):
        started = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best * 1000.0


def test_report_the_narrow_phase_crossover():
    rng = np.random.default_rng(3)
    print("\n  %d rays per row, best of %d\n" % (RAYS, REPEATS))
    print("  %-12s %7s %11s %11s   %s"
          % ("cands/ray", "blocked", "scalar ms", "dense ms", "faster"))
    crossed_at = None
    for n_brushes, spread in DENSITIES:
        grid, table = _world(n_brushes, spread)
        rays = _rays(rng)
        per_ray = _candidates_per_ray(grid, table, rays)

        def scalar():
            return [grid.has_line_of_sight(s, e, None, None) for s, e in rays]

        def dense():
            grid.LOS_DENSE_MIN_CANDIDATES = 0
            try:
                return [grid.has_line_of_sight(s, e, None, table)
                        for s, e in rays]
            finally:
                del grid.LOS_DENSE_MIN_CANDIDATES

        answers = scalar()
        blocked = 100.0 * sum(1 for a in answers if not a) / len(answers)
        assert answers == dense(), (
            "the two narrow phases disagreed at %.1f candidates per ray -- "
            "that is a correctness bug, not a benchmark result" % per_ray)

        gc.collect()
        gc.disable()
        try:
            scalar_ms = _best(scalar)
            dense_ms = _best(dense)
        finally:
            gc.enable()

        if crossed_at is None and dense_ms < scalar_ms:
            crossed_at = per_ray
        print("  %-12.1f %6.0f%% %11.3f %11.3f   %s"
              % (per_ray, blocked, scalar_ms, dense_ms,
                 "dense" if dense_ms < scalar_ms
                 else "scalar by %.2fx" % (dense_ms / scalar_ms)))

    print("\n  dense first wins at ~%s candidates/ray; the constant is %d"
          % ("%.0f" % crossed_at if crossed_at else "(never, in this range)",
             SpatialGrid.LOS_DENSE_MIN_CANDIDATES))
    print("  the crossover moves with how often the scalar walk exits early -- "
          "the blocked column is that rate --")
    print("  so treat it as a band rather than a point.")
