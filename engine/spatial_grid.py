"""The world's spatial index: where everything in the world is, by 512-unit cell.

Engine core, and the single source of truth for where the world is during
play. Everything that asks "what is here?" asks this grid -- player movement,
monster AI, line of sight, and whichever physics is active
(:mod:`engine.physics`): none of them keeps a copy of the world.

What it holds:

* **solid brushes**, each filed under every cell its footprint touches --
  including ones a streaming layer has parked out of range, which come back,
  and excluding what the map itself hides (:func:`engine.spatial.authored_hidden`);
* **movers and doors**, filed for the whole of their travel and read where
  they are *now*: their boxes are re-derived when a placement stamp says
  something may have moved (:mod:`engine.brush_placement`), the parked and
  the off-range ones included;
* **true shapes**: an angled brush or a spinning one is filed by its box
  but is not a box, and :meth:`SpatialGrid.columns` answers for the solid it
  really is -- the surface a body stands on, the face it meets;
* **the terrain** (:meth:`SpatialGrid.set_terrain`), where the map has one
  and it is solid: its height at a point (:meth:`SpatialGrid.terrain_heights`);
* **water volumes** (:attr:`SpatialGrid.water_brushes`): not solid, kept
  apart, for whatever swims or floats;
* **line of sight's rows**, a view of the same buckets the AI thread can read
  while the logic thread works.

What it leaves out is what is not a place in the world: fog, triggers that do
not move, and dynamic bodies' own boxes, which the physics that moves them
answers for. The cell convention itself is :mod:`engine.spatial`'s.
"""
import math
import threading
from itertools import chain

import glm
import numpy as np

from . import brush_placement as _placement
from . import mesh_columns
from .brush_placement import collision_shape, shared_bulk_boxes, world_bounds
from .mover_table import travel_range
from .constants import is_water_brush, brush_aabb_bounds
from .spatial import CELL_SIZE, CellIndex, authored_hidden, cell_range

_chain = chain.from_iterable


class _LosRows:
    """Line of sight's view of a populated grid; see SpatialGrid._build_los_rows."""

    __slots__ = ('lo', 'hi', 'static_by_cell', 'moving_by_cell',
                 'brushes_by_cell', 'static_bounds_by_cell')

    def __init__(self, lo, hi, static_by_cell, moving_by_cell, brushes_by_cell,
                 static_bounds_by_cell=None):
        self.lo = lo
        self.hi = hi
        self.static_by_cell = static_by_cell
        self.moving_by_cell = moving_by_cell
        self.brushes_by_cell = brushes_by_cell
        #: coord -> ``(row, lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)`` per static
        #: brush: what the per-brush walk tests, resolved once per populate
        #: instead of once per brush per ray.
        self.static_bounds_by_cell = static_bounds_by_cell or {}


_EMPTY_LOS_ROWS = _LosRows(np.empty((0, 3)), np.empty((0, 3)), {}, {}, {}, {})


#: Cell coordinates packed into one int64 so a batch of queries can find its
#: cells with one ``searchsorted``. Coordinates are offset to be non-negative;
#: Cells outside this range use exact coordinate lookups instead of packing.
_CELL_OFFSET = 1 << 20
_CELL_STRIDE = 1 << 21


def _cell_keys(cx, cz):
    return ((np.asarray(cx, dtype=np.int64) + _CELL_OFFSET) * _CELL_STRIDE
            + (np.asarray(cz, dtype=np.int64) + _CELL_OFFSET))


def _vectors(vectors):
    """``(n, 3)`` float64 of *n* 3-vectors: one flat pass; any vector of
    another length falls back to reading each one's first three."""
    count = len(vectors)
    flat = np.fromiter(_chain(vectors), dtype=np.float64)
    if flat.size != 3 * count:
        flat = np.array([(v[0], v[1], v[2]) for v in vectors], dtype=np.float64)
    return flat.reshape(count, 3)


