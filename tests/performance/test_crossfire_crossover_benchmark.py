"""Opt-in CANARY crossfire baseline/scalar/NumPy/adaptive crossover.

    python -m pytest tests/performance/test_crossfire_crossover_benchmark.py \
        --run-benchmarks -s -q

Reports best microseconds/query (7 interleaved repeats of 300 shots, warm
live cache), following the existing threshold benchmarks' least-contaminated
sample convention. Interleaving modes reduces clock/load drift between modes.
Gather is outside timing, as in an AI tick; cold measurements include live
cache initialization on every shot. Stale scenarios reset the cached
eligibility before each shot so every query exercises the fallback. Timings
are observations, never pass/fail thresholds. All modes must return the same
object and preserve identical cache evictions before they are timed.
"""

import gc
import time

import numpy as np
import pytest

from engine.monster_table import MonsterTable
from tests.helpers.crossfire import make_table, sorted_crossfire

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]
COUNTS = (1, 4, 8, 16, 24, 32, 48, 64, 128, 256, 1024)


def _scenario(count, distribution):
    rng = np.random.default_rng(42 + count)
    if distribution in ('aligned', 'ties', 'stale'):
        pos = np.zeros((count, 3))
        pos[:, 1] = -64
        pos[:, 2] = 200 if distribution == 'ties' else np.linspace(100, 1900, count)
        rng.shuffle(pos[1:])  # hit order is independent of row order
    else:
        pos = rng.uniform(-2000, 2000, (count, 3))
    pos[0] = (0, -64, -200)
    table = make_table(pos)
    if distribution == 'filtered':
        for row, p in enumerate(table.props):
            p.update(team='red' if row % 4 else 'blue', hidden=row % 3 == 0)
        table.gather(table.monsters)
    # Prime the live cache, then arrange deaths/hides after caching.
    sorted_crossfire(table, 0, (0, 0, 0), (0, 0, 2200))
    initial = table._live.copy()
    # Make the four nearest hits stale, independent of shuffled row order.
    if distribution == 'stale':
        for row in np.argsort(table.pos[1:count, 2])[:4] + 1:
            table.props[row]['dead'] = True
    return table, initial


@pytest.mark.parametrize('cache', ['warm', 'cold'])
def test_report_crossfire_crossover(monkeypatch, cache):
    print('\nCPython/NumPy crossfire, best of 7 x 300 shots, us/query; %s cache' % cache)
    print('cache distribution count   sorted   scalar    numpy adaptive  old/new')
    threshold = MonsterTable.CROSSFIRE_NUMPY_MIN
    for distribution in ('sparse', 'aligned', 'ties', 'filtered', 'stale'):
        if cache == 'cold' and distribution == 'stale':
            continue  # a first-shot cache cannot contain previously live dead targets
        for count in COUNTS:
            if cache == 'cold' and count > 32:
                continue
            table, initial = _scenario(count, distribution)
            python_initial = initial.tolist()
            def run(mode):
                if cache == 'cold':
                    table._live = None
                elif distribution == 'stale':
                    table._live[:] = (python_initial if isinstance(table._live, list)
                                      else initial)
                if mode == 'sorted':
                    return sorted_crossfire(table, 0, (0, 0, 0), (0, 0, 2200))
                return table.crossfire(0, (0, 0, 0), (0, 0, 2200))

            modes = [('sorted', threshold), ('scalar', 1 << 30),
                     ('numpy', 0), ('adaptive', threshold)]
            timings = [[] for _ in modes]
            table._live[:] = initial
            expected = run('sorted')
            expected_live = table._live.copy()
            for mode, limit in modes:
                monkeypatch.setattr(MonsterTable, 'CROSSFIRE_NUMPY_MIN', limit)
                table._live = (python_initial.copy() if mode != 'sorted' and count < limit
                               else initial.copy())
                assert run(mode) is expected
                np.testing.assert_array_equal(table._live, expected_live)
            gc.collect()
            was_enabled = gc.isenabled()
            gc.disable()
            try:
                for repeat in range(7):
                    for index in (range(4) if repeat % 2 else reversed(range(4))):
                        mode, limit = modes[index]
                        monkeypatch.setattr(MonsterTable, 'CROSSFIRE_NUMPY_MIN', limit)
                        table._live = (python_initial.copy()
                                       if mode != 'sorted' and count < limit else initial.copy())
                        run(mode)
                        start = time.perf_counter_ns()
                        for _ in range(300):
                            run(mode)
                        timings[index].append((time.perf_counter_ns() - start) / 300_000)
            finally:
                if was_enabled:
                    gc.enable()
            timings = [min(samples) for samples in timings]
            print('%-4s %-12s %4d %8.2f %8.2f %8.2f %8.2f %8.2fx'
                  % (cache, distribution, count, *timings, timings[0] / timings[3]), flush=True)
