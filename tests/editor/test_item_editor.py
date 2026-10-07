"""The Custom Items editor, the Prop inspector's Item list, and item persistence.

Custom 1 and Custom 2 are configured in Tools > Custom Items...; a Prop that
gives one references it by id, so applying a definition re-syncs every such
Prop, the change is one undo step, and the definitions save with the map.
"""

import copy
import json

import pytest

pytest.importorskip("PyQt5", reason="the item editor is editor-tier")

from PyQt5.QtCore import QEvent, Qt                       # noqa: E402
from PyQt5.QtGui import QKeyEvent                         # noqa: E402
from PyQt5.QtWidgets import QMessageBox                   # noqa: E402

from editor.console_commands import ConsoleCommandHandler  # noqa: E402
from editor.item_editor import ItemEditorDialog, slot_title  # noqa: E402
from editor.property_editor import PropertyEditor, item_choices  # noqa: E402
from editor.things import Prop                            # noqa: E402
from engine.items import DEFAULT_DEFINITIONS, ItemDefinitions  # noqa: E402

pytestmark = pytest.mark.qt


def _item_prop(item_id="custom1"):
    return Prop(pos=[0, 0, 0], properties={
        "collect_enabled": True, "collect_type": "item", "collect_item": item_id})


@pytest.fixture
def dialog(main_window):
    dialog = ItemEditorDialog(main_window)
    yield dialog
    dialog.deleteLater()


# -- the dialog ------------------------------------------------------------------

def test_tabs_name_the_slot_and_the_item(dialog):
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Custom 1 — Cigarette", "Custom 2 — Wine Glass"]
    assert slot_title("custom2", "Goblet") == "Custom 2 — Goblet"


def test_a_tab_title_follows_the_name_as_it_is_typed(dialog):
    dialog.pages["custom1"].name.setText("Cigar")
    assert dialog.tabs.tabText(0) == "Custom 1 — Cigar"


def test_pages_show_the_shipped_definitions(dialog):
    for item_id, page in dialog.pages.items():
        assert page.definition() == DEFAULT_DEFINITIONS[item_id]


def test_the_form_shows_only_what_the_kind_and_effect_use(dialog, qt_app):
    dialog.show()
    page = dialog.pages["custom1"]
    assert page.weapon_box.isVisible() and not page.pickup_box.isVisible()
    assert not page.damage.isEnabled()          # mode none: never fires

    page.mode.set_value("hitscan")
    assert page.damage.isEnabled()
    assert not page.projectile_speed.isEnabled()
    page.mode.set_value("projectile")
    assert page.projectile_speed.isEnabled()

    page.kind.set_value("pickup")
    page.effect.set_value("key")
    qt_app.processEvents()
    assert page.pickup_box.isVisible() and not page.weapon_box.isVisible()
    assert page.key_name.isVisible()
    assert not page.amount.isVisible() and not page.item_id_choice.isVisible()
    page.effect.set_value("armor")
    assert page.amount.isVisible() and not page.key_name.isVisible()


def test_apply_redefines_the_slot_and_resyncs_its_props(main_window, dialog):
    state = main_window.state
    prop, other = _item_prop("custom1"), _item_prop("gun1")
    state.things = [prop, other]
    state.sync_item_props()
    assert prop.properties["sprite_path"] == "assets/sprites/custom1.png"

    page = dialog.pages["custom1"]
    page.name.setText("Ember")
    page.world_sprite.set_value("assets/sprites/gun2.png")
    page.mode.set_value("projectile")
    page.damage.setValue(40)
    assert dialog.apply() is True

    assert state.item_definitions.name("custom1") == "Ember"
    item = state.item_definitions.registry().resolve("custom1")
    assert (item.weapon.mode, item.weapon.damage) == ("projectile", 40.0)
    assert prop.properties["sprite_path"] == "assets/sprites/gun2.png"
    assert prop.properties["collect_item"] == "custom1"
    assert other.properties["sprite_path"] == "assets/sprites/gun1.png"
    assert main_window.unsaved_changes


