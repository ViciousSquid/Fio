"""Key collection data is directly editable in the Prop property editor."""

import os

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from editor.main_window import MainWindow
from editor.property_editor import PropertyEditor
from editor.things import Prop

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

pytestmark = pytest.mark.qt


@pytest.fixture
def panel(qt_app):
    editor = MainWindow(ROOT)
    widget = PropertyEditor(editor)
    yield widget
    widget.deleteLater()
    editor.unsaved_changes = False
    editor.unsaved_changes = False
    editor.close()
    editor.deleteLater()
    qt_app.processEvents()


def collect_key_combo(panel, prop):
    panel.current_object = prop
    panel.populate_for_thing(prop)
    assert panel._prop_key_combo is not None, "the Prop inspector has no key control"
    return panel._prop_key_combo


def test_key_prop_exposes_a_selectable_colour(panel):
    prop = Prop(
        pos=[0, 0, 0],
        properties={"collect_enabled": True, "collect_type": "key"},
    )
    combo = collect_key_combo(panel, prop)

    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Blue Key", "Red Key", "Yellow Key"
    ]
    assert combo.currentText() == "Blue Key"

    combo.setCurrentText("Red Key")

    assert prop.properties["collect_key_name"] == "red_key"
    assert prop.properties["sprite_path"] == "assets/sprites/redkey.png"


def test_switching_collection_type_to_key_enables_the_colour_selector(panel):
    prop = Prop(
        pos=[0, 0, 0],
        properties={"collect_enabled": True, "collect_type": "health"},
    )
    combo = collect_key_combo(panel, prop)

    assert not combo.isVisibleTo(panel)
    assert combo.isEnabled()

    panel.on_collect_type_changed("key")

    assert prop.properties["collect_type"] == "key"
    assert combo.isVisibleTo(panel)
    assert combo.isEnabled()

    combo.setCurrentText("Yellow Key")
    assert prop.properties["collect_key_name"] == "yellow_key"


def test_enabling_collectible_defaults_to_the_pistol_item(panel):
    prop = Prop(pos=[0, 0, 0])
    panel.current_object = prop
    panel.populate_for_thing(prop)

    panel.on_prop_collectible_toggled(True)

    assert prop.properties["collect_enabled"] is True
    assert prop.properties["collect_type"] == "item"
    assert prop.properties["collect_item"] == "gun1"
    assert prop.properties["sprite_path"] == "assets/sprites/gun1.png"
    assert panel._prop_collect_type_combo.currentText() == "Item"


@pytest.mark.parametrize("item_id, label", [
    ("gun1", "Pistol"), ("gun2", "Shotgun"),
    ("custom1", "Cigarette"), ("custom2", "Wine Glass"),
])
def test_choosing_an_item_keeps_it_selected(panel, item_id, label):
    """Picking an item stores its id and the combo keeps showing its name.

    The refresh after a change used its own label table, which lacked Custom 2:
    choosing it stored ``custom2`` and then flipped the combo back to gun1.
    """
    weapon = item_id
    prop = Prop(
        pos=[0, 0, 0],
        properties={"collect_enabled": True, "collect_type": "item",
                    "collect_item": "gun1" if item_id != "gun1" else "gun2"},
    )
    panel.current_object = prop
    panel.populate_for_thing(prop)
    combo = panel._prop_item_combo

    combo.setCurrentText(label)

    assert prop.properties["collect_item"] == item_id
    assert prop.properties["sprite_path"] == f"assets/sprites/{weapon}.png"
    assert combo.currentText() == label
