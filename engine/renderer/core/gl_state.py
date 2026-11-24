"""GL state the renderer has set, so that it is not set again.

A frame is made of passes that each establish what they need: the program,
the active unit, a texture on it, depth and blend state, a sampler's unit, a
tint. Most of it is what the pass before, or the frame before, left there,
and GL was told again all the same -- 164 of the 701 state and uniform calls
of a ``_SHOWCASE`` frame set a value already set.

The functions here take the arguments of the ``OpenGL.GL`` function of the
same name and make the call only when it changes something:

* **bindings and switches** (program, active unit, texture per unit and
  target, vertex array, ``glEnable`` / ``glDisable``, depth mask and
  function, cull face, polygon mode, blend function) are known only inside
  a renderer's frame -- between :func:`begin_frame` and :func:`end_frame`.
  Outside one, anything (Qt's painter, an overlay, a loader) may have
  changed them, so every call goes to GL and nothing is remembered.
* **uniforms** are a program's own state and outlive the frame: a sampler's
  unit set when the program was first used is still set a thousand frames
  later. What each program holds is kept per renderer (the mapping passed to
  :func:`begin_frame`). A uniform set outside a frame, when the current
  program is not known, is sent and drops what is remembered of every
  program.

Every renderer module sets this state through here; a call made straight to
``OpenGL.GL`` during a frame would leave what is remembered wrong. The
deleting calls are here because deleting a bound object unbinds it, and a
deleted name may be given out again.

``FIO_GL_DEBUG=1`` (every GL call checked and counted) sends everything.
"""

import os
import weakref

import numpy as np
import OpenGL.GL as gl

_PASS_THROUGH = os.environ.get('FIO_GL_DEBUG') == '1'

_live = 0               # frames begun and not ended (a probe's frame nests)
_program = None         # the program in use; None: not known
_held = None            # {location: value} of that program; None: not known
_unit = None
_textures = {}          # (unit, target) -> texture
_vao = None
_caps = {}              # capability -> enabled
_misc = {}              # function name -> its arguments
_uniforms = None        # program -> {location: value}, the live renderer's
_stores = weakref.WeakSet()
_outside_dropped = False


class UniformStore(dict):
    """What each of one renderer's programs holds: program -> {location: value}."""
    __slots__ = ('__weakref__',)
    __hash__ = object.__hash__          # kept in a set by identity
    __eq__ = object.__eq__


def _forget_bindings():
    global _program, _held, _unit, _vao
    _program = _held = _unit = _vao = None
    _textures.clear()
    _caps.clear()
    _misc.clear()


def begin_frame(store):
    """A renderer's frame starts; *store* is its :class:`UniformStore`."""
    global _live, _uniforms, _outside_dropped
    if _PASS_THROUGH:
        return
    if _live == 0 or store is not _uniforms:
        _forget_bindings()
    _uniforms = store
    _stores.add(store)
    _outside_dropped = False
    _live += 1


def end_frame():
    global _live
    if _PASS_THROUGH:
        return
    _live -= 1
    if _live <= 0:
        _live = 0
        _forget_bindings()


def forget():
    """GL state was changed by something else: remember nothing of it."""
    _forget_bindings()
    for store in list(_stores):
        store.clear()


def _outside_uniform():
    """A uniform set with the program not known: any program may hold it."""
    global _outside_dropped
    if not _outside_dropped:
        for store in list(_stores):
            store.clear()
        _outside_dropped = True


def touch(location):
    """The uniform at *location* of the program in use was set elsewhere."""
    if _held is not None:
        _held.pop(location, None)
    elif not _outside_dropped:
        _outside_uniform()


# -- bindings and switches ------------------------------------------------------------

def glUseProgram(program):
    global _program, _held
    if _live:
        if program == _program:
            return
        _program = program
        if program in _uniforms:
            _held = _uniforms[program]
        else:
            _held = _uniforms[program] = {}
    gl.glUseProgram(program)


def glActiveTexture(unit):
    global _unit
    if _live:
        if unit == _unit:
            return
        _unit = unit
    gl.glActiveTexture(unit)


def glBindTexture(target, texture):
    if _live and _unit is not None:
        key = (_unit, target)
        if key in _textures and _textures[key] == texture:
            return
        _textures[key] = texture
    gl.glBindTexture(target, texture)


def glBindVertexArray(array):
    global _vao
    if _live:
        if array == _vao:
            return
        _vao = array
    gl.glBindVertexArray(array)


def glEnable(capability):
    if _live:
        if capability in _caps and _caps[capability] is True:
            return
        _caps[capability] = True
    gl.glEnable(capability)


def glDisable(capability):
    if _live:
        if capability in _caps and _caps[capability] is False:
            return
        _caps[capability] = False
    gl.glDisable(capability)


def glDepthMask(flag):
    if _live:
        flag = bool(flag)
        if 'glDepthMask' in _misc and _misc['glDepthMask'] is flag:
            return
        _misc['glDepthMask'] = flag
    gl.glDepthMask(flag)


