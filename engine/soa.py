"""Shared capacity-growth helpers for dense structure-of-arrays stores."""

from __future__ import annotations

import numpy as np


def grow_soa_arrays(required, capacity, *arrays):
    """Grow several SoA columns together without repeating data into the tail.

    Every array uses its first axis as the capacity axis.  The existing prefix
    is copied once into freshly allocated storage; newly exposed rows are left
    uninitialised and are only valid after the caller writes them.
    """
    required = int(required)
    capacity = int(capacity)
    if required <= capacity:
        return capacity, arrays

    new_capacity = max(required, max(1, capacity) * 2)
    grown = []
    for array in arrays:
        shape = (new_capacity,) + array.shape[1:]
        target = np.empty(shape, dtype=array.dtype)
        if array.shape[0]:
            target[:array.shape[0]] = array
        grown.append(target)
    return new_capacity, tuple(grown)
