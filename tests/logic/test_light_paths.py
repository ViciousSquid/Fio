"""Lights travelling along path-node chains in Play (engine.light_paths)."""

import math

import pytest

from engine.light_paths import LightPaths, follows_a_path, path_speed

pytestmark = pytest.mark.qt


# -- the runtime, against stand-ins -------------------------------------------------

class _Node:
    def __init__(self, name, pos, next_node='', wait=0.0, speed=1.0):
        self.properties = {'name': name}
        self.pos = list(pos)
        self._next, self._wait, self._speed = next_node, wait, speed

    def get_next_node_name(self):
        return self._next

    def get_wait_time(self):
        return self._wait

    def get_speed(self):
        return self._speed


class _Light:
    def __init__(self, pos, **props):
        self.pos = list(pos)
        self.properties = {'type': 'light', 'name': 'lamp', 'path_speed': 100.0,
                           'path_start_on': True}
        self.properties.update(props)


class _IO:
    def __init__(self):
        self.fired = []

    def fire_output(self, entity, output, **kwargs):
        self.fired.append((output, kwargs.get('value')))


def _paths(light, *nodes):
    by_name = {n.properties['name']: n for n in nodes}
    logic = type('L', (), {})()
    logic.editor_state = type('S', (), {'things': [light, *nodes]})()
    logic.world_runtime = type('W', (), {'find_path_node_by_name':
                                         staticmethod(lambda name: by_name.get(name))})()
    logic.io_manager = _IO()
    runtime = LightPaths(logic, light_type=_Light)
    runtime.start()
    return runtime, logic.io_manager


def _run(runtime, seconds, step=1.0 / 60.0):
    for _ in range(int(round(seconds / step))):
        runtime.update(step)


def test_a_light_glides_to_its_first_node_at_its_speed():
    light = _Light((0, 100, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (200, 100, 0)))
    _run(runtime, 1.0)
    assert light.pos == pytest.approx([100.0, 100.0, 0.0], abs=1e-6)    # constant speed
    _run(runtime, 1.5)
    assert light.pos == pytest.approx([200.0, 100.0, 0.0])
    assert io.fired == [('OnPathNodeReached', 'a'), ('OnPathEnd', None)]


def test_a_chain_that_loops_back_goes_round_for_ever():
    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (100, 0, 0), 'b'), _Node('b', (100, 0, 100), 'c'),
                         _Node('c', (0, 0, 100), 'a'))
    lap = (100 + 100 + math.hypot(100, 100)) / 100.0           # seconds round the triangle
    _run(runtime, 1.0 + lap * 4 + 0.1)            # out, then round it 4 times
    reached = [value for name, value in io.fired if name == 'OnPathNodeReached']
    assert len(reached) >= 12 and ('OnPathEnd', None) not in io.fired
    assert reached[:4] == ['a', 'b', 'c', 'a']


def test_a_node_s_wait_time_and_speed_apply():
    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (100, 0, 0), 'b', wait=1.0),
                         _Node('b', (100, 0, 200), speed=2.0))
    _run(runtime, 1.5)
    assert light.pos == pytest.approx([100.0, 0.0, 0.0])               # waiting at a
    _run(runtime, 0.5)
    assert light.pos == pytest.approx([100.0, 0.0, 0.0], abs=1e-6)
    _run(runtime, 0.5)                                                 # twice as fast to b
    assert light.pos == pytest.approx([100.0, 0.0, 100.0], abs=2.0)


def test_one_long_tick_spends_its_time_across_several_nodes():
    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (10, 0, 0), 'b'), _Node('b', (20, 0, 0), 'c'),
                         _Node('c', (30, 0, 0)))
    runtime.update(0.25)                                               # 25 units at 100 u/s
    assert light.pos == pytest.approx([25.0, 0.0, 0.0])


def test_a_path_that_loops_where_the_light_stands_arrives_once_a_tick():
    """A node on the light, whose next node is itself (or two nodes in one
    place that name each other), is a loop no time is spent in. It used to
    arrive -- and fire OnPathNodeReached -- 64 times every tick."""
    light = _Light((50, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (50, 0, 0), 'a'))
    _run(runtime, 1.0)
    assert len(io.fired) == 60 and set(io.fired) == {('OnPathNodeReached', 'a')}
    assert light.pos == [50.0, 0.0, 0.0]

    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (0, 0, 0), 'b'), _Node('b', (0, 0, 0), 'a'))
    _run(runtime, 1.0)
    assert len(io.fired) == 60


