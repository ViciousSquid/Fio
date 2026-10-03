"""The editor side of plugin API 1.5.0, built as real widgets.

``tests/plugins/test_api_1_5_extensions.py`` pins what the plugin manager
records and hands back. This file pins what the editor does with it:

* a property section appears in the Properties tab of its entity type, and its
  factory does not run until the section is opened;
* a LogicState panel lists a plugin's preset keys, and inserting one adds a
  typed designer default (or selects the key if it is already there);
* the Entity Inspector draws a plugin's document, falls back to the entity's
  public properties, refreshes values in place, and stops once the entity has
  left the scene.

Registrations go onto the process-wide manager, because that is what the
editor reads; ``monkeypatch`` restores its lists after every test.
"""

import configparser

import types

import pytest

pytest.importorskip("PyQt5", reason="the property editor is Qt")

from PyQt5.QtWidgets import QProgressBar, QPushButton, QWidget  # noqa: E402

from editor.editor_state import EditorState  # noqa: E402
from editor.entity_inspector import (EntityInspector, generic_document,  # noqa: E402
                                     inspect, normalise_document)
from editor.property_editor import CollapsibleSection, PropertyEditor  # noqa: E402
from editor.things import LogicState, Thing  # noqa: E402
from plugins.api import EditorAPI, FioPlugin  # noqa: E402
from plugins.manager import get_manager  # noqa: E402

pytestmark = pytest.mark.qt


class _FakeView:
    def update(self):
        pass


class FakeHost(QWidget):
    """The slice of MainWindow the property editor talks to."""

    def __init__(self):
        super().__init__()
        self.state = EditorState()
        self.state.selected_objects = []
        self.config = configparser.ConfigParser()
        self.grid_size = 16
        self.view_3d = _FakeView()

    def save_state(self):
        pass

    def update_views(self):
        pass

    def update_all_ui(self):
        pass


@pytest.fixture
def api(monkeypatch):
    """An EditorAPI onto the live manager, with its 1.5 lists isolated."""
    manager = get_manager()
    monkeypatch.setattr(manager, "_property_sections", [])
    monkeypatch.setattr(manager, "_kv_suggestion_providers", [])
    monkeypatch.setattr(manager, "_entity_inspectors", [])
    return EditorAPI(manager, FioPlugin())


@pytest.fixture
def panel(qt_app):
    host = FakeHost()
    return host, PropertyEditor(host)


@pytest.fixture(autouse=True)
def _clean_registry():
    LogicState._persistent_registry.clear()
    yield
    LogicState._persistent_registry.clear()


def _store(store_name="gauges", initial=None):
    return LogicState(pos=[0, 0, 0], properties={
        "store_name": store_name, "initial_data": dict(initial or {})})


def _sections(editor):
    return {s.objectName().split(":", 1)[1]: s
            for s in editor._page.findChildren(CollapsibleSection)
            if s.objectName().startswith("fio_property_section:")}


def _kv_keys(editor):
    table = editor._widgets["kv_table"]
    return [table.item(r, 0).text() for r in range(table.rowCount())]


# ---------------------------------------------------------------------------
# Property sections
# ---------------------------------------------------------------------------

def test_a_section_appears_in_the_properties_tab_of_its_type(api, panel):
    host, editor = panel
    api.register_property_section("Calibration", lambda thing: QWidget(),
                                  entity_type="logic_state")
    editor.set_object(_store())
    sections = _sections(editor)
    assert list(sections) == ["Calibration"]
    props_tab = editor.tab_widget.widget(0)
    assert editor.tab_widget.tabText(0) == "Properties"
    assert props_tab.isAncestorOf(sections["Calibration"])


def test_a_section_for_another_type_does_not_appear(api, panel):
    host, editor = panel
    api.register_property_section("Elsewhere", lambda thing: QWidget(),
                                  entity_type="light")
    editor.set_object(_store())
    assert _sections(editor) == {}


def test_a_collapsed_section_is_built_the_first_time_it_is_opened(api, panel):
    host, editor = panel
    built = []

    def factory(thing):
        built.append(thing)
        return QWidget()

    api.register_property_section("Lazy", factory, entity_type="logic_state")
    store = _store()
    editor.set_object(store)
    section = _sections(editor)["Lazy"]
    assert built == []                       # collapsed: nothing built yet
    section.toggle.setChecked(True)
    assert built == [store]
    section.toggle.setChecked(False)
    section.toggle.setChecked(True)
    assert built == [store]                  # built once, not per opening


