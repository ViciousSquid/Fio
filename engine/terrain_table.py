"""The dense terrain projection: one row per resident terrain chunk.

:mod:`engine.render_table` and :mod:`engine.entity_table` gave brushes and
entities a dense projection, so the renderer stopped asking every object what
it was on every frame. Terrain kept a ``dict`` of ``TerrainChunk`` dataclasses
and walked it in Python several times a frame -- residency, visibility, LOD and
the build queue were each a per-chunk loop -- and it kept *two* height grids
per chunk: a 33x33 one for collision and the 49x49 one the mesh was built
from, which disagreed by up to a few units on sculpted ground.

A :class:`TerrainTable` is the same kind of thing as its two siblings:

* **GL-free.** It holds numbers, never GL names. The terrain renderer keeps
  its GL objects in arrays indexed by the same ``slot``.
* **A projection, not a source.** Every value is derived from the terrain's
  procedural height function and the camera; throwing the table away and
  streaming again reproduces it exactly.
* **Addressed by slot, named by chunk coordinate.** ``slot_of_coord`` is the
  cache-boundary mechanism (crossed when a chunk enters or leaves, and by a
  collision query); frame code works on whole columns.

It differs where terrain differs. Rows are not authored objects, so there is
no UUID and no change journal: the row set moves with the streaming ring, and
a row goes stale when its heights change (a sculpt edit, a biome change),
which the terrain reports through :meth:`mark_dirty_region` /
:meth:`mark_all_dirty`. And a row carries a height grid -- 49x49 floats -- far
too much to double-buffer the way RenderState buffers the other two tables,
so the table has one writer (the thread that builds chunks) and readers on
other threads use a per-slot sequence number instead (:meth:`height_at`).

**One heightfield.** :attr:`heights` holds the grid a chunk was last built
at, and it is what both consumers read: the renderer draws it, and collision
interpolates it over the *same triangles* (the same quad diagonal) the
renderer draws. The player stands on the surface they can see.

**Absence is explicit.** A map without terrain has no ``Terrain`` and so no
table; nothing here is allocated for an ordinary map.

Every method below is an exact, vectorised port of the per-chunk loop it
replaced in :mod:`engine.terrain` (kept, frozen, in
``tests/helpers/terrain_reference.py``) -- the same float64 arithmetic in the
same order, so its decisions are the old decisions, not approximations of them.
"""

from __future__ import annotations

import math

import numpy as np

#: The largest grid a chunk is built at: LOD 0's 48 quads per side, plus one.
MAX_GRID = 49

#: Every per-row column: ``(name, trailing shape, dtype, fill)``.
_COLUMNS = (
    #: ``(cx, cz)`` chunk index, relative to the terrain offset.
    ('coord', (2,), np.int64, 0),
    #: ``coord`` packed into one int64, for vectorised membership tests.
    ('key', (), np.int64, 0),
    ('live', (), bool, False),
    #: Allocation counter. The old chunk dict iterated in insertion order and
    #: the build queue's sort was stable over it; this reproduces that order.
    ('order', (), np.int64, 0),
    #: World origin and edge length, frozen when the row is created -- exactly
    #: what ``TerrainChunk`` stored, and why an offset edit only takes effect
    #: for chunks created after it.
    ('world', (2,), np.float64, 0.0),
    ('size', (), np.float64, 0.0),
    #: Heights no longer match the terrain's height function.
    ('dirty', (), bool, True),
    #: A grid has been built (``TerrainChunk.is_uploaded``).
    ('built', (), bool, False),
    #: Index into the LOD resolutions the grid was built at.
    ('lod', (), np.int8, 0),
    #: LOD hysteresis state: the target *resolution* (0 until first set) and
    #: how many frames it has held.
    ('target_res', (), np.int32, 0),
    ('stable', (), np.int32, 0),
    #: Quads per side of the built grid; the grid is ``grid_res + 1`` square.
    ('grid_res', (), np.int32, 0),
    ('min_y', (), np.float64, 0.0),
    ('max_y', (), np.float64, 0.0),
    #: Bumped on every build; the renderer re-uploads a slot whose version it
    #: has not seen.
    ('version', (), np.int64, 0),
    #: Sequence lock for cross-thread readers: odd while the row is being
    #: written or is free.
    ('seq', (), np.int64, 1),
    ('grass_dirty', (), bool, True),
    ('heights', (MAX_GRID, MAX_GRID), np.float32, 0.0),
)

