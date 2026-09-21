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

class PhysicsBody:
    """Engine-owned dynamic rigid body backed by a Thing-like entity."""

    def __init__(self, entity, brush, rest_callback=None):
        self.entity = entity
        self.brush = brush
        self.rest_callback = rest_callback
        self.kinematic = False
        self.awake = False
        self.velocity = [0.0, 0.0, 0.0]
        props = getattr(entity, 'properties', {})
        self.mass = max(0.01, float(props.get('mass', 1.0)))
        self.gravity = bool(props.get('gravity', True))
        self.friction = max(0.0, min(1.0, float(props.get('friction', 0.55))))
        self.linear_damping = max(0.0, float(props.get('linear_damping', 0.08)))
        angular = props.get('drop_angular_velocity', [0.0, 0.0, 0.0])
        self.angular_velocity = [float(angular[i]) for i in range(3)]
        self.size, self.offset = self._shape_from_brush(brush)

    def _shape_from_brush(self, brush):
        bounds = brush.get('_mesh_bounds') if brush.get('_collision_mode') == 'mesh' else None
        if bounds:
            min_v, max_v = bounds
            center = tuple((float(min_v[i]) + float(max_v[i])) * 0.5 for i in range(3))
            size = tuple(float(max_v[i]) - float(min_v[i]) for i in range(3))
        else:
            center = tuple(float(v) for v in brush.get('pos', (0.0, 0.0, 0.0)))
            size = tuple(float(v) for v in brush.get('size', (64.0, 64.0, 64.0)))
        pos = tuple(float(v) for v in getattr(self.entity, 'pos', (0.0, 0.0, 0.0)))
        return size, tuple(center[i] - pos[i] for i in range(3))

    @property
    def solid(self):
        return not bool(getattr(self.entity, 'properties', {}).get('no_collision', True))

    def set_kinematic(self, value):
        self.kinematic = bool(value)
        if self.kinematic:
            self.velocity[:] = [0.0, 0.0, 0.0]
            self.awake = False

    def wake(self, velocity=None):
        if velocity is not None:
            self.velocity[:] = [float(v) for v in velocity]
        self.awake = True
        self.kinematic = False

    def set_rest_callback(self, callback):
        self.rest_callback = callback

    def clear(self):
        self.rest_callback = None
        self.kinematic = False
        self.awake = False
        self.velocity[:] = [0.0, 0.0, 0.0]

    def _fire_rest(self):
        if self.rest_callback is not None:
            self.rest_callback(self.entity)