def test_an_expanded_section_is_built_straight_away(api, panel):
    host, editor = panel
    built = []
    api.register_property_section("Open", lambda thing: built.append(thing) or QWidget(),
                                  entity_type="logic_state", expanded=True)
    editor.set_object(_store())
    assert len(built) == 1


def test_a_failing_section_factory_does_not_break_the_panel(api, panel):
    host, editor = panel

    def broken(thing):
        raise RuntimeError("section bug")

    api.register_property_section("Broken", broken, entity_type="logic_state",
                                  expanded=True)
    editor.set_object(_store())
    assert "Broken" in _sections(editor)
    assert "kv_table" in editor._widgets


# ---------------------------------------------------------------------------
# LogicState preset keys
# ---------------------------------------------------------------------------

def test_no_preset_picker_without_suggestions(api, panel):
    host, editor = panel
    editor.set_object(_store())
    assert "kv_preset_combo" not in editor._widgets


def test_the_preset_picker_lists_the_providers_keys_for_this_store(api, panel):
    host, editor = panel
    seen = []

    def provider(store):
        seen.append(store)
        return [("Gauge level", "gauge.level", "3", "starting level"),
                ("Gauge armed", "gauge.armed", "true")]

    api.register_kv_suggestions(provider)
    store = _store()
    editor.set_object(store)
    combo = editor._widgets["kv_preset_combo"]
    assert [combo.itemText(i) for i in range(combo.count())] == ["Gauge level", "Gauge armed"]
    assert seen[-1] is store


def test_inserting_a_preset_adds_a_typed_designer_default(api, panel):
    host, editor = panel
    api.register_kv_suggestions(lambda store: [("Gauge level", "gauge.level", "3")])
    store = _store()
    editor.set_object(store)
    editor._widgets["kv_preset_button"].click()
    assert store.properties["initial_data"] == {"gauge.level": 3}
    table = editor._widgets["kv_table"]
    assert table.item(0, 1).text() == "int"


def test_inserting_a_preset_already_present_selects_it_instead(api, panel):
    host, editor = panel
    api.register_kv_suggestions(lambda store: [("Gauge level", "gauge.level", "3")])
    store = _store(initial={"other": 1, "gauge.level": 7})
    editor.set_object(store)
    editor._widgets["kv_preset_button"].click()
    assert store.properties["initial_data"] == {"other": 1, "gauge.level": 7}
    table = editor._widgets["kv_table"]
    assert table.currentRow() == _kv_keys(editor).index("gauge.level")


def test_the_add_key_button_still_invents_an_unused_key(api, panel):
    host, editor = panel
    store = _store(initial={"key1": 1})
    editor.set_object(store)
    buttons = [b for b in editor._page.findChildren(QPushButton)
               if "Add Key" in b.text()]
    assert len(buttons) == 1
    buttons[0].click()
    assert len(store.properties["initial_data"]) == 2
    assert all(v == "value" for k, v in store.properties["initial_data"].items() if k != "key1")


# ---------------------------------------------------------------------------
# Entity Inspector: documents
# ---------------------------------------------------------------------------

def test_the_documented_example_is_a_valid_document():
    example = {"title": "Gate Keeper", "subtitle": "patrolling · awake",
               "sections": [("Vitals", [("Health", 80), ("Speed", 1.5)]),
                            ("Goals", [("Patrol", "", 0.9), ("Rest", "", 0.2)])]}
    assert normalise_document(example) == {
        "title": "Gate Keeper", "subtitle": "patrolling · awake",
        "sections": [("Vitals", [("Health", "80", None), ("Speed", "1.5", None)]),
                     ("Goals", [("Patrol", "", 0.9), ("Rest", "", 0.2)])]}


def test_a_sloppy_document_is_normalised_not_raised():
    doc = normalise_document({"sections": [("Ok", [("a", 1), "junk", ("b", None, "x"),
                                                   ("c", 2, 5.0)]),
                                           "not a section"]})
    assert doc["title"] == "Inspector"
    assert doc["sections"] == [("Ok", [("a", "1", None), ("b", "", None),
                                       ("c", "2", 1.0)])]
    assert normalise_document(None)["sections"] == []


