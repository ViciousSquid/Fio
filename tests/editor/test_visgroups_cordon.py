"""Visgroups & Cordon: Hammer's Visgroups and Cordon tool in one window.

User visgroups hide named sets of objects, the Auto tab is the Filter menu,
and the cordon hides everything outside a box -- in every editor view and in
what the logic thread publishes for the renderer, never in Play. Visgroups
and the cordon are saved with the map.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                    # noqa: E402

from editor.things import Light, Monster                       # noqa: E402
from engine import render_table as rt                          # noqa: E402
from engine.view_filters import ViewFilters, brush_bounds      # noqa: E402
from tests.editor.test_view_filters import _level, _published  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing         # noqa: E402

pytestmark = pytest.mark.qt


# -- the model ------------------------------------------------------------------------

def test_a_hidden_visgroup_hides_its_members_by_id():
    crate = box_brush("crate", (0, 0, 0), (64, 64, 64))
    wall = box_brush("wall", (200, 0, 0), (64, 64, 64))
    lamp = make_thing(Light, "lamp", (0, 100, 0))
    filters = ViewFilters()
    group = filters.add_visgroup("props", [crate, lamp])
    assert not filters.active and not filters.hides(crate)
    filters.set_visgroup_visible(group, False)
    assert filters.active
    assert filters.hides(crate) and filters.hides(lamp) and not filters.hides(wall)
    filters.remove_from_visgroup(group, [lamp])
    assert not filters.hides(lamp)
    assert filters.visgroups_of(crate) == [group]
    filters.remove_visgroup(group)
    assert not filters.hides(crate) and not filters.active


def test_the_cordon_hides_what_does_not_reach_into_it():
    inside = box_brush("in", (0, 0, 0), (64, 64, 64))
    straddling = box_brush("edge", (140, 0, 0), (64, 64, 64))    # 108..172 meets 128
    outside = box_brush("out", (400, 0, 0), (64, 64, 64))
    lamp_in = make_thing(Light, "a", (10, 10, 10))
    lamp_out = make_thing(Light, "b", (300, 10, 10))
    filters = ViewFilters()
    filters.set_cordon(enabled=True, lo=(128, 128, 128), hi=(-128, -128, -128))
    assert filters.cordon.lo == [-128, -128, -128]                 # corners put in order
    assert not filters.hides(inside) and not filters.hides(straddling)
    assert filters.hides(outside)
    assert not filters.hides(lamp_in) and filters.hides(lamp_out)
    filters.set_cordon(enabled=False)
    assert not filters.hides(outside)


def test_dense_rows_agree_with_the_object_predicates():
    from engine import entity_table as et
    brushes = [box_brush("b%d" % i, (i * 150.0, 0, 0), (64, 64, 64)) for i in range(5)]
    things = [make_thing(Light, "l%d" % i, (i * 150.0, 0, 0)) for i in range(5)]
    table = rt.RenderTable()
    table.sync(brushes, 1)
    etable = et.EntityTable()
    etable.begin_frame(things, epoch=1)
    filters = ViewFilters()
    group = filters.add_visgroup("g", [brushes[0], things[1]])
    filters.set_visgroup_visible(group, False)
    filters.set_cordon(enabled=True, lo=(-100, -100, -100), hi=(400, 100, 100))
    rows = filters.hidden_brush_rows(table.class_bits[:table.count], table)
    for brush in brushes:
        assert rows[table.slot_of_id[brush["id"]]] == filters.hides_brush(brush), brush["name"]
    rows = filters.hidden_entity_rows(etable)
    for slot, thing in enumerate(etable.things[:etable.count]):
        assert rows[slot] == filters.hides_thing(thing), thing.properties["name"]
    assert rows.tolist() == [False, True, False, True, True]

    things[0].pos = [900.0, 0.0, 0.0]                              # moved out of the cordon
    etable.begin_frame(things, epoch=1)
    assert filters.hidden_entity_rows(etable)[0]


def test_visgroups_and_the_cordon_are_saved_with_the_map(main_window):
    state = main_window.state
    crate = box_brush("crate", (0, 0, 0), (64, 64, 64))
    gone = box_brush("gone", (0, 0, 0), (64, 64, 64))
    state.brushes.extend([crate, gone])
    filters = state.view_filters
    group = filters.add_visgroup("Crates", [crate, gone])
    filters.set_visgroup_visible(group, False)
    filters.set_cordon(enabled=True, lo=(-10, -20, -30), hi=(10, 20, 30))
    state.brushes.remove(gone)

    data = state.get_level_data()
    assert data["visgroups"] == [{"name": "Crates", "visible": False, "ids": [crate["id"]]}]
    assert data["cordon"] == {"enabled": True, "min": [-10, -20, -30], "max": [10, 20, 30]}

    state.clear_scene()
    assert filters.visgroups == [] and not filters.cordon.enabled
    state.load_from_data(data)
    loaded = state.view_filters
    assert [g.name for g in loaded.visgroups] == ["Crates"]
    assert loaded.hides(next(b for b in state.brushes if b["name"] == "crate"))
    assert loaded.cordon.enabled and loaded.cordon.hi == [10, 20, 30]

    state.load_from_data({"brushes": [], "things": []})            # a map from before them
    assert loaded.visgroups == [] and not loaded.cordon.enabled
    assert "visgroups" not in state.get_level_data()


# -- the window -----------------------------------------------------------------------

def test_the_button_above_the_search_opens_and_closes_the_window(main_window):
    hierarchy = main_window.scene_hierarchy
    layout = hierarchy.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    button = hierarchy.visgroups_button
    search_row = hierarchy.search_box.parentWidget()
    assert widgets.index(button) == widgets.index(search_row) - 1
    button.click()
    window = main_window.visgroups_window
    assert window.isVisible()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == \
        ["User", "Auto", "Cordon"]
    button.click()
    assert not window.isVisible()


def test_the_padlock_keeps_the_window_on_top(main_window):
    window = main_window.toggle_visgroups_window()
    assert not window.always_on_top
    window.lock_btn.click()
    assert window.always_on_top and window.isVisible()
    assert main_window.config.getboolean("Visgroups", "always_on_top")
    window.lock_btn.click()
    assert not window.always_on_top
    window.hide()


def _select(main_window, objects):
    main_window.set_selected_objects(list(objects))


def test_user_visgroups_from_the_window(main_window):
    state = main_window.state
    crate = box_brush("crate", (0, 0, 0), (64, 64, 64))
    wall = box_brush("wall", (300, 0, 0), (64, 64, 64))
    grunt = make_thing(Monster, "grunt", (0, 0, 300))
    state.brushes.extend([crate, wall])
    state.things.append(grunt)
    window = main_window.toggle_visgroups_window()

    _select(main_window, [crate])
    window.new_btn.click()
    group = window.current_group()
    assert group.name == "Visgroup 1" and group.ids == {crate["id"]}
    assert window.group_tree.topLevelItem(0).text(1) == "1"

    _select(main_window, [grunt])
    window.add_btn.click()
    assert group.ids == {crate["id"], grunt.properties["id"]}

    item = window.group_tree.topLevelItem(0)
    item.setCheckState(0, Qt.Unchecked)                            # hide it
    view = main_window.view_top
    assert view._filtered(crate) and view._filtered(grunt) and not view._filtered(wall)
    assert main_window.unsaved_changes

    item.setText(0, "Crates")
    assert group.name == "Crates"

    _select(main_window, [])
    window.select_btn.click()
    assert set(map(id, state.selected_objects)) == {id(crate), id(grunt)}

    _select(main_window, [grunt])
    window.remove_btn.click()
    assert group.ids == {crate["id"]}

    window.delete_btn.click()
    assert state.view_filters.visgroups == [] and not view._filtered(crate)
    window.hide()


def test_the_auto_tab_is_the_filter_menu(main_window):
    window = main_window.toggle_visgroups_window()
    water = next(window.auto_list.item(i) for i in range(window.auto_list.count())
                 if window.auto_list.item(i).data(Qt.UserRole) == "water")
    water.setCheckState(Qt.Unchecked)
    assert not main_window.state.view_filters.shows("water")
    assert not main_window.filter_actions["water"].isChecked()
    main_window.set_view_filter("water", True)
    window.refresh()
    assert water.checkState() == Qt.Checked
    window.hide()


def test_cordon_from_the_selection(main_window):
    state = main_window.state
    a = box_brush("a", (0, 0, 0), (64, 64, 64))
    b = box_brush("b", (200, 0, 0), (64, 64, 64))
    far = box_brush("far", (2000, 0, 0), (64, 64, 64))
    state.brushes.extend([a, b, far])
    window = main_window.toggle_visgroups_window()
    _select(main_window, [a, b])
    window.cordon_from_selection()
    cordon = state.view_filters.cordon
    assert cordon.enabled and cordon.lo == [-32, -32, -32] and cordon.hi == [232, 32, 32]
    assert window.cordon_check.isChecked()
    assert [s.value() for s in window.cordon_max] == [232, 32, 32]
    assert main_window.view_top._filtered(far) and not main_window.view_top._filtered(b)

    window.cordon_max[0].setValue(5000)                            # widen it by hand
    assert not main_window.view_top._filtered(far)
    window.cordon_check.setChecked(False)
    assert not cordon.enabled
    window.hide()


# -- publication ----------------------------------------------------------------------

def test_hidden_visgroups_and_the_cordon_are_not_published_while_editing(fio_session):
    session = fio_session(_level())
    window = session.window
    filters = window.state.view_filters
    by_name = {b["name"]: b for b in window.state.brushes}
    grunt = next(t for t in window.state.things if isinstance(t, Monster))

    group = filters.add_visgroup("hide me", [by_name["crate"], grunt])
    filters.set_visgroup_visible(group, False)
    brushes, things = _published(session)
    assert by_name["crate"]["id"] not in brushes and by_name["pool"]["id"] in brushes
    assert grunt.properties["id"] not in things

    filters.set_visgroup_visible(group, True)
    filters.set_cordon(enabled=True, lo=(250, -100, -100), hi=(400, 200, 100))
    brushes, things = _published(session)
    assert brushes == {by_name["pool"]["id"]} | {
        b["id"] for b in window.state.brushes
        if filters.cordon.touches_box(*brush_bounds(b))}
    assert by_name["crate"]["id"] not in brushes

    session.start_play()                                           # Play shows everything
    session.step(1)
    brushes, _things = _published(session)
    assert by_name["crate"]["id"] in brushes
