"""
Cell groups over the dense render rows, and the occlusion probe bookkeeping.

The renderer's frustum and distance culls answer *is this in the pyramid, is it
in range?* in one batched NumPy pass each. Neither can answer the question only
the GPU can answer cheaply -- *is there a wall in front of it?* -- and asking
that per brush would hand back the draw calls the instanced passes exist to
collapse. This module holds the two pieces that make asking it per **cell**
possible:

* :func:`group_rows_by_cell` buckets a frame's brush rows by the 512-unit cell
  of each row's centre -- the same ``floor(coord / 512)`` convention
  :mod:`engine.spatial` owns -- and returns one union box per occupied cell.
  The union contains every row filed under it, which is the whole trick: a box
  that fails the depth test at every pixel means each brush inside it, being
  inside the box along the eye ray, fails it too. One probe per cell group
  therefore tells the truth about every brush in the cell, and the probe count
  is a few dozen rather than thousands.

* :class:`OcclusionProbeBook` is the bookkeeping that turns a stream of
  answered probes into one question -- *may this cell's brushes be skipped
  this frame?* -- without touching GL, so its hysteresis and fail-open rules
  are unit-testable headlessly, the way :mod:`engine.render_cull` is.

Nothing here is baked, cached or stored in the map. The groups are the live
rows' centres re-derived per call, so an edit, a mover or a streamed-in cell
is simply part of the next call's answer -- the same discipline the dense
tables follow.

Deliberately NumPy-only: no GL, no glm, so the editor's play mode, the
standalone ``.fiopak`` player and a headless test all reach the same grouping.
"""

from __future__ import annotations

import numpy as np

from .spatial import CELL_SIZE, cells_of_points

#: How many consecutive completed probes must say "zero samples" before a
#: cell's brushes may be skipped, and how recent the last of them must be.
#: These are :class:`OcclusionProbeBook`'s defaults; the renderer creates the
#: book with them and nothing else needs to know the numbers.
OCCLUDE_AFTER = 2
#: A completed result older than this many frames no longer culls: the GPU
#: was busy, the answer is for a view the camera has left, and the failure
#: direction of the whole feature is *draw it*.
MAX_RESULT_AGE = 2


def group_rows_by_cell(bounds, cell_size=CELL_SIZE):
    """Bucket AABB rows by the 512-unit cell of each row's centre.

    *bounds* are ``(N, 6)`` ``(cx, cy, cz, hx, hy, hz)`` rows, the layout the
    dense tables and the frustum test already share. Returns
    ``(group_bounds, group_of_row, group_cells)``:

    * *group_bounds* -- ``(M, 6)`` centre/half rows, one per occupied cell,
      each the union box of the rows whose centre falls in that cell. The
      union contains every member, so it passes any plane or depth test a
      member passes; a group that fails takes its rows with it, correctly,
      while a group that passes proves nothing about its rows and they are
      tested themselves.
    * *group_of_row* -- ``(N,)`` int64, the group each row belongs to.
    * *group_cells* -- ``(M, 2)`` int64 ``(cell_x, cell_z)``, the group keys.
      They are stable across frames (a cell's identity is the grid, not the
      rows currently in it), which is what lets an occlusion probe follow one
      cell's box from frame to frame however its contents change.

    Rows are bucketed by their *centre's* cell, exactly one group each. This
    is deliberately narrower than the streaming cell index, which files a
    brush under every cell its footprint touches: that index answers "which
    cells does this object belong to" (a brush must stream back in when any
    cell it touches activates), while this one only needs groups that
    *contain* their rows, and a row's centre names one cell.
    """
    bounds = np.asarray(bounds, dtype=np.float64)
    count = len(bounds)
    if count == 0:
        return (np.zeros((0, 6), dtype=np.float64),
                np.zeros(0, dtype=np.int64),
                np.zeros((0, 2), dtype=np.int64))

    # Which cell each centre is in is :mod:`engine.spatial`'s one definition --
    # the batched form, with the integer cast that is the caller's business.
    cx, cz = cells_of_points(bounds[:, 0], bounds[:, 2], cell_size)
    cell_x = np.asarray(cx, dtype=np.int64)
    cell_z = np.asarray(cz, dtype=np.int64)
    # One int64 key per row. cx * 2**32 + cz is unique while |cz| < 2**31 -- a
    # world would have to span a trillion cells in Z before two cells collided.
    keys = cell_x * (1 << 32) + cell_z

    order = np.argsort(keys, kind="stable")
    sorted_keys = keys[order]
    new_group = np.empty(count, dtype=bool)
    new_group[0] = True
    new_group[1:] = sorted_keys[1:] != sorted_keys[:-1]
    starts = np.flatnonzero(new_group)

    # The per-row group index, aligned with ``starts`` (sorted order), then
    # scattered back to row order.
    group_of_sorted = np.cumsum(new_group) - 1
    group_of_row = np.empty(count, dtype=np.int64)
    group_of_row[order] = group_of_sorted

    mins = bounds[:, :3] - bounds[:, 3:]
    maxs = bounds[:, :3] + bounds[:, 3:]
    group_min = np.minimum.reduceat(mins[order], starts)
    group_max = np.maximum.reduceat(maxs[order], starts)
    group_bounds = np.concatenate(
        ((group_min + group_max) * 0.5, (group_max - group_min) * 0.5), axis=1)
    group_cells = np.stack((cell_x[order][starts], cell_z[order][starts]), axis=1)
    return group_bounds, group_of_row, group_cells


