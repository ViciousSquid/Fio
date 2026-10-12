"""Linked portals are never one active and one inactive.

Portals are linked when either names the other as its ``portal_target``, and
links chain. Every way a portal's ``active`` can change -- the Enable, Disable
and Toggle inputs, SetTarget, the console, the property panel, loading a map
or a save -- leaves its whole linked group agreeing. A half-active pair drew a
view through a portal nobody could walk through, since a transit needs both
ends active: the Showcase map enabled only one end of its pair.
"""

import pytest

pytest.importorskip("PyQt5", reason="portals are Qt-backed editor entities")

from editor import io_system as io                        # noqa: E402
from editor.console_commands import ConsoleCommandHandler  # noqa: E402
from editor.editor_state import EditorState               # noqa: E402
from editor.io_system import OutputConnection             # noqa: E402
from editor.portal_links import (                         # noqa: E402
    normalize_portal_groups, portal_group, set_group_active, sync_group_to)
from editor.things import Light, Portal                   # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import make_thing               # noqa: E402

pytestmark = pytest.mark.qt


def _portal(name, target="", active=True, x=0.0):
    return make_thing(Portal, name, (x, 0.0, 0.0), portal_target=target, active=active)


def _states(*portals):
    return [p.is_active() for p in portals]


# -- which portals are linked --------------------------------------------------------

def test_a_mutual_pair_is_one_group():
    a, b = _portal("A", "B"), _portal("B", "A")
    assert portal_group(a, [a, b]) == [a, b]
    assert portal_group(b, [a, b]) == [a, b]


def test_a_one_way_link_still_links_both_ends():
    a, b = _portal("A", "B"), _portal("B", "")
    assert portal_group(b, [a, b]) == [a, b]


def test_links_chain_into_one_group():
    a, b, c = _portal("A", "B"), _portal("B", "C"), _portal("C", "")
    other = _portal("D", "E")
    things = [a, b, c, other, make_thing(Light, "lamp", (0, 0, 0))]
    assert portal_group(c, things) == [a, b, c]
    assert portal_group(other, things) == [other]


def test_an_unlinked_portal_is_alone():
    a = _portal("A", "missing")
    assert portal_group(a, [a]) == [a]


# -- the helpers -------------------------------------------------------------------------

def test_setting_one_portal_sets_its_whole_group():
    a, b, c = _portal("A", "B"), _portal("B", "C"), _portal("C", "")
    changed = set_group_active(a, [a, b, c], False)
    assert _states(a, b, c) == [False, False, False]
    assert changed == [b, c]
    assert all(p._fade_target == 0.0 for p in (a, b, c))


def test_a_new_link_takes_the_edited_portals_state():
    a, b = _portal("A", "", active=False), _portal("B", "", active=True)
    a.properties["portal_target"] = "B"
    sync_group_to(a, [a, b])
    assert _states(a, b) == [False, False]


def test_a_group_that_disagrees_is_made_inactive():
    a, b = _portal("A", "B", active=True), _portal("B", "A", active=False)
    c, d = _portal("C", "D", active=True), _portal("D", "C", active=True)
    e = _portal("E", "F", active="false")       # authored as text
    f = _portal("F", "E", active=True)
    changed = normalize_portal_groups([a, b, c, d, e, f])
    assert _states(a, b, c, d, e, f) == [False, False, True, True, False, False]
    assert changed == [a, f]


def test_loading_a_map_whose_pair_disagrees_loads_it_agreeing():
    state = EditorState()
    state.load_from_data({"version": 3, "brushes": [], "things": [
        {"type": "portal", "pos": [0, 0, 0], "properties": {
            "type": "portal", "name": "A", "portal_target": "B", "active": True}},
        {"type": "portal", "pos": [200, 0, 0], "properties": {
            "type": "portal", "name": "B", "portal_target": "A", "active": False}},
    ]}, save_undo=False)
    portals = [t for t in state.things if isinstance(t, Portal)]
    assert _states(*portals) == [False, False]


# -- at play time, through the I/O inputs ----------------------------------------------

@pytest.fixture
def playing():
    made = []

    def _build(*portals):
        state = EditorState()
        state.brushes = []
        state.things = list(portals)
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, -2000.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _send(logic, target, input_name, parameter=""):
    source = make_thing(Light, "switch_%s_%s" % (target, input_name), (0, 0, 0))
    logic.editor_state.things.append(source)
    io.add_connection(source, OutputConnection(
        output_name="OnTrigger", target_name=target, input_name=input_name,
        parameter=parameter))
    logic.world_runtime.build_entity_caches()
    logic.io_manager.fire_output(source, "OnTrigger")


