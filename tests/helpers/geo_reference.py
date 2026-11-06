"""The angled-brush textured path as it was in 3.1.0.0611 -- frozen.

Everything here is a verbatim port of code that used to live in
:mod:`engine.renderer.core.geometry` (``_build_geo_mesh`` and the
``_geo_run_*`` helpers) and in the convex half of
``PassesMixin.draw_textured_brushes_optimized``, which resolved every face's
texture and UV state per frame and drew faces in brush order. It is kept so
the path that replaced it -- face state resolved once per mesh build, faces
submitted sorted by texture -- can be proven against the *old behaviour*, not
against itself: the same role ``terrain_reference`` plays for the terrain.

It is deliberately independent of the dense tables: it starts from the brush
dicts, reads each brush's convex geometry, natural-scale flags and transform
itself, and resolves texture names to GL names the way the old loop did, per
run. The one shared dependency is the geometry kernel
(:func:`engine.brush_geometry.get_convex`), which is the input both paths are
fed, not the code under test.

Do not "fix" anything here. In particular the old record handed the convex
*face* dict to ``face_uses_natural_scale`` as its ``plane`` argument, so a
plane-level ``uv_natural`` flag never reached the renderer -- only the brush's
per-tag ``uv_natural`` dict did. That is the behaviour being preserved.
"""

import math

import numpy as np

from engine import brush_geometry


# ---------------------------------------------------------------------------
# The record the old renderer built its mesh from
# ---------------------------------------------------------------------------

def reference_record(brush):
    """``(origin, scale, convex, natural)`` for one angled brush, or None.

    The same values ``RenderTable._geometry_record`` derived, computed here
    from the brush dict rather than read from any table.
    """
    convex = brush_geometry.get_convex(brush)
    if convex is None or not convex.is_valid:
        return None
    pos = brush.get('pos') or (0.0, 0.0, 0.0)
    size = brush.get('size') or (64.0, 64.0, 64.0)
    origin = np.asarray(pos, dtype=np.float64).copy()
    scale = np.asarray([max(abs(float(s)), 1e-6) for s in size], dtype=np.float64)
    natural = {id(face): bool(brush_geometry.face_uses_natural_scale(
        brush, face.get('face'), face)) for face in convex.faces}
    return origin, scale, convex, natural


class ReferenceMesh:
    """The CPU half of the old ``BrushGeoMesh``: vertex data and runs."""

    __slots__ = ('vertices', 'edges', 'count', 'side_count', 'edge_count',
                 'runs', 'has_flat_top')


def reference_mesh(brush):
    """Verbatim port of the old ``GeometryMixin._build_geo_mesh``, minus GL."""
    record = reference_record(brush)
    if record is None:
        return None
    origin, scale, convex, natural_scale = record

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
        ring_w = convex.verts[idx]
        ring_l = (ring_w - origin) / scale
        n = np.array(face['normal'])
        ln = n * scale
        ll = math.sqrt(float(ln @ ln))
        ln = ln / ll if ll > 1e-9 else n
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
                     'uv_scale': face.get('uv_scale') or plane_meta.get('uv_scale'),
                     'plane': face.get('plane'),
                     'uv_angle': plane_meta.get('uv_angle', 0.0),
                     'uv_shift': plane_meta.get('uv_shift', (0.0, 0.0)),
                     'natural_scale': bool(natural_scale.get(id(face), False)),
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

    mesh = ReferenceMesh()
    mesh.count = vert_count
    mesh.side_count = side_count
    mesh.runs = runs
    mesh.has_flat_top = top_area >= 0.999
    mesh.vertices = np.asarray(data, dtype=np.float32).reshape(-1, 8)

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
    mesh.edges = np.asarray(everts, dtype=np.float32).reshape(-1, 3)
    mesh.edge_count = 2 * len(edge_set)
    return mesh


# ---------------------------------------------------------------------------
# The old per-run resolution
# ---------------------------------------------------------------------------

def run_texture(run):
    """Old ``_geo_run_texture``."""
    return run.get('texture') or 'default.png'


def run_tex_transform(run):
    """Old ``_geo_run_tex_transform``: ``(angle_radians, shift_u, shift_v)``."""
    shift = run.get('uv_shift') or (0.0, 0.0)
    return (math.radians(float(run.get('uv_angle', 0.0))),
            float(shift[0]), float(shift[1]))


def run_tex_scale(run, tex_name, texture_pixel_size):
    """Old ``_geo_run_tex_scale``; *texture_pixel_size* is ``name -> (w, h)``."""
    if run.get('natural_scale'):
        return brush_geometry.natural_repeats(
            run['extent'][0], run['extent'][1], texture_pixel_size(tex_name))
    uv = run.get('uv_scale')
    if uv is not None:
        return float(uv[0]), float(uv[1])
    return 1.0, 1.0


def run_is_drawn(run, play_mode):
    """The old loop's two string compares: caulk never, nodraw not in play."""
    name = run_texture(run)
    if name == 'caulk.jpg':
        return False
    if play_mode and name == 'nodraw.jpg':
        return False
    return True


# ---------------------------------------------------------------------------
# The old submission, as data
# ---------------------------------------------------------------------------

