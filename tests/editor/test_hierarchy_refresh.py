"""Hierarchy rebuilding preserves selection and recovers from failed updates."""

import pytest

pytest.importorskip('PyQt5', reason='the hierarchy is a Qt widget')

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor

from editor.scene_hierarchy import SceneHierarchy
from tests.helpers.hierarchy_reference import reference_refresh_list

pytestmark = pytest.mark.qt


class HierarchyThing:
    def __init__(self, name, **properties):
        self.name = name
        self.properties = dict(name=name, **properties)


class EqualThing(HierarchyThing):
    def __eq__(self, other):
        return isinstance(other, EqualThing) and self.name == other.name


@pytest.fixture
def hierarchy(main_window):
    # Exercise the real editor ownership and selection callbacks. The test
    # controls every mutation, so the background simulation is stopped first.
    logic = main_window.view_3d.logic_thread
    logic.stop()
    if logic.is_alive():
        logic.join(timeout=1)
    return main_window.scene_hierarchy


def hierarchy_rows(panel):
    """Read actual Qt row content, appearance, and selection."""
    rows = []
    for index in range(panel.tree.topLevelItemCount()):
        header = panel.tree.topLevelItem(index)
        for item in [header] + [header.child(i) for i in range(header.childCount())]:
            rows.append((item.text(0), item.text(1), item.data(0, Qt.UserRole),
                         item.isSelected(), item.isExpanded(), int(item.flags()),
                         item.font(0).bold(), item.foreground(0),
                         item.background(0), item.background(1),
                         item.icon(1).pixmap(44, 20).toImage()))
    return rows


@pytest.mark.parametrize('sort_mode', range(SceneHierarchy.SORT_MODE_COUNT))
def test_refresh_matches_original_tree_and_selection(hierarchy, sort_mode):
    class BrushSubclass(dict):
        pass

    brushes = [
        {'id': 'a', 'name': 'zebra', 'hidden': True, 'color': 'red'},
        {'id': 'b', 'name': 'alpha', 'lock': True, 'color': 'green'},
        {'name': 'legacy'}, {'id': ['legacy'], 'name': 'unhashable'},
        {'id': 'dup', 'name': 'duplicate-one'},
        {'id': 'dup', 'name': 'duplicate-two'},
        BrushSubclass(id='subclass', is_trigger=True),
    ]
    things = [HierarchyThing('prop', lock=True, color='pink'),
              EqualThing('equal', hidden=True), HierarchyThing('unselected')]
    state = hierarchy.main_window.state
    state.brushes, state.things = brushes, things
    state.terrain_data = {'biome': 'desert_canyon'}
    hierarchy.sort_mode = sort_mode
    hierarchy._search_matches = {id(brushes[0]), id(things[1])}

    selections = [[], brushes + things,
                  [dict(brushes[0]), dict(brushes[2]), dict(brushes[3]),
                   dict(brushes[5]), dict(brushes[6]), things[0], EqualThing('equal')],
                  [brushes[0], brushes[0], things[0], things[0]]]
    for selected in selections:
        state.selected_objects = selected
        reference_refresh_list(hierarchy)
        expected = hierarchy_rows(hierarchy)
        hierarchy.refresh_list()
        assert hierarchy_rows(hierarchy) == expected


@pytest.mark.parametrize('blocked', [False, True])
def test_refresh_preserves_callers_signal_blocking(hierarchy, blocked):
    hierarchy.main_window.state.brushes = [{'name': 'wall'}]
    hierarchy.tree.blockSignals(blocked)
    notifications = []
    hierarchy.tree.itemSelectionChanged.connect(lambda: notifications.append(True))
    hierarchy.refresh_list()
    assert hierarchy.tree.signalsBlocked() is blocked
    assert not notifications
    hierarchy.tree.blockSignals(False)
    # Select the row as a click does, through the view. Not with the item's
    # own setSelected(): the selection notification rebuilds the tree
    # synchronously (MainWindow.update_all_ui -> refresh_list), deleting the
    # item while QTreeWidgetItem::setSelected still writes to it afterwards --
    # a use-after-free that crashed this run intermittently.
    hierarchy.tree.setCurrentItem(hierarchy.tree.topLevelItem(0).child(0))
    assert notifications == [True]


