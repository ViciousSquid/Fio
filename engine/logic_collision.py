"""Collision and world-geometry runtime delegated from LogicThread.

Owns angled-brush mesh collision preparation, model collision generation,
collision-shape bounds, model-collision toggling, and the combined collision
brush cache. LogicThread retains the timing/orchestration surface.
"""

from __future__ import annotations

import math
import os
from itertools import chain

import numpy as np

from .brush_geometry import build_collision_mesh, brush_has_geometry
from . import brush_placement as _placement
from .brush_placement import shared_bulk_boxes, world_box
from .constants import brush_still_box, is_water_brush

_chain = chain.from_iterable

#: (-1, +1) as a (2, 1, 1) column: half-extents to the low and high corner.
_SIGNS = np.array([-1.0, 1.0]).reshape(2, 1, 1)

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


def model_basis(rotation, scale):
    """The 3x3 rotation/scale part of a model Prop's drawn transform.

    ``rotation`` is ``[pitch, yaw, roll]`` in degrees and ``scale`` a 3-vector.
    This is exactly what ``entity_table._model_transform_columns`` hands the
    renderer -- ``Ry(yaw) @ Rx(pitch) @ Rz(roll) @ S`` with right-handed GLM
    rotations -- so collision built from it sits where the model is drawn.

    Collision used to rotate yaw the opposite way (and apply the three angles
    in the reverse order). Yaw 0 and 180 agree either way, so most maps never
    showed it; a model at yaw 90 or 270 drew one way and collided mirrored.
    """
    try:
        pitch, yaw, roll = (math.radians(float(rotation[i])) for i in range(3))
    except (TypeError, ValueError, IndexError):
        pitch = yaw = roll = 0.0
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)))
    rx = np.array(((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp)))
    rz = np.array(((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0)))
    return ry @ rx @ rz @ np.diag(np.asarray(scale[:3], dtype=np.float64))


def model_world_box(bounds, pos, rotation, scale):
    """``(centre, size)`` of the world AABB around a model's rotated bounds.

    *bounds* is the model-space ``(min, max)``; ``None`` stands for the
    64-unit placeholder cube centred on the origin.
    """
    if bounds:
        low = np.asarray(bounds[0], dtype=np.float64)
        high = np.asarray(bounds[1], dtype=np.float64)
    else:
        low, high = np.full(3, -32.0), np.full(3, 32.0)
    basis = model_basis(rotation, scale)
    centre = np.asarray(pos[:3], dtype=np.float64) + basis @ ((low + high) * 0.5)
    half = np.abs(basis) @ ((high - low) * 0.5)
    return [float(v) for v in centre], [float(v) * 2.0 for v in half]


COLLISION_KEYS = (
    "_collision_mode",
    "_mesh_triangles",
    "_mesh_bounds",
    "_mesh_planes",
)


def _vectors(vectors):
    """``(n, 3)`` float64 of *n* 3-vectors (lists, tuples): one flat pass,
    Stage 1's values; any vector of another length falls back to reading
    each one's first three."""
    count = len(vectors)
    flat = np.fromiter(_chain(vectors), dtype=np.float64)
    if flat.size != 3 * count:
        flat = np.array([(v[0], v[1], v[2]) for v in vectors], dtype=np.float64)
    return flat.reshape(count, 3)