def test_applying_is_one_undo_step(main_window, dialog):
    state = main_window.state
    state.things = [_item_prop("custom2")]
    state.sync_item_props()
    dialog.pages["custom2"].name.setText("Goblet")
    dialog.pages["custom2"].world_sprite.set_value("assets/sprites/gun1.png")
    dialog.apply()
    assert state.item_definitions.name("custom2") == "Goblet"

    main_window.undo()

    assert state.item_definitions.name("custom2") == "Wine Glass"
    assert state.item_definitions.to_level_data() == {}
    assert state.things[0].properties["sprite_path"] == "assets/sprites/custom2.png"

    main_window.redo()
    assert state.item_definitions.name("custom2") == "Goblet"
    assert state.things[0].properties["sprite_path"] == "assets/sprites/gun1.png"


def test_applying_nothing_changed_records_no_undo_step(main_window, dialog):
    depth = len(main_window.state.undo_stack)
    assert dialog.apply() is True
    assert len(main_window.state.undo_stack) == depth


def test_an_invalid_page_is_refused_and_nothing_changes(main_window, dialog, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *args: warnings.append(args[2])))
    dialog.pages["custom1"].name.setText("Cigar")
    dialog.pages["custom2"].name.setText("")

    assert dialog.apply() is False

    assert warnings and "name" in warnings[0]
    assert dialog.tabs.currentWidget() is dialog.pages["custom2"]
    assert main_window.state.item_definitions.name("custom1") == "Cigarette"


def test_a_partial_definition_from_a_map_opens_complete(main_window):
    main_window.state.item_definitions.load({"custom1": {
        "name": "Dart", "weapon": {"mode": "projectile", "damage": 12}}})
    dialog = ItemEditorDialog(main_window)
    try:
        page = dialog.pages["custom1"]
        definition = page.definition()
        assert definition["name"] == "Dart"
        assert (definition["weapon"]["mode"], definition["weapon"]["damage"]) == (
            "projectile", 12)
        assert definition["weapon"]["range"] == 10000.0
        assert definition["world_sprite"] == ""
        # Opening and applying unchanged writes nothing.
        depth = len(main_window.state.undo_stack)
        assert dialog.apply() is True
        assert len(main_window.state.undo_stack) == depth
    finally:
        dialog.deleteLater()


def test_an_invalid_definition_from_a_map_is_shown_as_the_default(main_window):
    main_window.state.item_definitions.load({"custom2": {"name": "Bad", "kind": "spell"}})
    dialog = ItemEditorDialog(main_window)
    try:
        page = dialog.pages["custom2"]
        assert page.definition() == DEFAULT_DEFINITIONS["custom2"]
        assert dialog.apply() is True
        definitions = main_window.state.item_definitions
        assert definitions.registry().resolve("custom2").name == "Wine Glass"
        assert "custom2" not in definitions.registry().errors
    finally:
        dialog.deleteLater()


def test_reset_to_default_restores_the_shipped_page(dialog):
    page = dialog.pages["custom2"]
    page.name.setText("Goblet")
    page.kind.set_value("pickup")
    dialog.tabs.setCurrentWidget(page)
    dialog._reset_current()
    assert page.definition() == DEFAULT_DEFINITIONS["custom2"]


def test_the_tools_menu_opens_the_editor(main_window):
    assert main_window.item_editor_action.text() == "Custom Items…"


# -- the Prop inspector --------------------------------------------------------------

def test_the_item_list_names_items_by_their_definition():
    definitions = ItemDefinitions()
    assert item_choices(definitions) == [
        ("Pistol", "gun1"), ("Shotgun", "gun2"),
        ("Cigarette", "custom1"), ("Wine Glass", "custom2")]


def test_duplicate_names_are_told_apart_by_id():
    definitions = ItemDefinitions()
    definition = definitions.definition("custom1")
    definition["name"] = "Pistol"
    definitions.set_custom("custom1", definition)
    labels = [label for label, _ in item_choices(definitions)]
    assert labels == ["Pistol (gun1)", "Shotgun", "Pistol (custom1)", "Wine Glass"]


