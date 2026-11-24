"""The pointer-taking GL calls a frame makes natively (engine.renderer.core.raw_calls).

Each sets exactly what PyOpenGL's wrapper sets; the callable is PyOpenGL's own
prototype with its pointer a ``void *``; one that does not read back, or
cannot be resolved, is replaced by the wrapper; and the wrappers' cost in
Python calls -- the reason the module exists -- is what was measured.
"""

import ctypes
import sys

import numpy as np
import pytest

pytest.importorskip("PyQt5")

import glm                                          # noqa: E402
import OpenGL.GL as gl                              # noqa: E402

from engine.renderer.core import raw_calls         # noqa: E402
from tests.helpers import gl as glh                 # noqa: E402

pytestmark = [pytest.mark.gl]


@pytest.fixture
def fresh(monkeypatch):
    """The module as a new process finds it: nothing resolved or checked."""
    monkeypatch.setattr(raw_calls, "_resolved", {})
    monkeypatch.setattr(raw_calls, "_verified", {})
    return raw_calls


@pytest.fixture
def renderer():
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64) as context:
        made = glh.make_renderer(None)
        try:
            yield made, context
        finally:
            made.cleanup()
    glh.reset_texture_cache()


def _uniform(program, location, count=16):
    held = (ctypes.c_float * 32)()
    gl.glGetUniformfv(program, location, held)
    return [float(v) for v in held[:count]]


def _buffer(target, size):
    held = (ctypes.c_ubyte * size)()
    gl.glGetBufferSubData(target, 0, size, held)
    return bytes(held)


def test_a_matrix_is_sent_as_the_wrapper_sends_it(fresh, renderer):
    made, _context = renderer
    program = made.shaders['lit']
    gl.glUseProgram(program)
    location = gl.glGetUniformLocation(program, 'view')
    first = glm.translate(glm.mat4(1.0), glm.vec3(3.0, -7.0, 11.0))
    second = glm.perspective(glm.radians(70.0), 1.5, 0.1, 900.0)

    gl.glUniformMatrix4fv(location, 1, gl.GL_FALSE, glm.value_ptr(first))
    by_wrapper = _uniform(program, location)
    gl.glUniformMatrix4fv(location, 1, gl.GL_FALSE, glm.value_ptr(second))
    fresh.uniform_matrix4(location, glm.value_ptr(first))
    assert _uniform(program, location) == by_wrapper
    assert fresh._verified == {'glUniformMatrix4fv': True}
    fresh.uniform_matrix4(location, glm.value_ptr(second))      # the unchecked path
    assert _uniform(program, location) == [float(v) for column in second for v in column]
    fresh.uniform_matrix4(-1, glm.value_ptr(first))              # no such uniform: ignored
    assert gl.glGetError() == gl.GL_NO_ERROR


def test_vertex_pointers_and_uploads_land_where_the_wrappers_put_them(fresh, renderer):
    vao, vbo = gl.glGenVertexArrays(1), gl.glGenBuffers(1)
    try:
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        rows = np.arange(96, dtype=np.float32).reshape(12, 8)
        fresh.buffer_data(gl.GL_ARRAY_BUFFER, rows, gl.GL_DYNAMIC_DRAW)
        assert int(np.ravel(gl.glGetBufferParameteriv(
            gl.GL_ARRAY_BUFFER, gl.GL_BUFFER_SIZE))[0]) == rows.nbytes
        assert _buffer(gl.GL_ARRAY_BUFFER, rows.nbytes) == rows.tobytes()

        patch = np.full((2, 8), -5.0, dtype=np.float32)
        fresh.buffer_sub_data(gl.GL_ARRAY_BUFFER, 3 * 32, patch)
        expected = rows.copy()
        expected[3:5] = -5.0
        assert _buffer(gl.GL_ARRAY_BUFFER, rows.nbytes) == expected.tobytes()
        fresh.buffer_sub_data(gl.GL_ARRAY_BUFFER, 0, expected[6:9])   # a view of rows
        expected[0:3] = expected[6:9]
        assert _buffer(gl.GL_ARRAY_BUFFER, rows.nbytes) == expected.tobytes()

        fresh.buffer_orphan(gl.GL_ARRAY_BUFFER, 4096, gl.GL_STREAM_DRAW)
        assert int(np.ravel(gl.glGetBufferParameteriv(
            gl.GL_ARRAY_BUFFER, gl.GL_BUFFER_SIZE))[0]) == 4096
        assert int(np.ravel(gl.glGetBufferParameteriv(
            gl.GL_ARRAY_BUFFER, gl.GL_BUFFER_USAGE))[0]) == gl.GL_STREAM_DRAW

        gl.glEnableVertexAttribArray(4)
        for offset in (0, 48, 112):
            fresh.vertex_attrib_pointer(4, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, offset)
            held = gl.glGetVertexAttribPointerv(4, gl.GL_VERTEX_ATTRIB_ARRAY_POINTER)
            assert int(held) == offset
            assert gl.glGetVertexAttribiv(4, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE)[0] == 32
            assert gl.glGetVertexAttribiv(4, gl.GL_VERTEX_ATTRIB_ARRAY_SIZE)[0] == 3
            assert gl.glGetVertexAttribiv(4, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING)[0] == vbo
        assert fresh._verified == {name: True for name in (
            'glBufferData', 'glBufferSubData', 'glVertexAttribPointer')}
        assert gl.glGetError() == gl.GL_NO_ERROR
    finally:
        gl.glBindVertexArray(0)
        gl.glDeleteBuffers(1, [vbo])
        gl.glDeleteVertexArrays(1, [vao])