_EMPTY = np.empty(0, dtype=np.intp)


def pack_keys(cx, cz):
    """One int64 per ``(cx, cz)``: unique for any 32-bit chunk index."""
    cx = np.asarray(cx, dtype=np.int64)
    cz = np.asarray(cz, dtype=np.int64)
    return (cx << 32) + (cz & 0xFFFFFFFF)


class TerrainTable:
    """Dense per-chunk terrain state. See the module docstring."""

    def __init__(self):
        #: Bumped whenever the columns are reallocated, so a reader on another
        #: thread can tell it straddled a resize.
        self.generation = 0
        #: Live rows.
        self.count = 0
        #: ``(cx, cz)`` -> slot. Crossed on residency changes and by collision
        #: queries, never walked per frame.
        self.slot_of_coord: dict = {}
        self._free: list = []
        self._next_order = 0
        self._capacity = 0
        for name, shape, dtype, fill in _COLUMNS:
            setattr(self, name, np.full((0,) + shape, fill, dtype=dtype))

    # -- capacity / allocation --------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def _grow(self, n):
        if n <= self._capacity:
            return
        grown = max(64, self._capacity * 2, n)
        for name, shape, dtype, fill in _COLUMNS:
            new = np.full((grown,) + shape, fill, dtype=dtype)
            new[:self._capacity] = getattr(self, name)
            setattr(self, name, new)
        self._capacity = grown
        self.generation += 1

    def live_slots(self) -> np.ndarray:
        """Live slots in the old dict's iteration order (allocation order)."""
        slots = np.flatnonzero(self.live[:self._capacity])
        if len(slots) > 1:
            slots = slots[np.argsort(self.order[slots], kind='stable')]
        return slots

    def _allocate(self, cx, cz, chunk_size, offset_x, offset_z) -> int:
        if self._free:
            slot = self._free.pop()
        else:
            slot = self.count + len(self._free)
            self._grow(slot + 1)
        self.coord[slot] = (cx, cz)
        self.key[slot] = pack_keys(cx, cz)
        self.live[slot] = True
        self.order[slot] = self._next_order
        self._next_order += 1
        # Same float64 expressions TerrainChunk was built with.
        self.world[slot] = (cx * chunk_size + offset_x, cz * chunk_size + offset_z)
        self.size[slot] = chunk_size
        self.dirty[slot] = True
        self.built[slot] = False
        self.lod[slot] = 0
        self.target_res[slot] = 0
        self.stable[slot] = 0
        self.grid_res[slot] = 0
        self.min_y[slot] = 0.0
        self.max_y[slot] = 0.0
        self.grass_dirty[slot] = True
        if self.seq[slot] & 1:
            self.seq[slot] += 1     # even: readable, though not built yet
        self.slot_of_coord[(int(cx), int(cz))] = slot
        self.count += 1
        return slot

    def ensure(self, cx, cz, chunk_size, offset_x, offset_z) -> int:
        """The slot for chunk ``(cx, cz)``, allocating it if absent."""
        slot = self.slot_of_coord.get((int(cx), int(cz)))
        if slot is None:
            slot = self._allocate(int(cx), int(cz), chunk_size, offset_x, offset_z)
        return slot

    def ensure_many(self, cx, cz, chunk_size, offset_x, offset_z) -> np.ndarray:
        """Ensure every ``(cx[i], cz[i])``, allocating the missing ones in order.

        Membership is one vectorised test; only chunks that are genuinely new
        cost any Python, and they are created in the order given -- the order
        the old nested loops inserted them into the dict.
        """
        cx = np.asarray(cx, dtype=np.int64)
        cz = np.asarray(cz, dtype=np.int64)
        if not len(cx):
            return _EMPTY
        live = self.live[:self._capacity]
        present = np.isin(pack_keys(cx, cz), self.key[:self._capacity][live])
        new = np.flatnonzero(~present)
        for i in new:
            self._allocate(int(cx[i]), int(cz[i]), chunk_size, offset_x, offset_z)
        return new

    def release(self, slots) -> list:
        """Free *slots*; returns them so the renderer can free its GL side."""
        freed = []
        for slot in np.asarray(slots, dtype=np.intp):
            slot = int(slot)
            if not self.live[slot]:
                continue
            if not self.seq[slot] & 1:
                self.seq[slot] += 1     # odd: a reader must not trust it
            self.live[slot] = False
            self.built[slot] = False
            del self.slot_of_coord[(int(self.coord[slot, 0]), int(self.coord[slot, 1]))]
            self._free.append(slot)
            self.count -= 1
            freed.append(slot)
        return freed

    def clear(self) -> list:
        return self.release(self.live_slots())

    # -- residency ----------------------------------------------------------

    def stream(self, cam_x, cam_z, chunk_size, radius, evict_padding,
               bounds, offset_x, offset_z) -> list:
        """Keep the ring around the camera resident; returns the evicted slots.

        Port of ``Terrain._stream_chunks``: ensure every in-bounds chunk whose
        nearest point is within *radius* of the camera (in the old x-major,
        z-minor order), then evict every chunk beyond ``radius +
        evict_padding``. *cam_x*/*cam_z* are world coordinates.
        """
        min_cx, max_cx, min_cz, max_cz = bounds
        cam_x = float(cam_x) - offset_x
        cam_z = float(cam_z) - offset_z
        cs = chunk_size
        keep = radius + evict_padding
        r2 = radius * radius
        keep2 = keep * keep

        cam_cx = int(math.floor(cam_x / cs))
        cam_cz = int(math.floor(cam_z / cs))
        reach = int(math.ceil(radius / cs)) + 1
        lo_x = max(min_cx, cam_cx - reach)
        hi_x = min(max_cx, cam_cx + reach)
        lo_z = max(min_cz, cam_cz - reach)
        hi_z = min(max_cz, cam_cz + reach)

        if lo_x <= hi_x and lo_z <= hi_z:
            gx, gz = np.meshgrid(np.arange(lo_x, hi_x + 1, dtype=np.int64),
                                 np.arange(lo_z, hi_z + 1, dtype=np.int64),
                                 indexing='ij')
            gx = gx.ravel()
            gz = gz.ravel()
            want = self._ring_dist_sq(gx, gz, cam_x, cam_z, cs) <= r2
            self.ensure_many(gx[want], gz[want], cs, offset_x, offset_z)

        slots = self.live_slots()
        far = self._ring_dist_sq(self.coord[slots, 0], self.coord[slots, 1],
                                 cam_x, cam_z, cs) > keep2
        return self.release(slots[far])

    @staticmethod
    def _ring_dist_sq(cx, cz, cam_x, cam_z, cs):
        """``_stream_chunks``'s nearest-point distance, over chunk indices."""
        min_x = cx * cs
        min_z = cz * cs
        nx = np.minimum(np.maximum(cam_x, min_x), min_x + cs)
        nz = np.minimum(np.maximum(cam_z, min_z), min_z + cs)
        dx = nx - cam_x
        dz = nz - cam_z
        return dx * dx + dz * dz

    def ensure_bounds(self, bounds, chunk_size, offset_x, offset_z):
        """Make every chunk inside *bounds* resident (the non-streaming mode).

        The old loop ran z-major, x-minor; so does the allocation order here.
        """
        min_cx, max_cx, min_cz, max_cz = bounds
        if min_cx > max_cx or min_cz > max_cz:
            return
        gz, gx = np.meshgrid(np.arange(min_cz, max_cz + 1, dtype=np.int64),
                             np.arange(min_cx, max_cx + 1, dtype=np.int64),
                             indexing='ij')
        self.ensure_many(gx.ravel(), gz.ravel(), chunk_size, offset_x, offset_z)

    def prune_out_of_bounds(self, bounds) -> list:
        min_cx, max_cx, min_cz, max_cz = bounds
        slots = self.live_slots()
        c = self.coord[slots]
        out = ((c[:, 0] < min_cx) | (c[:, 0] > max_cx) |
               (c[:, 1] < min_cz) | (c[:, 1] > max_cz))
        return self.release(slots[out])

    # -- per-frame geometry -------------------------------------------------

    def nearest_dist_sq(self, slots, cam_x, cam_z) -> np.ndarray:
        """Squared XZ distance from the camera to each chunk's nearest point."""
        cam_x = float(cam_x)
        cam_z = float(cam_z)
        min_x = self.world[slots, 0]
        min_z = self.world[slots, 1]
        size = self.size[slots]
        nx = np.minimum(np.maximum(cam_x, min_x), min_x + size)
        nz = np.minimum(np.maximum(cam_z, min_z), min_z + size)
        dx = nx - cam_x
        dz = nz - cam_z
        return dx * dx + dz * dz

    def visible(self, slots, frustum_planes) -> np.ndarray:
        """Which of *slots* are inside the frustum (its far plane included).

        Port of ``Terrain._is_chunk_visible``. A built chunk is tested with its
        measured height span; an unbuilt one at its true XZ footprint with an
        unbounded height, which can only err towards visible.
        """
        n = len(slots)
        if frustum_planes is None:
            return np.ones(n, dtype=bool)
        half = self.size[slots] / 2
        built = self.built[slots]
        wx = self.world[slots, 0]
        wz = self.world[slots, 1]
        lo = self.min_y[slots]
        hi = self.max_y[slots]
        cx = wx + self.size[slots] / 2
        cz = wz + self.size[slots] / 2
        # Built rows use the centre _upload_chunk recorded; unbuilt rows the
        # footprint. Both centres are the same x/z expression.
        cy = np.where(built, (lo + hi) / 2, 0.0)
        half_y = np.where(built, (hi - lo) / 2 + 10, 1.0e6)
        inside = np.ones(n, dtype=bool)
        for a, b, c, d in frustum_planes:
            px = np.where(a >= 0, cx + half, cx - half)
            py = np.where(b >= 0, cy + half_y, cy - half_y)
            pz = np.where(c >= 0, cz + half, cz - half)
            inside &= ~(a * px + b * py + c * pz + d < 0)
        return inside

    def schedule(self, slots, dist_sq, visible, near_detail_radius,
                 lod_resolutions, lod_distances_sq, hysteresis_frames):
        """Advance LOD hysteresis and return the build queue.

        Port of the scheduling half of the old per-chunk loop in
        ``Terrain.update_and_render``: the protected zone, the LOD bands, the
        hysteresis counters, and the queue sorted dirty-first, then visible,
        then nearest -- ties in allocation order, as the stable sort over the
        old dict gave them. Returns ``(queue_slots, queue_resolutions)``.
        """
        res = np.asarray(lod_resolutions, dtype=np.int32)
        size = self.size[slots]
        prewarm = near_detail_radius + size
        protected = dist_sq <= prewarm * prewarm
        band = np.searchsorted(np.asarray(lod_distances_sq, dtype=np.float64),
                               dist_sq, side='right')
        banded = res[np.minimum(band, len(res) - 1)]
        target = np.where(protected, res[0], banded)

        built = self.built[slots]
        dirty = self.dirty[slots]
        current = np.where(built, res[self.lod[slots].astype(np.intp)], 0)
        needs = dirty | ~built
        diff = ~needs & (current != target)

        promote = diff & protected
        retarget = diff & ~protected & (self.target_res[slots] != target)
        hold = diff & ~protected & ~retarget

        reset = promote | retarget
        self.target_res[slots[reset]] = target[reset]
        self.stable[slots[reset]] = 0
        held = slots[hold]
        self.stable[held] += 1
        ripe = hold.copy()
        ripe[hold] = self.stable[held] >= hysteresis_frames
        self.stable[slots[ripe]] = 0
        needs = needs | promote | ripe

        q = np.flatnonzero(needs)
        order = np.lexsort((self.order[slots[q]], dist_sq[q],
                            ~visible[q], ~dirty[q]))
        q = q[order]
        return slots[q], target[q]

    # -- heights --------------------------------------------------------------

    def store(self, slot, resolution, lod_index, heights):
        """Record a freshly built grid for *slot* (the chunk-building thread)."""
        n = resolution + 1
        self.seq[slot] += 1                         # odd: being written
        self.heights[slot, :n, :n] = heights
        self.grid_res[slot] = resolution
        self.min_y[slot] = float(heights.min())
        self.max_y[slot] = float(heights.max())
        self.lod[slot] = lod_index
        self.built[slot] = True
        self.dirty[slot] = False
        self.version[slot] += 1
        self.seq[slot] += 1                         # even: consistent

    def mark_all_dirty(self):
        live = self.live[:self._capacity]
        self.dirty[:self._capacity][live] = True
        self.grass_dirty[:self._capacity][live] = True

    def mark_grass_dirty(self):
        live = self.live[:self._capacity]
        self.grass_dirty[:self._capacity][live] = True

    def mark_dirty_region(self, world_x, world_z, radius):
        """Port of ``_mark_sculpt_region_dirty``: chunks overlapping a brush.

        The built heights stay in place until the rebuild, so collision keeps
        matching the surface on screen for the frame or two in between.
        """
        slots = self.live_slots()
        size = self.size[slots]
        cx = self.world[slots, 0] + size / 2
        cz = self.world[slots, 1] + size / 2
        half = size / 2 + radius
        hit = (np.abs(cx - world_x) < half) & (np.abs(cz - world_z) < half)
        self.dirty[slots[hit]] = True
        self.grass_dirty[slots[hit]] = True

    def height_at(self, cx, cz, world_x, world_z):
        """Height of the built surface at a world point in chunk ``(cx, cz)``.

        Interpolates over the same two triangles per quad the renderer draws
        -- (x0,z0)(x1,z0)(x0,z1) and (x1,z0)(x1,z1)(x0,z1) -- so collision is
        the visible surface, not an approximation of it. ``None`` when the
        chunk has no built grid, the point is outside it, or the row changed
        while it was being read (the caller falls back to the height function).

        Safe from any thread: the builder bumps the row's sequence number
        around every write and the table's generation around every resize.
        """
        slot = self.slot_of_coord.get((cx, cz))
        if slot is None:
            return None
        generation = self.generation
        seq = self.seq
        s1 = int(seq[slot])
        if s1 & 1 or not self.built[slot]:
            return None
        n = int(self.grid_res[slot])
        wx0 = float(self.world[slot, 0])
        wz0 = float(self.world[slot, 1])
        step = float(self.size[slot]) / n
        lx = (world_x - wx0) / step
        lz = (world_z - wz0) / step
        if not (0.0 <= lx <= n and 0.0 <= lz <= n):
            return None
        i = min(int(lx), n - 1)
        k = min(int(lz), n - 1)
        fx = lx - i
        fz = lz - k
        h = self.heights[slot]
        h10 = float(h[i + 1, k])
        h01 = float(h[i, k + 1])
        if fx + fz <= 1.0:
            h00 = float(h[i, k])
            result = h00 + fx * (h10 - h00) + fz * (h01 - h00)
        else:
            h11 = float(h[i + 1, k + 1])
            result = h11 + (1.0 - fx) * (h01 - h11) + (1.0 - fz) * (h10 - h11)
        if int(self.seq[slot]) != s1 or self.generation != generation:
            return None
        if int(self.coord[slot, 0]) != cx or int(self.coord[slot, 1]) != cz:
            return None
        return result

    # -- stats ----------------------------------------------------------------

    def triangle_count(self) -> int:
        """Triangles in every built grid: ``2 * res^2`` per chunk."""
        built = self.live[:self._capacity] & self.built[:self._capacity]
        res = self.grid_res[:self._capacity][built].astype(np.int64)
        return int((2 * res * res).sum())

    def resident_coords(self) -> list:
        """``[(cx, cz), ...]`` in allocation order -- for tests and tools."""
        slots = self.live_slots()
        return [(int(a), int(b)) for a, b in self.coord[slots]]
