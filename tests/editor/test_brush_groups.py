"""Brush groups in the editor: Scene Hierarchy > Add to Group... / Edit
Group..., members kept together and boxed in the list, grouped brushes moved
and turned together, and the Groups tab of Visgroups & Cordon."""
import pytest
from PyQt5.QtCore import Qt

from editor import brush_groups
from editor.brush_group_dialog import BrushGroupDialog
from editor.scene_hierarchy import (
    GROUP_COLOUR_ROLE, GROUP_EDGE_ROLE, GROUP_FIRST, GROUP_LAST)

pytestmark = pytest.mark.qt


def _box(name, x, group=''):
    brush = {'name': name, 'pos': [float(x), 0.0, 0.0], 'size': [64, 64, 64]}
    if group:
        brush['brush_group'] = group
    return brush


@pytest.fixture
def scene(main_window):
    window = main_window
    brushes = [_box('door', 0, 'Door'), _box('wall', 200), _box('glass', 0, 'Door'),
               _box('lift', 400, 'Lift'), _box('pillar', 600), _box('rail', 400, 'Lift')]
    window.state.brushes.extend(brushes)
    window.update_all_ui()
    return window, {b['name']: b for b in brushes}


def _rows(window):
    tree = window.scene_hierarchy.tree
    header = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                  if tree.topLevelItem(i).text(0) == 'Brushes')
    return [header.child(i) for i in range(header.childCount())]


# -- the helpers ------------------------------------------------------------

def test_with_mates_adds_each_grouped_brush_once():
    a, b, c, d = _box('a', 0, 'G'), _box('b', 0, 'G'), _box('c', 0), _box('d', 0, 'H')
    every = [a, b, c, d]
    assert brush_groups.with_mates(every, [b]) == [b, a]
    assert brush_groups.with_mates(every, [c]) == [c]
    assert brush_groups.with_mates(every, [a, b, d]) == [a, b, d]


# -- the Scene Hierarchy ----------------------------------------------------

def test_a_group_s_members_stay_together_in_the_list(scene):
    window, _ = scene
    assert [row.text(0) for row in _rows(window)] == [
        'door', 'glass', 'wall', 'lift', 'rail', 'pillar']


def test_they_stay_together_in_every_sort_order(scene):
    window, _ = scene
    hierarchy = window.scene_hierarchy
    for _ in range(hierarchy.SORT_MODE_COUNT):
        names = [row.text(0) for row in _rows(window)]
        assert abs(names.index('door') - names.index('glass')) == 1, names
        assert abs(names.index('lift') - names.index('rail')) == 1, names
        hierarchy.cycle_sort_mode()


def test_each_group_is_tinted_and_boxed_in_its_own_colour(scene):
    window, _ = scene
    rows = {row.text(0): row for row in _rows(window)}
    door, lift = rows['door'].data(0, GROUP_COLOUR_ROLE), rows['lift'].data(0, GROUP_COLOUR_ROLE)
    assert door and lift and door != lift
    assert rows['glass'].data(0, GROUP_COLOUR_ROLE) == door
    assert rows['wall'].data(0, GROUP_COLOUR_ROLE) is None
    assert rows['door'].data(0, GROUP_EDGE_ROLE) == GROUP_FIRST
    assert rows['glass'].data(0, GROUP_EDGE_ROLE) == GROUP_LAST
    assert rows['door'].background(0).color().alpha() > 0
    assert rows['wall'].background(0).style() == Qt.NoBrush


def test_grouped_rows_keep_their_hidden_lock_and_tag_indicators(scene):
    window, brushes = scene
    brushes['door']['hidden'] = True
    brushes['glass']['color'] = 'red'
    brushes['lift']['lock'] = True
    brushes['rail'].update(lock=True, color='blue')
    window.update_all_ui()

    rows = {row.text(0): row for row in _rows(window)}
    for name in ('door', 'glass', 'lift', 'rail'):
        assert not rows[name].icon(0).isNull(), "%s lost its indicator" % name
        assert rows[name].data(0, GROUP_COLOUR_ROLE), "%s lost its group box" % name
    # A hidden row keeps its amber name as well.
    assert rows['door'].foreground(0).color().name().lower() == '#ffac1c'
    assert rows['wall'].icon(0).isNull()