def test_an_upload_of_a_strided_array_goes_through_the_wrapper(fresh, renderer):
    """Only a C-contiguous array is one block of memory to hand over."""
    vbo = gl.glGenBuffers(1)
    try:
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        every_other = np.arange(64, dtype=np.float32)[::2]
        assert not every_other.flags.c_contiguous
        fresh.buffer_data(gl.GL_ARRAY_BUFFER, every_other, gl.GL_STATIC_DRAW)
        assert _buffer(gl.GL_ARRAY_BUFFER, every_other.nbytes) == every_other.tobytes()
    finally:
        gl.glDeleteBuffers(1, [vbo])


def _indexed_frame(made, context):
    """A frame of the lit cube scene."""
    brushes, things = glh.lit_cube_scene()
    config = glh.render_config(all_brushes=brushes, all_things=things)
    projection, view, eye = glh.camera_matrices()
    context.bind()
    with glh.no_gl_errors("frame"):
        made.render_scene(projection, view, eye, None, config,
                          brush_slots=config["all_brush_slots"])
    return context.read_pixels()


def test_a_frame_drawn_natively_is_the_frame_the_wrappers_draw(monkeypatch, renderer):
    made, context = renderer
    native = _indexed_frame(made, context)
    native = _indexed_frame(made, context)
    assert not glh.is_blank(native)
    # Every entry point unresolvable: the same frame through PyOpenGL's wrappers.
    monkeypatch.setattr(raw_calls, "_resolved", {name: None for name in raw_calls._ENTRIES})
    wrapped = _indexed_frame(made, context)
    assert np.array_equal(native, wrapped)


def test_the_native_call_is_pyopengls_own_prototype(fresh, renderer):
    import importlib
    for name, (module, pointer) in raw_calls._ENTRIES.items():
        fn = fresh._entry(name)
        assert fn is not None, name
        theirs = getattr(importlib.import_module('OpenGL.raw.GL.VERSION.' + module), name).load()
        assert type(fn) is type(theirs), "PyOpenGL's function type: its calling convention"
        assert fn.restype is theirs.restype
        assert len(fn.argtypes) == len(theirs.argtypes)
        for index, (ours, declared) in enumerate(zip(fn.argtypes, theirs.argtypes)):
            assert ours is (ctypes.c_void_p if index == pointer else declared), (name, index)
        assert (ctypes.cast(fn, ctypes.c_void_p).value
                == ctypes.cast(theirs, ctypes.c_void_p).value), "the entry point PyOpenGL resolved"


def test_a_call_that_does_not_read_back_falls_back_to_the_wrapper(fresh, renderer, capsys):
    made, _context = renderer
    program = made.shaders['lit']
    gl.glUseProgram(program)
    location = gl.glGetUniformLocation(program, 'view')
    matrix = glm.translate(glm.mat4(1.0), glm.vec3(1.0, 2.0, 3.0))
    sent = []

    def broken(location, count, transpose, pointer):       # reaches GL with nothing
        sent.append(location)
    fresh._resolved['glUniformMatrix4fv'] = broken
    fresh.uniform_matrix4(location, glm.value_ptr(matrix))
    assert sent == [location]
    assert _uniform(program, location) == [float(v) for column in matrix for v in column], (
        "the wrapper sent it after the native call failed its check")
    assert fresh._verified['glUniformMatrix4fv'] is False
    assert "using PyOpenGL's wrapper" in capsys.readouterr().out
    other = glm.mat4(2.0)
    fresh.uniform_matrix4(location, glm.value_ptr(other))
    assert sent == [location], "never called again"
    assert _uniform(program, location)[0] == 2.0


def test_debug_mode_and_an_unresolvable_entry_point_use_the_wrappers(fresh, renderer, monkeypatch):
    monkeypatch.setattr(raw_calls, "raw_uploads_enabled", lambda: False)
    assert all(fresh._entry(name) is None for name in raw_calls._ENTRIES)
    made, _context = renderer
    program = made.shaders['lit']
    gl.glUseProgram(program)
    location = gl.glGetUniformLocation(program, 'view')
    matrix = glm.mat4(3.0)
    fresh.uniform_matrix4(location, glm.value_ptr(matrix))
    assert _uniform(program, location)[0] == 3.0 and fresh._verified == {}


def test_what_the_native_calls_save(fresh, renderer):
    """Python calls beneath one call: the wrapper's argument handling against
    none. Counted, not timed."""
    made, _context = renderer
    program = made.shaders['lit']
    gl.glUseProgram(program)
    location = gl.glGetUniformLocation(program, 'view')
    matrix = glm.mat4(1.0)
    pointer = glm.value_ptr(matrix)
    fresh.uniform_matrix4(location, pointer)                 # resolved and checked

    def calls(function):
        count = [0]

        def tracer(frame, event, arg):
            if event in ('call', 'c_call'):
                count[0] += 1
        sys.setprofile(tracer)
        try:
            function()
        finally:
            sys.setprofile(None)
        return count[0]

    native = calls(lambda: fresh.uniform_matrix4(location, pointer))
    wrapped = calls(lambda: gl.glUniformMatrix4fv(location, 1, gl.GL_FALSE, pointer))
    assert native <= 5, native
    assert wrapped >= 4 * native, (wrapped, native)
