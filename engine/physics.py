import math
import glm
import numpy as np

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
    """Lightweight handle for one row in the engine's batched physics state."""

    def __init__(self, world, entity, entity_id):
        self.world = world
        self.entity = entity
        self.entity_id = entity_id
        self.rest_callback = None

    @property
    def _index(self):
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

    def set_rest_callback(self, entity, callback):
        body = self.get_body(entity)
        if body is not None:
            body.set_rest_callback(callback)

    def wake_all(self):
        """Wake every non-kinematic physics body for runtime console tuning."""
        self._pack()
        if not self._entities:
            return
        active = self._physics_enabled & ~self._kinematic
        self._awake[active] = True

    def _sync_entities(self, indices=None):
        if indices is None:
            indices = range(len(self._entities))
        for i in indices:
            entity = self._entities[int(i)]
            entity.pos[:] = self._position[int(i)].tolist()

            angular = self._angular_velocity[int(i)]
            if np.any(angular):
                props = getattr(entity, 'properties', {})
                rotation = props.get('rotation', [0.0, 0.0, 0.0])
                props['rotation'] = (
                    np.asarray(rotation, dtype=np.float32) + angular
                ).tolist()

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
        """Raycast down beneath each active dynamic body to find its floor."""
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
        if previous_bottom is None:
            previous_bottom = (
                (self._position + self._offset)[:, 1] - self._half[:, 1]
            )
        else:
            previous_bottom = np.asarray(previous_bottom, dtype=np.float32)

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
                # Rotation remains an entity-property boundary operation; the
                # arithmetic itself is still one NumPy operation.
                for i in np.flatnonzero(np.any(angular != 0.0, axis=1)):
                    entity = self._entities[int(i)]
                    props = getattr(entity, 'properties', {})
                    rotation = np.asarray(props.get('rotation', [0.0, 0.0, 0.0]), dtype=np.float32)
                    props['rotation'] = (rotation + angular[i] * dt).tolist()

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