def test_a_search_keeps_the_group_tint_on_rows_it_does_not_match(scene):
    window, _ = scene
    hierarchy = window.scene_hierarchy
    hierarchy.search_box.setText('pillar')
    hierarchy.apply_search()
    rows = {row.text(0): row for row in _rows(window)}
    assert rows['door'].background(0).color().alpha() > 0


@pytest.mark.parametrize("names, label", [
    (['wall'], "Add to Group..."),
    (['wall', 'pillar'], "Add to Group..."),
    (['glass'], "Edit Group..."),
    (['wall', 'glass'], "Edit Group..."),
])
def test_the_menu_offers_add_or_edit(scene, names, label):
    from PyQt5.QtWidgets import QMenu
    window, brushes = scene
    menu = QMenu()
    action = window.scene_hierarchy._add_group_action(menu, [brushes[n] for n in names])
    assert action.text() == label


def _named(window, name):
    return next(b for b in window.state.brushes if b['name'] == name)


def test_ungroup_takes_out_only_the_chosen_brushes(scene):
    window, brushes = scene
    extra = _box('handle', 0, 'Door')
    window.state.brushes.append(extra)
    from PyQt5.QtWidgets import QMenu
    menu = QMenu()
    window.scene_hierarchy._add_group_action(menu, [brushes['glass']])
    assert [a.text() for a in menu.actions()] == [
        "Edit Group...", "Ungroup", "Hide Group"]

    window.ungroup_brushes([brushes['glass']])

    assert brush_groups.group_of(brushes['glass']) == ''
    assert brush_groups.group_of(brushes['door']) == 'Door'
    assert brush_groups.group_of(extra) == 'Door'
    rows = {row.text(0): row for row in _rows(window)}
    assert rows['glass'].data(0, GROUP_COLOUR_ROLE) is None, "the colour box stayed"
    assert rows['glass'].background(0).style() == Qt.NoBrush
    assert rows['lift'].data(0, GROUP_COLOUR_ROLE)
    window.unsaved_changes = False


def test_the_menu_hides_and_shows_the_whole_group(scene):
    from PyQt5.QtWidgets import QMenu
    window, brushes = scene

    def toggle():
        menu = QMenu()
        window.scene_hierarchy._add_group_action(menu, [_named(window, 'glass')])
        action = menu.actions()[-1]
        action.trigger()
        return action.text()

    assert toggle() == "Hide Group"
    assert _named(window, 'door')['hidden'] and _named(window, 'glass')['hidden']
    assert not _named(window, 'wall').get('hidden')
    assert not _named(window, 'lift').get('hidden')

    window.undo()
    assert not _named(window, 'door').get('hidden')
    window.redo()
    assert _named(window, 'door')['hidden']

    assert toggle() == "Show Group"
    assert not _named(window, 'door')['hidden'] and not _named(window, 'glass')['hidden']
    window.unsaved_changes = False


def test_a_group_left_with_one_brush_is_dissolved(scene):
    window, brushes = scene
    window.ungroup_brushes([brushes['glass']])
    assert brush_groups.group_of(brushes['door']) == ''
    window.unsaved_changes = False


def test_the_menu_has_no_ungroup_for_ungrouped_brushes(scene):
    from PyQt5.QtWidgets import QMenu
    window, brushes = scene
    menu = QMenu()
    window.scene_hierarchy._add_group_action(menu, [brushes['wall']])
    assert [a.text() for a in menu.actions()] == ["Add to Group..."]


