"""GPU geometry: shared VAOs, angled-brush meshes and models.

The unit cube, tessellated water surface and sprite quad VAOs; the convex
(angled) brush mesh cache keyed by geometry signature; and OBJ/GLB model
loading.
"""

import ctypes
import math
import os
import time

import numpy as np
import OpenGL.GL as gl

from engine import brush_geometry

# Try to import OBJ and GLB loaders
try:
    from engine.obj_loader import OBJ
except ImportError:
    OBJ = None

try:
    from engine.glb_loader import GLB
except ImportError:
    GLB = None


#: Seconds before a model path that failed to load is tried again.
_MODEL_RETRY_S = 5.0


class BrushGeoMesh:
    """GPU mesh for one angled (convex-geometry) brush.

    Vertices are stored in the brush's local unit-cube space — world
    coordinates mapped through the brush's AABB (``pos``/``size``, which
    ``brush_geometry.sync_brush_bounds`` keeps in sync with the plane set).
    Every existing draw path can therefore keep its translate*scale model
    matrix, per-brush uniforms and shaders unchanged: the mesh simply binds
    in place of the shared unit-cube VAO.

    ``runs`` holds one entry per face (dicts with ``face`` tag, plane
    ``texture``/``uv_scale``, ``first``/``count`` vertex range and the face's
    projected world-space ``extent``) so the textured path can draw each face
    with its own texture, exactly like the per-face cube batches.

    Faces are ordered sides-first, any flat top face last (``side_count``
    marks the split) so the water path can draw walls and surface separately,
    mirroring its box path.
    """
    __slots__ = ('vao', 'vbo', 'edge_vao', 'edge_vbo', 'count', 'side_count',
                 'edge_count', 'runs', 'has_flat_top', 'key', 'frame')

    def __init__(self):
        self.vao = self.vbo = self.edge_vao = self.edge_vbo = None
        self.count = self.side_count = self.edge_count = 0
        self.runs = []
        self.has_flat_top = False
        self.key = None
        self.frame = 0


