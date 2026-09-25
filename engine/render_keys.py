"""Numerical render keys, and the state runs they sort into.

Quake 3's backend never asks a surface what it is.  The frontend packs a
surface's state -- its shader, entity, fog and dynamic-light bits -- into the
integer ``drawSurf_t.sort``, sorts that array, and the backend walks it
changing GL state only where the decoded key changes.  Classification happens
once, numerically, at the front; submission is a walk over sorted integers.

This module is that boundary for Fio, and nothing more.  It is deliberately
small and deliberately GL-free:

* :class:`KeyLayout` declares what a pass's key is made of and packs the dense
  columns describing it into one sortable ``int64`` per item;
* :func:`sort_into_runs` sorts those keys and reports where each stretch of
  equal keys begins and ends.

A *run* is then the unit the renderer submits: everything in it shares the GPU
state the key encodes, so that state is established once, and everything that
differs within it -- the transform, the colour, the UV mapping -- travels as
instance data.  Which fields belong in the key is exactly the question "what
cannot vary within one draw", and it is answered per pass rather than here.

Nothing in this module knows about brushes, OpenGL or the renderer, so the key
design of a pass is unit-testable on its own.
"""

from __future__ import annotations

import numpy as np

#: Keys are signed ``int64`` and are sorted as such, so the packed width has to
#: stay under the sign bit -- a key that overflowed into it would sort before
#: everything else rather than raising.
MAX_KEY_BITS = 63


class KeyLayout:
    """The bit layout of one pass's render key.

    Fields are given most-significant first, because that is the order they
    sort in: the first field is the coarsest grouping.  For the textured brush
    pass that is the texture, since binding one is the expensive state change,
    and then the cube face, because six consecutive vertices are addressed by a
    per-draw parameter rather than a per-instance one.

    >>> layout = KeyLayout([('texture', 32), ('face', 3)])
    >>> keys = layout.pack(texture=np.array([7, 7, 2]), face=np.array([0, 1, 5]))
    >>> layout.field(keys, 'face').tolist()
    [0, 1, 5]
    """

    __slots__ = ('names', 'bits', 'shifts', 'width')

    def __init__(self, fields):
        names, bits = [], []
        for name, size in fields:
            if size <= 0:
                raise ValueError('field %r needs a positive width' % (name,))
            names.append(name)
            bits.append(int(size))
        total = sum(bits)
        if total > MAX_KEY_BITS:
            raise ValueError(
                'key layout needs %d bits; %d is the most that fits an int64 '
                'without reaching the sign bit' % (total, MAX_KEY_BITS))
        self.names = tuple(names)
        self.bits = tuple(bits)
        # Most significant first: the last field occupies the low bits.
        shifts, offset = [], total
        for size in bits:
            offset -= size
            shifts.append(offset)
        self.shifts = tuple(shifts)
        self.width = total

    def pack(self, **columns):
        """One ``int64`` key per item, from a dense column per field.

        Every field must be supplied and every value must fit its width -- a
        texture id wider than its field would silently collide with the field
        above it, which is a class of bug that only shows up as two surfaces
        sharing a run they should not.
        """
        missing = set(self.names) - set(columns)
        if missing:
            raise ValueError('missing key fields: %s' % ', '.join(sorted(missing)))
        extra = set(columns) - set(self.names)
        if extra:
            raise ValueError('unknown key fields: %s' % ', '.join(sorted(extra)))

        keys = None
        for name, size, shift in zip(self.names, self.bits, self.shifts):
            column = np.asarray(columns[name], dtype=np.int64)
            if column.size and (column.min() < 0 or column.max() >= (1 << size)):
                raise ValueError(
                    "key field %r does not fit %d bits (values %d..%d)"
                    % (name, size, int(column.min()), int(column.max())))
            shifted = column << shift
            keys = shifted if keys is None else keys | shifted
        if keys is None:
            return np.empty(0, dtype=np.int64)
        return keys

    def field(self, keys, name):
        """Read one field back out of packed keys."""
        index = self.names.index(name)
        shift = self.shifts[index]
        mask = (1 << self.bits[index]) - 1
        return (np.asarray(keys, dtype=np.int64) >> shift) & mask


def sort_into_runs(keys, secondary=None):
    """Sort a primary key and report the boundaries of each equal-key stretch.

    With no secondary key this is a stable argsort. With one, NumPy's stable
    lexicographic sort orders by the primary key first and the secondary key
    within it. That lets the renderer establish both depth order and texture
    grouping in one numeric sort rather than sorting the same slots twice.

    Run boundaries are still defined only by the primary key: the secondary
    key is a tie-breaker, never part of the state that requires a separate draw.
    """
    keys = np.asarray(keys)
    count = len(keys)
    if not count:
        return (np.empty(0, dtype=np.intp), np.zeros(1, dtype=np.int32))

    if secondary is None:
        order = np.argsort(keys, kind='stable')
    else:
        secondary = np.asarray(secondary)
        if len(secondary) != count:
            raise ValueError("secondary key must have the same length as keys")
        order = np.lexsort((secondary, keys))

    sorted_keys = keys[order]
    boundaries = np.flatnonzero(
        sorted_keys[1:] != sorted_keys[:-1]) + 1
    starts = np.empty(len(boundaries) + 2, dtype=np.int32)
    starts[0] = 0
    starts[1:-1] = boundaries
    starts[-1] = count
    return order, starts
