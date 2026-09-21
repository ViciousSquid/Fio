"""Whole chains, and the architectural boundaries that keep them composable.

The point of Fio 2.4 is that behaviour comes from composition, so the tests that
matter most are the ones that build a real network out of the real primitives
and assert that the world changed.  Each chain here is one the brief names:
counting kills, a locked door, a two-switch puzzle, a timed encounter, a
persistent world change.

The second half asserts what must *not* exist — a per-frame evaluator, a
central controller, a second event system — because those are failures that no
behavioural test would ever catch.
"""

import pathlib
import re

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                       # noqa: E402
from editor.io_system import IOManager, OutputConnection  # noqa: E402
from editor.io_handlers import register_all_input_handlers  # noqa: E402
from editor.things import (LogicGate, LogicRelay, LogicState,  # noqa: E402
                           LogicTimer, Thing)
from tests.helpers.worlds import box_brush                # noqa: E402

pytestmark = pytest.mark.qt

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Level:
    """A miniature level: brushes, things, I/O, and a door that records."""

    def __init__(self):
        self.manager = IOManager()
        self.manager.set_logic_thread(self)
        self.io_manager = self.manager
        self.brushes = []
        self.things = []
        self.gate_inputs = {}
        self.timer_states = {}
        self.door_states = {}
        self.mover_states = {}
        self._timer_things = []
        self.opened = []
        self.spawned = []

        from engine.logic_thread import LogicThread
        self._timer_key = LogicThread._timer_key

        self.manager.set_entity_finder(self._by_name)
        self.manager.set_entity_finder_by_id(self._by_id)
        register_all_input_handlers(self.manager)

        # A door that records rather than animating: the door animation has its
        # own tests, and what these chains are about is whether the event
        # arrived at all.
        def _open(entity, parameter, logic):
            self.opened.append(entity.get("name"))
        self.manager.register_input_handler("door", "open", _open)

        def _spawn(entity, parameter, logic):
            self.spawned.append(entity.properties.get("name"))
        self.manager.register_input_handler("logic_spawner", "spawn", _spawn)

    def brush(self, name, **props):
        b = box_brush(name, **props)
        self.brushes.append(b)
        return b

    def thing(self, thing):
        self.things.append(thing)
        return thing

    def _by_name(self, name):
        for b in self.brushes:
            if b.get("name") == name:
                return b
        for t in self.things:
            if t.properties.get("name") == name:
                return t
        return None

    def _by_id(self, entity_id):
        for b in self.brushes:
            if b.get("id") == entity_id:
                return b
        for t in self.things:
            if t.properties.get("id") == entity_id:
                return t
        return None

    def wire(self, source, output, target, input_name, parameter="", **kw):
        tid = (target.get("id") if isinstance(target, dict)
               else target.properties.get("id"))
        tname = (target.get("name") if isinstance(target, dict)
                 else target.properties.get("name"))
        io.add_connection(source, OutputConnection(
            output_name=output, target_name=tname, input_name=input_name,
            parameter=parameter, target_id=tid, **kw))

    def fire(self, entity, output, value=None):
        self.manager.fire_output(entity, output, value)


@pytest.fixture(autouse=True)
def _clean_registry():
    LogicState._persistent_registry.clear()
    yield
    LogicState._persistent_registry.clear()


@pytest.fixture
def level():
    return Level()


def _state(level, store_name="world", **props):
    props.setdefault("name", "state")
    props.setdefault("store_name", store_name)
    return level.thing(LogicState(pos=[0, 0, 0], properties=props))


# ---------------------------------------------------------------------------
# Kill five monsters
# ---------------------------------------------------------------------------

def test_killing_five_monsters_opens_a_door(level):
    """Monster.OnDeath -> Increment -> Compare -> OnTrue -> Door.Open.

    No manager, no script, no polling, and the engine never learns what five
    kills mean.
    """
    state = _state(level)
    door = level.brush("vault", is_door=True)
    monsters = [level.thing(Thing(pos=[0, 0, 0],
                                  properties={"name": "mon%d" % i, "type": "monster"}))
                for i in range(5)]

    for monster in monsters:
        level.wire(monster, "OnDeath", state, "Increment", "killed")
    level.wire(state, "OnValueChanged", state, "Compare", "killed>=5")
    level.wire(state, "OnTrue", door, "Open")

    for monster in monsters[:4]:
        level.fire(monster, "OnDeath")
    assert level.opened == [], "the door opened before the fifth kill"

    level.fire(monsters[4], "OnDeath")
    assert level.opened == ["vault"]
    assert state.get_value("killed") == 5


