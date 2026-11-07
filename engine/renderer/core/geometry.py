"""Renderer-independent GPU geometry.

The unit cube and sprite quad primitives, and the angled (convex) brush mesh
cache built from RenderTable geometry records, resolved per table into arrays
indexed by geometry handle.
"""

import ctypes
import math
import weakref

import numpy as np
import OpenGL.GL as gl

from engine import brush_geometry
from engine import render_table
from .geo_store import GeoStore


# Cube face order — index maps to the face's 6-vertex run in the cube VAO
# (face_idx * 6). Kept as a module constant so the per-frame texture batch
# build doesn't allocate a fresh list for every brush.
_CUBE_FACE_KEYS = ('south', 'north', 'west', 'east', 'down', 'top')


#: Face (run) class bits, resolved when a mesh is built.
#: The face's texture is ``caulk``: never drawn.
RUN_SKIP = 1 << 0
#: The face's texture is ``nodraw``: drawn while editing, dropped in play.
RUN_NODRAW = 1 << 1

#: Columns of the face-run arena: ``(name, trailing shape, dtype)``. One row
#: per face of every cached mesh, holding what the textured pass sends per
#: face -- already resolved, so the frame reads them and resolves nothing.
_RUN_COLUMNS = (
    ('first', (), np.int32),        # vertex range in the mesh
    ('count', (), np.int32),
    ('texture', (), np.int64),      # GL texture name
    ('bits', (), np.uint8),         # RUN_SKIP / RUN_NODRAW
    ('scale', (2,), np.float32),    # tex_scale, natural scale included
    ('angle', (), np.float32),      # tex_angle, radians
    ('shift', (2,), np.float32),    # tex_shift
)


class BrushGeoMesh:
    """GPU mesh for one angled (convex-geometry) brush.

    Vertices are stored in the brush's local unit-cube space — world
    coordinates mapped through the brush's AABB (``pos``/``size``, which
    ``brush_geometry.sync_brush_bounds`` keeps in sync with the plane set).
    Every existing draw path can therefore keep its translate*scale model
    matrix, per-brush uniforms and shaders unchanged: the mesh simply binds
    in place of the shared unit-cube VAO.

    The vertices live in a page of the renderer's shared geometry store
    (``page``, from vertex ``start``), each carrying its face's UV state and
    the mesh's transform ``slot`` (:mod:`engine.renderer.core.geo_store`).
    ``vao`` is the plain per-mesh view of them that one-object-at-a-time
    passes bind; ``face_vao`` adds the face UV state, for the textured pass's
    per-mesh path. Its faces' draw parameters are rows ``run_base :
    run_base + run_len`` of the face-run arena, resolved when it was built.

    Faces are ordered sides-first, any flat top face last (``side_count``
    marks the split) so the water path can draw walls and surface separately,
    mirroring its box path.
    """
    __slots__ = ('vao', 'edge_vao', 'edge_vbo', 'face_vao', 'page', 'start',
                 'slot', 'count', 'side_count', 'edge_count', 'has_flat_top',
                 'key', 'run_base', 'run_len', 'refs')

    def __init__(self):
        self.vao = self.edge_vao = self.edge_vbo = self.face_vao = None
        self.page = self.start = self.slot = -1
        self.count = self.side_count = self.edge_count = 0
        self.has_flat_top = False
        self.key = None
        self.run_base = self.run_len = 0
        #: How many tables' handles hold this mesh; at 0 it is released.
        self.refs = 0


class GeoMeshData:
    """A convex record's tessellation: CPU data only, built by
    :func:`tessellate_geo`. ``faces`` lists ``(face, plane, first, count,
    extent)`` in vertex-buffer order."""
    __slots__ = ('vertices', 'edges', 'count', 'side_count', 'edge_count',
                 'has_flat_top', 'faces')


