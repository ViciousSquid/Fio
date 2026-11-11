import pytest

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtTest import QTest

from editor.entity_linking import connect_entities
from editor.io_system import get_connections
from editor.things import Monster, PathNode
from plugins.deathmatch.entities import DeathmatchNode

pytestmark = pytest.mark.qt


def test_native_path_link_is_undoable_and_enables_monster_patrol(main_window):
    a = PathNode([0, 90, 0], {'name': 'A'})
    b = PathNode([300, 200, 0], {'name': 'B'})
    monster = Monster([0, 96, 300])
    main_window.state.things = [a, b, monster]
    connect_entities(main_window, a, b)
    assert a.properties['next_node'] == 'B'
    main_window.undo()
    restored_a = next(t for t in main_window.state.things if t.name == 'A')
    assert restored_a.properties['next_node'] == ''
    restored_monster = next(t for t in main_window.state.things if isinstance(t, Monster))
    connect_entities(main_window, restored_monster, restored_a)
    assert restored_monster.properties['patrol']
    assert restored_monster.properties['patrol_target'] == 'A'


def test_deathmatch_links_append_without_duplicates(main_window):
    a = DeathmatchNode([0, 90, 0], {'name': 'A'})
    b = DeathmatchNode([300, 90, 0], {'name': 'B'})
    c = DeathmatchNode([600, 90, 0], {'name': 'C'})
    main_window.state.things = [a, b, c]
    connect_entities(main_window, a, b)
    connect_entities(main_window, a, c)
    assert a.properties['links'] == 'B,C'
    assert connect_entities(main_window, b, a).startswith('Already linked')
    assert b.properties['links'] == ''
    assert connect_entities(main_window, a, b).startswith('Already linked')
    assert a.properties['links'] == 'B,C'


def test_trigger_link_creates_declared_output_input_and_stable_target_id(main_window):
    trigger = dict(pos=[0, 0, 0], size=[64, 64, 64], name='Trigger', is_trigger=True)
    door = dict(pos=[300, 0, 0], size=[64, 128, 64], name='', is_door=True)
    main_window.state.brushes = [trigger, door]
    connect_entities(main_window, trigger, door)
    connection, = get_connections(trigger)
    assert (connection.output_name, connection.input_name) == ('OnTrigger', 'Open')
    assert connection.target_id == door['id']
    assert connection.target_name == door['name']
    assert connect_entities(main_window, trigger, door).startswith('Already linked')
    assert len(get_connections(trigger)) == 1


def test_teleport_trigger_links_to_a_pathnode_destination(main_window):
    trigger = dict(pos=[0, 0, 0], size=[64, 64, 64], name='Teleport',
                   is_trigger=True, trigger_action='teleport')
    target = PathNode([300, 90, 0], {'name': 'Arrival'})
    main_window.state.brushes = [trigger]
    main_window.state.things = [target]
    connect_entities(main_window, trigger, target)
    assert trigger['target_node'] == 'Arrival'
    assert get_connections(trigger) == []


def test_drag_release_links_without_moving_the_entities(main_window, qt_app):
    a = PathNode([-150, 90, 0], {'name': 'A'})
    b = PathNode([150, 90, 0], {'name': 'B'})
    main_window.state.brushes = []
    main_window.state.things = [a, b]
    main_window.state.selected_objects = [a]
    view = main_window.view_top
    view.show()
    main_window.activateWindow()
    view.setFocus()
    qt_app.processEvents()
    QTest.qWait(30)
    start = view.world_to_screen(QPointF(-150, 0)).toPoint()
    end = view.world_to_screen(QPointF(150, 0)).toPoint()
    before = [list(a.pos), list(b.pos)]
    QTest.keyPress(view, Qt.Key_Control)
    try:
        QTest.keyClick(view, Qt.Key_K, Qt.ControlModifier)
    finally:
        QTest.keyRelease(view, Qt.Key_Control)
    qt_app.processEvents()
    assert main_window.entity_link_tool.source is a
    QTest.mousePress(view, Qt.LeftButton, pos=start)
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, Qt.LeftButton, pos=end)
    assert a.properties['next_node'] == 'B'
    assert [list(a.pos), list(b.pos)] == before
    assert main_window.entity_link_tool.source is None


def test_escape_cancels_link_without_mutation(main_window):
    a = PathNode(properties={'name': 'A'})
    main_window.state.things = [a]
    main_window.state.selected_objects = [a]
    tool = main_window.entity_link_tool
    tool.begin()
    QTest.keyClick(main_window.view_top, Qt.Key_Escape)
    assert tool.source is None
    assert a.properties['next_node'] == ''


def test_three_dimensional_view_uses_the_same_drag_link_workflow(main_window, qt_app):
    import glm
    a = PathNode([-150, 90, 0], {'name': 'A'})
    b = PathNode([150, 90, 0], {'name': 'B'})
    main_window.state.brushes = []
    main_window.state.things = [a, b]
    main_window.state.selected_objects = [a]
    view = main_window.view_3d
    main_window.activateWindow()
    view.setFocus()
    qt_app.processEvents()
    view.camera.fov = 60.0
    view.set_editor_camera([0, 90, 1000], yaw=-90.0, pitch=0.0)
    QTest.qWait(50)
    view.projection_matrix = glm.perspective(glm.radians(60.0), view.width()/max(1, view.height()), 1.0, 3000.0)
    view.view_matrix = glm.lookAt(glm.vec3(0, 90, 1000), glm.vec3(0, 90, 0), glm.vec3(0, 1, 0))
    view.logic_thread.camera.get_editor_camera().pos = glm.vec3(0, 90, 1000)
    start, end = view.picking._world_to_screen(a.pos), view.picking._world_to_screen(b.pos)
    main_window.entity_link_tool.begin()
    QTest.mousePress(view, Qt.LeftButton, pos=start)
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, Qt.LeftButton, pos=end)
    assert a.properties['next_node'] == 'B'


def test_self_link_and_deleted_target_are_rejected(main_window):
    a, b = PathNode(), PathNode()
    main_window.state.things = [a]
    with pytest.raises(ValueError):
        connect_entities(main_window, a, a)
    with pytest.raises(ValueError):
        connect_entities(main_window, a, b)


def test_placing_navigation_node_auto_links_in_one_undo_step(main_window):
    from plugins.manager import get_manager, load_plugins
    from tests.helpers.worlds import box_brush
    load_plugins()
    manager = get_manager()
    plugin = manager.find_plugin('deathmatch')
    was_enabled = plugin.enabled
    manager.set_enabled(plugin, True)
    try:
        main_window.state.brushes = [box_brush('Floor', (0, -25, 0), (3000, 50, 3000))]
        a = DeathmatchNode([0, 90, 0], {'name': 'A'})
        main_window.state.things = [a]
        main_window.view_top._place_plugin_entity(plugin, DeathmatchNode, 'Bot Navigation Node', [300, 90, 0])
        assert len(main_window.state.things) == 2
        placed = main_window.state.things[-1]
        assert placed.properties['links'] == 'A'
        assert a.properties['links'] == ''
        main_window.undo()
        assert len(main_window.state.things) == 1
        assert main_window.state.things[0].name == 'A'
    finally:
        manager.set_enabled(plugin, was_enabled)
