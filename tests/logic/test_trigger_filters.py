"""Trigger detection: activator filters and the per-trigger poll scheduler."""
import math
from types import SimpleNamespace

import glm
import pytest

from engine.logic_thread import LogicThread


def _logic(player_pos=(5, 5, 5), props=(), monsters=(), filters=None,
           poll_interval=None):
    logic = LogicThread.__new__(LogicThread)
    logic._reset_trigger_state()
    logic.player = SimpleNamespace(
        pos=glm.vec3(*player_pos), angle=0.0, velocity=glm.vec3(0.0)
    )
    logic._prop_things = list(props)
    logic._prop_by_id = {id(t): t for t in logic._prop_things}
    logic._monster_things = list(monsters)
    logic._monster_by_id = {id(t): t for t in logic._monster_things}
    brush = {
        'id': 'trigger_1',
        'pos': [0, 0, 0],
        'size': [20, 20, 20],
        'is_trigger': True,
        'trigger_type': 'Multiple',
        'trigger_activation': 'touch',
        'trigger_action': 'target',
    }
    if filters is not None:
        brush['trigger_filters'] = filters
    if poll_interval is not None:
        brush['trigger_poll_interval'] = poll_interval
    logic._trigger_brushes = [(1, brush)]
    logic._trigger_brush_by_bid = dict(logic._trigger_brushes)
    logic._refresh_use_triggers()
    logic.fired_once_triggers = set()
    logic.hurt_trigger_timers = {}
    logic.io_manager = None
    logic.plugins = None
    logic.current_hud_message = ''
    logic._events = []

    def on_enter(brush, trigger_id, activator_type='player', activator_entity=None):
        logic._events.append(('enter', activator_type))

    def on_exit(brush, trigger_id, activator_type='player', activator_entity=None):
        logic._events.append(('exit', activator_type))

    logic._on_trigger_enter = on_enter
    logic._on_trigger_exit = on_exit
    logic._process_hurt_trigger = lambda brush, trigger_id: None
    return logic


def _thing(kind, x=5, y=5, z=5):
    return SimpleNamespace(pos=[x, y, z], properties={'type': kind, 'disabled': False})


def test_default_filter_is_player_only_and_unchanged_contacts_are_silent():
    logic = _logic(props=[_thing('prop')], monsters=[_thing('monster')])

    logic._poll_triggers()
    logic._poll_triggers()

    assert logic._events == [('enter', 'player')]
    assert logic.player_in_triggers == {1}


def test_filters_select_props_and_monsters_without_player():
    logic = _logic(props=[_thing('prop')], monsters=[_thing('monster')],
                   filters=['props', 'monsters'])

    logic._poll_triggers()

    assert set(logic._events) == {('enter', 'props'), ('enter', 'monsters')}
    assert logic.player_in_triggers == set()


def test_enter_exit_reentry_is_tracked_per_entity():
    prop = _thing('prop')
    logic = _logic(props=[prop], filters=['props'])

    for pos in ([5, 5, 5], [50, 50, 50], [5, 5, 5]):
        prop.pos = pos
        logic._poll_triggers()

    assert logic._events == [('enter', 'props'), ('exit', 'props'), ('enter', 'props')]


@pytest.mark.parametrize('interval, polls_per_second', [
    (1.0, 1), (0.5, 2), (0.25, 4), (None, 1), ('bogus', 1),
])
def test_scheduler_polls_each_trigger_at_its_own_rate(interval, polls_per_second):
    logic = _logic(poll_interval=interval)
    calls = []
    original = logic._poll_triggers

    def tracked(use_key_pressed=False, trigger_ids=None):
        calls.append(trigger_ids)
        original(use_key_pressed=use_key_pressed, trigger_ids=trigger_ids)

    logic._poll_triggers = tracked

    for _ in range(60):  # one second at the 60 Hz logic rate
        logic._handle_triggers(False, 1.0 / 60.0)

    assert len(calls) == polls_per_second
    assert logic.player_in_triggers == {1}
    assert logic._events == [('enter', 'player')]


def test_reset_clears_occupancy_in_place():
    logic = _logic()
    logic._poll_triggers()
    held = logic.player_in_triggers

    logic._reset_trigger_state()

    assert held is logic.player_in_triggers and held == set()
    assert logic._trigger_contacts == {}
    logic._poll_triggers()  # re-entry after reset fires again
    assert logic._events == [('enter', 'player'), ('enter', 'player')]