def test_grouping_and_ungrouping_undo_and_redo(scene):
    window, brushes = scene
    window.save_state()                                  # a baseline to undo to
    window.set_brush_group([brushes['wall'], brushes['pillar']], 'Walls')
    assert brush_groups.group_of(_named(window, 'wall')) == 'Walls'

    window.undo()
    assert brush_groups.group_of(_named(window, 'wall')) == ''
    assert 'Walls' not in brush_groups.groups(window.state.brushes)
    window.redo()
    assert brush_groups.group_of(_named(window, 'wall')) == 'Walls'
    assert brush_groups.group_of(_named(window, 'pillar')) == 'Walls'

    window.ungroup_brushes([_named(window, 'rail')])
    assert 'Lift' not in brush_groups.groups(window.state.brushes)
    window.undo()
    assert brush_groups.group_of(_named(window, 'rail')) == 'Lift'
    assert _rows(window) and {r.text(0): r for r in _rows(window)}['rail'].data(
        0, GROUP_COLOUR_ROLE), "the list did not show the group again after undo"
    window.redo()
    assert brush_groups.group_of(_named(window, 'rail')) == ''
    window.unsaved_changes = False


# -- the dialog -------------------------------------------------------------

def _dialog(window, chosen):
    return BrushGroupDialog(window, window.state.brushes, chosen, lambda b: b['name'])


def test_adding_makes_a_named_group(scene):
    window, brushes = scene
    dialog = _dialog(window, [brushes['wall'], brushes['pillar']])
    assert dialog.name() == 'Group 1' and not dialog.editing
    dialog.name_box.setCurrentText('Walls')

    assert dialog.apply()

    assert brush_groups.group_of(brushes['wall']) == 'Walls'
    assert brush_groups.group_of(brushes['pillar']) == 'Walls'


def test_adding_to_an_existing_group_joins_it(scene):
    window, brushes = scene
    dialog = _dialog(window, [brushes['wall']])
    dialog.name_box.setCurrentText('Door')
    assert [b['name'] for b in dialog.kept()] == ['door', 'glass', 'wall']

    dialog.apply()

    assert [b['name'] for b in brush_groups.members(window.state.brushes, 'Door')] == [
        'door', 'wall', 'glass']


def test_editing_renames_the_whole_group(scene):
    window, brushes = scene
    dialog = _dialog(window, [brushes['glass']])
    assert dialog.editing == 'Door' and dialog.name() == 'Door'
    dialog.name_box.setCurrentText('Front door')
    assert dialog.renaming()

    dialog.apply()

    assert brush_groups.group_of(brushes['door']) == 'Front door'
    assert brush_groups.group_of(brushes['glass']) == 'Front door'
    assert 'Door' not in brush_groups.groups(window.state.brushes)


def test_unticking_a_member_takes_it_out(scene):
    from PyQt5.QtCore import Qt
    window, brushes = scene
    dialog = _dialog(window, [brushes['glass']])
    dialog.member_list.item(0).setCheckState(Qt.Unchecked)       # 'door'

    dialog.apply()

    assert brush_groups.group_of(brushes['door']) == ''
    assert brush_groups.group_of(brushes['glass']) == 'Door'


def test_ungroup_dissolves_the_group(scene):
    window, brushes = scene
    dialog = _dialog(window, [brushes['glass']])
    dialog._ungroup()

    dialog.apply()

    assert 'Door' not in brush_groups.groups(window.state.brushes)
    assert brush_groups.group_of(brushes['lift']) == 'Lift'


def test_the_menu_s_dialog_is_one_undo_step_and_marks_the_map(scene, monkeypatch):
    window, brushes = scene
    window.unsaved_changes = False
    monkeypatch.setattr(window, '_run_brush_group_dialog',
                        lambda dialog: dialog.name_box.setCurrentText('Walls') or True)

    window.open_brush_group_dialog([brushes['wall']])

    assert brush_groups.group_of(brushes['wall']) == 'Walls'
    assert window.unsaved_changes
    assert 'wall' in [row.text(0) for row in _rows(window)]
    window.undo()
    wall = next(b for b in window.state.brushes if b['name'] == 'wall')
    assert brush_groups.group_of(wall) == ''
    window.unsaved_changes = False


