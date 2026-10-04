"""Plugin editor extensions execute on their owning editor classes.

The 2.6 editor has no runtime monkey-patch layer. These tests pin that the
plugin-facing entry points are the concrete implementations on EditorState,
View2D, Ui_MainWindow, and PropertyEditor.
"""

import pytest

pytest.importorskip("PyQt5", reason="the editor extensions are Qt-side")

from editor.editor_state import EditorState                  # noqa: E402
from editor.property_editor import PropertyEditor            # noqa: E402
from editor.ui import Ui_MainWindow                           # noqa: E402
from editor.view_2d import View2D                             # noqa: E402

pytestmark = pytest.mark.qt


@pytest.mark.parametrize(
    "cls, method",
    [
        (EditorState, "load_from_data"),
        (EditorState, "clear_scene"),
        (View2D, "contextMenuEvent"),
        (Ui_MainWindow, "create_menu_bar"),
        (PropertyEditor, "populate_for_thing"),
        (PropertyEditor, "_create_thing_properties_tab"),
        (PropertyEditor, "_iterate_thing_properties"),
    ],
)
def test_plugin_editor_entry_points_are_native_methods(cls, method):
    attr = cls.__dict__.get(method)
    assert attr is not None
    assert attr.__qualname__ == f"{cls.__name__}.{method}"


@pytest.mark.parametrize(
    "cls",
    [EditorState, View2D, Ui_MainWindow, PropertyEditor],
)
def test_no_legacy_monkey_patch_marker_exists(cls):
    assert not hasattr(cls, "_fio_plugins_patched")


def test_plugin_menu_is_built_by_the_ui_owner():
    from editor.ui import _build_plugins_menu
    assert _build_plugins_menu.__module__ == "editor.ui"
