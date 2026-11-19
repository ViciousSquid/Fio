"""Groups in the Scene Hierarchy: Select Group, editing a group, its tint.

* Right-clicking any member -- a brush or a model -- offers "Select Group",
  which selects every member. Entities in a brush group used to be left out
  (and a group of models only resolved to nothing), so did I/O aimed at it.
* An edit made in the property panel with several objects selected goes to
  every selected object that has that property, never to per-object keys.
* Grouped rows are tinted in their group's colour; the tree's style sheet
  had been hiding the tint.
"""

import pytest

from editor.editor_state import EditorState
from editor.things import Light, Prop
from engine.io_groups import resolve_group
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt


def _chairs(n=3, group='Chairs'):
    chairs = []
    for i in range(n):
        chair = Prop.for_model('assets/models/chair.obj', pos=[i * 64, 0, 0])
        chair.properties['name'] = 'chair%d' % i
        chair.properties['brush_group'] = group
        chairs.append(chair)
    return chairs


def test_a_group_of_models_resolves_to_its_models():
    state = EditorState()
    chairs = _chairs()
    state.things.extend(chairs + [Light(pos=[0, 0, 0])])
    assert resolve_group(state, 'Chairs') == chairs


def test_a_mixed_group_holds_its_brushes_and_its_entities():
    state = EditorState()
    table = box_brush('table', (0, 0, 0), brush_group='Set')
    chairs = _chairs(group='Set')
    state.brushes.append(table)
    state.things.extend(chairs)
    assert resolve_group(state, 'Set') == [table] + chairs


def test_right_clicking_a_grouped_model_offers_and_runs_select_group(main_window):
    chairs = _chairs()
    main_window.state.things.extend(chairs)
    hierarchy = main_window.scene_hierarchy
    assert ('', 'Chairs') in hierarchy.groups_of([chairs[1]])

    from PyQt5.QtWidgets import QMenu
    menu = QMenu()
    actions = hierarchy._add_select_group_actions(menu, [chairs[1]])
    assert [a.text() for a in actions] == ["Select Group 'Chairs'"]
    actions[0].trigger()
    assert main_window.state.selected_objects == chairs


def test_an_edit_reaches_every_selected_object_with_that_property(main_window):
    chairs = _chairs()
    lamp = Light(pos=[0, 100, 0])
    main_window.state.things.extend(chairs + [lamp])
    main_window.set_selected_objects(chairs + [lamp])
    panel = main_window.property_editor
    assert panel.current_object is chairs[0]

    panel.update_object_prop('scale', [2.0, 2.0, 2.0])
    assert all(c.properties['scale'] == [2.0, 2.0, 2.0] for c in chairs)
    # Each its own copy: editing one later cannot move the others.
    assert chairs[1].properties['scale'] is not chairs[2].properties['scale']
    # A light has no scale and is not given one.
    assert 'scale' not in lamp.properties or lamp.properties['scale'] != [2.0, 2.0, 2.0]


def _typed_component(panel, thing, key, index, text):
    """Type *text* into component *index* of *thing*'s vector field *key*, as
    the panel's own three-box widget does on editingFinished."""
    from PyQt5.QtWidgets import QFormLayout, QLineEdit, QWidget
    host = QWidget()
    form = QFormLayout(host)
    panel.add_vector3_widget(form, thing, key)
    boxes = host.findChildren(QLineEdit)
    boxes[index].setText(text)
    boxes[index].editingFinished.emit()


def test_typing_one_axis_changes_only_that_axis_on_the_others(main_window):
    chairs = _chairs()
    for chair, yaw in zip(chairs, (0.0, 90.0, 180.0)):
        chair.properties['rotation'] = [0.0, yaw, 0.0]
    main_window.state.things.extend(chairs)
    main_window.set_selected_objects(chairs)
    panel = main_window.property_editor
    assert panel.current_object is chairs[0]

    _typed_component(panel, chairs[0], 'rotation', 0, '10')
    # Each chair keeps its own yaw; only the typed axis is shared.
    assert [c.properties['rotation'] for c in chairs] == [
        [10.0, 0.0, 0.0], [10.0, 90.0, 0.0], [10.0, 180.0, 0.0]]
    assert chairs[1].properties['rotation'] is not chairs[2].properties['rotation']


def test_one_undo_restores_every_object_a_shared_edit_reached(main_window):
    chairs = _chairs()
    for chair, yaw in zip(chairs, (0.0, 90.0, 180.0)):
        chair.properties['rotation'] = [0.0, yaw, 0.0]
    main_window.state.things.extend(chairs)
    main_window.set_selected_objects(chairs)
    # No checkpoint of its own: the shared edit takes the one undo uses.
    _typed_component(main_window.property_editor, chairs[0], 'rotation', 0, '10')
    main_window.undo()
    assert [t.properties['rotation'] for t in main_window.state.things[-3:]] == [
        [0.0, 0.0, 0.0], [0.0, 90.0, 0.0], [0.0, 180.0, 0.0]]


def test_names_and_ids_stay_per_object(main_window):
    chairs = _chairs()
    main_window.state.things.extend(chairs)
    main_window.set_selected_objects(chairs)
    panel = main_window.property_editor
    panel.update_object_prop('name', 'renamed')
    assert [c.properties['name'] for c in chairs] == ['renamed', 'chair1', 'chair2']


def test_brush_edits_reach_brushes_but_not_their_kind_toggles(main_window):
    a = box_brush('a', (0, 0, 0), is_mover=False, speed=1.0)
    b = box_brush('b', (100, 0, 0), is_mover=False, speed=1.0)
    main_window.state.brushes.extend([a, b])
    main_window.set_selected_objects([a, b])
    panel = main_window.property_editor
    panel.update_object_prop('speed', 3.0)
    assert b['speed'] == 3.0
    panel.update_object_prop('is_mover', True)
    assert b['is_mover'] is False           # a group holds at most one mover


def test_a_grouped_row_is_painted_in_its_group_tint(main_window):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor
    from editor.scene_hierarchy import GROUP_COLOUR_ROLE, GROUP_TINT_ALPHA

    chairs = _chairs()
    lamp = Light(pos=[0, 100, 0])               # an ungrouped row to compare
    main_window.state.things.extend([lamp] + chairs)
    hierarchy = main_window.scene_hierarchy
    hierarchy.refresh_list()
    tree = hierarchy.tree
    tree.resize(320, 600)
    row = plain_row = None
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        for j in range(top.childCount()):
            child = top.child(j)
            if child.data(0, GROUP_COLOUR_ROLE):
                row = row or child
            elif child.data(0, Qt.UserRole) == ('thing', 0):
                plain_row = child
    assert row is not None and plain_row is not None
    tree.scrollToItem(row)
    image = tree.viewport().grab().toImage()
    rect = tree.visualItemRect(row)
    pixel = image.pixelColor(rect.right() - 20, rect.center().y())
    plain_rect = tree.visualItemRect(plain_row)
    plain = image.pixelColor(plain_rect.right() - 20, plain_rect.center().y())
    tint = QColor(row.data(0, GROUP_COLOUR_ROLE))
    a = GROUP_TINT_ALPHA / 255.0
    expected = [round(a * t + (1 - a) * p) for t, p in
                zip((tint.red(), tint.green(), tint.blue()),
                    (plain.red(), plain.green(), plain.blue()))]
    got = [pixel.red(), pixel.green(), pixel.blue()]
    assert all(abs(g - e) <= 3 for g, e in zip(got, expected)), (got, expected)
