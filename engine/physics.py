import math
import glm
import numpy as np

from .constants import is_water_brush, brush_aabb_bounds
from .spatial import CELL_SIZE, CellIndex, authored_hidden, cells_of_points

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
        # The cell buckets re-expressed as dense render-projection rows. Built
        # lazily, disposable, and holding nothing the buckets do not already
        # say -- see cell_slots().
        self._cell_slots = None
        self._cell_slots_generation = None
        self._slot_source = None

    def clear(self):
        self.cells.clear()
        self._all_solid.clear()
        self.water_brushes.clear()
        self._invalidate_cell_slots()

    # ------------------------------------------------------------------
    # Dense addressing: the buckets, as render-projection rows
    # ------------------------------------------------------------------

    def _invalidate_cell_slots(self):
        self._cell_slots = None
        self._cell_slots_generation = None
        self._slot_source = None

    def cell_slots(self, table):
        """``{(cx, cz): int32 slots}`` -- this grid's buckets, as table rows.

        Line of sight spends almost all of its time not on finding cells but on
        what it does with the brushes in them: deduplicating them through a set
        of ``id()``, fetching each one's AABB from its dict, and running the
        slab test in Python.  Measured over rays captured from a real AI tick,
        that is 96% of the pass and the cell traversal is the other 4%.

        All three of those become array work if the candidates are *rows* of
        :class:`engine.render_table.RenderTable` rather than dicts -- and the
        rows already exist, holding the same numbers.  The only thing missing
        was the address, which is what this supplies.

        Emphatically a projection, like the table itself:

        * it stores nothing authored.  Every entry is ``slot_of_id[brush['id']]``
          for a brush the bucket already holds, so discarding it and rebuilding
          gives identical bits;
        * it is built **lazily**, on first use, and never by :meth:`populate`.
          At play start the grid is populated before the brushes have UUIDs and
          before the table has any rows at all -- the ids are stamped by the
          first render-state pass -- so building it eagerly would build it from
          nothing;
        * it is dropped whenever the grid is rebuilt, and whenever the table
          reconciles.  A ``slot`` is an address valid within one
          :attr:`~engine.render_table.RenderTable.generation`; the generation
          and the identity of the id map together say which address space this
          was built for.  In a play session neither moves, so this is built
          once.

        Returns ``None`` -- and remembers that it did, so the attempt is not
        repeated every ray -- when any brush in the grid has no row.  That is
        the fail-safe direction: a brush the projection could not address would
        be a missing occluder, so the caller keeps the scalar path instead.
        """
        if table is None:
            return None
        if (self._cell_slots_generation == table.generation
                and self._slot_source is table.slot_of_id):
            return self._cell_slots
        # Held rather than merely compared: keeping the dict alive is what stops
        # its identity being recycled under the check above. It is one small
        # dict, and it does not keep the table itself alive.
        self._slot_source = table.slot_of_id
        self._cell_slots_generation = table.generation
        self._cell_slots = self._build_cell_slots(table.slot_of_id)
        return self._cell_slots

    def _build_cell_slots(self, slot_of_id):
        """One int32 array per occupied cell, or None if a brush has no row."""
        built = {}
        for coord, bucket in self.cells.items():
            slots = np.empty(len(bucket), dtype=np.int32)
            for i, brush in enumerate(bucket):
                slot = slot_of_id.get(brush.get('id'))
                if slot is None:
                    return None
                slots[i] = slot
            built[coord] = slots
        return built

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def populate(self, brushes):
        """Builds the grid from a list of brushes.  Call once on play-mode enter
        and again whenever the static brush list changes (rare).

        The dense :meth:`cell_slots` projection goes with the buckets it was
        derived from -- ``clear()`` drops it, and the next line of sight
        rebuilds it against whatever the table says by then."""
        self.clear()
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

            pos = brush['pos']
            size = brush['size']

            # FIX: For mesh collision brushes, use the actual mesh bounds
            if brush.get('_collision_mode') == 'mesh':
                mesh_bounds = brush.get('_mesh_bounds')
                if mesh_bounds:
                    min_b, max_b = mesh_bounds
                    pos = [(min_b[i] + max_b[i]) / 2.0 for i in range(3)]
                    size = [max_b[i] - min_b[i] for i in range(3)]

            self._index.insert(brush,
                               pos[0] - size[0] * 0.5, pos[2] - size[2] * 0.5,
                               pos[0] + size[0] * 0.5, pos[2] + size[2] * 0.5)

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
        Uses the grid to limit the search to nearby cells only."""
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

        cells = self.cells
        if min_cx == max_cx and min_cz == max_cz:
            # Single-cell fast path: no de-duplication set needed.
            for brush in cells.get((min_cx, min_cz), ()):
                pos = brush['pos']
                size = brush['size']
                bx_min = pos[0] - size[0] * 0.5
                bx_max = pos[0] + size[0] * 0.5
                by_min = pos[1] - size[1] * 0.5
                by_max = pos[1] + size[1] * 0.5
                bz_min = pos[2] - size[2] * 0.5
                bz_max = pos[2] + size[2] * 0.5
                if (m_xmax > bx_min and m_xmin < bx_max and
                        m_ymax > by_min and m_ymin < by_max and
                        m_zmax > bz_min and m_zmin < bz_max):
                    return True
            return False

        seen = set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                for brush in cells.get((cx, cz), ()):
                    bid = id(brush)
                    if bid in seen:
                        continue
                    seen.add(bid)

                    pos = brush['pos']
                    size = brush['size']
                    bx_min = pos[0] - size[0] * 0.5
                    bx_max = pos[0] + size[0] * 0.5
                    by_min = pos[1] - size[1] * 0.5
                    by_max = pos[1] + size[1] * 0.5
                    bz_min = pos[2] - size[2] * 0.5
                    bz_max = pos[2] + size[2] * 0.5

                    if (m_xmax > bx_min and m_xmin < bx_max and
                        m_ymax > by_min and m_ymin < by_max and
                        m_zmax > bz_min and m_zmin < bz_max):
                        return True
        return False

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

    def _los_dense(self, start, ray_dir, ray_len, table, slots_by_cell, coords):
        """Line of sight over the projection's rows, when there are enough of them.

        Same candidate set as the walk, from the same traversal, and the same
        arithmetic -- expressed over arrays. The three phases the scalar path
        spends its time in become: ``concatenate`` the cells' slot arrays,
        ``np.unique`` in place of the ``id()`` set, one gather of
        ``center``/``half``, and one slab test over all candidates at once.

        **The float32 is not incidental.** ``brush_aabb_bounds`` builds its
        bounds through ``glm.vec3``, which is float32, and the slab test is a
        comparison -- so a float64 derivation from the same columns differs by
        up to 3.6e-04 and can decide a grazing ray differently. Casting the
        columns to float32 before the subtract and add reproduces it bit for
        bit (verified over 2000 random brushes); the arithmetic afterwards is
        float64, exactly as the scalar path's is once it has read the tuple.

        Returns ``None`` if this ray is not worth the array work, or if the
        projection cannot be read consistently. Either way the caller takes the
        scalar path, which answers the same thing.
        """
        get = slots_by_cell.get
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

        # float32 to match glm's rounding, then float64 for the test itself.
        #
        # The two columns are read separately, and this runs on the AI thread
        # while the logic thread owns the table. Reading a mover's transform
        # while it is being written is the race this path has always had --
        # the scalar path reads `brush['pos']` live for exactly the same
        # reason, deliberately, so a prop riding a platform sees its current
        # height. What is new is only that there are two arrays rather than
        # one dict: a reconcile between the reads would reallocate them, and a
        # slot valid for one could be past the end of the other. That cannot
        # be made atomic without synchronising the render pass, so it is
        # caught and answered by the scalar path instead -- the same fail-safe
        # direction as an unaddressable brush.
        try:
            centre = table.center[slots].astype(np.float32)
            half = table.half[slots].astype(np.float32)
        except IndexError:
            return None
        lo = (centre - half).astype(np.float64)
        hi = (centre + half).astype(np.float64)

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

    def raycast_down(self, x, z, start_y=10000.0):
        """Return Y of the highest solid brush surface below (x, z), or None.
        Uses the grid — only checks brushes in the cell containing (x, z)."""
        cx = int(math.floor(x / self.cell_size))
        cz = int(math.floor(z / self.cell_size))
        cell = (cx, cz)
        brushes = self.cells.get(cell, [])

        best_y = None
        for brush in brushes:
            pos = brush['pos']
            size = brush['size']
            bx_min = pos[0] - size[0] * 0.5
            bx_max = pos[0] + size[0] * 0.5
            bz_min = pos[2] - size[2] * 0.5
            bz_max = pos[2] + size[2] * 0.5
            by_max = pos[1] + size[1] * 0.5

            if bx_min <= x <= bx_max and bz_min <= z <= bz_max:
                if by_max <= start_y:
                    if best_y is None or by_max > best_y:
                        best_y = by_max
        return best_y

    def has_line_of_sight(self, start, end, intersect_ray_aabb_fn, table=None):
        """Return True if ray from start to end hits no solid wall brush.
        Uses the grid to only test brushes in cells the ray passes through.

        The cells are the ones the ray crosses, stepped boundary by boundary
        (:meth:`_cells_along_ray`). This used to be a point sample every
        ``cell_size`` plus that sample's eight neighbours -- a fan wide enough
        to cover the cells the sampling skipped. Stepping the boundaries skips
        none, so the fan has nothing left to cover; the equivalence, including
        the diagonal-straddle case the fan was added for, is held by
        ``tests/physics/test_line_of_sight.py``.

        *table* is an optional :class:`engine.render_table.RenderTable`. Given
        one, and a ray with at least :data:`LOS_DENSE_MIN_CANDIDATES` candidate
        brushes, they are tested as dense rows in a handful of NumPy operations
        (:meth:`_los_dense`). Below that count -- or without a table, or when
        the grid holds a brush the table cannot address -- the per-brush path
        below runs unchanged. Both reach the same answer; see
        ``tests/physics/test_line_of_sight.py``.

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

        slots_by_cell = self.cell_slots(table)
        if slots_by_cell is not None:
            dense = self._los_dense(start, ray_dir, ray_len, table,
                                    slots_by_cell, coords)
            if dense is not None:
                return dense

        # PERF: hoist the ray endpoints/direction to scalars once and inline the
        # slab test below (bit-identical to intersect_ray_aabb_fn). This avoids
        # two throwaway glm.vec3 constructions + a Python call per brush along
        # the ray -- the dominant cost of AI line-of-sight at tick rate. The
        # signature keeps intersect_ray_aabb_fn so existing callers are unchanged.
        ox, oy, oz = start.x, start.y, start.z
        rdx, rdy, rdz = ray_dir.x, ray_dir.y, ray_dir.z
        limit = ray_len - 0.1

        # Walk the cells the ray crosses. Test each unique brush immediately so
        # no temporary candidate list or second traversal is needed.
        cells = self.cells
        seen = set()
        seen_add = seen.add
        for coord in coords:
            for brush in cells.get(coord, ()):
                bid = id(brush)
                if bid in seen:
                    continue
                seen_add(bid)
                b0, b1, b2, b3, b4, b5 = brush_aabb_bounds(brush)
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