# ---------------------------------------------------------------------------
# HUD prompt ownership
#
# _handle_triggers runs after _handle_interactions and PropSession.tick in
# LogicThread._tick_play_mode, so whatever it leaves in current_hud_message is
# what the frame publishes. It may add a use-trigger prompt; it must never
# clear a prompt an earlier stage set, or doors, pickups, level changers and
# carried props all go silent.
# ---------------------------------------------------------------------------

def test_empty_trigger_prompt_does_not_clear_an_interaction_prompt():
    """A door/pickup/prop prompt survives a tick with no use trigger in range."""
    logic = _logic()
    logic._trigger_use_prompt = ""
    logic.current_hud_message = "NEED: Red Key"

    logic._handle_triggers(False, 1.0 / 60.0)

    assert logic.current_hud_message == "NEED: Red Key"


def _use_trigger(logic, label="Activate", radius=96.0, **brush_overrides):
    """Turn the helper's trigger into a use-activated one."""
    _, brush = logic._trigger_brushes[0]
    brush['trigger_activation'] = 'use'
    brush['use_radius'] = radius
    brush['use_label'] = label
    brush.update(brush_overrides)
    logic._refresh_use_triggers()
    return brush


def test_use_trigger_prompt_still_wins_the_hud_line():
    """A real in-range use trigger overrides an interaction prompt."""
    logic = _logic(player_pos=(0, 0, 60))
    _use_trigger(logic)
    logic.player.angle = math.pi          # facing the trigger at the origin
    logic.current_hud_message = "[E] Open"

    logic._handle_triggers(False, 1.0 / 60.0)

    assert logic.current_hud_message == "[E] Activate"


def test_use_prompt_appears_and_clears_within_one_tick():
    """The prompt tracks the live player, not the last poll.

    Sampling it at the poll cadence made it up to a poll interval late in both
    directions. Nothing here advances time by more than a single frame.
    """
    logic = _logic(player_pos=(0, 0, 60))
    _use_trigger(logic, radius=96.0)
    logic.player.angle = math.pi          # facing the trigger at the origin

    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == "[E] Activate"

    logic.player.pos = glm.vec3(0.0, 0.0, 5000.0)      # walk away
    logic.current_hud_message = ''
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == ''

    logic.player.pos = glm.vec3(0.0, 0.0, 60.0)         # and back
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == "[E] Activate"


def test_use_prompt_clears_when_the_player_turns_away():
    logic = _logic(player_pos=(0, 0, 60))
    _use_trigger(logic)

    logic.player.angle = math.pi          # facing the trigger at the origin
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == "[E] Activate"

    logic.player.angle = 0.0              # turned around, same spot
    logic.current_hud_message = ''
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == ''


def test_use_radius_is_a_sphere_not_a_box():
    """2.4.2 tested glm.distance < use_radius.

    The batched broad phase bounds a use trigger by an axis-aligned box of the
    same radius, which is what a vectorised AABB pass can do cheaply. Taking
    that box as the answer let a button be used from up to sqrt(3) times its
    authored radius on the diagonal.
    """
    radius = 100.0
    inside_box_outside_sphere = (80.0, 0.0, 80.0)   # |d| = 113 > 100
    logic = _logic(player_pos=inside_box_outside_sphere)
    _use_trigger(logic, radius=radius)
    logic.player.angle = math.pi + math.pi / 4      # facing the origin

    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == ''

    logic.player.pos = glm.vec3(60.0, 0.0, 60.0)    # |d| = 85 < 100
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == "[E] Activate"


def test_a_spent_once_use_trigger_stops_advertising_itself():
    """It can no longer do anything, so it must not keep offering."""
    logic = _logic(player_pos=(0, 0, 60))
    _use_trigger(logic, trigger_type='once')
    logic.player.angle = math.pi          # facing the trigger at the origin

    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == "[E] Activate"

    logic.fired_once_triggers.add(1)
    logic.current_hud_message = ''
    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == ''


def test_a_disabled_use_trigger_shows_no_prompt():
    logic = _logic(player_pos=(0, 0, 60))
    brush = _use_trigger(logic)
    brush['disabled'] = True
    logic.player.angle = math.pi          # facing the trigger at the origin

    logic._handle_triggers(False, 1.0 / 60.0)
    assert logic.current_hud_message == ''


def test_interaction_prompt_survives_a_full_poll_window():
    """Not just the frames between polls: the prompt must survive the poll too."""
    logic = _logic()
    logic._trigger_use_prompt = ""
    logic.current_hud_message = "[E] Drop"

    for _ in range(120):  # two full 1 Hz poll windows
        logic._handle_triggers(False, 1.0 / 60.0)

    assert logic.current_hud_message == "[E] Drop"
