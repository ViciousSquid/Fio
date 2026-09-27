"""Key collection data is directly editable in the Prop property editor."""

import types

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from editor.property_editor import PropertyEditor
from editor.things import Prop

pytestmark = pytest.mark.qt


@pytest.fixture
def panel(qt_app):
    from PyQt5.QtWidgets import QWidget

    editor = types.SimpleNamespace(
        state=types.SimpleNamespace(things=[], brushes=[]),
        view_3d=QWidget(),
    )
    widget = PropertyEditor(editor)
    yield widget
    widget.deleteLater()


def collect_key_combo(panel, prop):
    panel.current_object = prop
    panel.populate_for_thing(prop)
    assert panel._collect_key_widgets, "the Prop inspector has no key control"
    return panel._collect_key_widgets[0][1]


def test_key_prop_exposes_a_selectable_colour(panel):
    prop = Prop(
        pos=[0, 0, 0],
        properties={"collect_enabled": True, "collect_type": "key"},
    )
    combo = collect_key_combo(panel, prop)

    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()
    assert [combo.itemText(i) for i in range(combo.count())] == list(Prop.KEY_NAMES)
    assert combo.currentText() == Prop.DEFAULT_KEY_NAME

    combo.setCurrentText("red_key")

    assert prop.properties["collect_key_name"] == "red_key"
    assert prop.properties["sprite_path"] == "assets/sprites/redkey.png"


def test_switching_collection_type_to_key_enables_the_colour_selector(panel):
    prop = Prop(
        pos=[0, 0, 0],
        properties={"collect_enabled": True, "collect_type": "health"},
    )
    combo = collect_key_combo(panel, prop)

    assert not combo.isVisibleTo(panel)
    assert not combo.isEnabled()

    panel.on_collect_type_changed("key")

    assert prop.properties["collect_type"] == "key"
    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()

    combo.setCurrentText("yellow_key")
    assert prop.properties["collect_key_name"] == "yellow_key"