class PhysicsWorld:
    """Engine-level dynamic-body simulation and world collision."""

    def __init__(self, spatial_grid):
        self.spatial_grid = spatial_grid
        self.bodies = {}

    def clear(self):
        for body in self.bodies.values():
            body.clear()
        self.bodies.clear()

    def register_body(self, entity, brush, rest_callback=None):
        body = PhysicsBody(entity, brush, rest_callback)
        self.bodies[id(entity)] = body
        return body

    def get_body(self, entity):
        return self.bodies.get(id(entity))

    def set_rest_callback(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.set_rest_callback(callback)

    def set_kinematic(self, entity, value):
        body = self.get_body(entity)
        if body is not None:
            body.set_kinematic(value)

    def wake(self, entity, velocity=None):
        body = self.get_body(entity)
        if body is not None:
            body.wake(velocity)

    @staticmethod
    def _bounds(body):
        pos = body.entity.pos
        center = tuple(float(pos[i]) + body.offset[i] for i in range(3))
        half = tuple(v * 0.5 for v in body.size)
        return center, half

    @staticmethod
    def _overlaps_brush(center, half, brush):
        if brush.get('_collision_mode') == 'mesh':
            bounds = brush.get('_mesh_bounds')
            if bounds:
                lo, hi = bounds
                return (center[0] + half[0] > lo[0] and center[0] - half[0] < hi[0] and
                        center[1] + half[1] > lo[1] and center[1] - half[1] < hi[1] and
                        center[2] + half[2] > lo[2] and center[2] - half[2] < hi[2])
            return False
        pos = brush.get('pos', (0.0, 0.0, 0.0))
        size = brush.get('size', (0.0, 0.0, 0.0))
        return (center[0] + half[0] > pos[0] - size[0] * 0.5 and
                center[0] - half[0] < pos[0] + size[0] * 0.5 and
                center[1] + half[1] > pos[1] - size[1] * 0.5 and
                center[1] - half[1] < pos[1] + size[1] * 0.5 and
                center[2] + half[2] > pos[2] - size[2] * 0.5 and
                center[2] - half[2] < pos[2] + size[2] * 0.5)

    def _floor_y(self, body):
        if not body.solid:
            return None
        raycast = getattr(self.spatial_grid, 'raycast_down', None)
        if raycast is None:
            return None
        center, half = self._bounds(body)
        try:
            return raycast(center[0], center[2], center[1] + half[1] + 1.0)
        except Exception:
            return None

    def _move_horizontal(self, body, axis, amount):
        if abs(amount) < 0.00001:
            return
        before = list(body.entity.pos)
        body.entity.pos[axis] += amount
        if not body.solid:
            return
        center, half = self._bounds(body)
        query = getattr(self.spatial_grid, 'get_potential_colliders', None)
        if query is None:
            return
        colliders = query(glm.vec3(center[0]-half[0], center[1]-half[1], center[2]-half[2]),
                          glm.vec3(center[0]+half[0], center[1]+half[1], center[2]+half[2]))
        for brush in colliders:
            if brush.get('_physics_body'):
                continue
            if self._overlaps_brush(center, half, brush):
                body.entity.pos[:] = before
                body.velocity[axis] = 0.0
                return

    def _push_from_player(self, body, player):
        if body.kinematic or not body.solid:
            return
        ph = getattr(player, '_half', None)
        if ph is not None:
            player_half = (float(ph.x), float(ph.y), float(ph.z))
        else:
            player_half = (float(getattr(player, 'width', 50.0))*0.5, float(getattr(player, 'height', 100.0))*0.5, float(getattr(player, 'depth', 50.0))*0.5)
        ppos = player.pos
        pvel = getattr(player, 'velocity', None)
        pv = (float(getattr(pvel, 'x', 0.0)), float(getattr(pvel, 'z', 0.0)))
        if math.hypot(*pv) < 0.01:
            return
        center, half = self._bounds(body)
        dx = center[0] - float(ppos[0])
        dz = center[2] - float(ppos[2])
        ox = player_half[0] + half[0] - abs(dx)
        oz = player_half[2] + half[2] - abs(dz)
        if (ox <= 0.0 or oz <= 0.0 or
                float(ppos[1])+player_half[1] <= center[1]-half[1] or
                float(ppos[1])-player_half[1] >= center[1]+half[1]):
            return
        if ox <= oz:
            direction = 1.0 if dx >= 0.0 else -1.0
            body.entity.pos[0] += direction * (ox + 0.5)
            body.velocity[0] = direction * max(abs(pv[0]) / body.mass * 0.85, abs(body.velocity[0]))
        else:
            direction = 1.0 if dz >= 0.0 else -1.0
            body.entity.pos[2] += direction * (oz + 0.5)
            body.velocity[2] = direction * max(abs(pv[1]) / body.mass * 0.85, abs(body.velocity[2]))
        body.awake = True

    def step(self, delta, player=None):
        dt = min(0.05, max(0.0, float(delta) or 1.0/60.0))
        for body in tuple(self.bodies.values()):
            props = getattr(body.entity, 'properties', {})
            if body.kinematic or props.get('disabled') or not props.get('physics_enabled', False):
                continue
            if player is not None:
                self._push_from_player(body, player)
            if not body.awake:
                continue
            if body.gravity:
                body.velocity[1] += -900.0 * dt
            body.velocity[1] *= max(0.0, 1.0 - body.linear_damping * dt)
            damp = max(0.0, 1.0 - (body.linear_damping + body.friction) * dt)
            body.velocity[0] *= damp
            body.velocity[2] *= damp
            self._move_horizontal(body, 0, body.velocity[0] * dt)
            self._move_horizontal(body, 2, body.velocity[2] * dt)
            pos = body.entity.pos
            new_y = float(pos[1]) + body.velocity[1] * dt
            floor = self._floor_y(body)
            if floor is not None and new_y <= floor:
                pos[1] = floor
                body.velocity[1] = 0.0
            else:
                pos[1] = new_y
            angular = body.angular_velocity
            rotation = props.get('rotation', [0.0, 0.0, 0.0])
            props['rotation'] = [float(rotation[i]) + angular[i] * dt for i in range(3)]
            if max(abs(v) for v in body.velocity) < 1.0:
                body.velocity[:] = [0.0, 0.0, 0.0]
                if body.awake:
                    body.awake = False
                    body._fire_rest()

    def rebuild(self, brushes):
        self.clear()
        for brush in brushes:
            if not brush.get('_physics_body'):
                continue
            entity = brush.get('_physics_entity')
            if entity is not None:
                self.register_body(entity, brush)