class _CellRows:
    """Every filed brush's box, grouped by cell, for batched point/box queries.

    The grid's buckets as flat arrays: ``keys`` (sorted packed cell coords),
    ``starts`` (``keys[i]``'s rows are ``rows[starts[i]:starts[i+1]]``) and
    ``lo``/``hi`` float64 boxes computed exactly as :meth:`SpatialGrid.raycast_down`
    and :meth:`SpatialGrid.overlaps_wall` compute them: each brush's derived
    bounds (:func:`engine.brush_placement.world_bounds`).
    Movers and doors keep their row, re-derived when a placement stamp says
    something may have moved (:meth:`refresh_movers`).
    """

    __slots__ = ('keys', 'starts', 'rows', 'lo', 'hi', 'movers', 'brushes',
                 '_unpacked_cells', '_mover_rows', '_seen', '_lock', '_mover_boxes',
                 '_mover_at', '_asleep', 'shaped')

    def __init__(self, cells):
        # Packing is unique only inside the documented coordinate range.  A
        # distant cell must not alias a nearer bucket, or overflow int64 while
        # a grid is populated.  Such worlds keep exact Python-integer keys.
        packable = all(-_CELL_OFFSET <= cx < _CELL_OFFSET
                       and -_CELL_OFFSET <= cz < _CELL_OFFSET
                       for cx, cz in cells)
        order = (sorted(cells.items(),
                        key=lambda kv: int(_cell_keys(kv[0][0], kv[0][1])))
                 if packable else cells.items())
        self._unpacked_cells = None if packable else {}
        row_of = {}
        brushes = []
        keys = []
        starts = [0]
        rows = []
        for (cx, cz), bucket in order:
            if packable:
                keys.append(int(_cell_keys(cx, cz)))
            first = len(rows)
            for brush in bucket:
                row = row_of.get(id(brush))
                if row is None:
                    row = row_of[id(brush)] = len(brushes)
                    brushes.append(brush)
                rows.append(row)
            if not packable:
                self._unpacked_cells[(cx, cz)] = tuple(rows[first:])
            starts.append(len(rows))
        self.keys = np.asarray(keys, dtype=np.int64)
        self.starts = np.asarray(starts, dtype=np.int64)
        self.rows = np.asarray(rows, dtype=np.int64)
        # Every brush's derived bounds (world_bounds): a plain box's pos -/+
        # size/2 read in bulk (one flat pass per column), then the few that
        # need more -- planes, a mesh, a runtime transform -- in place.
        count = len(brushes)
        centre = _vectors([b['pos'] for b in brushes])
        half = _vectors([b['size'] for b in brushes]) * 0.5
        self.lo = centre - half
        self.hi = centre + half
        #: row -> the brush, for the per-brush predicates a caller still asks.
        self.brushes = brushes
        #: row -> whether the brush may be something other than its box: an
        #: angled (convex) brush, or a mover or door, which may be turning.
        #: What it is now is asked of it (SpatialGrid.columns).
        self.shaped = np.fromiter(
            ('geometry' in b or bool(b.get('is_mover') or b.get('is_door'))
             or b.get('_collision_mode') == 'mesh'
             for b in brushes), dtype=np.bool_, count=count)
        self.movers = []
        for row, brush in enumerate(brushes):
            if ('geometry' in brush or '_mesh_bounds' in brush
                    or 'is_mover' in brush or 'is_door' in brush):
                if brush.get('is_mover') or brush.get('is_door'):
                    self.movers.append((row, brush))
                elif 'geometry' not in brush and '_mesh_bounds' not in brush:
                    continue            # a flag set false: a plain box
                self._resolve(row, brush)
        self._mover_rows = np.asarray([row for row, _brush in self.movers],
                                      dtype=np.intp)
        #: The placement stamps the mover rows were last derived at; the
        #: constructor just derived them, but at no recorded stamp.
        self._seen = None
        #: ``(brush_placement.asleep_version, rows)``: the mover rows that
        #: are parked (see _resolve_asleep).
        self._asleep = None
        self._lock = threading.Lock()
        # The gather shared with the collision rows (the same movers).
        self._mover_boxes, self._mover_rows, self._mover_at = shared_bulk_boxes(
            [brush for _row, brush in self.movers], self._mover_rows)

    def _resolve(self, row, brush):
        # Planes, a mesh or a runtime transform: the resolver's bounds.
        bounds = world_bounds(brush)
        self.lo[row] = bounds[0:3]
        self.hi[row] = bounds[3:6]

    def refresh_movers(self):
        """Re-derive the movers' and doors' rows if anything they are derived
        from may have changed (see :meth:`CollisionRows.refresh_movers`): two
        integer reads when nothing has. Both the logic thread and the AI
        thread query these rows, so the work is done under a lock and the
        stamps it was done at are published last, as one tuple."""
        movers = self.movers
        if not movers:
            return
        seen = self._seen
        if (seen is not None and seen[0] == _placement.runtime_generation
                and seen[1] == _placement.authored_generation):
            return
        with self._lock:
            stamps = (_placement.runtime_generation, _placement.authored_generation)
            if self._seen == stamps:
                return
            # Gathered from the tables that hold them (brush_placement.BulkBoxes);
            # world_bounds' arithmetic: centre -/+ half.
            centre, half = self._mover_boxes.boxes()
            rows = self._mover_rows
            if self._mover_at is not None:
                centre, half = centre[self._mover_at], half[self._mover_at]
            self.lo[rows] = centre - half
            self.hi[rows] = centre + half
            self._seen = stamps

    def pairs(self, cx, cz):
        """``(query, row)`` for every brush filed in each query's cell, and
        every off-range mover or door (brush_placement.off_range) for every
        query: one may be anywhere."""
        query, rows = self._pairs_filed(cx, cz)
        off = _off_range_filed()
        if off and len(cx):
            row_of = {id(brush): row for row, brush in self.movers}
            extra = [row_of[id(b)] for b in off if id(b) in row_of]
            if extra:
                count = len(cx)
                query = np.concatenate((query, np.repeat(np.arange(count, dtype=np.int64),
                                                         len(extra))))
                rows = np.concatenate((rows, np.tile(np.asarray(extra, dtype=np.int64), count)))
        if _placement.asleep and len(rows):
            self._resolve_asleep(rows)
        return query, rows

    def _resolve_asleep(self, rows):
        """The parked movers and doors among *rows* (brush_placement.asleep):
        nothing ticks them, so their boxes -- as the table last stored them
        -- are worked out again for now, for the rows a query reaches."""
        key = _placement.asleep_version
        held = self._asleep
        if held is None or held[0] != key:
            asleep = _placement.asleep
            mine = [row for row, brush in self.movers if id(brush) in asleep]
            held = self._asleep = (key, np.asarray(mine, dtype=np.int64))
        if not len(held[1]):
            return
        hit = np.unique(rows[np.isin(rows, held[1])])
        if not len(hit):
            return
        with self._lock:
            for row in hit.tolist():
                bounds = world_bounds(self.brushes[row])
                self.lo[row] = bounds[0:3]
                self.hi[row] = bounds[3:6]

    def _pairs_filed(self, cx, cz):
        count = len(cx)
        if not len(self.rows) or not count:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty
        if (self._unpacked_cells is not None
                or not (-_CELL_OFFSET <= cx.min()
                        and cx.max() < _CELL_OFFSET
                        and -_CELL_OFFSET <= cz.min()
                        and cz.max() < _CELL_OFFSET)):
            return self._pairs_unpacked(cx, cz)
        keys = _cell_keys(cx, cz)
        at = np.searchsorted(self.keys, keys)
        clipped = np.minimum(at, len(self.keys) - 1)
        found = (at < len(self.keys)) & (self.keys[clipped] == keys)
        first = np.where(found, self.starts[clipped], 0)
        counts = np.where(found, self.starts[clipped + 1] - first, 0)
        total = int(counts.sum())
        query = np.repeat(np.arange(count, dtype=np.int64), counts)
        within = np.arange(total, dtype=np.int64) - np.repeat(
            np.cumsum(counts) - counts, counts)
        return query, self.rows[np.repeat(first, counts) + within]

    def _pairs_spanning(self, x0, z0, x1, z1):
        """``(query, row)`` for every brush filed in a cell of each query's
        cell range ``x0..x1, z0..z1`` but its first (``x0, z0``, which
        :meth:`pairs` answers): every filed cell tested against the range, a
        bounded slice of queries at a time. For a range far wider than a
        cell walk should go (:data:`MAX_WALK_CELLS`): this costs as the
        filed cells do, not as the cells the range covers."""
        empty = np.empty(0, dtype=np.int64)
        cells = len(self.starts) - 1
        if cells <= 0 or not len(x0):
            return empty, empty
        if self._unpacked_cells is not None:
            coords = np.asarray(list(self._unpacked_cells), dtype=np.float64).reshape(-1, 2)
            cx, cz = coords[:, 0], coords[:, 1]
        else:
            cx = self.keys // _CELL_STRIDE - _CELL_OFFSET
            cz = self.keys % _CELL_STRIDE - _CELL_OFFSET
        queries, found = [], []
        chunk = max(1, (1 << 20) // cells)
        for start in range(0, len(x0), chunk):
            ax, az = x0[start:start + chunk, None], z0[start:start + chunk, None]
            inside = ((cx >= ax) & (cx <= x1[start:start + chunk, None])
                      & (cz >= az) & (cz <= z1[start:start + chunk, None])
                      & ~((cx == ax) & (cz == az)))
            query, cell = np.nonzero(inside)
            if not query.size:
                continue
            first = self.starts[cell]
            counts = self.starts[cell + 1] - first
            total = int(counts.sum())
            within = np.arange(total, dtype=np.int64) - np.repeat(
                np.cumsum(counts) - counts, counts)
            queries.append(np.repeat(query + start, counts))
            found.append(self.rows[np.repeat(first, counts) + within])
        if not queries:
            return empty, empty
        return np.concatenate(queries), np.concatenate(found)

    def _pairs_unpacked(self, cx, cz):
        """Rare large-coordinate queries, retaining query and bucket order."""
        query = []
        rows = []
        exact = self._unpacked_cells
        for index, (x, z) in enumerate(zip(cx, cz)):
            if ((isinstance(x, (float, np.floating)) and not math.isfinite(x))
                    or (isinstance(z, (float, np.floating)) and not math.isfinite(z))):
                continue
            coord = (int(x), int(z))
            if exact is not None:
                bucket = exact.get(coord, ())
            elif (-_CELL_OFFSET <= coord[0] < _CELL_OFFSET
                  and -_CELL_OFFSET <= coord[1] < _CELL_OFFSET):
                key = ((coord[0] + _CELL_OFFSET) * _CELL_STRIDE
                       + coord[1] + _CELL_OFFSET)
                at = int(np.searchsorted(self.keys, key))
                if at == len(self.keys) or self.keys[at] != key:
                    continue
                bucket = self.rows[self.starts[at]:self.starts[at + 1]]
            else:
                continue
            query.extend([index] * len(bucket))
            rows.extend(bucket)
        return (np.asarray(query, dtype=np.int64),
                np.asarray(rows, dtype=np.int64))


_EMPTY_CELL_ROWS = _CellRows({})


#: The most cells one mover's or door's travel is filed under.  A travel is
#: filed in every cell it can reach so that a query finds the brush with one
#: bucket lookup; that is a few cells for any real door or lift, about 2,000
#: for a travel of a million units along an axis, and without a limit it is
#: every cell a typo'd ``distance`` spans.  Past this the brush is filed where
#: it stands and handled as off range (see :meth:`SpatialGrid.populate`).
MAX_TRAVEL_CELLS = 1 << 16


#: The most cells one :meth:`SpatialGrid.box_pairs` box is walked cell by
#: cell: eight square, where a body's move at its fastest (Soma's
#: ``MAX_SPEED``) spans five. Each cell is a lookup for every box at once, so
#: the walk is cheap for boxes that are all small; a box wider than this --
#: a vast body, or bad data -- would make it cost as the square of its width,
#: and has every filed cell tested against it instead
#: (:meth:`_CellRows._pairs_spanning`). Both find the same pairs.
MAX_WALK_CELLS = 64


def _travel_cells(box, cell_size):
    """How many cells the XZ footprint of *box* overlaps."""
    cx0, cz0, cx1, cz1 = cell_range(box[0], box[2], box[3], box[5], cell_size)
    return (cx1 - cx0 + 1) * (cz1 - cz0 + 1)


def _off_range_filed():
    """The movers and doors that may be outside the travel range the grid
    filed them for (brush_placement.off_range): every query adds them, as
    they may be anywhere. Empty in most levels."""
    off, moved = _placement.off_range, _placement.overridden
    if not (off or moved):
        return ()
    found = {id(b): b for b in off.values() if '_travel_filed' in b}
    found.update((id(b), b) for b in moved.values() if '_travel_filed' in b)
    return tuple(found.values())


def _off_range_overlaps(m_xmin, m_ymin, m_zmin, m_xmax, m_ymax, m_zmax):
    for brush in _off_range_filed():
        bx_min, by_min, bz_min, bx_max, by_max, bz_max = world_bounds(brush)
        if (m_xmax > bx_min and m_xmin < bx_max and m_ymax > by_min and m_ymin < by_max
                and m_zmax > bz_min and m_zmin < bz_max):
            return True
    return False


class _BoxRows:
    """What the per-query wall and floor tests read: for each cell, its
    static brushes' float64 boxes as plain tuples ``(id, x0, y0, z0, x1, y1,
    z1)`` -- copied from :class:`_CellRows`, so the per-query and the batched
    tests read the same numbers -- and its movers and doors, resolved live.
    Built with the cell rows and again when ``authored_generation`` moves
    (an editor edit during play), so no query classifies a brush or works
    out a static box again."""

    __slots__ = ('stamp', 'statics', 'moving')

    def __init__(self, cells, cell_rows, stamp):
        self.stamp = stamp
        lo = cell_rows.lo.tolist()
        hi = cell_rows.hi.tolist()
        row_of = {id(brush): row for row, brush in enumerate(cell_rows.brushes)}
        statics, moving = {}, {}
        for coord, bucket in cells.items():
            still, live = [], []
            for brush in bucket:
                if brush.get('is_mover') or brush.get('is_door'):
                    live.append(brush)
                    continue
                row = row_of[id(brush)]
                a, b = lo[row], hi[row]
                still.append((id(brush), a[0], a[1], a[2], b[0], b[1], b[2]))
            if still:
                statics[coord] = tuple(still)
            if live:
                moving[coord] = tuple(live)
        self.statics = statics
        self.moving = moving


_EMPTY_BOX_ROWS = _BoxRows({}, _EMPTY_CELL_ROWS, None)


#: A plane this close to vertical bounds a column's sides, not its ends.
_UPRIGHT = 1e-6


def _world_planes(brush):
    """*brush*'s true shape as world-space planes ``(n, 4)`` float64 -- unit
    normal and offset, inside where ``n . p <= d`` -- where it is now, or None
    when its box is its shape (a still box, a mesh proxy).

    Cached on the brush against the collision shape it was derived from,
    which is itself rebuilt only when the brush moves or its geometry is
    edited (:func:`engine.brush_placement.collision_shape`).
    """
    shape = collision_shape(brush)
    if shape is None:
        return None
    planes, _triangles, _bounds, frame = shape
    if not planes:
        return None
    cached = brush.get('_world_planes')
    if cached is not None and cached[0] is shape:
        return cached[1]
    local = np.asarray(planes, dtype=np.float64).reshape(-1, 4)
    normal = local[:, :3]
    offset = local[:, 3]
    if frame is not None:
        # s = A + R^T (w - P): a plane n . s <= d is (R n) . w <= d - n . A + (R n) . P
        offset = offset - normal @ np.array((frame.ax, frame.ay, frame.az))
        if frame.r is not None:
            turn = np.asarray(frame.r, dtype=np.float64).reshape(3, 3)
            normal = normal @ turn.T
        offset = offset + normal @ np.array((frame.px, frame.py, frame.pz))
    world = np.column_stack((normal, offset))
    brush['_world_planes'] = (shape, world)
    return world


def _column(planes, x, z):
    """The solid column of a convex shape (world *planes*) over each point:
    ``(bottom, top)``, NaN where the vertical line at the point misses it."""
    nx = planes[:, 0][:, None, None]
    ny = planes[:, 1][:, None, None]
    nz = planes[:, 2][:, None, None]
    d = planes[:, 3][:, None, None]
    across = d - nx * x[None] - nz * z[None]            # (planes, points...)
    up = planes[:, 1] > _UPRIGHT
    down = planes[:, 1] < -_UPRIGHT
    side = ~(up | down)
    top = np.full(x.shape, np.inf)
    bottom = np.full(x.shape, -np.inf)
    if up.any():
        top = (across[up] / ny[up]).min(axis=0)
    if down.any():
        bottom = (across[down] / ny[down]).max(axis=0)
    miss = top < bottom
    if side.any():
        miss |= (across[side] < 0.0).any(axis=0)
    top = np.where(miss, np.nan, top)
    bottom = np.where(miss, np.nan, bottom)
    return bottom, top


class SpatialGrid:
    """
    A 2D spatial partitioning grid to optimize collision detection.
    Groups solid brushes into cells to reduce O(N) collision checks.

    Used by:
      - Player physics  (get_potential_colliders)
      - MonsterAI       (get_nearby_brushes, raycast_down, overlaps_wall, line_of_sight)

    The cell convention itself (512-unit integer columns, ``floor(coord/size)``,
    an object referenced from every cell it spans) lives in
    :mod:`engine.spatial` so this grid and Big World's streaming lifecycle agree
    about which cell a brush is in by construction rather than by coincidence.
    ``self.cells`` is that index's bucket dict, read directly by the query
    methods below — no wrapper sits on the collision hot path.
    """
    #: Candidate brushes a ray must have before line of sight tests them as
    #: dense rows instead of walking them in Python.
    #:
    #: The dense narrow phase runs about forty NumPy operations per ray, and
    #: their dispatch does not care how many rows they touch: it costs tens of
    #: microseconds a ray whether the ray has twenty candidates or two
    #: thousand. The scalar walk costs per brush, and exits on the first one
    #: that blocks. Neither is faster in general -- one is a fixed fee, the
    #: other a per-brush price -- so the only question is how many candidates
    #: the ray has, and this number is where the answer changes.
    #:
    #: Measured over 240 rays, in two scenes (bare brushes, and the same
    #: densities inside a room, which changes how often the walk exits early):
    #:
    #: ===========  ===========  ==========  ===========  ==========
    #: cands/ray    scalar (A)   dense (A)   scalar (B)   dense (B)
    #: ===========  ===========  ==========  ===========  ==========
    #:         13        2.1 ms     13.6 ms            -           -
    #:         39        5.0 ms     14.2 ms            -           -
    #:        185             -           -      10.2 ms    17.7 ms
    #:        272             -           -      14.4 ms    17.7 ms
    #:        341       36.0 ms     18.4 ms            -           -
    #:        355             -           -      19.8 ms    18.6 ms
    #:        500       52.5 ms     19.9 ms            -           -
    #:        935      105.4 ms     26.1 ms            -           -
    #: ===========  ===========  ==========  ===========  ==========
    #:
    #: The two scenes cross over in different places -- somewhere between 270
    #: and 400 -- and inside that range the measurement is not stable enough to
    #: pick a point from, so this is chosen by which mistake is cheaper rather
    #: than by averaging. Set too low, the worst case seen is 1.2x; set too
    #: high, 2x. Hence the low end of the band.
    #:
    #: What the number actually guards is much further down. Exact cell
    #: traversal brought a real AI tick to ~27 candidates per ray, an order of
    #: magnitude below anything here, and at that size the fixed fee made the
    #: dense path 3.3x slower than the walk it was meant to beat.
    #:
    #: Re-measure with ``tests/performance/test_los_threshold_benchmark.py``.
    #: Set it on the class or an instance to try another value; both paths
    #: answer identically, so it is purely a speed knob, and zero -- always
    #: dense -- is a legal setting.
    LOS_DENSE_MIN_CANDIDATES = 256

    def __init__(self, cell_size=CELL_SIZE):
        self.cell_size = cell_size
        self._index = CellIndex(cell_size)
        self.cells = self._index.cells
        self._all_solid = []          # flat list kept for ray queries that span many cells
        self.water_brushes = []       # non-solid water volumes, for swim physics queries
        # What line of sight reads, built once per populate: see _build_los_rows.
        self._los_rows = _EMPTY_LOS_ROWS
        # What the batched point/box queries read, built with it.
        self._cell_rows = _EMPTY_CELL_ROWS
        self._cell_stamp = None
        # What the per-query wall and floor tests read (see _BoxRows).
        self._box_rows = _EMPTY_BOX_ROWS
        # The map's terrain, or a callable giving it (see set_terrain).
        self._terrain = None
        # Bumped by populate/clear: half of world_version (shape queries).
        self._generation = 0
        self._shape_keys = {}

    def set_terrain(self, terrain):
        """The map's terrain, for :meth:`terrain_heights`: the terrain, a
        zero-argument callable returning it (a session's can be replaced
        while it runs), or None."""
        self._terrain = terrain

    @property
    def terrain(self):
        """The terrain bodies can stand on now: the one set, if it is enabled
        and solid; None otherwise."""
        terrain = self._terrain
        if callable(terrain):
            terrain = terrain()
        if terrain is None or not terrain.is_solid():
            return None
        return terrain

    def terrain_heights(self, x, z):
        """The terrain's height at each point ``(x[i], z[i])``: the built
        surface the renderer draws, as the player stands on it
        (``Terrain.get_heights_at_safe``). NaN outside the terrain; None when
        the map has no solid terrain, so a world without one pays one
        attribute read."""
        terrain = self.terrain
        if terrain is None:
            return None
        return terrain.get_heights_at_safe(x, z)

    # ------------------------------------------------------------------
    # Shapes (WORLD_QUERIES): the world for a physics fed shapes up front
    # ------------------------------------------------------------------

    @property
    def world_version(self):
        """Changes whenever the set of solid shapes or any shape's authored
        form changes (a populate, an edit): ask :meth:`solid_shapes` again
        then. A mover or door moving does not change it (:meth:`shape_pose`)."""
        return (self._generation, _placement.authored_generation)

    def solid_shapes(self):
        """Every solid brush once: ``[(key, kind, data, centre, moves)]``.

        *key* is an opaque int for :meth:`shape_pose`; *centre* the shape's
        authored centre, the origin *data* is given about. *kind* is
        ``'box'`` (*data*: half extents), ``'convex'`` (*data*: its vertices)
        or ``'mesh'`` (*data*: its triangles, three vertices each). *moves*:
        a mover or door, whose pose changes in play."""
        shapes = []
        keyed = {}
        for brush in self._all_solid:
            box = _placement.authored_box(brush)
            centre = (float(box[0]), float(box[1]), float(box[2]))
            triangles = (brush.get('_mesh_triangles')
                         if brush.get('_collision_mode') == 'mesh' else None)
            convex = None if triangles is not None else _placement.shape_convex(brush)
            if triangles is not None and len(triangles):
                kind = 'mesh'
                data = tuple(
                    tuple(tuple(float(v[i]) - centre[i] for i in range(3)) for v in tri)
                    for tri in (item[0] if hasattr(item[0][0], '__len__') else item
                                for item in triangles))
            elif convex is not None:
                kind = 'convex'
                local = convex.shape_box
                data = tuple(tuple(float(p[i]) - local[i] for i in range(3))
                             for p in convex.verts)
            else:
                kind = 'box'
                data = (float(box[3]), float(box[4]), float(box[5]))
            keyed[id(brush)] = brush
            shapes.append((id(brush), kind, data, centre, _placement.is_dynamic(brush)))
        self._shape_keys = keyed
        return shapes

    def shape_pose(self, key):
        """``(centre, axis, angle_degrees)``: where a :meth:`solid_shapes`
        shape is now, turned by *angle* about *axis* through *centre*; None
        for a key the world no longer has."""
        brush = self._shape_keys.get(key)
        if brush is None:
            return None
        runtime = _placement.resolved_runtime(brush)
        if runtime is None:
            box = _placement.authored_box(brush)
            return ((float(box[0]), float(box[1]), float(box[2])), (0.0, 1.0, 0.0), 0.0)
        return ((float(runtime[0]), float(runtime[1]), float(runtime[2])),
                (float(runtime[3]), float(runtime[4]), float(runtime[5])),
                float(runtime[6]))

    @staticmethod
    def prepare_mesh(triangles):
        """A physics body's ``_mesh_triangles`` made ready for
        :meth:`mesh_columns`, or None when none is usable (WORLD_QUERIES)."""
        return mesh_columns.prepare(triangles)

    @staticmethod
    def mesh_columns(mesh, x, z, shift=(0.0, 0.0, 0.0)):
        """``(bottom, top)`` of a :meth:`prepare_mesh` surface over each point,
        moved by *shift* since it was prepared; NaN where a point misses it."""
        return mesh_columns.column(mesh, x, z, shift)

    def columns(self, rows, row, x, z):
        """``(bottom, top)``: the solid column of brush ``row[k]`` over each
        point ``(x[k, j], z[k, j])`` -- the heights its solid starts and ends
        at there, NaN where the column misses it.

        A box is its box. An angled brush, or one a mover is turning, is the
        solid its planes enclose (:func:`engine.brush_placement.collision_shape`,
        where it is now): the surface something stands on is ``top``, lower
        than the top of the box it is filed by wherever it slopes. *rows* is
        the rows object a broad phase returned (:meth:`box_pairs`).

        The work is one pass per distinct brush asked about, over all its
        points at once.
        """
        row = np.asarray(row, dtype=np.int64)
        x = np.asarray(x, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        bottom = np.full(x.shape, np.nan)
        top = np.full(x.shape, np.nan)
        if not len(row):
            return bottom, top
        lo = rows.lo[row]
        hi = rows.hi[row]
        inside = ((lo[:, 0:1] <= x) & (x <= hi[:, 0:1])
                  & (lo[:, 2:3] <= z) & (z <= hi[:, 2:3]))
        bottom[inside] = np.broadcast_to(lo[:, 1:2], x.shape)[inside]
        top[inside] = np.broadcast_to(hi[:, 1:2], x.shape)[inside]
        shaped = rows.shaped[row]
        if not shaped.any():
            return bottom, top
        brushes = rows.brushes
        for brush_row in np.unique(row[shaped]).tolist():
            brush = brushes[brush_row]
            at = np.flatnonzero(row == brush_row)
            if brush.get('_collision_mode') == 'mesh':
                # A Prop whose Collision Shape is Mesh: its model's triangles
                # (engine.mesh_columns), prepared once and kept on the proxy.
                mesh = brush.get('_mesh_columns')
                if mesh is None:
                    mesh = brush['_mesh_columns'] = (
                        mesh_columns.prepare(brush.get('_mesh_triangles')),)
                if mesh[0] is None:
                    continue                # nothing usable: its box
                b, t = mesh_columns.column(mesh[0], x[at], z[at])
                bottom[at] = b
                top[at] = t
                continue
            planes = _world_planes(brush)
            if planes is None:
                continue                    # its box is its shape
            b, t = _column(planes, x[at], z[at])
            bottom[at] = b
            top[at] = t
        return bottom, top

    def clear(self):
        self._generation += 1
        self._shape_keys = {}
        self.cells.clear()
        self._all_solid.clear()
        self.water_brushes.clear()
        self._los_rows = _EMPTY_LOS_ROWS
        self._cell_rows = _EMPTY_CELL_ROWS
        self._cell_stamp = None
        self._box_rows = _EMPTY_BOX_ROWS

    def _boxes(self):
        """The current :class:`_BoxRows`: rebuilt, with the cell rows, after
        an authored edit (one integer read when there has been none)."""
        rows = self._box_rows
        stamp = _placement.authored_generation
        if rows.stamp != stamp:
            cell_rows = self._cell_rows
            if self._cell_stamp != stamp:
                cell_rows = _CellRows(self.cells)
                self._cell_rows = cell_rows
                self._cell_stamp = stamp
            rows = _BoxRows(self.cells, cell_rows, stamp)
            self._box_rows = rows
        return rows

    def _rows_now(self):
        """The current :class:`_CellRows`, for a batched query: rebuilt after
        an authored edit (as :meth:`_boxes` rebuilds the per-query rows, so
        both read the same world), its movers and doors where they are now.
        Two integer reads when nothing has changed."""
        rows = self._cell_rows
        stamp = _placement.authored_generation
        if self._cell_stamp != stamp:
            rows = _CellRows(self.cells)
            self._cell_rows = rows
            self._cell_stamp = stamp
        rows.refresh_movers()
        return rows

    # ------------------------------------------------------------------
    # Line-of-sight rows
    # ------------------------------------------------------------------

    def _build_los_rows(self):
        """The buckets as line of sight reads them: static rows plus movers.

        Line of sight runs on the monster AI thread, so it must not read
        anything the logic thread rewrites -- in particular not a render
        table, which the logic thread refreshes and reconciles every tick and
        which, double-buffered, is recycled one publication after the AI saw
        it. What it reads instead is split by how often it can change:

        * a **static** brush cannot move during play, so its AABB is copied
          here once, straight from :func:`brush_aabb_bounds` -- the exact
          tuple the per-brush walk tests, so the dense narrow phase over these
          rows is bit-for-bit the same test;
        * a **mover or door** is kept as the brush itself and always walked
          per brush, reading ``pos`` live, which is what makes a prop riding a
          platform see its current height.

        Everything is held in fresh containers published by one assignment at
        the end of :meth:`populate`, so a ray in flight on the AI thread keeps
        the rows it started with even if the grid is rebuilt under it.
        """
        row_of = {}
        bounds = []
        static_by_cell = {}
        static_bounds_by_cell = {}
        moving_by_cell = {}
        brushes_by_cell = {}
        for coord, bucket in self.cells.items():
            brushes_by_cell[coord] = tuple(bucket)
            rows = []
            moving = []
            for brush in bucket:
                if brush.get('is_mover') or brush.get('is_door'):
                    moving.append(brush)
                    continue
                row = row_of.get(id(brush))
                if row is None:
                    row = row_of[id(brush)] = len(bounds)
                    bounds.append(brush_aabb_bounds(brush))
                rows.append(row)
            if rows:
                static_by_cell[coord] = np.asarray(rows, dtype=np.intp)
                static_bounds_by_cell[coord] = tuple(
                    (row,) + bounds[row] for row in rows)
            if moving:
                moving_by_cell[coord] = tuple(moving)
        box = np.asarray(bounds, dtype=np.float64).reshape(-1, 6)
        return _LosRows(np.ascontiguousarray(box[:, :3]),
                        np.ascontiguousarray(box[:, 3:]),
                        static_by_cell, moving_by_cell, brushes_by_cell,
                        static_bounds_by_cell)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def populate(self, brushes):
        """Builds the grid from a list of brushes.  Call once on play-mode enter
        and again whenever the static brush list changes (rare).

        Line of sight's rows are derived from the buckets here, at the end,
        and published in one assignment (:meth:`_build_los_rows`)."""
        # Buckets only: line of sight keeps the rows it has until the new ones
        # are published below.
        self._generation += 1
        self._shape_keys = {}
        self.cells.clear()
        self._all_solid.clear()
        self.water_brushes.clear()
        for brush in brushes:
            # `authored_hidden` rather than `hidden`: a cell-streaming layer
            # parks out-of-range brushes by hiding them, and this grid outlives
            # that — rebuilding it mid-play (a model-collision toggle) would
            # otherwise drop every parked brush from collision for good, even
            # after its cell came back.
            if authored_hidden(brush) or brush.get('is_fog'):
                continue
            # Physics bodies are simulated by the engine physics world rather than the static grid.
            if brush.get('_physics_body'):
                continue
            if is_water_brush(brush):
                self.water_brushes.append(brush)
                continue

            is_dynamic = brush.get('is_mover') or brush.get('is_door')
            if brush.get('is_trigger') and not is_dynamic:
                continue

            self._all_solid.append(brush)

            # Filed by its derived bounds (brush_placement): an angled
            # brush's planes -- one rule in the editor and in play. A mover
            # or door is filed for its whole travel (mover_table.travel_range:
            # every centre its travel can give it, widened by its box, or by
            # its box's radius if it turns) and for where it is now, so every
            # cell it can reach finds it; the range it was filed for is kept
            # with it, and a row that leaves it (a path, a restored placement,
            # an edit in play) is marked off range by the mover table.
            # (Membership tests first: most brushes carry none of these keys,
            # and world_bounds itself handles a flag that is set false.)
            if is_dynamic:
                lo, hi, turns, reach = travel_range(brush)
                lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = world_bounds(brush)
                # Everywhere its box can be on its travel (and where it is).
                box = (min(lo_x, lo[0] - reach[0]), min(lo_y, lo[1] - reach[1]),
                       min(lo_z, lo[2] - reach[2]), max(hi_x, hi[0] + reach[0]),
                       max(hi_y, hi[1] + reach[1]), max(hi_z, hi[2] + reach[2]))
                if _travel_cells(box, self.cell_size) > MAX_TRAVEL_CELLS:
                    # A travel too long to file cell by cell (a `distance`
                    # of 1e9 is millions of buckets, and exhausted memory at
                    # Play start): filed where it stands instead, and the
                    # range recorded is that one, so the brush is off range
                    # (brush_placement.off_range, MoverTable.check_range)
                    # and every query adds it wherever it looks -- the
                    # route a path follower already takes.
                    centre = _placement.authored_box(brush)[:3]
                    margin = 1.0 + 1e-6 * max(abs(float(v)) for v in centre)
                    lo = tuple(float(v) - margin for v in centre)
                    hi = tuple(float(v) + margin for v in centre)
                    box = (min(lo_x, lo[0] - reach[0]), min(lo_y, lo[1] - reach[1]),
                           min(lo_z, lo[2] - reach[2]), max(hi_x, hi[0] + reach[0]),
                           max(hi_y, hi[1] + reach[1]), max(hi_z, hi[2] + reach[2]))
                    if _travel_cells(box, self.cell_size) > MAX_TRAVEL_CELLS:
                        # Displaced that far already: where it is now, only.
                        box = (lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)
                    # Marked here as well: a mover that starts on has its
                    # state -- and its range check -- before the grid exists
                    # to be checked against. The mover table forgets the mark
                    # when play stops, as it does its own.
                    if id(brush) not in _placement.off_range:
                        _placement.off_range[id(brush)] = brush
                        _placement.note_off_range()
                brush['_travel_filed'] = (lo, hi, turns, box)
                self._index.insert(brush, box[0], box[2], box[3], box[5])
            elif ('geometry' in brush or '_mesh_bounds' in brush
                    or 'is_mover' in brush or 'is_door' in brush):
                lo_x, _lo_y, lo_z, hi_x, _hi_y, hi_z = world_bounds(brush)
                self._index.insert(brush, lo_x, lo_z, hi_x, hi_z)
            else:       # a plain box: the box rule, inline
                pos = brush['pos']
                size = brush['size']
                self._index.insert(brush,
                                   pos[0] - size[0] * 0.5, pos[2] - size[2] * 0.5,
                                   pos[0] + size[0] * 0.5, pos[2] + size[2] * 0.5)

        self._los_rows = self._build_los_rows()
        # The per-query box rows are built from these when first asked for.
        self._cell_rows = _CellRows(self.cells)
        self._cell_stamp = _placement.authored_generation
        self._box_rows = _EMPTY_BOX_ROWS

    # ------------------------------------------------------------------
    # Player queries  (unchanged API)
    # ------------------------------------------------------------------

    def get_potential_colliders(self, player_min, player_max):
        """Returns unique brushes sharing grid cells with the player's AABB."""
        min_x = int(math.floor(player_min.x / self.cell_size))
        max_x = int(math.floor(player_max.x / self.cell_size))
        min_z = int(math.floor(player_min.z / self.cell_size))
        max_z = int(math.floor(player_max.z / self.cell_size))

        # PERF: the overwhelmingly common case is an AABB inside one cell, where
        # no de-duplication is needed at all -- return that cell's bucket
        # directly. Elsewhere, `cells.get(...)` replaces the `in` + `[]` pair so
        # each cell costs one dict lookup instead of two.
        cells = self.cells
        if min_x == max_x and min_z == max_z:
            return list(cells.get((min_x, min_z), ()))

        colliders = []
        seen = set()
        for x in range(min_x, max_x + 1):
            for z in range(min_z, max_z + 1):
                for brush in cells.get((x, z), ()):
                    bid = id(brush)
                    if bid not in seen:
                        seen.add(bid)
                        colliders.append(brush)

        return colliders

    # ------------------------------------------------------------------
    # Monster queries (NEW)
    # ------------------------------------------------------------------

    def get_nearby_brushes(self, x, z, radius=0.0):
        """Return unique solid brushes in cells overlapping the point/radius."""
        min_cx = int(math.floor((x - radius) / self.cell_size))
        max_cx = int(math.floor((x + radius) / self.cell_size))
        min_cz = int(math.floor((z - radius) / self.cell_size))
        max_cz = int(math.floor((z + radius) / self.cell_size))

        cells = self.cells
        if min_cx == max_cx and min_cz == max_cz:
            return list(cells.get((min_cx, min_cz), ()))

        result = []
        seen = set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                for brush in cells.get((cx, cz), ()):
                    bid = id(brush)
                    if bid not in seen:
                        seen.add(bid)
                        result.append(brush)
        return result

    def overlaps_wall(self, mx, my, mz, margin):
        """Check if a monster-sized box at (mx, my, mz) overlaps any solid brush.
        Uses the grid to limit the search to nearby cells only.

        Static brushes are tested as the plain float64 boxes resolved for
        their cells (:class:`_BoxRows`, the same numbers the batched query
        reads); movers and doors are resolved now."""
        m_xmin = mx - margin
        m_xmax = mx + margin
        m_ymin = my
        m_ymax = my + 128.0
        m_zmin = mz - margin
        m_zmax = mz + margin

        min_cx = int(math.floor(m_xmin / self.cell_size))
        max_cx = int(math.floor(m_xmax / self.cell_size))
        min_cz = int(math.floor(m_zmin / self.cell_size))
        max_cz = int(math.floor(m_zmax / self.cell_size))

        rows = self._boxes()
        statics, moving = rows.statics, rows.moving
        if min_cx == max_cx and min_cz == max_cz:
            # Single-cell fast path: no de-duplication set needed.
            cell = (min_cx, min_cz)
            for _bid, bx_min, by_min, bz_min, bx_max, by_max, bz_max in statics.get(cell, ()):
                if (m_xmax > bx_min and m_xmin < bx_max and
                        m_ymax > by_min and m_ymin < by_max and
                        m_zmax > bz_min and m_zmin < bz_max):
                    return True
            for brush in moving.get(cell, ()):
                bx_min, by_min, bz_min, bx_max, by_max, bz_max = world_bounds(brush)
                if (m_xmax > bx_min and m_xmin < bx_max and
                        m_ymax > by_min and m_ymin < by_max and
                        m_zmax > bz_min and m_zmin < bz_max):
                    return True
            return _off_range_overlaps(m_xmin, m_ymin, m_zmin, m_xmax, m_ymax, m_zmax)

        seen = set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                cell = (cx, cz)
                for bid, bx_min, by_min, bz_min, bx_max, by_max, bz_max in statics.get(cell, ()):
                    if bid in seen:
                        continue
                    seen.add(bid)
                    if (m_xmax > bx_min and m_xmin < bx_max and
                            m_ymax > by_min and m_ymin < by_max and
                            m_zmax > bz_min and m_zmin < bz_max):
                        return True
                for brush in moving.get(cell, ()):
                    bid = id(brush)
                    if bid in seen:
                        continue
                    seen.add(bid)
                    bx_min, by_min, bz_min, bx_max, by_max, bz_max = world_bounds(brush)
                    if (m_xmax > bx_min and m_xmin < bx_max and
                            m_ymax > by_min and m_ymin < by_max and
                            m_zmax > bz_min and m_zmin < bz_max):
                        return True
        return _off_range_overlaps(m_xmin, m_ymin, m_zmin, m_xmax, m_ymax, m_zmax)

    def _cells_along_ray(self, start, ray_dir, ray_len):
        """Every cell the ray's XZ projection crosses, start cell first.

        Amanatides & Woo. This replaces a point sample every ``cell_size``
        along the ray plus that sample's eight neighbours, which is what the
        grid did for years and is where line of sight spent most of its time:
        the fan visited 9.5 cells where the ray crossed 1.2, and the candidate
        set it produced was 173 brushes per ray against the 27 the ray's own
        cells hold.

        **Why the fan was there, and why this is not a weakening of it.**  The
        sampled points miss cells: a ray crossing a corner diagonally can skip
        from one cell to the cell diagonally opposite without either being
        sampled, so a brush filed only in the cell between them was never
        tested.  The fan hid that by testing everything nearby.  Stepping the
        boundaries instead means no cell is skipped in the first place -- and
        because :meth:`engine.spatial.CellIndex.insert` files a brush under
        *every* cell its XZ footprint overlaps, a brush the ray actually
        intersects is by construction in the bucket of a cell the ray is
        inside.  Exact traversal is therefore not merely as good as the fan; it
        is what the fan was approximating.

        The one place that reasoning is delicate is a ray crossing exactly
        through a grid corner, where it touches all four cells at a single
        point and float comparison has to break the tie.  Rather than let the
        rounding pick, a tie steps both axes and yields both of the cells the
        corner separates -- the conservative answer, at the cost of two extra
        dict lookups on the rays that are geometrically exact enough to hit it.
        """
        cell_size = self.cell_size
        x0 = start.x
        z0 = start.z
        dx = ray_dir.x * ray_len
        dz = ray_dir.z * ray_len
        cx = int(math.floor(x0 / cell_size))
        cz = int(math.floor(z0 / cell_size))
        cells = [(cx, cz)]

        # The exact number of single-axis steps between the two end cells.
        # Using it as the loop bound keeps the walk correct without trusting
        # the accumulated `t` to land on the far cell: a step is taken because
        # a boundary is still between here and the end, not because a float
        # said so.
        remaining = (abs(int(math.floor((x0 + dx) / cell_size)) - cx) +
                     abs(int(math.floor((z0 + dz) / cell_size)) - cz))
        if not remaining:
            return cells

        # `t` is the fraction of the segment travelled, so the tie tolerance
        # below is scale free -- it means "within a billionth of the ray's
        # length of the corner", not a distance in world units.
        if dx > 0.0:
            step_x = 1
            t_max_x = ((cx + 1) * cell_size - x0) / dx
            t_delta_x = cell_size / dx
        elif dx < 0.0:
            step_x = -1
            t_max_x = (cx * cell_size - x0) / dx
            t_delta_x = -cell_size / dx
        else:
            step_x = 0
            t_max_x = t_delta_x = float('inf')

        if dz > 0.0:
            step_z = 1
            t_max_z = ((cz + 1) * cell_size - z0) / dz
            t_delta_z = cell_size / dz
        elif dz < 0.0:
            step_z = -1
            t_max_z = (cz * cell_size - z0) / dz
            t_delta_z = -cell_size / dz
        else:
            step_z = 0
            t_max_z = t_delta_z = float('inf')

        append = cells.append
        while remaining > 0:
            if remaining > 1 and abs(t_max_x - t_max_z) <= 1e-9:
                # Dead on a corner. Both single-axis neighbours are touched.
                append((cx + step_x, cz))
                append((cx, cz + step_z))
                cx += step_x
                cz += step_z
                t_max_x += t_delta_x
                t_max_z += t_delta_z
                remaining -= 2
            elif t_max_x < t_max_z:
                cx += step_x
                t_max_x += t_delta_x
                remaining -= 1
            else:
                cz += step_z
                t_max_z += t_delta_z
                remaining -= 1
            append((cx, cz))
        return cells

    def raycast_down_batch(self, x, z, start_y):
        """:meth:`raycast_down` for many points at once; NaN where it is None.

        The same brushes (those filed in each point's cell), the same float64
        boxes and the same comparisons, as one masked reduction instead of a
        Python loop per point.
        """
        x = np.asarray(x, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        start_y = np.broadcast_to(np.asarray(start_y, dtype=np.float64), x.shape)
        rows = self._rows_now()
        cs = self.cell_size
        query, row = rows.pairs(np.floor(x / cs), np.floor(z / cs))
        best = np.full(len(x), -np.inf)
        if len(query):
            lo = rows.lo[row]
            hi = rows.hi[row]
            qx = x[query]
            qz = z[query]
            hit = ((lo[:, 0] <= qx) & (qx <= hi[:, 0])
                   & (lo[:, 2] <= qz) & (qz <= hi[:, 2])
                   & (hi[:, 1] <= start_y[query]))
            np.maximum.at(best, query[hit], hi[hit, 1])
        best[np.isneginf(best)] = np.nan
        return best

    def overlaps_wall_batch(self, mx, my, mz, margin):
        """:meth:`overlaps_wall` for many monster boxes at once.

        Each box's cells are the same ``floor`` of its corners; a box spanning
        more than two cells on an axis (a margin wider than a cell) is asked of
        the per-box query instead, so the answer never depends on the path.
        """
        mx = np.asarray(mx, dtype=np.float64)
        my = np.asarray(my, dtype=np.float64)
        mz = np.asarray(mz, dtype=np.float64)
        count = len(mx)
        result = np.zeros(count, dtype=bool)
        if not count:
            return result
        rows = self._rows_now()
        cs = self.cell_size
        xmin, xmax = mx - margin, mx + margin
        zmin, zmax = mz - margin, mz + margin
        ymin, ymax = my, my + 128.0
        cx0, cx1 = np.floor(xmin / cs), np.floor(xmax / cs)
        cz0, cz1 = np.floor(zmin / cs), np.floor(zmax / cs)
        wide = ((cx1 - cx0) > 1) | ((cz1 - cz0) > 1)
        corners = []
        for cx, cz, distinct in ((cx0, cz0, None),
                                 (cx1, cz0, cx1 != cx0),
                                 (cx0, cz1, cz1 != cz0),
                                 (cx1, cz1, (cx1 != cx0) & (cz1 != cz0))):
            live = ~wide if distinct is None else (~wide & distinct)
            which = np.flatnonzero(live)
            query, row = rows.pairs(cx[which], cz[which])
            corners.append((which[query], row))
        query = np.concatenate([q for q, _ in corners])
        row = np.concatenate([r for _, r in corners])
        if len(query):
            lo = rows.lo[row]
            hi = rows.hi[row]
            hit = ((xmax[query] > lo[:, 0]) & (xmin[query] < hi[:, 0])
                   & (ymax[query] > lo[:, 1]) & (ymin[query] < hi[:, 1])
                   & (zmax[query] > lo[:, 2]) & (zmin[query] < hi[:, 2]))
            result[query[hit]] = True
        for i in np.flatnonzero(wide):
            result[i] = self.overlaps_wall(float(mx[i]), float(my[i]),
                                           float(mz[i]), margin)
        return result

    def box_pairs(self, lo, hi):
        """``(query, row, rows)``: for each box ``lo[i]..hi[i]`` (``(n, 3)``),
        every brush filed in a cell its XZ footprint touches -- box ``query[k]``
        with ``rows.lo[row[k]]..rows.hi[row[k]]``, the float64 boxes every
        other query of this grid reads, movers and doors where they are now.

        The broad phase only: which pairs overlap, and what follows from it,
        is the caller's test. A brush is filed in every cell it spans, so the
        cells a box's own corners are in are all that can hold what it
        touches -- one lookup for a box inside one cell -- and a brush that
        shares two of them with a box is paired with it twice, which no test
        that asks "any" or "nearest" can tell.
        """
        lo = np.asarray(lo, dtype=np.float64)
        hi = np.asarray(hi, dtype=np.float64)
        rows = self._rows_now()
        count = len(lo)
        if not count:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty, rows
        cs = self.cell_size
        cx0 = np.floor(lo[:, 0] / cs)
        cz0 = np.floor(lo[:, 2] / cs)
        # Every query's own cell, and the off-range movers and doors for each.
        query, row = rows.pairs(cx0, cz0)
        more_x = np.floor(hi[:, 0] / cs) - cx0
        more_z = np.floor(hi[:, 2] / cs) - cz0
        queries, found = [query], [row]
        side = np.maximum(more_x, more_z) + 1.0
        wide = side * side > MAX_WALK_CELLS
        if wide.any():
            # Too many cells to walk one by one: the filed cells are tested
            # against each such box instead, and the walk keeps to the rest.
            which = np.flatnonzero(wide)
            q, r = rows._pairs_spanning(cx0[which], cz0[which], cx0[which] + more_x[which],
                                        cz0[which] + more_z[which])
            if len(r):
                queries.append(which[q])
                found.append(r)
            more_x = np.where(wide, 0.0, more_x)
            more_z = np.where(wide, 0.0, more_z)
        span_x = int(more_x.max())
        span_z = int(more_z.max())
        if span_x or span_z:
            for dx in range(span_x + 1):
                for dz in range(span_z + 1):
                    if not (dx or dz):
                        continue
                    which = np.flatnonzero((more_x >= dx) & (more_z >= dz))
                    if not which.size:
                        continue
                    q, r = rows._pairs_filed(cx0[which] + dx, cz0[which] + dz)
                    if len(r):
                        queries.append(which[q])
                        found.append(r)
        if len(found) > 1:
            query = np.concatenate(queries)
            row = np.concatenate(found)
            if _placement.asleep:
                rows._resolve_asleep(row)
        return query, row, rows

    def raycast_down(self, x, z, start_y=10000.0):
        """Return Y of the highest solid brush surface below (x, z), or None.
        Uses the grid — only checks brushes in the cell containing (x, z).
        Static brushes are read as their cell's resolved boxes
        (:class:`_BoxRows`); movers and doors are resolved now."""
        cell = (int(math.floor(x / self.cell_size)), int(math.floor(z / self.cell_size)))
        rows = self._boxes()
        best_y = None
        for _bid, bx_min, _by_min, bz_min, bx_max, by_max, bz_max in rows.statics.get(cell, ()):
            if bx_min <= x <= bx_max and bz_min <= z <= bz_max:
                if by_max <= start_y:
                    if best_y is None or by_max > best_y:
                        best_y = by_max
        for brush in chain(rows.moving.get(cell, ()), _off_range_filed()):
            bx_min, _by_min, bz_min, bx_max, by_max, bz_max = world_bounds(brush)
            if bx_min <= x <= bx_max and bz_min <= z <= bz_max:
                if by_max <= start_y:
                    if best_y is None or by_max > best_y:
                        best_y = by_max
        return best_y

    def has_line_of_sight(self, start, end):
        """Return True if ray from start to end hits no solid wall brush.
        Uses the grid to only test brushes in cells the ray passes through.

        The cells are the ones the ray crosses, stepped boundary by boundary
        (:meth:`_cells_along_ray`). This used to be a point sample every
        ``cell_size`` plus that sample's eight neighbours -- a fan wide enough
        to cover the cells the sampling skipped. Stepping the boundaries skips
        none, so the fan has nothing left to cover; the equivalence, including
        the diagonal-straddle case the fan was added for, is held by
        ``tests/physics/test_line_of_sight.py``.

        With at least :data:`LOS_DENSE_MIN_CANDIDATES` static candidates the
        static brushes are tested as rows of :meth:`_build_los_rows` in a
        handful of NumPy operations (:meth:`_los_dense`) and only movers and
        doors are walked; below that count every candidate is walked. Both
        reach the same answer; see ``tests/physics/test_line_of_sight.py``.

        Which one is faster is a question about the candidate set, not about
        the scene or the hardware, so the count decides it: the array work is
        a fixed per-ray fee, the walk is a per-brush cost that can exit early.
        The constant carries the measurements.

        The cell traversal is the same either way -- it happens once, here, and
        is handed to whichever narrow phase takes the ray, so the two cannot
        drift apart on which brushes they consider, only on how they test them.
        """
        ray_dir = end - start
        ray_len = glm.length(ray_dir)
        if ray_len < 0.001:
            return True
        ray_dir = ray_dir / ray_len

        # Walked once, and handed to whichever narrow phase takes the ray.
        coords = self._cells_along_ray(start, ray_dir, ray_len)
        # One read: a populate() on the logic thread publishes a new set of
        # rows rather than editing these.
        rows = self._los_rows

        dense = self._los_dense(start, ray_dir, ray_len, rows, coords)
        if dense is None:
            # Static brushes from their resolved bounds, then the movers live:
            # the same candidate set as walking every brush, split by whether
            # its box can change. Either blocking the ray is the whole answer.
            if not self._los_walk(rows.static_bounds_by_cell, coords,
                                  start, ray_dir, ray_len, resolved=True):
                return False
        elif not dense:
            return False
        if not self._los_walk(rows.moving_by_cell, coords, start, ray_dir, ray_len):
            return False
        off = _off_range_filed()
        return not off or self._los_walk({0: off}, (0,), start, ray_dir, ray_len)

    def _los_dense(self, start, ray_dir, ray_len, rows, coords):
        """Line of sight over the static rows, when there are enough of them.

        Same candidate set as the walk, from the same traversal, and the same
        arithmetic -- expressed over arrays: ``concatenate`` the cells' row
        arrays, ``np.unique`` in place of the ``id()`` set, one gather of the
        bounds, and one slab test over all candidates at once. The bounds are
        the tuples :func:`brush_aabb_bounds` returned, so the comparison is on
        the same numbers the walk compares.

        Returns ``None`` if this ray is not worth the array work, in which case
        the caller walks every candidate instead.
        """
        get = rows.static_by_cell.get
        parts = []
        candidates = 0
        for coord in coords:
            found = get(coord)
            if found is not None:
                parts.append(found)
                candidates += len(found)
        if not parts or candidates < self.LOS_DENSE_MIN_CANDIDATES:
            # Too few rows to earn the dispatch. The scalar walk gets this ray,
            # over the cells already traversed, and answers the same thing --
            # a ray with no candidates at all included, which is why the empty
            # set is handled here rather than by an early `return True`. It is
            # spelled separately from the count so that a threshold of zero
            # means "always dense" and is safe to set, which is how the tests
            # and the benchmark force the path.
            return None

        slots = np.unique(parts[0] if len(parts) == 1 else np.concatenate(parts))
        lo = rows.lo[slots]
        hi = rows.hi[slots]

        origin = (start.x, start.y, start.z)
        direction = (ray_dir.x, ray_dir.y, ray_dir.z)
        count = len(slots)
        t_min = np.zeros(count, dtype=np.float64)
        t_max = np.full(count, 10000.0, dtype=np.float64)
        alive = np.ones(count, dtype=bool)

        for axis in (0, 1, 2):
            rd = direction[axis]
            o = origin[axis]
            axis_lo = lo[:, axis]
            axis_hi = hi[:, axis]
            if -1e-6 < rd < 1e-6:
                # Parallel to this slab: inside or rejected, and t_min/t_max
                # are left alone -- which is what the scalar path does.
                alive &= (axis_lo <= o) & (o <= axis_hi)
                continue
            inv = 1.0 / rd
            ta = (axis_lo - o) * inv
            tb = (axis_hi - o) * inv
            near = np.minimum(ta, tb)
            far = np.maximum(ta, tb)
            np.maximum(t_min, near, out=t_min)
            np.minimum(t_max, far, out=t_max)
            alive &= t_min <= t_max

        # A rejected lane keeps accumulating t_min/t_max, which cannot revive
        # it: `alive` only ever loses entries.
        return not bool(np.any(alive & (t_min < ray_len - 0.1)))

    @staticmethod
    def _los_walk(cells, coords, start, ray_dir, ray_len, resolved=False):
        """The per-brush narrow phase over ``cells``.

        ``cells`` maps coord -> brushes, whose bounds are read live; with
        *resolved*, coord -> ``(key, lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``
        tuples already resolved by :meth:`SpatialGrid._build_los_rows`.
        """
        # PERF: hoist the ray endpoints/direction to scalars once and inline the
        # slab test below (bit-identical to the previous ray/AABB slab test).
        # This avoids two throwaway glm.vec3 constructions + a Python call per
        # brush along the ray -- the dominant cost of AI line-of-sight at tick
        # rate.
        ox, oy, oz = start.x, start.y, start.z
        rdx, rdy, rdz = ray_dir.x, ray_dir.y, ray_dir.z
        limit = ray_len - 0.1

        # Walk the cells the ray crosses. Test each unique brush immediately so
        # no temporary candidate list or second traversal is needed.
        seen = set()
        seen_add = seen.add
        for coord in coords:
            for entry in cells.get(coord, ()):
                if resolved:
                    bid, b0, b1, b2, b3, b4, b5 = entry
                else:
                    bid = id(entry)
                if bid in seen:
                    continue
                seen_add(bid)
                if not resolved:
                    b0, b1, b2, b3, b4, b5 = brush_aabb_bounds(entry)
                # --- ray/AABB slab test (matches intersect_ray_aabb) ---
                t_min = 0.0
                t_max = 10000.0
                # X
                if -1e-6 < rdx < 1e-6:
                    if ox < b0 or ox > b3:
                        continue
                else:
                    inv = 1.0 / rdx
                    ta = (b0 - ox) * inv
                    tb = (b3 - ox) * inv
                    if ta > tb:
                        ta, tb = tb, ta
                    if ta > t_min:
                        t_min = ta
                    if tb < t_max:
                        t_max = tb
                    if t_min > t_max:
                        continue
                # Y
                if -1e-6 < rdy < 1e-6:
                    if oy < b1 or oy > b4:
                        continue
                else:
                    inv = 1.0 / rdy
                    ta = (b1 - oy) * inv
                    tb = (b4 - oy) * inv
                    if ta > tb:
                        ta, tb = tb, ta
                    if ta > t_min:
                        t_min = ta
                    if tb < t_max:
                        t_max = tb
                    if t_min > t_max:
                        continue
                # Z
                if -1e-6 < rdz < 1e-6:
                    if oz < b2 or oz > b5:
                        continue
                else:
                    inv = 1.0 / rdz
                    ta = (b2 - oz) * inv
                    tb = (b5 - oz) * inv
                    if ta > tb:
                        ta, tb = tb, ta
                    if ta > t_min:
                        t_min = ta
                    if tb < t_max:
                        t_max = tb
                    if t_min > t_max:
                        continue
                if t_min < limit:
                    return False
        return True


# ---------------------------------------------------------------------------
# Body orientation
# ---------------------------------------------------------------------------
# A body that rolls (``rolls``) turns by the distance it travels. Its
# orientation is a unit quaternion -- (w, x, y, z), float64 so minutes of
# rolling keep it unit length -- because rolls in different directions compose
# as rotations, which adding Euler angles does not. It becomes the entity's
# ``rotation`` (Fio's Y-X-Z Euler degrees) only to be drawn.
