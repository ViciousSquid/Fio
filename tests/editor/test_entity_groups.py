"""Entities (Things) in brush groups: grouped exactly as brushes are.

A group is every object carrying the same ``brush_group`` name -- on a brush
dict, or in an entity's ``properties``. The Scene Hierarchy's Add to Group...
/ Edit Group... / Ungroup / Hide Group, the group dialog, the Groups tab of
Visgroups & Cordon, moves, saves and undo all treat a grouped entity as they
treat a grouped brush.
"""
import json

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMenu

from editor import brush_groups
from editor.brush_group_dialog import BrushGroupDialog
from editor.scene_hierarchy import GROUP_COLOUR_ROLE, GROUP_EDGE_ROLE, GROUP_FIRST, GROUP_LAST
from editor.things import Light, Thing, UnresolvedThing

pytestmark = pytest.mark.qt


def _box(name, x, group=''):
    brush = {'name': name, 'pos': [float(x), 0.0, 0.0], 'size': [64, 64, 64]}
    if group:
        brush['brush_group'] = group
    return brush


def _light(name, x, group=''):
    props = {'name': name}
    if group:
        props['brush_group'] = group
    return Light(pos=[float(x), 0.0, 0.0], properties=props)


@pytest.fixture
def scene(main_window):
    window = main_window
    brushes = [_box('door', 0, 'Door'), _box('wall', 200), _box('glass', 0, 'Door')]
    things = [_light('lamp', 0, 'Door'), _light('torch', 300), _light('bulb', 500),
              _light('spot', 700)]
    window.state.brushes.extend(brushes)
    window.state.things.extend(things)
    window.update_all_ui()
    named = {b['name']: b for b in brushes}
    named.update({t.properties['name']: t for t in things})
    return window, named


def _section(window, title):
    tree = window.scene_hierarchy.tree
    header = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                  if tree.topLevelItem(i).text(0) == title)
    return {header.child(i).text(0): header.child(i) for i in range(header.childCount())}


def _thing(window, name):
    return next(t for t in window.state.things if t.properties.get('name') == name)


def _brush(window, name):
    return next(b for b in window.state.brushes if b.get('name') == name)


# -- the helpers ------------------------------------------------------------

def test_an_entity_s_group_lives_in_its_properties():
    lamp = _light('lamp', 0)
    brush_groups.set_group([lamp], 'G')
    assert lamp.properties['brush_group'] == 'G'
    assert brush_groups.group_of(lamp) == 'G'
    brush_groups.rename([lamp], 'G', 'H')
    assert brush_groups.group_of(lamp) == 'H'
    brush_groups.set_group([lamp], '')
    assert 'brush_group' not in lamp.properties
    assert brush_groups.group_of(None) == '' and not brush_groups.is_groupable(None)


def test_with_mates_carries_brushes_and_entities_alike():
    door, lamp, torch = _box('door', 0, 'G'), _light('lamp', 0, 'G'), _light('torch', 0)
    every = [door, lamp, torch]
    assert brush_groups.with_mates(every, [lamp]) == [lamp, door]
    assert brush_groups.with_mates(every, [door]) == [door, lamp]
    assert brush_groups.with_mates(every, [torch]) == [torch]


# -- the Scene Hierarchy ----------------------------------------------------

def test_grouped_entities_are_tinted_and_boxed_in_the_group_s_colour(scene):
    window, _ = scene
    brushes, things = _section(window, 'Brushes'), _section(window, 'Things')
    colour = brushes['door'].data(0, GROUP_COLOUR_ROLE)
    assert colour
    # A mixed group: one colour, boxed under each header.
    assert things['lamp'].data(0, GROUP_COLOUR_ROLE) == colour
    assert things['lamp'].data(0, GROUP_EDGE_ROLE) == GROUP_FIRST | GROUP_LAST
    assert things['lamp'].background(0).color().alpha() > 0
    assert things['lamp'].toolTip(0) == "Group: Door"
    assert things['torch'].data(0, GROUP_COLOUR_ROLE) is None
    assert things['torch'].background(0).style() == Qt.NoBrush


