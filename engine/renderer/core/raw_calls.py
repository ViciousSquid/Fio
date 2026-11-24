"""The GL calls a frame makes with a pointer or a buffer offset, made natively.

A PyOpenGL call whose argument is an array, a pointer or an offset is not one
Python call: the argument goes through its array handlers first, and the
wrapped entry points (``glUniformMatrix4fv``, ``glBufferData``, ...) through
argument calculators besides.  Counted with ``sys.setprofile`` on a showcase
frame (Classic, Play): 26 Python calls beneath each ``glUniformMatrix4fv``,
20 beneath each ``glVertexAttribPointer`` (the raw entry point included: its
``void *`` is PyOpenGL's array type), 18 beneath each ``glDrawElements``,
37 beneath each ``glBufferData`` / ``glBufferSubData`` -- 1,700 of the
frame's 7,300 Python calls for 70 GL calls, and 3-4 us each where the native
call is 0.5 us.

This module makes those calls the way :mod:`.uniform_rows` makes its per-row
uniform uploads, and for the same reasons is the call PyOpenGL makes:

* the entry point is the one PyOpenGL resolves for itself, refused if null;
* the callable is built from PyOpenGL's own ctypes function type (calling
  convention and flags) and its own declared scalar argument and return
  types.  The one difference is the pointer argument, declared ``void *`` in
  place of PyOpenGL's array type, whose ``from_param`` produces that same
  pointer after its handlers have run;
* the first native call of each kind in a process is checked by reading back
  what it set, through PyOpenGL's checked wrappers; if it does not read back
  (or cannot be checked) that kind uses PyOpenGL's wrapper from then on, the
  value sent again through it, with one line on stdout.  ``glDrawElements``
  sets nothing to read back and rests on the prototype alone.

``FIO_GL_DEBUG=1`` -- every GL call checked and counted per function --
routes all of these through the wrappers, as does an entry point that cannot
be resolved.  Both paths make the same GL call with the same values.

Platform status: as :mod:`.uniform_rows` -- exercised on Linux x86-64 (Mesa
llvmpipe, GLX); elsewhere expected to behave like PyOpenGL's own calls for
the reasons above.

Callers keep what a pointer points at alive across the call, as they must
for PyOpenGL; nothing here holds an address past the call that takes it.
"""

import ctypes

import numpy as np
import OpenGL.GL as gl

from .uniform_rows import raw_uploads_enabled

#: entry point -> (PyOpenGL's raw module, index of its pointer argument).
_ENTRIES = {
    'glUniformMatrix4fv': ('GL_2_0', 3),
    'glVertexAttribPointer': ('GL_2_0', 5),
    'glDrawElements': ('GL_1_1', 3),
    'glDrawElementsInstanced': ('GL_3_1', 3),
    'glBufferData': ('GL_1_5', 2),
    'glBufferSubData': ('GL_1_5', 3),
}

#: name -> the native callable, or None when it cannot be resolved (or
#: FIO_GL_DEBUG asked for the wrappers).  Filled on first use, with a context.
_resolved = {}

#: name -> True once a native call of that kind read back what it set, False
#: once one did not.  Absent: not checked yet.
_verified = {}


def _entry(name):
    """The resolved native entry point *name*, its pointer a ``void *``."""
    if name in _resolved:
        return _resolved[name]
    fn = None
    if raw_uploads_enabled():
        try:
            import importlib
            module, pointer = _ENTRIES[name]
            loaded = getattr(importlib.import_module(
                'OpenGL.raw.GL.VERSION.' + module), name).load()
            address = (ctypes.cast(loaded, ctypes.c_void_p).value
                       if loaded is not None else None)
            if address:
                fn = type(loaded)(address)
                types = list(loaded.argtypes)
                types[pointer] = ctypes.c_void_p
                fn.argtypes = tuple(types)
                fn.restype = loaded.restype
        except Exception:
            fn = None
    _resolved[name] = fn
    return fn


def _refused(name):
    _verified[name] = False
    _resolved[name] = None
    print("[Renderer] native %s did not read back what it set; "
          "using PyOpenGL's wrapper for it from now on" % name)


def _address(array):
    """Where a C-contiguous array's data starts (no Python-level call)."""
    return array.__array_interface__['data'][0]


# -- uniforms ------------------------------------------------------------------

def uniform_matrix4(location, pointer):
    """``glUniformMatrix4fv(location, 1, GL_FALSE, pointer)``: one column-major
    4x4 float matrix.  *pointer* is what ``glm.value_ptr(matrix)`` returns (a
    ctypes float pointer)."""
    fn = _resolved.get('glUniformMatrix4fv', _entry)
    if fn is _entry:
        fn = _entry('glUniformMatrix4fv')
    if fn is None:
        gl.glUniformMatrix4fv(location, 1, gl.GL_FALSE, pointer)
        return
    fn(location, 1, 0, pointer)
    if 'glUniformMatrix4fv' not in _verified and location >= 0:
        try:
            program = int(np.ravel(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))[0])
            held = (ctypes.c_float * 32)()
            gl.glGetUniformfv(program, location, held)
            ok = bool(program) and bytes(held)[:64] == ctypes.string_at(pointer, 64)
        except Exception:
            ok = False
        if ok:
            _verified['glUniformMatrix4fv'] = True
        else:
            _refused('glUniformMatrix4fv')
            gl.glUniformMatrix4fv(location, 1, gl.GL_FALSE, pointer)