def test_the_group_is_saved_with_the_map(scene):
    window, _ = scene
    saved = {b['name']: b.get('brush_group') for b in window.state.get_level_data()['brushes']}
    assert saved['glass'] == 'Door' and saved['rail'] == 'Lift' and not saved['wall']


# -- grouped brushes move together ------------------------------------------

def test_moving_one_member_in_the_3d_view_carries_the_group(scene):
    import glm
    window, brushes = scene
    window.set_selected_objects([brushes['glass']])

    window.view_3d.set_primary_selection_pos(glm.vec3(64.0, 0.0, 0.0))

    assert brushes['glass']['pos'][0] == 64.0
    assert brushes['door']['pos'][0] == 64.0, "the grouped brush was left behind"
    assert brushes['wall']['pos'][0] == 200.0
    assert brushes['lift']['pos'][0] == 400.0


def test_a_locked_member_is_not_dragged(scene):
    window, brushes = scene
    brushes['door']['lock'] = True
    moved = window.movable_with_mates([brushes['glass']])
    assert moved == [brushes['glass']]


def test_a_2d_drag_carries_the_group(scene):
    window, brushes = scene
    assert window.movable_with_mates([brushes['rail']]) == [brushes['rail'], brushes['lift']]


def test_rotating_one_member_turns_the_group_as_one_body(scene):
    window, brushes = scene
    brushes['glass']['pos'] = [100.0, 0.0, 0.0]
    window.set_selected_objects([brushes['glass']])

    assert window.apply_rotation_to_selection(90.0, (0.0, 1.0, 0.0)) == 2

    # Door at x 0, glass at x 100: they swing about x 50, their shared centre.
    door, glass = brushes['door']['pos'], brushes['glass']['pos']
    assert door[0] == pytest.approx(50.0, abs=1e-3) and glass[0] == pytest.approx(50.0, abs=1e-3)
    assert abs(door[2] - glass[2]) == pytest.approx(100.0, abs=1e-3)
    assert brushes['wall']['pos'] == [200.0, 0.0, 0.0]
    window.unsaved_changes = False


# -- Visgroups & Cordon > Groups --------------------------------------------

def _groups_tree(window):
    panel = window.set_visgroups_window_visible(True)
    panel.refresh()
    tree = panel.brush_group_tree
    tops = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    return panel, tree, tops


def test_the_dialog_lists_each_group_and_its_brushes(scene):
    window, _ = scene
    panel, _tree, tops = _groups_tree(window)
    listed = {top.text(0): [top.child(i).text(0) for i in range(top.childCount())]
              for top in tops}
    assert listed == {'Door': ['door', 'glass'], 'Lift': ['lift', 'rail']}
    assert [top.text(1) for top in tops] == ['2', '2']
    panel.hide()


def test_the_dialog_selects_and_ungroups_a_group(scene):
    window, brushes = scene
    panel, tree, tops = _groups_tree(window)
    tree.setCurrentItem(tops[1])

    panel.select_brush_group()
    assert window.selected_objects_list() == [brushes['lift'], brushes['rail']]

    panel.ungroup_brush_group()
    assert 'Lift' not in brush_groups.groups(window.state.brushes)
    assert panel.brush_group_tree.topLevelItemCount() == 1
    panel.hide()
    window.unsaved_changes = False


def test_renaming_in_the_dialog_renames_the_group(scene):
    window, brushes = scene
    panel, _tree, _tops = _groups_tree(window)

    panel._rename_brush_group('Door', 'Front door')

    assert brush_groups.group_of(brushes['glass']) == 'Front door'
    assert panel.brush_group_tree.topLevelItem(0).text(0) == 'Front door'
    panel.hide()
    window.unsaved_changes = False