def glDepthFunc(func):
    if _live:
        if 'glDepthFunc' in _misc and _misc['glDepthFunc'] == func:
            return
        _misc['glDepthFunc'] = func
    gl.glDepthFunc(func)


def glCullFace(mode):
    if _live:
        if 'glCullFace' in _misc and _misc['glCullFace'] == mode:
            return
        _misc['glCullFace'] = mode
    gl.glCullFace(mode)


def glPolygonMode(face, mode):
    if _live:
        value = (face, mode)
        if 'glPolygonMode' in _misc and _misc['glPolygonMode'] == value:
            return
        _misc['glPolygonMode'] = value
    gl.glPolygonMode(face, mode)


def glBlendFunc(source, destination):
    if _live:
        value = (source, destination, source, destination)
        if 'blend' in _misc and _misc['blend'] == value:
            return
        _misc['blend'] = value
    gl.glBlendFunc(source, destination)


def glBlendFuncSeparate(source, destination, source_alpha, destination_alpha):
    if _live:
        value = (source, destination, source_alpha, destination_alpha)
        if 'blend' in _misc and _misc['blend'] == value:
            return
        _misc['blend'] = value
    gl.glBlendFuncSeparate(source, destination, source_alpha, destination_alpha)


# -- deleting: unbinds, and frees a name to be given out again -------------------------

def glDeleteTextures(*args):
    _textures.clear()
    gl.glDeleteTextures(*args)


def glDeleteVertexArrays(*args):
    global _vao
    _vao = None
    gl.glDeleteVertexArrays(*args)


def glDeleteProgram(program):
    global _program, _held
    _program = _held = None
    for store in list(_stores):
        store.pop(program, None)
    gl.glDeleteProgram(program)


# -- uniforms -------------------------------------------------------------------------

def _scalar(name):
    return _made(name, 'value', 'value')


def _vector(name):
    width = int(name[len('glUniform')])
    names = ', '.join('v%d' % index for index in range(width))
    return _made(name, names, '(%s)' % names)


_UNIFORM = """
def {name}(location, {arguments}):
    held = _held
    if held is None:
        _outside_uniform()
    elif location is not None:
        value = {value}
        if location == -1 or (location in held and held[location] == value):
            return
        held[location] = value
    gl.{name}(location, {arguments})
"""


def _made(name, arguments, value):
    """The function for a uniform set by value: written out so that the
    comparison costs no call and the send is looked up on ``OpenGL.GL`` when
    made (a test recording GL calls replaces it there)."""
    source = _UNIFORM.format(name=name, arguments=arguments, value=value)
    exec(compile(source, __file__, 'exec'), globals())
    return globals()[name]


def _key(value):
    """Something to compare an array argument by, or None when it cannot be
    (a pointer: what it points at is not ours to read)."""
    kind = type(value)
    if kind is np.ndarray:
        return value.tobytes()
    if kind is tuple or kind is list:
        key = tuple(value)
    elif hasattr(value, 'to_tuple'):            # a glm vector
        key = value.to_tuple()
    else:
        return None
    for item in key:
        if type(item) is not float and type(item) is not int:
            return None
    return key


def _array(name):
    def uniform(location, count, value):
        held = _held
        if held is None:
            _outside_uniform()
        elif location is not None:
            if location == -1:
                return
            if count == 1:
                key = _key(value)
                if key is None:
                    held.pop(location, None)
                else:
                    if held.get(location) == key:
                        return
                    held[location] = key
            elif type(value) is np.ndarray:
                # An array's elements have locations of their own, in a row:
                # each is recorded as holding this send, so that one set
                # singly afterwards is seen.
                key = (count, value.tobytes())
                was = held.get(location)
                covered = range(location, location + count)
                if was == key and type(was) is tuple:
                    for index in covered:
                        if held.get(index) is not was:
                            break
                    else:
                        return
                for index in covered:
                    held[index] = key
            else:
                for index in range(location, location + count):
                    held.pop(index, None)
        getattr(gl, name)(location, count, value)
    uniform.__name__ = name
    return uniform


glUniform1i = _scalar('glUniform1i')
glUniform1f = _scalar('glUniform1f')
glUniform1ui = _scalar('glUniform1ui')
for _kind in 'fi':
    for _width in (2, 3, 4):
        _name = 'glUniform%d%s' % (_width, _kind)
        globals()[_name] = _vector(_name)
    for _width in (1, 2, 3, 4):
        _name = 'glUniform%d%sv' % (_width, _kind)
        globals()[_name] = _array(_name)
del _kind, _width, _name

if _PASS_THROUGH:
    for _name in [name for name in globals() if name.startswith('gl') and name != 'gl']:
        globals()[_name] = getattr(gl, _name)
    del _name


def __getattr__(name):
    """Everything else of ``OpenGL.GL``: this module can be handed to code
    that takes its GL as an argument (``engine.light_cookies``)."""
    return getattr(gl, name)
