"""The brush frustum cull on a 24 000-brush world, per camera pose.

Prints how many rows are visible and what the cull costs end to end: the
shown mask, the frustum test over every row, and the slots that pass, exactly
as ``LogicRender`` runs it. Reported, not asserted.

For the record, a grid-assisted broad phase (occupied 512-unit cells ->
slots, cell boxes through the same test) was built and measured against this.
It returned identical slots. Against the box-major test (0.85-0.97 ms here)
it paid: 0.21-0.74 ms. Against the plane-major test (0.24-0.30 ms) it cost
more in wide views (0.35-0.48 ms) and saved at most 0.15 ms in narrow ones.
Gathering a candidate row costs about as much as testing it, so the grid
cannot beat a full pass by much. It was removed; see the history of this
file.

    python -m pytest tests/performance/test_brush_cull_benchmark.py \
        -s --run-benchmarks
"""

import time
import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from engine.editor_state import EditorState
from engine.logic_thread import LogicThread                    # noqa: E402
from engine.threaded_game_state import ThreadedGameState        # noqa: E402
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


def test_report_the_brush_cull_cost():
    table = RenderTable()
    table.begin_frame(_rooms(), 1)
    keep, _ = table.shown()
    logic = LogicThread(ThreadedGameState(), EditorState())
    render = logic.render_runtime
    projection = glm.perspective(glm.radians(75.0), 16.0 / 9.0, 1.0, 10000.0)

    try:
        print("\n  %d rows, median of %d\n" % (table.count, REPEATS))
        print("  %-24s %8s %9s" % ("pose", "visible", "cull ms"))
        for name, (eye, look) in POSES.items():
            eye = glm.vec3(*eye)
            planes = render.extract_frustum_planes(projection * glm.lookAt(
                eye, eye + glm.normalize(glm.vec3(*look)), glm.vec3(0, 1, 0)))

            def cull():
                return np.flatnonzero(keep & render.aabb_in_frustum_bounds(
                    planes, table.bounds[:table.count]))

            print("  %-24s %8d %9.3f" % (name, len(cull()), _best_ms(cull)))
    finally:
        logic.stop()