def test_a_group_s_entities_stay_together_in_every_sort_order(scene):
    window, things = scene
    window.set_brush_group([things['torch'], things['spot']], 'Lights')
    hierarchy = window.scene_hierarchy
    for _ in range(hierarchy.SORT_MODE_COUNT):
        rows = _section(window, 'Things')
        names = list(rows)
        assert abs(names.index('torch') - names.index('spot')) == 1, names
        first, second = sorted((names.index('torch'), names.index('spot')))
        assert rows[names[first]].data(0, GROUP_EDGE_ROLE) == GROUP_FIRST
        assert rows[names[second]].data(0, GROUP_EDGE_ROLE) == GROUP_LAST
        hierarchy.cycle_sort_mode()
    window.unsaved_changes = False


@pytest.mark.parametrize("names, label", [
    (['torch'], "Add to Group..."),
    (['torch', 'bulb'], "Add to Group..."),
    (['torch', 'wall'], "Add to Group..."),
    (['lamp'], "Edit Group..."),
    (['torch', 'lamp'], "Edit Group..."),
])
def test_the_menu_offers_add_or_edit_for_entities(scene, names, label):
    window, objects = scene
    menu = QMenu()
    action = window.scene_hierarchy._add_group_action(menu, [objects[n] for n in names])
    assert action.text() == label


def test_the_hierarchy_context_menu_has_the_group_actions_for_an_entity(scene, monkeypatch):
    """The real right-click path, not just the helper: an entity's menu gets
    the same group actions a brush's does."""
    window, _ = scene
    hierarchy = window.scene_hierarchy
    seen = []

    def exec_(menu, *_args):
        seen.extend(a.text() for a in menu.actions())
        return None

    monkeypatch.setattr(QMenu, 'exec_', exec_)
    window.set_selected_objects([_thing(window, 'lamp')])
    item = _section(window, 'Things')['lamp']
    assert item.isSelected()
    hierarchy.open_menu(hierarchy.tree.visualItemRect(item).center())
    assert "Edit Group..." in seen and "Ungroup" in seen and "Hide Group" in seen


def test_grouping_entities_through_the_menu_s_dialog(scene, monkeypatch):
    window, objects = scene
    window.unsaved_changes = False
    monkeypatch.setattr(window, '_run_brush_group_dialog',
                        lambda dialog: dialog.name_box.setCurrentText('Lights') or True)
    menu = QMenu()
    action = window.scene_hierarchy._add_group_action(
        menu, [objects['torch'], objects['bulb']])

    action.trigger()

    assert brush_groups.group_of(objects['torch']) == 'Lights'
    assert brush_groups.group_of(objects['bulb']) == 'Lights'
    assert window.unsaved_changes
    assert _section(window, 'Things')['torch'].data(0, GROUP_COLOUR_ROLE)
    window.undo()
    assert brush_groups.group_of(_thing(window, 'torch')) == ''
    window.unsaved_changes = False


def test_a_mixed_selection_makes_one_group(scene):
    window, objects = scene
    window.save_state()
    assert window.set_brush_group([objects['wall'], objects['torch']], 'Mixed')
    assert brush_groups.members(brush_groups.scene_objects(window.state), 'Mixed') == [
        objects['wall'], objects['torch']]
    assert (_section(window, 'Brushes')['wall'].data(0, GROUP_COLOUR_ROLE)
            == _section(window, 'Things')['torch'].data(0, GROUP_COLOUR_ROLE))
    window.unsaved_changes = False


def test_ungroup_takes_an_entity_out_and_dissolves_a_lone_member(scene):
    window, objects = scene
    window.set_brush_group([objects['torch'], objects['bulb']], 'Lights')

    window.ungroup_brushes([objects['lamp']])
    assert brush_groups.group_of(objects['lamp']) == ''
    assert brush_groups.group_of(objects['door']) == 'Door'
    assert _section(window, 'Things')['lamp'].data(0, GROUP_COLOUR_ROLE) is None

    # A group left with a single member is no group.
    window.ungroup_brushes([objects['torch']])
    assert brush_groups.group_of(objects['bulb']) == ''
    window.unsaved_changes = False


