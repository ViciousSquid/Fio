"""A draw oracle that reads the GPU's state, not the renderer's tables.

The Debug Tables instrument (and ``_diff_rebuild`` in the stability audit)
proves that the published tables equal a from-scratch rebuild of the world.
It cannot see what the renderer then *does* with them: a wrong handle-to-VAO
mapping, a uniform uploaded from the wrong row, a texture bound for the wrong
run, a cull face left in the wrong state -- all of that happens after the
tables, and a table diff is clean through all of it.

This oracle closes that gap from the other end. While a :class:`DrawCapture`
is active, every ``glDrawArrays``, ``glMultiDrawArrays`` and
``glDrawElements`` issued through ``OpenGL.GL`` is intercepted and, before it
is forwarded, the *live GL state* it will draw with is read back from the
driver:

* the current program, and whether back-face culling is on and which face;
* the vertex array's attribute-0 buffer, and the exact bytes of the vertex
  range the draw covers (``glGetBufferSubData``);
* the texture bound to unit 0;
* the values the program's uniforms hold (``glGetUniformfv``), however they
  were uploaded -- PyOpenGL wrapper or the raw ``uniform_rows`` path;
* the bytes of every other enabled vertex attribute over the same vertices
  (per-vertex face state, when a program takes it as vertex input), read
  through whatever buffer, stride and offset the bound vertex array names;
* for a program that fetches its transforms from a buffer texture by a
  per-vertex index, the transform each vertex will fetch, read from the
  buffer the sampler's texture unit is bound to.

Draws are compared as multisets of *triangles*, each carrying the state it
was drawn with: a renderer is free to order its draws and to cover several
faces with one, but not to change what any triangle is drawn with.

The result is compared with what the frozen old code path says should be drawn
(``tests/helpers/geo_reference.py``), which works from the brush dicts and
never reads a table. A ``GL_PRIMITIVES_GENERATED`` query brackets the capture
as a count that does not depend on the interception at all: a draw that
bypassed ``OpenGL.GL.glDrawArrays`` would show up as primitives the capture
did not see.

Only for tests: every intercepted draw costs several synchronous read-backs.
"""

import ctypes
from collections import Counter

import numpy as np

#: The textured program's per-draw uniforms and their float counts.
TEXTURED_UNIFORMS = (('model', 16), ('normalMatrix', 9), ('tex_scale', 2),
                     ('tex_angle', 1), ('tex_shift', 2))

#: Attribute locations whose bytes, in this order, are a vertex's identity:
#: position, normal and texture coordinate -- 32 bytes, wherever and however
#: interleaved the buffer holding them is.
VERTEX_ATTRIBUTES = (0, 1, 2)

#: Vertex attribute locations that may carry the face UV state instead of
#: uniforms, in the order of the uniforms they stand for.
FACE_ATTRIBUTES = ((3, 'tex_scale'), (4, 'tex_angle'), (5, 'tex_shift'))

#: A per-vertex integer naming the vertex's transform in a buffer texture,
#: for a program that takes its transforms that way instead of as uniforms:
#: the sampler it reads, and the layout it reads -- seven RGBA32F texels per
#: transform, the model matrix's four columns then the normal matrix's three
#: (xyz). The oracle follows the GL state from the sampler uniform to the
#: buffer, so what it reads is what the shader fetches.
SLOT_ATTRIBUTE = 6
TRANSFORM_SAMPLER = 'uTransforms'
TEXELS_PER_TRANSFORM = 7


def _int(value):
    return int(np.asarray(value).reshape(-1)[0])


def convex_programs(renderer):
    """Every program the textured pass may draw convex faces with."""
    return [renderer.shaders.get(name)
            for name in ('textured', 'geo_textured', 'geo_batched')]


