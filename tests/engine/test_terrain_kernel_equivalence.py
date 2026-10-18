"""Terrain allocation optimizations retain exact heights and palette colors."""

import numpy as np
import pytest
from types import SimpleNamespace

from engine.terrain import BIOMES, BiomeConfig, PerlinNoise, Terrain
from tests.helpers.terrain_reference import (
    reference_colors, reference_noise_batch, reference_noise_gradient)


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
@pytest.mark.parametrize('count', [0, 1, 16, 1089, 16384])
def test_noise_and_gradient_match_original_bits(dtype, count):
    rng = np.random.default_rng(174)
    noise = PerlinNoise(73)
    x = rng.uniform(-512, 512, count * 2).astype(dtype)[::2]
    y = rng.uniform(-512, 512, count * 2).astype(dtype)[::2]
    hashes = rng.integers(-256, 256, count)
    before_x, before_y = x.copy(), y.copy()
    for actual, expected in (
        (noise._grad(hashes, x, y), reference_noise_gradient(hashes, x, y)),
        (noise.noise2d_batch(x, y), reference_noise_batch(noise, x, y)),
    ):
        assert actual.dtype == expected.dtype
        np.testing.assert_array_equal(actual.view(f'u{dtype().itemsize}'),
                                      expected.view(f'u{dtype().itemsize}'))
    np.testing.assert_array_equal(x, before_x)
    np.testing.assert_array_equal(y, before_y)


def test_gradient_preserves_signed_zero_and_all_four_signs():
    x = np.tile(np.array([-0., 0., 1., -1.], dtype=np.float32), 4)
    y = np.tile(np.array([0., -0., -1., 1.], dtype=np.float32), 4)
    hashes = np.repeat(np.arange(4), 4)
    actual = PerlinNoise()._grad(hashes, x, y)
    expected = reference_noise_gradient(hashes, x, y)
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
@pytest.mark.parametrize('stops', [
    preset.color_gradient for preset in BIOMES.values()
] + [[], [(0.5, (1., .25, 0.))],
     [(0., (0., 0., 0.)), (.5, (1., 0., 0.)), (.5, (0., 1., 0.)), (1., (0., 0., 1.))],
     [(.2, (0., 0., 0.)), (.8, (1., 1., 1.)), (.4, (0., 1., 0.))]])
def test_palette_interpolation_matches_original_bits(dtype, stops):
    terrain = SimpleNamespace(biome=BiomeConfig(name='parity', color_gradient=stops))
    knots = [s[0] for s in stops]
    h = np.array([*np.linspace(-.5, 1.5, 1089), *knots,
                  *[np.nextafter(dtype(k), dtype(-1)) for k in knots],
                  *[np.nextafter(dtype(k), dtype(2)) for k in knots], np.nan], dtype=dtype)
    actual = Terrain._get_colors_batch(terrain, h, h)
    expected = reference_colors(terrain.biome, h, h)
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))
