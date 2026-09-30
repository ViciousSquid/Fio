"""The batched sculpt sampler must agree with the scalar one.

Every streamed terrain chunk samples the sculpt offsets twice. That used to be
a Python loop of four dictionary lookups per vertex (~6 ms a call, on every
chunk anywhere in the world), and was the hitch felt walking into new terrain
on a Big World map. It is now a lookup into a dense grid built once per sculpt
change; :meth:`Terrain._sample_sculpt_scalar` is the reference it must match.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("glm")
pytest.importorskip("OpenGL")

from engine.terrain import Terrain  # noqa: E402


def sculpted_terrain(seed=0):
    t = object.__new__(Terrain)
    t.sculpt_grid_resolution = 4.0
    t.chunks = {}
    rng = np.random.default_rng(seed)
    t.sculpt_offsets = {
        (int(gx), int(gz)): float(rng.uniform(-40, 40))
        for gx, gz in rng.integers(-60, 60, size=(3000, 2))
    }
    return t


def scalar(t, xs, zs):
    return np.array([t._sample_sculpt_scalar(float(x), float(z))
                     for x, z in zip(xs, zs)], dtype=np.float32)


@pytest.mark.parametrize("lo,hi", [(-260.0, 260.0), (-900.0, 900.0),
                                   (5000.0, 6000.0)])
def test_batch_matches_the_scalar_sampler(lo, hi):
    t = sculpted_terrain()
    rng = np.random.default_rng(1)
    xs = rng.uniform(lo, hi, 3000)
    zs = rng.uniform(lo, hi, 3000)
    np.testing.assert_allclose(t._sample_sculpt_batch(xs, zs),
                               scalar(t, xs, zs), atol=1e-4)


def test_a_sculpt_edit_is_seen_by_the_next_sample():
    t = sculpted_terrain()
    xs = np.array([10.0, 11.0])
    zs = np.array([10.0, 13.0])
    t._sample_sculpt_batch(xs, zs)          # builds the dense grid
    t.apply_sculpt_at(10.0, 10.0, 12.0, 25.0)
    np.testing.assert_allclose(t._sample_sculpt_batch(xs, zs),
                               scalar(t, xs, zs), atol=1e-4)
    t.clear_sculpt()
    assert not t._sample_sculpt_batch(xs, zs).any()


def test_a_sculpt_too_wide_for_a_dense_grid_still_samples_correctly(monkeypatch):
    t = sculpted_terrain()
    monkeypatch.setattr(Terrain, "MAX_DENSE_SCULPT_CELLS", 10)
    rng = np.random.default_rng(2)
    xs = rng.uniform(-260, 260, 500)
    zs = rng.uniform(-260, 260, 500)
    np.testing.assert_allclose(t._sample_sculpt_batch(xs, zs),
                               scalar(t, xs, zs), atol=1e-4)
