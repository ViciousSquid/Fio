"""Where the level-changer facing query should switch from a loop to NumPy.

``LEVELCHANGER_VECTOR_FROM`` (engine/logic_interaction.py) is a small-N
crossover (tests/performance/test_small_n_invariant.py): a map has one to
three level changers, and NumPy's fixed cost a call made the per-tick query
cost far more than the few rows it reads. The agreement test holds the loop
and the NumPy path to the same row on random scenes; the benchmark prints
both at rising row counts:

    python -m pytest tests/performance/test_levelchanger_crossover_benchmark.py \\
        -s --run-benchmarks
"""

import math
import random
import time

import numpy as np
import pytest

from engine import logic_interaction as li


def _scene(rng, n):
    centres = np.asarray([[rng.uniform(-400, 400), rng.uniform(-50, 50), rng.uniform(-400, 400)]
                          for _ in range(n)], dtype=np.float32)
    radii = np.asarray([rng.uniform(16, 300) for _ in range(n)], dtype=np.float32)
    eligible = np.asarray([rng.random() > 0.2 for _ in range(n)], dtype=bool)
    pos = (rng.uniform(-300, 300), rng.uniform(-20, 20), rng.uniform(-300, 300))
    return centres, radii, eligible, pos, rng.uniform(-math.pi, math.pi)


def test_the_loop_and_numpy_find_the_same_level_changer(monkeypatch):
    rng = random.Random(7)
    found = 0
    for _ in range(3000):
        scene = _scene(rng, rng.randint(1, 40))
        monkeypatch.setattr(li, 'LEVELCHANGER_VECTOR_FROM', 10 ** 9)
        loop = li.facing_levelchanger_row(*scene)
        monkeypatch.setattr(li, 'LEVELCHANGER_VECTOR_FROM', 1)
        dense = li.facing_levelchanger_row(*scene)
        assert loop == dense, scene
        found += loop >= 0
    assert found > 300, "the scenes hardly ever had a changer in reach"


def test_no_level_changers_is_none(monkeypatch):
    empty = (np.empty((0, 3), np.float32), np.empty(0, np.float32), np.empty(0, bool))
    assert li.facing_levelchanger_row(*empty, (0.0, 0.0, 0.0), 0.0) == -1


@pytest.mark.benchmark
def test_report_levelchanger_crossover(monkeypatch):
    rng = random.Random(3)
    print("\n  rows    loop      NumPy   (us a query)")
    for n in (1, 2, 4, 8, 16, 20, 24, 32, 64):
        scenes = [_scene(rng, n) for _ in range(200)]
        row = []
        for vector_from in (10 ** 9, 1):
            monkeypatch.setattr(li, 'LEVELCHANGER_VECTOR_FROM', vector_from)
            best = float('inf')
            for _ in range(5):
                started = time.perf_counter()
                for scene in scenes:
                    li.facing_levelchanger_row(*scene)
                best = min(best, (time.perf_counter() - started) / len(scenes) * 1e6)
            row.append(best)
        print("  %4d  %7.2f   %7.2f" % (n, row[0], row[1]))
