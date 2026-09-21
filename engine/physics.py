import math
import glm

from .constants import is_water_brush, brush_aabb_bounds
from .spatial import CELL_SIZE, CellIndex, authored_hidden

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
    def __init__(self, cell_size=CELL_SIZE):
        self.cell_size = cell_size
        self._index = CellIndex(cell_size)
        self.cells = self._index.cells
        self._all_solid = []          # flat list kept for ray queries that span many cells
        self.water_brushes = []       # non-solid water volumes, for swim physics queries

    def clear(self):
        self.cells.clear()
        self._all_solid.clear()
        self.water_brushes.clear()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def populate(self, brushes):
        """Builds the grid from a list of brushes.  Call once on play-mode enter
        and again whenever the static brush list changes (rare)."""
        self.clear()
        for brush in brushes:
            # `authored_hidden` rather than `hidden`: a cell-streaming layer
            # parks out-of-range brushes by hiding them, and this grid outlives
            # that — rebuilding it mid-play (a model-collision toggle) would
            # otherwise drop every parked brush from collision for good, even
            # after its cell came back.
            if authored_hidden(brush) or brush.get('is_fog'):
                continue
            # Physics-enabled Props are dynamic bodies. Their collision is
            # resolved by PropSession rather than the static world grid.
            if brush.get('_dynamic_prop'):
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

    def has_line_of_sight(self, start, end, intersect_ray_aabb_fn):
        """Return True if ray from start to end hits no solid wall brush.
        Uses the grid to only test brushes in cells the ray passes through.

        FIX#11: Now checks neighbouring cells at each sample point to avoid
        missing brushes that straddle cell boundaries on diagonal rays."""
        ray_dir = end - start
        ray_len = glm.length(ray_dir)
        if ray_len < 0.001:
            return True
        ray_dir = ray_dir / ray_len

        # PERF: hoist the ray endpoints/direction to scalars once and inline the
        # slab test below (bit-identical to intersect_ray_aabb_fn). This avoids
        # two throwaway glm.vec3 constructions + a Python call per brush along
        # the ray -- the dominant cost of AI line-of-sight at tick rate. The
        # signature keeps intersect_ray_aabb_fn so existing callers are unchanged.
        ox, oy, oz = start.x, start.y, start.z
        rdx, rdy, rdz = ray_dir.x, ray_dir.y, ray_dir.z
        limit = ray_len - 0.1
        cell_size = self.cell_size

        # Gather cells along the ray path + neighbours. Test each unique brush
        # immediately so no temporary candidate list or second traversal is needed.
        steps = max(1, int(ray_len / cell_size) + 2)
        cells = self.cells
        seen = set()
        seen_add = seen.add
        for i in range(steps + 1):
            t = min(i / float(steps), 1.0) * ray_len
            pt = start + ray_dir * t
            cx = int(math.floor(pt.x / cell_size))
            cz = int(math.floor(pt.z / cell_size))
            # FIX#11: Check the cell AND its 8 neighbours to catch brushes
            # that straddle cell boundaries on diagonal rays.
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for brush in cells.get((cx + dx, cz + dz), ()):
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
