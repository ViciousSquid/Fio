"""A draw oracle that reads the GPU's state, not the renderer's tables.

The Debug Tables instrument (and ``_diff_rebuild`` in the stability audit)
proves that the published tables equal a from-scratch rebuild of the world.
It cannot see what the renderer then *does* with them: a wrong handle-to-VAO
mapping, a uniform uploaded from the wrong row, a texture bound for the wrong
run, a cull face left in the wrong state -- all of that happens after the
tables, and a table diff is clean through all of it.

This oracle closes that gap from the other end. While a :class:`DrawCapture`
is active, every ``glDrawArrays`` issued through ``OpenGL.GL`` is intercepted
and, before it is forwarded, the *live GL state* it will draw with is read
back from the driver:

* the current program, and whether back-face culling is on and which face;
* the vertex array's attribute-0 buffer, and the exact bytes of the vertex
  range the draw covers (``glGetBufferSubData``);
* the texture bound to unit 0;
* the values the program's uniforms hold (``glGetUniformfv``), however they
  were uploaded -- PyOpenGL wrapper or the raw ``uniform_rows`` path;
* the bytes of every other enabled vertex attribute over the same range
  (per-vertex face state, when a program takes it as vertex input).

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

#: Vertex attribute locations that may carry the face UV state instead of
#: uniforms, in the order of the uniforms they stand for.
FACE_ATTRIBUTES = ((3, 'tex_scale'), (4, 'tex_angle'), (5, 'tex_shift'))


def _int(value):
    return int(np.asarray(value).reshape(-1)[0])


class DrawCapture:
    """Context manager recording the GL state of each ``glDrawArrays``.

    *program* restricts the record to draws made with that program, or with
    any of a sequence of programs (others are forwarded untouched); *skip_buffers* drops draws whose vertex data comes
    from one of those buffers (the shared cube VBO, say). The stride of the
    vertex data is read from the bound VAO.
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
        self.primitives = 0
        self._locations = {}
        self._original = None
        self._query = None

    # -- interception -----------------------------------------------------
    def __enter__(self):
        import OpenGL.GL as gl
        self._original = gl.glDrawArrays
        original = self._original

        def glDrawArrays(mode, first, count):
            self._record(mode, int(first), int(count))
            return original(mode, first, count)

        gl.glDrawArrays = glDrawArrays
        self._query = gl.glGenQueries(1)[0]
        gl.glBeginQuery(gl.GL_PRIMITIVES_GENERATED, self._query)
        return self

    def __exit__(self, exc_type, exc, tb):
        import OpenGL.GL as gl
        gl.glDrawArrays = self._original
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

    def _record(self, mode, first, count):
        import OpenGL.GL as gl
        program = _int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        if self.programs is not None and program not in self.programs:
            return
        buffer = _int(gl.glGetVertexAttribiv(
            0, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))
        if buffer in self.skip_buffers:
            return
        stride = _int(gl.glGetVertexAttribiv(
            0, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE)) or 12
        previous = _int(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, buffer)
        raw = (ctypes.c_ubyte * (count * stride))()
        gl.glGetBufferSubData(gl.GL_ARRAY_BUFFER, first * stride,
                              count * stride, raw)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, previous)
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
        attributes = {}
        for location, name in FACE_ATTRIBUTES:
            if not _int(gl.glGetVertexAttribiv(
                    location, gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED)):
                continue
            size = _int(gl.glGetVertexAttribiv(
                location, gl.GL_VERTEX_ATTRIB_ARRAY_SIZE))
            source = _int(gl.glGetVertexAttribiv(
                location, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))
            step = _int(gl.glGetVertexAttribiv(
                location, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE)) or 4 * size
            offset = int(gl.glGetVertexAttribPointerv(
                location, gl.GL_VERTEX_ATTRIB_ARRAY_POINTER) or 0)
            # Whole vertices of the attribute's buffer, then the attribute's
            # bytes out of each (assumes interleaving within one stride).
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, source)
            data = (ctypes.c_ubyte * (count * step))()
            gl.glGetBufferSubData(gl.GL_ARRAY_BUFFER, first * step,
                                  count * step, data)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, previous)
            rows = np.frombuffer(bytes(data), dtype=np.uint8).reshape(count, step)
            attributes[name] = [bytes(row[offset:offset + 4 * size])
                                for row in rows]
        culled = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        self.draws.append({
            'mode': int(mode), 'first': first, 'count': count,
            'program': program, 'buffer': buffer, 'stride': stride,
            'vertices': bytes(raw), 'texture': texture,
            'active_unit': active,
            'cull': (_int(gl.glGetIntegerv(gl.GL_CULL_FACE_MODE))
                     if culled else 0),
            'uniforms': values, 'attributes': attributes,
        })


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _f32(blob):
    if len(blob) % 4:
        return np.array([], dtype=np.float32)   # a marker, not float data
    return np.frombuffer(blob, dtype=np.float32)


def _triangles_of_capture(draw):
    """``(key, model, normal)`` per triangle of one captured draw."""
    stride = draw['stride']
    uniforms = draw['uniforms']
    attributes = draw.get('attributes', {})
    out = []
    for tri in range(draw['count'] // 3):
        vertices = draw['vertices'][3 * tri * stride:3 * (tri + 1) * stride]
        state = []
        for _location, name in FACE_ATTRIBUTES:
            per_vertex = attributes.get(name)
            if per_vertex is not None:
                corners = per_vertex[3 * tri:3 * tri + 3]
                if len(set(corners)) != 1:
                    state.append(b'<varies within a triangle>')
                else:
                    state.append(corners[0])
            else:
                state.append(uniforms.get(name))
        out.append(((vertices, draw['texture'], draw['cull']) + tuple(state),
                    _f32(uniforms.get('model', b'')),
                    _f32(uniforms.get('normalMatrix', b''))))
    return out


def _triangles_of_expected(draw, stride, cull):
    out = []
    for tri in range(draw['count'] // 3):
        vertices = draw['vertices'][3 * tri * stride:3 * (tri + 1) * stride]
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
