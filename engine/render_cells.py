"""The brush frustum cull's broad phase: occupied cells -> RenderTable slots.

The frustum test (:meth:`engine.logic_thread.LogicThread._aabb_in_frustum_bounds`)
is one ``(N, 6) x (6, 6)`` product over every row of the
:class:`engine.render_table.RenderTable`, 24 000 rows on a large map, most of
them behind the camera or out past the far plane. This module finds the rows
worth testing, without looking at any brush.

It files every row under the :data:`engine.spatial.CELL_SIZE` columns its XZ
footprint overlaps -- the same cells, by the same ``floor(coord / cell_size)``
rule (:func:`engine.spatial.cells_of_points`), that the collision grid and Big
World use. It is not a second spatial system. It is that one, holding slots
instead of objects, and stored as flat arrays instead of
:class:`engine.spatial.CellIndex`'s dict of lists, so that a query is a few
NumPy calls and never a Python loop.

Exactness
---------
A cell's *box* is the union of the full AABBs of the rows filed under it (not
clipped to the cell), padded by :data:`CELL_BOX_PAD`. The broad phase runs the
**same** plane test over the cell boxes, then the narrow phase runs it over the
rows of the cells that passed. That gives exactly the full cull's answer, not an
approximation of it. For a plane with normal *n*, the test compares the largest
value of ``n . p`` over a box, and a box that contains another cannot have a
smaller one. So any row the full cull keeps is filed under at least one cell,
that cell's box contains the row, and the cell passes.

An XZ region such as :func:`engine.render_cull.visible_xz_bounds` cannot serve
here, however exact. The plane test is conservative: near the frustum's edges
it also keeps a few boxes that lie wholly outside the frustum, and outside any
region derived from it. A region-based broad phase would drop those and change
the visible set; the cell boxes cannot.

Staying current
---------------
The index is built from the table's ``bounds`` column when the row set changes
(a reconcile invalidates it) and otherwise kept:

* a row whose bounds are re-read -- a journalled move, an editor drag, a
  refresh -- is *touched*; at the next query its bounds are compared with the
  ones it was filed with, and a row that really moved becomes *loose*;
* loose rows are always candidates, so a stale filing costs a few extra
  narrow-phase rows and can never hide anything; the index is rebuilt only once
  more than :data:`REBUILD_LOOSE` rows are loose;
* movers and doors (``dynamic_slots``) move every tick. They are always
  candidates, and their filing is never consulted, so the moving rows never
  touch the index at all;
* a row wider than :data:`MAX_CELLS_PER_ROW` cells (a sky box, a terrain
  slab) is not filed at all, and is always a candidate.

Nothing here reads ``hidden``. The caller masks the candidates with the table's
shown mask, exactly as it masks the full cull, so hidden rows and rows Big World
has parked are dropped exactly as before.
"""

from __future__ import annotations

import numpy as np

from engine.spatial import CELL_SIZE, cells_of_points

#: Rows whose footprint spans more cells than this are not filed; they are
#: always candidates instead. Filing a 20 000-unit slab under 1 600 cells would
#: cost more to query than testing it.
MAX_CELLS_PER_ROW = 64

#: Rows that may be loose (moved since they were filed) before the index is
#: rebuilt. Loose rows are tested every frame, so this bounds that cost.
REBUILD_LOOSE = 512

#: World units added to every side of a cell box. Rounding in the ``[centre |
#: half]`` form could otherwise leave a cell box a hair smaller than the row
#: that defines one of its faces, and that row's cell failing a plane the row
#: passes by less than the rounding.
CELL_BOX_PAD = 1.0

_EMPTY_SLOTS = np.empty(0, dtype=np.intp)