def test_the_fallback_lists_public_properties_and_position():
    thing = Thing(pos=[1, 2, 3], properties={"type": "widget", "name": "W1",
                                             "id": "abc", "speed": 4,
                                             "_private": 1})
    doc = generic_document(thing)
    assert doc["title"] == "W1" and doc["subtitle"] == "widget"
    sections = dict(doc["sections"])
    assert sections["Position"] == [("x", "1.0", None), ("y", "2.0", None),
                                    ("z", "3.0", None)]
    labels = [label for label, _v, _f in sections["Properties"]]
    assert "speed" in labels
    assert not {"name", "type", "id", "_private"} & set(labels)
    assert labels == sorted(labels)


def test_inspect_prefers_a_provider_and_falls_back_without_one(api):
    thing = Thing(pos=[0, 0, 0], properties={"type": "widget", "name": "W1"})
    assert inspect(thing)["title"] == "W1"
    api.register_entity_inspector(lambda e, logic: {"title": "From plugin"})
    assert inspect(thing)["title"] == "From plugin"


# ---------------------------------------------------------------------------
# Entity Inspector: panel
# ---------------------------------------------------------------------------

def test_the_panel_draws_rows_and_bars(api, qt_app):
    thing = Thing(pos=[0, 0, 0], properties={"type": "gauge", "level": 4})
    api.register_entity_inspector(lambda e, logic: {
        "title": "Gauge", "subtitle": "calibrated",
        "sections": [("Readout", [("Level", e.properties["level"]),
                                  ("Fill", "", e.properties["level"] / 10)])]})
    panel = EntityInspector(thing)
    try:
        assert panel.windowTitle() == "Inspector - Gauge"
        assert panel.subtitle_label.text() == "calibrated"
        assert panel.row_values() == [("Readout", "Level", "4"), ("Readout", "Fill", "40%")]
        fill_item = panel.tree.topLevelItem(0).child(1)
        assert isinstance(panel.tree.itemWidget(fill_item, 1), QProgressBar)
    finally:
        panel.deleteLater()


def test_a_refresh_updates_values_in_place(api, qt_app):
    thing = Thing(pos=[0, 0, 0], properties={"type": "gauge", "level": 4})
    api.register_entity_inspector(lambda e, logic: {
        "sections": [("Readout", [("Level", e.properties["level"])])]})
    panel = EntityInspector(thing)
    try:
        item = panel.tree.topLevelItem(0).child(0)
        thing.properties["level"] = 9
        panel.refresh()
        assert panel.tree.topLevelItem(0).child(0) is item
        assert item.text(1) == "9"
    finally:
        panel.deleteLater()


def test_a_refresh_rebuilds_when_the_rows_change(api, qt_app):
    thing = Thing(pos=[0, 0, 0], properties={"type": "gauge", "rows": 1})
    api.register_entity_inspector(lambda e, logic: {
        "sections": [("Rows", [(f"r{i}", i) for i in range(e.properties["rows"])])]})
    panel = EntityInspector(thing)
    try:
        thing.properties["rows"] = 3
        panel.refresh()
        assert [label for _s, label, _v in panel.row_values()] == ["r0", "r1", "r2"]
    finally:
        panel.deleteLater()


def test_the_provider_is_handed_the_live_logic(api, qt_app):
    seen = []
    logic = object()
    api.register_entity_inspector(lambda e, lg: seen.append(lg) or {"title": "x"})
    thing = Thing(pos=[0, 0, 0], properties={"type": "t"})
    panel = EntityInspector(thing, logic=lambda: logic)
    try:
        assert seen and seen[-1] is logic
    finally:
        panel.deleteLater()


def test_the_panel_stops_once_the_entity_leaves_the_scene(api, qt_app):
    calls = []
    api.register_entity_inspector(lambda e, logic: calls.append(1) or {"title": "x"})
    present = {"yes": True}
    thing = Thing(pos=[0, 0, 0], properties={"type": "t"})
    panel = EntityInspector(thing, alive=lambda: present["yes"])
    try:
        panel.show()
        assert panel._timer.isActive()
        present["yes"] = False
        before = len(calls)
        panel.refresh()
        assert len(calls) == before
        assert not panel._timer.isActive()
        assert "no longer in the scene" in panel.subtitle_label.text()
    finally:
        panel.close()