def test_hide_group_hides_its_entities_too(scene):
    window, objects = scene
    menu = QMenu()
    window.scene_hierarchy._add_group_action(menu, [objects['lamp']])
    toggle = menu.actions()[-1]
    assert toggle.text() == "Hide Group"
    toggle.trigger()
    assert _thing(window, 'lamp').properties['hidden']
    assert _brush(window, 'door')['hidden'] and _brush(window, 'glass')['hidden']
    assert not _thing(window, 'torch').properties.get('hidden')

    menu = QMenu()
    window.scene_hierarchy._add_group_action(menu, [_thing(window, 'lamp')])
    assert menu.actions()[-1].text() == "Show Group"
    window.undo()
    assert not _thing(window, 'lamp').properties.get('hidden')
    window.unsaved_changes = False


# -- moving ------------------------------------------------------------------

def test_moving_a_grouped_brush_carries_its_entities_and_back(scene):
    window, objects = scene
    assert window.with_group_mates([objects['glass']]) == [
        objects['glass'], objects['door'], objects['lamp']]
    assert window.with_group_mates([objects['lamp']]) == [
        objects['lamp'], objects['door'], objects['glass']]
    objects['lamp'].properties['lock'] = True
    assert objects['lamp'] not in window.movable_with_mates([objects['door']])


def test_the_3d_view_move_carries_a_grouped_entity(scene):
    import glm
    window, objects = scene
    window.set_selected_objects([objects['glass']])
    window.view_3d.set_primary_selection_pos(glm.vec3(64.0, 0.0, 0.0))
    assert objects['lamp'].pos[0] == 64.0, "the grouped entity was left behind"
    assert objects['torch'].pos[0] == 300.0
    window.unsaved_changes = False


# -- selecting ---------------------------------------------------------------

def test_selecting_a_group_selects_its_entities(scene):
    window, objects = scene
    panel = window.set_visgroups_window_visible(True)
    panel.refresh()
    tree = panel.brush_group_tree
    top = tree.topLevelItem(0)
    assert top.text(0) == 'Door' and top.text(1) == '3'
    assert [top.child(i).text(0) for i in range(top.childCount())] == [
        'door', 'glass', 'lamp']
    tree.setCurrentItem(top)

    panel.select_brush_group()
    assert window.selected_objects_list() == [
        objects['door'], objects['glass'], objects['lamp']]

    # Double-clicking the entity's row selects just it.
    top = panel.brush_group_tree.topLevelItem(0)
    panel._on_brush_group_double_click(top.child(2), 0)
    assert window.selected_objects_list() == [objects['lamp']]

    panel._rename_brush_group('Door', 'Front door')
    assert brush_groups.group_of(objects['lamp']) == 'Front door'

    tree.setCurrentItem(panel.brush_group_tree.topLevelItem(0))
    panel.ungroup_brush_group()
    assert brush_groups.group_of(objects['lamp']) == ''
    assert panel.brush_group_tree.topLevelItemCount() == 0
    panel.hide()
    window.unsaved_changes = False


# -- the dialog -------------------------------------------------------------

def _dialog(window, chosen):
    return BrushGroupDialog(window, brush_groups.scene_objects(window.state), chosen,
                            brush_groups.labeller(window.state, window.scene_hierarchy))


def test_the_dialog_lists_and_edits_entity_members(scene):
    window, objects = scene
    dialog = _dialog(window, [objects['torch']])
    dialog.name_box.setCurrentText('Door')
    assert [dialog.member_list.item(i).text() for i in range(dialog.member_list.count())] == [
        'door', 'glass', 'lamp', 'torch']
    dialog.member_list.item(2).setCheckState(Qt.Unchecked)       # 'lamp'

    assert dialog.apply()

    assert brush_groups.group_of(objects['torch']) == 'Door'
    assert brush_groups.group_of(objects['lamp']) == ''