class DrawCapture:
    """Context manager recording the GL state of every draw it sees.

    ``glDrawArrays``, ``glMultiDrawArrays`` and ``glDrawElements`` issued
    through ``OpenGL.GL`` are intercepted; each contiguous range or index list
    becomes one entry of :attr:`draws` (a multi-draw call adds one per range),
    and :attr:`calls` counts the API calls themselves.

    *program* restricts the record to draws made with that program, or with
    any of a sequence of programs (others are forwarded untouched);
    *skip_buffers* drops draws whose positions come from one of those buffers
    (the shared cube VBO, say).
    """

    def __init__(self, program=None, uniforms=TEXTURED_UNIFORMS,
                 skip_buffers=()):
        if program is None:
            self.programs = None
        elif isinstance(program, (list, tuple, set)):
            self.programs = {int(p) for p in program if p}
        else:
            self.programs = {int(program)}
        self.uniforms = tuple(uniforms)
        self.skip_buffers = {int(b) for b in skip_buffers if b}
        self.draws = []
        self.calls = 0
        self.primitives = 0
        self._locations = {}
        self._originals = {}
        self._query = None

    # -- interception -----------------------------------------------------
    def __enter__(self):
        import OpenGL.GL as gl
        for name in ('glDrawArrays', 'glMultiDrawArrays', 'glDrawElements'):
            self._originals[name] = getattr(gl, name)
        originals = self._originals

        def glDrawArrays(mode, first, count):
            self._record_call(mode, [np.arange(int(first), int(first) + int(count))])
            return originals['glDrawArrays'](mode, first, count)

        def glMultiDrawArrays(mode, first, count, drawcount):
            firsts = np.asarray(first, dtype=np.int64).reshape(-1)[:int(drawcount)]
            counts = np.asarray(count, dtype=np.int64).reshape(-1)[:int(drawcount)]
            self._record_call(mode, [np.arange(f, f + c)
                                     for f, c in zip(firsts.tolist(), counts.tolist())])
            return originals['glMultiDrawArrays'](mode, first, count, drawcount)

        def glDrawElements(mode, count, kind, indices):
            self._record_call(mode, [self._element_ids(int(count), kind, indices)])
            return originals['glDrawElements'](mode, count, kind, indices)

        gl.glDrawArrays = glDrawArrays
        gl.glMultiDrawArrays = glMultiDrawArrays
        gl.glDrawElements = glDrawElements
        self._query = gl.glGenQueries(1)[0]
        gl.glBeginQuery(gl.GL_PRIMITIVES_GENERATED, self._query)
        return self

    def __exit__(self, exc_type, exc, tb):
        import OpenGL.GL as gl
        for name, fn in self._originals.items():
            setattr(gl, name, fn)
        gl.glEndQuery(gl.GL_PRIMITIVES_GENERATED)
        self.primitives = _int(gl.glGetQueryObjectuiv(self._query,
                                                      gl.GL_QUERY_RESULT))
        gl.glDeleteQueries(1, [self._query])
        return False

    def _location(self, program, name):
        key = (program, name)
        if key not in self._locations:
            import OpenGL.GL as gl
            self._locations[key] = gl.glGetUniformLocation(program, name)
        return self._locations[key]

    # -- reading GPU state --------------------------------------------------
    @staticmethod
    def _read(target_buffer, offset, size):
        """*size* bytes of buffer object *target_buffer* from *offset*."""
        import OpenGL.GL as gl
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, target_buffer)
        out = (ctypes.c_ubyte * size)()
        if size:
            gl.glGetBufferSubData(gl.GL_COPY_READ_BUFFER, offset, size, out)
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, 0)
        return np.frombuffer(bytes(out), dtype=np.uint8)

    def _element_ids(self, count, kind, indices):
        import OpenGL.GL as gl
        ebo = _int(gl.glGetIntegerv(gl.GL_ELEMENT_ARRAY_BUFFER_BINDING))
        width = {int(gl.GL_UNSIGNED_INT): 4, int(gl.GL_UNSIGNED_SHORT): 2,
                 int(gl.GL_UNSIGNED_BYTE): 1}[int(kind)]
        dtype = {4: np.uint32, 2: np.uint16, 1: np.uint8}[width]
        offset = int(indices.value or 0) if isinstance(indices, ctypes.c_void_p) \
            else int(indices or 0)
        return self._read(ebo, offset, count * width).view(dtype).astype(np.int64)

    def _attribute(self, location, ids):
        """Per-vertex bytes of attribute *location* for vertex *ids*, or None."""
        import OpenGL.GL as gl
        if not _int(gl.glGetVertexAttribiv(location,
                                           gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED)):
            return None
        size = _int(gl.glGetVertexAttribiv(location, gl.GL_VERTEX_ATTRIB_ARRAY_SIZE))
        source = _int(gl.glGetVertexAttribiv(
            location, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))
        step = _int(gl.glGetVertexAttribiv(
            location, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE)) or 4 * size
        offset = int(gl.glGetVertexAttribPointerv(
            location, gl.GL_VERTEX_ATTRIB_ARRAY_POINTER) or 0)
        if not len(ids):
            return []
        lo, hi = int(ids.min()), int(ids.max())
        data = self._read(source, offset + lo * step, (hi - lo) * step + 4 * size)
        return [bytes(data[(i - lo) * step:(i - lo) * step + 4 * size])
                for i in ids.tolist()]

    def _transforms(self, program):
        """Per-slot (model, normal) float32 arrays from the transform buffer."""
        import OpenGL.GL as gl
        location = self._location(program, TRANSFORM_SAMPLER)
        if location == -1:
            return None
        unit = (ctypes.c_int * 1)()
        gl.glGetUniformiv(program, location, unit)
        active = _int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE))
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit[0])
        store = _int(gl.glGetIntegerv(gl.GL_TEXTURE_BUFFER_DATA_STORE_BINDING))
        gl.glActiveTexture(active)
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, store)
        size = _int(gl.glGetBufferParameteriv(gl.GL_COPY_READ_BUFFER,
                                              gl.GL_BUFFER_SIZE))
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, 0)
        texels = self._read(store, 0, size).view(np.float32).reshape(-1, 4)
        slots = len(texels) // TEXELS_PER_TRANSFORM
        texels = texels[:slots * TEXELS_PER_TRANSFORM].reshape(
            slots, TEXELS_PER_TRANSFORM, 4)
        return (texels[:, 0:4, :].reshape(slots, 16),
                texels[:, 4:7, 0:3].reshape(slots, 9))

    def _record_call(self, mode, ranges):
        import OpenGL.GL as gl
        program = _int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        if self.programs is not None and program not in self.programs:
            return
        buffer = _int(gl.glGetVertexAttribiv(
            0, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))
        if buffer in self.skip_buffers:
            return
        self.calls += 1
        active = _int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        texture = _int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
        gl.glActiveTexture(active)
        values = {}
        for name, width in self.uniforms:
            location = self._location(program, name)
            if location == -1:
                continue
            held = (ctypes.c_float * 32)()
            gl.glGetUniformfv(program, location, held)
            values[name] = bytes(held)[:4 * width]
        culled = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        cull = _int(gl.glGetIntegerv(gl.GL_CULL_FACE_MODE)) if culled else 0
        transforms = (self._transforms(program)
                      if _int(gl.glGetVertexAttribiv(
                          SLOT_ATTRIBUTE, gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED))
                      else None)
        for ids in ranges:
            parts = [self._attribute(location, ids) for location in VERTEX_ATTRIBUTES]
            keys = [b''.join(corner) for corner in zip(*parts)]
            attributes = {}
            for location, name in FACE_ATTRIBUTES:
                rows = self._attribute(location, ids)
                if rows is not None:
                    attributes[name] = rows
            if transforms is not None:
                slots = np.frombuffer(b''.join(self._attribute(SLOT_ATTRIBUTE, ids)),
                                      dtype=np.int32)
                attributes['model'] = [transforms[0][s].tobytes() for s in slots.tolist()]
                attributes['normalMatrix'] = [transforms[1][s].tobytes()
                                              for s in slots.tolist()]
            self.draws.append({
                'mode': int(mode), 'first': int(ids[0]) if len(ids) else 0,
                'count': len(ids), 'program': program, 'buffer': buffer,
                'stride': 32, 'vertices': b''.join(keys), 'texture': texture,
                'active_unit': active, 'cull': cull,
                'uniforms': values, 'attributes': attributes,
            })


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _f32(blob):
    if len(blob) % 4:
        return np.array([], dtype=np.float32)   # a marker, not float data
    return np.frombuffer(blob, dtype=np.float32)


