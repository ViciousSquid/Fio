"""Trigger detection filter tests for player/prop/monster activation."""
from types import SimpleNamespace

from engine.logic_thread import LogicThread


class Vec:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

    def __getitem__(self, index):
        return (self.x, self.y, self.z)[index]


def _logic(player_pos=(5, 5, 5), props=(), monsters=(), filters=None):
    logic = LogicThread.__new__(LogicThread)
    logic.player = SimpleNamespace(
        pos=Vec(*player_pos), angle=0.0, velocity=Vec(0, 0, 0)
    )
    logic._prop_things = list(props)
    logic._monster_things = list(monsters)
    logic._trigger_brushes = [(1, {
        'id': 'trigger_1',
        'pos': [0, 0, 0],
        'size': [20, 20, 20],
        'is_trigger': True,
        'trigger_type': 'Multiple',
        'trigger_activation': 'touch',
        'trigger_action': 'target',
        **({'trigger_filters': filters} if filters is not None else {}),
    })]
    logic._trigger_brush_by_bid = dict(logic._trigger_brushes)
    logic._trigger_entities_inside = {}
    logic.player_in_triggers = set()
    logic.fired_once_triggers = set()
    logic.hurt_trigger_timers = {}
    logic.io_manager = None
    logic.plugins = None
    logic._events = []

    def on_enter(brush, trigger_id, activator_type='player'):
        logic._events.append(('enter', activator_type))

    def on_exit(brush, trigger_id, activator_type='player'):
        logic._events.append(('exit', activator_type))

    logic._on_trigger_enter = on_enter
    logic._on_trigger_exit = on_exit
    logic._process_hurt_trigger = lambda brush, trigger_id: None
    return logic


def _thing(kind, x=5, y=5, z=5):
    return SimpleNamespace(
        pos=[x, y, z],
        properties={'type': kind, 'disabled': False},
    )


def test_trigger_defaults_to_player_only():
    prop = _thing('prop')
    monster = _thing('monster')
    logic = _logic(props=[prop], monsters=[monster])

    logic._handle_triggers(False)

    assert logic._events == []
    assert logic.player_in_triggers == {1}


def test_trigger_can_target_props_and_monsters_in_any_combination():
    prop = _thing('prop')
    monster = _thing('monster')
    logic = _logic(
        props=[prop],
        monsters=[monster],
        filters=['props', 'monsters'],
    )

    logic._handle_triggers(False)

    assert set(logic._events) == {
        ('enter', 'props'),
        ('enter', 'monsters'),
    }
    assert logic.player_in_triggers == set()


def test_trigger_filter_reentry_is_per_entity():
    prop = _thing('prop')
    logic = _logic(props=[prop], filters=['props'])

    logic._handle_triggers(False)
    assert logic._events == [('enter', 'props')]

    prop.pos = [50, 50, 50]
    logic._handle_triggers(False)
    assert logic._events == [('enter', 'props'), ('exit', 'props')]

    prop.pos = [5, 5, 5]
    logic._handle_triggers(False)
    assert logic._events == [
        ('enter', 'props'),
        ('exit', 'props'),
        ('enter', 'props'),
    ]
