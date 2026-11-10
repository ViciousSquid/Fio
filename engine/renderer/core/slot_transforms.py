"""Every render-table row's model and normal matrices, kept by change (M3).

``render_table.model_matrices`` builds a row's matrices from two columns,
``bounds`` (centre and half-extents) and ``rot`` (axis and angle). Every pass
that drew boxes used to build them for every visible row on every frame,
although almost nothing moves from one frame to the next. Here they are
built once per row and kept, per render table; each reader first checks the
two columns against what the rows were last built from -- two whole-column
equality tests, no per-row Python -- and only the rows that differ are
rebuilt.

A row that was rebuilt is *stamped* with the cache's version at the time, so
a consumer that remembers the version it last read can find, in one gather
and compare, exactly the rows that changed since: what lets an instance
buffer upload only those rows (``instancing.BrushStream``).

A reconcile that renumbers the table's slots (``RenderTable.generation``
moves) or changes its row count rebuilds everything.
"""

import weakref

import numpy as np

from engine import render_table


class SlotTransforms:
    """The model ``(n, 16)`` and normal ``(n, 9)`` matrices of every row of
    one :class:`~engine.render_table.RenderTable`, column-major float32, as
    ``model_matrices`` builds them, to the bit."""

    __slots__ = ('_table', 'generation', 'count', 'bounds', 'rot', 'models',
                 'normals', 'stamp', 'version', 'rebuilt', '__weakref__')

    def __init__(self, table):
        self._table = weakref.ref(table)
        self.generation = None
        self.count = -1
        self.version = 0
        #: Rows rebuilt by the last sync (counts, for tests and tools).
        self.rebuilt = 0
        self.bounds = self.rot = self.models = self.normals = self.stamp = None

    def table(self):
        return self._table()

    def sync(self, table):
        """Bring the matrices up to *table*'s columns. Returns how many rows
        were rebuilt (0 for a still world).

        A still table is told by two whole-column equality tests (~8 us for
        3,600 rows here: ``np.array_equal`` over contiguous memory), so every
        reader can afford to sync before it reads and nothing depends on a
        frame boundary being announced; only on a difference are the changed
        rows found (from the flat element masks) and rebuilt."""
        count = table.count
        if table.generation != self.generation or count != self.count:
            self._rebuild(table, count)
            return count
        bounds = table.bounds[:count]
        rot = table.rot[:count]
        moved = not np.array_equal(bounds, self.bounds)
        turned = not np.array_equal(rot, self.rot)
        if not (moved or turned):
            self.rebuilt = 0
            return 0
        rows = (np.unique(np.flatnonzero((bounds != self.bounds).ravel()) // 6)
                if moved else None)
        if turned:
            spun = np.unique(np.flatnonzero((rot != self.rot).ravel()) // 4)
            rows = spun if rows is None else np.union1d(rows, spun)
        if not len(rows):
            self.rebuilt = 0            # NaN against NaN: equal, unchanged
            return 0
        models, normals = render_table.model_matrices(table, rows)
        self.models[rows] = models
        self.normals[rows] = normals
        self.bounds[rows] = bounds[rows]
        self.rot[rows] = rot[rows]
        self.version += 1
        self.stamp[rows] = self.version
        self.rebuilt = len(rows)
        return len(rows)

    def _rebuild(self, table, count):
        self.generation = table.generation
        self.count = count
        rows = np.arange(count, dtype=np.intp)
        self.models, self.normals = render_table.model_matrices(table, rows)
        self.bounds = table.bounds[:count].copy()
        self.rot = table.rot[:count].copy()
        self.version += 1
        self.stamp = np.full(count, self.version, dtype=np.int64)
        self.rebuilt = count

    def changed_since(self, version, slots):
        """Which of *slots* (an index array) were rebuilt after *version*:
        positions into *slots*."""
        if version >= self.version:
            return np.empty(0, dtype=np.intp)
        return np.flatnonzero(self.stamp[slots] > version)
