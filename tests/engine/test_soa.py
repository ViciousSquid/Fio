"""Tests for the shared dense SoA capacity-growth primitive."""

import numpy as np

from engine.soa import grow_soa_arrays


def test_grow_soa_arrays_preserves_prefix_shape_and_dtype():
    first = np.arange(6, dtype=np.float32).reshape(2, 3)
    second = np.array([11, 13], dtype=np.int64)

    capacity, (grown_first, grown_second) = grow_soa_arrays(
        5, 2, first, second
    )

    assert capacity == 5
    assert grown_first.shape == (5, 3)
    assert grown_second.shape == (5,)
    assert grown_first.dtype == np.float32
    assert grown_second.dtype == np.int64
    np.testing.assert_array_equal(grown_first[:2], first)
    np.testing.assert_array_equal(grown_second[:2], second)


def test_grow_soa_arrays_is_a_noop_when_capacity_is_sufficient():
    first = np.empty((4, 3), dtype=np.float32)
    second = np.empty(4, dtype=np.float32)

    capacity, arrays = grow_soa_arrays(3, 4, first, second)

    assert capacity == 4
    assert arrays[0] is first
    assert arrays[1] is second
