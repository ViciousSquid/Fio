"""Every global name a module references must actually exist.

Python resolves globals at call time, so a name that was never imported sits
harmlessly in the file until the one branch that uses it runs.  Three of these
shipped: ``brush_geometry`` in the textured-brush path (a ``NameError`` inside
``paintGL()``, which took down the frame), ``label_text`` in the Light
property rows, and ``add_model_action`` in the 2D context menu.  None were
caught by a test, because the render path needs a GL context and the other two
are single branches of long if/elif chains.

This reads each module's symbol table rather than executing it, so it needs no
GL context, no window, and no particular branch to be taken.
"""

import builtins
import importlib
import os
import symtable
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("OpenGL", reason="this module imports Fio's GL-backed modules; importing PyOpenGL needs no GPU, but it does need the package")

# The desktop dependency set (PyOpenGL here) has to be importable; no
# display and no GPU are needed.
pytestmark = pytest.mark.qt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: Modules that only ever run with a live GL context.
RENDER_MODULES = [
    "engine.renderer_core",
    "engine.renderer_F",
    "engine.brush_geometry",
    "engine.render_cull",
]

#: Editor modules whose long if/elif chains hide unreachable-until-clicked
#: branches.  These need Qt to import, so they are skipped where it is absent.
EDITOR_MODULES = [
    "editor.property_editor",
    "editor.view_2d",
    "editor.main_window",
    "editor.surface_inspector",
    "editor.io_system",
    "editor.component_edit",
    "editor.face_texture",
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


def _check(module_name):
    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    if not os.path.exists(path):
        pytest.skip("%s is not present in this checkout" % module_name)

    module = importlib.import_module(module_name)
    missing = unresolved_globals(path, module)

    assert not missing, "\n".join(
        "%s: %s() reads undefined name %r" % (module_name, scope, name)
        for scope, name in missing)


@pytest.mark.parametrize("module_name", RENDER_MODULES)
def test_render_module_names_all_resolve(module_name):
    _check(module_name)


@pytest.mark.parametrize("module_name", EDITOR_MODULES)
def test_editor_module_names_all_resolve(module_name):
    # Only when there is no display: the offscreen plugin cannot create an
    # OpenGL context, and forcing it here would disable the visual tier for
    # the whole session when the suite is run under Xvfb.
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="Qt is not available in this environment")
    _check(module_name)


def test_the_natural_scale_helpers_are_importable():
    """The two names the textured-brush path calls on every natural face."""
    brush_geometry = importlib.import_module("engine.brush_geometry")

    assert callable(brush_geometry.face_uses_natural_scale)
    assert callable(brush_geometry.natural_repeats)


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


def test_detector_notices_a_name_used_before_it_is_assigned(tmp_path):
    """The ``label_text`` shape: assigned in the function, but only later."""
    module = importlib.import_module("engine.renderer_F")
    broken = tmp_path / "late.py"
    broken.write_text(
        "def rows(keys):\n"
        "    for key in keys:\n"
        "        if key == 'show_radius':\n"
        "            yield label_text\n"
        "            continue\n"
        "        label_text = key\n",
        encoding="utf-8")

    # Assigned somewhere in the scope, so this one is a local -- out of reach
    # of a symbol-table check, and why the Light row needed reading instead.
    assert unresolved_globals(str(broken), module) == []


# ────────────────────────────
# The use-before-assignment shape
# ────────────────────────────
#
# A symbol table cannot see ``label_text``: it is assigned somewhere in the
# function, so it is a local, and only the order of statements makes the read
# ahead of the write wrong.  pyflakes does track that, so where it is
# installed it covers the shape the check above cannot.

PYFLAKES_MODULES = RENDER_MODULES + EDITOR_MODULES


def _pyflakes_messages(path):
    from pyflakes import api, reporter

    import io

    out, err = io.StringIO(), io.StringIO()
    api.checkPath(path, reporter.Reporter(out, err))
    return [line for line in out.getvalue().splitlines() if line.strip()]


@pytest.mark.parametrize("module_name", PYFLAKES_MODULES)
def test_no_names_are_used_before_they_are_assigned(module_name):
    pytest.importorskip("pyflakes", reason="pyflakes is not installed")

    path = os.path.join(ROOT, *module_name.split(".")) + ".py"
    if not os.path.exists(path):
        pytest.skip("%s is not present in this checkout" % module_name)

    bad = [m for m in _pyflakes_messages(path)
           if "undefined name" in m or "referenced before assignment" in m]

    assert not bad, "\n".join(bad)


def test_pyflakes_check_notices_a_read_before_the_write(tmp_path):
    """Guards the guard, on the shape the symbol-table check misses."""
    pytest.importorskip("pyflakes", reason="pyflakes is not installed")

    late = tmp_path / "late.py"
    late.write_text(
        "def rows(keys):\n"
        "    for key in keys:\n"
        "        if key == 'show_radius':\n"
        "            yield label_text\n"
        "            continue\n"
        "        label_text = key\n",
        encoding="utf-8")

    assert any("label_text" in m for m in _pyflakes_messages(str(late)))
