"""Per-row uniform uploads from a raw address.

The brush passes that draw one row at a time (lit, textured, glow, water,
glass, fog, the non-instanced shadow casters) send a ``glUniformMatrix4fv``
per drawn row, and most also a ``glUniformMatrix3fv`` and a ``glUniform3fv``,
each passing a row view of a NumPy array.  PyOpenGL's wrapper runs every array
argument through its NumPy handler, which reads
``ndarray.__array_interface__`` (a dict built per access) and converts the
row.  Measured in place on the showcase (llvmpipe, 300 frames, 3 rounds;
``tools/profile_uniform_uploads.py``): **12-16 us a call** through the
wrapper, **2.7-3.7 us** through this module, timer included (an isolated
micro-benchmark puts the bare calls at 6.2 vs 1.0 us; the audit's F-14
measured 6.6 vs 1.5 us).  ~28 such uploads a steady showcase frame.

How the native call is made, and why it is the call PyOpenGL makes
------------------------------------------------------------------
The entry point is the one PyOpenGL resolves for itself
(``OpenGL.raw.GL.VERSION.GL_2_0``), and the callable is built from **its**
ctypes function type and **its** declared argument types: same calling
convention flags (``functionTypeFor`` the GL library: ``WINFUNCTYPE`` on
Windows, ``CFUNCTYPE`` elsewhere), same ``GLint``/``GLsizei``/``GLboolean``
scalars, same return type.  The one difference is the last argument, declared
``void *`` instead of PyOpenGL's array type -- whose ``from_param`` produces
that same pointer after its handler has run.  So the native ABI of the call
is PyOpenGL's own on every platform it supports (x86-64, AArch64 including
Windows on ARM64, where there is a single calling convention), and the
function pointer is the one PyOpenGL caches process-wide after its first
resolution.

What makes the call safe, and what does not prove it
-----------------------------------------------------
* The function pointer is PyOpenGL's own resolution, refused if null.
* The signature is PyOpenGL's own (above); nothing here declares a type
  PyOpenGL does not already call through.
* The data pointer is only ever ``base + index * stride`` of the array the
  uploader holds, with ``0 <= index < len(rows)`` checked on every call (an
  out-of-range index raises ``IndexError`` instead of reaching GL as an
  address), and the array is kept alive by the uploader itself.
* A one-time read-back: the first raw upload of each kind in a process is
  read with ``glGetUniformfv`` -- through PyOpenGL's checked wrapper, into a
  buffer larger than any uniform this module sends -- and compared byte for
  byte with the row.  If they differ, or the check cannot run, that kind
  falls back to the wrapper for the rest of the process (the row is re-sent
  through it) with one line on stdout.  This catches wrong *data* reaching
  the uniform; it is not a proof that the call's ABI is right on a platform
  -- a wrong calling convention would more likely crash than mis-store.
  That rests on the function type being PyOpenGL's own.

Platform status: exercised on Linux x86-64 (Mesa llvmpipe, GLX) only.
Windows on ARM64, Android and macOS have not run this path; they are
expected to behave like PyOpenGL's own calls for the reasons above, but are
unverified until the GL tier (``tests/e2e/test_raw_uniform_uploads.py``) has
run there.

Why the base address is taken per pass rather than kept across frames: the
model/normal buffers persist and their address is stable from frame to frame,
but a pass that draws more rows than they hold reallocates them.  Reading the
base from the array actually being uploaded costs 0.85 us, once per pass, and
cannot go stale; the returned uploader holds the array, so the memory it
points at stays alive while it is in use.

The raw path does not depend on PyOpenGL's error checking being off: the test
suite runs with it on and should run the uploads the app runs (a GL error a
raw call raises stays latched and is reported by the next checked call).
``FIO_GL_DEBUG=1`` -- the switch that asks for every GL call to be checked and
counted per function -- routes the uploads through the high-level wrappers
instead, as does an entry point that cannot be resolved.  Both paths send the
same bytes.
"""

import ctypes
import os

import numpy as np
import OpenGL.GL as gl

#: Uniform kinds this module uploads: name -> (entry point, takes a
#: ``transpose`` argument, float32 elements per row).
_KINDS = {
    'mat4': ('glUniformMatrix4fv', True, 16),
    'mat3': ('glUniformMatrix3fv', True, 9),
    'vec3': ('glUniform3fv', False, 3),
}

#: name -> ctypes callable taking ``void *`` data, or None when the raw entry
#: point cannot be resolved.  Filled on first use, which needs a GL context.
_resolved = {}

#: name -> True once a raw upload of that kind read back exactly, False once
#: one did not (or could not be checked).  Absent: not checked yet.
_verified = {}

_GL_DEBUG = os.environ.get("FIO_GL_DEBUG") == "1"