def test_the_panel_does_not_keep_a_deleted_entity_alive(api, qt_app):
    """The panel holds the entity weakly: an entity the scene dropped is gone."""
    import gc
    thing = Thing(pos=[0, 0, 0], properties={"type": "t"})
    panel = EntityInspector(thing)
    try:
        del thing
        gc.collect()
        assert panel.entity is None
        panel.refresh()
        assert "no longer in the scene" in panel.subtitle_label.text()
    finally:
        panel.deleteLater()


# ---------------------------------------------------------------------------
# MainWindow.show_entity_inspector
# ---------------------------------------------------------------------------

@pytest.fixture
def inspector_host(qt_app):
    from editor.main_window import MainWindow

    class InspectorHost(QWidget):
        show_entity_inspector = MainWindow.show_entity_inspector

        def __init__(self):
            super().__init__()
            self.state = EditorState()
            self._entity_inspectors = {}
            self.view_3d = None

    host = InspectorHost()
    yield host
    for panel in list(host._entity_inspectors.values()):
        panel.close()
    host.deleteLater()


def test_one_inspector_per_entity(api, inspector_host):
    a = Thing(pos=[0, 0, 0], properties={"type": "t", "name": "a"})
    b = Thing(pos=[0, 0, 0], properties={"type": "t", "name": "b"})
    inspector_host.state.things.extend([a, b])
    first = inspector_host.show_entity_inspector(a)
    assert inspector_host.show_entity_inspector(a) is first
    other = inspector_host.show_entity_inspector(b)
    assert other is not first
    assert first.entity is a and other.entity is b


def test_a_closed_inspector_is_forgotten(api, inspector_host, qt_app):
    a = Thing(pos=[0, 0, 0], properties={"type": "t", "name": "a"})
    inspector_host.state.things.append(a)
    first = inspector_host.show_entity_inspector(a)
    first.close()
    qt_app.sendPostedEvents(None, 0)       # run the deferred delete
    from PyQt5.QtCore import QEvent
    qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
    assert id(a) not in inspector_host._entity_inspectors
    assert inspector_host.show_entity_inspector(a) is not first


def test_the_inspector_follows_the_scene(api, inspector_host):
    a = Thing(pos=[0, 0, 0], properties={"type": "t", "name": "a"})
    inspector_host.state.things.append(a)
    panel = inspector_host.show_entity_inspector(a)
    inspector_host.state.things.remove(a)
    panel.refresh()
    assert "no longer in the scene" in panel.subtitle_label.text()


@pytest.mark.parametrize("play_mode", [True, False])
def test_the_inspector_hands_providers_logic_only_in_play_mode(api, inspector_host, play_mode):
    """The logic thread lives for the whole editor session; providers are
    promised it only while Play Mode runs, and None otherwise."""
    seen = []
    api.register_entity_inspector(lambda e, logic: seen.append(logic) or {"title": "x"})

    class View:
        logic_thread = types.SimpleNamespace(play_mode=play_mode)

    inspector_host.view_3d = View()
    a = Thing(pos=[0, 0, 0], properties={"type": "t"})
    inspector_host.state.things.append(a)
    inspector_host.show_entity_inspector(a)
    assert seen[-1] is (View.logic_thread if play_mode else None)


def test_no_entity_opens_nothing(inspector_host):
    assert inspector_host.show_entity_inspector(None) is None
    assert inspector_host._entity_inspectors == {}


def test_the_hierarchy_offers_inspect_just_above_properties_for_entities():
    """Read as source, like the Properties placement test beside it: the menu
    is built and consumed inside a blocking ``menu.exec_()``."""
    import re
    source = open("editor/scene_hierarchy.py", encoding="utf-8").read()
    start = source.index("if data and data[0] == 'thing':")
    end = source.index("menu.exec_", start)
    entries = re.findall(r"menu\.add(?:Action|Menu)\(\s*\"([^\"]+)\"", source[start:end])
    assert entries[-2:] == ["Inspect", "Properties"]
    handled = source[end:source.index("elif data", end) if "elif data" in source[end:] else None]
    assert "show_entity_inspector(thing_obj)" in handled
