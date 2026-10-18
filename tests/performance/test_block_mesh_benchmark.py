"""Opt-in exact-parity block mesh time/peak-allocation comparison.

python -m pytest tests/performance/test_block_mesh_benchmark.py \
    --run-benchmarks -s -q

Original/current builders alternate order, without a wall-clock pass budget.
"""

import gc
import time
import tracemalloc

import numpy as np
import pytest

from engine.terrain_style import build_block_mesh
from tests.helpers.block_mesh_reference import reference_build_block_mesh

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


@pytest.mark.parametrize('pattern', ['flat', 'steps', 'checker'])
def test_report_block_mesh_cost(pattern):
    def heights(x, z):
        if pattern == 'flat':
            return np.full_like(x, 32.0)
        if pattern == 'steps':
            return (np.floor(x / 16) % 7) * 8 + (np.floor(z / 16) % 3) * 4
        return ((np.floor(x / 16) + np.floor(z / 16)) % 2) * 32

    def colors(h):
        return np.column_stack((h / 64, 1 - h / 64, h / 128)).astype(np.float32)

    print('\npattern cells faces original_us current_us old/new original_peak current_peak')
    for count in (4, 16, 64, 128):
        kwargs = dict(world_x=-7.125, world_z=11.375, size=count * 16,
                      cell=16, origin_x=1.375, origin_z=-5.125,
                      heights_fn=heights, colors_fn=colors,
                      bounds=((-1e12, 1e12, -1e12, 1e12) if pattern == 'flat' else
                              (-7.125, count * 16 - 7.125, 11.375, count * 16 + 11.375)),
                      floor_height=-16, tiling=20)
        fns = [lambda: reference_build_block_mesh(**kwargs),
               lambda: build_block_mesh(**kwargs)]
        old, new = [fn() for fn in fns]
        assert old[0].tobytes() == new[0].tobytes()
        assert old[1:] == new[1:]
        faces = len(old[0]) // 6
        del old, new
        repeats = 30 if count <= 16 else 3
        samples = [[], []]
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            for batch in range(7):
                for i in ([0, 1] if batch % 2 else [1, 0]):
                    start = time.perf_counter_ns()
                    for _ in range(repeats):
                        fns[i]()
                    samples[i].append((time.perf_counter_ns() - start) / repeats / 1000)
        finally:
            if was_enabled:
                gc.enable()
        peak_bytes = []
        for fn in fns:
            gc.collect()
            tracemalloc.start()
            fn()
            peak_bytes.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
        before, after = [min(v) for v in samples]
        print('%s %d %d %.2f %.2f %.2fx %d %d' %
              (pattern, count, faces, before, after, before / after, *peak_bytes), flush=True)
