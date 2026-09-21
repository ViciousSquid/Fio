"""The editor patches plugins install, asserted rather than assumed.

``plugins.integration`` extends the editor by wrapping methods on its classes.
Every one of those wraps is installed by a ``_patch_*`` function that ends with
an assignment back onto the class, and nothing downstream fails loudly when an
assignment does not happen — a plugin schema that never renders just looks like
a plugin that declared nothing.

That is exactly what had happened to ``_patch_property_editor``: the whole body
after ``_orig_iterate = ...`` had fallen out of the function, so the wrappers
were defined and then dropped on the floor.  ``register_properties`` schemas
fell back to type-guessed widgets and ``register_property_tab`` did nothing at
all, in every build, silently.

These tests pin the installation itself.
"""

import pytest

pytest.importorskip("PyQt5", reason="the editor patches are Qt-side")

from editor.property_editor import PropertyEditor      # noqa: E402
import plugins.integration as integration              # noqa: E402

pytestmark = pytest.mark.qt


#: Every class attribute a ``_patch_*`` function is supposed to replace, and
#: the patcher that owns it.  A new wrap belongs in this table.
PATCHED_METHODS = [
    (PropertyEditor, "_iterate_thing_properties", "_patch_property_editor"),
    (PropertyEditor, "populate_for_thing", "_patch_property_editor"),
]


@pytest.mark.parametrize("cls, method, patcher", PATCHED_METHODS)
def test_the_patch_is_actually_installed(cls, method, patcher):
    """The live attribute must be the patcher's closure, not the original.

    ``__qualname__`` is the check that cannot be faked by a same-named
    function: a wrapper defined inside ``_patch_x`` carries
    ``_patch_x.<locals>.<name>``, while the untouched editor method carries
    ``<Class>.<name>``.
    """
    attr = cls.__dict__.get(method)
    assert attr is not None, "%s.%s does not exist" % (cls.__name__, method)
    assert attr.__qualname__.startswith("%s.<locals>." % patcher), (
        "%s.%s is %r — %s defined a wrapper but never assigned it back onto "
        "the class" % (cls.__name__, method, attr.__qualname__, patcher)
    )


def test_every_patcher_marks_its_class_as_patched():
    """The idempotence guard doubles as proof the patcher ran to the end."""
    assert getattr(PropertyEditor, "_fio_plugins_patched", False) is True


def test_patching_twice_is_a_no_op():
    installed = PropertyEditor.__dict__.get("populate_for_thing")
    integration._patch_property_editor()
    assert PropertyEditor.__dict__.get("populate_for_thing") is installed, (
        "a second patch re-wrapped an already-wrapped method, which stacks a "
        "new closure over the old one on every call"
    )


def test_no_patcher_body_escaped_its_function():
    """Source-level guard for the defect class, not just this instance.

    A ``_patch_*`` helper works by closing over names it imported locally
    (``get_manager``, the editor class, the ``_orig_*`` it wraps).  A nested
    ``def`` that has slipped back to column 0 still parses, still imports, and
    still does nothing — so check the shape directly.
    """
    import ast
    import inspect

    source = inspect.getsource(integration)
    tree = ast.parse(source)
    patchers = {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name.startswith("_patch_")}
    assert patchers, "no _patch_* functions found; this test is looking in the wrong place"

    module_level = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    stranded = module_level & {
        "_iterate_thing_properties", "populate_for_thing", "load_from_data",
    }
    assert not stranded, (
        "these wrapper functions are defined at module level instead of inside "
        "their _patch_* function, so they are never installed: %s" % sorted(stranded)
    )
