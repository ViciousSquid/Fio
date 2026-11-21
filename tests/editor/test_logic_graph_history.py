"""The Logic Graph window across undo/redo and map loads (audit S11, M3).

S11: undo and redo rebuild every entity, and the modeless graph kept the old
objects. After one undo none of its nodes pointed at the scene; Apply then
wrote the connections into the detached copies, said "Applied", and the map
kept its old wiring. The graph now follows the history (rebuilt when it has
nothing unapplied, re-pointed by stable id when it has), and Apply finds
every entity in the live scene before writing.

M3: a hidden graph window was closed on each map load but never deleted, so
one window holding the previous map's whole graph piled up per load.
"""

import gc

import pytest
from PyQt5.QtCore import QEvent

from editor.io_system import get_connections
from editor.things import Light

pytestmark = pytest.mark.qt


@pytest.fixture
def lamps(main_window):
    a = Light(pos=[0, 0, 0])
    b = Light(pos=[128, 0, 0])
    a.properties.update(name='LampA', id='lamp-a')
    b.properties.update(name='LampB', id='lamp-b')
    main_window.state.things.extend([a, b])
    main_window.update_all_ui()
    return a, b


def _graph(main_window):
    main_window.open_logic_graph()
    return main_window._logic_graph_win


def _live(main_window):
    return {id(o) for o in list(main_window.state.things) + list(main_window.state.brushes)}


def test_a_clean_graph_is_rebuilt_on_undo(main_window, lamps):
    window = _graph(main_window)
    main_window.save_state()
    lamps[0].properties['intensity'] = 3.0
    main_window.undo()
    nodes = list(window.graph_scene._nodes.values())
    assert nodes and all(id(n.entity) in _live(main_window) for n in nodes)


def test_unapplied_edits_survive_an_undo_and_apply_to_the_live_map(main_window, lamps):
    window = _graph(main_window)
    scene = window.graph_scene
    assert scene.add_connection_by_name('LampA', 'OnTurnedOn', 'LampB', 'TurnOn')
    assert window._dirty
    main_window.save_state()                     # an unrelated edit, undone
    lamps[1].properties['intensity'] = 2.0
    main_window.undo()
    assert all(id(n.entity) in _live(main_window)
               for n in window.graph_scene._nodes.values())

    window._apply()
    live_a = main_window.state.find_entity_by_id('lamp-a')
    assert live_a is not lamps[0]
    assert [c.input_name for c in get_connections(live_a)] == ['TurnOn']
    assert get_connections(lamps[0]) == [], "Apply wrote into the detached copy"
    # One undo takes the Apply back, on the live map.
    main_window.undo()
    assert get_connections(main_window.state.find_entity_by_id('lamp-a')) == []


def test_apply_skips_an_entity_that_is_no_longer_in_the_map(main_window, lamps):
    window = _graph(main_window)
    window.graph_scene.add_connection_by_name('LampA', 'OnTurnedOn', 'LampB', 'TurnOn')
    main_window.state.things.remove(lamps[0])     # gone, and not undone
    main_window.invalidate_entity_caches()
    window._apply()
    assert window.graph_scene.skipped_entities == 1
    assert get_connections(lamps[0]) == []


def test_a_hidden_graph_window_is_deleted_on_map_load(main_window, lamps, qt_app):
    from editor.logic_graph_widget import LogicGraphWindow
    for _ in range(3):
        window = _graph(main_window)
        window.close()
        main_window._refresh_logic_graph()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        gc.collect()
    assert main_window.findChildren(LogicGraphWindow) == []