class OcclusionProbeBook:
    """Chained occlusion answers for the cell groups, GL-free.

    The renderer draws one depth-tested probe box per visible cell group after
    the frame's opaque geometry and reads the query a frame or two later
    (``GL_QUERY_RESULT_AVAILABLE`` only -- no frame ever waits on the GPU).
    This class turns that stream of answers into the one question the draw
    path asks -- *may this cell's brushes be skipped?* -- and every rule in it
    fails open:

    * a cell is skipped only after :data:`OCCLUDE_AFTER` *consecutive
      completed* probes reported zero samples -- a pending probe decides
      nothing, so a slow GPU simply means no culling rather than stale
      culling;
    * a result older than :data:`MAX_RESULT_AGE` frames no longer culls, so
      an answer for a view the camera has already left stops being used
      before a turn can reveal a hole;
    * one probe that says "samples passed" clears the streak, and a cell
      that has never been probed reads as not culled.

    The book holds plain per-cell integers; the query objects themselves stay
    with the renderer, on the thread that owns the GL context.
    """

    def __init__(self, occlude_after=OCCLUDE_AFTER,
                 max_result_age=MAX_RESULT_AGE):
        self.occlude_after = max(1, int(occlude_after))
        self.max_result_age = max(0, int(max_result_age))
        # coord -> consecutive completed zero-sample probes.
        self._streak = {}
        # coord -> the frame index of the most recent completed probe.
        self._last_result = {}

    def record(self, coord, samples, frame):
        """A completed probe: *samples* pixels of the cell's box survived.

        A visible result clears the streak outright -- the cell has been seen,
        and being seen once outranks any number of earlier hidings. ``frame``
        is the renderer's own monotonic frame counter, used only to age
        results; its absolute value means nothing.
        """
        coord = (int(coord[0]), int(coord[1]))
        if samples > 0:
            self._streak.pop(coord, None)
        else:
            self._streak[coord] = self._streak.get(coord, 0) + 1
        self._last_result[coord] = int(frame)

    def culled(self, coord, frame):
        """Whether the cell's brushes may be skipped at *frame*.

        The fail-open path is the point of the method: unknown cells, cells
        with an unfinished streak and cells whose newest answer has gone stale
        all read as *draw it*.
        """
        coord = (int(coord[0]), int(coord[1]))
        if self._streak.get(coord, 0) < self.occlude_after:
            return False
        return frame - self._last_result.get(coord, -1) <= self.max_result_age

    def retire(self, coords):
        """Drop every cell not in *coords*; they left the candidate set.

        Called once per frame with the cells the camera can currently reach,
        so the book describes the view, not the map: leaving play, an empty
        scene or a disabled feature simply retires everything.
        """
        keep = {(int(c[0]), int(c[1])) for c in coords}
        for coord in list(self._streak):
            if coord not in keep:
                del self._streak[coord]
        for coord in list(self._last_result):
            if coord not in keep:
                del self._last_result[coord]

    def __len__(self):
        return len(self._last_result)