def tessellate_geo(record):
    """Fan-triangulate a GeometryRecord in its anchor's unit-cube space.

    A pure function of the record's surface key -- planes, natural flags and
    anchor (``origin``/``scale``) -- and of nothing else: in particular not
    of the brush's current position, which reaches the GPU only as the
    transform. GL-free, so the key's invariant is testable headlessly.

    Faces are emitted sides first and any flat top face last (``side_count``
    marks the split), so the water path can draw walls and surface apart.
    """
    convex = record.convex
    if convex is None or not convex.is_valid:
        return None
    origin = record.origin
    scale = record.scale

    # A "top" face (flat, at the AABB top) is emitted last so water can
    # draw walls and surface separately, like its box path does.
    def is_top(face):
        if face['normal'][1] < 0.999:
            return False
        ring_y = convex.verts[face['indices']][:, 1]
        return bool(np.all((ring_y - origin[1]) / scale[1] > 0.5 - 1e-3))

    flags = [is_top(f) for f in convex.faces]
    ordered = ([(f, False) for f, t in zip(convex.faces, flags) if not t] +
               [(f, True) for f, t in zip(convex.faces, flags) if t])

    data = []
    faces = []
    vert_count = 0
    side_count = 0
    top_area = 0.0
    for face, top in ordered:
        idx = face['indices']
        ring_w = convex.verts[idx]                    # world space
        ring_l = (ring_w - origin) / scale            # local unit-cube space
        n = np.array(face['normal'])
        # Local-space normal chosen so normalMatrix (inverse-transpose of the
        # translate*scale model matrix) maps it back to the world one.
        ln = n * scale
        ll = math.sqrt(float(ln @ ln))
        ln = ln / ll if ll > 1e-9 else n
        # Projection along the face's own texture basis when it has one (a
        # rotated face carries a basis that turned with the brush), and along
        # world axes when it does not.
        us, vs, (u0, eu), (v0, ev) = brush_geometry.face_uv_projection(
            ring_w, face)
        first = vert_count
        for k in range(1, len(idx) - 1):
            for j in (0, k, k + 1):
                p = ring_l[j]
                data.extend((p[0], p[1], p[2], ln[0], ln[1], ln[2],
                             (us[j] - u0) / eu, (vs[j] - v0) / ev))
        vert_count += (len(idx) - 2) * 3
        faces.append((face, convex.planes[face['plane']], first,
                      vert_count - first, (eu, ev)))
        if top:
            x, z = ring_l[:, 0], ring_l[:, 2]
            top_area += 0.5 * abs(float(
                np.dot(x, np.roll(z, -1)) - np.dot(np.roll(x, -1), z)))
        else:
            side_count = vert_count
    if not data:
        return None

    edge_set = set()
    for face in convex.faces:
        idx = face['indices']
        for a, b in zip(idx, idx[1:] + idx[:1]):
            edge_set.add((a, b) if a < b else (b, a))
    everts = []
    for a, b in edge_set:
        pa = (convex.verts[a] - origin) / scale
        pb = (convex.verts[b] - origin) / scale
        everts.extend((pa[0], pa[1], pa[2], pb[0], pb[1], pb[2]))

    out = GeoMeshData()
    out.vertices = np.asarray(data, dtype=np.float32).reshape(-1, 8)
    out.edges = np.asarray(everts, dtype=np.float32)
    out.count = vert_count
    out.side_count = side_count
    out.edge_count = 2 * len(edge_set)
    # Full unit-square footprint (area 1) means the tessellated water surface
    # grid still caps this brush exactly.
    out.has_flat_top = top_area >= 0.999
    out.faces = faces
    return out


class GeoHandles:
    """One RenderTable's geometry handles, resolved to cached meshes.

    The terrain pattern: the table holds numbers, and the renderer keeps what
    it made of them in arrays indexed by the same handle -- the mesh's VAOs,
    its place in the shared store (``page[h]``, ``start[h]``, ``slot[h]``)
    and its face rows in the arena (``run_base[h]`` / ``run_len[h]``). On a
    still frame using it is one integer compare (the table's
    ``geometry_version``). When that moves, the table's per-handle record
    serials say exactly what changed: handles whose record this table already
    had keep what was resolved for it -- wherever a reconcile moved them --
    and only records new to it are resolved. ``meshes[h]`` is the
    :class:`BrushGeoMesh` itself, for passes that still draw angled brushes
    one object at a time.
    """
    COLUMNS = (('vao', np.int64), ('face_vao', np.int64), ('page', np.intp),
               ('start', np.intp), ('slot', np.intp), ('run_base', np.intp),
               ('run_len', np.intp))
    __slots__ = ('table', 'version', 'serials', 'meshes', 'used') + tuple(
        name for name, _ in COLUMNS)

    def __init__(self, table):
        self.table = weakref.ref(table)
        self.version = None
        self.serials = np.zeros(0, dtype=np.int64)
        for name, dtype in self.COLUMNS:
            setattr(self, name, np.zeros(0, dtype=dtype))
        self.meshes = np.empty(0, dtype=object)
        self.used = 0