def test_the_dialog_renames_a_mixed_group(scene):
    window, objects = scene
    dialog = _dialog(window, [objects['lamp']])
    assert dialog.editing == 'Door'
    dialog.name_box.setCurrentText('Front door')
    dialog.apply()
    assert brush_groups.group_of(objects['door']) == 'Front door'
    assert brush_groups.group_of(objects['lamp']) == 'Front door'


# -- undo, saving, copying ---------------------------------------------------

def test_grouping_entities_undoes_and_redoes(scene):
    window, objects = scene
    window.save_state()
    window.set_brush_group([objects['torch'], objects['bulb']], 'Lights')

    window.undo()
    assert brush_groups.group_of(_thing(window, 'torch')) == ''
    assert 'Lights' not in brush_groups.groups(brush_groups.scene_objects(window.state))
    window.redo()
    assert brush_groups.group_of(_thing(window, 'torch')) == 'Lights'
    assert brush_groups.group_of(_thing(window, 'bulb')) == 'Lights'
    assert _section(window, 'Things')['torch'].data(0, GROUP_COLOUR_ROLE)

    window.ungroup_brushes([_thing(window, 'lamp')])
    window.undo()
    assert brush_groups.group_of(_thing(window, 'lamp')) == 'Door'
    window.redo()
    assert brush_groups.group_of(_thing(window, 'lamp')) == ''
    window.unsaved_changes = False


def test_an_entity_s_group_is_saved_and_loaded_with_the_map(scene, tmp_path):
    window, _ = scene
    saved = {t['properties']['name']: t['properties'].get('brush_group')
             for t in window.state.get_level_data()['things']}
    assert saved['lamp'] == 'Door' and 'torch' in saved and saved['torch'] is None

    path = tmp_path / 'grouped.json'
    window._write_map(str(path))
    window.unsaved_changes = False
    window.load_level_file(str(path))

    assert brush_groups.group_of(_thing(window, 'lamp')) == 'Door'
    assert brush_groups.group_of(_thing(window, 'torch')) == ''
    assert brush_groups.members(brush_groups.scene_objects(window.state), 'Door') == [
        _brush(window, 'door'), _brush(window, 'glass'), _thing(window, 'lamp')]
    assert _section(window, 'Things')['lamp'].data(0, GROUP_COLOUR_ROLE)
    window.unsaved_changes = False


def test_an_old_map_s_entities_load_and_save_unchanged():
    old = Light(pos=[1.0, 2.0, 3.0], properties={'name': 'lamp'}).to_dict()
    assert 'brush_group' not in old['properties']
    loaded = Thing.from_dict(json.loads(json.dumps(old)))
    assert brush_groups.group_of(loaded) == ''
    assert loaded.to_dict() == old


def test_an_unresolved_entity_keeps_its_group():
    record = {'type': 'some_missing_plugin_thing', 'pos': [0, 0, 0],
              'properties': {'id': 'x', 'name': 'ghost', 'custom': 1}}
    ghost = UnresolvedThing(record)
    assert ghost.to_dict() == record                      # verbatim when untouched
    brush_groups.set_group([ghost], 'G')
    assert ghost.to_dict()['properties']['brush_group'] == 'G'
    assert ghost.to_dict()['properties']['custom'] == 1
    brush_groups.set_group([ghost], '')
    assert 'brush_group' not in ghost.to_dict()['properties']


def test_a_cloned_or_pasted_entity_stays_in_its_group_as_a_brush_does(scene):
    window, objects = scene
    window.set_selected_objects([objects['glass'], objects['lamp']])
    window.copy_selection()
    window.paste_selection()
    pasted = window.selected_objects_list()
    assert len(pasted) == 2
    assert [brush_groups.group_of(o) for o in pasted] == ['Door', 'Door']

    window.set_selected_objects([_thing(window, 'lamp')])
    window.clone_selected_object()
    clone = window.selected_objects_list()[0]
    window.finish_clone_placement()
    assert clone is not _thing(window, 'lamp')
    assert brush_groups.group_of(clone) == 'Door'
    window.unsaved_changes = False