class SlotCellIndex:
    """Occupied cells of one :class:`RenderTable`, each listing its slots.

    Flat, CSR-style arrays, all built together and never written in place:

    * ``cell_boxes`` -- ``(C, 6)`` ``[centre | half]`` of each occupied cell's
      padded row union, in the form the frustum test takes;
    * ``starts`` / ``counts`` -- each cell's run in ``members``;
    * ``members`` -- slot ids, grouped by cell (a row spanning several cells
      appears once per cell);
    * ``filed`` -- the ``bounds`` rows were filed with, to recognise a touched
      row that did not actually move;
    * ``unfiled`` -- the rows too wide to file.

    Because a build never mutates arrays it has handed out, the other render
    buffer's table can share them (:meth:`copy`).
    """

    __slots__ = ('cell_size', 'count', 'cell_boxes', 'starts', 'counts',
                 'members', 'filed', 'unfiled', 'stale', 'builds',
                 '_touched', '_loose_mask', '_loose', '_mark')

    def __init__(self, cell_size=CELL_SIZE):
        self.cell_size = float(cell_size)
        self.count = 0
        self.cell_boxes = np.empty((0, 6), dtype=np.float64)
        self.starts = _EMPTY_SLOTS
        self.counts = _EMPTY_SLOTS
        self.members = _EMPTY_SLOTS
        self.filed = np.empty((0, 6), dtype=np.float64)
        self.unfiled = _EMPTY_SLOTS
        #: Rebuild before the next query.
        self.stale = True
        #: How many times this index was built (Debug Tables, tests).
        self.builds = 0
        self._touched: list = []
        self._loose_mask = np.zeros(0, dtype=bool)
        self._loose = _EMPTY_SLOTS
        self._mark = np.zeros(0, dtype=bool)

    # -- keeping it current --------------------------------------------------

    def invalidate(self):
        """The row set changed: rebuild before the next query."""
        self.stale = True
        self._touched.clear()

    def touch(self, slots):
        """These rows' bounds were re-read and may have changed."""
        if not self.stale:
            self._touched.append(slots)

    def copy(self):
        """An index for another table holding the same rows.

        The built arrays are shared, never copied: nothing writes them in place.
        """
        other = SlotCellIndex(self.cell_size)
        for name in ('count', 'cell_boxes', 'starts', 'counts', 'members',
                     'filed', 'unfiled', 'stale', '_loose'):
            setattr(other, name, getattr(self, name))
        other._touched = list(self._touched)
        other._loose_mask = self._loose_mask.copy()
        return other

    @property
    def loose(self):
        """Rows that moved since they were filed (always candidates)."""
        return self._loose

    def build(self, bounds, count):
        """File rows ``[0, count)`` of *bounds* (``[centre | half]``)."""
        rows = bounds[:count]
        lo = rows[:, :3] - rows[:, 3:]
        hi = rows[:, :3] + rows[:, 3:]
        cx0, cz0 = cells_of_points(lo[:, 0], lo[:, 2], self.cell_size)
        cx1, cz1 = cells_of_points(hi[:, 0], hi[:, 2], self.cell_size)
        cx0 = cx0.astype(np.int64)
        cz0 = cz0.astype(np.int64)
        span_x = cx1.astype(np.int64) - cx0 + 1
        span_z = cz1.astype(np.int64) - cz0 + 1
        per_row = span_x * span_z
        fits = per_row <= MAX_CELLS_PER_ROW
        filed_rows = np.flatnonzero(fits)
        self.unfiled = np.flatnonzero(~fits)

        # One entry per (row, cell): row r repeated per_row[r] times, and its
        # k-th entry at cell (cx0 + k // span_z, cz0 + k % span_z).
        reps = per_row[filed_rows]
        slot = np.repeat(filed_rows, reps)
        k = np.arange(len(slot), dtype=np.int64) - np.repeat(np.cumsum(reps) - reps, reps)
        sz = span_z[slot]
        cx = cx0[slot] + k // sz
        cz = cz0[slot] + k % sz
        if len(slot):
            cz_min = cz.min()
            key = (cx - cx.min()) * (cz.max() - cz_min + 1) + (cz - cz_min)
        else:
            key = cx
        order = np.argsort(key, kind='stable')
        members = slot[order]
        _, starts, counts = np.unique(key[order], return_index=True,
                                      return_counts=True)

        if len(members):
            box_lo = np.minimum.reduceat(lo[members], starts) - CELL_BOX_PAD
            box_hi = np.maximum.reduceat(hi[members], starts) + CELL_BOX_PAD
            self.cell_boxes = np.concatenate(
                ((box_lo + box_hi) * 0.5, (box_hi - box_lo) * 0.5), axis=1)
        else:
            self.cell_boxes = np.empty((0, 6), dtype=np.float64)
        self.members = members.astype(np.intp)
        self.starts = starts.astype(np.intp)
        self.counts = counts.astype(np.intp)
        self.filed = rows.copy()
        self.count = count
        self._loose_mask = np.zeros(count, dtype=bool)
        self._loose = _EMPTY_SLOTS
        self._touched.clear()
        self.stale = False
        self.builds += 1

    def _settle(self, bounds, count):
        """Build if stale; turn touched rows that really moved into loose rows."""
        if self.stale or count != self.count:
            self.build(bounds, count)
            return
        if not self._touched:
            return
        touched = np.unique(np.concatenate(
            [np.asarray(slots, dtype=np.intp) for slots in self._touched]))
        self._touched.clear()
        touched = touched[touched < count]
        moved = touched[(bounds[touched] != self.filed[touched]).any(axis=1)]
        if len(moved):
            self._loose_mask[moved] = True
            self._loose = np.flatnonzero(self._loose_mask)
            if len(self._loose) > REBUILD_LOOSE:
                self.build(bounds, count)

    # -- the query -----------------------------------------------------------

    def candidate_mask(self, cell_passes, bounds, count, always=()):
        """Which rows the narrow phase must test, as a mask over ``[0, count)``.

        *cell_passes* is the frustum test to run, applied to ``(C, 6)`` cell
        boxes. It is taken as a callable so that the broad phase and the narrow
        phase are the same test. *always* lists rows that are candidates
        whatever their filing says (the movers and doors).

        The returned mask is scratch owned by the index, valid until the next
        call.
        """
        self._settle(bounds, count)
        mark = self._mark
        if len(mark) < count:
            mark = self._mark = np.zeros(max(count, 2 * len(mark)), dtype=bool)
        else:
            mark[:count] = False
        passing = np.flatnonzero(cell_passes(self.cell_boxes))
        if len(passing):
            counts = self.counts[passing]
            runs = np.repeat(self.starts[passing] - (np.cumsum(counts) - counts),
                             counts)
            mark[self.members[runs + np.arange(len(runs))]] = True
        mark[self.unfiled] = True
        mark[self._loose] = True
        if len(always):
            mark[always] = True
        return mark[:count]