class GeometryMixin:
    """Shared primitives and the angled-brush mesh cache."""

    #: Frames a table's handle arrays may go unused before they are dropped,
    #: and with them every mesh no other table refers to.
    GEO_IDLE_FRAMES = 240

    # -- face-run arena ------------------------------------------------------

    def _geo_runs(self):
        """The face-run arena: a namespace of the :data:`_RUN_COLUMNS`."""
        arena = self.__dict__.get('_geo_run_arena')
        if arena is None:
            arena = self._geo_run_arena = _RunArena()
        return arena

    def _geo_store(self):
        """The shared vertex pages and transform slots (:class:`GeoStore`)."""
        store = self.__dict__.get('_geo_shared_store')
        if store is None:
            store = self._geo_shared_store = GeoStore()
        return store

    # -- lifetime ------------------------------------------------------------

    def _begin_geo_frame(self):
        """Advance the geometry clock; drop tables not drawn for a while.

        A mesh lives while some table's handles hold it (``refs``): a deleted,
        reshaped or undone brush releases its old mesh at its table's next
        re-sync, and a table nobody draws any more (a finished play session, a
        replaced buffer) is dropped here after :attr:`GEO_IDLE_FRAMES`,
        releasing what only it held.
        """
        self._geo_mesh_frame += 1
        if self._geo_mesh_frame % self.GEO_IDLE_FRAMES:
            return
        frame = self._geo_mesh_frame
        stale = [key for key, entry in self._geo_by_table.items()
                 if entry.table() is None
                 or frame - entry.used > self.GEO_IDLE_FRAMES]
        for key in stale:
            self._unref_geo_meshes(self._geo_by_table.pop(key).meshes)
        # Meshes built for an overlay and never taken by a table.
        for key in [k for k, m in self._geo_mesh_cache.items() if m.refs <= 0]:
            self._delete_geo_mesh(self._geo_mesh_cache.pop(key))
        self._compact_geo_runs()

    def _delete_geo_mesh(self, mesh):
        try:
            for vao in (mesh.vao, mesh.face_vao, mesh.edge_vao):
                if vao:
                    gl.glDeleteVertexArrays(1, [vao])
            if mesh.edge_vbo:
                gl.glDeleteBuffers(1, [mesh.edge_vbo])
        except Exception:
            pass  # GL context may already be gone during shutdown
        store = self._geo_store()
        if mesh.page >= 0:
            store.release(mesh.page, mesh.start, mesh.count)
        if mesh.slot >= 0:
            store.give_slot(mesh.slot)
        mesh.vao = mesh.face_vao = mesh.edge_vao = mesh.edge_vbo = None
        mesh.page = mesh.slot = -1
        self._geo_runs().release(mesh)

    def _unref_geo_meshes(self, meshes):
        """Drop one table reference from each of *meshes*; free the unheld."""
        for mesh in meshes:
            if mesh is None:
                continue
            mesh.refs -= 1
            if mesh.refs <= 0 and self._geo_mesh_cache.get(mesh.key) is mesh:
                self._delete_geo_mesh(self._geo_mesh_cache.pop(mesh.key))

    def _compact_geo_runs(self):
        if self._geo_runs().compact(self._geo_mesh_cache.values()):
            # Every mesh's face rows moved: re-read every handle's offsets.
            for entry in self._geo_by_table.values():
                held = entry.meshes
                entry.run_base = np.fromiter(
                    (m.run_base if m is not None else 0 for m in held),
                    dtype=np.intp, count=len(held))

    # -- per-table handle arrays ---------------------------------------------

    def _geo_handles(self, table):
        """The cached meshes of *table*'s geometry handles (:class:`GeoHandles`).

        Steady state: one dict lookup and one integer compare per pass.
        """
        key = id(table)
        entry = self._geo_by_table.get(key)
        if entry is None or entry.table() is not table:
            if entry is not None:
                self._unref_geo_meshes(entry.meshes)
            entry = GeoHandles(table)
            self._geo_by_table[key] = entry
        entry.used = self._geo_mesh_frame
        if entry.version != table.geometry_version:
            self._sync_geo_handles(entry, table)
        return entry

    def _sync_geo_handles(self, entry, table):
        """Follow *table*'s geometry records to their meshes, by serial.

        A join of the table's per-handle serials against the ones this entry
        last saw: a record it already had keeps its VAOs and face rows, moved
        to the record's new handle, with no lookup at all; only serials new
        to this table go through the signature-keyed cache (where a mesh
        another table built is found without building it again).
        """
        serials = table.geometry_serial[:len(table.geometry_records)]
        old = entry.serials
        if len(old):
            order = np.argsort(old, kind='stable')
            ranked = old[order]
            at = np.minimum(np.searchsorted(ranked, serials), len(ranked) - 1)
            found = ranked[at] == serials
            source = order[at]
        else:
            found = np.zeros(len(serials), dtype=bool)
            source = np.zeros(len(serials), dtype=np.intp)
        kept = source[found]
        handles = len(serials)
        columns = {}
        for name, dtype in GeoHandles.COLUMNS:
            column = np.zeros(handles, dtype=dtype)
            column[found] = getattr(entry, name)[kept]
            columns[name] = column
        meshes = np.empty(handles, dtype=object)
        meshes[found] = entry.meshes[kept]
        # What this table no longer holds: removed handles and replaced records.
        dropped = np.ones(len(old), dtype=bool)
        dropped[kept] = False
        released = entry.meshes[dropped]
        records = table.geometry_records
        fresh = np.flatnonzero(~found).tolist()
        # Records new to this table: first take the meshes the cache already
        # has (another table, or a surface that came back); then let go of
        # what this table dropped, so a mesh only it held is freed; only then
        # build the misses -- into the space just freed. Building first held
        # every replaced mesh and its replacement at once (an undo doubled
        # the pages).
        misses = []
        for handle in fresh:
            record = records[handle]
            mesh = (self._geo_mesh_cache.get(record.surface)
                    if record is not None else None)
            if mesh is None:
                misses.append(handle)
            else:
                meshes[handle] = mesh
                mesh.refs += 1
        if len(released):
            self._unref_geo_meshes(released)
        for handle in misses:
            mesh = self._get_geo_mesh_record(records[handle])
            meshes[handle] = mesh
            if mesh is not None:
                mesh.refs += 1
        for handle in fresh:
            mesh = meshes[handle]
            if mesh is not None:
                columns['vao'][handle] = mesh.vao
                columns['face_vao'][handle] = mesh.face_vao or 0
                columns['page'][handle] = mesh.page
                columns['start'][handle] = mesh.start
                columns['slot'][handle] = mesh.slot
                columns['run_base'][handle] = mesh.run_base
                columns['run_len'][handle] = mesh.run_len
        for name, column in columns.items():
            setattr(entry, name, column)
        entry.serials = serials.copy()
        entry.meshes = meshes
        entry.version = table.geometry_version

    def _prepare_geo_meshes(self, table, slots):
        """``geometry handle -> BrushGeoMesh`` for the convex rows of *slots*.

        For the passes that still draw angled brushes one object at a time;
        the textured pass reads the :meth:`_geo_handles` arrays directly.
        """
        if table is None or slots is None or not len(slots):
            return {}
        gids = table.geometry_id[np.asarray(slots, dtype=np.int32)]
        gids = gids[gids >= 0]
        if not len(gids):
            return {}
        meshes = self._geo_handles(table).meshes
        return {gid: meshes[gid] for gid in np.unique(gids).tolist()
                if gid < len(meshes) and meshes[gid] is not None}

    # -- building ------------------------------------------------------------

    def _get_geo_mesh_record(self, record, geometry_id=None, geometry_generation=None):
        """Get or build a GPU mesh from a dense GeometryRecord.

        Keyed by the record's ``surface`` -- the exact authored inputs of the
        mesh, with no per-dict epoch -- so both render buffers' tables share
        one mesh, and an undo that restores a brush unchanged (as a new dict)
        finds its mesh again instead of building and uploading a copy.
        """
        if record is None or record.convex is None or not record.convex.is_valid:
            return None
        key = record.surface
        mesh = self._geo_mesh_cache.get(key)
        if mesh is None:
            try:
                mesh = self._build_geo_mesh(record, record.convex, key)
            except Exception as e:
                print(f"[GeoMesh] build failed: {e}")
                mesh = None
            if mesh is None:
                return None
            self._geo_mesh_cache[key] = mesh
        return mesh

    def _resolve_face_texture(self, name):
        """A face texture's GL name, loading it if needed (a cache miss)."""
        return int(self.texture_manager.get(self._tex_cache_path(name))
                   or self.load_texture_callback(name, 'textures') or 0)

    def _build_geo_mesh(self, record, convex, key):
        data = tessellate_geo(record)
        if data is None:
            return None
        runs = [self._resolve_face_run(face, plane, record, first, count, extent)
                for face, plane, first, count, extent in data.faces]
        mesh = BrushGeoMesh()
        mesh.key = key
        mesh.count = data.count
        mesh.side_count = data.side_count
        mesh.has_flat_top = data.has_flat_top
        mesh.run_base, mesh.run_len = self._geo_runs().append(runs)
        faces = np.empty((data.count, 5), dtype=np.float32)
        for first, count, _texture, _bits, uv_scale, angle, shift in runs:
            # float32 here: exactly what the per-face uniforms would hold.
            faces[first:first + count] = (uv_scale[0], uv_scale[1], angle,
                                          shift[0], shift[1])
        store = self._geo_store()
        mesh.slot = store.take_slot()
        placed = store.place(data.vertices, faces, mesh.slot)
        if placed is None:
            store.give_slot(mesh.slot)
            self._geo_runs().release(mesh)
            return None
        mesh.page, mesh.start = placed
        mesh.vao = store.mesh_vao(mesh.page, mesh.start, face=False)
        if 'geo_textured' in self.shaders:
            mesh.face_vao = store.mesh_vao(mesh.page, mesh.start, face=True)

        # Edge lines (local space) for the selection outline.
        mesh.edge_count = data.edge_count
        mesh.edge_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(mesh.edge_vao)
        mesh.edge_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, mesh.edge_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.edges.nbytes, data.edges,
                        gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        return mesh

    def _resolve_face_run(self, face, plane_meta, record, first, count, extent):
        """One face's arena row: what the textured pass draws it with.

        Exactly what the per-frame loop used to work out for the face on
        every frame -- the texture name and its caulk/nodraw meaning, the GL
        texture, the scale (natural or authored), the angle in radians and the
        shift -- worked out once, here, when the mesh is built, from the
        face's one material record as the geometry record resolved it. A
        caulk face never draws, so its texture is not loaded.
        """
        material = record.materials[id(face)]
        name = material.texture
        bits = 0
        if name == render_table.TEX_SKIP:
            bits |= RUN_SKIP
        elif name == render_table.TEX_NODRAW:
            bits |= RUN_NODRAW
        texture = 0 if bits & RUN_SKIP else self._resolve_face_texture(name)
        if material.natural:
            scale = brush_geometry.natural_repeats(
                extent[0], extent[1], self._texture_pixel_size(name))
        else:
            uv = material.uv_scale
            scale = (1.0, 1.0) if uv is None else (float(uv[0]), float(uv[1]))
        shift = material.uv_shift
        return (first, count, texture, bits, scale,
                math.radians(float(material.uv_angle)),
                (float(shift[0]), float(shift[1])))

    # --------------------------------------------------------------------------
    # VAO creation
    # --------------------------------------------------------------------------
    def _create_cube_vao(self):
        vertices = np.array([
            -0.5,-0.5,-0.5, 0,0,-1, 0,0,  0.5,-0.5,-0.5, 0,0,-1, 1,0,  0.5,0.5,-0.5, 0,0,-1, 1,1,
            0.5,0.5,-0.5, 0,0,-1, 1,1,  -0.5,0.5,-0.5, 0,0,-1, 0,1,  -0.5,-0.5,-0.5, 0,0,-1, 0,0,
            -0.5,-0.5,0.5, 0,0,1, 0,0,  0.5,0.5,0.5, 0,0,1, 1,1,  0.5,-0.5,0.5, 0,0,1, 1,0,
            0.5,0.5,0.5, 0,0,1, 1,1,  -0.5,-0.5,0.5, 0,0,1, 0,0,  -0.5,0.5,0.5, 0,0,1, 0,1,
            -0.5,0.5,0.5, -1,0,0, 1,0,  -0.5,-0.5,-0.5, -1,0,0, 0,1,  -0.5,0.5,-0.5, -1,0,0, 1,1,
            -0.5,-0.5,-0.5, -1,0,0, 0,1,  -0.5,0.5,0.5, -1,0,0, 1,0,  -0.5,-0.5,0.5, -1,0,0, 0,0,
            0.5,0.5,0.5, 1,0,0, 1,0,  0.5,0.5,-0.5, 1,0,0, 1,1,  0.5,-0.5,-0.5, 1,0,0, 0,1,
            0.5,-0.5,-0.5, 1,0,0, 0,1,  0.5,-0.5,0.5, 1,0,0, 0,0,  0.5,0.5,0.5, 1,0,0, 1,0,
            -0.5,-0.5,-0.5, 0,-1,0, 0,1,  0.5,-0.5,0.5, 0,-1,0, 1,0,  0.5,-0.5,-0.5, 0,-1,0, 1,1,
            0.5,-0.5,0.5, 0,-1,0, 1,0,  -0.5,-0.5,-0.5, 0,-1,0, 0,1,  -0.5,-0.5,0.5, 0,-1,0, 0,0,
            -0.5,0.5,-0.5, 0,1,0, 0,1,  0.5,0.5,-0.5, 0,1,0, 1,1,  0.5,0.5,0.5, 0,1,0, 1,0,
            0.5,0.5,0.5, 0,1,0, 1,0,  -0.5,0.5,0.5, 0,1,0, 0,0,  -0.5,0.5,-0.5, 0,1,0, 0,1
        ], dtype=np.float32)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glBindVertexArray(0)
        self._cube_vbo = vbo
        return vao

    def _create_sprite_vao(self):
        vertices = np.array([-0.5, -0.5, 0.5, -0.5, -0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self._sprite_vbo = vbo
        return vao


class _RunArena:
    """The face rows of every cached mesh, as columns (:data:`_RUN_COLUMNS`).

    Appended to when a mesh is built, released when it is deleted, and
    compacted when more than half of it is dead -- never touched per frame
    except to be read by index.
    """

    def __init__(self):
        self.used = 0
        self.dead = 0
        for name, shape, dtype in _RUN_COLUMNS:
            setattr(self, name, np.zeros((64,) + shape, dtype=dtype))

    def _grow(self, needed):
        capacity = len(self.first)
        if needed <= capacity:
            return
        capacity = max(needed, capacity * 2)
        for name, shape, dtype in _RUN_COLUMNS:
            new = np.zeros((capacity,) + shape, dtype=dtype)
            old = getattr(self, name)
            new[:len(old)] = old
            setattr(self, name, new)

    def append(self, runs):
        """Store *runs* (``_resolve_face_run`` tuples); ``(base, length)``."""
        base, length = self.used, len(runs)
        self._grow(base + length)
        rows = slice(base, base + length)
        first, count, texture, bits, scale, angle, shift = zip(*runs) if runs else ((),) * 7
        self.first[rows] = first
        self.count[rows] = count
        self.texture[rows] = texture
        self.bits[rows] = bits
        # float32 here: exactly what glUniform2f/1f made of these doubles.
        self.scale[rows] = np.asarray(scale, dtype=np.float64).reshape(-1, 2)
        self.angle[rows] = angle
        self.shift[rows] = np.asarray(shift, dtype=np.float64).reshape(-1, 2)
        self.used += length
        return base, length

    def release(self, mesh):
        self.dead += mesh.run_len

    def compact(self, meshes):
        """Close the holes dead meshes left; True when rows moved."""
        if self.dead <= 1024 or self.dead * 2 <= self.used:
            return False
        live = sorted(meshes, key=lambda m: m.run_base)
        order = np.concatenate([np.arange(m.run_base, m.run_base + m.run_len)
                                for m in live]) if live else np.zeros(0, np.intp)
        for name, _shape, _dtype in _RUN_COLUMNS:
            column = getattr(self, name)
            column[:len(order)] = column[order]
        base = 0
        for mesh in live:
            mesh.run_base = base
            base += mesh.run_len
        self.used = base
        self.dead = 0
        return True
