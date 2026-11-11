"""Projected decals as a dense table (no GL): blood, for now.

A decal is a box in the world. The renderer draws the box and, for every
pixel it covers, takes the surface already drawn there (from the depth
buffer), expresses that point in the box's own space and paints the decal's
texture where the point lies inside -- so one decal wraps over a wall, the
floor under it and the corner between them, whatever the geometry is (see
``decal.vert``/``decal.frag`` in engine/shaders.py and
engine/renderer/forward/decals.py).

:class:`DecalTable` is the logic side: a fixed ring of rows, the oldest
overwritten first, each a box (a model matrix taking the unit cube to the
world), what it landed on (a wall or a floor: the two take different
images), a texture variant, whether it is mirrored and the time it was made. The logic thread adds
rows when something bleeds; each frame :meth:`frame` hands the renderer two
arrays, the boxes and their ``(variant, opacity, kind, mirrored)``, faded out over the last
part of a decal's life as bullet marks are.
"""

from __future__ import annotations

import math

import numpy as np

#: Most decals alive at once; a new one replaces the oldest.
CAPACITY = 128

_UP = np.array([0.0, 1.0, 0.0])
_SIDE = np.array([1.0, 0.0, 0.0])


#: What a decal landed on: its texture is picked from that kind's images.
WALL, FLOOR = 0, 1


def box_matrix(centre, forward, width, depth, roll=0.0, height=None):
    """The column-major 4x4 (as 16 float32) taking the unit cube
    (-0.5..0.5) to a decal box: *width* across, *height* up (*width* if
    not given), *depth* along
    *forward* -- the direction the decal is projected in, into the surface
    -- turned by *roll* radians about it. With a level *forward* the box's
    up is the world's, so a texture's top is up on a wall."""
    f = np.asarray(forward, dtype=np.float64)
    f = f / max(np.linalg.norm(f), 1e-9)
    ref = _SIDE if abs(f[1]) > 0.9 else _UP
    right = np.cross(ref, f)
    right /= max(np.linalg.norm(right), 1e-9)
    up = np.cross(f, right)
    if roll:
        c, s = math.cos(roll), math.sin(roll)
        right, up = right * c + up * s, up * c - right * s
    m = np.zeros((4, 4), dtype=np.float64)
    m[:3, 0] = right * width
    m[:3, 1] = up * (width if height is None else height)
    m[:3, 2] = f * depth
    m[:3, 3] = centre
    m[3, 3] = 1.0
    return m.T.reshape(16).astype(np.float32)        # column-major


class DecalTable:
    """A ring of decal rows; see the module docstring."""

    __slots__ = ('model', 'variant', 'kind', 'flip', 'born', '_next', 'count', 'version')

    def __init__(self, capacity: int = CAPACITY):
        self.model = np.zeros((capacity, 16), dtype=np.float32)
        self.variant = np.zeros(capacity, dtype=np.float32)
        #: WALL or FLOOR, and whether the texture is mirrored.
        self.kind = np.zeros(capacity, dtype=np.float32)
        self.flip = np.zeros(capacity, dtype=np.float32)
        #: perf_counter time each row was made; -inf where there is none.
        self.born = np.full(capacity, -np.inf, dtype=np.float64)
        self._next = 0
        #: Rows ever added (tests, Debug Tables).
        self.count = 0
        self.version = 0

    @property
    def capacity(self) -> int:
        return len(self.born)

    def add(self, centre, forward, width, depth, variant, now, roll=0.0,
            height=None, kind=WALL, flip=False) -> int:
        """Add a decal box; returns its row."""
        row = self._next
        self.model[row] = box_matrix(centre, forward, width, depth, roll, height)
        self.variant[row] = float(int(variant))
        self.kind[row] = float(kind)
        self.flip[row] = 1.0 if flip else 0.0
        self.born[row] = now
        self._next = (row + 1) % self.capacity
        self.count += 1
        self.version += 1
        return row

    def clear(self):
        self.born[:] = -np.inf
        self._next = 0
        self.version += 1

    def live(self, now, lifetime) -> np.ndarray:
        """The rows still alive at *now*, oldest first."""
        rows = np.flatnonzero(now - self.born < lifetime)
        return rows[np.argsort(self.born[rows], kind='stable')]

    def frame(self, now, lifetime, fade):
        """``(models (n, 16), params (n, 4))`` float32 for the renderer, or
        None when nothing is alive. ``params`` is ``(variant, opacity, kind,
        mirrored)``; opacity falls to 0 over the last *fade* seconds of
        *lifetime*.
        Copies: the logic thread goes on writing the table."""
        rows = self.live(now, lifetime)
        if not len(rows):
            return None
        params = np.zeros((len(rows), 4), dtype=np.float32)
        params[:, 0] = self.variant[rows]
        age = now - self.born[rows]
        params[:, 1] = np.clip((lifetime - age) / max(fade, 1e-6), 0.0, 1.0)
        params[:, 2] = self.kind[rows]
        params[:, 3] = self.flip[rows]
        return self.model[rows].copy(), params
