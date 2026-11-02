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

import hashlib

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
    group_center = (group_min + group_max) * 0.5
    group_half = (group_max - group_min) * 0.5
    # Reconstructing min/max from centre +/- half can round INWARDS even in
    # float64. More importantly, GPU probe transforms are float32 on GL 3.3,
    # including an SQ3 Adreno: pad outwards for the loss of float32 precision
    # in the centre, scale and vertex transform. Never trust a marginal
    # false-negative result that could punch a hole in the scene.
    pad = 8.0 * np.finfo(np.float32).eps * np.maximum(
        1.0, np.maximum(np.abs(group_min), np.abs(group_max)))
    group_bounds = np.concatenate((group_center, group_half + pad), axis=1)
    group_cells = np.stack((cell_x[order][starts], cell_z[order][starts]), axis=1)
    return group_bounds, group_of_row, group_cells



class CellGroupIndex:
    """A change-driven cell index owned by one published RenderTable buffer.

    Group construction runs once at first use or on structural reconcile.
    Row refreshes update only old/new touched cells. The frame-time path
    consumes cached group IDs, boxes, and signatures with a NumPy bincount,
    never sorting or recomputing AABBs for the visible brush population.
    """

    def __init__(self, table):
        self.rebuild(table)

    @staticmethod
    def _eligible(table, slot):
        return (not bool(table.hidden[slot])
                and float(table.rot[slot, 3]) == 0.0
                and int(table.geometry_id[slot]) < 0)

    def rebuild(self, table):
        n = int(table.count)
        self.slot_group = np.full(n, -1, dtype=np.int32)
        self.coord_ids = {}
        self.member_sets = []
        self.cells = []
        self.boxes = np.zeros((0, 6), dtype=np.float64)
        self.signatures = []
        eligible = np.flatnonzero(
            (~table.hidden[:n]) & (table.rot[:n, 3] == 0)
            & (table.geometry_id[:n] < 0))
        if len(eligible):
            boxes, members, cells = group_rows_by_cell(table.bounds[eligible])
            self.boxes = boxes
            self.cells = [tuple(map(int, cell)) for cell in cells]
            self.coord_ids = {cell: i for i, cell in enumerate(self.cells)}
            self.member_sets = [set() for _ in self.cells]
            self.slot_group[eligible] = members.astype(np.int32)
            for slot, group in zip(eligible, members):
                self.member_sets[int(group)].add(int(slot))
            self.signatures = [self._signature(table, i)
                               for i in range(len(self.cells))]
        self.rebuilds = getattr(self, 'rebuilds', 0) + 1
        self.changed_groups = 0

    def _signature(self, table, group):
        """Stable across publication buffers for identical rows in a cell.

        Include full row data: moving/hidden geometry must invalidate old
        answers even if its cell union box happens to remain unchanged.
        Built only on a changed group, not in the hot camera frame.
        """
        slots = self.member_sets[group]
        box = tuple(map(float, self.boxes[group]))
        if not slots:
            return (box, ())
        # Hash the dense numeric rows in C instead of constructing a
        # Python tuple of every brush and every float. Slot addresses are
        # stable within a published generation; structural changes discard
        # this index. The same content in either publication buffer produces
        # the same signature, even if a mover changes the union box by zero.
        idx = np.fromiter(slots, dtype=np.intp, count=len(slots))
        idx.sort()
        digest = hashlib.blake2b(digest_size=16)
        digest.update(idx.tobytes())
        digest.update(table.bounds[idx].tobytes())
        digest.update(table.class_bits[idx].tobytes())
        digest.update(table.geo_epoch[idx].tobytes())
        return (box, digest.digest())

    def update(self, table, dirty):
        """Incrementally apply the journalled row edits and affected boxes."""
        dirty = {int(s) for s in dirty if 0 <= int(s) < table.count}
        if not dirty:
            return
        if (len(self.slot_group) != int(table.count)
                or len(dirty) > max(128, table.count // 8)):
            self.rebuild(table)
            return
        touched = set()
        for slot in dirty:
            previous = int(self.slot_group[slot])
            if self._eligible(table, slot):
                xs, zs = cells_of_points(
                    table.bounds[slot:slot + 1, 0],
                    table.bounds[slot:slot + 1, 2])
                coord = (int(xs[0]), int(zs[0]))
                current = self.coord_ids.get(coord)
                if current is None:
                    current = len(self.cells)
                    self.coord_ids[coord] = current
                    self.cells.append(coord)
                    self.member_sets.append(set())
                    self.boxes = np.vstack(
                        (self.boxes, np.zeros((1, 6), dtype=np.float64)))
                    self.signatures.append(None)
            else:
                current = -1
            if previous >= 0:
                touched.add(previous)
            if previous != current:
                if previous >= 0:
                    self.member_sets[previous].discard(slot)
                if current >= 0:
                    self.member_sets[current].add(slot)
                self.slot_group[slot] = current
            if current >= 0:
                touched.add(current)

        for group in touched:
            members = self.member_sets[group]
            if not members:
                self.boxes[group] = 0.0
                self.signatures[group] = None
                continue
            rows = table.bounds[np.fromiter(members, dtype=np.intp)]
            mins = np.min(rows[:, :3] - rows[:, 3:], axis=0)
            maxs = np.max(rows[:, :3] + rows[:, 3:], axis=0)
            center = (mins + maxs) * 0.5
            half = (maxs - mins) * 0.5
            pad = 8.0 * np.finfo(np.float32).eps * np.maximum(
                1.0, np.maximum(np.abs(mins), np.abs(maxs)))
            self.boxes[group] = np.r_[center, half + pad]
            self.signatures[group] = self._signature(table, group)
        self.changed_groups += len(touched)

    def visible(self, slots):
        """Cheap dense gather + bincount instead of per-frame cell grouping."""
        slots = np.asarray(slots, dtype=np.intp)
        per_slot = self.slot_group[slots]
        eligible = per_slot >= 0
        if not np.any(eligible):
            return None
        group = per_slot[eligible]
        counts = np.bincount(group, minlength=len(self.cells))
        active = np.flatnonzero(counts)
        if not len(active):
            return None
        reverse = np.empty(len(self.cells), dtype=np.int32)
        reverse[active] = np.arange(len(active), dtype=np.int32)
        return (eligible, self.boxes[active],
                reverse[group], np.asarray([self.cells[i] for i in active],
                                           dtype=np.int64),
                np.asarray(counts[active], dtype=np.intp),
                [self.signatures[i] for i in active])


def select_probe_groups(group_of_row, near_sq, budget, *, near_exempt_sq,
                        min_group_rows=16, counts=None):
    """Choose a *small* set of worthwhile groups to probe, without GL.

    The renderer has already calculated the squared 3-D distance to each
    union bounding box (not to its centre, and not an erroneous X/Y
    ground-plane distance).  Never spend a draw on near-exempt groups or
    groups containing too few brushes to plausibly repay the probe.
    Prioritise groups containing more brush rows, then nearer boxes.
    """
    near_sq = np.asarray(near_sq, dtype=np.float64)
    if budget <= 0 or not len(near_sq):
        return np.empty(0, dtype=np.intp)
    if counts is None:
        counts = np.bincount(np.asarray(group_of_row, dtype=np.intp),
                             minlength=len(near_sq))
    else:
        counts = np.asarray(counts, dtype=np.intp)
    candidates = np.flatnonzero(
        (near_sq >= near_exempt_sq) & (counts >= min_group_rows))
    if not len(candidates):
        return np.empty(0, dtype=np.intp)
    order = np.lexsort((near_sq[candidates], -counts[candidates]))
    return candidates[order[:int(budget)]].astype(np.intp, copy=False)


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

    def forget(self, coord):
        """Drop any visibility decision for a cell whose contents changed."""
        coord = (int(coord[0]), int(coord[1]))
        self._streak.pop(coord, None)
        self._last_result.pop(coord, None)

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


#: Fraction of its window's probed rows the probes must remove for probing
#: to keep its full budget; below it the budget halves, and at the floor
#: the feature parks for :data:`PROBE_COOLDOWN` frames, then re-arms.
PROBE_KEEP_FRACTION = 0.10
#: The smallest per-frame probe budget :class:`ProbeGovernor` shrinks to.
MIN_PROBE_BUDGET = 8
#: Frames :class:`ProbeGovernor` keeps the feature parked once the budget
#: floor still finds no savings.
PROBE_COOLDOWN = 180


class ProbeGovernor:
    """The probe budget, decided by what the probes actually remove.

    The occlusion probes are cheap one at a time and not cheap as a habit:
    every probed frame pays the grouping pass and up to the budget in extra
    draw calls, through drivers (the low-power ARM target) where a draw
    call costs a measurable share of the frame. Whether that habit pays
    depends on the scene -- an interior with long walls hides whole cells
    and repays it many times over; an open field hides nothing and the cost
    is pure loss. The governor keeps a rolling window of (rows probed,
    rows removed) and decides, once per window, whether the probes earned
    their budget:

    * at or above :data:`PROBE_KEEP_FRACTION` removed, the budget stays
      (or steps back up toward its maximum after a shrink);
    * below it, the budget halves, down to :data:`MIN_PROBE_BUDGET`;
    * at the floor with still nothing removed, the feature parks for
      :data:`PROBE_COOLDOWN` frames -- no grouping, no probes -- then
      re-arms with a small budget to re-sample a world that may have
      changed while parked.

    Frames the feature did not run (fast turns, empty scenes) are simply
    not sampled, so a held camera does not count against the probes. No GL
    lives here; like :class:`OcclusionProbeBook`, the rules are plain
    integers, pinned headlessly in ``tests/renderer/``.
    """

    def __init__(self, max_probes, window=32,
                 keep_fraction=PROBE_KEEP_FRACTION,
                 min_budget=MIN_PROBE_BUDGET, cooldown=PROBE_COOLDOWN,
                 max_cpu_ms=None, strong_savings_fraction=0.35):
        self.max_probes = int(max_probes)
        self.budget = self.max_probes
        self.window = int(window)
        self.keep_fraction = float(keep_fraction)
        self.min_budget = int(min_budget)
        self.cooldown = int(cooldown)
        # Cost-based guardrail, not a claim of proven net savings: true
        # payback still requires a full-frame A/B run on the target GPU.
        self.max_cpu_ms = (None if max_cpu_ms is None
                           else float(max_cpu_ms))
        self.strong_savings_fraction = float(strong_savings_fraction)
        self._samples = []
        self._idle_left = 0

    def record(self, rows_probed, rows_skipped, overhead_ms=None):
        """Record culling output AND optional measured CPU overhead."""
        if rows_probed <= 0:
            return
        self._samples.append((
            int(rows_probed), int(rows_skipped),
            None if overhead_ms is None else max(0.0, float(overhead_ms))))
        if len(self._samples) > self.window:
            del self._samples[:len(self._samples) - self.window]

    def probe_count(self):
        """How many probes this frame may issue; 0 means parked."""
        if self._idle_left:
            self._idle_left -= 1
            if not self._idle_left:
                # Re-arm small: the world may have changed while the feature
                # was parked -- movers, streamed-in cells, a corridor the
                # camera only now reached -- and a small budget spends
                # little to find out.
                self.budget = min(self.max_probes, self.min_budget * 2)
                self._samples = []
            return 0
        if len(self._samples) >= self.window:
            probed = sum(sample for sample, _, _ in self._samples)
            skipped = sum(sample for _, sample, _ in self._samples)
            costs = [cost for _, _, cost in self._samples
                     if cost is not None]
            self._samples = []
            fraction = skipped / probed
            # Even high skip rates can lose on Adreno if CPU-side group
            # work and GL queries cost too much. Only allow expensive
            # probing when it hides a substantial fraction of brushes.
            expensive = (self.max_cpu_ms is not None and costs
                         and sum(costs) / len(costs) > self.max_cpu_ms
                         and fraction < self.strong_savings_fraction)
            if fraction < self.keep_fraction or expensive:
                if self.budget > self.min_budget:
                    self.budget = max(self.min_budget, self.budget // 2)
                else:
                    self._idle_left = self.cooldown
                    return 0
            elif self.budget < self.max_probes:
                self.budget = min(self.max_probes, self.budget * 2)
        return self.budget

    def state(self):
        """One word for Debug Tables: the current budget, or 'parked'."""
        if self._idle_left:
            return f"parked {self._idle_left}"
        return f"budget {self.budget}"
