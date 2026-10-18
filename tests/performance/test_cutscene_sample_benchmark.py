"""Opt-in cutscene sampling benchmark against the frozen CANARY sampler.

python -m pytest tests/performance/test_cutscene_sample_benchmark.py \
    --run-benchmarks -s -q
"""

import gc
import random
import time

import pytest

from engine.cutscene_runtime import CutsceneRuntime
from tests.helpers.cutscene_reference import reference_sample


pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


@pytest.mark.parametrize('count', [1, 2, 8, 16, 64, 256])
@pytest.mark.parametrize('distribution', ['ordered', 'reversed', 'shuffled', 'tied'])
@pytest.mark.parametrize('phase', ['before', 'middle', 'after'])
def test_cutscene_sampling_crossover(count, distribution, phase):
    rng = random.Random(157)
    rows = [
        {'time': index // 3 if distribution == 'tied' else index,
         'pos': [index * 7, index * -3, index * 0.5],
         'yaw': index * 0.01, 'fov': 85.0 + index % 15,
         'teleport': index % 7 == 4}
        for index in range(count)
    ]
    end = rows[-1]['time']
    if distribution == 'reversed':
        rows.reverse()
    elif distribution in ('shuffled', 'tied'):
        rng.shuffle(rows)
    elapsed = {'before': -0.5, 'middle': end * 0.5 + 0.25,
               'after': end + 1.0}[phase]
    current_sample = CutsceneRuntime._cutscene_sample
    assert current_sample(rows, elapsed) == reference_sample(rows, elapsed)
    calls = max(48, min(1500, 12000 // count))
    samples = [[], []]
    functions = (reference_sample, current_sample)
    enabled = gc.isenabled()
    gc.disable()
    try:
        for repeat in range(7):
            for variant in ((0, 1) if repeat % 2 else (1, 0)):
                function = functions[variant]
                start = time.perf_counter_ns()
                for _ in range(calls):
                    function(rows, elapsed)
                samples[variant].append((time.perf_counter_ns() - start) / calls / 1000)
    finally:
        if enabled:
            gc.enable()
    before, after = (min(values) for values in samples)
    print(f'\nCUTSCENE_SAMPLE {count} {distribution} {phase} '
          f'{before:.3f} {after:.3f} {before / after:.3f}x', flush=True)