class GeometryMixin:
    """GPU geometry of :class:`engine.renderer.Renderer`."""

    # --------------------------------------------------------------------------
    # Models
    # --------------------------------------------------------------------------
    def load_model(self, filename):
        """Load a 3D model (OBJ or GLB).

        The normal render-time case is an already-loaded model. Keep that path
        to a single dictionary lookup; path normalisation and filesystem work
        belong exclusively to cache misses.
        """
        if not filename:
            return None

        # HOT PATH: model_path values are normally identical strings frame to
        # frame, so this is the entire lookup on the common render path.
        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        # A path that failed a moment ago is not retried every frame: every
        # draw, cull and shadow pass asks for it, and each retry was a
        # filesystem probe, a parse attempt and a log line. Retried after
        # _MODEL_RETRY_S, so a model added while Fio runs still appears.
        failed = self.__dict__.setdefault('_failed_models', {}).get(filename)
        if failed is not None and time.perf_counter() - failed < _MODEL_RETRY_S:
            return None

        # Cache miss only: normalise alternate slash/absolute-path spellings
        # so editor/package/file-dialog paths still collapse to one resource.
        original_filename = str(filename)
        normalized_filename = os.path.normpath(
            original_filename.replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)

        model = self.loaded_models.get(cache_key)
        if model is not None:
            # Alias this exact authored path so subsequent frames stay on the
            # one-dictionary-lookup path above.
            self.loaded_models[filename] = model
            return model

        full_path = normalized_filename
        if not os.path.isabs(full_path):
            candidate = os.path.join('assets', 'models', full_path)
            if os.path.exists(candidate):
                full_path = candidate
            elif os.path.exists(original_filename):
                full_path = original_filename

        if not os.path.exists(full_path):
            return self._model_load_failed(filename)

        # Determine format by extension
        ext = os.path.splitext(full_path)[1].lower()

        if ext == '.glb':
            if GLB is None:
                print(f"[Renderer] GLB support not available (glb_loader not found)")
                return None
            model = GLB(full_path)
        elif ext in ('.obj', ''):
            if OBJ is None:
                print(f"[Renderer] OBJ support not available (obj_loader not found)")
                return None
            model = OBJ(full_path)
        else:
            print(f"[Renderer] Unsupported model format: {ext}")
            return None

        if model.is_loaded:
            self.loaded_models[cache_key] = model
            self.loaded_models[filename] = model
            self.__dict__.setdefault('_failed_models', {}).pop(filename, None)
            return model

        return self._model_load_failed(filename)

    def _model_load_failed(self, filename):
        """Remember a failed model path; report it the first time only."""
        failed = self.__dict__.setdefault('_failed_models', {})
        if filename not in failed:
            print(f"Failed to load model: {filename}")
        failed[filename] = time.perf_counter()
        return None

    def get_loaded_model(self, filename):
        """Return a model already loaded by this renderer without touching GL."""
        if not filename:
            return None

        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        normalized_filename = os.path.normpath(
            str(filename).replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)
        model = self.loaded_models.get(cache_key)
        if model is not None:
            self.loaded_models[filename] = model
        return model

    def _begin_geo_frame(self):
        """Advance the angled-brush mesh cache clock and drop stale meshes.

        Called once per rendered frame; meshes whose brush hasn't been drawn
        for a few hundred frames (deleted / undone / hidden brushes) get their
        GL buffers released.
        """
        self._geo_mesh_frame += 1
        if self._geo_mesh_frame % 240 or not self._geo_mesh_cache:
            return
        stale = [k for k, m in self._geo_mesh_cache.items()
                 if self._geo_mesh_frame - m.frame > 240]
        for k in stale:
            self._delete_geo_mesh(self._geo_mesh_cache.pop(k))
        # The fast index may name deleted meshes and records that no longer
        # exist; it is only a shortcut into the cache, so rebuild it lazily.
        self._geo_mesh_by_record.clear()

    @staticmethod
    def _delete_geo_mesh(mesh):
        try:
            if mesh.vao:
                gl.glDeleteVertexArrays(1, [mesh.vao])
            if mesh.vbo:
                gl.glDeleteBuffers(1, [mesh.vbo])
            if mesh.edge_vao:
                gl.glDeleteVertexArrays(1, [mesh.edge_vao])
            if mesh.edge_vbo:
                gl.glDeleteBuffers(1, [mesh.edge_vbo])
        except Exception:
            pass  # GL context may already be gone during shutdown

    def _prepare_geo_meshes(self, table, slots):
        """Prepare convex meshes from dense geometry records."""
        if table is None or slots is None or not len(slots):
            return {}
        slots = np.asarray(slots, dtype=np.int32)
        gids = table.geometry_id[slots]
        gids = gids[gids >= 0]
        if not len(gids):
            return {}
        meshes = {}
        records = table.geometry_records
        for gid in np.unique(gids):
            gid = int(gid)
            if gid >= len(records):
                continue
            record = records[gid]
            if record is None:
                continue
            mesh = self._get_geo_mesh_record(record, geometry_id=gid,
                                             geometry_generation=table.generation)
            if mesh is not None:
                meshes[gid] = mesh
        return meshes

    def _get_geo_mesh_record(self, record, geometry_id=None, geometry_generation=None):
        """Get or build a GPU mesh from a dense GeometryRecord."""
        if record is None or record.convex is None or not record.convex.is_valid:
            return None
        key = record.signature
        by_record = self._geo_mesh_by_record
        mesh = by_record.get(id(record))
        if mesh is not None and (mesh.key is key or mesh.key == key):
            mesh.frame = self._geo_mesh_frame
            return mesh
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
        mesh.frame = self._geo_mesh_frame
        by_record[id(record)] = mesh
        return mesh

    def _build_geo_mesh(self, record, convex, key):
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
        runs = []
        vert_count = 0
        side_count = 0
        top_area = 0.0
        for face, top in ordered:
            idx = face['indices']
            ring_w = convex.verts[idx]                    # world space
            ring_l = (ring_w - origin) / scale            # local unit-cube space
            n = np.array(face['normal'])
            # Local-space normal chosen so normalMatrix (inverse-transpose of
            # the translate*scale model matrix) maps it back to the world one.
            ln = n * scale
            ll = math.sqrt(float(ln @ ln))
            ln = ln / ll if ll > 1e-9 else n
            # Projection along the face's own texture basis when it has one
            # (a rotated face carries a basis that turned with the brush), and
            # along world axes when it does not — exactly as before.
            us, vs, (u0, eu), (v0, ev) = brush_geometry.face_uv_projection(
                ring_w, face)
            plane_meta = convex.planes[face['plane']]
            first = vert_count
            for k in range(1, len(idx) - 1):
                for j in (0, k, k + 1):
                    p = ring_l[j]
                    data.extend((p[0], p[1], p[2], ln[0], ln[1], ln[2],
                                 (us[j] - u0) / eu, (vs[j] - v0) / ev))
            vert_count += (len(idx) - 2) * 3
            runs.append({'face': face.get('face'), 'texture': face.get('texture'),
                         'uv_scale': face.get('uv_scale') or plane_meta.get('uv_scale'), 'plane': face.get('plane'),
                         'uv_angle': plane_meta.get('uv_angle', 0.0),
                         'uv_shift': plane_meta.get('uv_shift', (0.0, 0.0)),
                         'natural_scale': bool(record.natural_scale.get(id(face), False)),
                         'first': first, 'count': vert_count - first,
                         'extent': (eu, ev)})
            if top:
                x, z = ring_l[:, 0], ring_l[:, 2]
                top_area += 0.5 * abs(float(
                    np.dot(x, np.roll(z, -1)) - np.dot(np.roll(x, -1), z)))
            else:
                side_count = vert_count

        if not data:
            return None

        mesh = BrushGeoMesh()
        mesh.key = key
        mesh.count = vert_count
        mesh.side_count = side_count
        mesh.runs = runs
        # Full unit-square footprint (area 1) means the tessellated water
        # surface grid still caps this brush exactly.
        mesh.has_flat_top = top_area >= 0.999

        arr = np.asarray(data, dtype=np.float32)
        mesh.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(mesh.vao)
        mesh.vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, mesh.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, arr.nbytes, arr, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)

        # Edge lines (local space) for the selection outline.
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
        earr = np.asarray(everts, dtype=np.float32)
        mesh.edge_count = 2 * len(edge_set)
        mesh.edge_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(mesh.edge_vao)
        mesh.edge_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, mesh.edge_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, earr.nbytes, earr, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        return mesh

    @staticmethod
    def _geo_run_texture(run):
        """Texture name baked into this cold geometry run."""
        return run.get('texture') or 'default.png'

    @staticmethod
    def _geo_run_tex_transform(run):
        """``(angle_radians, shift_u, shift_v)`` from cold run data."""
        shift = run.get('uv_shift') or (0.0, 0.0)
        return (math.radians(float(run.get('uv_angle', 0.0))),
                float(shift[0]), float(shift[1]))

    def _geo_run_tex_scale(self, run, tex_name):
        """UV repeat factors from cold convex-face data."""
        if run.get('natural_scale'):
            return brush_geometry.natural_repeats(
                run['extent'][0], run['extent'][1],
                self._texture_pixel_size(tex_name))
        uv = run.get('uv_scale')
        if uv is not None:
            return float(uv[0]), float(uv[1])
        return 1.0, 1.0

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

    def _create_water_surface_vao(self, subdivisions=64):
        """Tessellated unit-square grid on the cube's top face (y = +0.5).

        The water vertex shader needs real geometry to displace with Gerstner
        waves — the 2-triangle cube top gave it nothing to work with, which is
        why water used to look like a solid slab. Same attribute layout as the
        cube VAO (pos, normal, uv) so both bind to the water shader.
        """
        n = subdivisions
        verts = np.zeros(((n + 1) * (n + 1), 8), dtype=np.float32)
        idx = 0
        for j in range(n + 1):
            z = j / n - 0.5
            for i in range(n + 1):
                x = i / n - 0.5
                verts[idx] = (x, 0.5, z, 0.0, 1.0, 0.0, i / n, j / n)
                idx += 1

        indices = np.zeros(n * n * 6, dtype=np.uint32)
        k = 0
        for j in range(n):
            row = j * (n + 1)
            for i in range(n):
                a = row + i
                c = a + (n + 1)
                indices[k:k + 6] = (a, c, a + 1, a + 1, c, c + 1)
                k += 6

        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        ebo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glBindVertexArray(0)
        self._water_surface_vbo = vbo
        self._water_surface_ebo = ebo
        self._water_surface_index_count = len(indices)
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
