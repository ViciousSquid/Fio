"""Object-graph reachability, for "nothing may still hold X" invariants.

A cache that outlives the thing it caches is invisible to a behavioural test
until something resolves through it. This walks the garbage collector's
referent graph from a set of roots instead, and names the path to every
forbidden object it can reach, so a test can state the invariant directly:
*after Stop, nothing the runtime owns reaches an object of that session*.
"""

import gc
import threading
import types

_OPAQUE = (type, types.ModuleType, types.FrameType, types.CodeType,
           types.BuiltinFunctionType, type(threading.Lock()))


def _referents(obj):
    # A function's globals are the module, not state it owns; its closure
    # cells and defaults are.
    if isinstance(obj, types.FunctionType):
        out = list(obj.__defaults__ or ())
        for cell in obj.__closure__ or ():
            try:
                out.append(cell.cell_contents)
            except ValueError:
                pass
        return out
    if isinstance(obj, types.MethodType):
        return [obj.__self__]
    try:
        return gc.get_referents(obj)
    except Exception:
        return []


def _step(parent, child, path):
    if isinstance(parent, dict):
        for key, value in parent.items():
            if value is child:
                return "%s[%r]" % (path, key)
        return path + "{key}"
    if getattr(parent, "__dict__", None) is child:
        return path
    for klass in type(parent).__mro__:
        for name in getattr(klass, "__slots__", ()) or ():
            if getattr(parent, name, None) is child:
                return "%s.%s" % (path, name)
    return path + "[]"


def paths_to(roots, forbidden, exclude=()):
    """``[path, ...]`` from *roots* (``{name: obj}``) to any object whose
    ``id`` is in *forbidden* (``{id: obj}``), not walking through *exclude*.
    One path per distinct route is reported; the walk does not continue past
    a forbidden object."""
    seen = {id(obj) for obj in exclude}
    frontier = []
    for name, root in roots.items():
        seen.add(id(root))
        frontier.append((root, name))
    found = []
    while frontier:
        following = []
        for obj, path in frontier:
            for child in _referents(obj):
                cid = id(child)
                if cid in seen or isinstance(child, _OPAQUE):
                    continue
                seen.add(cid)
                where = _step(obj, child, path)
                if cid in forbidden:
                    found.append(where)
                else:
                    following.append((child, where))
        frontier = following
    return found