@pytest.mark.parametrize('blocked', [False, True])
@pytest.mark.parametrize('failure', ['_get_sorted_brushes', '_get_sorted_things',
                                   '_apply_search_styling'])
def test_failed_refresh_restores_signals_and_next_refresh_works(
        hierarchy, monkeypatch, blocked, failure):
    hierarchy.main_window.state.brushes = [{'name': 'wall'}]
    hierarchy._search_matches = {id(hierarchy.main_window.state.brushes[0])}
    hierarchy.tree.blockSignals(blocked)

    def fail():
        raise RuntimeError('injected refresh failure')

    with monkeypatch.context() as patch:
        patch.setattr(hierarchy, failure, fail)
        with pytest.raises(RuntimeError, match='injected refresh failure'):
            hierarchy.refresh_list()
    assert hierarchy.tree.signalsBlocked() is blocked
    hierarchy.refresh_list()
    assert hierarchy.tree.signalsBlocked() is blocked
    assert hierarchy.tree.topLevelItem(0).childCount() == 1


def test_each_refresh_reads_selection_and_object_mutations(hierarchy):
    state = hierarchy.main_window.state
    first, second = {'id': 'first'}, {'id': 'second'}
    state.brushes = [first, second]
    state.selected_objects = [dict(first)]
    hierarchy.refresh_list()
    assert [item.data(0, Qt.UserRole) for item in hierarchy.tree.selectedItems()] == [('brush', 0)]
    state.selected_objects[:] = [dict(second)]
    hierarchy.refresh_list()
    assert [item.data(0, Qt.UserRole) for item in hierarchy.tree.selectedItems()] == [('brush', 1)]
    second['id'] = 'changed'
    hierarchy.refresh_list()
    assert not hierarchy.tree.selectedItems()


def test_highlight_restores_search_tint_and_distinct_column_backgrounds(hierarchy):
    brush = {'name': 'wall'}
    hierarchy.main_window.state.brushes = [brush]
    hierarchy._search_matches = {id(brush)}
    hierarchy.refresh_list()
    row = hierarchy.tree.topLevelItem(0).child(0)
    row.setBackground(1, QBrush(QColor('#345678')))
    expected = (row.background(0), row.background(1))
    for _ in range(2):
        hierarchy.highlight_item(brush)
    assert row.background(0).color() == QColor('#00CED1')
    hierarchy._clear_highlight()
    assert (row.background(0), row.background(1)) == expected
    hierarchy._clear_highlight()
    assert (row.background(0), row.background(1)) == expected


@pytest.mark.parametrize('matched', [False, True])
def test_search_change_during_highlight_does_not_restore_stale_tint(hierarchy, matched):
    brush = {'name': 'wall'}
    hierarchy.main_window.state.brushes = [brush]
    hierarchy._search_matches = set() if matched else {id(brush)}
    hierarchy.refresh_list()
    row = hierarchy.tree.topLevelItem(0).child(0)
    hierarchy.highlight_item(brush)
    hierarchy._search_matches = {id(brush)} if matched else set()
    hierarchy._apply_search_styling()
    expected = (row.background(0), row.background(1))
    hierarchy._clear_highlight()
    assert (row.background(0), row.background(1)) == expected


@pytest.mark.parametrize('refresh', [False, True])
def test_highlight_cleanup_survives_deleted_rows(hierarchy, refresh):
    brush = {'name': 'wall'}
    hierarchy.main_window.state.brushes = [brush]
    hierarchy.refresh_list()
    hierarchy.highlight_item(brush)
    if refresh:
        hierarchy.refresh_list()
        assert not hierarchy._highlight_timer.isActive()
    else:
        hierarchy.tree.clear()
    hierarchy._clear_highlight()
    assert hierarchy._highlighted_item is None
    assert hierarchy._highlight_backgrounds is None