def brush_model_matrix(brush):
    """``T(pos) * R(rot_axis, _rot_angle) * S(size)``, column-major float64.

    Derived from the brush dict, not from a table column, by the scalar
    formula the renderer's matrices are documented to equal.
    """
    pos = np.asarray(brush.get('pos') or (0.0, 0.0, 0.0), dtype=np.float64)
    size = np.asarray(brush.get('size') or (64.0, 64.0, 64.0), dtype=np.float64)
    rot = np.eye(3)
    angle = brush.get('_rot_angle') or 0.0
    if angle:
        axis = np.asarray(brush.get('rot_axis') or (0.0, 1.0, 0.0), dtype=np.float64)
        length = float(np.linalg.norm(axis))
        if length > 0.001:
            ux, uy, uz = axis / length
            a = math.radians(float(angle))
            c, s = math.cos(a), math.sin(a)
            C = 1.0 - c
            rot = np.array([
                [c + ux * ux * C, ux * uy * C - uz * s, ux * uz * C + uy * s],
                [uy * ux * C + uz * s, c + uy * uy * C, uy * uz * C - ux * s],
                [uz * ux * C - uy * s, uz * uy * C + ux * s, c + uz * uz * C]])
    m = np.eye(4)
    m[:3, :3] = rot * size[None, :]
    m[:3, 3] = pos
    inv = np.zeros(3)
    np.divide(1.0, size, out=inv, where=size != 0.0)
    normal = rot * inv[None, :]
    # Column-major, as glUniformMatrix*fv(transpose=GL_FALSE) reads them.
    return m.T.reshape(16), normal.T.reshape(9)


def expected_geo_draws(brushes, play_mode, gl_texture, texture_pixel_size,
                       geometry_of=None):
    """What the old convex loop drew for *brushes*, in its own order.

    One dict per ``glDrawArrays``: the brush, the vertex range and the exact
    vertices it covers, the GL texture bound, the model/normal matrices and the
    three UV uniforms -- every input the draw depends on. *gl_texture* maps a
    texture name to the GL name the old loop bound (``texture_manager.get(...)
    or load_texture_callback(...)``).

    *geometry_of* maps ``id(brush)`` to a snapshot of the brush taken when its
    geometry was last resolved. A mover changes ``pos`` without touching its
    planes, and the old renderer drew it with the mesh it already had -- built
    against the old ``pos`` -- under the new transform; the snapshot is how the
    reference says the same.
    """
    geometry_of = geometry_of or {}
    draws = []
    for brush in brushes:
        if not brush_geometry.brush_has_geometry(brush):
            continue
        mesh = reference_mesh(geometry_of.get(id(brush), brush))
        if mesh is None:
            continue
        model, normal = brush_model_matrix(brush)
        for run in mesh.runs:
            if not run_is_drawn(run, play_mode):
                continue
            name = run_texture(run)
            su, sv = run_tex_scale(run, name, texture_pixel_size)
            angle, shift_u, shift_v = run_tex_transform(run)
            first, count = run['first'], run['count']
            draws.append({
                'brush': brush.get('id'),
                'first': first, 'count': count,
                'vertices': mesh.vertices[first:first + count].tobytes(),
                'texture': int(gl_texture(name) or 0),
                'model': model, 'normal': normal,
                'tex_scale': np.float32([su, sv]).tobytes(),
                'tex_angle': np.float32([angle]).tobytes(),
                'tex_shift': np.float32([shift_u, shift_v]).tobytes(),
            })
    return draws


# ---------------------------------------------------------------------------
# The old submission, drawn (for pixel parity)
# ---------------------------------------------------------------------------

class ReferenceGeoDrawer:
    """Draws angled brushes exactly as the old loop did, from brush dicts.

    Owns its own VAOs, built from :func:`reference_mesh`; uses the renderer's
    ``textured`` program and the uniform state the renderer set up, so the
    only thing that differs from the production pass is the submission.
    """

    def __init__(self):
        self._objects = []

    def _vao(self, mesh):
        import ctypes
        import OpenGL.GL as gl
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, mesh.vertices.nbytes, mesh.vertices,
                        gl.GL_STATIC_DRAW)
        for location, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
            gl.glVertexAttribPointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                     32, ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(location)
        gl.glBindVertexArray(0)
        self._objects.append((vao, vbo))
        return vao

    def draw(self, renderer, brushes, play_mode, gl_texture, texture_pixel_size):
        """The old convex loop over *brushes*; the program must be current."""
        import OpenGL.GL as gl
        uniforms = renderer.uniforms['textured']
        model_loc = uniforms['model']
        normal_loc = uniforms['normalMatrix']
        scale_loc = uniforms['tex_scale']
        angle_loc = uniforms['tex_angle']
        shift_loc = uniforms['tex_shift']
        current_tex = None
        for brush in brushes:
            if not brush_geometry.brush_has_geometry(brush):
                continue
            mesh = reference_mesh(brush)
            if mesh is None:
                continue
            model, normal = brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE,
                                  model.astype(np.float32))
            if normal_loc != -1:
                gl.glUniformMatrix3fv(normal_loc, 1, gl.GL_FALSE,
                                      normal.astype(np.float32))
            gl.glBindVertexArray(self._vao(mesh))
            for run in mesh.runs:
                if not run_is_drawn(run, play_mode):
                    continue
                name = run_texture(run)
                tex_id = gl_texture(name)
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                if scale_loc != -1:
                    su, sv = run_tex_scale(run, name, texture_pixel_size)
                    gl.glUniform2f(scale_loc, su, sv)
                angle, shift_u, shift_v = run_tex_transform(run)
                if angle_loc != -1:
                    gl.glUniform1f(angle_loc, angle)
                if shift_loc != -1:
                    gl.glUniform2f(shift_loc, shift_u, shift_v)
                gl.glDrawArrays(gl.GL_TRIANGLES, run['first'], run['count'])
        gl.glBindVertexArray(0)

    def release(self):
        import OpenGL.GL as gl
        for vao, vbo in self._objects:
            gl.glDeleteVertexArrays(1, [vao])
            gl.glDeleteBuffers(1, [vbo])
        self._objects = []
