"""Every global name the render modules reference must actually exist.

The render path needs a GL context, so none of it runs under the test suite;
a name that does not resolve therefore survives every test and first shows up
as a ``NameError`` in ``paintGL()``, which takes down the frame.  That is how
``brush_geometry.face_uses_natural_scale`` shipped: the module was referenced
by name but only two of its functions were imported.

This reads each module's symbol table instead of executing it, so it needs no
context and no window.
"""

import builtins
import importlib
import os
import symtable
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: The modules that only ever run with a live GL context.
RENDER_MODULES = [
    "engine.renderer_core",
    "engine.renderer_F",
    "engine.brush_geometry",
    "engine.render_cull",
]


def unresolved_globals(path, module):
    """Global names read by ``path`` that ``module`` cannot supply.

    A symbol is reported when some scope reads it as a global, nothing in the
    file ever assigns it, and it is neither an attribute of the imported
    module nor a builtin.
    """
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    found = []

    def walk(table):
        for symbol in table.get_symbols():
            if not symbol.is_global() or symbol.is_assigned():
                continue
            name = symbol.get_name()
            if hasattr(module, name) or hasattr(builtins, name):
                continue
            found.append((table.get_name(), name))
        for child in table.get_children():
            walk(child)

    walk(symtable.symtable(source, path, "exec"))
    return found


@pytest.mark.parametrize("module_name", RENDER_MODULES)
def test_render_module_names_all_resolve(module_name):
    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    if not os.path.exists(path):
        pytest.skip("%s is not present in this checkout" % module_name)

    module = importlib.import_module(module_name)
    missing = unresolved_globals(path, module)

    assert not missing, "\n".join(
        "%s: %s() reads undefined name %r" % (module_name, scope, name)
        for scope, name in missing)


def test_the_natural_scale_helpers_are_importable():
    """The two names the textured-brush path calls on every natural face."""
    renderer_F = importlib.import_module("engine.renderer_F")

    assert callable(renderer_F.face_uses_natural_scale)
    assert callable(renderer_F.natural_repeats)


def test_detector_notices_a_module_referenced_without_importing_it(tmp_path):
    """Guards the guard: the check must fail on the shape of bug it is for."""
    module = importlib.import_module("engine.renderer_F")
    broken = tmp_path / "broken.py"
    broken.write_text(
        "from engine.brush_geometry import brush_has_geometry\n"
        "def draw():\n"
        "    return brush_geometry.natural_repeats(1, 1, (2, 2))\n",
        encoding="utf-8")

    assert ("draw", "brush_geometry") in unresolved_globals(str(broken), module)
