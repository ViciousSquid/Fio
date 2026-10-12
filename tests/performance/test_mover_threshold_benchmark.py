"""Where a mover or door group should switch from its rows to the plan, measured.

``_Group.WALK_ROWS_BELOW`` (per group: ``LinearMovers``, ``Doors``) decides
whether a tick plans the group as one vectorised pass or walks its rows one by
one through ``_one`` -- the loop's own arithmetic, which the plan reproduces
bit for bit (``tests/logic/test_mover_table_equivalence.py``). Like
``LogicCollision.DENSE_WALK_MIN_BRUSHES`` it is a number about this machine's
NumPy dispatch cost against its interpreter, so it is checked in as a
measurement with a way to re-measure it.

This prints rows against plan for one group tick at rising row counts, every
row moving (movers sliding, doors cycling). Nothing is asserted about time;
the two must leave identical positions, which is asserted.

    python -m pytest tests/performance/test_mover_threshold_benchmark.py \\
        -s --run-benchmarks
"""

import gc
import time

import pytest

pytest.importorskip("PyQt5", reason="drives the real logic thread")

from editor.editor_state import EditorState               # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.mover_table import Doors, LinearMovers        # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.benchmark, pytest.mark.slow]

TICKS = 200
REPEATS = 5
COUNTS = (1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 128)
DELTA = 1.0 / 60.0


def _brushes(kind, count):
    out = []
    for k in range(count):
        brush = {"id": "m%d" % k, "name": "m%d" % k,
                 "pos": [k * 150.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0]}
        if kind == "movers":
            brush.update(is_mover=True, start_on=True, speed=64.0 + k,
                         distance=128.0, direction=[0, 1, 0], rotate=k % 3 == 0)
        else:
            brush.update(is_door=True, door_speed=128.0 + k, door_distance=96.0,
                         door_direction="up", open_time=0.25)
        out.append(brush)
    return out


def _logic(kind, count):
    state = EditorState()
    state.brushes = _brushes(kind, count)
    logic = LogicThread(ThreadedGameState(), state)
    player = Player(1.0, 3.0)
    logic.player_runtime.player = player
    logic.session_runtime.play_mode = True
    logic.world_runtime.build_entity_caches()
    logic.io_manager = None           # positions only: no I/O sequence points
    movers = logic.mover_runtime
    movers._init_movers()
    movers._init_doors()
    return logic, state


def _tick(logic, kind):
    movers = logic.mover_runtime
    if kind == "movers":
        movers._update_movers(DELTA)
    else:
        table = movers._mover_table
        for state in movers.door_states.values():
            if state["state"] == "closed":
                state["state"] = "opening"
        table.sync()
        movers._update_doors(DELTA)


def _run(kind, count, walk_rows):
    group = LinearMovers if kind == "movers" else Doors
    saved = group.WALK_ROWS_BELOW
    group.WALK_ROWS_BELOW = (1 << 30) if walk_rows else 0
    try:
        logic, state = _logic(kind, count)
        try:
            for _ in range(TICKS):
                _tick(logic, kind)
            positions = [list(b["pos"]) for b in state.brushes]

            def ticks():
                for _ in range(TICKS):
                    _tick(logic, kind)
            gc.collect()
            gc.disable()
            try:
                best = None
                for _ in range(REPEATS):
                    started = time.perf_counter()
                    ticks()
                    elapsed = time.perf_counter() - started
                    best = elapsed if best is None else min(best, elapsed)
            finally:
                gc.enable()
        finally:
            logic.stop()
    finally:
        group.WALK_ROWS_BELOW = saved
    return positions, best / TICKS * 1e6


@pytest.mark.parametrize("kind", ["movers", "doors"])
def test_report_the_group_crossover(kind):
    group = LinearMovers if kind == "movers" else Doors
    print("\n  one %s tick, best of %d x %d ticks\n" % (kind, REPEATS, TICKS))
    print("  %-6s %10s %10s   %s" % ("rows", "rows us", "plan us", "faster"))
    crossed_at = None
    for count in COUNTS:
        by_rows, rows_us = _run(kind, count, True)
        by_plan, plan_us = _run(kind, count, False)
        assert by_rows == by_plan, (kind, count)
        if crossed_at is None and plan_us < rows_us:
            crossed_at = count
        print("  %-6d %10.1f %10.1f   %s"
              % (count, rows_us, plan_us,
                 "plan" if plan_us < rows_us else "rows by %.2fx" % (plan_us / rows_us)))
    print("\n  the plan first wins at ~%s rows; the constant is %d"
          % (crossed_at, group.WALK_ROWS_BELOW))
