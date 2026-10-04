"""The batched sculpt sampler must agree with the scalar one.

Every streamed terrain chunk samples the sculpt offsets twice. That used to be
a Python loop of four dictionary lookups per vertex; the real Terrain path now
uses a dense grid built once per sculpt change. These tests run that path on a
live Terrain instance in an actual core-profile GL context.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("glm")
pytest.importorskip("OpenGL")

from engine.terrain import Terrain  # noqa: E402
from tests.helpers.gl import GLTestContext  # noqa: E402

pytestmark = pytest.mark.gl


@pytest.fixture
def terrain():
    with GLTestContext(64, 64):
        value = Terrain()
        try:
            yield value
        finally:
            value.cleanup()


def sculpted_terrain(terrain, seed=0):
    terrain.sculpt_grid_resolution = 4.0
    rng = np.random.default_rng(seed)
    terrain.sculpt_offsets = {
        (int(gx), int(gz)): float(rng.uniform(-40, 40))
        for gx, gz in rng.integers(-60, 60, size=(3000, 2))
    }
    return terrain


def scalar(t, xs, zs):
    return np.array(
        [t._sample_sculpt_scalar(float(x), float(z)) for x, z in zip(xs, zs)],
        dtype=np.float32,
    )


@pytest.mark.parametrize(
    "lo,hi",
    [(-260.0, 260.0), (-900.0, 900.0), (5000.0, 6000.0)],
)
def test_batch_matches_the_scalar_sampler(terrain, lo, hi):
    t = sculpted_terrain(terrain)
    rng = np.random.default_rng(1)
    xs = rng.uniform(lo, hi, 3000)
    zs = rng.uniform(lo, hi, 3000)
    np.testing.assert_allclose(
        t._sample_sculpt_batch(xs, zs),
        scalar(t, xs, zs),
        atol=1e-4,
    )


def test_a_sculpt_edit_is_seen_by_the_next_sample(terrain):
    t = sculpted_terrain(terrain)
    xs = np.array([10.0, 11.0])
    zs = np.array([10.0, 13.0])
    t._sample_sculpt_batch(xs, zs)
    t.apply_sculpt_at(10.0, 10.0, 12.0, 25.0)
    np.testing.assert_allclose(
        t._sample_sculpt_batch(xs, zs),
        scalar(t, xs, zs),
        atol=1e-4,
    )
    t.clear_sculpt()
    assert not t._sample_sculpt_batch(xs, zs).any()


def test_a_sculpt_too_wide_for_a_dense_grid_still_samples_correctly(terrain):
    t = sculpted_terrain(terrain)
    # Instance configuration: this exercises the real branch without replacing
    # the production Terrain class or its methods.
    t.MAX_DENSE_SCULPT_CELLS = 10
    rng = np.random.default_rng(2)
    xs = rng.uniform(-260, 260, 500)
    zs = rng.uniform(-260, 260, 500)
    np.testing.assert_allclose(
        t._sample_sculpt_batch(xs, zs),
        scalar(t, xs, zs),
        atol=1e-4,
    )