# -- vertex layout ----------------------------------------------------------------

def vertex_attrib_pointer(index, size, kind, normalized, stride, offset):
    """``glVertexAttribPointer`` with *offset* a byte offset into the bound
    array buffer (an int)."""
    fn = _resolved.get('glVertexAttribPointer', _entry)
    if fn is _entry:
        fn = _entry('glVertexAttribPointer')
    if fn is None:
        gl.glVertexAttribPointer(index, size, kind, normalized, stride,
                                 ctypes.c_void_p(offset))
        return
    fn(index, size, kind, normalized, stride, offset)
    if 'glVertexAttribPointer' not in _verified:
        try:
            held = gl.glGetVertexAttribPointerv(index, gl.GL_VERTEX_ATTRIB_ARRAY_POINTER)
            try:
                held = int(held)
            except TypeError:
                held = int(ctypes.cast(held, ctypes.c_void_p).value or 0)
            ok = (held == int(offset)
                  and int(gl.glGetVertexAttribiv(
                      index, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE)[0]) == int(stride)
                  and int(gl.glGetVertexAttribiv(
                      index, gl.GL_VERTEX_ATTRIB_ARRAY_SIZE)[0]) == int(size))
        except Exception:
            ok = False
        if ok:
            _verified['glVertexAttribPointer'] = True
        else:
            _refused('glVertexAttribPointer')
            gl.glVertexAttribPointer(index, size, kind, normalized, stride,
                                     ctypes.c_void_p(offset))


# -- draws --------------------------------------------------------------------------

def draw_elements(mode, count, kind, offset):
    """``glDrawElements`` with *offset* a byte offset into the bound element
    buffer (an int)."""
    fn = _resolved.get('glDrawElements', _entry)
    if fn is _entry:
        fn = _entry('glDrawElements')
    if fn is None:
        gl.glDrawElements(mode, count, kind, ctypes.c_void_p(offset))
    else:
        fn(mode, count, kind, offset)


def draw_elements_instanced(mode, count, kind, offset, instances):
    """``glDrawElementsInstanced`` with *offset* a byte offset (an int)."""
    fn = _resolved.get('glDrawElementsInstanced', _entry)
    if fn is _entry:
        fn = _entry('glDrawElementsInstanced')
    if fn is None:
        gl.glDrawElementsInstanced(mode, count, kind, ctypes.c_void_p(offset), instances)
    else:
        fn(mode, count, kind, offset, instances)


# -- buffer uploads -------------------------------------------------------------------

#: How much of an upload the one-time check reads back.
_CHECKED_BYTES = 256


def _holds(target, offset, array):
    """Whether the buffer bound to *target* holds the start of *array* at
    *offset* (PyOpenGL's checked wrappers only)."""
    size = min(int(array.nbytes), _CHECKED_BYTES)
    held = (ctypes.c_ubyte * size)()
    gl.glGetBufferSubData(target, offset, size, held)
    return bytes(held) == array.tobytes()[:size]


def buffer_data(target, array, usage):
    """``glBufferData(target, array.nbytes, array, usage)``: a new store for
    the bound buffer, holding *array* (a NumPy array)."""
    fn = _resolved.get('glBufferData', _entry)
    if fn is _entry:
        fn = _entry('glBufferData')
    if fn is None or not array.flags.c_contiguous:
        gl.glBufferData(target, array.nbytes, array, usage)
        return
    fn(target, array.nbytes, _address(array), usage)
    if 'glBufferData' not in _verified and array.nbytes:
        try:
            ok = _holds(target, 0, array)
        except Exception:
            ok = False
        if ok:
            _verified['glBufferData'] = True
        else:
            _refused('glBufferData')
            gl.glBufferData(target, array.nbytes, array, usage)


def buffer_orphan(target, nbytes, usage):
    """``glBufferData(target, nbytes, None, usage)``: a new, unfilled store
    for the bound buffer, so an upload does not wait for the draws still
    reading the old one."""
    fn = _resolved.get('glBufferData', _entry)
    if fn is _entry:
        fn = _entry('glBufferData')
    if fn is None:
        gl.glBufferData(target, nbytes, None, usage)
    else:
        fn(target, nbytes, None, usage)


def buffer_sub_data(target, offset, array):
    """``glBufferSubData(target, offset, array.nbytes, array)``: *array* (a
    NumPy array) written into the bound buffer's store at *offset*."""
    fn = _resolved.get('glBufferSubData', _entry)
    if fn is _entry:
        fn = _entry('glBufferSubData')
    if fn is None or not array.flags.c_contiguous:
        gl.glBufferSubData(target, offset, array.nbytes, array)
        return
    fn(target, offset, array.nbytes, _address(array))
    if 'glBufferSubData' not in _verified and array.nbytes:
        try:
            ok = _holds(target, offset, array)
        except Exception:
            ok = False
        if ok:
            _verified['glBufferSubData'] = True
        else:
            _refused('glBufferSubData')
            gl.glBufferSubData(target, offset, array.nbytes, array)
