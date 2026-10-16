"""Opt-in terrain CPU allocation benchmark; reports speed without budgets.

python -m pytest tests/performance/test_terrain_kernel_benchmark.py \
    --run-benchmarks -s -q

Original and current kernels run interleaved over identical inputs. The best
of seven batches of 50 calls reduces scheduler contamination. All outputs
must agree exactly before timing, including the float32 palette rounding.
"""

import gc
import time
from types import SimpleNamespace

import numpy as np
import pytest

from engine.terrain import BIOMES, PerlinNoise, Terrain
from tests.helpers.terrain_reference import reference_colors, reference_noise_batch

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
def test_report_terrain_cpu_kernels(dtype):
    rng = np.random.default_rng(174)
    noise = PerlinNoise(73)
    terrain = SimpleNamespace(biome=BIOMES['desert_canyon'])
    print('\noperation dtype count original_us current_us old/new')
    for count in (64, 1024, 16384):
        x, y = rng.uniform(-512, 512, (2, count)).astype(dtype)
        h = rng.uniform(-.5, 1.5, count).astype(dtype)
        for name, old, new in (
            ('noise', lambda: reference_noise_batch(noise, x, y),
             lambda: noise.noise2d_batch(x, y)),
            ('palette', lambda: reference_colors(terrain.biome, h, h),
             lambda: Terrain._get_colors_batch(terrain, h, h)),
        ):
            np.testing.assert_array_equal(new(), old())
            samples = [[], []]
            gc.collect()
            was_enabled = gc.isenabled()
            gc.disable()
            try:
                for repeat in range(7):
                    for i in (range(2) if repeat % 2 else reversed(range(2))):
                        fn = (old, new)[i]
                        started = time.perf_counter_ns()
                        for _ in range(50):
                            fn()
                        samples[i].append((time.perf_counter_ns() - started) / 50_000)
            finally:
                if was_enabled:
                    gc.enable()
            before, after = [min(s) for s in samples]
            print('%s %s %d %.2f %.2f %.2fx' %
                  (name, dtype.__name__, count, before, after, before / after), flush=True)