# -- at most one mover per group ---------------------------------------------

@pytest.fixture
def toasts(scene):
    window, _ = scene
    shown = []
    real = window.show_toast

    def show_toast(message, *args, **kwargs):
        shown.append(str(message))
        return real(message, *args, **kwargs)

    window.show_toast = show_toast
    return shown


def test_a_group_cannot_be_made_with_two_movers(scene, toasts):
    window, brushes = scene
    brushes['wall']['is_mover'] = True
    brushes['pillar']['is_door'] = True

    assert not window.set_brush_group([brushes['wall'], brushes['pillar']], 'Both')

    assert toasts == ["Maximum of 1 mover per group"]
    assert brush_groups.group_of(brushes['wall']) == ''
    assert brush_groups.group_of(brushes['pillar']) == ''


def test_a_second_mover_cannot_join_a_group_that_has_one(scene, toasts):
    window, brushes = scene
    brushes['door']['is_door'] = True
    brushes['wall']['is_mover'] = True

    assert not window.set_brush_group([brushes['wall']], 'Door')
    assert toasts == ["Maximum of 1 mover per group"]
    assert brush_groups.group_of(brushes['wall']) == ''
    # One mover and any number of plain brushes is what a group is for.
    assert window.set_brush_group([brushes['pillar']], 'Door')
    window.unsaved_changes = False


def test_the_dialog_will_not_accept_two_movers(scene, toasts):
    window, brushes = scene
    brushes['door']['is_door'] = True
    brushes['wall']['is_mover'] = True
    dialog = _dialog(window, [brushes['wall']])
    dialog.name_box.setCurrentText('Door')

    dialog.accept()

    assert dialog.result() != dialog.Accepted
    assert "Maximum of 1 mover per group" in dialog.hint.text()
    assert not dialog.apply()
    assert brush_groups.group_of(brushes['wall']) == ''
    # Unticking one of the movers makes it a group again.
    dialog.member_list.item(0).setCheckState(Qt.Unchecked)       # 'door'
    assert dialog.allowed()


def test_renaming_a_group_onto_another_with_a_mover_is_refused(scene, toasts):
    window, brushes = scene
    brushes['door']['is_door'] = True
    brushes['lift']['is_mover'] = True

    assert not window.rename_brush_group('Lift', 'Door')

    assert toasts == ["Maximum of 1 mover per group"]
    assert brush_groups.group_of(brushes['lift']) == 'Lift'


def test_a_grouped_brush_cannot_become_the_group_s_second_mover(scene, toasts):
    window, brushes = scene
    brushes['door']['is_door'] = True
    editor = window.property_editor
    window.set_selected_objects([brushes['glass']])
    editor.set_object(brushes['glass'])

    editor.on_mover_changed(True)
    assert not brushes['glass'].get('is_mover')
    editor.on_door_changed(True)
    assert not brushes['glass'].get('is_door')
    assert toasts == ["Maximum of 1 mover per group"] * 2

    # The group's own mover may still be switched between door and mover.
    editor.set_object(brushes['door'])
    editor.on_mover_changed(True)
    assert brushes['door'].get('is_mover')
    window.unsaved_changes = False


def test_the_property_field_cannot_put_a_second_mover_in_a_group(scene, toasts):
    window, brushes = scene
    brushes['door']['is_door'] = True
    brushes['wall']['is_mover'] = True
    editor = window.property_editor
    window.set_selected_objects([brushes['wall']])
    editor.set_object(brushes['wall'])
    field = editor._widgets['brush_group_input']

    field.setText('Door')
    editor._on_brush_group_edited(field)

    assert toasts == ["Maximum of 1 mover per group"]
    assert brush_groups.group_of(brushes['wall']) == '' and field.text() == ''
    window.unsaved_changes = False