class PhysicsBody:
    """Lightweight handle for one row in the engine's batched physics state."""

    def __init__(self, world, entity, entity_id):
        self.world = world
        self.entity = entity
        self.entity_id = entity_id
        self.rest_callback = None

    @property
    def _index(self):
        # Registration only queues a row; the index exists once the world packs.
        # Pack on demand so a handle read right after register_body() reports
        # the body's real state instead of the not-found defaults. _pack()
        # early-outs unless the world is dirty, so this costs nothing on the
        # steady-state path.
        self.world._pack()
        return self.world._indices.get(self.entity_id)

    @property
    def velocity(self):
        i = self._index
        if i is None:
            return [0.0, 0.0, 0.0]
        return self.world._velocity[i].tolist()

    @velocity.setter
    def velocity(self, value):
        i = self._index
        if i is not None:
            self.world._velocity[i] = np.asarray(value, dtype=np.float32)

    @property
    def mass(self):
        i = self._index
        return float(self.world._mass[i]) if i is not None else 1.0

    # ------------------------------------------------------------------
    # Shape and authored-material accessors.
    #
    # Before the batched-NumPy rewrite these were plain attributes on the
    # body. Packing the state into the world's SoA arrays kept the arrays
    # but dropped the handle's read side, so callers -- and this class's
    # own tests -- lost the ability to ask a body what shape it is. They
    # are restored here as index-backed properties in the same style as
    # ``velocity``/``mass`` above, reading the packed row rather than
    # duplicating any state.
    # ------------------------------------------------------------------

    @property
    def half_extents(self):
        """Half the body's collision box, as the world stores it."""
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) for v in self.world._half[i])

    @property
    def size(self):
        """The body's full collision box.

        ``size`` means full extent everywhere else in Fio (``brush['size']``,
        ``collision_size``), and it meant full extent on this handle before
        the rewrite, so it keeps that meaning. Use :attr:`half_extents` for
        the half-size the batched maths works in.
        """
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) * 2.0 for v in self.world._half[i])

    @property
    def offset(self):
        """Collision-box centre relative to the entity's origin."""
        i = self._index
        if i is None:
            return (0.0, 0.0, 0.0)
        return tuple(float(v) for v in self.world._offset[i])

    @property
    def solid(self):
        i = self._index
        return bool(self.world._solid[i]) if i is not None else False

    @property
    def gravity(self):
        i = self._index
        return bool(self.world._gravity[i]) if i is not None else True

    @property
    def friction(self):
        i = self._index
        return float(self.world._friction[i]) if i is not None else 0.55

    @property
    def linear_damping(self):
        i = self._index
        return float(self.world._damping[i]) if i is not None else 0.08

    @property
    def angular_velocity(self):
        i = self._index
        if i is None:
            return [0.0, 0.0, 0.0]
        return self.world._angular_velocity[i].tolist()

    @property
    def awake(self):
        i = self._index
        return bool(self.world._awake[i]) if i is not None else False

    @awake.setter
    def awake(self, value):
        i = self._index
        if i is not None:
            self.world._awake[i] = bool(value)

    @property
    def kinematic(self):
        i = self._index
        return bool(self.world._kinematic[i]) if i is not None else False

    @kinematic.setter
    def kinematic(self, value):
        self.world._set_kinematic_index(self._index, bool(value))

    def set_kinematic(self, value):
        self.kinematic = value

    def wake(self, velocity=None):
        self.world._wake_index(self._index, velocity)

    def set_rest_callback(self, callback):
        self.rest_callback = callback

    def clear(self):
        self.rest_callback = None


