"""Real doors, platforms, Player controllers and placement-time neighbour links."""
from pathlib import Path

import pytest

from editor.editor_state import EditorState
from editor.things import PathNode
from engine.entity_table import sprite_candidates
from plugins.deathmatch.authoring import connect_neighbours
from plugins.deathmatch.entities import DeathmatchNode
from plugins.deathmatch.navigation import Navigation, MoverTraversal, Waypoint
from tests.helpers.worlds import box_brush
from plugins.deathmatch.tests.test_deathmatch import arena, place, walls

pytestmark = pytest.mark.qt


def graph(session, nodes):
    session.nav = Navigation(nodes, project=session.navigation_point,
        surfaces=session.nav_world.surfaces, transition=session.nav_world.transfer)
    return session.nav


def test_closed_and_closing_door_invalidate_an_existing_route(arena):
    logic, session, human, bot = arena
    door = box_brush('Gate', (0, 80, 150), (120, 160, 32), is_door=True,
                     door_speed=200, door_distance=200, door_direction='up')
    walls(logic, box_brush('West wall', (-280, 120, 150), (440, 240, 40)),
          box_brush('East wall', (280, 120, 150), (440, 240, 40)), door)
    logic.mover_runtime._init_doors()
    nodes = [DeathmatchNode((0, 90, 0), {'name': 'Before', 'links': 'After'}),
             DeathmatchNode((0, 90, 300), {'name': 'After'})]
    nav = graph(session, nodes)
    assert nav.route((0, 0, 0), (0, 0, 300), session.walkable) == []
    place(bot, [0, 0, 0])
    assert session.plan_route(bot, (0, 0, 300)) == []
    assert 'door' in bot.navigation_reason
    index = logic.editor_state.brushes.index(door)
    logic.mover_runtime._trigger_door_open(index, door)
    logic.mover_runtime._update_doors(0.1)
    assert nav.route((0, 0, 0), (0, 0, 300), session.walkable) == []
    logic.mover_runtime._update_doors(1)
    route = nav.route((0, 0, 0), (0, 0, 300), session.walkable)
    assert route
    place(bot, [0, 0, 0])
    logic.mover_runtime.door_states[index]['state'] = 'closing'
    logic.mover_runtime._update_doors(0.6)
    assert session.follow_route(bot, route) is None
    assert not route
    assert 'door' in bot.navigation_reason


def lift_world(arena, path=False):
    logic, session, human, bot = arena
    lift = box_brush('Lift', (0, -10, 0), (160, 20, 160), is_mover=True,
                     start_on=True, direction=[0, 1, 0], distance=300, speed=128)
    if path:
        lift['path_target'] = 'Lift bottom'
        logic.editor_state.things.extend([
            PathNode((0, -10, 0), {'name': 'Lift bottom', 'next_node': 'Lift top', 'wait_time': 0.5}),
            PathNode((0, 290, 0), {'name': 'Lift top', 'next_node': 'Lift bottom', 'wait_time': 1.0})])
    walls(logic, box_brush('Upper landing', (220, 290, 0), (280, 20, 320)), lift)
    logic.mover_runtime._init_movers()
    logic.mover_runtime._init_doors()
    graph(session, [DeathmatchNode((-160, 90, 0), {'name': 'Low', 'links': 'High', 'radius': 24}),
                    DeathmatchNode((220, 390, 0), {'name': 'High', 'radius': 24})])
    place(human, [-1400, 0, -1400])
    bot.think_at = bot.fire_at = bot.memory_until = 10000
    return logic, session, bot, lift


@pytest.mark.parametrize('path', [False, True])
@pytest.mark.parametrize('down', [False, True])
def test_bot_boards_rides_and_leaves_a_real_platform(arena, path, down):
    logic, session, bot, lift = lift_world(arena, path)
    start, target = ((220, 300, 0), (-160, 0, 0)) if down else ((-160, 0, 0), (220, 300, 0))
    place(bot, start)
    bot.actor.on_ground = True
    bot.actor.ground_object = logic.editor_state.brushes[1 if down else 0]
    bot.last_seen = target
    before = list(lift['pos'])
    route = session.nav.route(session.feet(bot), bot.last_seen, session.walkable)
    assert any(isinstance(point, MoverTraversal) for point in route)
    assert lift['pos'] == before  # Prediction must never move the actual platform.
    aboard = False
    modes = set()
    for _ in range(1800):
        logic._tick_play_mode(1/60)
        modes.add(bot.mode)
        aboard |= bot.actor.ground_object is lift and bot.actor.on_ground
        reached = (bot.actor.pos.x < -110 and abs(session.feet(bot)[1]) < 1) if down else (session.feet(bot)[1] >= 299 and bot.actor.pos.x > 140)
        if reached:
            break
    assert aboard, (bot.mode, session.feet(bot), bot.navigation_reason)
    assert 'riding mover' in modes
    assert reached, (bot.mode, session.feet(bot), bot.navigation_reason)
    assert session.feet(bot)[1] == pytest.approx(target[1], abs=1)
    assert bot.actor.ground_object is not lift


def test_clear_air_over_a_gap_is_not_a_walking_connection(arena):
    logic, session, human, bot = arena
    logic.editor_state.brushes = [box_brush('Left', (-150, -10, 0), (200, 20, 300)),
                                box_brush('Right', (150, -10, 0), (200, 20, 300))]
    logic.collision_runtime.refresh_collision_brushes_cache()
    logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes)
    assert session.clear((-100, 0, 0), (100, 0, 0))
    assert not session.walkable((-100, 0, 0), (100, 0, 0))


