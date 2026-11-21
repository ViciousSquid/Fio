"""A selection change costs the hierarchy what changed, not the whole map
(audit P16).

Every selection change built a key from every object's name and flags and
asked every row whether it was selected: 75 of the 92 ms a selection change
took on a 20k-brush map -- even with the dock closed. Now:

* the objects' part of the key is reused until an editor transaction moves
  the world epoch (or the I/O revision, or the lists);
* the rows found through an object -> row map, and only the rows whose
  selection changed are touched;
* nothing at all is done while the panel is out of sight, and it catches up
  when shown.

These count the work; equality with a from-scratch rebuild is held by
test_hierarchy_refresh.py.
"""

import pytest
from PyQt5.QtCore import Qt

import editor.scene_hierarchy as scene_hierarchy
from tests.helpers.hierarchy_reference import reference_refresh_list

pytestmark = pytest.mark.qt


@pytest.fixture
def scene(main_window, monkeypatch):
    logic = main_window.view_3d.logic_thread
    logic.stop()
    if logic.is_alive():
        logic.join(timeout=1)
    state = main_window.state
    state.brushes = [{'id': 'b%d' % i, 'name': 'brush%d' % i} for i in range(200)]
    main_window.update_all_ui()
    return main_window


def _count(monkeypatch, owner, name):
    calls = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, counted)
    return calls


def _selected_rows(hierarchy):
    return sorted(item.data(0, Qt.UserRole) for item in hierarchy.tree.selectedItems())


def test_selection_changes_reread_no_object_and_ask_no_row(scene, monkeypatch):
    hierarchy = scene.scene_hierarchy
    brushes = scene.state.brushes
    scene.set_selected_objects([brushes[0]])
    lookups = _count(monkeypatch, scene_hierarchy.SelectionLookup, '__contains__')
    rebuilds = _count(monkeypatch, hierarchy, '_rebuild_items')
    key_builds = []
    real_objects_key = hierarchy._objects_key

    def objects_key(reuse=False):
        token = hierarchy._world_token()
        cached = hierarchy._objects_key_cache
        if not (reuse and cached is not None and hierarchy._same_token(cached[0], token)):
            key_builds.append(True)
        return real_objects_key(reuse)
    monkeypatch.setattr(hierarchy, '_objects_key', objects_key)

    for i in range(1, 40):
        scene.set_selected_objects([brushes[i], brushes[i + 100]])
        assert _selected_rows(hierarchy) == [('brush', i), ('brush', i + 100)]
    assert key_builds == [], "a selection change re-read every object"
    assert lookups == [], "a selection change asked every row"
    assert rebuilds == []


def test_an_edit_is_still_seen_after_selection_only_refreshes(scene):
    hierarchy = scene.scene_hierarchy
    brushes = scene.state.brushes
    scene.set_selected_objects([brushes[0]])
    scene.save_state()                                  # an editor operation
    brushes[5]['name'] = 'renamed'
    scene.set_selected_objects([brushes[5]])
    row = hierarchy._find_item(brushes[5])
    assert 'renamed' in row.text(0)
    assert row.isSelected()


def test_the_fast_path_selects_what_a_rebuild_would(scene):
    hierarchy = scene.scene_hierarchy
    brushes = scene.state.brushes
    for selected in ([brushes[3]], brushes[10:20], [], [dict(brushes[4])]):
        scene.set_selected_objects(selected)
        fast = _selected_rows(hierarchy)
        reference_refresh_list(hierarchy)
        assert fast == _selected_rows(hierarchy), selected
        hierarchy.refresh_list()


def test_a_hidden_panel_does_no_work_and_catches_up_when_shown(scene, monkeypatch, qt_app):
    hierarchy = scene.scene_hierarchy
    brushes = scene.state.brushes
    dock = scene.scene_hierarchy_dock
    dock.hide()
    qt_app.processEvents()
    keys = _count(monkeypatch, hierarchy, '_structure_key')
    scene.set_selected_objects([brushes[7]])
    scene.save_state()
    brushes[7]['name'] = 'seen later'
    scene.update_all_ui()
    assert keys == [], "the closed hierarchy was refreshed"
    dock.show()
    qt_app.processEvents()
    row = hierarchy._find_item(brushes[7])
    assert row is not None and 'seen later' in row.text(0)
    assert row.isSelected()
