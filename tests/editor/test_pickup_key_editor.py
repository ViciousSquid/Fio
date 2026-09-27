"""Pickup key colour is directly editable in the property editor."""

import types

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from editor.property_editor import PropertyEditor
from editor.things import Pickup

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


def pickup_key_combo(panel, pickup):
    panel.current_object = pickup
    panel.populate_for_thing(pickup)
    assert panel._pickup_key_widgets, "the Pickup inspector has no key control"
    return panel._pickup_key_widgets[0][1]


def test_key_pickup_exposes_a_selectable_colour(panel):
    pickup = Pickup(pos=[0, 0, 0], properties={"item_type": "key"})
    combo = pickup_key_combo(panel, pickup)

    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()
    assert [combo.itemText(i) for i in range(combo.count())] == list(Pickup.KEY_NAMES)
    assert combo.currentText() == Pickup.DEFAULT_KEY_NAME

    combo.setCurrentText("red_key")

    assert pickup.properties["key_name"] == "red_key"


def test_switching_item_type_to_key_enables_the_colour_selector(panel):
    pickup = Pickup(pos=[0, 0, 0], properties={"item_type": "health"})
    combo = pickup_key_combo(panel, pickup)

    assert not combo.isVisibleTo(panel)
    assert not combo.isEnabled()

    panel.on_pickup_item_type_changed("key")

    assert pickup.properties["item_type"] == "key"
    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()

    combo.setCurrentText("yellow_key")
    assert pickup.properties["key_name"] == "yellow_key"
