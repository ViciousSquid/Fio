import math
import glm

from .constants import is_water_brush

class SpatialGrid:
    """
    A 2D spatial partitioning grid to optimize collision detection.
    Groups solid brushes into cells to reduce O(N) collision checks.

    Used by:
      - Player physics  (get_potential_colliders)
      - MonsterAI       (get_nearby_brushes, raycast_down, overlaps_wall, line_of_sight)
    """
    def __init__(self, cell_size=512.0):
        self.cell_size = cell_size
        self.cells = {}
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
            if brush.get('hidden') or brush.get('is_fog'):
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

            min_x = int(math.floor((pos[0] - size[0] * 0.5) / self.cell_size))
            max_x = int(math.floor((pos[0] + size[0] * 0.5) / self.cell_size))
            min_z = int(math.floor((pos[2] - size[2] * 0.5) / self.cell_size))
            max_z = int(math.floor((pos[2] + size[2] * 0.5) / self.cell_size))

            for x in range(min_x, max_x + 1):
                for z in range(min_z, max_z + 1):
                    cell = (x, z)
                    if cell not in self.cells:
                        self.cells[cell] = []
                    self.cells[cell].append(brush)

    # ------------------------------------------------------------------
    # Player queries  (unchanged API)
    # ------------------------------------------------------------------

    def get_potential_colliders(self, player_min, player_max):
        """Returns unique brushes sharing grid cells with the player's AABB."""
        min_x = int(math.floor(player_min.x / self.cell_size))
        max_x = int(math.floor(player_max.x / self.cell_size))
        min_z = int(math.floor(player_min.z / self.cell_size))
        max_z = int(math.floor(player_max.z / self.cell_size))

        colliders = []
        seen = set()

        for x in range(min_x, max_x + 1):
            for z in range(min_z, max_z + 1):
                cell = (x, z)
                if cell in self.cells:
                    for brush in self.cells[cell]:
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

        result = []
        seen = set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                cell = (cx, cz)
                if cell in self.cells:
                    for brush in self.cells[cell]:
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

        seen = set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                cell = (cx, cz)
                if cell not in self.cells:
                    continue
                for brush in self.cells[cell]:
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

        # Gather cells along the ray path + neighbours
        steps = max(1, int(ray_len / self.cell_size) + 2)
        seen = set()
        candidates = []
        for i in range(steps + 1):
            t = min(i / float(steps), 1.0) * ray_len
            pt = start + ray_dir * t
            cx = int(math.floor(pt.x / self.cell_size))
            cz = int(math.floor(pt.z / self.cell_size))
            # FIX#11: Check the cell AND its 8 neighbours to catch brushes
            # that straddle cell boundaries on diagonal rays
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    cell = (cx + dx, cz + dz)
                    if cell in self.cells:
                        for brush in self.cells[cell]:
                            bid = id(brush)
                            if bid not in seen:
                                seen.add(bid)
                                candidates.append(brush)

        for brush in candidates:
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            b_min = pos - size * 0.5
            b_max = pos + size * 0.5
            hit, dist = intersect_ray_aabb_fn(start, ray_dir, b_min, b_max)
            if hit and dist < ray_len - 0.1:
                return False
        return True