def test_neighbours_auto_connect_once_and_preserve_manual_edits():
    state = EditorState()
    state.brushes = [box_brush('Floor', (0, -25, 0), (3000, 50, 3000))]
    a = DeathmatchNode((0, 90, 0), {'name': 'A'})
    b = DeathmatchNode((300, 5000, 0), {'name': 'B'})
    state.things = [a, b]
    assert connect_neighbours(b, state) == ['A']
    b.properties['links'] = ''
    c = DeathmatchNode((150, -500, 0), {'name': 'C'})
    state.things.append(c)
    assert set(connect_neighbours(c, state)) == {'A', 'B'}
    assert b.properties['links'] == ''
    assert a.properties['links'] == ''


def test_auto_links_ignore_door_state_but_do_not_go_through_static_walls():
    state = EditorState()
    state.brushes = [box_brush('Floor', (0, -25, 0), (2000, 50, 2000)),
                    box_brush('Door', (0, 100, 150), (1000, 200, 32), is_door=True)]
    a = DeathmatchNode((0, 90, 0), {'name': 'A'})
    b = DeathmatchNode((0, 90, 300), {'name': 'B'})
    state.things = [a, b]
    assert connect_neighbours(b, state) == ['A']
    state.brushes[1]['is_door'] = False
    b.properties['links'] = ''
    assert connect_neighbours(b, state) == []


def test_bot_node_has_its_own_teal_marker_and_editor_recipe(qt_app):
    node = DeathmatchNode()
    assert Path(node.pixmap_path).name == 'navigation_node.png'
    assert node.get_pixmap() is not None and not node.get_pixmap().isNull()
    assert PathNode.pixmap_path is None
    assert sprite_candidates(node)[0][1] == 'navigation_node.png'


def test_platform_route_rejected_when_rider_cannot_clear_the_ceiling(arena):
    logic, session, bot, lift = lift_world(arena)
    logic.editor_state.brushes.append(box_brush('Ceiling', (0, 200, 0), (160, 20, 160)))
    logic.collision_runtime.refresh_collision_brushes_cache()
    assert session.nav_world.transfer((-160, 0, 0), (220, 300, 0)) is None


def test_unavailable_mover_cancels_an_active_trip(arena):
    logic, session, bot, lift = lift_world(arena)
    place(bot, [-160, 0, 0])
    route = session.nav.route(session.feet(bot), (220, 300, 0), session.walkable)
    session.follow_route(bot, route)
    assert bot.traversal is not None
    logic.editor_state.brushes.remove(lift)
    assert session.traversal_target(bot) is None
    assert bot.traversal is None and not route
    assert 'replanning' in bot.navigation_reason


def test_reaching_a_landing_node_starts_waiting_for_an_absent_platform(arena):
    logic, session, bot, lift = lift_world(arena)
    logic.editor_state.brushes[0].update(pos=[-600, -25, 0], size=[1040, 50, 4000])
    lift['pos'] = [0, 290, 0]
    place(bot, [-160, 0, 0])
    action = session.nav_world.transfer(session.feet(bot), (220, 300, 0))
    assert action is not None
    assert not session.walkable(session.feet(bot), action)
    route = [Waypoint(session.feet(bot)), action, action.target]
    assert session.follow_route(bot, route) is None
    assert route[0] is action and bot.traversal is action
    assert bot.mode == 'waiting for mover'


def test_auto_link_opt_out_and_search_range():
    state = EditorState()
    state.brushes = [box_brush('Floor', (0, -25, 0), (3000, 50, 3000))]
    a = DeathmatchNode((0, 90, 0), {'name': 'A'})
    b = DeathmatchNode((300, 90, 0), {'name': 'B', 'auto_link': False})
    state.things = [a, b]
    assert not connect_neighbours(b, state)
    b.properties.update(auto_link=True, link_range=200)
    assert not connect_neighbours(b, state)
    b.properties['link_range'] = 400
    assert connect_neighbours(b, state) == ['A']


def test_bot_rides_a_horizontal_platform_across_a_gap(arena):
    logic, session, human, bot = arena
    lift = box_brush('Ferry', (-100, -10, 0), (160, 20, 160), is_mover=True,
                     start_on=True, direction=[1, 0, 0], distance=200, speed=80)
    logic.editor_state.brushes = [box_brush('Left', (-400, -25, 0), (600, 50, 500)),
                                box_brush('Right', (400, -25, 0), (600, 50, 500)), lift]
    logic.collision_runtime.refresh_collision_brushes_cache()
    logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes)
    logic.mover_runtime._init_movers()
    graph(session, [DeathmatchNode((-220, 90, 0), {'name': 'Left', 'links': 'Right'}),
                    DeathmatchNode((220, 90, 0), {'name': 'Right'})])
    place(human, [-600, 0, 200])
    place(bot, [-220, 0, 0])
    bot.actor.on_ground, bot.actor.ground_object = True, logic.editor_state.brushes[0]
    bot.think_at = bot.fire_at = bot.memory_until = 10000
    bot.last_seen = (220, 0, 0)
    aboard = False
    for _ in range(1800):
        logic._tick_play_mode(1/60)
        aboard |= bot.actor.ground_object is lift
        if bot.actor.pos.x > 200:
            break
    assert aboard
    assert bot.actor.pos.x > 200, (bot.mode, session.feet(bot), bot.navigation_reason)
    assert session.feet(bot)[1] == pytest.approx(0, abs=1)
    assert bot.actor.ground_object is not lift