def raw_uploads_enabled():
    """True unless ``FIO_GL_DEBUG=1`` asked for every GL call to be checked."""
    return not _GL_DEBUG


def _raw_entry(name):
    """The resolved native entry point *name*, taking its data as ``void *``."""
    if name in _resolved:
        return _resolved[name]
    fn = None
    try:
        from OpenGL.raw.GL.VERSION import GL_2_0
        loaded = getattr(GL_2_0, name).load()
        address = (ctypes.cast(loaded, ctypes.c_void_p).value
                   if loaded is not None else None)
        if address:
            # PyOpenGL's own function type (calling convention and flags)
            # and its scalar argument types; only the data pointer's
            # declared type changes.
            fn = type(loaded)(address)
            fn.argtypes = tuple(loaded.argtypes[:-1]) + (ctypes.c_void_p,)
            fn.restype = loaded.restype
    except Exception:
        fn = None
    _resolved[name] = fn
    return fn


#: Floats the read-back buffer holds: the largest uniform GL can return for
#: one location (a mat4) and then some, so a location whose uniform is larger
#: than the kind sent can never write past the buffer.
_READBACK_FLOATS = 32


def _readback_matches(location, row, width):
    """Whether the current program's uniform at *location* holds *row*.

    Only PyOpenGL's checked wrappers are called here.
    """
    program = int(np.asarray(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM)).reshape(-1)[0])
    if not program:
        return False
    held = (ctypes.c_float * _READBACK_FLOATS)()
    gl.glGetUniformfv(program, location, held)
    sent = np.ascontiguousarray(row, dtype=np.float32).tobytes()
    return bytes(held)[:4 * width] == sent


def _wrapped(name, has_transpose, rows):
    wrapper = getattr(gl, name)
    count = len(rows)
    # Indexed exactly as the raw path is: no negative rows from the end.
    if has_transpose:
        def upload(location, index):
            if not 0 <= index < count:
                raise IndexError("row %r of %d" % (index, count))
            wrapper(location, 1, gl.GL_FALSE, rows[index])
    else:
        def upload(location, index):
            if not 0 <= index < count:
                raise IndexError("row %r of %d" % (index, count))
            wrapper(location, 1, rows[index])
    return upload


def uniform_rows(kind, rows):
    """``upload(location, index)`` sending row *index* of *rows* as a uniform.

    *kind* is ``'mat4'``, ``'mat3'`` or ``'vec3'``; *rows* is a 2-D float32
    array with that many elements per row (a scratch-buffer view or a
    gathered table column).  Matrices are sent untransposed: the arrays are
    already column-major, as ``engine.render_table.model_matrices`` builds
    them.  Call once per pass, then once per drawn row.
    """
    name, has_transpose, width = _KINDS[kind]
    if (rows.dtype != np.float32 or rows.ndim != 2 or rows.shape[1] != width
            or not rows.flags.c_contiguous):
        rows = np.ascontiguousarray(rows, dtype=np.float32).reshape(-1, width)
    verdict = _verified.get(name)
    fn = (_raw_entry(name) if raw_uploads_enabled() and verdict is not False
          else None)
    if fn is None:
        return _wrapped(name, has_transpose, rows)
    base = rows.ctypes.data
    stride = rows.strides[0]
    count = len(rows)
    if verdict is None:
        return _verifying(name, has_transpose, width, fn, rows, base, stride)
    # ``rows`` is referenced by the closure: the address stays valid for as
    # long as the uploader exists.
    if has_transpose:
        def upload(location, index, _rows=rows):
            if not 0 <= index < count:
                raise IndexError("row %r of %d" % (index, count))
            fn(location, 1, 0, base + index * stride)
    else:
        def upload(location, index, _rows=rows):
            if not 0 <= index < count:
                raise IndexError("row %r of %d" % (index, count))
            fn(location, 1, base + index * stride)
    return upload


def _verifying(name, has_transpose, width, fn, rows, base, stride):
    """The uploader for a kind not checked yet: checks its first real upload."""
    wrapped = _wrapped(name, has_transpose, rows)
    count = len(rows)

    def upload(location, index):
        if not 0 <= index < count:
            raise IndexError("row %r of %d" % (index, count))
        verdict = _verified.get(name)
        if verdict is False:
            wrapped(location, index)
            return
        if has_transpose:
            fn(location, 1, 0, base + index * stride)
        else:
            fn(location, 1, base + index * stride)
        if verdict is None and location >= 0:
            try:
                ok = _readback_matches(location, rows[index], width)
            except Exception:
                ok = False
            _verified[name] = ok
            if not ok:
                print("[Renderer] raw %s did not read back what it sent; "
                      "using PyOpenGL's wrapper for it from now on" % name)
                wrapped(location, index)
    return upload