def _per_triangle(draw, name, tri):
    """A triangle's value of *name*: per-vertex (all corners equal) or uniform."""
    per_vertex = draw.get('attributes', {}).get(name)
    if per_vertex is not None:
        corners = per_vertex[3 * tri:3 * tri + 3]
        return corners[0] if len(set(corners)) == 1 else b'<varies within a triangle>'
    return draw['uniforms'].get(name)


def _triangles_of_capture(draw):
    """``(key, model, normal)`` per triangle of one captured draw."""
    stride = draw['stride']
    out = []
    for tri in range(draw['count'] // 3):
        vertices = _snapped(draw['vertices'][3 * tri * stride:3 * (tri + 1) * stride])
        state = tuple(_per_triangle(draw, name, tri)
                      for _location, name in FACE_ATTRIBUTES)
        out.append(((vertices, draw['texture'], draw['cull']) + state,
                    _f32(_per_triangle(draw, 'model', tri) or b''),
                    _f32(_per_triangle(draw, 'normalMatrix', tri) or b'')))
    return out


#: A vertex value this close to zero is zero (see :func:`compare_geo_draws`).
ZERO = 1e-9


def _snapped(vertices):
    """*vertices* (float32 bytes) with every value within :data:`ZERO` of
    zero -- including ``-0.0`` -- written as ``0.0``."""
    values = np.frombuffer(vertices, dtype=np.float32).copy()
    values[np.abs(values) <= ZERO] = 0.0
    return values.tobytes()


def _triangles_of_expected(draw, stride, cull):
    out = []
    for tri in range(draw['count'] // 3):
        vertices = _snapped(draw['vertices'][3 * tri * stride:3 * (tri + 1) * stride])
        out.append(((vertices, draw['texture'], cull, draw['tex_scale'],
                     draw['tex_angle'], draw['tex_shift']),
                    draw['model'], draw['normal']))
    return out


def compare_geo_draws(expected, captured, cull=None, stride=32,
                      matrix_rtol=1e-6, matrix_atol=1e-4):
    """Mismatches between reference draws and captured draws, as text.

    Compared triangle by triangle, as multisets: draw *order* is the
    renderer's choice (it sorts by texture), and so is how many faces one draw
    covers; what each triangle is drawn with is not. Vertex bytes, the bound
    texture, the cull state (*cull*; ``None`` takes each capture's own) and
    the UV scale/angle/shift -- uniforms or per-vertex attributes, whichever
    the program reads -- must be identical; the model and normal matrices,
    which the reference derives independently from the brush dict, must agree
    to float32 precision. An empty list is a pass.

    One canonicalisation: a vertex value within ``1e-9`` of zero is zero.
    Since Stage 2d the engine builds an angled mesh from the brush's local
    planes while the frozen reference projects its world-space corners (the
    old path's arithmetic), and a texture coordinate that is exactly 0 by
    construction (a corner on the face's UV origin line) comes out as
    anything up to ``~1e-12`` in either. ``1e-9`` is two orders below
    float32's resolution at 1.0 (4e-6 of a texel on a 4,096-texel texture)
    and draws the same pixels; any visible difference still fails.
    """
    problems = []
    got = [t for draw in captured for t in _triangles_of_capture(draw)]
    want = []
    for draw in expected:
        want.extend(_triangles_of_expected(
            draw, stride, draw.get('cull', cull) if cull is not None else None))
    if cull is None:
        got = [((k[0], k[1], None) + k[3:], m, n) for k, m, n in got]
    if len(want) != len(got):
        problems.append('triangle count: expected %d, captured %d'
                        % (len(want), len(got)))
    pending = {}
    for key, model, normal in want:
        pending.setdefault(key, []).append((model, normal))
    unmatched = []
    for key, model, normal in got:
        bucket = pending.get(key)
        if not bucket:
            unmatched.append(key)
            continue
        for index, (want_model, want_normal) in enumerate(bucket):
            if (len(model) == 16 and np.allclose(model, want_model,
                                                 rtol=matrix_rtol, atol=matrix_atol)
                    and (len(normal) != 9 or np.allclose(
                        normal, want_normal, rtol=matrix_rtol,
                        atol=matrix_atol))):
                bucket.pop(index)
                break
        else:
            unmatched.append(key)
    for key in unmatched[:5]:
        problems.append('unexpected triangle: texture=%d cull=%s scale=%s '
                        'angle=%s shift=%s' % (
                            key[1], key[2], _f32(key[3] or b'').tolist(),
                            _f32(key[4] or b'').tolist(),
                            _f32(key[5] or b'').tolist()))
    missing = [key for key, bucket in pending.items() for _ in bucket]
    for key in missing[:5]:
        problems.append('missing triangle: texture=%d scale=%s'
                        % (key[1], _f32(key[3] or b'').tolist()))
    if len(unmatched) > 5 or len(missing) > 5:
        problems.append('... %d unexpected, %d missing in all'
                        % (len(unmatched), len(missing)))
    return problems


def cull_states(captured):
    """``Counter`` of the cull-face state each captured draw was made with."""
    return Counter(draw['cull'] for draw in captured)
