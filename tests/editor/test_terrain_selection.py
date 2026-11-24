"""The terrain is selected and locked as a brush is.

Shift+click on it in the 3D view (when it is the nearest thing under the
cursor), or its row in the Scene Hierarchy, selects it in place of any
objects; the property editor then shows its lock and an EDIT button that
opens the Terrain Editor. Locked, it is skipped by the 3D pick and its editor
does not open -- and the lock is saved with the map.
"""
import pytest
from PyQt5.QtWidgets import QCheckBox, QPushButton

pytestmark = pytest.mark.qt


@pytest.fixture
def window(main_window):
    from engine.terrain import Terrain

    main_window.terrain = Terrain(seed=42)
    main_window.state.terrain_data = main_window.terrain.to_dict()
    toasts = []
    main_window.show_toast = lambda message, is_error=False, **_kw: toasts.append(
        (str(message), is_error))
    main_window.toasts = toasts
    yield main_window
    main_window.unsaved_changes = False


def _panel(window):
    widgets = window.property_editor._widgets
    return widgets.get('terrain_lock_cb'), widgets.get('terrain_edit_btn')


def test_selecting_the_terrain_shows_its_lock_and_edit_button(window):
    assert window.select_terrain()

    lock, edit = _panel(window)
    assert window.terrain_selected and window.state.selected_objects == []
    assert isinstance(lock, QCheckBox) and not lock.isChecked()
    assert isinstance(edit, QPushButton) and edit.text() == "EDIT" and edit.isEnabled()


def test_only_the_terrain_gets_the_edit_button(window):
    brush = {'id': 'b', 'name': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    window.state.brushes.append(brush)

    window.set_selected_objects([brush])

    assert not window.terrain_selected
    assert window.property_editor._widgets.get('terrain_edit_btn') is None


def test_the_edit_button_opens_the_terrain_editor(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, '_show_terrain_editor_panel', lambda: opened.append(True))
    window.select_terrain()

    _panel(window)[1].click()

    assert opened == [True], "EDIT did not open the Terrain Editor the normal way"


def test_selecting_an_object_deselects_the_terrain(window):
    brush = {'id': 'b', 'name': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    window.state.brushes.append(brush)
    window.select_terrain()

    window.set_selected_objects([brush])

    assert not window.terrain_selected
    assert window.property_editor.current_object is brush


def test_a_map_with_no_terrain_has_nothing_to_select(main_window):
    main_window.state.terrain_data = None

    assert not main_window.select_terrain()
    assert not main_window.terrain_selected


def test_a_locked_terrain_is_not_edited_and_is_skipped_by_the_3d_pick(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, '_show_terrain_editor_panel', lambda: opened.append(True))
    monkeypatch.setattr(window.view_3d.terrain_edit, 'raycast_terrain',
                        lambda mx, my: (0.0, 0.0, -100.0))
    assert window.view_3d.picking.terrain_pick_distance(10, 10) is not None

    window.select_terrain()
    _panel(window)[0].setChecked(True)

    assert window.terrain_locked() and window.terrain.locked
    assert window.unsaved_changes, "locking the terrain is an edit of the map"
    assert window.view_3d.picking.terrain_pick_distance(10, 10) is None, (
        "a locked terrain was picked")
    lock, edit = _panel(window)
    assert lock.isChecked() and not edit.isEnabled()
    window.open_terrain_editor()
    assert opened == [] and window.toasts and window.toasts[-1][1]

    _panel(window)[0].setChecked(False)
    assert not window.terrain_locked() and not window.terrain.locked
    assert _panel(window)[1].isEnabled()


def test_the_lock_is_saved_with_the_map_and_only_when_set(window):
    from engine.terrain import Terrain

    assert 'lock' not in window.terrain.to_dict(), (
        "an unlocked terrain's record changed")
    window.set_terrain_locked(True)
    saved = window.terrain.to_dict()
    assert saved.get('lock') is True

    again = Terrain(seed=1)
    again.from_dict(saved)
    assert again.locked
    again.from_dict({k: v for k, v in saved.items() if k != 'lock'})
    assert not again.locked


def test_the_scene_hierarchy_row_selects_the_terrain_even_when_locked(window):
    from PyQt5.QtCore import Qt

    window.set_terrain_locked(True)
    window.set_selected_objects([])
    tree = window.scene_hierarchy.tree

    def terrain_row():
        return next(item for item in tree.findItems("", Qt.MatchContains | Qt.MatchRecursive)
                    if item.data(0, Qt.UserRole) == ('terrain', 0))

    tree.clearSelection()
    # Looked up again: a selection change rebuilds the list, and a row held
    # from before it is a deleted item. That includes the row being selected:
    # QTreeWidgetItem.setSelected writes to the item after the selection
    # signal returns, and the rebuild that signal runs has deleted it (heap
    # corruption, then a crash a little later). So the row is selected
    # quietly and the signal sent once setSelected is done with it, as the
    # tree would send it for a click.
    was_blocked = tree.blockSignals(True)
    try:
        terrain_row().setSelected(True)
    finally:
        tree.blockSignals(was_blocked)
    tree.itemSelectionChanged.emit()

    assert window.terrain_selected, "the row is how a locked terrain is reached"
    assert _panel(window)[0].isChecked()


def test_shift_click_picks_the_terrain_only_when_it_is_nearest(window, monkeypatch):
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtTest import QTest

    view = window.view_3d
    brush = {'id': 'b', 'name': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    window.state.brushes.append(brush)
    picking = view.picking
    state = {'object': None, 'object_distance': None, 'ground': None}

    def pick_object(mx, my, cycle=False):
        picking.last_pick_distance = state['object_distance']
        return state['object']

    monkeypatch.setattr(picking, 'get_object_at_3d', pick_object)
    monkeypatch.setattr(picking, 'terrain_pick_distance', lambda mx, my: state['ground'])

    def shift_click():
        # The modifier's own press and release, as a keyboard sends them.
        QTest.keyPress(view, Qt.Key_Shift)
        try:
            QTest.mouseClick(view, Qt.LeftButton, Qt.ShiftModifier, QPoint(20, 20))
        finally:
            QTest.keyRelease(view, Qt.Key_Shift)

    state.update(object=None, ground=300.0)
    shift_click()
    assert window.terrain_selected, "Shift+click on bare terrain did not select it"

    state.update(object=brush, object_distance=100.0, ground=300.0)
    shift_click()
    assert not window.terrain_selected and window.primary_selection() is brush, (
        "a brush in front of the terrain was not picked")

    # Selecting changes nothing in the map: the click that picked the brush
    # took an undo checkpoint, which dropped the redo history and marked an
    # untouched map modified.
    window.unsaved_changes = False
    window.save_state()
    window.undo()
    window.unsaved_changes = False
    depth, redo = len(window.state.undo_stack), len(window.state.redo_stack)
    assert redo == 1
    window.set_selected_objects([])
    shift_click()
    assert window.primary_selection() is brush
    assert (len(window.state.undo_stack), len(window.state.redo_stack)) == (depth, redo)
    assert window.unsaved_changes is False

    state.update(object=brush, object_distance=500.0, ground=300.0)
    shift_click()
    assert window.terrain_selected, "terrain in front of a brush was not picked"

    state.update(object=brush, object_distance=100.0, ground=None)   # locked, or none there
    shift_click()
    assert not window.terrain_selected and window.primary_selection() is brush
