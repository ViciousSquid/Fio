"""Collision and world-geometry runtime delegated from LogicThread.

Owns angled-brush mesh collision preparation, model collision generation,
collision-shape bounds, model-collision toggling, and the combined collision
brush cache. LogicThread retains the timing/orchestration surface.
"""

from __future__ import annotations

import math
import os

import numpy as np

from .brush_geometry import build_collision_mesh, brush_has_geometry
from .constants import is_water_brush

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


COLLISION_KEYS = (
    "_collision_mode",
    "_mesh_triangles",
    "_mesh_bounds",
    "_mesh_planes",
)


class CollisionRows:
    """Every box in the collision brush cache, as rows in the cache's order.

    What the per-brush walks over ``_collision_brushes_cache`` read, held as
    arrays so a query is a handful of NumPy operations instead of a Python
    loop over every brush. Two sets of boxes, because the two walks round
    differently:

    * ``lo32``/``hi32``: the corners the hitscan walk tests, ``glm.vec3(pos)
      -/+ glm.vec3(size) * 0.5`` -- rounded to float32 -- held as float64,
      since the slab test itself then runs in Python floats;
    * ``lo``/``hi``: the corners the projectile wall walk tests, ``pos -/+
      size * 0.5`` in float64.

    Only geometry lives here. Whether a brush blocks (``hidden``, water, fog,
    trigger) is asked of the brush itself, live, for the few rows a query
    returns: a streaming layer or an I/O input can change those at any time.
    Movers and doors are the only brushes whose ``pos`` changes during play,
    and their rows are re-read on every query (:meth:`refresh_movers`), which
    is what the walks do.

    Rebuilt when the cache list is replaced or the editor's world epoch moves;
    see :meth:`LogicCollision.collision_rows`.
    """

    __slots__ = ('source', 'epoch', 'brushes', 'lo32', 'hi32', 'lo', 'hi',
                 'mover_rows', 'movers')

    def __init__(self, brushes, epoch):
        self.source = brushes
        self.epoch = epoch
        self.brushes = list(brushes)
        count = len(self.brushes)
        # One contiguous row per axis: every query reads a whole axis at once.
        self.lo32 = np.empty((3, count), dtype=np.float64)
        self.hi32 = np.empty((3, count), dtype=np.float64)
        self.lo = np.empty((3, count), dtype=np.float64)
        self.hi = np.empty((3, count), dtype=np.float64)
        self._resolve(slice(None), self.brushes)
        movers = [row for row, b in enumerate(self.brushes)
                  if b.get('is_mover') or b.get('is_door')]
        self.mover_rows = np.asarray(movers, dtype=np.intp)
        self.movers = [self.brushes[row] for row in movers]

    def _resolve(self, rows, brushes):
        count = len(brushes)
        pos = np.array([(p[0], p[1], p[2]) for p in (b['pos'] for b in brushes)],
                       dtype=np.float64).reshape(count, 3).T
        size = np.array([(s[0], s[1], s[2]) for s in (b['size'] for b in brushes)],
                        dtype=np.float64).reshape(count, 3).T
        pos32 = pos.astype(np.float32)
        half32 = size.astype(np.float32) * np.float32(0.5)
        self.lo32[:, rows] = pos32 - half32
        self.hi32[:, rows] = pos32 + half32
        half = size * 0.5
        self.lo[:, rows] = pos - half
        self.hi[:, rows] = pos + half

    def refresh_movers(self):
        if self.movers:
            self._resolve(self.mover_rows, self.movers)

    def ray_hits(self, origin, direction, reach):
        """Rows whose box the ray meets within *reach*, nearest first.

        The slab test of :meth:`LogicCombat.intersect_ray_aabb` on every row
        at once, in the same float64 arithmetic: ``(rows, dist)``, ordered by
        distance and then by row, so the first row that blocks is the one the
        walk would have kept (it keeps the first brush at the least distance).
        """
        count = self.lo32.shape[1]
        t_min = np.zeros(count)
        t_max = np.full(count, float(reach))
        t1 = np.empty(count)
        t2 = np.empty(count)
        hit = None
        for axis in range(3):
            lo = self.lo32[axis]
            hi = self.hi32[axis]
            o = float(origin[axis])
            d = float(direction[axis])
            if abs(d) < 1e-6:
                within = (lo <= o) & (o <= hi)
                hit = within if hit is None else hit & within
                continue
            inv_d = 1.0 / d
            np.multiply(np.subtract(lo, o, out=t1), inv_d, out=t1)
            np.multiply(np.subtract(hi, o, out=t2), inv_d, out=t2)
            np.maximum(t_min, np.minimum(t1, t2), out=t_min)
            np.minimum(t_max, np.maximum(t1, t2), out=t_max)
        if hit is None:
            hit = t_min <= t_max
        else:
            hit &= t_min <= t_max
        rows = np.flatnonzero(hit)
        dist = t_min[rows]
        if len(rows) > 1:
            order = np.lexsort((rows, dist))
            rows, dist = rows[order], dist[order]
        # + 0.0: the walk's distance starts at 0.0 and can never become -0.0.
        return rows, dist + 0.0

    def point_hits(self, x, y, z):
        """Rows whose float64 box holds the point, boundaries included."""
        lo, hi = self.lo, self.hi
        return np.flatnonzero(
            (lo[0] <= x) & (x <= hi[0])
            & (lo[1] <= y) & (y <= hi[1])
            & (lo[2] <= z) & (z <= hi[2]))