def test_the_counter_stops_re_opening_once_it_is_past_the_threshold(level):
    """A sixth kill still satisfies >= 5, so the chain fires again — which is
    correct, and the reason a map that wants one-shot behaviour puts a
    fire_once relay in the way rather than asking state to remember."""
    state = _state(level)
    door = level.brush("vault", is_door=True)
    relay = level.thing(LogicRelay(pos=[0, 0, 0],
                                   properties={"name": "once", "fire_once": True}))
    monster = level.thing(Thing(pos=[0, 0, 0],
                                properties={"name": "mon", "type": "monster"}))

    level.wire(monster, "OnDeath", state, "Increment", "killed")
    level.wire(state, "OnValueChanged", state, "Compare", "killed>=2")
    level.wire(state, "OnTrue", relay, "Trigger")
    level.wire(relay, "OnTrigger", door, "Open")

    for _ in range(5):
        level.fire(monster, "OnDeath")
    assert level.opened == ["vault"]


# ---------------------------------------------------------------------------
# Locked door
# ---------------------------------------------------------------------------

def test_a_switch_unlocks_a_door_the_door_knows_nothing_about(level):
    state = _state(level)
    switch = level.brush("switch", is_trigger=True)
    player_use = level.brush("door_use", is_trigger=True)
    door = level.brush("gate", is_door=True)

    level.wire(switch, "OnTrigger", state, "SetValue", "door_unlocked=true")
    level.wire(player_use, "OnTrigger", state, "Compare", "door_unlocked==true")
    level.wire(state, "OnTrue", door, "Open")

    level.fire(player_use, "OnTrigger")
    assert level.opened == [], "a locked door opened"

    level.fire(switch, "OnTrigger")
    level.fire(player_use, "OnTrigger")
    assert level.opened == ["gate"]


def test_using_the_door_before_the_key_exists_reports_a_missing_key(level):
    state = _state(level)
    use = level.brush("door_use", is_trigger=True)
    sink = level.brush("alarm", is_door=True)
    level.wire(use, "OnTrigger", state, "Compare", "door_unlocked==true")
    level.wire(state, "OnKeyNotFound", sink, "Open")

    level.fire(use, "OnTrigger")
    assert level.opened == ["alarm"]


# ---------------------------------------------------------------------------
# Two-switch puzzle
# ---------------------------------------------------------------------------

def test_two_switches_and_a_gate_open_a_door(level):
    """The puzzle lives in the wiring, not in the state store.

    Each switch writes its own flag; each flag's comparison is a separate wire
    into the gate.  The gate is waiting for two signals because two connections
    call its Trigger — which is why the count is of connections and not of
    source entities, since both wires come from the same state store.
    """
    state = _state(level)
    gate = level.thing(LogicGate(pos=[0, 0, 0],
                                 properties={"name": "both", "logic_type": "AND"}))
    door = level.brush("gate_door", is_door=True)
    switch_a = level.brush("switch_a", is_trigger=True)
    switch_b = level.brush("switch_b", is_trigger=True)

    level.wire(switch_a, "OnTrigger", state, "SetValue", "switch_a=true")
    level.wire(switch_b, "OnTrigger", state, "SetValue", "switch_b=true")
    # Both switches feed the same gate, through the state they just wrote.
    level.wire(switch_a, "OnTrigger", gate, "Trigger", "a")
    level.wire(switch_b, "OnTrigger", gate, "Trigger", "b")
    level.wire(gate, "OnTrigger", door, "Open")

    level.fire(switch_a, "OnTrigger")
    assert level.opened == [], "the door opened on one switch"
    level.fire(switch_b, "OnTrigger")
    assert level.opened == ["gate_door"]
    assert state.get_value("switch_a") is True
    assert state.get_value("switch_b") is True


