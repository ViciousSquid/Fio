"""Console commands that change the scene: undo, I/O wiring, and caches.

``EditorState.save_state`` is a checkpoint taken *before* an operation (every
editor tool calls it at the start of a gesture). Console commands called it
after mutating, so undo restored the already-changed scene -- a no-op -- and
the user's previous edit took a second undo to reach.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from editor.console_commands import ConsoleCommandHandler  # noqa: E402
from editor.io_system import get_connections  # noqa: E402
from editor.things import Light, Portal  # noqa: E402

pytestmark = pytest.mark.qt


def _light(state, name, **props):
    light = Light(pos=[0.0, 100.0, 0.0])
    light.properties['name'] = name
    light.properties.update(props)
    state.things.append(light)
    return light


@pytest.fixture
def console(main_window):
    handler = ConsoleCommandHandler(main_window)
    return handler, handler.main_window.state



def _find(state, name):
    return next(t for t in state.things if t.properties.get('name') == name)


def test_undo_after_setprop_restores_the_previous_value(console):
    handler, state = console
    _light(state, 'lamp', intensity=1.0)
    state.save_state()
    handler.handle_command("setprop lamp intensity 7")
    assert _find(state, 'lamp').properties['intensity'] == '7'
    assert state.undo()
    assert _find(state, 'lamp').properties['intensity'] == 1.0, (
        "undo after setprop left the new value in place")


def test_undo_after_spawn_removes_the_spawned_entity(console):
    handler, state = console
    before = len(state.things)
    handler.handle_command("spawn light")
    assert len(state.things) == before + 1
    assert state.undo()
    assert len(state.things) == before


def test_connect_attaches_to_a_thing_and_undo_removes_it(console):
    handler, state = console
    src = _light(state, 'src')
    _light(state, 'dst', id='dst-uuid')
    state.save_state()
    handler.handle_command("connect src OnTurnedOff dst TurnOn 0.5")
    conns = get_connections(src)
    assert len(conns) == 1, "connect failed to attach to a Thing"
    assert conns[0].target_id == 'dst-uuid'
    assert conns[0].delay == 0.5
    assert state.undo()
    assert get_connections(_find(state, 'src')) == []


def test_undo_after_portal_link_restores_the_old_target(console):
    handler, state = console
    for name, target in (('a', 'b'), ('b', 'a'), ('c', '')):
        p = Portal(pos=[0.0, 0.0, 0.0])
        p.properties.update(name=name, portal_target=target)
        state.things.append(p)
    state.save_state()
    handler.handle_command("portal_link a c")
    assert _find(state, 'a').properties['portal_target'] == 'c'
    assert state.undo()
    assert _find(state, 'a').properties['portal_target'] == 'b'