def test_enabling_one_end_of_a_pair_enables_both(playing):
    # The Showcase map: both ends start inactive and one Enable is sent.
    a, b = _portal("Portal_1", "Portal_2", False), _portal("Portal_2", "Portal_1", False, x=300)
    logic = playing(a, b)
    _send(logic, "Portal_1", "Enable")
    assert _states(a, b) == [True, True]
    assert (a._fade_target, b._fade_target) == (1.0, 1.0)


def test_disabling_one_end_disables_both(playing):
    a, b = _portal("A", "B"), _portal("B", "A", x=300)
    logic = playing(a, b)
    _send(logic, "B", "Disable")
    assert _states(a, b) == [False, False]


def test_toggling_one_end_toggles_both(playing):
    a, b = _portal("A", "B"), _portal("B", "A", x=300)
    logic = playing(a, b)
    _send(logic, "A", "Toggle")
    assert _states(a, b) == [False, False]
    _send(logic, "B", "Toggle")
    assert _states(a, b) == [True, True]


def test_only_the_portal_sent_the_input_fires_its_output(playing, monkeypatch):
    a, b = _portal("A", "B", False), _portal("B", "A", False, x=300)
    logic = playing(a, b)
    fired = []
    real = logic.io_manager.fire_output

    def record(entity, output, *args, **kwargs):
        if isinstance(entity, Portal):
            fired.append((entity.properties["name"], output))
        return real(entity, output, *args, **kwargs)

    monkeypatch.setattr(logic.io_manager, "fire_output", record)
    _send(logic, "A", "Enable")
    assert fired == [("A", "OnEnabled")]


def test_retargeting_a_portal_brings_its_new_partner_into_step(playing):
    a = _portal("A", "", True)
    b = _portal("B", "", False, x=300)
    logic = playing(a, b)
    _send(logic, "A", "SetTarget", "B")
    assert _states(a, b) == [True, True]


# -- in the editor ----------------------------------------------------------------------

@pytest.fixture
def console(main_window):
    return ConsoleCommandHandler(main_window), main_window


def _add(window, *portals):
    window.state.things.extend(portals)
    window.view_3d.logic_thread.world_runtime.build_entity_caches()
    return portals


def test_the_console_enables_and_disables_both_ends(console):
    handler, window = console
    a, b = _add(window, _portal("A", "B", False), _portal("B", "A", False, x=300))
    handler.handle_command("portal_enable A")
    assert _states(a, b) == [True, True]
    handler.handle_command("portal_disable B")
    assert _states(a, b) == [False, False]


def test_setprop_on_active_spreads_to_the_linked_portal(console):
    handler, window = console
    a, b = _add(window, _portal("A", "B"), _portal("B", "A", x=300))
    handler.handle_command("setprop A active false")
    assert _states(a, b) == [False, False]


def test_linking_from_the_console_brings_the_target_into_step(console):
    handler, window = console
    a, b = _add(window, _portal("A", "", False), _portal("B", "", True, x=300))
    handler.handle_command("portal_link A B")
    assert a.properties["portal_target"] == "B"
    assert _states(a, b) == [False, False]


def test_the_property_panels_active_box_switches_the_pair(main_window):
    window = main_window
    a, b = _add(window, _portal("A", "B"), _portal("B", "A", x=300))
    window.set_selected_objects([a])
    window.property_editor.update_object_prop("active", False)
    assert _states(a, b) == [False, False]


def test_choosing_a_portal_target_in_the_panel_brings_it_into_step(main_window):
    from editor.property_editor import ClickableComboBox
    window = main_window
    a, b = _add(window, _portal("A", "", False), _portal("B", "", True, x=300))
    window.set_selected_objects([a])
    combo = next(c for c in window.property_editor.findChildren(ClickableComboBox)
                 if c.count() and c.itemText(0) == "(none)")
    combo.setCurrentIndex(combo.findText("B"))
    assert a.properties["portal_target"] == "B"
    assert _states(a, b) == [False, False]


def test_a_rename_that_completes_a_link_brings_the_pair_into_step(main_window):
    window = main_window
    a, b = _add(window, _portal("A", "Exit", True), _portal("B", "", False, x=300))
    window.set_selected_objects([b])
    window.property_editor.update_object_prop("name", "Exit")
    assert _states(a, b) == [False, False]


def test_restoring_a_save_whose_pair_disagrees_restores_it_agreeing(playing):
    from engine import savegame
    a, b = _portal("A", "B"), _portal("B", "A", x=300)
    logic = playing(a, b)
    b.properties["active"] = False          # as a save from before the rule held it
    data = savegame.build_snapshot(logic)
    b.properties["active"] = True

    savegame.restore_snapshot(logic, data)

    assert _states(a, b) == [False, False]