class LogicCollision:
    """Runtime for static and model-backed collision geometry."""

    def __init__(self, logic):
        self.logic = logic
        self._dirty = False
        self.model_collision_enabled = True
        self._model_collision_brushes = []
        self._physics_body_brushes = []
        self._collision_brushes_cache = []
        self._collision_rows = None

    def mark_dirty(self):
        self._dirty = True

    def rebuild_if_dirty(self) -> bool:
        if not self._dirty:
            return False
        self._dirty = False
        logic = self.logic
        with logic.session_runtime.monster_lock:
            logic.world_runtime.notify_authored_visibility_changed()
        return True

    def angled_brush_is_solid(self, brush):
        """Which angled brushes get solid mesh collision.

        Mirrors the player's own collision-skip logic (hidden / water / fog /
        trigger volumes are non-solid) and excludes movers/doors, which keep
        their existing dynamic AABB path.  Non-solid angled brushes simply fall
        through to the default AABB handling — a water/trigger volume never
        blocks the player, angled or not.
        """
        logic = self.logic
        if brush.get('hidden') or brush.get('is_fog'):
            return False
        if is_water_brush(brush):
            return False
        if brush.get('operation') == 'subtract':
            return False
        if brush.get('is_mover') or brush.get('is_door'):
            return False
        if brush.get('is_trigger'):
            return False
        return True


    def prepare_angled_brush_collision(self):
        """Attach swept-mesh collision to angled (clipped/convex) brushes.

        Angled brushes carry a ``geometry`` plane set instead of a plain box, so
        they can't collide as an AABB.  Here we bake each solid angled brush into
        world-space collision triangles and flag it ``_collision_mode='mesh'`` —
        the exact format the player's collide-and-slide path already uses for
        models — so ramps and wedges collide correctly and you can walk up
        slopes.  Box brushes are left untouched and keep the fast AABB path.

        Runs at play start; results are private keys stripped on save.
        """
        logic = self.logic
        count = 0
        for brush in logic.editor_state.brushes:
            if not brush_has_geometry(brush):
                continue
            if not self.angled_brush_is_solid(brush):
                # Ensure a previously-solid brush that became non-solid loses
                # its stale mesh flag.
                self.clear_brush_collision(brush)
                continue
            if build_collision_mesh(brush):
                count += 1
            else:
                # Degenerate geometry — fall back to AABB rather than break.
                self.clear_brush_collision(brush)
        if count:
            debug_log("Collision", f"Prepared mesh collision for {count} angled brush(es)")
        return count

    #: What build_collision_mesh attaches, and all a revert may remove. The
    #: rest of GEO_RUNTIME_KEYS is the brush's geometry identity and cache:
    #: popping ``_geo_epoch`` gave every angled brush a new epoch behind the
    #: render tables' back at each play start/stop, so their rows held stale
    #: records and every convex shape was re-derived.
    _COLLISION_KEYS = ('_collision_mode', '_mesh_triangles', '_mesh_bounds',
                       '_mesh_planes')


    @staticmethod
    def clear_brush_collision(brush):
        """Strip runtime mesh-collision keys so the brush reverts to AABB."""
        for k in COLLISION_KEYS:
            brush.pop(k, None)


    def clear_angled_brush_collision(self):
        logic = self.logic
        """Remove play-time mesh-collision data from all angled brushes."""
        for brush in logic.editor_state.brushes:
            if brush_has_geometry(brush):
                self.clear_brush_collision(brush)


    def build_model_collision_brushes(self):
        """Create collision data for model entities.

        Props can explicitly choose Automatic, AABB, or Mesh collision. A
        non-zero collision_size always overrides the shape choice with a
        custom AABB.
        """
        logic = self.logic
        model_collision_enabled = self.model_collision_enabled
        brushes = []

        for thing in logic.editor_state.things:
            props = getattr(thing, 'properties', {})
            if not props.get('model_path'):
                continue
            physics_enabled = bool(props.get('physics_enabled', False))
            if not model_collision_enabled and not physics_enabled:
                continue
            if props.get('no_collision', False) and not physics_enabled:
                continue

            pos = getattr(thing, 'pos', [0, 0, 0])
            if hasattr(pos, 'x'):
                pos = [pos.x, pos.y, pos.z]
            else:
                pos = list(pos)

            scale = props.get('scale', 1.0)
            if isinstance(scale, (int, float)):
                scale = [scale, scale, scale]
            else:
                scale = list(scale)

            rot = props.get('rotation', [0, 0, 0])
            is_physics_body = physics_enabled
            collision_shape = str(
                props.get('collision_shape', 'auto')
            ).lower()

            # A non-zero explicit collision_size always forces a custom AABB.
            collision_size = props.get('collision_size')
            has_collision_size = (
                isinstance(collision_size, (list, tuple))
                and len(collision_size) == 3
                and any(float(v) != 0.0 for v in collision_size)            )

            if has_collision_size:
                size = list(collision_size)
                brushes.append({
                    'pos': pos,
                    'size': size,
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'aabb',
                })
                continue

            # A Prop drawn as a sprite has no model-shaped collision.
            # ``model_path`` alone decides whether this loop looks at a Thing,
            # which is right for a Model entity but wrong for a Prop: a Prop
            # keeps its mesh path when its representation is switched back to
            # Billboard, and would otherwise collide as a mesh nobody can see.
            # An explicit collision_size still applies -- that is authored for
            # the entity, not derived from the model -- and is handled above.
            if str(props.get('render_mode', 'model')).lower() == 'billboard':
                continue

            model_path = props.get('model_path', '')

            # Explicit AABB mode skips mesh loading and always uses the model's
            # scaled bounds. This is useful for barrels, bricks and other props
            # where a stable box is preferable to triangle-level collision.
            if collision_shape == 'aabb':
                bounds = self.compute_model_bounds(model_path)
                if bounds:
                    min_v, max_v = bounds
                    size = [
                        (max_v[i] - min_v[i]) * scale[i]
                        for i in range(3)
                    ]
                    local_centre = [
                        (min_v[i] + max_v[i]) * 0.5
                        for i in range(3)
                    ]
                    aabb_pos = [
                        pos[i] + local_centre[i] * scale[i]
                        for i in range(3)
                    ]
                else:
                    base = 64.0
                    size = [base * scale[i] for i in range(3)]
                    aabb_pos = pos

                brushes.append({
                    'pos': aabb_pos,
                    'size': size,
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'aabb',
                })
                continue

            # Automatic uses mesh collision where supported. Explicit Mesh
            # behaves the same today and falls back to AABB if the model cannot
            # provide mesh collision.
            mesh_tris = self.compute_model_collision_mesh(
                model_path, pos, scale, rot
            )
            if mesh_tris and collision_shape in ('auto', 'mesh'):
                brushes.append({
                    'pos': pos,
                    'size': [1, 1, 1],
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'mesh',
                    '_mesh_triangles': mesh_tris,
                    '_mesh_bounds': self.compute_mesh_bounds(mesh_tris),
                })
                continue

            # Fallback for Automatic/Mesh when the model has no CPU collision
            # mesh (for example OBJ today).
            bounds = self.compute_model_bounds(model_path)
            if bounds:
                min_v, max_v = bounds
                size = [
                    (max_v[i] - min_v[i]) * scale[i]
                    for i in range(3)
                ]
                local_centre = [
                    (min_v[i] + max_v[i]) * 0.5
                    for i in range(3)
                ]
                aabb_pos = [
                    pos[i] + local_centre[i] * scale[i]
                    for i in range(3)
                ]
            else:
                base = 64.0
                size = [base * scale[i] for i in range(3)]
                aabb_pos = pos

            brushes.append({
                'pos': aabb_pos,
                'size': size,
                'hidden': False,
                'is_trigger': False,
                'is_mover': False,
                'is_door': False,
                'is_water': False,
                'is_fog': False,
                '_model_collision': True,
                '_physics_entity': thing,
                '_physics_body': is_physics_body,
                '_collision_mode': 'aabb',
            })

        return brushes


    def compute_model_collision_mesh(self, model_path, world_pos, scale, rotation):
        """Load model and return world-space triangles for collision.

        Uses GLBLoader (CPU-only, no OpenGL calls) so this is safe to call from
        any thread regardless of whether a GL context is current.  The old path
        used GLB which called glGenVertexArrays/glGenBuffers and would silently
        fail when the GL context was not active on this thread.
        """
        logic = self.logic
        if not model_path:
            return None

        full_path = os.path.join('assets', 'models', model_path)
        from engine.asset_archives import asset_exists
        if not asset_exists(full_path):
            full_path = model_path
        if not asset_exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        if ext != '.glb':
            return None  # Only GLB supports mesh collision for now

        try:
            # GLBLoader is pure file I/O + JSON parsing — zero OpenGL calls.
            from .glb_loader import GLBLoader

            loader = GLBLoader()
            loader._filepath_hint = full_path
            if not loader.load(full_path):
                debug_log("Collision", f"GLBLoader failed to load {model_path}")
                return None

            all_verts = loader.get_flattened_vertices()   # list of (x, y, z)
            all_tris  = loader.get_flattened_triangles()  # list of (i0, i1, i2)

            if not all_verts or not all_tris:
                debug_log("Collision", f"No geometry in {model_path}")
                return None

            # Build rotation matrix from euler angles (YXZ order, matching renderer)
            yaw, pitch, roll = (math.radians(rotation[1]),
                                math.radians(rotation[0]),
                                math.radians(rotation[2]))
            cy, sy = math.cos(yaw),   math.sin(yaw)
            cp, sp = math.cos(pitch), math.sin(pitch)
            cr, sr = math.cos(roll),  math.sin(roll)

            def transform_point(x, y, z):
                # Scale
                x, y, z = x * scale[0], y * scale[1], z * scale[2]
                # Rotate Y (yaw)
                x, z = x * cy - z * sy, x * sy + z * cy
                # Rotate X (pitch)
                y, z = y * cp - z * sp, y * sp + z * cp
                # Rotate Z (roll)
                x, y = x * cr - y * sr, x * sr + y * cr
                # Translate to world
                return (x + world_pos[0], y + world_pos[1], z + world_pos[2])

            world_tris = []
            for i0, i1, i2 in all_tris:
                if i0 >= len(all_verts) or i1 >= len(all_verts) or i2 >= len(all_verts):
                    continue
                w0 = transform_point(*all_verts[i0])
                w1 = transform_point(*all_verts[i1])
                w2 = transform_point(*all_verts[i2])

                # Compute face normal from world-space edge vectors
                e1 = (w1[0]-w0[0], w1[1]-w0[1], w1[2]-w0[2])
                e2 = (w2[0]-w0[0], w2[1]-w0[1], w2[2]-w0[2])
                nx = e1[1]*e2[2] - e1[2]*e2[1]
                ny = e1[2]*e2[0] - e1[0]*e2[2]
                nz = e1[0]*e2[1] - e1[1]*e2[0]
                length = math.sqrt(nx*nx + ny*ny + nz*nz)
                w_normal = (nx/length, ny/length, nz/length) if length > 0.001 else (0.0, 1.0, 0.0)

                world_tris.append(((w0, w1, w2), w_normal))

            debug_log("Collision", f"Built {len(world_tris)} mesh-collision tris for {model_path}")
            return world_tris if world_tris else None

        except Exception as e:
            debug_log("Collision", f"Failed to build mesh collision for {model_path}: {e}")
            return None


    def compute_mesh_bounds(self, mesh_tris):
        logic = self.logic
        """Compute AABB from mesh triangles for broad-phase culling."""
        if not mesh_tris:
            return None
        all_verts = []
        for (v0, v1, v2), _ in mesh_tris:
            all_verts.extend([v0, v1, v2])
        min_v = [min(v[i] for v in all_verts) for i in range(3)]
        max_v = [max(v[i] for v in all_verts) for i in range(3)]
        return (min_v, max_v)


    def compute_model_bounds(self, model_path):
        logic = self.logic
        """Compute axis-aligned bounds from a model file. Returns (min, max) or None."""
        if not model_path:
            return None

        full_path = os.path.join('assets', 'models', model_path)
        if not os.path.exists(full_path):
            full_path = model_path
        if not os.path.exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        try:
            if ext == '.glb':
                from .glb_loader import GLBLoader
                loader = GLBLoader()
                loader._filepath_hint = full_path
                if loader.load(full_path):
                    verts = loader.get_flattened_vertices()
                else:
                    verts = None
            elif ext == '.obj':
                # OBJ collision only needs CPU geometry.  Do not instantiate the
                # OpenGL-backed OBJ model on the logic thread.
                from .obj_loader import OBJLoader
                loader = OBJLoader()
                if loader.load(full_path):
                    source_vertices = np.asarray(loader.vertices, dtype=np.float32)
                    if source_vertices.size == 0:
                        verts = None
                    else:
                        source_min = source_vertices.min(axis=0)
                        source_max = source_vertices.max(axis=0)
                        source_centre = (source_min + source_max) * 0.5
                        half_extent = (source_max - source_min) * 0.5
                        threshold = np.maximum(half_extent * 4.0, 2.0)
                        offset = np.where(
                            np.abs(source_centre) > threshold,
                            source_centre,
                            0.0,
                        ).astype(np.float32)
                        corrected = source_vertices - offset
                        verts = corrected.tolist()
                else:
                    verts = None
            else:
                return None

            if verts:
                min_v = [min(v[i] for v in verts) for i in range(3)]
                max_v = [max(v[i] for v in verts) for i in range(3)]
                return min_v, max_v
        except Exception as e:
            debug_log("Collision", f"Failed to compute {ext.upper()} bounds for {model_path}: {e}")
        return None


    def toggle_model_collision(self, enabled: bool = None) -> bool:
        """Toggle model collision on/off. If enabled is None, flip current state.
        Returns the new state. Works in both play mode and editor mode."""
        logic = self.logic
        if enabled is None:
            self.model_collision_enabled = not self.model_collision_enabled
        else:
            self.model_collision_enabled = bool(enabled)

        # Rebuild collision brushes in both play mode and editor mode
        # (editor mode uses them for visualization via showcollision command)
        if self.model_collision_enabled:
            self._model_collision_brushes = self.build_model_collision_brushes()
            self._physics_body_brushes = [
                b for b in self._model_collision_brushes
                if b.get('_physics_body')
            ]
            if logic.session_runtime.play_mode and logic.session_runtime.spatial_grid is not None:
                logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes + self._model_collision_brushes)
                if logic.session_runtime.physics_world is not None:
                    logic.session_runtime.physics_world.rebuild(self._physics_body_brushes)
        else:
            self._model_collision_brushes = []
            if logic.session_runtime.play_mode and logic.session_runtime.spatial_grid is not None:
                logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes)
                if logic.session_runtime.physics_world is not None:
                    logic.session_runtime.physics_world.rebuild(self._physics_body_brushes)
        self.refresh_collision_brushes_cache()

        return self.model_collision_enabled


    def refresh_collision_brushes_cache(self):
        """Recompute the combined static+model collision brush list.

        PERF: `logic.editor_state.brushes + self._model_collision_brushes` was previously
        rebuilt (a full list concatenation) every single tick — and, worse,
        once per active projectile per tick. Both collections only change
        here (model-collision toggle, play-mode enter/exit), so cache the
        concatenation and reuse it from the hot paths instead.
        """
        logic = self.logic
        self._collision_brushes_cache = logic.editor_state.brushes + self._model_collision_brushes
        self._collision_rows = None

    #: Brushes the collision cache must hold before a whole-cache walk (the
    #: hitscan, the small-swarm projectile wall test) is run as one NumPy
    #: query over :class:`CollisionRows` instead of a Python loop.
    #:
    #: The walk never exits early -- it must see every brush to find the
    #: nearest -- so it costs about 2 us a brush; the query is a fixed ~21 us
    #: of NumPy dispatch until the rows get long. One pellet's brush pass, 200
    #: rays, best of 7:
    #:
    #: =======  =========  ========
    #: brushes  scalar us  dense us
    #: =======  =========  ========
    #:       4        8.2      20.5
    #:       8       16.4      21.5
    #:      16       32.3      21.4
    #:      64      130.2      20.8
    #:     256      552.4      23.4
    #:    3600     7718.4      59.4
    #: =======  =========  ========
    #:
    #: Both answer identically, so this is purely a speed knob; zero means
    #: always dense. Re-measure with
    #: ``tests/performance/test_hitscan_threshold_benchmark.py``.
    DENSE_WALK_MIN_BRUSHES = 16

    def collision_rows(self):
        """:class:`CollisionRows` for the current cache, or None below the
        dense threshold.

        Built on first use after :meth:`refresh_collision_brushes_cache`, and
        again if the cache list is replaced behind its back (Stop assigns an
        empty one) or an editor edit moves the world epoch; movers and doors
        are re-read on every call.
        """
        brushes = self._collision_brushes_cache
        if len(brushes) < self.DENSE_WALK_MIN_BRUSHES:
            return None
        epoch = getattr(self.logic.editor_state, 'world_epoch', 0)
        rows = self._collision_rows
        if rows is None or rows.source is not brushes or rows.epoch != epoch:
            rows = self._collision_rows = CollisionRows(brushes, epoch)
        rows.refresh_movers()
        return rows

    # -- visibility invalidation ------------------------------------------
    #
    # Two notifications, because "what is drawn" and "what is collided with"
    # go stale at different costs.  Both are the *host* side of the streaming
    # contract in ``plugins.bigworld.runtime.StreamingHost``; neither knows
    # anything about a particular streaming layer.