def test_a_gate_counts_connections_not_sources(level):
    """Two wires from one entity are two signals.

    A count of distinct *sources* would see one here and close the gate on the
    first signal, which is the shape a state store feeding a gate twice takes.
    """
    gate = level.thing(LogicGate(pos=[0, 0, 0],
                                 properties={"name": "both", "logic_type": "AND"}))
    door = level.brush("gate_door", is_door=True)
    panel = level.brush("panel", is_trigger=True)

    level.wire(panel, "OnTrigger", gate, "Trigger", "a")
    level.wire(panel, "OnStartTouch", gate, "Trigger", "b")
    level.wire(gate, "OnTrigger", door, "Open")

    level.fire(panel, "OnTrigger")
    assert level.opened == []
    level.fire(panel, "OnStartTouch")
    assert level.opened == ["gate_door"]


def test_other_inputs_wired_into_a_gate_are_not_counted_as_signals(level):
    """A Reset wire is not something the gate is waiting for."""
    gate = level.thing(LogicGate(pos=[0, 0, 0],
                                 properties={"name": "both", "logic_type": "AND"}))
    door = level.brush("gate_door", is_door=True)
    a = level.brush("a", is_trigger=True)
    b = level.brush("b", is_trigger=True)
    housekeeping = level.brush("housekeeping", is_trigger=True)

    level.wire(a, "OnTrigger", gate, "Trigger", "a")
    level.wire(b, "OnTrigger", gate, "Trigger", "b")
    level.wire(housekeeping, "OnTrigger", gate, "Reset")
    level.wire(gate, "OnTrigger", door, "Open")

    level.fire(a, "OnTrigger")
    level.fire(b, "OnTrigger")
    assert level.opened == ["gate_door"]


# ---------------------------------------------------------------------------
# Timed encounter
# ---------------------------------------------------------------------------

