"""Where the whole-cache walks should switch to dense rows, measured.

``LogicCollision.DENSE_WALK_MIN_BRUSHES`` decides whether the hitscan's brush
pass (and the small-swarm projectile wall test) walks the collision cache in
Python or queries :class:`engine.logic_collision.CollisionRows`. Like
``SpatialGrid.LOS_DENSE_MIN_CANDIDATES`` it is a number about this machine's
NumPy dispatch cost against its interpreter, so it is checked in as a
measurement with a way to re-measure it.

This prints scalar against dense for one pellet's brush pass at rising brush
counts, rows fetched as a shot fetches them. Nothing is asserted about time;
the two passes must agree, which is asserted, because a disagreement is a
correctness bug, not a benchmark result.

    python -m pytest tests/performance/test_hitscan_threshold_benchmark.py \\
        -s --run-benchmarks
"""

import gc
import time

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="drives the real logic thread")

import glm                                                # noqa: E402

from editor.editor_state import EditorState               # noqa: E402
from engine.logic_collision import LogicCollision         # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush                # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.benchmark, pytest.mark.slow]

RAYS = 200
REPEATS = 7
COUNTS = (4, 8, 16, 24, 32, 48, 64, 96, 128, 256, 1024, 3600)


def _brushes(count, rng):
    return [box_brush("b%d" % i, rng.uniform(-2000.0, 2000.0, 3),
                      rng.uniform(16.0, 256.0, 3)) for i in range(count)]


def _best(fn):
    fn()
    best = None
    for _ in range(REPEATS):
        started = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


def test_report_the_brush_pass_crossover():
    rng = np.random.default_rng(11)
    rays = [(glm.vec3(*rng.uniform(-2000.0, 2000.0, 3)),
             glm.normalize(glm.vec3(*rng.normal(size=3)))) for _ in range(RAYS)]
    print("\n  one pellet's brush pass, best of %d over %d rays\n" % (REPEATS, RAYS))
    print("  %-8s %11s %11s   %s" % ("brushes", "scalar us", "dense us", "faster"))
    crossed_at = None
    for count in COUNTS:
        state = EditorState()
        state.brushes = _brushes(count, rng)
        thread = LogicThread(ThreadedGameState(), state)
        try:
            collision = thread.collision_runtime
            combat = thread.combat_runtime
            collision.refresh_collision_brushes_cache()

            def run(threshold):
                collision.DENSE_WALK_MIN_BRUSHES = threshold
                out = []
                for origin, direction in rays:
                    rows = collision.collision_rows()   # once per shot
                    out.append(combat._trace_brushes(rows, origin, direction, 4000.0))
                return out

            scalar = run(1 << 62)
            dense = run(0)
            assert [(d, None if p is None else tuple(p)) for d, p in scalar] == \
                   [(d, None if p is None else tuple(p)) for d, p in dense]

            gc.collect()
            gc.disable()
            try:
                scalar_us = _best(lambda: run(1 << 62)) / RAYS * 1e6
                dense_us = _best(lambda: run(0)) / RAYS * 1e6
            finally:
                gc.enable()
        finally:
            thread.stop()
        if crossed_at is None and dense_us < scalar_us:
            crossed_at = count
        print("  %-8d %11.1f %11.1f   %s"
              % (count, scalar_us, dense_us,
                 "dense" if dense_us < scalar_us
                 else "scalar by %.2fx" % (dense_us / scalar_us)))
    print("\n  dense first wins at ~%s brushes; the constant is %d"
          % (crossed_at, LogicCollision.DENSE_WALK_MIN_BRUSHES))
