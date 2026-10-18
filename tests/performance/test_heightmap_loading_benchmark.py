"""Opt-in real image loading comparison, with a frozen pre-pass Pillow path."""

import gc
import time
import tracemalloc
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from engine.terrain import Terrain

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


def _reference(terrain, path):
    # Frozen original Pillow branch of Terrain.load_heightmap.
    image = Image.open(path).convert('L')
    terrain.heightmap_data = np.array(image, dtype=np.float32) / 255.0
    terrain.mark_all_dirty()


@pytest.mark.parametrize('side', [1024, 2048])
def test_report_heightmap_loading(tmp_path, side):
    path = tmp_path / 'height.png'
    pixels = np.random.default_rng(24).integers(0, 256, (side, side), dtype=np.uint8)
    Image.fromarray(pixels).save(path)
    terrain = SimpleNamespace(heightmap_data=None, mark_all_dirty=lambda: None)
    expected = pixels.astype(np.float32) / 255.0
    operations = (_reference, Terrain.load_heightmap)
    for operation in operations:
        operation(terrain, str(path))
        np.testing.assert_array_equal(terrain.heightmap_data, expected)
    del expected, pixels
    samples = [[], []]
    for repeat in range(7):
        for index in ((0, 1) if repeat % 2 else (1, 0)):
            terrain.heightmap_data = None
            start = time.perf_counter_ns()
            operations[index](terrain, str(path))
            samples[index].append((time.perf_counter_ns() - start) / 1_000_000)
    peaks = []
    for operation in operations:
        terrain.heightmap_data = None
        gc.collect()
        tracemalloc.start()
        try:
            operation(terrain, str(path))
            peaks.append(tracemalloc.get_traced_memory()[1] / (1024 * 1024))
        finally:
            tracemalloc.stop()
    before, after = (min(values) for values in samples)
    print(f'\nHEIGHTMAP_LOAD {side} {before:.3f} {after:.3f} '
          f'{before / after:.2f}x peak_MiB {peaks[0]:.3f} {peaks[1]:.3f}', flush=True)