class PhysicsWorld:
    """Engine-owned batched dynamic-body simulation.

    Body state is stored as structure-of-arrays NumPy buffers. The per-tick
    integration, damping, gravity, push response, sleep test and position
    updates are vectorised. Python remains only at the engine-object boundary
    and for the small number of world-collision cell/contact groups that
    cannot be expressed as one dense operation without wasting large amounts
    of memory.
    """

    GRAVITY = np.float32(-900.0)
    #: Active-body count from which the grouped floor query is worth its setup.
    #: Below it the NumPy assembly costs more than the scalar raycasts it saves
    #: -- measured crossover is around 56-64 bodies on this scene shape, so the
    #: grouped path is only taken where it is demonstrably faster. Both paths
    #: are bit-identical (tests/physics/test_dynamic_bodies.py), so this is
    #: purely a cost choice and never a behavioural one.
    FLOOR_BATCH_MIN_BODIES = 64
    MAX_STEP = np.float32(0.05)
    REST_SPEED = np.float32(1.0)

    # Runtime physics controls. Entity-authored mass/friction/damping remain
    # intact; these values provide global tuning/debug controls for play mode.
    DEFAULT_TIME_SCALE = np.float32(1.0)
    DEFAULT_FRICTION_SCALE = np.float32(1.0)
    DEFAULT_DAMPING_SCALE = np.float32(1.0)
    DEFAULT_SLEEP_ENABLED = True

    def __init__(self, spatial_grid):
        self.spatial_grid = spatial_grid
        self.bodies = {}
        self._indices = {}

        self._entities = []
        self._velocity = np.empty((0, 3), dtype=np.float32)
        self._position = np.empty((0, 3), dtype=np.float32)
        self._offset = np.empty((0, 3), dtype=np.float32)
        self._half = np.empty((0, 3), dtype=np.float32)
        self._mass = np.empty(0, dtype=np.float32)
        self._friction = np.empty(0, dtype=np.float32)
        self._damping = np.empty(0, dtype=np.float32)
        self._gravity = np.empty(0, dtype=np.float32)
        self._angular_velocity = np.empty((0, 3), dtype=np.float32)
        self._solid = np.empty(0, dtype=np.bool_)
        self._physics_enabled = np.empty(0, dtype=np.bool_)
        self._awake = np.empty(0, dtype=np.bool_)
        self._kinematic = np.empty(0, dtype=np.bool_)
        self._dirty = False
        self._static_cells = {}
        self._static_query_cache = {}
        # Baseline for entities_that_changed_cell(): one (N, 2) cell array.
        self._reported_cell = None

        # Console-adjustable world controls.
        self.gravity = float(self.GRAVITY)
        self.time_scale = float(self.DEFAULT_TIME_SCALE)
        self.friction_scale = float(self.DEFAULT_FRICTION_SCALE)
        self.damping_scale = float(self.DEFAULT_DAMPING_SCALE)
        self.sleep_enabled = bool(self.DEFAULT_SLEEP_ENABLED)

    def clear(self):
        for body in self.bodies.values():
            body.clear()
        self.bodies.clear()
        self._indices.clear()
        self._entities.clear()
        self._velocity = np.empty((0, 3), dtype=np.float32)
        self._position = np.empty((0, 3), dtype=np.float32)
        self._offset = np.empty((0, 3), dtype=np.float32)
        self._half = np.empty((0, 3), dtype=np.float32)
        self._mass = np.empty(0, dtype=np.float32)
        self._friction = np.empty(0, dtype=np.float32)
        self._damping = np.empty(0, dtype=np.float32)
        self._gravity = np.empty(0, dtype=np.float32)
        self._angular_velocity = np.empty((0, 3), dtype=np.float32)
        self._solid = np.empty(0, dtype=np.bool_)
        self._physics_enabled = np.empty(0, dtype=np.bool_)
        self._awake = np.empty(0, dtype=np.bool_)
        self._kinematic = np.empty(0, dtype=np.bool_)
        self._static_cells.clear()
        self._static_query_cache.clear()
        self._reported_cell = None
        self._dirty = False

    @staticmethod
    def _shape_from_brush(entity, brush):
        bounds = brush.get('_mesh_bounds') if brush.get('_collision_mode') == 'mesh' else None
        if bounds:
            min_v, max_v = bounds
            center = np.array(
                [(float(min_v[i]) + float(max_v[i])) * 0.5 for i in range(3)],
                dtype=np.float32,
            )
            size = np.array(
                [float(max_v[i]) - float(min_v[i]) for i in range(3)],
                dtype=np.float32,
            )
        else:
            center = np.asarray(
                brush.get('pos', (0.0, 0.0, 0.0)), dtype=np.float32
            )
            size = np.asarray(
                brush.get('size', (64.0, 64.0, 64.0)), dtype=np.float32
            )

        origin = np.asarray(
            getattr(entity, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32
        )
        return size * np.float32(0.5), center - origin

    def register_body(self, entity, brush, rest_callback=None):
        """Register one body; state is packed once before the next tick."""
        entity_id = id(entity)
        body = self.bodies.get(entity_id)
        if body is None:
            body = PhysicsBody(self, entity, entity_id)
            self.bodies[entity_id] = body
            self._entities.append(entity)

        props = getattr(entity, 'properties', {})
        half, offset = self._shape_from_brush(entity, brush)
        body.rest_callback = rest_callback

        # Keep registration descriptors on the handle until packing. This is
        # construction-time Python work, not physics work.
        body._initial = (
            np.asarray(getattr(entity, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32),
            half,
            offset,
            max(0.01, float(props.get('mass', 1.0))),
            max(0.0, min(1.0, float(props.get('friction', 0.55)))),
            max(0.0, float(props.get('linear_damping', 0.08))),
            1.0 if props.get('gravity', True) else 0.0,
            np.asarray(props.get('drop_angular_velocity', [0.0, 0.0, 0.0]), dtype=np.float32),
            not bool(props.get('no_collision', True)),
            bool(props.get('physics_enabled', False)),
        )
        self._dirty = True
        return body

    def _pack(self):
        if not self._dirty:
            return
        n = len(self._entities)
        if n == 0:
            self._dirty = False
            return

        self._position = np.empty((n, 3), dtype=np.float32)
        self._offset = np.empty((n, 3), dtype=np.float32)
        self._half = np.empty((n, 3), dtype=np.float32)
        self._velocity = np.zeros((n, 3), dtype=np.float32)
        self._mass = np.empty(n, dtype=np.float32)
        self._friction = np.empty(n, dtype=np.float32)
        self._damping = np.empty(n, dtype=np.float32)
        self._gravity = np.empty(n, dtype=np.float32)
        self._angular_velocity = np.empty((n, 3), dtype=np.float32)
        self._solid = np.empty(n, dtype=np.bool_)
        self._physics_enabled = np.empty(n, dtype=np.bool_)
        self._awake = np.zeros(n, dtype=np.bool_)
        self._kinematic = np.zeros(n, dtype=np.bool_)

        for i, entity in enumerate(self._entities):
            body = self.bodies[id(entity)]
            (
                position, half, offset, mass, friction, damping, gravity,
                angular_velocity, solid, physics_enabled,
            ) = body._initial
            self._indices[id(entity)] = i
            self._position[i] = position
            self._half[i] = half
            self._offset[i] = offset
            self._mass[i] = mass
            self._friction[i] = friction
            self._damping[i] = damping
            self._gravity[i] = gravity
            self._angular_velocity[i] = angular_velocity
            self._solid[i] = solid
            self._physics_enabled[i] = physics_enabled

        self._dirty = False

    def get_body(self, entity):
        self._pack()
        return self.bodies.get(id(entity))

    def _set_kinematic_index(self, index, value):
        if index is None:
            return
        self._pack()

        # Kinematic bodies are positioned by the gameplay/runtime layer
        # (for example PropSession while a prop is being carried). Synchronise
        # the physics copy whenever transform ownership changes so physics
        # cannot restore a stale pre-carry position on release.
        entity = self._entities[index]
        self._position[index] = np.asarray(
            getattr(entity, "pos", self._position[index]),
            dtype=np.float32,
        )

        self._kinematic[index] = value
        if value:
            self._velocity[index] = 0.0
            self._awake[index] = False

    def set_kinematic(self, entity, value):
        self._pack()
        self._set_kinematic_index(self._indices.get(id(entity)), value)

    def _wake_index(self, index, velocity=None):
        if index is None:
            return
        self._pack()
        if velocity is not None:
            self._velocity[index] = np.asarray(velocity, dtype=np.float32)
        self._awake[index] = True
        self._kinematic[index] = False

    def wake(self, entity, velocity=None):
        self._pack()
        self._wake_index(self._indices.get(id(entity)), velocity)

    def sync_entity_position(self, entity, wake=False):
        """Synchronize a body's cached position after external movement."""
        self._pack()
        index = self._indices.get(id(entity))
        if index is None:
            return
        self._position[index] = np.asarray(
            getattr(entity, 'pos', self._position[index]),
            dtype=np.float32,
        )
        if wake:
            self._awake[index] = True
            self._kinematic[index] = False
            self._velocity[index] = 0.0

    def set_rest_callback(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.set_rest_callback(callback)

    def entities_that_changed_cell(self):
        """Bodies that crossed a cell boundary since the last call.

        PhysicsWorld owns an entity's position while it is in motion, so a
        spatial index built on those entities has to be told when to re-file
        one. This is how, and it answers the narrowest useful question: not
        "which bodies are awake" and not even "which moved", but "which are no
        longer in the cell they were in" — the only ones whose membership can
        actually be wrong.

        The detection is one vectorised comparison over the body arrays, so the
        per-frame cost does not grow a Python loop as the world fills up; what
        comes back is normally empty, because a body has to travel a whole
        512-unit column to appear in it. The returned entities are objects, so
        acting on them is per-entity by nature — which is the point of making
        this set as small as it can correctly be.
        """
        self._pack()
        if not self._entities:
            self._reported_cell = None
            return ()

        cx, cz = cells_of_points(self._position[:, 0], self._position[:, 2])
        current = np.stack((cx, cz), axis=1)
        previous = self._reported_cell

        if previous is None or previous.shape != current.shape:
            # First call, or the body set changed size and the rows no longer
            # line up. Registration filed every body from its live position, so
            # nothing is stale — take this as the new baseline.
            self._reported_cell = current
            return ()

        changed = np.flatnonzero(np.any(current != previous, axis=1))
        self._reported_cell = current
        if changed.size == 0:
            return ()
        return [self._entities[i] for i in changed]

    def wake_all(self):
        """Wake every non-kinematic physics body for runtime console tuning."""
        self._pack()
        if not self._entities:
            return
        active = self._physics_enabled & ~self._kinematic
        self._awake[active] = True

    def _disabled_mask(self, candidates):
        """Which candidate bodies their entity currently marks ``disabled``.

        ``disabled`` is authored state that changes at runtime -- an I/O
        Disable, or Big World parking a cell, which stashes the authored values
        and forces the flag on. It is written in a dozen places across
        io_handlers and the streaming layer, so there is no single setter to
        hook; it has to be read.

        Reading it is bounded the way the pre-vectorisation simulation bounded
        it: that loop only ever consulted ``disabled`` for bodies it was
        actually moving, so this consults only candidates that are also awake.
        A world with nothing in motion pays nothing, and a parked or disabled
        prop stops being integrated instead of quietly falling while dormant.
        """
        mask = np.zeros(len(self._entities), dtype=np.bool_)
        indices = np.flatnonzero(candidates & self._awake)
        if indices.size == 0:
            return mask
        for i in indices:
            props = getattr(self._entities[int(i)], 'properties', None)
            if isinstance(props, dict) and props.get('disabled', False):
                mask[i] = True
        return mask

    def _integrate_rotation(self, angular, dt):
        """Advance the rotation of every spinning body.

        Rotation lives on the entity's property dict rather than in the packed
        arrays, so this is necessarily an object-boundary operation. What it
        does not have to be is scalar arithmetic: the gather, the multiply-add
        and the conversion back to lists are each one operation over all
        spinning bodies, and only the dict reads and writes stay per-entity.

        Which bodies spin is unchanged -- any body with a non-zero angular
        velocity, awake or not, exactly as before.
        """
        spinning = np.flatnonzero(np.any(angular != 0.0, axis=1))
        if spinning.size == 0:
            return

        current = np.zeros((spinning.size, 3), dtype=np.float32)
        targets = []
        for slot, index in enumerate(spinning):
            props = getattr(self._entities[int(index)], 'properties', None)
            if not isinstance(props, dict):
                targets.append(None)
                continue
            targets.append(props)
            rotation = props.get('rotation')
            if rotation is None:
                continue
            try:
                current[slot] = rotation
            except (TypeError, ValueError):
                # A malformed authored rotation restarts from zero rather than
                # aborting the whole step for every other body.
                current[slot] = 0.0

        updated = (current + angular[spinning] * dt).tolist()
        for props, rotation in zip(targets, updated):
            if props is not None:
                props['rotation'] = rotation

    def _sync_entities(self, indices=None):
        # Position only. Rotation is integrated (angular * dt) in step();
        # applying angular velocity here as well spun bodies ~61x too fast.
        if indices is None:
            indices = range(len(self._entities))
        for i in indices:
            entity = self._entities[int(i)]
            entity.pos[:] = self._position[int(i)].tolist()

    def _rebuild_static_cells(self):
        """Cache static collision AABBs as contiguous NumPy arrays per grid cell."""
        cells = {}
        for key, brushes in self.spatial_grid.cells.items():
            if not brushes:
                continue
            rows = []
            for brush in brushes:
                if brush.get('_physics_body'):
                    continue
                bounds = brush.get('_mesh_bounds') if brush.get('_collision_mode') == 'mesh' else None
                if bounds:
                    lo, hi = bounds
                    rows.append([
                        float(lo[0]), float(lo[1]), float(lo[2]),
                        float(hi[0]), float(hi[1]), float(hi[2]),
                    ])
                else:
                    pos = brush.get('pos', (0.0, 0.0, 0.0))
                    size = brush.get('size', (0.0, 0.0, 0.0))
                    rows.append([
                        float(pos[0]) - float(size[0]) * 0.5,
                        float(pos[1]) - float(size[1]) * 0.5,
                        float(pos[2]) - float(size[2]) * 0.5,
                        float(pos[0]) + float(size[0]) * 0.5,
                        float(pos[1]) + float(size[1]) * 0.5,
                        float(pos[2]) + float(size[2]) * 0.5,
                    ])
            if rows:
                cells[key] = np.asarray(rows, dtype=np.float32)
        self._static_cells = cells

    def rebuild(self, brushes):
        self.clear()

        for brush in brushes:
            if not brush.get('_physics_body'):
                continue
            entity = brush.get('_physics_entity')
            if entity is not None:
                self.register_body(entity, brush)

        self._pack()
        self._rebuild_static_cells()

    def _candidate_aabbs(self, cell_x, cell_z):
        key = (cell_x, cell_z)
        cached = self._static_query_cache.get(key)
        if cached is False:
            return None
        if cached is not None:
            return cached

        parts = []
        cells = self._static_cells
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                aabbs = cells.get((cell_x + dx, cell_z + dz))
                if aabbs is not None:
                    parts.append(aabbs)

        if not parts:
            self._static_query_cache[key] = False
            return None
        if len(parts) == 1:
            result = parts[0]
        else:
            result = np.concatenate(parts, axis=0)
        self._static_query_cache[key] = result
        return result

    def _batch_static_collision(self, axis, old_position):
        """Resolve one horizontal axis with vectorised AABB tests per cell group."""
        active = (
            self._physics_enabled
            & self._awake
            & ~self._kinematic
            & self._solid
        )
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return

        cell_size = np.float32(self.spatial_grid.cell_size)
        cell_keys = np.column_stack((
            np.floor(self._position[indices, 0] / cell_size).astype(np.int64),
            np.floor(self._position[indices, 2] / cell_size).astype(np.int64),
        ))
        unique_cells, inverse = np.unique(
            cell_keys, axis=0, return_inverse=True
        )

        collision = np.zeros(indices.size, dtype=np.bool_)

        for group_id, key in enumerate(unique_cells):
            local_indices = np.flatnonzero(inverse == group_id)
            bi = indices[local_indices]
            aabbs = self._candidate_aabbs(int(key[0]), int(key[1]))
            if aabbs is None:
                continue

            pos = self._position[bi] + self._offset[bi]
            half = self._half[bi]

            bmin = pos - half
            bmax = pos + half

            cmin = aabbs[:, :3]
            cmax = aabbs[:, 3:]

            overlap = (
                (bmax[:, None, 0] > cmin[None, :, 0]) &
                (bmin[:, None, 0] < cmax[None, :, 0]) &
                (bmax[:, None, 1] > cmin[None, :, 1]) &
                (bmin[:, None, 1] < cmax[None, :, 1]) &
                (bmax[:, None, 2] > cmin[None, :, 2]) &
                (bmin[:, None, 2] < cmax[None, :, 2])
            )
            hit = overlap.any(axis=1)

            collision[local_indices] |= hit

        if np.any(collision):
            hit_indices = indices[collision]
            self._position[hit_indices, axis] = old_position[hit_indices, axis]
            self._velocity[hit_indices, axis] = 0.0

    def _batch_floor(self, previous_bottom=None):
        """Find the floor beneath each active dynamic body.

        Five support points per body -- centre plus four in-footprint samples --
        so a body straddling a seam between floor brushes cannot lose contact
        just because its centre crossed the gap.

        That is 5N downward queries per step. Done one at a time they were the
        largest remaining scalar section of the dynamic path: each
        ``raycast_down`` call re-derives the cell and then walks that cell's
        brush dicts in Python, so the cost is 5N x (brushes per cell) Python
        iterations. :meth:`_batch_floor_grouped` instead groups all 5N sample
        points by grid cell and tests every point in a cell against every brush
        in it with one broadcast, which makes the Python work proportional to
        the number of *distinct cells* rather than to the number of samples.

        Brush positions are still read live on every step. Movers and doors sit
        in the grid and move without it being repopulated, so a prop riding a
        platform depends on seeing the platform's current height; caching the
        AABBs would quietly break that.

        The grouped path is used only for a stock :class:`SpatialGrid`. Anything
        that overrides ``raycast_down`` -- a test double, or a future grid with
        its own tracing -- keeps the scalar path, because its answers are not
        derivable from ``cells`` alone.
        """
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        if (int(active.sum()) >= self.FLOOR_BATCH_MIN_BODIES
                and self._can_group_floor_queries()):
            return self._batch_floor_grouped(previous_bottom)
        return self._batch_floor_scalar(previous_bottom)

    def _can_group_floor_queries(self):
        grid = self.spatial_grid
        if type(grid).__dict__.get('raycast_down') is None:
            # Inherited from SpatialGrid rather than overridden.
            if not isinstance(grid, SpatialGrid):
                return False
        elif type(grid).raycast_down is not SpatialGrid.raycast_down:
            return False
        return (isinstance(getattr(grid, 'cells', None), dict)
                and getattr(grid, 'cell_size', None))

    def _support_offsets(self, indices):
        """Per-body support-point offsets and ray origins, in float64.

        float64 deliberately: the scalar path promotes each float32 component
        to a Python float before the offset arithmetic, so matching the width
        here keeps the two paths bit-identical rather than merely close.
        """
        centers = (self._position[indices] + self._offset[indices]).astype(np.float64)
        half = self._half[indices].astype(np.float64)
        zeros = np.zeros(indices.size, dtype=np.float64)
        hx = half[:, 0] * 0.75
        hz = half[:, 2] * 0.75
        off_x = np.stack([zeros, -hx, hx, zeros, zeros], axis=1)
        off_z = np.stack([zeros, zeros, zeros, -hz, hz], axis=1)
        px = centers[:, 0:1] + off_x
        pz = centers[:, 2:3] + off_z
        bottom = centers[:, 1] - half[:, 1]
        return px, pz, bottom

    def _batch_floor_grouped(self, previous_bottom=None):
        n = len(self._entities)
        floors = np.full(n, -np.inf, dtype=np.float32)
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return floors

        previous_bottom = self._previous_bottom(previous_bottom)
        px, pz, bottom = self._support_offsets(indices)
        ray_y = np.maximum(bottom, previous_bottom[indices].astype(np.float64)) + 1.0

        samples = px.size
        flat_x = px.ravel()
        flat_z = pz.ravel()
        flat_y = np.repeat(ray_y, px.shape[1])

        cell_size = float(self.spatial_grid.cell_size)
        cx = np.floor(flat_x / cell_size).astype(np.int64)
        cz = np.floor(flat_z / cell_size).astype(np.int64)

        best = np.full(samples, -np.inf, dtype=np.float64)
        cells = self.spatial_grid.cells

        # Sort once, then walk contiguous runs: one pass per distinct cell
        # instead of one boolean scan of every sample per cell.
        order = np.lexsort((cz, cx))
        s_cx = cx[order]
        s_cz = cz[order]
        starts = np.flatnonzero(
            np.r_[True, (s_cx[1:] != s_cx[:-1]) | (s_cz[1:] != s_cz[:-1])]
        )
        ends = np.r_[starts[1:], samples]

        for start, end in zip(starts, ends):
            brushes = cells.get((int(s_cx[start]), int(s_cz[start])))
            if not brushes:
                continue
            bounds = self._cell_bounds(brushes)
            if bounds is None:
                continue
            lo_x, hi_x, lo_z, hi_z, top_y = bounds

            sel = order[start:end]
            X = flat_x[sel][:, None]
            Z = flat_z[sel][:, None]
            Y = flat_y[sel][:, None]
            inside = (
                (X >= lo_x) & (X <= hi_x)
                & (Z >= lo_z) & (Z <= hi_z)
                & (top_y[None, :] <= Y)
            )
            if not inside.any():
                continue
            best[sel] = np.where(inside, top_y[None, :], -np.inf).max(axis=1)

        per_body = best.reshape(px.shape).max(axis=1)
        hit = per_body > -np.inf
        if hit.any():
            floors[indices[hit]] = per_body[hit].astype(np.float32)
        return floors

    @staticmethod
    def _cell_bounds(brushes):
        """Live XZ extents and top surface of every brush in one grid cell.

        Read from the dicts on every call, not cached: see _batch_floor.
        """
        count = len(brushes)
        lo_x = np.empty(count, dtype=np.float64)
        hi_x = np.empty(count, dtype=np.float64)
        lo_z = np.empty(count, dtype=np.float64)
        hi_z = np.empty(count, dtype=np.float64)
        top_y = np.empty(count, dtype=np.float64)
        kept = 0
        for brush in brushes:
            try:
                pos = brush['pos']
                size = brush['size']
                x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                sx, sy, sz = float(size[0]), float(size[1]), float(size[2])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            lo_x[kept] = x - sx * 0.5
            hi_x[kept] = x + sx * 0.5
            lo_z[kept] = z - sz * 0.5
            hi_z[kept] = z + sz * 0.5
            top_y[kept] = y + sy * 0.5
            kept += 1
        if kept == 0:
            return None
        return (lo_x[:kept], hi_x[:kept], lo_z[:kept], hi_z[:kept], top_y[:kept])

    def _previous_bottom(self, previous_bottom):
        if previous_bottom is None:
            return (self._position + self._offset)[:, 1] - self._half[:, 1]
        return np.asarray(previous_bottom, dtype=np.float32)

    def _batch_floor_scalar(self, previous_bottom=None):
        """One raycast_down per support point. Fallback for a custom grid."""
        n = len(self._entities)
        floors = np.full(n, -np.inf, dtype=np.float32)
        active = self._physics_enabled & self._awake & ~self._kinematic & self._solid
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return floors

        raycast = getattr(self.spatial_grid, 'raycast_down', None)
        if raycast is None:
            return floors

        # Use the body's previous bottom as the ray origin while it is falling.
        # If integration has already carried the body slightly through the
        # floor this keeps the ray above the surface, allowing raycast_down()
        # to see it instead of starting underneath it.
        previous_bottom = self._previous_bottom(previous_bottom)

        for i in indices:
            i = int(i)
            center = self._position[i] + self._offset[i]
            current_bottom = float(center[1] - self._half[i, 1])
            ray_y = max(current_bottom, float(previous_bottom[i])) + 1.0

            # A pushed barrel can straddle a floor seam while its centre is
            # briefly over the seam itself.  Sample the centre plus four
            # in-footprint support points so horizontal motion cannot make a
            # grounded body lose contact just because its centre crossed a
            # small gap between floor brushes.
            hx = float(self._half[i, 0]) * 0.75
            hz = float(self._half[i, 2]) * 0.75
            support_points = (
                (float(center[0]), float(center[2])),
                (float(center[0] - hx), float(center[2])),
                (float(center[0] + hx), float(center[2])),
                (float(center[0]), float(center[2] - hz)),
                (float(center[0]), float(center[2] + hz)),
            )

            best_floor = None
            for ray_x, ray_z in support_points:
                try:
                    floor = raycast(ray_x, ray_z, ray_y)
                except Exception:
                    floor = None
                if floor is not None and (
                    best_floor is None or floor > best_floor
                ):
                    best_floor = float(floor)

            if best_floor is not None:
                floors[i] = best_floor

        return floors


    def step(self, delta, player=None):
        self._pack()
        if not self._entities:
            return

        base_dt = float(delta) or 1.0 / 60.0
        scale = max(0.0, float(self.time_scale))
        dt = np.float32(min(0.05, max(0.0, base_dt * scale)))
        active = self._physics_enabled & ~self._kinematic
        active &= ~self._disabled_mask(active)

        if player is not None:
            ppos = np.asarray(getattr(player, 'pos', (0.0, 0.0, 0.0)), dtype=np.float32)
            ph = getattr(player, '_half', None)
            if ph is not None:
                player_half = np.asarray([float(ph.x), float(ph.y), float(ph.z)], dtype=np.float32)
            else:
                player_half = np.asarray([
                    float(getattr(player, 'width', 50.0)) * 0.5,
                    float(getattr(player, 'height', 100.0)) * 0.5,
                    float(getattr(player, 'depth', 50.0)) * 0.5,
                ], dtype=np.float32)

            pvel = getattr(player, 'velocity', None)
            player_velocity = np.asarray([
                float(getattr(pvel, 'x', 0.0)),
                0.0,
                float(getattr(pvel, 'z', 0.0)),
            ], dtype=np.float32)

            center = self._position + self._offset
            body_min = center - self._half
            body_max = center + self._half
            player_min = ppos - player_half
            player_max = ppos + player_half

            overlap = (
                (body_max[:, 0] > player_min[0]) &
                (body_min[:, 0] < player_max[0]) &
                (body_max[:, 1] > player_min[1]) &
                (body_min[:, 1] < player_max[1]) &
                (body_max[:, 2] > player_min[2]) &
                (body_min[:, 2] < player_max[2])
            )
            pushable = active & self._solid & ~self._kinematic & overlap
            speed = float(np.hypot(player_velocity[0], player_velocity[2]))
            if speed >= 0.01 and np.any(pushable):
                dx = center[:, 0] - ppos[0]
                dz = center[:, 2] - ppos[2]
                overlap_x = player_half[0] + self._half[:, 0] - np.abs(dx)
                overlap_z = player_half[2] + self._half[:, 2] - np.abs(dz)
                use_x = overlap_x <= overlap_z

                x_indices = pushable & use_x
                z_indices = pushable & ~use_x

                x_dir = np.where(dx >= 0.0, 1.0, -1.0).astype(np.float32)
                z_dir = np.where(dz >= 0.0, 1.0, -1.0).astype(np.float32)

                self._position[x_indices, 0] += x_dir[x_indices] * (overlap_x[x_indices] + 0.5)
                self._position[z_indices, 2] += z_dir[z_indices] * (overlap_z[z_indices] + 0.5)

                target_x = np.abs(player_velocity[0]) / self._mass * 0.85
                target_z = np.abs(player_velocity[2]) / self._mass * 0.85
                self._velocity[x_indices, 0] = x_dir[x_indices] * np.maximum(
                    target_x[x_indices], np.abs(self._velocity[x_indices, 0])
                )
                self._velocity[z_indices, 2] = z_dir[z_indices] * np.maximum(
                    target_z[z_indices], np.abs(self._velocity[z_indices, 2])
                )
                self._awake[pushable] = True

        moving = active & self._awake
        if np.any(moving):
            if np.any(moving & (self._gravity != 0.0)):
                self._velocity[moving, 1] += (
                    self._gravity[moving] * np.float32(self.gravity) * dt
                )

            damping = self._damping * np.float32(max(0.0, float(self.damping_scale)))
            self._velocity[moving, 1] *= np.maximum(
                0.0, 1.0 - damping[moving] * dt
            )

            horizontal_damp = np.maximum(
                0.0,
                1.0 - damping * dt,
            )
            self._velocity[moving, 0] *= horizontal_damp[moving]
            self._velocity[moving, 2] *= horizontal_damp[moving]

            old_position = self._position.copy()
            old_center = old_position + self._offset
            previous_bottom = old_center[:, 1] - self._half[:, 1]

            self._position[moving, 0] += self._velocity[moving, 0] * dt
            self._batch_static_collision(0, old_position)

            self._position[moving, 2] += self._velocity[moving, 2] * dt
            self._batch_static_collision(2, old_position)

            self._position[moving, 1] += self._velocity[moving, 1] * dt
            floors = self._batch_floor(previous_bottom)
            new_center = self._position + self._offset
            new_bottom = new_center[:, 1] - self._half[:, 1]
            landed = moving & np.isfinite(floors) & (
                new_bottom <= floors + 1.0
            )
            if np.any(landed):
                self._position[landed, 1] = (
                    floors[landed]
                    - self._offset[landed, 1]
                    + self._half[landed, 1]
                )
                self._velocity[landed, 1] = 0.0

                # Treat friction as a surface coefficient rather than a tiny
                # per-frame damping term. Apply it only while grounded.
                friction_accel = (
                    self._friction[landed]
                    * np.float32(max(0.0, float(self.friction_scale)))
                    * np.float32(abs(float(self.gravity)))
                )
                ground_speed = np.hypot(
                    self._velocity[landed, 0],
                    self._velocity[landed, 2],
                )
                friction_delta = friction_accel * dt
                scale = np.maximum(
                    0.0,
                    1.0 - friction_delta / np.maximum(ground_speed, 1e-6),
                )
                self._velocity[landed, 0] *= scale
                self._velocity[landed, 2] *= scale

            angular = self._angular_velocity
            if np.any(angular):
                self._integrate_rotation(angular, dt)

            speed = np.max(np.abs(self._velocity), axis=1)
            sleeping = (
                self.sleep_enabled
                & moving
                & (speed < self.REST_SPEED)
            )
            if np.any(sleeping):
                self._velocity[sleeping] = 0.0
                rest_indices = np.flatnonzero(sleeping)
                self._awake[sleeping] = False
                for i in rest_indices:
                    body = self.bodies[id(self._entities[int(i)])]
                    callback = body.rest_callback
                    if callback is not None:
                        callback(body.entity)

            self._sync_entities(np.flatnonzero(moving))