class CollisionRows:
    """Every box in the collision brush cache, as rows in the cache's order.

    What the per-brush walks over ``_collision_brushes_cache`` read, held as
    arrays so a query is a handful of NumPy operations instead of a Python
    loop over every brush. Two sets of boxes, because the two walks round
    differently:

    * ``lo32``/``hi32``: the corners the hitscan walk tests, the derived
      bounds' centre -/+ half rounded to float32 (for a box, ``glm.vec3(pos)
      -/+ glm.vec3(size) * 0.5``), held as float64, since the slab test
      itself then runs in Python floats;
    * ``lo``/``hi``: the corners the projectile wall walk tests, the same
      bounds in float64.

    Both are the brush's derived bounds (:mod:`engine.brush_placement`): a
    convex brush's planes, not its ``pos``/``size`` copy, and a spinning
    brush's turned box. Hitscan, projectiles and line of sight still test
    every brush as this box (a true-shape narrow phase is Stage 2e).

    Only geometry lives here. Whether a brush blocks (``hidden``, water, fog,
    trigger) is asked of the brush itself, live, for the few rows a query
    returns: a streaming layer or an I/O input can change those at any time.
    Movers and doors are the only brushes that move during play (by their
    runtime transform, Stage 2b), and their rows are re-derived when a
    placement stamp says something may have moved (:meth:`refresh_movers`);
    a query that changes nothing pays two integer reads.

    Rebuilt when the cache list is replaced or the editor's world epoch moves;
    see :meth:`LogicCollision.collision_rows`.
    """

    __slots__ = ('source', 'epoch', 'brushes', 'lo32', 'hi32', 'lo', 'hi', '_by_row',
                 'mover_rows', 'movers', '_mover_seen', '_mover_boxes', '_mover_at')

    def __init__(self, brushes, epoch):
        self.source = brushes
        self.epoch = epoch
        self.brushes = list(brushes)
        count = len(self.brushes)
        # One contiguous row per axis: every query reads a whole axis at once.
        # The four bounds are views of one block, so a refresh puts a row's
        # twelve numbers in place with one store (_store).
        block = np.empty((4, 3, count), dtype=np.float64)
        self.lo32, self.hi32, self.lo, self.hi = block
        #: The block indexed by row: ``(4, count, 3)``.
        self._by_row = block.transpose(0, 2, 1)
        movers = self._resolve(slice(None), self.brushes)
        self.mover_rows = np.asarray(movers, dtype=np.intp)
        self.movers = [self.brushes[row] for row in movers]
        #: The placement stamps the mover rows were last derived at (None
        #: until the first refresh): see refresh_movers.
        self._mover_seen = None
        #: Their world boxes, in bulk, from the tables that hold them -- the
        #: gather shared with any other structure keeping rows for the same
        #: brushes -- and the rows to put each one in.
        bulk, rows, at = shared_bulk_boxes(self.movers, self.mover_rows)
        self._mover_boxes = bulk
        self._mover_at = (rows, at)

    def _resolve(self, rows, brushes):
        # Each brush's derived bounds (brush_placement.world_box): a convex
        # brush's planes, a mover where it is now, a spinner's turned box.
        # A plain box's are its pos/size. One pass finds the rest
        # (membership tests: most brushes carry none of these keys); a still
        # angled brush's box is the one remembered with its AABB
        # (constants.brush_still_box), a mover's is asked of the resolver,
        # and each is put in place of the brush's pos/size before the
        # columns are read in bulk -- its half-extent doubled, which the
        # halving below undoes exactly. Returns the movers' and doors' rows.
        pos = [b['pos'] for b in brushes]
        size = [b['size'] for b in brushes]
        special = [i for i, brush in enumerate(brushes)
                   if 'geometry' in brush or '_mesh_bounds' in brush
                   or 'is_mover' in brush or 'is_door' in brush]
        movers = []
        for i in special:
            brush = brushes[i]
            if brush.get('is_mover') or brush.get('is_door'):
                movers.append(i)
                cx, cy, cz, hx, hy, hz = world_box(brush)
            elif 'geometry' in brush or '_mesh_bounds' in brush:
                cx, cy, cz, hx, hy, hz = brush_still_box(brush)
            else:
                continue                # a flag set false: a plain box
            pos[i] = (cx, cy, cz)
            size[i] = (hx * 2.0, hy * 2.0, hz * 2.0)
        self._store(rows, _vectors(pos), _vectors(size) * 0.5)
        return movers

    def _store(self, rows, pos, half):
        # *pos* and *half* are (k, 3), one row per brush. The corners are
        # pos + (-half, +half) -- a + (-b) is a - b exactly, and negation and
        # halving are exact, so float32(size * 0.5) == float32(size) * 0.5:
        # the hitscan corners round exactly as glm.vec3(size) * 0.5 did --
        # in float32 and in float64, stacked as the block is and stored at
        # once.
        signed = half * _SIGNS
        corners = np.concatenate((pos.astype(np.float32) + signed.astype(np.float32),
                                  pos + signed))
        self._by_row[:, rows] = corners

    def refresh_movers(self):
        """Re-derive the movers' and doors' rows if anything they are derived
        from may have changed since the last call.

        Every query calls this, so the common answer -- nothing moved -- is
        two integer reads: the resolver's placement stamps
        (``brush_placement.runtime_generation``, moved on by every runtime
        source after every change, and ``authored_generation``, by every
        edit the editor journals). Only when one has moved are the movers
        looked at, once, in bulk.
        """
        movers = self.movers
        if not movers:
            return
        runtime_stamp = _placement.runtime_generation
        authored_stamp = _placement.authored_generation
        seen = self._mover_seen
        if seen is not None and seen[0] == runtime_stamp and seen[1] == authored_stamp:
            return
        # Gathered from the tables that hold them (the mover table's columns),
        # not rediscovered brush by brush: brush_placement.BulkBoxes.
        centre, half = self._mover_boxes.boxes()
        rows, at = self._mover_at
        if at is not None:
            centre, half = centre[at], half[at]
        self._store(rows, centre, half)
        # The stamps read before the movers were: a change that lands while
        # this ran moves a stamp past them, and the next query looks again.
        self._mover_seen = (runtime_stamp, authored_stamp)

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
        self.forget_model_files()
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

        Props choose AABB (the default) or Mesh collision. A
        non-zero collision_size always overrides the shape choice with a
        custom AABB.
        """
        logic = self.logic
        model_collision_enabled = self.model_collision_enabled
        brushes = []
        if not logic.session_runtime.play_mode:
            self.forget_model_files()       # the editor: a file may have changed

        physics_on = bool(getattr(logic.session_runtime, 'physics_enabled', True))
        explosions = getattr(logic, 'explosion_runtime', None)
        exploded = explosions.exploded if explosions is not None else {}
        for thing in logic.editor_state.things:
            props = getattr(thing, 'properties', {})
            if not props.get('model_path'):
                continue
            if exploded and id(thing) in exploded:
                continue            # blown up this session: nothing there
            # With physics switched off for the session nothing is a body: a
            # physics Prop collides as the scenery it otherwise is.
            physics_enabled = physics_on and bool(props.get('physics_enabled', False))
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
            if (len(pos) < 3 or len(scale) < 3
                    or not all(math.isfinite(float(v)) for v in pos[:3])
                    or not all(math.isfinite(float(v)) for v in scale[:3])):
                debug_log("Collision", f"Skipping model with non-finite transform: {props.get('model_path', '')}")
                continue

            rot = props.get('rotation', [0, 0, 0])
            is_physics_body = physics_enabled
            # Two shapes, both taken from the model: AABB (the compatibility
            # default for existing Prop records, and the Ball/Barrel presets)
            # or Mesh (the model's actual triangles). Newly placed generic
            # models are assigned Mesh by Prop.for_model(). 'auto', which older
            # maps hold, remains AABB.
            collision_shape = str(
                props.get('collision_shape', 'aabb')
            ).lower()
            if collision_shape != 'mesh':
                collision_shape = 'aabb'

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
                # The box encloses the model as drawn, rotation included: a
                # long model turned 90 degrees swaps its X and Z extents.
                aabb_pos, size = model_world_box(
                    self.compute_model_bounds(model_path), pos, rot, scale
                )

                if not all(math.isfinite(float(v)) for v in size + aabb_pos):
                    debug_log("Collision", f"Skipping model with invalid AABB bounds: {model_path}")
                    continue
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

            # Mesh: the model's own triangles. A model that cannot give any
            # (an unreadable file, an unsupported format) falls back to the
            # box below.
            mesh_tris = self._placed_mesh(thing, model_path, pos, scale, rot)
            if mesh_tris and collision_shape == 'mesh':
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
                built, self._built_mesh = self._built_mesh, None
                if built is not None and built[0] is mesh_tris:
                    # The arrays the list was made from, where
                    # brush_placement keeps a mesh proxy's.
                    brushes[-1]['_mesh_arrays'] = built
                continue

            # Fallback for Automatic/Mesh when the model has no CPU collision
            # mesh (for example OBJ today).
            aabb_pos, size = model_world_box(
                self.compute_model_bounds(model_path), pos, rot, scale
            )

            if not all(math.isfinite(float(v)) for v in size + aabb_pos):
                debug_log("Collision", f"Skipping model with invalid AABB fallback: {model_path}")
                continue
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


    def forget_model_files(self):
        """Drop what was read from model files (:meth:`_model_file_mesh`,
        :meth:`compute_model_bounds`). They are kept for a Play session, in
        which a model file does not change; outside one, each build reads
        them afresh."""
        self._model_file_meshes = {}
        self._model_file_bounds = {}
        self._built_mesh = None
        self._placed_meshes = {}

    def _placed_mesh(self, thing, model_path, pos, scale, rot):
        """:meth:`compute_model_collision_mesh` for *thing*, kept while it
        stays where it is: the rebuild that follows an entity coming or going
        (:meth:`sync_bodies` -- a barrel exploding, a plugin's spawn) makes
        new proxies for every model, but only what moved needs new triangles.
        """
        placed = (model_path, tuple(pos), tuple(scale), tuple(rot))
        kept = self._placed_meshes.get(id(thing))
        if kept is not None and kept[0] is thing and kept[1] == placed:
            self._built_mesh = kept[3]
            return kept[2]
        mesh_tris = self.compute_model_collision_mesh(model_path, pos, scale, rot)
        built = self._built_mesh
        if built is None or built[0] is not mesh_tris:
            built = None
        self._placed_meshes[id(thing)] = (thing, placed, mesh_tris, built)
        return mesh_tris

    def _model_file_mesh(self, model_path):
        """A model file's collision geometry in its own space:
        ``(vertices (n, 3) float64, triangles (m, 3) int64)``, or None.

        Read and parsed once per model file, not once per Prop that uses it:
        forty chairs placed from the Asset Browser are one file. Uses the
        loaders' CPU side only (no OpenGL calls), so it is safe on the logic
        thread.
        """
        cache = self._model_file_meshes
        if model_path in cache:
            return cache[model_path]
        mesh = cache[model_path] = self._read_model_file_mesh(model_path)
        return mesh

    def _read_model_file_mesh(self, model_path):
        logic = self.logic
        from engine.asset_archives import asset_exists
        full_path = os.path.join('assets', 'models', model_path)
        if not asset_exists(full_path):
            full_path = model_path
        package = getattr(logic, 'package', None)
        if not package and not asset_exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        if ext not in ('.glb', '.obj'):
            return None

        from .model_source import load_model
        if ext == '.obj':
            # CPU geometry only: the OpenGL-backed OBJ model is not made
            # on the logic thread. The same origin repair the drawn model
            # and its bounds get (compute_model_bounds), then each face
            # as a fan of triangles.
            from .obj_loader import OBJLoader
            loader = OBJLoader()
            if package:
                loader = load_model(model_path, package)
            if loader is None or (not package and not loader.load(full_path)):
                debug_log("Collision", f"OBJLoader failed to load {model_path}")
                return None
            source = np.asarray(loader.vertices, dtype=np.float32)
            if source.size == 0:
                return None
            source_min, source_max = source.min(axis=0), source.max(axis=0)
            centre = (source_min + source_max) * 0.5
            threshold = np.maximum((source_max - source_min) * 2.0, 2.0)
            all_verts = source - np.where(np.abs(centre) > threshold, centre, 0.0)
            all_tris = []
            for face in loader.faces:
                corners = [c[0] for c in face['vertices']]
                for k in range(1, len(corners) - 1):
                    all_tris.append((corners[0], corners[k], corners[k + 1]))
        else:
            # GLBLoader is pure file I/O + JSON parsing — zero OpenGL calls.
            from .glb_loader import GLBLoader

            loader = GLBLoader()
            loader._filepath_hint = full_path
            if package:
                loader = load_model(model_path, package)
            if loader is None or (not package and not loader.load(full_path)):
                debug_log("Collision", f"GLBLoader failed to load {model_path}")
                return None

            all_verts = loader.get_flattened_vertices()   # list of (x, y, z)
            all_tris  = loader.get_flattened_triangles()  # list of (i0, i1, i2)

        if len(all_verts) == 0 or len(all_tris) == 0:
            debug_log("Collision", f"No geometry in {model_path}")
            return None
        vertices = np.asarray(all_verts, dtype=np.float64).reshape(-1, 3)
        triangles = np.asarray(all_tris, dtype=np.int64).reshape(-1, 3)
        # A triangle naming a vertex that is not there is no surface.
        triangles = triangles[((triangles >= 0) & (triangles < len(vertices))).all(axis=1)]
        return vertices, triangles

    def compute_model_collision_mesh(self, model_path, world_pos, scale, rotation):
        """A model's world-space triangles for collision:
        ``[((v0, v1, v2), normal)]``, or None when the file gives none.

        The file's geometry comes from :meth:`_model_file_mesh`; here it is
        put where this Prop is (the renderer's own transform) and each
        triangle is given its face normal, in bulk.
        """
        if not model_path:
            return None
        try:
            mesh = self._model_file_mesh(model_path)
            if mesh is None:
                return None
            vertices, triangles = mesh

            # Model space to world space with the renderer's own transform.
            basis = model_basis(rotation, scale)
            world = vertices @ basis.T + np.asarray(world_pos[:3], dtype=np.float64)
            corners = world[triangles]                              # (m, 3, 3)

            # Face normals from the world-space edge vectors.
            e1 = corners[:, 1] - corners[:, 0]
            e2 = corners[:, 2] - corners[:, 0]
            nx = e1[:, 1] * e2[:, 2] - e1[:, 2] * e2[:, 1]
            ny = e1[:, 2] * e2[:, 0] - e1[:, 0] * e2[:, 2]
            nz = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
            with np.errstate(over='ignore', invalid='ignore'):
                length = np.sqrt(nx * nx + ny * ny + nz * nz)
                # Zero-area faces cannot define collision surfaces. Do not
                # publish a degenerate triangle with a fake normal.
                sound = np.isfinite(length) & (length > 1e-8)
            if not sound.any():
                return None
            length = length[sound]
            normals = np.stack((nx[sound] / length, ny[sound] / length,
                                nz[sound] / length), axis=1)
            corners = corners[sound]
            # The list every collision pass walks, made in two conversions
            # (as a turned mesh's is, brush_placement._turned_mesh), and the
            # arrays it came from, kept for the proxy built from it.
            world_tris = list(zip(corners.tolist(), normals.tolist()))
            self._built_mesh = (world_tris, corners, normals)

            debug_log("Collision", f"Built {len(world_tris)} mesh-collision tris for {model_path}")
            return world_tris

        except Exception as e:
            debug_log("Collision", f"Failed to build mesh collision for {model_path}: {e}")
            return None


    def compute_mesh_bounds(self, mesh_tris):
        """Compute AABB from mesh triangles for broad-phase culling."""
        if not mesh_tris:
            return None
        built = self._built_mesh
        if built is not None and built[0] is mesh_tris:
            points = built[1].reshape(-1, 3)
        else:
            points = np.asarray([tri for tri, _normal in mesh_tris],
                                dtype=np.float64).reshape(-1, 3)
        return (points.min(axis=0).tolist(), points.max(axis=0).tolist())


    def compute_model_bounds(self, model_path):
        """Axis-aligned bounds of a model file: ``(min, max)`` or None. Read
        once per file (:meth:`forget_model_files`)."""
        if not model_path:
            return None
        cache = self._model_file_bounds
        if model_path not in cache:
            cache[model_path] = self._read_model_bounds(model_path)
        return cache[model_path]

    def _read_model_bounds(self, model_path):
        logic = self.logic

        from engine.asset_archives import asset_exists
        full_path = os.path.join('assets', 'models', model_path)
        if not asset_exists(full_path):
            full_path = model_path
        package = getattr(logic, 'package', None)
        if not package and not asset_exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        try:
            if ext == '.glb':
                from .glb_loader import GLBLoader
                loader = GLBLoader()
                loader._filepath_hint = full_path
                from .model_source import load_model
                if package:
                    loader = load_model(model_path, package)
                if loader is not None and (package or loader.load(full_path)):
                    verts = loader.get_flattened_vertices()
                else:
                    verts = None
            elif ext == '.obj':
                # OBJ collision only needs CPU geometry.  Do not instantiate the
                # OpenGL-backed OBJ model on the logic thread.
                from .obj_loader import OBJLoader
                loader = OBJLoader()
                from .model_source import load_model
                if package:
                    loader = load_model(model_path, package)
                if loader is not None and (package or loader.load(full_path)):
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
                values = np.asarray(verts, dtype=np.float64)
                if (values.ndim != 2 or values.shape[1] < 3
                        or not np.isfinite(values[:, :3]).all()):
                    debug_log("Collision", f"Non-finite model vertices in {model_path}")
                    return None
                min_v = values[:, :3].min(axis=0).tolist()
                max_v = values[:, :3].max(axis=0).tolist()
                if not all(math.isfinite(v) for v in min_v + max_v):
                    debug_log("Collision", f"Non-finite model bounds in {model_path}")
                    return None
                return min_v, max_v
        except Exception as e:
            debug_log("Collision", f"Failed to compute {ext.upper()} bounds for {model_path}: {e}")
        return None


    def toggle_model_collision(self, enabled: bool = None) -> bool:
        """Toggle model collision on/off. If enabled is None, flip current state.
        Returns the new state. Works in both play mode and editor mode.

        Called from the UI thread (F6, showcollision): it repopulates the
        session's spatial grid and rebuilds its physics, which the logic tick
        and the monster AI read, so it holds the tick lock and then the
        monster lock -- the engine's lock order -- for the whole change.
        """
        logic = self.logic
        session = logic.session_runtime
        with logic._tick_lock, session.monster_lock:
            return self._toggle_model_collision_locked(enabled)

    def _toggle_model_collision_locked(self, enabled):
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
            if logic.session_runtime.play_mode:
                self.carry_moving_models()
            if logic.session_runtime.play_mode and logic.session_runtime.spatial_grid is not None:
                logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes + self._model_collision_brushes)
                if logic.session_runtime.physics_world is not None:
                    logic.session_runtime.physics_world.rebuild(self._physics_body_brushes)
        else:
            self._model_collision_brushes = []
            if logic.session_runtime.play_mode:
                self.carry_moving_models()
            if logic.session_runtime.play_mode and logic.session_runtime.spatial_grid is not None:
                logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes)
                if logic.session_runtime.physics_world is not None:
                    logic.session_runtime.physics_world.rebuild(self._physics_body_brushes)
        self.refresh_collision_brushes_cache()

        return self.model_collision_enabled


    def sync_bodies(self):
        """Entities came or went while the session runs (a plugin spawned or
        despawned one): rebuild the model collision brushes, and give the
        physics engine the bodies that are new and take away those that are
        gone -- the rest carry on exactly as they are.

        Solid scenery among them is in the spatial grid, which is repopulated
        only if that changed; a physics Prop is a body, not a grid brush.
        """
        logic = self.logic
        session = logic.session_runtime
        if not session.play_mode:
            return
        before_bodies = {id(b.get('_physics_entity')): b for b in self._physics_body_brushes}
        before_scenery = {id(b.get('_physics_entity'))
                          for b in self._model_collision_brushes
                          if not b.get('_physics_body')}
        self._model_collision_brushes = (
            self.build_model_collision_brushes() if self.model_collision_enabled else [])
        self._physics_body_brushes = [
            b for b in self._model_collision_brushes if b.get('_physics_body')]
        scenery = {id(b.get('_physics_entity')) for b in self._model_collision_brushes
                   if not b.get('_physics_body')}
        # A carried or turning model's proxy is new too, and only the new one
        # is carried:
        # the grid must hold it instead of the old one.
        carried = self.carry_moving_models()
        if (scenery != before_scenery or carried) and session.spatial_grid is not None:
            session.spatial_grid.populate(
                logic.editor_state.brushes + self._model_collision_brushes)
        physics = session.physics_world
        if physics is not None:
            now = {id(b.get('_physics_entity')): b for b in self._physics_body_brushes}
            for key, brush in before_bodies.items():
                if key not in now:
                    physics.remove_body(brush.get('_physics_entity'))
            for key, brush in now.items():
                if key not in before_bodies:
                    physics.add_body(brush)
        self.refresh_collision_brushes_cache()

    def carry_moving_models(self):
        """Hand the model collision proxies just built to the brush groups,
        which move those of solid models riding a group's mover or door, or
        turning about their own axis (Rotate), where the model is drawn
        (engine.logic_groups.CarriedModels). In play, before the spatial grid
        is populated with them; whether any is carried (or was)."""
        groups = getattr(getattr(self.logic, 'mover_runtime', None), 'groups', None)
        if groups is None:
            return False
        return groups.carry_models(self._model_collision_brushes)

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

