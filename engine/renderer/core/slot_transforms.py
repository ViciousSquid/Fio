"""Every render-table row's model and normal matrices, kept by change (M3, M4).

``render_table.model_matrices`` builds a row's matrices from two columns,
``bounds`` (centre and half-extents) and ``rot`` (axis and angle). Every pass
that drew boxes used to build them for every visible row on every frame,
although almost nothing moves from one frame to the next. Here they are
built once per row and kept, per render table.

What changed is not found by looking (M4): every write to those columns
goes through the table, which logs the rows it wrote
(``RenderTable.transforms_since``). A still frame costs one integer test; a
frame with k rows written rebuilds those k. (M3 compared both columns whole
every frame; in oracle mode -- ``change_detection.ORACLE`` -- that
comparison still runs, and a row that changed without being logged raises
``MissedChange``.)

A row that was rebuilt is *stamped* with the cache's version, and the rows
of each rebuild are logged here in turn, so an instance buffer that
remembers the version it last matched finds exactly the rows to rewrite
(``instancing.BrushStream``).

A reconcile that renumbers the table's slots (``RenderTable.generation``
moves), a change of row count, or a reader further behind than the table's
log rebuilds everything.
"""

from collections import deque
import weakref

import numpy as np

from engine import change_detection, render_table

#: Rebuilds a SlotTransforms remembers the rows of (for the streams).
REBUILD_LOG = 64


class SlotTransforms:
    """The model ``(n, 16)`` and normal ``(n, 9)`` matrices of every row of
    one :class:`~engine.render_table.RenderTable`, column-major float32, as
    ``model_matrices`` builds them, to the bit."""

    __slots__ = ('_table', 'generation', 'count', 'bounds', 'rot', 'models',
                 'normals', 'stamp', 'version', 'rebuilt', 'table_version',
                 '_log', '__weakref__')

    def __init__(self, table):
        self._table = weakref.ref(table)
        self.generation = None
        self.count = -1
        self.version = 0
        #: Rows rebuilt by the last sync (counts, for tests and tools).
        self.rebuilt = 0
        self.bounds = self.rot = self.models = self.normals = self.stamp = None
        #: The table's ``transform_version`` these matrices were brought to.
        self.table_version = -1
        #: ``(version, rows)`` per partial rebuild.
        self._log = deque(maxlen=REBUILD_LOG)

    def table(self):
        return self._table()

    def sync(self, table):
        """Bring the matrices up to *table*'s columns. Returns how many rows
        were rebuilt (0 for a still world).

        The rows to rebuild are the ones the table logged since these
        matrices were last brought up to it: a still table costs one integer
        test, whatever its size."""
        count = table.count
        if table.generation != self.generation or count != self.count:
            self._rebuild(table, count)
            return count
        if self.table_version == table.transform_version:
            self.rebuilt = 0
            if change_detection.ORACLE:
                self._check(table, ())
            return 0
        rows = table.transforms_since(self.table_version)
        if rows is None:                    # further behind than the log
            self._rebuild(table, count)
            return count
        self.table_version = table.transform_version
        if len(rows):
            rows = rows[rows < count]
        if change_detection.ORACLE:
            self._check(table, rows)
        if not len(rows):
            self.rebuilt = 0
            return 0
        models, normals = render_table.model_matrices(table, rows)
        self.models[rows] = models
        self.normals[rows] = normals
        self.bounds[rows] = table.bounds[rows]
        self.rot[rows] = table.rot[rows]
        self.version += 1
        self.stamp[rows] = self.version
        self._log.append((self.version, rows))
        self.rebuilt = len(rows)
        return len(rows)

    def _check(self, table, rows):
        """Oracle: every row whose columns differ from what these matrices
        were built from is among *rows* (the logged ones)."""
        count = self.count
        differ = ((table.bounds[:count] != self.bounds).any(axis=1)
                  | (table.rot[:count] != self.rot).any(axis=1))
        # NaN against NaN: equal, unchanged.
        differ &= ~(np.isnan(table.bounds[:count]).any(axis=1)
                    & np.isnan(self.bounds).any(axis=1))
        missed = np.setdiff1d(np.flatnonzero(differ), np.asarray(rows, dtype=np.intp))
        if len(missed):
            change_detection.missed(
                "render-table rows %s changed transform without being logged"
                % missed[:8].tolist())

    def rows_since(self, version):
        """Rows rebuilt after *version* (a value of :attr:`version`), in no
        particular order and possibly repeated, or None when this does not
        remember that far back (every row, as far as the caller knows)."""
        if version >= self.version:
            return render_table._NO_ROWS
        log = self._log
        if not log or log[0][0] > version + 1:
            return None
        parts = [rows for rebuilt, rows in log if rebuilt > version]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _rebuild(self, table, count):
        self.generation = table.generation
        self.count = count
        rows = np.arange(count, dtype=np.intp)
        self.models, self.normals = render_table.model_matrices(table, rows)
        self.bounds = table.bounds[:count].copy()
        self.rot = table.rot[:count].copy()
        self.version += 1
        self.stamp = np.full(count, self.version, dtype=np.int64)
        self.table_version = table.transform_version
        self._log.clear()                   # every row: rows_since says so
        self.rebuilt = count

    def changed_since(self, version, slots):
        """Which of *slots* (an index array) were rebuilt after *version*:
        positions into *slots*."""
        if version >= self.version:
            return np.empty(0, dtype=np.intp)
        return np.flatnonzero(self.stamp[slots] > version)