def test_the_prop_inspector_shows_a_renamed_item(main_window, dialog):
    dialog.pages["custom1"].name.setText("Cigar")
    dialog.apply()
    panel = PropertyEditor(main_window)
    try:
        prop = _item_prop("custom1")
        panel.current_object = prop
        panel.populate_for_thing(prop)
        assert panel._prop_item_combo.currentText() == "Cigar"
    finally:
        panel.deleteLater()


# -- persistence -----------------------------------------------------------------

def test_definitions_and_item_props_round_trip_through_the_map(main_window):
    state = main_window.state
    definition = copy.deepcopy(DEFAULT_DEFINITIONS["custom1"])
    definition.update(name="Vest", kind="pickup", world_sprite="assets/sprites/gun2.png")
    definition["pickup"].update(effect="armor", amount=50)
    state.things = [_item_prop("custom1")]
    state.set_item_definition("custom1", definition)

    data = json.loads(json.dumps(state.get_level_data()))
    assert data["items"] == {"custom1": definition}
    saved_prop = data["things"][0]["properties"]
    assert saved_prop["collect_item"] == "custom1"
    assert "collect_weapon" not in saved_prop

    state.clear_scene()
    assert state.item_definitions.name("custom1") == "Cigarette"
    state.load_from_data(data)

    assert state.item_definitions.definition("custom1") == definition
    assert state.things[0].properties["sprite_path"] == "assets/sprites/gun2.png"
    assert state.item_definitions.registry().resolve("custom1").pickup.amount == 50


def test_a_map_with_default_items_saves_no_items_block(main_window):
    assert "items" not in main_window.state.get_level_data()


def test_a_map_written_before_items_were_data_loads_as_items(main_window):
    state = main_window.state
    state.load_from_data({"version": 3, "brushes": [], "things": [{
        "type": "prop", "pos": [0, 0, 0], "properties": {
            "type": "prop", "collect_enabled": True,
            "collect_type": "weapon", "collect_weapon": "gun2"}}]})
    props = state.things[0].properties
    assert (props["collect_type"], props["collect_item"]) == ("item", "gun2")
    assert "collect_weapon" not in props
    assert props["sprite_path"] == "assets/sprites/gun2.png"


# -- console and keys -----------------------------------------------------------------

def test_hudtext_is_an_alias_of_hudstyle(main_window):
    if main_window.config.has_section("Display"):
        main_window.config.remove_option("Display", "hudstyle")
    handler = ConsoleCommandHandler(main_window)
    handler.handle_command("hudtext 2")
    assert main_window.view_3d._hud_style == 2
    handler.handle_command("hudtext 3")
    assert main_window.view_3d._hud_style == 3


def test_spawn_prop_by_item_id_makes_an_item_pickup(main_window):
    handler = ConsoleCommandHandler(main_window)
    before = len(main_window.state.things)
    handler.handle_command("spawn prop custom2")
    assert len(main_window.state.things) == before + 1
    props = main_window.state.things[-1].properties
    assert (props["collect_type"], props["collect_item"]) == ("item", "custom2")
    assert props["sprite_path"] == "assets/sprites/custom2.png"


@pytest.mark.parametrize("key, slot", [(Qt.Key_1, 1), (Qt.Key_2, 2),
                                       (Qt.Key_3, 3), (Qt.Key_4, 4)])
def test_number_keys_queue_weapon_slots_in_play(main_window, key, slot):
    view = main_window.view_3d
    view.game_state.consume_weapon_slots()
    view.play_mode = True
    try:
        view.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        assert view.game_state.consume_weapon_slots() == [slot]
    finally:
        view.play_mode = False


def test_number_keys_queue_nothing_in_the_editor(main_window):
    view = main_window.view_3d
    view.game_state.consume_weapon_slots()
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_1, Qt.NoModifier))
    assert view.game_state.consume_weapon_slots() == []


def test_a_prop_naming_an_unknown_item_opens_in_the_inspector(main_window):
    panel = PropertyEditor(main_window)
    try:
        prop = _item_prop("gun9")
        sprite = prop.properties["sprite_path"]
        panel.current_object = prop
        panel.populate_for_thing(prop)
        panel.on_collect_type_changed("item")
        assert prop.properties["collect_item"] == "gun9"
        assert prop.properties["sprite_path"] == sprite
    finally:
        panel.deleteLater()