def test_a_node_in_the_same_place_as_the_last_does_not_hold_the_journey_up():
    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (10, 0, 0), 'b'), _Node('b', (10, 0, 0), 'c'),
                         _Node('c', (30, 0, 0)))
    _run(runtime, 1.0)
    assert light.pos == pytest.approx([30.0, 0.0, 0.0])
    assert io.fired == [('OnPathNodeReached', 'a'), ('OnPathNodeReached', 'b'),
                        ('OnPathNodeReached', 'c'), ('OnPathEnd', None)]


def test_it_waits_for_start_path_and_pauses_on_stop_path():
    light = _Light((0, 0, 0), path_target='a', path_start_on=False)
    runtime, io = _paths(light, _Node('a', (300, 0, 0)))
    _run(runtime, 1.0)
    assert light.pos == [0, 0, 0]
    assert runtime.start_path(light)
    _run(runtime, 1.0)
    assert light.pos == pytest.approx([100.0, 0.0, 0.0], abs=1e-6)
    runtime.toggle_path(light)                                         # pause
    _run(runtime, 1.0)
    assert light.pos == pytest.approx([100.0, 0.0, 0.0], abs=1e-6)
    runtime.toggle_path(light)
    _run(runtime, 1.0)
    assert light.pos == pytest.approx([200.0, 0.0, 0.0], abs=1e-6)


def test_stop_puts_the_light_back_where_it_was_placed():
    light = _Light((5, 6, 7), path_target='a')
    runtime, io = _paths(light, _Node('a', (300, 0, 0)))
    _run(runtime, 1.0)
    runtime.stop()
    assert light.pos == [5.0, 6.0, 7.0]


def test_a_play_restarted_without_a_stop_still_stops_where_it_was_placed():
    light = _Light((5, 6, 7), path_target='a')
    runtime, io = _paths(light, _Node('a', (300, 0, 0)))
    _run(runtime, 1.0)
    runtime.start()                                    # restarted, no Stop
    assert light.pos == [5.0, 6.0, 7.0]
    _run(runtime, 1.0)
    runtime.stop()
    assert light.pos == [5.0, 6.0, 7.0]


def test_stop_path_from_a_node_s_arrival_stops_it_at_that_node():
    light = _Light((0, 0, 0), path_target='a')
    runtime, io = _paths(light, _Node('a', (10, 0, 0), 'b'), _Node('b', (30, 0, 0)))
    fire = io.fire_output

    def stop_on_arrival(entity, output, **kwargs):
        fire(entity, output, **kwargs)
        if output == 'OnPathNodeReached':
            runtime.stop_path(entity)

    io.fire_output = stop_on_arrival
    runtime.update(0.25)                               # would reach x=25
    assert light.pos == pytest.approx([10.0, 0.0, 0.0])
    io.fire_output = fire
    assert runtime.start_path(light)
    runtime.update(0.1)                                # on to b, no second arrival at a
    assert light.pos == pytest.approx([20.0, 0.0, 0.0])
    assert io.fired == [('OnPathNodeReached', 'a')]


def test_a_saved_journey_carries_on_from_where_it_was_saved():
    light = _Light((0, 0, 0), path_target='a', id='lamp-1')
    runtime, io = _paths(light, _Node('a', (100, 0, 0), 'b'), _Node('b', (100, 0, 100)))
    _run(runtime, 0.5)
    saved = runtime.snapshot()
    saved_pos = list(light.pos)
    _run(runtime, 1.0)                                 # on past a, towards b
    light.pos = list(saved_pos)                        # the load's overlay
    runtime.restore(saved)
    runtime.update(1.0 / 60.0)
    assert light.pos[0] == pytest.approx(saved_pos[0] + 100.0 / 60.0)
    assert light.pos[2] == pytest.approx(0.0)
    runtime.stop()
    assert light.pos == [0.0, 0.0, 0.0]                # home is still as placed


def test_a_light_attached_to_a_mover_ignores_its_path():
    light = _Light((0, 0, 0), path_target='a', parent_mover='lift')
    assert not follows_a_path(light)
    runtime, io = _paths(light, _Node('a', (300, 0, 0)))
    _run(runtime, 1.0)
    assert light.pos == [0, 0, 0]


def test_a_missing_node_or_a_bad_speed_stays_put():
    light = _Light((0, 0, 0), path_target='nowhere')
    runtime, io = _paths(light)
    _run(runtime, 1.0)
    assert light.pos == [0, 0, 0] and not runtime.journeys
    assert path_speed({'path_speed': 'fast'}) == 64.0
    assert path_speed({'path_speed': float('nan')}) == 64.0
    assert path_speed({'path_speed': -5}) == 0.0