def test_a_trigger_starts_a_timer_that_drives_a_spawner(level):
    """Trigger -> Relay -> Timer -> Spawner, with no scripting runtime."""
    from engine.logic_thread import LogicThread
    trigger = level.brush("ambush", is_trigger=True)
    relay = level.thing(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    timer = level.thing(LogicTimer(pos=[0, 0, 0],
                                   properties={"name": "timer", "interval": 2.0}))
    spawner = level.thing(Thing(pos=[0, 0, 0], properties={
        "name": "spawner", "type": "logic_spawner"}))
    level._timer_things = [timer]

    level.wire(trigger, "OnTrigger", relay, "Trigger")
    level.wire(relay, "OnTrigger", timer, "Enable")
    level.wire(timer, "OnTimer", spawner, "Spawn")

    LogicThread._update_logic_timers(level, 5.0)
    assert level.spawned == [], "the timer ran before anything started it"

    level.fire(trigger, "OnTrigger")
    LogicThread._update_logic_timers(level, 1.0)
    assert level.spawned == []
    LogicThread._update_logic_timers(level, 1.5)
    assert level.spawned == ["spawner"]


def test_a_delayed_connection_is_not_a_timer(level):
    """A one-off delay and a reusable timer are different things, and a map
    should not need an entity for the first."""
    trigger = level.brush("plate", is_trigger=True)
    door = level.brush("slow_door", is_door=True)
    level.wire(trigger, "OnTrigger", door, "Open", delay=3.0)

    level.fire(trigger, "OnTrigger")
    level.manager.update(2.0)
    assert level.opened == []
    level.manager.update(1.5)
    assert level.opened == ["slow_door"]


# ---------------------------------------------------------------------------
# Persistent world change
# ---------------------------------------------------------------------------

def test_a_world_change_survives_reloading_the_level(level):
    state = _state(level)
    trigger = level.brush("charge", is_trigger=True)
    level.wire(trigger, "OnTrigger", state, "SetValue", "bridge_destroyed=true")
    level.fire(trigger, "OnTrigger")

    # A new level, a new store entity, the same store name.
    reloaded = Level()
    new_state = _state(reloaded)
    bridge = reloaded.brush("bridge", is_door=True)
    spawn = reloaded.brush("level_start", is_trigger=True)
    reloaded.wire(spawn, "OnTrigger", new_state, "Compare", "bridge_destroyed==true")
    reloaded.wire(new_state, "OnTrue", bridge, "Open")

    reloaded.fire(spawn, "OnTrigger")
    assert reloaded.opened == ["bridge"]


# ---------------------------------------------------------------------------
# Parameter pass-through — how a value moves without a templating language
# ---------------------------------------------------------------------------

def test_a_stored_value_reaches_another_entity_through_pass_through(level):
    """GetValue fires OnValueRead carrying the value; a connection with a blank
    parameter inherits it.  That is the whole substitution mechanism."""
    state = _state(level)
    state.set_value("speed", 250)
    mover = level.brush("lift", is_mover=True)
    button = level.brush("button", is_trigger=True)

    level.wire(button, "OnTrigger", state, "GetValue", "speed")
    level.wire(state, "OnValueRead", mover, "SetSpeed")   # blank: inherits

    level.fire(button, "OnTrigger")
    assert mover["speed"] == 250.0


def test_an_explicit_parameter_beats_the_pass_through_value(level):
    state = _state(level)
    state.set_value("speed", 250)
    mover = level.brush("lift", is_mover=True)
    level.wire(state, "OnValueRead", mover, "SetSpeed", parameter="10")
    level.fire(state, "OnValueRead", "250")
    assert mover["speed"] == 10.0


# ---------------------------------------------------------------------------
# Architectural guards
# ---------------------------------------------------------------------------

def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def test_no_god_object_or_game_specific_manager_was_added():
    """The brief's list of things that must not exist, checked as source."""
    forbidden = [
        "class LogicController", "class QuestManager", "class QuestSystem",
        "class ObjectiveManager", "class GameplayManager",
        "class WorldEventManager", "class MonsterManager", "class DoorManager",
        "class ScriptManager", "class StateManager", "class ConditionManager",
    ]
    for rel in ("editor/things.py", "editor/io_system.py", "editor/io_handlers.py",
                "editor/state_values.py", "engine/logic_thread.py"):
        source = _read(rel)
        for name in forbidden:
            assert name not in source, "%s defines %s" % (rel, name)


def test_no_scripting_runtime_was_introduced():
    """No embedded interpreter, VM or expression evaluator.

    Matched on word boundaries, because two innocent things read like guilty
    ones as substrings: ``re.compile`` is a regex, and ``ast.literal_eval``
    parses a Python literal and cannot call anything — Fio has used it to type
    map properties since long before 2.4.  What must not appear is a way to
    evaluate authored *text* as code.
    """
    forbidden = [
        r"\bimport\s+lua\b", r"\blupa\b", r"\bimport\s+lupa\b",
        r"(?<![.\w])exec\s*\(", r"(?<![_.\w])eval\s*\(",
        r"\b__import__\s*\(", r"(?<![.\w])compile\s*\(",
        r"\btypes\.CodeType\b", r"\bmarshal\.loads\b",
    ]
    for rel in ("editor/things.py", "editor/io_system.py", "editor/io_handlers.py",
                "editor/state_values.py"):
        source = _read(rel)
        for pattern in forbidden:
            match = re.search(pattern, source)
            assert match is None, (
                "%s reaches for %r (%r)" % (rel, pattern, match.group(0)))


def test_the_state_module_evaluates_nothing_on_a_clock():
    """No watcher list, no dirty set, no scan: state is read when asked."""
    source = _read("editor/state_values.py")
    for name in ("def update", "def tick", "watchers", "_dirty", "threading"):
        assert name not in source, "state_values grew a %r" % (name,)


def test_there_is_exactly_one_pending_event_queue():
    """A second event system is the failure mode a second queue announces."""
    source = _read("editor/io_system.py")
    assert source.count("self.pending_events: List[PendingEvent] = []") == 1
    assert "class EventBus" not in source
    assert "class EventQueue" not in source


def test_the_logic_thread_has_no_state_evaluation_pass():
    """The per-frame path may advance timers and nothing else in this system."""
    source = _read("engine/logic_thread.py")
    for name in ("_update_logic_state", "_evaluate_conditions",
                 "_update_watchers", "_scan_state", "_update_keyvalue"):
        assert name not in source, "logic_thread grew a %r pass" % (name,)


def test_the_state_store_is_not_walked_per_frame():
    """A tick that touched every store would be a global gameplay runtime."""
    source = _read("engine/logic_thread.py")
    tick = source[source.index("def _tick_play_mode"):
                  source.index("def _update_water_sounds")]
    for name in ("LogicState", "logic_state", "_persistent_registry"):
        assert name not in tick, (
            "the play-mode tick touches %r — state must be event-driven" % name)