# -- in Play, in the real editor ---------------------------------------------------------

def _level():
    floor = {"id": "floor", "name": "floor", "pos": [0, -8, 0], "size": [2048, 16, 2048]}
    lift = {"id": "lift", "name": "lift", "pos": [0, 100, 0], "size": [128, 16, 128],
            "is_mover": True, "start_on": True, "direction": [0, 1, 0],
            "distance": 200.0, "speed": 100.0, "brush_group": "lift"}
    def node(name, pos, nxt=''):
        return {"type": "path_node", "pos": list(pos),
                "properties": {"type": "path_node", "name": name, "id": "pn-" + name,
                               "next_node": nxt, "wait_time": 0.0, "speed": 1.0}}
    def light(name, pos, **props):
        p = {"type": "light", "name": name, "id": "light-" + name}
        p.update(props)
        return {"type": "light", "pos": list(pos), "properties": p}
    return {"version": 5, "brushes": [floor, lift],
            "things": [{"type": "playerstart", "pos": [0, 40, 600],
                        "properties": {"type": "player_start", "name": "start",
                                       "id": "ps-1", "primary": True}},
                       node("n1", (300, 150, 0), "n2"), node("n2", (300, 150, 300), "n1"),
                       light("walker", (0, 150, 0), path_target="n1", path_speed=120.0),
                       # In the lift's group, and on a path: the path wins.
                       light("rider", (0, 150, 0), path_target="n1", brush_group="lift"),
                       light("parented", (0, 150, 0), path_target="n1",
                             parent_mover="lift")]}


def _thing(session, name):
    return next(t for t in session.state.things if t.properties.get("name") == name)


def test_in_play_a_light_walks_its_path_and_stop_puts_it_back(fio_session, monkeypatch):
    session = fio_session(_level())
    monkeypatch.setattr(session.window, "_write_autosave", lambda: None)
    walker, rider = _thing(session, "walker"), _thing(session, "rider")
    parented = _thing(session, "parented")
    session.start_play()
    session.step(60)                                         # one second
    seconds = 60 * session.logic.TICK_DURATION
    assert walker.pos[0] == pytest.approx(120.0 * seconds, abs=2.0)
    assert walker.pos[2] == pytest.approx(0.0)
    # The group's mover does not drag a light that is on a path.
    assert rider.pos[1] == pytest.approx(150.0)
    assert rider.pos[0] > 0.0
    # A light parented to the lift rides the lift, not its path.
    assert parented.pos[0] == pytest.approx(0.0)
    assert parented.pos[1] > 150.0
    session.stop_play()
    for light in (walker, rider, parented):
        assert list(light.pos) == pytest.approx([0.0, 150.0, 0.0])


def test_the_path_inputs_are_declared_and_handled():
    from editor.io_handlers import register_all_input_handlers
    from editor.io_system import IOManager, get_input_names, get_output_names
    for name in ('StartPath', 'StopPath', 'TogglePath'):
        assert name in get_input_names('light')
    for name in ('OnPathNodeReached', 'OnPathEnd'):
        assert name in get_output_names('light')
    mgr = IOManager()
    register_all_input_handlers(mgr)
    for name in ('startpath', 'stoppath', 'togglepath'):
        assert ('light', name) in mgr._input_handlers


def test_a_new_light_has_no_path():
    from editor.things import Light
    light = Light(pos=[0, 0, 0])
    assert light.properties['path_target'] == ''
    assert not follows_a_path(light)


def test_the_light_panel_sets_the_path(main_window):
    from PyQt5.QtWidgets import QComboBox, QLabel
    from editor.things import Light, PathNode
    light = Light(pos=[0, 0, 0])
    node = PathNode(pos=[100, 0, 0], properties={'name': 'gate'})
    main_window.state.things[:] = [light, node]
    main_window.set_selected_objects([light])
    panel = main_window.property_editor
    panel.set_object(light)
    labels = {w.text() for w in panel.findChildren(QLabel)}
    assert {"Path Start:", "Path Speed:"} <= labels
    combo = next(c for c in panel.findChildren(QComboBox)
                 if c.findText('gate') >= 0 and c.itemText(0) == '(none)')
    combo.setCurrentText('gate')
    assert light.properties['path_target'] == 'gate'
    combo.setCurrentText('(none)')
    assert light.properties['path_target'] == ''
